"""由统一事实包生成确定性报告；无模型时也保留完整程序事实区。"""

from __future__ import annotations

import json
from typing import Any

from .facts import FactPackage
from .i18n import localize_report_text


EVENT_NAMES_EN = {
    "rapid_rise": "Rapid rise",
    "rapid_fall": "Rapid fall",
    "volume_breakout": "Volume breakout",
    "below_vwap": "Below estimated VWAP",
    "fixed_stop": "Fixed stop",
}


def _number(value: Any, language: str) -> str:
    if value is None:
        return "不可用" if language == "zh-CN" else "unavailable"
    if isinstance(value, bool):
        return ("是" if value else "否") if language == "zh-CN" else str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_name(event: dict[str, Any], language: str) -> str:
    return event["event_name"] if language == "zh-CN" else EVENT_NAMES_EN.get(event["rule_id"], event["rule_id"])


def _status(status: Any, language: str) -> str:
    if language == "zh-CN":
        return {"active": "持续中", "cleared": "已解除", "ok": "正常", "unknown": "未知"}.get(status, str(status))
    return {"active": "active", "cleared": "resolved", "ok": "ok", "unknown": "unknown"}.get(status, str(status))


def _ids(items: list[Any], limit: int = 30) -> str:
    if len(items) <= limit:
        return ", ".join(str(item) for item in items)
    shown = ", ".join(str(item) for item in items[:limit])
    return f"{shown}, … (+{len(items) - limit})"


def _evidence_summary(event: dict[str, Any], language: str) -> str:
    label = "个" if language == "zh-CN" else "records"
    parts = []
    for key, values in event["evidence_record_ids"].items():
        parts.append(f"{key} {len(values)} {label} [{_ids(values, 8)}]")
    return "; ".join(parts)


def _interpretation(event: dict[str, Any], language: str) -> str:
    rule = event["rule_id"]
    if language == "en":
        messages = {
            "rapid_rise": "This only describes a one-minute threshold crossing. Short-term noise is also possible; news, capital flows, and future direction cannot be inferred.",
            "rapid_fall": "This only describes a one-minute threshold crossing. Short-term noise is also possible; news, capital flows, and future direction cannot be inferred.",
            "volume_breakout": "Price and volume met the stored conditions, but individual prints or baseline limits may also explain the observation; it is not a forecast.",
            "below_vwap": "This is only a comparison with typical-price estimated VWAP, not exact turnover VWAP. It does not establish a trend by itself.",
            "fixed_stop": "This is a manually confirmed risk-record threshold. Polling and source latency can affect the alert; no order is executed.",
        }
    else:
        messages = {
            "rapid_rise": "这只说明一分钟收益达到阈值，短时波动同样可能；不能据此推断消息、资金或后续方向。",
            "rapid_fall": "这只说明一分钟收益达到阈值，短时波动同样可能；不能据此推断消息、资金或后续方向。",
            "volume_breakout": "价格与成交量达到已保存条件，但单笔成交或基线限制也可能形成该现象；这不是走势预测。",
            "below_vwap": "这里仅比较典型价估算 VWAP，不是按真实成交额计算的精确 VWAP，也不能单独确认趋势。",
            "fixed_stop": "这是人工确认风险记录的阈值提醒，轮询与来源延迟可能影响提醒时间；程序不执行订单。",
        }
    return messages.get(rule, "只能复述已保存事实，不能补充未经记录支持的原因。" if language == "zh-CN" else "Only saved facts are available; no unsupported cause can be inferred.")


def _display_options(display: dict[str, Any] | None) -> dict[str, Any]:
    options = {
        "detail_level": "standard", "length_preference": "standard",
        "show_data_quality": True, "show_indicators": True, "show_rule_basis": True,
        "show_timeline": True, "show_conditional_interpretation": True,
        "show_limitations_traceability": True,
    }
    if display:
        options.update(display)
    return options


