"""把已保存的规则检查转换为摘要输入、固定模板和确定性输出校验。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pandeng.reporting.facts import FactPackage
from pandeng.rules.engine import RuleNotice
from pandeng.storage.db import Database


_NUMBER = re.compile(r"(?<![0-9A-Za-z:])[+-]?\d+(?:\.\d+)?(?![0-9A-Za-z:.])")
_TAGS = re.compile(r"<\s*/?\s*think\s*>|<\|[^>]*\|>", re.IGNORECASE)
_FORBIDDEN = {
    "交易指令": (
        "买入", "卖出", "加仓", "减仓", "清仓", "满仓", "止盈",
        "保证收益", "稳赚", "包赚", "buy now", "sell now", "guaranteed return",
    ),
    "未经输入支持的原因或预测": (
        "利好消息", "利空消息", "主力资金", "因为", "由于", "预计将会",
        "必涨", "必跌", "肯定上涨", "肯定下跌", "because ", "due to ",
        "will rise", "will fall", "certain to rise", "certain to fall",
    ),
}
_REQUIRED_FIELDS = {
    "fixed_stop": ("close", "stop_loss_price"),
    "rapid_rise": ("return_1m_pct", "threshold_pct"),
    "rapid_fall": ("return_1m_pct", "threshold_pct"),
    "volume_breakout": (
        "close", "prior_breakout_high", "volume_ratio", "volume_threshold",
    ),
    "below_vwap": ("close", "vwap_estimate"),
}


@dataclass(frozen=True)
class SummaryFacts:
    event_check_id: int
    event_id: int
    snapshot_id: int
    analysis_run_id: int
    stock_code: str
    data_time: str
    event_type: str
    priority: str
    notification_kind: str
    message: str
    position_lot_id: int | None
    values: dict[str, Any]
    payload: dict[str, Any]
    unknown_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """只规范摘要展示事实，不回写指标快照或规则原始载荷。"""
        raw_vwap = self.payload.get("vwap_estimate")
        display_vwap = None if raw_vwap is None else _format_vwap(raw_vwap)
        for field in ("values", "payload"):
            normalized = dict(getattr(self, field))
            if normalized.get("vwap_estimate") is not None:
                normalized["vwap_estimate"] = _format_vwap(normalized["vwap_estimate"])
            object.__setattr__(self, field, normalized)
        if display_vwap is not None:
            object.__setattr__(
                self, "message", self.message.replace(str(raw_vwap), display_vwap)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_check_id": self.event_check_id,
            "event_id": self.event_id,
            "snapshot_id": self.snapshot_id,
            "analysis_run_id": self.analysis_run_id,
            "stock_code": self.stock_code,
            "data_time": self.data_time,
            "event_type": self.event_type,
            "priority": self.priority,
            "notification_kind": self.notification_kind,
            "message": self.message,
            "position_lot_id": self.position_lot_id,
            "values": self.values,
            "payload": self.payload,
            "unknown_reasons": list(self.unknown_reasons),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SummaryFacts":
        return cls(
            event_check_id=int(raw["event_check_id"]),
            event_id=int(raw["event_id"]),
            snapshot_id=int(raw["snapshot_id"]),
            analysis_run_id=int(raw["analysis_run_id"]),
            stock_code=str(raw["stock_code"]),
            data_time=str(raw["data_time"]),
            event_type=str(raw["event_type"]),
            priority=str(raw["priority"]),
            notification_kind=str(raw["notification_kind"]),
            message=str(raw["message"]),
            position_lot_id=(
                None if raw.get("position_lot_id") is None else int(raw["position_lot_id"])
            ),
            values=dict(raw["values"]),
            payload=dict(raw["payload"]),
            unknown_reasons=tuple(str(item) for item in raw["unknown_reasons"]),
        )


def load_summary_facts(database: Database, notice: RuleNotice) -> SummaryFacts:
    """只读取已经提交的检查链；恢复、回放和未实际提醒的检查没有摘要资格。"""
    if notice.event_check_id is None:
        raise ValueError("规则提醒缺少事件检查 ID，不能创建摘要任务。")
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT c.id AS event_check_id, c.event_id, c.snapshot_id, c.analysis_run_id,
                   c.data_time, c.notified, c.notification_kind, c.payload_json,
                   e.stock_code, e.event_type, e.priority, e.position_lot_id,
                   r.analysis_mode, s.values_json, s.unknown_reasons_json
            FROM rule_event_checks AS c
            JOIN rule_events AS e ON e.id = c.event_id
            JOIN analysis_runs AS r ON r.id = c.analysis_run_id
            JOIN indicator_snapshots AS s ON s.id = c.snapshot_id
            WHERE c.id = ?
            """,
            (notice.event_check_id,),
        ).fetchone()
    if row is None:
        raise ValueError("规则提醒对应的事件检查不存在。")
    if not bool(row["notified"]) or row["analysis_mode"] != "live":
        raise ValueError("只有 live 模式下实际产生的提醒可以创建摘要任务。")
    if (
        row["stock_code"] != notice.stock_code
        or row["event_type"] != notice.event_type
        or row["event_id"] != notice.event_id
    ):
        raise ValueError("规则提醒与数据库中的事件检查不一致。")
    return SummaryFacts(
        event_check_id=int(row["event_check_id"]),
        event_id=int(row["event_id"]),
        snapshot_id=int(row["snapshot_id"]),
        analysis_run_id=int(row["analysis_run_id"]),
        stock_code=str(row["stock_code"]),
        data_time=str(row["data_time"]),
        event_type=str(row["event_type"]),
        priority=str(row["priority"]),
        notification_kind=str(row["notification_kind"]),
        message=notice.message,
        position_lot_id=(
            None if row["position_lot_id"] is None else int(row["position_lot_id"])
        ),
        values=dict(json.loads(row["values_json"])),
        payload=dict(json.loads(row["payload_json"])),
        unknown_reasons=tuple(json.loads(row["unknown_reasons_json"])),
    )


