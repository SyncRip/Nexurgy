"""Materials of the AI workspace (S2, docs/agent/stability-fixes.zh-CN.md §S2): a request text longer than
``limits.CHAT_TEXT_CHARS`` is not refused. It is saved as a project material — a Markdown file version through the
ordinary file / document-asset path, so it is listed, versioned and referenceable like an uploaded file — and the
request references it; the request text keeps the beginning and the end of the paste (where the instruction usually
is) and says where the rest is.

How the material is read is decided by the context build (``assistant_context.build_context``: full → digest →
chunked reading → an explicit failure naming the material) or, for ``assembly_create``, by the engineering spec step
(chunked, every chunk recorded on the spec version). The request snapshot's ``materials`` and the message payload's
``material`` say what happened, so the conversation shows it.

A material is a PROJECT file: every project member can see it (the conversation itself stays private). Saving one
needs the permission to add project files (``design``).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..ai import limits
from ..errors import AppError
from ..models import AiConversation, File, FileVersion, User
from ..security import ProjectAccess
from ..storage import save_blob
from . import assets
from . import assistant_refs as refs
from .audit import audit

CONTENT_TYPE = "text/markdown; charset=utf-8"
#: Characters of the paste's beginning / end kept in the request text (the instruction is usually there).
HEAD_CHARS = 600
TAIL_CHARS = 400
_TITLE_STRIP = re.compile(r"^[#>*\-\s]+")
_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def needs_material(text: str) -> bool:
    return len(text or "") > limits.CHAT_TEXT_CHARS


def _title(text: str) -> str:
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    first = _UNSAFE.sub(" ", _TITLE_STRIP.sub("", first)).strip()
    return (first[:60].rstrip() or "AI material")


def save_material(db: Session, user: User, acc: ProjectAccess, conv: AiConversation, text: str) -> dict[str, Any]:
    """The text as a new project file (kind document, version 1, document asset) and its normalised reference.
    Flushes; the caller commits with the request."""
    if not acc.can("design"):
        raise AppError(403, "material_not_allowed",
                       f"a request longer than {limits.CHAT_TEXT_CHARS} characters is saved as a project material, which needs "
                       "the permission to add project files; shorten it or ask a project designer",
                       {"max": limits.CHAT_TEXT_CHARS, "length": len(text)})
    name = f"{_title(text)}.md"
    data = text.encode("utf-8")
    sha = save_blob(db, data, CONTENT_TYPE)
    f = File(project_id=conv.project_id, part_id=None, kind="document", name=name[:300], created_by=user.id)
    db.add(f)
    db.flush()
    fv = FileVersion(file_id=f.id, version=1, blob_sha256=sha, original_name=name[:300],
                     note="Saved from an AI workspace request (S2: longer than the request limit)", created_by=user.id)
    db.add(fv)
    db.flush()
    assets.register_file_version(db, fv)
    audit(db, action="file.upload", subject_type="file", subject_id=f.id, user_id=user.id, project_id=conv.project_id,
          data={"sha256": sha, "name": name, "origin": "ai_material", "conversation_id": str(conv.id), "chars": len(text)})
    # ``material``: later requests that still reference it read it the same way (not as a capped reference).
    ref = {**refs.normalize_ref(db, user, conv.project_id, refs.AiRefInput(kind="file_version", object_id=fv.id)), "material": True,
           "chars": len(text)}
    return {"key": ref["key"], "ref": ref, "file_id": str(f.id), "file_version_id": str(fv.id), "name": name, "chars": len(text),
            "limit": limits.CHAT_TEXT_CHARS}


def snapshot_entry(ref: dict[str, Any], *, new: bool) -> dict[str, Any]:
    """A material as the request snapshot / message payload records it."""
    return {"key": ref.get("key"), "file_version_id": ref.get("object_id"), "name": ref.get("title"), "chars": ref.get("chars"),
            "limit": limits.CHAT_TEXT_CHARS, "new": new}


def _cut_head(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = max(text.rfind("\n", 0, n), text.rfind("。", 0, n), text.rfind(". ", 0, n))
    return text[: cut + 1 if cut > n // 2 else n].rstrip()


def _cut_tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    start = len(text) - n
    cut = text.find("\n", start)
    return text[cut + 1 if 0 <= cut < start + n // 2 else start:].lstrip()


_NOTE = "[The full request ({chars} characters) is the material \"{name}\" v1 referenced with this request; above are its beginning and end.]"
_NOTE_RE = re.compile(r"\n\n\[The full request \(\d+ characters\) is the material \".*\" v1 referenced with this request; above are its "
                      r"beginning and end\.\]$", re.DOTALL)


def request_text(text: str, material: dict[str, Any]) -> str:
    """The request as sent on: the paste's beginning and end and where the whole text is."""
    text = text.strip()
    head, tail = _cut_head(text, HEAD_CHARS), _cut_tail(text, TAIL_CHARS)
    return f"{head}\n…\n{tail}\n\n" + _NOTE.format(chars=material["chars"], name=material["name"])


def is_excerpt(text: str) -> bool:
    """Is this request text the shortened form of a pasted material (e.g. re-sent with another operation)?"""
    return bool(_NOTE_RE.search(text or ""))
