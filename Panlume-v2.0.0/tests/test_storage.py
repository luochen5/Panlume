import shutil
import sqlite3
from datetime import date
from pathlib import Path

from pandeng.config.schema import parse_config
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.storage.db import Database
from pandeng.summarizer.demo import run_summary_demo


def test_initialize_is_repeatable_and_starts_empty(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime" / "test.db")

    assert database.is_initialized() is False
    database.initialize()
    database.initialize()

    assert database.is_initialized() is True
    assert database.metadata("schema_version") == "6"
    assert database.metadata("initialized_at") is not None
    assert database.list_position_lots() == []

    with database.connect() as connection:
        log_count = connection.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0]
    assert log_count == 2


def test_version_2_upgrade_preserves_confirmed_position(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "old.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE app_metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE position_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL CHECK (length(stock_code) = 6),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                buy_date TEXT NOT NULL,
                stop_loss_price REAL CHECK (stop_loss_price IS NULL OR stop_loss_price > 0),
                confirmed_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL, event TEXT NOT NULL,
                message TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO app_metadata VALUES ('schema_version', '2', '2026-01-01T00:00:00+00:00');
            INSERT INTO position_lots(stock_code, quantity, buy_date, stop_loss_price, confirmed_at)
            VALUES ('600000', 100, '2026-01-02', 9.5, '2026-01-02T10:00:00+08:00');
            """
        )
    database = Database(path)

    database.initialize()

    assert database.metadata("schema_version") == "6"
    lots = database.list_position_lots()
    assert len(lots) == 1 and lots[0]["stock_code"] == "600000"
    assert lots[0]["quantity"] == 100 and lots[0]["stop_loss_price"] == 9.5
    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM monitor_state"
        ).fetchone() is None


def test_version_4_to_current_upgrade_on_database_copy_preserves_source_and_holdings(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "version-4-source.db"
    source = Database(source_path)
    source.initialize()
    add_position_lot(
        source,
        stock_code="600000",
        quantity=100,
        buy_date="2026-01-02",
        stop_loss_price=9.5,
        confirmation=CONFIRM_ADD,
    )
    with source.connect() as connection:
        # 从当前完整结构构造版本 4 夹具；摘要和复盘表都属于后续结构。
        connection.execute("DROP TABLE cloud_review_calls")
        connection.execute("DROP TABLE review_runs")
        connection.execute("DROP TABLE model_calls")
        connection.execute("DROP TABLE summary_results")
        connection.execute("DROP TABLE summary_tasks")
        connection.execute(
            "UPDATE app_metadata SET value = '4' WHERE key = 'schema_version'"
        )

    copied_path = tmp_path / "version-4-copy.db"
    shutil.copy2(source_path, copied_path)
    copied = Database(copied_path)
    copied.initialize()

    assert source.metadata("schema_version") == "4"
    assert copied.metadata("schema_version") == "6"
    assert copied.list_position_lots()[0]["quantity"] == 100
    assert copied.list_position_lots()[0]["stop_loss_price"] == 9.5
    with source.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'summary_tasks'"
        ).fetchone() is None
    with copied.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'summary_tasks'"
        ).fetchone() is not None
        assert connection.execute("SELECT COUNT(*) FROM position_audit").fetchone()[0] == 1



def test_version_5_to_6_upgrade_on_copy_preserves_business_and_summary_data(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    source_path = tmp_path / "runtime" / "summary-test.db"
    raw_config["storage"]["database"] = "runtime/summary-test.db"
    raw_config["watchlist"] = [{"code": "600000", "name": "版本 5 副本"}]
    raw_config["models"]["ollama"]["enabled"] = False
    config = parse_config(raw_config, tmp_path)
    source = Database(config.storage.database)
    source.initialize()
    run_summary_demo(config, date(2026, 1, 8))
    capsys.readouterr()
    with source.connect() as connection:
        # 用真实旧业务表构造版本 5 源库；源文件随后保持版本 5，不执行升级。
        connection.execute("DROP TABLE cloud_review_calls")
        connection.execute("DROP TABLE review_runs")
        connection.execute(
            "UPDATE app_metadata SET value = '5' WHERE key = 'schema_version'"
        )
        tables = (
            "minute_bars", "ingestion_runs", "analysis_runs", "indicator_snapshots",
            "rule_events", "rule_event_checks", "summary_tasks", "summary_results",
            "model_calls", "position_lots", "position_audit",
        )
        before = {
            table: [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            )]
            for table in tables
        }
    assert before["minute_bars"] and before["summary_tasks"]
    assert before["summary_results"] and before["model_calls"]
    assert before["position_lots"] and before["position_audit"]

    copied_path = tmp_path / "version-5-copy.db"
    shutil.copy2(source_path, copied_path)
    copied = Database(copied_path)
    copied.initialize()

    assert source_path != copied_path
    assert source.metadata("schema_version") == "5"
    assert copied.metadata("schema_version") == "6"
    with source.connect() as connection:
        for table in ("review_runs", "cloud_review_calls"):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone() is None
        source_after = {
            table: [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            )]
            for table in tables
        }
    assert source_after == before
    with copied.connect() as connection:
        for table in ("review_runs", "cloud_review_calls"):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone() is not None
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        copied_after = {
            table: [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            )]
            for table in tables
        }
    assert copied_after == before
