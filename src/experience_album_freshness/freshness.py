"""定时新鲜度检查。

- 时间全部来自可注入时钟；
- 每条通知有稳定键，已发键落在事件存储里，进程重启后不会重复通知；
- 检查覆盖专辑条目（事实变化、地点撤除/合并、体验超期）与已分享快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol

from .changes import diff_facts, place_lifecycle
from .clock import Clock, SystemClock
from .service import FreshnessService


@dataclass(frozen=True)
class FreshnessNotice:
    key: str
    kind: str
    album_id: str
    entry_id: str
    detail: dict


class Notifier(Protocol):
    def send(self, notice: FreshnessNotice) -> None: ...


class CollectingNotifier:
    """测试/进程内收集器。"""

    def __init__(self) -> None:
        self.sent: list[FreshnessNotice] = []

    def send(self, notice: FreshnessNotice) -> None:
        self.sent.append(notice)


class PrintingNotifier:
    def send(self, notice: FreshnessNotice) -> None:
        print(f"{notice.kind}\t{notice.album_id}\t{notice.entry_id}\t{notice.key}")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed


class FreshnessChecker:
    def __init__(
        self,
        service: FreshnessService,
        notifier: Notifier,
        *,
        stale_after: timedelta = timedelta(days=180),
        clock: Optional[Clock] = None,
    ) -> None:
        self.service = service
        self.notifier = notifier
        self.stale_after = stale_after
        self.clock = clock or SystemClock()

    def _emit_if(
        self, sink: list[FreshnessNotice], key: str, kind: str, album_id: str, entry_id: str, detail: dict
    ) -> None:
        if self.service.store.has_notification(key):
            return
        notice = FreshnessNotice(key, kind, album_id, entry_id, detail)
        self.notifier.send(notice)
        self.service.store.mark_notification(key)
        sink.append(notice)

    # ----------------------------------------------------------------- 条目检查

    def _check_entry(self, registry, album, entry) -> list[FreshnessNotice]:
        delivered: list[FreshnessNotice] = []
        place = registry.places.get(entry.place_id)
        now = self.clock.now()

        # 1) 亲历时间过久且作者仍认可：提醒体验可能过期。
        if entry.endorsed:
            experience_at = _parse(entry.experience_at)
            if now - experience_at >= self.stale_after:
                key = f"stale:{album.album_id}:{entry.entry_id}:{entry.experience_at}"
                self._emit_if(
                    delivered,
                    key,
                    "experience_stale",
                    album.album_id,
                    entry.entry_id,
                    {
                        "experience_at": entry.experience_at,
                        "age_days": (now - experience_at).days,
                    },
                )

        if place is None:
            return delivered

        # 2) 地点生命周期：撤除、合并。
        lifecycle = place_lifecycle(registry, entry.place_id)
        if lifecycle["state"] in {"removed", "merged"}:
            key = (
                f"lifecycle:{album.album_id}:{entry.entry_id}:"
                f"{entry.place_id}:{lifecycle['state']}:{place.version}"
            )
            self._emit_if(
                delivered,
                key,
                f"place_{lifecycle['state']}",
                album.album_id,
                entry.entry_id,
                lifecycle,
            )

        # 3) 条目锚点之后的事实变化（营业时间变化、设施撤除等）。
        if lifecycle["state"] == "active" and place.version > entry.anchored_place_version:
            changed = diff_facts(place.facts_at(entry.anchored_place_version), dict(place.facts))
            if changed["changed_keys"]:
                key = f"facts:{album.album_id}:{entry.entry_id}:{entry.place_id}:v{place.version}"
                self._emit_if(
                    delivered,
                    key,
                    "place_facts_changed",
                    album.album_id,
                    entry.entry_id,
                    {
                        "anchored_place_version": entry.anchored_place_version,
                        "current_place_version": place.version,
                        "changed": changed["changed"],
                    },
                )
        return delivered

    def _emit_if(
        self, sink: list[FreshnessNotice], key: str, kind: str, album_id: str, entry_id: str, detail: dict
    ) -> None:
        if self.service.store.has_notification(key):
            return
        notice = FreshnessNotice(key, kind, album_id, entry_id, detail)
        self.notifier.send(notice)
        self.service.store.mark_notification(key)
        sink.append(notice)

    # ----------------------------------------------------------------- 快照检查

    def _check_snapshot(self, registry, snapshot) -> list[FreshnessNotice]:
        delivered: list[FreshnessNotice] = []
        album = registry.albums.get(snapshot.album_id)
        for item in snapshot.entries:
            place = registry.places.get(item.place_id)
            current_entry = album.entries.get(item.entry_id) if album else None

            if current_entry is not None and not current_entry.endorsed:
                key = f"snapshot-endorsement:{snapshot.snapshot_id}:{item.entry_id}"
                self._emit_if(
                    delivered,
                    key,
                    "snapshot_author_no_longer_endorses",
                    snapshot.album_id,
                    item.entry_id,
                    {"snapshot_id": snapshot.snapshot_id},
                )

            if place is None:
                continue
            lifecycle = place_lifecycle(registry, item.place_id)
            if lifecycle["state"] in {"removed", "merged"}:
                key = (
                    f"snapshot-lifecycle:{snapshot.snapshot_id}:{item.entry_id}:"
                    f"{lifecycle['state']}:{place.version}"
                )
                self._emit_if(
                    delivered,
                    key,
                    f"snapshot_place_{lifecycle['state']}",
                    snapshot.album_id,
                    item.entry_id,
                    {"snapshot_id": snapshot.snapshot_id, **lifecycle},
                )
            elif place.version > item.place_version:
                changed = diff_facts(place.facts_at(item.place_version), dict(place.facts))
                if changed["changed_keys"]:
                    key = (
                        f"snapshot-facts:{snapshot.snapshot_id}:{item.entry_id}:v{place.version}"
                    )
                    self._emit_if(
                        delivered,
                        key,
                        "snapshot_place_facts_changed",
                        snapshot.album_id,
                        item.entry_id,
                        {
                            "snapshot_id": snapshot.snapshot_id,
                            "anchored_place_version": item.place_version,
                            "current_place_version": place.version,
                            "changed": changed["changed"],
                        },
                    )
        return delivered

    # --------------------------------------------------------------------- 扫一次

    def sweep(self) -> list[FreshnessNotice]:
        registry = self.service.registry()
        delivered: list[FreshnessNotice] = []
        for album in registry.albums.values():
            for entry in album.entries.values():
                delivered.extend(self._check_entry(registry, album, entry))
        for snapshot in registry.snapshots.values():
            delivered.extend(self._check_snapshot(registry, snapshot))
        return delivered
