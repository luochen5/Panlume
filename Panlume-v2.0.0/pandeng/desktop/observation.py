"""桌面观察会话：显式启停、整数轮次、动态设置和分钟修订审计。

本模块只调度只读适配器和确定性入库。启动本身不联网；是否联网完全取决于
调用方注入的适配器。模型、账户和交易功能均不在此模块的能力范围内。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from pandeng.data.base import MarketDataAdapter
from pandeng.data.eastmoney_adapter import ProviderLiveError
from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.settings import DesktopSettings
from pandeng.storage.db import Database
from pandeng.storage.monitor import (
    add_monitor_event,
    finish_monitor_round,
    start_monitor_round,
)
from pandeng.storage.repositories import ingest_minutes


Phase = Literal["idle", "running", "stopping", "stopped", "completed", "error"]


@dataclass(frozen=True)
class StockObservation:
    stock_code: str
    status: str = "等待"
    source: str = "—"
    bar_time: str = "—"
    confirmed: str = "—"
    close: str = "—"
    message: str = "尚未请求"


@dataclass(frozen=True)
class ObservationSnapshot:
    phase: Phase = "idle"
    round_number: int = 0
    interval_seconds: int = 60
    stock_codes: tuple[str, ...] = ()
    message: str = "观察未启动"
    stocks: tuple[StockObservation, ...] = ()


class ObservationRunner:
    """同步执行观察轮次；桌面线程和测试共用同一套边界。"""

    def __init__(
        self,
        database: Database,
        adapter: MarketDataAdapter,
        settings_provider: Callable[[], DesktopSettings],
        *,
        request_start: datetime,
        request_end: datetime,
        on_change: Callable[[ObservationSnapshot], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.adapter = adapter
        self.settings_provider = settings_provider
        self.request_start = self._minute(request_start)
        self.request_end = self._minute(request_end)
        if self.request_start > self.request_end:
            raise ValueError("观察请求起点不能晚于终点。")
        self.on_change = on_change or (lambda snapshot: None)
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self._snapshot = ObservationSnapshot()
        self._lock = threading.Lock()

    @staticmethod
    def _minute(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("观察时间必须带时区。")
        return value.astimezone(CHINA_TZ).replace(second=0, microsecond=0)

    def snapshot(self) -> ObservationSnapshot:
        with self._lock:
            return self._snapshot

    def _publish(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
            snapshot = self._snapshot
        self.on_change(snapshot)

    def request_stop(self) -> None:
        self.stop_event.set()
        if self.snapshot().phase == "running":
            self._publish(phase="stopping", message="正在停止；当前请求结束后不再启动新请求")

    def _stock_rows(self, codes: tuple[str, ...]) -> tuple[StockObservation, ...]:
        rows: list[StockObservation] = []
        current = {item.stock_code: item for item in self.snapshot().stocks}
        with self.database.connect() as connection:
            for code in codes:
                row = connection.execute(
                    """
                    SELECT source, bar_time, confirmed, close
                    FROM minute_bars WHERE stock_code = ?
                    ORDER BY bar_time DESC LIMIT 1
                    """,
                    (code,),
                ).fetchone()
                if row is None:
                    rows.append(StockObservation(code))
                else:
                    previous = current.get(code, StockObservation(code))
                    rows.append(StockObservation(
                        code,
                        status=previous.status,
                        source=row["source"],
                        bar_time=row["bar_time"],
                        confirmed="已确认" if row["confirmed"] else "未确认",
                        close=f"{row['close']:.2f}",
                        message=previous.message,
                    ))
        return tuple(rows)

    def run(self, *, max_rounds: int = 1, deadline_monotonic: float | None = None) -> None:
        if isinstance(max_rounds, bool) or max_rounds < 1:
            raise ValueError("观察轮次必须是正整数。")
        for round_number in range(1, max_rounds + 1):
            if self.stop_event.is_set():
                break
            # 整数轮次先判边界，不用浮点剩余时间决定是否多发一轮。
            if deadline_monotonic is not None and self.monotonic() >= deadline_monotonic:
                break
            settings = self.settings_provider()
            codes = tuple(item.code for item in settings.stocks if item.enabled)
            self._publish(
                phase="running",
                round_number=round_number,
                interval_seconds=settings.interval_seconds,
                stock_codes=codes,
                message=f"第 {round_number} 轮观察中",
                stocks=self._stock_rows(codes),
            )
            self._run_round(round_number, codes)
            if self.stop_event.is_set():
                break
            if round_number < max_rounds:
                self._publish(message=f"第 {round_number} 轮完成；等待下一轮")
                # Event.wait 可被停止立即唤醒；动态设置在下一轮开头重新读取。
                self.stop_event.wait(settings.interval_seconds)
        phase = self.snapshot().phase
        if phase != "error":
            final_phase: Phase = "stopped" if self.stop_event.is_set() else "completed"
            self._publish(
                phase=final_phase,
                message="观察已停止，未启动额外请求" if final_phase == "stopped" else "观察轮次已完成",
            )

    def _run_round(self, round_number: int, codes: tuple[str, ...]) -> None:
        target = self.request_end
        session_date = target.date().isoformat()
        round_id = start_monitor_round(
            self.database,
            session_date,
            trigger="desktop",
            recovery=False,
            scheduled_at=target,
            target_time=target,
        )
        totals = {"received": 0, "accepted": 0, "duplicates": 0, "rejected": 0}
        errors: list[str] = []
        for code in codes:
            if self.stop_event.is_set():
                break
            start = self._next_start(code)
            if start is None:
                errors.append(f"{code} 来源或复权口径与已有记录不同")
                self._update_stock(code, status="来源冲突", message="已拒绝静默拼接")
                continue
            if start > target:
                self._update_stock(code, status="已最新", message="没有新的分钟需要请求")
                continue
            try:
                records = list(self.adapter.fetch_minute_bars((code,), start, target))
                summary = ingest_minutes(
                    self.database,
                    records,
                    adapter=self.adapter.name,
                    replay_clock=target,
                    allow_unconfirmed_revisions=True,
                )
                for key in totals:
                    totals[key] += getattr(summary, key)
                self._update_stock(
                    code,
                    status="状态未知" if summary.unknown_codes else "已入库",
                    message=(
                        f"收到 {summary.received}，入库/修订 {summary.accepted}，"
                        f"重复 {summary.duplicates}，拒绝 {summary.rejected}"
                    ),
                )
            except ProviderLiveError as exc:
                errors.append(f"{code} {exc.category}")
                self._update_stock(code, status="请求失败", message=f"{exc.category}：{exc}")
                add_monitor_event(
                    self.database, session_date, "request_error", str(exc),
                    round_id=round_id, stock_code=code,
                )
            except Exception as exc:
                errors.append(f"{code} request_failed")
                self._update_stock(code, status="处理失败", message=str(exc)[:240])
                add_monitor_event(
                    self.database, session_date, "processing_error", str(exc)[:500],
                    round_id=round_id, stock_code=code,
                )
        status = "partial" if errors else "ok"
        if self.stop_event.is_set():
            status = "stopped"
        finish_monitor_round(
            self.database,
            round_id,
            status=status,
            received=totals["received"],
            accepted=totals["accepted"],
            duplicates=totals["duplicates"],
            rejected=totals["rejected"],
            message="；".join(errors),
        )
        self._publish(
            stocks=self._stock_rows(codes),
            message=(
                f"第 {round_number} 轮完成：{len(errors)} 个代码有错误"
                if errors else f"第 {round_number} 轮完成"
            ),
        )

    def _next_start(self, code: str) -> datetime | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT bar_time, source, adjustment, confirmed FROM minute_bars
                WHERE stock_code = ? ORDER BY bar_time DESC LIMIT 1
                """,
                (code,),
            ).fetchone()
        if row is None:
            return self.request_start
        if row["source"] != self.adapter.name or row["adjustment"] != "none":
            return None
        latest = datetime.fromisoformat(row["bar_time"])
        # 未确认分钟需要重读以捕获盘中修订；已确认分钟只向后增量。
        return latest if not row["confirmed"] else latest + timedelta(minutes=1)

    def _update_stock(self, code: str, **changes: object) -> None:
        current = {item.stock_code: item for item in self.snapshot().stocks}
        previous = current.get(code, StockObservation(code))
        current[code] = replace(previous, **changes)
        self._publish(stocks=tuple(current.values()))
