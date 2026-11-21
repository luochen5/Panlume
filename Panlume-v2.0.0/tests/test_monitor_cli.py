from pathlib import Path

from pandeng.app.cli import main
from pandeng.storage.db import Database
from pandeng.storage.monitor import recent_monitor_events, recent_monitor_rounds
from pandeng.storage.repositories import table_count


def test_monitor_demo_and_status_cli(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([*base, "monitor-demo", "--session-date", "2026-01-06"]) == 0
    demo = capsys.readouterr().out
    assert "离线验收开始" in demo
    assert "暂停期间" in demo and "新增请求 0 次" in demo
    assert "09:46 注入断网" in demo
    assert "午休" in demo
    assert "正式日线结论必须保持状态未知" in demo
    assert "未调用 Ollama 或云端" in demo

    database = Database((tmp_path / "runtime" / "test.db").resolve())
    assert database.metadata("schema_version") == "6"
    assert table_count(database, "minute_bars") == 480
    rounds = recent_monitor_rounds(database, limit=100)
    events = recent_monitor_events(database, limit=100)
    assert len(rounds) == 6
    assert sum(row["event_type"] == "offline_recovery" for row in events) == 4
    assert any(row["event_type"] == "daily_unconfirmed" for row in events)

    assert main([*base, "monitor-status"]) == 0
    status = capsys.readouterr().out
    assert "盘灯调度状态" in status
    assert "运行状态 closed" in status
    assert "日 K unknown" in status
    assert "逐股票连续检查点" in status

    assert main([*base, "monitor-demo", "--session-date", "2026-01-06"]) == 1
    repeated = capsys.readouterr().out
    assert "为避免覆盖历史" in repeated
    assert len(recent_monitor_rounds(database, limit=100)) == 6
