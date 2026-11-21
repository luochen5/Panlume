from __future__ import annotations

import json
from pathlib import Path

from pandeng.desktop.data_manager import DesktopDataManager
from pandeng.desktop.settings import DesktopSettingsStore
from test_desktop_history import populate_history


def table_rows(database, excluded=()):
    with database.connect() as connection:
        names = [
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ) if row["name"] != "sqlite_sequence" and row["name"] not in excluded
        ]
        return {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in names
        }


def test_v1_schema6_backup_reinitialize_and_records_survive(tmp_path) -> None:
    manager = DesktopDataManager(tmp_path / "personal" / "data" / "pandeng.db")
    manager.initialize()
    populate_history(manager.database)
    before = table_rows(manager.database, excluded=("app_metadata", "run_logs"))
    backup = manager.backup(tmp_path / "backups" / "v1-before-v2.db")

    manager.initialize()

    assert manager.schema_version() == "6"
    assert table_rows(manager.database, excluded=("app_metadata", "run_logs")) == before
    restored = DesktopDataManager(backup)
    assert table_rows(restored.database, excluded=("app_metadata", "run_logs")) == before


def test_v1_desktop_json_keeps_language_watchlist_and_adds_only_compatible_defaults(tmp_path) -> None:
    path = tmp_path / "desktop-settings.json"
    legacy = {
        "theme": "dark",
        "interval_seconds": 300,
        "stocks": [{"code": "000002", "name": "二"}, {"code": "000001", "name": "一"}],
        "ollama_enabled": True,
        "model_name": "deepseek-r1:14b",
        "ollama_timeout_seconds": 60,
        "summary_trigger": "event_and_manual",
        "output_style": "brief",
        "save_prompt_debug": False,
        "save_raw_response_debug": False,
        "save_thinking_debug": False,
        "minute_retention_days": 20,
        "log_retention_days": 30,
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    store = DesktopSettingsStore(path)

    loaded = store.load(default_language="en")
    store.save(loaded)
    reloaded = store.load(default_language="en")

    assert loaded.language == "zh-CN"
    assert loaded.theme == "dark" and loaded.interval_seconds == 300
    assert [item.code for item in loaded.stocks] == ["000002", "000001"]
    assert all(item.enabled is True for item in loaded.stocks)
    assert loaded.ollama_base_url == "http://127.0.0.1:11434"
    assert reloaded == loaded
