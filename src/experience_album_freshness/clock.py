"""可注入时钟。

新鲜度检查不能依赖系统墙钟，否则测试无法稳定复现“刚过期/很久之前”。
业务服务只依赖 ``Clock.now()``，生产环境注入 ``SystemClock``，
测试与重放注入 ``FixedClock``。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """必须返回携带时区的时间。"""


class SystemClock:
    """生产时钟，默认 UTC。"""

    def __init__(self, tz=timezone.utc) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    """固定时钟，可通过 ``advance`` 推进，供测试与重放使用。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("FixedClock 的初始时间必须携带时区")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def set_now(self, moment: datetime) -> None:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("时间必须携带时区")
        self._moment = moment

    def advance(self, delta: timedelta) -> datetime:
        self._moment += delta
        return self._moment
