from pathlib import Path

import pytest

from pandeng.app.cli import main
from pandeng.config.schema import polling_interval_label
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_minutes
from test_cleaner import minute_record


@pytest.mark.parametrize("seconds,expected", [
    (60, "1 分钟档"),
    (300, "5 分钟（默认）档"),
    (600, "10 分钟档"),
    (120, "自定义 120 秒（2 分钟）"),
    (90, "自定义 90 秒（1.5 分钟）"),
])
def test_polling_interval_labels(seconds: int, expected: str) -> None:
    assert polling_interval_label(seconds) == expected


def test_show_config_displays_presets_without_adding_yaml_keys(
    config_file: Path, capsys
) -> None:
    assert main(["--config", str(config_file), "show-config"]) == 0
    output = capsys.readouterr().out
    assert "检查间隔：300 秒；5 分钟（默认）档" in output
    assert "60 秒=1 分钟" in output and "600 秒=10 分钟" in output
    assert "其他 60～3600 秒合法值按自定义档处理" in output


def test_data_audit_cli_returns_success_for_completed_unknown_audit_and_stays_read_only(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    db = Database(tmp_path / "runtime" / "test.db")
    ingest_calendar(db, [{
        "trade_date": "2026-01-12", "is_open": True, "source": "audit_cli"
    }], adapter="audit_cli")
    ingest_minutes(db, [
        minute_record(stock_code="600000", bar_time="2026-01-12T09:31:00+08:00"),
        minute_record(stock_code="600000", bar_time="2026-01-12T09:33:00+08:00"),
    ], adapter="audit_cli", check_gaps=False)
    with db.connect() as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ingestion_runs", "data_quality_issues", "run_logs", "monitor_rounds")
        }

    assert main([
        *base, "data-audit", "--session-date", "2026-01-12",
        "--as-of-time", "2026-01-12T09:33:00+08:00", "--code", "600000",
    ]) == 0
    output = capsys.readouterr().out
    assert "分钟总体状态 unknown" in output
    assert "600000：仅价格线可用" in output
    assert "缺口 1" in output and "价格线 可用" in output
    assert "当日前复权日 K 缺失" in output
    assert "没有补数、改写结论、请求行情或调用模型" in output
    with db.connect() as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_data_audit_cli_rejects_code_outside_watchlist_and_bad_time(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    common = [*base, "data-audit", "--session-date", "2026-01-12"]

    assert main([
        *common, "--as-of-time", "2026-01-12T09:33:00+08:00", "--code", "999999"
    ]) == 1
    assert "必须属于当前 YAML" in capsys.readouterr().out

    assert main([*common, "--as-of-time", "2026-01-12 09:33:00"]) == 1
    assert "必须带时区" in capsys.readouterr().out
