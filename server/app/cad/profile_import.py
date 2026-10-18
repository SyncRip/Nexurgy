"""Strict DXF PROFILE import: one closed straight outline (+ inner cut-outs)
extruded to a plate :class:`~app.cad.ops.ModelSpec` (design.md §5).

Deterministic, no AI, nothing guessed:

- Only model-space entities on ``layer`` (default ``PROFILE``, compared
  case-insensitively like CAD does) are read: closed LWPOLYLINE / 2D POLYLINE
  made of straight segments, and CIRCLE, all in the XY plane. Anything else on
  that layer (LINE, ARC, bulged segments, SPLINE, TEXT, INSERT, …), profile-layer
  entities in paper space or anywhere inside an inserted block tree (nested
  blocks included; a layer-0 block entity takes the layer of its INSERT) →
  refusal; a block tree nested deeper than ``MAX_BLOCK_DEPTH`` levels (or
  cyclically) cannot be verified and is refused too. Entities on other layers /
  in paper space are ignored and listed in the report.
- Units must be millimetres: ``$INSUNITS = 4``, or a unitless file
  (``$INSUNITS`` 0 / missing) with ``units="mm"`` declared by the caller. A file
  that declares another unit is refused even when the caller says mm.
- ``thickness_mm`` and ``scale`` have no defaults (N01/N02). ``scale``
  multiplies drawing coordinates into part millimetres about the DXF origin:
  ``part_xy = drawing_xy × scale`` (a 1:2 drawing needs ``scale=2``). The
  thickness is physical and not scaled. The profile sits on ``z = base_z`` and
  is extruded to ``base_z + thickness_mm``; XY positions are kept (no
  re-centring), so the original origin stays the part origin.
- Exactly one outer contour; every other contour strictly inside it, not
  touching it or each other (clearance > 0.01 mm in part units); no islands
  inside cut-outs; no self-intersections.

Mapping (documented choice): the outer contour becomes ``F-01`` (extrude,
mode ``new``; polygon or circle profile). Inner polygons become extrude
``subtract`` cuts through the plate. Inner circles become through
:class:`~app.cad.ops.HoleOp` cuts drilled from the top face; their labels say
they are geometry cuts taken from a profile circle, not a confirmed
drilled-hole specification (no thread, fit or depth intent is implied).
HoleOp is used so the kernel, drawings and the checker see cylindrical through
holes with their exact diameter and position.

Refusals raise :class:`ProfileRefusal` with one of the codes in
:data:`RefusalCode`; its ``report`` lists every problem found and every
entity used / ignored / rejected with its handle.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .drawing_evidence import MAX_BLOCK_DEPTH, DrawingEvidenceError, block_costs, read_dxf_bytes
from .ops import MAX_DIM, MAX_OPS, MIN_DIM, ModelSpec
from .results import Issue

SCHEMA = "partroute.profile_import/1"
IMPORTER_VERSION = "1"
TOUCH_TOL_MM = 0.01  # minimum clearance between contours (= ops.MIN_DIM)
MAX_POLYGON_POINTS = 256  # ops.PolygonProfile limit
_EPS = 1e-9

RefusalCode = Literal[
    "profile_missing_parameter",  # thickness / scale / units missing or invalid
    "profile_units",  # file not in mm (or unitless without a declaration)
    "profile_unsupported_entity",  # anything but closed straight polylines and circles on the layer
    "profile_open_contour",  # an open polyline on the layer
    "profile_no_outer",  # no closed contour on the layer
    "profile_touching",  # contours cross / touch, or a contour self-intersects
    "profile_multiview",  # more than one outer contour, or an island inside a cut-out
    "profile_unreadable",  # the bytes are not a readable DXF
]
_PRIORITY: list[str] = ["profile_unreadable", "profile_missing_parameter", "profile_units", "profile_unsupported_entity",
                        "profile_open_contour", "profile_no_outer", "profile_touching", "profile_multiview"]
_MM_NAMES = {"mm", "millimeter", "millimeters", "millimetre", "millimetres"}

P2 = tuple[float, float]


class ProfileEntity(BaseModel):
    id: str
    type: str
    layer: str
    space: str = "model"  # "model" or the paper-space layout name
    role: Literal["outer", "inner_polygon", "inner_circle"] | None = None
    op_id: str | None = None
    reason: str | None = None  # why it was ignored or rejected
    vertices: list[P2] | None = None  # drawing units, as read
    center: P2 | None = None
    radius: float | None = None


class ProfileProblem(BaseModel):
    code: str
    message: str
    ids: list[str] = Field(default_factory=list)


class ProfileUnits(BaseModel):
    insunits: int | None = None
    declared: str | None = None
    used: Literal["mm"] | None = None
    source: Literal["header", "declared"] | None = None


class ProfileExpected(BaseModel):
    """Plain arithmetic on the accepted contours (part mm), for checks."""

    area_mm2: float
    volume_mm3: float
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    inner_count: int


class ProfileReport(BaseModel):
    schema_version: str = SCHEMA
    importer_version: str = IMPORTER_VERSION
    status: Literal["accepted", "refused"]
    refusal: ProfileProblem | None = None
    problems: list[ProfileProblem] = Field(default_factory=list)
    source_sha256: str
    source_bytes: int
    layer: str
    units: ProfileUnits = Field(default_factory=ProfileUnits)
    scale: float | None = None
    scale_meaning: str = "part_mm = drawing_units × scale (about the DXF origin); thickness is not scaled"
    thickness_mm: float | None = None
    base_z: float | None = None
    used: list[ProfileEntity] = Field(default_factory=list)
    rejected: list[ProfileEntity] = Field(default_factory=list)
    ignored: list[ProfileEntity] = Field(default_factory=list)
    expected: ProfileExpected | None = None
    warnings: list[Issue] = Field(default_factory=list)


class ProfileImport(BaseModel):
    spec: ModelSpec
    report: ProfileReport


class ProfileRefusal(ValueError):
    """Typed refusal; ``code`` is a :data:`RefusalCode`, ``report`` the full
    report (status ``refused``) with every problem found."""

    def __init__(self, code: str, message: str, report: ProfileReport):
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = report

    def to_issue(self) -> Issue:
        return Issue(code=self.code, message=self.message,
                     details={"problems": [p.model_dump(mode="json") for p in self.report.problems]})


# --------------------------------------------------------------------------- #
# Planar geometry (part mm)
# --------------------------------------------------------------------------- #


class _Contour:
    def __init__(self, ent: ProfileEntity, *, points: list[P2] | None = None, center: P2 | None = None,
                 radius: float | None = None):
        self.ent = ent
        self.points = points  # polygon, CCW
        self.center = center
        self.radius = radius

    @property
    def is_circle(self) -> bool:
        return self.points is None

    @property
    def area(self) -> float:
        if self.is_circle:
            assert self.radius is not None
            return math.pi * self.radius * self.radius
        return abs(_signed_area(self.points or []))

    def bbox(self) -> tuple[float, float, float, float]:
        if self.is_circle:
            assert self.center is not None and self.radius is not None
            c, r = self.center, self.radius
            return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        xs = [p[0] for p in self.points or []]
        ys = [p[1] for p in self.points or []]
        return (min(xs), min(ys), max(xs), max(ys))

    def centroid_key(self) -> tuple[float, float]:
        b = self.bbox()
        return (round((b[0] + b[2]) / 2, 6), round((b[1] + b[3]) / 2, 6))

    def segments(self) -> list[tuple[P2, P2]]:
        pts = self.points or []
        return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _signed_area(pts: list[P2]) -> float:
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _point_seg_dist(p: P2, a: P2, b: P2) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 <= _EPS * _EPS:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _orient(a: P2, b: P2, c: P2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(a: P2, b: P2, c: P2, d: P2) -> bool:
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and all(abs(o) > 0 for o in (o1, o2, o3, o4))


def _seg_seg_dist(a: P2, b: P2, c: P2, d: P2) -> float:
    if _segments_cross(a, b, c, d):
        return 0.0
    return min(_point_seg_dist(a, c, d), _point_seg_dist(b, c, d), _point_seg_dist(c, a, b), _point_seg_dist(d, a, b))


def _inside_polygon(p: P2, pts: list[P2]) -> bool:
    x, y = p
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                inside = not inside
    return inside


def _self_touch(c: _Contour, tol: float) -> bool:
    segs = c.segments()
    n = len(segs)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbours share a vertex
            if _seg_seg_dist(*segs[i], *segs[j]) <= tol:
                return True
    return False


def _relation(a: _Contour, b: _Contour, tol: float) -> str:
    """'a_contains_b', 'b_contains_a', 'disjoint' or 'touching'."""
    if a.is_circle and b.is_circle:
        assert a.center and b.center and a.radius is not None and b.radius is not None
        d = math.dist(a.center, b.center)
        if d + b.radius < a.radius - tol:
            return "a_contains_b"
        if d + a.radius < b.radius - tol:
            return "b_contains_a"
        if d > a.radius + b.radius + tol:
            return "disjoint"
        return "touching"
    if a.is_circle != b.is_circle:
        circ, poly = (a, b) if a.is_circle else (b, a)
        assert circ.center and circ.radius is not None and poly.points
        dmin = min(_point_seg_dist(circ.center, s0, s1) for s0, s1 in poly.segments())
        vmax = max(math.dist(circ.center, p) for p in poly.points)
        if _inside_polygon(circ.center, poly.points) and dmin > circ.radius + tol:
            rel = "poly_contains_circle"
        elif vmax < circ.radius - tol:
            rel = "circle_contains_poly"
        elif not _inside_polygon(circ.center, poly.points) and dmin > circ.radius + tol:
            return "disjoint"
        else:
            return "touching"
        if rel == "poly_contains_circle":
            return "b_contains_a" if a.is_circle else "a_contains_b"
        return "a_contains_b" if a.is_circle else "b_contains_a"
    assert a.points and b.points
    for s in a.segments():
        for t in b.segments():
            if _seg_seg_dist(*s, *t) <= tol:
                return "touching"
    if _inside_polygon(b.points[0], a.points):
        return "a_contains_b"
    if _inside_polygon(a.points[0], b.points):
        return "b_contains_a"
    return "disjoint"


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #


def _valid_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _layer_of(e: Any) -> str:
    return str(e.dxf.get("layer", "0"))


def _xy_extrusion(e: Any) -> bool:
    try:
        ex = e.dxf.get("extrusion", (0.0, 0.0, 1.0))
        return abs(ex[0]) < 1e-9 and abs(ex[1]) < 1e-9 and ex[2] > 0
    except Exception:
        return True


def profile_to_spec(
    dxf_bytes: bytes,
    *,
    layer: str = "PROFILE",
    thickness_mm: float | None,
    scale: float | None,
    base_z: float = 0.0,
    units: str | None = None,
) -> ProfileImport:
    """Import the closed contours on ``layer`` as a plate (see the module
    docstring). ``thickness_mm`` and ``scale`` must be given (None → refusal);
    ``units`` declares the unit of a unitless file (only ``"mm"`` accepted).
    Returns :class:`ProfileImport`; raises :class:`ProfileRefusal`."""
    data = bytes(dxf_bytes)
    report = ProfileReport(
        status="refused", source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data), layer=layer,
        scale=float(scale) if _valid_number(scale) else None,
        thickness_mm=float(thickness_mm) if _valid_number(thickness_mm) else None,
        base_z=float(base_z) if _valid_number(base_z) else None,
    )
    problems: list[ProfileProblem] = report.problems

    def refuse() -> ProfileRefusal:
        problems.sort(key=lambda p: _PRIORITY.index(p.code) if p.code in _PRIORITY else len(_PRIORITY))
        report.refusal = problems[0]
        report.status = "refused"
        return ProfileRefusal(problems[0].code, problems[0].message, report)

    # -- parameters ------------------------------------------------------- #
    missing = []
    if thickness_mm is None:
        missing.append("thickness_mm is required (no default thickness)")
    elif not _valid_number(thickness_mm) or not (MIN_DIM <= float(thickness_mm) <= MAX_DIM):
        missing.append(f"thickness_mm must be a number between {MIN_DIM} and {MAX_DIM} mm")
    if scale is None:
        missing.append("scale is required (no default scale; part_mm = drawing_units × scale)")
    elif not _valid_number(scale) or float(scale) <= 0:
        missing.append("scale must be a positive finite number")
    if not _valid_number(base_z) or abs(float(base_z)) > MAX_DIM:
        missing.append(f"base_z must be a finite number within ±{MAX_DIM} mm")
    if not isinstance(layer, str) or not layer.strip():
        missing.append("layer must be a non-empty layer name")
    if missing:
        problems.append(ProfileProblem(code="profile_missing_parameter", message="; ".join(missing)))
        raise refuse()
    t = float(thickness_mm)  # type: ignore[arg-type]
    k = float(scale)  # type: ignore[arg-type]
    z0 = float(base_z)

    # -- load + units ----------------------------------------------------- #
    try:
        loaded = read_dxf_bytes(data)
    except DrawingEvidenceError as exc:
        problems.append(ProfileProblem(code="profile_unreadable", message=exc.message))
        raise refuse() from exc
    doc = loaded.doc
    report.warnings.extend(loaded.warnings)
    ins = doc.header.get("$INSUNITS", None)
    ins_i = int(ins) if _valid_number(ins) else None
    report.units = ProfileUnits(insunits=ins_i, declared=units)
    declared_mm = units is not None and units.strip().lower() in _MM_NAMES
    if units is not None and not declared_mm:
        problems.append(ProfileProblem(code="profile_units", message=f"declared units {units!r}: only millimetres are supported"))
    elif ins_i == 4:
        report.units.used, report.units.source = "mm", "header"
    elif ins_i in (None, 0):
        if declared_mm:
            report.units.used, report.units.source = "mm", "declared"
        else:
            problems.append(ProfileProblem(code="profile_units",
                                           message=f"$INSUNITS={ins_i} (unitless): declare units='mm' explicitly; units are never guessed"))
    else:
        problems.append(ProfileProblem(code="profile_units",
                                       message=f"$INSUNITS={ins_i} is not millimetres (4); convert the file to mm first"))

    # -- collect ---------------------------------------------------------- #
    want = layer.strip().casefold()
    blocks: dict[str, Any] = {}  # block costs and per-(block, inherited layer) hits, shared by every INSERT
    on_layer: list[Any] = []
    for e in doc.modelspace():
        if _layer_of(e).casefold() == want:
            on_layer.append(e)
        else:
            report.ignored.append(ProfileEntity(id=str(e.dxf.handle), type=e.dxftype(), layer=_layer_of(e), reason="other_layer"))
            if e.dxftype() == "INSERT":
                _check_block(e, want, report, problems, doc, blocks)
    for layout in doc.layouts:
        if layout.name.lower() == "model":
            continue
        for e in layout:
            if e.dxftype() == "VIEWPORT":
                continue
            pe = ProfileEntity(id=str(e.dxf.handle), type=e.dxftype(), layer=_layer_of(e), space=layout.name)
            if _layer_of(e).casefold() == want:
                pe.reason = "profile layer content in paper space"
                report.rejected.append(pe)
                problems.append(ProfileProblem(code="profile_unsupported_entity", message=f"{pe.type} {pe.id} on the profile layer is in paper space ({layout.name})", ids=[pe.id]))
            else:
                pe.reason = "paper_space"
                report.ignored.append(pe)

    contours: list[_Contour] = []
    unsupported: list[str] = []
    open_ids: list[str] = []
    for e in on_layer:
        typ = e.dxftype()
        h = str(e.dxf.handle)
        pe = ProfileEntity(id=h, type=typ, layer=_layer_of(e))
        try:
            if typ == "CIRCLE":
                c, r = e.dxf.center, float(e.dxf.radius)
                pe.center, pe.radius = (float(c.x), float(c.y)), r
                if not _xy_extrusion(e):
                    pe.reason = "not in the XY plane (extrusion is not +Z)"
                elif not (math.isfinite(c.x) and math.isfinite(c.y) and math.isfinite(r)) or r <= 0:
                    pe.reason = "non-finite or zero radius"
                else:
                    contours.append(_Contour(pe, center=(_s(c.x, k), _s(c.y, k)), radius=_s(r, k)))
                    continue
            elif typ in ("LWPOLYLINE", "POLYLINE"):
                reason, pts, closed = _polyline(e)
                pe.vertices = [(float(x), float(y)) for x, y in pts] if pts else None
                if reason is None and not closed:
                    open_ids.append(h)
                    pe.reason = "open contour"
                    report.rejected.append(pe)
                    continue
                if reason is None:
                    poly = _clean_polygon([(_s(x, k), _s(y, k)) for x, y in pts])
                    if poly is None:
                        reason = "degenerate contour (fewer than 3 distinct vertices or zero area)"
                    elif len(poly) > MAX_POLYGON_POINTS:
                        reason = f"more than {MAX_POLYGON_POINTS} vertices"
                    else:
                        contours.append(_Contour(pe, points=poly))
                        _polyline_warnings(e, h, report)
                        continue
                pe.reason = reason
            else:
                pe.reason = f"{typ} is not supported on the profile layer (closed straight polylines and circles only)"
        except Exception as exc:  # unreadable entity data
            pe.reason = f"unreadable: {type(exc).__name__}"
        unsupported.append(h)
        report.rejected.append(pe)
    if unsupported:
        problems.append(ProfileProblem(code="profile_unsupported_entity",
                                       message=f"{len(unsupported)} unsupported entit{'y' if len(unsupported) == 1 else 'ies'} on layer {layer!r}: "
                                               + "; ".join(f"{p.type} {p.id}: {p.reason}" for p in report.rejected if p.id in unsupported)[:600],
                                       ids=unsupported))
    if open_ids:
        problems.append(ProfileProblem(code="profile_open_contour", message=f"open polyline(s) on layer {layer!r}: {', '.join(open_ids)}", ids=open_ids))
    if not contours and not unsupported and not open_ids:
        problems.append(ProfileProblem(code="profile_no_outer", message=f"no closed contour on layer {layer!r}"))
    if problems:
        raise refuse()

    # -- topology --------------------------------------------------------- #
    tol = TOUCH_TOL_MM
    bad = [c.ent.id for c in contours if not c.is_circle and _self_touch(c, tol)]
    if bad:
        problems.append(ProfileProblem(code="profile_touching", message=f"self-intersecting or self-touching contour(s): {', '.join(bad)}", ids=bad))
        raise refuse()
    n = len(contours)
    contains: dict[int, set[int]] = {i: set() for i in range(n)}
    touching: list[tuple[str, str]] = []
    for i in range(n):
        for j in range(i + 1, n):
            rel = _relation(contours[i], contours[j], tol)
            if rel == "a_contains_b":
                contains[i].add(j)
            elif rel == "b_contains_a":
                contains[j].add(i)
            elif rel == "touching":
                touching.append((contours[i].ent.id, contours[j].ent.id))
    if touching:
        ids = sorted({h for pair in touching for h in pair})
        problems.append(ProfileProblem(code="profile_touching",
                                       message=f"contours touch or cross (clearance ≤ {tol} mm): " + ", ".join(f"{a}/{b}" for a, b in touching[:20]),
                                       ids=ids))
        raise refuse()
    depth = {i: sum(1 for j in range(n) if i in contains[j]) for i in range(n)}
    roots = [i for i in range(n) if depth[i] == 0]
    islands = [i for i in range(n) if depth[i] >= 2]
    if len(roots) > 1:
        ids = [contours[i].ent.id for i in roots]
        problems.append(ProfileProblem(code="profile_multiview",
                                       message=f"{len(roots)} separate outer contours on layer {layer!r} (several views or parts): {', '.join(ids)}",
                                       ids=ids))
    if islands:
        ids = [contours[i].ent.id for i in islands]
        problems.append(ProfileProblem(code="profile_multiview",
                                       message=f"contour(s) inside a cut-out would form a second solid: {', '.join(ids)}", ids=ids))
    if problems:
        raise refuse()

    outer = contours[roots[0]]
    inner = [contours[i] for i in range(n) if i != roots[0]]
    inner.sort(key=lambda c: (c.is_circle, c.centroid_key(), c.ent.id))
    if 1 + len(inner) > MAX_OPS:
        problems.append(ProfileProblem(code="profile_unsupported_entity", message=f"{1 + len(inner)} contours exceed the {MAX_OPS}-operation limit",
                                       ids=[c.ent.id for c in inner[MAX_OPS - 1:]]))
        raise refuse()

    # -- spec ------------------------------------------------------------- #
    ops: list[dict[str, Any]] = []
    outer.ent.role, outer.ent.op_id = "outer", "F-01"
    if outer.is_circle:
        assert outer.center and outer.radius is not None
        ops.append({"id": "F-01", "type": "extrude", "mode": "new", "label": f"profile outline {outer.ent.id} (DXF circle)",
                    "profile": {"kind": "circle", "radius": outer.radius}, "plane": "XY", "offset": z0,
                    "at": list(outer.center), "height": t})
    else:
        ops.append({"id": "F-01", "type": "extrude", "mode": "new", "label": f"profile outline {outer.ent.id} (DXF polyline)",
                    "profile": {"kind": "polygon", "points": [list(p) for p in outer.points or []]}, "plane": "XY",
                    "offset": z0, "at": [0.0, 0.0], "height": t})
    for idx, c in enumerate(inner, start=2):
        op_id = f"F-{idx:02d}"
        c.ent.op_id = op_id
        if c.is_circle:
            assert c.center and c.radius is not None
            c.ent.role = "inner_circle"
            ops.append({"id": op_id, "type": "hole", "label": f"profile circle {c.ent.id}: through cut Ø{_fmt(2 * c.radius)} "
                                                              "(geometry from the profile, not a drilled-hole spec)",
                        "at": [c.center[0], c.center[1], _r(z0 + t)], "direction": [0.0, 0.0, -1.0],
                        "diameter": _r(2 * c.radius), "depth": None})
        else:
            c.ent.role = "inner_polygon"
            ops.append({"id": op_id, "type": "extrude", "mode": "subtract",
                        "label": f"profile cut-out {c.ent.id} (through, geometry from the profile)",
                        "profile": {"kind": "polygon", "points": [list(p) for p in c.points or []]}, "plane": "XY",
                        "offset": z0, "at": [0.0, 0.0], "height": t})
    try:
        spec = ModelSpec.model_validate({"ops": ops})
    except ValidationError as exc:
        problems.append(ProfileProblem(code="profile_unsupported_entity",
                                       message=f"the scaled profile is outside the modelling limits: {exc.errors()[0].get('msg', exc)}"[:400],
                                       ids=[c.ent.id for c in contours]))
        raise refuse() from exc

    report.used = [outer.ent] + [c.ent for c in inner]
    area = outer.area - sum(c.area for c in inner)
    b = outer.bbox()
    report.expected = ProfileExpected(area_mm2=_r(area), volume_mm3=_r(area * t), bbox_min=(_r(b[0]), _r(b[1]), _r(z0)),
                                      bbox_max=(_r(b[2]), _r(b[3]), _r(z0 + t)), inner_count=len(inner))
    report.status = "accepted"
    return ProfileImport(spec=spec, report=report)


def _r(v: float) -> float:
    return round(float(v), 6) + 0.0


def _s(v: float, k: float) -> float:
    return _r(float(v) * k)


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _polyline(e: Any) -> tuple[str | None, list[P2], bool]:
    """(rejection reason or None, vertices, closed)."""
    typ = e.dxftype()
    if not _xy_extrusion(e):
        return "not in the XY plane (extrusion is not +Z)", [], False
    if typ == "LWPOLYLINE":
        raw = [(float(x), float(y), float(b)) for x, y, b in e.get_points("xyb")]
        closed = bool(e.closed)
    else:
        if not e.is_2d_polyline:
            return "3D polyline / mesh (only 2D polylines are supported)", [], False
        raw = [(float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.get("bulge", 0.0) or 0.0)) for v in e.vertices]
        closed = bool(e.is_closed)
    if any(not (math.isfinite(x) and math.isfinite(y)) for x, y, _ in raw):
        return "non-finite vertex coordinate", [], False
    pts = [(x, y) for x, y, _ in raw]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) <= 1e-9 * max(1.0, max(abs(c) for p in pts for c in p)):
        closed = True  # explicit closing vertex
        pts = pts[:-1]
        raw = raw[:-1]
    bulged = [b for _, _, b in (raw if closed else raw[:-1]) if abs(b) > 1e-12]
    if bulged:
        return "arc segment (bulge) in the polyline; only straight segments are supported", pts, closed
    return None, pts, closed


def _clean_polygon(pts: list[P2]) -> list[P2] | None:
    out: list[P2] = []
    for p in pts:
        if not out or math.dist(out[-1], p) > 1e-6:
            out.append(p)
    while len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-6:
        out.pop()
    if len(out) < 3 or abs(_signed_area(out)) <= 1e-9:
        return None
    if _signed_area(out) < 0:
        out.reverse()
    return out


def _polyline_warnings(e: Any, h: str, report: ProfileReport) -> None:
    try:
        if e.dxftype() == "LWPOLYLINE":
            if abs(float(e.dxf.get("elevation", 0.0) or 0.0)) > 1e-9:
                report.warnings.append(Issue(code="profile_elevation_ignored", message=f"{h}: polyline elevation ignored; the profile is placed at base_z"))
            if abs(float(e.dxf.get("const_width", 0.0) or 0.0)) > 1e-9 or any(abs(s) > 1e-9 or abs(w) > 1e-9 for s, w in e.get_points("se")):
                report.warnings.append(Issue(code="profile_width_ignored", message=f"{h}: polyline width ignored; the centre line is the contour"))
    except Exception:
        pass


def _block_hits(doc: Any, name: str, inherited: str, want: str, memo: dict[tuple[str, bool], list[str]], level: int) -> list[str]:
    """Handles of profile-layer entities anywhere in block ``name`` (nested INSERTs followed; an entity on
    layer 0 takes the layer of the INSERT that places it). ``level`` is bounded by the caller's depth check."""
    key = (name, inherited.casefold() == want)
    if key in memo:
        return memo[key]
    memo[key] = []  # cycle guard (cycles are refused before this walk)
    hits: list[str] = []
    block = doc.blocks.get(name)
    if block is not None and level <= MAX_BLOCK_DEPTH:
        for be in block:
            own = _layer_of(be)
            eff = inherited if own == "0" else own
            if eff.casefold() == want:
                hits.append(f"{name}/{be.dxf.handle}")
            if be.dxftype() == "INSERT":
                hits += _block_hits(doc, str(be.dxf.get("name", "")), eff, want, memo, level + 1)
    memo[key] = hits
    return hits


