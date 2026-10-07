"""角色、身份声明与附件个人信息授权。

角色刻意保持扁平，权限点在需要处显式判断而不是用一张大矩阵：

- ``author``：专辑作者，可改判断、撤认可、定协作者。
- ``collaborator``：被邀请的协作者，可并发编辑条目（须显式合并）。
- ``merchant``：商家，只能提交/更正事实，不能触碰用户观点。
- ``reviewer``：审核员，处理纠错申诉；参与过编辑的专辑必须回避。
- ``reader``：阅读者，看到体验时间、事实变化与作者是否仍认可。
- ``snapshot_recipient``：快照接收者，只能读自己 scope 内的分享快照原文。

附件可能含票据、姓名、电话等个人信息。摘要字段必须打
``contains_personal_info`` 标记，阅读者接口按 :data:`PII_VISIBLE_ROLES`
决定是否下发这些字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Role(str, Enum):
    AUTHOR = "author"
    COLLABORATOR = "collaborator"
    MERCHANT = "merchant"
    REVIEWER = "reviewer"
    READER = "reader"
    SNAPSHOT_RECIPIENT = "snapshot_recipient"


# 可以看到附件中个人信息的角色：作者本人、同专辑协作者、负责审核的审核员。
# 商家、普通阅读者、快照接收者一律不可见。
PII_VISIBLE_ROLES = frozenset({Role.AUTHOR, Role.COLLABORATOR, Role.REVIEWER})


@dataclass(frozen=True)
class Identity:
    """请求方身份声明（作者身份声明的运行时载体）。"""

    user_id: str
    roles: frozenset[Role] = frozenset()
    displayed_name: str | None = None
    # SNAPSHOT_RECIPIENT 可访问的 recipient_scope 集合（"public" 除外，人人可读）。
    scopes: frozenset[str] = frozenset()

    @staticmethod
    def of(user_id: str, *roles: Role, displayed_name: str | None = None, scopes: frozenset[str] = frozenset()) -> Identity:
        return Identity(
            user_id=user_id,
            roles=frozenset(roles),
            displayed_name=displayed_name,
            scopes=frozenset(scopes),
        )

    def has(self, role: Role) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class AttachmentSummary:
    """证据附件的摘要；原文不进入阅读接口。"""

    attachment_id: str
    kind: str # receipt | photo | official_notice | screenshot | other
    summary: str
    contains_personal_info: bool = False
    personal_fields: tuple[str, ...] = ()  # 例如 ("phone", "real_name")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AttachmentSummary:
        return cls(
            attachment_id=str(payload["attachment_id"]),
            kind=str(payload.get("kind", "other")),
            summary=str(payload.get("summary", "")),
            contains_personal_info=bool(payload.get("contains_personal_info", False)),
            personal_fields=tuple(payload.get("personal_fields", ())),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "kind": self.kind,
            "summary": self.summary,
            "contains_personal_info": self.contains_personal_info,
            "personal_fields": list(self.personal_fields),
        }

    def redacted(self) -> dict[str, Any]:
        """无权角色看到的形态：保留存在性，遮蔽个人内容。"""

        return {
            "attachment_id": self.attachment_id,
            "kind": self.kind,
            "summary": "（附件含个人信息，当前角色无权查看）" if self.contains_personal_info else self.summary,
            "contains_personal_info": self.contains_personal_info,
            "personal_fields": list(self.personal_fields) if not self.contains_personal_info else [],
        }


def redact_attachments(
    attachments: list[AttachmentSummary],
    identity: Identity | None,
) -> list[dict[str, Any]]:
    """按角色过滤附件摘要中的个人信息。"""

    allowed = bool(identity) and bool(identity.roles & PII_VISIBLE_ROLES)
    if allowed:
        return [item.to_payload() for item in attachments]
    return [item.redacted() for item in attachments]
