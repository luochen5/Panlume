from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pandeng.config.loader import load_config
from pandeng.config.schema import ConfigError, parse_config


def test_load_valid_config(config_file: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(config_file)

    assert config.monitor.interval_seconds == 300
    assert config.monitor.fetch_timeout_seconds == 20
    assert config.monitor.close_timeout_seconds == 45
    assert config.storage.database == (tmp_path / "runtime/test.db").resolve()
    assert [item.code for item in config.watchlist] == ["600000", "000001"]
    assert config.cloud.enabled is False
    assert config.rules.rapid_move_pct == 1.0
    assert config.rules.breakout_lookback_minutes == 20
    assert config.rules.normal_cooldown_seconds == 1800
    assert config.rules.high_priority_repeat_seconds == 900
    assert config.summary.queue_capacity == 8
    assert config.summary.shutdown_timeout_seconds == 10
    assert config.summary.max_chars == 400


def test_environment_overrides_ollama_url(raw_config: dict, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PANDENG_OLLAMA_BASE_URL", "http://localhost:11434/")
    config = parse_config(raw_config, tmp_path)
    assert config.ollama.base_url == "http://localhost:11434"


def test_cloud_requires_complete_environment(raw_config: dict, tmp_path: Path, monkeypatch) -> None:
    raw_config["models"]["cloud"]["enabled"] = True
    monkeypatch.delenv("PANDENG_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("PANDENG_CLOUD_BASE_URL", raising=False)
    monkeypatch.delenv("PANDENG_CLOUD_MODEL", raising=False)

    with pytest.raises(ConfigError, match="启用云端前"):
        parse_config(raw_config, tmp_path)


def test_cloud_environment_is_validated_without_exposing_key(
    raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    raw_config["models"]["cloud"]["enabled"] = True
    monkeypatch.setenv("PANDENG_CLOUD_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PANDENG_CLOUD_MODEL", "test-model")
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "secret-for-test")

    config = parse_config(raw_config, tmp_path)
    assert config.cloud.api_key_configured is True
    assert "secret-for-test" not in repr(config)


def test_duplicate_watchlist_code_is_rejected(raw_config: dict, tmp_path: Path) -> None:
    raw_config["watchlist"].append(deepcopy(raw_config["watchlist"][0]))
    with pytest.raises(ConfigError, match="重复股票代码"):
        parse_config(raw_config, tmp_path)


def test_unquoted_numeric_code_is_rejected(raw_config: dict, tmp_path: Path) -> None:
    raw_config["watchlist"][0]["code"] = 1
    with pytest.raises(ConfigError, match="必须是字符串"):
        parse_config(raw_config, tmp_path)


def test_too_frequent_interval_is_rejected(raw_config: dict, tmp_path: Path) -> None:
    raw_config["monitor"]["interval_seconds"] = 5
    with pytest.raises(ConfigError, match="60～3600"):
        parse_config(raw_config, tmp_path)


def test_unknown_key_is_rejected(raw_config: dict, tmp_path: Path) -> None:
    raw_config["monitor"]["interval_second"] = 300
    with pytest.raises(ConfigError, match="无法识别"):
        parse_config(raw_config, tmp_path)


def test_monitor_timeout_range_is_checked(raw_config: dict, tmp_path: Path) -> None:
    raw_config["monitor"]["close_timeout_seconds"] = 0
    with pytest.raises(ConfigError, match="5～600"):
        parse_config(raw_config, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rapid_move_pct", True, "必须是数值"),
        ("volume_ratio_threshold", 0, "0.1～100.0"),
        ("breakout_lookback_minutes", 4, "5～60"),
        ("normal_cooldown_seconds", 59, "60～86400"),
        ("high_priority_repeat_seconds", "900", "必须是整数"),
    ],
)
def test_rule_thresholds_are_strictly_validated(
    raw_config: dict, tmp_path: Path, field: str, value, message: str
) -> None:
    raw_config["rules"] = {field: value}
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config, tmp_path)


@pytest.mark.parametrize("sector_code", [1, "abcde", "600000"])
def test_sector_code_must_be_distinct_quoted_six_digits(
    raw_config: dict, tmp_path: Path, sector_code
) -> None:
    raw_config["watchlist"][0]["sector_code"] = sector_code
    with pytest.raises(ConfigError):
        parse_config(raw_config, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("queue_capacity", 0, "1～100"),
        ("shutdown_timeout_seconds", 61, "1～60"),
        ("max_chars", 79, "80～1000"),
        ("max_chars", True, "必须是整数"),
    ],
)
def test_summary_limits_are_strictly_validated(
    raw_config: dict, tmp_path: Path, field: str, value, message: str
) -> None:
    raw_config["summary"] = {field: value}
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config, tmp_path)