def build_prompt(
    facts: SummaryFacts,
    max_chars: int,
    language: str = "zh-CN",
    fact_package: FactPackage | None = None,
) -> str:
    payload = facts.to_dict()
    if fact_package is not None:
        payload["fact_package"] = fact_package.model_copy()
    facts_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if language == "en":
        return (
            "You are a writing assistant for local research records. Restate only facts already "
            "present in the input. Do not infer news or price-movement causes, add numbers, or "
            "provide trading instructions or return promises.\n"
            f"Write one final English summary of at most {max_chars} characters, with no minimum length. "
            "Preserve the stock code, complete data_time, and key event values exactly. "
            "Output only the final summary, without reasoning, Markdown fences, or extra tags.\n"
            f"Saved facts: {facts_json}"
        )
    return (
        "你是本地研究记录的文字整理器。只复述输入中已有的事实，不推测新闻或涨跌原因，"
        "不补充输入外的数字，不提供买卖、加减仓或收益承诺。\n"
        f"请输出一段中文最终摘要，最多 {max_chars} 个字符，不设最低长度。"
        "必须逐字保留股票代码和完整 data_time，并保留该事件的关键数值。"
        "只输出最终摘要，不输出思考过程、Markdown 代码块或额外标签。\n"
        f"已保存事实：{facts_json}"
    )


def _english_template(facts: SummaryFacts) -> str:
    """从已保存事实构造英文模板，不改写原始中文告警。"""
    code, stamp = facts.stock_code, facts.data_time
    values = facts.payload
    if facts.event_type in {"rapid_rise", "rapid_fall"}:
        label = "Rapid rise" if facts.event_type == "rapid_rise" else "Rapid fall"
        change = values.get("return_1m_pct")
        threshold = values.get("threshold_pct")
        if change is None:
            return f"{code} {stamp} {label}: indicator unknown."
        return (
            f"{code} {stamp} {label}: 1-minute change {float(change):.4f}%, "
            f"threshold ±{float(threshold):.4f}%."
        )
    if facts.event_type == "volume_breakout":
        if values.get("volume_ratio") is None or values.get("prior_breakout_high") is None:
            return f"{code} {stamp} Volume breakout: indicators unknown."
        return (
            f"{code} {stamp} Volume breakout: close {float(values['close']):.4f}, "
            f"prior high {float(values['prior_breakout_high']):.4f}, "
            f"same-minute volume ratio {float(values['volume_ratio']):.4f}."
        )
    if facts.event_type == "below_vwap":
        vwap = values.get("vwap_estimate")
        if vwap is None:
            return f"{code} {stamp} VWAP unknown."
        relation = "below" if float(values["close"]) < float(vwap) else "not below"
        return (
            f"{code} {stamp} Minute close {relation} estimated typical-price VWAP: "
            f"close {float(values['close']):.4f}, estimated VWAP {_format_vwap(vwap)}."
        )
    if facts.event_type == "fixed_stop":
        close, stop = float(values["close"]), float(values["stop_loss_price"])
        sellable = values.get("sellable_quantity")
        return (
            f"{code} {stamp} Fixed stop {'triggered' if close <= stop else 'not triggered'}: "
            f"lot {facts.position_lot_id}, minute close {close:.4f}, manually confirmed stop "
            f"{stop:.4f}; trigger condition: close <= stop. T+1 status {values.get('t1_status')}, "
            f"sellable quantity {'unknown' if sellable is None else sellable}. "
            "Alerts are affected by polling and source latency. Risk record only; no trade is executed."
        )
    return f"{code} {stamp} {facts.event_type}: see the original deterministic alert; no trade is executed."


