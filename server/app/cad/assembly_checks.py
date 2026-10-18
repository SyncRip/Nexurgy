"""Standard-mode engineering checks of one resolved assembly revision (W4).

Every business check runs through :func:`app.policy.run_check` with its stable name
(``assembly.interference``, ``assembly.clearance``, ``assembly.mate_residual``, ``assembly.bom``,
``assembly.spec``), so Turbo records ``skipped_turbo`` and never calls the checker. Structural validity
(definitions exist and built, rigid transforms, no cycles, budget) is NOT a business check: it always runs.

Interference: world bounding boxes prefilter the leaf pairs, then an exact boolean common gives the shared
volume of each candidate pair. A positive volume is an interference (the exact pair, both paths and the
volume are reported); zero volume at zero distance is a contact; a positive distance below the requested
minimum clearance is a clearance violation. Running out of the time budget makes the result ``unknown``
(``budget_exceeded``), never ``passed``.

Pure: no database (backend directory rule).
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import assembly_model as am

INTERFERENCE_BUDGET_S = 120.0
VOLUME_TOL_MM3 = 1e-3  # common volume below this is numerical noise of touching faces
CONTACT_TOL_MM = 1e-4
MAX_REPORTED = 200


@dataclass
class CheckLeaf:
    path: tuple[str, ...]
    names: tuple[str, ...]
    part_key: str
    world: am.Matrix4


def _status(items_failed: bool, unknown: bool = False) -> str:
    return "unknown" if unknown else ("failed" if items_failed else "passed")


def world_bbox(bbox: tuple[Any, Any], m: am.Matrix4) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = np.asarray(bbox[0], float), np.asarray(bbox[1], float)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    r = np.array([row[:3] for row in m[:3]], float)
    t = np.array([m[0][3], m[1][3], m[2][3]], float)
    w = corners @ r.T + t
    return w.min(axis=0), w.max(axis=0)


def _pair_key(a: CheckLeaf, b: CheckLeaf) -> tuple[str, str]:
    pa, pb = "/".join(a.path), "/".join(b.path)
    return (pa, pb) if pa <= pb else (pb, pa)


def _allowed(a: CheckLeaf, b: CheckLeaf, allow: list[list[str]]) -> bool:
    """``allow`` pairs name instance ids (any element of a path) or part keys on each side."""
    for pair in allow:
        if len(pair) != 2:
            continue
        x, y = pair
        ka = set(a.path) | {a.part_key}
        kb = set(b.path) | {b.part_key}
        if (x in ka and y in kb) or (x in kb and y in ka):
            return True
    return False


def interference(leaves: list[CheckLeaf], shapes: dict[str, Any], bboxes: dict[str, Any], *, clearance_mm: float | None = None,
                 allow: list[list[str]] | None = None, budget_s: float = INTERFERENCE_BUDGET_S) -> dict[str, Any]:
    """Pairwise interference / contact / clearance of placed leaves (see module doc).

    ``shapes`` and ``bboxes`` are keyed by part key (definition frame). Returns the report with separate
    ``interference`` and ``clearance`` statuses."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from .assembly_step import located

    t0 = time.monotonic()
    allow = allow or []
    margin = max(CONTACT_TOL_MM, float(clearance_mm or 0.0))
    boxes = [world_bbox(bboxes[leaf.part_key], leaf.world) if bboxes.get(leaf.part_key) else None for leaf in leaves]
    missing = sorted({leaf.part_key for leaf in leaves if leaf.part_key not in shapes})
    candidates: list[tuple[int, int]] = []
    valid = [i for i, b in enumerate(boxes) if b is not None and leaves[i].part_key in shapes]
    if valid:
        lo = np.array([boxes[i][0] for i in valid])  # type: ignore[index]
        hi = np.array([boxes[i][1] for i in valid])  # type: ignore[index]
        for k, i in enumerate(valid[:-1]):
            sep = np.maximum(lo[k + 1:] - hi[k], lo[k] - hi[k + 1:]).max(axis=1)
            for off in np.nonzero(sep <= margin)[0]:
                candidates.append((i, valid[k + 1 + int(off)]))
    clashes: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    tight: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    checked = 0
    budget_exceeded = False
    moved: dict[int, Any] = {}

    def shape(i: int) -> Any:
        if i not in moved:
            moved[i] = located(shapes[leaves[i].part_key], leaves[i].world)
        return moved[i]

    for i, j in candidates:
        if time.monotonic() - t0 > budget_s:
            budget_exceeded = True
            break
        a, b = leaves[i], leaves[j]
        entry = {"a": {"path": list(a.path), "names": list(a.names), "part_key": a.part_key},
                 "b": {"path": list(b.path), "names": list(b.names), "part_key": b.part_key}}
        try:
            common = BRepAlgoAPI_Common(shape(i), shape(j))
            vol = 0.0
            if common.IsDone():
                props = GProp_GProps()
                BRepGProp.VolumeProperties_s(common.Shape(), props)
                vol = abs(float(props.Mass()))
            else:
                raise RuntimeError("boolean common failed")
            checked += 1
            if vol > VOLUME_TOL_MM3:
                rec = {**entry, "volume_mm3": round(vol, 4)}
                (allowed if _allowed(a, b, allow) else clashes).append(rec)
                continue
            dist = BRepExtrema_DistShapeShape(shape(i), shape(j))
            d = float(dist.Value()) if dist.IsDone() else None
            if d is None:
                raise RuntimeError("distance computation failed")
            if d <= CONTACT_TOL_MM:
                contacts.append({**entry, "distance_mm": round(d, 6)})
            elif clearance_mm is not None and d < clearance_mm:
                tight.append({**entry, "distance_mm": round(d, 6), "required_mm": clearance_mm})
        except Exception as exc:  # an OCCT failure on one pair makes the verdict unknown, never passed
            errors.append({**entry, "error": f"{type(exc).__name__}: {exc}"})
    elapsed = round(time.monotonic() - t0, 3)
    unknown = budget_exceeded or bool(errors) or bool(missing)
    report = {
        "check": "assembly.interference",
        "status": _status(bool(clashes), unknown and not clashes),
        "leaves": len(leaves),
        "candidate_pairs": len(candidates),
        "pairs_checked": checked,
        "interferences": clashes[:MAX_REPORTED],
        "interference_count": len(clashes),
        "allowed_interferences": allowed[:MAX_REPORTED],
        "contacts": contacts[:MAX_REPORTED],
        "contact_count": len(contacts),
        "errors": errors[:50],
        "missing_shapes": missing,
        "budget_s": budget_s,
        "elapsed_s": elapsed,
        "budget_exceeded": budget_exceeded,
        "tolerances": {"volume_mm3": VOLUME_TOL_MM3, "contact_mm": CONTACT_TOL_MM},
    }
    if clashes and unknown:
        report["status"] = "failed"  # a found clash is a real failure even when other pairs are unknown
    clearance = None
    if clearance_mm is not None:
        clearance = {
            "check": "assembly.clearance",
            "status": _status(bool(tight), unknown and not tight),
            "required_mm": clearance_mm,
            "violations": tight[:MAX_REPORTED],
            "violation_count": len(tight),
            "budget_exceeded": budget_exceeded,
        }
    return {"interference": report, "clearance": clearance}


