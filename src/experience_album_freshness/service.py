"""生活经验专辑时效台：业务服务。

把机械规则（:mod:`store`）与读模型（:mod:`model`）组装成领域用例：

- 作者与协作者维护条目；并发改同一条目必须先撞乐观锁、再走显式合并。
- 分享快照冻结原文；后续地点失效只做标注，不改写快照。
- 地点合并只迁移被确认的条目/评论引用，旧门店体验留在原地。
- 商家只能提交事实更正，观点字段一律拒绝；审核员参与过的专辑回避。
- 阅读接口同时给出亲历时间、已变化事实与作者当前是否认可，
  附件个人信息按角色遮蔽。

所有写操作都强制要求 ``request_id``：相同请求只执行一次，
重试/重启重放返回首次事件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from .access import AttachmentSummary, Identity, Role, redact_attachments
from .errors import (
    FactCorrectionBoundaryError,
    MergeRequiredError,
    NotFound,
    PermissionDenied,
    ReviewerConflict,
    ValidationError,
)
from .model import (
    ENTRY_UPDATABLE_FIELDS,
    FACT_KEYS,
    OPINION_FIELDS,
    RECOMMENDATIONS,
    AlbumState,
    Comment,
    CorrectionCase,
    Entry,
    Place,
    Repository,
    Snapshot,
)
from .store import EventStore, StoredEvent, content_fingerprint


def _entry_stream(album_id: str, entry_id: str) -> str:
    return f"{album_id}::entry::{entry_id}"


def _place_stream(album_id: str, place_id: str) -> str:
    return f"{album_id}::place::{place_id}"


def _snapshot_stream(album_id: str, snapshot_id: str) -> str:
    return f"{album_id}::snapshot::{snapshot_id}"


def _case_stream(album_id: str, case_id: str) -> str:
    return f"{album_id}::case::{case_id}"


def _require_request(request_id: str | None) -> str:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValidationError("写操作必须携带 request_id，用于幂等去重")
    return request_id


def _require_tz(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} 必须是携带时区的 datetime")
    return value


def _attachment_payloads(attachments: Iterable[AttachmentSummary | Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in attachments or []:
        if isinstance(item, AttachmentSummary):
            result.append(item.to_payload())
        elif isinstance(item, Mapping):
            result.append(AttachmentSummary.from_payload(item).to_payload())
        else:
            raise ValidationError("附件必须是 AttachmentSummary 或对象")
    return result


class AlbumService:
    def __init__(self, store: EventStore, clock) -> None:
        self._store = store
        self._clock = clock
        self._repo = Repository(store)

    # ================================================================ 专辑

    def create_album(self, identity: Identity, album_id: str, name: str | None = None, *, request_id: str) -> StoredEvent:
        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        if self._repo.get_album(album_id) is not None:
            raise ValidationError("专辑已存在", context={"album_id": album_id})
        return self._store.append(
            event_type="ALBUM_CREATED",
            aggregate_type="experience_album",
            aggregate_id=album_id,
            payload={"name": name},
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def invite_collaborator(self, identity: Identity, album_id: str, user_id: str, *, request_id: str) -> StoredEvent:
        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_author(identity, state)
        return self._store.append(
            event_type="COLLABORATOR_INVITED",
            aggregate_type="experience_album",
            aggregate_id=album_id,
            payload={"user_id": user_id},
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    # ================================================================ 地点

    def register_place(
        self,
        identity: Identity,
        album_id: str,
        place_id: str,
        *,
        name: str,
        facts: Mapping[str, Any] | None = None,
        request_id: str,
    ) -> StoredEvent:
        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        if place_id in state.places:
            raise ValidationError("地点已注册", context={"place_id": place_id})
        facts = dict(facts or {})
        self._reject_opinion_keys(facts)
        return self._store.append(
            event_type="PLACE_REGISTERED",
            aggregate_type="experience_album",
            aggregate_id=_place_stream(album_id, place_id),
            payload={"album_id": album_id, "place_id": place_id, "name": name, "facts": facts, "place_version": 1},
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def record_place_change(
        self,
        identity: Identity,
        album_id: str,
        place_id: str,
        *,
        changed_facts: Mapping[str, Any] | None = None,
        removed_facilities: Iterable[str] = (),
        change_kind: str = "other",
        request_id: str,
    ) -> StoredEvent:
        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        place = self._place(state, place_id)
        changed_facts = dict(changed_facts or {})
        self._reject_opinion_keys(changed_facts)
        removed = list(removed_facilities)
        return self._append_place_change(
            state, place, changed_facts=changed_facts, removed=removed,
            change_kind=change_kind, actor=identity.user_id, request_id=request_id,
        )

    def merge_places(
        self,
        identity: Identity,
        album_id: str,
        old_place_id: str,
        new_place_id: str,
        *,
        confirmed_entry_ids: Iterable[str] = (),
        confirmed_comment_ids: Iterable[str] = (),
        request_id: str,
    ) -> StoredEvent:
        """合并门店：只迁移显式确认仍指向同一主体的引用。

        未确认的条目与评论保留在旧门店，针对旧门店的体验不会被吞掉。
        """

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        old = self._place(state, old_place_id)
        self._place(state, new_place_id)
        if old.status == "merged":
            raise ValidationError("旧地点已被合并", context={"place_id": old_place_id})

        confirmed_entries = list(confirmed_entry_ids)
        confirmed_comments = list(confirmed_comment_ids)
        for entry_id in confirmed_entries:
            entry = state.entries.get(entry_id)
            if entry is None:
                raise ValidationError(f"确认迁移的条目不存在: {entry_id}")
            if entry.place_id != old_place_id:
                raise ValidationError(
                    f"条目 {entry_id} 当前并不指向旧门店 {old_place_id}，不能随合并迁移",
                    context={"entry_id": entry_id, "place_id": entry.place_id},
                )
        for comment_id in confirmed_comments:
            comment = state.comments.get(comment_id)
            if comment is None:
                raise ValidationError(f"确认迁移的评论不存在: {comment_id}")
            if comment.place_id != old_place_id:
                raise ValidationError(
                    f"评论 {comment_id} 当前并不指向旧门店 {old_place_id}，不能随合并迁移",
                    context={"comment_id": comment_id, "place_id": comment.place_id},
                )

        return self._store.append(
            event_type="PLACE_MERGED",
            aggregate_type="experience_album",
            aggregate_id=_place_stream(album_id, old_place_id),
            payload={
                "album_id": album_id,
                "old_place_id": old_place_id,
                "new_place_id": new_place_id,
                "confirmed_entry_ids": confirmed_entries,
                "confirmed_comment_ids": confirmed_comments,
            },
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    # ================================================================ 条目

    def add_entry(
        self,
        identity: Identity,
        album_id: str,
        entry_id: str,
        place_id: str,
        *,
        title: str,
        experience_at: datetime,
        audience_conditions: str,
        recommendation: str = "neutral",
        recommendation_reason: str = "",
        negative_experience: str | None = None,
        attachments: Iterable[AttachmentSummary | Mapping[str, Any]] | None = None,
        request_id: str,
    ) -> StoredEvent:
        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        self._place(state, place_id)
        if entry_id in state.entries:
            raise ValidationError("条目已存在", context={"entry_id": entry_id})
        _require_tz(experience_at, "experience_at")
        if recommendation not in RECOMMENDATIONS:
            raise ValidationError(f"recommendation 必须是 {sorted(RECOMMENDATIONS)} 之一")
        if not audience_conditions.strip():
            raise ValidationError("适用条件 audience_conditions 不能为空")

        place = state.places[place_id]
        payload = {
            "album_id": album_id,
            "entry_id": entry_id,
            "author_id": identity.user_id,
            "place_id": place_id,
            "place_version": place.version,
            "title": title,
            "experience_at": experience_at.isoformat(),
            "audience_conditions": audience_conditions,
            "recommendation": recommendation,
            "recommendation_reason": recommendation_reason,
            "negative_experience": negative_experience,
            "attachments": _attachment_payloads(attachments),
        }
        return self._store.append(
            event_type="ENTRY_ADDED",
            aggregate_type="experience_album",
            aggregate_id=_entry_stream(album_id, entry_id),
            payload=payload,
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def update_entry(
        self,
        identity: Identity,
        album_id: str,
        entry_id: str,
        fields: Mapping[str, Any],
        *,
        expected_version: int,
        request_id: str,
    ) -> StoredEvent:
        """更新条目判断/理由/适用条件等。

        必须基于读取到的 ``entry_version`` 提交；落后于当前版本时抛
        :class:`MergeRequiredError`（携带当前状态），调用方解决冲突后
        走 :meth:`resolve_entry_merge`。
        """

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        entry = self._entry(state, entry_id)
        clean = self._clean_entry_fields(fields)
        payload = {
            "album_id": album_id,
            "entry_id": entry_id,
            "place_id": entry.place_id,
            "place_version": entry.place_version,
            **clean,
        }
        # 作者本人更新判断意味着仍然认可；协作者编辑不动作者的认可立场。
        if identity.user_id == entry.author_id:
            payload["endorsed"] = True

        try:
            return self._store.append(
                event_type="ENTRY_UPDATED",
                aggregate_type="experience_album",
                aggregate_id=_entry_stream(album_id, entry_id),
                payload=payload,
                occurred_at=self._clock.now(),
                actor=identity.user_id,
                request_id=request_id,
                expected_version=expected_version,
            )
        except Exception as exc:
            if getattr(exc, "code", None) == "concurrency_conflict":
                raise MergeRequiredError(
                    aggregate_id=_entry_stream(album_id, entry_id),
                    expected=expected_version,
                    current=exc.current_version,
                    current_state=self.entry_view(identity, album_id, entry_id),
                ) from exc
            raise

    def resolve_entry_merge(
        self,
        identity: Identity,
        album_id: str,
        entry_id: str,
        resolved_fields: Mapping[str, Any],
        *,
        base_version: int,
        request_id: str,
    ) -> StoredEvent:
        """协作者并发修改后的显式合并落点。

        ``resolved_fields`` 必须是合并后的完整条目内容；``base_version``
        是冲突方读取时所基于的版本，用于审计“谁合并了谁的改动”。
        """

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        entry = self._entry(state, entry_id)
        if base_version >= entry.entry_version:
            raise ValidationError(
                "基线版本不落后于当前版本，不存在需要显式合并的冲突",
                context={"base_version": base_version, "current_version": entry.entry_version},
            )
        missing = set(ENTRY_UPDATABLE_FIELDS) - set(resolved_fields)
        if missing:
            raise ValidationError(f"显式合并必须给出完整条目内容，缺少: {sorted(missing)}")
        clean = self._clean_entry_fields(resolved_fields)
        payload = {
            "album_id": album_id,
            "entry_id": entry_id,
            "place_id": entry.place_id,
            "place_version": entry.place_version,
            "base_version": base_version,
            "conflicting_with_version": entry.entry_version,
            **clean,
        }
        if identity.user_id == entry.author_id:
            payload["endorsed"] = True
        return self._store.append(
            event_type="ENTRY_MERGE_RESOLVED",
            aggregate_type="experience_album",
            aggregate_id=_entry_stream(album_id, entry_id),
            payload=payload,
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def author_withdraw_endorsement(
        self,
        identity: Identity,
        album_id: str,
        entry_id: str,
        reason: str,
        *,
        request_id: str,
    ) -> StoredEvent:
        """作者撤回认可。旧快照原文保留，只改“作者是否仍认可”。"""

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        entry = self._entry(state, entry_id)
        if identity.user_id != entry.author_id:
            raise PermissionDenied("只有条目作者本人可以撤回认可")
        return self._store.append(
            event_type="AUTHOR_WITHDREW",
            aggregate_type="experience_album",
            aggregate_id=_entry_stream(album_id, entry_id),
            payload={"album_id": album_id, "entry_id": entry_id, "reason": reason},
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    # ================================================================ 评论

    def add_comment(
        self,
        identity: Identity,
        album_id: str,
        *,
        comment_id: str,
        entry_id: str,
        quote: str,
        request_id: str,
    ) -> StoredEvent:
        """评论必须引用原文片段，并钉住评论时的地点与地点版本。"""

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        entry = self._entry(state, entry_id)
        if not quote.strip():
            raise ValidationError("评论引用 quote 不能为空")
        if comment_id in state.comments:
            raise ValidationError("评论已存在", context={"comment_id": comment_id})
        return self._store.append(
            event_type="COMMENT_ADDED",
            aggregate_type="experience_album",
            aggregate_id=album_id,
            payload={
                "album_id": album_id,
                "comment_id": comment_id,
                "entry_id": entry_id,
                "quote": quote,
                "place_id": entry.place_id,
                "place_version": entry.place_version,
            },
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    # ================================================================ 快照

    def share_snapshot(
        self,
        identity: Identity,
        album_id: str,
        entry_id: str,
        *,
        snapshot_id: str,
        recipient_scope: str,
        request_id: str,
    ) -> StoredEvent:
        """把条目当前原文与地点事实冻结成分享快照。"""

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        self._require_editor(identity, state)
        entry = self._entry(state, entry_id)
        if not recipient_scope.strip():
            raise ValidationError("recipient_scope 不能为空")
        place = state.places.get(entry.place_id)
        pinned_facts = place.facts_at(entry.place_version) if place else {}
        content = {
            "title": entry.title,
            "recommendation": entry.recommendation,
            "recommendation_reason": entry.recommendation_reason,
            "audience_conditions": entry.audience_conditions,
            "negative_experience": entry.negative_experience,
            "experience_at": entry.experience_at,
            "place": {
                "place_id": entry.place_id,
                "place_version": entry.place_version,
                "name": place.name if place else entry.place_id,
                "facts": pinned_facts,
            },
            "attachments": [item.to_payload() for item in entry.attachments],
        }
        snapshot_hash = content_fingerprint("SNAPSHOT_SHARED", content)
        payload = {
            "album_id": album_id,
            "snapshot_id": snapshot_id,
            "entry_id": entry_id,
            "recipient_scope": recipient_scope,
            "snapshot_hash": snapshot_hash,
            "title": entry.title,
            "content": content,
            "place_versions": {entry.place_id: entry.place_version},
            "endorsed_at_share": entry.endorsed,
            "shared_by": identity.user_id,
        }
        return self._store.append(
            event_type="SNAPSHOT_SHARED",
            aggregate_type="shared_snapshot",
            aggregate_id=_snapshot_stream(album_id, snapshot_id),
            payload=payload,
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def snapshot_view(self, identity: Identity, album_id: str, snapshot_id: str) -> dict[str, Any]:
        """阅读者看到的快照：原文不动，附当前失效标注。"""

        state = self._album(album_id)
        snapshot = state.snapshots.get(snapshot_id)
        if snapshot is None:
            raise NotFound("快照不存在", context={"snapshot_id": snapshot_id})
        self._require_snapshot_read(identity, state, snapshot)

        content = dict(snapshot.content)
        content["attachments"] = redact_attachments(
            [AttachmentSummary.from_payload(item) for item in content.get("attachments", [])],
            identity if self._is_privileged(identity, state) else None,
        )
        staleness = self._snapshot_staleness(state, snapshot)
        entry = state.entries.get(snapshot.entry_id)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "shared_at": snapshot.shared_at,
            "shared_by": snapshot.shared_by,
            "recipient_scope": snapshot.recipient_scope,
            "snapshot_hash": snapshot.snapshot_hash,
            "original_content": content,
            "place_versions_pinned": snapshot.place_versions,
            "endorsed_at_share": snapshot.endorsed_at_share,
            "author_endorses_now": entry.endorsed if entry else None,
            "withdrew_reason": entry.withdrew_reason if entry else None,
            "stale": staleness["stale"],
            "staleness": staleness["items"],
        }

    # ================================================================ 纠错

    def submit_correction(
        self,
        identity: Identity,
        album_id: str,
        *,
        case_id: str,
        place_id: str,
        factual_patches: Mapping[str, Any],
        removed_facilities: Iterable[str] = (),
        rationale: str = "",
        request_id: str,
    ) -> StoredEvent:
        """商家提交事实更正（营业时间、设施撤除等）。

        只允许事实键；任何用户观点字段越界都会被拒绝，观点不会被删除。
        """

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        if not identity.has(Role.MERCHANT):
            raise PermissionDenied("只有商家角色可以提交事实更正")
        self._place(state, place_id)
        patches = dict(factual_patches)
        offending = sorted(set(patches) & OPINION_FIELDS)
        unknown = sorted(key for key in patches if key not in FACT_KEYS and key not in OPINION_FIELDS)
        if offending or unknown:
            raise FactCorrectionBoundaryError(
                "事实更正只能修改营业时间/设施等事实字段，禁止改动推荐、理由、负面体验、适用条件等用户观点",
                context={"opinion_keys": offending, "unknown_keys": unknown},
            )
        removed = list(removed_facilities)
        if _case_stream(album_id, case_id) in {e.aggregate_id for e in self._store.events}:
            raise ValidationError("申诉已存在", context={"case_id": case_id})

        return self._store.append(
            event_type="CORRECTION_SUBMITTED",
            aggregate_type="correction_case",
            aggregate_id=_case_stream(album_id, case_id),
            payload={
                "album_id": album_id,
                "case_id": case_id,
                "subject_type": "place",
                "subject_id": place_id,
                "submitter_id": identity.user_id,
                "submitter_role": Role.MERCHANT.value,
                "factual_patches": patches,
                "removed_facilities": removed,
                "rationale": rationale,
            },
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
        )

    def decide_correction(
        self,
        identity: Identity,
        album_id: str,
        case_id: str,
        *,
        decision: str,
        rationale: str = "",
        request_id: str,
    ) -> StoredEvent:
        """审核员裁决纠错申诉。

        - 参与过该专辑编辑（含此前在该专辑落地过事实更正）的审核员回避；
        - 接受事实更正只落地事实，不删除任何用户观点；
        - 裁决幂等：同一 request_id 重放返回首次事件。
        """

        request_id = _require_request(request_id)
        replayed = self._store.get_by_request(request_id)
        if replayed is not None:
            return replayed
        state = self._album(album_id)
        if not identity.has(Role.REVIEWER):
            raise PermissionDenied("只有审核员角色可以裁决纠错申诉")
        case = state.cases.get(case_id)
        if case is None:
            raise NotFound("纠错申诉不存在", context={"case_id": case_id})
        if decision not in ("accepted", "rejected"):
            raise ValidationError("decision 必须是 accepted 或 rejected")

        # 崩溃恢复：事实变化已落地、裁决事件尚未写入时，重放必须能走完，
        # 而不是被“审核员已参与编辑”的回避规则挡死。
        place_request_id = f"{request_id}:place"
        recovery_change = self._store.get_by_request(place_request_id)
        recovering = recovery_change is not None
        if not recovering:
            if case.status != "pending":
                raise ValidationError(
                    "申诉已裁决，不能重复处理",
                    context={"case_id": case_id, "status": case.status},
                )
            if identity.user_id in state.edit_participants or identity.user_id == case.submitter_id:
                raise ReviewerConflict(
                    "审核员参与过该专辑编辑或即提交人，必须回避",
                    context={"reviewer": identity.user_id, "album_id": album_id, "case_id": case_id},
                )
        elif recovery_change.payload.get("origin_case_id") != case_id:
            raise ValidationError("恢复中的请求标识属于另一申诉，拒绝继续", context={"case_id": case_id})

        applied_place_version: int | None = None
        if decision == "accepted":
            place = self._place(state, case.subject_id)
            if recovering:
                applied_place_version = recovery_change.version
            else:
                # 先落地事实变化（独立派生幂等键，崩溃重试不会漏事件）。
                change = self._append_place_change(
                    state,
                    place,
                    changed_facts=dict(case.factual_patches),
                    removed=list(case.removed_facilities),
                    change_kind="correction_accepted",
                    actor=identity.user_id,
                    request_id=place_request_id,
                    origin_case_id=case_id,
                )
                applied_place_version = change.version

        return self._store.append(
            event_type="CORRECTION_DECIDED",
            aggregate_type="correction_case",
            aggregate_id=_case_stream(album_id, case_id),
            payload={
                "album_id": album_id,
                "case_id": case_id,
                "decision": decision,
                "rationale": rationale,
                "accepted_facts": dict(case.factual_patches) if decision == "accepted" else {},
                "removed_facilities": list(case.removed_facilities) if decision == "accepted" else [],
                "applied_place_version": applied_place_version,
                "decided_by": identity.user_id,
            },
            occurred_at=self._clock.now(),
            actor=identity.user_id,
            request_id=request_id,
            expected_version=1,
        )

    # ================================================================ 阅读

    def album_view(self, identity: Identity | None, album_id: str) -> dict[str, Any]:
        state = self._album(album_id)
        return {
            "album_id": state.album_id,
            "name": state.name,
            "author_id": state.author_id,
            "collaborators": sorted(state.collaborators),
            "entries": [self.entry_view(identity, album_id, entry_id) for entry_id in sorted(state.entries)],
        }

    def entry_view(self, identity: Identity | None, album_id: str, entry_id: str) -> dict[str, Any]:
        """阅读者视角：基于何时的体验、哪些事实已变化、作者是否仍认可。"""

        state = self._album(album_id)
        entry = self._entry(state, entry_id)
        place = state.places.get(entry.place_id)
        changed = place.changes_since(entry.place_version) if place else []
        # 折叠模型内部记 removed_facts，阅读接口统一用领域语言 removed_facilities。
        changed = [
            {
                **item,
                "removed_facilities": item.get("removed_facts", []),
            }
            for item in changed
        ]
        stale_reasons = self._stale_reasons(place, changed)
        accepted_cases = [
            {
                "case_id": case.case_id,
                "decided_at": case.decided_at,
                "facts": case.accepted_facts,
                "removed_facilities": case.removed_facilities,
            }
            for case in state.cases.values()
            if case.status == "accepted" and case.subject_id == entry.place_id
        ]
        return {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "experience_at": entry.experience_at,
            "audience_conditions": entry.audience_conditions,
            "recommendation": entry.recommendation,
            "recommendation_reason": entry.recommendation_reason,
            "negative_experience": entry.negative_experience,
            "based_on_place": {
                "place_id": entry.place_id,
                "place_version": entry.place_version,
                "place_name": place.name if place else entry.place_id,
                "place_status": place.status if place else "unknown",
                "merged_into": place.merged_into if place else None,
            },
            "current_place_version": place.version if place else entry.place_version,
            "changed_facts_since_experience": changed,
            "accepted_corrections": accepted_cases,
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "author_endorses": entry.endorsed,
            "withdrew_reason": entry.withdrew_reason,
            "entry_version": entry.entry_version,
            "updated_at": entry.updated_at,
            "attachments": redact_attachments(entry.attachments, identity if self._is_privileged(identity, state) else None),
            "comments": [
                {
                    "comment_id": comment.comment_id,
                    "actor": comment.actor,
                    "quote": comment.quote,
                    "pinned_place": {"place_id": comment.place_id, "place_version": comment.place_version},
                    "migrated_by_place_merge": comment.migrated,
                    "created_at": comment.created_at,
                }
                for comment in entry.comments
            ],
        }

    # ================================================================ 内部

    def _append_place_change(
        self,
        state: AlbumState,
        place: Place,
        *,
        changed_facts: dict[str, Any],
        removed: list[str],
        change_kind: str,
        actor: str,
        request_id: str,
        origin_case_id: str | None = None,
    ) -> StoredEvent:
        if place.status == "merged":
            raise ValidationError("地点已合并，不能再追加事实变化", context={"place_id": place.place_id})
        next_version = place.version + 1
        payload: dict[str, Any] = {
            "album_id": state.album_id,
            "place_id": place.place_id,
            "place_version": next_version,
            "changed_facts": changed_facts,
            "removed_facilities": removed,
            "change_kind": change_kind,
        }
        if origin_case_id:
            payload["origin_case_id"] = origin_case_id
        event = self._store.append(
            event_type="PLACE_CHANGED",
            aggregate_type="experience_album",
            aggregate_id=_place_stream(state.album_id, place.place_id),
            payload=payload,
            occurred_at=self._clock.now(),
            actor=actor,
            request_id=request_id,
        )
        place.version = next_version  # 供同一次调用内的后续步骤读取
        return event

    @staticmethod
    def _reject_opinion_keys(values: Mapping[str, Any]) -> None:
        offending = sorted(set(values) & OPINION_FIELDS)
        if offending:
            raise FactCorrectionBoundaryError(
                "事实通道禁止携带用户观点字段",
                context={"opinion_keys": offending},
            )

    @staticmethod
    def _clean_entry_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(fields) - ENTRY_UPDATABLE_FIELDS)
        if unknown:
            raise ValidationError(f"存在不可更新的字段: {unknown}")
        clean = dict(fields)
        if "recommendation" in clean and clean["recommendation"] not in RECOMMENDATIONS:
            raise ValidationError(f"recommendation 必须是 {sorted(RECOMMENDATIONS)} 之一")
        if "audience_conditions" in clean and not str(clean["audience_conditions"]).strip():
            raise ValidationError("适用条件 audience_conditions 不能为空")
        if "experience_at" in clean:
            value = clean["experience_at"]
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValidationError("experience_at 必须是携带时区的 datetime")
            clean["experience_at"] = value.isoformat()
        if "attachments" in clean:
            clean["attachments"] = _attachment_payloads(clean["attachments"])
        return clean

    def _album(self, album_id: str) -> AlbumState:
        state = self._repo.get_album(album_id)
        if state is None:
            raise NotFound("专辑不存在", context={"album_id": album_id})
        return state

    @staticmethod
    def _entry(state: AlbumState, entry_id: str) -> Entry:
        entry = state.entries.get(entry_id)
        if entry is None:
            raise NotFound("条目不存在", context={"entry_id": entry_id})
        return entry

    @staticmethod
    def _place(state: AlbumState, place_id: str) -> Place:
        place = state.places.get(place_id)
        if place is None:
            raise NotFound("地点不存在", context={"place_id": place_id})
        return place

    @staticmethod
    def _require_author(identity: Identity, state: AlbumState) -> None:
        if identity.user_id != state.author_id:
            raise PermissionDenied("只有专辑作者可以执行此操作")

    @staticmethod
    def _require_editor(identity: Identity, state: AlbumState) -> None:
        if identity.user_id != state.author_id and identity.user_id not in state.collaborators:
            raise PermissionDenied("需要作者或协作者身份")

    @staticmethod
    def _is_privileged(identity: Identity | None, state: AlbumState) -> bool:
        if identity is None:
            return False
        if identity.user_id == state.author_id or identity.user_id in state.collaborators:
            return True
        return bool(identity.roles & {Role.REVIEWER})

    @staticmethod
    def _require_snapshot_read(identity: Identity, state: AlbumState, snapshot: Snapshot) -> None:
        if AlbumService._is_privileged(identity, state):
            return
        if identity is not None and identity.has(Role.SNAPSHOT_RECIPIENT):
            if snapshot.recipient_scope == "public" or snapshot.recipient_scope in getattr(identity, "scopes", frozenset()):
                return
        raise PermissionDenied("无权查看该分享快照")

    @staticmethod
    def _stale_reasons(place: Place | None, changes: list[dict[str, Any]]) -> list[str]:
        reasons: list[str] = []
        if place is None:
            return reasons
        if place.status == "closed":
            reasons.append("place_closed")
        if place.status == "merged":
            reasons.append("place_merged")
        for item in changes:
            changed = item.get("changed_facts", {})
            if "hours" in changed:
                reasons.append("hours_changed")
            if "smoke_free" in changed:
                reasons.append("smoke_free_status_changed")
            if item.get("removed_facts") or item.get("removed_facilities"):
                reasons.append("facilities_removed")
            if "facilities" in changed:
                reasons.append("facilities_changed")
            if "status" in changed and changed["status"] == "closed":
                reasons.append("place_closed")
        return sorted(set(reasons))

    def _snapshot_staleness(self, state: AlbumState, snapshot: Snapshot) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for place_id, pinned_version in snapshot.place_versions.items():
            place = state.places.get(place_id)
            if place is None:
                items.append({"place_id": place_id, "kind": "place_missing", "pinned_version": pinned_version})
                continue
            for change in place.changes_since(pinned_version):
                items.append(
                    {
                        "place_id": place_id,
                        "kind": "facts_changed",
                        "from_version": pinned_version,
                        "to_version": change["place_version"],
                        "changed_facts": change.get("changed_facts", {}),
                        "removed_facilities": change.get("removed_facts", []),
                        "occurred_at": change.get("occurred_at"),
                    }
                )
            if place.status in ("closed", "merged"):
                items.append(
                    {
                        "place_id": place_id,
                        "kind": f"place_{place.status}",
                        "pinned_version": pinned_version,
                        "current_version": place.version,
                        "merged_into": place.merged_into,
                    }
                )
        return {"stale": bool(items), "items": items}
