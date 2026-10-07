from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from experience_album_freshness.access import Identity, Role
from experience_album_freshness.clock import FixedClock
from experience_album_freshness.freshness import (
    EXPERIENCE_AGED,
    FACT_CHANGED,
    PLACE_INVALID,
    SNAPSHOT_STALE,
    CollectingNotifier,
    FreshnessChecker,
    NotificationLedger,
)
from experience_album_freshness.service import AlbumService
from experience_album_freshness.store import EventStore

TZ = timezone(timedelta(hours=8))


class FreshnessTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime(2026, 9, 1, 12, tzinfo=TZ))
        self.store = EventStore()
        self.svc = AlbumService(self.store, self.clock)
        self.author = Identity.of("author-1", Role.AUTHOR)
        self.svc.create_album(self.author, "album-1", name="攻略", request_id="req-album")
        self.svc.register_place(
            self.author, "album-1", "place-1",
            name="清风无烟餐厅",
            facts={"smoke_free": True, "hours": "10:00-22:00", "facilities": ["母婴室"]},
            request_id="req-place",
        )

    def _entry(self, *, experience_at=datetime(2026, 8, 1, 12, tzinfo=TZ), entry_id="entry-1"):
        self.svc.add_entry(
            self.author, "album-1", entry_id, "place-1",
            title="无烟餐厅",
            experience_at=experience_at,
            audience_conditions="亲子家庭",
            recommendation="recommended",
            request_id=f"req-entry-{entry_id}",
        )

    def _checker(self, ledger, *, stale_after=timedelta(days=180)):
        notifier = CollectingNotifier()
        checker = FreshnessChecker(self.store, self.clock, notifier, ledger, stale_after=stale_after)
        return checker, notifier


class FreshnessNotificationTests(FreshnessTestBase):
    def test_fact_change_notifies_author_once(self) -> None:
        self._entry()
        self.svc.record_place_change(
            self.author, "album-1", "place-1",
            changed_facts={"hours": "11:00-20:00"},
            request_id="req-change",
        )
        ledger = NotificationLedger()
        checker, notifier = self._checker(ledger)
        sent = checker.run_due()
        self.assertTrue(any(n.kind == FACT_CHANGED and n.reason == "hours_changed" for n in sent))
        # 全部发给作者
        self.assertTrue(all(n.recipient_id == "author-1" for n in sent))

        # 立即再跑一轮：不重复
        again = checker.run_due()
        self.assertEqual([], again)

    def test_restart_with_persisted_ledger_does_not_renotify(self) -> None:
        self._entry()
        self.svc.record_place_change(
            self.author, "album-1", "place-1",
            removed_facilities=["母婴室"], request_id="req-change",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            checker, notifier = self._checker(ledger_path)
            first = checker.run_due()
            self.assertTrue(any(n.reason == "facilities_removed" for n in first))

            # 模拟重启：新台账实例从同一文件恢复
            checker2, notifier2 = self._checker(NotificationLedger(ledger_path))
            second = checker2.run_due()
            self.assertEqual([], second)
            # 重启后又发生一次新变化（新版本号）→ 只通知新变化
            self.svc.record_place_change(
                self.author, "album-1", "place-1",
                changed_facts={"hours": "9:00-21:00"}, request_id="req-change-2",
            )
            third = checker2.run_due()
            reasons = {n.reason for n in third}
            self.assertEqual({"hours_changed"}, reasons)

    def test_closed_place_notifies_invalid(self) -> None:
        self._entry()
        self.svc.record_place_change(
            self.author, "album-1", "place-1",
            changed_facts={"status": "closed"}, request_id="req-close",
        )
        checker, notifier = self._checker(NotificationLedger())
        sent = checker.run_due()
        self.assertIn(PLACE_INVALID, {n.kind for n in sent})

    def test_experience_aged_uses_injected_clock(self) -> None:
        self._entry(experience_at=datetime(2026, 1, 1, 12, tzinfo=TZ))
        checker, _ = self._checker(NotificationLedger(), stale_after=timedelta(days=180))
        sent = checker.run_due()
        aged = [n for n in sent if n.kind == EXPERIENCE_AGED]
        self.assertEqual(1, len(aged))
        self.assertGreaterEqual(aged[0].detail["age_days"], 180)

        # 时间未继续推进时不重复通知
        self.assertEqual([], checker.run_due())

    def test_fresh_experience_not_aged(self) -> None:
        self._entry(experience_at=datetime(2026, 8, 20, 12, tzinfo=TZ))
        checker, _ = self._checker(NotificationLedger(), stale_after=timedelta(days=180))
        self.assertEqual([], [n for n in checker.run_due() if n.kind == EXPERIENCE_AGED])

    def test_shared_snapshot_notified_when_place_goes_stale(self) -> None:
        self._entry()
        self.svc.share_snapshot(
            self.author, "album-1", "entry-1",
            snapshot_id="snap-1", recipient_scope="friends", request_id="req-share",
        )
        self.svc.record_place_change(
            self.author, "album-1", "place-1",
            changed_facts={"hours": "11:00-20:00"}, request_id="req-change",
        )
        checker, _ = self._checker(NotificationLedger())
        sent = checker.run_due()
        stale = [n for n in sent if n.kind == SNAPSHOT_STALE]
        self.assertEqual(1, len(stale))
        self.assertEqual("author-1", stale[0].recipient_id)
        self.assertEqual(1, stale[0].detail["pinned_version"])
        self.assertEqual(2, stale[0].detail["current_version"])
        # 再跑不重复
        self.assertEqual([], checker.run_due())

    def test_clock_must_be_timezone_aware(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessChecker(self.store, _NaiveClock(), CollectingNotifier(), NotificationLedger())


class _NaiveClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 1)


if __name__ == "__main__":
    unittest.main()
