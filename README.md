# 乡镇边界面积权重 / 站点归属质控 API

两块能力：
1. **面积权重**：把旧版乡镇面映射到新版边界，为历史指标的跨版汇总提供面积权重；
2. **站点归属质控**：县域站点数据入仓前，核对上报乡镇号与当前区划，逐点判定归属。

技术栈：FastAPI + SQLite + Shapely 2 + pyproj。

## 站点归属质控业务规则

| 规则 | 实现方式 |
| --- | --- |
| 提交一批唯一站点号、WGS84 坐标、上报乡镇号，并选用已受理地图版号 | `POST /api/qc/batches`，同步出结论并落库；批内站点号重复/坐标越界/空批 -> 400，版号不存在 -> 404 |
| 只有落在**单个乡镇面内部**才判归属 `matched` | Shapely `contains` 唯一命中且无其他面覆盖 |
| 边界、洞内、无覆盖、多重命中一律**待核**，不能当作匹配 | 状态分别为 `boundary` / `hole` / `no_coverage` / `multi_hit`，逐点给出原因 |
| 上报乡镇号与几何归属不一致单列 | 匹配点上 `reported_match=false` 并计入 `report_mismatch`，阻断整批供数 |
| 返回逐点归属、问题原因及整批是否可供数 | 每点含 status / matched_township_id / 覆盖与内部命中乡镇号 / reasons；批次含 `eligible_for_ingestion` |
| 批次和结论留存供重查 | 批次元数据、入参、逐点结论全部持久化在 SQLite（`qc_batches` / `qc_points`），重启不丢 |
| 同版地图更正后，相关批次旧结论**立即**失去供数资格 | 批次钉取受理时的 `revision`；每次查询动态比对当前修订号，不一致即 `stale`、不可供数，无需额外写状态 |
| 重算须读取一致的地图修订 | 工作线程在 DB 写锁内一次性快照“修订号 + 该版全部几何”，与整版更正互斥 |
| 重算期间再更正、重算失败或重启，都不能使旧结论重新可用 | 重算生成新批次，源批次在同一事务内置 `superseded`（永不复活）；新批次交付时若修订又变则 `stale`；失败落 `failed`；重启把遗留 `recalculating` 落 `failed` |
| 其他版本的批次照常查询 | 失效只影响钉取该版号的批次；列表支持 `?map_version=` 过滤 |

### 归属判定口径（拓扑，不做投影）

| 状态 | 拓扑条件 |
| --- | --- |
| `matched` | 恰好一个乡镇面 `contains` 该点，且无其他面覆盖 |
| `boundary` | 点在某个面的边界上（被 `covers` 但不被 `contains`，且仅一个面覆盖）；外环与内环（洞）边缘都算 |
| `hole` | 点不被任何面实体覆盖，但被某个面的外环包围（严格位于洞内，不在洞边缘上） |
| `multi_hit` | 点被两个及以上乡镇面覆盖（交叠区内部或共同边界），归属不唯一 |
| `no_coverage` | 点不被任何乡镇面外环包围 |

### 批次状态机

```
ready ──同版地图更正──> 查询时动态显示 stale（数据行仍为 ready，钉取修订号已过期）
  │
  └──发起重算──> [新批次 recalculating，源批次同事务 superseded]
                        ├── 计算成功且修订未再变 -> ready
                        ├── 计算期间地图再更正   -> ready 落库，查询时显示 stale
                        ├── 计算失败             -> failed（可再次发起重算）
                        └── 进程重启             -> 启动恢复落 failed
```

`superseded` 是终态：旧结论永久失去供数资格，不会因重算失败、再次更正或重启而回退。
`eligible_for_ingestion=true` 当且仅当：`status=ready`（动态校验修订号未变）
且全部点 `matched` 且无上报乡镇号不一致。

## 面积权重功能与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 按版上传 WGS84 GeoJSON（Polygon / MultiPolygon），要素带唯一乡镇号 | `PUT /api/maps/{version}`，支持 `application/json` 与 `multipart/form-data` 文件上传 |
| 同版重传 = 整版更正 | 单事务先删后插，版本修订号 `revision` 每次 +1 |
| 无效面 / 重复乡镇号 / 非面几何 / 缺乡镇号 / 坐标越界 → 拒收并保留旧版 | 整版先校验后入库，失败返回 400，数据库回滚 |
| 等面积坐标计算 | 以数据中心构造 Albers 等面积投影（AEA，单位米），两版在同一 CRS 下计算 |
| 逐个旧乡镇返回与各新乡镇的交叠面积、占旧乡镇比例、未覆盖面积 | `POST /api/weights/jobs` 异步任务 + `GET /api/weights/jobs/{id}` 交付 |
| 洞内面积、仅共边部分不计入 | Shapely 面交集天然扣除内环；共边交集面积为 0，被 `POSITIVE_AREA_EPS_M2` 阈值过滤 |
| 新版乡镇彼此正面积交叠 → 拒绝计算，不能重复计权 | 提交时几何预检 409；工作线程在等面积投影后按平方米复核（双保险） |
| 计算期间仍可更正边界 | 计算在线程池执行，上传接口不被阻塞 |
| 任一源版变化 → 拒绝交付旧权重并提示重试 | 提交时钉取 `revision`，交付前比对当前修订号，变化则任务置 `stale` 并返回 409 |
| 重启后保留已受理地图 | 地图持久化在 SQLite（WAL）；任务为可重算的临时态，仅存内存 |

