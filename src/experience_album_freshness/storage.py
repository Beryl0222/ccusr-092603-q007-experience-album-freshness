"""事件溯源存储。

一个 JSON 文件保存全部事件、请求幂等索引和已发通知键。
每次追加都整文件原子落盘，使进程重启后：
- 同一 request_id 不会再次执行；
- 同一通知键不会重复通知。

生产环境可替换为数据库实现，接口保持不变。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional


class VersionOccupied(RuntimeError):
    """同一聚合的同一版本号已被占用。"""

    def __init__(self, aggregate_id: str, version: int, occupant: Mapping[str, Any]) -> None:
        super().__init__(f"{aggregate_id} 版本 {version} 已存在事件 {occupant.get('event_id')}")
        self.aggregate_id = aggregate_id
        self.version = version
        self.occupant = dict(occupant)


def canonical_json(value: Any) -> str:
    """稳定序列化：用作同版本异内容比较与快照哈希。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class EventStore:
    def __init__(self, path: Optional[str | os.PathLike[str]] = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        self._by_aggregate: dict[str, list[dict[str, Any]]] = {}
        self._requests: dict[str, list[str]] = {}
        self._notifications: set[str] = set()
        self._versions: dict[str, int] = {}
        self._version_owner: dict[tuple[str, int], dict[str, Any]] = {}
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        assert self._path is not None
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        for envelope in raw.get("events", []):
            self._index_event(envelope)
        self._requests = {key: list(value) for key, value in raw.get("requests", {}).items()}
        self._notifications = set(raw.get("notifications", []))

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": self._events,
            "requests": self._requests,
            "notifications": sorted(self._notifications),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=self._path.name + ".", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _index_event(self, envelope: Mapping[str, Any]) -> None:
        self._events.append(dict(envelope))
        aggregate_id = envelope["aggregate_id"]
        self._by_aggregate.setdefault(aggregate_id, []).append(dict(envelope))
        version = int(envelope["version"])
        self._versions[aggregate_id] = max(self._versions.get(aggregate_id, 0), version)
        self._version_owner[(aggregate_id, version)] = dict(envelope)

    # --------------------------------------------------------------------- 事件

    def append_event(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """追加事件；同聚合版本号必须严格递增且每个版本至多一个事件。"""
        aggregate_id = str(envelope["aggregate_id"])
        version = int(envelope["version"])
        expected = self._versions.get(aggregate_id, 0) + 1
        if version != expected:
            occupant = self._version_owner.get((aggregate_id, version))
            if occupant is not None:
                raise VersionOccupied(aggregate_id, version, occupant)
            raise ValueError(
                f"{aggregate_id} 版本 {version} 不连续，下一版本应为 {expected}"
            )
        stored = dict(envelope)
        self._index_event(stored)
        self._flush()
        return stored

    def commit(self, request_id: str, envelopes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """一次请求的全部事件与幂等索引原子提交（单次落盘）。

        任一事件版本冲突时整体不写入；文件原子替换保证崩溃后要么
        整批可见、要么完全不可见，因此同一 request_id 恰好执行一次。
        """
        if self._requests.get(request_id) is not None:
            raise AssertionError(f"请求 {request_id} 已提交，应先走幂等检查")

        staged_indexes: list[tuple[dict[str, Any], str, int]] = []
        next_versions = dict(self._versions)
        for envelope in envelopes:
            aggregate_id = str(envelope["aggregate_id"])
            version = int(envelope["version"])
            expected = next_versions.get(aggregate_id, 0) + 1
            if version != expected:
                occupant = self._version_owner.get((aggregate_id, version))
                if occupant is not None:
                    raise VersionOccupied(aggregate_id, version, occupant)
                raise ValueError(
                    f"{aggregate_id} 版本 {version} 不连续，下一版本应为 {expected}"
                )
            staged_indexes.append((dict(envelope), aggregate_id, version))
            next_versions[aggregate_id] = version

        stored: list[dict[str, Any]] = []
        for envelope, _, _ in staged_indexes:
            self._index_event(envelope)
            stored.append(envelope)
        self._requests[request_id] = [str(envelope["event_id"]) for envelope in stored]
        self._flush()
        return stored

    def occupant_at(self, aggregate_id: str, version: int) -> Optional[dict[str, Any]]:
        occupant = self._version_owner.get((aggregate_id, version))
        return dict(occupant) if occupant else None

    def events(self, aggregate_id: Optional[str] = None) -> list[dict[str, Any]]:
        if aggregate_id is None:
            return [dict(event) for event in self._events]
        return [dict(event) for event in self._by_aggregate.get(aggregate_id, [])]

    def version(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    # ----------------------------------------------------------------- 幂等索引

    def request_event_ids(self, request_id: str) -> Optional[list[str]]:
        ids = self._requests.get(request_id)
        return list(ids) if ids is not None else None

    # ----------------------------------------------------------------- 通知去重

    def has_notification(self, key: str) -> bool:
        return key in self._notifications

    def mark_notification(self, key: str) -> None:
        self._notifications.add(key)
        self._flush()
