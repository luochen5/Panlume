"""严格 JSON 行情样例适配器。

它用于验证真实数据适配器需要遵守的统一接口，不代表任何真实服务商的
字段格式。文件只读、全程离线；真实接入时只需新增适配器并完成字段映射。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .base import AdapterRecord, DailyBar, MarketDataAdapter, MinuteBar, TradingDay
from .cleaner import normalize_daily, normalize_minute, normalize_trading_day
from .schedule import CHINA_TZ


FEED_FORMAT = "pandeng.market-feed.v1"
MAX_FEED_BYTES = 10 * 1024 * 1024


class FeedFormatError(ValueError):
    """本地 JSON 样例不符合统一接口约定。"""


@dataclass(frozen=True)
class MinuteRequest:
    stock_codes: tuple[str, ...]
    start: datetime
    end: datetime


class JsonFeedAdapter(MarketDataAdapter):
    name = "json_feed_offline"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.minute_requests: list[MinuteRequest] = []
        payload = self._load(path)
        self.source = payload["source"]
        self.minute_bars = self._minutes(payload["minute_bars"])
        self.daily_bars = self._daily(payload["daily_qfq"])
        self.calendar = self._calendar(payload["trading_calendar"])

    @staticmethod
    def _load(path: Path) -> dict[str, object]:
        if not path.is_file():
            raise FeedFormatError(f"找不到 JSON 行情样例：{path}")
        if path.stat().st_size > MAX_FEED_BYTES:
            raise FeedFormatError("JSON 行情样例超过 10 MB 上限。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FeedFormatError(f"JSON 行情样例无法解析：{exc}") from exc
        if not isinstance(payload, dict):
            raise FeedFormatError("JSON 根节点必须是对象。")
        expected = {
            "format", "source", "minute_bars", "daily_qfq", "trading_calendar"
        }
        if set(payload) != expected:
            missing = sorted(expected - set(payload))
            extra = sorted(set(payload) - expected)
            raise FeedFormatError(f"JSON 根节点字段不符；缺少={missing}，额外={extra}。")
        if payload["format"] != FEED_FORMAT:
            raise FeedFormatError(f"format 必须逐字为 {FEED_FORMAT}。")
        source = payload["source"]
        if not isinstance(source, str) or not source.strip() or len(source.strip()) > 100:
            raise FeedFormatError("source 必须是 1～100 字符的非空字符串。")
        for field in ("minute_bars", "daily_qfq", "trading_calendar"):
            if not isinstance(payload[field], list):
                raise FeedFormatError(f"{field} 必须是数组。")
        return payload

    def _record(self, value: object, field: str, index: int) -> dict[str, object]:
        if not isinstance(value, dict):
            raise FeedFormatError(f"{field}[{index}] 必须是对象。")
        record = dict(value)
        source = record.get("source", self.source)
        if source != self.source:
            raise FeedFormatError(f"{field}[{index}] 的 source 与根节点不一致。")
        record["source"] = self.source
        return record

    @staticmethod
    def _require(record: dict[str, object], required: set[str], label: str) -> None:
        missing = sorted(required - set(record))
        if missing:
            raise FeedFormatError(f"{label} 缺少明确字段：{', '.join(missing)}")

    def _minutes(self, values: list[object]) -> tuple[MinuteBar, ...]:
        records: list[MinuteBar] = []
        required = {
            "stock_code", "bar_time", "open", "high", "low", "close",
            "volume", "adjustment", "confirmed",
        }
        for index, value in enumerate(values):
            raw = self._record(value, "minute_bars", index)
            self._require(raw, required, f"minute_bars[{index}]")
            result = normalize_minute(raw, default_source=self.source)
            if result.issues or not isinstance(result.record, MinuteBar):
                messages = "；".join(issue.message for issue in result.issues)
                raise FeedFormatError(f"minute_bars[{index}] 无效：{messages}")
            records.append(result.record)
        return tuple(records)

    def _daily(self, values: list[object]) -> tuple[DailyBar, ...]:
        records: list[DailyBar] = []
        required = {
            "stock_code", "trade_date", "open", "high", "low", "close",
            "volume", "adjustment", "confirmed",
        }
        for index, value in enumerate(values):
            raw = self._record(value, "daily_qfq", index)
            self._require(raw, required, f"daily_qfq[{index}]")
            result = normalize_daily(raw, default_source=self.source)
            if result.issues or not isinstance(result.record, DailyBar):
                messages = "；".join(issue.message for issue in result.issues)
                raise FeedFormatError(f"daily_qfq[{index}] 无效：{messages}")
            records.append(result.record)
        return tuple(records)

    def _calendar(self, values: list[object]) -> tuple[TradingDay, ...]:
        records: list[TradingDay] = []
        for index, value in enumerate(values):
            raw = self._record(value, "trading_calendar", index)
            self._require(raw, {"trade_date", "is_open"}, f"trading_calendar[{index}]")
            result = normalize_trading_day(raw, default_source=self.source)
            if result.issues or not isinstance(result.record, TradingDay):
                messages = "；".join(issue.message for issue in result.issues)
                raise FeedFormatError(f"trading_calendar[{index}] 无效：{messages}")
            records.append(result.record)
        return tuple(records)

    @staticmethod
    def _time_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start 必须带时区。")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("end 必须带时区。")
        start = start.astimezone(CHINA_TZ)
        end = end.astimezone(CHINA_TZ)
        if start > end:
            raise ValueError("start 不能晚于 end。")
        return start, end

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        start, end = self._time_range(start, end)
        self.minute_requests.append(MinuteRequest(codes, start, end))
        selected = set(codes)
        return [
            bar for bar in self.minute_bars
            if bar.stock_code in selected and start <= bar.bar_time <= end
        ]

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        if start > end:
            raise ValueError("start 不能晚于 end。")
        selected = set(stock_codes)
        return [
            bar for bar in self.daily_bars
            if bar.stock_code in selected and start <= bar.trade_date <= end
        ]

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        if start > end:
            raise ValueError("start 不能晚于 end。")
        return [day for day in self.calendar if start <= day.trade_date <= end]
