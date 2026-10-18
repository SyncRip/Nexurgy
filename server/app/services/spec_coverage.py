"""Target vs actual of a generated assembly (generation supplement §2.2–2.3, B-01, A-03;
.trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.1).

Counts are kept apart: part definitions vs part instances (the same bolt used eight times is one definition and eight
instances), subassemblies on their own line. The target is what the spec recognised (``basis: "spec"``); a total the
sources state but the extraction missed is not known here, so nothing claims the original drawing's count.

- :func:`targets`: what the spec asks for under its root, and the definitions that hang nowhere (``unplaced``).
- :func:`coverage`: one block for an assembly build — the target, what was built, what is placed in the assembly,
  what was only arranged for display, and every missing object with its stage, reason and recovery. The target never
  shrinks to the successful subset; a missing object stays listed.
- :func:`chain_placement` (generation reliability §2.6): "placed" is global — an instance counts installed only when
  every sub-assembly instance above it up to the root has a position too; positioned only inside an unplaced
  sub-assembly is ``local_only``. Round 2 (P): a "floating" group — siblings positioned only by relations to each
  other, none anchored to the parent frame — is ``local_only`` too; ``estimated`` counts the installed instances whose
  position (own or of a sub-assembly instance above) is an AI estimate (``basis_kind`` :data:`BASIS_KINDS`).

Pure functions over the spec content (no database).
"""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from typing import Any, Literal

CoverageStatus = Literal["complete", "partial", "none"]
#: Where a missing object was lost: not recognised in the sources / recognised but not under the root / no geometry /
#: built but could not be placed / dropped while the assembly was made.
MISSING_STAGES: tuple[str, ...] = ("extraction", "structure", "modeling", "placement", "assembly")

_MODELING_DIAGNOSTICS = ("code", "reason", "recovery", "protocol_error")


def _modeling_identity(item: Any) -> tuple[Any, ...] | None:
    if not isinstance(item, dict) or item.get("stage") != "modeling":
        return None
    key, kind, quantity, instances = (item.get(field) for field in ("key", "kind", "quantity", "instances"))
    if (not isinstance(key, str) or not key or not isinstance(kind, str) or not kind
            or type(quantity) is not int or quantity < 0 or not isinstance(instances, list)
            or any(not isinstance(instance, str) or not instance for instance in instances)):
        return None
    # Sorting retains multiplicity: a repeated instance cannot impersonate another one.
    return "modeling", key, kind, quantity, tuple(sorted(instances))


def _modeling_diagnostic_fields(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item.get("code"), str) or not item["code"] or not isinstance(item.get("reason"), str):
        return None
    if "recovery" not in item or (item["recovery"] is not None and not isinstance(item["recovery"], str)):
        return None
    if "protocol_error" in item and type(item["protocol_error"]) is not bool:
        return None
    return {field: item[field] for field in _MODELING_DIAGNOSTICS if field in item}