def _check_block(insert: Any, want: str, report: ProfileReport, problems: list[ProfileProblem], doc: Any,
                 cache: dict[str, Any]) -> None:
    """Profile-layer geometry hidden anywhere inside an inserted block tree is refused (it would be a contour the
    importer does not read); a tree too deep or cyclic to verify is refused as well."""
    h = str(insert.dxf.handle)
    name = str(insert.dxf.get("name", ""))
    try:
        if "costs" not in cache:
            cache["costs"] = block_costs(doc)
            cache["memo"] = {}
        each, height = cache["costs"].get(doc.block_records.key(name), (0.0, -1.0))
        if math.isinf(each) or height + 1 > MAX_BLOCK_DEPTH:
            why = "references itself" if math.isinf(each) else f"nests {height + 1:.0f} levels deep (limit {MAX_BLOCK_DEPTH})"
            report.rejected.append(ProfileEntity(id=h, type="INSERT", layer=_layer_of(insert), reason=f"block {name!r} {why}"))
            problems.append(ProfileProblem(code="profile_unsupported_entity",
                                           message=f"INSERT {h} (block {name!r}) {why}; its content cannot be verified, "
                                                   "explode it first", ids=[h]))
            return
        hits = _block_hits(doc, name, _layer_of(insert), want, cache["memo"], 1)
    except Exception:
        return
    if hits:
        report.rejected.append(ProfileEntity(id=h, type="INSERT", layer=_layer_of(insert),
                                             reason=f"block {name!r} holds profile-layer entities {', '.join(hits[:10])}"))
        problems.append(ProfileProblem(code="profile_unsupported_entity",
                                       message=f"INSERT {h} (block {name!r}) contains profile-layer geometry; explode it first",
                                       ids=[h]))
