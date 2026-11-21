from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pandeng.desktop.i18n import translate, translate_observation_message
from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.settings import (
    DesktopSettings,
    DesktopSettingsError,
    DesktopSettingsStore,
    StockChoice,
)


def test_default_settings_are_offline_safe() -> None:
    settings = DesktopSettings()
    assert settings.theme == "light"
    assert settings.interval_seconds == 300
    assert settings.stocks == (StockChoice("002156", "通富微电"),)
    assert settings.model_name == "deepseek-r1:14b"
    assert "key" not in json.dumps(settings.to_dict()).lower()


def test_settings_round_trip_and_atomic_file(tmp_path) -> None:
    store = DesktopSettingsStore(tmp_path / "config" / "desktop.json")
    expected = DesktopSettings(
        theme="dark",
        interval_seconds=60,
        stocks=(StockChoice("000001", "平安银行"), StockChoice("002156", "通富微电")),
        output_style="detailed",
        minute_retention_days=10,
    )
    store.save(expected)
    assert store.load() == expected
    assert not (store.path.parent / "desktop.json.tmp").exists()


@pytest.mark.parametrize("value", [59, 3601, True, "300"])
def test_interval_is_strict(value) -> None:
    with pytest.raises(DesktopSettingsError):
        replace(DesktopSettings(), interval_seconds=value)


def test_stock_limit_and_duplicates() -> None:
    ten = tuple(StockChoice(f"{index:06d}") for index in range(10))
    assert len(replace(DesktopSettings(), stocks=ten).stocks) == 10
    eleven = tuple(StockChoice(f"{index:06d}") for index in range(11))
    with pytest.raises(DesktopSettingsError, match="1～10"):
        replace(DesktopSettings(), stocks=eleven)
    with pytest.raises(DesktopSettingsError, match="重复"):
        replace(DesktopSettings(), stocks=(StockChoice("000001"), StockChoice("000001")))


def test_local_model_name_and_connection_are_validated_without_restricting_to_one_model() -> None:
    custom = replace(
        DesktopSettings(), model_name="qwen3:4b", ollama_base_url="http://127.0.0.1:11435/",
    )
    assert custom.model_name == "qwen3:4b"
    assert custom.ollama_base_url == "http://127.0.0.1:11435"
    with pytest.raises(DesktopSettingsError, match="模型名称格式"):
        replace(DesktopSettings(), model_name="")
    with pytest.raises(DesktopSettingsError, match="连接地址"):
        replace(DesktopSettings(), ollama_base_url="file:///tmp/model")


def test_unknown_json_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "settings.json"
    payload = DesktopSettings().to_dict()
    payload["cloud_api_key"] = "must-not-be-here"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DesktopSettingsError, match="未知键"):
        DesktopSettingsStore(path).load()


def test_stock_json_requires_six_digit_string(tmp_path) -> None:
    path = tmp_path / "settings.json"
    payload = DesktopSettings().to_dict()
    payload["stocks"] = [{"code": 2156, "name": "错误类型"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DesktopSettingsError):
        DesktopSettingsStore(path).load()


def test_public_default_and_legacy_settings_keep_their_language(tmp_path) -> None:
    store = DesktopSettingsStore(tmp_path / "desktop.json")
    assert store.load(default_language="en").language == "en"

    legacy = DesktopSettings().to_dict()
    legacy.pop("language")
    store.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert store.load(default_language="en").language == "zh-CN"


def test_language_is_strict_and_saved_atomically(tmp_path) -> None:
    with pytest.raises(DesktopSettingsError, match="zh-CN、zh-TW、en 或 ja"):
        replace(DesktopSettings(), language="en-US")

    store = DesktopSettingsStore(tmp_path / "desktop.json")
    store.save(DesktopSettings(language="en"))
    assert store.load().language == "en"
    assert not store.path.with_name("desktop.json.tmp").exists()


def test_legacy_stock_json_defaults_enabled_and_preserves_order(tmp_path) -> None:
    path = tmp_path / "settings.json"
    payload = DesktopSettings().to_dict()
    payload["stocks"] = [{"code": "000002", "name": "二"}, {"code": "000001", "name": "一"}]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = DesktopSettingsStore(path).load()
    assert [item.code for item in loaded.stocks] == ["000002", "000001"]
    assert all(item.enabled is True for item in loaded.stocks)


def test_missing_translation_falls_back_to_chinese() -> None:
    assert translate("总览", "en") == "Overview"
    assert translate("一条尚未收录的界面文本", "en") == "一条尚未收录的界面文本"
    assert translate("总览", "zh-CN") == "总览"
    assert translate_observation_message("第 2 轮观察中", "en") == "Round 2 in progress"
    assert translate_observation_message("第 3 轮完成", "en") == "Round 3 completed"


def test_data_dir_override_does_not_use_project_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANDENG_DATA_DIR", str(tmp_path / "isolated"))
    paths = DesktopPaths.default()
    assert paths.root == (tmp_path / "isolated").resolve()
    assert paths.database == paths.root / "data" / "pandeng.db"
    paths.ensure_directories()
    assert paths.settings_file.parent.is_dir()