def _filter_realtime(lines: list[str], display: dict[str, Any] | None, language: str) -> str:
    options = _display_options(display)
    filtered = []
    for line in lines:
        if not options["show_rule_basis"] and line.startswith(("触发依据：", "Basis:")):
            continue
        if not options["show_data_quality"] and line.startswith(("数据：", "Data:")):
            continue
        if not options["show_limitations_traceability"] and line.startswith(("限制：", "Limits:")):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _filter_detailed(text: str, display: dict[str, Any] | None, language: str) -> str:
    options = _display_options(display)
    if language == "en":
        sections = {
            "1. ": "show_data_quality", "3. ": "show_timeline",
            "4. ": "show_indicators", "6. ": "show_conditional_interpretation",
            "7. ": "show_limitations_traceability", "8. ": "show_limitations_traceability",
        }
        detail_prefixes = ("Indicator values:", "Original alert:")
    else:
        sections = {
            "一、": "show_data_quality", "三、": "show_timeline",
            "四、": "show_indicators", "六、": "show_conditional_interpretation",
            "七、": "show_limitations_traceability", "八、": "show_limitations_traceability",
        }
        detail_prefixes = ("指标明细：", "原始告警：")
    filtered = []
    hiding = False
    compact = options["detail_level"] == "brief" or options["length_preference"] == "short"
    for line in text.splitlines():
        heading = next((prefix for prefix in sections if line.startswith(prefix)), None)
        if heading is not None:
            hiding = not options[sections[heading]]
        if hiding:
            continue
        stripped = line.strip()
        if not options["show_rule_basis"] and stripped.startswith(("条件 ", "Condition ")):
            continue
        if compact and stripped.startswith(detail_prefixes):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def render_realtime_summary(
    facts: FactPackage, language: str = "zh-CN", display: dict[str, Any] | None = None
) -> str:
    if not facts.rule_events:
        text = (
            f"{facts.stock_code} {facts.data_range['end']} 没有可追溯的规则事件；"
            "这不等于证明价格没有变化。请查看数据质量与原始记录。"
            if language != "en" else
            f"{facts.stock_code} {facts.data_range['end']}: no traceable rule event. "
            "This does not prove that prices did not change; review data quality and source records."
        )
        return localize_report_text(text, language)
    event = facts.rule_events[0]
    quality = facts.data_quality
    trace = event["evidence_record_ids"]
    if language == "en":
        lines = [
            f"{facts.stock_code} {facts.data_range['end']} {_event_name(event, language)}: {_status(event['status'], language)}.",
            f"Basis: {event['condition_expression']}; observed {_json(event['observed_values'])}; thresholds {_json(event['thresholds'])}; window {_json(event['time_window'])}.",
            f"First {event['first_triggered_at']}; last {event['last_triggered_at']}; resolved {_number(event['resolved_at'], language)}; merged repeats {event['merge_count']}; cooldown until {_number(event['cooldown_until'], language)}.",
            f"Data: {quality['errors']} quality errors, {len(quality['missing_minutes'])} missing minutes, sources {_json(facts.source_records['sources'])}.",
            f"Limits: {' '.join(facts.limitations) or 'none recorded'} Evidence event {event['event_id']}, checks {_ids(trace['event_check_ids'])}, snapshots {_ids(trace['snapshot_ids'])}. No trade is executed.",
        ]
    else:
        lines = [
            f"{facts.stock_code} {facts.data_range['end']} {_event_name(event, language)}：当前{_status(event['status'], language)}。",
            f"触发依据：{event['condition_expression']}；实际值 {_json(event['observed_values'])}；阈值 {_json(event['thresholds'])}；窗口 {_json(event['time_window'])}。",
            f"首次 {event['first_triggered_at']}；末次 {event['last_triggered_at']}；解除 {_number(event['resolved_at'], language)}；重复合并 {event['merge_count']} 次；冷却至 {_number(event['cooldown_until'], language)}。",
            f"数据：质量错误 {quality['errors']} 条，缺失分钟 {len(quality['missing_minutes'])} 条，来源 {_json(facts.source_records['sources'])}。",
            f"限制：{' '.join(facts.limitations) or '无额外记录'} 追溯：事件 {event['event_id']}，检查 {_ids(trace['event_check_ids'])}，快照 {_ids(trace['snapshot_ids'])}。仅作事实记录，不执行交易。",
        ]
    return localize_report_text(_filter_realtime(lines, display, language), language)


