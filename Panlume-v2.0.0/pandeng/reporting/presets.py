"""本地报告预设的数据契约与严格 JSON 原子存储。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .models import TIERS


PRESET_FORMAT = "pandeng.report-presets.v1"
REPORT_TYPES = {"realtime_event_summary", "daily_close_detail"}
DETAIL_LEVELS = {"brief", "standard", "detailed"}
LENGTH_PREFERENCES = {"short", "standard", "full"}
REPORT_LANGUAGES = {"startup", "zh-CN", "zh-TW", "en", "ja"}
DISPLAY_FIELDS = (
    "show_data_quality", "show_indicators", "show_rule_basis", "show_timeline",
    "show_conditional_interpretation", "show_limitations_traceability",
)


class ReportPresetError(ValueError):
    pass


@dataclass(frozen=True)
class ReportPreset:
    name: str = "default"
    report_type: str = "daily_close_detail"
    detail_level: str = "standard"
    length_preference: str = "standard"
    show_data_quality: bool = True
    show_indicators: bool = True
    show_rule_basis: bool = True
    show_timeline: bool = True
    show_conditional_interpretation: bool = True
    show_limitations_traceability: bool = True
    report_language: str = "startup"
    model_name: str = ""
    model_tier_override: str = ""
    custom_format_instructions: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name.strip()) <= 40:
            raise ReportPresetError("预设名称必须是 1～40 字的字符串。")
        object.__setattr__(self, "name", self.name.strip())
        if self.report_type not in REPORT_TYPES:
            raise ReportPresetError("报告类型无效。")
        if self.detail_level not in DETAIL_LEVELS:
            raise ReportPresetError("详细程度无效。")
        if self.length_preference not in LENGTH_PREFERENCES:
            raise ReportPresetError("篇幅偏好无效。")
        for name in DISPLAY_FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise ReportPresetError(f"{name} 必须是 true 或 false。")
        if self.report_language not in REPORT_LANGUAGES:
            raise ReportPresetError("报告语言无效。")
        if not isinstance(self.model_name, str) or len(self.model_name.strip()) > 200:
            raise ReportPresetError("模型名称必须是至多 200 字的字符串。")
        object.__setattr__(self, "model_name", self.model_name.strip())
        if self.model_tier_override not in {"", *TIERS}:
            raise ReportPresetError("模型推荐等级覆盖值无效。")
        if not isinstance(self.custom_format_instructions, str) or len(self.custom_format_instructions) > 2000:
            raise ReportPresetError("自定义格式说明必须是至多 2000 字的字符串。")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def display_options(self) -> dict[str, Any]:
        return {
            "detail_level": self.detail_level,
            "length_preference": self.length_preference,
            **{name: getattr(self, name) for name in DISPLAY_FIELDS},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ReportPreset":
        if not isinstance(raw, dict):
            raise ReportPresetError("报告预设必须是 JSON 对象。")
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        optional = {"model_tier_override"}
        missing = sorted(allowed - set(raw) - optional)
        if unknown or missing:
            raise ReportPresetError(f"预设字段不匹配，未知={unknown}，缺失={missing}。")
        values = dict(raw)
        for name in optional:
            if name not in values:
                values[name] = cls.__dataclass_fields__[name].default
        return cls(**values)


class ReportPresetStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[ReportPreset, ...]:
        if not self.path.exists():
            return (ReportPreset(),)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReportPresetError(f"报告预设无法读取：{exc}") from exc
        if not isinstance(raw, dict) or set(raw) != {"format", "presets"}:
            raise ReportPresetError("报告预设文件根节点格式无效。")
        if raw["format"] != PRESET_FORMAT or not isinstance(raw["presets"], list):
            raise ReportPresetError("报告预设文件版本或数组格式无效。")
        if not 1 <= len(raw["presets"]) <= 20:
            raise ReportPresetError("报告预设数量必须为 1～20。")
        presets = tuple(ReportPreset.from_dict(item) for item in raw["presets"])
        names = [item.name for item in presets]
        if len(names) != len(set(names)):
            raise ReportPresetError("报告预设名称不能重复。")
        return presets

    def save(self, presets: tuple[ReportPreset, ...]) -> None:
        if not 1 <= len(presets) <= 20:
            raise ReportPresetError("报告预设数量必须为 1～20。")
        if any(not isinstance(item, ReportPreset) for item in presets):
            raise ReportPresetError("预设列表包含无效对象。")
        names = [item.name for item in presets]
        if len(names) != len(set(names)):
            raise ReportPresetError("报告预设名称不能重复。")
        payload = json.dumps(
            {"format": PRESET_FORMAT, "presets": [item.to_dict() for item in presets]},
            ensure_ascii=False, indent=2, allow_nan=False,
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def upsert(self, preset: ReportPreset) -> tuple[ReportPreset, ...]:
        presets = list(self.load())
        for index, item in enumerate(presets):
            if item.name == preset.name:
                presets[index] = preset
                break
        else:
            presets.append(preset)
        updated = tuple(presets)
        self.save(updated)
        return updated

    def restore_default(self) -> tuple[ReportPreset, ...]:
        presets = tuple(
            item if item.name != "default" else ReportPreset() for item in self.load()
        )
        if all(item.name != "default" for item in presets):
            presets = (ReportPreset(), *presets)
        self.save(presets)
        return presets