## 目录结构

```
app/
  config.py      环境变量配置（DB_PATH、面积阈值、质控批大小等）
  db.py          SQLite 持久化：maps / map_features / qc_batches / qc_points，
                 事务化整版替换、重算取代、重启恢复
  geo.py         GeoJSON 校验、AEA 等面积投影、互斥检查、交叠权重计算
  station_qc.py  站点入参校验、点-面拓扑判定（内部/边界/洞/无覆盖/多重命中）、整批质控
  jobs.py        权重后台任务、版本修订号钉取、交付失效检测
  qc_jobs.py     质控批次生命周期：同步提交、异步重算、一致快照、供数资格动态判定
  main.py        FastAPI 路由与 Swagger
tests/           pytest 套件（59 个用例）
Dockerfile
docker-compose.yml
```

## 快速启动（Docker Compose）

```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs   （根路径 / 自动跳转）
# 健康检查:    http://localhost:8000/healthz
```

SQLite 数据存放在命名卷 `township_data`（容器内 `/data/weighting.db`），
容器重建后已受理地图不丢失。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：DB_PATH=/your/path.db uvicorn app.main:app ...
```

## API 用法示例

### 1. 上传 / 整版更正

```bash
curl -X PUT http://localhost:8000/api/maps/2010版 \
  -H "Content-Type: application/json" \
  --data-binary @old.geojson
# 200 -> {"version":"2010版","revision":1,"feature_count":12,
#         "replaced_previous":false,"message":"...首次受理成功"}
```

GeoJSON 要素属性中的乡镇号字段取 `township_code`，其次 `code`，
值会统一转为字符串。再次 `PUT` 同版号即整版更正（`revision` 递增）；
若 payload 不合法，返回 400 且**旧版原样保留**。

### 2. 提交权重计算任务

```bash
curl -X POST http://localhost:8000/api/weights/jobs \
  -H "Content-Type: application/json" \
  -d '{"old_version":"2010版","new_version":"2020版"}'
# 202 -> {"job_id":"...","state":"pending","pinned_revisions":{...}}
```

### 3. 轮询并交付

```bash
curl http://localhost:8000/api/weights/jobs/{job_id}
```

`state` 为 `pending/running` 时继续轮询；`completed` 时返回：

```json
{
  "state": "completed",
  "result": {
    "area_unit": "m2",
    "crs": "PROJCRS[...Albers 等面积...]",
    "weights": [
      {
        "old_township_id": "OLD_A",
        "old_area_m2": 4277873.39,
        "covered_area_m2": 4277839.67,
        "uncovered_area_m2": 33.71,
        "coverage_ratio": 0.999992,
        "overlaps": [
          {"new_township_id": "NEW_1",
           "intersection_area_m2": 2851820.49,
           "ratio_of_old": 0.666644},
          {"new_township_id": "NEW_2",
           "intersection_area_m2": 1426019.18,
           "ratio_of_old": 0.333348}
        ]
      }
    ]
  }
}
```

- `ratio_of_old = 交叠面积 / 旧乡镇面积`，历史指标汇总时直接作为权重；
- 若源版在交付前被更正：返回 **409 + `state=stale`**，并在
  `error_detail.changed_versions` 中给出钉取/当前修订号，用同版号重新提交即可；
- 新版内部交叠：提交即 **409**，给出冲突乡镇号配对。

### 4. 提交站点归属质控批次

```bash
curl -X POST http://localhost:8000/api/qc/batches \
  -H "Content-Type: application/json" \
  -d '{"map_version":"2020版","stations":[
    {"station_id":"ST001","lon":113.12,"lat":29.34,"reported_township_id":"T1"},
    {"station_id":"ST002","lon":113.50,"lat":29.80,"reported_township_id":"T2"}
  ]}'
# 201 ->
# {
#   "batch_id": "qc_...", "map_version": "2020版",
#   "pinned_revision": 1, "current_revision": 1, "status": "ready",
#   "eligible_for_ingestion": true, "ineligible_reasons": [],
#   "summary": {"total":2,"matched":2,"pending_review":0,
#               "report_mismatch":0,"all_matched":true},
#   "points": [{"station_id":"ST001","status":"matched",
#               "matched_township_id":"T1","reported_match":true, ...}]
# }
```

待核点示例（边界点）：

```json
{"station_id":"ST003","status":"boundary","matched_township_id":null,
 "covering_township_ids":["T1"],"reported_match":null,
 "reasons":["点落在乡镇 T1 面的边界上，不属于面内部"]}
