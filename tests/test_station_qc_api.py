"""站点归属质控 API 端到端测试：提交、供数资格、更正失效、重算交错、重启。"""
from __future__ import annotations

import time

from tests.conftest import (
    feature_collection,
    rect_geojson,
    rect_with_hole,
)


def _upload_map(client, version="v1", revision_features=None):
    if revision_features is None:
        revision_features = [
            rect_geojson("T1", 0, 0, 2, 2),
            rect_geojson("T2", 2, 0, 4, 2),
        ]
    return client.put(
        f"/api/maps/{version}", json=feature_collection(*revision_features)
    )


def _submit_batch(client, stations, version="v1"):
    return client.post(
        "/api/qc/batches", json={"map_version": version, "stations": stations}
    )


def _wait_recalc(client, batch_id, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/qc/batches/{batch_id}")
        last = r
        if r.json()["status"] != "recalculating":
            return r
        time.sleep(0.02)
    raise AssertionError(f"重算未在 {timeout}s 内结束：{last.text if last else None}")


# ----------------------------------------------------------------------
# 基础判定
# ----------------------------------------------------------------------
def test_batch_matched_boundary_no_coverage_and_eligibility(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},          # T1 内部
        {"station_id": "S2", "lon": 3, "lat": 1,
         "reported_township_id": "T2"},          # T2 内部
        {"station_id": "S3", "lon": 2, "lat": 1,
         "reported_township_id": "T1"},          # 共边 -> multi_hit 待核
        {"station_id": "S4", "lon": 9, "lat": 9,
         "reported_township_id": "T1"},          # 无覆盖
    ]
    r = _submit_batch(client, stations)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["pinned_revision"] == 1

    by_id = {p["station_id"]: p for p in body["points"]}
    assert by_id["S1"]["status"] == "matched"
    assert by_id["S1"]["matched_township_id"] == "T1"
    assert by_id["S2"]["status"] == "matched"
    assert by_id["S3"]["status"] == "multi_hit"
    assert by_id["S4"]["status"] == "no_coverage"

    # 有待核点 -> 整批不可供数
    assert body["eligible_for_ingestion"] is False
    assert any("待核" in reason for reason in body["ineligible_reasons"])


def test_all_matched_batch_is_eligible(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
        {"station_id": "S2", "lon": 3, "lat": 1,
         "reported_township_id": "T2"},
    ]
    body = _submit_batch(client, stations).json()
    assert body["summary"] == {
        "total": 2, "matched": 2, "pending_review": 0,
        "report_mismatch": 0, "all_matched": True,
    }
    assert body["eligible_for_ingestion"] is True
    assert body["ineligible_reasons"] == []


def test_hole_and_boundary_points_never_matched(client):
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_with_hole("H1", 0, 0, 4, 4, 1, 1, 2, 2)),
    )
    stations = [
        {"station_id": "HOLE", "lon": 1.5, "lat": 1.5,
         "reported_township_id": "H1"},                 # 洞内
        {"station_id": "EDGE", "lon": 0, "lat": 2,
         "reported_township_id": "H1"},                 # 外边界
        {"station_id": "IN", "lon": 0.5, "lat": 0.5,
         "reported_township_id": "H1"},                 # 内部
    ]
    body = _submit_batch(client, stations).json()
    by_id = {p["station_id"]: p for p in body["points"]}
    assert by_id["HOLE"]["status"] == "hole"
    assert by_id["EDGE"]["status"] == "boundary"
    assert by_id["IN"]["status"] == "matched"
    assert body["eligible_for_ingestion"] is False


def test_reported_township_mismatch_blocks_ingestion(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T2"},  # 几何属 T1，上报 T2
    ]
    body = _submit_batch(client, stations).json()
    assert body["summary"]["all_matched"] is True       # 几何上确实匹配
    assert body["summary"]["report_mismatch"] == 1
    assert body["eligible_for_ingestion"] is False
    point = body["points"][0]
    assert point["status"] == "matched"
    assert point["reported_match"] is False


