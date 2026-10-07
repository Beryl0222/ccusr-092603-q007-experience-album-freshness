"""事件流折叠出的领域读模型。

模型本身不可变：所有状态变更都来自 :class:`~experience_album_freshness.store.EventStore`
里追加的事件。历史事件永不删除，因此“旧分享仍在转发”时，阅读接口既能
给出快照原文，也能对照当前地点状态标注失效。

聚合与事件流的物理布局：

- 专辑主流 ``aggregate_id = album_id``：建专辑、邀请协作者、评论。
- 条目流 ``aggregate_id = album_id::entry::<entry_id>``：条目的增改、
  显式合并、作者撤认可。独立事件流让乐观锁精确到“同一条目”。
- 地点流 ``aggregate_id = album_id::place::<place_id>``：注册、事实变化，
  事件流版本号即业务地点版本号。
- 快照流、申诉流同理挂在专辑下；所有非主流事件都在 payload 带 ``album_id``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .access import AttachmentSummary

# ---------------------------------------------------------------- 事件类型

ALBUM_CREATED = "ALBUM_CREATED"
ENTRY_ADDED = "ENTRY_ADDED"
ENTRY_UPDATED = "ENTRY_UPDATED"
ENTRY_MERGE_RESOLVED = "ENTRY_MERGE_RESOLVED"
PLACE_REGISTERED = "PLACE_REGISTERED"
PLACE_CHANGED = "PLACE_CHANGED"
PLACE_MERGED = "PLACE_MERGED"
SNAPSHOT_SHARED = "SNAPSHOT_SHARED"
COMMENT_ADDED = "COMMENT_ADDED"
CORRECTION_SUBMITTED = "CORRECTION_SUBMITTED"
CORRECTION_DECIDED = "CORRECTION_DECIDED"
AUTHOR_WITHDREW = "AUTHOR_WITHDREW"
COLLABORATOR_INVITED = "COLLABORATOR_INVITED"

ALL_EVENT_TYPES = frozenset(
    {
        ALBUM_CREATED,
        ENTRY_ADDED,
        ENTRY_UPDATED,
        ENTRY_MERGE_RESOLVED,
        PLACE_REGISTERED,
        PLACE_CHANGED,
        PLACE_MERGED,
        SNAPSHOT_SHARED,
        COMMENT_ADDED,
        CORRECTION_SUBMITTED,
        CORRECTION_DECIDED,
        AUTHOR_WITHDREW,
        COLLABORATOR_INVITED,
    }
)

# 属于“编辑”的事件类型；其 actor 即专辑编辑参与者，审核员需回避。
EDIT_EVENT_TYPES = frozenset(
    {
        ALBUM_CREATED,
        ENTRY_ADDED,
        ENTRY_UPDATED,
        ENTRY_MERGE_RESOLVED,
        PLACE_CHANGED,
        PLACE_MERGED,
        COLLABORATOR_INVITED,
    }
)

# 商家事实更正允许触及的事实键；用户观点字段永远不在其中。
FACT_KEYS = frozenset({"smoke_free", "hours", "facilities", "location_note", "contact", "status"})
OPINION_FIELDS = frozenset(
    {"recommendation", "recommendation_reason", "negative_experience", "audience_conditions"}
)

ENTRY_UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "recommendation",
        "recommendation_reason",
        "audience_conditions",
        "negative_experience",
        "experience_at",
        "attachments",
    }
)

RECOMMENDATIONS = frozenset({"recommended", "not_recommended", "neutral"})


# -------------------------------------------------------------------- 读模型


@dataclass
class Place:
    place_id: str
    name: str
    version: int = 0
    facts: dict[str, Any] = field(default_factory=dict)
    status: str = "active"  # active | closed | merged
    merged_into: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def facts_at(self, version: int) -> dict[str, Any]:
        """返回某一地点版本时的事实快照。"""

        facts: dict[str, Any] = {}
        for item in self.history:
            if item["place_version"] > version:
                break
            for key, value in item.get("changed_facts", {}).items():
                facts[key] = value
            for key in item.get("removed_facts", []):
                facts.pop(key, None)
        return facts

    def changes_since(self, version: int) -> list[dict[str, Any]]:
        """某版本之后发生的全部事实变化（含设施撤除）。"""

        return [item for item in self.history if item["place_version"] > version]


@dataclass
class Comment:
    comment_id: str
    entry_id: str
    actor: str
    quote: str
    place_id: str
    place_version: int
    created_at: str
    migrated: bool = False


@dataclass
class Entry:
    entry_id: str
    place_id: str
    place_version: int
    author_id: str
    title: str
    recommendation: str
    recommendation_reason: str
    audience_conditions: str
    negative_experience: str | None
    experience_at: str
    attachments: list[AttachmentSummary] = field(default_factory=list)
    created_at: str = ""
    updated_at: str | None = None
    entry_version: int = 1
    endorsed: bool = True
    withdrew_reason: str | None = None
    comments: list[Comment] = field(default_factory=list)
    editors: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    snapshot_id: str
    entry_id: str
    shared_by: str
    shared_at: str
    recipient_scope: str
    snapshot_hash: str
    title: str
    content: dict[str, Any]
    place_versions: dict[str, int]
    endorsed_at_share: bool


@dataclass
class CorrectionCase:
    case_id: str
    subject_type: str  # place
    subject_id: str
    submitter_id: str
    submitter_role: str
    factual_patches: dict[str, Any]
    removed_facilities: list[str]
    rationale: str
    status: str = "pending"  # pending | accepted | rejected
    decided_by: str | None = None
    decided_at: str | None = None
    decision_rationale: str | None = None
    accepted_facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlbumState:
    album_id: str
    name: str | None = None
    author_id: str | None = None
    created_at: str | None = None
    collaborators: set[str] = field(default_factory=set)
    entries: dict[str, Entry] = field(default_factory=dict)
    places: dict[str, Place] = field(default_factory=dict)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    comments: dict[str, Comment] = field(default_factory=dict)
    cases: dict[str, CorrectionCase] = field(default_factory=dict)
    # 参与过专辑编辑的人（作者、协作者、落地过事实变更的审核员），回避判定用。
    edit_participants: set[str] = field(default_factory=set)


def _attachments(payload: Mapping[str, Any]) -> list[AttachmentSummary]:
    return [AttachmentSummary.from_payload(item) for item in payload.get("attachments", [])]


# -------------------------------------------------------------------- 折叠器


class Repository:
    """把事件存储折叠成 :class:`AlbumState`。"""

    def __init__(self, store) -> None:
        self._store = store

    def get_album(self, album_id: str) -> AlbumState | None:
        touched = False
        state = AlbumState(album_id=album_id)
        for event in self._store.events:
            if event.aggregate_id == album_id:
                touched = True
                self._apply_main_event(state, event)
            elif event.payload.get("album_id") == album_id:
                touched = True
                self._apply_attached_event(state, event)
        return state if touched else None

    def list_album_ids(self) -> list[str]:
        ids: set[str] = set()
        for event in self._store.events:
            if event.aggregate_type == "experience_album" and "::" not in event.aggregate_id:
                ids.add(event.aggregate_id)
            album_id = event.payload.get("album_id")
            if isinstance(album_id, str):
                ids.add(album_id)
        return sorted(ids)

    # ------------------------------------------------------------ 专辑主流

    def _apply_main_event(self, state: AlbumState, event) -> None:
        p = event.payload
        if event.event_type == ALBUM_CREATED:
            state.author_id = event.actor
            state.name = p.get("name")
            state.created_at = event.occurred_at
            if event.actor:
                state.edit_participants.add(event.actor)

        elif event.event_type == COLLABORATOR_INVITED:
            state.collaborators.add(p["user_id"])

        elif event.event_type == COMMENT_ADDED:
            comment = Comment(
                comment_id=p["comment_id"],
                entry_id=p["entry_id"],
                actor=event.actor or p.get("actor", "anonymous"),
                quote=p["quote"],
                place_id=p["place_id"],
                place_version=int(p["place_version"]),
                created_at=event.occurred_at,
            )
            state.comments[comment.comment_id] = comment
            entry = state.entries.get(comment.entry_id)
            if entry is not None:
                entry.comments.append(comment)

    # ----------------------------------------------------- 条目/地点等支流

    def _apply_attached_event(self, state: AlbumState, event) -> None:
        etype = event.event_type
        p = event.payload

        if etype == ENTRY_ADDED:
            entry = Entry(
                entry_id=p["entry_id"],
                place_id=p["place_id"],
                place_version=int(p["place_version"]),
                author_id=event.actor or p["author_id"],
                title=p.get("title", ""),
                recommendation=p.get("recommendation", "neutral"),
                recommendation_reason=p.get("recommendation_reason", ""),
                audience_conditions=p.get("audience_conditions", ""),
                negative_experience=p.get("negative_experience"),
                experience_at=p["experience_at"],
                attachments=_attachments(p),
                created_at=event.occurred_at,
                editors=[event.actor] if event.actor else [],
            )
            state.entries[entry.entry_id] = entry
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype in (ENTRY_UPDATED, ENTRY_MERGE_RESOLVED):
            entry = state.entries[p["entry_id"]]
            entry.entry_version = event.version
            entry.updated_at = event.occurred_at
            entry.place_id = p.get("place_id", entry.place_id)
            entry.place_version = int(p.get("place_version", entry.place_version))
            for field_name in (
                "title",
                "recommendation",
                "recommendation_reason",
                "audience_conditions",
                "negative_experience",
                "experience_at",
            ):
                if field_name in p:
                    setattr(entry, field_name, p[field_name])
            if "attachments" in p:
                entry.attachments = _attachments(p)
            # 只有作者本人提交更新才代表“重新认可”；协作者编辑不动作者态度。
            if "endorsed" in p:
                entry.endorsed = bool(p["endorsed"])
                if entry.endorsed:
                    entry.withdrew_reason = None
            if event.actor and event.actor not in entry.editors:
                entry.editors.append(event.actor)
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype == AUTHOR_WITHDREW:
            entry = state.entries[p["entry_id"]]
            entry.entry_version = event.version
            entry.endorsed = False
            entry.withdrew_reason = p.get("reason")
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype == PLACE_REGISTERED:
            place = Place(
                place_id=p["place_id"],
                name=p.get("name", p["place_id"]),
                version=int(p["place_version"]),
                facts=dict(p.get("facts", {})),
                history=[
                    {
                        "place_version": int(p["place_version"]),
                        "changed_facts": dict(p.get("facts", {})),
                        "removed_facts": [],
                        "change_kind": "registered",
                        "occurred_at": event.occurred_at,
                    }
                ],
            )
            state.places[place.place_id] = place
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype == PLACE_CHANGED:
            place = state.places[p["place_id"]]
            place.version = int(p["place_version"])
            changed = dict(p.get("changed_facts", {}))
            removed = list(p.get("removed_facilities", []))
            for key, value in changed.items():
                place.facts[key] = value
            for key in removed:
                place.facts.pop(key, None)
            if "status" in changed:
                place.status = str(changed["status"])
            place.history.append(
                {
                    "place_version": place.version,
                    "changed_facts": changed,
                    "removed_facts": removed,
                    "change_kind": p.get("change_kind", "other"),
                    "occurred_at": event.occurred_at,
                }
            )
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype == PLACE_MERGED:
            old = state.places[p["old_place_id"]]
            old.status = "merged"
            old.merged_into = p["new_place_id"]
            confirmed_entries = set(p.get("confirmed_entry_ids", []))
            confirmed_comments = set(p.get("confirmed_comment_ids", []))
            # 只迁移被确认的引用；针对旧门店的体验保留在旧地点上。
            for entry in state.entries.values():
                if entry.entry_id in confirmed_entries:
                    entry.place_id = p["new_place_id"]
            for comment in state.comments.values():
                if comment.comment_id in confirmed_comments:
                    comment.place_id = p["new_place_id"]
                    comment.migrated = True
            if event.actor:
                state.edit_participants.add(event.actor)

        elif etype == SNAPSHOT_SHARED:
            snapshot = Snapshot(
                snapshot_id=p["snapshot_id"],
                entry_id=p["entry_id"],
                shared_by=event.actor or p.get("shared_by", "unknown"),
                shared_at=event.occurred_at,
                recipient_scope=p["recipient_scope"],
                snapshot_hash=p["snapshot_hash"],
                title=p.get("title", ""),
                content=dict(p["content"]),
                place_versions={k: int(v) for k, v in p.get("place_versions", {}).items()},
                endorsed_at_share=bool(p.get("endorsed_at_share", True)),
            )
            state.snapshots[snapshot.snapshot_id] = snapshot

        elif etype == CORRECTION_SUBMITTED:
            case = CorrectionCase(
                case_id=p["case_id"],
                subject_type=p["subject_type"],
                subject_id=p["subject_id"],
                submitter_id=event.actor or p["submitter_id"],
                submitter_role=p["submitter_role"],
                factual_patches=dict(p.get("factual_patches", {})),
                removed_facilities=list(p.get("removed_facilities", [])),
                rationale=p.get("rationale", ""),
            )
            state.cases[case.case_id] = case

        elif etype == CORRECTION_DECIDED:
            case = state.cases[p["case_id"]]
            case.status = p["decision"]
            case.decided_by = event.actor or p.get("decided_by")
            case.decided_at = event.occurred_at
            case.decision_rationale = p.get("rationale")
            case.accepted_facts = dict(p.get("accepted_facts", {}))