def _major_timeline(facts: FactPackage) -> list[dict[str, Any]]:
    snapshots = [item for item in facts.timeline if item["kind"] == "indicator_snapshot"]
    checks = [item for item in facts.timeline if item["kind"] == "rule_check"]
    major: list[dict[str, Any]] = []
    if snapshots:
        major.append(snapshots[0])
        if len(snapshots) > 1:
            major.append(snapshots[-1])
    previous: dict[int, Any] = {}
    for check in checks:
        event_id = int(check["event_id"])
        state = check["is_active"]
        if event_id not in previous or previous[event_id] != state or check["notified"]:
            major.append(check)
        previous[event_id] = state
    return sorted(major, key=lambda item: (item["time"], item["kind"], item.get("event_check_id", item.get("snapshot_id", 0))))


def render_detailed_report(
    facts: FactPackage, language: str = "zh-CN", display: dict[str, Any] | None = None
) -> str:
    text = _detailed_en(facts) if language == "en" else _detailed_zh(facts)
    return localize_report_text(_filter_detailed(text, display, language), language)


def _detailed_zh(facts: FactPackage) -> str:
    minutes = facts.confirmed_status
    quality = facts.data_quality
    price = facts.price_volume_statistics
    latest = facts.indicators["latest_snapshot"]
    daily = facts.indicators["daily"]
    calendar_text = {"open": "明确开市", "closed": "明确休市", "unknown": "状态未知"}
    lines = [
        f"盘灯本地每日复盘：{facts.data_range['session_date']}；{facts.stock_code}。",
        "一、程序事实区｜数据范围与质量",
        (
            f"数据状态 {quality['status']}；日历 {calendar_text[minutes['calendar']]}；"
            f"分钟 {minutes['minutes_saved']} 条，已确认 {minutes['minutes_confirmed']}，"
            f"未确认 {minutes['minutes_unconfirmed']}；来源 {', '.join(facts.source_records['sources']) or '无'}。"
        ),
        (
            f"数据范围 {facts.data_range['start'] or '无'} 至 {facts.data_range['end']}；"
            f"质量错误 {quality['errors']} 条；信息与去重记录 {quality['info']} 条；"
            f"已保存指标快照 {facts.indicators['snapshot_count']} 条。"
        ),
        f"缺失分钟 {len(quality['missing_minutes'])} 条；非预期分钟 {len(quality['extra_minutes'])} 条；分钟调整口径 adjustment=none。",
        "二、价格、成交量与日内高低点",
        (
            f"首分钟开盘 {_number(price['open'], 'zh-CN')}；最新收盘 {_number(price['close'], 'zh-CN')}；"
            f"日内最高 {_number(price['high'], 'zh-CN')}；最低 {_number(price['low'], 'zh-CN')}；"
            f"累计成交量 {price['total_volume']}；典型价估算 VWAP {_number(price['vwap_estimate'], 'zh-CN')}。"
        ),
    ]
    if price["daily"] is None:
        lines.append("当日前复权日 K：不可用；不能补写开高低收或成交量。")
    else:
        daily_bar = price["daily"]
        lines.append(
            f"当日前复权日 K（记录 {daily_bar['daily_bar_id']}，{daily_bar['adjustment']}）：开 {daily_bar['open']}，"
            f"高 {daily_bar['high']}，低 {daily_bar['low']}，收 {daily_bar['close']}，量 {daily_bar['volume']}；"
            f"确认={'是' if daily_bar['confirmed'] else '否'}；来源 {daily_bar['source']}。"
        )
    lines.append("三、按时间排列的主要变化")
    major = _major_timeline(facts)
    if not major:
        lines.append("无已保存指标快照或规则状态变化，时间线不可用。")
    event_names = {int(event["event_id"]): event["event_name"] for event in facts.rule_events}
    for item in major:
        if item["kind"] == "indicator_snapshot":
            lines.append(
                f"  {item['time']} 指标快照 {item['snapshot_id']}：收盘 {_number(item['close'], 'zh-CN')}；"
                f"1 分钟收益 {_number(item['return_1m_pct'], 'zh-CN')}%；5 分钟收益 {_number(item['return_5m_pct'], 'zh-CN')}%。"
            )
        else:
            state = "状态未知" if item["is_active"] is None else "触发/持续" if item["is_active"] else "解除"
            lines.append(
                f"  {item['time']} 规则检查 {item['event_check_id']}：事件 {item['event_id']} "
                f"{event_names.get(item['event_id'], '未知事件')} {state}；提醒={item['notification_kind'] or '无'}。"
            )
    lines.append("四、已启用指标与日线结果")
    if latest is None:
        lines.append("最近指标快照：无；相关指标状态未知。")
    else:
        values = latest["values"]
        lines.append(
            f"最近指标快照 {latest['snapshot_id']}：{latest['data_time']}；状态 {latest['status']}；"
            f"收盘 {_number(values.get('close'), 'zh-CN')}；VWAP 估算 {_number(values.get('vwap_estimate'), 'zh-CN')}；"
            f"成交量比 {_number(values.get('volume_ratio'), 'zh-CN')}；模式 {latest['analysis_mode']}。"
        )
        lines.append("指标明细：" + _json(values))
        lines.append(f"指标证据分钟 ID：{_ids(latest['source_bar_ids'])}。")
    for timeframe, momentum in facts.indicators.get("momentum", {}).items():
        label = "分钟" if timeframe == "minute" else "日线"
        macd = momentum.get("macd")
        lines.append(
            f"{label} MACD/RSI：状态 {momentum['status']}；参数 {_json(momentum['parameters'])}；"
            f"输入范围 {_json(momentum['input_range'])}；有效样本 {momentum['sample_count']}；"
            f"MACD {_json(macd) if macd else '数据不足或不可用'}；"
            f"RSI {_number(momentum['rsi'], 'zh-CN')}；原因 {momentum['reason'] or '无'}。"
        )
    if daily is None:
        lines.append("日线结论：尚未保存每日分析，状态未知；收盘本身不代表日 K 已确认。")
    else:
        signal = "未知" if daily["formal_signal"] is None else "是" if daily["formal_signal"] else "否"
        lines.append(
            f"日线结论：状态 {daily['status']}；MA20 {_number(daily['ma20'], 'zh-CN')}；"
            f"拐头天数 {_number(daily['turn_days'], 'zh-CN')}；正式双条件 {signal}；"
            f"补记={daily['is_backfill']}；原因 {daily['unknown_reason'] or '无'}。"
        )
    lines.append(f"五、规则命中、解除、合并与冷却（规则事件：{len(facts.rule_events)} 条）")
    if not facts.rule_events:
        lines.append("没有已保存规则事件；这不等于证明没有价格变化，也不能补写命中结论。")
    for event in facts.rule_events:
        lines.append(
            f"  事件 {event['event_id']} {event['rule_id']} {event['event_name']} {event['priority']} "
            f"{event['status']}；首次 {event['first_triggered_at']}；末次 {event['last_triggered_at']}；"
            f"解除 {_number(event['resolved_at'], 'zh-CN')}；检查 {event['check_count']}；提醒 {event['reminder_count']}；"
            f"重复合并 {event['merge_count']}；冷却至 {_number(event['cooldown_until'], 'zh-CN')}。"
        )
        lines.append(
            f"  条件 {event['condition_expression']}；实际 {_json(event['observed_values'])}；"
            f"阈值 {_json(event['thresholds'])}；窗口 {_json(event['time_window'])}；"
            f"证据 {_evidence_summary(event, 'zh-CN')}。"
        )
        lines.append(f"  原始告警：{event['latest_message']}")
    if facts.holdings_context:
        lines.append("人工确认持仓上下文（只读）：" + _json(facts.holdings_context))
    lines.append("六、有数据边界的解释及相反可能")
    if not facts.rule_events:
        lines.append("没有规则事件可供解释；只能复核原始价格和质量记录，不能补写归因。")
    for event in facts.rule_events:
        lines.append(f"  {event['event_name']}：{_interpretation(event, 'zh-CN')}")
    lines.append("七、数据不足与无法确认的事项")
    lines.extend(f"  - {item}" for item in facts.limitations) if facts.limitations else lines.append("  - 无额外限制记录；仍不构成收益承诺或投资建议。")
    lines.extend([
        "八、原始记录和计算结果的追溯信息",
        f"分钟记录 {len(facts.traceability['minute_bar_ids'])} 条，ID {_ids(facts.traceability['minute_bar_ids'])}；日 K ID {_ids(facts.traceability['daily_bar_ids'])}。",
        f"指标快照 ID {_ids(facts.traceability['snapshot_ids'])}；分析运行 ID {_ids(facts.traceability['analysis_run_ids'])}。",
        f"规则事件 ID {_ids(facts.traceability['rule_event_ids'])}；规则检查 ID {_ids(facts.traceability['event_check_ids'])}；质量记录 ID {_ids(facts.traceability['quality_issue_ids'])}。",
        "分析输入 SHA-256：" + _json(facts.traceability["analysis_input_hashes"]),
        "本报告只整理已保存事实，不重新计算规则，不产生实时提醒。",
        "本次本地复盘未调用 Ollama 或云端，不修改行情、规则、持仓，也不执行交易。",
    ])
    return "\n".join(lines)


