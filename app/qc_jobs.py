"""站点归属质控批次的生命周期与供数资格管理。

一致性规则（对应业务要求）：
1. 首次提交在请求线程内同步完成（仅读已持久化地图），批次直接落库为 ready；
2. 重算生成*新批次*：单事务把源批次置 superseded、新批次置 recalculating，
   源批次的旧结论随即失去供数资格，且任何情况下都不会复活；
3. 工作线程在 DB 锁内一次性快照“当前修订号 + 该版全部几何”，保证一次重算
   读取的是同一个地图修订；计算期间地图再被更正，只会让新批次交付时被判
   stale，旧批次依旧 superseded；
4. 重算失败 -> 新批次 failed（无源结论可回退）；进程重启 -> 启动恢复把
   遗留 recalculating 批次落 failed（见 db.recover_interrupted_recalculations）；
5. 供数资格在每次查询时动态计算：ready 且地图修订号未变化且全部点匹配且
   上报乡镇号全部一致，才 eligible_for_ingestion=true。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, station_qc
from .db import Database

# 批次状态
READY = "ready"
RECALCULATING = "recalculating"
SUPERSEDED = "superseded"
FAILED = "failed"
STALE = "stale"  # 非持久化状态：查询时由修订号比对动态得出


class QcManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS,
            thread_name_prefix="qc-recalc",
        )

    # ------------------------------------------------------------------
    def new_batch_id(self) -> str:
        return "qc_" + uuid.uuid4().hex

    def submit_initial(
        self,
        map_version: str,
        stations: list[dict[str, Any]],
    ) -> str:
        """同步计算首次质控批次并持久化，返回 batch_id。

        在 DB 写锁内快照修订号与全部几何，与同版更正互斥，
        避免提交瞬间读到半版数据。
        """
        batch_id = self.new_batch_id()
        with self.db._lock:
            pinned = self.db.get_revision(map_version)
            features = self.db.get_features(map_version)
        result = station_qc.run_batch(features or [], stations)
        self.db.insert_qc_batch(
            batch_id=batch_id,
            map_version=map_version,
            map_revision=pinned,
            status=READY,
            input_stations=stations,
            summary=result["summary"],
            point_results=result["points"],
        )
        return batch_id

    def start_recalculation(self, source_batch_id: str) -> str:
        """创建重算批次并取代源批次；返回新 batch_id。

        源批次不存在 -> LookupError；非 ready 状态 -> ValueError。
        """
        new_batch_id = self.new_batch_id()
        with self._lock:
            source = self.db.get_qc_batch_row(source_batch_id)
            if source is None:
                raise LookupError(source_batch_id)
            current_revision = self.db.get_revision(source["map_version"])
            if current_revision is None:
                raise ValueError(
                    f"地图版本 {source['map_version']} 已不存在，无法重算"
                )
            # 单事务：新批次 recalculating + 源批次 superseded
            self.db.begin_recalculation(
                new_batch_id, source_batch_id, current_revision
            )
        self._pool.submit(self._run_recalculation, new_batch_id)
        return new_batch_id

    # ------------------------------------------------------------------
    def _run_recalculation(self, batch_id: str) -> None:
        row = self.db.get_qc_batch_row(batch_id)
        if row is None or row["status"] != RECALCULATING:
            return
        try:
            self._compute(batch_id, row)
        except Exception as exc:  # 防御：任何意外都落 failed，不拖垮线程池
            self.db.fail_qc_batch(
                batch_id, f"重算异常：{type(exc).__name__}: {exc}"
            )

    def _compute(self, batch_id: str, row: dict) -> None:
        map_version = row["map_version"]
        stations = self.db.get_qc_input_stations(batch_id)

        # 关键：在同一把 DB 写锁内快照修订号与全部几何，
        # 与 replace_map 互斥，保证一次重算读到一致的地图修订
        with self.db._lock:
            snapshot_revision = self.db.get_revision(map_version)
            features = self.db.get_features(map_version)

        if snapshot_revision is None or features is None:
            self.db.fail_qc_batch(
                batch_id, f"地图版本 {map_version} 在重算期间被删除，重算失败"
            )
            return

        result = station_qc.run_batch(features, stations)
        # 完成时以快照修订号落库；交付前的修订号比对由 eligibility 动态执行
        self.db.complete_qc_batch(
            batch_id,
            map_revision=snapshot_revision,
            summary=result["summary"],
            point_results=result["points"],
        )

    # ------------------------------------------------------------------
    def build_batch_payload(self, batch_id: str) -> Optional[dict[str, Any]]:
        """组装对外批次视图，含动态供数资格判定。"""
        row = self.db.get_qc_batch_row(batch_id)
        if row is None:
            return None

        current_revision = self.db.get_revision(row["map_version"])
        status = row["status"]
        ineligible_reasons: list[str] = []

        if status == READY:
            if current_revision is None:
                status = STALE
                ineligible_reasons.append(
                    f"地图版本 {row['map_version']} 已不存在"
                )
            elif current_revision != row["map_revision"]:
                status = STALE
                ineligible_reasons.append(
                    "同版地图已更正：批次钉取修订号 "
                    f"{row['map_revision']}，当前修订号 {current_revision}，"
                    "旧结论立即失效，须发起重算"
                )
            else:
                if not row["all_matched"]:
                    ineligible_reasons.append(
                        f"存在 {row['pending_review']} 个待核站点"
                        "（边界/洞内/无覆盖/多重命中），不能当作匹配"
                    )
                if row["report_mismatch"]:
                    ineligible_reasons.append(
                        f"存在 {row['report_mismatch']} 个上报乡镇号与"
                        "几何归属不一致的站点"
                    )
        elif status == RECALCULATING:
            ineligible_reasons.append("重算进行中，结论尚未产生")
        elif status == SUPERSEDED:
            ineligible_reasons.append(
                f"已被重算批次 {row['superseded_by']} 取代，旧结论不得再供数"
            )
        elif status == FAILED:
            ineligible_reasons.append(row["error"] or "重算失败，无有效结论")

        eligible = status == READY and not ineligible_reasons
        payload: dict[str, Any] = {
            "batch_id": row["batch_id"],
            "map_version": row["map_version"],
            "pinned_revision": row["map_revision"],
            "current_revision": current_revision,
            "status": status,
            "source_batch_id": row["source_batch_id"],
            "superseded_by": row["superseded_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "summary": {
                "total": row["total"],
                "matched": row["matched"],
                "pending_review": row["pending_review"],
                "report_mismatch": row["report_mismatch"],
                "all_matched": bool(row["all_matched"]),
            },
            "eligible_for_ingestion": eligible,
            "ineligible_reasons": ineligible_reasons,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        if row["error"]:
            payload["error"] = row["error"]

        # 逐点结论仅在已产出结论的批次上返回
        if row["status"] in (READY, SUPERSEDED, STALE):
            payload["points"] = self.db.get_qc_points(batch_id)
        else:
            payload["points"] = []
        return payload

    def list_batch_summaries(
        self, map_version: Optional[str] = None
    ) -> list[dict[str, Any]]:
        rows = self.db.list_qc_batches(map_version)
        summaries = []
        for row in rows:
            current_revision = self.db.get_revision(row["map_version"])
            effective_status = row["status"]
            if (
                row["status"] == READY
                and (
                    current_revision is None
                    or current_revision != row["map_revision"]
                )
            ):
                effective_status = STALE
            summaries.append(
                {
                    "batch_id": row["batch_id"],
                    "map_version": row["map_version"],
                    "pinned_revision": row["map_revision"],
                    "current_revision": current_revision,
                    "status": effective_status,
                    "source_batch_id": row["source_batch_id"],
                    "superseded_by": row["superseded_by"],
                    "summary": {
                        "total": row["total"],
                        "matched": row["matched"],
                        "pending_review": row["pending_review"],
                        "report_mismatch": row["report_mismatch"],
                        "all_matched": bool(row["all_matched"]),
                    },
                    "created_at": row["created_at"],
                }
            )
        return summaries

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
