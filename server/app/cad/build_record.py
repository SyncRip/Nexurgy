"""Build record of a part revision (generation reliability M, design §2.7).

What one build actually made, kept apart from what the model said and from the facts it read:

``{schema, representation, created, not_created, usable, complete, capabilities}``

- ``representation``: ``exact`` | ``nominal`` | ``simplified`` | ``placeholder``, the least exact of the level the
  caller states (template or part property), ``simplified`` when a requirement note says ``simplified:`` or
  its Chinese equivalent, and ``placeholder`` for a mesh reference (no B-rep, no STEP).
- ``created``: every op the kernel built (``ok``): op id, type, summary (text ops add the text and the font used).
- ``not_created``: ``{requirement, reason, …}`` for every requirement the geometry does not carry — an op that failed
  or was not built (``build_failed`` / ``not_built``), a text requirement no built text op carries
  (``text_not_built``: a text requirement of kind ``text``, or a note ``text:`` or its Chinese equivalent listing the lines,
  separated by ``；`` / ``;`` / ``|``), a font the kernel replaced (``font_substituted``: requested, used, impact) and a
  stated simplification (``simplified``).
- ``usable``: the build gave a valid solid with a STEP; ``complete``: usable and nothing is listed as not created.
- ``not_created[].capability`` (generation reliability C, design §6.3): the code of :mod:`app.cad.capabilities` the
  missing item needs — the reply's tag on its note (``NoteReq.capability``: such a note is an omission whatever its
  wording, and the representation is ``simplified``), ``text`` for lettering and fonts, the op's code for an op that
  failed, ``other`` otherwise (an untagged ``simplified:`` note included). ``capabilities``: the catalogue version and
  the codes available to this build (``{version, available}``), only when the caller passes them — a record without
  it (made before the catalogue) cannot say what a rebuild would gain (:func:`app.cad.capabilities.reuse_blocker`).

A blank plate is therefore never a finished nameplate: its lettering requirement stays under ``not_created`` and the
record is not complete. Pure: no database, no kernel.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .capabilities import of_op, record_block

BUILD_RECORD_SCHEMA = "partroute.build_record/2"
REPRESENTATIONS = ("exact", "nominal", "simplified", "placeholder")
_RANK = {r: i for i, r in enumerate(REPRESENTATIONS)}
SIMPLIFIED_PREFIXES = ("simplified:", "简化表示：", "简化表示:")
TEXT_PREFIXES = ("text:", "lettering:", "文字：", "文字:")
_SPLIT = re.compile(r"[；;|\n]+")
_QUOTES = "\"'“”‘’「」『』《》"


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).strip(_QUOTES).casefold()


def worst(*levels: Any) -> str:
    """The least exact of the given representation levels (unknown values ignored; ``exact`` when none)."""
    known = [lv for lv in levels if lv in _RANK]
    return max(known, key=lambda lv: _RANK[lv]) if known else "exact"


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    t = text.strip()
    low = t.casefold()
    for p in prefixes:
        if low.startswith(p.casefold()):
            return t[len(p):].strip()
    return None


def build_record(*, features: Any, warnings: Any = (), requirements: Any = None, representation: str | None = None,
                 usable: bool = True, mesh_only: bool = False, capabilities: Iterable[str] | None = None) -> dict[str, Any]:
    """The record (see the module doc) from a build's feature records and warnings (objects or dicts) and the
    revision's accepted requirement list (dicts or models); ``capabilities``: the codes available to this build
    (recorded with the catalogue version; None for a record derived later, which then has no capability block)."""
    created: list[dict[str, Any]] = []
    not_created: list[dict[str, Any]] = []
    built_text: list[str] = []
    for f in features or []:
        status, op_id, kind, summary = _get(f, "status"), _get(f, "op_id"), _get(f, "type"), _get(f, "summary")
        if status == "ok":
            item: dict[str, Any] = {"op_id": op_id, "type": kind, "summary": summary}
            det = _get(f, "detail") or {}
            if kind == "text" and isinstance(det, dict):
                item["text"] = det.get("text")
                item["font_used"] = (det.get("font") or {}).get("used")
                built_text.append(_norm(det.get("text") or ""))
            created.append(item)
        elif status in ("error", "skipped"):
            not_created.append({"requirement": f"{op_id} {kind}: {summary}", "reason": "build_failed" if status == "error" else "not_built",
                                "op_id": op_id, "capability": of_op(kind)})
    for w in warnings or []:
        if _get(w, "code") != "font_substituted":
            continue
        d = _get(w, "details") or {}
        not_created.append({"requirement": f"font {d.get('requested')} for {_get(w, 'op_id')} \"{d.get('text') or ''}\"",
                            "reason": "font_substituted", "op_id": _get(w, "op_id"), "requested": d.get("requested"),
                            "used": d.get("used"), "impact": d.get("impact"), "capability": "text"})
    simplified = False
    for r in requirements or []:
        kind, rid = _get(r, "kind"), _get(r, "id")
        if kind == "text":
            lines = [str(_get(r, "text") or "")]
        elif kind == "note":
            text = str(_get(r, "text") or "")
            tag = _get(r, "capability")
            if tag or _strip_prefix(text, SIMPLIFIED_PREFIXES) is not None:
                simplified = True
                not_created.append({"requirement": text, "reason": "simplified", "requirement_id": rid, "capability": tag or "other"})
                continue
            body = _strip_prefix(text, TEXT_PREFIXES)
            if body is None:
                continue
            lines = [s for s in _SPLIT.split(body) if _norm(s)]
        else:
            continue
        for line in lines:
            want = _norm(line)
            if want and not any(want == b or want in b for b in built_text):
                not_created.append({"requirement": f"text \"{line.strip()}\"", "reason": "text_not_built", "requirement_id": rid,
                                    "capability": "text"})
    level = worst(representation, "simplified" if simplified else None, "placeholder" if mesh_only else None)
    ok = bool(usable) and not mesh_only
    out = {"schema": BUILD_RECORD_SCHEMA, "representation": level, "created": created, "not_created": not_created,
           "usable": ok, "complete": ok and not not_created}
    if capabilities is not None:
        out["capabilities"] = record_block(capabilities)
    return out


def with_representation(record: dict[str, Any], *levels: Any) -> dict[str, Any]:
    """The record with its representation lowered to the least exact of ``levels`` (a part property set later)."""
    level = worst(record.get("representation"), *levels)
    return record if level == record.get("representation") else {**record, "representation": level}


__all__ = ["BUILD_RECORD_SCHEMA", "REPRESENTATIONS", "build_record", "with_representation", "worst"]
