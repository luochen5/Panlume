"""桌面事件与摘要历史：只读串联既有规则、摘要和模型审计记录。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from pandeng.storage.db import Database


@dataclass(frozen=True)
class EventHistoryItem:
    event_id: int
    session_date: str
    stock_code: str
    event_type: str
    status: str
    priority: str
    position_lot_id: int | None
    first_seen_at: str
    last_seen_at: str
    last_reminded_at: str | None
    check_count: int
    reminder_count: int
    latest_message: str
    fingerprint: str

    def render_detail(self, language: str = "zh-CN") -> str:
        if language == "en":
            return (
                f"Rule event {self.event_id} | {self.session_date} | {self.stock_code}\n"
                f"Type {self.event_type} | Status {self.status} | Priority {self.priority}\n"
                f"Position lot {self.position_lot_id if self.position_lot_id is not None else 'not linked'}\n"
                f"First seen {self.first_seen_at} | Last seen {self.last_seen_at}\n"
                f"Last reminder {self.last_reminded_at or 'no live reminder yet'}; "
                f"checks {self.check_count}; reminders {self.reminder_count}.\n"
                f"Fingerprint: {self.fingerprint}\n\nLatest deterministic message (original text):\n{self.latest_message}\n\n"
                "This page is read-only. It does not recalculate rules or change event states or reminder frequency."
            )
        return (
            f"规则事件 {self.event_id}｜{self.session_date}｜{self.stock_code}\n"
            f"类型 {self.event_type}｜状态 {self.status}｜优先级 {self.priority}\n"
            f"持仓批次 {self.position_lot_id if self.position_lot_id is not None else '无关联'}\n"
            f"首次 {self.first_seen_at}｜最近 {self.last_seen_at}\n"
            f"最近提醒 {self.last_reminded_at or '尚无实时提醒'}；"
            f"检查 {self.check_count} 次；提醒 {self.reminder_count} 次。\n"
            f"指纹：{self.fingerprint}\n\n最近确定性消息：\n{self.latest_message}\n\n"
            "本页只读历史，不重新计算规则，也不改变事件状态或提醒频率。"
        )

    @property
    def detail(self) -> str:
        return self.render_detail()


@dataclass(frozen=True)
class ModelCallHistoryItem:
    call_id: int
    provider: str
    model_name: str
    status: str
    error_type: str | None
    error_message: str | None
    elapsed_ms: int
    started_at: str
    finished_at: str

    def render_detail(self, language: str = "zh-CN") -> str:
        if language == "en":
            return (
                f"  Call {self.call_id} | {self.provider}/{self.model_name} | {self.status}\n"
                f"  Elapsed {self.elapsed_ms} ms | Started {self.started_at} | Finished {self.finished_at}\n"
                f"  Error {self.error_type or 'none'}: {self.error_message or 'none'}"
            )
        return (
            f"  调用 {self.call_id}｜{self.provider}/{self.model_name}｜{self.status}\n"
            f"  耗时 {self.elapsed_ms} ms｜开始 {self.started_at}｜结束 {self.finished_at}\n"
            f"  错误 {self.error_type or '无'}：{self.error_message or '无'}"
        )

    @property
    def detail(self) -> str:
        return self.render_detail()


@dataclass(frozen=True)
class SummaryHistoryItem:
    task_id: int
    event_id: int
    event_check_id: int
    snapshot_id: int
    analysis_run_id: int
    session_date: str
    stock_code: str
    event_type: str
    data_time: str
    status: str
    summary_kind: str | None
    summary_text: str | None
    validation_reasons_json: str | None
    model_name: str | None
    fallback_reason: str | None
    attempt_count: int
    recovery_count: int
    created_at: str
    updated_at: str
    input_json: str
    payload_json: str
    calls: tuple[ModelCallHistoryItem, ...]

    def render_detail(self, language: str = "zh-CN") -> str:
        if language == "en":
            lines = [
                (
                    f"Summary task {self.task_id} | Status {self.status} | "
                    f"Final result {self.summary_kind or 'not generated'}"
                ),
                (
                    f"Rule event {self.event_id} | Event check {self.event_check_id} | "
                    f"Indicator snapshot {self.snapshot_id} | Analysis run {self.analysis_run_id}"
                ),
                (
                    f"{self.session_date} | {self.stock_code} | {self.event_type} | "
                    f"Data time {self.data_time}"
                ),
                (
                    f"Result model {self.model_name or 'none'} | Attempts {self.attempt_count} | "
                    f"Recoveries {self.recovery_count}"
                ),
                f"Created {self.created_at} | Updated {self.updated_at}",
                f"Fallback reason: {self.fallback_reason or 'none'}",
                f"Validation records: {self.validation_reasons_json or 'no result validation yet'}",
                "",
                "Final saved summary (original text):",
                self.summary_text or "No final summary yet; the waiting or running state is preserved.",
                "",
                f"Input facts JSON:\n{self.input_json}",
                "",
                f"Event-check payload JSON:\n{self.payload_json}",
                "",
                f"Model-call audits: {len(self.calls)}",
            ]
            if self.calls:
                lines.extend(call.render_detail(language) for call in self.calls)
            else:
                lines.append("  No model calls; a fixed template can be used directly when the model is disabled or the queue is full.")
            lines.extend([
                "",
                "Model prompts, raw responses, and R1 thinking are not expanded here. No summaries are generated, tasks resumed, or requests made.",
            ])
            return "\n".join(lines)
        return self.detail

    @property
    def detail(self) -> str:
        lines = [
            (
                f"摘要任务 {self.task_id}｜状态 {self.status}｜"
                f"最终结果 {self.summary_kind or '尚未生成'}"
            ),
            (
                f"规则事件 {self.event_id}｜事件检查 {self.event_check_id}｜"
                f"指标快照 {self.snapshot_id}｜分析运行 {self.analysis_run_id}"
            ),
            (
                f"{self.session_date}｜{self.stock_code}｜{self.event_type}｜"
                f"数据时间 {self.data_time}"
            ),
            (
                f"结果模型 {self.model_name or '无'}｜尝试 {self.attempt_count} 次｜"
                f"恢复 {self.recovery_count} 次"
            ),
            f"创建 {self.created_at}｜更新 {self.updated_at}",
            f"回退原因：{self.fallback_reason or '无'}",
            f"校验记录：{self.validation_reasons_json or '尚无结果校验记录'}",
            "",
            "最终保存摘要：",
            self.summary_text or "尚未生成最终摘要；等待或运行状态原样保留。",
            "",
            f"输入事实 JSON：\n{self.input_json}",
            "",
            f"事件检查 payload JSON：\n{self.payload_json}",
            "",
            f"模型调用审计 {len(self.calls)} 条：",
        ]
        if self.calls:
            lines.extend(call.detail for call in self.calls)
        else:
            lines.append("  无模型调用；模型关闭或队列满等情况可以直接回退固定模板。")
        lines.extend([
            "",
            "本页不展开模型请求、原始响应或 R1 思考文本；不会生成摘要、恢复任务或发起请求。",
        ])
        return "\n".join(lines)


@dataclass(frozen=True)
class DesktopHistoryResult:
    events: tuple[EventHistoryItem, ...]
    summaries: tuple[SummaryHistoryItem, ...]

    def render_summary(self, language: str = "zh-CN") -> str:
        if language != "en":
            return self.summary
        model = sum(item.summary_kind == "model" for item in self.summaries)
        template = sum(item.summary_kind == "template" for item in self.summaries)
        unfinished = sum(item.status in {"pending", "running"} for item in self.summaries)
        return (
            f"Rule events: {len(self.events)}; summary tasks: {len(self.summaries)}; "
            f"model summaries: {model}; template fallbacks: {template}; unfinished: {unfinished}.\n"
            "Only existing SQLite history was read. No recalculation, task recovery, table writes, model, cloud, or live market requests."
        )

    @property
    def summary(self) -> str:
        model = sum(item.summary_kind == "model" for item in self.summaries)
        template = sum(item.summary_kind == "template" for item in self.summaries)
        unfinished = sum(item.status in {"pending", "running"} for item in self.summaries)
        return (
            f"规则事件 {len(self.events)} 条；摘要任务 {len(self.summaries)} 条；"
            f"模型摘要 {model}；模板回退 {template}；未完成 {unfinished}。\n"
            "仅读取 SQLite 已有历史，没有重新计算、任务恢复、写表、模型、云端或真实行情请求。"
        )


class DesktopHistoryService:
    """筛选条件先校验；所有历史连接均启用 SQLite query_only。"""

    RESULT_KINDS = {"all", "model", "template", "unfinished"}

    def __init__(self, database: Database) -> None:
        self.database = database

    @classmethod
    def _filters(
        cls, stock_code: str, session_date_text: str, result_kind: str, limit: int,
    ) -> tuple[str, str, str, int]:
        code = stock_code.strip()
        if code and not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票筛选必须留空或填写 6 位数字代码。")
        day = session_date_text.strip()
        if day:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("日期筛选必须留空或使用 YYYY-MM-DD 格式。")
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise ValueError("日期筛选不是有效日历日期。") from exc
        kind = result_kind.strip()
        if kind not in cls.RESULT_KINDS:
            raise ValueError("摘要结果筛选无效。")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("历史读取数量必须是 1～100 的整数。")
        return code, day, kind, limit

    @staticmethod
    def _where(code: str, day: str, alias: str = "e") -> tuple[str, list[str]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if code:
            clauses.append(f"{alias}.stock_code = ?")
            parameters.append(code)
        if day:
            clauses.append(f"{alias}.session_date = ?")
            parameters.append(day)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def run(
        self,
        stock_code: str = "",
        session_date_text: str = "",
        result_kind: str = "all",
        limit: int = 100,
    ) -> DesktopHistoryResult:
        code, day, kind, limit = self._filters(
            stock_code, session_date_text, result_kind, limit,
        )
        where, parameters = self._where(code, day)
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            event_rows = list(connection.execute(
                f"SELECT * FROM rule_events AS e{where} ORDER BY e.id DESC LIMIT ?",
                (*parameters, limit),
            ))
            task_where = where
            task_parameters = list(parameters)
            if kind == "model":
                task_where += (" AND " if task_where else " WHERE ") + "r.summary_kind = 'model'"
            elif kind == "template":
                task_where += (" AND " if task_where else " WHERE ") + "r.summary_kind = 'template'"
            elif kind == "unfinished":
                task_where += (" AND " if task_where else " WHERE ") + "t.status IN ('pending', 'running')"
            task_parameters.append(str(limit))
            task_rows = list(connection.execute(
                f"""
                SELECT t.id, t.event_check_id, t.status, t.input_json,
                       t.fallback_reason, t.attempt_count, t.recovery_count,
                       t.created_at, t.updated_at,
                       c.event_id, c.snapshot_id, c.analysis_run_id,
                       c.data_time, c.payload_json,
                       e.session_date, e.stock_code, e.event_type,
                       r.summary_kind, r.text AS summary_text,
                       r.validation_reasons_json, r.model_name
                FROM summary_tasks AS t
                JOIN rule_event_checks AS c ON c.id = t.event_check_id
                JOIN rule_events AS e ON e.id = c.event_id
                LEFT JOIN summary_results AS r ON r.task_id = t.id
                {task_where}
                ORDER BY t.id DESC LIMIT ?
                """,
                tuple(task_parameters),
            ))
            call_rows = self._calls(connection, [int(row["id"]) for row in task_rows])
        calls_by_task: dict[int, list[ModelCallHistoryItem]] = {}
        for row in call_rows:
            calls_by_task.setdefault(int(row["task_id"]), []).append(ModelCallHistoryItem(
                int(row["id"]), row["provider"], row["model_name"], row["status"],
                row["error_type"], row["error_message"], int(row["elapsed_ms"]),
                row["started_at"], row["finished_at"],
            ))
        events = tuple(EventHistoryItem(
            int(row["id"]), row["session_date"], row["stock_code"], row["event_type"],
            row["status"], row["priority"], row["position_lot_id"],
            row["first_seen_at"], row["last_seen_at"], row["last_reminded_at"],
            int(row["check_count"]), int(row["reminder_count"]),
            row["latest_message"], row["fingerprint"],
        ) for row in event_rows)
        summaries = tuple(SummaryHistoryItem(
            int(row["id"]), int(row["event_id"]), int(row["event_check_id"]),
            int(row["snapshot_id"]), int(row["analysis_run_id"]),
            row["session_date"], row["stock_code"], row["event_type"], row["data_time"],
            row["status"], row["summary_kind"], row["summary_text"],
            row["validation_reasons_json"], row["model_name"], row["fallback_reason"],
            int(row["attempt_count"]), int(row["recovery_count"]),
            row["created_at"], row["updated_at"], row["input_json"], row["payload_json"],
            tuple(calls_by_task.get(int(row["id"]), ())),
        ) for row in task_rows)
        return DesktopHistoryResult(events, summaries)

    @staticmethod
    def _calls(connection, task_ids: Iterable[int]) -> list:
        ids = tuple(task_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return list(connection.execute(
            f"""
            SELECT id, task_id, provider, model_name, status, error_type,
                   error_message, elapsed_ms, started_at, finished_at
            FROM model_calls WHERE task_id IN ({placeholders}) ORDER BY id
            """,
            ids,
        ))
