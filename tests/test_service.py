"""时效台业务规则测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import START, TZ, build_world, schema
from experience_album_freshness.clock import FixedClock
from experience_album_freshness.contracts import validate_event
from experience_album_freshness.errors import (
    ConflictOfInterest,
    EntryFrozen,
    FreshnessError,
    MergeRequired,
    NotFound,
    OpinionNotEditable,
    RequestAlreadyExecuted,
    StaleVersion,
)
from experience_album_freshness.freshness import CollectingNotifier, FreshnessChecker
from experience_album_freshness.service import FreshnessService
from experience_album_freshness.storage import EventStore


def entry_view(service: FreshnessService, album_id: str, entry_id: str, viewer_id=None):
    album = service.read_album(album_id, viewer_id)
    return next(item for item in album["entries"] if item["entry_id"] == entry_id)


class ContractShapeTests(unittest.TestCase):
    def test_every_generated_event_passes_contract(self) -> None:
        store, _, _ = build_world()
        issues = []
        for event in store.events():
            issues.extend(validate_event(event, schema()))
        self.assertEqual([], issues, [f"{i.field}:{i.code}" for i in issues])


class IdempotencyTests(unittest.TestCase):
    def test_same_request_executes_once_and_replays_result(self) -> None:
        store, service, _ = build_world()
        before = len(store.events())
        service.change_place("req-change-1", "place-cafe", {"hours": "10:00-20:00"})
        self.assertEqual(before + 1, len(store.events()))

        with self.assertRaises(RequestAlreadyExecuted) as caught:
            service.change_place("req-change-1", "place-cafe", {"hours": "00:00-01:00"})
        self.assertEqual(["req-change-1#1"], caught.exception.event_ids)
        # 重试没有产生第二个事件，第一次的事实生效。
        self.assertEqual(before + 1, len(store.events()))
        self.assertEqual("10:00-20:00", service.registry().place("place-cafe").facts["hours"])

    def test_idempotency_survives_restart_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            store, service, _ = build_world(path)
            service.change_place("req-persist-1", "place-cafe", {"hours": "10:00-20:00"})

            restarted_store = EventStore(path)
            restarted = FreshnessService(restarted_store)
            with self.assertRaises(RequestAlreadyExecuted):
                restarted.change_place("req-persist-1", "place-cafe", {"hours": "11:00-19:00"})
            self.assertEqual("10:00-20:00", restarted.registry().place("place-cafe").facts["hours"])


class ConcurrencyTests(unittest.TestCase):
    def _two_editors_read_same_version(self, service: FreshnessService):
        base_version = service.store.version("album-1")
        common = dict(
            album_id="album-1",
            entry_id="entry-1",
            experience_at="2026-10-05T20:00:00+08:00",
            audience_conditions=["独自前往"],
        )
        return base_version, common

    def test_same_version_different_content_freezes_entry(self) -> None:
        store, service, _ = build_world()
        base_version, common = self._two_editors_read_same_version(service)

        service.update_entry(
            "req-A",
            editor_id="owner-a",
            recommendation="A：还是适合久坐",
            expected_version=base_version,
            **common,
        )
        with self.assertRaises(EntryFrozen) as caught:
            service.update_entry(
                "req-B",
                editor_id="collab-b",
                recommendation="B：晚上太吵，不再推荐办公",
                expected_version=base_version,
                **common,
            )
        frozen = entry_view(service, "album-1", "entry-1")
        self.assertTrue(frozen["frozen"])
        # 竞争两支：A 的更新事件与 B 的冻结事件。
        self.assertEqual(["req-A#1", "req-B#1"], caught.exception.competing_event_ids)

        # 冻结后普通更新被拒。
        with self.assertRaises(EntryFrozen):
            service.update_entry(
                "req-C",
                editor_id="owner-a",
                recommendation="覆盖试试",
                expected_version=base_version + 2,
                **common,
            )

    def test_frozen_entry_requires_explicit_merge_listing_both_branches(self) -> None:
        store, service, _ = build_world()
        base_version, common = self._two_editors_read_same_version(service)
        service.update_entry(
            "req-A", editor_id="owner-a", recommendation="A 的判断",
            expected_version=base_version, **common,
        )
        with self.assertRaises(EntryFrozen):
            service.update_entry(
                "req-B", editor_id="collab-b", recommendation="B 的判断",
                expected_version=base_version, **common,
            )

        # 只列一支不允许。
        with self.assertRaises(MergeRequired):
            service.merge_entry(
                "req-merge-bad",
                album_id="album-1",
                entry_id="entry-1",
                editor_id="collab-b",
                competing_event_ids=["req-A#1"],
                recommendation="合并判断",
                **{k: v for k, v in common.items() if k not in {"album_id", "entry_id"}},
            )

        # 列错集合不允许。
        with self.assertRaises(MergeRequired):
            service.merge_entry(
                "req-merge-wrong",
                album_id="album-1",
                entry_id="entry-1",
                editor_id="collab-b",
                competing_event_ids=["req-entry-1#1", "req-A#1"],
                recommendation="合并判断",
                **{k: v for k, v in common.items() if k not in {"album_id", "entry_id"}},
            )

        service.merge_entry(
            "req-merge-ok",
            album_id="album-1",
            entry_id="entry-1",
            editor_id="collab-b",
            competing_event_ids=["req-A#1", "req-B#1"],
            recommendation="合并：工作日适合办公，周末嘈杂",
            experience_at="2026-10-05T20:00:00+08:00",
            audience_conditions=["独自前往", "工作日"],
        )
        merged = entry_view(service, "album-1", "entry-1")
        self.assertFalse(merged["frozen"])
        self.assertIn("工作日", merged["audience_conditions"])
        merge_event = store.events("album-1")[-1]
        self.assertEqual(["req-A#1", "req-B#1"], merge_event["payload"]["merged_from_event_ids"])

    def test_same_version_same_content_converges_without_second_event(self) -> None:
        store, service, _ = build_world()
        base_version, common = self._two_editors_read_same_version(service)
        service.update_entry(
            "req-A", editor_id="owner-a", recommendation="一致的判断",
            expected_version=base_version, **common,
        )
        count_after_a = len(store.events("album-1"))
        result = service.update_entry(
            "req-B", editor_id="collab-b", recommendation="一致的判断",
            expected_version=base_version, **common,
        )
        self.assertEqual(["req-A#1"], [event["event_id"] for event in result])
        self.assertEqual(count_after_a, len(store.events("album-1")))
        self.assertFalse(entry_view(service, "album-1", "entry-1")["frozen"])

    def test_update_for_other_entry_at_occupied_version_is_stale_not_freeze(self) -> None:
        store, service, _ = build_world()
        base_version = service.store.version("album-1")
        service.update_entry(
            "req-A",
            album_id="album-1",
            entry_id="entry-1",
            editor_id="owner-a",
            experience_at="2026-10-05T20:00:00+08:00",
            recommendation="A 改 entry-1",
            audience_conditions=["独自前往"],
            expected_version=base_version,
        )
        with self.assertRaises(StaleVersion):
            service.update_entry(
                "req-B",
                album_id="album-1",
                entry_id="entry-2",
                editor_id="collab-b",
                experience_at="2026-10-05T20:00:00+08:00",
                recommendation="B 改 entry-2",
                audience_conditions=["携带行李"],
                expected_version=base_version,
            )
        self.assertFalse(entry_view(service, "album-1", "entry-2")["frozen"])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_keeps_original_text_but_flags_stale_facts(self) -> None:
        store, service, _ = build_world()
        shared = service.share_snapshot(
            "req-share",
            album_id="album-1",
            sharer_id="owner-a",
            recipient_scope="friends",
        )
        snapshot_id = shared["snapshot_id"]

        # 作者随后更新判断并撤回认可；门店改了营业时间。
        service.change_place("req-hours", "place-cafe", {"hours": "11:00-23:00"})
        service.withdraw_endorsement("req-withdraw", "album-1", "entry-1", "owner-a", "重开后变吵")

        view = service.read_snapshot(snapshot_id)
        snap_entry = next(item for item in view["entries"] if item["original"]["entry_id"] == "entry-1")
        # 原文原样保留。
        self.assertEqual("无烟区安静，适合带电脑久坐", snap_entry["original"]["recommendation"])
        self.assertEqual("09:00-22:00", snap_entry["original"]["facts"]["hours"])
        # 时效标注反映新事实与不再认可。
        self.assertEqual(["09:00-22:00", "11:00-23:00"], snap_entry["annotations"]["changed_facts"]["hours"])
        self.assertFalse(snap_entry["annotations"]["author_endorses_now"])
        self.assertEqual("重开后变吵", snap_entry["annotations"]["withdraw_reason"])

        # 快照哈希在事件流中不可变。
        self.assertEqual(shared["snapshot_hash"], view["snapshot_hash"])

    def test_snapshot_flags_removed_and_merged_places(self) -> None:
        store, service, _ = build_world()
        shared = service.share_snapshot(
            "req-share-all", album_id="album-1", sharer_id="owner-a", recipient_scope="friends"
        )
        service.change_place("req-remove", "place-cafe", {"status": "removed"})

        # 商场合并到新商场，但只确认迁移 entry-2 的引用（此处 entry-2 就在旧商场）。
        service.register_place("req-place-new", "place-mall-new", "新商场服务台", {"free_storage": True})
        service.merge_places(
            "req-merge",
            surviving_place_id="place-mall-new",
            merged_place_id="place-mall-old",
            migrated_citations=["entry-2"],
        )

        view = service.read_snapshot(shared["snapshot_id"])
        by_id = {item["original"]["entry_id"]: item for item in view["entries"]}
        self.assertEqual("removed", by_id["entry-1"]["annotations"]["place_lifecycle"]["state"])
        self.assertEqual("merged", by_id["entry-2"]["annotations"]["place_lifecycle"]["state"])
        self.assertTrue(by_id["entry-2"]["annotations"]["citation_migrated"])

    def test_place_merge_does_not_swallow_unconfirmed_old_store_experience(self) -> None:
        store, service, _ = build_world()
        service.register_place("req-place-new2", "place-mall-new", "新商场", {"free_storage": True})
        # 旧门店还有第二条亲身体验 entry-3，未被确认迁移，必须留在旧门店。
        service.add_entry(
            "req-entry-3",
            "album-1",
            entry_id="entry-3",
            place_id="place-mall-old",
            author_id="owner-a",
            experience_at="2026-10-02T10:00:00+08:00",
            recommendation="老服务台寄存免排队",
            audience_conditions=["携带行李"],
        )
        # 只迁移 entry-2；entry-1 本来在 cafe，这里再校验"非旧门店条目不能借迁移"。
        with self.assertRaises(FreshnessError):
            service.merge_places(
                "req-merge-bad",
                surviving_place_id="place-mall-new",
                merged_place_id="place-mall-old",
                migrated_citations=["entry-1"],
            )
        service.merge_places(
            "req-merge-ok",
            surviving_place_id="place-mall-new",
            merged_place_id="place-mall-old",
            migrated_citations=["entry-2"],
        )
        registry = service.registry()
        entries = registry.album("album-1").entries
        self.assertEqual("place-mall-new", entries["entry-2"].place_id)
        # 未确认的旧门店体验保留在旧门店，不被存活门店吞掉。
        self.assertEqual("place-mall-old", entries["entry-3"].place_id)
        # 旧门店实体仍在，且记录了并入方向。
        self.assertEqual("place-mall-new", registry.place("place-mall-old").merged_into)
        # entry-1 原封不动留在 cafe。
        self.assertEqual("place-cafe", entries["entry-1"].place_id)


class CorrectionTests(unittest.TestCase):
    def test_merchant_fact_correction_cannot_touch_opinions(self) -> None:
        store, service, _ = build_world()
        with self.assertRaises(OpinionNotEditable):
            service.submit_correction(
                "req-corr-opinion",
                case_id="case-1",
                place_id="place-cafe",
                album_id="album-1",
                submitter_id="merchant-1",
                submitter_role="merchant",
                fact_patch={"recommendation": "商家要求删除差评"},
            )

    def test_accepted_correction_changes_facts_only(self) -> None:
        store, service, _ = build_world()
        service.submit_correction(
            "req-corr-1",
            case_id="case-1",
            place_id="place-cafe",
            album_id="album-1",
            submitter_id="merchant-1",
            submitter_role="merchant",
            fact_patch={"hours": "08:30-21:30"},
            evidence_summary="新营业时间公示牌照片",
        )
        service.decide_correction(
            "req-decide-1", case_id="case-1", reviewer_id="reviewer-r", decision="accepted"
        )
        self.assertEqual("08:30-21:30", service.registry().place("place-cafe").facts["hours"])
        # 用户观点（含负面体验）没有被改动。
        view = entry_view(service, "album-1", "entry-1")
        self.assertEqual("无烟区安静，适合带电脑久坐", view["recommendation"])
        self.assertEqual("周末晚上排队较久", view["negative_experience"])
        self.assertEqual("accepted", service.registry().cases["case-1"].status)

    def test_reviewer_who_edited_album_must_recuse(self) -> None:
        store, service, _ = build_world()
        service.submit_correction(
            "req-corr-2",
            case_id="case-2",
            place_id="place-cafe",
            album_id="album-1",
            submitter_id="merchant-1",
            submitter_role="merchant",
            fact_patch={"hours": "08:30-21:30"},
        )
        # owner-a 建过专辑/录过条目，collab-b 是协作者，都必须回避。
        for conflicted in ("owner-a", "collab-b"):
            with self.assertRaises(ConflictOfInterest):
                service.decide_correction(
                    f"req-decide-{conflicted}",
                    case_id="case-2",
                    reviewer_id=conflicted,
                    decision="accepted",
                )
        # 无关联审核员可以裁决（拒绝时不改事实）。
        service.decide_correction(
            "req-decide-r", case_id="case-2", reviewer_id="reviewer-r",
            decision="rejected", reason="证据不足",
        )
        self.assertEqual("09:00-22:00", service.registry().place("place-cafe").facts["hours"])

    def test_decided_case_cannot_be_decided_again(self) -> None:
        store, service, _ = build_world()
        service.submit_correction(
            "req-corr-3", case_id="case-3", place_id="place-cafe", album_id="album-1",
            submitter_id="merchant-1", submitter_role="merchant", fact_patch={"hours": "08:00-20:00"},
        )
        service.decide_correction("req-decide-3a", case_id="case-3", reviewer_id="reviewer-r",
                                  decision="accepted")
        with self.assertRaises(FreshnessError):
            service.decide_correction("req-decide-3b", case_id="case-3", reviewer_id="reviewer-r2",
                                      decision="rejected")


class PermissionTests(unittest.TestCase):
    def test_attachment_pii_hidden_from_unauthorized_roles(self) -> None:
        store, service, _ = build_world()
        outsider = entry_view(service, "album-1", "entry-1", viewer_id="reader-x")
        attachment = outsider["attachments"][0]
        self.assertIsNone(attachment["pii"])
        self.assertTrue(attachment["pii_hidden"])
        self.assertEqual("店内无烟标识照片", attachment["summary"])

        for allowed in ("owner-a", "collab-b"):  # 作者本人/上传者、owner、协作者
            view = entry_view(service, "album-1", "entry-1", viewer_id=allowed)
            self.assertEqual({"phone": "13800000000"}, view["attachments"][0]["pii"])

        # 匿名阅读者同样隐藏。
        anon = entry_view(service, "album-1", "entry-1", viewer_id=None)
        self.assertIsNone(anon["attachments"][0]["pii"])

    def test_pii_redaction_applies_in_snapshot_view(self) -> None:
        store, service, _ = build_world()
        shared = service.share_snapshot(
            "req-share-pii", album_id="album-1", sharer_id="owner-a",
            recipient_scope="friends", entry_ids=["entry-1"],
        )
        view = service.read_snapshot(shared["snapshot_id"], viewer_id="friend-1")
        attachment = view["entries"][0]["original"]["attachments"][0]
        self.assertIsNone(attachment["pii"])
        self.assertTrue(attachment["pii_hidden"])


class ReaderViewTests(unittest.TestCase):
    def test_reader_sees_experience_time_changed_facts_and_endorsement(self) -> None:
        store, service, _ = build_world()
        service.change_place("req-fact", "place-cafe", {"baby_care": True, "free_storage": None})
        view = entry_view(service, "album-1", "entry-1", viewer_id="reader-x")
        self.assertEqual("2026-10-01T19:30:00+08:00", view["experience_at"])
        self.assertTrue(view["endorsed"])
        changes = view["facts_changes"]["changed"]
        self.assertEqual([False, True], changes["baby_care"])
        self.assertIn("free_storage", changes)

    def test_withdrawn_entry_shows_not_endorsed_but_keeps_text(self) -> None:
        store, service, _ = build_world()
        service.withdraw_endorsement("req-w", "album-1", "entry-1", "owner-a", "门店易主")
        view = entry_view(service, "album-1", "entry-1")
        self.assertFalse(view["endorsed"])
        self.assertEqual("门店易主", view["withdraw_reason"])
        self.assertEqual("无烟区安静，适合带电脑久坐", view["recommendation"])

    def test_comment_citation_pins_entry_version(self) -> None:
        store, service, _ = build_world()
        service.cite_comment(
            "req-cite", album_id="album-1", comment_id="comment-1",
            entry_id="entry-1", author_id="reader-x", quoted_text="楼主说适合带电脑",
        )
        pinned = service.registry().album("album-1").citations[-1]
        pinned_version = pinned.entry_version
        service.update_entry(
            "req-after-cite", album_id="album-1", entry_id="entry-1", editor_id="owner-a",
            experience_at="2026-10-06T20:00:00+08:00", recommendation="更新后的判断",
            audience_conditions=["独自前往"],
        )
        same_citation = service.registry().album("album-1").citations[-1]
        self.assertEqual(pinned_version, same_citation.entry_version)
        self.assertEqual("楼主说适合带电脑", same_citation.quoted_text)


class FreshnessSchedulerTests(unittest.TestCase):
    def test_scheduled_checks_use_injected_clock_and_deduplicate_notifications(self) -> None:
        clock = FixedClock(START)
        store, service, clock = build_world(clock=clock)
        notifier = CollectingNotifier()
        checker = FreshnessChecker(service, notifier, stale_after=timedelta(days=30), clock=clock)

        # entry-2 的亲历时间是 2026-09-15，距 10-07 不足 30 天……拨到超期。
        first = checker.sweep()
        self.assertEqual([], first)
        clock.advance(timedelta(days=20))  # entry-2 约 52 天
        second = checker.sweep()
        kinds = {notice.kind for notice in second}
        self.assertIn("experience_stale", kinds)

        # 再扫一次不重复通知。
        again = checker.sweep()
        self.assertEqual([], again)
        self.assertEqual(1, len(notifier.sent))

    def test_restart_does_not_repeat_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            clock = FixedClock(START + timedelta(days=200))
            store, service, clock = build_world(path=path, clock=clock)
            notifier = CollectingNotifier()
            FreshnessChecker(service, notifier, stale_after=timedelta(days=30), clock=clock).sweep()
            sent = len(notifier.sent)
            self.assertGreater(sent, 0)

            restarted_store = EventStore(path)
            restarted = FreshnessService(restarted_store, clock)
            restarted_notifier = CollectingNotifier()
            FreshnessChecker(
                restarted, restarted_notifier, stale_after=timedelta(days=30), clock=clock
            ).sweep()
            self.assertEqual([], restarted_notifier.sent)

    def test_place_change_and_removal_notify_affiliated_entries_once_per_version(self) -> None:
        clock = FixedClock(START)
        store, service, clock = build_world(clock=clock)
        notifier = CollectingNotifier()
        checker = FreshnessChecker(service, notifier, stale_after=timedelta(days=3650), clock=clock)

        service.change_place("req-hours2", "place-cafe", {"hours": "11:00-23:00"})
        checker.sweep()
        self.assertIn("place_facts_changed", {n.kind for n in notifier.sent})
        self.assertEqual(1, len(notifier.sent))
        checker.sweep()
        self.assertEqual(1, len(notifier.sent))

        # 设施撤除（新事实版本）再通知一次；门店撤除再通知。
        service.change_place("req-facility", "place-cafe", {"smoke_free": False})
        checker.sweep()
        self.assertEqual(2, len(notifier.sent))
        service.change_place("req-removed", "place-cafe", {"status": "removed"})
        checker.sweep()
        self.assertEqual(3, len(notifier.sent))
        self.assertEqual("place_removed", notifier.sent[-1].kind)

    def test_snapshot_staleness_notifies_share_recipients_once(self) -> None:
        clock = FixedClock(START)
        store, service, clock = build_world(clock=clock)
        shared = service.share_snapshot(
            "req-snap", album_id="album-1", sharer_id="owner-a", recipient_scope="friends"
        )
        notifier = CollectingNotifier()
        checker = FreshnessChecker(service, notifier, stale_after=timedelta(days=3650), clock=clock)
        service.change_place("req-h", "place-cafe", {"hours": "11:00-23:00"})
        checker.sweep()
        snapshot_notices = [n for n in notifier.sent if n.kind.startswith("snapshot_")]
        self.assertTrue(any(n.kind == "snapshot_place_facts_changed" for n in snapshot_notices))
        checker.sweep()
        self.assertEqual(len(snapshot_notices), len([n for n in notifier.sent if n.kind.startswith("snapshot_")]))


if __name__ == "__main__":
    unittest.main()
