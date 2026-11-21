from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pandeng.data.base import MinuteBar
from pandeng.desktop.data_manager import DataManagementError, DesktopDataManager
from pandeng.storage.repositories import ingest_minutes


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 5, 9, 31, tzinfo=timezone(timedelta(hours=8)))


def manager(tmp_path):
    result = DesktopDataManager(tmp_path / "data" / "pandeng.db", now_provider=lambda: NOW)
    result.initialize()
    return result


def add_old_minutes(result: DesktopDataManager):
    bars = [MinuteBar(code, OLD, 10, 11, 9, 10.5, 100, "desktop-test") for code in ("000001", "002156", "300750")]
    ingest_minutes(result.database, bars, adapter="desktop-test", check_gaps=False)
    with result.database.connect() as connection:
        return {row["stock_code"]: row["id"] for row in connection.execute("SELECT id, stock_code FROM minute_bars")}


def test_cleanup_protects_analysis_and_review_evidence(tmp_path) -> None:
    result = manager(tmp_path)
    ids = add_old_minutes(result)
    with result.database.connect() as connection:
        run_id = connection.execute(
            "INSERT INTO analysis_runs(session_date, stock_code, target_time, analysis_mode, status, input_hash, created_at) "
            "VALUES ('2026-01-05', '000001', ?, 'replay', 'ok', 'hash', ?)",
            (OLD.isoformat(), OLD.isoformat()),
        ).lastrowid
        connection.execute(
            "INSERT INTO indicator_snapshots(analysis_run_id, session_date, stock_code, data_time, "
            "latest_minute_bar_id, status, unknown_reasons_json, values_json, source_bar_ids_json, created_at) "
            "VALUES (?, '2026-01-05', '000001', ?, ?, 'ok', '[]', '{}', ?, ?)",
            (run_id, OLD.isoformat(), ids["000001"], f'[{ids["000001"]}]', OLD.isoformat()),
        )
        connection.execute(
            "INSERT INTO review_runs(session_date, stock_code, input_hash, input_json, local_report, data_status, created_at) "
            "VALUES ('2026-01-05', '300750', 'review-hash', '{}', 'report', 'unknown', ?)",
            (OLD.isoformat(),),
        )
        connection.execute(
            "INSERT INTO run_logs(level, event, message, created_at) VALUES ('INFO', 'old', 'old', '2026-01-01T00:00:00+00:00')"
        )
    preview = result.preview_cleanup(20, 30)
    assert preview.minute_ids == (ids["002156"],)
    assert preview.protected_minutes == 2
    assert len(preview.log_ids) == 1
    assert result.cleanup(preview, 20, 30) == (1, 1)
    with result.database.connect() as connection:
        remaining = {row[0] for row in connection.execute("SELECT stock_code FROM minute_bars")}
        assert remaining == {"000001", "300750"}
        assert connection.execute("SELECT COUNT(*) FROM run_logs WHERE event='desktop_cleanup'").fetchone()[0] == 1


def test_cleanup_preview_becomes_stale_when_data_changes(tmp_path) -> None:
    result = manager(tmp_path)
    add_old_minutes(result)
    preview = result.preview_cleanup(20, 30)
    ingest_minutes(
        result.database,
        [MinuteBar("600000", OLD, 1, 1, 1, 1, 1, "desktop-test")],
        adapter="desktop-test",
        check_gaps=False,
    )
    with pytest.raises(DataManagementError, match="重新预览"):
        result.cleanup(preview, 20, 30)


def test_sqlite_backup_preserves_committed_data_and_never_overwrites(tmp_path) -> None:
    result = manager(tmp_path)
    add_old_minutes(result)
    target = tmp_path / "backups" / "copy.db"
    result.backup(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 3
    with pytest.raises(DataManagementError, match="不会覆盖"):
        result.backup(target)


def test_backup_cannot_replace_live_database(tmp_path) -> None:
    result = manager(tmp_path)
    with pytest.raises(DataManagementError, match="不能覆盖"):
        result.backup(result.database.path)


def test_empty_database_statistics_and_zero_cleanup(tmp_path) -> None:
    result = manager(tmp_path)
    stats = result.statistics()
    assert stats["minute_bars"] == 0
    assert result.schema_version() == "6"
    preview = result.preview_cleanup(20, 30)
    assert preview.total == 0
    assert result.cleanup(preview, 20, 30) == (0, 0)


def test_backup_reopens_with_same_rows_and_leaves_source_unchanged(tmp_path) -> None:
    result = manager(tmp_path)
    add_old_minutes(result)
    with result.database.connect() as connection:
        before = [tuple(row) for row in connection.execute(
            "SELECT id, stock_code, bar_time, close FROM minute_bars ORDER BY id"
        )]
        schema_before = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]

    target = result.backup(tmp_path / "backups" / "verified.db")

    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as connection:
        copied = [tuple(row) for row in connection.execute(
            "SELECT id, stock_code, bar_time, close FROM minute_bars ORDER BY id"
        )]
        assert copied == before
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == schema_before == "6"
    with result.database.connect() as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT id, stock_code, bar_time, close FROM minute_bars ORDER BY id"
        )] == before
