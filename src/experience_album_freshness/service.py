"""时效台业务服务。

在交换契约之上承载：请求幂等、同版本异内容冻结、并发显式合并、
快照不可变、地点合并只迁移确认引用、商家事实更正不触碰用户观点、
审核员回避，以及面向阅读者的时效视图。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional

from .changes import changes_since, diff_facts, place_lifecycle
from .clock import Clock, SystemClock
from .errors import (
    ConflictOfInterest,
    EntryFrozen,
    FreshnessError,
    MergeRequired,
    NotFound,
    OpinionNotEditable,
    StaleVersion,
)
from .model import Registry, replay
from .permissions import redact_attachment
from .storage import EventStore, VersionOccupied, canonical_json

MERCHANT_ROLE = "merchant"
READER_ROLE = "reader"

# 事实更正只允许触碰地点事实，以下保留键属于用户观点域。
OPINION_KEYS = {
    "entry_id",
    "recommendation",
    "negative_experience",
    "audience_conditions",
    "endorsed",
    "opinions",
}


def _as_iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("时间必须携带时区")
        return value.isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return value


class _Batch:
    """按请求构造事件，事件标识由 request_id 确定性派生。"""

    def __init__(self, request_id: str, store: EventStore, clock: Clock) -> None:
        self.request_id = request_id
        self._store = store
        self._clock = clock
        self._versions = {
            aggregate_id: store.version(aggregate_id)
            for aggregate_id in {event["aggregate_id"] for event in store.events()}
        }
        self.envelopes: list[dict[str, Any]] = []

    def add(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        version: Optional[int] = None,
        occurred_at: Optional[datetime | str] = None,
    ) -> dict[str, Any]:
        if version is None:
            version = self._versions.get(aggregate_id, 0) + 1
        self._versions[aggregate_id] = version
        envelope = {
            "event_id": f"{self.request_id}#{len(self.envelopes) + 1}",
            "request_id": self.request_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_BY_EVENT[event_type],
            "aggregate_id": aggregate_id,
            "occurred_at": _as_iso(occurred_at) if occurred_at else self._clock.now().isoformat(),
            "version": version,
            "payload": payload,
        }
        self.envelopes.append(envelope)
        return envelope


_AGGREGATE_BY_EVENT = {
    "ALBUM_CREATED": "experience_album",
    "COLLABORATOR_ADDED": "experience_album",
    "ENTRY_ADDED": "experience_album",
    "ENTRY_UPDATED": "experience_album",
    "ENTRY_FROZEN": "experience_album",
    "AUTHOR_WITHDREW": "experience_album",
    "COMMENT_CITED": "experience_album",
    "SNAPSHOT_SHARED": "shared_snapshot",
    "PLACE_REGISTERED": "place_revision",
    "PLACE_CHANGED": "place_revision",
    "PLACE_MERGED": "place_revision",
    "CORRECTION_SUBMITTED": "correction_case",
    "CORRECTION_DECIDED": "correction_case",
}

_COMPARABLE_ENTRY_FIELDS = (
    "place_id",
    "experience_at",
    "recommendation",
    "audience_conditions",
    "negative_experience",
)


class FreshnessService:
    def __init__(self, store: EventStore, clock: Optional[Clock] = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    # ----------------------------------------------------------------- 内部工具

    def registry(self) -> Registry:
        return replay(self.store.events())

    def _begin(self, request_id: str) -> None:
        existing = self.store.request_event_ids(request_id)
        if existing is not None:
            from .errors import RequestAlreadyExecuted

            raise RequestAlreadyExecuted(request_id, existing)

    def _commit(self, batch: _Batch) -> list[dict[str, Any]]:
        return self.store.commit(batch.request_id, batch.envelopes)

    def _album(self, registry: Registry, album_id: str):
        try:
            return registry.album(album_id)
        except KeyError:
            raise NotFound(f"专辑不存在: {album_id}") from None

    def _entry(self, registry: Registry, album_id: str, entry_id: str):
        album = self._album(registry, album_id)
        entry = album.entries.get(entry_id)
        if entry is None:
            raise NotFound(f"条目不存在: {entry_id}")
        return album, entry

    def _require_member(self, album, user_id: str) -> None:
        if not album.is_member(user_id):
            raise FreshnessError(f"用户 {user_id} 不是专辑成员，不能编辑")

    def _event_version_map(self, album_id: str) -> dict[str, int]:
        return {
            event["event_id"]: int(event["version"])
            for event in self.store.events(album_id)
        }

    # ----------------------------------------------------------------- 专辑与协作

    def create_album(
        self,
        request_id: str,
        album_id: str,
        title: str,
        owner_id: str,
        identity_statement: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        if album_id in self.registry().albums:
            raise FreshnessError(f"专辑已存在: {album_id}")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "ALBUM_CREATED",
            album_id,
            {
                "title": title,
                "owner_id": owner_id,
                "identity_statement": identity_statement,
            },
        )
        return self._commit(batch)

    def add_collaborator(
        self, request_id: str, album_id: str, user_id: str, added_by: str
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        registry = self.registry()
        album = self._album(registry, album_id)
        if added_by != album.owner_id:
            raise FreshnessError("只有专辑 owner 能添加协作者")
        if user_id in album.collaborators:
            raise FreshnessError(f"用户 {user_id} 已是协作者")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "COLLABORATOR_ADDED",
            album_id,
            {"user_id": user_id, "added_by": added_by},
        )
        return self._commit(batch)

    # ----------------------------------------------------------------- 条目

    @staticmethod
    def _entry_payload(
        *,
        entry_id: str,
        place_id: str,
        author_id: str,
        experience_at: datetime | str,
        recommendation: str,
        audience_conditions: list[str],
        negative_experience: Optional[str],
        identity_statement: Optional[str],
        attachments: Optional[list[dict[str, Any]]],
        place_version: int,
        endorsed: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry_id": entry_id,
            "place_id": place_id,
            "author_id": author_id,
            "experience_at": _as_iso(experience_at),
            "recommendation": recommendation,
            "audience_conditions": list(audience_conditions),
            "place_version": place_version,
            "endorsed": endorsed,
        }
        if negative_experience is not None:
            payload["negative_experience"] = negative_experience
        if identity_statement is not None:
            payload["identity_statement"] = identity_statement
        if attachments:
            payload["attachments"] = [dict(item) for item in attachments]
        return payload

    def _anchor_place_version(self, registry: Registry, place_id: str) -> int:
        place = registry.places.get(place_id)
        if place is None:
            raise NotFound(f"地点尚未登记: {place_id}")
        return place.version

    def add_entry(
        self,
        request_id: str,
        album_id: str,
        *,
        entry_id: str,
        place_id: str,
        author_id: str,
        experience_at: datetime | str,
        recommendation: str,
        audience_conditions: list[str],
        negative_experience: Optional[str] = None,
        identity_statement: Optional[str] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        registry = self.registry()
        album = self._album(registry, album_id)
        self._require_member(album, author_id)
        if entry_id in album.entries:
            raise FreshnessError(f"条目已存在: {entry_id}")
        place_version = self._anchor_place_version(registry, place_id)
        payload = self._entry_payload(
            entry_id=entry_id,
            place_id=place_id,
            author_id=author_id,
            experience_at=experience_at,
            recommendation=recommendation,
            audience_conditions=audience_conditions,
            negative_experience=negative_experience,
            identity_statement=identity_statement,
            attachments=attachments,
            place_version=place_version,
        )
        batch = _Batch(request_id, self.store, self.clock)
        batch.add("ENTRY_ADDED", album_id, payload)
        return self._commit(batch)

    def update_entry(
        self,
        request_id: str,
        album_id: str,
        *,
        entry_id: str,
        editor_id: str,
        experience_at: datetime | str,
        recommendation: str,
        audience_conditions: list[str],
        negative_experience: Optional[str] = None,
        identity_statement: Optional[str] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        place_id: Optional[str] = None,
        endorsed: bool = True,
        expected_version: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """更新作者判断。

        expected_version 是调用方读取专辑时看到的专辑版本。两位协作者都基于
        同一版本提交且内容不同、先到者已占用该版本号时，判定为“同版本异内容”，
        条目冻结，必须改用 merge_entry 显式合并。
        """
        self._begin(request_id)
        registry = self.registry()
        album, entry = self._entry(registry, album_id, entry_id)
        self._require_member(album, editor_id)
        if entry.frozen:
            raise EntryFrozen(entry_id, entry.competing_event_ids)

        target_place_id = place_id or entry.place_id
        place = registry.places.get(target_place_id)
        if place is None:
            raise NotFound(f"地点尚未登记: {target_place_id}")

        payload = self._entry_payload(
            entry_id=entry_id,
            place_id=target_place_id,
            author_id=entry.author_id,
            experience_at=experience_at,
            recommendation=recommendation,
            audience_conditions=audience_conditions,
            negative_experience=negative_experience,
            identity_statement=identity_statement,
            attachments=attachments,
            place_version=place.version,
            endorsed=endorsed,
        )
        payload["edited_by"] = editor_id

        base_version = self.store.version(album_id) if expected_version is None else expected_version
        base_event = self.store.occupant_at(album_id, base_version)
        if base_event is None and base_version > 0:
            raise FreshnessError(f"未知的基线版本: {base_version}")

        batch = _Batch(request_id, self.store, self.clock)
        batch.add("ENTRY_UPDATED", album_id, payload, version=base_version + 1)

        try:
            return self._commit(batch)
        except VersionOccupied as occupied:
            return self._handle_occupied_version(request_id, album_id, entry_id, payload, occupied)

    def _handle_occupied_version(
        self,
        request_id: str,
        album_id: str,
        entry_id: str,
        intended_payload: dict[str, Any],
        occupied: VersionOccupied,
    ) -> list[dict[str, Any]]:
        occupant = occupied.occupant
        if occupant.get("event_type") not in {"ENTRY_ADDED", "ENTRY_UPDATED"}:
            raise StaleVersion("专辑已被其他操作推进，请刷新到最新版本后重试")
        if occupant.get("payload", {}).get("entry_id") != entry_id:
            raise StaleVersion("专辑已被其他条目更新，请刷新到最新版本后重试")

        if self._same_entry_content(occupant.get("payload", {}), intended_payload):
            # 同版本同内容：并发重复写入收敛，不产生第二个事件；
            # request_id 仍登记为空结果，请求只执行一次。
            self.store.commit(request_id, [])
            return [occupant]

        # 同版本异内容：冻结条目，记录竞争的两支（先到编辑事件 + 本次冻结事件），
        # 之后必须显式列出这些事件才能合并。
        competing_ids = sorted({occupant["event_id"], f"{request_id}#1"})
        freeze_batch = _Batch(request_id, self.store, self.clock)
        freeze_batch.add(
            "ENTRY_FROZEN",
            album_id,
            {
                "entry_id": entry_id,
                "reason": "same_version_different_content",
                "competing_event_ids": competing_ids,
            },
        )
        self.store.commit(request_id, freeze_batch.envelopes)
        raise EntryFrozen(entry_id, competing_ids)

    @staticmethod
    def _same_entry_content(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return canonical_json(
            {key: left.get(key) for key in _COMPARABLE_ENTRY_FIELDS}
        ) == canonical_json({key: right.get(key) for key in _COMPARABLE_ENTRY_FIELDS})

    def merge_entry(
        self,
        request_id: str,
        album_id: str,
        *,
        entry_id: str,
        editor_id: str,
        competing_event_ids: list[str],
        experience_at: datetime | str,
        recommendation: str,
        audience_conditions: list[str],
        negative_experience: Optional[str] = None,
        identity_statement: Optional[str] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        place_id: Optional[str] = None,
        endorsed: bool = True,
        expected_version: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """显式合并：必须逐一列出冲突事件标识，不允许静默覆盖任一方。

        冻结状态下必须与冻结时记录的竞争事件集合完全一致；
        未冻结但基于旧版本提交时，也必须显式列出对方的编辑事件。
        """
        self._begin(request_id)
        registry = self.registry()
        album, entry = self._entry(registry, album_id, entry_id)
        self._require_member(album, editor_id)

        declared = set(competing_event_ids)
        own_event_ids = {event_id for event_id in {entry.added_event_id, *entry.update_event_ids} if event_id}
        if entry.frozen:
            # 冻结时竞争集合由系统记录，必须逐支列出、完全一致。
            if declared != set(entry.competing_event_ids):
                raise MergeRequired(entry_id, sorted(entry.competing_event_ids))
        else:
            unknown = declared - own_event_ids
            if unknown:
                raise MergeRequired(entry_id, sorted(own_event_ids))
            if len(declared) < 2:
                raise FreshnessError("显式合并必须至少列出两支冲突编辑")

        target_place_id = place_id or entry.place_id
        place = registry.places.get(target_place_id)
        if place is None:
            raise NotFound(f"地点尚未登记: {target_place_id}")
        payload = self._entry_payload(
            entry_id=entry_id,
            place_id=target_place_id,
            author_id=entry.author_id,
            experience_at=experience_at,
            recommendation=recommendation,
            audience_conditions=audience_conditions,
            negative_experience=negative_experience,
            identity_statement=identity_statement,
            attachments=attachments,
            place_version=place.version,
            endorsed=endorsed,
        )
        payload["edited_by"] = editor_id
        payload["merged_from_event_ids"] = sorted(declared)

        base_version = self.store.version(album_id) if expected_version is None else expected_version
        batch = _Batch(request_id, self.store, self.clock)
        batch.add("ENTRY_UPDATED", album_id, payload, version=base_version + 1)
        return self._commit(batch)

    def withdraw_endorsement(
        self, request_id: str, album_id: str, entry_id: str, author_id: str, reason: str
    ) -> list[dict[str, Any]]:
        """作者不再认可；原文与旧快照保留，只改变认可状态。"""
        self._begin(request_id)
        registry = self.registry()
        album, entry = self._entry(registry, album_id, entry_id)
        if author_id != entry.author_id:
            raise FreshnessError("只有条目录入作者能撤回认可")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "AUTHOR_WITHDREW",
            album_id,
            {"entry_id": entry_id, "reason": reason},
        )
        return self._commit(batch)

    def cite_comment(
        self,
        request_id: str,
        album_id: str,
        *,
        comment_id: str,
        entry_id: str,
        author_id: str,
        quoted_text: str,
    ) -> list[dict[str, Any]]:
        """评论引用条目时固化被引用版本，之后条目更新不改变引用所见。"""
        self._begin(request_id)
        registry = self.registry()
        album, entry = self._entry(registry, album_id, entry_id)
        if any(citation.comment_id == comment_id for citation in album.citations):
            raise FreshnessError(f"评论已引用: {comment_id}")
        head_event_id = entry.update_event_ids[-1] if entry.update_event_ids else entry.added_event_id
        entry_version = self._event_version_map(album_id)[head_event_id]
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "COMMENT_CITED",
            album_id,
            {
                "comment_id": comment_id,
                "entry_id": entry_id,
                "author_id": author_id,
                "quoted_text": quoted_text,
                "entry_version": entry_version,
                "entry_event_id": head_event_id,
            },
        )
        return self._commit(batch)

    # ----------------------------------------------------------------- 地点

    def register_place(
        self,
        request_id: str,
        place_id: str,
        name: str,
        facts: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        if place_id in self.registry().places:
            raise FreshnessError(f"地点已登记: {place_id}")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "PLACE_REGISTERED",
            place_id,
            {"place_id": place_id, "name": name, "facts": dict(facts or {})},
        )
        return self._commit(batch)

    def change_place(
        self, request_id: str, place_id: str, changed_facts: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        if place_id not in self.registry().places:
            raise NotFound(f"地点尚未登记: {place_id}")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "PLACE_CHANGED",
            place_id,
            {"place_version": self.store.version(place_id) + 1, "changed_facts": dict(changed_facts)},
        )
        return self._commit(batch)

    def merge_places(
        self,
        request_id: str,
        *,
        surviving_place_id: str,
        merged_place_id: str,
        migrated_citations: list[str],
    ) -> list[dict[str, Any]]:
        """合并地点。

        只迁移 migrated_citations 中被确认的条目引用；其余针对旧门店的
        亲身体验继续挂在旧地点下，不被存活门店吞掉。
        """
        self._begin(request_id)
        registry = self.registry()
        if surviving_place_id not in registry.places:
            raise NotFound(f"存活地点不存在: {surviving_place_id}")
        merged_place = registry.places.get(merged_place_id)
        if merged_place is None:
            raise NotFound(f"被合并地点不存在: {merged_place_id}")
        if merged_place.merged_into:
            raise FreshnessError("被合并地点已经并入其他地点")

        for entry_id in migrated_citations:
            album_id = registry.entry_location(entry_id)
            if album_id is None:
                raise FreshnessError(f"引用的条目不存在: {entry_id}")
            if registry.album(album_id).entries[entry_id].place_id != merged_place_id:
                raise FreshnessError(
                    f"条目 {entry_id} 当前不指向被合并门店，不能随合并迁移"
                )

        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "PLACE_MERGED",
            surviving_place_id,
            {
                "surviving_place_id": surviving_place_id,
                "merged_place_id": merged_place_id,
                "migrated_citations": list(migrated_citations),
            },
        )
        return self._commit(batch)

    # ----------------------------------------------------------------- 纠错申诉

    def submit_correction(
        self,
        request_id: str,
        *,
        case_id: str,
        place_id: str,
        album_id: str,
        submitter_id: str,
        submitter_role: str,
        fact_patch: dict[str, Any],
        evidence_summary: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """商家/读者提交事实更正；载荷触碰观点域一律拒绝。"""
        self._begin(request_id)
        registry = self.registry()
        if place_id not in registry.places:
            raise NotFound(f"地点尚未登记: {place_id}")
        self._album(registry, album_id)
        if submitter_role not in {MERCHANT_ROLE, READER_ROLE}:
            raise FreshnessError("submitter_role 必须是 merchant 或 reader")
        if not isinstance(fact_patch, dict) or not fact_patch:
            raise FreshnessError("事实更正必须携带非空 fact_patch")
        forbidden = set(fact_patch) & OPINION_KEYS
        if forbidden:
            raise OpinionNotEditable(f"事实更正不得携带用户观点字段: {sorted(forbidden)}")
        if case_id in registry.cases:
            raise FreshnessError(f"案件已存在: {case_id}")

        payload: dict[str, Any] = {
            "case_id": case_id,
            "place_id": place_id,
            "album_id": album_id,
            "submitter_id": submitter_id,
            "submitter_role": submitter_role,
            "fact_patch": dict(fact_patch),
        }
        if evidence_summary is not None:
            payload["evidence_summary"] = evidence_summary
        batch = _Batch(request_id, self.store, self.clock)
        batch.add("CORRECTION_SUBMITTED", case_id, payload)
        return self._commit(batch)

    def decide_correction(
        self,
        request_id: str,
        *,
        case_id: str,
        reviewer_id: str,
        decision: str,
        reason: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._begin(request_id)
        registry = self.registry()
        try:
            case = registry.cases[case_id]
        except KeyError:
            raise NotFound(f"案件不存在: {case_id}") from None
        if case.status != "open":
            raise FreshnessError(f"案件已裁决: {case.status}")
        if decision not in {"accepted", "rejected"}:
            raise FreshnessError("decision 必须是 accepted 或 rejected")
        if reviewer_id == case.submitter_id:
            raise ConflictOfInterest("案件提交者不能裁决自己提交的纠错")

        album = registry.albums.get(case.album_id)
        if album is not None and reviewer_id in album.editors:
            raise ConflictOfInterest("审核员参与过该专辑编辑，必须回避")

        batch = _Batch(request_id, self.store, self.clock)
        if decision == "accepted":
            # 只把事实补丁写入地点，用户观点原封不动。
            batch.add(
                "PLACE_CHANGED",
                case.place_id,
                {
                    "place_version": self.store.version(case.place_id) + 1,
                    "changed_facts": dict(case.fact_patch),
                    "origin_case_id": case_id,
                },
            )
        decide_payload: dict[str, Any] = {
            "case_id": case_id,
            "decided_by": reviewer_id,
            "decision": decision,
        }
        if reason is not None:
            decide_payload["reason"] = reason
        batch.add("CORRECTION_DECIDED", case_id, decide_payload)
        return self._commit(batch)

    # ----------------------------------------------------------------- 分享快照

    @staticmethod
    def _attachment_raw(attachment) -> dict[str, Any]:
        return {
            "attachment_id": attachment.attachment_id,
            "summary": attachment.summary,
            "uploader_id": attachment.uploader_id,
            "pii": attachment.pii,
        }

    def share_snapshot(
        self,
        request_id: str,
        *,
        album_id: str,
        sharer_id: str,
        recipient_scope: str,
        entry_ids: Optional[list[str]] = None,
        snapshot_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """固化原文快照；之后任何更新/撤回都不改写它。"""
        self._begin(request_id)
        registry = self.registry()
        album = self._album(registry, album_id)
        self._require_member(album, sharer_id)
        chosen = entry_ids or sorted(album.entries)
        raw_entries: list[dict[str, Any]] = []
        for entry_id in chosen:
            entry = album.entries.get(entry_id)
            if entry is None:
                raise NotFound(f"条目不存在: {entry_id}")
            place = registry.places.get(entry.place_id)
            raw_entries.append(
                {
                    "entry_id": entry.entry_id,
                    "place_id": entry.place_id,
                    "author_id": entry.author_id,
                    "experience_at": entry.experience_at,
                    "recommendation": entry.recommendation,
                    "audience_conditions": list(entry.audience_conditions),
                    "negative_experience": entry.negative_experience,
                    "identity_statement": entry.identity_statement,
                    "place_name": place.name if place else "",
                    "place_version": entry.anchored_place_version,
                    "facts": dict(place.facts) if place else {},
                    "attachments": [self._attachment_raw(item) for item in entry.attachments],
                    "entry_event_id": entry.added_event_id,
                    "endorsed_at_share": entry.endorsed,
                }
            )
        snapshot_hash = hashlib.sha256(
            canonical_json(raw_entries).encode("utf-8")
        ).hexdigest()
        snapshot_id = snapshot_id or f"snapshot-{request_id}"
        if snapshot_id in registry.snapshots:
            raise SnapshotImmutable(f"分享快照不可修改: {snapshot_id}")
        batch = _Batch(request_id, self.store, self.clock)
        batch.add(
            "SNAPSHOT_SHARED",
            snapshot_id,
            {
                "album_id": album_id,
                "sharer_id": sharer_id,
                "recipient_scope": recipient_scope,
                "snapshot_hash": snapshot_hash,
                "entries": raw_entries,
            },
        )
        committed = self._commit(batch)
        return {"snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash, "event": committed[0]}

    # ----------------------------------------------------------------- 阅读视图

    def _entry_view(self, album, registry: Registry, entry, viewer_id: Optional[str]) -> dict[str, Any]:
        view = {
            "entry_id": entry.entry_id,
            "place_id": entry.place_id,
            "author_id": entry.author_id,
            "identity_statement": entry.identity_statement,
            "experience_at": entry.experience_at,
            "recommendation": entry.recommendation,
            "audience_conditions": list(entry.audience_conditions),
            "negative_experience": entry.negative_experience,
            "anchored_place_version": entry.anchored_place_version,
            "endorsed": entry.endorsed,
            "withdraw_reason": entry.withdraw_reason,
            "frozen": entry.frozen,
            "facts_changes": changes_since(registry, entry.place_id, entry.anchored_place_version),
            "attachments": [
                redact_attachment(self._attachment_raw(item), album, viewer_id, entry.author_id)
                for item in entry.attachments
            ],
        }
        return view

    def read_album(self, album_id: str, viewer_id: Optional[str] = None) -> dict[str, Any]:
        registry = self.registry()
        album = self._album(registry, album_id)
        return {
            "album_id": album_id,
            "title": album.title,
            "owner_id": album.owner_id,
            "identity_statement": album.identity_statement,
            "entries": [
                self._entry_view(album, registry, entry, viewer_id)
                for entry in album.entries.values()
            ],
        }

    def read_snapshot(self, snapshot_id: str, viewer_id: Optional[str] = None) -> dict[str, Any]:
        """读快照：原文不动，附加当前时效标注（失效地点、事实变化、作者是否仍认可）。"""
        registry = self.registry()
        snapshot = registry.snapshots.get(snapshot_id)
        if snapshot is None:
            raise NotFound(f"快照不存在: {snapshot_id}")
        album = registry.albums.get(snapshot.album_id)

        entries_view: list[dict[str, Any]] = []
        for item in snapshot.entries:
            place = registry.places.get(item.place_id)
            current_entry = None
            if album is not None:
                current_entry = album.entries.get(item.entry_id)
            lifecycle = place_lifecycle(registry, item.place_id) if place else {
                "place_id": item.place_id,
                "state": "unknown",
            }
            old_facts = place.facts_at(item.place_version) if place else dict(item.facts)
            annotations = {
                "place_lifecycle": lifecycle,
                "changed_facts": None if place is None else diff_facts(old_facts, place.facts)["changed"],
                "author_endorses_now": None if current_entry is None else current_entry.endorsed,
                "withdraw_reason": None if current_entry is None else current_entry.withdraw_reason,
                "citation_migrated": (
                    current_entry.place_id != item.place_id if current_entry is not None else False
                ),
            }
            entries_view.append(
                {
                    "original": {
                        "entry_id": item.entry_id,
                        "place_id": item.place_id,
                        "place_name": item.place_name,
                        "place_version": item.place_version,
                        "facts": dict(item.facts),
                        "author_id": item.author_id,
                        "experience_at": item.experience_at,
                        "recommendation": item.recommendation,
                        "audience_conditions": list(item.audience_conditions),
                        "negative_experience": item.negative_experience,
                        "identity_statement": item.identity_statement,
                        "attachments": [
                            redact_attachment(dict(raw), album, viewer_id, item.author_id)
                            for raw in item.attachments
                        ],
                    },
                    "annotations": annotations,
                }
            )

        return {
            "snapshot_id": snapshot_id,
            "album_id": snapshot.album_id,
            "sharer_id": snapshot.sharer_id,
            "recipient_scope": snapshot.recipient_scope,
            "snapshot_hash": snapshot.snapshot_hash,
            "shared_at": snapshot.created_at,
            "entries": entries_view,
        }
