from __future__ import annotations

import json
from pathlib import Path

import pytest

from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes, table_count
from test_cleaner import minute_record


def initialized_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def test_minute_confirmation_promotion_is_audited_and_exact_repeat_is_deduplicated(
    tmp_path: Path,
) -> None:
    database = initialized_database(tmp_path / "runtime" / "minute-confirmation-test.db")
    ingest_minutes(
        database,
        [minute_record(confirmed=False, provider_request={"sequence": 1})],
        adapter="test",
    )

    promoted_record = minute_record(confirmed=True, provider_request={"sequence": 2})
    promoted = ingest_minutes(database, [promoted_record], adapter="test")
    repeated = ingest_minutes(database, [promoted_record], adapter="test")

    assert promoted.status == "ok" and promoted.rejected == 0
    assert repeated.status == "ok" and repeated.duplicates == 1
    assert table_count(database, "minute_bars") == 1
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM minute_bars").fetchone()
        issue = connection.execute(
            "SELECT * FROM data_quality_issues WHERE issue_type = 'confirmation_promoted'"
        ).fetchone()
    assert row["confirmed"] == 1 and row["ingestion_run_id"] == promoted.run_id
    assert issue is not None and issue["severity"] == "info"
    audit = json.loads(issue["raw_json"])
    assert audit["before"]["confirmed"] is False
    assert audit["after"]["confirmed"] is True
    assert audit["before"]["provider_request"] == {"sequence": 1}
    assert audit["after"]["provider_request"] == {"sequence": 2}
    assert json.loads(row["raw_json"])["provider_request"] == {"sequence": 2}


@pytest.mark.parametrize(
    "changes",
    [
        {"open": "10.01"},
        {"high": "10.21"},
        {"low": "9.91"},
        {"close": "10.11"},
        {"volume": "1001"},
        {"source": "another_source"},
        {"adjustment": "qfq"},
    ],
    ids=["open", "high", "low", "close", "volume", "source", "adjustment"],
)
def test_minute_confirmation_cannot_modify_saved_market_facts(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    database = initialized_database(tmp_path / "runtime" / "minute-conflict-test.db")
    original = minute_record(confirmed=False)
    ingest_minutes(database, [original], adapter="test")

    conflict = ingest_minutes(
        database,
        [minute_record(confirmed=True, **changes)],
        adapter="test",
    )

    assert conflict.status == "unknown" and conflict.rejected == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT open, high, low, close, volume, source, adjustment, confirmed "
            "FROM minute_bars"
        ).fetchone()
    assert tuple(row) == (10.0, 10.2, 9.9, 10.1, 1000, "test", "none", 0)


def test_minute_confirmation_cannot_move_back_to_unconfirmed(tmp_path: Path) -> None:
    database = initialized_database(tmp_path / "runtime" / "minute-rollback-test.db")
    ingest_minutes(database, [minute_record(confirmed=True)], adapter="test")

    rollback = ingest_minutes(database, [minute_record(confirmed=False)], adapter="test")

    assert rollback.status == "unknown" and rollback.rejected == 1
    with database.connect() as connection:
        row = connection.execute("SELECT confirmed, raw_json FROM minute_bars").fetchone()
        issue = connection.execute(
            "SELECT issue_type FROM data_quality_issues ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["confirmed"] == 1 and json.loads(row["raw_json"])["confirmed"] is True
    assert issue[0] == "conflicting_duplicate"


def test_minute_promotion_writes_only_supplied_database_and_never_changes_positions(
    tmp_path: Path,
) -> None:
    main = initialized_database(tmp_path / "runtime" / "pandeng.db")
    add_position_lot(
        main,
        stock_code="600000",
        quantity=100,
        buy_date="2026-01-05",
        stop_loss_price=9.5,
        confirmation=CONFIRM_ADD,
    )
    before_lots = [dict(row) for row in main.list_position_lots()]
    isolated = initialized_database(tmp_path / "isolated" / "minute-confirmation-test.db")
    ingest_minutes(isolated, [minute_record(confirmed=False)], adapter="test")

    result = ingest_minutes(isolated, [minute_record(confirmed=True)], adapter="test")

    assert result.status == "ok"
    assert [dict(row) for row in main.list_position_lots()] == before_lots
    assert table_count(main, "minute_bars") == 0
    with main.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM position_audit").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
