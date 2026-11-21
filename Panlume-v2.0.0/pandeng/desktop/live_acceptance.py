"""桌面真实短验收：显式单次计划、逐字确认、独立测试库，无自动重试。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

from pandeng.config.loader import load_config
from pandeng.config.schema import ConfigError, StorageConfig, WatchItem
from pandeng.data.akshare_demo import (
    LIVE_CONFIRMATION,
    EastmoneyLiveOutcome,
    run_akshare_live_minute,
)
from pandeng.data.eastmoney_adapter import INTERFACE_NAME
from pandeng.data.schedule import CHINA_TZ, is_session_minute
from pandeng.desktop.paths import resource_path
from pandeng.desktop.settings import DesktopSettingsError, StockChoice
from pandeng.storage.db import Database


DESKTOP_LIVE_CONFIRMATION = LIVE_CONFIRMATION


@dataclass(frozen=True)
class LiveAcceptancePlan:
    stock_code: str
    session_date: date
    start: datetime
    end: datetime
    database: Path
    interface: str = INTERFACE_NAME
    planned_requests: int = 1
    retry_count: int = 0
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def describe(self, language: str = "zh-CN") -> str:
        if language == "en":
            return (
                f"Plan {self.plan_id[:12]}\n"
                f"Endpoint: AKShare {self.interface} (Eastmoney public A-share one-minute data)\n"
                f"Code: {self.stock_code}\n"
                f"Closed range: {self.start.isoformat()} through {self.end.isoformat()}\n"
                "Frequency: once; planned requests: 1; automatic retries: 0; all minutes remain unconfirmed\n"
                f"Sole database to write: {self.database}\n"
                "The main database, holdings, Ollama, and cloud services are neither written nor called."
            )
        return (
            f"计划 {self.plan_id[:12]}\n"
            f"接口：AKShare {self.interface}（东方财富公开 A 股一分钟）\n"
            f"代码：{self.stock_code}\n"
            f"闭区间：{self.start.isoformat()} 至 {self.end.isoformat()}\n"
            "频率：单次；预计请求 1 次；自动重试 0 次；全部分钟保持未确认\n"
            f"唯一写入数据库：{self.database}\n"
            "主库、持仓、Ollama 和云端均不写入或调用。"
        )


@dataclass(frozen=True)
class LiveAcceptanceResult:
    text: str
    audit_path: Path
    request_count: int
    database: Path


Executor = Callable[..., EastmoneyLiveOutcome]


class DesktopLiveAcceptanceService:
    def __init__(
        self,
        data_root: Path,
        personal_database: Path,
        *,
        now_provider: Callable[[], datetime] | None = None,
        executor: Executor = run_akshare_live_minute,
    ) -> None:
        self.data_root = data_root.resolve()
        self.personal_database = personal_database.resolve()
        self.database = Database(
            self.data_root / "runtime" / "live-acceptance" / "akshare-live-test.db"
        )
        self.now_provider = now_provider or (lambda: datetime.now(CHINA_TZ))
        self.executor = executor
        self._consumed_plans: set[str] = set()

    def _now(self) -> datetime:
        value = self.now_provider()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ConfigError("真实短验收时钟必须带时区。")
        return value.astimezone(CHINA_TZ)

    def make_plan(
        self,
        stock_code: str,
        session_date_text: str,
        end_time_text: str,
    ) -> LiveAcceptancePlan:
        """只构造计划，不创建目录、数据库、审计或网络请求。"""
        try:
            code = StockChoice(stock_code.strip()).code
            session_date = date.fromisoformat(session_date_text.strip())
        except (DesktopSettingsError, ValueError) as exc:
            raise ConfigError(f"代码或日期格式无效：{exc}") from exc
        if not re.fullmatch(r"\d{2}:\d{2}", end_time_text.strip()):
            raise ConfigError("请求终点必须使用 24 小时 HH:MM 格式。")
        hour, minute = (int(part) for part in end_time_text.strip().split(":"))
        try:
            end = datetime.combine(session_date, time(hour, minute), CHINA_TZ)
        except ValueError as exc:
            raise ConfigError(f"请求终点无效：{exc}") from exc
        now = self._now()
        if session_date > now.date():
            raise ConfigError("真实短验收不允许请求未来日期。")
        if not is_session_minute(end):
            raise ConfigError("请求终点必须位于 09:31～11:30 或 13:01～15:00。")
        if session_date == now.date() and end > now.replace(second=0, microsecond=0):
            raise ConfigError("请求终点不能晚于当前整分钟。")
        start = datetime.combine(session_date, time(9, 31), CHINA_TZ)
        return LiveAcceptancePlan(
            stock_code=code,
            session_date=session_date,
            start=start,
            end=end,
            database=self.database.path,
        )

    def execute(
        self,
        plan: LiveAcceptancePlan,
        *,
        confirmation: str,
        language: str = "zh-CN",
    ) -> LiveAcceptanceResult:
        # 确认先于一切文件写入、依赖加载和网络活动。
        if confirmation != DESKTOP_LIVE_CONFIRMATION:
            raise ConfigError("真实请求确认文本不匹配；没有发起请求、创建审计或写数据库。")
        if plan.database.resolve() != self.database.path.resolve():
            raise ConfigError("请求计划的数据库不属于本次独立短验收目录。")
        if plan.database.resolve() == self.personal_database:
            raise ConfigError("真实短验收禁止写入桌面个人主库。")
        if plan.planned_requests != 1 or plan.retry_count != 0:
            raise ConfigError("真实短验收只允许一次请求且禁止自动重试。")
        if plan.plan_id in self._consumed_plans:
            raise ConfigError("该请求计划已经执行过；如需再次请求，请重新生成计划并确认。")
        self._validate_still_current(plan)
        self._consumed_plans.add(plan.plan_id)

        self.database.initialize()
        config = load_config(resource_path("configs/analysis-test.yaml"))
        config = replace(
            config,
            storage=StorageConfig(self.database.path),
            ollama=replace(config.ollama, enabled=False),
            cloud=replace(config.cloud, enabled=False),
            watchlist=(WatchItem(plan.stock_code, "桌面真实短验收"),),
        )
        outcome = self.executor(
            config,
            plan.session_date,
            plan.stock_code,
            confirmation=DESKTOP_LIVE_CONFIRMATION,
            confirmed_through=None,
            request_end=plan.end,
        )
        if outcome.request_count != 1:
            raise ConfigError("实际请求次数与单次计划不一致；请保留审计并停止继续请求。")
        if language == "en":
            text = (
                f"Live acceptance completed: {outcome.request_count} request, zero automatic retries.\n"
                f"Raw rows: {outcome.mapping.raw_row_count}; regular minutes: "
                f"{outcome.ingestion.received}; accepted: {outcome.ingestion.accepted}; "
                f"duplicates: {outcome.ingestion.duplicates}; rejected: {outcome.ingestion.rejected}.\n"
                "All minutes remain unconfirmed; the main database, holdings, Ollama, and cloud services were not modified or called.\n"
                f"Independent database: {self.database.path}\nRequest audit: {outcome.audit_path}"
            )
        else:
            text = (
                f"真实短验收完成：实际请求 {outcome.request_count} 次，自动重试 0 次。\n"
                f"原始 {outcome.mapping.raw_row_count} 行；常规分钟 "
                f"{outcome.ingestion.received}；新增 {outcome.ingestion.accepted}；"
                f"重复 {outcome.ingestion.duplicates}；拒绝 {outcome.ingestion.rejected}。\n"
                "全部分钟保持未确认；主库、持仓、Ollama 和云端未修改或调用。\n"
                f"独立数据库：{self.database.path}\n请求审计：{outcome.audit_path}"
            )
        return LiveAcceptanceResult(
            text=text,
            audit_path=outcome.audit_path,
            request_count=outcome.request_count,
            database=self.database.path,
        )

    def _validate_still_current(self, plan: LiveAcceptancePlan) -> None:
        now = self._now()
        if plan.session_date > now.date():
            raise ConfigError("请求计划日期已无效，未发起请求。")
        if plan.session_date == now.date() and plan.end > now.replace(second=0, microsecond=0):
            raise ConfigError("请求计划终点已晚于当前整分钟，未发起请求。")
