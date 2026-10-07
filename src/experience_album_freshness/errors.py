"""时效台业务错误。"""

from __future__ import annotations


class FreshnessError(Exception):
    """业务规则冲突基类。"""


class RequestAlreadyExecuted(FreshnessError):
    """相同 request_id 的编辑请求已经执行过。"""

    def __init__(self, request_id: str, event_ids: list[str]) -> None:
        super().__init__(f"请求 {request_id} 已执行")
        self.request_id = request_id
        self.event_ids = list(event_ids)


class EntryFrozen(FreshnessError):
    """同版本异内容导致条目冻结，必须走显式合并。"""

    def __init__(self, entry_id: str, competing_event_ids: list[str]) -> None:
        super().__init__(f"条目 {entry_id} 已冻结，需要显式合并")
        self.entry_id = entry_id
        self.competing_event_ids = list(competing_event_ids)


class MergeRequired(FreshnessError):
    """协作者并发修改了同一条目，必须显式列出冲突事件后合并。"""

    def __init__(self, entry_id: str, expected_event_ids: list[str]) -> None:
        super().__init__(f"条目 {entry_id} 需要显式合并冲突编辑")
        self.entry_id = entry_id
        self.expected_event_ids = list(expected_event_ids)


class ConflictOfInterest(FreshnessError):
    """审核员参与过该专辑的编辑，必须回避。"""


class OpinionNotEditable(FreshnessError):
    """事实更正不得携带或删除用户观点。"""


class SnapshotImmutable(FreshnessError):
    """分享快照不可修改。"""


class NotFound(FreshnessError):
    """聚合或条目不存在。"""


class StaleVersion(FreshnessError):
    """编辑基于的版本过旧，且与现状不符。"""
