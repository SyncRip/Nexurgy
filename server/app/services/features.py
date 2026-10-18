"""Build the manufacturing FeatureSummary from a stored model revision."""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..domain.schemas import FeatureSummary, HoleFeature
from ..models import ModelRevision
from ..storage import sha256_hex

if TYPE_CHECKING:
    from ..cad.conformance import FeatureSurvey


def _axis_key(axis: list[float] | tuple[float, ...] | None) -> tuple[int, int, int]:
    if not axis:
        return (0, 0, -1)
    n = math.sqrt(sum(a * a for a in axis)) or 1.0
    # A hole drilled from +Z or -Z needs a different setup; round to 0.1 to merge noise.
    return tuple(int(round(a / n * 10)) for a in axis)  # type: ignore[return-value]


def _extent_along(size: list[float], axis: list[float] | None) -> float:
    if not axis:
        return float(size[2])
    n = math.sqrt(sum(a * a for a in axis)) or 1.0
    return round(sum(abs(a) / n * s for a, s in zip(axis, size)), 4)


def _depth_upper_bound(hole: dict[str, Any], bbox: dict[str, Any], size: list[float]) -> float | None:
    axis = hole.get("axis") or [0.0, 0.0, -1.0]
    norm = math.sqrt(sum(a * a for a in axis)) or 1.0
    direction = [a / norm for a in axis]
    bound = _extent_along(size, axis)
    lo, hi = bbox.get("min"), bbox.get("max")
    positions = hole.get("positions") or ([hole["entry"]] if hole.get("entry") else [])
    if lo is not None and hi is not None:
        for entry in positions:
            # A legacy op can start in air in front of the part. Its full reach can exceed the box extent.
            bound = max(bound, sum(((hi[i] if d >= 0 else lo[i]) - entry[i]) * d for i, d in enumerate(direction)))
    return round(bound, 4) or None


def _shares_axis(hole: dict[str, Any], holes: list[dict[str, Any]]) -> bool:
    """Another hole operation can shorten the measured wall from either end (e.g. a reverse countersink)."""
    axis = hole.get("axis") or [0.0, 0.0, -1.0]
    norm = math.sqrt(sum(a * a for a in axis))
    if norm <= 0:
        return True
    direction = [a / norm for a in axis]
    positions = hole.get("positions") or ([hole["entry"]] if hole.get("entry") else [])
    for other in holes:
        if other is hole:
            continue
        other_axis = other.get("axis") or [0.0, 0.0, -1.0]
        other_norm = math.sqrt(sum(a * a for a in other_axis))
        if other_norm <= 0:
            continue
        alignment = sum(a * b / other_norm for a, b in zip(direction, other_axis, strict=True))
        if abs(abs(alignment) - 1.0) > 1e-7:
            continue
        other_positions = other.get("positions") or ([other["entry"]] if other.get("entry") else [])
        if not other_positions:
            return True
        for entry in positions:
            for origin in other_positions:
                delta = [a - b for a, b in zip(entry, origin, strict=True)]
                along = sum(a * b for a, b in zip(delta, direction, strict=True))
                if math.sqrt(sum((a - along * d) ** 2 for a, d in zip(delta, direction, strict=True))) <= 1e-4:
                    return True
    return False


def _through_depths(hole: dict[str, Any], survey: FeatureSurvey) -> list[float] | None:
    """Match every explicit occurrence to a full inner STEP cylinder, preserving drill reach across cavities."""
    positions = hole.get("positions") or ([hole["entry"]] if hole.get("count", 1) == 1 and hole.get("entry") else [])
    if len(positions) != hole.get("count", 1):
        return None
    axis = hole.get("axis") or [0.0, 0.0, -1.0]
    norm = math.sqrt(sum(a * a for a in axis))
    if norm <= 0:
        return None
    direction = [a / norm for a in axis]
    tol = 1e-4  # STEP and explicit occurrence coordinates are rounded to six decimal places.
    mouth = float((hole.get("counterbore") or {}).get("depth", 0.0))
    if hole.get("countersink"):
        sink = hole["countersink"]
        mouth = (float(sink["diameter"]) - float(hole["diameter"])) / (2 * math.tan(math.radians(sink.get("angle", 90) / 2)))
    depths: list[float] = []
    for entry in positions:
        spans: list[tuple[float, float]] = []
        for bore in survey.holes:
            if abs(bore.diameter - float(hole["diameter"])) > tol:
                continue
            alignment = sum(a * b for a, b in zip(direction, bore.direction, strict=True))
            if abs(abs(alignment) - 1.0) > 1e-7:
                continue
            delta = [e - o for e, o in zip(entry, bore.point, strict=True)]
            along = sum(a * b for a, b in zip(delta, bore.direction, strict=True))
            if math.sqrt(sum((x - along * d) ** 2 for x, d in zip(delta, bore.direction, strict=True))) > tol:
                continue
            near, far = sorted((alignment * (bore.t0 - along), alignment * (bore.t1 - along)))
            if far > tol and near >= -tol:
                spans.append((near, far))
        # A remote coaxial bore is not evidence of this hole's mouth. Counterbores / sinks shorten the
        # small cylinder at the entrance, but their specified mouth depth still belongs to the drilling reach.
        if not spans or abs(min(a for a, _ in spans) - mouth) > tol:
            return None
        # Do not sum wall thickness or select the nearest exit: a drill must reach the last coaxial wall.
        depths.append(round(max(b for _, b in spans), 6))
    return depths


