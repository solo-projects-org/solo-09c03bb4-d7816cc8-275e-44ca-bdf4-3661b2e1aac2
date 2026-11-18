"""station_qc 拓扑判定单元测试：内部/边界/洞/无覆盖/多重命中。"""
from __future__ import annotations

import pytest
from shapely.geometry import Point, shape

from app import station_qc as sq
from tests.conftest import (
    feature_collection,
    multipolygon_geojson,
    rect_geojson,
    rect_with_hole,
)


def _features(*specs):
    """specs: (code, geojson_feature) -> [(code, shapely_geom), ...]"""
    return [(code, shape(feat["geometry"])) for code, feat in specs]


def test_interior_point_matches_single_township():
    feats = _features(("T1", rect_geojson("T1", 0, 0, 2, 2)))
    qc = sq.classify_point(Point(1, 1), feats)
    assert qc.status == sq.MATCHED
    assert qc.matched_township_id == "T1"


def test_boundary_point_is_pending_not_matched():
    feats = _features(("T1", rect_geojson("T1", 0, 0, 2, 2)))
    # 点正好落在右边界 x=2 上
    qc = sq.classify_point(Point(2, 1), feats)
    assert qc.status == sq.BOUNDARY
    assert qc.matched_township_id is None
    assert "边界" in qc.reason


def test_corner_point_is_boundary():
    feats = _features(("T1", rect_geojson("T1", 0, 0, 2, 2)))
    qc = sq.classify_point(Point(0, 0), feats)
    assert qc.status == sq.BOUNDARY


def test_point_in_hole_is_pending():
    feat = rect_with_hole("H1", 0, 0, 4, 4, 1, 1, 2, 2)
    feats = _features(("H1", feat))
    qc = sq.classify_point(Point(1.5, 1.5), feats)
    assert qc.status == sq.HOLE
    assert qc.matched_township_id is None
    assert "洞" in qc.reason


def test_inner_ring_edge_is_boundary_pending():
    # 洞边缘（内环）按 OGC 语义也是面边界：covers 为真、contains 为假，
    # 归为 boundary；与洞内点一样属于待核，绝不匹配
    feat = rect_with_hole("H1", 0, 0, 4, 4, 1, 1, 2, 2)
    feats = _features(("H1", feat))
    qc = sq.classify_point(Point(1.0, 1.5), feats)
    assert qc.status == sq.BOUNDARY
    assert qc.matched_township_id is None


def test_point_outside_is_no_coverage():
    feats = _features(("T1", rect_geojson("T1", 0, 0, 2, 2)))
    qc = sq.classify_point(Point(9, 9), feats)
    assert qc.status == sq.NO_COVERAGE
    assert qc.covering_township_ids == []


def test_shared_edge_point_is_multi_hit():
    # 两个乡镇恰好共边 x=1：边界点同时被两个面 covers -> 多重命中待核
    feats = _features(
        ("A", rect_geojson("A", 0, 0, 1, 1)),
        ("B", rect_geojson("B", 1, 0, 2, 1)),
    )
    qc = sq.classify_point(Point(1, 0.5), feats)
    assert qc.status == sq.MULTI_HIT
    assert set(qc.covering_township_ids) == {"A", "B"}
    assert qc.matched_township_id is None


def test_overlapping_interior_point_is_multi_hit():
    # 正面积交叠区域内部的点同时被两个面 contains
    feats = _features(
        ("A", rect_geojson("A", 0, 0, 2, 2)),
        ("B", rect_geojson("B", 1, 1, 3, 3)),
    )
    qc = sq.classify_point(Point(1.5, 1.5), feats)
    assert qc.status == sq.MULTI_HIT
    assert set(qc.containing_township_ids) == {"A", "B"}


