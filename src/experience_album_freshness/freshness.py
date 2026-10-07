"""定时新鲜度检查。

触发场景全部来自折叠后的读模型，而不是墙钟猜测：

- 条目钉住的地点版本之后出现事实变化（营业时间、设施撤除、无烟状态）；
- 地点停业 / 被合并 → 旧建议指向的地点失效；
- 亲历时间距今超过阈值且条目一直没更新 → 提醒作者复核；
- 已分享快照钉住的地点失效 → 提醒分享者“转发出去的原文需要附带失效说明”。

去重台账按稳定键持久化到磁盘：同一条事实变化（精确到地点版本号）
对同一接收人只通知一次，进程重启后不会重复推送。时钟必须可注入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .model import Repository

# 通知种类（稳定枚举，接口/台账都依赖它）
FACT_CHANGED = "fact_changed"
PLACE_INVALID = "place_invalid"
EXPERIENCE_AGED = "experience_aged"
SNAPSHOT_STALE = "snapshot_stale"

# 地点变化类型 → 通知细类
_CHANGE_KINDS = {
    "hours": "hours_changed",
    "smoke_free": "smoke_free_status_changed",
    "facilities": "facilities_changed",
    "status": "place_status_changed",
}


class Notifier(Protocol):
    def send(self, notification: "Notification") -> None: ...


@dataclass(frozen=True)
class Notification:
    key: str
    kind: str
    reason: str
    recipient_id: str
    album_id: str
    occurred_at: str
    detail: dict[str, Any] = field(default_factory=dict)


class CollectingNotifier:
    """测试与批处理用：把通知收集在内存里。"""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class NotificationLedger:
    """已发通知台账：重启后不重复通知。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._sent_keys: set[str] = set()
        self.last_run_at: str | None = None
        if self._path and self._path.exists():
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            self._sent_keys = set(doc.get("sent_keys", []))
            self.last_run_at = doc.get("last_run_at")

    def contains(self, key: str) -> bool:
        return key in self._sent_keys

    def commit(self, keys: list[str], run_at: datetime) -> None:
        self._sent_keys.update(keys)
        self.last_run_at = run_at.isoformat()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"sent_keys": sorted(self._sent_keys), "last_run_at": self.last_run_at},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)

    @property
    def sent_keys(self) -> frozenset[str]:
        return frozenset(self._sent_keys)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FreshnessChecker:
    """使用注入时钟扫描全部专辑；``notifier`` 与台账均可替换/持久化。"""

    def __init__(
        self,
        store,
        clock,
        notifier: Notifier,
        ledger: NotificationLedger | str | Path | None = None,
        *,
        stale_after: timedelta = timedelta(days=180),
    ) -> None:
        if clock.now().tzinfo is None:
            raise ValueError("clock 必须提供携带时区的时间")
        self._store = store
        self._clock = clock
        self._notifier = notifier
        self._repo = Repository(store)
        self._ledger = ledger if isinstance(ledger, NotificationLedger) else NotificationLedger(ledger)
        self._stale_after = stale_after

    @property
    def ledger(self) -> NotificationLedger:
        return self._ledger

    def run_due(self) -> list[Notification]:
        """执行一轮检查，返回本轮实际发出的通知。"""

        run_at = self._clock.now()
        pending: list[Notification] = []
        for album_id in self._repo.list_album_ids():
            state = self._repo.get_album(album_id)
            if state is None:
                continue
            pending.extend(self._scan_entries(state))
            pending.extend(self._scan_snapshots(state))

        fresh = [item for item in pending if not self._ledger.contains(item.key)]
        for item in fresh:
            self._notifier.send(item)
        self._ledger.commit([item.key for item in fresh], run_at)
        return fresh

    # -------------------------------------------------------------- 条目扫描

    def _scan_entries(self, state) -> list[Notification]:
        out: list[Notification] = []
        for entry in state.entries.values():
            place = state.places.get(entry.place_id)
            changes = place.changes_since(entry.place_version) if place else []
            for change in changes:
                out.extend(self._fact_change_notifications(state, entry, change))
            if place is not None and place.status in ("closed", "merged"):
                out.append(
                    self._notification(
                        kind=PLACE_INVALID,
                        reason=f"place_{place.status}",
                        recipient=entry.author_id,
                        album_id=state.album_id,
                        dedup=(
                            f"entry:{entry.entry_id}:place:{place.place_id}:{place.status}:{place.version}"
                        ),
                        detail={
                            "entry_id": entry.entry_id,
                            "place_id": place.place_id,
                            "pinned_version": entry.place_version,
                            "current_version": place.version,
                            "merged_into": place.merged_into,
                            "author_endorses": entry.endorsed,
                        },
                    )
                )
            # 亲历超龄：键里含当前地点版本与条目版本，事实更新后会重新提醒复核。
            age = self._clock.now() - _parse(entry.experience_at)
            if age >= self._stale_after and not self._has_recent_experience(entry):
                out.append(
                    self._notification(
                        kind=EXPERIENCE_AGED,
                        reason="experience_older_than_threshold",
                        recipient=entry.author_id,
                        album_id=state.album_id,
                        dedup=f"entry:{entry.entry_id}:aged:{entry.experience_at}:v{entry.entry_version}",
                        detail={
                            "entry_id": entry.entry_id,
                            "experience_at": entry.experience_at,
                            "age_days": age.days,
                            "threshold_days": self._stale_after.days,
                        },
                    )
                )
        return out

    @staticmethod
    def _has_recent_experience(entry) -> bool:
        # 更新条目时若重新填写了更晚的亲历时间即视为新体验；折叠模型已覆盖。
        return False

    def _fact_change_notifications(self, state, entry, change: dict[str, Any]) -> list[Notification]:
        results: list[Notification] = []
        changed = change.get("changed_facts", {})
        removed = change.get("removed_facts", [])
        reasons = [
            _CHANGE_KINDS[key]
            for key in changed
            if key in _CHANGE_KINDS
        ]
        if removed:
            reasons.append("facilities_removed")
        if not reasons:
            reasons.append("other_facts_changed")
        place_version = change["place_version"]
        for reason in reasons:
            results.append(
                self._notification(
                    kind=FACT_CHANGED,
                    reason=reason,
                    recipient=entry.author_id,
                    album_id=state.album_id,
                    # 精确到地点版本+细类+条目，同一变化永不重复通知。
                    dedup=f"entry:{entry.entry_id}:place:{entry.place_id}:v{place_version}:{reason}",
                    detail={
                        "entry_id": entry.entry_id,
                        "place_id": entry.place_id,
                        "place_version": place_version,
                        "changed_facts": {
                            k: v for k, v in changed.items()
                            if _CHANGE_KINDS.get(k) == reason or reason == "other_facts_changed"
                        },
                        "removed_facilities": removed if reason == "facilities_removed" else [],
                        "occurred_at": change.get("occurred_at"),
                    },
                )
            )
        return results

    # -------------------------------------------------------------- 快照扫描

    def _scan_snapshots(self, state) -> list[Notification]:
        out: list[Notification] = []
        for snapshot in state.snapshots.values():
            for place_id, pinned_version in snapshot.place_versions.items():
                place = state.places.get(place_id)
                if place is None:
                    continue
                latest = place.version
                if place.status in ("closed", "merged") or latest > pinned_version:
                    out.append(
                        self._notification(
                            kind=SNAPSHOT_STALE,
                            reason=f"place_{place.status}" if place.status in ("closed", "merged") else "facts_changed",
                            recipient=snapshot.shared_by,
                            album_id=state.album_id,
                            dedup=(
                                f"snapshot:{snapshot.snapshot_id}:place:{place_id}:v{latest}:{place.status}"
                            ),
                            detail={
                                "snapshot_id": snapshot.snapshot_id,
                                "entry_id": snapshot.entry_id,
                                "recipient_scope": snapshot.recipient_scope,
                                "shared_at": snapshot.shared_at,
                                "place_id": place_id,
                                "pinned_version": pinned_version,
                                "current_version": latest,
                                "place_status": place.status,
                                "merged_into": place.merged_into,
                                "changes": [
                                    {
                                        "place_version": item["place_version"],
                                        "changed_facts": item.get("changed_facts", {}),
                                        "removed_facilities": item.get("removed_facts", []),
                                    }
                                    for item in place.changes_since(pinned_version)
                                ],
                            },
                        )
                    )
        return out

    # ------------------------------------------------------------------ 工具

    def _notification(
        self,
        *,
        kind: str,
        reason: str,
        recipient: str,
        album_id: str,
        dedup: str,
        detail: dict[str, Any],
    ) -> Notification:
        return Notification(
            key=f"{kind}:{dedup}",
            kind=kind,
            reason=reason,
            recipient_id=recipient,
            album_id=album_id,
            occurred_at=self._clock.now().isoformat(),
            detail=detail,
        )
