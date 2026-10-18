"""Turbo alias / structure resolution in the spec step (generation supplement H; .trellis/tasks/archive/2026-10/10-12-generation-supplement/
status-h.md, design §2.1–2.3) — the FALLBACK for spec versions without a skeleton.

Generation reliability A (design §6.1): a version read since then has a skeleton (``content.skeleton``): its
definitions were keyed to the structure reading of all sources before any chunk was read, so aliases are never
created and nothing is merged after the fact — :func:`precheck_aliases` skips such a version. What follows applies only
to versions without one (read before the skeleton, or from attachments only).

Why: in the BHD1 real-model run 37 of 90 part definitions hung nowhere under the root. Most were the same part the
chunk merge recognised twice under different names (no drawing number to tie them: "lock washer" / "Ø8 spring washer", "B3 pressure disc" /
"pressure disc (B3)", "cover" / "cover weldment"); a few were no part at all (a cable, a title-like assembly). They were counted
as structure gaps; the A-03 rule repair (``attachments.hang_unplaced``) only acts when NOTHING hangs under the root.

What (Turbo only, in the spec-node pre-check, for an extracted version and for an exact referenced one): when some
part definitions hang under the root and others do not, the unplaced top-level definitions go to the run's planning
model in at most :data:`MAX_CALLS` structured calls (``ai/alias_agent``), each with candidate definitions of the tree
pre-selected by name / kind / size similarity (:func:`candidates`, at most :data:`MAX_CANDIDATES`). The answers are
applied through a derived spec version (``engineering_spec.derive_rule_version``, origin ``rule``, rule
:data:`RULE`, the call ids in its planning record like the other spec calls) by the pure :func:`apply`:

- ``same_as <key>``: the alias is merged into the target — its facts re-keyed onto the target where they do not
  conflict (an equal reading only adds its evidence; a different one keeps the target's reading as a ``rule`` decision
  with ``trigger: conflict`` and the other reading in ``alternatives``, and a conflict entry); its children (an
  assembly alias) re-parented, except those whose definition the target already holds (left out with one ``rule``
  decision each, ``children_skipped`` in the merge record: the alias describes the same children again); the alias
  definition removed and listed in the version's merge record (``validation.derived.merged``). Merged aliases leave the
  target count (no part counted twice). Alias and target are of one kind group (part / standard part, or assembly),
  and the target is never inside the alias.
- ``belongs_to <assembly>``: hung under that assembly with the stated quantity (placement left open for the assembly
  step).
- ``not_a_part`` / ``unknown``: stays unplaced; the decision is the reason (generation reliability K,
  ``services/spec_identity``: a non-part is in the exclusion audit — identity class ``not_a_part``, never a missing part;
  an unknown one is ``unresolved``, a ``structure`` gap ``definition_unresolved`` with the decision's basis).

Generation reliability K: every ``same_as`` also passes the deterministic identity guard
(``spec_identity.merge_guard``): different drawing numbers, series / size tags (B3 vs Ø60) or key name characters (pressure plate vs
pressure disc) are never merged unless a source sentence explicitly says they are one part; a refused pair is listed under
``rejected`` (``guard: true``) and the definition stays unresolved with that reason. Titles, "—" and placeholders are
never asked (no part by rule).

Every applied answer is one ``ai_turbo`` decision on the definition (``field: identity`` with ``trigger: ambiguous`` for
same_as / not_a_part; ``field: parent`` with ``trigger: missing`` for belongs_to / unknown), with ``affected``,
``basis`` and ``sources``. An invalid answer (unknown target, two different drawing numbers, another kind group, a
cycle) is not applied and is listed in the record. A provider error never fails the spec step: the definitions stay
unplaced and the step notes say why. The derived version is made only when every asked batch was answered; when one
batch failed nothing is applied (the step record says why) and the next run asks again. A version that already carries
these decisions, or whose derived version exists (a simulated one only in simulation mode), is not asked again.

Standard: nothing is called; unplaced definitions keep being listed as structure gaps.
"""

from __future__ import annotations

import copy
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import alias_agent as agent
from ..ai.providers.base import ProviderError, clip
from ..ai.spec_agent import PROVIDER_PURPOSE
from ..models import EngineeringSpecChunk, EngineeringSpecVersion
from ..storage import read_blob

log = logging.getLogger("partroute.spec_aliases")

RULE = "turbo_alias_resolution"
#: Definitions per call, calls per resolution, candidates per definition (design: ONE call, at most 3 when batched).
BATCH = 20
MAX_CALLS = 3
MAX_CANDIDATES = 8
MAX_FACTS = 8
MAX_SENTENCES = 3
MAX_ASSEMBLIES = 40
MAX_CHUNKS = 60
CALL_TIMEOUT_S = 180
#: The fields of a structure decision on a definition (the coverage reason of a definition left unplaced).
DECISION_FIELDS = ("identity", "parent")
_SENTENCE = re.compile(r"(?<=[。！？；!?;])|\n+")
_SIZE_FIELDS = re.compile(r"(length|width|height|thickness|diameter|outer_diameter|inner_diameter|radius|depth|dim_|size|长|宽|高|厚|径)", re.I)