def test_strtree_path_matches_bruteforce_path():
    feats = _features(
        ("A", rect_geojson("A", 0, 0, 1, 1)),
        ("B", rect_geojson("B", 1, 0, 2, 1)),
        ("C", rect_geojson("C", 2, 2, 4, 4)),
    )
    from shapely.strtree import STRtree

    tree = STRtree([g for _, g in feats])
    probe_points = [
        (Point(0.5, 0.5), sq.MATCHED),
        (Point(1, 0.5), sq.MULTI_HIT),
        (Point(3, 3), sq.MATCHED),
        (Point(-1, -1), sq.NO_COVERAGE),
    ]
    for pt, expected in probe_points:
        assert sq.classify_point(pt, feats, tree).status == expected
        assert sq.classify_point(pt, feats, None).status == expected


def test_multipolygon_interior_and_hole_semantics():
    # MultiPolygon 两个分散矩形
    feat = multipolygon_geojson("M1", [(0, 0, 1, 1), (5, 5, 6, 6)])
    feats = _features(("M1", feat))
    assert sq.classify_point(Point(0.5, 0.5), feats).status == sq.MATCHED
    assert sq.classify_point(Point(5.5, 5.5), feats).status == sq.MATCHED
    assert sq.classify_point(Point(3, 3), feats).status == sq.NO_COVERAGE


def test_validate_rejects_duplicate_station_id():
    with pytest.raises(sq.StationQcValidationError):
        sq.validate_station_points(
            [
                {"station_id": "S1", "lon": 0.5, "lat": 0.5,
                 "reported_township_id": "T1"},
                {"station_id": "S1", "lon": 0.6, "lat": 0.6,
                 "reported_township_id": "T1"},
            ],
            max_points=100,
        )


def test_validate_rejects_out_of_range_and_nan():
    base = {"station_id": "S1", "reported_township_id": "T1"}
    with pytest.raises(sq.StationQcValidationError):
        sq.validate_station_points(
            [{**base, "lon": 200, "lat": 0}], max_points=100
        )
    with pytest.raises(sq.StationQcValidationError):
        sq.validate_station_points(
            [{**base, "lon": 0, "lat": float("nan")}], max_points=100
        )


def test_validate_rejects_empty_and_oversized_batch():
    with pytest.raises(sq.StationQcValidationError):
        sq.validate_station_points([], max_points=100)
    with pytest.raises(sq.StationQcValidationError):
        sq.validate_station_points(
            [
                {"station_id": f"S{i}", "lon": 0, "lat": 0,
                 "reported_township_id": "T"}
                for i in range(3)
            ],
            max_points=2,
        )


def test_run_batch_summary_and_report_mismatch():
    from app import geo

    parsed = geo.parse_feature_collection(
        feature_collection(rect_geojson("T1", 0, 0, 2, 2))
    )
    stored = [(tid, g.__geo_interface__) for tid, g in parsed]
    stations = [
        # 内部命中且上报一致
        {"station_id": "S1", "lon": 1.0, "lat": 1.0,
         "reported_township_id": "T1"},
        # 内部命中但上报不一致
        {"station_id": "S2", "lon": 1.2, "lat": 1.2,
         "reported_township_id": "T9"},
        # 边界 -> 待核
        {"station_id": "S3", "lon": 2.0, "lat": 1.0,
         "reported_township_id": "T1"},
        # 无覆盖 -> 待核
        {"station_id": "S4", "lon": 8.0, "lat": 8.0,
         "reported_township_id": "T1"},
    ]
    result = sq.run_batch(stored, stations)
    summary = result["summary"]
    assert summary["total"] == 4
    assert summary["matched"] == 2
    assert summary["pending_review"] == 2
    assert summary["report_mismatch"] == 1
    assert summary["all_matched"] is False

    by_id = {p["station_id"]: p for p in result["points"]}
    assert by_id["S1"]["status"] == sq.MATCHED
    assert by_id["S1"]["reported_match"] is True
    assert by_id["S2"]["reported_match"] is False
    assert any("不一致" in r for r in by_id["S2"]["reasons"])
    assert by_id["S3"]["status"] == sq.BOUNDARY
    assert by_id["S4"]["status"] == sq.NO_COVERAGE
