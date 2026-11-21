"""发布候选离线演示：只写独立 analysis-test.db，不调用网络或模型。"""

from __future__ import annotations

import io
import threading
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Callable

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.config.loader import load_config
from pandeng.config.schema import AppConfig, ConfigError, StorageConfig
from pandeng.desktop.audit import DesktopAuditService
from pandeng.desktop.history import DesktopHistoryService
from pandeng.desktop.paths import resource_path
from pandeng.desktop.review import DesktopReviewService
from pandeng.storage.db import Database
from pandeng.summarizer.content import load_summary_facts
from pandeng.summarizer.service import SummaryStore


DEMO_DAY = date(2026, 1, 7)


@dataclass(frozen=True)
class DesktopDemoResult:
    reused: bool
    output: str
    database_path: Path


class DesktopDemoService:
    def __init__(
        self,
        database_path: Path,
        *,
        language_provider: Callable[[], str] = lambda: "zh-CN",
    ) -> None:
        self.database_path = database_path
        self.language_provider = language_provider
        self._lock = threading.Lock()

    def run(self) -> DesktopDemoResult:
        if not self._lock.acquire(blocking=False):
            raise ConfigError("离线演示正在运行，请勿重复启动。")
        try:
            config = load_config(resource_path("configs/analysis-test.yaml"))
            config = replace(config, storage=StorageConfig(self.database_path))
            database = Database(self.database_path)
            if database.is_initialized():
                return self._reuse(database, config)
            return self._create(database, config)
        finally:
            self._lock.release()

    def _create(self, database: Database, config: AppConfig) -> DesktopDemoResult:
        database.initialize()
        language = self.language_provider()
        notices = []
        stream = io.StringIO()
        with redirect_stdout(stream):
            outcome = run_analysis_demo(config, DEMO_DAY, on_notice=notices.append)

        store = SummaryStore(database)
        for notice in notices:
            facts = load_summary_facts(database, notice)
            task, created = store.create_task(facts, config.summary.max_chars, language)
            if not created or not store.finish_fallback(int(task["id"]), "model_disabled"):
                raise ConfigError("离线演示的固定模板摘要未能保存；不会自动重试或覆盖演示库。")

        codes = tuple(item.code for item in config.watchlist)
        review_service = DesktopReviewService(
            database, language_provider=lambda: language
        )
        for code in codes:
            review_service.generate(code, DEMO_DAY.isoformat())

        if language == "en":
            summary = (
                f"Offline demo complete: scheduler rounds {outcome.rounds}; analysis runs "
                f"{outcome.analysis_runs}; indicator snapshots {outcome.snapshots}; "
                f"rule events {outcome.notices}.\n"
                "Simulated data only. No live market, Ollama, or cloud requests.\n"
                f"Independent database: {self.database_path}\n\n"
                + self._integration_report(database, codes, language)
            )
            output = summary
        else:
            summary = (
                f"离线演示完成：调度轮次 {outcome.rounds}；分析运行 {outcome.analysis_runs}；"
                f"指标快照 {outcome.snapshots}；规则事件 {outcome.notices}。\n"
                "全部使用模拟数据，未请求真实行情，未调用 Ollama 或云端。\n"
                f"独立数据库：{self.database_path}\n\n"
                + self._integration_report(database, codes)
            )
            output = stream.getvalue() + "\n" + summary
        return DesktopDemoResult(
            reused=False,
            output=output,
            database_path=self.database_path,
        )

    def _reuse(self, database: Database, config: AppConfig) -> DesktopDemoResult:
        counts = self._counts(database)
        if not counts["rounds"] and not counts["lots"]:
            raise ConfigError(
                "独立演示库已初始化但没有完整演示记录；程序未自动修复、补写或覆盖。"
                "请使用新的 PANDENG_DATA_DIR 隔离目录。"
            )
        codes = tuple(item.code for item in config.watchlist)
        integrated = (
            counts["live_notices"] > 0
            and counts["summary_tasks"] == counts["live_notices"]
            and counts["template_results"] == counts["summary_tasks"]
            and counts["model_calls"] == 0
            and counts["reviews"] >= len(codes)
        )
        if integrated:
            heading = "检测到已完成的发布候选独立演示，本次只读取结果，没有覆盖或追加。\n"
        else:
            heading = (
                "检测到旧版或不完整独立演示，本次只读取结果，没有自动补写、修复、覆盖或追加。\n"
                "如需完整发布候选演示，请使用新的 PANDENG_DATA_DIR 隔离目录。\n"
            )
        if self.language_provider() == "en":
            heading = (
                "A completed independent demo was found. Existing results were read without appending or overwriting.\n"
                if integrated else
                "An old or incomplete demo was found. It was read without repair, backfill, overwrite, or append.\n"
                "Use a new PANDENG_DATA_DIR for a complete demo.\n"
            )
            output = (
                heading
                + f"Scheduler rounds {counts['rounds']}; indicator snapshots {counts['snapshots']}; "
                + f"rule events {counts['events']}; simulated position lots {counts['lots']}.\n"
                + "All market and holdings records are simulated. No Ollama, cloud, or live-market calls.\n\n"
                + self._integration_report(database, codes, "en")
                + f"\nIndependent database: {self.database_path}"
            )
        else:
            output = (
                heading
                + (
                    f"调度轮次 {counts['rounds']}；指标快照 {counts['snapshots']}；"
                    f"规则事件 {counts['events']}；模拟持仓批次 {counts['lots']}。\n"
                    "所有行情和持仓均为模拟记录；未调用 Ollama、云端或真实行情。\n\n"
                )
                + self._integration_report(database, codes)
                + f"\n独立数据库：{self.database_path}"
            )
        return DesktopDemoResult(True, output, self.database_path)

    @staticmethod
    def _counts(database: Database) -> dict[str, int]:
        queries = {
            "rounds": "SELECT COUNT(*) FROM monitor_rounds",
            "snapshots": "SELECT COUNT(*) FROM indicator_snapshots",
            "events": "SELECT COUNT(*) FROM rule_events",
            "lots": "SELECT COUNT(*) FROM position_lots",
            "summary_tasks": "SELECT COUNT(*) FROM summary_tasks",
            "template_results": "SELECT COUNT(*) FROM summary_results WHERE summary_kind = 'template'",
            "model_calls": "SELECT COUNT(*) FROM model_calls",
            "reviews": "SELECT COUNT(*) FROM review_runs",
            "live_notices": (
                "SELECT COUNT(*) FROM rule_event_checks AS c "
                "JOIN analysis_runs AS a ON a.id = c.analysis_run_id "
                "WHERE c.notified = 1 AND a.analysis_mode = 'live'"
            ),
        }
        with database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            return {
                key: int(connection.execute(sql).fetchone()[0])
                for key, sql in queries.items()
            }

    @staticmethod
    def _integration_report(
        database: Database, codes: tuple[str, ...], language: str = "zh-CN"
    ) -> str:
        day = DEMO_DAY.isoformat()
        review_service = DesktopReviewService(database)
        reviews = [
            item for item in review_service.history()
            if item.session_date == day and item.stock_code in codes
        ]
        audit = DesktopAuditService(database).run(codes, day, "15:00")
        history = DesktopHistoryService(database).run(session_date_text=day)
        counts = DesktopDemoService._counts(database)
        review_text = "；".join(
            f"#{item.review_id} {item.stock_code} {item.data_status}" for item in reviews
        ) or "无（旧版或不完整演示保持原状）"
        code_status = "；".join(
            f"{item.stock_code} {item.status}，分钟 {item.saved_minutes}/{item.expected_minutes}"
            for item in audit.report.codes
        )
        if language == "en":
            review_text_en = "; ".join(
                f"#{item.review_id} {item.stock_code} {item.data_status}" for item in reviews
            ) or "none (old or incomplete demo left unchanged)"
            code_status_en = "; ".join(
                f"{item.stock_code} {item.status}, minutes {item.saved_minutes}/{item.expected_minutes}"
                for item in audit.report.codes
            )
            return (
                "Offline release checks\n"
                "1. Observation and recovery: simulated scheduling, outage recovery, indicators, and rule events saved.\n"
                f"2. Fixed-template summaries: {counts['summary_tasks']} tasks; "
                f"{counts['template_results']} template results; {counts['model_calls']} model calls.\n"
                f"3. Local reviews: {review_text_en}.\n"
                f"4. Data health: overall {audit.report.status}; {code_status_en}; "
                f"quality details {len(audit.issues)}.\n"
                f"5. Events and summaries: {len(history.events)} rule events; "
                f"{len(history.summaries)} summary tasks; "
                f"{sum(item.summary_kind == 'model' for item in history.summaries)} model summaries; "
                f"{sum(item.summary_kind == 'template' for item in history.summaries)} template fallbacks; "
                f"{sum(item.status in {'pending', 'running'} for item in history.summaries)} unfinished.\n"
                "Reviews, health checks, and history trace to the same independent demo database. "
                "The personal database was not used."
            )
        return (
            "发布候选离线检查\n"
            "1. 观察与恢复：模拟调度、断网补记、指标和规则事件已由核心演示保存。\n"
            f"2. 固定模板摘要：任务 {counts['summary_tasks']} 条；"
            f"模板结果 {counts['template_results']} 条；模型调用 {counts['model_calls']} 次。\n"
            f"3. 本地复盘：{review_text}。\n"
            f"4. 数据体检：总体 {audit.report.status}；{code_status}；"
            f"质量明细 {len(audit.issues)} 条。\n"
            f"5. 事件与摘要：规则事件 {len(history.events)} 条；"
            f"摘要任务 {len(history.summaries)} 条；"
            f"模型摘要 {sum(item.summary_kind == 'model' for item in history.summaries)} 条；"
            f"模板回退 {sum(item.summary_kind == 'template' for item in history.summaries)} 条；"
            f"未完成 {sum(item.status in {'pending', 'running'} for item in history.summaries)} 条。\n"
            "复盘、体检和历史均追溯到同一独立演示库；个人数据库未参与。"
        )