def _defs(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["key"]: d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}


def structure_decision(content: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The AI structure decision recorded on a definition (subject = its key, field identity / parent), if any."""
    return next((a for a in content.get("assumptions") or [] if isinstance(a, dict) and a.get("subject") == key
                 and a.get("field") in DECISION_FIELDS and a.get("author") == "ai_turbo"), None)


def _children(content: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in content.get("instances") or []:
        if isinstance(i, dict) and i.get("parent") and i.get("definition"):
            out.setdefault(i["parent"], set()).add(i["definition"])
    return out


def _descendants(children: dict[str, set[str]], key: str) -> set[str]:
    seen: set[str] = set()
    todo = [key]
    while todo:
        k = todo.pop()
        for c in children.get(k, ()):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


def to_ask(content: dict[str, Any]) -> list[str]:
    """The unplaced definitions to resolve: the top-level ones (no definition contains them; their children follow
    them), parts and the assemblies that contain something, without a structure decision yet. Empty unless some part
    definition hangs under the root (nothing under it is the A-03 rule repair's case)."""
    from .assemblies import spec_tree
    from .spec_coverage import targets
    from .spec_identity import placeholder_reason

    t = targets(content)
    if t["definitions"] == 0 or not t["unplaced"]:
        return []
    defs = _defs(content)
    tree = spec_tree(content)
    children = _children(content)
    contained = {c for p, cs in children.items() if p in defs for c in cs}
    out = []
    for k in tree["unplaced"]:
        d = defs.get(k) or {}
        if k == tree["root"] or k in contained or structure_decision(content, k) is not None:
            continue
        if placeholder_reason(d, content) is not None:
            continue  # generation reliability K: a title / "—" / placeholder is no part by rule (the exclusion audit)
        if d.get("kind") == "assembly" and not children.get(k):
            continue
        out.append(k)
    return out


# --- context -----------------------------------------------------------------------------------------------------------


def _facts_of(content: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [f for f in content.get("facts") or [] if isinstance(f, dict) and str(f.get("subject") or "").split(".")[0] == key]


def _locator(e: dict[str, Any]) -> str:
    return f"{e.get('source')} · {e.get('locator')}" if e.get("locator") else str(e.get("source") or "")


def _fact_entry(f: dict[str, Any]) -> dict[str, Any]:
    ev = next((e for e in f.get("evidence") or [] if isinstance(e, dict) and e.get("quote")), None)
    value = f.get("value")
    out: dict[str, Any] = {"field": str(f.get("subject") or "").split(".", 1)[-1],
                           "value": value if isinstance(value, (int, float)) or value is None else clip(str(value), 120),
                           "unit": f.get("unit"), "status": f.get("status")}
    if ev:
        out["quote"], out["locator"] = clip(str(ev["quote"]), 160), _locator(ev)
    return out


def _sizes(content: dict[str, Any]) -> dict[str, set[tuple[str, Any, Any]]]:
    """Definition key → its numeric size facts (field, value, unit)."""
    out: dict[str, set[tuple[str, Any, Any]]] = {}
    for f in content.get("facts") or []:
        if not isinstance(f, dict):
            continue
        key, _, fld = str(f.get("subject") or "").partition(".")
        v = f.get("value")
        if _SIZE_FIELDS.search(fld) and isinstance(v, (int, float)) and not isinstance(v, bool):
            out.setdefault(key, set()).add((fld, round(float(v), 4), f.get("unit")))
    return out


def _norm_number(n: Any) -> str:
    return re.sub(r"[\s\-_./]+", "", unicodedata.normalize("NFKC", str(n or "")).casefold())


def candidates(content: dict[str, Any], key: str, sentences: list[dict[str, str]] | None = None, *,
               sizes_of: dict[str, set[tuple[str, Any, Any]]] | None = None) -> list[str]:
    """At most :data:`MAX_CANDIDATES` definitions of the tree that look like ``key``: shared CJK characters / tokens of
    the names (spaces and brackets removed), the same drawing number, the same kind, shared size facts; an assembly
    named in a source sentence that names ``key`` (where it may belong)."""
    from .assemblies import spec_tree

    defs = _defs(content)
    tree = spec_tree(content)
    d = defs.get(key) or {}
    sizes_of = _sizes(content) if sizes_of is None else sizes_of
    sizes = sizes_of.get(key, set())
    texts = " ".join(s.get("text") or "" for s in sentences or [])
    scored: list[tuple[float, int, str]] = []
    order = [k for k in defs if k in tree["definitions"]]
    for n, k in enumerate(order):
        t = defs[k]
        if k == key:
            continue
        score = agent.name_similarity(d.get("name"), t.get("name"))
        if d.get("number") and t.get("number") and _norm_number(d["number"]) == _norm_number(t["number"]):
            score += 2.0
        shared = len(sizes & sizes_of.get(k, set()))
        if score > 0 or shared >= 2:
            score += 0.3 * min(shared, 3)
        mentioned = t.get("kind") == "assembly" and len(str(t.get("name") or "")) >= 2 and str(t["name"]) in texts
        if mentioned:
            score += 1.0
        if score <= 0:
            continue
        same_group = (t.get("kind") == "assembly") == (d.get("kind") == "assembly")
        score += 0.2 if same_group else 0.0
        scored.append((score, n, k))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [k for _, _, k in scored[:MAX_CANDIDATES]]


def _read_sentences(db: Session, v: EngineeringSpecVersion) -> list[dict[str, str]]:
    """Every sentence of the version's text / drawing chunks with its locator (the source id and the chunk)."""
    rows = db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.id, EngineeringSpecChunk.kind != "attachment",
                                                         EngineeringSpecChunk.text_sha256.is_not(None))
                      .order_by(EngineeringSpecChunk.seq).limit(MAX_CHUNKS)).all()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in rows:
        try:
            text = read_blob(db, c.text_sha256)[0].decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            continue
        for s in _SENTENCE.split(text):
            s = " ".join((s or "").split())
            if s and s not in seen:
                seen.add(s)
                out.append({"locator": f"{c.source_id} · chunk {c.seq}", "text": s})
    return out


def _mentions(sentences: list[dict[str, str]], name: str, longer: list[str] | None = None) -> list[dict[str, str]]:
    """The sentences that name ``name`` (as written, or with its spaces collapsed) other than inside a ``longer`` name
    of another definition (the Chinese cover name within the longer cover-weldment name), at most :data:`MAX_SENTENCES`."""
    variants = {v for v in (name.strip(), "".join(name.split())) if v}
    out: list[dict[str, str]] = []
    for s in sentences:
        text = s["text"]
        for other in longer or []:
            text = text.replace(other, " ")
        if any(v in text or v in "".join(text.split()) for v in variants):
            out.append({"locator": s["locator"], "text": clip(s["text"], 300)})
            if len(out) >= MAX_SENTENCES:
                break
    return out


def context(content: dict[str, Any], keys: list[str], sentences: list[dict[str, str]]) -> tuple[dict[str, Any], set[str]]:
    """(context JSON, locators it shows) of one call: the definitions to answer for with their facts, notes, source
    sentences and candidate keys; the candidates in the tree (name, number, kind, parents, quantity, facts); every
    assembly of the tree."""
    from .assemblies import spec_tree
    from .engineering_spec import quantities
    from .spec_identity import merge_guard

    defs = _defs(content)
    tree = spec_tree(content)
    qty = quantities(content)
    parents: dict[str, list[str]] = {}
    for i in tree["instances"]:
        parents.setdefault(i.get("definition"), [])
        if (i.get("parent") or tree["root"]) not in parents[i.get("definition")]:
            parents[i.get("definition")].append(i.get("parent") or tree["root"])
    locators: set[str] = set()
    unplaced: list[dict[str, Any]] = []
    wanted: list[str] = []
    sizes_of = _sizes(content)
    names = [str(d.get("name") or "").strip() for d in defs.values()]
    for k in keys:
        d = defs.get(k) or {}
        name = str(d.get("name") or "").strip()
        said = _mentions(sentences, name, [n for n in names if n != name and name and name in n])
        facts = [_fact_entry(f) for f in _facts_of(content, k)][:MAX_FACTS]
        cands = candidates(content, k, said, sizes_of=sizes_of)
        entry: dict[str, Any] = {"key": k, "name": d.get("name") or k, "number": d.get("number"), "kind": d.get("kind") or "part",
                                 "facts": facts, "sentences": said, "candidates": cands}
        # generation reliability K: candidates the server never merges with this definition (the guard says why)
        never = {c: g for c in cands if (g := merge_guard(d, defs.get(c) or {}, sentences)) is not None}
        if never:
            entry["never_same"] = never
        notes = [clip(str(n.get("text") or ""), 200) for n in content.get("manufacturing_notes") or [] if isinstance(n, dict) and n.get("subject") == k][:3]
        if d.get("notes"):
            notes.insert(0, clip(str(d["notes"]), 300))
        if notes:
            entry["notes"] = notes
        child_defs = sorted({i.get("definition") for i in content.get("instances") or [] if isinstance(i, dict) and i.get("parent") == k} - {None})
        if child_defs:
            entry["contains"] = [{"key": c, "name": (defs.get(c) or {}).get("name") or c} for c in child_defs][:12]
        locators |= {f["locator"] for f in facts if f.get("locator")} | {s["locator"] for s in said}
        unplaced.append(entry)
        wanted += [c for c in cands if c not in wanted]
    described: dict[str, Any] = {}
    for c in wanted:
        t = defs.get(c) or {}
        described[c] = {"name": t.get("name") or c, "number": t.get("number"), "kind": t.get("kind") or "part",
                        "parents": parents.get(c, []), "quantity": int((qty.get(c) or {}).get("total") or 0),
                        "facts": [f"{e['field']} = {e['value']}{(' ' + str(e['unit'])) if e.get('unit') else ''}" for e in
                                  (_fact_entry(f) for f in _facts_of(content, c)[:6])]}
    assemblies = [{"key": k, "name": d.get("name") or k, "number": d.get("number"), "parent": (parents.get(k) or [None])[0]}
                  for k, d in defs.items() if k in tree["definitions"] and d.get("kind") == "assembly"][:MAX_ASSEMBLIES]
    return {"root": tree["root"], "assemblies": assemblies, "unplaced": unplaced, "tree": described}, locators


# --- apply (pure) ------------------------------------------------------------------------------------------------------


def _next_key(used: set[str], prefix: str, width: int = 2) -> str:
    n = 1
    while f"{prefix}-{n:0{width}d}" in used:
        n += 1
    key = f"{prefix}-{n:0{width}d}"
    used.add(key)
    return key


def _same_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    from .spec_merge import norm_unit

    va, vb = a.get("value"), b.get("value")
    if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool) and not isinstance(vb, bool):
        return abs(float(va) - float(vb)) < 1e-9 and norm_unit(a.get("unit")) == norm_unit(b.get("unit"))
    return unicodedata.normalize("NFKC", str(va)).strip().casefold() == unicodedata.normalize("NFKC", str(vb)).strip().casefold()


