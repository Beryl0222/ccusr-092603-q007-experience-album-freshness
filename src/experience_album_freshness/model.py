"""事件回放得到的领域状态。

所有状态都由事件重放得出，不在事件之外保存可变真相，
这样重启进程、重建索引得到的结论完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Attachment:
    attachment_id: str
    summary: str
    uploader_id: str
    pii: Optional[dict[str, Any]] = None


@dataclass
class Entry:
    entry_id: str
    place_id: str
    author_id: str
    experience_at: str
    recommendation: str
    audience_conditions: list[str]
    negative_experience: Optional[str] = None
    identity_statement: Optional[str] = None
    attachments: list[Attachment] = field(default_factory=list)
    endorsed: bool = True
    withdraw_reason: Optional[str] = None
    anchored_place_version: int = 0
    frozen: bool = False
    competing_event_ids: list[str] = field(default_factory=list)
    last_editor_id: Optional[str] = None
    added_event_id: Optional[str] = None
    update_event_ids: list[str] = field(default_factory=list)


@dataclass
class Citation:
    comment_id: str
    entry_id: str
    author_id: str
    quoted_text: str
    entry_version: int
    event_id: str


@dataclass
class Album:
    album_id: str
    title: str = ""
    owner_id: str = ""
    identity_statement: Optional[str] = None
    collaborators: set[str] = field(default_factory=set)
    entries: dict[str, Entry] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    created: bool = False

    @property
    def editors(self) -> set[str]:
        """参与过专辑编辑的人：owner、协作者、条目录入/更新者。"""
        people = {self.owner_id} | set(self.collaborators)
        for entry in self.entries.values():
            if entry.author_id:
                people.add(entry.author_id)
            if entry.last_editor_id:
                people.add(entry.last_editor_id)
        people.discard("")
        return people

    def is_member(self, user_id: str) -> bool:
        return user_id == self.owner_id or user_id in self.collaborators


@dataclass
class Place:
    place_id: str
    name: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    registered: bool = False
    merged_into: Optional[str] = None
    history: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def removed(self) -> bool:
        return self.facts.get("status") == "removed"

    def facts_at(self, version: int) -> dict[str, Any]:
        """该地点版本对应的事实快照；锚点早于登记时视为空事实。"""
        if version <= 0:
            return {}
        known = self.history.get(version)
        if known is not None:
            return dict(known)
        # 合并等不改变事实的版本号：取不超过该版本的最近事实。
        earlier = [v for v in self.history if v <= version]
        if earlier:
            return dict(self.history[max(earlier)])
        return {}


@dataclass
class CorrectionCase:
    case_id: str
    place_id: str = ""
    album_id: str = ""
    submitter_id: str = ""
    submitter_role: str = ""
    fact_patch: dict[str, Any] = field(default_factory=dict)
    evidence_summary: Optional[str] = None
    status: str = "open"
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None


@dataclass
class SnapshotEntry:
    entry_id: str
    place_id: str
    author_id: str
    experience_at: str
    recommendation: str
    audience_conditions: list[str]
    negative_experience: Optional[str]
    identity_statement: Optional[str]
    place_name: str
    place_version: int
    facts: dict[str, Any]
    attachments: list[dict[str, Any]]
    entry_event_id: str


@dataclass
class Snapshot:
    snapshot_id: str
    album_id: str
    sharer_id: str
    recipient_scope: str
    snapshot_hash: str
    created_at: str
    entries: list[SnapshotEntry] = field(default_factory=list)


@dataclass
class Registry:
    albums: dict[str, Album] = field(default_factory=dict)
    places: dict[str, Place] = field(default_factory=dict)
    cases: dict[str, CorrectionCase] = field(default_factory=dict)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    _entry_location: dict[str, str] = field(default_factory=dict)

    def album(self, album_id: str) -> Album:
        found = self.albums.get(album_id)
        if found is None:
            raise KeyError(album_id)
        return found

    def place(self, place_id: str) -> Place:
        found = self.places.get(place_id)
        if found is None:
            raise KeyError(place_id)
        return found

    def entry_location(self, entry_id: str) -> Optional[str]:
        return self._entry_location.get(entry_id)


def _attachment(raw: dict[str, Any]) -> Attachment:
    return Attachment(
        attachment_id=raw["attachment_id"],
        summary=raw.get("summary", ""),
        uploader_id=raw.get("uploader_id", ""),
        pii=raw.get("pii"),
    )


def replay(events: list[dict[str, Any]]) -> Registry:
    registry = Registry()
    for event in events:
        _apply(registry, event)
    return registry


def _apply(registry: Registry, event: dict[str, Any]) -> None:
    event_type = event["event_type"]
    aggregate_id = event["aggregate_id"]
    payload = event.get("payload", {})

    if event_type == "ALBUM_CREATED":
        registry.albums[aggregate_id] = Album(
            album_id=aggregate_id,
            title=payload["title"],
            owner_id=payload["owner_id"],
            identity_statement=payload.get("identity_statement"),
            created=True,
        )
        return

    if event_type == "COLLABORATOR_ADDED":
        registry.album(aggregate_id).collaborators.add(payload["user_id"])
        return

    album = registry.albums.get(aggregate_id)

    if event_type == "ENTRY_ADDED":
        assert album is not None
        entry = Entry(
            entry_id=payload["entry_id"],
            place_id=payload["place_id"],
            author_id=payload["author_id"],
            experience_at=payload["experience_at"],
            recommendation=payload["recommendation"],
            audience_conditions=list(payload.get("audience_conditions", [])),
            negative_experience=payload.get("negative_experience"),
            identity_statement=payload.get("identity_statement"),
            attachments=[_attachment(raw) for raw in payload.get("attachments", [])],
            anchored_place_version=int(payload.get("place_version", 0)),
            last_editor_id=payload["author_id"],
            added_event_id=event["event_id"],
        )
        album.entries[entry.entry_id] = entry
        registry._entry_location[entry.entry_id] = aggregate_id
        return

    if event_type == "ENTRY_UPDATED":
        assert album is not None
        entry = album.entries[payload["entry_id"]]
        entry.place_id = payload["place_id"]
        entry.experience_at = payload["experience_at"]
        entry.recommendation = payload["recommendation"]
        entry.audience_conditions = list(payload.get("audience_conditions", []))
        entry.negative_experience = payload.get("negative_experience")
        entry.attachments = [_attachment(raw) for raw in payload.get("attachments", [])]
        entry.endorsed = bool(payload.get("endorsed", True))
        entry.anchored_place_version = int(payload.get("place_version", entry.anchored_place_version))
        entry.last_editor_id = payload["author_id"]
        entry.frozen = False
        entry.competing_event_ids = []
        entry.update_event_ids.append(event["event_id"])
        return

    if event_type == "ENTRY_FROZEN":
        assert album is not None
        entry = album.entries[payload["entry_id"]]
        entry.frozen = True
        entry.competing_event_ids = list(payload.get("competing_event_ids", []))
        return

    if event_type == "AUTHOR_WITHDREW":
        assert album is not None
        entry = album.entries[payload["entry_id"]]
        entry.endorsed = False
        entry.withdraw_reason = payload.get("reason")
        return

    if event_type == "COMMENT_CITED":
        assert album is not None
        album.citations.append(
            Citation(
                comment_id=payload["comment_id"],
                entry_id=payload["entry_id"],
                author_id=payload["author_id"],
                quoted_text=payload["quoted_text"],
                entry_version=int(payload.get("entry_version", event["version"])),
                event_id=event["event_id"],
            )
        )
        return

    if event_type == "PLACE_REGISTERED":
        place = Place(
            place_id=aggregate_id,
            name=payload["name"],
            facts=dict(payload.get("facts", {})),
            version=event["version"],
            registered=True,
        )
        place.history[event["version"]] = dict(place.facts)
        registry.places[aggregate_id] = place
        return

    if event_type == "PLACE_CHANGED":
        place = registry.place(aggregate_id)
        for key, value in payload.get("changed_facts", {}).items():
            if value is None:
                place.facts.pop(key, None)
            else:
                place.facts[key] = value
        place.version = event["version"]
        place.history[event["version"]] = dict(place.facts)
        return

    if event_type == "PLACE_MERGED":
        surviving = registry.place(aggregate_id)
        surviving.version = event["version"]
        surviving.history[event["version"]] = dict(surviving.facts)
        merged = registry.places.get(payload["merged_place_id"])
        if merged is not None:
            merged.merged_into = surviving.place_id
        for entry_id in payload.get("migrated_citations", []):
            located = registry._entry_location.get(entry_id)
            if located and entry_id in registry.albums[located].entries:
                registry.albums[located].entries[entry_id].place_id = surviving.place_id
        return

    if event_type == "CORRECTION_SUBMITTED":
        registry.cases[aggregate_id] = CorrectionCase(
            case_id=aggregate_id,
            place_id=payload["place_id"],
            album_id=payload.get("album_id", ""),
            submitter_id=payload["submitter_id"],
            submitter_role=payload.get("submitter_role", "reader"),
            fact_patch=dict(payload.get("fact_patch", {})),
            evidence_summary=payload.get("evidence_summary"),
        )
        return

    if event_type == "CORRECTION_DECIDED":
        case = registry.cases[aggregate_id]
        case.status = payload.get("decision", case.status)
        case.decided_by = payload.get("decided_by")
        case.decision_reason = payload.get("reason")
        return

    if event_type == "SNAPSHOT_SHARED":
        payload_entries = [
            SnapshotEntry(
                entry_id=raw["entry_id"],
                place_id=raw["place_id"],
                author_id=raw["author_id"],
                experience_at=raw["experience_at"],
                recommendation=raw["recommendation"],
                audience_conditions=list(raw.get("audience_conditions", [])),
                negative_experience=raw.get("negative_experience"),
                identity_statement=raw.get("identity_statement"),
                place_name=raw.get("place_name", ""),
                place_version=int(raw.get("place_version", 0)),
                facts=dict(raw.get("facts", {})),
                attachments=list(raw.get("attachments", [])),
                entry_event_id=raw.get("entry_event_id", ""),
            )
            for raw in payload.get("entries", [])
        ]
        registry.snapshots[aggregate_id] = Snapshot(
            snapshot_id=aggregate_id,
            album_id=payload.get("album_id", ""),
            sharer_id=payload.get("sharer_id", ""),
            recipient_scope=payload["recipient_scope"],
            snapshot_hash=payload["snapshot_hash"],
            created_at=event["occurred_at"],
            entries=payload_entries,
        )