def test_multi_hit_from_overlapping_polygons(client):
    # 同版两个面正面积交叠（本 API 允许地图存在交叠，质控侧判 multi_hit）
    client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 2, 2),
            rect_geojson("T2", 1, 1, 3, 3),
        ),
    )
    stations = [
        {"station_id": "S1", "lon": 1.5, "lat": 1.5,
         "reported_township_id": "T1"},  # 交叠区内部
        {"station_id": "S2", "lon": 0.5, "lat": 0.5,
         "reported_township_id": "T1"},  # T1 独有内部
    ]
    body = _submit_batch(client, stations).json()
    by_id = {p["station_id"]: p for p in body["points"]}
    assert by_id["S1"]["status"] == "multi_hit"
    assert set(by_id["S1"]["containing_township_ids"]) == {"T1", "T2"}
    assert by_id["S2"]["status"] == "matched"


def test_submit_validation_errors(client):
    _upload_map(client)
    # 站点号重复
    r = _submit_batch(
        client,
        [
            {"station_id": "S1", "lon": 1, "lat": 1,
             "reported_township_id": "T1"},
            {"station_id": "S1", "lon": 1, "lat": 1,
             "reported_township_id": "T1"},
        ],
    )
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    # 经纬度越界
    r = _submit_batch(
        client,
        [{"station_id": "S2", "lon": 1, "lat": 99,
          "reported_township_id": "T1"}],
    )
    assert r.status_code == 400 and "WGS84" in r.json()["detail"]

    # 空批次（Pydantic 层）
    r = client.post(
        "/api/qc/batches", json={"map_version": "v1", "stations": []}
    )
    assert r.status_code == 422

    # 版号不存在
    r = _submit_batch(
        client,
        [{"station_id": "S3", "lon": 1, "lat": 1,
          "reported_township_id": "T1"}],
        version="nope",
    )
    assert r.status_code == 404


# ----------------------------------------------------------------------
# 更正即失效
# ----------------------------------------------------------------------
def test_map_correction_immediately_invalidates_old_batch(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    batch_id = _submit_batch(client, stations).json()["batch_id"]
    assert client.get(f"/api/qc/batches/{batch_id}").json()[
        "eligible_for_ingestion"
    ] is True

    # 同版整版更正（revision 1 -> 2）
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)),
    )
    body = client.get(f"/api/qc/batches/{batch_id}").json()
    assert body["status"] == "stale"
    assert body["current_revision"] == 2
    assert body["pinned_revision"] == 1
    assert body["eligible_for_ingestion"] is False
    assert any("更正" in r for r in body["ineligible_reasons"])
    # 旧逐点结论仍可重查留存
    assert len(body["points"]) == 1


def test_other_version_batches_still_queryable_and_eligible(client):
    _upload_map(client, version="v1")
    _upload_map(
        client,
        version="v2",
        revision_features=[rect_geojson("X1", 0, 0, 2, 2)],
    )
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    b1 = _submit_batch(client, stations, version="v1").json()["batch_id"]
    b2 = _submit_batch(
        client,
        [{"station_id": "S1", "lon": 1, "lat": 1,
          "reported_township_id": "X1"}],
        version="v2",
    ).json()["batch_id"]

    # 只更正 v1
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)),
    )
    assert client.get(f"/api/qc/batches/{b1}").json()["status"] == "stale"
    v2_body = client.get(f"/api/qc/batches/{b2}").json()
    assert v2_body["status"] == "ready"
    assert v2_body["eligible_for_ingestion"] is True

    # 列表按版号过滤
    v1_list = client.get("/api/qc/batches", params={"map_version": "v1"}).json()
    assert {b["batch_id"] for b in v1_list} == {b1}
    all_list = client.get("/api/qc/batches").json()
    assert {b["batch_id"] for b in all_list} == {b1, b2}


