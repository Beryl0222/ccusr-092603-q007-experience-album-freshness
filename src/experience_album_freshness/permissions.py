"""附件可见性：对无权角色隐藏附件中的个人信息。

附件摘要人人可见；附件里的 PII 只对作者本人、上传者、专辑 owner 与协作者开放。
事件流仍保留完整数据，脱敏只发生在阅读视图。
"""

from __future__ import annotations

from typing import Any, Optional

PII_KEY = "pii"


def can_view_pii(album, viewer_id: Optional[str], author_id: str, uploader_id: str) -> bool:
    if not viewer_id:
        return False
    if viewer_id in {author_id, uploader_id}:
        return True
    return album.is_member(viewer_id)


def redact_attachment(raw: dict[str, Any], album, viewer_id: Optional[str], author_id: str) -> dict[str, Any]:
    view = dict(raw)
    uploader_id = str(view.get("uploader_id", ""))
    if PII_KEY in view and not can_view_pii(album, viewer_id, author_id, uploader_id):
        view[PII_KEY] = None
        view["pii_hidden"] = True
    return view
