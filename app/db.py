"""SQLite 持久化层。

只持久化“已受理”的地图版本；计算任务是临时的，仅保留在内存中
（详见 jobs.py 与 README 的说明）。
所有写入在单个连接上串行化，并通过 WAL 提升并发读能力。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config


class Database:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        # 重启恢复：上次进程遗留的 recalculating 质控批次落 failed。
        # 放在构造函数里，保证任何进程/连接以该库启动都会执行，不依赖 Web 层。
        self.recover_interrupted_recalculations()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS maps (
                    version     TEXT PRIMARY KEY,
                    revision    INTEGER NOT NULL DEFAULT 1,
                    feature_cnt INTEGER NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS map_features (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    version     TEXT NOT NULL REFERENCES maps(version) ON DELETE CASCADE,
                    township_id TEXT NOT NULL,
                    geom_json   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_map_features_version
                    ON map_features(version);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_map_features_version_township
                    ON map_features(version, township_id);

                CREATE TABLE IF NOT EXISTS qc_batches (
                    batch_id          TEXT PRIMARY KEY,
                    map_version       TEXT NOT NULL,
                    map_revision      INTEGER NOT NULL,
                    status            TEXT NOT NULL,
                    source_batch_id   TEXT,
                    superseded_by     TEXT,
                    total             INTEGER NOT NULL DEFAULT 0,
                    matched           INTEGER NOT NULL DEFAULT 0,
                    pending_review    INTEGER NOT NULL DEFAULT 0,
                    report_mismatch   INTEGER NOT NULL DEFAULT 0,
                    all_matched       INTEGER NOT NULL DEFAULT 0,
                    input_stations    TEXT NOT NULL,
                    error             TEXT,
                    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_qc_batches_version
                    ON qc_batches(map_version);
                CREATE INDEX IF NOT EXISTS idx_qc_batches_source
                    ON qc_batches(source_batch_id);

                CREATE TABLE IF NOT EXISTS qc_points (
                    batch_id    TEXT NOT NULL
                        REFERENCES qc_batches(batch_id) ON DELETE CASCADE,
                    seq         INTEGER NOT NULL,
                    station_id  TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (batch_id, station_id)
                );
                """
            )

    def recover_interrupted_recalculations(self) -> int:
        """进程重启恢复：把停留在 recalculating 的批次落为 failed。

        工作线程随进程消失，重算不可能再完成；其源批次此前已置 superseded，
        旧结论不会因此恢复供数资格。返回被恢复的批次数。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id FROM qc_batches WHERE status = 'recalculating'"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE qc_batches SET status = 'failed', "
                    "error = '重算因进程重启中断，结论不可用，请重新发起重算', "
                    "updated_at = datetime('now') WHERE batch_id = ?",
                    (row["batch_id"],),
                )
            return len(rows)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def replace_map(
        self, version: str, features: list[tuple[str, dict]]
    ) -> int:
        """整版写入（同版重传 = 整版更正）。

        在单个事务内先删后插；任何一步失败都会回滚，旧版完整保留。
        返回写入后的版本修订号 revision。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT revision FROM maps WHERE version = ?", (version,)
                ).fetchone()
                next_revision = (row["revision"] + 1) if row else 1

                self._conn.execute(
                    "INSERT INTO maps(version, revision, feature_cnt) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET "
                    "revision = excluded.revision, "
                    "feature_cnt = excluded.feature_cnt, "
                    "updated_at = datetime('now')",
                    (version, next_revision, len(features)),
                )
                self._conn.execute(
                    "DELETE FROM map_features WHERE version = ?", (version,)
                )
                self._conn.executemany(
                    "INSERT INTO map_features(version, township_id, geom_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (version, township_id, json.dumps(geom, ensure_ascii=False))
                        for township_id, geom in features
                    ],
                )
                self._conn.execute("COMMIT")
                return next_revision
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get_revision(self, version: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM maps WHERE version = ?", (version,)
            ).fetchone()
            return int(row["revision"]) if row else None

    def get_features(self, version: str) -> Optional[list[tuple[str, dict]]]:
        """返回 [(township_id, geojson_geometry_dict), ...]；版本不存在返回 None。"""
        with self._lock:
            if self.get_revision(version) is None:
                return None
            rows = self._conn.execute(
                "SELECT township_id, geom_json FROM map_features "
                "WHERE version = ? ORDER BY id",
                (version,),
            ).fetchall()
            return [(r["township_id"], json.loads(r["geom_json"])) for r in rows]

    def list_versions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, revision, feature_cnt, created_at, updated_at "
                "FROM maps ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 站点归属质控批次
    # ------------------------------------------------------------------
    def insert_qc_batch(
        self,
        batch_id: str,
        map_version: str,
        map_revision: int,
        status: str,
        input_stations: list[dict],
        summary: Optional[dict] = None,
        point_results: Optional[list[dict]] = None,
        source_batch_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """写入一个质控批次（含逐点结论），单事务。"""
        summary = summary or {}
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO qc_batches(batch_id, map_version, map_revision, "
                    "status, source_batch_id, total, matched, pending_review, "
                    "report_mismatch, all_matched, input_stations, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        map_version,
                        map_revision,
                        status,
                        source_batch_id,
                        int(summary.get("total", len(input_stations))),
                        int(summary.get("matched", 0)),
                        int(summary.get("pending_review", 0)),
                        int(summary.get("report_mismatch", 0)),
                        1 if summary.get("all_matched", False) else 0,
                        json.dumps(input_stations, ensure_ascii=False),
                        error,
                    ),
                )
                if point_results:
                    self._conn.executemany(
                        "INSERT INTO qc_points(batch_id, seq, station_id, result_json) "
                        "VALUES (?, ?, ?, ?)",
                        [
                            (
                                batch_id,
                                seq,
                                p["station_id"],
                                json.dumps(p, ensure_ascii=False),
                            )
                            for seq, p in enumerate(point_results)
                        ],
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def begin_recalculation(
        self,
        new_batch_id: str,
        source_batch_id: str,
        current_revision: int,
    ) -> dict:
        """单事务创建重算批次并把源批次置为 superseded。

        原子性保证：要么“新批次 recalculating + 旧批次 superseded”同时生效，
        要么整体回滚；任何中间态下旧结论都不会被当作当前有效结论交付。
        返回源批次行（dict）。源批次非 ready / 已被取代时抛 ValueError。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM qc_batches WHERE batch_id = ?",
                    (source_batch_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(source_batch_id)
                # ready（地图更正后的常规重算）与 failed（上次重算失败/重启
                # 中断后的重试）允许重算；recalculating/superseded 不允许：
                # 前者已有进行中的重算，后者的旧结论已被新批次取代、永不复活
                if row["status"] not in ("ready", "failed"):
                    raise ValueError(
                        f"源批次当前状态为 {row['status']}，不允许重算"
                    )
                input_stations = json.loads(row["input_stations"])
                self._conn.execute(
                    "INSERT INTO qc_batches(batch_id, map_version, map_revision, "
                    "status, source_batch_id, total, input_stations) "
                    "VALUES (?, ?, ?, 'recalculating', ?, ?, ?)",
                    (
                        new_batch_id,
                        row["map_version"],
                        current_revision,
                        source_batch_id,
                        len(input_stations),
                        json.dumps(input_stations, ensure_ascii=False),
                    ),
                )
                self._conn.execute(
                    "UPDATE qc_batches SET status = 'superseded', "
                    "superseded_by = ?, updated_at = datetime('now') "
                    "WHERE batch_id = ?",
                    (new_batch_id, source_batch_id),
                )
                self._conn.execute("COMMIT")
                return dict(row)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def complete_qc_batch(
        self,
        batch_id: str,
        map_revision: int,
        summary: dict,
        point_results: list[dict],
    ) -> None:
        """重算成功：写入逐点结论并置 ready，单事务。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE qc_batches SET status = 'ready', map_revision = ?, "
                    "total = ?, matched = ?, pending_review = ?, report_mismatch = ?, "
                    "all_matched = ?, error = NULL, updated_at = datetime('now') "
                    "WHERE batch_id = ?",
                    (
                        map_revision,
                        int(summary["total"]),
                        int(summary["matched"]),
                        int(summary["pending_review"]),
                        int(summary["report_mismatch"]),
                        1 if summary["all_matched"] else 0,
                        batch_id,
                    ),
                )
                self._conn.executemany(
                    "INSERT INTO qc_points(batch_id, seq, station_id, result_json) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            batch_id,
                            seq,
                            p["station_id"],
                            json.dumps(p, ensure_ascii=False),
                        )
                        for seq, p in enumerate(point_results)
                    ],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def fail_qc_batch(self, batch_id: str, error: str) -> None:
        """重算失败：批次落 failed（不写逐点结论），单事务。"""
        with self._lock:
            self._conn.execute(
                "UPDATE qc_batches SET status = 'failed', error = ?, "
                "updated_at = datetime('now') WHERE batch_id = ?",
                (error, batch_id),
            )

    def get_qc_batch_row(self, batch_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qc_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_qc_points(self, batch_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result_json FROM qc_points WHERE batch_id = ? ORDER BY seq",
                (batch_id,),
            ).fetchall()
            return [json.loads(r["result_json"]) for r in rows]

    def get_qc_input_stations(self, batch_id: str) -> list[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT input_stations FROM qc_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return json.loads(row["input_stations"]) if row else []

    def list_qc_batches(self, map_version: Optional[str] = None) -> list[dict]:
        with self._lock:
            if map_version is not None:
                rows = self._conn.execute(
                    "SELECT * FROM qc_batches WHERE map_version = ? "
                    "ORDER BY created_at, batch_id",
                    (map_version,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM qc_batches ORDER BY created_at, batch_id"
                ).fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
