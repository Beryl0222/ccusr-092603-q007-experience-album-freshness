from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from experience_album_freshness.errors import (
    ConcurrencyConflictError,
    IdempotencyConflict,
    VersionFrozenError,
)
from experience_album_freshness.store import EventStore, StoredEvent

TZ = timezone.utc


def make_event(version: int, payload: dict, *, event_id: str = "evt-x") -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event_type="PLACE_CHANGED",
        aggregate_type="experience_album",
        aggregate_id="album-1::place::p1",
        occurred_at="2026-09-01T10:00:00+00:00",
        version=version,
        payload=payload,
        actor="author-1",
        request_id=None,
    )


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.at = datetime(2026, 9, 1, tzinfo=TZ)

    def _append(self, request_id="req-1", payload=None, expected_version=None):
        return self.store.append(
            event_type="ENTRY_UPDATED",
            aggregate_type="experience_album",
            aggregate_id="album-1::entry::e1",
            payload=payload or {"entry_id": "e1", "title": "无烟餐厅清单"},
            occurred_at=self.at,
            actor="author-1",
            request_id=request_id,
            expected_version=expected_version,
        )

    def test_same_request_executes_once(self) -> None:
        first = self._append(request_id="req-only-once")
        second = self._append(request_id="req-only-once")
        self.assertIs(first, second)
        self.assertEqual(1, len(self.store.events))

    def test_same_request_with_different_body_is_rejected(self) -> None:
        self._append(request_id="req-clash", payload={"entry_id": "e1", "title": "A"})
        with self.assertRaises(IdempotencyConflict):
            self._append(request_id="req-clash", payload={"entry_id": "e1", "title": "B"})

    def test_versions_increment_from_one(self) -> None:
        e1 = self._append(request_id="r1")
        e2 = self._append(request_id="r2")
        self.assertEqual((1, 2), (e1.version, e2.version))

    def test_expected_version_blocks_stale_writer(self) -> None:
        self._append(request_id="r1")
        self._append(request_id="r2")  # 当前版本到 2
        with self.assertRaises(ConcurrencyConflictError) as ctx:
            self._append(request_id="r3", expected_version=1)
        self.assertEqual(2, ctx.exception.current_version)

    def test_expected_version_matching_current_succeeds(self) -> None:
        self._append(request_id="r1")
        event = self._append(request_id="r2", expected_version=1)
        self.assertEqual(2, event.version)

    def test_same_version_same_content_replays(self) -> None:
        event = make_event(3, {"place_version": 3, "changed_facts": {"hours": "9-22"}})
        first = self.store.import_event(event)
        again = self.store.import_event(make_event(3, {"place_version": 3, "changed_facts": {"hours": "9-22"}}))
        self.assertIs(first, again)

    def test_same_version_different_content_freezes_stream(self) -> None:
        self.store.import_event(make_event(1, {"place_version": 1, "changed_facts": {"hours": "9-22"}}))
        with self.assertRaises(VersionFrozenError):
            self.store.import_event(make_event(1, {"place_version": 1, "changed_facts": {"hours": "0-24"}}))
        self.assertTrue(self.store.is_frozen("album-1::place::p1"))
        with self.assertRaises(VersionFrozenError):
            self.store.append(
                event_type="PLACE_CHANGED",
                aggregate_type="experience_album",
                aggregate_id="album-1::place::p1",
                payload={"place_version": 2, "changed_facts": {}},
                occurred_at=self.at,
            )

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append(
                event_type="ENTRY_UPDATED",
                aggregate_type="experience_album",
                aggregate_id="x",
                payload={},
                occurred_at=datetime(2026, 9, 1),
            )

    def test_persistence_roundtrip_keeps_idempotency_and_freeze(self) -> None:
        self._append(request_id="persist-1")
        self.store.import_event(make_event(1, {"place_version": 1, "changed_facts": {"hours": "9-22"}}))
        with self.assertRaises(VersionFrozenError):
            self.store.import_event(make_event(1, {"place_version": 1, "changed_facts": {"hours": "0-24"}}))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            self.store.save(path)
            restored = EventStore.load(path)

        # 重启后相同请求仍只执行一次
        replayed = restored.append(
            event_type="ENTRY_UPDATED",
            aggregate_type="experience_album",
            aggregate_id="album-1::entry::e1",
            payload={"entry_id": "e1", "title": "无烟餐厅清单"},
            occurred_at=self.at,
            actor="author-1",
            request_id="persist-1",
        )
        self.assertEqual(replayed.event_id, "persist-1")
        self.assertEqual(1, len([e for e in restored.events if e.request_id == "persist-1"]))
        # 冻结状态随持久化恢复
        self.assertTrue(restored.is_frozen("album-1::place::p1"))


if __name__ == "__main__":
    unittest.main()
