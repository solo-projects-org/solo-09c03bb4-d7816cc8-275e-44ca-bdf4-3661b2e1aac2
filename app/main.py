"""FastAPI 应用入口与路由。

端点：
- GET  /healthz                       健康检查
- GET  /docs                          Swagger UI（入口）
- PUT  /api/maps/{version}            按版上传/整版更正 GeoJSON
- GET  /api/maps                      已受理版本列表（含修订号）
- GET  /api/maps/{version}            版本元数据
- POST /api/weights/jobs              提交旧版→新版权重计算任务
- GET  /api/weights/jobs              任务列表
- GET  /api/weights/jobs/{job_id}     查询并交付结果（含失效检测）
- POST /api/qc/batches                提交站点归属质控批次
- GET  /api/qc/batches                质控批次列表（可按版号过滤）
- GET  /api/qc/batches/{batch_id}     查询批次逐点结论与供数资格
- POST /api/qc/batches/{batch_id}/recalculate  基于当前地图修订重算
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import config, geo, station_qc
from .db import Database
from .geo import GeoValidationError
from .jobs import COMPLETED, FAILED, PENDING, RUNNING, STALE, JobError, JobManager
from .qc_jobs import QcManager
from .station_qc import StationQcValidationError


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class MapUploadResponse(BaseModel):
    version: str
    revision: int = Field(..., description="该版当前修订号，重传一次加 1")
    feature_count: int
    replaced_previous: bool
    message: str


class MapVersionMeta(BaseModel):
    version: str
    revision: int
    feature_count: int
    created_at: str
    updated_at: str


class JobSubmitRequest(BaseModel):
    old_version: str = Field(..., description="旧版（历史指标所在边界）版号")
    new_version: str = Field(..., description="新版边界版号")


# ----------------------------------------------------------------------
# 站点归属质控请求 / 响应模型
# ----------------------------------------------------------------------
class StationPointIn(BaseModel):
    station_id: str = Field(..., description="批内唯一站点号")
    lon: float = Field(..., description="WGS84 经度")
    lat: float = Field(..., description="WGS84 纬度")
    reported_township_id: str = Field(..., description="上报乡镇号")


class QcBatchSubmitRequest(BaseModel):
    map_version: str = Field(
        ..., description="选用的已受理地图版号（计算时钉取其修订号）"
    )
    stations: list[StationPointIn] = Field(..., min_length=1)


class QcSummary(BaseModel):
    total: int
    matched: int
    pending_review: int
    report_mismatch: int
    all_matched: bool


class QcPointOut(BaseModel):
    station_id: str
    lon: float
    lat: float
    reported_township_id: str
    status: str = Field(
        ...,
        description="matched=唯一面内部；boundary/hole/no_coverage/multi_hit 均为待核",
    )
    matched_township_id: Optional[str] = None
    covering_township_ids: list[str] = []
    containing_township_ids: list[str] = []
    reported_match: Optional[bool] = None
    reasons: list[str] = []


class QcBatchOut(BaseModel):
    batch_id: str
    map_version: str
    pinned_revision: int
    current_revision: Optional[int] = None
    status: str
    source_batch_id: Optional[str] = None
    superseded_by: Optional[str] = None
    created_at: str
    updated_at: str
    summary: QcSummary
    eligible_for_ingestion: bool = Field(
        ..., description="整批是否可供数：ready 且地图未更正且全部匹配且上报一致"
    )
    ineligible_reasons: list[str] = []
    evaluated_at: str
    error: Optional[str] = None
    points: list[QcPointOut] = []


class QcBatchSummaryOut(BaseModel):
    batch_id: str
    map_version: str
    pinned_revision: int
    current_revision: Optional[int] = None
    status: str
    source_batch_id: Optional[str] = None
    superseded_by: Optional[str] = None
    summary: QcSummary
    created_at: str


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database()
    app.state.jobs = JobManager(app.state.db)
    app.state.qc = QcManager(app.state.db)
    # 重启恢复：上次进程遗留的 recalculating 批次落 failed，
    # 其源批次保持 superseded，旧结论不会复活
    app.state.db.recover_interrupted_recalculations()
    yield
    app.state.qc.shutdown()
    app.state.jobs.shutdown()
    app.state.db.close()


app = FastAPI(
    title="乡镇边界面积权重 / 站点归属质控 API",
    description=(
        "按版受理 WGS84 乡镇面 GeoJSON，计算旧版乡镇到新版乡镇的"
        "等面积交叠权重；并提供县域站点入仓前的归属质控："
        "仅落在单个乡镇面内部的站点判为归属，边界、洞内、无覆盖、"
        "多重命中一律待核。同版地图更正后旧批次结论立即失去供数资格。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def get_qc(request: Request) -> QcManager:
    return request.app.state.qc


async def _read_geojson_body(request: Request) -> bytes:
    """同时支持 application/json 原始体与 multipart/form-data 文件上传。"""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=400,
                detail="multipart 上传需提供名为 file 的 GeoJSON 文件字段",
            )
        return await upload.read()
    return await request.body()


# ----------------------------------------------------------------------
# 基础端点
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["system"])
def healthz(request: Request):
    return {"status": "ok", "versions": len(get_db(request).list_versions())}


# ----------------------------------------------------------------------
# 地图上传 / 查询
# ----------------------------------------------------------------------
@app.put(
    "/api/maps/{version}",
    response_model=MapUploadResponse,
    tags=["maps"],
    status_code=status.HTTP_200_OK,
    summary="按版上传 GeoJSON（同版重传 = 整版更正）",
)
async def upload_map(version: str, request: Request) -> MapUploadResponse:
    if not version or len(version) > 128:
        raise HTTPException(status_code=400, detail="版号不能为空且长度不超过 128")

    raw = await _read_geojson_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="请求体为空，需要 GeoJSON FeatureCollection")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"GeoJSON 解析失败：{exc}") from exc

    # 整版校验：任何要素无效/乡镇号重复都会在这里抛错，旧版保持不动
    try:
        parsed = geo.parse_feature_collection(payload)
    except GeoValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = get_db(request)
    replaced = db.get_revision(version) is not None
    stored = geo.to_storage_geometries(parsed)
    # 单事务替换，失败回滚，旧版完整保留
    revision = db.replace_map(version, stored)

    return MapUploadResponse(
        version=version,
        revision=revision,
        feature_count=len(stored),
        replaced_previous=replaced,
        message=(
            f"版号 {version} 已整版更正（第 {revision} 次受理）"
            if replaced
            else f"版号 {version} 首次受理成功"
        ),
    )


@app.get("/api/maps", response_model=list[MapVersionMeta], tags=["maps"])
def list_maps(request: Request) -> list[MapVersionMeta]:
    rows = get_db(request).list_versions()
    return [
        MapVersionMeta(
            version=r["version"],
            revision=r["revision"],
            feature_count=r["feature_cnt"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@app.get("/api/maps/{version}", response_model=MapVersionMeta, tags=["maps"])
def get_map(version: str, request: Request) -> MapVersionMeta:
    for r in get_db(request).list_versions():
        if r["version"] == version:
            return MapVersionMeta(
                version=r["version"],
                revision=r["revision"],
                feature_count=r["feature_cnt"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
    raise HTTPException(status_code=404, detail=f"版号不存在：{version}")


# ----------------------------------------------------------------------
# 权重计算任务
# ----------------------------------------------------------------------
@app.post(
    "/api/weights/jobs",
    tags=["weights"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交旧版→新版交叠权重计算任务",
)
def submit_job(body: JobSubmitRequest, request: Request) -> dict[str, Any]:
    db = get_db(request)
    jobs = get_jobs(request)

    if db.get_revision(body.old_version) is None:
        raise HTTPException(
            status_code=404, detail=f"旧版地图不存在：{body.old_version}"
        )
    if db.get_revision(body.new_version) is None:
        raise HTTPException(
            status_code=404, detail=f"新版地图不存在：{body.new_version}"
        )

    # 同步几何预检：新版乡镇存在正面积交叠候选 -> 立即拒绝，不建任务。
    # 定量平方米面积在工作线程的等面积投影阶段复核并报告。
    new_stored = db.get_features(body.new_version)
    from shapely.geometry import shape

    new_geoms = [(tid, shape(g)) for tid, g in (new_stored or [])]
    overlap_pairs = geo.find_overlapping_pairs(new_geoms)
    if overlap_pairs:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"新版 {body.new_version} 内乡镇之间存在 "
                    f"{len(overlap_pairs)} 处正面积交叠，拒绝计算（不能重复计权），"
                    "请更正新版后重试"
                ),
                "overlapping_pairs": [
                    {"township_a": a, "township_b": b} for a, b in overlap_pairs
                ],
            },
        )

    try:
        job = jobs.submit(body.old_version, body.new_version)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.public()


@app.get(
    "/api/weights/jobs",
    tags=["weights"],
    summary="列出全部计算任务",
)
def list_jobs(request: Request) -> dict[str, Any]:
    return {"jobs": get_jobs(request).list_jobs()}


@app.get(
    "/api/weights/jobs/{job_id}",
    tags=["weights"],
    summary="查询任务并交付结果（源版变化则拒绝交付）",
)
def get_job(job_id: str, request: Request):
    jobs = get_jobs(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")

    # 交付前失效检测
    job = jobs.deliver(job_id)
    payload = job.public()

    if job.state in (PENDING, RUNNING):
        return payload  # 200 + state=pending/running，客户端轮询即可
    if job.state == COMPLETED:
        return payload
    if job.state == STALE:
        raise HTTPException(status_code=409, detail=payload)
    if job.state == FAILED:
        raise HTTPException(status_code=422, detail=payload)
    return payload


# ----------------------------------------------------------------------
# 站点归属质控
# ----------------------------------------------------------------------
@app.post(
    "/api/qc/batches",
    response_model=QcBatchOut,
    tags=["station-qc"],
    status_code=status.HTTP_201_CREATED,
    summary="提交站点归属质控批次（同步出结论并留存）",
)
def submit_qc_batch(
    body: QcBatchSubmitRequest, request: Request
) -> QcBatchOut:
    db = get_db(request)
    qc = get_qc(request)

    revision = db.get_revision(body.map_version)
    if revision is None:
        raise HTTPException(
            status_code=404, detail=f"地图版号不存在：{body.map_version}"
        )

    # 显式再校验一次：Pydantic 已保证结构/类型，这里覆盖批内唯一性、
    # WGS84 范围、批大小等业务约束并给出中文错误
    raw_stations = [s.model_dump() for s in body.stations]
    try:
        stations = station_qc.validate_station_points(
            raw_stations, config.MAX_QC_POINTS
        )
    except StationQcValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # manager 在 DB 锁内快照修订号 + 几何，与同版更正互斥
    batch_id = qc.submit_initial(body.map_version, stations)
    payload = qc.build_batch_payload(batch_id)
    return QcBatchOut(**payload)


@app.get(
    "/api/qc/batches",
    response_model=list[QcBatchSummaryOut],
    tags=["station-qc"],
    summary="列出质控批次（可按 map_version 过滤，其他版本照常查询）",
)
def list_qc_batches(
    request: Request, map_version: Optional[str] = None
) -> list[QcBatchSummaryOut]:
    return [
        QcBatchSummaryOut(**row)
        for row in get_qc(request).list_batch_summaries(map_version)
    ]


@app.get(
    "/api/qc/batches/{batch_id}",
    response_model=QcBatchOut,
    tags=["station-qc"],
    summary="查询批次：逐点归属、问题原因与整批供数资格",
)
def get_qc_batch(batch_id: str, request: Request) -> QcBatchOut:
    payload = get_qc(request).build_batch_payload(batch_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"质控批次不存在：{batch_id}")
    return QcBatchOut(**payload)


@app.post(
    "/api/qc/batches/{batch_id}/recalculate",
    response_model=QcBatchOut,
    tags=["station-qc"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="基于当前地图修订重算（旧批次同事务被取代，立即失去供数资格）",
)
def recalculate_qc_batch(batch_id: str, request: Request) -> QcBatchOut:
    qc = get_qc(request)
    try:
        new_batch_id = qc.start_recalculation(batch_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"质控批次不存在：{exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    payload = qc.build_batch_payload(new_batch_id)
    return QcBatchOut(**payload)
