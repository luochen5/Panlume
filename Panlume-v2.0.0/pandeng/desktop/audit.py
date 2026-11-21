"""桌面只读数据体检：调用核心审计并读取质量、入库批次证据。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable

from pandeng.data.audit import AuditReport, audit_market_data
from pandeng.data.schedule import CHINA_TZ, is_session_minute
from pandeng.storage.db import Database


@dataclass(frozen=True)
class AuditIssueItem:
    issue_id: int
    run_id: int
    data_kind: str
    stock_code: str
    data_time: str
    issue_type: str
    severity: str
    message: str
    raw_json: str

    def render_detail(self, language: str = "zh-CN") -> str:
        if language == "en":
            return (
                f"Quality issue {self.issue_id} | Ingestion run {self.run_id} | {self.severity}\n"
                f"Type {self.data_kind}/{self.issue_type} | Code {self.stock_code}\n"
                f"Data time {self.data_time}\n{self.message}\n\nOriginal record JSON:\n{self.raw_json}"
            )
        return (
            f"质量问题 {self.issue_id}｜入库批次 {self.run_id}｜{self.severity}\n"
            f"类型 {self.data_kind}/{self.issue_type}｜代码 {self.stock_code}\n"
            f"数据时间 {self.data_time}\n{self.message}\n\n原始记录 JSON：\n{self.raw_json}"
        )

    @property
    def detail(self) -> str:
        return self.render_detail()


@dataclass(frozen=True)
class AuditRunItem:
    run_id: int
    data_kind: str
    adapter: str
    status: str
    received_count: int
    accepted_count: int
    duplicate_count: int
    rejected_count: int
    created_at: str

    def render_detail(self, language: str = "zh-CN") -> str:
        if language == "en":
            return (
                f"Ingestion run {self.run_id} | {self.data_kind} | {self.status}\n"
                f"Adapter {self.adapter} | Created {self.created_at}\n"
                f"Received {self.received_count}; accepted {self.accepted_count}; "
                f"duplicates {self.duplicate_count}; rejected {self.rejected_count}.\n"
                "Minute, daily, calendar, and quality records link to this run through ingestion_run_id or run_id."
            )
        return (
            f"入库批次 {self.run_id}｜{self.data_kind}｜{self.status}\n"
            f"适配器 {self.adapter}｜创建时间 {self.created_at}\n"
            f"收到 {self.received_count}；接受 {self.accepted_count}；"
            f"去重 {self.duplicate_count}；拒绝 {self.rejected_count}。\n"
            "分钟、日线、日历和质量记录通过 ingestion_run_id 或 run_id 关联到该批次。"
        )

    @property
    def detail(self) -> str:
        return self.render_detail()


@dataclass(frozen=True)
class DesktopAuditResult:
    report: AuditReport
    issues: tuple[AuditIssueItem, ...]
    runs: tuple[AuditRunItem, ...]

    def render(self, language: str = "zh-CN") -> str:
        if language == "en":
            return self._render_english()
        return self.summary

    def _render_english(self) -> str:
        report = self.report
        calendar_text = {"open": "confirmed open", "closed": "confirmed closed", "unknown": "unknown"}
        status_text = {"ok": "complete", "price_only": "price line only", "unknown": "unknown"}
        daily_text = {
            "confirmed": "confirmed", "unconfirmed": "unconfirmed",
            "missing": "missing", "unknown": "unknown",
        }
        lines = [
            f"Read-only data health check: {report.session_date.isoformat()}; as of {report.as_of_time.isoformat()}.",
            (
                f"Calendar {calendar_text[report.calendar_status]}; source {report.calendar_source or 'none'}; "
                f"overall {report.status}; complete {report.complete_count}; "
                f"price-line only {report.price_only_count}; unknown {report.unknown_count}."
            ),
        ]
        for item in report.codes:
            target_state = (
                "missing" if item.target_confirmed is None
                else "confirmed" if item.target_confirmed else "unconfirmed"
            )
            lines.extend([
                (
                    f"{item.stock_code}: {status_text[item.status]}; minutes {item.saved_minutes}/{item.expected_minutes}; "
                    f"confirmed {item.confirmed_minutes}; unconfirmed {item.unconfirmed_minutes}; "
                    f"gaps {len(item.missing_times)}; quality errors {item.quality_error_count}."
                ),
                (
                    f"  Target minute {target_state}; price line {'available' if item.price_line_available else 'unknown'}; "
                    f"latest minute {item.latest_minute_time or 'none'}; sources {', '.join(item.sources) or 'none'}."
                ),
                (
                    f"  Consecutive checkpoint {item.checkpoint_time or 'none'}; schedule {item.checkpoint_status or 'none'}; "
                    f"daily bar {daily_text[item.daily_status]}; source {item.daily_source or 'none'}."
                ),
            ])
            if item.missing_times:
                preview = ", ".join(item.missing_times[:3])
                lines.append(f"  Gap examples: {preview}{'…' if len(item.missing_times) > 3 else ''}")
            lines.extend(f"  Note: {reason}" for reason in item.reasons)
        lines.extend([
            "Daily bars are reported separately; passing the minute check does not confirm daily bars.",
            "This report reviews the current database and does not prove that data arrived in real time.",
            "This run only read SQLite. No backfilling, repairs, model, cloud, or live market requests were made.",
        ])
        return "\n".join(lines)

    @property
    def summary(self) -> str:
        report = self.report
        calendar_text = {"open": "明确开市", "closed": "明确休市", "unknown": "状态未知"}
        status_text = {"ok": "完整", "price_only": "仅价格线可用", "unknown": "状态未知"}
        daily_text = {
            "confirmed": "已确认", "unconfirmed": "未确认",
            "missing": "缺失", "unknown": "状态未知",
        }
        lines = [
            f"只读数据体检：{report.session_date.isoformat()}；截至 {report.as_of_time.isoformat()}。",
            (
                f"日历 {calendar_text[report.calendar_status]}；来源 {report.calendar_source or '无'}；"
                f"总体 {report.status}；完整 {report.complete_count}；"
                f"仅价格线 {report.price_only_count}；未知 {report.unknown_count}。"
            ),
        ]
        for item in report.codes:
            target_state = (
                "缺失" if item.target_confirmed is None
                else "已确认" if item.target_confirmed else "未确认"
            )
            lines.extend([
                (
                    f"{item.stock_code}：{status_text[item.status]}；分钟 {item.saved_minutes}/{item.expected_minutes}；"
                    f"已确认 {item.confirmed_minutes}；未确认 {item.unconfirmed_minutes}；"
                    f"缺口 {len(item.missing_times)}；质量错误 {item.quality_error_count}。"
                ),
                (
                    f"  目标分钟 {target_state}；价格线 {'可用' if item.price_line_available else '未知'}；"
                    f"最新分钟 {item.latest_minute_time or '无'}；来源 {', '.join(item.sources) or '无'}。"
                ),
                (
                    f"  连续检查点 {item.checkpoint_time or '无'}；调度 {item.checkpoint_status or '无'}；"
                    f"日 K {daily_text[item.daily_status]}；来源 {item.daily_source or '无'}。"
                ),
            ])
            if item.missing_times:
                preview = "、".join(item.missing_times[:3])
                lines.append(f"  缺口示例：{preview}{'……' if len(item.missing_times) > 3 else ''}")
            lines.extend(f"  说明：{reason}" for reason in item.reasons)
        lines.extend([
            "日 K 单独报告；分钟体检通过不会把日 K 自动视为已确认。",
            "本报告按当前数据库回看，不证明数据在当时已经实时到达。",
            "本次只读 SQLite，没有补数、修复、模型调用、云端或真实行情请求。",
        ])
        return "\n".join(lines)


class DesktopAuditService:
    def __init__(
        self,
        database: Database,
        *,
        auditor: Callable[..., AuditReport] = audit_market_data,
    ) -> None:
        self.database = database
        self.auditor = auditor

    @staticmethod
    def _arguments(
        stock_codes: Iterable[str], session_date_text: str, as_of_time_text: str,
    ) -> tuple[tuple[str, ...], date, datetime]:
        codes = tuple(dict.fromkeys(code.strip() for code in stock_codes))
        if not codes or any(not re.fullmatch(r"\d{6}", code) for code in codes):
            raise ValueError("体检对象至少包含一只股票，且每只股票代码必须是 6 位数字。")
        date_text = session_date_text.strip()
        time_text = as_of_time_text.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            raise ValueError("体检日期必须使用 YYYY-MM-DD 格式。")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text):
            raise ValueError("截止分钟必须使用 HH:MM 格式。")
        try:
            session_date = date.fromisoformat(date_text)
            hour, minute = (int(part) for part in time_text.split(":"))
            as_of_time = datetime(
                session_date.year, session_date.month, session_date.day,
                hour, minute, tzinfo=CHINA_TZ,
            )
        except ValueError as exc:
            raise ValueError("体检日期或截止分钟无效。") from exc
        if not is_session_minute(as_of_time):
            raise ValueError(
                "截止分钟必须是 09:31～11:30 或 13:01～15:00 的整分钟。"
            )
        return codes, session_date, as_of_time

    def run(
        self, stock_codes: Iterable[str], session_date_text: str, as_of_time_text: str,
    ) -> DesktopAuditResult:
        codes, session_date, as_of_time = self._arguments(
            stock_codes, session_date_text, as_of_time_text,
        )
        report = self.auditor(self.database, codes, session_date, as_of_time)
        issues, runs = self._evidence(codes, session_date)
        return DesktopAuditResult(report, issues, runs)

    def _evidence(
        self, codes: tuple[str, ...], session_date: date,
    ) -> tuple[tuple[AuditIssueItem, ...], tuple[AuditRunItem, ...]]:
        placeholders = ",".join("?" for _ in codes)
        day = session_date.isoformat()
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            issue_rows = list(connection.execute(
                f"""
                SELECT * FROM data_quality_issues
                WHERE (stock_code IN ({placeholders}) OR stock_code IS NULL)
                  AND substr(data_time, 1, 10) = ?
                ORDER BY id DESC LIMIT 100
                """,
                (*codes, day),
            ))
            run_ids = {int(row["run_id"]) for row in issue_rows}
            for table, time_column in (
                ("minute_bars", "bar_time"), ("daily_qfq_bars", "trade_date"),
            ):
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT ingestion_run_id FROM {table}
                    WHERE stock_code IN ({placeholders})
                      AND substr({time_column}, 1, 10) = ?
                    """,
                    (*codes, day),
                )
                run_ids.update(int(row["ingestion_run_id"]) for row in rows)
            calendar = connection.execute(
                "SELECT ingestion_run_id FROM trading_calendar WHERE trade_date = ?", (day,),
            ).fetchone()
            if calendar is not None:
                run_ids.add(int(calendar["ingestion_run_id"]))
            run_rows = []
            if run_ids:
                run_placeholders = ",".join("?" for _ in run_ids)
                run_rows = list(connection.execute(
                    f"""
                    SELECT * FROM ingestion_runs WHERE id IN ({run_placeholders})
                    ORDER BY id DESC LIMIT 100
                    """,
                    tuple(sorted(run_ids)),
                ))
        issues = tuple(
            AuditIssueItem(
                int(row["id"]), int(row["run_id"]), row["data_kind"],
                row["stock_code"] or "全局", row["data_time"], row["issue_type"],
                row["severity"], row["message"], row["raw_json"],
            )
            for row in issue_rows
        )
        runs = tuple(
            AuditRunItem(
                int(row["id"]), row["data_kind"], row["adapter"], row["status"],
                int(row["received_count"]), int(row["accepted_count"]),
                int(row["duplicate_count"]), int(row["rejected_count"]), row["created_at"],
            )
            for row in run_rows
        )
        return issues, runs
