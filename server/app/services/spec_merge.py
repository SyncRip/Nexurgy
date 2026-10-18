"""Deterministic merge of chunk readings into one engineering spec (S7, docs/agent/stability-fixes.zh-CN.md §S7).

Pure: no database and no model. Input: the skeleton the structure reading returned first (:func:`skeleton_from`,
generation reliability A, design §6.1) and the compact findings every chunk returned (``ai/spec_agent.ChunkReply``),
each keyed by the chunk the server really sent (sequence + source). Output: the W0 §5 spec in reply form (the service
checks the evidence and finishes it exactly as before), plus what rules cannot settle, which the caller may send to the
model once (``spec_resolve``) and pass back:

- ``adoptable`` (Turbo): conflicts and blocking missing values a value has to be adopted for (``adopt``).

Rules:

- **Identity = key** (``spec_identity.identity_key``): the drawing number; for a standard part the standard number AND
  size (a standard number alone is only the type); else the name plus the parent. Every skeleton entry is a definition
  (its key fixed before the chunks were read, its uses are the tree). A chunk mention is the skeleton component whose
  key it writes (always: a number / size it writes with the key is one more reading of that component's number / size,
  a conflict when it differs — never another component), else the component of its identity key, else the known
  components its ``of`` names (a generic mention: its facts apply to each, it is never a definition), else the one
  known component whose name or other name it writes. With a skeleton, a component it lacks is a new definition only
  when a chunk declares it (``b``, resting on its number or a BOM line / count statement of that chunk); every other
  mention — and a name several known components carry, written without ``of`` — is recorded with its facts
  (``unbound``) and applied to nothing: no definition, no instances. Without a skeleton every new component is a
  definition (numbered by its number, else name + parent). Names are never compared by similarity.
- **Tree.** The skeleton owns the containment of the components it places: a chunk's count for a skeleton pair is
  one more reading of that count; a chunk's count of a placed component under another parent (a top-level BOM total
  of a sub-assembly member) is recorded (``restated``), never more instances. Components the skeleton does not place
  get their containment from the chunks.
- **Interfaces** (design §6.2): the assembly-level geometry of the skeleton, keyed ``if.<key>``; a chunk relation to
  ``if.<key>`` joins the component's instances to that datum of the owner assembly (``@<owner>``).
- **Facts.** One fact per (definition, field). Equal normalised values merge their evidence; different values are a
  conflict that keeps every value with its source (fact status ``conflicting``). A number written without a unit takes
  the unit its source declares (the Chinese equivalent of ``units: mm``), otherwise it stays ``unknown`` with a ``<subject>.unit`` question.
- **Quantities** only from containment statements (BOM lines, "n x"): equal repeats merge, different counts conflict.
- **Keys** D-/I-/M-/F-/N-/Q-/C-/A- in order of first appearance (the skeleton's first); the previous version's keys are
  reused (definitions by identity key, then by name — :func:`definition_keys`, one policy for the skeleton's entries and
  the definitions the chunks add; the rest by subject / signature). The root is D-00 unless a previous key applies.
- **Readings** without a located source (a skeleton count whose quote was not found) count only when no located
  reading states the same thing.
- **Turbo decisions** (generation supplement D, design §2.3): every adopted value carries its ``trigger`` (conflict →
  ``conflict`` with the other readings in ``alternatives`` and every reading's locator in ``sources``, missing value /
  unit → ``missing``) and the definitions / instances it affects. An adoption for anything
  that is not an open item is dropped (``record.dropped_decisions``); one for a value the sources state without a
  conflict also leaves a note on its definition. A count stated once is never overridden. A re-extraction reuses the
  previous version's decisions that still answer an open item (:func:`reusable_adoptions`).

The result does not depend on the order in which the chunks are given (they are sorted by chunk sequence first).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..ai.spec_agent import DELIVERABLE_KINDS, interface_problems
from .drawing_identity import validated_title_updates
from .spec_identity import compatible_standard_readings, identity_key, is_standard_number, series_tags, size_of, tags_conflict

SERVER_SUMMARY = "Merged by the server from {chunks} chunk reading(s): {defs} component(s), {facts} fact(s), {conflicts} conflict(s)."
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "－": "-"})
_UNITS = {"mm": "mm", "毫米": "mm", "millimeter": "mm", "millimetre": "mm", "millimeters": "mm", "millimetres": "mm", "cm": "cm",
          "厘米": "cm", "m": "m", "米": "m", "in": "in", "inch": "in", "inches": "in", '"': "in", "deg": "deg", "°": "deg", "º": "deg",
          "degree": "deg", "degrees": "deg", "度": "deg", "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "件": "pcs", "个": "pcs"}
_UNIT_DECL = re.compile(r"(?:单位|units?)\s*(?:为|是|=|:|：)?\s*(?:mm|毫米|millimet)", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
#: Fields that never block building geometry or the assembly: they flow into the BOM / process / quote and stay
#: ``unknown`` until answered (material grade, density, mass, finish, supplier, cost …). Found with the first real
#: model round (2026-10-10): asking for steel grades the request said were not provided blocked every part.
NON_BLOCKING_FIELD = re.compile(r"(material|grade|density|densities|mass|weight|finish|surface|coating|colou?r|treatment|"
                                r"supplier|vendor|cost|price|standard_ref|marking|"
                                # physical properties: the static nominal shape never depends on them (capabilities G07)
                                r"elastic|modulus|hardness|shore|stiffness|poisson|compress|squeeze|thermal|conductiv|"
                                r"tensile|yield|strength)", re.I)


def field_blocks(subject: str) -> bool:
    """Whether an open question about ``subject`` stops what it affects from being built (decided from the field name;
    a ``<subject>.unit`` question follows the field it qualifies)."""
    subject = str(subject or "")
    if subject.endswith(".unit"):
        subject = subject[:-5]
    return not NON_BLOCKING_FIELD.search(subject.rsplit(".", 1)[-1])


def declares_mm(text: str) -> bool:
    """The text declares millimetres as its unit (the Chinese equivalent of ``units: mm``, ``units: mm``)."""
    return bool(_UNIT_DECL.search(text or ""))


# --- normalisation ----------------------------------------------------------------------------------------------------


def _nfkc(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s)).translate(_DASHES)


def norm_name(name: Any) -> str:
    return re.sub(r"\s+", "", _nfkc(name or "")).casefold()


def norm_unit(unit: Any) -> str | None:
    if unit is None:
        return None
    s = _nfkc(unit).strip()
    return _UNITS.get(s.casefold(), s) if s else None


def norm_field(f: Any) -> str:
    s = re.sub(r"[\s.\-]+", "_", _nfkc(f).strip()).strip("_")
    return s.lower() or "value"


def coerce_value(v: Any) -> Any:
    """A number written as text becomes a number; text is trimmed."""
    if isinstance(v, str):
        s = _nfkc(v).strip()
        if _NUMBER.fullmatch(s):
            f = float(s)
            return int(f) if f.is_integer() and "." not in s else f
        return v.strip()
    return v


def value_for(subject: str, v: Any) -> Any:
    """The stored value of a fact: numbers written as text become numbers, except for descriptive fields (material
    grade, finish, …), where a number is text ("45" steel stays "45")."""
    if field_blocks(subject):
        return coerce_value(v)
    if _is_number(v):
        return str(v)
    return v.strip() if isinstance(v, str) else v


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _vkey(v: Any, unit: str | None) -> tuple[Any, ...]:
    if _is_number(v):
        return ("n", round(float(v), 9), unit)
    if isinstance(v, str):
        return ("s", re.sub(r"\s+", " ", _nfkc(v)).strip().casefold(), unit)
    return ("j", json.dumps(v, sort_keys=True, ensure_ascii=False), unit)


def _key_no(key: str) -> int:
    m = re.search(r"(\d+)$", key)
    return int(m.group(1)) if m else 0


# --- input / output ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkFindings:
    """What one chunk the server sent stated (``data``: the canonical compact reply)."""

    seq: int
    source: str
    data: dict[str, Any]
    kind: str = "text"
    declares_mm: bool = False


@dataclass
class Merged:
    content: dict[str, Any]
    adoptable: list[dict[str, Any]]
    record: dict[str, Any]
    # Internal source candidates for unresolved fields, bound through this merge's
    # canonical identity map. The service must still verify the original evidence.
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Skeleton:
    """The structure read before the chunks (:func:`skeleton_from`): ``entries`` one per component with its definition
    key (``{key, id, name, number, size, aliases, kind, uses: [{parent, n, q, ev}], ev}``), ``interfaces``
    (``{key, owner, name, kind, at, dir, points, status, how, ev}``), the ``root`` key and what was not taken
    (``problems``)."""

    entries: list[dict[str, Any]]
    interfaces: list[dict[str, Any]]
    root: str | None
    problems: list[str] = field(default_factory=list)


@dataclass(eq=False)
class _Mention:
    seq: int
    idx: int
    source: str
    k: str
    name: str
    number: str | None
    t: str | None
    tpl: dict[str, Any] | None
    size: str | None = None
    of: tuple[str, ...] = ()
    basis: str | None = None
    parent_name: str | None = None  # the parent this chunk puts it in (by name)
    visual: bool = False

    @property
    def order(self) -> tuple[int, int]:
        return (self.seq, self.idx)

    @property
    def nname(self) -> str:
        return norm_name(self.name)

    @property
    def ident(self) -> str | None:
        """Its identity key when the chunk numbers it (a drawing number, or a standard number with a size)."""
        key = identity_key({"number": self.number, "size": self.size, "name": self.name})
        return key if key and not key.startswith("name:") else None


@dataclass(eq=False)
class _Def:
    mentions: list[_Mention]
    skel: dict[str, Any] | None = None
    ident: str | None = None
    key: str = ""
    notes: list[str] = field(default_factory=list)
    index: int = 0  # skeleton entries: their place in the skeleton
    title_update: dict[str, Any] | None = None

    @property
    def order(self) -> tuple[int, int]:
        if self.skel is not None:
            return (-1, self.index)
        return min((m.order for m in self.mentions), default=(1 << 30, 0))

    @property
    def number(self) -> str | None:
        return ((self.title_update or {}).get("number") or (self.skel or {}).get("number")
                or next((m.number for m in sorted(self.mentions, key=lambda m: m.order) if m.number), None))

    @property
    def size(self) -> str | None:
        if (self.skel or {}).get("size"):
            return str(self.skel["size"])  # type: ignore[index]
        return size_of(next((m.size for m in self.mentions if m.size), None), self.name) if is_standard_number(self.number) else None

    @property
    def name(self) -> str:
        if self.skel is not None:
            return str(self.skel.get("name") or self.key)
        ms = sorted(self.mentions, key=lambda m: m.order)
        return next((m.name for m in ms if m.number), None) or ms[0].name

    @property
    def aliases(self) -> list[str]:
        """Other names of this component: the skeleton's, then the other names the chunks wrote for it."""
        own = norm_name(self.name)
        out: dict[str, str] = {}
        historical = [(self.skel or {}).get("number")] if self.title_update else []
        for n in [*historical, *((self.skel or {}).get("aliases") or []), *(m.name for m in sorted(self.mentions, key=lambda m: m.order))]:
            nn = norm_name(n)
            if nn and nn != own and nn not in out and not re.fullmatch(r"[A-Za-z]-\d+|c\d+", str(n)):
                out[nn] = str(n)[:200]
        return list(out.values())[:30]


