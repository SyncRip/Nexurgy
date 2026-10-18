"""Bounded model views of frozen completion payloads; never a replacement event log."""
from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from ..errors import AppError

EVENT_CONTEXT_CHARS = 64_000
_COMPACT_FIELDS = {"chain", "spec", "assembly", "application", "recovery"}
_EXACT_LISTS = {"steps", "subassembly_packages", "result_refs", "drawing_ids", "errors", "problems",
                "questions", "open_questions", "incomplete_reasons"}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _invalid() -> AppError:
    return AppError(422, "completion_context_invalid", "The frozen completion context has invalid identity or data")


def validate_event(event: dict[str, Any]) -> None:
    """Reject ambiguous identities rather than silently discarding required context."""
    for key in ("event_id", "operation_id", "root_operation_id"):
        value = event.get(key)
        if not isinstance(value, str) or len(value) != 36:
            raise _invalid()
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise _invalid() from exc
    for key, value in event.items():
        if key.endswith("_id") and value is not None and not isinstance(value, str):
            raise _invalid()
    if type(event.get("generation")) is not int or event["generation"] < 1:
        raise _invalid()
    if type(event.get("event_seq")) is not int or event["event_seq"] < 1:
        raise _invalid()
    if any(not isinstance(event.get(key), str) or not event[key] for key in ("kind", "status", "outcome", "phase")):
        raise _invalid()


def _path(parent: str, key: str | int) -> str:
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _compact(value: Any, event_id: str, path: str, field: str = "") -> Any:
    if isinstance(value, dict):
        # Physical reports key their bulk shape fingerprints by definition rather than
        # using a list. Their final report fingerprint/status remains in the summary.
        if (field == "shapes" and value and all(isinstance(item, dict) and set(item) == {"revision_id", "step_sha256"}
                                               for item in value.values())):
            table = {"omitted_entries": len(value), "ref": {"event_id": event_id, "event_path": path}}
            if len(json_text(table)) < len(json_text(value)):
                return table
        return {key: _compact(item, event_id, _path(path, key), key) for key, item in value.items()}
    if isinstance(value, list):
        if field in _EXACT_LISTS:
            return [_compact(item, event_id, _path(path, i)) for i, item in enumerate(value)]
        if not value:
            return []
        out: dict[str, Any] = {"omitted_items": len(value), "ref": {"event_id": event_id, "event_path": path}}
        # Small scalar lists carry exact identities/reasons more cheaply than a locator.
        # Notes always use summaries, including when the original note is plain text.
        if field != "notes" and all(not isinstance(item, (dict, list)) for item in value) and len(json_text(value)) <= len(json_text(out)):
            return copy.deepcopy(value)
        # Retain every note's outcome/uncertainty, not only notes containing reports.
        # Bulky attempts/occurrences still carry exact immutable fragment locators.
        if field == "notes":
            out["summaries"] = [
                {**(_compact(note, event_id, _path(path, i)) if isinstance(note, dict) else {"value": note}),
                 "event_path": _path(path, i)} for i, note in enumerate(value)
            ]
        return out
    return value


def event_text(events: list[dict[str, Any]], *, max_chars: int = EVENT_CONTEXT_CHARS) -> str:
    """Retain essential summaries or refuse. The full snapshot remains behind each exact event ID."""
    try:
        projected = []
        for event in events:
            validate_event(event)
            projected.append({key: _compact(value, event["event_id"], _path("", key), key)
                              if key in _COMPACT_FIELDS else copy.deepcopy(value)
                              for key, value in event.items()})
        text = json_text({"completion_events": projected, "completion_projection": {
            "version": 2, "full_payload": "immutable completion event; use get_operation_status with operation_id, "
            "event_id and event_path from ref; offset paginates exact JSON text",
            "omissions": "Array/map bodies marked omitted_items/omitted_entries are not evaluated by this view; counts are not acceptance.",
        }})
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc
    if len(text) > max_chars:
        raise AppError(422, "context_over_budget", "Required completion summaries exceed the model-context bound",
                       {"stage": "completion_projection", "required_chars": len(text), "available_chars": max_chars,
                        "event_ids": [e["event_id"] for e in events]})
    return text


def fragment(event: dict[str, Any], path: str, offset: int, chars: int) -> dict[str, Any]:
    """Read exact frozen JSON by RFC 6901 pointer, with lossless character pagination."""
    validate_event(event)
    value: Any = event
    if path and not path.startswith("/"):
        raise AppError(422, "invalid_event_path", "Use a JSON pointer into the frozen event")
    try:
        for raw in path.split("/")[1:] if path else []:
            # RFC 6901 accepts only ~0 and ~1 escape sequences.
            if "~" in raw.replace("~0", "").replace("~1", ""):
                raise ValueError("invalid pointer escape")
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not key.isascii() or not key.isdecimal() or (len(key) > 1 and key[0] == "0"):
                    raise ValueError("invalid array index")
                value = value[int(key)]
            else:
                value = value[key]
        text = json_text(value)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(422, "invalid_event_path", "No such path in the frozen completion event") from exc
    if offset < 0 or offset > len(text) or chars < 1:
        raise AppError(422, "invalid_event_range", "Choose a character range within the frozen JSON")
    end = min(len(text), offset + chars)
    return {"event_id": event["event_id"], "operation_id": event["operation_id"],
            "root_operation_id": event["root_operation_id"], "generation": event["generation"],
            "event_seq": event["event_seq"], "phase": event["phase"], "status": event["status"],
            "outcome": event["outcome"], "event_path": path, "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None, "content_json": text[offset:end]}