def _known(f: dict[str, Any]) -> bool:
    return f.get("value") is not None and f.get("status") != "unknown"


def _text(v: Any, unit: Any) -> str:
    return f"{v}{(' ' + str(unit)) if unit else ''}"


def _reading(f: dict[str, Any]) -> dict[str, Any] | None:
    e = next((e for e in f.get("evidence") or [] if isinstance(e, dict) and e.get("source")), None)
    if e is None:
        return None
    return {"value": f.get("value"), "unit": f.get("unit"), "source": e.get("source"), "chunk": e.get("chunk"), "quote": e.get("quote"),
            "entity": e.get("entity")}


def _rekey(subject: str, alias: str, target: str) -> str:
    head, dot, rest = subject.partition(".")
    if head == alias:
        head = target
    return head + dot + rest.replace(f"[{alias}]", f"[{target}]")


def _check(answer: dict[str, Any], key: str, defs: dict[str, dict[str, Any]], in_tree: set[str], root: str,
           children: dict[str, set[str]], sentences: list[dict[str, str]] | None = None) -> str | None:
    """Why an answer cannot be applied (None: it can). A same_as passes the deterministic identity guard too
    (generation reliability K, ``spec_identity.merge_guard``: different drawing numbers, series tags or key characters are
    never merged unless a source sentence says they are the same part)."""
    from .spec_identity import merge_guard

    a, target = answer.get("answer"), answer.get("target") or ""
    d = defs.get(key) or {}
    if a not in agent.ANSWERS:
        return f"unknown answer {a!r}"
    if a == "same_as":
        t = defs.get(target)
        if t is None or target not in in_tree:
            return f"{target} is not a definition in the tree"
        if target == key:
            return "same_as itself"
        # one kind group (part / standard part vs assembly): a part merged into an assembly would put its facts on a
        # definition that is never modeled (and the root is an assembly)
        if (d.get("kind") == "assembly") != (t.get("kind") == "assembly"):
            alias_kind, target_kind = ("assembly", "part") if d.get("kind") == "assembly" else ("part", "assembly")
            return f"the {alias_kind} {key} cannot be the same as the {target_kind} {target}"
        if target in _descendants(children, key):  # D-07 same_as D-05 while D-07 contains D-05: D-05 would contain itself
            return f"{target} is inside {key} (a cycle)"
        guard = merge_guard(d, t, sentences)
        if guard is not None:
            return guard
    elif a == "belongs_to":
        t = defs.get(target)
        if t is None or target not in in_tree or t.get("kind") != "assembly":
            return f"{target} is not an assembly in the tree"
        if target == key or target in _descendants(children, key):
            return f"{target} is inside {key} (a cycle)"
    return None


