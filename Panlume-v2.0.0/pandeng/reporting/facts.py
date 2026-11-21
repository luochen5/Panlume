"""可稳定序列化的只读报告事实包。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


FACT_PACKAGE_VERSION = "1.0"
REPORT_TYPES = {"realtime_event_summary", "daily_close_detail"}
RULE_EVENT_FIELDS = {
    "event_id",
    "rule_id",
    "event_name",
    "status",
    "first_triggered_at",
    "last_triggered_at",
    "resolved_at",
    "condition_expression",
    "observed_values",
    "thresholds",
    "time_window",
    "merge_count",
    "cooldown_until",
    "evidence_record_ids",
}


def _aware_iso(value: Any, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是带时区的 ISO 字符串或 null。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是有效 ISO 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区。")


@dataclass(frozen=True)
class FactPackage:
    fact_package_version: str
    report_type: str
    stock_code: str
    market_timezone: str
    data_range: dict[str, Any]
    source_records: dict[str, Any]
    data_quality: dict[str, Any]
    confirmed_status: dict[str, Any]
    price_volume_statistics: dict[str, Any]
    timeline: list[dict[str, Any]] = field(default_factory=list)
    indicators: dict[str, Any] = field(default_factory=dict)
    rule_events: list[dict[str, Any]] = field(default_factory=list)
    holdings_context: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    traceability: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fact_package_version != FACT_PACKAGE_VERSION:
            raise ValueError("不支持的事实包版本。")
        if self.report_type not in REPORT_TYPES:
            raise ValueError("不支持的报告类型。")
        if not isinstance(self.stock_code, str) or not re.fullmatch(r"\d{6}", self.stock_code):
            raise ValueError("事实包股票代码必须是 6 位数字字符串。")
        if self.market_timezone != "UTC+08:00":
            raise ValueError("事实包必须沿用 UTC+08:00 市场时区。")
        for name in (
            "data_range", "source_records", "data_quality", "confirmed_status",
            "price_volume_statistics", "indicators", "traceability",
        ):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name} 必须是 JSON 对象。")
        for name in ("timeline", "rule_events", "holdings_context", "limitations"):
            if not isinstance(getattr(self, name), list):
                raise ValueError(f"{name} 必须是 JSON 数组。")
        for event in self.rule_events:
            if not isinstance(event, dict) or not RULE_EVENT_FIELDS.issubset(event):
                missing = sorted(RULE_EVENT_FIELDS - set(event))
                raise ValueError("规则事件缺少字段：" + ", ".join(missing))
            for name in ("first_triggered_at", "last_triggered_at", "resolved_at", "cooldown_until"):
                _aware_iso(event[name], f"rule_events.{name}")
            if not isinstance(event["evidence_record_ids"], dict):
                raise ValueError("规则事件 evidence_record_ids 必须是 JSON 对象。")
        self.canonical_json()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_package_version": self.fact_package_version,
            "report_type": self.report_type,
            "stock_code": self.stock_code,
            "market_timezone": self.market_timezone,
            "data_range": copy.deepcopy(self.data_range),
            "source_records": copy.deepcopy(self.source_records),
            "data_quality": copy.deepcopy(self.data_quality),
            "confirmed_status": copy.deepcopy(self.confirmed_status),
            "price_volume_statistics": copy.deepcopy(self.price_volume_statistics),
            "timeline": copy.deepcopy(self.timeline),
            "indicators": copy.deepcopy(self.indicators),
            "rule_events": copy.deepcopy(self.rule_events),
            "holdings_context": copy.deepcopy(self.holdings_context),
            "limitations": copy.deepcopy(self.limitations),
            "traceability": copy.deepcopy(self.traceability),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def model_copy(self) -> dict[str, Any]:
        return copy.deepcopy(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FactPackage":
        if not isinstance(raw, dict):
            raise ValueError("事实包根节点必须是 JSON 对象。")
        return cls(**raw)