def project_modeling_diagnostics(frozen: dict[str, Any], current_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Copy frozen coverage and refresh only exactly matched modeling diagnostics.

    Counts, missing rows and their order remain frozen. Repeated identities must
    occur equally often on both sides and state one unambiguous current diagnosis;
    different diagnoses for indistinguishable rows retain the historical values.
    """
    out = deepcopy(frozen)
    missing = out.get("missing")
    if not isinstance(missing, list) or not isinstance(current_items, list):
        return out
    frozen_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    current_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for index, item in enumerate(missing):
        if (identity := _modeling_identity(item)) is not None:
            frozen_groups[identity].append(index)
    for item in current_items:
        if (identity := _modeling_identity(item)) is not None:
            current_groups[identity].append(item)
    for identity, indices in frozen_groups.items():
        current = current_groups.get(identity, [])
        if len(current) != len(indices):
            continue
        diagnoses = [_modeling_diagnostic_fields(item) for item in current]
        if not diagnoses or diagnoses[0] is None or any(diagnosis != diagnoses[0] for diagnosis in diagnoses):
            continue
        for index in indices:
            for field in _MODELING_DIAGNOSTICS:
                missing[index].pop(field, None)
            missing[index].update(deepcopy(diagnoses[0]))
    return out


def _is_json_value(value: Any) -> bool:
    if value is None or type(value) in (str, bool, int, float):
        return True
    if type(value) is list:
        return all(_is_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False


def _strict_json(value: Any) -> str | None:
    try:
        if not _is_json_value(value):
            return None
        # JSON distinguishes true, 1 and 1.0, unlike Python's container equality.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _without_modeling_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(value)
    missing = out.get("missing")
    if isinstance(missing, list):
        for item in missing:
            if isinstance(item, dict) and item.get("stage") == "modeling":
                for field in _MODELING_DIAGNOSTICS:
                    item.pop(field, None)
    return out


def accept_modeling_diagnostics(frozen: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Accept a detached diagnostic projection only when all other JSON is exact.

    Provenance binding is the caller's responsibility. List order and duplicate
    rows participate in the comparison; accepted values are projected onto the
    frozen copy instead of trusting the candidate's remaining fields.
    """
    if not isinstance(frozen, dict) or not isinstance(candidate, dict):
        return None
    frozen_structure = _strict_json(_without_modeling_diagnostics(frozen))
    candidate_structure = _strict_json(_without_modeling_diagnostics(candidate))
    if frozen_structure is None or candidate_structure is None or frozen_structure != candidate_structure:
        return None
    projected = project_modeling_diagnostics(frozen, candidate.get("missing"))
    projected_json, candidate_json = _strict_json(projected), _strict_json(candidate)
    if projected_json is None or candidate_json is None or projected_json != candidate_json:
        return None
    return projected


def with_design_constraints(coverage: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Required design constraints survive even when every recognized instance was placed."""
    missing = list(coverage.get("missing") or [])
    for problem in report.get("problems") or []:
        missing.append(missing_item(stage="assembly", key=problem["key"], kind="constraint",
            code=problem["code"], reason=problem["reason"], recovery="fix_spec", path=problem.get("path")))
    return {**coverage, "constraints": dict(report), "missing": missing,
            "status": "partial" if report.get("status") == "failed" else coverage["status"]}


def _defs(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["key"]: d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}


def targets(content: dict[str, Any]) -> dict[str, Any]:
    """What the spec asks for under its root.

    ``definitions`` / ``instances``: part definitions (part and standard part) under the root and their instance total
    through every level; ``subassemblies`` / ``subassembly_instances``: assembly definitions under the root (the root
    itself not counted); ``recognized_definitions``: part definitions the spec holds at all (under the root or not);
    ``unplaced``: definition keys with no path to the root; ``per_definition``: part key → instance total."""
    from .assemblies import spec_tree
    from .engineering_spec import quantities

    defs = _defs(content)
    tree = spec_tree(content)
    qty = quantities(content)
    root = tree["root"]
    root_is_part = defs.get(root, {}).get("kind") in {"part", "standard_part"}
    under = {k for k in tree["definitions"] if k in defs and (k != root or root_is_part)}
    parts = sorted(k for k in under if defs[k].get("kind") != "assembly")
    subs = sorted(k for k in under if defs[k].get("kind") == "assembly")
    unplaced = [k for k in tree["unplaced"] if k != root]
    per = {k: int((qty.get(k) or {}).get("total") or 0) for k in parts}
    return {"basis": "spec", "root": root, "definitions": len(parts), "instances": sum(per.values()),
            "subassemblies": len(subs), "subassembly_instances": sum(int((qty.get(k) or {}).get("total") or 0) for k in subs),
            "recognized_definitions": sum(1 for k, d in defs.items() if (k != root or root_is_part) and d.get("kind") != "assembly"),
            "unplaced": unplaced, "per_definition": per}


def missing_item(*, stage: str, key: str, name: str = "", kind: str = "part", code: str, reason: str, quantity: int = 0,
                 instances: list[str] | None = None, recovery: str | None = None, **extra: Any) -> dict[str, Any]:
    """One missing object in the shared shape (``stage`` one of :data:`MISSING_STAGES`)."""
    if stage not in MISSING_STAGES:
        raise ValueError(f"unknown missing stage {stage!r}")
    out = {"stage": stage, "key": key, "name": name or key, "kind": kind, "code": code, "reason": reason, "quantity": int(quantity),
           "instances": list(instances or []), "recovery": recovery}
    out.update(extra)
    return out


def _unplaced_item(content: dict[str, Any], key: str, d: dict[str, Any], entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """The structure gap of a real definition that hangs nowhere (generation reliability K: identity class
    ``unresolved``, code ``definition_unresolved``); with a Turbo structure decision on it (generation supplement H,
    ``services/spec_aliases``: not decidable) or a refused merge, that is the reason. A definition that is no part (a
    title, "—", an AI judgement) is not missing at all: it is in the identity's exclusion audit."""
    decision = next((a for a in content.get("assumptions") or [] if isinstance(a, dict) and a.get("subject") == key
                     and a.get("field") in ("identity", "parent") and a.get("author") == "ai_turbo"), None)
    name, kind = str(d.get("name") or key), str(d.get("kind") or "part")
    if decision is None:
        reason = str((entry or {}).get("reason") or "recognised in the sources but not under the root assembly")
        return missing_item(stage="structure", key=key, name=name, kind=kind, code="definition_unresolved", reason=reason, recovery="fix_spec")
    return missing_item(stage="structure", key=key, name=name, kind=kind, code="definition_unresolved",
                        reason=f"AI 判断 {decision.get('id')}：{decision.get('adopted')}；原因：{decision.get('basis')}", recovery="fix_spec",
                        decision=decision.get("id"))


def coverage(content: dict[str, Any], *, built: set[str] | list[str], placed: dict[str, int] | None, missing: list[dict[str, Any]],
             arranged: dict[str, int] | None = None, assembled: bool = True, decided: dict[str, int] | None = None,
             local_only: dict[str, int] | None = None, subassemblies: dict[str, Any] | None = None,
             estimated: dict[str, int] | None = None) -> dict[str, Any]:
    """The coverage block of one assembly build, frozen on the root revision's ``spec_ref.coverage``.

    The drawing package retains that revision snapshot. A current ``assembly``
    port value and a new delivery may carry a detached diagnostic projection for
    exactly matching modeling-missing items. Only their code, reason, recovery
    and protocol-error flag may change; all other coverage semantics stay frozen.

    ``built``: part definition keys with geometry; ``placed``: definition key → instances installed in the assembly —
    globally (generation reliability §2.6: the instance and every sub-assembly instance above it up to the root have a
    position from the spec, a mate, an AI decision or a rule; :func:`chain_placement` counts it); ``arranged``:
    definition key → instances with only a display position of their own (a layout preview, never installed);
    ``missing``: items from :func:`missing_item`; ``assembled``: an assembly revision exists. Every unplaced definition
    is listed as missing (stage ``structure``), apart from separately audited non-parts and catalog alternatives.

    ``decided`` (generation supplement F, optional): definition key → instances installed where a recorded Turbo AI
    decision placed them (a subset of ``placed``). Only when there is one, the block carries ``decided: {definitions,
    instances}``; a block without the key has none.

    Generation reliability §2.6 (package L): ``local_only``: definition key → instances positioned in their parent
    but under a sub-assembly instance that has no position (never installed); ``subassemblies``: the sub-assembly part
    of :func:`chain_placement`. The block always carries ``local_only: {definitions, instances}`` and
    ``subassembly_placement: {placed, total, unplaced: [instance keys]}``; every kept sub-assembly instance without a
    position is a ``placement`` item (``subassembly_unplaced``) saying how many part instances under it it keeps from
    being installed (``descendants``), unless the caller already listed it. ``complete`` only when nothing is missing
    and every target part instance is installed globally.

    Generation reliability round 2 (package P, WP06): ``estimated``: definition key → installed instances whose
    position — their own or a sub-assembly instance's above them — is an AI estimate (``basis_kind: estimated``; a
    subset of ``placed``, shown apart). The block always carries ``estimated: {definitions, instances}``."""
    from .spec_identity import baseline_of, counts, identity_of

    t = targets(content)
    defs = _defs(content)
    built_set = set(built)
    placed = {k: int(v) for k, v in (placed or {}).items() if int(v) > 0}
    arranged = {k: int(v) for k, v in (arranged or {}).items() if int(v) > 0}
    local = {k: int(v) for k, v in (local_only or {}).items() if int(v) > 0}
    guessed = {k: min(int(v), placed.get(k, 0)) for k, v in (estimated or {}).items() if int(v) > 0 and placed.get(k)}
    # without an assembly plan (nothing built / no assembly made) none is placed; the total still says how many there are
    subs = subassemblies or {"total": chain_placement(content, kept=[], positioned={})["subassemblies"]["total"]}
    items = list(missing)
    listed = {m.get("key") for m in items}
    # generation reliability K (design §2.5): candidates by identity class; no-parts are excluded, never missing
    ident = {str(e.get("key")): e for e in identity_of(content) if isinstance(e, dict)}
    for k in t["unplaced"]:
        if (k not in listed and defs.get(k, {}).get("kind") != "assembly"
                and (ident.get(k) or {}).get("class") not in ("not_a_part", "catalog_alternative")):
            items.append(_unplaced_item(content, k, defs.get(k, {}), ident.get(k)))
    for k in subs.get("unplaced") or []:
        if k not in listed:
            items.append(_subassembly_item(content, k, subs))
    placed_inst = sum(placed.values())
    status: CoverageStatus = "none" if not assembled else ("complete" if not items and placed_inst == t["instances"] else "partial")
    by_ai = {k: min(int(v), placed.get(k, 0)) for k, v in (decided or {}).items() if int(v) > 0 and placed.get(k)}
    extra: dict[str, Any] = {"decided": {"definitions": len(by_ai), "instances": sum(by_ai.values())}} if by_ai else {}
    return {
        "status": status,
        "target": {k: t[k] for k in ("basis", "definitions", "instances", "subassemblies", "subassembly_instances", "recognized_definitions")},
        "built": {"definitions": sum(1 for k in t["per_definition"] if k in built_set)},
        "placed": {"definitions": len(placed), "instances": placed_inst},
        "arranged": {"definitions": len(arranged), "instances": sum(arranged.values())},
        "local_only": {"definitions": len(local), "instances": sum(local.values())},
        # generation reliability P: installed at an AI-estimated position (a subset of placed, shown apart)
        "estimated": {"definitions": len(guessed), "instances": sum(guessed.values())},
        "subassembly_placement": {"placed": int(subs.get("placed") or 0), "total": int(subs.get("total") or 0),
                                  "unplaced": list(subs.get("unplaced") or [])},
        "missing": items,
        "missing_counts": {s: sum(1 for m in items if m.get("stage") == s) for s in MISSING_STAGES if any(m.get("stage") == s for m in items)},
        # generation reliability K (design §2.5): identity classes and the lineage's frozen target next to the current one
        "identity": counts(list(ident.values()), len(content.get("unbound") or [])),
        'catalog_alternatives': [dict(e) for e in ident.values() if e.get('class') == 'catalog_alternative'],
        "unbound": [{k: u.get(k) for k in ("name", "number", "reason", "quote", "source", "chunk") if u.get(k) is not None}
                    for u in content.get("unbound") or [] if isinstance(u, dict)][:200],
        "baseline": baseline_of(content),
        **extra,
    }


# --- generation reliability §2.6 (package L): placement along the parent chain ------------------------------------------

#: How a kept instance has a position in its parent assembly: stated in the spec, a kept relation, a recorded AI
#: decision, a rule (the only child at its parent's origin). A kept instance with none of them is only arranged for
#: display (a layout preview, never installed).
POSE_SOURCES: tuple[str, ...] = ("stated", "mate", "ai", "rule")
#: Generation reliability round 2 (package P, WP06): what a position rests on — ``stated`` (the sources give it),
#: ``derived`` (worked out from stated dimensions / geometry / relations, or a frame rule) or ``estimated`` (an AI
#: estimate where the sources give no exact value: installed, shown apart, recorded as an AI decision).
BASIS_KINDS: tuple[str, ...] = ("stated", "derived", "estimated")


def descendant_instances(content: dict[str, Any], definition: str, _depth: int = 0) -> int:
    """Part instances under ONE instance of an assembly definition, through every level of the spec tree."""
    defs = _defs(content)
    if _depth > 16:
        return 0
    n = 0
    for i in content.get("instances") or []:
        if not isinstance(i, dict) or i.get("parent") != definition:
            continue
        dk = i.get("definition")
        n += descendant_instances(content, str(dk), _depth + 1) if (defs.get(dk) or {}).get("kind") == "assembly" else 1
    return n


def chain_placement(content: dict[str, Any], *, kept: list[str] | set[str], positioned: dict[str, str],
                    decided: set[str] | frozenset[str] = frozenset(), floating: set[str] | frozenset[str] = frozenset(),
                    estimated: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Global counting of one assembly build (generation reliability design §2.6, brief WP06 / AC13 / AC16). Pure.

    Walks the spec tree from the root over the ``kept`` instances, each path once (a sub-assembly used twice counts its
    parts twice). ``positioned``: instance key → how it has a position in its parent (:data:`POSE_SOURCES`); a kept
    instance missing from it is only arranged for display. Per part definition: ``placed`` — the instance and every
    sub-assembly instance above it up to the root are positioned (installed); ``local_only`` — positioned itself, but
    some sub-assembly instance above it is not; ``arranged`` — no position of its own; ``decided`` — of the placed, those
    with an AI decision (``decided`` instance keys) on their path. ``subassemblies``: ``total`` (sub-assembly instance
    keys under the root in the spec), ``placed`` (kept and positioned up to the root), ``unplaced`` (kept, no position of
    their own, in tree order), ``descendants`` (unplaced key → kept part instances under it) and ``copies`` (unplaced
    key → paths through it).

    Round 2 (package P, WP06): ``floating`` — instance keys positioned only relative to siblings none of which is
    anchored to the parent frame (a floating group): a part counts ``local_only``, a sub-assembly instance is listed
    ``unplaced`` (``subassemblies.floating`` names it) and its insides count local only. ``estimated`` — instance keys
    whose own position is an AI estimate: of the placed, those with one on their path are counted ``estimated``."""
    from .assemblies import spec_tree

    defs = _defs(content)
    tree = spec_tree(content)
    root = tree["root"]
    kept_set = set(kept)
    children: dict[str, list[dict[str, Any]]] = {}
    for i in tree["instances"]:
        if i["key"] in kept_set:
            children.setdefault(i.get("parent") or root, []).append(i)

    def is_asm(dk: Any) -> bool:
        return (defs.get(dk) or {}).get("kind") == "assembly"

    counts: dict[str, dict[str, int]] = {"placed": {}, "local_only": {}, "arranged": {}, "decided": {}, "estimated": {}}
    sub_placed: set[str] = set()
    unplaced: list[str] = []
    below: dict[str, int] = {}
    copies: dict[str, int] = {}

    def bump(bucket: str, dk: str) -> None:
        counts[bucket][dk] = counts[bucket].get(dk, 0) + 1

    def walk(parent: str, chain_ok: bool, ai: bool, guess: bool, depth: int) -> int:
        if depth > 16:
            return 0
        leaves = 0
        for i in children.get(parent, []):
            k, dk = i["key"], str(i["definition"])
            own = k in positioned
            anchored = own and k not in floating  # a floating group's position is relative only (P)
            mine = ai or k in decided  # placed by an AI decision: itself, or a sub-assembly instance above it
            est = guess or k in estimated  # at an AI-estimated position: itself, or a sub-assembly instance above it
            if is_asm(dk):
                if not anchored and k not in unplaced:
                    unplaced.append(k)
                if anchored and chain_ok:
                    sub_placed.add(k)
                if not anchored:
                    copies[k] = copies.get(k, 0) + 1
                n = walk(dk, chain_ok and anchored, mine, est, depth + 1)
                if not anchored:
                    below[k] = below.get(k, 0) + n
                leaves += n
                continue
            leaves += 1
            if not own:
                bump("arranged", dk)
            elif chain_ok and anchored:
                bump("placed", dk)
                if mine:
                    bump("decided", dk)
                if est:
                    bump("estimated", dk)
            else:
                bump("local_only", dk)
        return leaves

    walk(root, True, False, False, 0)
    total = sum(1 for i in tree["instances"] if is_asm(i.get("definition")))
    return {**counts, "subassemblies": {"total": total, "placed": len(sub_placed), "unplaced": unplaced,
                                        "descendants": {k: below.get(k, 0) for k in unplaced}, "copies": {k: copies.get(k, 1) for k in unplaced},
                                        "floating": [k for k in unplaced if k in floating]}}


def _subassembly_item(content: dict[str, Any], key: str, subs: dict[str, Any]) -> dict[str, Any]:
    """The ``placement`` item of a kept sub-assembly instance without a position: what it keeps from being installed."""
    inst = next((i for i in content.get("instances") or [] if isinstance(i, dict) and i.get("key") == key), {})
    d = _defs(content).get(inst.get("definition")) or {}
    n = int((subs.get("descendants") or {}).get(key) or 0)
    parent = inst.get("parent") or content.get("root") or "the root"
    how = ("is positioned only relative to sibling(s) none of which is anchored to the parent frame (a floating group)"
           if key in (subs.get("floating") or []) else "has no position")
    return missing_item(stage="placement", key=key, name=str(inst.get("name") or d.get("name") or key), kind="assembly",
                        code="subassembly_unplaced",
                        reason=(f"sub-assembly instance {key} {how} in {parent}: its {n} part instance(s) are not installed "
                                "(placed only inside it, or arranged for display) until it is placed"),
                        quantity=int((subs.get("copies") or {}).get(key) or 1), instances=[key], recovery="fix_spec",
                        definition=inst.get("definition"), descendants=n)
