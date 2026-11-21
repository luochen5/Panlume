"""Tushare 响应的离线映射与严格虚拟样例适配器。

本模块没有 HTTP 客户端，也不读取 Token。它验证的是字段、代码、时间、单位
和复权／确认标记的转换逻辑，不代表已经取得任何 Tushare 权限。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

from .base import AdapterRecord, MarketDataAdapter
from .provider_common import (
    ProviderFormatError,
    load_json_document,
    pandeng_code_to_tushare,
    parse_china_provider_time,
    parse_compact_date,
    tushare_code_to_pandeng,
    volume_to_shares,
)
from .schedule import CHINA_TZ


FIXTURE_FORMAT = "pandeng.tushare-fixture.v1"
FIXTURE_SOURCE = "tushare_fixture_offline"


@dataclass(frozen=True)
class MappedResponse:
    records: tuple[dict[str, object], ...]
    request_id: str | None
    excluded_opening_auction: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class FixtureMinuteRequest:
    stock_codes: tuple[str, ...]
    start: datetime
    end: datetime


def _response_rows(
    payload: object,
) -> tuple[list[dict[str, object]], str | None, list[str]]:
    if not isinstance(payload, dict):
        raise ProviderFormatError("Tushare 响应必须是 JSON 对象。")
    code = payload.get("code")
    if type(code) is not int:
        raise ProviderFormatError("Tushare 响应缺少整数 code。")
    if code != 0:
        message = str(payload.get("msg") or "未提供错误说明")[:300]
        raise ProviderFormatError(f"Tushare 响应返回错误 code={code}：{message}")
    request_id = payload.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ProviderFormatError("Tushare request_id 必须是字符串或 null。")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProviderFormatError("Tushare 成功响应必须包含 data 对象。")
    fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field for field in fields)
        or len(set(fields)) != len(fields)
    ):
        raise ProviderFormatError("Tushare data.fields 必须是非空且不重复的字符串数组。")
    if not isinstance(items, list) or any(not isinstance(row, list) for row in items):
        raise ProviderFormatError("Tushare data.items 必须是数组，每行也必须是数组。")
    rows: list[dict[str, object]] = []
    for index, item in enumerate(items, 1):
        if len(item) != len(fields):
            raise ProviderFormatError(f"Tushare 第 {index} 行与 fields 长度不一致。")
        rows.append(dict(zip(fields, item)))
    return rows, request_id, fields


def _require_fields(
    fields: Iterable[str], required: set[str], response_name: str
) -> None:
    missing = required.difference(fields)
    if missing:
        raise ProviderFormatError(
            f"{response_name} 缺少必要字段：{', '.join(sorted(missing))}。"
        )


def _provider_metadata(
    source: str, row: dict[str, object], request_id: str | None
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "provider": "tushare",
        "provider_raw": dict(row),
    }
    if request_id is not None:
        metadata["provider_request_id"] = request_id
    return metadata


def map_minute_response(
    payload: object, *, confirmed_through: datetime, source: str = FIXTURE_SOURCE
) -> MappedResponse:
    """映射 rt_min 风格行；分钟 vol 按股处理。

    官方样例出现的 09:30 记录单列为开盘集合竞价排除项。连续竞价分钟仍从
    09:31 开始，绝不把 09:30 改名成 09:31。
    """
    if not isinstance(payload, dict):
        raise ProviderFormatError("分钟响应必须是 JSON 对象。")
    rows, request_id, fields = _response_rows(payload)
    _require_fields(
        fields,
        {"ts_code", "trade_time", "open", "high", "low", "close", "vol"},
        "分钟响应",
    )
    through = parse_china_provider_time(confirmed_through)
    records: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for row in rows:
        code = tushare_code_to_pandeng(row.get("ts_code"))
        bar_time = parse_china_provider_time(row.get("trade_time"))
        if bar_time.time() == time(9, 30):
            excluded.append(dict(row))
            continue
        record: dict[str, object] = {
            "stock_code": code,
            "bar_time": bar_time.isoformat(),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": volume_to_shares(row.get("vol"), unit="share"),
            "source": source,
            "adjustment": "none",
            "confirmed": bar_time <= through,
        }
        record.update(_provider_metadata(source, row, request_id))
        records.append(record)
    return MappedResponse(tuple(records), request_id, tuple(excluded))


def map_daily_qfq_response(
    payload: object,
    *,
    adjustment: str,
    confirmed: bool,
    source: str = FIXTURE_SOURCE,
) -> MappedResponse:
    """映射明确使用 adj=qfq 得到的日线；普通 daily 响应不能冒充满前复权。"""
    if adjustment != "qfq":
        raise ProviderFormatError("日线离线映射只接受明确的 qfq 复权口径。")
    if not isinstance(confirmed, bool):
        raise ProviderFormatError("日线 confirmed 必须由调用方显式提供布尔值。")
    if not isinstance(payload, dict):
        raise ProviderFormatError("日线响应必须是 JSON 对象。")
    rows, request_id, fields = _response_rows(payload)
    _require_fields(
        fields,
        {"ts_code", "trade_date", "open", "high", "low", "close", "vol"},
        "前复权日线响应",
    )
    records: list[dict[str, object]] = []
    for row in rows:
        record: dict[str, object] = {
            "stock_code": tushare_code_to_pandeng(row.get("ts_code")),
            "trade_date": parse_compact_date(row.get("trade_date")).isoformat(),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": volume_to_shares(row.get("vol"), unit="lot_100"),
            "source": source,
            "adjustment": "qfq",
            "confirmed": confirmed,
        }
        record.update(_provider_metadata(source, row, request_id))
        records.append(record)
    return MappedResponse(tuple(records), request_id)


def map_calendar_response(
    payload: object, *, source: str = FIXTURE_SOURCE
) -> MappedResponse:
    if not isinstance(payload, dict):
        raise ProviderFormatError("交易日历响应必须是 JSON 对象。")
    rows, request_id, fields = _response_rows(payload)
    _require_fields(
        fields,
        {"cal_date", "is_open"},
        "交易日历响应",
    )
    records: list[dict[str, object]] = []
    for row in rows:
        raw_open = row.get("is_open")
        if isinstance(raw_open, bool) or raw_open not in (0, 1, "0", "1"):
            raise ProviderFormatError("交易日历 is_open 只能是 0 或 1。")
        record: dict[str, object] = {
            "trade_date": parse_compact_date(row.get("cal_date")).isoformat(),
            "is_open": bool(int(raw_open)),
            "source": source,
        }
        record.update(_provider_metadata(source, row, request_id))
        records.append(record)
    return MappedResponse(tuple(records), request_id)


class TushareFixtureAdapter(MarketDataAdapter):
    """从本地虚拟响应提供数据，接口形状与未来真实适配器一致。"""

    name = FIXTURE_SOURCE

    def __init__(self, fixture_file: Path):
        payload = load_json_document(fixture_file)
        required = {
            "format",
            "description",
            "source",
            "confirmed_through",
            "daily_adjustment",
            "daily_confirmed",
            "minute_response",
            "daily_qfq_response",
            "calendar_response",
        }
        if set(payload) != required:
            missing = sorted(required.difference(payload))
            extra = sorted(set(payload).difference(required))
            raise ProviderFormatError(
                f"Tushare 离线样例键不完整，缺少={missing}，额外={extra}。"
            )
        if payload["format"] != FIXTURE_FORMAT:
            raise ProviderFormatError("Tushare 离线样例 format 不匹配。")
        if payload["source"] != FIXTURE_SOURCE:
            raise ProviderFormatError("离线样例不能把来源伪装成真实 Tushare 服务。")
        if not isinstance(payload["description"], str) or not payload["description"]:
            raise ProviderFormatError("离线样例必须包含非空 description。")
        daily_confirmed = payload["daily_confirmed"]
        if not isinstance(daily_confirmed, bool):
            raise ProviderFormatError("离线样例 daily_confirmed 必须是布尔值。")

        self._minutes = map_minute_response(
            payload["minute_response"],
            confirmed_through=payload["confirmed_through"],
        )
        self._daily = map_daily_qfq_response(
            payload["daily_qfq_response"],
            adjustment=payload["daily_adjustment"],
            confirmed=daily_confirmed,
        )
        self._calendar = map_calendar_response(payload["calendar_response"])
        self.minute_requests: list[FixtureMinuteRequest] = []

    @property
    def excluded_opening_auction(self) -> tuple[dict[str, object], ...]:
        return self._minutes.excluded_opening_auction

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        if not codes:
            raise ValueError("至少需要一个股票代码。")
        for code in codes:
            pandeng_code_to_tushare(code)
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("分钟请求必须使用带时区时间，且 start 不晚于 end。")
        requested = set(codes)
        start_local = start.astimezone(CHINA_TZ)
        end_local = end.astimezone(CHINA_TZ)
        self.minute_requests.append(FixtureMinuteRequest(codes, start, end))
        selected: list[AdapterRecord] = []
        for record in self._minutes.records:
            bar_time = datetime.fromisoformat(str(record["bar_time"]))
            if (
                record["stock_code"] in requested
                and start_local <= bar_time <= end_local
            ):
                selected.append(record)
        return selected

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        if not codes:
            raise ValueError("至少需要一个股票代码。")
        for code in codes:
            pandeng_code_to_tushare(code)
        if start > end:
            raise ValueError("日线请求 start 不能晚于 end。")
        requested = set(codes)
        return [
            record
            for record in self._daily.records
            if record["stock_code"] in requested
            and start.isoformat() <= str(record["trade_date"]) <= end.isoformat()
        ]

    def fetch_trading_calendar(
        self, start: date, end: date
    ) -> Iterable[AdapterRecord]:
        if start > end:
            raise ValueError("交易日历请求 start 不能晚于 end。")
        return [
            record
            for record in self._calendar.records
            if start.isoformat() <= str(record["trade_date"]) <= end.isoformat()
        ]
