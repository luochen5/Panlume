from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import OllamaConfig, RulesConfig, SummaryConfig
from pandeng.data.base import MinuteBar
from pandeng.data.schedule import session_minutes
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.reporting.facts import FactPackage
from pandeng.rules.engine import RuleNotice
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes
from pandeng.summarizer.content import load_summary_facts
from pandeng.summarizer.ollama import OllamaError, OllamaResponse
from pandeng.summarizer.service import SummaryStore
from pandeng.summarizer.worker import SummaryService


CODE = "600000"
DAY = date(2026, 1, 5)


def ollama_config(enabled: bool = True) -> OllamaConfig:
    return OllamaConfig(
        enabled=enabled,
        base_url="http://127.0.0.1:11434",
        model="deepseek-r1:14b",
        timeout_seconds=1,
    )


def make_notices(
    tmp_path: Path, count: int = 1, name: str = "summary.db"
) -> tuple[Database, list[RuleNotice]]:
    database = Database(tmp_path / name)
    database.initialize()
    points = session_minutes(DAY)
    bars = [
        MinuteBar(CODE, points[index], 9, 9, 9, 9, 100, "test")
        for index in range(count)
    ]
    ingest_minutes(database, bars, adapter="test", check_gaps=False)
    add_position_lot(
        database,
        stock_code=CODE,
        quantity=100,
        buy_date=DAY.isoformat(),
        stop_loss_price=9,
        confirmation=CONFIRM_ADD,
    )
    analyzer = AnalysisService(
        database,
        rules=RulesConfig(
            rapid_move_pct=20,
            normal_cooldown_seconds=60,
            high_priority_repeat_seconds=60,
        ),
    )
    notices: list[RuleNotice] = []
    for index in range(count):
        result = analyzer.analyze_code(CODE, points[index], analysis_mode="live")
        notices.extend(
            notice for notice in result.notices if notice.event_type == "fixed_stop"
        )
    assert len(notices) == count
    return database, notices


