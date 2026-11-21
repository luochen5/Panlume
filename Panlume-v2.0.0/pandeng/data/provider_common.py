"""真实数据服务商接入的通用、确定性校验。

C1 只处理人工编写的离线响应，不发起网络请求。这里的函数也可供后续
显式联网适配器复用；凭据不属于本模块的输入或输出。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .schedule import CHINA_TZ


MAX_PROVIDER_RESPONSE_BYTES = 10 * 1024 * 1024


class ProviderFormatError(ValueError):
    """服务商响应或离线契约样例无法安全映射。"""


def decode_json_response(
    content: bytes, *, max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES
) -> dict[str, object]:
    """限制响应大小并解析 JSON 对象，供离线与后续 HTTP 客户端共用。"""
    if len(content) > max_bytes:
        raise ProviderFormatError(
            f"服务商响应超过 {max_bytes / 1024 / 1024:g} MB 上限。"
        )
    try:
        text = content.decode("utf-8-sig")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderFormatError(f"服务商响应不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderFormatError("服务商响应根节点必须是 JSON 对象。")
    return payload


def load_json_document(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ProviderFormatError(f"找不到服务商离线样例：{path}")
    return decode_json_response(path.read_bytes())


def pandeng_code_to_tushare(value: str) -> str:
    """按 A 股代码段补充交易所后缀；无法确定的代码直接拒绝。"""
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value):
        raise ProviderFormatError("盘灯股票代码必须是 6 位数字字符串。")
    if value.startswith("6"):
        suffix = "SH"
    elif value.startswith(("0", "3")):
        suffix = "SZ"
    elif value.startswith(("4", "8", "920")):
        suffix = "BJ"
    else:
        raise ProviderFormatError(f"无法从代码 {value} 确定 A 股交易所后缀。")
    return f"{value}.{suffix}"


def tushare_code_to_pandeng(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderFormatError("ts_code 必须是类似 600000.SH 的字符串。")
    normalized = value.strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if match is None:
        raise ProviderFormatError(f"无法识别的 ts_code：{value!r}。")
    code = match.group(1)
    if pandeng_code_to_tushare(code) != normalized:
        raise ProviderFormatError(f"ts_code 的代码段与交易所后缀不一致：{value!r}。")
    return code


def parse_china_provider_time(value: object) -> datetime:
    """服务商无时区时间按固定 UTC+8 解释；已有偏移则先换算。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderFormatError(f"服务商时间无法解析：{value!r}。") from exc
    else:
        raise ProviderFormatError("服务商时间必须是非空字符串或 datetime。")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        local = parsed.replace(tzinfo=CHINA_TZ)
    else:
        local = parsed.astimezone(CHINA_TZ)
    if local.second or local.microsecond:
        raise ProviderFormatError("一分钟行情时间必须是整分钟，秒和微秒必须为 0。")
    return local


def parse_compact_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value.strip()):
        raise ProviderFormatError("服务商日期必须使用 YYYYMMDD 字符串。")
    text = value.strip()
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError as exc:
        raise ProviderFormatError(f"服务商日期无效：{value!r}。") from exc


def volume_to_shares(value: object, *, unit: str) -> int:
    """明确换算股或手；拒绝负数、布尔值、非有限值及换算后非整数。"""
    if value is None or isinstance(value, bool):
        raise ProviderFormatError("成交量不能缺失或为布尔值。")
    multipliers = {"share": Decimal(1), "lot_100": Decimal(100)}
    if unit not in multipliers:
        raise ProviderFormatError(f"未知成交量单位：{unit!r}。")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProviderFormatError(f"成交量不是有效数值：{value!r}。") from exc
    if not number.is_finite() or number < 0:
        raise ProviderFormatError("成交量必须是非负有限数值。")
    shares = number * multipliers[unit]
    if shares != shares.to_integral_value():
        raise ProviderFormatError("成交量换算为股后不是整数，不能静默取整。")
    return int(shares)