def template_summary(facts: SummaryFacts, max_chars: int, language: str = "zh-CN") -> str:
    """回退内容来自确定性事实；极端长文本只截断展示，原始告警不受影响。"""
    message = _english_template(facts) if language == "en" else facts.message
    if len(message) <= max_chars:
        return message
    suffix = (
        "… See the original alert; no trade is executed." if language == "en"
        else "……详见原始告警；不执行交易。"
    )
    return message[: max(1, max_chars - len(suffix))] + suffix


def _format_vwap(value: Any) -> str:
    """VWAP 只在摘要事实层统一为四位小数字符串。"""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("VWAP 摘要事实必须是有限数值。") from exc
    if not number.is_finite():
        raise ValueError("VWAP 摘要事实必须是有限数值。")
    return format(number, ".4f")


def _canonical_number(token: Any) -> str | None:
    try:
        value = Decimal(str(token).strip().rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _number_tokens(text: str, facts: SummaryFacts) -> list[str]:
    protected = text.replace(facts.stock_code, " ").replace(facts.data_time, " ")
    return _NUMBER.findall(protected)


def _allowed_numbers(facts: SummaryFacts) -> set[str]:
    source = json.dumps(facts.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)
    return {
        number
        for token in _number_tokens(source, facts)
        if (number := _canonical_number(token)) is not None
    }


def validate_summary(text: str, facts: SummaryFacts, max_chars: int) -> tuple[str, ...]:
    """返回全部失败原因；空元组表示可保存为模型摘要。"""
    reasons: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ("最终摘要为空。",)
    if len(stripped) > max_chars:
        reasons.append(f"最终摘要超过 {max_chars} 个字符。")
    if _TAGS.search(stripped):
        reasons.append("最终摘要仍包含思考段或异常标签。")
    if facts.stock_code not in stripped:
        reasons.append("最终摘要缺少原股票代码。")
    if facts.data_time not in stripped:
        reasons.append("最终摘要缺少完整行情时间。")
    lowered = stripped.lower()
    for label, terms in _FORBIDDEN.items():
        if any(term in lowered for term in terms):
            reasons.append(f"最终摘要包含{label}。")

    tokens = _number_tokens(stripped, facts)
    canonical = [
        number for token in tokens if (number := _canonical_number(token)) is not None
    ]
    allowed = _allowed_numbers(facts)
    extras = sorted({number for number in canonical if number not in allowed})
    if extras:
        reasons.append("最终摘要包含输入外数字：" + ", ".join(extras) + "。")

    available = Counter(canonical)
    required = Counter()
    required_labels: dict[str, list[str]] = {}
    missing: list[str] = []
    for field in _REQUIRED_FIELDS.get(facts.event_type, ()):
        value = facts.payload.get(field)
        number = _canonical_number(value) if value is not None else None
        if number is not None:
            required[number] += 1
            required_labels.setdefault(number, []).append(field)
    for number, count in required.items():
        if available[number] < count:
            missing.append(
                f"{'/'.join(required_labels[number])}={number}（需要出现 {count} 次）"
            )
    if missing:
        reasons.append("最终摘要缺少关键数值：" + ", ".join(missing) + "。")
    return tuple(dict.fromkeys(reasons))