# ----------------------------------------------------------------------
# 重算
# ----------------------------------------------------------------------
def test_recalculation_supersedes_and_new_batch_becomes_eligible(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    old_id = _submit_batch(client, stations).json()["batch_id"]

    # 更正地图使旧批次失效
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)),
    )
    assert client.get(f"/api/qc/batches/{old_id}").json()["status"] == "stale"

    # 发起重算
    r = client.post(f"/api/qc/batches/{old_id}/recalculate")
    assert r.status_code == 202, r.text
    new_id = r.json()["batch_id"]
    assert new_id != old_id
    assert r.json()["status"] == "recalculating"

    # 旧批次立即被取代，无论新批次成功与否都不再可供数
    old_after = client.get(f"/api/qc/batches/{old_id}").json()
    assert old_after["status"] == "superseded"
    assert old_after["superseded_by"] == new_id
    assert old_after["eligible_for_ingestion"] is False

    new_body = _wait_recalc(client, new_id).json()
    assert new_body["status"] == "ready"
    assert new_body["pinned_revision"] == 2
    # 新地图 T1 只覆盖 [0,1]，原站点 (1,1) 落在角点上 -> boundary 待核
    assert new_body["points"][0]["status"] == "boundary"
    assert new_body["eligible_for_ingestion"] is False

    # 重算批次保留来源链
    assert new_body["source_batch_id"] == old_id


def test_correction_during_recalculation_makes_new_batch_stale(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    old_id = _submit_batch(client, stations).json()["batch_id"]
    # 第一次更正：revision 2
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 2, 2)),
    )

    # 阻塞工作线程“快照之后、计算完成之前”的窗口，
    # 保证重算读到的是 rev2 的一致快照，再制造 rev3 更正
    import app.qc_jobs as qc_jobs
    import app.station_qc as station_qc_mod

    original_run_batch = station_qc_mod.run_batch
    import threading

    proceed = threading.Event()
    entered = threading.Event()

    def stalled_run_batch(features, stations):
        # 走到这里说明修订号与几何的一致快照已经在 DB 锁内读取完成
        entered.set()
        proceed.wait(timeout=10)
        return original_run_batch(features, stations)

    qc_jobs.station_qc.run_batch = stalled_run_batch
    try:
        r = client.post(f"/api/qc/batches/{old_id}/recalculate")
        new_id = r.json()["batch_id"]
        assert entered.wait(timeout=5)

        # 重算进行中再次更正：revision 3
        client.put(
            "/api/maps/v1",
            json=feature_collection(rect_geojson("T1", 0, 0, 3, 3)),
        )
        # 旧批次仍是 superseded，不会因再更正而复活
        assert (
            client.get(f"/api/qc/batches/{old_id}").json()["status"]
            == "superseded"
        )
        proceed.set()
    finally:
        qc_jobs.station_qc.run_batch = original_run_batch

    # 重算读到的是 revision 2 的一致快照；交付时当前已是 revision 3 -> stale
    new_body = _wait_recalc(client, new_id).json()
    assert new_body["pinned_revision"] == 2
    assert new_body["current_revision"] == 3
    assert new_body["status"] == "stale"
    assert new_body["eligible_for_ingestion"] is False

    # 再基于 rev3 重算一次 -> 新结论可用，链条继续
    r3 = client.post(f"/api/qc/batches/{new_id}/recalculate")
    assert r3.status_code == 202
    third_id = r3.json()["batch_id"]
    third = _wait_recalc(client, third_id).json()
    assert third["status"] == "ready"
    assert third["pinned_revision"] == 3
    assert third["eligible_for_ingestion"] is True
    # 前两代批次都不可供数
    assert client.get(f"/api/qc/batches/{new_id}").json()["status"] == "superseded"
    assert client.get(f"/api/qc/batches/{old_id}").json()["status"] == "superseded"


