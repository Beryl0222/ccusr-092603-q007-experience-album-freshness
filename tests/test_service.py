from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from experience_album_freshness.access import AttachmentSummary, Identity, Role
from experience_album_freshness.clock import FixedClock
from experience_album_freshness.errors import (
    FactCorrectionBoundaryError,
    MergeRequiredError,
    PermissionDenied,
    ReviewerConflict,
    ValidationError,
)
from experience_album_freshness.service import AlbumService
from experience_album_freshness.store import EventStore

TZ = timezone(timedelta(hours=8))


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime(2026, 9, 1, 12, tzinfo=TZ))
        self.store = EventStore()
        self.svc = AlbumService(self.store, self.clock)
        self.author = Identity.of("author-1", Role.AUTHOR)
        self.collab = Identity.of("collab-1", Role.COLLABORATOR)
        self.merchant = Identity.of("merchant-1", Role.MERCHANT)
        self.reviewer = Identity.of("reviewer-1", Role.REVIEWER)
        self.other_reviewer = Identity.of("reviewer-2", Role.REVIEWER)
        self.reader = Identity.of("reader-1", Role.READER)

        self.svc.create_album(self.author, "album-1", name="无烟亲子攻略", request_id="req-album")
        self.svc.invite_collaborator(self.author, "album-1", "collab-1", request_id="req-invite")
        self.svc.register_place(
            self.author,
            "album-1",
            "place-smoke",
            name="清风无烟餐厅",
            facts={
                "smoke_free": True,
                "hours": "10:00-22:00",
                "facilities": ["母婴室", "免费寄存"],
            },
            request_id="req-place",
        )

    def _add_entry(self, request_id="req-entry", identity=None):
        return self.svc.add_entry(
            identity or self.author,
            "album-1",
            "entry-1",
            "place-smoke",
            title="带娃午餐好去处",
            experience_at=datetime(2026, 8, 20, 12, tzinfo=TZ),
            audience_conditions="带 0-3 岁婴幼儿家庭",
            recommendation="recommended",
            recommendation_reason="全程无烟，母婴室在二层",
            negative_experience="周末排队较久",
            attachments=[
                AttachmentSummary(
                    attachment_id="att-1",
                    kind="receipt",
                    summary="结账单显示无烟楼层",
                    contains_personal_info=True,
                    personal_fields=("phone",),
                )
            ],
            request_id=request_id,
        )


