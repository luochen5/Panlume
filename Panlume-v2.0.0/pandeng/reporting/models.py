"""本地模型参数识别与推荐分档；分档不是能力认证。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


TIERS = {"tier1", "tier2", "tier3", "tier3_plus"}
TIER_LABELS = {
    "tier1": "一级",
    "tier2": "二级",
    "tier3": "三级",
    "tier3_plus": "三级增强",
    "unknown": "未知",
    "outside": "超出推荐范围",
}


@dataclass(frozen=True)
class ModelRecommendation:
    parameter_size_b: float | None
    recommended_tier: str
    effective_tier: str
    override_tier: str
    warning: str = ""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    parameter_size_b: float | None
    recommended_tier: str
    generation: str = ""
    quantization: str = ""
    test_notes: str = ""


def _size(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bB]?)\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) and number > 0 else None


def infer_parameter_size_b(model_name: str, metadata: dict[str, Any] | None = None) -> float | None:
    if metadata:
        details = metadata.get("details")
        candidates = [
            metadata.get("parameter_size_b"), metadata.get("parameter_size"),
            metadata.get("parameters"),
        ]
        if isinstance(details, dict):
            candidates.extend((details.get("parameter_size_b"), details.get("parameter_size")))
        for candidate in candidates:
            if (number := _size(candidate)) is not None:
                return number
    if not isinstance(model_name, str):
        return None
    matches = re.findall(
        r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z0-9])",
        model_name,
    )
    if len(matches) != 1:
        return None
    return _size(matches[0])


def recommended_tier(parameter_size_b: float | None) -> str:
    if parameter_size_b is None:
        return "unknown"
    if parameter_size_b < 4 or parameter_size_b > 32:
        return "outside"
    if parameter_size_b == 4:
        return "tier1"
    if parameter_size_b <= 8:
        return "tier2"
    if parameter_size_b <= 14:
        return "tier3"
    return "tier3_plus"


def resolve_model_recommendation(
    model_name: str,
    metadata: dict[str, Any] | None = None,
    override_tier: str = "",
) -> ModelRecommendation:
    if override_tier not in {"", *TIERS}:
        raise ValueError("模型推荐等级覆盖值无效。")
    size = infer_parameter_size_b(model_name, metadata)
    recommended = recommended_tier(size)
    warning = ""
    if recommended == "unknown":
        warning = "parameter_size_unknown"
    elif recommended == "outside":
        warning = "outside_recommended_range"
    if override_tier and recommended in {"unknown", "outside"}:
        warning = "manual_override_unverified"
    return ModelRecommendation(
        parameter_size_b=size,
        recommended_tier=recommended,
        effective_tier=override_tier or recommended,
        override_tier=override_tier,
        warning=warning,
    )
