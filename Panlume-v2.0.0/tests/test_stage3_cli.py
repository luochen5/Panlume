from __future__ import annotations

from datetime import date
from pathlib import Path

from pandeng.app.cli import main
from pandeng.data.base import DailyBar, MinuteBar, TradingDay
from pandeng.data.schedule import recent_weekdays, session_minutes
from pandeng.holdings.positions import CONFIRM_ADD, CONFIRM_ARCHIVE
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes


def test_holdings_add_t1_archive_cli(config_file: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    wrong = [
        *base, "holdings", "add", "--code", "600000", "--quantity", "100",
        "--buy-date", "2026-01-05", "--stop-loss", "9.5", "--confirmation", "确认",
    ]
    assert main(wrong) == 1
    assert "必须逐字确认" in capsys.readouterr().out

    correct = [*wrong[:-1], CONFIRM_ADD]
    assert main(correct) == 0
    assert "position_audit" in capsys.readouterr().out
    database = Database((tmp_path / "runtime" / "test.db").resolve())
    ingest_calendar(
        database,
        [
            TradingDay(date(2026, 1, 5), True, "test"),
            TradingDay(date(2026, 1, 6), True, "test"),
        ],
        adapter="test",
    )
    assert main([*base, "holdings", "t1", "--code", "600000", "--as-of-date", "2026-01-05"]) == 0
    same_day = capsys.readouterr().out
    assert "可卖数量 0" in same_day and "状态 ok" in same_day
    assert main([*base, "holdings", "t1", "--code", "600000", "--as-of-date", "2026-01-06"]) == 0
    assert "可卖数量 100" in capsys.readouterr().out

    archive = [
        *base, "holdings", "archive", "--lot-id", "1", "--reason", "命令行归档验收",
        "--confirmation", CONFIRM_ARCHIVE,
    ]
    assert main(archive) == 0
    assert "原始记录和审计历史均未删除" in capsys.readouterr().out
    assert main([*base, "holdings", "list"]) == 0
    assert "已归档：命令行归档验收" in capsys.readouterr().out


def test_replay_and_daily_analysis_cli(config_file: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    database = Database((tmp_path / "runtime" / "test.db").resolve())
    points = session_minutes(date(2026, 1, 5))
    ingest_minutes(
        database,
        [MinuteBar("600000", point, 10, 10.1, 9.9, 10, 100, "test") for point in points[:2]],
        adapter="test",
    )
    days = recent_weekdays(date(2026, 1, 5), 20)
    ingest_daily(
        database,
        [DailyBar("600000", day, 10, 10.1, 9.9, 10, 1000, "test") for day in days],
        adapter="test",
    )
    capsys.readouterr()

    assert main([
        *base, "analyze-replay", "--code", "600000",
        "--target-time", "2026-01-05T09:32:00+08:00",
    ]) == 0
    replay = capsys.readouterr().out
    assert "回放分析" in replay and "状态 ok" in replay
    assert "不产生实时提醒" in replay

    assert main([*base, "daily-analyze", "--through-date", "2026-01-05", "--code", "600000"]) == 0
    daily = capsys.readouterr().out
    assert "MA20 10.000000" in daily and "拐头天数 未知" in daily
    assert "回放补记=是" in daily


def test_analysis_demo_status_and_trace_cli(raw_config: dict, tmp_path: Path, monkeypatch, capsys) -> None:
    import yaml

    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    path = tmp_path / "configs" / "analysis-test.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(path)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([*base, "analysis-demo", "--session-date", "2026-01-07"]) == 0
    demo = capsys.readouterr().out
    assert "第三阶段离线验收开始" in demo and "恢复轮次实时提醒 0 条" in demo

    assert main([*base, "analysis-status", "--session-date", "2026-01-07"]) == 0
    status = capsys.readouterr().out
    assert "盘灯分析状态：2026-01-07" in status
    assert "离线恢复检查" in status and "实时提醒 0 条" in status
    assert "日线分析" in status

    database = Database((tmp_path / "runtime" / "analysis-test.db").resolve())
    with database.connect() as connection:
        event_id = connection.execute("SELECT MIN(id) FROM rule_events").fetchone()[0]
    assert main([*base, "analysis-trace", "--event-id", str(event_id)]) == 0
    trace = capsys.readouterr().out
    assert "全部来源分钟 ID" in trace and "输入 SHA-256" in trace
    assert "原始行情 ID → 指标快照 ID → 规则事件 ID" in trace