def response_for_prompt(prompt: str, suffix: str = "") -> OllamaResponse:
    facts = json.loads(prompt.split("已保存事实：", 1)[1])
    text = (
        f"{facts['stock_code']} {facts['data_time']} 固定止损触及，"
        f"分钟收盘 9，人工确认止损价 9。仅整理程序事实。{suffix}"
    )
    return OllamaResponse(
        text=text,
        thinking="",
        model="deepseek-r1:14b",
        raw_json=json.dumps({"response": text}, ensure_ascii=False),
        total_duration_ns=123,
        load_duration_ns=45,
        eval_count=7,
        eval_duration_ns=89,
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.thread_ids: list[int] = []

    def generate(self, prompt: str) -> OllamaResponse:
        self.calls.append(prompt)
        self.thread_ids.append(threading.get_ident())
        return response_for_prompt(prompt)


class BlockingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def generate(self, prompt: str) -> OllamaResponse:
        self.calls.append(prompt)
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if not self.release.wait(timeout=2):
            raise OllamaError("timeout", "测试等待超时")
        response = response_for_prompt(prompt)
        self.finished.set()
        return response


def test_model_success_separates_r1_thinking_and_uses_worker_local_connections(
    tmp_path: Path,
) -> None:
    database, notices = make_notices(tmp_path)

    class ThinkingClient(FakeClient):
        def generate(self, prompt: str) -> OllamaResponse:
            self.calls.append(prompt)
            self.thread_ids.append(threading.get_ident())
            response = response_for_prompt(prompt)
            return OllamaResponse(
                text="<think>响应内推理</think>" + response.text,
                thinking="独立通道推理",
                model=response.model,
                raw_json=response.raw_json,
                total_duration_ns=response.total_duration_ns,
                load_duration_ns=response.load_duration_ns,
                eval_count=response.eval_count,
                eval_duration_ns=response.eval_duration_ns,
            )

    client = ThinkingClient()
    service = SummaryService(database, ollama_config(), client=client)
    service.start()
    submission = service.submit(notices[0])

    assert submission is not None and submission.enqueued
    assert service.close(timeout_seconds=2) is True
    assert client.thread_ids and client.thread_ids[0] != threading.get_ident()
    store = SummaryStore(database)
    task = store.task(submission.task_id)
    assert task is not None and task["status"] == "succeeded"
    recent = store.recent()[0]
    assert recent["summary_kind"] == "model"
    assert "think" not in recent["summary_text"]
    call = store.calls_for_task(submission.task_id)[0]
    assert "响应内推理" in call["thinking_text"]
    assert "独立通道推理" in call["thinking_text"]
    assert call["total_duration_ns"] == 123 and call["eval_count"] == 7


def test_duplicate_check_has_one_task_and_disabled_service_has_zero_calls(
    tmp_path: Path,
) -> None:
    database, notices = make_notices(tmp_path)
    client = FakeClient()
    service = SummaryService(database, ollama_config(), client=client)
    service.start()
    first = service.submit(notices[0])
    second = service.submit(notices[0])
    service.close(timeout_seconds=2)

    assert first is not None and second is not None
    assert second.task_id == first.task_id and second.reason == "duplicate_check"
    assert len(client.calls) == 1 and len(store_rows(database, "summary_tasks")) == 1

    disabled_client = FakeClient()
    disabled = SummaryService(
        database,
        ollama_config(enabled=False),
        client=disabled_client,
    )
    disabled.start()
    assert disabled.submit(notices[0]) is None
    disabled.close()
    assert disabled_client.calls == []
    assert len(store_rows(database, "model_calls")) == 1


def store_rows(database: Database, table: str):
    with database.connect() as connection:
        return list(connection.execute(f"SELECT * FROM {table}"))


def test_queue_full_immediately_saves_template_without_third_model_call(
    tmp_path: Path,
) -> None:
    database, notices = make_notices(tmp_path, count=3, name="queue.db")
    client = BlockingClient()
    service = SummaryService(
        database,
        ollama_config(),
        SummaryConfig(queue_capacity=1, shutdown_timeout_seconds=2),
        client=client,
    )
    service.start()
    first = service.submit(notices[0])
    assert first is not None and client.started.wait(timeout=1)
    second = service.submit(notices[1])
    third = service.submit(notices[2])

    assert second is not None and second.enqueued
    assert third is not None and third.status == "fallback"
    assert third.reason == "queue_full"
    third_task = SummaryStore(database).task(third.task_id)
    assert third_task is not None and third_task["status"] == "fallback"
    assert third_task["fallback_reason"] == "queue_full"

    client.release.set()
    assert service.close(timeout_seconds=2) is True
    counts = SummaryStore(database).counts()
    assert counts["succeeded"] == 2 and counts["fallback"] == 1
    assert counts["model_calls"] == 2 and len(client.calls) == 2


def test_interrupted_running_task_recovers_once_after_restart(tmp_path: Path) -> None:
    database, notices = make_notices(tmp_path, name="recovery.db")
    facts = load_summary_facts(database, notices[0])
    store = SummaryStore(database)
    row, created = store.create_task(facts, 400)
    assert created and store.mark_running(int(row["id"]))

    client = FakeClient()
    restarted = SummaryService(database, ollama_config(), client=client)
    assert restarted.start() == 1
    assert restarted.close(timeout_seconds=2) is True

    task = store.task(int(row["id"]))
    assert task is not None and task["status"] == "succeeded"
    assert task["recovery_count"] == 1 and task["attempt_count"] == 2
    assert len(client.calls) == 1


def test_validation_failure_and_timeout_fall_back_without_changing_alert(
    tmp_path: Path,
) -> None:
    database, notices = make_notices(tmp_path, count=2, name="fallback.db")

    class InvalidClient(FakeClient):
        def generate(self, prompt: str) -> OllamaResponse:
            self.calls.append(prompt)
            return response_for_prompt(prompt, "额外目标价 999。")

    class TimeoutClient(FakeClient):
        def generate(self, prompt: str) -> OllamaResponse:
            self.calls.append(prompt)
            raise OllamaError("timeout", "测试超时")

    invalid = InvalidClient()
    first = SummaryService(database, ollama_config(), client=invalid)
    first.start()
    submission = first.submit(notices[0])
    first.close(timeout_seconds=2)
    assert submission is not None
    recent = SummaryStore(database).recent()[0]
    assert recent["status"] == "fallback"
    assert recent["summary_kind"] == "template"
    original_alert = json.loads(recent["input_json"])["message"]
    assert original_alert == notices[0].message
    assert "触发依据" in recent["summary_text"]
    assert "当前" in recent["summary_text"]
    assert "追溯：事件" in recent["summary_text"]
    assert "输入外数字" in recent["validation_reasons_json"]

    timeout = TimeoutClient()
    second = SummaryService(database, ollama_config(), client=timeout)
    second.start()
    other = second.submit(notices[1])
    second.close(timeout_seconds=2)
    assert other is not None
    task = SummaryStore(database).task(other.task_id)
    assert task is not None and task["fallback_reason"] == "timeout"


def test_close_timeout_is_finite_and_late_model_result_cannot_overwrite_template(
    tmp_path: Path,
) -> None:
    database, notices = make_notices(tmp_path, name="shutdown.db")
    client = BlockingClient()
    service = SummaryService(database, ollama_config(), client=client)
    service.start()
    submission = service.submit(notices[0])
    assert submission is not None and client.started.wait(timeout=1)

    started = time.monotonic()
    drained = service.close(timeout_seconds=0.02)
    elapsed = time.monotonic() - started
    assert drained is False and elapsed < 0.5
    task = SummaryStore(database).task(submission.task_id)
    assert task is not None and task["status"] == "fallback"
    assert task["fallback_reason"] == "shutdown_timeout"

    client.release.set()
    assert client.finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while service.worker_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    task = SummaryStore(database).task(submission.task_id)
    calls = SummaryStore(database).calls_for_task(submission.task_id)
    assert task is not None and task["status"] == "fallback"
    assert calls[-1]["status"] == "ignored_late_result"


def test_recovery_or_non_notified_check_cannot_reach_model(tmp_path: Path) -> None:
    database, notices = make_notices(tmp_path, name="recovery-gate.db")
    with database.connect() as connection:
        connection.execute("UPDATE rule_event_checks SET notified = 0")
        connection.execute(
            "UPDATE analysis_runs SET analysis_mode = 'offline_recovery'"
        )
    client = FakeClient()
    service = SummaryService(database, ollama_config(), client=client)
    service.start()

    with pytest.raises(ValueError, match="live 模式"):
        service.submit(notices[0])

    service.close(timeout_seconds=1)
    assert client.calls == []
    assert store_rows(database, "summary_tasks") == []
    assert store_rows(database, "model_calls") == []



def test_summary_task_persists_fact_package_and_complete_fixed_template(tmp_path: Path) -> None:
    database, notices = make_notices(tmp_path, name="fact-package.db")
    facts = load_summary_facts(database, notices[0])
    store = SummaryStore(database)

    row, created = store.create_task(facts, 400)

    assert created is True
    saved = json.loads(row["input_json"])
    package = FactPackage.from_dict(saved["fact_package"])
    assert package.report_type == "realtime_event_summary"
    assert package.rule_events[0]["event_id"] == notices[0].event_id
    assert package.rule_events[0]["condition_expression"] == "close <= stop_loss_price"
    assert package.rule_events[0]["cooldown_until"] is not None
    assert package.traceability["selected_event_check_id"] == notices[0].event_check_id
    assert '"fact_package"' in row["prompt_text"]
    assert "触发依据" in row["template_text"]
    assert "当前" in row["template_text"]
    assert "追溯：事件" in row["template_text"]
