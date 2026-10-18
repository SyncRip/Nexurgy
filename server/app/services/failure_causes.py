"""Bounded public causes of final failures, without changing their business outcome.

Only explicit causes and failed definition outcomes are projected here. Failed call
history alone is not evidence that a work unit ultimately failed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..ai.providers.base import clip, redact

MAX_CAUSES = 12
MAX_MESSAGE_CHARS = 300
MAX_INSTANCES = 20
MAX_CALL_IDS = 10
MAX_FIELD_CHARS = 160

FAILURE_INFO_KEYS = ("stage", "retryable", "recovery", "recovery_message", "failure_class")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _text(value: Any, limit: int) -> str:
    # Never stringify mappings or arbitrary objects: they may contain private context.
    return clip(redact(value, []), limit) if isinstance(value, str) else ""


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item, MAX_FIELD_CHARS).strip()
        if text and text not in out:
            out.append(text)
            if len(out) == limit:
                break
    return out


def cause(error: Mapping[str, Any] | None, *, stage: str, definition: str | None = None, name: str | None = None,
          instances: Sequence[str] | None = None, call_ids: Sequence[str] | None = None) -> dict[str, Any] | None:
    """Project one observed error; no message keywords or model names classify it.

    The supplied stage identifies the failed work, and a missing recovery stays
    unknown rather than inventing an action. Messages use the provider redactor
    before clipping, so a truncated key cannot escape the redaction pattern.
    """
    if not isinstance(error, Mapping) or not (code := _identifier(error.get("code"))):
        return None
    details = error.get("details") if isinstance(error.get("details"), Mapping) else {}
    retryable = error.get("retryable")
    if not isinstance(retryable, bool):
        retryable = details.get("retryable")
    out: dict[str, Any] = {
        "stage": _identifier(stage) or _identifier(error.get("stage")) or "unknown",
        "code": code,
        "message": _text(error.get("message"), MAX_MESSAGE_CHARS),
        "retryable": retryable if isinstance(retryable, bool) else False,
        "recovery": _identifier(error.get("recovery")) or _identifier(details.get("recovery")),
    }
    if cls := _identifier(error.get("failure_class")):
        out["failure_class"] = cls
    key = _text(definition if definition is not None else error.get("definition"), MAX_FIELD_CHARS).strip()
    if key:
        out["definition"] = key
    label = _text(name if name is not None else error.get("name"), MAX_FIELD_CHARS).strip()
    if label:
        out["name"] = label
    for field, values, limit in (("instances", instances, MAX_INSTANCES), ("call_ids", call_ids, MAX_CALL_IDS)):
        values = values if values is not None else error.get(field)
        if clean := _strings(values, limit):
            out[field] = clean
    return out


def _project_causes(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    out = []
    for value in values:
        if isinstance(value, Mapping) and (projected := cause(value, stage=value.get("stage"))):
            out.append(projected)
    return out


def public_error(error: Mapping[str, Any] | None, *, causes: Sequence[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Copy a public error, preserving existing details and adding bounded causes.

    Existing details retain their contract except the server-only root trace;
    ``details.causes`` itself always uses the strict cause projection. Explicit
    appended causes precede inferred failed-definition causes so placement
    failures remain visible even when many definitions failed.
    """
    if not isinstance(error, Mapping) or not error:
        return None
    details = deepcopy(dict(error.get("details"))) if isinstance(error.get("details"), Mapping) else {}
    details.pop("trace", None)
    candidates = _project_causes(details.pop("causes", None)) + _project_causes(causes)
    definitions = details.get("definitions")
    if isinstance(definitions, Sequence) and not isinstance(definitions, (str, bytes, bytearray)):
        for definition in definitions:
            if not isinstance(definition, Mapping) or definition.get("status") != "failed":
                continue
            err = definition.get("error")
            if not isinstance(err, Mapping):
                continue
            item = cause(err, stage=_identifier(err.get("stage")) or "modeling", definition=definition.get("key"), name=definition.get("name"),
                         instances=definition.get("instances"))
            if item is not None:
                candidates.append(item)
    projected: list[dict[str, Any]] = []
    for item in candidates:
        if item not in projected:
            projected.append(item)
            if len(projected) == MAX_CAUSES:
                break
    if projected:
        details["causes"] = projected
    out = {"code": deepcopy(error.get("code") or "failed"), "message": deepcopy(error.get("message") or ""), "details": details,
           **{key: deepcopy(error[key]) for key in FAILURE_INFO_KEYS if key in error}}
    # Omitted causes still count: a display cap must not turn a mixed failure into
    # a misleading single class merely because the differing cause is thirteenth.
    classes = {item.get("failure_class") for item in candidates}
    if "failure_class" not in out and projected and len(classes) == 1 and None not in classes:
        out["failure_class"] = next(iter(classes))
    return out
