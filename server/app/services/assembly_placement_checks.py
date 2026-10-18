"""Bounded, read-only physical feedback for a solved assembly placement candidate.

This is generation feedback, not an acceptance action or a substitute for business
checks. Only already stated/solved poses are read; this module never solves, moves
an instance, writes a revision, or allows an interference. Deadlines/cancellation
are cooperative between kernel operations: an individual OCCT call cannot be
preempted. Incomplete examination is always explicit, even when a clash is found.
"""
from __future__ import annotations

import math
import copy
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from ..cad import assembly_checks as chk
from ..cad import assembly_model as am
from ..domain.hashing import sha256_hex

VERSION = "assembly-placement-geometry/1"
MAX_OCCURRENCES = 2000
MAX_TREE_NODES = 10000
MAX_DEPTH = 10
MAX_CANDIDATE_PAIRS = 4096
MIN_KERNEL_RUNTIME_S = 0.05


def bind_saved_report(db: Any, revision: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Bind candidate measurements only to exactly identical persisted occurrences and STEP bytes.

    A later solve or serialization can change a transform. No tolerance permits reusing physical measurements:
    unmatched candidates remain audit evidence, and the saved revision's check is unknown until re-examined.
    """
    from .assemblies import Tree

    result = copy.deepcopy(report)
    try:
        tree = Tree(db, revision.id)
        expected = {tuple(row["path"]): row for row in report.get("occurrences", [])}
        if not expected or len(expected) != len(report.get("occurrences", [])):
            raise ValueError("candidate_occurrences_incomplete")
        actual: dict[tuple[str, ...], dict[str, Any]] = {}
        nodes = 0

        def walk(rid: str, path: tuple[str, ...], world: am.Matrix4 | None, ancestors: tuple[str, ...]) -> None:
            nonlocal nodes
            if rid in ancestors or len(ancestors) > MAX_DEPTH:
                raise ValueError("saved_tree_invalid")
            parent = tree.revision(rid)
            if parent is None:
                raise ValueError("saved_revision_missing")
            solved = (parent.solve or {}).get("instances") or {}
            for component in tree.children(rid):
                if component.suppressed:
                    continue
                nodes += 1
                if nodes > MAX_TREE_NODES or len(actual) >= MAX_OCCURRENCES:
                    raise ValueError("saved_tree_limit")
                key = component.meta.get("spec_key")
                definition = component.meta.get("definition_spec_key")
                if not key or not definition:
                    raise ValueError("saved_source_identity_missing")
                child_path = (*path, str(key))
                grounded = (solved.get(component.instance_id) or {}).get("grounded") is True
                transform = _matrix(am.compose(world, component.transform)) if world is not None and grounded else None
                if component.kind == "assembly":
                    walk(str(component.child_revision_id), child_path, transform, (*ancestors, rid))
                else:
                    part = tree.part_revision(str(component.model_revision_id))
                    if part is None or not part.step_sha or child_path in actual:
                        raise ValueError("saved_part_identity_missing")
                    actual[child_path] = {"definition": str(definition), "revision_id": str(part.id),
                                          "step_sha256": part.step_sha, "world": transform}

        walk(str(revision.id), (), am.IDENTITY, ())
        if set(actual) != set(expected):
            raise ValueError("saved_occurrences_changed")
        for path, item in actual.items():
            old = expected[path]
            shape = (report.get("shapes") or {}).get(old["definition"]) or {}
            if (item["definition"] != old["definition"] or item["revision_id"] != shape.get("revision_id")
                    or item["step_sha256"] != shape.get("step_sha256")):
                raise ValueError("saved_shape_changed")
            before, after = _matrix(old.get("world")), item["world"]
            before_bits = [[float(v).hex() for v in row] for row in before] if before is not None else None
            after_bits = [[float(v).hex() for v in row] for row in after] if after is not None else None
            if before_bits != after_bits:
                raise ValueError("saved_transform_changed")
        result.update(assembly_revision_id=str(revision.id), binding="exact_saved_occurrences")
        return result
    except Exception as exc:
        return {"check": "assembly.placement_geometry", "version": VERSION, "status": "unknown", "complete": False,
                "fingerprint": None, "fingerprint_complete": False, "assembly_revision_id": str(revision.id),
                "binding": "candidate_mismatch", "incomplete_reasons": [str(exc) if isinstance(exc, ValueError) else type(exc).__name__],
                "candidate_report": result, "acceptance": "not_evaluated"}


def _matrix(value: Any) -> am.Matrix4 | None:
    if value is None:
        return None
    matrix = am.from_list(value)
    if not all(math.isfinite(v) for row in matrix for v in row) or not am.is_rigid(matrix):
        raise ValueError("not a finite rigid matrix")
    return matrix


def _shape_bounds(shape: Any) -> Any:
    """Conservative unrounded bounds of actual material, not revision metrics."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    if shape is None or shape.IsNull() or not TopExp_Explorer(shape, TopAbs_SOLID).More():
        raise ValueError("missing solid material")
    if not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError("invalid solid material")
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid() or box.IsOpen():
        raise ValueError("unbounded material")
    lower, upper = box.CornerMin(), box.CornerMax()
    bounds = (lower.X(), lower.Y(), lower.Z(), upper.X(), upper.Y(), upper.Z())
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError("nonfinite material bounds")
    return (bounds[:3], bounds[3:])


def check_layout(layout: Any, *, deadline: float | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 budget_s: float = chk.INTERFERENCE_BUDGET_S, reserve_s: float = 15.0,
                 shape_loader: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Inspect the current ``_Layout`` without changing it or starting a solve.

    Call after normal placement summarization has populated ``_solved``. A missing
    cached pose stays unknown. ``deadline`` and ``layout.deadline`` are overall
    monotonic deadlines; ``reserve_s`` remains available for final persistence.
    Injected loaders have the ``assemblies.part_shape`` signature (including the
    optional ``deadline``/``cancelled`` keywords) and return the exact revision's
    OCP shape. The fingerprint pins STEP hashes and full precision world matrices.
    It is not reusable when ``fingerprint_complete`` is false.
    """
    from . import assemblies
    from .revisions import KERNEL_LOCK

    started = time.monotonic()
    ends = [float(x) - max(0.0, reserve_s) for x in (deadline, getattr(layout, "deadline", None)) if x is not None]
    overall_end = min(ends) if ends else None
    budget_end = started + min(chk.INTERFERENCE_BUDGET_S, max(0.0, budget_s))
    end = min([budget_end, *ends])
    loader = shape_loader or assemblies.part_shape
    unknown: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    reasons: list[str] = []
    occurrences: list[dict[str, Any]] = []
    shape_records: dict[str, dict[str, Any]] = {}
    leaf_records: dict[tuple[str, ...], dict[str, Any]] = {}
    root = str(layout.root)
    defs = {str(d["key"]): d for d in layout.content.get("definitions", []) if d.get("key")}
    instances = list(layout.tree.get("instances") or [])
    counts = Counter(str(i.get("key")) for i in instances)
    children: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        children.setdefault(str(inst.get("parent") or root), []).append(inst)
    for kids in children.values():
        kids.sort(key=lambda i: str(i.get("key")))

    def note(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    def stop() -> str | None:
        was_cancelled = bool(cancelled and cancelled())
        if was_cancelled or getattr(layout, "stopped", False):
            code = "cancelled" if was_cancelled else "layout_stopped"
        elif overall_end is not None and time.monotonic() >= overall_end:
            code = "deadline_exceeded"
        elif time.monotonic() >= budget_end:
            code = "budget_exceeded"
        else:
            return None
        note(code)
        return code

    def acquire() -> bool:
        while not stop():
            remaining = end - time.monotonic()
            if remaining < MIN_KERNEL_RUNTIME_S:
                note("insufficient_runtime")
                return False
            if KERNEL_LOCK.acquire(timeout=min(0.05, remaining)):
                stopped = stop()
                too_late = end - time.monotonic() < MIN_KERNEL_RUNTIME_S
                if stopped or too_late:
                    if too_late and not stopped:
                        note("insufficient_runtime")
                    KERNEL_LOCK.release()
                    return False
                return True
        return False

    expanded = 0
    tree_complete = True

    def walk(dk: str, path: tuple[str, ...], names: tuple[str, ...], chain: tuple[str, ...], world: am.Matrix4 | None) -> None:
        nonlocal expanded, tree_complete
        if stop():
            tree_complete = False
            return
        if dk in chain or len(chain) > MAX_DEPTH:
            code = "assembly_cycle" if dk in chain else "depth_limit"
            rejected.append({"code": code, "path": list(path), "definition": dk})
            note(code)
            tree_complete = False
            return
        if not children.get(dk):
            unknown.append({"code": "empty_assembly", "path": list(path), "definition": dk})
            return
        for inst in children[dk]:
            if stop() or expanded >= MAX_TREE_NODES or len(occurrences) >= MAX_OCCURRENCES:
                tree_complete = False
                if expanded >= MAX_TREE_NODES or len(occurrences) >= MAX_OCCURRENCES:
                    note("occurrence_limit")
                return
            expanded += 1
            key, child = str(inst.get("key") or ""), str(inst.get("definition") or "")
            child_path = (*path, key)
            child_names = (*names, str(inst.get("name") or key))
            if not key or counts[key] != 1 or child not in defs:
                code = "duplicate_instance_key" if key and counts[key] > 1 else "unknown_definition"
                rejected.append({"code": code, "path": list(child_path), "definition": child})
                tree_complete = False
                continue
            pose = None
            if key not in layout.insts:
                unknown.append({"code": "instance_not_kept", "path": list(child_path)})
            else:
                try:
                    pose = layout.stated(key)
                    if pose is None:
                        solved = getattr(layout, "_solved", {}).get(dk)
                        pose = solved[0].get(key) if solved else None
                    pose = _matrix(pose)
                    if pose is None:
                        unknown.append({"code": "unknown_pose", "path": list(child_path)})
                except (ValueError, TypeError, IndexError, KeyError, OverflowError):
                    pose = None
                    rejected.append({"code": "invalid_pose", "path": list(child_path)})
            try:
                composed = _matrix(am.compose(world, pose)) if world is not None and pose is not None else None
            except (ValueError, OverflowError):
                composed = None
                rejected.append({"code": "invalid_world_pose", "path": list(child_path)})
            if defs[child].get("kind") == "assembly":
                walk(child, child_path, child_names, (*chain, dk), composed)
            else:
                record = {"path": list(child_path), "names": list(child_names), "definition": child,
                          "assembly_chain": [*chain, dk], "world": am.to_list(composed) if composed is not None else None}
                occurrences.append(record)
                leaf_records[child_path] = record

    walk(root, (), (), (), am.IDENTITY)
    shapes: dict[str, Any] = {}
    bboxes: dict[str, Any] = {}
    for dk in sorted({r["definition"] for r in occurrences}):
        if stop():
            break
        try:
            rev = layout.rev(dk)
            sha = getattr(rev, "step_sha", None)
            if rev is None or not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                unknown.append({"code": "missing_shape_identity", "definition": dk})
                continue
            shape_records[dk] = {"revision_id": str(rev.id), "step_sha256": sha}
            if not any(r["definition"] == dk and r["world"] is not None for r in occurrences):
                continue
            if end - time.monotonic() < MIN_KERNEL_RUNTIME_S:
                note("insufficient_runtime")
                break
            shape = loader(layout.db, rev, deadline=end, cancelled=lambda: bool(stop()))
            if shape is None:
                unknown.append({"code": "missing_shape", "definition": dk})
                continue
            if not acquire():
                break
            try:
                box = _shape_bounds(shape)
            finally:
                KERNEL_LOCK.release()
            shapes[dk], bboxes[dk] = shape, box
        except Exception as exc:  # a failed actual shape cannot become a passing pair
            unknown.append({"code": "shape_unavailable", "definition": dk, "error_type": type(exc).__name__,
                            "reason": getattr(exc, "code", "shape_read_failed")})
        stop()

    leaves = [chk.CheckLeaf(tuple(r["path"]), tuple(r["names"]), r["definition"], _matrix(r["world"]))
              for r in occurrences if r["world"] is not None and r["definition"] in shapes]
    boxes = [chk.world_bbox(bboxes[leaf.part_key], leaf.world) for leaf in leaves]
    candidates: list[tuple[int, int]] = []
    candidate_scan_complete = not bool(stop())
    # Sweep along X, then test Y/Z. Pair count and storage are bounded even for
    # hundreds of coincident duplicate instances; a truncated sweep stays unknown.
    order = sorted(range(len(leaves)), key=lambda i: (float(boxes[i][0][0]), leaves[i].path))
    for offset, i in enumerate(order):
        if not candidate_scan_complete or stop():
            candidate_scan_complete = False
            break
        for j in order[offset + 1:]:
            if boxes[j][0][0] - boxes[i][1][0] > chk.CONTACT_TOL_MM:
                break
            if stop():
                candidate_scan_complete = False
                break
            if max(float(max(boxes[j][0][axis] - boxes[i][1][axis], boxes[i][0][axis] - boxes[j][1][axis]))
                   for axis in (1, 2)) > chk.CONTACT_TOL_MM:
                continue
            if len(candidates) >= MAX_CANDIDATE_PAIRS:
                note("candidate_limit")
                candidate_scan_complete = False
                break
            candidates.append((i, j))

    interferences: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    pairs_checked = interference_count = contact_count = 0
    targets: dict[str, set[str]] = {}

    def decorate(pair: dict[str, Any]) -> dict[str, Any]:
        a, b = pair["a"]["path"], pair["b"]["path"]
        common = 0
        while common < min(len(a), len(b)) and a[common] == b[common]:
            common += 1
        parent = leaf_records[tuple(a)]["assembly_chain"][common]
        siblings = sorted((a[common], b[common]))
        return {**pair, "repair_scope": {"parent_definition": parent, "parent_path": a[:common], "instances": siblings}}

    for i, j in candidates:
        if not acquire():
            break
        try:
            # Per-pair calls preserve the existing exact checker and allow a
            # cancellation/deadline check between every OCCT pair operation.
            found = chk.interference([leaves[i], leaves[j]], shapes, bboxes,
                                     budget_s=max(0.0, end - time.monotonic()))["interference"]
        except Exception as exc:
            errors.append({"a": {"path": list(leaves[i].path)}, "b": {"path": list(leaves[j].path)},
                           "error_type": type(exc).__name__})
            continue
        finally:
            KERNEL_LOCK.release()
        pairs_checked += found["pairs_checked"]
        interference_count += found["interference_count"]
        contact_count += found["contact_count"]
        for pair in found["interferences"]:
            record = decorate(pair)
            scope = record["repair_scope"]
            targets.setdefault(scope["parent_definition"], set()).update(scope["instances"])
            if len(interferences) < chk.MAX_REPORTED:
                interferences.append(record)
        contacts.extend(decorate(pair) for pair in found["contacts"][:max(0, chk.MAX_REPORTED - len(contacts))])
        errors.extend(found["errors"])
        if found["budget_exceeded"]:
            note("budget_exceeded")
        stop()

    if len(leaves) != len(occurrences):
        note("unexamined_leaves")
    if pairs_checked != len(candidates):
        note("unexamined_pairs")
    if errors:
        note("kernel_errors")
    fingerprint_rows = [{"path": r["path"], "definition": r["definition"], "assembly_chain": r["assembly_chain"],
                         "world_hex": [[float(v).hex() for v in row] for row in r["world"]] if r["world"] is not None else None}
                        for r in sorted(occurrences, key=lambda r: r["path"])]
    complete = bool(occurrences) and tree_complete and candidate_scan_complete and not (unknown or rejected or reasons)
    return {"check": "assembly.placement_geometry", "version": VERSION,
            "status": "failed" if interference_count else ("passed" if complete else "unknown"), "complete": complete,
            "fingerprint": sha256_hex({"version": VERSION, "root": root, "shapes": shape_records, "occurrences": fingerprint_rows}),
            "fingerprint_complete": complete,
            "leaf_count": len(occurrences), "leaf_count_complete": tree_complete,
            "positioned_leaf_count": sum(r["world"] is not None for r in occurrences), "examined_leaf_count": len(leaves),
            "candidate_pairs": len(candidates), "candidate_scan_complete": candidate_scan_complete, "pairs_checked": pairs_checked,
            "interferences": interferences, "interference_count": interference_count, "contacts": contacts, "contact_count": contact_count,
            "findings_truncated": interference_count > len(interferences) or contact_count > len(contacts),
            "repair_targets": {k: sorted(v) for k, v in sorted(targets.items())}, "errors": errors[:50], "error_count": len(errors),
            "unknown": unknown, "rejected": rejected, "incomplete_reasons": reasons, "shapes": shape_records, "occurrences": occurrences,
            "budget_s": min(chk.INTERFERENCE_BUDGET_S, max(0.0, budget_s)), "elapsed_s": round(time.monotonic() - started, 3),
            "tolerances": {"volume_mm3": chk.VOLUME_TOL_MM3, "contact_mm": chk.CONTACT_TOL_MM},
            "runtime_boundary": "cooperative_between_kernel_operations", "acceptance": "not_evaluated"}
