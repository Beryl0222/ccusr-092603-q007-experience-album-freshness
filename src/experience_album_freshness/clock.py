"""可注入时钟。

新鲜度检查与事件时间都从时钟取，生产用系统时钟，测试用固定/手动时钟。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试时钟：手动拨动，且拒绝拨回。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("固定时钟的起始时间必须携带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta) -> None:
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须携带时区")
        if value < self._now:
            raise ValueError("时钟不允许回拨，以免掩盖重复通知")
        self._now = value
