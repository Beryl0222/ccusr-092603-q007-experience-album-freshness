"""领域错误。

错误码稳定，可直接映射给接口调用方；中文消息面向运营与日志。
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}


class NotFound(DomainError):
    code = "not_found"


class PermissionDenied(DomainError):
    code = "permission_denied"


class ValidationError(DomainError):
    code = "validation_error"


class IdempotencyConflict(DomainError):
    """同一 request_id 携带了不同请求体。"""

    code = "idempotency_conflict"


class VersionFrozenError(DomainError):
    """同一聚合、同一版本号出现不同内容，流必须冻结等待人工介入。"""

    code = "version_frozen"


class ConcurrencyConflictError(DomainError):
    """expected_version 落后于当前版本，调用方必须先读取再显式合并。"""

    code = "concurrency_conflict"

    def __init__(self, *, aggregate_id: str, expected: int, current: int, current_state: Any = None) -> None:
        super().__init__(
            f"条目已被他人修改（期望基于版本 {expected}，当前版本 {current}），需要显式合并",
            context={
                "aggregate_id": aggregate_id,
                "expected_version": expected,
                "current_version": current,
                "current_state": current_state,
            },
        )
        self.current_version = current
        self.current_state = current_state


class MergeRequiredError(ConcurrencyConflictError):
    code = "merge_required"


class FactCorrectionBoundaryError(DomainError):
    """外部事实更正试图改动用户观点字段。"""

    code = "fact_correction_boundary"


class ReviewerConflict(DomainError):
    """审核员参与过该专辑的编辑，必须回避。"""

    code = "reviewer_conflict"
