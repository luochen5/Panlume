"""摘要任务、结果和模型调用的事务化存储；不持有跨线程 SQLite 连接。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pandeng.reporting.builder import build_event_fact_package
from pandeng.reporting.templates import render_realtime_summary
from pandeng.summarizer.content import SummaryFacts, build_prompt
from pandeng.storage.db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CallRecord:
    status: str
    model_name: str
    request_json: str
    response_text: str | None
    thinking_text: str | None
    error_type: str | None
    error_message: str | None
    elapsed_ms: int
    total_duration_ns: int | None
    load_duration_ns: int | None
    eval_count: int | None
    eval_duration_ns: int | None
    started_at: str
    finished_at: str


class SummaryStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_task(
        self, facts: SummaryFacts, max_chars: int, language: str = "zh-CN"
    ) -> tuple[sqlite3.Row, bool]:
        now = _now()
        fact_package = build_event_fact_package(self.database, facts.event_check_id)
        prompt = build_prompt(facts, max_chars, language, fact_package)
        template = render_realtime_summary(fact_package, language)
        input_payload = facts.to_dict()
        input_payload["fact_package"] = fact_package.to_dict()
        input_json = json.dumps(
            input_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM summary_tasks WHERE event_check_id = ?",
                (facts.event_check_id,),
            ).fetchone()
            if existing is not None:
                return existing, False
            cursor = connection.execute(
                """
                INSERT INTO summary_tasks(
                    event_check_id, status, input_json, prompt_text, template_text,
                    created_at, updated_at
                ) VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """,
                (facts.event_check_id, input_json, prompt, template, now, now),
            )
            row = connection.execute(
                "SELECT * FROM summary_tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return row, True

    def task(self, task_id: int) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM summary_tasks WHERE id = ?", (task_id,)
            ).fetchone()

    def pending_tasks(self) -> list[sqlite3.Row]:
        with self.database.connect() as connection:
            return list(connection.execute(
                "SELECT * FROM summary_tasks WHERE status = 'pending' ORDER BY id"
            ))

    def recover_interrupted(self) -> list[sqlite3.Row]:
        """仅恢复上次遗留任务；成功或已回退的任务保持原结果。"""
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE summary_tasks
                SET status = 'pending', recovery_count = recovery_count + 1,
                    started_at = NULL, finished_at = NULL, fallback_reason = NULL,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return list(connection.execute(
                "SELECT * FROM summary_tasks WHERE status = 'pending' ORDER BY id"
            ))

    def mark_running(self, task_id: int) -> bool:
        now = _now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE summary_tasks
                SET status = 'running', attempt_count = attempt_count + 1,
                    started_at = ?, finished_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, task_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _insert_call(
        connection: sqlite3.Connection, task_id: int, call: CallRecord, *, active: bool
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_calls(
                task_id, provider, model_name, status, request_json,
                response_text, thinking_text, error_type, error_message, elapsed_ms,
                total_duration_ns, load_duration_ns, eval_count, eval_duration_ns,
                started_at, finished_at
            ) VALUES (?, 'ollama', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, call.model_name,
                call.status if active else "ignored_late_result",
                call.request_json, call.response_text, call.thinking_text,
                call.error_type, call.error_message, call.elapsed_ms,
                call.total_duration_ns, call.load_duration_ns,
                call.eval_count, call.eval_duration_ns,
                call.started_at, call.finished_at,
            ),
        )

    def finish_model(
        self,
        task_id: int,
        text: str,
        *,
        call: CallRecord,
    ) -> bool:
        now = _now()
        with self.database.connect() as connection:
            task = connection.execute(
                "SELECT * FROM summary_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            active = task is not None and task["status"] == "running"
            self._insert_call(connection, task_id, call, active=active)
            if not active:
                return False
            connection.execute(
                """
                INSERT INTO summary_results(
                    task_id, event_check_id, summary_kind, text,
                    validation_reasons_json, model_name, created_at
                ) VALUES (?, ?, 'model', ?, '[]', ?, ?)
                """,
                (task_id, task["event_check_id"], text, call.model_name, now),
            )
            connection.execute(
                """
                UPDATE summary_tasks
                SET status = 'succeeded', finished_at = ?, fallback_reason = NULL,
                    updated_at = ? WHERE id = ? AND status = 'running'
                """,
                (now, now, task_id),
            )
            return True

    def finish_fallback(
        self,
        task_id: int,
        reason: str,
        *,
        validation_reasons: tuple[str, ...] = (),
        call: CallRecord | None = None,
    ) -> bool:
        now = _now()
        with self.database.connect() as connection:
            task = connection.execute(
                "SELECT * FROM summary_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            active = task is not None and task["status"] in {"pending", "running"}
            if call is not None:
                self._insert_call(connection, task_id, call, active=active)
            if not active:
                return False
            reasons = validation_reasons or (reason,)
            connection.execute(
                """
                INSERT INTO summary_results(
                    task_id, event_check_id, summary_kind, text,
                    validation_reasons_json, model_name, created_at
                ) VALUES (?, ?, 'template', ?, ?, NULL, ?)
                """,
                (
                    task_id, task["event_check_id"], task["template_text"],
                    json.dumps(reasons, ensure_ascii=False), now,
                ),
            )
            connection.execute(
                """
                UPDATE summary_tasks
                SET status = 'fallback', fallback_reason = ?, finished_at = ?,
                    updated_at = ?
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (reason, now, now, task_id),
            )
            return True

    def fallback_unfinished(self, reason: str) -> int:
        with self.database.connect() as connection:
            ids = [
                int(row["id"]) for row in connection.execute(
                    "SELECT id FROM summary_tasks WHERE status IN ('pending', 'running')"
                )
            ]
        return sum(self.finish_fallback(task_id, reason) for task_id in ids)

    def counts(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = list(connection.execute(
                "SELECT status, COUNT(*) AS count FROM summary_tasks GROUP BY status"
            ))
            calls = connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        result = {key: 0 for key in ("pending", "running", "succeeded", "fallback")}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        result["model_calls"] = int(calls)
        return result

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.database.connect() as connection:
            return list(connection.execute(
                """
                SELECT t.*, r.summary_kind, r.text AS summary_text,
                       r.validation_reasons_json,
                       e.stock_code, e.event_type
                FROM summary_tasks AS t
                JOIN rule_event_checks AS c ON c.id = t.event_check_id
                JOIN rule_events AS e ON e.id = c.event_id
                LEFT JOIN summary_results AS r ON r.task_id = t.id
                ORDER BY t.id DESC LIMIT ?
                """,
                (limit,),
            ))

    def calls_for_task(self, task_id: int) -> list[sqlite3.Row]:
        with self.database.connect() as connection:
            return list(connection.execute(
                "SELECT * FROM model_calls WHERE task_id = ? ORDER BY id", (task_id,)
            ))

    def input_for_task(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(json.loads(row["input_json"]))