def mate_check(solve: dict[str, Any]) -> dict[str, Any]:
    """Mate residual / solve check from a stored solve result."""
    mates = solve.get("mates") or {}
    bad = {k: v for k, v in mates.items() if v.get("status") in ("conflict", "broken", "invalid", "dropped")}
    under = {k: v for k, v in (solve.get("instances") or {}).items() if v.get("status") in ("under_constrained", "floating")}
    unplaced = {k: v for k, v in (solve.get("instances") or {}).items() if v.get("status") == "unplaced"}
    failed = bool(bad) or bool(unplaced) or solve.get("converged") is False
    return {
        "check": "assembly.mate_residual",
        "status": "failed" if failed else "passed",
        "mates": len(mates),
        "problems": [{"mate_id": k, **v} for k, v in bad.items()],
        "unplaced": sorted(unplaced),
        "under_constrained": [{"instance": k, "dof": v.get("dof"), "status": v.get("status"), "free": v.get("free", [])} for k, v in under.items()],
        "max_residual": solve.get("max_residual") or {},
        "tolerances": {"position_mm": 1e-5, "angle_rad": 1e-6},
    }


def bom_check(bom: dict[str, Any], leaf_paths: list[tuple[str, ...]]) -> dict[str, Any]:
    """BOM / item consistency against the expanded instance tree."""
    problems: list[dict[str, Any]] = []
    flat = bom.get("flattened") or []
    hier = bom.get("hierarchical") or []
    total = sum(int(line["total_quantity"]) for line in flat)
    if total != len(leaf_paths):
        problems.append({"code": "leaf_count_mismatch", "message": f"flattened BOM totals {total}, the tree has {len(leaf_paths)} leaves"})
    covered = Counter(tuple(p) for line in flat for p in line.get("instance_paths") or [])
    if covered != Counter(leaf_paths):
        problems.append({"code": "instance_coverage", "message": "flattened BOM lines do not cover every placed leaf exactly once"})
    for line in flat:
        if int(line["quantity"]) != len(line.get("instance_paths") or []):
            problems.append({"code": "quantity_mismatch", "item": line["item"], "message": "quantity differs from its instance paths"})
    items = [line["item"] for line in flat]
    if len(set(items)) != len(items):
        problems.append({"code": "duplicate_item", "message": "flattened item numbers repeat"})
    if bom.get("item_basis") == "spec_target":  # a partial spec build keeps the target's numbers (gaps = missing items)
        if not all(i.isdigit() for i in items) or [int(i) for i in items] != sorted({int(i) for i in items}):
            problems.append({"code": "item_sequence", "message": "flattened item numbers are not increasing target numbers"})
    elif items != [str(i + 1) for i in range(len(items))]:
        problems.append({"code": "item_sequence", "message": "flattened item numbers are not 1..n"})
    hitems = [line["item"] for line in hier]
    if len(set(hitems)) != len(hitems):
        problems.append({"code": "duplicate_item", "message": "hierarchical item numbers repeat"})
    keys = [line["def_key"] for line in flat]
    if len(set(keys)) != len(keys):
        problems.append({"code": "duplicate_definition", "message": "a part definition has more than one flattened line"})
    return {"check": "assembly.bom", "status": "failed" if problems else "passed", "problems": problems,
            "flattened_lines": len(flat), "hierarchical_lines": len(hier), "leaf_instances": len(leaf_paths)}


