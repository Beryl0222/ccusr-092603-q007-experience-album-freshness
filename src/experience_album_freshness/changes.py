"""地点事实差异：让阅读者看到条目基于何时的事实、哪些事实已变化。"""

from __future__ import annotations

from typing import Any, Optional


def diff_facts(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, list[Any]] = {}
    for key in sorted(set(old) | set(new)):
        if key not in old:
            changed[key] = [None, new[key]]
        elif key not in new:
            changed[key] = [old[key], None]
        elif old[key] != new[key]:
            changed[key] = [old[key], new[key]]
    return {
        "changed": changed,
        "changed_keys": sorted(changed),
    }


def place_lifecycle(registry, place_id: str) -> dict[str, Any]:
    place = registry.places.get(place_id)
    if place is None:
        return {"place_id": place_id, "state": "unknown"}
    if place.merged_into:
        return {"place_id": place_id, "state": "merged", "surviving_place_id": place.merged_into}
    if place.removed:
        return {"place_id": place_id, "state": "removed"}
    return {"place_id": place_id, "state": "active"}


def changes_since(registry, place_id: str, anchored_version: int) -> Optional[dict[str, Any]]:
    """条目锚点版本与当前事实之间的差异；地点消失/合并时只给生命周期状态。"""
    place = registry.places.get(place_id)
    if place is None:
        return None
    lifecycle = place_lifecycle(registry, place_id)
    old_facts = place.facts_at(anchored_version)
    diff = diff_facts(old_facts, dict(place.facts))
    return {
        "anchored_place_version": anchored_version,
        "current_place_version": place.version,
        "lifecycle": lifecycle,
        **diff,
    }
