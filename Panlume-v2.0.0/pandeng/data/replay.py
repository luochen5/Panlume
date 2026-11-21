"""CSV 历史数据回放；文件只读，不进行网络访问。"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .base import AdapterRecord, MarketDataAdapter


class CsvReplayAdapter(MarketDataAdapter):
    name = "csv_replay"

    def __init__(
        self,
        minute_file: Path | None = None,
        daily_file: Path | None = None,
        calendar_file: Path | None = None,
    ) -> None:
        self.minute_file = minute_file
        self.daily_file = daily_file
        self.calendar_file = calendar_file

    @staticmethod
    def _rows(path: Path) -> list[dict[str, str]]:
        if not path.is_file():
            raise FileNotFoundError(f"找不到 CSV 文件：{path}")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"CSV 文件缺少表头：{path}")
            return [dict(row) for row in reader]

    @staticmethod
    def _date(value: str, field: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} 必须使用 YYYY-MM-DD 格式。") from exc

    @staticmethod
    def _datetime(value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field} 必须带时区，例如 +08:00。")
        return parsed

    def fetch_minute_bars(
        self, stock_codes: Iterable[str], start: datetime, end: datetime
    ) -> Iterable[AdapterRecord]:
        if self.minute_file is None:
            return []
        codes = set(stock_codes)
        rows = self._rows(self.minute_file)
        selected: list[AdapterRecord] = []
        for row in rows:
            code = row.get("stock_code", "")
            if code not in codes:
                continue
            # 格式错误也交给清洗层记录，不在这里丢弃。
            try:
                bar_time = self._datetime(row.get("bar_time", ""), "bar_time")
            except ValueError:
                selected.append(row)
                continue
            if start <= bar_time <= end:
                selected.append(row)
        return selected

    def fetch_daily_qfq(
        self, stock_codes: Iterable[str], start: date, end: date
    ) -> Iterable[AdapterRecord]:
        if self.daily_file is None:
            return []
        codes = set(stock_codes)
        selected: list[AdapterRecord] = []
        for row in self._rows(self.daily_file):
            if row.get("stock_code", "") not in codes:
                continue
            try:
                trade_date = self._date(row.get("trade_date", ""), "trade_date")
            except ValueError:
                selected.append(row)
                continue
            if start <= trade_date <= end:
                selected.append(row)
        return selected

    def fetch_trading_calendar(self, start: date, end: date) -> Iterable[AdapterRecord]:
        if self.calendar_file is None:
            return []
        selected: list[AdapterRecord] = []
        for row in self._rows(self.calendar_file):
            try:
                trade_date = self._date(row.get("trade_date", ""), "trade_date")
            except ValueError:
                selected.append(row)
                continue
            if start <= trade_date <= end:
                selected.append(row)
        return selected