def spec_check(spec: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Missing / extra / quantity against an engineering spec (W0 §5 content).

    ``actual`` = {"instances": [{"instance_id", "spec_key", "definition_spec_key", "parent_spec_key"}],
    "definitions": {spec definition key: definition key}}. Instances are matched by their spec key;
    quantities per (parent, definition) are compared as well."""
    problems: list[dict[str, Any]] = []
    want = {i["key"]: i for i in spec.get("instances") or [] if i.get("key")}
    have = {i["spec_key"]: i for i in actual.get("instances") or [] if i.get("spec_key")}
    for key, inst in want.items():
        if key not in have:
            problems.append({"code": "missing_instance", "subject": key, "definition": inst.get("definition"),
                             "message": f"spec instance {key} ({inst.get('definition')}) is not in the assembly"})
    extra = [i for i in actual.get("instances") or [] if not i.get("spec_key") or i["spec_key"] not in want]
    for inst in extra:
        problems.append({"code": "extra_instance", "subject": inst.get("instance_id"),
                         "message": f"instance {inst.get('instance_id')} is not in the spec"})
    for key, inst in want.items():
        got = have.get(key)
        if got is not None and inst.get("definition") and got.get("definition_spec_key") not in (None, inst.get("definition")):
            problems.append({"code": "wrong_definition", "subject": key,
                             "message": f"{key} uses {got.get('definition_spec_key')}, the spec says {inst.get('definition')}"})
    want_q = Counter((i.get("parent") or spec.get("root"), i.get("definition")) for i in want.values())  # null parent = root
    have_q = Counter((i.get("parent_spec_key"), i.get("definition_spec_key")) for i in have.values() if i.get("spec_key") in want)
    for (parent, definition), n in want_q.items():
        if have_q.get((parent, definition), 0) != n:
            problems.append({"code": "quantity_mismatch", "subject": f"{parent}/{definition}", "expected": n,
                             "actual": have_q.get((parent, definition), 0),
                             "message": f"{definition} under {parent}: expected {n}, found {have_q.get((parent, definition), 0)}"})
    defs = {d["key"] for d in spec.get("definitions") or [] if d.get("key")}
    unbound = sorted(defs - set((actual.get("definitions") or {}).keys()) - {spec.get("root")})
    for key in unbound:
        problems.append({"code": "unbound_definition", "subject": key, "message": f"spec definition {key} has no built definition"})
    return {"check": "assembly.spec", "status": "failed" if problems else "passed", "problems": problems,
            "expected_instances": len(want), "matched_instances": len(set(want) & set(have))}


def aggregate(records: dict[str, dict[str, Any] | None], required: list[str]) -> str:
    """One check_status over the required business checks: failed > unknown > pending > passed."""
    statuses = []
    for name in required:
        rec = records.get(name)
        statuses.append(rec.get("status") if rec else "pending")
    if not statuses:
        return "not_requested"
    if all(s == "skipped_turbo" for s in statuses):
        return "skipped_turbo"
    if "failed" in statuses:
        return "failed"
    if "unknown" in statuses:
        return "unknown"
    if any(s not in ("passed",) for s in statuses):
        return "pending"
    return "passed"
