"""桌面偏好设置：严格 JSON 校验、原子保存，不保存任何密钥。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pandeng.desktop.i18n import DEFAULT_LANGUAGE, normalize_language


SUPPORTED_MODEL = "deepseek-r1:14b"
SUMMARY_TRIGGERS = {"event_and_manual", "manual_only"}
OUTPUT_STYLES = {"brief", "detailed", "facts_only"}


class DesktopSettingsError(ValueError):
    pass


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DesktopSettingsError(f"{field} 必须是 true 或 false。")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DesktopSettingsError(f"{field} 必须是整数。")
    if not minimum <= value <= maximum:
        raise DesktopSettingsError(f"{field} 必须在 {minimum}～{maximum} 之间。")
    return value


@dataclass(frozen=True)
class StockChoice:
    code: str
    name: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not re.fullmatch(r"\d{6}", self.code):
            raise DesktopSettingsError("股票代码必须是 6 位数字字符串。")
        if not isinstance(self.name, str) or len(self.name.strip()) > 30:
            raise DesktopSettingsError("股票名称必须是至多 30 字的字符串。")
        object.__setattr__(self, "name", self.name.strip())
        _boolean(self.enabled, "enabled")


@dataclass(frozen=True)
class DesktopSettings:
    language: str = DEFAULT_LANGUAGE
    theme: str = "light"
    interval_seconds: int = 300
    stocks: tuple[StockChoice, ...] = (StockChoice("002156", "通富微电"),)
    ollama_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    model_name: str = SUPPORTED_MODEL
    ollama_timeout_seconds: int = 60
    summary_trigger: str = "event_and_manual"
    output_style: str = "brief"
    save_prompt_debug: bool = False
    save_raw_response_debug: bool = False
    save_thinking_debug: bool = False
    minute_retention_days: int = 20
    log_retention_days: int = 30

    def __post_init__(self) -> None:
        try:
            normalize_language(self.language)
        except ValueError as exc:
            raise DesktopSettingsError(str(exc)) from exc
        if self.theme not in {"light", "dark"}:
            raise DesktopSettingsError("theme 只支持 light 或 dark。")
        _integer(self.interval_seconds, "interval_seconds", 60, 3600)
        if not 1 <= len(self.stocks) <= 10:
            raise DesktopSettingsError("同时观察股票必须为 1～10 只。")
        if any(not isinstance(item, StockChoice) for item in self.stocks):
            raise DesktopSettingsError("stocks 包含无效观察对象。")
        codes = [item.code for item in self.stocks]
        if len(codes) != len(set(codes)):
            raise DesktopSettingsError("观察股票不能重复。")
        _boolean(self.ollama_enabled, "ollama_enabled")
        if not isinstance(self.ollama_base_url, str):
            raise DesktopSettingsError("Ollama 连接地址必须是字符串。")
        parsed = urlparse(self.ollama_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DesktopSettingsError("Ollama 连接地址必须是完整的 http:// 或 https:// 地址。")
        object.__setattr__(self, "ollama_base_url", self.ollama_base_url.rstrip("/"))
        if not isinstance(self.model_name, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", self.model_name):
            raise DesktopSettingsError("模型名称格式无效，只允许字母、数字、点、下划线、冒号、斜杠和连字符。")
        _integer(self.ollama_timeout_seconds, "ollama_timeout_seconds", 5, 300)
        if self.summary_trigger not in SUMMARY_TRIGGERS:
            raise DesktopSettingsError("summary_trigger 不在允许范围内。")
        if self.output_style not in OUTPUT_STYLES:
            raise DesktopSettingsError("output_style 不在允许范围内。")
        for field in ("save_prompt_debug", "save_raw_response_debug", "save_thinking_debug"):
            _boolean(getattr(self, field), field)
        _integer(self.minute_retention_days, "minute_retention_days", 5, 365)
        _integer(self.log_retention_days, "log_retention_days", 7, 365)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DesktopSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, *, default_language: str = DEFAULT_LANGUAGE) -> DesktopSettings:
        if not self.path.exists():
            return DesktopSettings(language=default_language)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopSettingsError(f"桌面设置无法读取：{exc}") from exc
        if not isinstance(raw, dict):
            raise DesktopSettingsError("桌面设置根节点必须是 JSON 对象。")
        stocks_raw = raw.get("stocks")
        raw = dict(raw)
        raw.pop("stocks", None)
        fields = {
            "language", "theme", "interval_seconds", "ollama_enabled", "ollama_base_url", "model_name",
            "ollama_timeout_seconds", "summary_trigger", "output_style",
            "save_prompt_debug", "save_raw_response_debug", "save_thinking_debug",
            "minute_retention_days", "log_retention_days",
        }
        unknown = sorted(set(raw) - fields)
        if unknown:
            raise DesktopSettingsError(f"桌面设置包含未知键：{', '.join(unknown)}")
        if not isinstance(stocks_raw, list):
            raise DesktopSettingsError("stocks 必须是观察对象列表。")
        stocks = []
        for item in stocks_raw:
            if not isinstance(item, dict) or set(item) - {"code", "name", "enabled"}:
                raise DesktopSettingsError("stocks 中存在无效字段。")
            stocks.append(StockChoice(
                item.get("code"), item.get("name", ""), item.get("enabled", True)
            ))
        allowed = {field.name for field in __import__("dataclasses").fields(DesktopSettings)}
        defaults = DesktopSettings()
        values = {name: getattr(defaults, name) for name in allowed if name != "stocks"}
        values.update(raw)
        values["stocks"] = tuple(stocks)
        return DesktopSettings(**values)

    def save(self, settings: DesktopSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
