from __future__ import annotations

from dataclasses import replace

from pandeng.summarizer.content import (
    SummaryFacts,
    build_prompt,
    template_summary,
    validate_summary,
)


def facts() -> SummaryFacts:
    return SummaryFacts(
        event_check_id=1,
        event_id=2,
        snapshot_id=3,
        analysis_run_id=4,
        stock_code="600000",
        data_time="2026-01-05T09:31:00+08:00",
        event_type="fixed_stop",
        priority="high",
        notification_kind="initial",
        message=(
            "600000 2026-01-05T09:31:00+08:00 固定止损触发：批次 1，"
            "分钟收盘 9.0000，人工确认止损价 9.0000。提醒不执行交易。"
        ),
        position_lot_id=1,
        values={"close": 9.0, "vwap_estimate": None},
        payload={"close": 9.0, "stop_loss_price": 9.0},
        unknown_reasons=("成交量比未知。",),
    )


def test_prompt_and_template_use_only_saved_facts_and_upper_limit() -> None:
    item = facts()
    prompt = build_prompt(item, 200)

    assert "最多 200 个字符，不设最低长度" in prompt
    assert "不补充输入外的数字" in prompt
    assert item.message in prompt
    assert template_summary(item, 200) == item.message
    shortened = template_summary(item, 80)
    assert len(shortened) == 80 and shortened.endswith("不执行交易。")


def test_english_prompt_and_template_keep_original_alert_unchanged() -> None:
    item = facts()
    original_message = item.message
    prompt = build_prompt(item, 500, "en")
    text = template_summary(item, 500, "en")

    assert "final English summary" in prompt
    assert original_message in prompt
    assert text.startswith("600000 2026-01-05T09:31:00+08:00 Fixed stop triggered")
    assert "minute close 9.0000" in text and "stop 9.0000" in text
    assert "no trade is executed" in text
    assert item.message == original_message
    assert validate_summary(text, item, 500) == ()


def test_valid_summary_preserves_code_time_and_equivalent_numbers() -> None:
    text = (
        "600000 2026-01-05T09:31:00+08:00 固定止损触及，"
        "分钟收盘 9.00，人工确认止损价 9。仅整理程序事实。"
    )
    assert validate_summary(text, facts(), 200) == ()


def test_output_validation_rejects_extra_numbers_missing_facts_and_instructions() -> None:
    text = (
        "600000 2026-01-05T09:31:00+08:00 因利好消息预计将会上涨，"
        "分钟收盘 9，止损价 9，目标价 12，建议买入。"
    )
    reasons = validate_summary(text, facts(), 200)

    assert any("输入外数字" in reason and "12" in reason for reason in reasons)
    assert any("交易指令" in reason for reason in reasons)
    assert any("原因或预测" in reason for reason in reasons)


def test_output_validation_checks_length_identity_tags_and_required_numbers() -> None:
    reasons = validate_summary(
        "<think>额外推理</think>600000 时间未知，分钟收盘 9。",
        facts(),
        20,
    )

    assert any("超过 20" in reason for reason in reasons)
    assert any("异常标签" in reason for reason in reasons)
    assert any("完整行情时间" in reason for reason in reasons)
    assert any("stop_loss_price" in reason for reason in reasons)
    assert validate_summary("   ", facts(), 200) == ("最终摘要为空。",)


def vwap_facts() -> SummaryFacts:
    raw_vwap = 9.333333333333334
    return replace(
        facts(),
        event_type="below_vwap",
        priority="normal",
        message=(
            "600000 2026-01-05T09:31:00+08:00 分钟收盘低于典型价估算 VWAP："
            f"收盘 9.0000，VWAP 估算 {raw_vwap}。"
        ),
        position_lot_id=None,
        values={"close": 9.0, "vwap_estimate": raw_vwap},
        payload={"close": 9.0, "vwap_estimate": raw_vwap},
        unknown_reasons=(),
    )


def test_vwap_prompt_validation_and_template_share_four_decimal_fact() -> None:
    item = vwap_facts()
    prompt = build_prompt(item, 200)
    text = (
        "600000 2026-01-05T09:31:00+08:00 分钟收盘 9.0000，"
        "VWAP 估算 9.3333。仅整理程序事实。"
    )

    assert item.payload["vwap_estimate"] == "9.3333"
    assert item.values["vwap_estimate"] == "9.3333"
    assert '"vwap_estimate": "9.3333"' in prompt
    assert "9.333333333333334" not in prompt
    assert "9.333333333333334" not in template_summary(item, 200)
    assert "VWAP 估算 9.3333" in template_summary(item, 200)
    assert validate_summary(text, item, 200) == ()


def test_vwap_normalization_does_not_relax_strict_number_validation() -> None:
    item = vwap_facts()
    valid = (
        "600000 2026-01-05T09:31:00+08:00 分钟收盘 9.0000，"
        "VWAP 估算 9.3333。仅整理程序事实。"
    )

    full_precision = validate_summary(
        valid.replace("9.3333", "9.333333333333334"), item, 200
    )
    extra_number = validate_summary(valid + "输入外数字 999。", item, 200)

    assert any("输入外数字" in reason for reason in full_precision)
    assert any("vwap_estimate=9.3333" in reason for reason in full_precision)
    assert any("输入外数字" in reason and "999" in reason for reason in extra_number)
