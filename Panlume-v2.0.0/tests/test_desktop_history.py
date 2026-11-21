from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import RulesConfig
from pandeng.data.base import MinuteBar
from pandeng.data.schedule import session_minutes
from pandeng.desktop.history import DesktopHistoryService
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes
from pandeng.summarizer.content import load_summary_facts
from pandeng.summarizer.service import CallRecord, SummaryStore


CODE = "600000"
DAY = date(2026, 1, 5)


def prepare_notices(database: Database):
    database.initialize()
    points = session_minutes(DAY)
    ingest_minutes(database, [
        MinuteBar(CODE, point, 9, 9, 9, 9, 100, "desktop_history_fixture")
        for point in points[:2]
    ], adapter="desktop_history_fixture", check_gaps=False)
    add_position_lot(
        database, stock_code=CODE, quantity=100, buy_date=DAY.isoformat(),
        stop_loss_price=9, confirmation=CONFIRM_ADD,
    )
    analyzer = AnalysisService(database, rules=RulesConfig(
        rapid_move_pct=20, normal_cooldown_seconds=60,
        high_priority_repeat_seconds=60,
    ))
    notices = []
    for point in points[:2]:
        result = analyzer.analyze_code(CODE, point, analysis_mode="live")
        notices.extend(item for item in result.notices if item.event_type == "fixed_stop")
    assert len(notices) == 2
    return notices


def populate_history(database: Database) -> None:
    notices = prepare_notices(database)
    store = SummaryStore(database)
    facts = load_summary_facts(database, notices[0])
    task, created = store.create_task(facts, 400)
    assert created and store.mark_running(int(task["id"]))
    call = CallRecord(
        status="succeeded", model_name="deepseek-r1:14b",
        request_json="请求_SECRET", response_text="原始响应_SECRET",
        thinking_text="思考_SECRET", error_type=None, error_message=None,
        elapsed_ms=123, total_duration_ns=456, load_duration_ns=45,
        eval_count=7, eval_duration_ns=89,
        started_at="2026-01-05T01:32:00+00:00",
        finished_at="2026-01-05T01:32:00.123000+00:00",
    )
    assert store.finish_model(
        int(task["id"]), f"{CODE} 固定止损模型整理样本。", call=call,
    )
    fallback_facts = load_summary_facts(database, notices[1])
    fallback, created = store.create_task(fallback_facts, 400)
    assert created
    assert store.finish_fallback(int(fallback["id"]), "model_disabled")


def table_snapshot(database: Database) -> dict[str, list[tuple]]:
    with database.connect() as connection:
        names = [
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
            )
        ]
        return {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in names
        }


def test_history_reads_complete_event_summary_and_call_chain_without_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.db")
    populate_history(database)
    before = table_snapshot(database)

    result = DesktopHistoryService(database).run()

    assert len(result.events) == 1
    assert len(result.summaries) == 2
    event = result.events[0]
    assert event.event_type == "fixed_stop" and event.check_count == 2
    assert event.reminder_count == 2 and "固定止损" in event.detail
    model = next(item for item in result.summaries if item.summary_kind == "model")
    fallback = next(item for item in result.summaries if item.summary_kind == "template")
    assert model.event_id == fallback.event_id == event.event_id
    assert model.event_check_id != fallback.event_check_id
    assert model.snapshot_id > 0 and model.analysis_run_id > 0
    assert len(model.calls) == 1 and model.calls[0].elapsed_ms == 123
    assert fallback.calls == () and fallback.fallback_reason == "model_disabled"
    assert "输入事实 JSON" in model.detail and "payload JSON" in model.detail
    assert "模型整理样本" in model.detail and "123 ms" in model.detail
    assert "无模型调用" in fallback.detail
    assert "模型摘要 1；模板回退 1；未完成 0" in result.summary
    assert table_snapshot(database) == before


