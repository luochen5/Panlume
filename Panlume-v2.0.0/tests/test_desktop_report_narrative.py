from __future__ import annotations

import json

from pandeng.config.schema import OllamaConfig
from pandeng.desktop.reports import DesktopReportService
from pandeng.reporting.builder import build_event_fact_package
from pandeng.reporting.narrative import validate_narrative
from pandeng.reporting.presets import ReportPreset
from pandeng.storage.db import Database
from pandeng.summarizer.ollama import OllamaResponse
from test_desktop_history import populate_history


class FactClient:
    def __init__(self, suffix: str = "") -> None:
        self.suffix = suffix
        self.calls = 0

    def generate(self, prompt: str) -> OllamaResponse:
        self.calls += 1
        facts = json.loads(prompt.split("只读事实包：", 1)[1])
        event = facts["rule_events"][0]
        close = event["observed_values"]["close"]
        stop = event["thresholds"]["stop_loss_price"]
        text = (
            f"{facts['stock_code']} {facts['data_range']['session_date']} "
            f"事件 {event['event_id']} {event['status']}，"
            f"分钟收盘 {close}，人工确认止损价 {stop}。{self.suffix}"
        )
        return OllamaResponse(text, "", "fake", json.dumps({"response": text}))


def config(enabled: bool = True) -> OllamaConfig:
    return OllamaConfig(enabled, "http://127.0.0.1:11434", "default", 1)


def test_report_keeps_program_facts_and_appends_validated_narrative(tmp_path) -> None:
    database = Database(tmp_path / "model.db")
    populate_history(database)
    client = FactClient()
    service = DesktopReportService(
        database,
        ollama_config_provider=lambda: config(),
        client_factory=lambda current: client,
    )
    preset = ReportPreset(model_name="qwen3:4b")

    report = service.generate(preset, "600000", "2026-01-05")

    assert report.model_used is True and report.model_status == "succeeded"
    assert report.validation_result == "passed"
    assert report.model_tier == "tier1" and report.parameter_size_b == 4.0
    assert report.program_facts_text.startswith("盘灯本地每日复盘")
    assert report.narrative_text.startswith("600000 2026-01-05 事件")
    assert report.text.startswith(report.program_facts_text)
    assert "可选模型叙述区" in report.text
    assert client.calls == 1
    exported = report.export_dict()["report"]
    assert exported["program_facts_text"] == report.program_facts_text
    assert exported["narrative_text"] == report.narrative_text


def test_invalid_narrative_falls_back_only_narrative_and_preserves_facts(tmp_path) -> None:
    database = Database(tmp_path / "invalid.db")
    populate_history(database)
    client = FactClient("额外目标价 999。")
    service = DesktopReportService(
        database,
        ollama_config_provider=lambda: config(),
        client_factory=lambda current: client,
    )
    preset = ReportPreset(model_name="qwen3:4b")

    report = service.generate(preset, "600000", "2026-01-05")

    assert report.model_used is False and report.model_status == "fallback"
    assert report.fallback_reason == "validation_failed"
    assert report.validation_result == "failed"
    assert any("事实包外数字" in item for item in report.validation_reasons)
    assert report.narrative_text == ""
    assert report.text == report.program_facts_text
    assert report.package.data_range["saved_minutes"] == 2
    assert report.package.rule_events


def test_disabled_model_never_constructs_client_and_returns_complete_template(tmp_path) -> None:
    database = Database(tmp_path / "disabled.db")
    populate_history(database)
    service = DesktopReportService(
        database,
        ollama_config_provider=lambda: config(False),
        client_factory=lambda current: (_ for _ in ()).throw(AssertionError("不应创建模型客户端")),
    )

    report = service.generate(ReportPreset(model_name="qwen3:4b"), "600000", "2026-01-05")

    assert report.model_used is False and report.fallback_reason == "model_disabled"
    assert report.text == report.program_facts_text
    assert "规则命中、解除、合并与冷却" in report.text



def test_captured_compact_json_with_repeated_close_and_stop_values_is_valid(tmp_path) -> None:
    database = Database(tmp_path / "captured.db")
    populate_history(database)
    with database.connect() as connection:
        check_id = connection.execute(
            "SELECT id FROM rule_event_checks ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    facts = build_event_fact_package(database, check_id)
    captured = (
        "股票与时间：600000 2026-01-05T09:32:00+08:00\n"
        "事件：事件 1 fixed_stop active\n"
        '实际值：{"buy_date":"2026-01-05","close":9.0,"quantity":100}\n'
        '阈值：{"stop_loss_price":9.0}\n'
        "状态与限制：active；分钟序列仍需后续确认。"
    )

    assert validate_narrative(captured, facts, 250) == ()
