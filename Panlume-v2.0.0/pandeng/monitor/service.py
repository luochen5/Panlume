"""低频调度服务：增量补数、暂停恢复、午休、收盘和安全退出。"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from pandeng.data.base import AdapterRecord, MarketDataAdapter
from pandeng.data.response import (
    ensure_calendar_scope,
    ensure_daily_scope,
    ensure_minute_scope,
)
from pandeng.data.schedule import (
    AFTERNOON_END,
    AFTERNOON_START,
    CHINA_TZ,
    MORNING_END,
    MORNING_START,
    next_session_minute,
    session_minutes,
)
from pandeng.storage.db import Database
from pandeng.storage.monitor import (
    add_monitor_event,
    add_run_log,
    current_daily_confirmation,
    ensure_monitor_state,
    finish_monitor_round,
    get_code_states,
    get_monitor_state,
    missing_minutes,
    save_code_state,
    session_round_count,
    start_monitor_round,
    update_monitor_state,
)
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes

from .clock import Clock, SystemClock
from .schedule import day_boundary, next_poll_time, seconds_until, session_target


class AdapterTimeout(TimeoutError):
    """数据源等待超过本轮允许时间。"""


class AdapterBusy(RuntimeError):
    """上一次只读请求尚未结束，不能叠加新请求。"""


@dataclass(frozen=True)
class CodeRoundResult:
    stock_code: str
    start: datetime | None
    end: datetime
    status: str
    analysis_allowed: bool
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    missing: tuple[datetime, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RoundResult:
    round_id: int | None
    trigger: str
    recovery: bool
    target: datetime | None
    status: str
    analysis_ready_codes: tuple[str, ...]
    code_results: tuple[CodeRoundResult, ...] = ()
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    message: str = ""


@dataclass(frozen=True)
class TickResult:
    action: str
    status: str
    message: str
    round_result: RoundResult | None = None
    daily_status: str | None = None


class MonitorService:
    """只读调度器；不计算指标，不修改持仓，也不调用任何模型。"""

    def __init__(
        self,
        database: Database,
        adapter: MarketDataAdapter,
        stock_codes: Iterable[str],
        session_date: date,
        *,
        clock: Clock | None = None,
        interval_seconds: int = 300,
        fetch_timeout_seconds: float = 20,
        close_timeout_seconds: float = 45,
        on_analysis_ready: Callable[[RoundResult], None] | None = None,
        on_daily_confirmed: Callable[[date, bool], None] | None = None,
    ) -> None:
        self.database = database
        self.adapter = adapter
        self.stock_codes = tuple(dict.fromkeys(stock_codes))
        if not self.stock_codes:
            raise ValueError("监测至少需要一个股票代码。")
        if interval_seconds < 1 or fetch_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("轮询间隔和超时必须大于 0。")
        self.session_date = session_date
        self.session_key = session_date.isoformat()
        self.clock = clock or SystemClock()
        self.interval_seconds = interval_seconds
        self.fetch_timeout_seconds = float(fetch_timeout_seconds)
        self.close_timeout_seconds = float(close_timeout_seconds)
        self.on_analysis_ready = on_analysis_ready
        self.on_daily_confirmed = on_daily_confirmed
        self._round_active = False
        self._fetch_thread: threading.Thread | None = None
        ensure_monitor_state(database, self.session_key)

    def _fetch(
        self,
        call: Callable[[], Iterable[AdapterRecord]],
        timeout_seconds: float,
    ) -> list[AdapterRecord]:
        """在守护线程中等待只读请求；超时结果会被丢弃，不在线程中写数据库。"""
        if self._fetch_thread is not None:
            if self._fetch_thread.is_alive():
                raise AdapterBusy("上一次行情请求仍在等待，已跳过叠加请求。")
            self._fetch_thread = None
        if timeout_seconds <= 0:
            raise AdapterTimeout("本轮收尾时间已经用完。")

        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put((True, list(call())))
            except Exception as exc:  # 适配器异常交回主线程统一记录
                result_queue.put((False, exc))

        thread = threading.Thread(target=worker, name="pandeng-readonly-fetch", daemon=True)
        self._fetch_thread = thread
        thread.start()
        try:
            success, payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise AdapterTimeout(f"数据源等待超过 {timeout_seconds:.3f} 秒。") from exc
        thread.join(timeout=0.05)
        # 结果已完整放入队列，适配器读取已经结束，不让线程收尾阻塞下一轮。
        self._fetch_thread = None
        if not success:
            raise payload
        return list(payload)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)

    def _expected(self, start: datetime, end: datetime) -> list[datetime]:
        return [point for point in session_minutes(self.session_date) if start <= point <= end]

    def _target_minute_ready(self, code: str, target: datetime) -> bool:
        """只确认本轮目标分钟本身；历史连续性由指标层另行判断。"""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT confirmed FROM minute_bars
                WHERE stock_code = ? AND bar_time = ?
                """,
                (code, target.isoformat()),
            ).fetchone()
        return row is not None and bool(row["confirmed"])

    def _start_for(self, row: Any) -> datetime:
        if row is None or row["last_contiguous_time"] is None:
            return session_minutes(self.session_date)[0]
        previous = datetime.fromisoformat(row["last_contiguous_time"])
        following = next_session_minute(previous)
        return following or day_boundary(self.session_date, AFTERNOON_END) + timedelta(minutes=1)

    def _unknown(
        self,
        code: str,
        row: Any,
        attempt: datetime,
        reason: str,
        *,
        round_id: int,
        target: datetime,
    ) -> CodeRoundResult:
        last = None if row is None else row["last_contiguous_time"]
        start = self._start_for(row)
        save_code_state(
            self.database, self.session_key, code,
            last_contiguous_time=last, last_attempt_time=attempt.isoformat(),
            status="unknown", unknown_reason=reason,
        )
        add_monitor_event(
            self.database, self.session_key, "data_unknown", reason,
            round_id=round_id, stock_code=code, data_time=target,
        )
        return CodeRoundResult(
            code, start, target, "unknown", False, reason=reason
        )

    def _calendar_ready(self, round_id: int, now: datetime, timeout: float) -> bool:
        state = get_monitor_state(self.database)
        if state["calendar_status"] == "ok":
            return True
        if state["calendar_status"] == "closed":
            return False
        try:
            records = ensure_calendar_scope(
                self._fetch(
                    lambda: self.adapter.fetch_trading_calendar(
                        self.session_date, self.session_date
                    ),
                    timeout,
                ),
                self.session_date,
                self.session_date,
            )
            summary = ingest_calendar(self.database, records, adapter=self.adapter.name)
        except Exception as exc:
            update_monitor_state(self.database, calendar_status="unknown", status="running")
            add_monitor_event(
                self.database, self.session_key, "calendar_unknown",
                f"交易日历获取失败：{type(exc).__name__}: {exc}",
                round_id=round_id, data_time=now,
            )
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT is_open FROM trading_calendar WHERE trade_date = ?",
                (self.session_key,),
            ).fetchone()
        if summary.status != "ok" or row is None:
            update_monitor_state(self.database, calendar_status="unknown")
            add_monitor_event(
                self.database, self.session_key, "calendar_unknown",
                "交易日历缺失或存在冲突，不能假定今天开市。",
                round_id=round_id, data_time=now,
            )
            return False
        if not bool(row["is_open"]):
            update_monitor_state(
                self.database, calendar_status="closed", status="closed",
                stop_reason="not_trading_day", next_poll_at=None,
            )
            add_monitor_event(
                self.database, self.session_key, "not_trading_day",
                "交易日历确认今天不开市，未安排分钟轮询。", round_id=round_id,
            )
            return False
        update_monitor_state(self.database, calendar_status="ok")
        return True

    def run_round(
        self,
        *,
        trigger: str = "interval",
        recovery: bool = False,
        now: datetime | None = None,
        closing: bool = False,
        deadline: float | None = None,
    ) -> RoundResult:
        if self._round_active:
            return RoundResult(
                None, trigger, recovery, None, "skipped", (),
                message="上一轮尚未结束，本轮没有启动。",
            )
        now = (now or self.clock.now()).astimezone(CHINA_TZ)
        target = day_boundary(self.session_date, AFTERNOON_END) if closing else session_target(now)
        if target is None:
            return RoundResult(
                None, trigger, recovery, None, "skipped", (),
                message="当前不是可轮询的交易分钟。",
            )
        state = ensure_monitor_state(self.database, self.session_key)
        if state["status"] == "paused" and trigger not in {"resume", "close"}:
            return RoundResult(
                None, trigger, recovery, target, "skipped", (),
                message="监测已暂停，没有发起行情请求。",
            )

        self._round_active = True
        prior_states = get_code_states(self.database, self.session_key)
        recovery = recovery or any(
            row["status"] == "unknown" for row in prior_states.values()
        )
        round_id = start_monitor_round(
            self.database, self.session_key,
            trigger=trigger, recovery=recovery, scheduled_at=now, target_time=target,
        )
        update_monitor_state(self.database, status="running", stop_reason=None)
        code_results: list[CodeRoundResult] = []
        try:
            remaining = (
                self.fetch_timeout_seconds if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            if not self._calendar_ready(round_id, now, remaining):
                state = get_monitor_state(self.database)
                status = "skipped" if state["status"] == "closed" else "unknown"
                message = "非交易日，未轮询。" if status == "skipped" else "交易日历状态未知。"
                finish_monitor_round(self.database, round_id, status=status, message=message)
                update_monitor_state(
                    self.database, last_round_at=now.isoformat(),
                    next_poll_at=(
                        None if status == "skipped"
                        else next_poll_time(now, self.interval_seconds).isoformat()
                    ),
                )
                return RoundResult(
                    round_id, trigger, recovery, target, status, (), message=message
                )

            for code in self.stock_codes:
                row = prior_states.get(code)
                start = self._start_for(row)
                if start > target:
                    already_ready = (
                        row is not None and row["status"] == "ok"
                        and row["last_contiguous_time"] == target.isoformat()
                    )
                    code_results.append(CodeRoundResult(
                        code, None, target,
                        "ok" if already_ready else "unknown", already_ready,
                        reason="没有新的交易分钟。" if already_ready else "连续检查点尚未确认。",
                    ))
                    continue

                remaining = (
                    self.fetch_timeout_seconds if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                try:
                    records = ensure_minute_scope(
                        self._fetch(
                            lambda code=code, start=start: self.adapter.fetch_minute_bars(
                                [code], start, target
                            ),
                            remaining,
                        ),
                        [code],
                        start,
                        target,
                    )
                except Exception as exc:
                    reason = f"行情获取失败：{type(exc).__name__}: {exc}"
                    result = self._unknown(
                        code, row, now, reason, round_id=round_id, target=target
                    )
                    code_results.append(result)
                    if recovery:
                        add_monitor_event(
                            self.database, self.session_key, "offline_recovery",
                            f"离线恢复补取 {start.isoformat()} 至 {target.isoformat()}：状态未知。",
                            round_id=round_id, stock_code=code, data_time=target,
                        )
                    continue

                summary = ingest_minutes(
                    self.database, records, adapter=self.adapter.name,
                    replay_clock=target, check_gaps=False,
                )
                absent = tuple(missing_minutes(
                    self.database, code, self._expected(start, target)
                ))
                if summary.status == "ok" and not absent:
                    save_code_state(
                        self.database, self.session_key, code,
                        last_contiguous_time=target.isoformat(),
                        last_attempt_time=now.isoformat(), status="ok", unknown_reason=None,
                    )
                    result = CodeRoundResult(
                        code, start, target, "ok", True,
                        summary.received, summary.accepted, summary.duplicates,
                        summary.rejected,
                    )
                else:
                    reasons: list[str] = []
                    if summary.status != "ok":
                        reasons.append("数据清洗、重复冲突或分钟序列检查存在异常")
                    if absent:
                        reasons.append(
                            "仍缺分钟：" + ", ".join(point.isoformat() for point in absent[:5])
                        )
                    price_line_ready = (
                        summary.status == "ok" and self._target_minute_ready(code, target)
                    )
                    if price_line_ready and absent:
                        reasons.append(
                            "目标分钟有效，可独立检查固定止损；历史缺口仍使上下文指标未知"
                        )
                    reason = "；".join(reasons) or "状态未知"
                    last = None if row is None else row["last_contiguous_time"]
                    save_code_state(
                        self.database, self.session_key, code,
                        last_contiguous_time=last, last_attempt_time=now.isoformat(),
                        status="unknown", unknown_reason=reason,
                    )
                    add_monitor_event(
                        self.database, self.session_key, "data_unknown", reason,
                        round_id=round_id, stock_code=code, data_time=target,
                    )
                    result = CodeRoundResult(
                        code, start, target, "unknown", price_line_ready,
                        summary.received, summary.accepted, summary.duplicates,
                        summary.rejected, absent, reason,
                    )
                code_results.append(result)
                if recovery:
                    add_monitor_event(
                        self.database, self.session_key, "offline_recovery",
                        (
                            f"离线恢复补取 {start.isoformat()} 至 {target.isoformat()}："
                            f"{'数据完整' if result.status == 'ok' else '仅价格线可判定' if result.analysis_allowed else '状态未知'}；"
                            "只能生成补记，不能冒充当时实时告警。"
                        ),
                        round_id=round_id, stock_code=code, data_time=target,
                    )

            ready = tuple(item.stock_code for item in code_results if item.analysis_allowed)
            totals = {
                field: sum(getattr(item, field) for item in code_results)
                for field in ("received", "accepted", "duplicates", "rejected")
            }
            status = (
                "ok" if all(item.status == "ok" for item in code_results) else "unknown"
            )
            price_only = sum(
                item.status != "ok" and item.analysis_allowed for item in code_results
            )
            unavailable = sum(not item.analysis_allowed for item in code_results)
            message = (
                "全部代码已补齐并通过连续性检查。"
                if status == "ok"
                else (
                    f"完整分析 {len(self.stock_codes) - price_only - unavailable} 只；"
                    f"仅价格线可判定 {price_only} 只；状态未知 {unavailable} 只。"
                )
            )
            finish_monitor_round(
                self.database, round_id, status=status,
                analysis_ready_count=len(ready), **totals, message=message,
            )
            if target.time() == MORNING_END:
                following_poll = day_boundary(self.session_date, AFTERNOON_START)
            else:
                following_poll = next_poll_time(now, self.interval_seconds)
            update_monitor_state(
                self.database, status="running", last_round_at=now.isoformat(),
                last_successful_round_at=now.isoformat() if status == "ok" else state[
                    "last_successful_round_at"
                ],
                next_poll_at=following_poll.isoformat(),
            )
            result = RoundResult(
                round_id, trigger, recovery, target, status, ready,
                tuple(code_results), **totals, message=message,
            )
            if ready and self.on_analysis_ready is not None:
                try:
                    self.on_analysis_ready(result)
                except Exception as exc:
                    add_monitor_event(
                        self.database, self.session_key, "analysis_hook_error",
                        f"分析入口执行失败，原始行情和检查点已保存：{type(exc).__name__}: {exc}",
                        round_id=round_id,
                    )
            return result
        except KeyboardInterrupt:
            finish_monitor_round(
                self.database, round_id, status="interrupted", message="用户中断，轮询已停止。"
            )
            self.stop("ctrl_c", round_id=round_id)
            raise
        except Exception as exc:
            finish_monitor_round(
                self.database, round_id, status="unknown",
                message=f"调度内部错误：{type(exc).__name__}: {exc}",
            )
            update_monitor_state(self.database, status="error", next_poll_at=None)
            add_run_log(
                self.database, "monitor_internal_error",
                f"{type(exc).__name__}: {exc}", level="ERROR",
            )
            raise
        finally:
            self._round_active = False

    def pause(self, now: datetime | None = None) -> None:
        now = (now or self.clock.now()).astimezone(CHINA_TZ)
        ensure_monitor_state(self.database, self.session_key)
        update_monitor_state(
            self.database, status="paused", next_poll_at=None, stop_reason=None
        )
        add_monitor_event(
            self.database, self.session_key, "monitor_paused",
            "用户暂停监测；检查点已保存，暂停期间不安排新轮询。", data_time=now,
        )
        add_run_log(self.database, "monitor_paused", now.isoformat())

    def resume(self, now: datetime | None = None) -> RoundResult:
        now = (now or self.clock.now()).astimezone(CHINA_TZ)
        update_monitor_state(self.database, status="running", stop_reason=None)
        add_monitor_event(
            self.database, self.session_key, "monitor_resumed",
            "用户继续监测；先补取暂停期间数据，再开放逐股票分析门控。", data_time=now,
        )
        return self.run_round(trigger="resume", recovery=True, now=now)

    def stop(self, reason: str = "user_stop", *, round_id: int | None = None) -> None:
        ensure_monitor_state(self.database, self.session_key)
        update_monitor_state(
            self.database, status="stopped", stop_reason=reason, next_poll_at=None
        )
        add_monitor_event(
            self.database, self.session_key, "monitor_stopped",
            f"监测停止：{reason}；检查点已保存，SQLite 连接已按事务关闭。",
            round_id=round_id, data_time=self.clock.now(),
        )
        add_run_log(self.database, "monitor_stopped", reason)

    def _finalize_daily(self, round_id: int | None, deadline: float) -> str:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            records = ensure_daily_scope(
                self._fetch(
                    lambda: self.adapter.fetch_daily_qfq(
                        self.stock_codes, self.session_date, self.session_date
                    ),
                    remaining,
                ),
                self.stock_codes,
                self.session_date,
                self.session_date,
            )
            summary = ingest_daily(self.database, records, adapter=self.adapter.name)
            confirmed = current_daily_confirmation(
                self.database, self.session_key, self.stock_codes
            )
            daily_status = "ok" if summary.status == "ok" and confirmed else "unknown"
            message = (
                "当日全部前复权日 K 已明确确认。"
                if daily_status == "ok"
                else "日 K 缺失、冲突或 confirmed=false；正式日线结论保持状态未知。"
            )
        except Exception as exc:
            daily_status = "unknown"
            message = (
                f"收盘日 K 收尾未完成：{type(exc).__name__}: {exc}；"
                "不无限重试，正式日线结论保持状态未知。"
            )
        update_monitor_state(self.database, daily_status=daily_status)
        add_monitor_event(
            self.database, self.session_key,
            "daily_confirmed" if daily_status == "ok" else "daily_unconfirmed",
            "收盘不等于日 K 确认。" + message,
            round_id=round_id, data_time=day_boundary(self.session_date, AFTERNOON_END),
        )
        return daily_status

    def close(self, now: datetime | None = None) -> TickResult:
        now = (now or self.clock.now()).astimezone(CHINA_TZ)
        if self._round_active:
            return TickResult(
                "wait", "unknown", "上一轮尚未结束，收盘收尾不会叠加启动。"
            )
        state = ensure_monitor_state(self.database, self.session_key)
        if state["status"] == "closed":
            return TickResult(
                "closed", state["daily_status"], "本交易日已经收尾。",
                daily_status=state["daily_status"],
            )
        deadline = time.monotonic() + self.close_timeout_seconds
        round_result = self.run_round(
            trigger="close", recovery=state["status"] == "paused", now=now,
            closing=True, deadline=deadline,
        )
        daily_status = self._finalize_daily(round_result.round_id, deadline)
        if daily_status == "ok" and self.on_daily_confirmed is not None:
            try:
                self.on_daily_confirmed(self.session_date, round_result.recovery)
            except Exception as exc:
                add_monitor_event(
                    self.database, self.session_key, "daily_analysis_hook_error",
                    f"确认日 K 已保存，但日线分析入口失败：{type(exc).__name__}: {exc}",
                    round_id=round_result.round_id,
                    data_time=day_boundary(self.session_date, AFTERNOON_END),
                )
        update_monitor_state(
            self.database, status="closed", stop_reason="session_closed", next_poll_at=None
        )
        message = (
            f"收盘收尾结束：分钟轮次={round_result.status}；日 K 状态={daily_status}；"
            "已停止新轮询并自动退出。"
        )
        add_monitor_event(
            self.database, self.session_key, "monitor_closed", message,
            round_id=round_result.round_id,
            data_time=day_boundary(self.session_date, AFTERNOON_END),
        )
        add_run_log(self.database, "monitor_closed", message)
        return TickResult(
            "closed", "ok" if round_result.status == "ok" and daily_status == "ok" else "unknown",
            message, round_result, daily_status,
        )

    def tick(self) -> TickResult:
        """推进一次调度决策；午休和等待分支不会调用适配器。"""
        state = ensure_monitor_state(self.database, self.session_key)
        now = self.clock.now().astimezone(CHINA_TZ)
        if state["status"] in {"closed", "stopped"}:
            return TickResult(
                state["status"], state["daily_status"],
                f"监测已结束：{state['stop_reason'] or state['status']}。",
                daily_status=state["daily_status"],
            )
        if state["status"] == "paused":
            return TickResult("paused", "unknown", "已暂停：本轮没有行情请求。")

        target = session_target(now)
        if target is not None:
            if target.time() == AFTERNOON_END:
                return self.close(now)
            last_round = self._parse_time(state["last_round_at"])
            next_poll = self._parse_time(state["next_poll_at"])
            morning_boundary = (
                target.time() == MORNING_END
                and (
                    last_round is None
                    or last_round.replace(second=0, microsecond=0) != target
                )
            )
            due = next_poll is None or now >= next_poll
            if morning_boundary:
                result = self.run_round(trigger="morning_close", now=now)
                return TickResult("round", result.status, result.message, result)
            if due:
                states = get_code_states(self.database, self.session_key)
                recovery = any(row["status"] == "unknown" for row in states.values())
                result = self.run_round(
                    trigger="recovery" if recovery else "interval",
                    recovery=recovery, now=now,
                )
                return TickResult("round", result.status, result.message, result)
            return TickResult("wait", state["calendar_status"], "未到下一轮检查时间。")

        local_time = now.time()
        if MORNING_END < local_time < AFTERNOON_START:
            following = day_boundary(self.session_date, AFTERNOON_START)
            update_monitor_state(self.database, next_poll_at=following.isoformat())
            return TickResult("lunch", "ok", "午休：不轮询；13:01 后先续接上午检查点。")
        if local_time < MORNING_START:
            following = day_boundary(self.session_date, MORNING_START)
            update_monitor_state(self.database, next_poll_at=following.isoformat())
            return TickResult("before_open", "ok", "尚未进入分钟监测时段。")

        if session_round_count(self.database, self.session_key) > 0:
            return self.close(now)
        update_monitor_state(
            self.database, status="closed", stop_reason="outside_session", next_poll_at=None
        )
        add_monitor_event(
            self.database, self.session_key, "outside_session",
            "收盘后启动监测：不补做自动轮询，安全退出；仍可手动回放和查询。", data_time=now,
        )
        return TickResult("closed", "not_ready", "非监测时段，未发起行情请求。", daily_status="not_ready")

    def sleep_seconds(self) -> float:
        now = self.clock.now().astimezone(CHINA_TZ)
        state = get_monitor_state(self.database)
        if state["status"] == "paused":
            return 1.0
        local_time = now.time()
        if local_time == MORNING_END or MORNING_END < local_time < AFTERNOON_START:
            return seconds_until(day_boundary(self.session_date, AFTERNOON_START), now)
        if local_time < MORNING_START:
            return seconds_until(day_boundary(self.session_date, MORNING_START), now)
        if local_time > AFTERNOON_END:
            return 0.0
        candidates: list[float] = []
        next_poll = self._parse_time(state["next_poll_at"])
        if next_poll is not None:
            candidates.append(seconds_until(next_poll, now))
        for boundary in (MORNING_END, AFTERNOON_END):
            later = day_boundary(self.session_date, boundary)
            if later >= now:
                candidates.append(seconds_until(later, now))
        return min(candidates) if candidates else 1.0

    def run(self, *, max_ticks: int | None = None) -> str:
        """持续运行到收盘或停止；max_ticks 只用于测试，避免无限循环。"""
        ticks = 0
        while True:
            state = ensure_monitor_state(self.database, self.session_key)
            if state["status"] in {"closed", "stopped"}:
                return state["stop_reason"] or state["status"]
            try:
                result = self.tick()
                if result.action in {"closed", "stopped"}:
                    state = get_monitor_state(self.database)
                    return state["stop_reason"] or result.action
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    self.stop("max_ticks")
                    return "max_ticks"
                self.clock.sleep(self.sleep_seconds())
            except KeyboardInterrupt:
                state = get_monitor_state(self.database)
                if state["status"] != "stopped":
                    self.stop("ctrl_c")
                return "ctrl_c"