class SnapshotAndStalenessTests(ServiceTestBase):
    def test_snapshot_keeps_original_and_flags_stale_place(self) -> None:
        self._add_entry()
        self.svc.share_snapshot(
            self.author, "album-1", "entry-1",
            snapshot_id="snap-1", recipient_scope="friends", request_id="req-share",
        )
        # 商家后来改了营业时间、撤除母婴室、门店停业
        self.svc.record_place_change(
            self.author, "album-1", "place-smoke",
            changed_facts={"hours": "11:00-20:00"},
            change_kind="hours_update", request_id="req-hours",
        )
        self.svc.record_place_change(
            self.author, "album-1", "place-smoke",
            removed_facilities=["母婴室"],
            change_kind="facility_removed", request_id="req-facility",
        )
        self.svc.record_place_change(
            self.author, "album-1", "place-smoke",
            changed_facts={"status": "closed"},
            change_kind="closed", request_id="req-closed",
        )

        recipient = Identity.of("friend-1", Role.SNAPSHOT_RECIPIENT, scopes=frozenset({"friends"}))
        view = self.svc.snapshot_view(recipient, "album-1", "snap-1")

        # 原文保留：快照内仍是旧营业时间、旧推荐与旧设施
        self.assertEqual("10:00-22:00", view["original_content"]["place"]["facts"]["hours"])
        self.assertEqual("带娃午餐好去处", view["original_content"]["title"])
        self.assertIn("母婴室", view["original_content"]["place"]["facts"]["facilities"])
        # 失效标注
        kinds = {item["kind"] for item in view["staleness"]}
        self.assertIn("facts_changed", kinds)
        self.assertIn("place_closed", kinds)
        self.assertTrue(view["stale"])

    def test_withdraw_flips_endorsement_but_keeps_snapshot_text(self) -> None:
        self._add_entry()
        self.svc.share_snapshot(
            self.author, "album-1", "entry-1",
            snapshot_id="snap-1", recipient_scope="public", request_id="req-share",
        )
        self.svc.author_withdraw_endorsement(
            self.author, "album-1", "entry-1", "门店近期允许吸烟，不再认可",
            request_id="req-withdraw",
        )
        recipient = Identity.of("friend-2", Role.SNAPSHOT_RECIPIENT)
        view = self.svc.snapshot_view(recipient, "album-1", "snap-1")
        self.assertTrue(view["endorsed_at_share"])
        self.assertFalse(view["author_endorses_now"])
        self.assertEqual("门店近期允许吸烟，不再认可", view["withdrew_reason"])
        # 原文未被撤回动作改写
        self.assertEqual("recommended", view["original_content"]["recommendation"])

    def test_entry_view_shows_experience_time_changes_and_endorsement(self) -> None:
        self._add_entry()
        self.svc.record_place_change(
            self.author, "album-1", "place-smoke",
            changed_facts={"hours": "11:00-20:00"}, request_id="req-hours",
        )
        view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        self.assertEqual("2026-08-20T12:00:00+08:00", view["experience_at"])
        self.assertEqual(1, view["based_on_place"]["place_version"])
        self.assertEqual(2, view["current_place_version"])
        self.assertIn("hours_changed", view["stale_reasons"])
        self.assertTrue(view["author_endorses"])

    def test_attachments_pii_hidden_from_reader_visible_to_author(self) -> None:
        self._add_entry()
        reader_view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        self.assertNotIn("phone", reader_view["attachments"][0]["personal_fields"])
        self.assertIn("无权查看", reader_view["attachments"][0]["summary"])

        author_view = self.svc.entry_view(self.author, "album-1", "entry-1")
        self.assertIn("phone", author_view["attachments"][0]["personal_fields"])
        self.assertEqual("结账单显示无烟楼层", author_view["attachments"][0]["summary"])

    def test_snapshot_recipient_scope_enforced(self) -> None:
        self._add_entry()
        self.svc.share_snapshot(
            self.author, "album-1", "entry-1",
            snapshot_id="snap-1", recipient_scope="close-friends", request_id="req-share",
        )
        outsider = Identity.of("friend-9", Role.SNAPSHOT_RECIPIENT, scopes=frozenset({"friends"}))
        with self.assertRaises(PermissionDenied):
            self.svc.snapshot_view(outsider, "album-1", "snap-1")
        insider = Identity.of("friend-3", Role.SNAPSHOT_RECIPIENT, scopes=frozenset({"close-friends"}))
        self.assertTrue(self.svc.snapshot_view(insider, "album-1", "snap-1"))


class PlaceMergeTests(ServiceTestBase):
    def _two_places_and_entries(self) -> None:
        self.svc.register_place(
            self.author, "album-1", "place-old-branch",
            name="旧分店", facts={"smoke_free": True}, request_id="req-place2",
        )
        self._add_entry()
        self.svc.add_entry(
            self.author, "album-1", "entry-old", "place-old-branch",
            title="旧分店亲历",
            experience_at=datetime(2026, 7, 1, tzinfo=TZ),
            audience_conditions="所有人",
            recommendation="recommended",
            request_id="req-entry-old",
        )
        self.svc.add_comment(
            self.author, "album-1",
            comment_id="cmt-old", entry_id="entry-old",
            quote="旧分店的寄存点在门口", request_id="req-cmt",
        )

    def test_merge_migrates_only_confirmed_references(self) -> None:
        self._two_places_and_entries()
        self.svc.merge_places(
            self.author, "album-1",
            "place-old-branch", "place-smoke",
            confirmed_entry_ids=[],  # 旧分店体验不被确认指向新主体
            confirmed_comment_ids=[],
            request_id="req-merge",
        )
        old_entry = self.svc.entry_view(self.reader, "album-1", "entry-old")
        self.assertEqual("place-old-branch", old_entry["based_on_place"]["place_id"])
        self.assertEqual("merged", old_entry["based_on_place"]["place_status"])
        self.assertEqual("place-smoke", old_entry["based_on_place"]["merged_into"])

    def test_merge_migrates_confirmed_entry_and_comment(self) -> None:
        self._two_places_and_entries()
        self.svc.merge_places(
            self.author, "album-1",
            "place-old-branch", "place-smoke",
            confirmed_entry_ids=["entry-old"],
            confirmed_comment_ids=["cmt-old"],
            request_id="req-merge",
        )
        view = self.svc.entry_view(self.reader, "album-1", "entry-old")
        self.assertEqual("place-smoke", view["based_on_place"]["place_id"])
        self.assertTrue(view["comments"][0]["migrated_by_place_merge"])

    def test_merge_rejects_reference_not_pointing_at_old_place(self) -> None:
        self._two_places_and_entries()
        with self.assertRaises(ValidationError):
            self.svc.merge_places(
                self.author, "album-1",
                "place-old-branch", "place-smoke",
                confirmed_entry_ids=["entry-1"],  # 指向 place-smoke，不是旧门店
                request_id="req-merge-bad",
            )

    def test_experience_against_old_branch_survives_with_new_comment(self) -> None:
        self._two_places_and_entries()
        self.svc.merge_places(
            self.author, "album-1",
            "place-old-branch", "place-smoke",
            request_id="req-merge",
        )
        # 合并后仍可对旧门店体验追加评论（钉在旧地点，不被吞掉）
        self.svc.add_comment(
            self.collab, "album-1",
            comment_id="cmt-after", entry_id="entry-old",
            quote="旧分店当时确实有免费寄存", request_id="req-cmt-after",
        )
        view = self.svc.entry_view(self.reader, "album-1", "entry-old")
        self.assertEqual(2, len(view["comments"]))
        self.assertEqual("place-old-branch", view["comments"][-1]["pinned_place"]["place_id"])


