"""事件存储：幂等、版本冻结、乐观并发与重启持久化。

存储层只管三条机械规则，不理解任何业务语义：

1. 相同 ``request_id`` 的请求只追加一次；同号异体抛
   :class:`IdempotencyConflict`。
2. 同一聚合同一版本号只能对应一份内容；重复且一致视为重放（跳过），
   重复但内容不同立即冻结该聚合事件流（:class:`VersionFrozenError`），
   冻结后拒绝一切追加，等待人工介入。
3. 命令带 ``expected_version`` 时做乐观并发检查，落后则抛
   :class:`ConcurrencyConflictError`，逼迫上层走显式合并。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .errors import (
    ConcurrencyConflictError,
    IdempotencyConflict,
    VersionFrozenError,
)


def canonical_json(value: Any) -> str:
    """稳定序列化，作为内容指纹与请求指纹的基础。"""

    def normalize(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Mapping):
            return {k: normalize(obj[k]) for k in sorted(obj)}
        if isinstance(obj, (list, tuple)):
            return [normalize(item) for item in obj]
        return obj

    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_fingerprint(event_type: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"event_type": event_type, "payload": payload}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: dict[str, Any]
    actor: str | None = None
    request_id: str | None = None

    def to_envelope(self) -> dict[str, Any]:
        """对外交换信封（严格符合 domain.schema.json）。"""

        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": self.payload,
        }


@dataclass
class _RequestRecord:
    fingerprint: str
    event_id: str


class EventStore:
    def __init__(self) -> None:
        self._events: list[StoredEvent] = []
        self._requests: dict[str, _RequestRecord] = {}
        self._frozen: set[str] = set()

    # ------------------------------------------------------------------ 查询

    @property
    def events(self) -> list[StoredEvent]:
        return list(self._events)

    def events_for(self, aggregate_id: str) -> list[StoredEvent]:
        return [event for event in self._events if event.aggregate_id == aggregate_id]

    def current_version(self, aggregate_id: str) -> int:
        return max(
            (event.version for event in self._events if event.aggregate_id == aggregate_id),
            default=0,
        )

    def is_frozen(self, aggregate_id: str) -> bool:
        return aggregate_id in self._frozen

    def get_by_request(self, request_id: str) -> StoredEvent | None:
        record = self._requests.get(request_id)
        if record is None:
            return None
        return next(event for event in self._events if event.event_id == record.event_id)

    # ------------------------------------------------------------------ 写入

    def append(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
        actor: str | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
        event_id: str | None = None,
    ) -> StoredEvent:
        """追加事件；返回已存储事件（幂等重放时返回首次追加的那一条）。"""

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at 必须携带时区")

        # 1) 请求幂等：相同请求只执行一次，同号异体直接拒绝。
        request_fingerprint = canonical_json(
            {
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload": payload,
                "expected_version": expected_version,
            }
        )
        if request_id is not None:
            seen = self._requests.get(request_id)
            if seen is not None:
                if seen.fingerprint != request_fingerprint:
                    raise IdempotencyConflict(
                        f"请求标识 {request_id} 已用于另一次编辑请求",
                        context={"request_id": request_id},
                    )
                existing = next(event for event in self._events if event.event_id == seen.event_id)
                return existing

        # 2) 冻结流拒绝一切写入。
        if aggregate_id in self._frozen:
            raise VersionFrozenError(
                f"聚合 {aggregate_id} 的事件流已因版本号异内容被冻结，需人工介入",
                context={"aggregate_id": aggregate_id},
            )

        current = self.current_version(aggregate_id)
        next_version = current + 1

        # 3) 乐观并发：协作者必须基于最新版本提交，否则去显式合并。
        if expected_version is not None and expected_version != current:
            raise ConcurrencyConflictError(
                aggregate_id=aggregate_id,
                expected=expected_version,
                current=current,
            )

        stored = StoredEvent(
            event_id=event_id or (request_id if request_id is not None else f"evt-{uuid4().hex}"),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at.isoformat(),
            version=next_version,
            payload=dict(payload),
            actor=actor,
            request_id=request_id,
        )
        self._events.append(stored)
        if request_id is not None:
            self._requests[request_id] = _RequestRecord(request_fingerprint, stored.event_id)
        return stored

    def import_event(self, event: StoredEvent) -> StoredEvent:
        """按显式版本号导入（跨节点重放/修复）。

        同一版本号内容一致 → 跳过（幂等重放）；内容不一致 → 冻结事件流。
        """

        aggregate_id = event.aggregate_id
        if aggregate_id in self._frozen:
            raise VersionFrozenError(
                f"聚合 {aggregate_id} 的事件流已冻结",
                context={"aggregate_id": aggregate_id},
            )
        for existing in self.events_for(aggregate_id):
            if existing.version != event.version:
                continue
            same = (
                existing.event_type == event.event_type
                and content_fingerprint(existing.event_type, existing.payload)
                == content_fingerprint(event.event_type, event.payload)
            )
            if same:
                return existing
            self._frozen.add(aggregate_id)
            raise VersionFrozenError(
                f"聚合 {aggregate_id} 版本 {event.version} 出现异内容，事件流已冻结",
                context={
                    "aggregate_id": aggregate_id,
                    "version": event.version,
                    "existing_event_id": existing.event_id,
                    "incoming_event_id": event.event_id,
                },
            )
        self._events.append(event)
        return event

    def unfreeze(self, aggregate_id: str) -> None:
        """人工排障后解冻；业务流程绝不自动调用。"""

        self._frozen.discard(aggregate_id)

    # ------------------------------------------------------------------ 持久化

    def save(self, path: str | Path) -> None:
        """原子写入，保证重启后幂等表与冻结状态不丢。"""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "events": [event.__dict__ for event in self._events],
            "requests": {
                key: {"fingerprint": value.fingerprint, "event_id": value.event_id}
                for key, value in self._requests.items()
            },
            "frozen": sorted(self._frozen),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> EventStore:
        path = Path(path)
        store = cls()
        if not path.exists():
            return store
        doc = json.loads(path.read_text(encoding="utf-8"))
        store._events = [StoredEvent(**item) for item in doc.get("events", [])]
        store._requests = {
            key: _RequestRecord(value["fingerprint"], value["event_id"])
            for key, value in doc.get("requests", {}).items()
        }
        store._frozen = set(doc.get("frozen", []))
        return store