def apply(content: dict[str, Any], answers: dict[str, dict[str, Any]], *, order: list[str] | None = None,
          sentences: list[dict[str, str]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The spec content with the answers applied (see the module docstring) and the merge record. Pure. ``sentences``:
    the source sentences the identity guard may find an explicit "same part" statement in (generation reliability K);
    a same_as the guard refuses is listed under ``rejected`` (``guard: true``) and its definition stays unresolved."""
    from .assemblies import spec_tree
    from .spec_identity import explicit_same, merge_guard

    c = copy.deepcopy(content)
    for k in ("definitions", "instances", "facts", "assumptions", "conflicts", "questions", "manufacturing_notes"):
        c.setdefault(k, [])
    defs = _defs(c)
    tree = spec_tree(c)
    root = tree["root"]
    in_tree = set(tree["definitions"])
    children = _children(c)
    used_a = {str(a.get("id")) for a in c["assumptions"] if isinstance(a, dict)}
    used_i = {str(i.get("key")) for i in c["instances"] if isinstance(i, dict)}
    used_c = {str(x.get("id")) for x in c["conflicts"] if isinstance(x, dict)}
    at = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {"rule": RULE, "merged": [], "hung": [], "not_a_part": [], "unknown": [], "rejected": []}
    keys = [k for k in (order or list(answers)) if k in answers]
    rank = {"same_as": 0, "belongs_to": 1, "not_a_part": 2, "unknown": 3}
    for key in sorted(keys, key=lambda k: (rank.get(answers[k].get("answer"), 9), keys.index(k))):
        ans = answers[key]
        if key not in defs:
            continue
        why = _check(ans, key, defs, in_tree, root, children, sentences)
        if why is not None:
            guarded = ans.get("answer") == "same_as" and ans.get("target") in defs and merge_guard(defs[key], defs[ans["target"]], sentences) == why
            record["rejected"].append({"key": key, "answer": ans.get("answer"), "target": ans.get("target") or None, "reason": why,
                                       **({"guard": True} if guarded else {})})
            continue
        d = defs[key]
        name = d.get("name") or key
        kind_zh = {"assembly": "装配", "standard_part": "标准件"}.get(str(d.get("kind")), "零件")
        aid = _next_key(used_a, "A")
        decision: dict[str, Any] = {"id": aid, "subject": key, "unit": None, "basis": ans["basis"], "alternatives": [],
                                    "sources": list(ans.get("sources") or []), "author": "ai_turbo", "at": at}
        a = ans["answer"]
        if a == "same_as":
            target = ans["target"]
            t = defs[target]
            said = explicit_same(sentences, name, t.get("name") or target) if merge_guard(d, t) is not None else None
            if said is not None:  # merged only because a source sentence says so: that sentence is the decision's evidence
                decision["sources"] = list(dict.fromkeys([*decision["sources"], said["locator"]]))
            merged = _merge(c, defs, key, target, aid, used_a, used_c, at)
            if said is not None:
                merged["explicit_evidence"] = {"locator": said["locator"], "text": clip(str(said.get("text") or ""), 300)}
            children = _children(c)  # the alias's children hang under the target now (the next cycle checks see it)
            decision.update(field="identity", trigger="ambiguous", affected=[key, target],
                            adopted=f"与 {target}「{t.get('name') or target}」是同一{kind_zh}：合并到 {target}，{key}「{name}」不再单独计数",
                            alternatives=[f"{key}「{name}」是另一个{kind_zh}"])
            record["merged"].append({"alias": key, "alias_name": name, "into": target, "into_name": t.get("name") or target, "decision": aid,
                                     **merged})
        elif a == "belongs_to":
            target = ans["target"]
            t = defs[target]
            n = int(ans.get("quantity") or 1)
            new = [_next_key(used_i, "I") for _ in range(n)]
            for ik in new:
                c["instances"].append({"key": ik, "definition": key, "parent": target, "name": name, "placement": None, "fact_ids": []})
            children.setdefault(target, set()).add(key)
            decision.update(field="parent", trigger="missing", affected=[key, *new, target],
                            adopted=f"挂到 {target}「{t.get('name') or target}」下，{n} 个实例（{'、'.join(new)}），位置留给装配步骤")
            record["hung"].append({"key": key, "name": name, "under": target, "instances": new, "decision": aid})
        elif a == "not_a_part":
            decision.update(field="identity", trigger="ambiguous", affected=[key],
                            adopted=f"「{name}」不是要建模的{kind_zh}：不挂到装配树，不建模")
            record["not_a_part"].append({"key": key, "name": name, "decision": aid})
        else:
            decision.update(field="parent", trigger="missing", affected=[key],
                            adopted=f"「{name}」暂不挂到装配树：资料没有说明它是哪个已挂零件的别名，也没有说明装在哪里")
            record["unknown"].append({"key": key, "name": name, "decision": aid})
        c["assumptions"].append(decision)
    return c, record


def _merge(c: dict[str, Any], defs: dict[str, dict[str, Any]], alias: str, target: str, aid: str, used_a: set[str], used_c: set[str],
           at: str) -> dict[str, Any]:
    """Merge the alias definition into the target in place (facts, children, questions, conflicts, decisions, notes);
    returns what moved."""
    a_def, t_def = defs[alias], defs[target]
    facts = c["facts"]
    by_subject = {f.get("subject"): f for f in facts if isinstance(f, dict)}
    moved, merged, conflicts = [], [], []
    added: list[tuple[dict[str, Any], dict[str, Any] | None]] = []  # (decision, conflict entry) appended after re-keying
    renamed: dict[str, str] = {}  # dropped alias fact id → the target's fact that holds its reading now
    keep: list[dict[str, Any]] = []
    for f in facts:
        s = str(f.get("subject") or "")
        ns = _rekey(s, alias, target)
        if ns == s:
            keep.append(f)
            continue
        cur = by_subject.get(ns)
        if cur is None:
            f["subject"] = ns
            by_subject[ns] = f
            moved.append(f.get("id"))
            keep.append(f)
            if ns.split(".")[0] == target and f.get("id") not in t_def.setdefault("fact_ids", []):
                t_def["fact_ids"].append(f.get("id"))
            continue
        renamed[str(f.get("id"))] = str(cur.get("id"))
        if not _known(f):  # the alias states nothing here: the target's fact stays as it is
            merged.append(f.get("id"))
        elif not _known(cur):  # the target had no value: the alias's reading fills it
            cur.update(value=f.get("value"), unit=f.get("unit") or cur.get("unit"), status=f.get("status"), derivation=f.get("derivation"))
            cur["evidence"] = (list(cur.get("evidence") or []) + list(f.get("evidence") or []))[:20]
            merged.append(f.get("id"))
        elif _same_value(f, cur):
            cur["evidence"] = (list(cur.get("evidence") or []) + [e for e in f.get("evidence") or [] if e not in (cur.get("evidence") or [])])[:20]
            merged.append(f.get("id"))
        else:
            entry, decision, conflict = _conflict(ns, cur, f, alias, target, aid, used_a, used_c, at, a_def, t_def)
            conflicts.append(entry)
            added.append((decision, conflict))
    c["facts"] = keep
    for coll in ("instances", "relations", "manufacturing_notes"):
        for x in c.get(coll) or []:
            if isinstance(x, dict) and x.get("fact_ids"):
                x["fact_ids"] = list(dict.fromkeys(renamed.get(str(i), str(i)) for i in x["fact_ids"]))
    # The alias's children move under the target — except those whose definition the target already holds: the alias
    # describes the same assembly again (a title-like "BHD1" with base + cover plate merged into the root that holds them), so
    # its copies are left out (one rule decision per such definition) instead of doubling the quantities.
    under_target: dict[str, list[str]] = {}
    for i in c["instances"]:
        if isinstance(i, dict) and i.get("parent") == target and i.get("definition"):
            under_target.setdefault(i["definition"], []).append(str(i.get("key")))
    moved_children: list[str] = []
    skipped: dict[str, list[str]] = {}
    kept_instances: list[dict[str, Any]] = []
    for i in c["instances"]:
        if i.get("parent") == alias:
            if i.get("definition") in under_target:
                skipped.setdefault(i["definition"], []).append(str(i.get("key")))
                continue
            i["parent"] = target
            moved_children.append(str(i.get("key")))
        if i.get("definition") == alias:
            i["definition"] = target
        kept_instances.append(i)
    c["instances"] = kept_instances
    if skipped:
        gone = {k for ks in skipped.values() for k in ks}
        c["relations"] = [r for r in c.get("relations") or [] if not isinstance(r, dict) or not (
            (r.get("a") or {}).get("instance") in gone or (r.get("b") or {}).get("instance") in gone)]
    children_skipped = []
    for child, keys in skipped.items():
        did = _next_key(used_a, "A")
        cname = (defs.get(child) or {}).get("name") or child
        held = under_target[child]
        added.append(({
            "id": did, "subject": child, "field": "quantity", "unit": None, "trigger": "conflict",
            "adopted": f"{target} 下保留已有的 {len(held)} 个「{cname}」实例（{'、'.join(held)}），别名 {alias} 里的 {len(keys)} 个不再挂入",
            "basis": (f"AI 判断 {aid} 认定 {alias}「{a_def.get('name') or alias}」与 {target}「{t_def.get('name') or target}」是同一装配；"
                      f"{target} 已包含 {child}「{cname}」，别名里的 {'、'.join(keys)} 是同一组零件的另一处描述，按规则不重复计数"),
            "alternatives": [f"再加上别名里的 {len(keys)} 个（合计 {len(held) + len(keys)} 个）"],
            "affected": [child, target, alias, *keys], "sources": [], "author": "rule", "at": at}, None))
        children_skipped.append({"definition": child, "name": cname, "instances": keys, "kept": held, "decision": did})
    for q in c["questions"]:
        q["subject"] = _rekey(str(q.get("subject") or ""), alias, target)
        q["affects"] = list(dict.fromkeys(target if x == alias else x for x in q.get("affects") or []))
    for x in c["conflicts"]:
        x["subject"] = _rekey(str(x.get("subject") or ""), alias, target)
    for a in c["assumptions"]:
        a["subject"] = _rekey(str(a.get("subject") or ""), alias, target)
        a["affected"] = list(dict.fromkeys(target if x == alias else x for x in a.get("affected") or []))
    for n in c["manufacturing_notes"]:
        if n.get("subject") == alias:
            n["subject"] = target
    for decision, conflict in added:
        c["assumptions"].append(decision)
        if conflict is not None:
            c["conflicts"].append(conflict)
    filled = []
    for fld in ("number", "material"):
        if not t_def.get(fld) and a_def.get(fld):
            t_def[fld] = a_def[fld]
            filled.append(fld)
    if not t_def.get("template") and a_def.get("template"):
        t_def["template"] = a_def["template"]
        filled.append("template")
    note = f"别名「{a_def.get('name') or alias}」（{alias}）已合并到此定义（AI 判断 {aid}）。"
    t_def["notes"] = (f"{t_def.get('notes')}\n{note}" if t_def.get("notes") else note)[:2000]
    t_def["fact_ids"] = list(dict.fromkeys(i for i in t_def.get("fact_ids") or [] if i not in renamed))
    c["definitions"] = [d for d in c["definitions"] if d.get("key") != alias]
    defs.pop(alias, None)
    return {"facts_moved": moved, "facts_merged": merged, "conflicts": conflicts, "filled": filled,
            **({"children_moved": moved_children} if moved_children else {}),
            **({"children_skipped": children_skipped} if children_skipped else {})}


def _conflict(subject: str, kept: dict[str, Any], other: dict[str, Any], alias: str, target: str, aid: str, used_a: set[str],
              used_c: set[str], at: str, a_def: dict[str, Any],
              t_def: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Two readings of one field after a merge: the target's (already in the tree) is kept as a ``rule`` decision with
    ``trigger: conflict``, the alias's reading is the alternative; a conflict entry resolved by that decision."""
    fld = subject.split(".", 1)[-1]
    did = _next_key(used_a, "A")
    locs = [_locator(e) for f in (kept, other) for e in (f.get("evidence") or [])[:1] if isinstance(e, dict)]
    quote = lambda f: next((f" “{clip(str(e['quote']), 60)}”" for e in f.get("evidence") or [] if isinstance(e, dict) and e.get("quote")), "")  # noqa: E731
    decision = {
        "id": did, "subject": subject, "field": fld, "adopted": kept.get("value"), "unit": kept.get("unit"), "trigger": "conflict",
        "basis": (f"AI 判断 {aid} 认定 {alias}「{a_def.get('name') or alias}」与 {target}「{t_def.get('name') or target}」是同一零件，合并后 {fld} 有两个读数："
                  f"{target} 的 {_text(kept.get('value'), kept.get('unit'))}{quote(kept)}，别名的 {_text(other.get('value'), other.get('unit'))}{quote(other)}；"
                  f"按规则保留已挂在装配树上的 {target} 的读数，别名的读数列为备选"),
        "alternatives": [_text(other.get("value"), other.get("unit"))], "affected": [target, alias], "sources": locs[:6], "author": "rule", "at": at}
    readings = [r for r in (_reading(kept), _reading(other)) if r is not None]
    conflict = None
    if len(readings) == 2:
        conflict = {"id": _next_key(used_c, "C"), "subject": subject, "values": readings, "resolution": {"assumption": did}}
    kept.update(status="assumed", derivation=did)
    entry = {"subject": subject, "kept": _text(kept.get("value"), kept.get("unit")), "alias_value": _text(other.get("value"), other.get("unit")),
             "decision": did, **({"conflict": conflict["id"]} if conflict else {})}
    return entry, decision, conflict


# --- resolve (database, model) ----------------------------------------------------------------------------------------


def _existing(db: Session, v: EngineeringSpecVersion) -> EngineeringSpecVersion | None:
    """A version this rule already derived from ``v`` (the same gaps are not decided again). A simulated one only in
    simulation mode or when ``v`` is simulated itself (a real run never builds on decisions a simulated model made; like
    ``orchestration._EarlierBuilds``)."""
    from ..config import get_settings
    from .engineering_spec import effective_status

    q = select(EngineeringSpecVersion).where(EngineeringSpecVersion.parent_version_id == v.id, EngineeringSpecVersion.origin == "rule",
                                             EngineeringSpecVersion.execution_policy == v.execution_policy,
                                             EngineeringSpecVersion.planning["rule"].astext == RULE)
    if not get_settings().ai_simulation and not v.simulated:
        q = q.where(EngineeringSpecVersion.simulated.is_(False))
    rows = db.scalars(q.order_by(EngineeringSpecVersion.version_no.desc())).all()
    return next((r for r in rows if effective_status(r)[0] in ("ready", "needs_answers")), None)


def precheck_aliases(db: Session, v: EngineeringSpecVersion, *, user_id: uuid.UUID, model_ref: Any = None,
                     provider_of: Callable[[], Any] | None = None) -> Any:
    """The Turbo pre-check step for a version with some definitions under the root and others not: an
    ``attachments.Precheck`` with the derived version (or none, when nothing is asked / applied), the call ids to bind
    to the node run and a brief for the step record. Never fails the spec step."""
    from . import ai as ai_svc
    from . import engineering_spec as spec_svc
    from .attachments import Precheck

    content = v.content or {}
    if content.get("skeleton"):
        # generation reliability A: identity was keyed to the skeleton while reading; nothing is merged after the fact
        return Precheck(aliases={"asked": 0, "calls": 0, "skipped": "skeleton"})
    keys = to_ask(content)
    if not keys or (v.planning or {}).get("rule") == RULE:  # resolved already: what is left stays listed, never asked twice
        return Precheck()
    earlier = _existing(db, v)
    if earlier is not None:
        return Precheck(derived=earlier, rule=RULE, repaired=_changed((earlier.validation or {}).get("derived") or {}),
                        aliases={"asked": len(keys), "calls": 0, "reused_version": earlier.version_no})

    def default_provider() -> Any:
        ref = model_ref if model_ref is not None else ai_svc.frozen_ref((v.planning or {}).get("model_snapshot"), v.model_config_id)
        return ai_svc.provider_for(db, PROVIDER_PURPOSE, ref)[0]

    brief: dict[str, Any] = {"asked": len(keys), "calls": 0}
    try:
        provider = (provider_of or default_provider)()
    except ProviderError as exc:
        brief["error"] = {"code": exc.code, "message": clip(exc.message, 200)}
        return Precheck(aliases=brief)
    sentences = _read_sentences(db, v)
    batches = [keys[i:i + BATCH] for i in range(0, len(keys), BATCH)]
    answers: dict[str, dict[str, Any]] = {}
    calls: list[Any] = []
    problems: list[str] = []
    simulated = False
    failed: list[int] = []  # batches without an answer (context error, failed call, no usable reply)
    for n, batch in enumerate(batches[:MAX_CALLS]):
        try:
            ctx, locators = context(content, batch, sentences)
            user = agent.user_message(ctx, batch)
        except Exception as exc:  # noqa: BLE001 - a context bug must not fail the spec step; the definitions stay unplaced
            log.exception("alias context of spec version %s", v.id)
            problems.append(f"batch {n + 1}: the context could not be built ({type(exc).__name__})")
            failed.append(n + 1)
            continue
        res = agent.run_alias(provider, user, batch, locators=locators, timeout_s=CALL_TIMEOUT_S)
        calls += list(res.calls)
        simulated = simulated or res.simulated
        if res.error is not None:
            problems.append(f"batch {n + 1}: the call failed ({res.error['code']}: {res.error['message']})")
            failed.append(n + 1)
            continue
        problems += res.problems
        if not res.answers:
            failed.append(n + 1)
        answers.update(res.answers)
    asked = [k for b in batches[:MAX_CALLS] for k in b]
    not_asked = [k for b in batches[MAX_CALLS:] for k in b]
    call_ids = spec_svc._record_calls(db, calls)
    brief.update(calls=len(calls), answered=len(answers))
    if not answers or failed:
        # Review fix: a derived version marks the gaps as resolved (never asked again), so it is made only when every
        # asked batch answered; otherwise nothing is applied and the next run asks again.
        brief["problems"] = problems[:20]
        if answers:
            brief["not_applied"] = f"batch {', '.join(map(str, failed))} of {len(batches[:MAX_CALLS])} got no answer: nothing applied, asked again next run"
        return Precheck(call_ids=call_ids, aliases=brief)
    new, record = apply(content, answers, order=asked, sentences=sentences)
    record.update(definitions=asked, calls=len(calls), unanswered=[k for k in asked if k not in answers], not_asked=not_asked,
                  problems=problems[:40])
    brief.update({k: len(record[k]) for k in ("merged", "hung", "not_a_part", "unknown", "rejected")})
    derived = spec_svc.derive_rule_version(db, v, content=new, created_by=user_id, note=record, ai_call_ids=call_ids, simulated=simulated,
                                           planning_extra={"purpose": "planning", "prompt_version": agent.PROMPT_VERSION,
                                                           "provider": getattr(provider, "provider_name", None),
                                                           "model_id": getattr(provider, "model_id", None)})
    return Precheck(derived=derived, rule=RULE, repaired=_changed(record), call_ids=call_ids, aliases=brief)


def _changed(record: dict[str, Any]) -> list[str]:
    """The definitions the derived version changed (aliases merged away, definitions hung)."""
    return [m["alias"] for m in record.get("merged") or []] + [h["key"] for h in record.get("hung") or []]
