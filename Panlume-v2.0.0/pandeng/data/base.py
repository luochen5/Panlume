"""统一行情数据结构和只读适配器接口。

实时数据服务、模拟器与 CSV 回放以后都实现同一组接口。
本阶段所有时间必须带时区，数据库统一保存为 ISO 8601 字符串。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping, TypeAlias


RawRecord: TypeAlias = Mapping[str, object]


@dataclass(frozen=True)
class MinuteBar:
    stock_code: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str
    adjustment: str = "none"
    confirmed: bool = True


@dataclass(frozen=True)
class DailyBar:
    stock_code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str
    adjustment: str = "qfq"
    confirmed: bool = True


@dataclass(frozen=True)
class TradingDay:
    trade_date: date
    is_open: bool
    source: str


AdapterRecord: TypeAlias = MinuteBar | DailyBar | TradingDay | RawRecord


class MarketDataAdapter(ABC):
    """只读行情接口；实现类不得包含交易或账户操作。"""

    name: str

    @abstractmethod
    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        """返回原始未复权一分钟行情，包含起止时间。"""

    @abstractmethod
    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        """返回前复权日 K，包含起止日期。"""

    @abstractmethod
    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        """返回交易日历，包含起止日期。"""
