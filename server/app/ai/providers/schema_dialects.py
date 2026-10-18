"""Convert Pydantic-generated JSON Schemas into what each provider accepts.

One source of truth (``ModelSpec`` / agent reply models) produces a Pydantic
JSON Schema. Providers accept different subsets, so we send a simplified,
provider-compatible schema and ALWAYS validate the reply with Pydantic on the
server. Dialects:

``gemini``
    The ``response_json_schema`` subset documented on
    ``GenerateContentConfig`` in google-genai 2.28 ($defs, $ref, type, format,
    description, enum, items, prefixItems, minItems, maxItems, minimum,
    maximum, anyOf, properties, additionalProperties, required). ``oneOf`` is
    rewritten to ``anyOf``; ``const`` to a one-value ``enum``; ``$ref`` keeps
    no siblings; defaults move into descriptions. Variable-length array upper
    bounds move into descriptions to avoid native schema compiler complexity;
    fixed-length arrays keep both bounds. Source models still enforce every bound.
``portable``
    The documented JSON Schema subset above, retaining variable-array upper
    bounds; used for OpenAI-compatible servers in non-strict ``json_schema`` mode.
``openai_strict``
    OpenAI Structured Outputs strict mode: every object lists all properties
    in ``required`` and sets ``additionalProperties: false``; optional fields
    become nullable (``null`` = "use the default"); numeric/array bounds and
    tuple ``prefixItems`` are dropped and restated in descriptions.

Because optional fields may come back as ``null``, consumers call
:func:`drop_nulls` before validation. That is lossless only if every nullable
field in the source model is optional with default ``None`` — checked by
:func:`nullable_required_violations` in the tests.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

Dialect = Literal["gemini", "portable", "openai_strict"]

_GEMINI_KEYS = {
    "$defs", "$ref", "type", "format", "description", "enum", "items", "prefixItems",
    "minItems", "maxItems", "minimum", "maximum", "anyOf", "properties",
    "additionalProperties", "required",
}
_OPENAI_STRICT_KEYS = {
    "$defs", "$ref", "type", "description", "enum", "items", "anyOf", "properties",
    "additionalProperties", "required",
}
_BOUND_WORDS = {
    "minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<",
}


class SchemaDialectError(ValueError):
    """The schema cannot be expressed in the requested dialect."""


def to_dialect(schema: dict[str, Any], dialect: Dialect) -> dict[str, Any]:
    """Return a converted deep copy of ``schema``."""
    if schema.get("type") != "object":
        raise SchemaDialectError("root schema must be an object")
    out = _clean(copy.deepcopy(schema), dialect)
    if dialect == "openai_strict":
        out = _strictify(out)
    return out


def _fmt(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


def _clean(node: Any, dialect: Dialect) -> Any:
    if isinstance(node, list):
        return [_clean(v, dialect) for v in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    notes: list[str] = []

    if "const" in node:
        node["enum"] = [node.pop("const")]
    if "oneOf" in node:
        node["anyOf"] = list(node.pop("oneOf")) + list(node.get("anyOf", []))
    if "default" in node:
        default = node.pop("default")
        if default is not None and len(node.get("enum", [None, None])) != 1:
            notes.append(f"default {_fmt(default)}")

    if dialect == "gemini" and node.get("type") == "array" and "maxItems" in node:
        upper = node["maxItems"]
        if node.get("minItems", 0) != upper:
            # Large variable bounds can exhaust Gemini's native schema compiler even in a small schema. Preserve
            # fixed coordinate lengths on the wire; the original Pydantic model remains the authority for all limits.
            node.pop("maxItems")
            notes.append(f"at most {upper} items")

    if dialect == "openai_strict":
        if "prefixItems" in node:
            prefix = [_clean(p, dialect) for p in node.pop("prefixItems")]
            uniq: list[Any] = []
            for p in prefix:
                if p not in uniq:
                    uniq.append(p)
            node["items"] = uniq[0] if len(uniq) == 1 else {"anyOf": uniq}
        lo, hi = node.pop("minItems", None), node.pop("maxItems", None)
        if lo is not None and lo == hi:
            notes.append(f"exactly {lo} items")
        elif lo is not None or hi is not None:
            notes.append(f"{lo if lo is not None else 0}..{hi if hi is not None else 'n'} items")
        for key, word in _BOUND_WORDS.items():
            if key in node:
                notes.append(f"{word} {node.pop(key)}")
        for key in ("multipleOf", "minLength", "maxLength", "pattern", "format"):
            if key in node:
                notes.append(f"{key} {node.pop(key)}")

    for key in ("properties", "$defs"):
        if isinstance(node.get(key), dict):
            node[key] = {k: _clean(v, dialect) for k, v in node[key].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            node[key] = _clean(node[key], dialect)
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [_clean(v, dialect) for v in node["prefixItems"]]
    if isinstance(node.get("anyOf"), list):
        flat: list[Any] = []
        for v in node["anyOf"]:
            v = _clean(v, dialect)
            if isinstance(v, dict) and set(v) == {"anyOf"}:
                flat.extend(v["anyOf"])
            else:
                flat.append(v)
        deduped: list[Any] = []
        for v in flat:
            if v not in deduped:
                deduped.append(v)
        node["anyOf"] = deduped

    if "$ref" in node:  # both Gemini and OpenAI strict reject siblings of $ref
        return {"$ref": node["$ref"]}

    if isinstance(node.get("properties"), dict):
        required = list(node.get("required", []))
        for name, prop in node["properties"].items():
            # Discriminator-like single-value enums must always be present.
            if isinstance(prop, dict) and isinstance(prop.get("enum"), list) and len(prop["enum"]) == 1 and name not in required:
                required.append(name)
        node["required"] = required

    if notes:
        desc = node.get("description")
        node["description"] = (desc.rstrip(". ") + ". " if desc else "") + "; ".join(notes)

    allowed = _OPENAI_STRICT_KEYS if dialect == "openai_strict" else _GEMINI_KEYS
    return {k: v for k, v in node.items() if k in allowed}


def admits_null(node: Any, defs: dict[str, Any] | None = None) -> bool:
    if not isinstance(node, dict):
        return False
    t = node.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    if "$ref" in node and defs is not None:
        return admits_null(_resolve(node["$ref"], defs), defs)
    return any(admits_null(v, defs) for v in node.get("anyOf", []) + node.get("oneOf", []))


def _resolve(ref: str, defs: dict[str, Any]) -> Any:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise SchemaDialectError(f"unsupported $ref {ref}")
    return defs.get(ref[len(prefix):], {})


def _make_nullable(node: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in node:
        return {**node, "anyOf": [*node["anyOf"], {"type": "null"}]}
    desc = node.get("description")
    inner = {k: v for k, v in node.items() if k != "description"}
    out: dict[str, Any] = {"anyOf": [inner, {"type": "null"}]}
    if desc and "$ref" not in node:
        out["description"] = desc + " (null = default)"
    return out


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    if isinstance(node.get("$defs"), dict):
        node["$defs"] = {k: _strictify(v) for k, v in node["$defs"].items()}
    if node.get("type") == "object":
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            raise SchemaDialectError("strict mode cannot express free-form objects")
        if node.get("additionalProperties") not in (None, False):
            raise SchemaDialectError("strict mode needs additionalProperties: false")
        required = set(node.get("required", []))
        new_props = {}
        for name, prop in props.items():
            prop = _strictify(prop)
            if name not in required and not admits_null(prop):
                prop = _make_nullable(prop)
            new_props[name] = prop
        node["properties"] = new_props
        node["required"] = list(new_props)
        node["additionalProperties"] = False
    if isinstance(node.get("items"), dict):
        node["items"] = _strictify(node["items"])
    if isinstance(node.get("anyOf"), list):
        node["anyOf"] = [_strictify(v) for v in node["anyOf"]]
    return node


def drop_nulls(obj: Any) -> Any:
    """Remove null-valued object keys recursively (list items are kept)."""
    if isinstance(obj, dict):
        return {k: drop_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_nulls(v) for v in obj]
    return obj


def nullable_required_violations(schema: dict[str, Any]) -> list[str]:
    """Paths of required properties that also admit null (breaks drop_nulls)."""
    defs = schema.get("$defs", {})
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for name in node.get("required", []):
                if admits_null(props.get(name), defs):
                    found.append(f"{path}.{name}")
            for name, prop in props.items():
                walk(prop, f"{path}.{name}")
        for key in ("items", "additionalProperties"):
            walk(node.get(key), f"{path}[]")
        for i, v in enumerate(node.get("anyOf", []) + node.get("oneOf", []) + node.get("prefixItems", [])):
            walk(v, f"{path}|{i}")

    walk(schema, "$")
    for name, d in defs.items():
        walk(d, f"$defs.{name}")
    return found


def schema_stats(schema: dict[str, Any]) -> dict[str, int]:
    """Size figures used in call records and tests (chars, object properties)."""
    props = 0

    def walk(node: Any) -> None:
        nonlocal props
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                props += len(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return {"chars": len(json.dumps(schema, separators=(",", ":"))), "properties": props}