class ConcurrencyTests(ServiceTestBase):
    def test_concurrent_edit_requires_explicit_merge(self) -> None:
        self._add_entry()
        # 两个协作者/作者都读到版本 1
        base = self.svc.entry_view(self.author, "album-1", "entry-1")["entry_version"]
        self.assertEqual(1, base)

        self.svc.update_entry(
            self.author, "album-1", "entry-1",
            {"recommendation_reason": "作者更新：仍然不错"},
            expected_version=1, request_id="req-update-a",
        )
        # 第二人仍基于旧版本提交 → 必须显式合并
        with self.assertRaises(MergeRequiredError) as ctx:
            self.svc.update_entry(
                self.collab, "album-1", "entry-1",
                {"recommendation_reason": "协作者更新：排队久"},
                expected_version=1, request_id="req-update-b",
            )
        self.assertEqual(2, ctx.exception.current_version)

        # 不允许直接拿旧基线假装合并
        with self.assertRaises(ValidationError):
            self.svc.resolve_entry_merge(
                self.collab, "album-1", "entry-1",
                self._full_fields("合并后理由"),
                base_version=2, request_id="req-merge-bad",
            )

        merged = self.svc.resolve_entry_merge(
            self.collab, "album-1", "entry-1",
            self._full_fields("合并后：不错但周末排队"),
            base_version=1, request_id="req-merge-ok",
        )
        self.assertEqual("ENTRY_MERGE_RESOLVED", merged.event_type)
        self.assertEqual(3, merged.version)
        view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        self.assertEqual("合并后：不错但周末排队", view["recommendation_reason"])

    @staticmethod
    def _full_fields(reason: str) -> dict:
        return {
            "title": "带娃午餐好去处",
            "recommendation": "recommended",
            "recommendation_reason": reason,
            "audience_conditions": "带 0-3 岁婴幼儿家庭",
            "negative_experience": "周末排队较久",
            "experience_at": datetime(2026, 8, 20, 12, tzinfo=TZ),
            "attachments": [],
        }

    def test_same_request_replays_after_concurrency_failure(self) -> None:
        self._add_entry()
        kwargs = dict(
            fields={"recommendation_reason": "重试内容"},
            expected_version=1,
        )
        self.svc.update_entry(self.author, "album-1", "entry-1",
                              {"recommendation_reason": "第一笔"},
                              expected_version=1, request_id="req-first")
        with self.assertRaises(MergeRequiredError):
            self.svc.update_entry(self.collab, "album-1", "entry-1", request_id="req-dup", **kwargs)
        # 同一请求重试仍是同一结果，不会产生第二事件
        with self.assertRaises(MergeRequiredError):
            self.svc.update_entry(self.collab, "album-1", "entry-1", request_id="req-dup", **kwargs)


