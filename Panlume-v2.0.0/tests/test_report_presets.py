from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pandeng.reporting.presets import (
    PRESET_FORMAT, ReportPreset, ReportPresetError, ReportPresetStore,
)


def test_missing_store_uses_default_and_roundtrips_strict_json(tmp_path: Path) -> None:
    store = ReportPresetStore(tmp_path / "config" / "report-presets.json")
    assert store.load() == (ReportPreset(),)
    custom = replace(
        ReportPreset(), name="我的预设", detail_level="brief",
        show_timeline=False, report_language="en", model_name="local-model:4b",
        custom_format_instructions="先列事件，再列限制",
    )

    store.save((ReportPreset(), custom))
    loaded = store.load()

    assert loaded[1] == custom
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["format"] == PRESET_FORMAT
    assert set(raw) == {"format", "presets"}
    assert "api_key" not in raw["presets"][1]
    assert not list(store.path.parent.glob("*.tmp"))


def test_upsert_and_restore_default_only_change_display_presets(tmp_path: Path) -> None:
    store = ReportPresetStore(tmp_path / "report-presets.json")
    store.save((ReportPreset(),))
    store.upsert(replace(ReportPreset(), show_indicators=False))
    assert store.load()[0].show_indicators is False

    restored = store.restore_default()

    assert restored[0] == ReportPreset()
    assert store.load()[0].show_indicators is True


def test_preset_rejects_unknown_missing_and_non_boolean_fields(tmp_path: Path) -> None:
    raw = ReportPreset().to_dict()
    raw["api_key"] = "must-not-be-added"
    with pytest.raises(ReportPresetError, match="未知"):
        ReportPreset.from_dict(raw)

    raw = ReportPreset().to_dict()
    del raw["show_timeline"]
    with pytest.raises(ReportPresetError, match="缺失"):
        ReportPreset.from_dict(raw)

    with pytest.raises(ReportPresetError, match="show_timeline"):
        replace(ReportPreset(), show_timeline=1)

    legacy = ReportPreset().to_dict()
    del legacy["model_tier_override"]
    assert ReportPreset.from_dict(legacy).model_tier_override == ""


def test_store_rejects_unknown_root_duplicate_names_and_bad_version(tmp_path: Path) -> None:
    path = tmp_path / "report-presets.json"
    store = ReportPresetStore(path)
    path.write_text(json.dumps({"format": PRESET_FORMAT, "presets": [], "secret": "x"}))
    with pytest.raises(ReportPresetError, match="根节点"):
        store.load()

    path.write_text(json.dumps({"format": "wrong", "presets": [ReportPreset().to_dict()]}))
    with pytest.raises(ReportPresetError, match="版本"):
        store.load()

    with pytest.raises(ReportPresetError, match="不能重复"):
        store.save((ReportPreset(), ReportPreset()))


def test_display_options_cover_all_customizable_sections() -> None:
    preset = replace(
        ReportPreset(), detail_level="detailed", length_preference="full",
        show_data_quality=False, show_indicators=False, show_rule_basis=False,
        show_timeline=False, show_conditional_interpretation=False,
        show_limitations_traceability=False,
    )
    options = preset.display_options()
    assert options == {
        "detail_level": "detailed", "length_preference": "full",
        "show_data_quality": False, "show_indicators": False,
        "show_rule_basis": False, "show_timeline": False,
        "show_conditional_interpretation": False,
        "show_limitations_traceability": False,
    }