def _numbered(ident: str | None) -> str | None:
    """An identity key that is a number (drawing number, or standard number + size); None for name + parent."""
    return ident if ident and not ident.startswith("name:") else None


def definition_keys(items: list[tuple[str, str | None, str]], *, root: int | None, previous: dict[str, Any] | None) -> list[str]:
    """The definition key of every item ``(key already given or "", identity key, name)``, in order: a given key stays;
    else the previous version's key of the same identity key, then of the same name (unless both are numbered
    differently: two numbers are two parts); the root (index ``root``) D-00; the rest the next free ``D-NN``. One policy
    for the skeleton's entries (:func:`skeleton_from`) and the definitions the chunks add."""
    prev = [d for d in (previous or {}).get("definitions") or [] if isinstance(d, dict) and d.get("key")]
    prev_ident = {i: d["key"] for d in prev if (i := _numbered(identity_key(d)))}
    prev_name: dict[str, list[tuple[str, str | None]]] = {}
    for d in prev:
        prev_name.setdefault(norm_name(d.get("name")), []).append((d["key"], _numbered(identity_key(d))))
    keys = [k for k, _, _ in items]
    used = {k for k in keys if k}
    for j, (_, ident, _) in enumerate(items):
        k = prev_ident.get(_numbered(ident) or "")
        if not keys[j] and k is not None and k not in used:
            keys[j] = k
            used.add(k)
    for j, (_, ident, name) in enumerate(items):
        if keys[j]:
            continue
        mine = _numbered(ident)
        k = next((k for k, i in prev_name.get(norm_name(name), []) if k not in used and not (mine and i and i != mine)), None)
        if k is not None:
            keys[j] = k
            used.add(k)
    if root is not None and not keys[root] and "D-00" not in used:
        keys[root] = "D-00"
        used.add("D-00")
    n = 0
    for j in range(len(keys)):
        if keys[j]:
            continue
        n += 1
        while f"D-{n:02d}" in used:
            n += 1
        keys[j] = f"D-{n:02d}"
        used.add(keys[j])
    return keys


def _slug(k: Any) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", unicodedata.normalize("NFKC", str(k or "")).casefold()).strip("_")
    return (s or "if")[:50]


def skeleton_from(replies: list[dict[str, Any]], *, previous: dict[str, Any] | None = None,
                  locate: Callable[[str], tuple[str, int] | None] | None = None) -> Skeleton:
    """The skeleton of one structure reading (one or two replies of ``ai/spec_agent.SkeletonReply``, the second reusing
    the first's keys). Collect stable model keys before comparing final identities: later detail evidence may correct
    a catalog number that another, different part actually owns. Old numbers remain labels, never identity indexes.
    Compatible final identities merge (numberless ones by name + first parent); unresolved number collisions stay
    distinct and are recorded. ``locate``: where a quote stands (source, chunk) — the evidence checked on finish."""

    def ev(q: Any) -> dict[str, Any] | None:
        hit = locate(str(q)) if (q and locate is not None) else None
        return {"source": hit[0], "chunk": hit[1], "quote": str(q)[:400], "entity": None, "region": None, "detail": "skeleton"} if hit else None

    comps: list[dict[str, Any]] = []
    by_k: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    root_k: str | None = None
    raw_ifs: list[dict[str, Any]] = []
    for r in replies:
        root_k = root_k or r.get("root")
        raw_ifs += list(r.get("ifs") or [])
        for c in r.get("comps") or []:
            number = c.get("no") or None
            m = by_k.get(c["k"])
            if m is None:
                m = {"ks": [], "name": str(c.get("n") or c["k"]), "number": number, "size": None, "aliases": [], "t": None, "pa": [], "q": None,
                     "ident": None}
                comps.append(m)
            if c["k"] not in m["ks"]:
                m["ks"].append(c["k"])
            by_k[c["k"]] = m
            old_name, old_number = m["name"], m["number"]
            if c.get("n"):
                m["name"] = str(c["n"])
            if number:
                m["number"] = number
            if is_standard_number(m["number"]):
                m["size"] = size_of(c.get("sz"), c.get("n")) or m["size"]
            else:
                m["size"] = None
            m["t"] = c.get("t") or m["t"]
            m["q"] = c.get("q") or m["q"]
            old_number_alias = old_number if old_number and number and old_number != number else None
            for a in [old_name, old_number_alias, *(c.get("al") or [])]:
                if a and norm_name(a) != norm_name(m["name"]) and norm_name(a) not in {norm_name(x) for x in m["aliases"]}:
                    m["aliases"].append(str(a)[:200])
            m["pa"] += [u for u in c.get("pa") or [] if u.get("p")]

    # Index only final canonical numbers. Distinct model keys are not aliases merely because an old catalog reused a
    # number. Equal final standard+size identities are the same standard component; custom parts also need the same
    # canonical name and a compatible kind. An unresolved collision has no number identity for downstream binding.
    by_ident: dict[str, list[dict[str, Any]]] = {}
    for m in list(comps):
        ident = _numbered(identity_key(m))
        if ident is None:
            continue
        candidates = by_ident.setdefault(ident, [])
        keep = next((x for x in candidates
                     if (not x["t"] or not m["t"] or x["t"] == m["t"])
                     and (ident.startswith("std:") or norm_name(x["name"]) == norm_name(m["name"]))), None)
        if keep is not None:
            keep["ks"] += m["ks"]
            keep["pa"] += m["pa"]
            keep["aliases"] += [a for a in [m["name"], *m["aliases"]]
                                if norm_name(a) != norm_name(keep["name"]) and a not in keep["aliases"]]
            keep["t"] = keep["t"] or m["t"]
            for k in m["ks"]:
                by_k[k] = keep
            comps.remove(m)
        else:
            m["ident"] = ident
            candidates.append(m)
    for m in comps:
        m["aliases"] = [a for a in m["aliases"] if norm_name(a) != norm_name(m["name"])]
    for ident, candidates in by_ident.items():
        if len(candidates) <= 1:
            continue
        problems.append(f"identity {ident}: distinct skeleton components share a final number: "
                        + ", ".join(f"{m['ks'][0]} ({m['name']})" for m in candidates))
        for m in candidates:
            # Do not assign the ambiguous number to whichever component happens to occur first. A stable key remains
            # authoritative; preserve the literal number on each entry so the conflict can be inspected.
            m["ident"] = f"name:{norm_name(m['name'])}@skeleton:{m['ks'][0]}"
    # a numberless component is its name plus its (first) parent: two entries of one name in one parent are one
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for m in list(comps):
        if m["ident"]:
            continue
        parent = by_k.get((m["pa"] or [{}])[0].get("p") or "")
        sig = (norm_name(m["name"]), str(id(parent)) if parent else "")
        if sig in seen and seen[sig] is not m:
            keep = seen[sig]
            keep["ks"] += m["ks"]
            keep["pa"] += m["pa"]
            keep["aliases"] += [a for a in m["aliases"] if a not in keep["aliases"]]
            for k in m["ks"]:
                by_k[k] = keep
            comps.remove(m)
            continue
        seen[sig] = m
    root = by_k.get(root_k or "")
    parents = {id(by_k[u["p"]]) for m in comps for u in m["pa"] if u["p"] in by_k}
    if root is None:  # not named: the one assembly no other component contains (as the merge decides it)
        tops = [m for m in comps if id(m) in parents and not any(u["p"] in by_k for u in m["pa"])]
        root = tops[0] if len(tops) == 1 else None
    # Frozen skeleton identities precede later canonical corrections. Otherwise
    # a catalog number now used by another definition can steal the old key on
    # re-extraction, invalidating the local keys in already-read chunks.
    original: dict[tuple[str | None, str], set[str]] = {}
    original_kind: dict[str, str | None] = {}
    for entry in (previous or {}).get("skeleton", []):
        if entry.get("key"):
            original.setdefault((identity_key(entry), norm_name(entry.get("name"))), set()).add(entry["key"])
            original_kind[entry["key"]] = entry.get("kind")
    inherited = []
    inherited_used: set[str] = set()
    for m in comps:
        matches = {key for key in original.get((identity_key(m), norm_name(m["name"])), set())
                   if key not in inherited_used and (not m["t"] or not original_kind[key] or m["t"] == original_kind[key])}
        key = next(iter(matches)) if len(matches) == 1 else ""
        inherited.append(key)
        if key:
            inherited_used.add(key)
    keys = definition_keys([(key, m["ident"], m["name"]) for key, m in zip(inherited, comps, strict=True)], previous=previous,
                           root=next((j for j, m in enumerate(comps) if m is root), None))
    for m, key in zip(comps, keys, strict=True):
        m["key"] = key
    entries: list[dict[str, Any]] = []
    for m in comps:
        kind = ("assembly" if m["t"] == "assembly" or id(m) in parents or m is root else
                "standard_part" if m["t"] == "standard_part" or is_standard_number(m["number"]) else "purchased" if m["t"] == "purchased"
                else "part")
        uses: list[dict[str, Any]] = []
        for u in m["pa"]:
            p = by_k.get(u["p"])
            if p is None or p is m:
                continue
            uses.append({"parent": p["key"], "n": max(1, int(u.get("n") or 1)), "q": u.get("q"), "ev": ev(u.get("q"))})
        first_parent = uses[0]["parent"] if uses else None
        entries.append({"key": m["key"], "id": m["ident"] or identity_key({"name": m["name"]}, parent=first_parent),
                        "name": m["name"], "number": m["number"], "size": m["size"], "aliases": m["aliases"][:30], "kind": kind,
                        "uses": uses, "ev": ev(m["q"])})
    interfaces: list[dict[str, Any]] = []
    keys: set[str] = set()
    kinds = {e["key"]: e["kind"] for e in entries}
    for i in raw_ifs:  # an unusable interface is dropped alone (recorded), never the reading
        owner = by_k.get(str(i.get("o") or ""))
        if owner is None or kinds.get(owner["key"]) != "assembly":
            problems.append(f"interface {i.get('k')}: owner {i.get('o')} is not an assembly of the skeleton")
            continue
        bad = interface_problems(i.get("t"), i.get("at"), i.get("dir"), i.get("pts"))
        if bad:
            problems.append(f"interface {i.get('k')} ({i.get('t')}) dropped: needs " + "; ".join(bad))
            continue
        key, j = _slug(i.get("k")), 1
        while key in keys:
            j += 1
            key = f"{_slug(i.get('k'))[:46]}_{j}"
        keys.add(key)
        interfaces.append({"key": key, "owner": owner["key"], "name": str(i.get("n") or key)[:200], "kind": i.get("t") or "point",
                           "at": i.get("at"), "dir": i.get("dir"), "points": list(i.get("pts") or [])[:200],
                           "status": "derived" if i.get("st") == "derived" else "asserted", "how": i.get("how"), "ev": ev(i.get("q"))})
    return Skeleton(entries, interfaces, root["key"] if root is not None else None, problems)


