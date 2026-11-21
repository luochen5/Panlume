"""桌面报告预览、固定模板生成、导出和原始证据读取。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pandeng.reporting.builder import build_daily_fact_package, build_event_fact_package
from pandeng.reporting.facts import FactPackage
from pandeng.config.schema import MomentumConfig, OllamaConfig
from pandeng.reporting.i18n import localize_report_text
from pandeng.reporting.narrative import NarrativeService
from pandeng.reporting.presets import ReportPreset
from pandeng.reporting.templates import render_report
from pandeng.summarizer.ollama import OllamaClient
from pandeng.storage.db import Database


@dataclass(frozen=True)
class ReportEventChoice:
    event_check_id: int
    event_id: int
    rule_id: str
    data_time: str
    status: str
    notified: bool


@dataclass(frozen=True)
class ReportPreview:
    package: FactPackage
    summary: str
    language: str


@dataclass(frozen=True)
class GeneratedReport:
    package: FactPackage
    preset: ReportPreset
    text: str
    language: str
    model_used: bool
    model_status: str
    fallback_reason: str
    validation_result: str
    program_facts_text: str = ""
    narrative_text: str = ""
    model_tier: str = "unknown"
    parameter_size_b: float | None = None
    model_warning: str = ""
    validation_reasons: tuple[str, ...] = ()

    def export_dict(self) -> dict[str, Any]:
        return {
            "format": "pandeng.report-export.v1",
            "fact_package": self.package.to_dict(),
            "fact_content_sha256": self.package.content_hash(),
            "report": {
                "text": self.text,
                "language": self.language,
                "model_used": self.model_used,
                "model_status": self.model_status,
                "fallback_reason": self.fallback_reason,
                "validation_result": self.validation_result,
                "validation_reasons": list(self.validation_reasons),
                "program_facts_text": self.program_facts_text,
                "narrative_text": self.narrative_text,
                "model_tier": self.model_tier,
                "parameter_size_b": self.parameter_size_b,
                "model_warning": self.model_warning,
            },
            "display_preset": self.preset.to_dict(),
        }


class DesktopReportService:
    def __init__(
        self,
        database: Database,
        *,
        language_provider: Callable[[], str] = lambda: "zh-CN",
        ollama_config_provider: Callable[[], OllamaConfig | None] = lambda: None,
        client_factory: Callable[[OllamaConfig], Any] = OllamaClient,
        momentum: MomentumConfig | None = None,
    ) -> None:
        self.database = database
        self.language_provider = language_provider
        self.momentum = momentum or MomentumConfig()
        self.narrative = NarrativeService(
            ollama_config_provider, client_factory=client_factory
        )

    @staticmethod
    def _arguments(stock_code: str, session_date_text: str) -> tuple[str, date]:
        code = stock_code.strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码必须是 6 位数字。")
        text = session_date_text.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("报告日期必须使用 YYYY-MM-DD 格式。")
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("报告日期不是有效日历日期。") from exc
        return code, day

    def event_choices(self, stock_code: str, session_date_text: str) -> list[ReportEventChoice]:
        code, day = self._arguments(stock_code, session_date_text)
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """
                SELECT c.id, c.event_id, c.data_time, c.notified,
                       e.event_type, e.status
                FROM rule_event_checks AS c
                JOIN rule_events AS e ON e.id = c.event_id
                WHERE e.stock_code = ? AND e.session_date = ?
                ORDER BY c.data_time DESC, c.id DESC LIMIT 200
                """,
                (code, day.isoformat()),
            ).fetchall()
        return [
            ReportEventChoice(
                int(row["id"]), int(row["event_id"]), row["event_type"],
                row["data_time"], row["status"], bool(row["notified"]),
            )
            for row in rows
        ]

    def preview(
        self,
        preset: ReportPreset,
        stock_code: str,
        session_date_text: str,
        event_check_id: int | None = None,
    ) -> ReportPreview:
        code, day = self._arguments(stock_code, session_date_text)
        if preset.report_type == "realtime_event_summary":
            if isinstance(event_check_id, bool) or not isinstance(event_check_id, int) or event_check_id < 1:
                raise ValueError("实时事件摘要必须选择有效的事件检查。")
            package = build_event_fact_package(
                self.database, event_check_id, momentum=self.momentum
            )
            if package.stock_code != code or package.data_range["session_date"] != day.isoformat():
                raise ValueError("所选事件与股票或日期不一致，请刷新事件列表。")
        else:
            package = build_daily_fact_package(
                self.database, day, code, momentum=self.momentum
            )
        language = self.language_provider() if preset.report_language == "startup" else preset.report_language
        return ReportPreview(
            package, localize_report_text(self._summary(package, language), language), language
        )

    @staticmethod
    def _summary(package: FactPackage, language: str) -> str:
        if language == "en":
            return (
                f"Fact package {package.fact_package_version} | {package.report_type} | {package.stock_code}\n"
                f"Range {package.data_range['start'] or 'none'} to {package.data_range['end']} ({package.market_timezone})\n"
                f"Data quality {package.data_quality['status']}; minutes {package.confirmed_status['minutes_saved']}; "
                f"snapshots {package.indicators['snapshot_count']}; rule events {len(package.rule_events)}; "
                f"limitations {len(package.limitations)}.\n"
                f"Evidence: {len(package.traceability['minute_bar_ids'])} minute bars, "
                f"{len(package.traceability['event_check_ids'])} rule checks, "
                f"{len(package.traceability['quality_issue_ids'])} quality records.\n"
                f"Content SHA-256: {package.content_hash()}\n"
                "Preview and display settings do not modify the fact package or source records."
            )
        return (
            f"事实包 {package.fact_package_version}｜{package.report_type}｜{package.stock_code}\n"
            f"范围 {package.data_range['start'] or '无'} 至 {package.data_range['end']}（{package.market_timezone}）\n"
            f"数据质量 {package.data_quality['status']}；分钟 {package.confirmed_status['minutes_saved']} 条；"
            f"指标快照 {package.indicators['snapshot_count']} 个；规则事件 {len(package.rule_events)} 条；"
            f"限制 {len(package.limitations)} 项。\n"
            f"证据：分钟 {len(package.traceability['minute_bar_ids'])} 条，"
            f"规则检查 {len(package.traceability['event_check_ids'])} 条，"
            f"质量记录 {len(package.traceability['quality_issue_ids'])} 条。\n"
            f"内容 SHA-256：{package.content_hash()}\n"
            "预览和显示预设不会修改事实包或原始记录。"
        )

    def generate(
        self,
        preset: ReportPreset,
        stock_code: str,
        session_date_text: str,
        event_check_id: int | None = None,
    ) -> GeneratedReport:
        preview = self.preview(preset, stock_code, session_date_text, event_check_id)
        facts_text = render_report(preview.package, preview.language, preset.display_options())
        outcome = self.narrative.generate(preview.package, preset, preview.language)
        text = facts_text
        if outcome.used_model:
            heading = "Optional model narrative" if preview.language == "en" else "可选模型叙述区"
            heading = localize_report_text(heading, preview.language)
            text = f"{facts_text}\n\n{heading}\n{outcome.text}"
        return GeneratedReport(
            package=preview.package,
            preset=preset,
            text=text,
            language=preview.language,
            model_used=outcome.used_model,
            model_status=outcome.status,
            fallback_reason=outcome.fallback_reason,
            validation_result="passed" if outcome.used_model else "failed" if outcome.validation_reasons else "not_applicable",
            program_facts_text=facts_text,
            narrative_text=outcome.text,
            model_tier=outcome.tier,
            parameter_size_b=outcome.parameter_size_b,
            model_warning=outcome.warning,
            validation_reasons=outcome.validation_reasons,
        )

    def raw_evidence(self, report: GeneratedReport) -> str:
        trace = report.package.traceability
        result: dict[str, Any] = {
            "fact_content_sha256": report.package.content_hash(),
            "minute_bars": [],
            "daily_qfq_bars": [],
            "rule_event_checks": [],
            "analysis_runs": [],
            "note": "Only the first five records of each kind are shown; the fact package retains all evidence IDs.",
        }
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            for table, key in (("minute_bars", "minute_bar_ids"), ("daily_qfq_bars", "daily_bar_ids")):
                for record_id in trace[key][:5]:
                    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
                    if row is not None:
                        result[table].append(dict(row))
            for record_id in trace["event_check_ids"][:5]:
                row = connection.execute("SELECT * FROM rule_event_checks WHERE id = ?", (record_id,)).fetchone()
                if row is not None:
                    result["rule_event_checks"].append(dict(row))
            for record_id in trace["analysis_run_ids"][:5]:
                row = connection.execute("SELECT * FROM analysis_runs WHERE id = ?", (record_id,)).fetchone()
                if row is not None:
                    result["analysis_runs"].append(dict(row))
        return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)

    def export(self, report: GeneratedReport, path: Path, format_name: str) -> Path:
        if format_name == "json":
            if path.suffix.lower() != ".json":
                raise ValueError("JSON 导出文件必须使用 .json 后缀。")
            content = json.dumps(
                report.export_dict(), ensure_ascii=False, indent=2,
                sort_keys=True, allow_nan=False,
            ) + "\n"
        elif format_name == "markdown":
            if path.suffix.lower() not in {".md", ".markdown"}:
                raise ValueError("Markdown 导出文件必须使用 .md 或 .markdown 后缀。")
            content = report.text.rstrip() + "\n"
        else:
            raise ValueError("不支持的导出格式。")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
