"""可替换时钟：正式运行使用系统时间，测试使用手动推进时间。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pandeng.data.schedule import CHINA_TZ


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""

    def sleep(self, seconds: float) -> None:
        """等待指定秒数；测试时钟可以直接推进，不实际等待。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(CHINA_TZ)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class ManualClock:
    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None or self.current.utcoffset() is None:
            raise ValueError("模拟时钟必须带时区。")
        self.current = self.current.astimezone(CHINA_TZ)

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("模拟时钟必须带时区。")
        self.current = value.astimezone(CHINA_TZ)

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("等待秒数不能为负数。")
        self.current += timedelta(seconds=seconds)
