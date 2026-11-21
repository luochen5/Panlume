from pathlib import Path

from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_daily, ingest_minutes, table_count
from test_cleaner import daily_record, minute_record


def initialized_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "runtime" / "market.db")
    database.initialize()
    return database


def test_exact_duplicates_are_idempotent(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    records = [
        minute_record(bar_time=f"2026-01-05T09:3{minute}:00+08:00")
        for minute in (1, 2, 3)
    ]

    first = ingest_minutes(database, records, adapter="test")
    second = ingest_minutes(database, records, adapter="test")

    assert first.status == "ok" and first.accepted == 3
    assert second.status == "ok" and second.duplicates == 3
    assert table_count(database, "minute_bars") == 3
    with database.connect() as connection:
        issue_types = {
            row[0]
            for row in connection.execute(
                "SELECT issue_type FROM data_quality_issues"
            ).fetchall()
        }
    assert issue_types == {"exact_duplicate"}


def test_minute_gap_marks_code_unknown(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    records = [
        minute_record(bar_time="2026-01-05T09:31:00+08:00"),
        minute_record(bar_time="2026-01-05T09:33:00+08:00"),
    ]

    summary = ingest_minutes(database, records, adapter="test")

    assert summary.status == "unknown"
    assert summary.unknown_codes == ("600000",)
    with database.connect() as connection:
        issue = connection.execute(
            "SELECT issue_type FROM data_quality_issues"
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT status FROM processing_checkpoints WHERE stream_key = 'minute:600000'"
        ).fetchone()
    assert issue[0] == "minute_gap"
    assert checkpoint[0] == "unknown"


def test_conflicting_duplicate_is_not_overwritten(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    ingest_minutes(database, [minute_record()], adapter="test")

    conflict = minute_record(close="10.15")
    summary = ingest_minutes(database, [conflict], adapter="test")

    assert summary.status == "unknown"
    assert summary.rejected == 1
    with database.connect() as connection:
        stored_close = connection.execute(
            "SELECT close FROM minute_bars"
        ).fetchone()[0]
        issue_type = connection.execute(
            "SELECT issue_type FROM data_quality_issues ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert stored_close == 10.10
    assert issue_type == "conflicting_duplicate"


def test_invalid_record_without_code_still_makes_run_unknown(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    summary = ingest_minutes(
        database, [minute_record(stock_code="bad")], adapter="test"
    )
    assert summary.status == "unknown"
    assert summary.rejected == 1


def test_invalid_record_for_code_marks_valid_part_unknown(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    invalid = minute_record()
    invalid.pop("close")
    summary = ingest_minutes(database, [minute_record(), invalid], adapter="test")

    assert summary.accepted == 1
    assert summary.rejected == 1
    assert summary.unknown_codes == ("600000",)


def test_unconfirmed_daily_is_saved_but_not_marked_confirmed(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    summary = ingest_daily(
        database, [daily_record(confirmed=False)], adapter="test"
    )
    assert summary.status == "ok"
    with database.connect() as connection:
        confirmed = connection.execute(
            "SELECT confirmed FROM daily_qfq_bars"
        ).fetchone()[0]
    assert confirmed == 0


def test_daily_confirmation_can_be_promoted_without_price_change(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    ingest_daily(database, [daily_record(confirmed=False)], adapter="test")

    promoted = ingest_daily(database, [daily_record(confirmed=True)], adapter="test")

    assert promoted.status == "ok" and promoted.rejected == 0
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM daily_qfq_bars").fetchone()
        issue = connection.execute(
            "SELECT * FROM data_quality_issues WHERE issue_type = 'confirmation_promoted'"
        ).fetchone()
    assert row["confirmed"] == 1 and row["close"] == 10.1
    assert issue is not None and "before" in issue["raw_json"]


def test_daily_confirmation_promotion_still_rejects_price_conflict(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    ingest_daily(database, [daily_record(confirmed=False)], adapter="test")

    conflict = ingest_daily(
        database, [daily_record(confirmed=True, close=10.2)], adapter="test"
    )

    assert conflict.status == "unknown" and conflict.rejected == 1
    with database.connect() as connection:
        row = connection.execute("SELECT confirmed, close FROM daily_qfq_bars").fetchone()
    assert tuple(row) == (0, 10.1)
