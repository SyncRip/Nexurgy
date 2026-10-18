"""Bind component identities to actual drawing titles, never catalog/BOM mentions.

Title rows are server-owned: their evidence must name real title entities in sent
chunks. The pure merge can then correct a stale number on the SAME stable key,
without merging components or rewriting the skeleton's historical claims.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Iterable

from .spec_identity import is_standard_number

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "－": "-"})


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).translate(_DASHES)).casefold()


def _name(value: Any) -> str:
    lines = [line for line in str(value or "").splitlines() if line.strip()]
    name = _norm(lines[-1] if lines else "")
    # A parenthesized diameter tag has the same written variant with or without
    # its diameter symbol. Do not erase letter codes (B3/B5) or arbitrary tags.
    return re.sub(r"\([øφ⌀](\d+(?:\.\d+)?)\)", r"(\1)", name)


def title_match(definition: dict[str, Any], *, number: Any, name: Any) -> str | None:
    """Return canonical ownership or an exact-name alternative, never fuzzy identity.

    An equal drawing number cannot override a conflicting component name. Old name
    aliases are deliberately not accepted: they can themselves be catalog errors.
    The final title line is the component; preceding lines may name the product.
    """
    own_name, title_name = _name(definition.get("name")), _name(name)
    own_number, title_number = _norm(definition.get("number")), _norm(number)
    names_equal = title_name == own_name
    if (not names_equal and own_number and own_number == title_number and definition.get("kind") == "assembly"):
        # Some BOMs omit the explicit assembly suffix from an assembly's title.
        # This is permitted only with the same canonical number and type; it
        # cannot authorize an alternative-number correction or part/assembly merge.
        names_equal = own_name.removesuffix("装配") == title_name.removesuffix("装配")
    if own_name and not names_equal:
        return None
    if own_number and title_number == own_number:
        return "canonical"
    if (own_name and title_name == own_name and definition.get("kind") != "standard_part"
            and not is_standard_number(definition.get("number")) and not is_standard_number(number)):
        return "alternate"
    return None


def _title_evidence(title: dict[str, Any]) -> list[dict[str, Any]]:
    """Require both actual title labels, with entity references in this frame's chunks."""
    source, chunks = title.get("source"), set(title.get("chunks") or [])
    if not source or not title.get("frame") or not chunks:
        return []
    refs = [e for e in title.get("title_evidence") or [] if isinstance(e, dict) and e.get("source") == source
            and e.get("chunk") in chunks and e.get("entity") and not e.get("region")]
    # These rows were produced from actual title entities by the source reader. A
    # bare number mentioned elsewhere in the frame is not a title binding.
    number, name = _norm(title.get("number")), _name(title.get("name"))
    if not number or not name:
        return []
    roles = title.get("title_entities")
    if roles is not None:
        if not all(roles.get(role) and any(e["entity"] == roles[role] for e in refs) for role in ("number", "name")):
            return []
    elif (not any(number in _norm(e.get("quote")) for e in refs)
          or not any(name in _name(e.get("quote")) for e in refs)):
        return []
    return refs


def validated_title_updates(entries: Iterable[dict[str, Any]], findings: Iterable[Any],
                            titles: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Canonical corrections supported by a stable-key declaration on its real sheet.

    ``findings`` are ChunkFindings-like values; ``titles`` have source, frame,
    number, name, chunks, title_evidence. No source IO or model calls occur here.
    A later chunk alone is not authority. Competing actual titles, standard part
    types and incompatible names/kinds remain unresolved.
    """
    rows = list(titles)
    chunks = list(findings)
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if (entry.get("kind") == "standard_part" or is_standard_number(entry.get("number"))
                or not _name(entry.get("name"))):
            continue
        # An already proven canonical identity cannot be replaced by another
        # similarly named sheet merely because that sheet was read later.
        if any(title_match(entry, number=t.get("number"), name=t.get("name")) == "canonical"
               and _title_evidence(t) for t in rows):
            continue
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for title in rows:
            if title_match(entry, number=title.get("number"), name=title.get("name")) != "alternate":
                continue
            evidence = _title_evidence(title)
            if not evidence:
                continue
            declared = []
            for chunk in chunks:
                if chunk.source != title.get("source") or chunk.seq not in (title.get("chunks") or []):
                    continue
                for comp in chunk.data.get("comps") or []:
                    if (comp.get("k") != entry.get("key") or _norm(comp.get("no")) != _norm(title.get("number"))
                            or _name(comp.get("n")) != _name(title.get("name"))):
                        continue
                    kind = "part" if comp.get("t") == "purchased" else comp.get("t")
                    if kind and entry.get("kind") and kind != entry["kind"]:
                        continue
                    declared.append({"source": chunk.source, "chunk": chunk.seq, "component": copy.deepcopy(comp)})
            if declared:
                candidates[(str(title.get("source")), str(title.get("frame")))] = {
                    "key": entry["key"], "number": title["number"], "name": entry["name"],
                    "old": copy.deepcopy(entry), "title": copy.deepcopy(title), "declarations": declared,
                    "basis": "Actual title number and component name agree with the same stable key's declaration on this sheet; "
                             "the former number has no compatible actual title and remains a historical alias.",
                }
        if len(candidates) == 1:
            result[entry["key"]] = next(iter(candidates.values()))
    return result