def summary_from_revision(rev: ModelRevision, material: str, *, step_bytes: bytes | None = None) -> FeatureSummary:
    """Explicit hole features come from hole operations; heuristically detected
    holes on imported geometry are reported as coverage gaps, not features."""
    m = rev.metrics or {}
    size = (m.get("bbox") or {}).get("size") or [0.0, 0.0, 0.0]
    unsupported: list[str] = []
    if rev.mesh_only:
        unsupported.append("mesh_reference_only")
    estimated = bool(rev.mesh_only)
    survey = None
    unresolved_through = any(h.get("through", h.get("depth") is None) and h.get("depth") is None for h in rev.holes or [])
    # Edge dressings can shorten an observed cylinder at its exit. Without a verified exit boundary, its
    # cylinder length alone is not a safe drilling depth; retain the explicitly labelled upper-bound estimate.
    dressed = any(f.get("type") in ("fillet", "chamfer") and f.get("status") == "ok" for f in rev.features or [])
    if unresolved_through and not dressed and step_bytes and sha256_hex(step_bytes) == rev.step_sha:
        from ..cad.conformance import survey_features

        try:
            survey = survey_features(step_bytes, require_complete_bores=True)
        except Exception:  # An optional geometry measurement must not turn an unknown depth into a measured value.
            survey = None
        if survey is not None and survey.error:
            survey = None
    holes: list[HoleFeature] = []
    directions = {(0, 0, -1)}  # the top setup always exists
    for h in rev.holes or []:
        depth = h.get("depth")
        through = bool(h.get("through", depth is None))
        groups = [(depth, int(h.get("count", 1)))]
        if through and depth is None:
            measured = _through_depths(h, survey) if survey is not None and not _shares_axis(h, rev.holes or []) else None
            if measured is not None:
                groups = list(Counter(measured).items())
            else:
                # A global extent is only a conservative estimate, never the measured local wall thickness.
                depth = _depth_upper_bound(h, m.get("bbox") or {}, size)
                groups = [(depth, int(h.get("count", 1)))]
                estimated = True
                if "through_hole_depth_unverified" not in unsupported:
                    unsupported.append("through_hole_depth_unverified")
        holes.extend(
            HoleFeature(
                diameter=float(h["diameter"]),
                depth=depth,
                through=through,
                count=count,
                counterbore=bool(h.get("counterbore")),
                countersink=bool(h.get("countersink")),
            )
            for depth, count in groups
        )
        directions.add(_axis_key(h.get("axis")))
    fillets = sum(1 for f in rev.features or [] if f.get("type") == "fillet" and f.get("status") == "ok")
    chamfers = sum(1 for f in rev.features or [] if f.get("type") == "chamfer" and f.get("status") == "ok")
    if rev.detected_holes:
        # Bores found on imported geometry are guesses; planning must not
        # price them as holes until someone models them explicitly.
        unsupported.append("imported_holes_unverified")
    if rev.base and not rev.ops:
        unsupported.append("imported_features_not_recognised")
    return FeatureSummary(
        source="model",
        estimated=estimated,
        material=material,
        bbox_mm=(float(size[0]), float(size[1]), float(size[2])),
        volume_mm3=m.get("volume_mm3"),
        area_mm2=m.get("area_mm2"),
        holes=holes,
        fillets=fillets,
        chamfers=chamfers,
        setup_directions=len(directions),
        unsupported=unsupported,
    )