def test_history_does_not_expand_raw_request_response_or_thinking(tmp_path: Path) -> None:
    database = Database(tmp_path / "redaction.db")
    populate_history(database)
    result = DesktopHistoryService(database).run()
    model = next(item for item in result.summaries if item.summary_kind == "model")
    assert "请求_SECRET" not in model.detail
    assert "原始响应_SECRET" not in model.detail
    assert "思考_SECRET" not in model.detail
    assert "不展开模型请求、原始响应或 R1 思考文本" in model.detail


def test_history_filters_by_code_date_result_and_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "filters.db")
    populate_history(database)
    service = DesktopHistoryService(database)
    assert len(service.run(CODE, DAY.isoformat(), "model").summaries) == 1
    assert len(service.run(CODE, DAY.isoformat(), "template").summaries) == 1
    assert service.run(CODE, DAY.isoformat(), "unfinished").summaries == ()
    empty = service.run("000001", DAY.isoformat())
    assert empty.events == () and empty.summaries == ()
    other_day = service.run(CODE, "2026-01-06")
    assert other_day.events == () and other_day.summaries == ()
    limited = service.run(limit=1)
    assert len(limited.events) == 1 and len(limited.summaries) == 1


def test_pending_history_is_unfinished_and_has_no_model_call(tmp_path: Path) -> None:
    database = Database(tmp_path / "pending.db")
    notices = prepare_notices(database)
    store = SummaryStore(database)
    facts = load_summary_facts(database, notices[0])
    task, created = store.create_task(facts, 400)
    assert created and task["status"] == "pending"
    before = table_snapshot(database)

    result = DesktopHistoryService(database).run(result_kind="unfinished")

    assert len(result.summaries) == 1
    item = result.summaries[0]
    assert item.status == "pending" and item.summary_kind is None
    assert item.summary_text is None and item.calls == ()
    assert "尚未生成最终摘要" in item.detail
    assert "模型摘要 0；模板回退 0；未完成 1" in result.summary
    assert table_snapshot(database) == before


def test_empty_history_is_explicit_and_read_only(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    before = table_snapshot(database)
    result = DesktopHistoryService(database).run()
    assert result.events == () and result.summaries == ()
    assert "规则事件 0 条；摘要任务 0 条" in result.summary
    assert "仅读取 SQLite" in result.summary
    assert table_snapshot(database) == before


@pytest.mark.parametrize(
    ("code", "day", "kind", "limit", "message"),
    [
        ("60000", "", "all", 100, "6 位"),
        (CODE, "20260105", "all", 100, "YYYY-MM-DD"),
        (CODE, "2026-02-30", "all", 100, "有效日历日期"),
        (CODE, "", "unknown", 100, "结果筛选"),
        (CODE, "", "all", 0, "1～100"),
        (CODE, "", "all", 101, "1～100"),
        (CODE, "", "all", True, "1～100"),
    ],
)
def test_invalid_history_filters_rejected_before_database_query(
    tmp_path: Path, monkeypatch, code: str, day: str, kind: str, limit: int, message: str,
) -> None:
    database = Database(tmp_path / "invalid.db")
    database.initialize()
    service = DesktopHistoryService(database)

    def forbidden():
        raise AssertionError("非法筛选不应打开历史查询连接")

    monkeypatch.setattr(database, "connect", forbidden)
    with pytest.raises(ValueError, match=message):
        service.run(code, day, kind, limit)


def test_history_does_not_call_ollama_or_cloud(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "zero-requests.db")
    populate_history(database)

    def forbidden(*args, **kwargs):
        raise AssertionError("只读历史不应调用模型")

    monkeypatch.setattr("pandeng.summarizer.ollama.OllamaClient.generate", forbidden)
    monkeypatch.setattr("pandeng.review.cloud.OpenAICompatibleCloudClient.generate", forbidden)
    result = DesktopHistoryService(database).run()
    assert len(result.summaries) == 2
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0
