from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from pandeng.app.cli import main
from pandeng.config.loader import load_config
from pandeng.data.akshare_demo import LIVE_CONFIRMATION, run_akshare_live_minute
from pandeng.storage.db import Database


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"


def fixture_rows() -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"])


def save_config(path: Path, raw: dict, database_name: str) -> None:
    raw["storage"]["database"] = f"runtime/{database_name}"
    raw["models"]["ollama"]["enabled"] = False
    raw["models"]["cloud"]["enabled"] = False
    raw["watchlist"] = [{"code": "000001", "name": "C2 独立测试"}]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def table_counts(database: Database) -> dict[str, int]:
    tables = (
        "minute_bars", "daily_qfq_bars", "trading_calendar", "data_quality_issues",
        "model_calls", "position_lots",
    )
    with database.connect() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_fixture_demo_end_to_end_raw_trace_and_unconfirmed_audit(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_config(config_file, raw_config, "akshare-fixture-test.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "eastmoney-fixture-demo", "--fixture-file", str(FIXTURE)
    ]) == 0
    output = capsys.readouterr().out
    assert "原始响应 241 行；常规分钟 240；新增 240" in output
    assert "09:30 特殊点单独保留 1 行" in output
    assert "零成交量常规行 1 条，未删除" in output
    assert "confirmed_through 显式为 None；已确认 0；未确认 240" in output
    assert "本次没有 HTTP 请求，不导入 AKShare" in output

    database = Database(tmp_path / "runtime/akshare-fixture-test.db")
    counts = table_counts(database)
    assert counts == {
        "minute_bars": 240,
        "daily_qfq_bars": 0,
        "trading_calendar": 0,
        "data_quality_issues": 0,
        "model_calls": 0,
        "position_lots": 0,
    }
    with database.connect() as connection:
        first = connection.execute(
            "SELECT * FROM minute_bars ORDER BY bar_time LIMIT 1"
        ).fetchone()
        last = connection.execute(
            "SELECT * FROM minute_bars ORDER BY bar_time DESC LIMIT 1"
        ).fetchone()
        zero = connection.execute("SELECT * FROM minute_bars WHERE volume = 0").fetchone()
        confirmed = connection.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE confirmed = 1"
        ).fetchone()[0]
        at_1300 = connection.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE bar_time LIKE '%T13:00:%'"
        ).fetchone()[0]
    assert first["bar_time"] == "2026-11-17T09:31:00+08:00"
    assert first["volume"] == 4888800
    assert first["source"] == "eastmoney_post_close_capture_offline"
    raw = json.loads(first["raw_json"])
    assert raw["provider_raw"]["时间"] == "2026-11-17 09:31:00"
    assert raw["provider_request"] == {
        "symbol": "000001",
        "start_date": "2026-11-17 09:30:00",
        "end_date": "2026-11-17 15:00:00",
        "period": "1",
        "adjust": "",
    }
    assert last["bar_time"] == "2026-11-17T15:00:00+08:00"
    assert zero["bar_time"] == "2026-11-17T14:59:00+08:00"
    assert confirmed == 0
    assert at_1300 == 0

    assert main([
        *base, "data-audit", "--session-date", "2026-11-17",
        "--as-of-time", "2026-11-17T15:00:00+08:00",
    ]) == 0
    audit = capsys.readouterr().out
    assert "分钟总体状态 unknown" in audit
    assert "分钟已保存 240/240；已确认 0；未确认 240；缺口 0；质量错误 0" in audit
    assert "当日前复权日 K 缺失" in audit


