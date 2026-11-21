"""AKShare 东方财富一分钟响应映射、离线样本与显式联网适配器。

联网只发生在 AkshareEastmoneyMinuteAdapter 实际执行 fetch_minute_bars 时。
模块导入、离线样本读取和 pytest 均不会导入 AKShare 或发起网络请求。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .base import AdapterRecord, MarketDataAdapter
from .provider_common import (
    ProviderFormatError,
    pandeng_code_to_tushare,
    parse_china_provider_time,
    volume_to_shares,
)
from .schedule import CHINA_TZ, is_session_minute


VERIFIED_AKSHARE_VERSION = "1.20.65"
LIVE_SOURCE = "akshare_eastmoney"
FIXTURE_SOURCE = "eastmoney_post_close_capture_offline"
FIXTURE_FORMAT = "pandeng.eastmoney-minute-capture.v1"
INTERFACE_NAME = "stock_zh_a_hist_min_em"
REQUIRED_COLUMNS = ("时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "均价")
MAX_RAW_ROWS = 5000


class ProviderLiveError(ValueError):
    """真实请求失败；category 保留失败类型，不能统一伪装成无数据。"""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class ProviderEmptyResponse(ProviderLiveError):
    def __init__(self, message: str = "服务商返回真实空数据；本次没有完整行情。"):
        super().__init__("empty_data", message)


@dataclass(frozen=True)
class EastmoneyMinuteMapping:
    records: tuple[dict[str, object], ...]
    excluded_0930: tuple[dict[str, object], ...]
    raw_row_count: int
    request: dict[str, object]


def _aware_minute(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderFormatError(f"{field} 必须是带时区的 datetime。")
    local = value.astimezone(CHINA_TZ)
    if local.second or local.microsecond:
        raise ProviderFormatError(f"{field} 必须是整分钟。")
    return local


def validate_minute_request(
    stock_code: str, start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    """在任何网络调用之前校验代码和闭区间。"""
    pandeng_code_to_tushare(stock_code)
    start_local = _aware_minute(start, "start")
    end_local = _aware_minute(end, "end")
    if start_local > end_local:
        raise ProviderFormatError("分钟请求 start 不能晚于 end。")
    return start_local, end_local


def _confirmed_boundary(value: datetime | str | None) -> datetime | None:
    # None 是调用方显式选择“全部未确认”，不是省略参数后的默认值。
    if value is None:
        return None
    return parse_china_provider_time(value)


def _plain_scalar(value: object) -> object:
    """把 numpy/pandas 标量转换成可序列化 Python 标量，不改变数值。"""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (ValueError, TypeError):
            pass
    return value


def _plain_row(row: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _plain_scalar(value) for key, value in row.items()}


def dataframe_to_rows(frame: object) -> list[dict[str, object]]:
    """读取 DataFrame 的最小 duck-type 接口，测试无需安装 pandas。"""
    if isinstance(frame, list) and all(isinstance(row, Mapping) for row in frame):
        return [_plain_row(row) for row in frame]
    columns = getattr(frame, "columns", None)
    to_dict = getattr(frame, "to_dict", None)
    if columns is None or not callable(to_dict):
        raise ProviderFormatError("AKShare 分钟响应必须是 pandas DataFrame。")
    try:
        empty = bool(getattr(frame, "empty"))
        rows = to_dict(orient="records")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProviderFormatError(f"无法读取 AKShare DataFrame：{exc}") from exc
    if empty:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ProviderFormatError("AKShare DataFrame 无法转换为原始行数组。")
    column_names = [str(item) for item in list(columns)]
    if len(column_names) != len(set(column_names)):
        raise ProviderFormatError("AKShare DataFrame 存在重复列名。")
    missing = [name for name in REQUIRED_COLUMNS if name not in column_names]
    if missing:
        raise ProviderFormatError(f"东方财富分钟响应缺少字段：{', '.join(missing)}。")
    return [_plain_row(row) for row in rows]


def map_eastmoney_minute_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    stock_code: str,
    start: datetime,
    end: datetime,
    confirmed_through: datetime | str | None,
    source: str = LIVE_SOURCE,
    request_metadata: Mapping[str, object] | None = None,
) -> EastmoneyMinuteMapping:
    """严格映射单次原始响应；09:30 留审计，常规分钟不自动确认。"""
    start_local, end_local = validate_minute_request(stock_code, start, end)
    through = _confirmed_boundary(confirmed_through)
    if through is not None and through > end_local:
        raise ProviderFormatError("confirmed_through 不能晚于本次请求 end。")
    if not isinstance(source, str) or not source.strip():
        raise ProviderFormatError("source 必须是非空字符串。")

    raw_rows = [_plain_row(row) for row in rows]
    if not raw_rows:
        raise ProviderEmptyResponse()
    if len(raw_rows) > MAX_RAW_ROWS:
        raise ProviderFormatError(f"单次分钟响应超过 {MAX_RAW_ROWS} 行安全上限。")

    request = dict(request_metadata or {
        "symbol": stock_code,
        "start_date": start_local.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": end_local.strftime("%Y-%m-%d %H:%M:%S"),
        "period": "1",
        "adjust": "",
    })
    records: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    previous_time: datetime | None = None

    for index, raw in enumerate(raw_rows, 1):
        missing = [name for name in REQUIRED_COLUMNS if name not in raw]
        if missing:
            raise ProviderFormatError(
                f"东方财富分钟第 {index} 行缺少字段：{', '.join(missing)}。"
            )
        bar_time = parse_china_provider_time(raw.get("时间"))
        if previous_time is not None:
            if bar_time == previous_time:
                raise ProviderFormatError(
                    f"东方财富分钟响应存在重复时间：{bar_time.isoformat()}。"
                )
            if bar_time < previous_time:
                raise ProviderFormatError("东方财富分钟响应时间必须严格递增。")
        previous_time = bar_time

        # 仅允许请求日期闭区间内、且不晚于 end 的 09:30 原始特殊点。
        if bar_time.time() == time(9, 30):
            if not (start_local.date() <= bar_time.date() <= end_local.date()) or bar_time > end_local:
                raise ProviderFormatError(
                    f"09:30 特殊点超出请求日期范围：{bar_time.isoformat()}。"
                )
            excluded.append({
                "stock_code": stock_code,
                "bar_time": bar_time.isoformat(),
                "reason": "09:30_special_point",
                "provider_raw": raw,
            })
            continue

        if not start_local <= bar_time <= end_local:
            raise ProviderFormatError(
                f"原始分钟 {bar_time.isoformat()} 超出请求闭区间 "
                f"{start_local.isoformat()} 至 {end_local.isoformat()}。"
            )
        if not is_session_minute(bar_time):
            raise ProviderFormatError(
                f"原始分钟不在盘灯 09:31～11:30、13:01～15:00 口径内："
                f"{bar_time.isoformat()}。"
            )

        record: dict[str, object] = {
            "stock_code": stock_code,
            "bar_time": bar_time.isoformat(),
            "open": raw.get("开盘"),
            "high": raw.get("最高"),
            "low": raw.get("最低"),
            "close": raw.get("收盘"),
            "volume": volume_to_shares(raw.get("成交量"), unit="lot_100"),
            "source": source.strip(),
            "adjustment": "none",
            "confirmed": through is not None and bar_time <= through,
            "provider": "akshare",
            "upstream": "eastmoney",
            "provider_interface": INTERFACE_NAME,
            "provider_request": dict(request),
            "provider_raw": raw,
            "provider_raw_row": index,
        }
        records.append(record)

    if not records:
        raise ProviderEmptyResponse("响应没有盘灯常规一分钟记录；09:30 特殊点不能充当完整数据。")
    return EastmoneyMinuteMapping(tuple(records), tuple(excluded), len(raw_rows), request)


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_live_exception(exc: BaseException) -> tuple[str, str]:
    """仅对有明确证据的错误分类；无法确认时保留 request_failed。"""
    chain = list(_exception_chain(exc))
    for current in chain:
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(current, "status_code", None)
        if isinstance(status, int):
            category = {
                400: "parameter_error",
                401: "authentication_failed",
                403: "permission_denied",
                408: "timeout",
                422: "parameter_error",
                429: "rate_limited",
            }.get(status, "http_error")
            return category, f"上游 HTTP 状态 {status}。"
    names = {
        name
        for current in chain
        for name in (type(current).__name__, *(base.__name__ for base in type(current).__mro__))
    }
    if any("Timeout" in name for name in names):
        return "timeout", "真实请求发生超时。"
    if names.intersection({
        "ConnectionError", "ConnectError", "ProxyError", "SSLError", "NetworkError"
    }):
        return "network_error", "真实请求发生网络连接失败。"
    return "request_failed", "真实请求失败，现有异常信息不足以进一步分类。"


def _load_verified_fetcher() -> Callable[..., object]:
    try:
        installed = version("akshare")
    except PackageNotFoundError as exc:
        raise ProviderLiveError(
            "dependency_missing",
            "虚拟环境未安装 AKShare；请使用 requirements-akshare.txt 安装经验证版本。",
        ) from exc
    if installed != VERIFIED_AKSHARE_VERSION:
        raise ProviderLiveError(
            "dependency_version",
            f"AKShare 已安装版本为 {installed}；本阶段只验收过 {VERIFIED_AKSHARE_VERSION}，"
            "程序不会自动升级或降级。",
        )
    try:
        import akshare as ak
    except ImportError as exc:
        raise ProviderLiveError("dependency_missing", f"AKShare 导入失败：{exc}") from exc
    fetcher = getattr(ak, INTERFACE_NAME, None)
    if not callable(fetcher):
        raise ProviderLiveError("dependency_version", f"当前 AKShare 缺少 {INTERFACE_NAME}。")
    return fetcher


class AkshareEastmoneyMinuteAdapter(MarketDataAdapter):
    """一次 fetch 对每个代码各请求一次；无重试，不猜测分钟闭合。"""

    name = LIVE_SOURCE

    def __init__(
        self,
        *,
        confirmed_through: datetime | str | None,
        fetcher: Callable[..., object] | None = None,
        source: str = LIVE_SOURCE,
    ):
        self._confirmed_through = confirmed_through
        self._fetcher = fetcher
        self.name = source.strip()
        if not self.name:
            raise ProviderFormatError("分钟数据来源不能为空。")
        self.request_count = 0
        self.last_mappings: tuple[EastmoneyMinuteMapping, ...] = ()
        self.last_raw_rows: list[dict[str, object]] = []
        self.last_parameters: dict[str, object] = {}

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        try:
            if not codes:
                raise ProviderFormatError("至少需要一个股票代码。")
            if len(set(codes)) != len(codes):
                raise ProviderFormatError("单次请求的股票代码不能重复。")
            validated = {
                code: validate_minute_request(code, start, end)
                for code in codes
            }
            through = _confirmed_boundary(self._confirmed_through)
            if through is not None and any(through > bounds[1] for bounds in validated.values()):
                raise ProviderFormatError("confirmed_through 不能晚于本次请求 end。")
        except ProviderFormatError as exc:
            raise ProviderLiveError("parameter_error", str(exc)) from exc
        fetcher = self._fetcher or _load_verified_fetcher()
        self.request_count = 0
        self.last_mappings = ()
        self.last_raw_rows = []
        self.last_parameters = {}
        records: list[AdapterRecord] = []
        mappings: list[EastmoneyMinuteMapping] = []
        for code in codes:
            start_local, end_local = validated[code]
            parameters = {
                "symbol": code,
                "start_date": start_local.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end_local.strftime("%Y-%m-%d %H:%M:%S"),
                "period": "1",
                "adjust": "",
            }
            self.last_parameters = dict(parameters)
            self.request_count += 1
            try:
                frame = fetcher(**parameters)
            except Exception as exc:
                category, detail = classify_live_exception(exc)
                message = str(exc).strip()[:300]
                suffix = f" 原始异常：{message}" if message else ""
                raise ProviderLiveError(category, detail + suffix) from exc
            rows = dataframe_to_rows(frame)
            self.last_raw_rows = rows
            mapping = map_eastmoney_minute_rows(
                rows,
                stock_code=code,
                start=start_local,
                end=end_local,
                confirmed_through=self._confirmed_through,
                source=self.name,
                request_metadata=parameters,
            )
            mappings.append(mapping)
            records.extend(mapping.records)
        self.last_mappings = tuple(mappings)
        return records

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        raise NotImplementedError("C3 尚未接入前复权日线；不能伪造成已支持。")

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        raise NotImplementedError("C3 尚未接入交易日历；不能伪造成已支持。")


class EastmoneyMinuteFixtureAdapter(MarketDataAdapter):
    """读取已保存的收盘后真实公开响应；来源明确标为离线回放。"""

    name = FIXTURE_SOURCE

    def __init__(self, fixture_file: Path, *, confirmed_through: datetime | str | None):
        try:
            payload = json.loads(fixture_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderFormatError(f"无法读取东方财富离线样本：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("format") != FIXTURE_FORMAT:
            raise ProviderFormatError("东方财富离线样本 format 不匹配。")
        required = {
            "format", "description", "capture_kind", "provider", "client",
            "akshare_version", "interface", "request", "original_probe", "columns", "rows",
        }
        if set(payload) != required:
            raise ProviderFormatError("东方财富离线样本顶层键不完整或包含未知键。")
        if payload["capture_kind"] != "post_close_probe":
            raise ProviderFormatError("离线样本必须明确标为 post_close_probe。")
        if payload["provider"] != "eastmoney" or payload["client"] != "akshare":
            raise ProviderFormatError("离线样本提供方或客户端标识不匹配。")
        if payload["akshare_version"] != VERIFIED_AKSHARE_VERSION:
            raise ProviderFormatError("离线样本 AKShare 版本不是本次验收版本。")
        if payload["interface"] != INTERFACE_NAME:
            raise ProviderFormatError("离线样本接口名称不匹配。")
        if not isinstance(payload["description"], str) or not payload["description"].strip():
            raise ProviderFormatError("离线样本必须包含非空说明。")
        probe = payload["original_probe"]
        if not isinstance(probe, dict) or set(probe) != {
            "elapsed_seconds", "database_written", "model_called", "result", "csv_sha256"
        }:
            raise ProviderFormatError("离线样本原始探针记录不完整。")
        if (
            probe["database_written"] is not False
            or probe["model_called"] is not False
            or probe["result"] != "DATAFRAME_RECEIVED"
        ):
            raise ProviderFormatError("离线样本的原始探针副作用或结果标记不匹配。")
        elapsed = probe["elapsed_seconds"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise ProviderFormatError("离线样本原始耗时必须是非负数值。")
        if (
            not isinstance(probe["csv_sha256"], str)
            or len(probe["csv_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in probe["csv_sha256"])
        ):
            raise ProviderFormatError("离线样本原始 CSV SHA-256 格式无效。")
        if payload["columns"] != list(REQUIRED_COLUMNS):
            raise ProviderFormatError("离线样本字段顺序与已验收响应不一致。")
        request = payload["request"]
        if not isinstance(request, dict) or set(request) != {
            "symbol", "start_date", "end_date", "period", "adjust"
        }:
            raise ProviderFormatError("离线样本请求参数不完整。")
        if request["period"] != "1" or request["adjust"] != "":
            raise ProviderFormatError("离线样本必须明确为一分钟、不复权。")
        rows = payload["rows"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProviderFormatError("离线样本 rows 必须是对象数组。")

        try:
            capture_start = parse_china_provider_time(request["start_date"])
            capture_end = parse_china_provider_time(request["end_date"])
        except ProviderFormatError as exc:
            raise ProviderFormatError(f"离线样本请求时间无效：{exc}") from exc
        if (
            capture_start.time() != time(9, 30)
            or capture_end.time() != time(15, 0)
            or capture_start.date() != capture_end.date()
        ):
            raise ProviderFormatError("离线样本请求必须是同一交易日 09:30～15:00。")
        # 调用方请求从 09:31 开始；原始 09:30 仍由映射器单列审计。
        regular_start = capture_start.replace(hour=9, minute=31)
        self._stock_code = str(request["symbol"])
        self.capture_start = regular_start
        self.capture_end = capture_end
        self._mapping = map_eastmoney_minute_rows(
            rows,
            stock_code=self._stock_code,
            start=regular_start,
            end=capture_end,
            confirmed_through=confirmed_through,
            source=self.name,
            request_metadata=request,
        )

    @property
    def excluded_0930(self) -> tuple[dict[str, object], ...]:
        return self._mapping.excluded_0930

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        codes = tuple(stock_codes)
        if len(codes) != 1 or codes[0] != self._stock_code:
            raise ProviderFormatError("该离线样本只包含一只股票，请求代码必须完全匹配。")
        start_local, end_local = validate_minute_request(codes[0], start, end)
        selected = []
        for record in self._mapping.records:
            bar_time = datetime.fromisoformat(str(record["bar_time"]))
            if start_local <= bar_time <= end_local:
                selected.append(record)
        if not selected:
            raise ProviderEmptyResponse("离线样本在请求闭区间内没有常规分钟。")
        return selected

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        raise NotImplementedError("该分钟样本不包含前复权日线。")

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        raise NotImplementedError("该分钟样本不包含交易日历。")
