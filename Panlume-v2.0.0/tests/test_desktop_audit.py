from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.audit import DesktopAuditService
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes
from test_cleaner import daily_record, minute_record


DAY = "2026-01-12"
CODE = "600000"


def minute(minute_value: int, **changes):
    return minute_record(
        stock_code=CODE,
        bar_time=f"{DAY}T09:{minute_value:02d}:00+08:00",
        confirmed=True, source="desktop_audit_fixture", **changes,
    )


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "desktop-audit.db")
    database.initialize()
    ingest_calendar(database, [{
        "trade_date": DAY, "is_open": True, "source": "desktop_audit_fixture",
    }], adapter="desktop_audit_fixture")
    ingest_minutes(
        database, [minute(31), minute(32), minute(33)],
        adapter="desktop_audit_fixture",
    )
    ingest_daily(database, [daily_record(
        stock_code=CODE, trade_date=DAY, confirmed=True,
    )], adapter="desktop_audit_fixture")
    return database


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


def test_complete_desktop_audit_reads_sources_and_runs_without_any_write(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    before = table_snapshot(database)

    result = DesktopAuditService(database).run([CODE], DAY, "09:33")

    item = result.report.codes[0]
    assert result.report.status == "ok"
    assert (item.saved_minutes, item.expected_minutes, item.confirmed_minutes) == (3, 3, 3)
    assert item.sources == ("desktop_audit_fixture",)
    assert item.price_line_available and item.daily_status == "confirmed"
    assert result.issues == ()
    assert {run.data_kind for run in result.runs} == {"calendar", "minute", "daily_qfq"}
    assert all(run.adapter == "desktop_audit_fixture" for run in result.runs)
    assert "本次只读 SQLite" in result.summary
    assert table_snapshot(database) == before


def test_gap_and_empty_data_keep_core_unknown_semantics(tmp_path: Path) -> None:
    gap = Database(tmp_path / "gap.db")
    gap.initialize()
    ingest_calendar(gap, [{
        "trade_date": DAY, "is_open": True, "source": "desktop_audit_fixture",
    }], adapter="desktop_audit_fixture")
    ingest_minutes(
        gap, [minute(31), minute(33)], adapter="desktop_audit_fixture", check_gaps=False,
    )
    result = DesktopAuditService(gap).run([CODE], DAY, "09:33")
    assert result.report.codes[0].status == "price_only"
    assert len(result.report.codes[0].missing_times) == 1
    assert "缺口示例" in result.summary

    empty = Database(tmp_path / "empty.db")
    empty.initialize()
    empty_result = DesktopAuditService(empty).run([CODE], DAY, "09:33")
    assert empty_result.report.status == "unknown"
    assert "目标分钟缺失" in empty_result.summary
    assert empty_result.issues == () and empty_result.runs == ()


def test_quality_issue_exposes_raw_json_and_linked_ingestion_run(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    with database.connect() as connection:
        run_id = int(connection.execute(
            """
            INSERT INTO ingestion_runs(
                data_kind, adapter, status, received_count, accepted_count,
                duplicate_count, rejected_count, created_at
            ) VALUES ('minute', 'desktop_issue_fixture', 'unknown', 1, 0, 0, 1, ?)
            """,
            (f"{DAY}T02:00:00+00:00",),
        ).lastrowid)
        issue_id = int(connection.execute(
            """
            INSERT INTO data_quality_issues(
                run_id, record_index, data_kind, stock_code, data_time,
                issue_type, severity, message, raw_json, created_at
            ) VALUES (?, 0, 'minute', ?, ?, 'desktop_conflict', 'error',
                      '桌面审计样本冲突', '{"before": 1, "after": 2}', ?)
            """,
            (run_id, CODE, f"{DAY}T09:33:00+08:00", f"{DAY}T02:00:00+00:00"),
        ).lastrowid)
    before = table_snapshot(database)

    result = DesktopAuditService(database).run([CODE], DAY, "09:33")

    assert result.report.codes[0].quality_error_count == 1
    assert result.issues[0].issue_id == issue_id
    assert result.issues[0].run_id == run_id
    assert '"before": 1' in result.issues[0].detail
    assert result.issues[0].severity == "error"
    assert any(run.run_id == run_id and run.adapter == "desktop_issue_fixture" for run in result.runs)
    assert table_snapshot(database) == before


@pytest.mark.parametrize(
    ("codes", "day", "time_text", "message"),
    [
        ([], DAY, "09:33", "至少"),
        (["60000"], DAY, "09:33", "6 位数字"),
        ([CODE], "20260112", "09:33", "YYYY-MM-DD"),
        ([CODE], DAY, "9:33", "HH:MM"),
        ([CODE], DAY, "12:00", "整分钟"),
        ([CODE], DAY, "09:30", "整分钟"),
        ([CODE], "2026-02-30", "09:33", "无效"),
    ],
)
def test_invalid_desktop_audit_input_rejected_before_core(
    tmp_path: Path, codes: list[str], day: str, time_text: str, message: str,
) -> None:
    database = Database(tmp_path / "invalid.db")
    database.initialize()
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("非法输入不应进入核心审计")

    service = DesktopAuditService(database, auditor=forbidden)
    before = table_snapshot(database)
    with pytest.raises(ValueError, match=message):
        service.run(codes, day, time_text)
    assert called is False
    assert table_snapshot(database) == before


def test_desktop_audit_does_not_construct_or_call_models(tmp_path: Path, monkeypatch) -> None:
    database = make_database(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("只读体检不应调用模型")

    monkeypatch.setattr("pandeng.summarizer.ollama.OllamaClient.generate", forbidden)
    monkeypatch.setattr("pandeng.review.cloud.OpenAICompatibleCloudClient.generate", forbidden)
    result = DesktopAuditService(database).run([CODE], DAY, "09:33")
    assert result.report.status == "ok"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0
