from __future__ import annotations

import pytest

from pandeng.reporting.models import (
    ModelProfile, infer_parameter_size_b, recommended_tier, resolve_model_recommendation,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("deepseek-r1:14b", 14.0),
        ("qwen3:4b", 4.0),
        ("qwen2.5:7.5b-instruct", 7.5),
        ("custom-model", None),
        ("qwen2.5", None),
        ("model:4b:8b", None),
    ],
)
def test_parameter_size_from_name_only_when_explicit(name, expected) -> None:
    assert infer_parameter_size_b(name) == expected


def test_parameter_size_metadata_precedes_name_and_disk_size_is_not_parameter_count() -> None:
    assert infer_parameter_size_b("custom", {"parameter_size": "8B"}) == 8.0
    assert infer_parameter_size_b("custom", {"details": {"parameter_size": "4.5B"}}) == 4.5
    assert infer_parameter_size_b("custom", {"size": 9_000_000_000}) is None


@pytest.mark.parametrize(
    ("size", "tier"),
    [(None, "unknown"), (3.9, "outside"), (4, "tier1"), (4.1, "tier2"),
     (8, "tier2"), (8.1, "tier3"), (14, "tier3"), (14.1, "tier3_plus"),
     (32, "tier3_plus"), (32.1, "outside")],
)
def test_recommendation_boundaries(size, tier) -> None:
    assert recommended_tier(size) == tier


def test_manual_override_is_separate_from_recommendation_and_unknown_is_not_guessed() -> None:
    unknown = resolve_model_recommendation("custom-model")
    assert unknown.parameter_size_b is None
    assert unknown.recommended_tier == "unknown"
    assert unknown.effective_tier == "unknown"
    assert unknown.warning == "parameter_size_unknown"

    overridden = resolve_model_recommendation("custom-model", override_tier="tier3")
    assert overridden.recommended_tier == "unknown"
    assert overridden.effective_tier == "tier3"
    assert overridden.warning == "manual_override_unverified"
    with pytest.raises(ValueError, match="覆盖"):
        resolve_model_recommendation("qwen3:4b", override_tier="certified")


def test_generation_quantization_and_test_notes_are_separate_profile_fields() -> None:
    profile = ModelProfile(
        "qwen3:4b", 4.0, "tier1", generation="Qwen3",
        quantization="Q4_K_M", test_notes="fixed sample accepted",
    )
    assert profile.parameter_size_b == 4.0
    assert profile.generation == "Qwen3"
    assert profile.quantization == "Q4_K_M"
    assert profile.test_notes == "fixed sample accepted"
