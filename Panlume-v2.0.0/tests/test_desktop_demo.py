from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.loader import load_config
from pandeng.config.schema import ConfigError, StorageConfig
from pandeng.desktop.demo import DEMO_DAY, DesktopDemoService
from pandeng.desktop.paths import resource_path
from pandeng.storage.db import Database


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


def demo_config(path: Path):
    config = load_config(resource_path("configs/analysis-test.yaml"))
    return replace(config, storage=StorageConfig(path))


def test_integrated_demo_runs_once_then_returns_read_only_without_requests(
    tmp_path: Path, monkeypatch,
) -> None:
    database_path = tmp_path / "runtime" / "demo" / "analysis-test.db"
    personal = Database(tmp_path / "personal" / "pandeng.db")
    personal.initialize()
    personal_before = table_snapshot(personal)

    def forbidden(*args, **kwargs):
        raise AssertionError("整合离线演示不应调用真实模型")

    monkeypatch.setattr("pandeng.summarizer.ollama.OllamaClient.generate", forbidden)
    monkeypatch.setattr("pandeng.review.cloud.OpenAICompatibleCloudClient.generate", forbidden)
    service = DesktopDemoService(database_path)

    first = service.run()

    assert not first.reused
    assert "未请求真实行情" in first.output
    assert "固定模板摘要" in first.output
    assert "本地复盘" in first.output and "数据体检" in first.output
    assert "事件与摘要" in first.output and "模型调用 0 次" in first.output
    database = Database(database_path)
    with database.connect() as connection:
        live_notices = connection.execute(
            "SELECT COUNT(*) FROM rule_event_checks AS c "
            "JOIN analysis_runs AS a ON a.id = c.analysis_run_id "
            "WHERE c.notified = 1 AND a.analysis_mode = 'live'"
        ).fetchone()[0]
        assert live_notices > 0
        assert connection.execute("SELECT COUNT(*) FROM summary_tasks").fetchone()[0] == live_notices
        assert connection.execute(
            "SELECT COUNT(*) FROM summary_results WHERE summary_kind = 'template'"
        ).fetchone()[0] == live_notices
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        reviews = list(connection.execute(
            "SELECT stock_code, data_status FROM review_runs ORDER BY stock_code"
        ))
        assert [(row["stock_code"], row["data_status"]) for row in reviews] == [
            ("000001", "ok"), ("600000", "ok"),
        ]
    before = table_snapshot(database)

    second = service.run()

    assert second.reused
    assert "没有覆盖或追加" in second.output
    assert "发布候选离线检查" in second.output
    assert table_snapshot(database) == before
    assert table_snapshot(personal) == personal_before


def test_english_demo_generates_english_templates_and_preserves_original_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "english-demo" / "analysis-test.db"
    service = DesktopDemoService(path, language_provider=lambda: "en")

    first = service.run()

    assert not first.reused
    assert "Offline release checks" in first.output
    assert "Fixed-template summaries" in first.output
    assert "model calls" in first.output
    database = Database(path)
    with database.connect() as connection:
        summaries = [row["text"] for row in connection.execute(
            "SELECT text FROM summary_results WHERE summary_kind = 'template'"
        )]
        reviews = [row["local_report"] for row in connection.execute(
            "SELECT local_report FROM review_runs"
        )]
        original_messages = [row["latest_message"] for row in connection.execute(
            "SELECT latest_message FROM rule_events"
        )]
    assert summaries and all("2026-01-07" in text for text in summaries)
    assert all("触发" not in text for text in summaries)
    assert reviews and all(text.startswith("Panlume local daily review") for text in reviews)
    assert any("触发" in text for text in original_messages)
    before = table_snapshot(database)

    second = service.run()

    assert second.reused
    assert "read without appending or overwriting" in second.output
    assert table_snapshot(database) == before


def test_legacy_demo_is_not_automatically_extended(tmp_path: Path) -> None:
    path = tmp_path / "legacy" / "analysis-test.db"
    database = Database(path)
    database.initialize()
    run_analysis_demo(demo_config(path), DEMO_DAY)
    before = table_snapshot(database)

    result = DesktopDemoService(path).run()

    assert result.reused
    assert "旧版或不完整" in result.output
    assert "没有自动补写" in result.output
    assert "新的 PANDENG_DATA_DIR" in result.output
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM summary_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
    assert table_snapshot(database) == before


def test_initialized_but_incomplete_demo_is_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "incomplete" / "analysis-test.db"
    database = Database(path)
    database.initialize()
    before = table_snapshot(database)

    with pytest.raises(ConfigError, match="未自动修复、补写或覆盖"):
        DesktopDemoService(path).run()

    assert table_snapshot(database) == before