@dataclass
class _Reading:
    order: tuple[int, int, int]
    source: str
    seq: int
    value: Any
    unit: str | None
    status: str
    how: str | None
    ev: dict[str, Any] | None
    unitless: bool = False
    more: tuple[dict[str, Any] | None, ...] = ()


class _Keys:
    """Keys ``<prefix>-NN`` in order of first appearance; a signature the previous version had keeps its key."""

    def __init__(self, prefix: str, width: int, previous: dict[Any, str], wanted: Iterable[Any]) -> None:
        self.prefix, self.width = prefix, width
        self.previous = previous
        wanted = set(wanted)
        self.reserved = {k for s, k in previous.items() if s in wanted}
        self.used: set[str] = set()
        self.n = 0

    def get(self, sig: Any) -> str:
        prev = self.previous.get(sig)
        if prev is not None and prev in self.reserved and prev not in self.used:
            self.used.add(prev)
            return prev
        while True:
            self.n += 1
            key = f"{self.prefix}-{self.n:0{self.width}d}"
            if key not in self.reserved and key not in self.used:
                self.used.add(key)
                return key


def _chunk_facts(data: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """(component k, fact) of one reply: every component's ``fs``, then the top-level facts (k None)."""
    out: list[tuple[str | None, dict[str, Any]]] = []
    for c in data.get("comps") or []:
        out.extend((str(c.get("k")), f) for f in c.get("fs") or [])
    out.extend((None, f) for f in data.get("facts") or [])
    return out


def _ev(source: str, seq: int, item: dict[str, Any]) -> dict[str, Any] | None:
    """Evidence of a compact item: the server adds source and chunk; the item cites a quote, an entity or a region."""
    if not (item.get("q") or item.get("e") or item.get("r")):
        return None
    return {"source": source, "chunk": seq, "quote": item.get("q"), "entity": item.get("e"), "region": item.get("r"),
            "detail": str(item.get("how") or "")[:200]}


def _dedupe_ev(evs: Iterable[dict[str, Any] | None], limit: int = 20) -> list[dict[str, Any]]:
    out, seen = [], set()
    for e in evs:
        if not e:
            continue
        sig = (e.get("source"), e.get("chunk"), e.get("quote"), e.get("entity"), json.dumps(e.get("region")))
        if sig not in seen:
            seen.add(sig)
            out.append(e)
    return out[:limit]


def reading_locator(r: dict[str, Any] | None) -> str:
    """Where one reading / evidence stands, for a decision's ``sources``: ``"<source> · chunk <n> “quote”"`` (or the
    drawing entity), with the value of a conflict reading. The server writes the finer locator when it finishes the spec."""
    r = r or {}
    out = f"{r.get('source') or '?'}" + (f" · chunk {r['chunk']}" if r.get("chunk") is not None else "")
    if r.get("quote"):
        out += f" “{str(r['quote'])[:120]}”"
    elif r.get("entity"):
        out += f" · entity {r['entity']}"
    elif r.get("region"):
        out += " · image region " + ",".join(str(x) for x in r["region"]) + " px (visual, unverified)"
    if "value" in r and r.get("value") is not None:
        out += f" = {r['value']}{(' ' + str(r['unit'])) if r.get('unit') else ''}"
    return out[:300]


def _basis(a: dict[str, Any]) -> str:
    """The basis of an adopted value; a value reused from the previous version says so (D-03: re-runs reuse decisions)."""
    b = str(a.get("b") or "")
    return f"{b} (reused from {a['reused']} of the previous version)" if a.get("reused") else b


def reusable_adoptions(previous: dict[str, Any] | None, open_items: list[dict[str, Any]], *,
                       skip: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """The previous version's AI decisions that still answer an open item of this merge (generation supplement D-03: a
    re-extraction reuses what was decided instead of letting the model decide the same gap again): same subject, an
    ``ai_turbo`` decision not in ``skip`` (e.g. one a person corrected), and for a conflict a value that is still one of
    the readings. Returned in ``adopt`` form (``{"v", "u", "b", "reused": <assumption id>}``) keyed by open item id."""
    prev = {str(a.get("subject")): a for a in (previous or {}).get("assumptions") or []
            if isinstance(a, dict) and (a.get("author") or "ai_turbo") == "ai_turbo" and str(a.get("id")) not in (skip or set())
            and not a.get("replaced_by") and a.get("adopted") is not None}
    out: dict[str, dict[str, Any]] = {}
    for it in open_items:
        a = prev.get(str(it.get("id")))
        if a is None:
            continue
        if it.get("kind") == "conflict":
            keys = {_vkey(value_for(it["id"], x.get("value")), norm_unit(x.get("unit"))) for x in it.get("readings") or []}
            if _vkey(value_for(it["id"], a["adopted"]), norm_unit(a.get("unit"))) not in keys:
                continue
        basis = re.sub(r" \(reused from [^()]* of the previous version\)$", "", str(a.get("basis") or ""))
        out[str(it["id"])] = {"id": it["id"], "v": a["adopted"], "u": a.get("unit"), "b": basis, "reused": a.get("id")}
    return out


# --- the merge --------------------------------------------------------------------------------------------------------


def merge(chunks: Iterable[ChunkFindings], *, policy: str, previous: dict[str, Any] | None = None,
          adopt: dict[str, dict[str, Any]] | None = None, skeleton: Skeleton | None = None,
          title_bindings: Iterable[dict[str, Any]] = (), membership_bindings: Iterable[dict[str, Any]] = ()) -> Merged:
    """Merge the skeleton and the findings of every chunk into one spec (reply form).

    ``skeleton``: the structure reading (:func:`skeleton_from`; None or empty: the chunks alone, e.g. attachments only).
    ``adopt`` (turbo): adopted values per open item id (``{"v", "u", "b"}``); None = not asked yet (``adoptable`` lists
    what to ask)."""
    return _Merge(sorted(chunks, key=lambda c: (c.seq, c.source)), policy, previous or {}, adopt,
                  skeleton or Skeleton([], [], None), list(title_bindings), list(membership_bindings)).run()


class _Merge:
    def __init__(self, chunks: list[ChunkFindings], policy: str, previous: dict[str, Any], adopt: dict[str, dict[str, Any]] | None,
                 skeleton: Skeleton, title_bindings: list[dict[str, Any]], membership_bindings: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.turbo = policy == "turbo"
        self.previous = previous
        self.adopt = adopt
        self.skeleton = skeleton
        self.title_updates = validated_title_updates(skeleton.entries, chunks, title_bindings)
        self.membership_bindings = membership_bindings
        self.mm_sources = {c.source for c in chunks if c.declares_mm or c.data.get("units") == "mm"}

    # -- components ----------------------------------------------------------------------------------------------------
    def _mentions(self) -> tuple[list[_Mention], dict[tuple[int, str], _Mention], dict[int, set[str]]]:
        mentions: list[_Mention] = []
        local: dict[tuple[int, str], _Mention] = {}
        rel_keys: dict[int, set[str]] = {}
        for ch in self.chunks:
            d = ch.data
            rk = {str(r["k"]) for r in d.get("rels") or [] if r.get("k")}
            rel_keys[ch.seq] = rk
            names = {str(c.get("k")): str(c.get("n") or c.get("k")) for c in d.get("comps") or [] if c.get("k")}
            parent_of = {str(u.get("c")): names.get(str(u.get("p")), str(u.get("p"))) for u in d.get("uses") or [] if u.get("c") and u.get("p")}
            idx = 0
            for c in d.get("comps") or []:
                k = str(c.get("k") or "")
                if not k or (ch.seq, k) in local:
                    continue
                tpl = {"id": str(c["tpl"]), "params": dict(c.get("tpp") or {})} if c.get("tpl") else None
                m = _Mention(ch.seq, idx, ch.source, k, str(c.get("n") or k), c.get("no") or None, c.get("t"), tpl, c.get("sz") or None,
                             tuple(str(x) for x in c.get("of") or []), c.get("b") or None, parent_of.get(k), bool(c.get("r")))
                idx += 1
                mentions.append(m)
                local[(ch.seq, k)] = m
            refs: list[Any] = [d.get("root")]
            refs += [f.get("c") for f in d.get("facts") or [] if f.get("c") and f.get("c") not in rk]
            for u in d.get("uses") or []:
                refs += [u.get("p"), u.get("c")]
            for p in d.get("place") or []:
                refs += [p.get("c"), p.get("p")]
            for r in d.get("rels") or []:
                refs += [r.get("a"), r.get("b")]
            refs += [x.get("c") for x in d.get("missing") or []] + [n.get("c") for n in d.get("notes") or []]
            for ref in refs:
                if ref and not str(ref).startswith("if.") and (ch.seq, str(ref)) not in local:
                    # A key the reply uses without listing it: a mention of a component by that name (or skeleton key).
                    m = _Mention(ch.seq, idx, ch.source, str(ref), str(ref), None, None, None, parent_name=parent_of.get(str(ref)))
                    idx += 1
                    mentions.append(m)
                    local[(ch.seq, str(ref))] = m
        return mentions, local, rel_keys

    def _bind(self, mentions: list[_Mention]) -> tuple[list[_Def], dict[int, list[_Def]], dict[str, Any]]:
        """Every mention → the definitions it is (see the module docstring "Identity = key"): (definitions, mention id →
        its definitions — one, the several of a generic mention, or none —, the record)."""
        defs: list[_Def] = []
        by_key: dict[str, _Def] = {}
        by_ident: dict[str, _Def] = {}
        by_name: dict[str, list[_Def]] = {}
        ident_keys: dict[str, set[str]] = {}
        for entry in self.skeleton.entries:
            ident = identity_key({**entry, **self.title_updates.get(entry["key"], {})})
            if _numbered(ident):
                ident_keys.setdefault(ident, set()).add(entry["key"])
        ambiguous_idents = {ident for ident, keys in ident_keys.items() if len(keys) > 1}

        def named(d: _Def) -> None:
            for n in (d.name, *((d.skel or {}).get("aliases") or [])):
                lst = by_name.setdefault(norm_name(n), [])
                if d not in lst:
                    lst.append(d)

        for i, e in enumerate(self.skeleton.entries):
            update = self.title_updates.get(e["key"])
            canonical = identity_key({**e, **(update or {})})
            d = _Def([], skel=e, ident=canonical if update else e.get("id"), key=e["key"], index=i, title_update=update)
            defs.append(d)
            by_key[e["key"]] = d
            if _numbered(canonical) and canonical not in ambiguous_idents:
                by_ident[canonical] = d
            named(d)
        skeleton = bool(self.skeleton.entries)
        bound: dict[int, list[_Def]] = {}
        record: dict[str, Any] = {"generic": [], "unbound": [], "new_components": [], "standard_identity_splits": []}

        def bind(m: _Mention, hit: list[_Def]) -> None:
            bound[id(m)] = hit
            if len(hit) == 1:
                hit[0].mentions.append(m)

        def unbind(m: _Mention, reason: str, **more: Any) -> None:
            bound[id(m)] = []
            record["unbound"].append({**self._mention_record(m), "reason": reason, **more})

        def declared(ms: list[_Mention]) -> bool:
            """A new component needs a basis plus number or parent; visual bases are validated before merge."""
            return not skeleton or any(m.basis and (m.ident or m.parent_name) or any(
                b["source"] == m.source and b["chunk"] == m.seq and b["local_child"] == m.k
                and b["child_identity"] == m.ident for b in self.membership_bindings) for m in ms)

        # 1. an explicit skeleton key is that component; a number / size written with it is one more reading of the
        #    component's own (compared in _build), and names it for the other chunks when no component carries it
        numbered: dict[str, list[_Mention]] = {}
        rest: list[_Mention] = []
        for m in sorted(mentions, key=lambda m: m.order):
            if m.k in by_key:
                target = by_key[m.k]
                canonical = identity_key({"number": target.number, "size": target.size, "name": target.name})
                if (m.name != m.k and not is_standard_number(target.number)
                        and tags_conflict(series_tags(target.name), series_tags(m.name))):
                    unbind(m, "known_key_variant_mismatch", matches=[target.key])
                    continue
                different_standard = bool(m.ident and canonical and m.ident.startswith("std:") and canonical.startswith("std:")
                                          and m.ident != canonical)
                if different_standard and not compatible_standard_readings(canonical, m.ident):
                    # A reused prompt key cannot make two standard head types or
                    # sizes one physical part, nor poison the number index for a
                    # separately declared local component in another chunk.
                    numbered.setdefault(m.ident, []).append(m)
                    record["standard_identity_splits"].append({"source": m.source, "chunk": m.seq,
                        "local_key": m.k, "old_definition": target.key, "old_identity": canonical,
                        "new_identity": m.ident})
                    continue
                bind(m, [target])
                # A same-key partial standard reading remains a conflict on this
                # component; it never becomes a number alias for other keys.
                if (m.ident and m.ident not in ambiguous_idents and not different_standard
                        and (not target.title_update or norm_name(m.number) == norm_name(target.number))):
                    by_ident.setdefault(m.ident, target)
            elif m.ident:
                numbered.setdefault(m.ident, []).append(m)
            else:
                rest.append(m)
        # 2. a number (a drawing number, or a standard number and its size) names one component
        for ik, ms in numbered.items():
            if ik in ambiguous_idents:
                for m in ms:
                    unbind(m, "several_components", matches=sorted(ident_keys[ik]))
                continue
            d = by_ident.get(ik)
            if d is None and declared(ms):
                d = by_ident[ik] = _Def([], ident=ik)
                defs.append(d)
            for m in ms:
                if d is not None:
                    bind(m, [d])
                else:
                    unbind(m, "not_in_skeleton")
        for d in defs:
            if d.skel is None:
                named(d)
        # 3. a generic mention (of), else the one component carrying the name it writes
        residual: list[_Mention] = []
        for m in rest:
            if m.of and [x for x in m.of if x in by_key]:
                hit = list(dict.fromkeys(by_key[x] for x in m.of if x in by_key))
                if len(hit) > 1:
                    record["generic"].append({"name": m.name, "chunk": m.seq, "source": m.source, "of": [d.key for d in hit]})
            else:
                hit = by_name.get(m.nname) or []
                if len(hit) > 1:  # a name several components carry, written without saying which: nothing to apply it to
                    unbind(m, "several_components", matches=[d.key or d.name for d in hit])
                    continue
            if hit:
                bind(m, hit)
            else:
                residual.append(m)
        # 4. anything else new: its name plus the parent the chunk puts it in (one without a stated parent is the only
        #    component of that name that has one), when declared
        groups: dict[tuple[str, str], list[_Mention]] = {}
        for m in residual:
            groups.setdefault((m.nname, norm_name(m.parent_name) if m.parent_name else ""), []).append(m)
        for (nn, pn) in [g for g in groups if not g[1]]:
            homes = [g for g in groups if g[0] == nn and g[1]]
            if len(homes) == 1:
                groups[homes[0]] += groups.pop((nn, pn))
        for (nn, pn), ms in groups.items():
            if not declared(ms):
                for m in ms:
                    unbind(m, "not_in_skeleton")
                continue
            d = _Def([], ident=f"name:{nn}@{pn}")
            defs.append(d)
            for m in ms:
                bind(m, [d])
        if skeleton:
            for d in defs:
                if d.skel is None:
                    m = sorted(d.mentions, key=lambda m: m.order)[0]
                    basis = next((x.basis for x in d.mentions if x.basis), None)
                    by = "image_region" if any(x.visual for x in d.mentions) else "number" if _numbered(d.ident) else "bom_line"
                    d.notes.append(f"Not in the skeleton: declared in chunk {m.seq} ({m.source}) — {basis}")
                    record["new_components"].append({"name": d.name, "chunk": m.seq, "source": m.source, "by": by, "basis": basis})
        defs.sort(key=lambda d: d.order)  # the skeleton's first, then in order of first appearance (new keys follow it)
        return defs, bound, record

    def _mention_record(self, m: _Mention) -> dict[str, Any]:
        """What a mention bound to no component stated (``merge.unbound``): its name, number, basis, the quote of the
        containment statement naming it and its facts with their quotes — nothing of it is applied."""
        data = next((c.data for c in self.chunks if c.seq == m.seq), {})
        comp = next((c for c in data.get("comps") or [] if str(c.get("k")) == m.k), {})
        facts = [*(comp.get("fs") or []), *(f for f in data.get("facts") or [] if str(f.get("c")) == m.k)]
        quote = next((u.get("q") for u in data.get("uses") or [] if str(u.get("c")) == m.k and u.get("q")), None)
        out = {"name": m.name, "number": m.number, "size": m.size, "chunk": m.seq, "source": m.source, "basis": m.basis, "quote": quote,
               "facts": [{k: f[k] for k in ("f", "v", "u", "q") if f.get(k) is not None} for f in facts][:20]}
        return {k: v for k, v in out.items() if v not in (None, [])}

    # -- run -----------------------------------------------------------------------------------------------------------
    def run(self) -> Merged:
        mentions, local, rel_keys = self._mentions()
        defs, bound, bind_record = self._bind(mentions)
        by_key = {d.key: d for d in defs if d.key}

        def drefs(seq: int, k: Any) -> list[_Def]:
            m = local.get((seq, str(k))) if k else None
            return list(bound.get(id(m), [])) if m is not None else []

        def dref(seq: int, k: Any) -> _Def | None:
            ds = drefs(seq, k)
            return ds[0] if len(ds) == 1 else None

        self.dref, self.drefs = dref, drefs
        # containment edges (for kinds and the root), in order of first appearance: the skeleton's tree, then what the
        # chunks state about the components it does not place (a count of a skeleton pair is one more reading of it)
        uses: list[tuple[tuple[int, int], _Def, _Def, int, dict[str, Any] | None, str, int, dict[str, Any]]] = []
        tree: set[tuple[str, str]] = set()
        for d in defs:
            for i, u in enumerate((d.skel or {}).get("uses") or []):
                p = by_key.get(u["parent"])
                if p is None or p is d:
                    continue
                e = u.get("ev")
                uses.append(((-1, d.index * 1000 + i), p, d, max(1, int(u.get("n") or 1)), e, (e or {}).get("source") or "", (e or {}).get("chunk") or 0, {}))
                tree.add((p.key, d.key))
        placed = {c for _, c in tree}
        restated: list[dict[str, Any]] = []
        for ch in self.chunks:
            for i, u in enumerate(ch.data.get("uses") or []):
                p, c = dref(ch.seq, u.get("p")), dref(ch.seq, u.get("c"))
                if p is None or c is None or p is c:
                    continue
                own_bom = next((b for b in self.membership_bindings if b["source"] == ch.source and b["chunk"] == ch.seq
                                and b["parent"] == p.key and b["local_child"] == str(u.get("c"))
                                and b["child_identity"] == identity_key({"number": c.number, "size": c.size, "name": c.name})
                                and b["quantity"] == int(u.get("n") or 1)), None)
                if c.key in placed and (p.key, c.key) not in tree and own_bom is None:
                    restated.append({"parent": p.key, "child": c.key, "n": max(1, int(u.get("n") or 1)), "chunk": ch.seq, "source": ch.source,
                                     "quote": u.get("q"), "skeleton_parents": sorted(pp for pp, cc in tree if cc == c.key)})
                    continue
                uses.append(((ch.seq, i), p, c, max(1, int(u.get("n") or 1)), _ev(ch.source, ch.seq, u), ch.source, ch.seq, u))
        parents_used = {id(p) for _, p, _, _, _, _, _, _ in uses}
        children_used = {id(c) for _, _, c, _, _, _, _, _ in uses}
        root: _Def | None = by_key.get(self.skeleton.root or "")
        for ch in self.chunks:
            if root is not None:
                break
            root = dref(ch.seq, ch.data.get("root"))
        kinds: dict[int, tuple[str, str, str]] = {}
        for d in defs:
            stated = [m.t for m in d.mentions if m.t] + ([str(d.skel.get("kind"))] if d.skel else [])
            if "assembly" in stated or id(d) in parents_used or d is root:
                kinds[id(d)] = ("assembly", "unknown", "exact")
            elif "standard_part" in stated or (d.skel is None and is_standard_number(d.number)):
                kinds[id(d)] = ("standard_part", "standard", "nominal")
            elif "purchased" in stated:
                kinds[id(d)] = ("part", "buy", "exact")
            else:
                kinds[id(d)] = ("part", "unknown", "exact")
        if root is None:
            tops = [d for d in defs if kinds[id(d)][0] == "assembly" and id(d) not in children_used]
            root = tops[0] if len(tops) == 1 else None
        # the definitions the skeleton did not key (the same policy as the skeleton's own)
        keys = definition_keys([(d.key, d.ident, d.name) for d in defs], previous=self.previous,
                               root=next((j for j, d in enumerate(defs) if d is root), None))
        for d, key in zip(defs, keys, strict=True):
            d.key = key
        for split in bind_record["standard_identity_splits"]:
            target = dref(split["chunk"], split["local_key"])
            split["target"] = target.key if target is not None else None
            chunk = next(ch for ch in self.chunks if ch.seq == split["chunk"])
            split["parents"] = sorted({p.key for u in chunk.data.get("uses") or []
                if str(u.get("c")) == split["local_key"] and (p := dref(chunk.seq, u.get("p"))) is not None})
        source_memberships = [{**b, "child": d.key} for b in self.membership_bindings
                              if (d := dref(b["chunk"], b["local_child"])) is not None]
        record = {**bind_record, "restated": restated[:100], "source_memberships": source_memberships}
        return self._build(defs, root, kinds, uses, rel_keys, record)

    # -- everything keyed by definitions ---------------------------------------------------------------------------------
    def _build(self, defs: list[_Def], root: _Def | None, kinds: dict[int, tuple[str, str, str]], uses: list[Any],
               rel_keys: dict[int, set[str]], bind_record: dict[str, Any]) -> Merged:
        dref, drefs = self.dref, self.drefs
        prev = self.previous
        adopt = self.adopt or {}
        defs_by_key = {d.key: d for d in defs}
        name_of = {d.key: d.name + (f" ({d.number})" if d.number else "") for d in defs}

        # instances (containment), skipping a statement that would close a cycle
        contain: dict[tuple[str, str], list[tuple[int, dict[str, Any] | None, str, int, tuple[int, int]]]] = {}
        edges: dict[str, set[str]] = {}
        structure_q: list[tuple[str, str, tuple[int, int]]] = []

        def reaches(a: str, b: str) -> bool:
            stack, seen = [a], set()
            while stack:
                x = stack.pop()
                if x == b:
                    return True
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(edges.get(x, ()))
            return False

        # Every containment statement is compared with the others: equal counts merge their evidence, different counts
        # conflict (a count restated is never added up). Instances described one by one with their own positions are
        # kept through the positions (below), whatever count the statements give.
        for order, p, c, n, ev, src, seq, reading in uses:
            pair = (p.key, c.key)
            if pair not in contain:
                if reaches(c.key, p.key):
                    structure_q.append((p.key, c.key, order))
                    continue
                edges.setdefault(p.key, set()).add(c.key)
                contain[pair] = []
            contain[pair].append({"n": n, "evs": [ev], "src": src, "seq": seq, "order": order,
                                  "status": reading.get("st"), "how": reading.get("how")})
        placements: dict[str, list[tuple[str | None, list[float], list[float]]]] = {}
        for ch in self.chunks:
            for pl in ch.data.get("place") or []:
                c = dref(ch.seq, pl.get("c"))
                if c is None:
                    continue
                p = dref(ch.seq, pl.get("p")) if pl.get("p") else None
                item = (p.key if p else None, [float(x) for x in pl["at"]], [float(x) for x in (pl.get("rot") or [0, 0, 0])])
                lst = placements.setdefault(c.key, [])
                if item not in lst:
                    lst.append(item)
        # Every distinct stated position is an instance: the count is never below the positions stated for it.
        parents_of: dict[str, set[str]] = {}
        for p_key, c_key in contain:
            parents_of.setdefault(c_key, set()).add(p_key)
        placed = {(p_key, c_key): sum(1 for pp, _, _ in placements.get(c_key, [])
                                      if pp == p_key or (pp is None and len(parents_of.get(c_key, ())) == 1))
                  for p_key, c_key in contain}

        # readings of every fact, grouped by subject in order of first appearance
        readings: dict[str, list[_Reading]] = {}
        rel_items: list[tuple[tuple[int, int], int, str, dict[str, Any]]] = []
        missing: list[tuple[tuple[int, int, int], str, dict[str, Any], dict[str, Any] | None]] = []
        for ch in self.chunks:
            for i, r in enumerate(ch.data.get("rels") or []):
                rel_items.append(((ch.seq, i), ch.seq, ch.source, r))
        rel_first: dict[tuple[int, str], str] = {}  # (seq, local relation key) -> first relation key, filled below
        rel_facts: list[tuple[int, int, ChunkFindings, dict[str, Any]]] = []
        unplaced = 0
        for ch in self.chunks:
            for i, (k, f) in enumerate(_chunk_facts(ch.data)):
                if k is None and str(f.get("c")) in rel_keys.get(ch.seq, set()):
                    rel_facts.append((ch.seq, i, ch, f))
                    continue
                # a component's fs (each component a generic mention covers); a top-level fact naming a component (older
                # replies); a product-level fact: the root
                ds = drefs(ch.seq, k if k is not None else f.get("c")) if (k is not None or f.get("c")) else [root] if root else []
                if not ds:
                    unplaced += 1
                for d in ds:
                    self._reading(readings, f"{d.key}.{norm_field(f.get('f'))}", (ch.seq, 0, i), ch, f)
            for i, m in enumerate(ch.data.get("missing") or []):
                for d in drefs(ch.seq, m.get("c")):
                    missing.append(((ch.seq, 2, i), f"{d.key}.{norm_field(m.get('f'))}", m, _ev(ch.source, ch.seq, m)))
        for d in defs:
            self._identity_readings(readings, d)
        for (p_key, c_key), stmts in contain.items():
            subject = f"{p_key}.qty[{c_key}]"
            for st in stmts:
                # a skeleton count whose quote was not found in the sources is the skeleton's reading, not a located one
                visual = bool((st["evs"][0] or {}).get("region"))
                how = st.get("how") or ((st["evs"][0] or {}).get("detail") if visual else None if st["src"] else "skeleton reading (its quote was not found in the sources)")
                readings.setdefault(subject, []).append(_Reading((st["order"][0], 1, st["order"][1]), st["src"], st["seq"], st["n"], "pcs",
                                                                 "derived" if visual or st.get("status") == "derived" or not st["src"] else "asserted", how, st["evs"][0],
                                                                 more=tuple(st["evs"][1:])))

        # instances
        prev_inst: dict[tuple[str, str, int], str] = {}
        counter: dict[tuple[str, str], int] = {}
        for i in prev.get("instances") or []:
            pair = (i.get("parent"), i.get("definition"))
            j = counter.get(pair, 0)  # type: ignore[arg-type]
            counter[pair] = j + 1  # type: ignore[index]
            prev_inst[(str(i.get("parent")), str(i.get("definition")), j)] = i["key"]
        counts: dict[tuple[str, str], int] = {}
        for (p_key, c_key), stmts in contain.items():
            n = stmts[0]["n"]
            # an adopted count applies only where the statements disagree: a count stated once is never overridden (D-03)
            a = adopt.get(f"{p_key}.qty[{c_key}]") if self.turbo and len({st["n"] for st in stmts}) > 1 else None
            if a is not None and _is_number(coerce_value(a.get("v"))) and int(coerce_value(a["v"])) >= 1:
                n = int(coerce_value(a["v"]))
            counts[(p_key, c_key)] = max(n, placed[(p_key, c_key)])
        inst_sigs = [(p, c, j) for (p, c), n in counts.items() for j in range(n)]
        ikeys = _Keys("I", 2, prev_inst, inst_sigs)
        instances: list[dict[str, Any]] = []
        inst_of: dict[str, list[str]] = {}
        used_place: dict[str, set[int]] = {}
        for (p_key, c_key), n in counts.items():
            for j in range(n):
                key = ikeys.get((p_key, c_key, j))
                pl = None
                options = placements.get(c_key, [])
                taken = used_place.setdefault(c_key, set())
                pick = next((x for x, (pp, _, _) in enumerate(options) if x not in taken and pp == p_key), None)
                if pick is None:
                    pick = next((x for x, (pp, _, _) in enumerate(options) if x not in taken and pp is None), None)
                if pick is not None:
                    taken.add(pick)
                    pl = {"translate": options[pick][1], "rotate": options[pick][2]}
                instances.append({"key": key, "definition": c_key, "parent": p_key, "name": f"{defs_by_key[c_key].name} #{j + 1}",
                                  "placement": pl, "fact_ids": []})
                inst_of.setdefault(c_key, []).append(key)

        def affects(*keys: str) -> list[str]:
            out: list[str] = []
            for k in keys:
                if k:
                    out.append(k)
                    out.extend(inst_of.get(k, []))
            return list(dict.fromkeys(out))

        # relations: endpoints → instances (paired by index when both sides repeat equally); an end "if.<key>" is that
        # interface of its owner assembly ({"instance": "@<owner>", "datum": "if.<key>"}, design §6.2)
        prev_rel = {(r.get("kind"), (r.get("a") or {}).get("instance"), (r.get("b") or {}).get("instance")): r["key"]
                    for r in prev.get("relations") or [] if r.get("key")}
        ifs = {i["key"]: i for i in self.skeleton.interfaces}
        rel_specs: list[tuple[str, str, str, dict[str, Any], dict[str, Any], dict[str, Any], int, str | None]] = []
        placement_q: list[tuple[str, list[str], str, tuple[int, int]]] = []
        mapping_q: list[dict[str, Any]] = []
        seen_rel: set[str] = set()

        def end(seq: int, ref: Any, datum: Any) -> tuple[_Def | None, list[str], str]:
            """(definition, its instances or the interface's owner frame, the datum) of one relation end."""
            if isinstance(ref, str) and ref.startswith("if."):
                i = ifs.get(ref[3:])
                return None, ([f"@{i['owner']}"] if i is not None else []), ref
            d = dref(seq, ref)
            return d, (inst_of.get(d.key, []) if d is not None else []), datum if isinstance(datum, str) and datum.startswith("port.") else "origin"

        for order, seq, _src, r in rel_items:
            (a, ia, da), (b, ib, db) = end(seq, r.get("a"), r.get("da")), end(seq, r.get("b"), r.get("db"))
            if (a is None and not ia) or (b is None and not ib) or (a is None and b is None):
                continue
            kind = r.get("t")
            params: dict[str, Any] = {}
            if kind == "offset" and _is_number(r.get("d")):
                params = {"distance": r["d"]}
            elif kind == "angle" and _is_number(r.get("ang")):
                params = {"angle": r["ang"]}
            sig = json.dumps([kind, a.key if a else da, b.key if b else db, da, db, params], sort_keys=True)
            if not ia or not ib:
                lone = a if not ia else b
                placement_q.append((f"{lone.key}.placement", affects(*(d.key for d in (a, b) if d is not None)), str(kind), order))  # type: ignore[union-attr]
                continue
            if sig in seen_rel:
                continue
            seen_rel.add(sig)
            if len(ia) > 1 and len(ib) > 1 and len(ia) != len(ib):
                # A group relation carries no instance mapping. Joining only the first member of a to every b
                # invented mates and could move correctly stated poses; retain the ambiguity instead.
                mapping_q.append({"subject": f"{a.key}.relation_mapping[{b.key}]",
                                  "question": f"配合 {kind} 的两组零件分别有 {len(ia)} 和 {len(ib)} 个实例，具体哪些实例对应？",
                                  "affects": affects(a.key, b.key), "candidates": [], "origin": "server", "_order": order})
                continue
            pairs = list(zip(ia, ib)) if len(ia) == len(ib) else [(x, ib[0]) for x in ia] if len(ib) == 1 else [(ia[0], y) for y in ib]
            for x, y in pairs:
                if x != y:
                    rel_specs.append((str(kind), x, y, {"instance": x, "datum": da}, {"instance": y, "datum": db}, params, seq, r.get("k")))
        mkeys = _Keys("M", 2, prev_rel, [(k, x, y) for k, x, y, *_ in rel_specs])
        relations: list[dict[str, Any]] = []
        for kind, x, y, ea, eb, params, seq, lk in rel_specs:
            key = mkeys.get((kind, x, y))
            relations.append({"key": key, "kind": kind, "a": ea, "b": eb, "params": dict(params), "fact_ids": []})
            if lk and (seq, str(lk)) not in rel_first:
                rel_first[(seq, str(lk))] = key
        for seq, i, ch, f in rel_facts:
            key = rel_first.get((seq, str(f.get("c"))))
            if key:
                self._reading(readings, f"{key}.{norm_field(f.get('f'))}", (seq, 0, i), ch, f)

        # facts, conflicts and the questions rules raise
        facts: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = mapping_q
        assumptions: list[dict[str, Any]] = []
        adoptable: list[dict[str, Any]] = []
        known_subjects: set[str] = set()
        fact_of: dict[str, dict[str, Any]] = {}
        unit_q: list[tuple[tuple[int, int, int], str, Any]] = []
        for subject, rs in readings.items():
            known = [r for r in rs if r.value is not None and r.status in ("asserted", "derived")]
            known = [r for r in known if r.source] or known  # an unlocated reading counts only when nothing located states it
            distinct: list[_Reading] = []
            seen_v: set[tuple[Any, ...]] = set()
            for r in known:
                vk = _vkey(r.value, r.unit)
                if vk not in seen_v:
                    seen_v.add(vk)
                    distinct.append(r)
            evidence = _dedupe_ev(e for r in rs for e in (r.ev, *r.more))
            kind = "quantity" if ".qty[" in subject else ("material" if subject.rsplit(".", 1)[-1] in ("material", "grade") else "dimension")
            if len(distinct) > 1:
                vals, seen_vs = [], set()
                for r in known:
                    sig = (_vkey(r.value, r.unit), r.source)
                    if sig in seen_vs:
                        continue
                    seen_vs.add(sig)
                    vals.append({"value": r.value, "unit": r.unit, "source": r.source, "chunk": r.seq,
                                 "quote": (r.ev or {}).get("quote"), "entity": (r.ev or {}).get("entity"),
                                 "region": (r.ev or {}).get("region"), "detail": (r.ev or {}).get("detail") or "",
                                 "status": r.status, "derivation": r.how})
                fact = {"subject": subject, "kind": kind, "value": None, "unit": distinct[0].unit, "status": "conflicting",
                        "evidence": evidence, "derivation": None, "_order": rs[0].order}
                conflicts.append({"subject": subject, "values": vals[:20], "resolution": None, "_order": rs[0].order})
                known_subjects.add(subject)
            elif known:
                r0 = known[0]
                fact = {"subject": subject, "kind": kind, "value": r0.value, "unit": r0.unit,
                        "status": "asserted" if any(r.status == "asserted" for r in known) else "derived", "evidence": evidence,
                        "derivation": r0.how, "_order": rs[0].order}
                known_subjects.add(subject)
            else:
                r0 = rs[0]
                fact = {"subject": subject, "kind": kind, "value": r0.value, "unit": r0.unit, "status": "unknown", "evidence": evidence,
                        "derivation": r0.how, "_order": rs[0].order}
                lone = next((r for r in rs if r.unitless), None)
                if lone is not None:
                    unit_q.append((rs[0].order, subject, lone.value))
            qty = re.fullmatch(r"(?P<p>[^.]+)\.qty\[(?P<c>[^\]]+)\]", subject)
            if qty and fact["status"] in ("asserted", "derived") and _is_number(fact["value"]):
                n_inst = counts.get((qty["p"], qty["c"]), 0)
                if n_inst > fact["value"]:
                    fact.update(value=n_inst, status="derived",
                                derivation=f"one instance per distinct stated position ({n_inst}); the statements give {fact['value']}")
            facts.append(fact)
            fact_of[subject] = fact

        # missing values the chunks reported (dropped when another chunk states the value)
        miss: dict[str, dict[str, Any]] = {}
        missing_evidence: list[dict[str, Any]] = []
        for order, subject, m, ev in missing:
            if subject in known_subjects:
                continue
            if ev:
                missing_evidence.append({"subject": subject, "evidence": ev})
            cands = [value_for(subject, x) for x in m.get("cand") or []]
            if subject in miss:
                miss[subject]["candidates"] = list(dict.fromkeys(miss[subject]["candidates"] + cands))[:20]
                continue
            own = subject.split(".", 1)[0]
            fld = subject.split(".", 1)[1]
            miss[subject] = {"subject": subject, "question": f"{name_of.get(own, own)} 的 {fld} 未给出：{m.get('q') or fld}",
                             "affects": affects(own), "candidates": cands[:20], "origin": "model", "_order": order, "_ev": ev,
                             "_kind": "missing"}
        for order, subject, value in unit_q:
            own = subject.split(".", 1)[0]
            fld = subject.split(".", 1)[1]
            miss[f"{subject}.unit"] = {"subject": f"{subject}.unit", "question": f"{name_of.get(own, own)} 的 {fld} = {value} 没有单位，单位是什么？",
                                       "affects": affects(own), "candidates": ["mm"], "origin": "server", "_order": order, "_kind": "unit",
                                       "_value": value}

        for p_key, c_key, order in structure_q:
            questions.append({"subject": f"{p_key}.structure", "question": f"{name_of[p_key]} 与 {name_of[c_key]} 的包含关系成环，哪一个是上级？",
                              "affects": affects(p_key, c_key), "candidates": [], "origin": "server", "_order": order})
        for subject, aff, kind, order in placement_q:
            if any(q["subject"] == subject for q in questions):
                continue
            questions.append({"subject": subject, "question": f"配合 {kind} 涉及的零件没有放入装配，放在哪里？", "affects": aff,
                              "candidates": [], "origin": "server", "_order": order})

        # Turbo: adopted values for conflicts and blocking missing values; the rest stays open. Every adopted value is a
        # decision with its trigger, the other readings and where each reading stands (generation supplement D-02).
        for c in conflicts:
            own = c["subject"].split(".", 1)[0]
            a = adopt.get(c["subject"]) if self.turbo else None
            title_update = self.title_updates.get(own) if c["subject"] == f"{own}.number" else None
            if self.turbo and title_update:
                a = {"v": title_update["number"], "u": None, "b": title_update["basis"]}
            if self.turbo and self.adopt is None and not title_update:
                adoptable.append(self._open_item("conflict", c["subject"], name_of, fact_of, values=c["values"]))
            if a is not None and value_for(c["subject"], a.get("v")) is not None:
                v = value_for(c["subject"], a["v"])
                unit = norm_unit(a.get("u")) or c["values"][0].get("unit")
                assumptions.append({"subject": c["subject"], "field": c["subject"].split(".", 1)[1], "adopted": v, "unit": unit,
                                    "basis": _basis(a), "alternatives": [x["value"] for x in c["values"] if _vkey(x["value"], x.get("unit")) != _vkey(v, unit)][:20],
                                    "affected": affects(own) if ".qty[" not in c["subject"] else affects(own, c["subject"].split("[", 1)[1].rstrip("]")),
                                    "sources": [reading_locator(x) for x in c["values"]][:20], "trigger": "conflict",
                                    "_order": c["_order"], "_conflict": c, "_fact": fact_of.get(c["subject"])})
            else:
                qty = ".qty[" in c["subject"]
                readings_txt = "；".join(f"{x['value']}{x.get('unit') or ''}（{x['source']}）" for x in c["values"])
                questions.append({"subject": c["subject"], "question": f"{c['subject']} 有不同读数：{readings_txt}。采用哪一个？",
                                  "affects": affects(own, c["subject"].split("[", 1)[1].rstrip("]")) if qty else affects(own),
                                  "candidates": [x["value"] for x in c["values"]][:20], "origin": "server", "_order": c["_order"]})
        for subject, q in miss.items():
            blocks = field_blocks(subject)
            a = adopt.get(subject) if self.turbo else None
            if self.turbo and self.adopt is None and blocks:
                base = subject[:-5] if q["_kind"] == "unit" else subject
                adoptable.append(self._open_item(q["_kind"], subject, name_of, fact_of, candidates=q["candidates"],
                                                 quote=(q.get("_ev") or {}).get("quote"), peers_field=base.split(".", 1)[1],
                                                 owner=base.split(".", 1)[0], value=q.get("_value")))
            if a is not None and value_for(subject, a.get("v")) is not None:
                v = value_for(subject, a["v"])
                own = subject.split(".", 1)[0]
                if q["_kind"] == "unit":
                    unit = None
                    v = norm_unit(v) or v
                else:
                    unit = norm_unit(a.get("u")) or (("pcs" if ".qty[" in subject else "mm") if _is_number(v) else None)
                assumptions.append({"subject": subject, "field": subject.split(".", 1)[1], "adopted": v, "unit": unit,
                                    "basis": _basis(a), "alternatives": [x for x in q["candidates"] if x != v][:20],
                                    "affected": affects(own), "sources": [reading_locator(q["_ev"])] if q.get("_ev") else [],
                                    "trigger": "missing", "_order": q["_order"], "_missing": q})
                continue
            questions.append({k: val for k, val in q.items() if k not in ("_ev", "_kind", "_value")})

        # A value the sources state without a conflict is never replaced by an AI decision (D-03): an adoption for anything
        # that is not an open item is dropped; one for a stated value leaves a note on its definition.
        open_ids = {c["subject"] for c in conflicts} | set(miss)
        dropped: list[dict[str, Any]] = []
        for aid, a in sorted(adopt.items()) if self.turbo else ():
            if aid in open_ids:
                continue
            f = fact_of.get(aid)
            stated = f is not None and f["status"] in ("asserted", "derived") and f.get("value") is not None
            dropped.append({"subject": aid, "adopted": a.get("v"), "unit": a.get("u"), "reason": "stated_value" if stated else "not_open",
                            **({"stated": f["value"], "stated_unit": f.get("unit")} if stated else {})})
            d_note = defs_by_key.get(aid.split(".", 1)[0]) or root
            if stated and d_note is not None:
                text = (f"AI 判断未采用：{aid} 资料已写明 {f['value']}{(' ' + f['unit']) if f.get('unit') else ''} 且没有冲突，保留资料值"
                        f"（AI 曾提出 {a.get('v')}{(' ' + str(a['u'])) if a.get('u') else ''}）。")
                if text not in d_note.notes:
                    d_note.notes.append(text)

        # apply the adopted values (turbo) to the facts / conflicts / instances counted above
        assumptions.sort(key=lambda a: a["_order"])
        akeys = _Keys("A", 2, {a.get("subject"): a["id"] for a in prev.get("assumptions") or [] if a.get("id")},
                      [a["subject"] for a in assumptions])
        seen_a: set[str] = set()
        final_assumptions: list[dict[str, Any]] = []
        for a in assumptions:
            if a["subject"] in seen_a:
                continue
            seen_a.add(a["subject"])
            a["id"] = akeys.get(a["subject"])
            if a.get("_conflict") is not None:
                a["_conflict"]["resolution"] = {"assumption": a["id"]}
                f = a.get("_fact")
                if f is not None:
                    f.update(value=a["adopted"], unit=a["unit"] or f.get("unit"), status="assumed", derivation=a["id"])
            elif a.get("_missing") is not None:
                subject = a["subject"]
                if a["_missing"]["_kind"] == "unit":
                    f = fact_of.get(subject[:-5])
                    if f is not None:
                        f.update(unit=a["adopted"], status="assumed", derivation=a["id"])
                else:
                    f = fact_of.get(subject)
                    if f is None:
                        f = {"subject": subject, "kind": "material" if subject.endswith(".material") else "dimension", "value": a["adopted"],
                             "unit": a["unit"], "status": "assumed", "evidence": [], "derivation": a["id"], "_order": a["_order"]}
                        facts.append(f)
                        fact_of[subject] = f
                    else:
                        f.update(value=a["adopted"], unit=a["unit"] or f.get("unit"), status="assumed", derivation=a["id"])
            final_assumptions.append({k: v for k, v in a.items() if not k.startswith("_")})

        # keys of facts / notes / questions / conflicts, definitions' fact ids
        facts.sort(key=lambda f: f["_order"])
        fkeys = _Keys("F", 3, {f.get("subject"): f["id"] for f in prev.get("facts") or [] if f.get("id")}, [f["subject"] for f in facts])
        out_facts = []
        def_facts: dict[str, list[str]] = {}
        rel_fact: dict[str, list[str]] = {}
        for f in facts:
            f["id"] = fkeys.get(f["subject"])
            own = f["subject"].split(".", 1)[0]
            (def_facts if own in defs_by_key else rel_fact).setdefault(own, []).append(f["id"])
            out_facts.append({k: v for k, v in f.items() if not k.startswith("_")})
        for rel in relations:
            rel["fact_ids"] = rel_fact.get(rel["key"], [])
        conflicts.sort(key=lambda c: c["_order"])
        ckeys = _Keys("C", 2, {c.get("subject"): c["id"] for c in prev.get("conflicts") or [] if c.get("id")}, [c["subject"] for c in conflicts])
        out_conflicts = [{"id": ckeys.get(c["subject"]), **{k: v for k, v in c.items() if not k.startswith("_")}} for c in conflicts]
        questions.sort(key=lambda q: q["_order"])
        qkeys = _Keys("Q", 2, {q.get("subject"): q["id"] for q in prev.get("questions") or [] if q.get("id")}, [q["subject"] for q in questions])
        out_questions, seen_q = [], set()
        for q in questions:
            if q["subject"] in seen_q:
                continue
            seen_q.add(q["subject"])
            out_questions.append({"id": qkeys.get(q["subject"]), **{k: v for k, v in q.items() if not k.startswith("_")}})

        notes_raw: list[tuple[tuple[int, int], str, str, dict[str, Any] | None]] = []
        for ch in self.chunks:
            for i, n in enumerate(ch.data.get("notes") or []):
                ds = drefs(ch.seq, n.get("c")) if n.get("c") else []
                text = str(n.get("text") or n.get("q") or "").strip()
                if text:
                    ev = _ev(ch.source, ch.seq, n) or {"source": ch.source, "chunk": ch.seq, "quote": text[:200], "entity": None,
                                                       "region": None, "detail": ""}
                    for subject in [d.key for d in ds] or [root.key if root is not None else ""]:
                        notes_raw.append(((ch.seq, i), subject, text[:4000], ev))
        nsigs: list[tuple[str, str]] = []
        note_items: dict[tuple[str, str], dict[str, Any]] = {}
        for _order, subject, text, ev in notes_raw:
            sig = (subject, re.sub(r"\s+", " ", _nfkc(text)).casefold())
            if sig in note_items:
                note_items[sig]["evidence"] = _dedupe_ev(note_items[sig]["evidence"] + [ev], 10)
                continue
            nsigs.append(sig)
            note_items[sig] = {"subject": subject, "text": text, "fact_ids": [], "evidence": [ev] if ev else []}
        prev_notes = {(n.get("subject") or "", re.sub(r"\s+", " ", _nfkc(n.get("text") or "")).casefold()): n["id"]
                      for n in prev.get("manufacturing_notes") or [] if n.get("id")}
        nkeys = _Keys("N", 2, prev_notes, nsigs)
        out_notes = [{"id": nkeys.get(sig), **note_items[sig]} for sig in nsigs]

        deliverables: list[dict[str, Any]] = []
        for ch in self.chunks:
            for k in ch.data.get("dl") or []:
                if k in DELIVERABLE_KINDS and k not in [x["kind"] for x in deliverables]:
                    deliverables.append({"kind": k, "required": True})

        definitions = []
        for d in sorted(defs, key=lambda d: _key_no(d.key)):
            kind, procurement, representation = kinds[id(d)]
            mat = fact_of.get(f"{d.key}.material")
            tpl = next((m.tpl for m in d.mentions if m.tpl), None)
            definitions.append({"key": d.key, "kind": kind, "name": d.name[:200], "number": d.number, "size": d.size, "aliases": d.aliases,
                                "procurement": procurement, "representation": representation, "template": tpl,
                                "material": str(mat["value"])[:120] if mat and mat["status"] in ("asserted", "derived") and mat["value"] is not None else None,
                                "fact_ids": def_facts.get(d.key, []), "notes": "\n".join(d.notes)[:2000]})
        content = {
            "summary": SERVER_SUMMARY.format(chunks=len(self.chunks), defs=len(definitions), facts=len(out_facts), conflicts=len(out_conflicts)),
            "units": "mm",
            "root": root.key if root is not None else None,
            "definitions": definitions,
            "instances": instances,
            "relations": relations,
            "facts": out_facts,
            "manufacturing_notes": out_notes,
            "deliverables": deliverables,
            "questions": out_questions,
            "conflicts": out_conflicts,
            "assumptions": final_assumptions,
            # generation reliability A (design §6.1 / §6.2): the structure the definitions were keyed to, and the assembly-level
            # geometry it states (evidence dicts in ``sources``; the service writes their locators when it finishes the spec)
            "skeleton": [{"key": e["key"], "id": e.get("id"), "name": e.get("name"), "number": e.get("number"), "size": e.get("size"),
                          "aliases": list(e.get("aliases") or []), "kind": e.get("kind"),
                          "parents": [{"parent": u["parent"], "quantity": u["n"]} for u in e.get("uses") or []],
                          "sources": [x for x in [e.get("ev"), *(u.get("ev") for u in e.get("uses") or [])] if x][:20]}
                         for e in self.skeleton.entries],
            "interfaces": [{**{k: v for k, v in i.items() if k != "ev"}, "sources": [i["ev"]] if i.get("ev") else []}
                           for i in self.skeleton.interfaces],
            "unbound": list(bind_record.get("unbound") or [])[:500],
        }
        record = {"chunks": len(self.chunks), "skeleton": len(self.skeleton.entries), "interfaces": len(self.skeleton.interfaces),
                  **{k: v[:100] for k, v in bind_record.items()}, "skeleton_problems": self.skeleton.problems[:50],
                  "facts_unplaced": unplaced, "adopted": len(final_assumptions), "open_values": len(adoptable),
                  "reused_decisions": sum(1 for a in adopt.values() if a.get("reused")) if self.turbo else 0,
                  "dropped_decisions": dropped[:50], "title_identity_updates": list(self.title_updates.values()),
                  "standard_identity_splits": bind_record.get("standard_identity_splits", []),
                  "source_memberships": bind_record.get("source_memberships", [])}
        return Merged(content, adoptable, record, missing_evidence)

    # -- helpers -------------------------------------------------------------------------------------------------------
    @staticmethod
    def _identity_readings(readings: dict[str, list[_Reading]], d: _Def) -> None:
        """The component's number and size as the skeleton and each chunk that writes them state them. When they
        disagree (a chunk writes the component's skeleton key with ``GB/T 93`` for ``GB/T 93-1987``, ``M12`` for
        ``M12×30``) every reading is kept on ``<key>.number`` / ``<key>.size`` — a conflict like any other — and the
        component stays one; readings that agree add nothing."""
        skel = d.skel or {}
        sev = next((e for e in [skel.get("ev"), *(u.get("ev") for u in skel.get("uses") or [])] if e), None)
        ms = sorted(d.mentions, key=lambda m: m.order)
        for fld in ("number", "size"):
            own = skel.get(fld)
            stated = [(m, raw, v) for m in ms if (raw := m.number if fld == "number" else m.size)
                      and (v := raw if fld == "number" else size_of(raw, None))]
            if not stated or len({norm_name(v) for v in [own, *(v for _, _, v in stated)] if v}) < 2:
                continue
            subject, spelling = f"{d.key}.{fld}", {}  # one spelling per normalised value, the skeleton's first
            first = stated[0][0]
            if own:
                readings.setdefault(subject, []).append(_Reading(
                    (first.seq, 0, first.idx), (sev or {}).get("source") or "", (sev or {}).get("chunk") or 0,
                    spelling.setdefault(norm_name(own), str(own)), None, "asserted" if sev else "derived",
                    None if sev else "skeleton reading (its quote was not found in the sources)", sev))
            for m, raw, v in stated:
                readings.setdefault(subject, []).append(_Reading(
                    (m.seq, 0, m.idx), m.source, m.seq, spelling.setdefault(norm_name(v), str(v)), None, "asserted", None,
                    {"source": m.source, "chunk": m.seq, "quote": str(raw)[:400], "entity": None, "region": None, "detail": ""}))
            if fld == "number" and d.title_update:
                for ev in d.title_update["title"]["title_evidence"]:
                    readings.setdefault(subject, []).append(_Reading(
                        (int(ev["chunk"]), 0, first.idx), ev["source"], int(ev["chunk"]), d.number,
                        None, "asserted", None, ev))

    def _reading(self, readings: dict[str, list[_Reading]], subject: str, order: tuple[int, int, int], ch: ChunkFindings,
                 f: dict[str, Any]) -> None:
        # declared unknown by the source: no value (and no question unless the field blocks a part, see finalize)
        value = None if f.get("st") == "unknown" else value_for(subject, f.get("v"))
        unit = norm_unit(f.get("u"))
        status = "derived" if f.get("r") else f.get("st") or "asserted"
        how = f.get("how")
        unitless = False
        if _is_number(value) and unit is None:
            if ch.source in self.mm_sources:
                unit, how = "mm", how or "unit from the source's unit declaration (mm)"
            else:
                status, unitless = "unknown", True
        if value is None and status != "unknown":
            status = "unknown"
        readings.setdefault(subject, []).append(_Reading(order, ch.source, ch.seq, value, unit, status, how, _ev(ch.source, ch.seq, f), unitless))

    @staticmethod
    def _open_item(kind: str, subject: str, name_of: dict[str, str], fact_of: dict[str, dict[str, Any]],
                   *, values: list[dict[str, Any]] | None = None, candidates: list[Any] | None = None,
                   quote: str | None = None, peers_field: str | None = None, owner: str | None = None, value: Any = None) -> dict[str, Any]:
        own = owner or subject.split(".", 1)[0]
        item: dict[str, Any] = {"id": subject, "kind": kind, "component": name_of.get(own, own),
                                "field": (peers_field or subject.split(".", 1)[1])}
        if values is not None:
            item["readings"] = [{k: v[k] for k in ("value", "unit", "source", "chunk", "quote", "entity", "region", "detail", "status", "derivation")
                                 if v.get(k) is not None} for v in values]
        if candidates:
            item["candidates"] = candidates
        if quote:
            item["quote"] = quote
        if kind == "unit":
            item["value"] = value
        elif kind == "missing":
            item["unit"] = "pcs" if ".qty[" in subject else "mm"
            fld = peers_field or subject.split(".", 1)[1]
            peers = [{"component": name_of.get(f["subject"].split(".", 1)[0], ""), "value": f["value"], "unit": f.get("unit")}
                     for s, f in fact_of.items() if s.split(".", 1)[-1] == fld and s.split(".", 1)[0] != own
                     and f["status"] in ("asserted", "derived") and f.get("value") is not None]
            if peers:
                item["peers"] = peers[:10]
        return item