class CorrectionTests(ServiceTestBase):
    def _case(self, request_id="req-case", **overrides):
        patches = {"hours": "11:00-20:00"}
        patches.update(overrides.pop("factual_patches", {}))
        return self.svc.submit_correction(
            self.merchant, "album-1",
            case_id="case-1", place_id="place-smoke",
            factual_patches=patches,
            removed_facilities=overrides.pop("removed_facilities", ["母婴室"]),
            rationale="商户公告：营业时间调整并撤除母婴设施",
            request_id=request_id,
        )

    def test_merchant_cannot_touch_opinions(self) -> None:
        with self.assertRaises(FactCorrectionBoundaryError) as ctx:
            self._case(factual_patches={"recommendation": "not_recommended"})
        self.assertEqual(["recommendation"], ctx.exception.context["opinion_keys"])
        with self.assertRaises(FactCorrectionBoundaryError):
            self._case(request_id="req-case-2", factual_patches={"recommendation_reason": "x"})

    def test_non_merchant_cannot_submit_correction(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.submit_correction(
                self.reader, "album-1",
                case_id="case-x", place_id="place-smoke",
                factual_patches={"hours": "9-9"}, request_id="req-case-x",
            )

    def test_reviewer_who_edited_album_must_recuse(self) -> None:
        self._add_entry()
        self._case()
        # reviewer-1 没有编辑过，但让这位审核员先参与一次事实维护 → 必须回避
        # （用 author 邀请不成；直接以 reviewer 身份在别处落地编辑）
        editing_reviewer = Identity.of("reviewer-3", Role.REVIEWER, Role.COLLABORATOR)
        self.svc.invite_collaborator(self.author, "album-1", "reviewer-3", request_id="req-invite-r3")
        self.svc.record_place_change(
            editing_reviewer, "album-1", "place-smoke",
            changed_facts={"contact": "010-xxxx"}, request_id="req-r3-edit",
        )
        with self.assertRaises(ReviewerConflict):
            self.svc.decide_correction(
                editing_reviewer, "album-1", "case-1",
                decision="accepted", request_id="req-decide",
            )

    def test_accepted_correction_applies_facts_but_keeps_opinions(self) -> None:
        self._add_entry()
        self._case()
        decision = self.svc.decide_correction(
            self.other_reviewer, "album-1", "case-1",
            decision="accepted", rationale="商户公告属实",
            request_id="req-decide",
        )
        self.assertEqual("CORRECTION_DECIDED", decision.event_type)

        view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        # 事实更新
        self.assertEqual(2, view["current_place_version"])
        self.assertIn("hours_changed", view["stale_reasons"])
        self.assertIn("facilities_removed", view["stale_reasons"])
        # 用户观点原样保留
        self.assertEqual("recommended", view["recommendation"])
        self.assertEqual("全程无烟，母婴室在二层", view["recommendation_reason"])
        self.assertEqual("周末排队较久", view["negative_experience"])
        # 裁决记录可追溯
        case = view["accepted_corrections"][0]
        self.assertEqual("case-1", case["case_id"])

    def test_correction_decision_is_idempotent(self) -> None:
        self._add_entry()
        self._case()
        first = self.svc.decide_correction(
            self.other_reviewer, "album-1", "case-1",
            decision="accepted", request_id="req-decide",
        )
        second = self.svc.decide_correction(
            self.other_reviewer, "album-1", "case-1",
            decision="accepted", request_id="req-decide",
        )
        self.assertIs(first, second)
        # 同一案件换 request_id 重复裁决 → 拒绝
        with self.assertRaises(ValidationError):
            self.svc.decide_correction(
                self.other_reviewer, "album-1", "case-1",
                decision="rejected", request_id="req-decide-again",
            )

    def test_submitter_cannot_review_own_case(self) -> None:
        self._case()
        merchant_reviewer = Identity.of("merchant-1", Role.MERCHANT, Role.REVIEWER)
        with self.assertRaises(ReviewerConflict):
            self.svc.decide_correction(
                merchant_reviewer, "album-1", "case-1",
                decision="accepted", request_id="req-decide",
            )

    def test_collaborator_edit_does_not_change_author_endorsement(self) -> None:
        self._add_entry()
        self.svc.update_entry(
            self.collab, "album-1", "entry-1",
            {"negative_experience": "协作者补充：工作日也排队"},
            expected_version=1, request_id="req-collab-edit",
        )
        view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        self.assertTrue(view["author_endorses"])
        self.assertIsNone(view["withdrew_reason"])

    def test_author_update_after_withdraw_re_endorses(self) -> None:
        self._add_entry()
        self.svc.author_withdraw_endorsement(
            self.author, "album-1", "entry-1", "情况变化", request_id="req-wd",
        )
        self.svc.update_entry(
            self.author, "album-1", "entry-1",
            {"recommendation_reason": "复查后恢复"},
            expected_version=2, request_id="req-reendorse",
        )
        view = self.svc.entry_view(self.reader, "album-1", "entry-1")
        self.assertTrue(view["author_endorses"])
        self.assertIsNone(view["withdrew_reason"])


if __name__ == "__main__":
    unittest.main()