def _detailed_en(facts: FactPackage) -> str:
    minutes = facts.confirmed_status
    quality = facts.data_quality
    price = facts.price_volume_statistics
    latest = facts.indicators["latest_snapshot"]
    daily = facts.indicators["daily"]
    lines = [
        f"Panlume local daily review: {facts.data_range['session_date']}; {facts.stock_code}.",
        "1. Program facts | Data range and quality",
        (
            f"Data status {quality['status']}; calendar {minutes['calendar']}; "
            f"minutes {minutes['minutes_saved']}, confirmed {minutes['minutes_confirmed']}, "
            f"unconfirmed {minutes['minutes_unconfirmed']}; sources {', '.join(facts.source_records['sources']) or 'none'}."
        ),
        (
            f"Range {facts.data_range['start'] or 'none'} to {facts.data_range['end']}; "
            f"quality errors {quality['errors']}; information/deduplication records {quality['info']}; "
            f"saved indicator snapshots {facts.indicators['snapshot_count']}."
        ),
        f"Missing minutes {len(quality['missing_minutes'])}; unexpected minutes {len(quality['extra_minutes'])}; minute adjustment=none.",
        "2. Price, volume, and intraday range",
        (
            f"First-minute open {_number(price['open'], 'en')}; latest close {_number(price['close'], 'en')}; "
            f"high {_number(price['high'], 'en')}; low {_number(price['low'], 'en')}; "
            f"total volume {price['total_volume']}; typical-price estimated VWAP {_number(price['vwap_estimate'], 'en')}."
        ),
    ]
    if price["daily"] is None:
        lines.append("Daily qfq bar: unavailable; OHLC and volume cannot be invented.")
    else:
        daily_bar = price["daily"]
        lines.append(
            f"Daily bar {daily_bar['daily_bar_id']} ({daily_bar['adjustment']}): open {daily_bar['open']}, "
            f"high {daily_bar['high']}, low {daily_bar['low']}, close {daily_bar['close']}, "
            f"volume {daily_bar['volume']}; confirmed={str(daily_bar['confirmed']).lower()}; source {daily_bar['source']}."
        )
    lines.append("3. Main changes in time order")
    major = _major_timeline(facts)
    if not major:
        lines.append("No saved indicator snapshot or rule-state transition; timeline unavailable.")
    event_names = {int(event["event_id"]): _event_name(event, "en") for event in facts.rule_events}
    for item in major:
        if item["kind"] == "indicator_snapshot":
            lines.append(
                f"  {item['time']} snapshot {item['snapshot_id']}: close {_number(item['close'], 'en')}; "
                f"1-minute return {_number(item['return_1m_pct'], 'en')}%; 5-minute return {_number(item['return_5m_pct'], 'en')}%."
            )
        else:
            state = "unknown" if item["is_active"] is None else "active" if item["is_active"] else "resolved"
            lines.append(
                f"  {item['time']} rule check {item['event_check_id']}: event {item['event_id']} "
                f"{event_names.get(item['event_id'], 'unknown event')} {state}; notification={item['notification_kind'] or 'none'}."
            )
    lines.append("4. Enabled indicators and daily result")
    if latest is None:
        lines.append("Latest indicator snapshot: none; related indicators are unknown.")
    else:
        values = latest["values"]
        lines.append(
            f"Latest indicator snapshot {latest['snapshot_id']}: {latest['data_time']}; "
            f"status {latest['status']}; close {_number(values.get('close'), 'en')}; estimated VWAP "
            f"{_number(values.get('vwap_estimate'), 'en')}; volume ratio {_number(values.get('volume_ratio'), 'en')}; "
            f"mode {latest['analysis_mode']}."
        )
        lines.append("Indicator values: " + _json(values))
        lines.append(f"Evidence minute IDs: {_ids(latest['source_bar_ids'])}.")
    for timeframe, momentum in facts.indicators.get("momentum", {}).items():
        label = "Minute" if timeframe == "minute" else "Daily"
        macd = momentum.get("macd")
        lines.append(
            f"{label} MACD/RSI: status {momentum['status']}; parameters {_json(momentum['parameters'])}; "
            f"input range {_json(momentum['input_range'])}; valid samples {momentum['sample_count']}; "
            f"MACD {_json(macd) if macd else 'insufficient or unavailable'}; "
            f"RSI {_number(momentum['rsi'], 'en')}; reason {momentum['reason'] or 'none'}."
        )
    if daily is None:
        lines.append("Daily conclusion: no saved daily analysis; status unknown. Close alone does not confirm the daily bar.")
    else:
        signal = "unknown" if daily["formal_signal"] is None else str(bool(daily["formal_signal"])).lower()
        lines.append(
            f"Daily conclusion: status {daily['status']}; MA20 {_number(daily['ma20'], 'en')}; turn days "
            f"{_number(daily['turn_days'], 'en')}; formal dual condition {signal}; backfill={daily['is_backfill']}; "
            f"reason {daily['unknown_reason'] or 'none'}."
        )
    lines.append(f"5. Rule triggers, resolution, merging, and cooldown ({len(facts.rule_events)} events)")
    if not facts.rule_events:
        lines.append("No saved rule events. This does not prove that prices did not change, and no trigger can be invented.")
    for event in facts.rule_events:
        lines.append(
            f"  Event {event['event_id']} {event['rule_id']} {_event_name(event, 'en')} {event['priority']} "
            f"{event['status']}; first {event['first_triggered_at']}; last {event['last_triggered_at']}; "
            f"resolved {_number(event['resolved_at'], 'en')}; checks {event['check_count']}; alerts {event['reminder_count']}; "
            f"merged repeats {event['merge_count']}; cooldown until {_number(event['cooldown_until'], 'en')}."
        )
        lines.append(
            f"  Condition {event['condition_expression']}; observed {_json(event['observed_values'])}; "
            f"thresholds {_json(event['thresholds'])}; window {_json(event['time_window'])}; "
            f"evidence {_evidence_summary(event, 'en')}."
        )
        lines.append(f"  Original alert: {event['latest_message']}")
    if facts.holdings_context:
        lines.append("Read-only manually confirmed holdings context: " + _json(facts.holdings_context))
    lines.append("6. Bounded interpretations and alternative possibilities")
    if not facts.rule_events:
        lines.append("No rule event is available for interpretation; only source prices and quality records can be reviewed.")
    for event in facts.rule_events:
        lines.append(f"  {_event_name(event, 'en')}: {_interpretation(event, 'en')}")
    lines.append("7. Missing data and unconfirmed matters")
    lines.extend(f"  - {item}" for item in facts.limitations) if facts.limitations else lines.append("  - No additional limitation recorded; this is not a return promise or investment advice.")
    lines.extend([
        "8. Source records and calculation traceability",
        f"Minute records {len(facts.traceability['minute_bar_ids'])}, IDs {_ids(facts.traceability['minute_bar_ids'])}; daily bar IDs {_ids(facts.traceability['daily_bar_ids'])}.",
        f"Snapshot IDs {_ids(facts.traceability['snapshot_ids'])}; analysis run IDs {_ids(facts.traceability['analysis_run_ids'])}.",
        f"Rule event IDs {_ids(facts.traceability['rule_event_ids'])}; rule check IDs {_ids(facts.traceability['event_check_ids'])}; quality record IDs {_ids(facts.traceability['quality_issue_ids'])}.",
        "Analysis input SHA-256: " + _json(facts.traceability["analysis_input_hashes"]),
        "This report only organizes saved facts. It does not recalculate rules or emit live alerts.",
        "No Ollama or cloud call was made. Market data, rules, and holdings were not modified; no trade was executed.",
    ])
    return "\n".join(lines)


def render_report(
    facts: FactPackage, language: str = "zh-CN", display: dict[str, Any] | None = None
) -> str:
    if facts.report_type == "realtime_event_summary":
        return render_realtime_summary(facts, language, display)
    return render_detailed_report(facts, language, display)
