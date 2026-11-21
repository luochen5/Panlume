"""配置数据结构与安全校验。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigError(ValueError):
    """表示配置内容无法安全使用。"""


POLLING_PRESETS = {60: "1 分钟", 300: "5 分钟（默认）", 600: "10 分钟"}


def polling_interval_label(seconds: int) -> str:
    """显示推荐档位；其他合法秒数仍按自定义值使用。"""
    preset = POLLING_PRESETS.get(seconds)
    if preset is not None:
        return f"{preset}档"
    return f"自定义 {seconds} 秒（{seconds / 60:g} 分钟）"


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: int
    fetch_timeout_seconds: int = 20
    close_timeout_seconds: int = 45


@dataclass(frozen=True)
class StorageConfig:
    database: Path


@dataclass(frozen=True)
class OllamaConfig:
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: int


@dataclass(frozen=True)
class CloudConfig:
    enabled: bool
    base_url: str
    model: str
    api_key_configured: bool
    event_whitelist: tuple[str, ...]


@dataclass(frozen=True)
class RulesConfig:
    rapid_move_pct: float = 1.0
    volume_ratio_threshold: float = 1.5
    breakout_lookback_minutes: int = 20
    normal_cooldown_seconds: int = 1800
    high_priority_repeat_seconds: int = 900


@dataclass(frozen=True)
class SummaryConfig:
    queue_capacity: int = 8
    shutdown_timeout_seconds: int = 10
    max_chars: int = 400


@dataclass(frozen=True)
class MomentumConfig:
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14

    def __post_init__(self) -> None:
        if self.macd_fast < 1 or self.macd_slow <= self.macd_fast:
            raise ConfigError("MACD 必须满足 macd_slow > macd_fast >= 1。")
        if self.macd_signal < 1 or self.rsi_period < 1:
            raise ConfigError("MACD signal 与 RSI 周期必须大于 0。")


@dataclass(frozen=True)
class WatchItem:
    code: str
    name: str
    sector_code: str | None = None


@dataclass(frozen=True)
class AppConfig:
    monitor: MonitorConfig
    storage: StorageConfig
    ollama: OllamaConfig
    cloud: CloudConfig
    watchlist: tuple[WatchItem, ...]
    rules: RulesConfig
    summary: SummaryConfig = SummaryConfig()
    momentum: MomentumConfig = MomentumConfig()


def _mapping(value: Any, section: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{section} 必须是 YAML 映射。")
    return value


def _check_keys(data: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{section} 包含无法识别的配置项：{', '.join(unknown)}")


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field} 必须是字符串。")
    value = value.strip()
    if not allow_empty and not value:
        raise ConfigError(f"{field} 不能为空。")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field} 必须填写 true 或 false。")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} 必须是整数。")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{field} 必须在 {minimum}～{maximum} 之间。")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} 必须是数值。")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{field} 必须在 {minimum}～{maximum} 之间。")
    return result


def _url(value: str, field: str, *, allow_empty: bool = False) -> str:
    if not value and allow_empty:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{field} 必须是完整的 http:// 或 https:// 地址。")
    return value.rstrip("/")


def parse_config(raw: Any, base_dir: Path) -> AppConfig:
    """把 YAML 字典转换为经过校验的配置对象。"""
    root = _mapping(raw, "配置根节点")
    _check_keys(
        root, {"monitor", "storage", "models", "watchlist", "rules", "summary", "indicators"}, "配置根节点"
    )

    monitor_raw = _mapping(root.get("monitor"), "monitor")
    _check_keys(
        monitor_raw,
        {"interval_seconds", "fetch_timeout_seconds", "close_timeout_seconds"},
        "monitor",
    )
    interval = _integer(
        monitor_raw.get("interval_seconds"), "monitor.interval_seconds", 60, 3600
    )
    fetch_timeout = _integer(
        monitor_raw.get("fetch_timeout_seconds", 20),
        "monitor.fetch_timeout_seconds", 1, 300,
    )
    close_timeout = _integer(
        monitor_raw.get("close_timeout_seconds", 45),
        "monitor.close_timeout_seconds", 5, 600,
    )

    rules_raw = _mapping(root.get("rules", {}), "rules")
    _check_keys(
        rules_raw,
        {
            "rapid_move_pct", "volume_ratio_threshold", "breakout_lookback_minutes",
            "normal_cooldown_seconds", "high_priority_repeat_seconds",
        },
        "rules",
    )
    rules = RulesConfig(
        rapid_move_pct=_number(
            rules_raw.get("rapid_move_pct", 1.0), "rules.rapid_move_pct", 0.1, 20.0
        ),
        volume_ratio_threshold=_number(
            rules_raw.get("volume_ratio_threshold", 1.5),
            "rules.volume_ratio_threshold", 0.1, 100.0,
        ),
        breakout_lookback_minutes=_integer(
            rules_raw.get("breakout_lookback_minutes", 20),
            "rules.breakout_lookback_minutes", 5, 60,
        ),
        normal_cooldown_seconds=_integer(
            rules_raw.get("normal_cooldown_seconds", 1800),
            "rules.normal_cooldown_seconds", 60, 86400,
        ),
        high_priority_repeat_seconds=_integer(
            rules_raw.get("high_priority_repeat_seconds", 900),
            "rules.high_priority_repeat_seconds", 60, 86400,
        ),
    )

    summary_raw = _mapping(root.get("summary", {}), "summary")
    _check_keys(
        summary_raw,
        {"queue_capacity", "shutdown_timeout_seconds", "max_chars"},
        "summary",
    )
    summary = SummaryConfig(
        queue_capacity=_integer(
            summary_raw.get("queue_capacity", 8), "summary.queue_capacity", 1, 100
        ),
        shutdown_timeout_seconds=_integer(
            summary_raw.get("shutdown_timeout_seconds", 10),
            "summary.shutdown_timeout_seconds", 1, 60,
        ),
        max_chars=_integer(
            summary_raw.get("max_chars", 400), "summary.max_chars", 80, 1000
        ),
    )

    indicators_raw = _mapping(root.get("indicators", {}), "indicators")
    _check_keys(
        indicators_raw, {"macd_fast", "macd_slow", "macd_signal", "rsi_period"}, "indicators"
    )
    momentum = MomentumConfig(
        macd_fast=_integer(indicators_raw.get("macd_fast", 12), "indicators.macd_fast", 1, 500),
        macd_slow=_integer(indicators_raw.get("macd_slow", 26), "indicators.macd_slow", 2, 500),
        macd_signal=_integer(indicators_raw.get("macd_signal", 9), "indicators.macd_signal", 1, 500),
        rsi_period=_integer(indicators_raw.get("rsi_period", 14), "indicators.rsi_period", 1, 500),
    )

    storage_raw = _mapping(root.get("storage"), "storage")
    _check_keys(storage_raw, {"database"}, "storage")
    database_text = _text(storage_raw.get("database"), "storage.database")
    database = Path(database_text).expanduser()
    if not database.is_absolute():
        database = (base_dir / database).resolve()

    models_raw = _mapping(root.get("models"), "models")
    _check_keys(models_raw, {"ollama", "cloud"}, "models")

    ollama_raw = _mapping(models_raw.get("ollama"), "models.ollama")
    _check_keys(
        ollama_raw, {"enabled", "base_url", "model", "timeout_seconds"}, "models.ollama"
    )
    ollama_url = os.environ.get(
        "PANDENG_OLLAMA_BASE_URL",
        _text(ollama_raw.get("base_url"), "models.ollama.base_url"),
    ).strip()
    ollama = OllamaConfig(
        enabled=_boolean(ollama_raw.get("enabled"), "models.ollama.enabled"),
        base_url=_url(ollama_url, "PANDENG_OLLAMA_BASE_URL"),
        model=_text(ollama_raw.get("model"), "models.ollama.model"),
        timeout_seconds=_integer(
            ollama_raw.get("timeout_seconds"), "models.ollama.timeout_seconds", 5, 300
        ),
    )

    cloud_raw = _mapping(models_raw.get("cloud"), "models.cloud")
    _check_keys(
        cloud_raw,
        {"enabled", "base_url", "model", "event_whitelist"},
        "models.cloud",
    )
    cloud_enabled = _boolean(cloud_raw.get("enabled"), "models.cloud.enabled")
    cloud_url = os.environ.get(
        "PANDENG_CLOUD_BASE_URL",
        _text(cloud_raw.get("base_url"), "models.cloud.base_url", allow_empty=True),
    ).strip()
    cloud_model = os.environ.get(
        "PANDENG_CLOUD_MODEL",
        _text(cloud_raw.get("model"), "models.cloud.model", allow_empty=True),
    ).strip()
    api_key_configured = bool(os.environ.get("PANDENG_CLOUD_API_KEY", "").strip())

    whitelist_raw = cloud_raw.get("event_whitelist")
    if not isinstance(whitelist_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in whitelist_raw
    ):
        raise ConfigError("models.cloud.event_whitelist 必须是由非空字符串组成的列表。")
    whitelist = tuple(dict.fromkeys(item.strip() for item in whitelist_raw))

    if cloud_enabled:
        if not cloud_url or not cloud_model or not api_key_configured:
            raise ConfigError(
                "启用云端前，必须配置 PANDENG_CLOUD_BASE_URL、"
                "PANDENG_CLOUD_MODEL 和 PANDENG_CLOUD_API_KEY。"
            )
    cloud = CloudConfig(
        enabled=cloud_enabled,
        base_url=_url(cloud_url, "PANDENG_CLOUD_BASE_URL", allow_empty=True),
        model=cloud_model,
        api_key_configured=api_key_configured,
        event_whitelist=whitelist,
    )

    watchlist_raw = root.get("watchlist")
    if not isinstance(watchlist_raw, list) or not 1 <= len(watchlist_raw) <= 10:
        raise ConfigError("watchlist 必须包含 1～10 个观察对象。")
    watch_items: list[WatchItem] = []
    seen_codes: set[str] = set()
    for index, item_raw in enumerate(watchlist_raw, start=1):
        item = _mapping(item_raw, f"watchlist 第 {index} 项")
        _check_keys(item, {"code", "name", "sector_code"}, f"watchlist 第 {index} 项")
        code = _text(item.get("code"), f"watchlist 第 {index} 项的 code")
        if not re.fullmatch(r"\d{6}", code):
            raise ConfigError(f"股票代码 {code} 无效：必须是 6 位数字，并放在引号中。")
        if code in seen_codes:
            raise ConfigError(f"watchlist 中存在重复股票代码：{code}。")
        seen_codes.add(code)
        name = _text(item.get("name", ""), "watchlist.name", allow_empty=True)
        sector_text = _text(
            item.get("sector_code", ""), f"watchlist 第 {index} 项的 sector_code",
            allow_empty=True,
        )
        if sector_text and not re.fullmatch(r"\d{6}", sector_text):
            raise ConfigError(f"板块代码 {sector_text} 无效：必须是带引号的 6 位数字字符串。")
        if sector_text == code:
            raise ConfigError("sector_code 不能与股票自身代码相同。")
        watch_items.append(WatchItem(code=code, name=name, sector_code=sector_text or None))

    return AppConfig(
        monitor=MonitorConfig(
            interval_seconds=interval,
            fetch_timeout_seconds=fetch_timeout,
            close_timeout_seconds=close_timeout,
        ),
        storage=StorageConfig(database=database),
        ollama=ollama,
        cloud=cloud,
        watchlist=tuple(watch_items),
        rules=rules,
        summary=summary,
        momentum=momentum,
    )