def test_fixture_demo_refuses_rerun_and_main_database(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_config(config_file, raw_config, "akshare-fixture-test.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    arguments = [*base, "eastmoney-fixture-demo", "--fixture-file", str(FIXTURE)]
    assert main([*base, "init"]) == 0
    assert main(arguments) == 0
    capsys.readouterr()
    assert main(arguments) == 1
    assert "只使用全新的 akshare-fixture-test.db" in capsys.readouterr().out
    database = Database(tmp_path / "runtime/akshare-fixture-test.db")
    assert table_counts(database)["minute_bars"] == 240
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1


def test_fixture_demo_rejects_main_database_before_reading_sample(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_config(config_file, raw_config, "pandeng.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "eastmoney-fixture-demo", "--fixture-file", str(tmp_path / "missing.json")
    ]) == 1
    assert "只允许写入 akshare-fixture-test.db" in capsys.readouterr().out
    assert table_counts(Database(tmp_path / "runtime/pandeng.db"))["minute_bars"] == 0


def test_live_runner_with_injected_response_writes_only_live_test_db_and_audit(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_config(config_file, raw_config, "akshare-live-test.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    config = load_config(config_file)
    outcome = run_akshare_live_minute(
        config,
        session_date=date(2026, 11, 17),
        stock_code="000001",
        confirmation=LIVE_CONFIRMATION,
        confirmed_through=None,
        fetcher=lambda **kwargs: fixture_rows(),
    )
    assert outcome.request_count == 1
    assert outcome.mapping.raw_row_count == 241
    assert len(outcome.mapping.records) == 240
    assert outcome.audit_path.is_file()
    audit = json.loads(outcome.audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "ingested"
    assert audit["request_count_planned"] == 1
    assert audit["request_count_actual"] == 1
    assert audit["retry_count"] == 0
    assert audit["raw_row_count"] == 241
    assert len(audit["raw_rows"]) == 241
    assert audit["excluded_0930_count"] == 1
    assert audit["excluded_0930"][0]["provider_raw"]["时间"] == "2026-11-17 09:30:00"
    assert audit["database_written"] is True
    assert audit["model_called"] is False
    assert audit["position_written"] is False
    database = Database(tmp_path / "runtime/akshare-live-test.db")
    assert table_counts(database)["minute_bars"] == 240
    with database.connect() as connection:
        sources = {row[0] for row in connection.execute("SELECT DISTINCT source FROM minute_bars")}
        confirmed = connection.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE confirmed = 1"
        ).fetchone()[0]
    assert sources == {"akshare_eastmoney"}
    assert confirmed == 0
    assert not (tmp_path / "runtime/pandeng.db").exists()


def test_live_wrong_confirmation_makes_no_request_or_audit(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_config(config_file, raw_config, "akshare-live-test.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "akshare-live-minute", "--session-date", "2026-11-17", "--code", "000001",
        "--confirmed-through", "none", "--confirmation", "错误确认",
    ]) == 1
    assert "没有发起请求或写数据库" in capsys.readouterr().out
    database = Database(tmp_path / "runtime/akshare-live-test.db")
    assert table_counts(database)["minute_bars"] == 0
    assert not (tmp_path / "runtime/akshare-live-audit").exists()


def test_live_empty_response_is_not_ingested_or_checkpointed(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    save_config(config_file, raw_config, "akshare-live-test.db")
    monkeypatch.chdir(tmp_path)
    config = load_config(config_file)
    Database(config.storage.database).initialize()
    with pytest.raises(ValueError, match="empty_data"):
        run_akshare_live_minute(
            config,
            session_date=date(2026, 11, 17),
            stock_code="000001",
            confirmation=LIVE_CONFIRMATION,
            confirmed_through=None,
            fetcher=lambda **kwargs: [],
        )
    database = Database(tmp_path / "runtime/akshare-live-test.db")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM processing_checkpoints").fetchone()[0] == 0
    audits = list((tmp_path / "runtime/akshare-live-audit").glob("*.json"))
    assert len(audits) == 1
    payload = json.loads(audits[0].read_text(encoding="utf-8"))
    assert payload["status"] == "request_failed"
    assert payload["error_category"] == "empty_data"
    assert payload["database_written"] is False


def test_shipped_c2_configs_load_without_network_or_new_keys() -> None:
    fixture = load_config(ROOT / "configs/akshare-fixture-test.yaml")
    live = load_config(ROOT / "configs/akshare-live-test.yaml")
    assert fixture.storage.database.name == "akshare-fixture-test.db"
    assert live.storage.database.name == "akshare-live-test.db"
    for config in (fixture, live):
        assert config.monitor.interval_seconds == 300
        assert config.ollama.enabled is False
        assert config.cloud.enabled is False
        assert [item.code for item in config.watchlist] == ["000001"]
