"""
Executor V2 - 结果写入器

将执行结果持久化到 SQLite 数据库。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from trustharness.runner.models import ExecutionResult


class ResultWriter:
    """执行结果持久化"""

    def __init__(self, db_path: str = "output/execution_results.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    def _get_conn(self):
        import sqlite3
        return sqlite3.connect(self._db_path)

    def _ensure_table(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    case_id TEXT NOT NULL,
                    case_title TEXT,
                    status TEXT NOT NULL,
                    duration_ms REAL,
                    request_json TEXT,
                    response_json TEXT,
                    assertions_json TEXT,
                    error_message TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_results_run_id
                ON execution_results(run_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_results_status
                ON execution_results(status)
            """)
            conn.commit()

    def write(self, result: ExecutionResult, run_id: str = "") -> int:
        """写入单条执行结果，返回记录 ID"""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO execution_results
                    (run_id, case_id, case_title, status, duration_ms,
                     request_json, response_json, assertions_json,
                     error_message, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.case_id,
                    result.case_title,
                    result.status,
                    result.duration_ms,
                    json.dumps(result.request.to_dict(), ensure_ascii=False) if result.request else None,
                    json.dumps(result.response.to_dict(), ensure_ascii=False) if result.response else None,
                    json.dumps([a.to_dict() for a in result.assertions], ensure_ascii=False),
                    result.error_message,
                    result.started_at,
                    result.finished_at,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def write_batch(self, results: List[ExecutionResult], run_id: str = "") -> List[int]:
        """批量写入"""
        ids = []
        for r in results:
            ids.append(self.write(r, run_id))
        return ids

    def get_by_run(self, run_id: str) -> List[dict]:
        """按 run_id 查询所有结果"""
        with self._get_conn() as conn:
            conn.row_factory = _dict_factory
            rows = conn.execute(
                "SELECT * FROM execution_results WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            for row in rows:
                row["request"] = json.loads(row.pop("request_json") or "null")
                row["response"] = json.loads(row.pop("response_json") or "null")
                row["assertions"] = json.loads(row.pop("assertions_json") or "[]")
            return rows

    def get_by_id(self, record_id: int) -> Optional[dict]:
        """按 ID 查询单条"""
        with self._get_conn() as conn:
            conn.row_factory = _dict_factory
            row = conn.execute(
                "SELECT * FROM execution_results WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row:
                row["request"] = json.loads(row.pop("request_json") or "null")
                row["response"] = json.loads(row.pop("response_json") or "null")
                row["assertions"] = json.loads(row.pop("assertions_json") or "[]")
            return row

    def get_summary(self, run_id: str) -> dict:
        """获取某次运行的统计摘要"""
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) as passed,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error,
                    SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) as skipped,
                    AVG(duration_ms) as avg_duration_ms,
                    SUM(duration_ms) as total_duration_ms
                FROM execution_results WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row:
                return {
                    "total": row[0],
                    "passed": row[1],
                    "failed": row[2],
                    "error": row[3],
                    "skipped": row[4],
                    "avg_duration_ms": round(row[5] or 0, 2),
                    "total_duration_ms": round(row[6] or 0, 2),
                }
            return {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}


def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
