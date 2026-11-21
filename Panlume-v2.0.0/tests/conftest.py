from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def raw_config() -> dict[str, Any]:
    return {
        "monitor": {"interval_seconds": 300},
        "storage": {"database": "runtime/test.db"},
        "models": {
            "ollama": {
                "enabled": True,
                "base_url": "http://127.0.0.1:11434",
                "model": "deepseek-r1:14b",
                "timeout_seconds": 60,
            },
            "cloud": {
                "enabled": False,
                "base_url": "",
                "model": "",
                "event_whitelist": [],
            },
        },
        "watchlist": [
            {"code": "600000", "name": "示例一"},
            {"code": "000001", "name": "示例二"},
        ],
    }


@pytest.fixture
def config_file(tmp_path: Path, raw_config: dict[str, Any]) -> Path:
    path = tmp_path / "configs" / "settings.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path
