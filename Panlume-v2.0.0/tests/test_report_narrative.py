from __future__ import annotations

import json

import pytest

from pandeng.config.schema import OllamaConfig
from pandeng.reporting.comparison import compare_model_outputs
from pandeng.reporting.models import ModelProfile, resolve_model_recommendation
from pandeng.reporting.narrative import (
    NarrativeService, build_narrative_prompt, narrative_limit, validate_narrative,
)
from pandeng.reporting.presets import ReportPreset
from pandeng.summarizer.ollama import OllamaError, OllamaResponse
from test_report_facts import package


def valid_output() -> str:
    return (
        "600000 2026-01-07T09:33:00+08:00 事件 2 rapid_rise active，"
        "一分钟收益 1.25%，阈值 1.0%。仅复述已保存事实。"
    )


def config(enabled: bool = True) -> OllamaConfig:
    return OllamaConfig(enabled, "http://127.0.0.1:11434", "default", 1)


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts = []
        self.configs = []

    def generate(self, prompt: str) -> OllamaResponse:
        self.prompts.append(prompt)
        return OllamaResponse(self.text, "", "fake", json.dumps({"response": self.text}))


def test_narrative_validation_accepts_exact_facts_and_rejects_each_mutation() -> None:
    facts = package()
    assert validate_narrative(valid_output(), facts, 400) == ()

    assert any("事实包外数字" in item for item in validate_narrative(
        valid_output() + "目标价 999。", facts, 400
    ))
    assert any("遗漏事件" in item for item in validate_narrative(
        valid_output().replace("事件 2", "该事件"), facts, 400
    ))
    assert any("原始状态" in item for item in validate_narrative(
        valid_output().replace("active", "cleared"), facts, 400
    ))
    assert any("关键日期" in item for item in validate_narrative(
        valid_output().replace("2026-01-07T09:33:00+08:00", "时间缺失"), facts, 400
    ))
    assert any("原因或预测" in item for item in validate_narrative(
        valid_output() + "因为利好消息预计将会上涨。", facts, 400
    ))
    reversed_time = (
        "600000 末次 2026-01-07T09:33:00+08:00，首次 2026-01-07T09:32:00+08:00，"
        "事件 2 active，一分钟收益 1.25%，阈值 1.0%。"
    )
    assert any("时间顺序" in item for item in validate_narrative(reversed_time, facts, 400))

    compact_json = (
        "600000 2026-01-07T09:33:00+08:00 事件 2 active；"
        '实际值 {"return_1m_pct":1.25}；阈值 {"threshold_pct":1.0}。'
    )
    assert validate_narrative(compact_json, facts, 400) == ()


def test_prompt_uses_read_only_package_tier_and_custom_format_subordination() -> None:
    preset = ReportPreset(
        model_name="qwen3:4b", custom_format_instructions="把事件放在限制之前",
    )
    recommendation = resolve_model_recommendation(preset.model_name)
    prompt = build_narrative_prompt(package(), preset, recommendation, "zh-CN")

    assert "只读事实包：" in prompt
    assert '"fact_package_version":"1.0"' in prompt
    assert "不能覆盖" in prompt or "绝不能覆盖" in prompt
    assert preset.custom_format_instructions in prompt
    assert "不是能力认证" in prompt
    assert "严格只输出以下五行" in prompt
    assert "事件 <事件ID>" in prompt
    assert "实际 close=9" in prompt and "stop_loss_price=9" in prompt
    assert "绝不能改写成卖出指令" in prompt
    assert narrative_limit(package(), preset) == 250


def test_narrative_service_adopts_valid_model_text_and_uses_manual_tier() -> None:
    client = FakeClient(valid_output())
    service = NarrativeService(
        lambda: config(), client_factory=lambda current: client
    )
    preset = ReportPreset(model_name="custom-model", model_tier_override="tier3")

    outcome = service.generate(package(), preset, "zh-CN")

    assert outcome.used_model is True and outcome.status == "succeeded"
    assert outcome.text == valid_output()
    assert outcome.tier == "tier3"
    assert outcome.parameter_size_b is None
    assert "只读事实包：" in client.prompts[0]


def test_narrative_service_falls_back_on_invalid_output_or_disabled_model() -> None:
    invalid = FakeClient(valid_output() + "输入外数字 999。")
    service = NarrativeService(lambda: config(), client_factory=lambda current: invalid)
    preset = ReportPreset(model_name="qwen3:4b")

    failed = service.generate(package(), preset, "zh-CN")
    disabled = NarrativeService(lambda: config(False)).generate(package(), preset, "zh-CN")

    assert failed.used_model is False and failed.status == "fallback"
    assert failed.fallback_reason == "validation_failed"
    assert any("事实包外数字" in item for item in failed.validation_reasons)
    assert disabled.fallback_reason == "model_disabled"
    assert disabled.tier == "tier1"


def test_same_historical_sample_comparison_records_fact_error_categories() -> None:
    facts = package()
    outputs = {
        "good": valid_output(),
        "omission": valid_output().replace("事件 2", "该事件"),
        "number": valid_output() + "额外数值 999。",
        "time": valid_output().replace("2026-01-07T09:33:00+08:00", "时间不明"),
        "cause": valid_output() + "因为利好消息。",
    }
    profiles = {
        name: ModelProfile(name, 4.0, "tier1", generation="test", quantization="Q4")
        for name in outputs
    }

    results = {item.profile.name: item for item in compare_model_outputs(facts, outputs, profiles)}

    assert results["good"].accepted is True
    assert results["omission"].issue_categories == ("omitted_facts",)
    assert "wrong_number" in results["number"].issue_categories
    assert "wrong_time" in results["time"].issue_categories
    assert "unsupported_explanation" in results["cause"].issue_categories
    assert all(item.profile.quantization == "Q4" for item in results.values())



def test_narrative_timeout_preserves_specific_fallback_kind() -> None:
    class TimeoutClient:
        def generate(self, prompt: str) -> OllamaResponse:
            raise OllamaError("timeout", "test timeout")

    service = NarrativeService(lambda: config(), client_factory=lambda current: TimeoutClient())
    outcome = service.generate(package(), ReportPreset(model_name="qwen3:4b"), "zh-CN")
    assert outcome.used_model is False
    assert outcome.fallback_reason == "timeout"
