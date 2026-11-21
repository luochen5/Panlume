from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.schema import parse_config
from pandeng.desktop.review import DesktopReviewService
from pandeng.storage.db import Database


CODE = "600000"
DAY = "2026-01-07"


def test_empty_review_is_unknown_and_traceable(tmp_path: Path) -> None:
    database = Database(tmp_path / "desktop-review.db")
    database.initialize()
    service = DesktopReviewService(database)

    detail = service.generate(CODE, DAY)

    assert detail.created is True
    assert detail.data_status == "unknown"
    assert len(detail.input_hash) == 64
    assert "本次模型、云端和真实行情调用均为 0 次" in detail.metadata
    assert "最近指标快照：无" in detail.report
    loaded = service.load(detail.review_id)
    assert loaded.created is None
    assert loaded.input_hash == detail.input_hash
    assert loaded.report == detail.report


def test_same_desktop_input_is_idempotent_and_history_is_bounded(tmp_path: Path) -> None:
    database = Database(tmp_path / "desktop-review.db")
    database.initialize()
    service = DesktopReviewService(database)
    first = service.generate(CODE, DAY)

    second = service.generate(CODE, DAY)

    assert second.created is False
    assert second.review_id == first.review_id
    assert second.input_hash == first.input_hash
    assert [item.review_id for item in service.history()] == [first.review_id]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
        saved = connection.execute("SELECT input_json FROM review_runs").fetchone()
    assert json.loads(saved["input_json"])["stock_code"] == CODE


@pytest.mark.parametrize(
    ("code", "day", "message"),
    [
        ("60000", DAY, "6 位数字"),
        ("60000a", DAY, "6 位数字"),
        (CODE, "20260107", "YYYY-MM-DD"),
        (CODE, "2026-02-30", "有效日历日期"),
    ],
)
def test_invalid_desktop_review_input_has_no_database_write(
    tmp_path: Path, code: str, day: str, message: str,
) -> None:
    database = Database(tmp_path / "desktop-review.db")
    database.initialize()
    service = DesktopReviewService(database)
    with pytest.raises(ValueError, match=message):
        service.generate(code, day)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0


def test_history_detail_and_limits(tmp_path: Path) -> None:
    database = Database(tmp_path / "desktop-review.db")
    database.initialize()
    service = DesktopReviewService(database)
    first = service.generate(CODE, DAY)
    second = service.generate("000001", DAY)

    history = service.history(limit=1)

    assert [item.review_id for item in history] == [second.review_id]
    assert history[0].stock_code == "000001"
    assert service.load(first.review_id).stock_code == CODE
    for invalid in (0, -1, True, 101):
        with pytest.raises(ValueError):
            service.history(invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不存在"):
        service.load(999)


def test_desktop_review_preserves_all_upstream_tables_and_never_calls_models(
    raw_config: dict, tmp_path: Path, monkeypatch, capsys,
) -> None:
    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    raw_config["models"]["ollama"]["enabled"] = True
    config = parse_config(raw_config, tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    run_analysis_demo(config, date.fromisoformat(DAY))
    capsys.readouterr()

    def forbidden(*args, **kwargs):
        raise AssertionError("桌面本地复盘不应调用模型")

    monkeypatch.setattr("pandeng.summarizer.ollama.OllamaClient.generate", forbidden)
    monkeypatch.setattr("pandeng.review.cloud.OpenAICompatibleCloudClient.generate", forbidden)
    with database.connect() as connection:
        tables = [
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('review_runs', 'sqlite_sequence')"
            )
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }

    detail = DesktopReviewService(database).generate(CODE, DAY)

    assert detail.data_status == "ok"
    assert "分钟 240 条，已确认 240" in detail.report
    with database.connect() as connection:
        after = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
        }
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0
    assert after == before