def test_failed_recalculation_never_revives_old_conclusion(client, monkeypatch):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    old_id = _submit_batch(client, stations).json()["batch_id"]
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 2, 2)),
    )

    def boom(self, batch_id, row):
        raise RuntimeError("模拟重算失败")

    monkeypatch.setattr("app.qc_jobs.QcManager._compute", boom)

    r = client.post(f"/api/qc/batches/{old_id}/recalculate")
    new_id = r.json()["batch_id"]
    failed = _wait_recalc(client, new_id).json()
    assert failed["status"] == "failed"
    assert failed["eligible_for_ingestion"] is False
    assert "模拟重算失败" in failed["error"]

    # 旧批次保持 superseded，不会因重算失败回退为可供数
    old_body = client.get(f"/api/qc/batches/{old_id}").json()
    assert old_body["status"] == "superseded"
    assert old_body["eligible_for_ingestion"] is False

    # 修复后允许对失败批次重新发起重算
    monkeypatch.undo()
    r2 = client.post(f"/api/qc/batches/{new_id}/recalculate")
    assert r2.status_code == 202
    fixed = _wait_recalc(client, r2.json()["batch_id"]).json()
    assert fixed["status"] == "ready"
    assert fixed["eligible_for_ingestion"] is True


def test_recalculate_guard_states(client):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    old_id = _submit_batch(client, stations).json()["batch_id"]
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 2, 2)),
    )
    new_id = client.post(
        f"/api/qc/batches/{old_id}/recalculate"
    ).json()["batch_id"]
    _wait_recalc(client, new_id)

    # 已被取代的批次不能再重算
    r = client.post(f"/api/qc/batches/{old_id}/recalculate")
    assert r.status_code == 409
    # 不存在的批次
    assert client.post("/api/qc/batches/nope/recalculate").status_code == 404
    # 未知批次查询
    assert client.get("/api/qc/batches/nope").status_code == 404


# ----------------------------------------------------------------------
# 持久化与重启
# ----------------------------------------------------------------------
def test_batches_and_conclusions_survive_restart(client, tmp_db_path):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
        {"station_id": "S2", "lon": 9, "lat": 9,
         "reported_township_id": "T1"},
    ]
    batch_id = _submit_batch(client, stations).json()["batch_id"]

    from app.db import Database
    from app.qc_jobs import QcManager

    db2 = Database(tmp_db_path)
    try:
        qc2 = QcManager(db2)
        payload = qc2.build_batch_payload(batch_id)
        assert payload["status"] == "ready"
        assert payload["summary"]["total"] == 2
        assert len(payload["points"]) == 2
        assert payload["points"][0]["station_id"] == "S1"
        assert payload["eligible_for_ingestion"] is False
    finally:
        db2.close()


def test_interrupted_recalculation_marked_failed_after_restart(client, tmp_db_path):
    _upload_map(client)
    stations = [
        {"station_id": "S1", "lon": 1, "lat": 1,
         "reported_township_id": "T1"},
    ]
    old_id = _submit_batch(client, stations).json()["batch_id"]
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_geojson("T1", 0, 0, 2, 2)),
    )
    new_id = client.post(
        f"/api/qc/batches/{old_id}/recalculate"
    ).json()["batch_id"]
    _wait_recalc(client, new_id)  # 先让一次成功，制造两个批次

    third_resp = client.post(f"/api/qc/batches/{new_id}/recalculate")
    third_id = third_resp.json()["batch_id"]
    _wait_recalc(client, third_id)  # 正常完成，链条上已有三代批次
    assert client.get(f"/api/qc/batches/{new_id}").json()["status"] == "superseded"

    # 模拟进程在重算中途被杀死：把最新批次人为置回 recalculating 遗留态
    with client.app.state.db._lock:
        client.app.state.db._conn.execute(
            "UPDATE qc_batches SET status='recalculating', error=NULL "
            "WHERE batch_id=?",
            (third_id,),
        )
        assert (
            client.app.state.db.get_qc_batch_row(third_id)["status"]
            == "recalculating"
        )

    from app.db import Database

    db2 = Database(tmp_db_path)  # 构造即执行启动恢复
    try:
        row = db2.get_qc_batch_row(third_id)
        assert row["status"] == "failed"
        assert "重启" in row["error"]
        # 源批次仍 superseded，旧结论没有复活
        assert db2.get_qc_batch_row(new_id)["status"] == "superseded"
        assert db2.get_qc_batch_row(old_id)["status"] == "superseded"
    finally:
        db2.close()
