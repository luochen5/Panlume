from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.schema import ConfigError, parse_config
from pandeng.storage.db import Database


def test_stage3_demo_complete_and_isolated(raw_config: dict, tmp_path: Path, capsys) -> None:
    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    config = parse_config(raw_config, tmp_path)
    database = Database(config.storage.database)
    database.initialize()

    outcome = run_analysis_demo(config, date(2026, 1, 7))
    output = capsys.readouterr().out

    assert outcome.rounds == 8
    assert outcome.analysis_runs == 14 and outcome.snapshots == 14
    assert outcome.notices > 0 and outcome.recovery_checks > 0
    assert outcome.daily_unknown_before_promotion == 2
    assert outcome.formal_backfills_after_promotion == 2
    assert "恢复轮次实时提醒 0 条" in output
    assert "全部为回放补记" in output
    assert "未调用 Ollama 或云端" in output
    with database.connect() as connection:
        recovery_notified = connection.execute(
            """
            SELECT COALESCE(SUM(c.notified), 0)
            FROM rule_event_checks AS c
            JOIN analysis_runs AS r ON r.id = c.analysis_run_id
            WHERE r.analysis_mode = 'offline_recovery'
            """
        ).fetchone()[0]
        daily = list(connection.execute(
            "SELECT formal_signal, is_backfill FROM daily_analysis WHERE trade_date = '2026-01-07'"
        ))
        assert connection.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0] == 1
    assert recovery_notified == 0
    assert len(daily) == 2
    assert all(row["formal_signal"] == 1 and row["is_backfill"] == 1 for row in daily)

    with pytest.raises(ConfigError, match="不会覆盖或追加"):
        run_analysis_demo(config, date(2026, 1, 8))


def test_stage3_demo_refuses_non_dedicated_database(raw_config: dict, tmp_path: Path) -> None:
    raw_config["storage"]["database"] = "runtime/pandeng.db"
    config = parse_config(raw_config, tmp_path)
    Database(config.storage.database).initialize()

    with pytest.raises(ConfigError, match="独立数据库"):
        run_analysis_demo(config, date(2026, 1, 7))
