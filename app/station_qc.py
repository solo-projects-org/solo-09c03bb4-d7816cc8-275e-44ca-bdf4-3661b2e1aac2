"""站点归属质控：点-乡镇面拓扑判定与批次计算。

业务口径（只有第一种算“归属”，其余一律待核，不能当作匹配）：

1. matched      点落在唯一一个乡镇面的*内部*（Shapely contains 为真且仅一个）；
2. boundary     点落在任一面的边界上（没有任何面 contains，但有面 covers）；
3. hole         点落在某面的洞内（被面 covers 却不被 contains，且无面 contains）；
4. no_coverage  点不被任何乡镇面覆盖；
5. multi_hit    点同时被两个及以上乡镇面覆盖（面之间正面积交叠区域内的点）。

判定全部基于 WGS84 经纬度拓扑关系，不做投影（点在面内/边界上的拓扑结论
与投影无关）；面积类计算才需要等面积投影。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

# 逐点状态
MATCHED = "matched"
BOUNDARY = "boundary"
HOLE = "hole"
NO_COVERAGE = "no_coverage"
MULTI_HIT = "multi_hit"

# 待核（非匹配）状态集合
PENDING_STATUSES = (BOUNDARY, HOLE, NO_COVERAGE, MULTI_HIT)

# 上报乡镇号与几何归属不一致的附加原因码
REPORT_MISMATCH = "reported_township_mismatch"


class StationQcValidationError(ValueError):
    """批次请求不合法（HTTP 400）。"""


@dataclass
class PointQC:
    """单个站点的拓扑判定中间结果。"""

    status: str
    matched_township_id: Optional[str] = None
    covering_township_ids: list[str] = field(default_factory=list)
    containing_township_ids: list[str] = field(default_factory=list)
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "matched_township_id": self.matched_township_id,
            "covering_township_ids": list(self.covering_township_ids),
            "containing_township_ids": list(self.containing_township_ids),
            "reason": self.reason,
        }


# ----------------------------------------------------------------------
# 请求校验
# ----------------------------------------------------------------------
def validate_station_points(points: list[dict[str, Any]], max_points: int) -> list[dict[str, str]]:
    """校验批次入参并归一化。

    - 批次非空且不超过 max_points；
    - 站点号 / 上报乡镇号为非空字符串，批内站点号唯一；
    - 经纬度为有限数值且落在 WGS84 范围内。
    返回 [{"station_id", "lon", "lat", "reported_township_id"}, ...]。
    """
    if not isinstance(points, list) or not points:
        raise StationQcValidationError("stations 不能为空，至少提交一个站点")
    if len(points) > max_points:
        raise StationQcValidationError(
            f"单批站点数 {len(points)} 超过上限 {max_points}"
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(points):
        if not isinstance(item, dict):
            raise StationQcValidationError(f"第 {idx} 个站点必须是对象")

        station_id = str(item.get("station_id", "")).strip()
        if not station_id:
            raise StationQcValidationError(f"第 {idx} 个站点缺少站点号 station_id")
        if station_id in seen:
            raise StationQcValidationError(f"站点号在批内重复：{station_id}")
        seen.add(station_id)

        reported = item.get("reported_township_id")
        reported_id = "" if reported is None else str(reported).strip()
        if not reported_id:
            raise StationQcValidationError(
                f"站点 {station_id} 缺少上报乡镇号 reported_township_id"
            )

        lon = item.get("lon")
        lat = item.get("lat")
        if isinstance(lon, bool) or isinstance(lat, bool) or not isinstance(
            lon, (int, float)
        ) or not isinstance(lat, (int, float)):
            raise StationQcValidationError(
                f"站点 {station_id} 的经纬度必须是数值（lon/lat）"
            )
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise StationQcValidationError(
                f"站点 {station_id} 的经纬度不能是 NaN/Infinity"
            )
        if not (-180.0 <= lon <= 180.0):
            raise StationQcValidationError(
                f"站点 {station_id} 经度 {lon} 超出 WGS84 范围 [-180, 180]"
            )
        if not (-90.0 <= lat <= 90.0):
            raise StationQcValidationError(
                f"站点 {station_id} 纬度 {lat} 超出 WGS84 范围 [-90, 90]"
            )

        normalized.append(
            {
                "station_id": station_id,
                "lon": float(lon),
                "lat": float(lat),
                "reported_township_id": reported_id,
            }
        )
    return normalized


# ----------------------------------------------------------------------
# 单点拓扑判定
# ----------------------------------------------------------------------
def _build_strtree(geoms: list[BaseGeometry]):
    try:
        from shapely.strtree import STRtree

        return STRtree(geoms)
    except Exception:
        return None


def classify_point(
    point: Point,
    features: list[tuple[str, BaseGeometry]],
    tree=None,
) -> PointQC:
    """按业务口径判定单点相对一组乡镇面的归属。

    拓扑语义（Shapely）：
    - contains：点在面*内部*，边界线与洞内均为假；
    - covers：点在面实体上（内部或边界），洞内为假；
    - 用外环单独构造的无洞 Polygon 做 covers：点被外环包围即为真
      （内部、内环/外环边界、洞内都算），用于把“洞内点”与“无覆盖”区分。
    """
    if tree is not None:
        candidate_idx = [int(i) for i in tree.query(point)]
    else:
        candidate_idx = list(range(len(features)))

    containing: list[str] = []
    covering: list[str] = []
    outer_covering: list[str] = []
    for i in candidate_idx:
        tid, geom = features[i]
        if geom.contains(point):
            containing.append(tid)
        if geom.covers(point):
            covering.append(tid)
        polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polygons:
            if not isinstance(poly, Polygon):
                continue
            # 外环包围判定：忽略内环（洞），仅看外环覆盖范围
            if Polygon(poly.exterior).covers(point):
                outer_covering.append(tid)
                break

    # 1) 唯一内部命中：恰有一个面 contains，且没有其他面覆盖该点
    if len(containing) == 1 and len(covering) == 1 and containing[0] == covering[0]:
        return PointQC(
            status=MATCHED,
            matched_township_id=containing[0],
            covering_township_ids=covering,
            containing_township_ids=containing,
        )

    # 2) 多重命中：被两个及以上乡镇面实体覆盖（交叠区内部或共同边界），
    #    归属不唯一，不能当作匹配
    if len(covering) > 1:
        return PointQC(
            status=MULTI_HIT,
            covering_township_ids=sorted(covering),
            containing_township_ids=sorted(containing),
            reason=(
                "点同时落在多个乡镇面覆盖范围内"
                + (f"：{', '.join(sorted(covering))}" if covering else "")
            ),
        )

    # 3) 单个面 covers 但不 contains：点在该面的外边界上
    if len(covering) == 1:
        return PointQC(
            status=BOUNDARY,
            covering_township_ids=covering,
            containing_township_ids=containing,
            reason=f"点落在乡镇 {covering[0]} 面的边界上，不属于面内部",
        )

    # 4) 无面实体覆盖，但被某个外环包围：点在洞内（或内环边界上）
    if len(outer_covering) == 1:
        return PointQC(
            status=HOLE,
            covering_township_ids=[],
            containing_township_ids=containing,
            reason=(
                f"点位于乡镇 {outer_covering[0]} 面的洞内，"
                "不属于任何乡镇面内部"
            ),
        )
    if len(outer_covering) > 1:
        # 多个面的洞/外环交叠：归属仍不唯一，按多重命中待核
        return PointQC(
            status=MULTI_HIT,
            covering_township_ids=[],
            containing_township_ids=containing,
            reason=(
                "点同时被多个乡镇外环包围但不落在任何面实体内部："
                + ", ".join(sorted(outer_covering))
            ),
        )

    # 5) 完全无覆盖
    return PointQC(
        status=NO_COVERAGE,
        reason="点不被任何已受理乡镇面覆盖（无覆盖）",
    )


# ----------------------------------------------------------------------
# 整批计算
# ----------------------------------------------------------------------
def run_batch(
    stored_features: list[tuple[str, dict]],
    stations: list[dict[str, Any]],
) -> dict[str, Any]:
    """对整批站点执行归属质控。

    入参：
    - stored_features：持久层读出的 [(township_id, geojson_geom_dict), ...]；
    - stations：validate_station_points 归一化后的站点列表。
    返回 {"points": [...逐点结论...], "summary": {...}}。
    """
    features: list[tuple[str, BaseGeometry]] = [
        (tid, shape(geom_dict)) for tid, geom_dict in stored_features
    ]
    geoms = [g for _, g in features]
    tree = _build_strtree(geoms)

    point_results: list[dict[str, Any]] = []
    matched_cnt = 0
    pending_cnt = 0
    report_mismatch_cnt = 0

    for st in stations:
        point = Point(st["lon"], st["lat"])
        qc = classify_point(point, features, tree)

        reported_match: Optional[bool] = None
        reasons: list[str] = []
        if qc.reason:
            reasons.append(qc.reason)

        if qc.status == MATCHED:
            matched_cnt += 1
            reported_match = qc.matched_township_id == st["reported_township_id"]
            if not reported_match:
                report_mismatch_cnt += 1
                reasons.append(
                    f"上报乡镇号 {st['reported_township_id']} 与几何归属 "
                    f"{qc.matched_township_id} 不一致"
                )
        else:
            pending_cnt += 1

        point_results.append(
            {
                "station_id": st["station_id"],
                "lon": st["lon"],
                "lat": st["lat"],
                "reported_township_id": st["reported_township_id"],
                "status": qc.status,
                "matched_township_id": qc.matched_township_id,
                "covering_township_ids": qc.covering_township_ids,
                "containing_township_ids": qc.containing_township_ids,
                "reported_match": reported_match,
                "reasons": reasons,
            }
        )

    summary = {
        "total": len(stations),
        "matched": matched_cnt,
        "pending_review": pending_cnt,
        "report_mismatch": report_mismatch_cnt,
        "all_matched": pending_cnt == 0,
    }
    return {"points": point_results, "summary": summary}


__all__ = [
    "BOUNDARY",
    "HOLE",
    "MATCHED",
    "MULTI_HIT",
    "NO_COVERAGE",
    "PENDING_STATUSES",
    "REPORT_MISMATCH",
    "PointQC",
    "StationQcValidationError",
    "classify_point",
    "run_batch",
    "validate_station_points",
]