```

只要存在待核点或上报不一致，`eligible_for_ingestion=false` 并在
`ineligible_reasons` 说明原因。

### 5. 查询批次 / 列表（重查入口）

```bash
curl http://localhost:8000/api/qc/batches/{batch_id}      # 逐点结论 + 供数资格
curl "http://localhost:8000/api/qc/batches?map_version=2020版"  # 按版号过滤
```

同版地图一旦被整版更正（`PUT /api/maps/{version}`），再查旧批次即返回
`status=stale`、`eligible_for_ingestion=false`，原因注明钉取/当前修订号；
其他版本的批次不受影响，仍为 `ready` 且可正常供数。

### 6. 地图更正后重算

```bash
curl -X POST http://localhost:8000/api/qc/batches/{batch_id}/recalculate
# 202 -> 新批次（status=recalculating）；源批次同事务被置 superseded
```

- 重算在后台线程执行，读取的是 DB 锁内一致快照（同一修订号的全部几何）；
- 完成后 `GET` 新批次：若期间地图未再更正则 `ready`，否则显示 `stale`；
- 源批次此后恒为 `superseded`，**重算失败、期间再更正、进程重启都不会让它复活**；
- 重算失败的批次状态为 `failed`，允许对其再次发起重算（`superseded`/
  `recalculating` 状态拒绝重算，返回 409）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/weighting.db`（容器内） | SQLite 文件路径 |
| `POSITIVE_AREA_EPS_M2` | `1e-6` | 平方米阈值，低于此交叠面积视为仅共边/数值噪声 |
| `MAX_WORKERS` | `4` | 后台计算线程数（权重任务与质控重算共用） |
| `MAX_QC_POINTS` | `10000` | 单个质控批次允许的最大站点数 |

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖（59 个用例）：
- 原有：上传校验与拒收保留、整版更正修订号、重复乡镇号、无效面、WGS84 范围、
  洞面积扣除、仅共边不计、未覆盖面积、等面积投影精度、新版互斥 409、
  计算期间更正的 stale 409 与重试、重启持久化等 32 个；
- 新增站点归属质控 27 个，含：内部/边界/角点/洞内/无覆盖/共边多重命中/
  交叠区多重命中/MultiPolygon 等拓扑分支、STRtree 与暴力路径一致性、
  入参校验（重复站点号、越界、NaN、空批、超限）、上报不一致阻断供数、
  更正即失效且旧结论留存、其他版本批次照常查询、重算取代链、
  **重算期间再次更正的 stale 交错**、重算失败不复活旧结论且可重试、
  状态守卫 409/404、重启后批次留存与 `recalculating` 遗留恢复。

## 设计说明与已知边界

- **投影选择**：Albers 等面积投影保证面积不失真，中央经线/纬度原点取两版
  并集质心，标准纬线相对原点 ±5°，适合县域尺度；面积与 pyproj `Geod`
  大地测量面积的相对误差 < 0.1%。
- **浮点缝隙**：相邻边界经重投影后可能出现极窄缝隙（实测约 1e-5 量级比例），
  表现为小额 `uncovered_area_m2`，可通过阈值或后续边界预处理（坐标对齐）控制。
- **任务态不持久化**：权重任务结果是可由地图重新派生的临时产物，进程重启后
  需重新提交；已受理地图本身持久化，不受重启影响。
- **质控批次全量持久化**：与权重任务不同，质控批次的入参与逐点结论是入仓核对
  凭据，必须留存供重查，因此落 SQLite；仅“重算中”这一过渡态在进程死亡后由
  启动恢复落 `failed`（源批次保持 `superseded`，旧结论不复活）。
- **拓扑判定不投影**：点在面内部/边界/洞内是拓扑性质，与投影无关，直接在
  WGS84 几何上用 Shapely 谓词判定；STRtree 只做候选裁剪，最终结论仍以
  `contains`/`covers` 谓词为准。浮点坐标恰好压在边界上的点按业务口径一律
  待核（`boundary`/`multi_hit`），不会被当作匹配。
- **供数资格是查询期派生值**：批次表只存受理时钉取的修订号，不随地图更正
  主动改写状态；`stale` 在每次查询时与当前修订号比对动态得出，因此“更正后
  立即失效”不依赖任何后台回调或事件。
- 超大图层（数十万要素）建议前置分片/抽稀；当前实现已使用 STRtree 空间索引
  避免配对计算 O(n²) 化，但单任务仍为单机线程模型。
