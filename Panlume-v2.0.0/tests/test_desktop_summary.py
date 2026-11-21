from __future__ import annotations

from datetime import datetime

from pandeng.data.base import MinuteBar
from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.settings import DesktopSettings, StockChoice
from pandeng.desktop.summary import ObservationSummaryService
from pandeng.summarizer.ollama import OllamaError, OllamaResponse
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes


def prepared_service(tmp_path, *, enabled=True, client=None, language="zh-CN"):
    database = Database(tmp_path / "summary.db")
    database.initialize()
    ingest_minutes(database, [MinuteBar(
        "000001", datetime(2026, 11, 17, 10, 0, tzinfo=CHINA_TZ),
        10, 11, 9, 10.5, 1000, "offline-test", confirmed=False,
    )], adapter="offline-test")
    settings = DesktopSettings(
        stocks=(StockChoice("000001"),), ollama_enabled=enabled, language=language,
    )
    service = ObservationSummaryService(
        database, lambda: settings, tmp_path / "logs",
        client_factory=lambda config: client,
    )
    return service


def test_disabled_model_uses_template_without_client(tmp_path):
    service = prepared_service(tmp_path, enabled=False)
    result = service.generate()
    assert not result.used_model
    assert "000001" in result.text
    assert "2026-11-17T10:00:00+08:00" in result.text
    assert "未确认" in result.text


def test_english_template_preserves_facts_without_model(tmp_path):
    service = prepared_service(tmp_path, enabled=False, language="en")
    result = service.generate()

    assert not result.used_model
    assert result.note == "Model disabled; fixed template used"
    assert "Observation facts (prepared by local Python)" in result.text
    assert "000001: market time 2026-11-17T10:00:00+08:00" in result.text
    assert "close 10.50; unconfirmed; source offline-test" in result.text
    assert "未确认" not in result.text


def test_model_only_returns_text_and_does_not_change_minutes(tmp_path):
    class Client:
        def generate(self, prompt):
            assert "不得补充" in prompt
            return OllamaResponse("000001：收盘 10.50，未确认。", "", "deepseek-r1:14b", "{}")

    service = prepared_service(tmp_path, client=Client())
    before = service._facts()
    result = service.generate()
    assert result.used_model
    assert result.text == "000001：收盘 10.50，未确认。"
    assert service._facts() == before


def test_model_failure_falls_back_to_complete_python_timestamp(tmp_path):
    class Client:
        def generate(self, prompt):
            raise OllamaError("timeout", "测试超时")

    service = prepared_service(tmp_path, client=Client())
    result = service.generate()
    assert not result.used_model
    assert "OllamaError" in result.note
    assert "2026-11-17T10:00:00+08:00" in result.text
