"""Preflight declared design poses without moving parts or claiming STEP evidence."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from ..cad import assembly_model as am, mates
from ..cad.datums import frame_datums, interface_datums
from ..cad.sketch import cross3, dot3
from ..errors import AppError


def _relation_diagnostic(relation: mates.SolveMate, owner: str,
                         instances: dict[str, mates.SolveInstance]) -> dict[str, Any]:
    """Explain declared poses using the same endpoint frames and metric as mates._residual.

    This is diagnostic data only: the solver's status/residual still decides acceptance.
    A missing or incompatible datum never receives an inferred replacement or distance.
    """
    diagnostic: dict[str, Any] = {"coordinate_frame": owner, "kind": relation.kind, "params": dict(relation.params)}
    if relation.kind == "pattern_match":
        # Patterns resolve a set of axes, not one datum named by the pattern prefix.
        return {**diagnostic, "a": dict(relation.a or {}), "b": dict(relation.b or {})}
    resolved: dict[str, am.Datum] = {}
    for side, endpoint in (("a", relation.a), ("b", relation.b)):
        ref = endpoint or {}
        instance = instances.get(str(ref.get("instance")))
        name = str(ref.get("datum") or "origin")
        local = (instance.datums or {}).get(name) if instance is not None else None
        values: dict[str, Any] = {"instance": ref.get("instance"), "datum": name}
        if local is None:
            values["missing"] = True
        else:
            datum = resolved[side] = mates.transformed(local, instance.transform)
            values.update(kind=datum.kind, origin=list(datum.origin), direction=list(datum.direction))
        diagnostic[side] = values
    if len(resolved) != 2 or relation.kind not in {"planar", "offset"}:
        return diagnostic
    a, b = resolved["a"], resolved["b"]
    if mates.compatible(relation.kind, a.kind, b.kind):
        return diagnostic
    requested = relation.params.get("distance", relation.params.get("offset", 0.0) if relation.kind == "planar" else None)
    if type(requested) not in (int, float):
        return diagnostic
    delta = tuple(b.origin[i] - a.origin[i] for i in range(3))
    if a.kind in {"plane", "frame"} and b.kind in {"plane", "frame"}:
        same = mates.same_way(a.kind, b.kind, relation.params.get("flip"))
        diagnostic["orientation"] = {"expected": "same_way" if same else "opposed", "dot": dot3(a.direction, b.direction),
            "residual": [a.direction[i] - (1 if same else -1) * b.direction[i] for i in range(3)]}
        metric, formula = "signed_plane_distance", "dot(b.origin - a.origin, a.direction)"
        measured = dot3(delta, a.direction)
    elif a.kind == "plane":
        metric, formula = "signed_plane_distance", "dot(b.origin - a.origin, a.direction)"
        measured = dot3(delta, a.direction)
    elif b.kind == "plane":
        metric, formula = "signed_plane_distance", "dot(a.origin - b.origin, b.direction)"
        measured = -dot3(delta, b.direction)
    elif a.kind == b.kind == "axis":
        metric = "axis_separation"
        formula = "norm(delta - dot(delta, a.direction) * a.direction), delta = b.origin - a.origin"
        along = dot3(delta, a.direction)
        measured = math.sqrt(sum((delta[i] - along * a.direction[i]) ** 2 for i in range(3)))
        diagnostic["orientation"] = {"expected": "parallel", "dot": dot3(a.direction, b.direction),
                                     "residual": list(cross3(a.direction, b.direction))}
    else:
        metric, formula = "Euclidean_distance", "norm(b.origin - a.origin)"
        measured = math.dist(a.origin, b.origin)
    diagnostic["distance"] = {"metric": metric, "formula": formula, "requested_mm": requested,
                              "measured_mm": measured, "residual_mm": measured - requested}
    return diagnostic


def validate_static_plan(content: dict[str, Any]) -> None:
    """Check a structurally validated plan in each assembly definition's local frame.

    Reused subassemblies share the same local equations. Every instance, including the
    assembly's own frame, stays fixed: this checks the declared pose, never repairs it.
    Named interfaces are declarations here; the exported STEP gate still has to prove
    that a part actually contains each referenced physical surface.
    """
    instances = {item["key"]: item for item in content["instances"]}
    interfaces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in content.get("interfaces", []):
        interfaces[item["owner"]].append(item)
    datums = {
        item["key"]: {datum.name: datum for datum in [*frame_datums(), *interface_datums(interfaces[item["key"]])]}
        for item in content["definitions"]
    }
    children: dict[str, list[mates.SolveInstance]] = defaultdict(list)
    for item in instances.values():
        pose = item["placement"]
        children[item["parent"]].append(mates.SolveInstance(
            iid=item["key"],
            transform=am.placement(tuple(pose.get("translate") or (0, 0, 0)),
                                   tuple(pose.get("rotate") or (0, 0, 0))),
            fixed=True, datums=datums[item["definition"]],
        ))
    relations: dict[str, list[mates.SolveMate]] = defaultdict(list)
    for position, relation in enumerate(content.get("relations", [])):
        endpoint = relation["a"]["instance"]
        owner = endpoint[1:] if endpoint.startswith("@") else instances[endpoint]["parent"]
        relations[owner].append(mates.SolveMate(
            mate_id=relation["key"], kind=relation["kind"], a=relation["a"], b=relation["b"],
            params=relation.get("params") or {}, position=position,
        ))

    problems: list[dict[str, Any]] = []
    for owner, required in relations.items():
        frame = mates.SolveInstance(iid=f"@{owner}", transform=am.IDENTITY, fixed=True, datums=datums[owner])
        declared = [frame, *children[owner]]
        result = mates.solve(declared, required, turbo=False)
        by_instance = {instance.iid: instance for instance in declared}
        for relation in required:
            state = result.mates.get(relation.mate_id, {"status": "pending"})
            if not result.converged or state.get("status") not in {"solved", "redundant"}:
                problem = {"key": relation.mate_id, "owner": owner, **state}
                if state.get("status") not in {"solved", "redundant"}:
                    problem["diagnostic"] = _relation_diagnostic(relation, owner, by_instance)
                problems.append(problem)
    if problems:
        # Put actionable failures before collateral solved entries when repair feedback has a size limit.
        problems.sort(key=lambda problem: problem.get("status") in {"solved", "redundant"})
        raise AppError(422, "design_static_conflict", "declared design poses do not satisfy required assembly relations",
                       {"problems": problems})
