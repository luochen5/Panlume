"""有界队列、单模型线程、异常恢复与有限收尾。"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pandeng.config.schema import OllamaConfig, SummaryConfig
from pandeng.rules.engine import RuleNotice
from pandeng.storage.db import Database

from .content import (
    SummaryFacts,
    load_summary_facts,
    validate_summary,
)
from .ollama import OllamaClient, OllamaError, OllamaResponse, separate_r1_output
from .service import CallRecord, SummaryStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Submission:
    task_id: int
    status: str
    enqueued: bool
    reason: str = ""


class SummaryService:
    """固定告警先由调用方输出，再把 RuleNotice 交给本服务。"""

    def __init__(
        self,
        database: Database,
        ollama: OllamaConfig,
        summary: SummaryConfig | None = None,
        *,
        client: Any | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.ollama = ollama
        self.summary = summary or SummaryConfig()
        self.enabled = ollama.enabled if enabled is None else bool(enabled)
        self.client = client or OllamaClient(ollama)
        self.store = SummaryStore(database)
        self._queue: queue.Queue[int | None] = queue.Queue(
            maxsize=self.summary.queue_capacity
        )
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._started = False
        self._accepting = False
        self._closed = False

    @property
    def worker_thread_id(self) -> int | None:
        return None if self._thread is None else self._thread.ident

    @property
    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        if self._closed:
            raise RuntimeError("摘要服务已经关闭，不能重新启动同一实例。")
        if not self.enabled or self._started:
            return 0
        self._started = True
        self._accepting = True
        recovered = self.store.recover_interrupted()
        self._thread = threading.Thread(
            target=self._run, name="pandeng-summary-worker", daemon=True
        )
        self._thread.start()
        overflow = 0
        for row in recovered:
            try:
                self._queue.put_nowait(int(row["id"]))
            except queue.Full:
                self.store.finish_fallback(int(row["id"]), "queue_full_during_recovery")
                overflow += 1
        return len(recovered) - overflow

    def submit(self, notice: RuleNotice) -> Submission | None:
        if not self.enabled:
            return None
        if not self._started or not self._accepting or self._closed:
            raise RuntimeError("摘要服务尚未启动或已经停止接收任务。")
        facts = load_summary_facts(self.store.database, notice)
        row, created = self.store.create_task(facts, self.summary.max_chars)
        task_id = int(row["id"])
        if not created:
            return Submission(task_id, str(row["status"]), False, "duplicate_check")
        try:
            self._queue.put_nowait(task_id)
        except queue.Full:
            self.store.finish_fallback(task_id, "queue_full")
            return Submission(task_id, "fallback", False, "queue_full")
        return Submission(task_id, "pending", True)

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def close(self, timeout_seconds: float | None = None) -> bool:
        """等待有上限；超时任务转固定模板，迟到结果不能覆盖回退结论。"""
        if not self.enabled or self._closed:
            return True
        self._accepting = False
        if not self._started:
            self._closed = True
            return True
        timeout = (
            self.summary.shutdown_timeout_seconds
            if timeout_seconds is None else max(0.0, timeout_seconds)
        )
        started = time.monotonic()
        drained = self.wait_idle(timeout)
        if drained:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            remaining = max(0.0, timeout - (time.monotonic() - started))
            if self._thread is not None:
                self._thread.join(timeout=remaining)
        else:
            self.store.fallback_unfinished("shutdown_timeout")
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        self._closed = True
        return drained and not self.worker_alive

    def _run(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                row = self.store.task(task_id)
                if row is None or row["status"] != "pending":
                    continue
                if not self.store.mark_running(task_id):
                    continue
                try:
                    self._process(row)
                except Exception as exc:  # 工作线程故障只影响摘要，原始告警已经保存
                    self.store.finish_fallback(
                        task_id,
                        "worker_internal_error",
                        validation_reasons=(f"{type(exc).__name__}: {exc}",),
                    )
            finally:
                self._queue.task_done()
                with self._condition:
                    self._condition.notify_all()

    def _request_json(self, prompt: str) -> str:
        return json.dumps(
            {
                "model": self.ollama.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1},
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _call_record(
        self,
        *,
        status: str,
        model_name: str,
        request_json: str,
        started_at: str,
        started: float,
        response: OllamaResponse | None = None,
        response_text: str | None = None,
        thinking_text: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CallRecord:
        return CallRecord(
            status=status,
            model_name=model_name,
            request_json=request_json,
            response_text=response_text,
            thinking_text=thinking_text,
            error_type=error_type,
            error_message=error_message,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            total_duration_ns=None if response is None else response.total_duration_ns,
            load_duration_ns=None if response is None else response.load_duration_ns,
            eval_count=None if response is None else response.eval_count,
            eval_duration_ns=None if response is None else response.eval_duration_ns,
            started_at=started_at,
            finished_at=_now(),
        )

    def _process(self, row: Any) -> None:
        task_id = int(row["id"])
        prompt = str(row["prompt_text"])
        request_json = self._request_json(prompt)
        started_at = _now()
        started = time.monotonic()
        response: OllamaResponse | None = None
        try:
            response = self.client.generate(prompt)
        except OllamaError as exc:
            call = self._call_record(
                status=exc.kind,
                model_name=self.ollama.model,
                request_json=request_json,
                started_at=started_at,
                started=started,
                error_type=exc.kind,
                error_message=str(exc),
            )
            self.store.finish_fallback(task_id, exc.kind, call=call)
            return
        except Exception as exc:
            call = self._call_record(
                status="client_error",
                model_name=self.ollama.model,
                request_json=request_json,
                started_at=started_at,
                started=started,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self.store.finish_fallback(task_id, "client_error", call=call)
            return

        try:
            final_text, thinking_text = separate_r1_output(
                response.text, response.thinking
            )
        except OllamaError as exc:
            call = self._call_record(
                status=exc.kind,
                model_name=response.model,
                request_json=request_json,
                started_at=started_at,
                started=started,
                response=response,
                response_text=response.raw_json,
                error_type=exc.kind,
                error_message=str(exc),
            )
            self.store.finish_fallback(task_id, exc.kind, call=call)
            return

        facts = SummaryFacts.from_dict(dict(json.loads(row["input_json"])))
        reasons = validate_summary(final_text, facts, self.summary.max_chars)
        if reasons:
            call = self._call_record(
                status="validation_failed",
                model_name=response.model,
                request_json=request_json,
                started_at=started_at,
                started=started,
                response=response,
                response_text=response.raw_json,
                thinking_text=thinking_text,
                error_type="validation_failed",
                error_message="；".join(reasons),
            )
            self.store.finish_fallback(
                task_id, "validation_failed", validation_reasons=reasons, call=call
            )
            return
        call = self._call_record(
            status="succeeded",
            model_name=response.model,
            request_json=request_json,
            started_at=started_at,
            started=started,
            response=response,
            response_text=response.raw_json,
            thinking_text=thinking_text,
        )
        self.store.finish_model(task_id, final_text, call=call)
