"""测试共用构造。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from experience_album_freshness.clock import FixedClock
from experience_album_freshness.service import FreshnessService
from experience_album_freshness.storage import EventStore

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 10, 7, 9, 0, tzinfo=TZ)


def schema():
    return json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))


def build_world(path=None, clock: FixedClock | None = None):
    """建一个含专辑、协作者、两家门店与两条经验的最小世界。"""
    clock = clock or FixedClock(START)
    store = EventStore(path)
    service = FreshnessService(store, clock)

    service.create_album("req-album", "album-1", "无烟餐厅指南", "owner-a")
    service.add_collaborator("req-collab", "album-1", "collab-b", "owner-a")
    service.register_place(
        "req-place-cafe",
        "place-cafe",
        "巷口无烟咖啡馆",
        {
            "smoke_free": True,
            "hours": "09:00-22:00",
            "baby_care": False,
            "free_storage": False,
        },
    )
    service.register_place(
        "req-place-mall",
        "place-mall-old",
        "老商场服务台",
        {"smoke_free": True, "baby_care": True, "free_storage": True, "hours": "10:00-21:00"},
    )
    service.add_entry(
        "req-entry-1",
        "album-1",
        entry_id="entry-1",
        place_id="place-cafe",
        author_id="owner-a",
        experience_at=datetime(2026, 10, 1, 19, 30, tzinfo=TZ),
        recommendation="无烟区安静，适合带电脑久坐",
        audience_conditions=["独自前往", "需要电源插座"],
        negative_experience="周末晚上排队较久",
        identity_statement="本人当晚到店消费",
        attachments=[
            {
                "attachment_id": "att-1",
                "summary": "店内无烟标识照片",
                "uploader_id": "owner-a",
                "pii": {"phone": "13800000000"},
            }
        ],
    )
    service.add_entry(
        "req-entry-2",
        "album-1",
        entry_id="entry-2",
        place_id="place-mall-old",
        author_id="collab-b",
        experience_at=datetime(2026, 9, 15, 14, 0, tzinfo=TZ).isoformat(),
        recommendation="服务台可免费寄存，母婴室在三楼",
        audience_conditions=["带婴幼儿", "携带行李"],
    )
    return store, service, clock
