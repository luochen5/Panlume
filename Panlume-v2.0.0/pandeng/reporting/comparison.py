"""同一历史事实样本的模型输出对照记录，不以参数规模判定质量。"""

from __future__ import annotations

from dataclasses import dataclass

from .facts import FactPackage
from .models import ModelProfile
from .narrative import validate_narrative


@dataclass(frozen=True)
class ModelSampleResult:
    profile: ModelProfile
    accepted: bool
    issue_categories: tuple[str, ...]
    validation_reasons: tuple[str, ...]


def _categories(reasons: tuple[str, ...]) -> tuple[str, ...]:
    categories = []
    for reason in reasons:
        if "遗漏事件" in reason or "缺少规则关键数值" in reason:
            categories.append("omitted_facts")
        if "事实包外数字" in reason:
            categories.append("wrong_number")
        if "关键日期" in reason or "完整事件时间" in reason or "时间顺序" in reason:
            categories.append("wrong_time")
        if "未经输入支持的原因或预测" in reason:
            categories.append("unsupported_explanation")
        if "原始状态" in reason:
            categories.append("wrong_event_status")
    return tuple(dict.fromkeys(categories))


def compare_model_outputs(
    package: FactPackage,
    outputs: dict[str, str],
    profiles: dict[str, ModelProfile],
    *,
    max_chars: int = 1000,
    language: str = "zh-CN",
) -> tuple[ModelSampleResult, ...]:
    results = []
    for model_name in sorted(outputs):
        if model_name not in profiles:
            raise ValueError(f"缺少模型 {model_name} 的独立代际/量化档案。")
        reasons = validate_narrative(outputs[model_name], package, max_chars, language)
        results.append(ModelSampleResult(
            profile=profiles[model_name],
            accepted=not reasons,
            issue_categories=_categories(reasons),
            validation_reasons=reasons,
        ))
    return tuple(results)
