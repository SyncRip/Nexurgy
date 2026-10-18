"""Typed geometry completeness for new plans, before building or certifying any STEP."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from ..cad.conformance_w3 import contour_loops
from ..cad.datums import interface_datums
from ..cad.requirements import BandReq, CutReq, HoleReq, PocketReq, PrismReq, RevolveReq, SweepReq
from ..cad.sketch import Frame, cross3, resolve_path, unit3
from ..errors import AppError

if TYPE_CHECKING:
    from ..ai.design_contracts import DesignPlanContent

POSITIVE_BODY_KINDS = {"band", "prism", "revolve", "sweep", "coil"}
Axis = tuple[Sequence[float], Sequence[float]]


def _arc_centres(contour: Any) -> Iterable[tuple[float, float]]:
    outer, holes = contour_loops(contour)
    for loop in [outer, *holes]:
        for segment in loop:
            if segment.kind == "arc" and segment.center is not None:
                yield segment.center


def _requirement_axes(requirements: Sequence[Any]) -> Iterable[Axis]:
    """Only surfaces explicitly described by typed dimensions; no inferred CAD model."""
    for requirement in requirements:
        if isinstance(requirement, HoleReq):
            for point in requirement.entries:
                yield point, unit3(requirement.direction)
        elif isinstance(requirement, (PrismReq, BandReq, CutReq, PocketReq)):
            if isinstance(requirement, BandReq):
                frame = Frame.make((0, 0, requirement.z0), (0, 0, 1))
                contours = [requirement.outer, *requirement.voids]
            elif isinstance(requirement, PocketReq):
                frame = Frame.make((0, 0, requirement.entry_z), (0, 0, 1))
                contours = [requirement.contour]
            else:
                frame = requirement.plane.frame()
                contours = [requirement.outline, *(requirement.voids if isinstance(requirement, PrismReq) else [])]
            for contour in contours:
                for centre in _arc_centres(contour):
                    yield frame.point(*centre), frame.z
        elif isinstance(requirement, RevolveReq):
            outer, holes = requirement.section.loops()
            # A horizontal positive-radius edge revolves to a cylinder. A cone or torus alone does not.
            if any(segment.kind == "line" and segment.p0[1] > 1e-7
                   and abs(segment.p1[1] - segment.p0[1]) <= 1e-7
                   for loop in [outer, *holes] for segment in loop):
                yield requirement.axis_origin, unit3(requirement.axis)
        elif isinstance(requirement, SweepReq):
            frame = requirement.plane.frame()
            centres = list(_arc_centres(requirement.section))
            for segment in resolve_path(requirement.path.start, requirement.path.segments):
                if segment.kind != "line":
                    continue
                tx, ty = segment.tangent(0)
                for u, v in centres:
                    yield frame.point(segment.p0[0] - ty * u, segment.p0[1] + tx * u, v), frame.vector(tx, ty)


def _same_axis(a: Axis, b: Axis) -> bool:
    displacement = tuple(a[0][i] - b[0][i] for i in range(3))
    return (math.dist(cross3(a[1], b[1]), (0, 0, 0)) <= 1e-7
            and math.dist(cross3(displacement, b[1]), (0, 0, 0)) <= 1e-5)


def validate_design_geometry(plan: DesignPlanContent) -> None:
    """Reject missing body/axis contracts on new designs; actual STEP remains authoritative."""
    for owner, requirements in plan.part_requirements.items():
        if not any(requirement.kind in POSITIVE_BODY_KINDS for requirement in requirements):
            raise AppError(422, "design_body_missing", "each design part needs an explicit positive body requirement",
                           {"definition": owner, "supported_kinds": sorted(POSITIVE_BODY_KINDS)})
        interfaces = [interface for interface in plan.interfaces if interface.owner == owner
                      and interface.kind in {"axis", "pattern"}]
        if not interfaces:
            continue
        candidates = list(_requirement_axes(requirements))
        for interface in interfaces:
            datums = interface_datums([interface.model_dump(mode="json")])
            if not datums or any(not any(_same_axis((datum.origin, datum.direction), candidate) for candidate in candidates)
                                 for datum in datums):
                raise AppError(422, "design_axis_unsupported", "design axis interface needs a matching cylindrical surface or hole requirement",
                               {"definition": owner, "interface": interface.key})
