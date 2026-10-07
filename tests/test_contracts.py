from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from experience_album_freshness.access import AttachmentSummary, Identity, Role
from experience_album_freshness.clock import FixedClock
from experience_album_freshness.contracts import validate_event
from experience_album_freshness.service import AlbumService
from experience_album_freshness.store import EventStore


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_envelope_fields_have_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_specific_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="ENTRY_ADDED", payload={})
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.experience_at", "required"), [(issue.field, issue.code) for issue in issues])

    def test_unknown_event_is_rejected(self) -> None:
        event = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(event, self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(issue.field, issue.code) for issue in issues])


class ServiceEventsMatchSchemaTests(unittest.TestCase):
    """服务落地的每一种事件信封都必须能通过交换契约校验。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.tz = timezone(timedelta(hours=8))
        cls.clock = FixedClock(datetime(2026, 9, 1, 12, tzinfo=cls.tz))
        cls.store = EventStore()
        cls.svc = AlbumService(cls.store, cls.clock)
        cls.author = Identity.of("a1", Role.AUTHOR)
        cls.merchant = Identity.of("m1", Role.MERCHANT)
        cls.reviewer = Identity.of("r9", Role.REVIEWER)

    def test_full_lifecycle_events_validate(self) -> None:
        svc = self.svc
        tz = self.tz
        svc.create_album(self.author, "album-contract", name="契约联调", request_id="c1")
        svc.invite_collaborator(self.author, "album-contract", "c2-user", request_id="c2")
        svc.register_place(
            self.author, "album-contract", "p1", name="门店",
            facts={"smoke_free": True, "hours": "10-22"}, request_id="c3",
        )
        svc.add_entry(
            self.author, "album-contract", "e1", "p1",
            title="t", experience_at=datetime(2026, 8, 1, tzinfo=tz),
            audience_conditions="家庭",
            attachments=[AttachmentSummary(attachment_id="x", kind="receipt", summary="s")],
            request_id="c4",
        )
        svc.update_entry(
            self.author, "album-contract", "e1",
            {"title": "t2"}, expected_version=1, request_id="c5",
        )
        svc.add_comment(
            self.author, "album-contract", comment_id="cm1",
            entry_id="e1", quote="原文片段", request_id="c6",
        )
        svc.share_snapshot(
            self.author, "album-contract", "e1",
            snapshot_id="s1", recipient_scope="public", request_id="c7",
        )
        svc.record_place_change(
            self.author, "album-contract", "p1",
            changed_facts={"hours": "11-20"}, request_id="c8",
        )
        svc.register_place(
            self.author, "album-contract", "p2", name="新店",
            facts={"smoke_free": False}, request_id="c9",
        )
        svc.merge_places(
            self.author, "album-contract", "p2", "p1",
            request_id="c10",
        )
        svc.submit_correction(
            self.merchant, "album-contract", case_id="case1", place_id="p1",
            factual_patches={"hours": "12-18"}, rationale="公告", request_id="c11",
        )
        svc.decide_correction(
            self.reviewer, "album-contract", "case1",
            decision="accepted", rationale="属实", request_id="c12",
        )
        svc.author_withdraw_endorsement(
            self.author, "album-contract", "e1", "不再认可", request_id="c13",
        )
        svc.resolve_entry_merge(
            self.author, "album-contract", "e1",
            {
                "title": "合并稿",
                "recommendation": "neutral",
                "recommendation_reason": "r",
                "audience_conditions": "家庭",
                "negative_experience": None,
                "experience_at": datetime(2026, 8, 1, tzinfo=tz),
                "attachments": [],
            },
            base_version=2, request_id="c14",
        )

        seen: set[str] = set()
        for event in self.store.events:
            issues = validate_event(event.to_envelope(), self.schema)
            self.assertEqual([], issues, msg=f"{event.event_type}: {issues}")
            seen.add(event.event_type)
        # 13 种事件类型全部在真实流程中出现过
        self.assertEqual(
            {
                "ALBUM_CREATED", "COLLABORATOR_INVITED", "PLACE_REGISTERED",
                "ENTRY_ADDED", "ENTRY_UPDATED", "COMMENT_ADDED",
                "SNAPSHOT_SHARED", "PLACE_CHANGED", "PLACE_MERGED",
                "CORRECTION_SUBMITTED", "CORRECTION_DECIDED",
                "AUTHOR_WITHDREW", "ENTRY_MERGE_RESOLVED",
            },
            seen,
        )


if __name__ == "__main__":
    unittest.main()
