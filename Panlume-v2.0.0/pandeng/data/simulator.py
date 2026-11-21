"""可重复的本地模拟行情，不访问网络。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Iterable

from .base import AdapterRecord, DailyBar, MarketDataAdapter, MinuteBar, TradingDay
from .schedule import CHINA_TZ, recent_weekdays, session_minutes


def _stable_number(*parts: object) -> int:
    text = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _price(code: str, day: date, index: int) -> float:
    base = 8.0 + (_stable_number(code) % 2200) / 100
    movement = ((_stable_number(code, day, index) % 21) - 10) / 1000
    return round(base * (1 + movement), 2)


class SimulatedDataAdapter(MarketDataAdapter):
    name = "simulator"

    def __init__(self, stock_codes: Iterable[str], dates: Iterable[date]) -> None:
        self.stock_codes = tuple(stock_codes)
        self.dates = tuple(dates)

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        for code in stock_codes:
            if code not in self.stock_codes:
                continue
            for day in self.dates:
                previous_close = _price(code, day, -1)
                for index, bar_time in enumerate(session_minutes(day)):
                    if not start <= bar_time <= end:
                        continue
                    close = _price(code, day, index)
                    open_price = previous_close
                    high = round(max(open_price, close) + 0.02, 2)
                    low = round(max(0.01, min(open_price, close) - 0.02), 2)
                    yield MinuteBar(
                        stock_code=code,
                        bar_time=bar_time,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=1000 + _stable_number(code, day, index, "volume") % 9000,
                        source=self.name,
                        adjustment="none",
                        confirmed=True,
                    )
                    previous_close = close

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        for code in stock_codes:
            if code not in self.stock_codes:
                continue
            previous_close = _price(code, start, -2)
            for day in self.dates:
                if not start <= day <= end:
                    continue
                close = _price(code, day, 999)
                yield DailyBar(
                    stock_code=code,
                    trade_date=day,
                    open=previous_close,
                    high=round(max(previous_close, close) + 0.05, 2),
                    low=round(max(0.01, min(previous_close, close) - 0.05), 2),
                    close=close,
                    volume=100000 + _stable_number(code, day, "daily") % 900000,
                    source=self.name,
                    adjustment="qfq",
                    confirmed=True,
                )
                previous_close = close

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        cursor = start
        while cursor <= end:
            yield TradingDay(
                trade_date=cursor,
                is_open=cursor.weekday() < 5 and cursor in self.dates,
                source=self.name,
            )
            cursor = date.fromordinal(cursor.toordinal() + 1)


def make_simulator(
    stock_codes: Iterable[str], session_date: date, daily_days: int = 25
) -> SimulatedDataAdapter:
    dates = recent_weekdays(session_date, daily_days)
    return SimulatedDataAdapter(stock_codes, dates)


def demo_time_range(session_date: date, minutes: int) -> tuple[datetime, datetime]:
    points = session_minutes(session_date)
    if not 1 <= minutes <= len(points):
        raise ValueError("分钟数量必须在 1～240 之间。")
    return points[0], points[minutes - 1]
