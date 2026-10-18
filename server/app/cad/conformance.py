"""Independent requirement checker (design §2 of .trellis/tasks/archive/2026-10/10-08-cad-ai/design.md).

:func:`check_requirements` re-reads the exported STEP bytes with OCCT's own
STEP reader and measures the solid against a frozen requirement list
(:mod:`app.cad.requirements`). It never imports the compiler under test
(``app.cad.kernel`` / build123d) and never sees the ops.

Primitives (pure OCP):

- point classification (``BRepClass3d_SolidClassifier``);
- material intervals along a line: every face crossing of the line
  (``IntCurvesFace_ShapeIntersector``), each sub-segment classified at its
  midpoint — exact floors, exits and wall radii, no step size to miss thin
  material;
- planar sections (common of the solid with a large planar face) for area
  and centroid;
- concave cylindrical faces (``BRepAdaptor_Surface`` / ``GeomAbs_Cylinder``)
  for bore faces and unrequested holes;
- planar faces for unrequested recesses: horizontal faces strictly between
  the part's Z min and max and vertical walls facing material across a gap
  must be a pocket floor / outline, band boundary / outline, blind-hole or
  counterbore floor of some requirement (or the end of a round bore);
- volume accounting (no fillets / chamfers present): the solid against a
  reference solid built with OCP primitives from the requirement list alone
  (bounding box or band prisms minus holes, counterbores, countersinks and
  pockets); difference pieces thicker than the tolerance are unaccounted;
- optimal bounding box (``BRepBndLib.AddOptimal``, tightened on trimmed free-form faces by
  :func:`app.cad.bounds.tight_bounds`).

Every item reports expected / measured / tolerance and a message precise
enough to feed a model repair ("R3 hole Ø20: floor found at depth 12.00 mm,
required 8.00 mm"). Units: mm, world frame of the STEP file.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import OCP
from OCP.Bnd import Bnd_Box
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.collections import List_TopoDS_Shape
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Lin, gp_Pln, gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_SOLID, TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from pydantic import BaseModel, ConfigDict, Field

from . import conformance_w3 as w3
from .bounds import tight_bounds
from .requirements import (
    GEOMETRIC_KINDS,
    CircleContour,
    PolygonContour,
    RectContour,
    contour_area,
    contour_centroid,
    contour_perimeter,
    parse_requirements,
    polygon_area,
    requirements_sha256,
)

log = logging.getLogger("partroute.cad")

CHECK_SCHEMA = "partroute.check/1"
VALIDATOR_VERSION = f"partroute-conformance-1/ocp-{'.'.join(str(getattr(OCP, '__version__', '0')).split('.')[:3])}"

CheckStatus = Literal["passed", "failed", "not_evaluated"]
CheckMode = Literal["create", "modify", "reconstruct"]

_EPS = 1e-9


class Tolerance(BaseModel):
    """Frozen check tolerances. ``linear``: sizes, positions, depths (mm).
    Probe offset δ = max(``probe_min``, ``probe_rel`` × feature radius)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    linear: float = Field(default=0.05, gt=0)
    probe_min: float = Field(default=0.1, gt=0)
    probe_rel: float = Field(default=0.02, ge=0)
    angular_deg: float = Field(default=0.5, gt=0)

    def probe(self, radius: float) -> float:
        return max(self.probe_min, self.probe_rel * radius)


class CheckItem(BaseModel):
    """One checked requirement (one item per hole instance). ``failures`` holds
    one line per failed sub-check; ``message`` is the whole verdict."""

    requirement_id: str | None = None
    kind: str
    instance: int | None = None
    status: CheckStatus
    expected: dict[str, Any] = Field(default_factory=dict)
    measured: dict[str, Any] = Field(default_factory=dict)
    tolerance: dict[str, float] = Field(default_factory=dict)
    message: str
    failures: list[str] = Field(default_factory=list)


class UnrequestedFeature(BaseModel):
    """Geometry that no requirement explains (create / reconstruct mode).

    ``hole``: a full round concave bore (``diameter``, ``axis``, ``start``,
    ``end``, ``length``). ``recess_face``: a horizontal planar face strictly
    between the part's Z min and max that is no pocket floor, band boundary,
    blind-hole or counterbore floor (``z``, ``area`` of the unexplained part,
    ``bbox``). ``recess_wall``: a vertical planar wall facing material across a
    recess that lies on no pocket / band outline (``bbox``, ``area``).
    ``volume``: material added or removed against the volume the requirements
    imply (``volume``, ``bbox``; only checked without fillets / chamfers).
    ``surface``: a large free-form face (B-spline: a coil, a swept wire) that no
    requirement explains (``area``, ``bbox``) — too large to be an edge break."""

    kind: Literal["hole", "recess_face", "recess_wall", "volume", "surface"] = "hole"
    diameter: float | None = None
    axis: list[float] | None = None
    start: list[float] | None = None
    end: list[float] | None = None
    length: float | None = None
    z: float | None = None
    area: float | None = None
    volume: float | None = None
    bbox: list[float] | None = None  # [xmin, ymin, zmin, xmax, ymax, zmax]
    message: str


class VolumeAccounting(BaseModel):
    """Material accounting of the solid against the requirement list
    (``passed`` / ``failed`` / ``skipped`` with fillets or chamfers present /
    ``not_evaluated`` when other checks already failed or the reference
    solid could not be built)."""

    status: Literal["passed", "failed", "skipped", "not_evaluated"]
    reason: str = ""
    measured_mm3: float | None = None
    expected_mm3: float | None = None
    unaccounted_removed_mm3: float | None = None
    unaccounted_added_mm3: float | None = None


class NoteEntry(BaseModel):
    requirement_id: str
    category: str
    text: str
    reason: str


class NotesCoverage(BaseModel):
    covered: list[NoteEntry] = Field(default_factory=list)
    uncovered: list[NoteEntry] = Field(default_factory=list)
    unknown: list[NoteEntry] = Field(default_factory=list)


class SolidSummary(BaseModel):
    solids: int = 0
    valid: bool | None = None
    volume_mm3: float | None = None
    bbox_min: list[float] | None = None
    bbox_max: list[float] | None = None


class CheckReport(BaseModel):
    """Verdict of one check (dumps with the key ``schema``)."""

    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True, validate_by_alias=True)

    schema_id: str = Field(default=CHECK_SCHEMA, alias="schema")
    validator_version: str = VALIDATOR_VERSION
    step_sha256: str
    requirements_sha256: str
    mode: CheckMode
    tolerance: Tolerance
    status: CheckStatus
    solid: SolidSummary = Field(default_factory=SolidSummary)
    items: list[CheckItem] = Field(default_factory=list)
    unrequested: list[UnrequestedFeature] = Field(default_factory=list)
    accounting: VolumeAccounting | None = None
    notes: NotesCoverage = Field(default_factory=NotesCoverage)

    def problems(self) -> list[str]:
        """Failure lines for repair feedback (failed items, then unrequested)."""
        return [i.message for i in self.items if i.status == "failed"] + [u.message for u in self.unrequested]


# --------------------------------------------------------------------------- #
# Small vector helpers (tuples, no kernel types)
# --------------------------------------------------------------------------- #

V3 = tuple[float, float, float]


def _add(a: V3, b: V3) -> V3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: V3, b: V3) -> V3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: V3, s: float) -> V3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: V3, b: V3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: V3, b: V3) -> V3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: V3) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Sequence[float]) -> V3:
    v = (float(a[0]), float(a[1]), float(a[2]))
    n = _norm(v)
    return (v[0] / n, v[1] / n, v[2] / n)


def _basis(d: V3) -> tuple[V3, V3]:
    """Two unit vectors perpendicular to ``d`` (deterministic)."""
    a = min(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), key=lambda e: abs(_dot(e, d)))
    u = _unit(_sub(a, _mul(d, _dot(a, d))))
    return u, _cross(d, u)


def _axis_distance(p: V3, origin: V3, d: V3) -> float:
    w = _sub(p, origin)
    return _norm(_sub(w, _mul(d, _dot(w, d))))


def _r(x: float, n: int = 4) -> float:
    return round(float(x), n) + 0.0


def _rv(v: Sequence[float], n: int = 4) -> list[float]:
    return [_r(c, n) for c in v]


def _f(x: float) -> str:
    return f"{round(x, 2) + 0.0:.2f}"


def _pt(p: Sequence[float]) -> str:
    return "(" + ", ".join(f"{round(c, 2) + 0.0:.2f}" for c in p) + ")"


def _zr(values: Sequence[float]) -> str:
    lo, hi = min(values), max(values)
    return _f(lo) if hi - lo < 0.005 else f"{_f(lo)}..{_f(hi)}"


def _g(p: Sequence[float]) -> str:
    return "(" + ", ".join(f"{round(c, 6) + 0.0:g}" for c in p) + ")"


def _cast_face(shape: Any) -> Any:
    cast = getattr(TopoDS, "Face", None) or getattr(TopoDS, "Face_s")
    return cast(shape)


def _cast_edge(shape: Any) -> Any:
    cast = getattr(TopoDS, "Edge", None) or getattr(TopoDS, "Edge_s")
    return cast(shape)


# --------------------------------------------------------------------------- #
# Geometry probe
# --------------------------------------------------------------------------- #


@dataclass
class _Bore:
    """Group of coaxial, equal-radius concave cylindrical faces."""

    radius: float
    direction: V3
    origin: V3  # axis point closest to the world origin
    t0: float
    t1: float
    coverage: float

    def point(self, t: float) -> V3:
        return _add(self.origin, _mul(self.direction, t))


class _Probe:
    def __init__(self, solid: Any):
        self.solid = solid
        self._cls = BRepClass3d_SolidClassifier(solid)
        self._int = IntCurvesFace_ShapeIntersector()
        self._int.Load(solid, 1e-7)
        self.lo, self.hi = tight_bounds(solid)  # optimal box, tightened on trimmed free-form faces (coil ends)
        self.diag = _norm(_sub(self.hi, self.lo))
        self.center: V3 = _mul(_add(self.lo, self.hi), 0.5)
        self._bores: list[_Bore] | None = None

    # -- points and lines --------------------------------------------------

    def inside(self, p: Sequence[float]) -> bool:
        self._cls.Perform(gp_Pnt(float(p[0]), float(p[1]), float(p[2])), 1e-7)
        return self._cls.State() == TopAbs_IN

    def intervals(self, origin: V3, d: V3, t0: float | None = None, t1: float | None = None) -> list[tuple[float, float]]:
        """Material intervals ``[(a, b)]`` of the line ``origin + t d`` for t in [t0, t1]
        (default: far enough both ways to cross the whole part)."""
        reach = self.reach(origin)
        lo = -reach if t0 is None else t0
        hi = reach if t1 is None else t1
        ts = [lo, hi]
        self._int.Perform(gp_Lin(gp_Pnt(*origin), gp_Dir(*d)), lo, hi)
        if self._int.IsDone():
            ts += [self._int.WParameter(i) for i in range(1, self._int.NbPnt() + 1)]
        else:  # pragma: no cover - defensive: dense sampling
            n = 4000
            ts += [lo + (hi - lo) * k / n for k in range(1, n)]
        ts = sorted(t for t in ts if lo <= t <= hi)
        out: list[tuple[float, float]] = []
        for a, b in zip(ts, ts[1:]):
            if b - a < _EPS:
                continue
            if self.inside(_add(origin, _mul(d, 0.5 * (a + b)))):
                if out and abs(out[-1][1] - a) < 1e-7:
                    out[-1] = (out[-1][0], b)
                else:
                    out.append((a, b))
        return out

    def gap(self, iv: list[tuple[float, float]], t: float) -> tuple[float, float] | None:
        """The empty stretch ``(g0, g1)`` containing t; None when t is in material."""
        g0, g1 = -math.inf, math.inf
        for a, b in iv:
            if a <= t <= b:
                return None
            if b < t:
                g0 = max(g0, b)
            elif a > t:
                g1 = min(g1, a)
        return g0, g1

    @staticmethod
    def segment(iv: list[tuple[float, float]], t: float) -> tuple[float, float] | None:
        for a, b in iv:
            if a <= t <= b:
                return a, b
        return None

    @staticmethod
    def open_end(g: float) -> bool:
        """An end of a gap with no material beyond it."""
        return math.isinf(g)

    def box_distance(self, p: Sequence[float]) -> float:
        """Distance from ``p`` to the bounding box (0 inside)."""
        return math.sqrt(sum(max(self.lo[i] - p[i], 0.0, p[i] - self.hi[i]) ** 2 for i in range(3)))

    def reach(self, p: Sequence[float]) -> float:
        """A distance from ``p`` beyond which there is no material."""
        return _norm(_sub((float(p[0]), float(p[1]), float(p[2])), self.center)) + self.diag + 10.0

    def ray(self, origin: V3, w: V3, max_len: float) -> float | None:
        """Distance to the first material along a ray (0 when origin is in material)."""
        iv = self.intervals(origin, w, 0.0, max_len)
        return iv[0][0] if iv else None

    def circle_fit(self, center: V3, u: V3, v: V3, max_len: float) -> dict[str, Any] | None:
        """Measure a round boundary around ``center`` in the (u, v) plane from 8 rays.

        Returns ``{"diameters": [...], "offset": (du, dv), "center": world}``;
        None when a ray finds no wall. Exact for a circle: ρ(w) − ρ(−w) = 2 w·o.
        """
        dirs: list[V3] = []
        for k in range(4):
            a = math.pi * k / 4
            dirs.append(_unit(_add(_mul(u, math.cos(a)), _mul(v, math.sin(a)))))
        rho: list[tuple[float, float]] = []
        for w in dirs:
            r1 = self.ray(center, w, max_len)
            r2 = self.ray(center, _mul(w, -1.0), max_len)
            if r1 is None or r2 is None:
                return None
            rho.append((r1, r2))
        ou = (rho[0][0] - rho[0][1]) / 2.0
        ov = (rho[2][0] - rho[2][1]) / 2.0
        o = _add(_mul(u, ou), _mul(v, ov))
        diameters = []
        for w, (r1, r2) in zip(dirs, rho):
            half = (r1 + r2) / 2.0
            wo = _dot(w, o)
            diameters.append(2.0 * math.sqrt(max(0.0, half * half + _dot(o, o) - wo * wo)))
        return {"diameters": diameters, "offset": (ou, ov), "center": _add(center, o)}

    def section(self, z: float) -> tuple[float, float, float] | None:
        """``(area, cx, cy)`` of the horizontal cross-section at Z (None if the boolean fails)."""
        pln = gp_Pln(gp_Pnt(0.0, 0.0, z), gp_Dir(0.0, 0.0, 1.0))
        face = BRepBuilderAPI_MakeFace(pln, self.lo[0] - 10, self.hi[0] + 10, self.lo[1] - 10, self.hi[1] + 10).Face()
        op = BRepAlgoAPI_Common(self.solid, face)
        op.Build()
        if not op.IsDone():
            return None
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(op.Shape(), props)
        area = float(props.Mass())
        if area < 1e-12:
            return (0.0, float("nan"), float("nan"))
        c = props.CentreOfMass()
        return (area, c.X(), c.Y())

    # -- cylindrical bores -------------------------------------------------

    def bores(self) -> list[_Bore]:
        if self._bores is not None:
            return self._bores
        groups: dict[tuple, list[tuple[float, V3, V3, float, float, float]]] = {}
        exp = TopExp_Explorer(self.solid, TopAbs_FACE)
        while exp.More():
            face = _cast_face(exp.Current())
            exp.Next()
            surf = BRepAdaptor_Surface(face)
            if surf.GetType() != GeomAbs_Cylinder:
                continue
            cyl = surf.Cylinder()
            ax = cyl.Axis()
            r = float(cyl.Radius())
            ds = (ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
            ls = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
            umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
            pnt, nrm = gp_Pnt(), gp_Vec()
            BRepGProp_Face(face).Normal(0.5 * (umin + umax), 0.5 * (vmin + vmax), pnt, nrm)
            p = (pnt.X(), pnt.Y(), pnt.Z())
            radial = _sub(p, _add(ls, _mul(ds, _dot(_sub(p, ls), ds))))
            if _norm(radial) < 1e-9 or _dot((nrm.X(), nrm.Y(), nrm.Z()), radial) >= 0:
                continue  # convex (outer) cylinder
            d = _unit(ds)
            dom = max(range(3), key=lambda i: abs(d[i]))
            if d[dom] < 0:
                d = _mul(d, -1.0)
            origin = _sub(ls, _mul(d, _dot(ls, d)))
            ta = _dot(_sub(_add(ls, _mul(_unit(ds), vmin)), origin), d)
            tb = _dot(_sub(_add(ls, _mul(_unit(ds), vmax)), origin), d)
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            key = (tuple(_r(c, 4) for c in d), tuple(_r(c, 3) for c in origin), _r(r, 4))
            groups.setdefault(key, []).append((r, d, origin, min(ta, tb), max(ta, tb), float(props.Mass())))
        out: list[_Bore] = []
        for items in groups.values():
            r, d, origin = items[0][0], items[0][1], items[0][2]
            # One bore per contiguous stretch of the axis: two coaxial holes of the same size drilled
            # from opposite faces are two bores (each judged on its own), not one half-covered bore.
            runs: list[list[tuple[float, V3, V3, float, float, float]]] = []
            for it in sorted(items, key=lambda i: (i[3], i[4])):
                if runs and it[3] <= max(i[4] for i in runs[-1]) + 1e-6:
                    runs[-1].append(it)
                else:
                    runs.append([it])
            for run in runs:
                t0 = min(i[3] for i in run)
                t1 = max(i[4] for i in run)
                length = t1 - t0
                if length <= 1e-6:
                    continue
                coverage = sum(i[5] for i in run) / (2 * math.pi * r * length)
                out.append(_Bore(radius=r, direction=d, origin=origin, t0=t0, t1=t1, coverage=coverage))
        out.sort(key=lambda b: (b.radius, b.origin, b.direction, b.t0))
        self._bores = out
        return out

    def holes(self) -> list[_Bore]:
        """Full 360° concave cylinders (hole-like cavities)."""
        return [b for b in self.bores() if b.coverage >= 0.98]


# --------------------------------------------------------------------------- #
# STEP loading
# --------------------------------------------------------------------------- #


def _load_solid(data: bytes) -> tuple[Any | None, SolidSummary, str | None]:
    summary = SolidSummary()
    if not data.lstrip()[:16].upper().startswith(b"ISO-10303-21"):
        return None, summary, "not a STEP file (missing ISO-10303-21 header)"
    reader = STEPControl_Reader()
    try:
        status = reader.ReadStream("check.step", io.BytesIO(data))
    except Exception as exc:  # pragma: no cover - reader raises rarely
        return None, summary, f"STEP is not readable: {exc}"
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        return None, summary, f"STEP is not readable (reader status {getattr(status, 'name', status)})"
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        return None, summary, "STEP contains no shape"
    solids = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        solids.append(exp.Current())
        exp.Next()
    summary.solids = len(solids)
    if len(solids) != 1:
        return None, summary, f"STEP has {len(solids)} solids; exactly one solid is required"
    solid = solids[0]
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, props)
    summary.volume_mm3 = _r(props.Mass(), 6)
    summary.valid = bool(BRepCheck_Analyzer(solid).IsValid())
    lo, hi = tight_bounds(solid)
    summary.bbox_min = _rv(lo, 6)
    summary.bbox_max = _rv(hi, 6)
    if not summary.valid:
        return None, summary, "the solid is not valid (BRepCheck failed)"
    if props.Mass() <= 1e-9:
        return None, summary, f"the solid has no positive volume ({props.Mass():.3f} mm³)"
    return solid, summary, None


# --------------------------------------------------------------------------- #
# Requirement checks
# --------------------------------------------------------------------------- #


def _item(req: Any, failures: list[str], head: str, ok_text: str, **kw: Any) -> CheckItem:
    if failures:
        return CheckItem(
            requirement_id=req.id, kind=req.kind, status="failed", message=f"{head}: " + "; ".join(failures), failures=failures, **kw
        )
    return CheckItem(requirement_id=req.id, kind=req.kind, status="passed", message=f"{head}: {ok_text}", **kw)


def _check_envelope(P: _Probe, req: Any, tol: Tolerance) -> CheckItem:
    size = _sub(P.hi, P.lo)
    failures: list[str] = []
    for i, n in enumerate("XYZ"):
        want = req.size[i]
        if want is not None and abs(size[i] - want) > tol.linear:
            failures.append(f"size {n} measured {_f(size[i])} mm, required {_f(want)} mm")
    if req.min is not None:
        for i, n in enumerate("XYZ"):
            if abs(P.lo[i] - req.min[i]) > tol.linear:
                failures.append(
                    f"bounding box starts at {n} {_f(P.lo[i])}, required {n} {_f(req.min[i])} (part is shifted by {_f(P.lo[i] - req.min[i])} mm)"
                )
    sz = "×".join("?" if s is None else f"{s:g}" for s in req.size)
    return _item(
        req,
        failures,
        f"{req.id} envelope {sz}",
        f"ok (measured {' × '.join(_f(s) for s in size)} mm)",
        expected={"size": list(req.size), "min": list(req.min) if req.min is not None else None},
        measured={"size": _rv(size), "min": _rv(P.lo), "max": _rv(P.hi)},
        tolerance={"linear_mm": tol.linear},
    )


def _nearest_bore(P: _Probe, entry: V3, d: V3, radius: float) -> tuple[_Bore, float] | None:
    """The full round bore (roughly parallel to ``d``) closest to the entry, for messages."""
    cos_tol = math.cos(math.radians(5.0))
    found = [(b, _axis_distance(entry, b.origin, b.direction)) for b in P.holes() if abs(_dot(b.direction, d)) >= cos_tol]
    return min(found, key=lambda bd: bd[1] + abs(bd[0].radius - radius), default=None)


def _check_hole(P: _Probe, req: Any, k: int, entry: Sequence[float], tol: Tolerance) -> CheckItem:
    E: V3 = (float(entry[0]), float(entry[1]), float(entry[2]))
    d = _unit(req.direction)
    u, v = _basis(d)
    D = float(req.diameter)
    r = D / 2.0
    delta = tol.probe(r)
    lin = tol.linear
    blind = not req.through
    h = float(req.depth) if req.depth is not None else 0.0
    many = len(req.entries) > 1
    head = f"{req.id} hole Ø{D:g}{f' #{k + 1}' if many else ''} at {_g(E)}"
    failures: list[str] = []
    measured: dict[str, Any] = {}
    expected: dict[str, Any] = {
        "entry": list(E),
        "direction": _rv(d, 6),
        "diameter": D,
        "through": req.through,
        "depth": req.depth,
        "counterbore": req.counterbore.model_dump() if req.counterbore else None,
        "countersink": req.countersink.model_dump() if req.countersink else None,
    }
    kw = {"instance": k, "expected": expected, "measured": measured, "tolerance": {"linear_mm": lin, "probe_mm": delta}}
    cb, cs = req.counterbore, req.countersink
    walls = [u, _mul(u, -1.0), v, _mul(v, -1.0)]

    def at(t: float, w: V3 | None = None, off: float = 0.0) -> V3:
        p = _add(E, _mul(d, t))
        return p if w is None else _add(p, _mul(w, off))

    # 1. axis: an opening at the entry, then the floor (blind) or a clear path (through)
    t_probe = min(delta, 0.5 * h) if blind else delta
    outside = P.box_distance(E)
    if outside > max(lin, delta):
        failures.append(f"the entry point is {_f(outside)} mm outside the part's bounding box")
        return _item(req, failures, head, "", **kw)
    axis_iv = P.intervals(E, d)
    gap = P.gap(axis_iv, t_probe)
    if gap is None:
        failures.append(f"no hole at the entry: material on the axis {_f(t_probe)} mm below the entry point")
        near = _nearest_bore(P, E, d, r)
        if near is not None:
            b, dist = near
            foot = b.point(_dot(_sub(E, b.origin), b.direction))
            failures.append(
                f"nearest round bore parallel to the direction is Ø{_f(2 * b.radius)} with its axis {_f(dist)} mm from the entry "
                f"(passes {_pt(foot)})"
            )
            measured["nearest_bore"] = {"diameter": _r(2 * b.radius), "axis_point": _rv(foot), "distance": _r(dist)}
        else:
            failures.append("no round bore parallel to the required direction was found")
        return _item(req, failures, head, "", **kw)
    g0, g1 = gap
    if g0 > -delta:
        failures.append(f"the opening starts {_f(g0)} mm along the axis from the entry point (material right above the entry)")
    floor_open = P.open_end(g1)
    if blind:
        if floor_open:
            failures.append(f"no floor: the axis is open through the part, required a blind hole {_f(h)} mm deep")
            measured["through"] = True
        else:
            seg = P.segment(axis_iv, g1 + 1e-6)
            measured["through"] = False
            measured["floor_depth"] = _r(g1)
            if seg is not None:
                measured["floor_thickness"] = _r(seg[1] - seg[0])
            if abs(g1 - h) > lin:
                failures.append(f"floor found at depth {_f(g1)} mm, required {_f(h)} mm")
            elif seg is not None and seg[1] - seg[0] < delta:
                failures.append(f"only {_f(seg[1] - seg[0])} mm of material below the floor (blind bottom thinner than {_f(delta)} mm)")
    elif not floor_open:
        if any(P.inside(at(g1 - delta, w, r + delta)) for w in walls):
            failures.append(f"material on the axis at depth {_f(g1)} mm (a blind floor), required a through hole")
            measured["through"] = False
            measured["floor_depth"] = _r(g1)
        else:
            measured["through"] = True
            measured["exits_into_cavity_at"] = _r(g1)
    else:
        measured["through"] = True

    # 2. counterbore / countersink, measured where they actually are
    r_top, t_top = r, 0.0
    if cb is not None:
        r_top = cb.diameter / 2.0
        floors = []
        for w in walls:
            g = P.gap(P.intervals(at(0.0, w, 0.5 * (r + r_top)), d), min(delta, 0.5 * cb.depth))
            if g is not None and not P.open_end(g[1]):
                floors.append(g[1])
        if floors:
            cfd = sum(floors) / len(floors)
            t_top = max(cb.depth, max(floors))
            measured["counterbore_depth"] = _r(cfd)
            if max(abs(x - cb.depth) for x in floors) > lin:
                failures.append(f"counterbore depth measured {_f(cfd)} mm, required {_f(cb.depth)} mm")
        else:
            t_top = cb.depth
            failures.append(f"counterbore Ø{_f(cb.diameter)} not found (no step between the bore and the counterbore wall)")
        cdepth = 0.5 * min(cb.depth, *(floors or [cb.depth]))
        fit = P.circle_fit(at(cdepth), u, v, 4 * r_top + 10.0)
        if fit is None:
            failures.append("counterbore wall missing")
        else:
            cdm = sum(fit["diameters"]) / 4.0
            measured["counterbore_diameter"] = _r(cdm)
            if max(abs(x - cb.diameter) for x in fit["diameters"]) > lin:
                failures.append(f"counterbore diameter measured {_f(cdm)} mm, required {_f(cb.diameter)} mm")
    elif cs is not None:
        r_top = cs.diameter / 2.0
        half = math.radians(cs.angle) / 2.0
        ends = []
        for w in walls:  # where the cone meets a line just outside the bore
            g = P.gap(P.intervals(at(0.0, w, r + delta), d), min(delta, 0.5 * (r_top - r) / math.tan(half)))
            if g is not None and not P.open_end(g[1]):
                ends.append(g[1])
        if not ends:
            failures.append(f"countersink Ø{_f(cs.diameter)} not found")
            t_top = (r_top - r) / math.tan(half)
        else:
            t_cone = min(ends)
            t_top = max(ends) + delta / math.tan(half)
            samples = []
            for q in (0.2, 0.8):
                fit = P.circle_fit(at(q * t_cone), u, v, 4 * r_top + 10.0)
                if fit is None:
                    break
                samples.append((q * t_cone, sum(fit["diameters"]) / 8.0))
            if len(samples) == 2 and samples[1][0] > samples[0][0]:
                (t1, m1), (t2, m2) = samples
                slope = (m1 - m2) / (t2 - t1)
                ang = 2.0 * math.degrees(math.atan(slope))
                dsurf = 2.0 * (m1 + t1 * slope)
                measured["countersink_diameter"] = _r(dsurf)
                measured["countersink_angle"] = _r(ang, 2)
                if abs(dsurf - cs.diameter) > lin or abs(ang - cs.angle) > tol.angular_deg:
                    failures.append(
                        f"countersink measured Ø{_f(dsurf)} × {ang:.1f}°, required Ø{_f(cs.diameter)} × {cs.angle:g}°"
                    )
            else:
                failures.append("countersink wall missing")

    # 3. diameter and position of the main bore (two depths), from wall rays
    t_a = t_top
    if blind:
        t_b = min(h, g1) if not floor_open else h
    else:
        exits = []
        for w in walls:
            later = [(a, b) for a, b in P.intervals(at(0.0, w, r + delta), d) if b > t_a + delta]
            if later:
                exits.append(min(later)[1])
        t_b = min(exits) if exits else (g1 if not floor_open else t_a + 2 * delta)
        if not floor_open:
            t_b = min(t_b, g1)  # measure the bore above a (wrong) floor, not the material below it
    span = t_b - t_a
    if span > 2 * _EPS:
        fits = []
        for q in (0.25, 0.75):
            t = t_a + q * span
            fit = P.circle_fit(at(t), u, v, 4 * r + 10.0)
            if fit is None:
                failures.append(f"bore wall missing at depth {_f(t)} mm (the hole breaks out of the part)")
                continue
            fit["depth"] = t
            fits.append(fit)
        if fits:
            dias = [x for f in fits for x in f["diameters"]]
            dm = sum(dias) / len(dias)
            off = max(fits, key=lambda f: math.hypot(*f["offset"]))
            off_len = math.hypot(*off["offset"])
            measured["diameter"] = _r(dm)
            measured["axis_offset"] = _r(off_len)
            measured["axis_point"] = _rv(off["center"])
            if max(abs(x - D) for x in dias) > lin:
                failures.append(f"diameter measured {_f(dm)} mm, required {_f(D)} mm")
            if off_len > lin:
                failures.append(
                    f"hole axis is {_f(off_len)} mm from the required position (measured centre {_pt(off['center'])} at depth "
                    f"{_f(off['depth'])} mm)"
                )
            elif max(abs(x - D) for x in dias) <= lin:
                thin = [
                    at(f["depth"], w, r + delta)
                    for f in fits
                    for w in (_unit(_add(_mul(u, math.cos(a)), _mul(v, math.sin(a)))) for a in (k * math.pi / 4 for k in range(8)))
                    if not P.inside(at(f["depth"], w, r + delta))
                ]
                if thin:
                    failures.append(f"wall thinner than {_f(delta)} mm around the hole (no material at {_pt(thin[0])})")

    # 4. an exact cylindrical face of the required radius on the required axis
    cos_tol = math.cos(math.radians(tol.angular_deg))
    face_ok = any(
        abs(b.radius - r) <= lin and abs(_dot(b.direction, d)) >= cos_tol and _axis_distance(E, b.origin, b.direction) <= lin
        for b in P.bores()
    )
    measured["cylinder_face"] = face_ok
    if not face_ok and not any("diameter measured" in f or "axis is" in f for f in failures):
        failures.append(f"no cylindrical bore face Ø{_f(D)} on the required axis")

    # 5. the surface is at the entry point: material starts there next to the opening
    #    (probed just outside the measured opening so an oversized hole is not mistaken)
    r_wall = r_top
    if "diameter" in measured:
        r_wall = max(r_wall, measured["diameter"] / 2.0 + measured.get("axis_offset", 0.0))
    for key in ("counterbore_diameter", "countersink_diameter"):
        if key in measured:
            r_wall = max(r_wall, measured[key] / 2.0)
    offsets: list[float] = []
    for w in walls:
        starts = [a for a, b in P.intervals(at(0.0, w, r_wall + delta), d) if b > delta]
        if starts:
            offsets.append(min(starts, key=abs))
    if offsets:
        best = min(offsets, key=abs)
        measured["entry_surface_offset"] = _r(best)
        if abs(best) > lin:
            where = "above" if best > 0 else "below"
            failures.append(f"the entry point is {_f(abs(best))} mm {where} the part surface next to the hole")
    else:
        failures.append("no material around the entry point")

    if blind:
        ok = f"ok (Ø{_f(measured.get('diameter', D))}, floor at depth {_f(measured.get('floor_depth', h))} mm)"
    else:
        ok = f"ok (Ø{_f(measured.get('diameter', D))}, through)"
    return _item(req, failures, head, ok, **kw)


def _contour_text(c: Any) -> str:
    if isinstance(c, RectContour):
        return f"rect {c.size[0]:g}×{c.size[1]:g} at {_g(c.center)}"
    if isinstance(c, CircleContour):
        return f"Ø{c.diameter:g} at {_g(c.center)}"
    return f"polygon of {len(c.points)} points"


def _ring_points(c: Any, offset: float) -> list[tuple[float, float]]:
    """Points ``offset`` outside (+) or inside (−) the outline: edge midpoints, and corners / 8 directions."""
    if isinstance(c, CircleContour):
        rr = c.diameter / 2.0 + offset
        return [(c.center[0] + rr * math.cos(a), c.center[1] + rr * math.sin(a)) for a in (k * math.pi / 4 for k in range(8))]
    if isinstance(c, RectContour):
        cx, cy = c.center
        hw, hl = c.size[0] / 2.0 + offset, c.size[1] / 2.0 + offset
        pts = [(cx + hw, cy), (cx - hw, cy), (cx, cy + hl), (cx, cy - hl)]
        if offset < 0:  # inside the corners (also inside a rounded corner)
            s = c.corner_radius * (1 - 1 / math.sqrt(2)) - offset
            hw0, hl0 = c.size[0] / 2.0, c.size[1] / 2.0
            pts += [(cx + sx * (hw0 - s), cy + sy * (hl0 - s)) for sx in (1, -1) for sy in (1, -1)]
        return pts
    pts_in = c.points
    sign = 1.0 if polygon_area(pts_in) > 0 else -1.0  # CCW: outward normal is right of the edge direction
    out = []
    n = len(pts_in)
    for i in range(n):
        (x1, y1), (x2, y2) = pts_in[i], pts_in[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        length = math.hypot(ex, ey)
        if length < 1e-9:
            continue
        nx, ny = sign * ey / length, -sign * ex / length
        out.append(((x1 + x2) / 2 + nx * offset, (y1 + y2) / 2 + ny * offset))
    return out


def _measure_outline(P: _Probe, c: Any, z: float, *, from_outside: bool) -> dict[str, float] | None:
    """Measured size / diameter and centre of a rect / circle outline at Z by rays
    (from the centre outward for openings, from outside inward for outer outlines)."""
    if isinstance(c, PolygonContour):
        return None
    cx, cy = c.center
    half = (c.size[0] / 2.0, c.size[1] / 2.0) if isinstance(c, RectContour) else (c.diameter / 2.0,) * 2
    b: dict[str, float] = {}
    for name, w, hlen in (("+x", (1.0, 0.0, 0.0), half[0]), ("-x", (-1.0, 0.0, 0.0), half[0]),
                          ("+y", (0.0, 1.0, 0.0), half[1]), ("-y", (0.0, -1.0, 0.0), half[1])):
        if from_outside:
            far = hlen + P.reach((cx, cy, z))
            start = (cx + w[0] * far, cy + w[1] * far, z)
            t = P.ray(start, _mul(w, -1.0), far)
            if t is None:
                return None
            b[name] = far - t
        else:
            t = P.ray((cx, cy, z), w, hlen * 4 + 10.0)
            if t is None or t <= 0.0:
                return None
            b[name] = t
    out = {
        "size_x": b["+x"] + b["-x"],
        "size_y": b["+y"] + b["-y"],
        "center_x": cx + (b["+x"] - b["-x"]) / 2.0,
        "center_y": cy + (b["+y"] - b["-y"]) / 2.0,
    }
    return out


def _outline_failures(c: Any, m: dict[str, float] | None, what: str, lin: float) -> list[str]:
    if m is None:
        return []
    out = []
    if isinstance(c, RectContour):
        if abs(m["size_x"] - c.size[0]) > lin or abs(m["size_y"] - c.size[1]) > lin:
            out.append(f"{what} measured {_f(m['size_x'])} × {_f(m['size_y'])} mm, required {_f(c.size[0])} × {_f(c.size[1])} mm")
    elif isinstance(c, CircleContour):
        dm = 0.5 * (m["size_x"] + m["size_y"])
        if abs(m["size_x"] - c.diameter) > lin or abs(m["size_y"] - c.diameter) > lin:
            out.append(f"{what} diameter measured {_f(dm)} mm, required {_f(c.diameter)} mm")
    if math.hypot(m["center_x"] - c.center[0], m["center_y"] - c.center[1]) > lin:
        out.append(
            f"{what} centre measured ({_f(m['center_x'])}, {_f(m['center_y'])}), required ({_f(c.center[0])}, {_f(c.center[1])})"
        )
    return out


def _char_radius(c: Any) -> float:
    if isinstance(c, RectContour):
        return min(c.size) / 2.0
    if isinstance(c, CircleContour):
        return c.diameter / 2.0
    return math.sqrt(contour_area(c)) / 2.0


def _in_xy(P: _Probe, x: float, y: float) -> bool:
    return P.lo[0] + 1e-6 < x < P.hi[0] - 1e-6 and P.lo[1] + 1e-6 < y < P.hi[1] - 1e-6


def _check_pocket(P: _Probe, req: Any, tol: Tolerance) -> CheckItem:
    c = req.contour
    lin = tol.linear
    delta = tol.probe(_char_radius(c))
    ez = float(req.entry_z)
    blind = not req.through
    h = float(req.depth) if blind else None
    down: V3 = (0.0, 0.0, -1.0)
    head = f"{req.id} pocket {_contour_text(c)} from Z {ez:g}"
    failures: list[str] = []
    measured: dict[str, Any] = {}
    t_probe = min(delta, 0.5 * h) if h is not None else delta
    interior = [p for p in _ring_points(c, -delta) if _in_xy(P, *p)]
    cen = contour_centroid(c)
    if not isinstance(c, PolygonContour) or _point_in_polygon(cen, c.points):
        interior.insert(0, cen)
    floors: list[float] = []
    solid_at: list[V3] = []
    closed_top: list[float] = []
    no_floor = 0
    bad_floor: list[float] = []
    thin_floor: list[float] = []
    blocked: list[float] = []
    for x, y in interior:
        iv = P.intervals((x, y, ez), down)
        g = P.gap(iv, t_probe)
        if g is None:
            solid_at.append((x, y, ez - t_probe))
            continue
        g0, g1 = g
        if g0 > -delta:
            closed_top.append(ez - g0)
        if blind:
            assert h is not None
            if P.open_end(g1):
                no_floor += 1
            else:
                floors.append(ez - g1)
                seg = P.segment(iv, g1 + 1e-6)
                if abs(g1 - h) > lin:
                    bad_floor.append(ez - g1)
                elif seg is not None and seg[1] - seg[0] < delta:
                    thin_floor.append(seg[1] - seg[0])
        elif not P.open_end(g1):
            blocked.append(ez - g1)
    n = len(interior)
    if solid_at:
        failures.append(f"material inside the outline at {len(solid_at)} of {n} probes, e.g. {_pt(solid_at[0])} (pocket missing or smaller)")
    if closed_top:
        failures.append(f"material above the pocket at {len(closed_top)} of {n} probes: the opening starts at Z {_zr(closed_top)}")
    if no_floor:
        failures.append(f"no floor at {no_floor} of {n} probes (open through the part), required a floor at Z {_f(ez - h)}")  # type: ignore[operator]
    if bad_floor:
        failures.append(f"floor found at Z {_zr(bad_floor)} at {len(bad_floor)} of {n} probes, required Z {_f(ez - h)} (depth {_f(h)} mm)")  # type: ignore[operator,arg-type]
    if thin_floor:
        failures.append(f"only {_zr(thin_floor)} mm of material below the pocket floor (thinner than {_f(delta)} mm)")
    if blocked:
        failures.append(f"material below the pocket at Z {_zr(blocked)} at {len(blocked)} of {n} probes, required through")
    if floors:
        measured["floor_z"] = [_r(min(floors)), _r(max(floors))]
    z_m = ez - 0.5 * h if h is not None else 0.5 * (ez + P.lo[2])
    ring = [(x, y) for x, y in _ring_points(c, delta) if _in_xy(P, x, y)]
    missing = [(x, y, z_m) for x, y in ring if not P.inside((x, y, z_m))]
    if missing:
        failures.append(
            f"no material just outside the outline at {len(missing)} of {len(ring)} probes, e.g. {_pt(missing[0])} (pocket larger or shifted)"
        )
    m = _measure_outline(P, c, z_m, from_outside=False)
    if m is not None:
        measured["outline"] = {k: _r(v) for k, v in m.items()}
    failures += _outline_failures(c, m, "outline", lin)
    ok = f"ok (floor at Z {_f(ez - h)})" if h is not None else "ok (through)"
    return _item(
        req,
        failures,
        head,
        ok,
        expected={"contour": c.model_dump(), "entry_z": ez, "through": req.through, "depth": h},
        measured=measured,
        tolerance={"linear_mm": lin, "probe_mm": delta},
    )


def _point_in_polygon(p: tuple[float, float], pts: Sequence[Sequence[float]]) -> bool:
    x, y = p
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _check_band(P: _Probe, req: Any, tol: Tolerance) -> CheckItem:
    lin = tol.linear
    z0, z1 = float(req.z0), float(req.z1)
    dz = z1 - z0
    eps = min(max(2 * lin, tol.probe_min), dz / 4.0)
    z_m = 0.5 * (z0 + z1)
    outer, voids = req.outer, list(req.voids)
    head = f"{req.id} band Z {z0:g}..{z1:g} outer {_contour_text(outer)}" + (f", {len(voids)} void(s)" if voids else "")
    failures: list[str] = []
    measured: dict[str, Any] = {}
    a_out = contour_area(outer)
    exp_area = a_out - sum(contour_area(v) for v in voids)
    co = contour_centroid(outer)
    mx = a_out * co[0] - sum(contour_area(v) * contour_centroid(v)[0] for v in voids)
    my = a_out * co[1] - sum(contour_area(v) * contour_centroid(v)[1] for v in voids)
    exp_c = (mx / exp_area, my / exp_area) if exp_area > 1e-12 else co
    area_tol = (contour_perimeter(outer) + sum(contour_perimeter(v) for v in voids)) * lin + 1e-6
    delta = tol.probe(_char_radius(outer))

    sections = []
    bad_area: list[tuple[float, float]] = []
    bad_centroid: list[tuple[float, float, float]] = []
    for z in (z0 + eps, z_m, z1 - eps):
        sec = P.section(z)
        if sec is None:
            failures.append(f"section at Z {_f(z)} could not be computed")
            continue
        area, cx, cy = sec
        sections.append({"z": _r(z), "area": _r(area, 3), "centroid": [_r(cx), _r(cy)] if area > 0 else None})
        if abs(area - exp_area) > area_tol:
            bad_area.append((z, area))
        elif math.hypot(cx - exp_c[0], cy - exp_c[1]) > lin:
            bad_centroid.append((z, cx, cy))
    measured["sections"] = sections
    if bad_area:
        failures.append(
            f"section area {' / '.join(f'{a:.1f}' for _, a in bad_area)} mm² at Z {' / '.join(_f(z) for z, _ in bad_area)}, "
            f"required {exp_area:.1f} ± {area_tol:.1f} mm²"
        )
    if bad_centroid:
        z, cx, cy = bad_centroid[0]
        failures.append(f"section centroid ({_f(cx)}, {_f(cy)}) at Z {_f(z)}, required ({_f(exp_c[0])}, {_f(exp_c[1])})")

    up: V3 = (0.0, 0.0, 1.0)
    lo_need, hi_need = z0 + lin, z1 - lin

    def need_material(pts: list[tuple[float, float]], what: str) -> None:
        for x, y in pts:
            seg = P.segment(P.intervals((x, y, z_m), up), 0.0)
            if seg is None:
                failures.append(f"no material at {_pt((x, y, z_m))} {what}")
                return
            a, b = z_m + seg[0], z_m + seg[1]
            if a > lo_need:
                failures.append(f"material {what} at ({_f(x)}, {_f(y)}) starts at Z {_f(a)}, above the band start Z {_f(z0)}")
                return
            if b < hi_need:
                failures.append(f"material {what} at ({_f(x)}, {_f(y)}) ends at Z {_f(b)}, below the band end Z {_f(z1)}")
                return

    def need_empty(pts: list[tuple[float, float]], what: str) -> None:
        for x, y in pts:
            g = P.gap(P.intervals((x, y, z_m), up), 0.0)
            if g is None:
                failures.append(f"material at {_pt((x, y, z_m))} {what}")
                return
            a, b = z_m + g[0], z_m + g[1]
            if a > lo_need:
                failures.append(f"material {what} at ({_f(x)}, {_f(y)}) reaches up to Z {_f(a)}, above the band start Z {_f(z0)}")
                return
            if b < hi_need:
                failures.append(f"material {what} at ({_f(x)}, {_f(y)}) from Z {_f(b)} up, below the band end Z {_f(z1)}")
                return

    need_material(_ring_points(outer, -delta), "just inside the outer outline")
    need_empty(_ring_points(outer, delta), "just outside the outer outline")
    for i, vd in enumerate(voids):
        dv = tol.probe(_char_radius(vd))
        need_empty(_ring_points(vd, -dv), f"inside void {i + 1} ({_contour_text(vd)})")
        need_material(_ring_points(vd, dv), f"just outside void {i + 1} ({_contour_text(vd)})")

    m = _measure_outline(P, outer, z_m, from_outside=True)
    if m is not None:
        measured["outer"] = {k: _r(v) for k, v in m.items()}
    failures += _outline_failures(outer, m, "outer outline", lin)
    mv = []
    for i, vd in enumerate(voids):
        m = _measure_outline(P, vd, z_m, from_outside=False)
        mv.append({k: _r(v) for k, v in m.items()} if m else None)
        failures += _outline_failures(vd, m, f"void {i + 1}", lin)
    if voids:
        measured["voids"] = mv
    return _item(
        req,
        _dedupe(failures),
        head,
        f"ok (section {exp_area:.1f} mm² at 3 heights)",
        expected={
            "z0": z0,
            "z1": z1,
            "outer": outer.model_dump(),
            "voids": [vd.model_dump() for vd in voids],
            "area": _r(exp_area, 3),
            "centroid": [_r(exp_c[0]), _r(exp_c[1])],
        },
        measured=measured,
        tolerance={"linear_mm": lin, "area_mm2": _r(area_tol, 4), "probe_mm": delta},
    )


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


# --------------------------------------------------------------------------- #
# Coverage: unrequested holes, notes
# --------------------------------------------------------------------------- #


def _covered(a: float, b: float, spans: Sequence[tuple[float, float]], tol: float) -> bool:
    """Whether the union of ``spans`` covers [a, b] (gaps up to ``tol`` allowed)."""
    cur = a
    for lo, hi in sorted(spans):
        if lo - tol > cur:
            break
        cur = max(cur, hi)
    return cur >= b - tol


def _hole_geometry(req: Any) -> tuple[V3, float, list[float], float, float]:
    """(drilling direction, main radius, cylinder radii, depth of the entry feature, reach) of a hole requirement."""
    d = _unit(req.direction)
    r = req.diameter / 2.0
    radii = [r]
    t_top = 0.0
    reach = r
    if req.counterbore is not None:
        radii.append(req.counterbore.diameter / 2.0)
        t_top = req.counterbore.depth
        reach = max(reach, req.counterbore.diameter / 2.0)
    if req.countersink is not None:
        rs = req.countersink.diameter / 2.0
        t_top = (rs - r) / math.tan(math.radians(req.countersink.angle) / 2.0)
        reach = max(reach, rs)
    return d, r, radii, t_top, reach


def _bore_matches(b: _Bore, reqs: Sequence[Any], tol: Tolerance) -> bool:
    """A full round bore is explained by

    - a hole requirement: parallel, its axis within the hole's reach of an entry, on the drilling side of that
      entry, and either starting right at the entry (the required hole, possibly at a wrong size or depth: its
      item fails) or of a required radius (bore / counterbore) and, for a blind hole, not past the floor;
    - circular band voids / pocket outlines around its axis whose Z spans cover the whole bore (a smaller hole
      drilled into the floor of a round pocket is not explained by the pocket)."""
    lin = tol.linear
    cos_tol = math.cos(math.radians(1.0))
    for req in reqs:
        if req.kind != "hole":
            continue
        d, _, radii, t_top, reach = _hole_geometry(req)
        if abs(_dot(d, b.direction)) < cos_tol:
            continue
        for e in req.entries:
            E: V3 = (float(e[0]), float(e[1]), float(e[2]))
            if _axis_distance(E, b.origin, b.direction) > reach + lin:
                continue
            ta, tb = _dot(_sub(b.point(b.t0), E), d), _dot(_sub(b.point(b.t1), E), d)
            lo, hi = min(ta, tb), max(ta, tb)
            if lo < -lin:
                continue  # on the far side of the entry (e.g. drilled from the opposite face)
            if lo <= t_top + lin:
                return True  # the required hole itself, right at its entry (a wrong size or depth fails its item)
            if not req.through and req.depth is not None and hi > req.depth + lin:
                continue  # a deeper bore past the required floor
            if any(abs(b.radius - x) <= lin for x in radii):
                return True
    if abs(b.direction[2]) < cos_tol:
        return False
    zs = sorted((b.point(b.t0)[2], b.point(b.t1)[2]))
    spans: list[tuple[float, float]] = []
    for req in reqs:
        if req.kind == "band":
            circles, lo, hi = [c for c in req.voids if isinstance(c, CircleContour)], float(req.z0), float(req.z1)
        elif req.kind == "pocket" and isinstance(req.contour, CircleContour):
            circles = [req.contour]
            lo = float(req.entry_z - req.depth) if (not req.through and req.depth is not None) else -math.inf
            hi = float(req.entry_z)
        else:
            continue
        for c in circles:
            foot = b.point(_dot(_sub((c.center[0], c.center[1], 0.0), b.origin), b.direction))
            if math.hypot(foot[0] - c.center[0], foot[1] - c.center[1]) <= c.diameter / 2.0 + lin:
                spans.append((lo, hi))
    return _covered(zs[0], zs[1], spans, lin)


def _axis_name(d: Sequence[float]) -> str:
    return "Z" if abs(d[2]) > 0.999 else ("X" if abs(d[0]) > 0.999 else ("Y" if abs(d[1]) > 0.999 else _pt(d)))


def _bore_points(b: _Bore) -> list[V3]:
    """Points on a bore's cylinder (3 stations × 4 azimuths)."""
    u, v = _basis(b.direction)
    L = b.t1 - b.t0
    out = []
    for t in (b.t0 + 0.1 * L, 0.5 * (b.t0 + b.t1), b.t1 - 0.1 * L):
        c = b.point(t)
        for k in range(4):
            a = k * math.pi / 2
            out.append(_add(c, _add(_mul(u, b.radius * math.cos(a)), _mul(v, b.radius * math.sin(a)))))
    return out


def _unrequested_holes(P: _Probe, reqs: Sequence[Any], tol: Tolerance) -> list[UnrequestedFeature]:
    out = []
    shapes = w3.shapes_of(reqs)
    for b in P.holes():
        if _bore_matches(b, reqs, tol):
            continue
        if shapes and w3.explained(_bore_points(b), shapes, tol.linear):
            continue  # a bore of a revolve / prism / sweep / cut requirement (sleeve bore, circular void, round cut)
        p0, p1 = b.point(b.t0), b.point(b.t1)
        length = b.t1 - b.t0
        out.append(
            UnrequestedFeature(
                diameter=_r(2 * b.radius),
                axis=_rv(b.direction, 6),
                start=_rv(p0),
                end=_rv(p1),
                length=_r(length),
                message=(
                    f"unrequested Ø{_f(2 * b.radius)} hole (axis {_axis_name(b.direction)}, from {_pt(p0)} to {_pt(p1)}, "
                    f"{_f(length)} mm long): no requirement asks for it; remove it or cite a source"
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Coverage: unrequested planar recesses (pockets, steps, slots, windows)
# --------------------------------------------------------------------------- #


@dataclass
class _PlaneFace:
    face: Any
    normal: V3  # outward (out of the material)
    center: V3  # a point on the face
    area: float
    lo: V3
    hi: V3


@dataclass
class _Footprint:
    """A horizontal level a requirement explains, with its XY outline (``contour`` model or circle)."""

    z: float | None  # None: any level (a pocket of unknown depth)
    contour: Any = None
    circle: tuple[float, float, float] | None = None  # cx, cy, r


@dataclass
class _WallLine:
    """A straight outline edge a requirement explains as a vertical wall between ``zlo`` and ``zhi``."""

    a: tuple[float, float]
    b: tuple[float, float]
    zlo: float
    zhi: float


@dataclass
class _Floor:
    """The flat floor disc of a hole (or of a bore) at ``point`` across ``direction``, radius ``r``."""

    point: V3
    direction: V3
    r: float


def _planar_faces(P: _Probe) -> list[_PlaneFace]:
    out = []
    exp = TopExp_Explorer(P.solid, TopAbs_FACE)
    while exp.More():
        face = _cast_face(exp.Current())
        exp.Next()
        if BRepAdaptor_Surface(face).GetType() != GeomAbs_Plane:
            continue
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        pnt, nrm = gp_Pnt(), gp_Vec()
        BRepGProp_Face(face).Normal(0.5 * (umin + umax), 0.5 * (vmin + vmax), pnt, nrm)
        if nrm.Magnitude() < 1e-12:
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(face, box, False, False)
        lo, hi = box.CornerMin(), box.CornerMax()
        out.append(_PlaneFace(face=face, normal=_unit((nrm.X(), nrm.Y(), nrm.Z())), center=(pnt.X(), pnt.Y(), pnt.Z()),
                              area=float(props.Mass()), lo=(lo.X(), lo.Y(), lo.Z()), hi=(hi.X(), hi.Y(), hi.Z())))
    return out


def _face_points(face: Any) -> list[V3]:
    """Points on the boundary of a face: ends and middles of its edges."""
    pts: list[V3] = []
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while exp.More():
        try:
            c = BRepAdaptor_Curve(_cast_edge(exp.Current()))
            for t in (c.FirstParameter(), 0.5 * (c.FirstParameter() + c.LastParameter()), c.LastParameter()):
                p = c.Value(t)
                pts.append((p.X(), p.Y(), p.Z()))
        except Exception:  # pragma: no cover - degenerate edge
            pass
        exp.Next()
    return pts


def _interior_points(face: Any, n: int = 4) -> list[V3]:
    """A few points strictly inside a face (UV grid classified against the face)."""
    surf = BRepAdaptor_Surface(face)
    umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
    out: list[V3] = []
    for i in range(n):
        for j in range(n):
            u = umin + (umax - umin) * (i + 0.5) / n
            v = vmin + (vmax - vmin) * (j + 0.5) / n
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_IN:
                p = surf.Value(u, v)
                out.append((p.X(), p.Y(), p.Z()))
    return out


def _contour_edges(c: Any) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if isinstance(c, RectContour):
        cx, cy = c.center
        hw, hl = c.size[0] / 2.0, c.size[1] / 2.0
        q = [(cx - hw, cy - hl), (cx + hw, cy - hl), (cx + hw, cy + hl), (cx - hw, cy + hl)]
        return [(q[i], q[(i + 1) % 4]) for i in range(4)]
    if isinstance(c, PolygonContour):
        pts = [(float(p[0]), float(p[1])) for p in c.points]
        return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
    return []


def _explanations(reqs: Sequence[Any], bores: Sequence[_Bore], tol: Tolerance) -> tuple[list[_Footprint], list[_WallLine], list[_Floor]]:
    """Horizontal levels, straight walls and hole floors the requirements explain, plus the end discs of every
    full round bore (a bore is judged as a hole: matched to a requirement, whose item checks its floor, or
    reported as an unrequested hole)."""
    cos_tol = math.cos(math.radians(tol.angular_deg))
    levels: list[_Footprint] = []
    walls: list[_WallLine] = []
    floors: list[_Floor] = []
    for req in reqs:
        if req.kind == "band":
            for z in (float(req.z0), float(req.z1)):
                levels.append(_Footprint(z=z, contour=req.outer))
            for c in [req.outer, *req.voids]:
                walls += [_WallLine(a, b, float(req.z0), float(req.z1)) for a, b in _contour_edges(c)]
        elif req.kind == "pocket":
            if req.through:
                zlo = -math.inf
            elif req.depth is not None:
                zlo = float(req.entry_z - req.depth)
                levels.append(_Footprint(z=zlo, contour=req.contour))
            else:  # depth unknown (not evaluated): any floor inside the outline
                zlo = -math.inf
                levels.append(_Footprint(z=None, contour=req.contour))
            walls += [_WallLine(a, b, zlo, float(req.entry_z)) for a, b in _contour_edges(req.contour)]
        elif req.kind == "hole":
            d, r, _, _, _ = _hole_geometry(req)
            for e in req.entries:
                E: V3 = (float(e[0]), float(e[1]), float(e[2]))
                ends: list[tuple[float, float]] = []
                if not req.through and req.depth is not None:
                    ends.append((float(req.depth), r))
                if req.counterbore is not None:
                    ends.append((float(req.counterbore.depth), req.counterbore.diameter / 2.0))
                for t, rr in ends:
                    p = _add(E, _mul(d, t))
                    floors.append(_Floor(point=p, direction=d, r=rr))
                    if abs(d[2]) >= cos_tol:
                        levels.append(_Footprint(z=p[2], circle=(p[0], p[1], rr)))
    for b in bores:
        for t in (b.t0, b.t1):
            p = b.point(t)
            floors.append(_Floor(point=p, direction=b.direction, r=b.radius))
            if abs(b.direction[2]) >= cos_tol:
                levels.append(_Footprint(z=p[2], circle=(p[0], p[1], b.radius)))
    return levels, walls, floors


def _offset_polygon(pts: list[tuple[float, float]], g: float) -> list[tuple[float, float]]:
    """Polygon grown outwards by ``g`` (miter joints; small offsets only)."""
    n = len(pts)
    sign = 1.0 if polygon_area(pts) > 0 else -1.0
    out = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        normals = []
        for a, b in ((p0, p1), (p1, p2)):
            ex, ey = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(ex, ey) or 1.0
            normals.append((sign * ey / ln, -sign * ex / ln))
        nx, ny = normals[0][0] + normals[1][0], normals[0][1] + normals[1][1]
        ln = math.hypot(nx, ny)
        if ln < 1e-9:
            out.append(p1)
            continue
        nx, ny = nx / ln, ny / ln
        cos_half = max(0.2, nx * normals[0][0] + ny * normals[0][1])
        out.append((p1[0] + nx * g / cos_half, p1[1] + ny * g / cos_half))
    return out


def _xy_face(z: float, *, contour: Any = None, circle: tuple[float, float, float] | None = None, grow: float = 0.0) -> Any:
    """A planar face at height ``z`` with a requirement outline (grown by ``grow``)."""
    if circle is not None or isinstance(contour, CircleContour):
        cx, cy, r = circle if circle is not None else (contour.center[0], contour.center[1], contour.diameter / 2.0)
        circ = gp_Circ(gp_Ax2(gp_Pnt(cx, cy, z), gp_Dir(0.0, 0.0, 1.0)), r + grow)
        wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire()
        return BRepBuilderAPI_MakeFace(wire, True).Face()
    if isinstance(contour, RectContour):
        cx, cy = contour.center
        hw, hl = contour.size[0] / 2.0 + grow, contour.size[1] / 2.0 + grow
        cr = contour.corner_radius + grow if contour.corner_radius > 1e-9 else 0.0
        if cr <= 1e-9:
            return _polygon_face([(cx - hw, cy - hl), (cx + hw, cy - hl), (cx + hw, cy + hl), (cx - hw, cy + hl)], z)
        mk = BRepBuilderAPI_MakeWire()
        corners = [(cx + hw - cr, cy + hl - cr, 0.0), (cx - hw + cr, cy + hl - cr, 90.0), (cx - hw + cr, cy - hl + cr, 180.0),
                   (cx + hw - cr, cy - hl + cr, 270.0)]
        pts: list[gp_Pnt] = []
        for ox, oy, a0 in corners:
            for a in (a0, a0 + 90.0):
                t = math.radians(a)
                pts.append(gp_Pnt(ox + cr * math.cos(t), oy + cr * math.sin(t), z))
        for i, (ox, oy, a0) in enumerate(corners):
            circ = gp_Circ(gp_Ax2(gp_Pnt(ox, oy, z), gp_Dir(0.0, 0.0, 1.0)), cr)
            mk.Add(BRepBuilderAPI_MakeEdge(circ, pts[2 * i], pts[2 * i + 1]).Edge())
            nxt = pts[(2 * i + 2) % 8]
            if pts[2 * i + 1].Distance(nxt) > 1e-9:
                mk.Add(BRepBuilderAPI_MakeEdge(pts[2 * i + 1], nxt).Edge())
        return BRepBuilderAPI_MakeFace(mk.Wire(), True).Face()
    pts2 = [(float(p[0]), float(p[1])) for p in contour.points]
    if grow:
        pts2 = _offset_polygon(pts2, grow)
    return _polygon_face(pts2, z)


def _polygon_face(pts: list[tuple[float, float]], z: float) -> Any:
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in pts:
        poly.Add(gp_Pnt(x, y, z))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()


def _shape_list(shapes: Sequence[Any]) -> Any:
    out = List_TopoDS_Shape()
    for s in shapes:
        out.Append(s)
    return out


def _cut(a: Any, tools: Sequence[Any]) -> Any:
    """``a`` minus every tool (a tool list, not a compound: overlapping tools in one compound are not
    subtracted reliably)."""
    op = BRepAlgoAPI_Cut()
    op.SetArguments(_shape_list([a]))
    op.SetTools(_shape_list(tools))
    op.SetFuzzyValue(1e-6)
    op.Build()
    if not op.IsDone():
        raise RuntimeError("boolean cut failed")
    return op.Shape()


def _shape_props(shape: Any) -> tuple[float, float, Bnd_Box]:
    """(area, perimeter, bbox) of a face set."""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    lprops = GProp_GProps()
    BRepGProp.LinearProperties_s(shape, lprops)
    box = Bnd_Box()
    if props.Mass() > 0:
        BRepBndLib.AddOptimal_s(shape, box, False, False)
    return float(props.Mass()), float(lprops.Mass()), box


def _box_list(box: Bnd_Box) -> list[float]:
    lo, hi = box.CornerMin(), box.CornerMax()
    return _rv((lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()), 3)


def _box_text(b: Sequence[float]) -> str:
    return f"X {_f(b[0])}..{_f(b[3])}, Y {_f(b[1])}..{_f(b[4])}, Z {_f(b[2])}..{_f(b[5])}"


def _floor_explains(f: _Floor, face: _PlaneFace, pts: Sequence[V3], tol: Tolerance) -> bool:
    if abs(_dot(face.normal, f.direction)) < math.cos(math.radians(tol.angular_deg)):
        return False
    if abs(_dot(_sub(face.center, f.point), f.direction)) > tol.linear:
        return False
    return all(_axis_distance(p, f.point, f.direction) <= f.r + tol.linear for p in pts)


def _walls_explain(lines: Sequence[_WallLine], face: _PlaneFace, pts: Sequence[V3], tol: Tolerance) -> bool:
    lin = tol.linear
    sin_tol = math.sin(math.radians(1.0))
    nx, ny = face.normal[0], face.normal[1]
    zs = [p[2] for p in pts]
    spans = []
    for w in lines:
        ex, ey = w.b[0] - w.a[0], w.b[1] - w.a[1]
        length = math.hypot(ex, ey)
        if length < 1e-9:
            continue
        ux, uy = ex / length, ey / length
        if abs(nx * ux + ny * uy) > sin_tol:
            continue
        # every boundary point of the face on the edge line, within the edge
        ok = True
        for p in pts:
            rx, ry = p[0] - w.a[0], p[1] - w.a[1]
            s = rx * ux + ry * uy
            if abs(rx * uy - ry * ux) > lin or s < -lin or s > length + lin:
                ok = False
                break
        if ok:
            spans.append((w.zlo, w.zhi))
    return bool(spans) and _covered(min(zs), max(zs), spans, lin)


def _unrequested_recesses(P: _Probe, reqs: Sequence[Any], tol: Tolerance) -> list[UnrequestedFeature]:
    """Planar faces that make a recess no requirement explains: horizontal faces strictly between the part's Z
    min and max (an unrequested pocket floor, step or ledge) and vertical walls facing material across empty
    space (an unrequested slot or window), unless they are pocket floors, band boundaries, blind-hole or
    counterbore floors, pocket / band outline walls, or end discs of round bores (judged as holes)."""
    lin = tol.linear
    cos_tol = math.cos(math.radians(tol.angular_deg))
    sin_tol = math.sin(math.radians(tol.angular_deg))
    levels, lines, floors = _explanations(reqs, P.holes(), tol)
    out: list[UnrequestedFeature] = []
    walls_found: list[tuple[_PlaneFace, float]] = []
    shapes = w3.shapes_of(reqs)
    for pf in _planar_faces(P):
        n = pf.normal
        if shapes and w3.explained(_face_points(pf.face) + _interior_points(pf.face, 3), shapes, lin):
            continue  # a face on the boundary of a prism / revolve / sweep / cut requirement
        if abs(n[2]) >= cos_tol:
            z = pf.center[2]
            if not (P.lo[2] + lin < z < P.hi[2] - lin):
                continue
            here = [fp for fp in levels if fp.z is None or abs(fp.z - z) <= lin]
            try:
                if here:
                    rest = _cut(pf.face, [_xy_face(z, contour=fp.contour, circle=fp.circle, grow=lin) for fp in here])
                    area, perim, box = _shape_props(rest)
                else:
                    rest, (area, perim, box) = pf.face, _shape_props(pf.face)
            except Exception as exc:  # pragma: no cover - OCCT failure: judge the whole face by its bbox
                log.warning("recess check boolean failed at Z %.3f: %s", z, exc)
                continue
            if area <= max(1e-4, 0.5 * lin * perim):
                continue
            bb = _box_list(box)
            side = "floor (faces up)" if n[2] > 0 else "ceiling (faces down)"
            out.append(UnrequestedFeature(
                kind="recess_face", z=_r(z), area=_r(area, 3), bbox=bb,
                message=(f"unrequested recess: a horizontal {side} at Z {_f(z)} ({area:.1f} mm² at X {_f(bb[0])}..{_f(bb[3])}, "
                         f"Y {_f(bb[1])}..{_f(bb[4])}) is no pocket floor, band boundary, blind-hole or counterbore floor of any "
                         "requirement (an invented pocket, step or ledge?); remove it or add the requirement its source states"),
            ))
        elif abs(n[2]) <= sin_tol:
            walls_found.append((pf, pf.area))
    floors_found = [u for u in out if u.bbox is not None and u.z is not None]
    bad_walls: list[tuple[list[float], float]] = []
    for pf, area in walls_found:
        n = pf.normal
        if any(u.bbox[0] - lin <= pf.lo[0] and pf.hi[0] <= u.bbox[3] + lin and u.bbox[1] - lin <= pf.lo[1]
               and pf.hi[1] <= u.bbox[4] + lin and min(abs(pf.lo[2] - u.z), abs(pf.hi[2] - u.z)) <= lin
               for u in floors_found if u.bbox is not None and u.z is not None):
            continue  # a wall of a recess already reported by its floor / ceiling
        inner = _interior_points(pf.face)
        if not inner:
            continue
        delta = tol.probe_min
        recess = False
        for p in inner:
            start = _add(p, _mul(n, delta))
            if P.inside(start):
                continue
            hit = P.ray(start, n, P.reach(start))
            if hit is not None:
                recess = True
                break
        if not recess:
            continue
        pts = _face_points(pf.face)
        if any(_floor_explains(f, pf, pts, tol) for f in floors) or _walls_explain(lines, pf, pts, tol):
            continue
        bad_walls.append(([*pf.lo, *pf.hi], area))
    # walls that touch each other are one recess (the four walls of a window): one report each
    groups: list[tuple[list[float], float, int]] = []
    for box, area in bad_walls:
        merged = True
        cur = (list(box), area, 1)
        while merged:
            merged = False
            for g in groups:
                gb = g[0]
                if all(cur[0][i] <= gb[i + 3] + lin and gb[i] <= cur[0][i + 3] + lin for i in range(3)):
                    groups.remove(g)
                    cur = ([min(cur[0][i], gb[i]) for i in range(3)] + [max(cur[0][i + 3], gb[i + 3]) for i in range(3)],
                           cur[1] + g[1], cur[2] + g[2])
                    merged = True
                    break
        groups.append(cur)
    for box, area, count in groups:
        bb = _rv(box, 3)
        walls_text = "a vertical wall" if count == 1 else f"{count} vertical walls"
        out.append(UnrequestedFeature(
            kind="recess_wall", area=_r(area, 3), bbox=bb,
            message=(f"unrequested recess: {walls_text} ({area:.1f} mm² at {_box_text(bb)}) face material across a gap and lie "
                     "on no pocket or band outline of any requirement (an invented slot or window?); remove it or add the "
                     "requirement its source states"),
        ))
    return out


# --------------------------------------------------------------------------- #
# Coverage: volume accounting against the requirement list
# --------------------------------------------------------------------------- #


def _cyl_groups(P: _Probe, skip: Any = None) -> list[tuple[float, V3, V3, bool, float]]:
    """Every cylindrical face as (radius, axis direction, axis origin, concave, coverage of its coaxial group);
    faces for which ``skip(face)`` is true are left out."""
    faces: list[tuple[float, V3, V3, bool, float, float, float]] = []
    exp = TopExp_Explorer(P.solid, TopAbs_FACE)
    while exp.More():
        face = _cast_face(exp.Current())
        exp.Next()
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        if skip is not None and skip(face):
            continue
        cyl = surf.Cylinder()
        ax = cyl.Axis()
        r = float(cyl.Radius())
        ds = (ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        ls = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        pnt, nrm = gp_Pnt(), gp_Vec()
        BRepGProp_Face(face).Normal(0.5 * (umin + umax), 0.5 * (vmin + vmax), pnt, nrm)
        p = (pnt.X(), pnt.Y(), pnt.Z())
        radial = _sub(p, _add(ls, _mul(ds, _dot(_sub(p, ls), ds))))
        concave = _dot((nrm.X(), nrm.Y(), nrm.Z()), radial) < 0
        d = _unit(ds)
        dom = max(range(3), key=lambda i: abs(d[i]))
        if d[dom] < 0:
            d = _mul(d, -1.0)
        origin = _sub(ls, _mul(d, _dot(ls, d)))
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        ta = _dot(_sub(_add(ls, _mul(_unit(ds), vmin)), origin), d)
        tb = _dot(_sub(_add(ls, _mul(_unit(ds), vmax)), origin), d)
        faces.append((r, d, origin, concave, min(ta, tb), max(ta, tb), float(props.Mass())))
    groups: dict[tuple, list[int]] = {}
    for i, (r, d, o, concave, *_rest) in enumerate(faces):
        groups.setdefault((tuple(_r(c, 4) for c in d), tuple(_r(c, 3) for c in o), _r(r, 4), concave), []).append(i)
    out = []
    for idx in groups.values():
        r, d, o, concave = faces[idx[0]][:4]
        t0 = min(faces[i][4] for i in idx)
        t1 = max(faces[i][5] for i in idx)
        length = max(t1 - t0, 1e-9)
        # per contiguous run (see _Probe.bores)
        runs: list[list[int]] = []
        for i in sorted(idx, key=lambda i: faces[i][4]):
            if runs and faces[i][4] <= max(faces[j][5] for j in runs[-1]) + 1e-6:
                runs[-1].append(i)
            else:
                runs.append([i])
        for run in runs:
            t0 = min(faces[i][4] for i in run)
            t1 = max(faces[i][5] for i in run)
            length = max(t1 - t0, 1e-9)
            cov = sum(faces[i][6] for i in run) / (2 * math.pi * r * length)
            out += [(r, d, o, concave, cov)] * len(run)
    return out


def _hole_cylinder(face: Any, holes: Sequence[Any], lin: float) -> bool:
    """A cylindrical face whose axis and radius are those of a hole requirement (bore or counterbore)."""
    surf = BRepAdaptor_Surface(face)
    if surf.GetType() != GeomAbs_Cylinder:
        return False
    cyl = surf.Cylinder()
    ax = cyl.Axis()
    d = _unit((ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()))
    o = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
    r = float(cyl.Radius())
    cos_tol = math.cos(math.radians(1.0))
    for h in holes:
        hd, hr, radii, _, _ = _hole_geometry(h)
        if abs(_dot(hd, d)) < cos_tol or not any(abs(r - x) <= lin for x in radii):
            continue
        if any(_axis_distance((float(e[0]), float(e[1]), float(e[2])), o, d) <= lin for e in h.entries):
            return True
    return False


FREEFORM_SHARE = 0.25  # a free-form face with more of the part's area than this is a shape of its own, not an edge break


def _unrequested_surfaces(P: _Probe, reqs: Sequence[Any], tol: Tolerance) -> list[UnrequestedFeature]:
    """Large free-form faces (B-spline and other non-analytic surfaces: a coil's wire, a swept bar) that no W3
    requirement's boundary explains. A fillet or blend is small; a face carrying a quarter of the part's area is the
    part's shape, and an envelope alone does not state it (without this the edge-break rule would skip the volume
    accounting and let it pass)."""
    total = GProp_GProps()
    BRepGProp.SurfaceProperties_s(P.solid, total)
    area_all = float(total.Mass())
    shapes = w3.shapes_of(reqs)
    out: list[UnrequestedFeature] = []
    exp = TopExp_Explorer(P.solid, TopAbs_FACE)
    while exp.More():
        face = _cast_face(exp.Current())
        exp.Next()
        if BRepAdaptor_Surface(face).GetType() in (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus):
            continue
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = float(props.Mass())
        if area_all <= 0 or area < FREEFORM_SHARE * area_all:
            continue
        if shapes and w3.explained(_face_points(face) + _interior_points(face, 3), shapes, tol.linear):
            continue
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(face, box, False, False)
        bb = _box_list(box)
        out.append(UnrequestedFeature(kind="surface", area=_r(area, 3), bbox=bb,
                                      message=(f"unrequested free-form surface ({100 * area / area_all:.0f}% of the part's area) at "
                                               f"{_box_text(bb)}: a coil, swept wire or curved shape that no requirement states; "
                                               "add the coil (or sweep / revolve) requirement its source states")))
    return out


def _edge_breaks(P: _Probe, reqs: Sequence[Any], tol: Tolerance) -> list[str]:
    """Faces that look like fillets or chamfers (no requirement kind describes edge breaks, so their material
    cannot be accounted for): curved faces other than cylinders and cones, partial cylinders that are no rounded
    rectangle corner of a requirement, cones that are no countersink, tilted planes, and vertical planes at an
    angle that lie on no requirement polygon edge."""
    lin = tol.linear
    cos_tol = math.cos(math.radians(tol.angular_deg))
    sin_tol = math.sin(math.radians(tol.angular_deg))
    corner_radii: list[float] = []
    lines: list[_WallLine] = []
    sinks: list[tuple[V3, V3]] = []
    for req in reqs:
        contours = []
        if req.kind == "pocket":
            contours = [req.contour]
        elif req.kind == "band":
            contours = [req.outer, *req.voids]
        elif req.kind == "hole" and req.countersink is not None:
            d = _unit(req.direction)
            sinks += [((float(e[0]), float(e[1]), float(e[2])), d) for e in req.entries]
        for c in contours:
            if isinstance(c, RectContour) and c.corner_radius > 1e-9:
                corner_radii.append(c.corner_radius)
            if isinstance(c, PolygonContour):
                lines += [_WallLine(a, b, -math.inf, math.inf) for a, b in _contour_edges(c)]
    shapes = w3.shapes_of(reqs)
    holes = [r for r in reqs if r.kind == "hole"]
    skip = None
    if shapes or holes:
        # Requested shape, not an edge break: faces on a prism / revolve / sweep / cut boundary (arcs, cones, tilted
        # planes), and the bore of a hole requirement cut short by an inclined face (its coverage is below 360°).

        def skip(face: Any) -> bool:
            if holes and _hole_cylinder(face, holes, lin):
                return True
            return bool(shapes) and w3.explained(_face_points(face) + _interior_points(face, 3), shapes, lin)

    reasons: list[str] = []
    for r, d, o, concave, cov in _cyl_groups(P, skip):
        if cov >= 0.98:
            continue
        if abs(d[2]) >= cos_tol and any(abs(r - cr) <= lin for cr in corner_radii):
            continue
        reasons.append(f"partial cylinder R{_f(r)} (axis {_axis_name(d)}): a fillet?")
    exp = TopExp_Explorer(P.solid, TopAbs_FACE)
    while exp.More():
        face = _cast_face(exp.Current())
        exp.Next()
        surf = BRepAdaptor_Surface(face)
        kind = surf.GetType()
        if kind in (GeomAbs_Cylinder, GeomAbs_Plane):
            continue
        if skip is not None and skip(face):
            continue
        if kind == GeomAbs_Cone:
            ax = surf.Cone().Axis()
            o = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
            dd = (ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
            if any(abs(_dot(dd, d)) >= cos_tol and _axis_distance(e, o, _unit(dd)) <= lin for e, d in sinks):
                continue
            reasons.append("conical face that is no countersink: a chamfer?")
        else:
            reasons.append(f"curved face ({getattr(kind, 'name', kind)}): a fillet?")
    for pf in _planar_faces(P):
        n = pf.normal
        if abs(n[2]) >= cos_tol:
            continue
        if skip is not None and skip(pf.face):
            continue
        if abs(n[2]) > sin_tol:
            reasons.append(f"tilted planar face at {_pt(pf.center)}: a chamfer?")
            continue
        if max(abs(n[0]), abs(n[1])) >= cos_tol:
            continue
        if not _walls_explain(lines, pf, _face_points(pf.face), tol):
            reasons.append(f"angled vertical face at {_pt(pf.center)}: a chamfer?")
    return _dedupe(reasons)


def _reference_solid(P: _Probe, reqs: Sequence[Any], tol: Tolerance, *, letters: bool = True) -> Any:
    """The solid the requirement list implies: the part's bounding box (or the band prisms where bands are
    stated) minus every hole, counterbore, countersink and pocket. Built with OCP primitives from the
    requirements alone (never from the ops). ``letters=False`` leaves the text requirements out."""
    lin = tol.linear
    zlo, zhi = P.lo[2], P.hi[2]
    bands = sorted((r for r in reqs if r.kind == "band"), key=lambda r: r.z0)
    for a, b in zip(bands, bands[1:]):
        if b.z0 < a.z1 - lin:
            raise ValueError(f"bands {a.id} and {b.id} overlap in Z")
    shapes = [s for s in w3.shapes_of(reqs) if letters or not isinstance(s, w3.TextShape)]
    mats = [s for s in shapes if not s.removal and not s.addition]
    pieces = []
    cur = zlo
    spans = [(max(float(b.z0), zlo), min(float(b.z1), zhi)) for b in bands]
    for (a, b), band in zip(spans + [(zhi, zhi)], bands + [None]):
        if a - cur > lin and not mats:  # with W3 material requirements the material is stated: no box filler
            pieces.append(BRepPrimAPI_MakeBox(gp_Pnt(P.lo[0], P.lo[1], cur), gp_Pnt(P.hi[0], P.hi[1], a)).Shape())
        if band is not None and b - a > lin:
            prism = BRepPrimAPI_MakePrism(_xy_face(a, contour=band.outer), gp_Vec(0.0, 0.0, b - a)).Shape()
            voids = [BRepPrimAPI_MakePrism(_xy_face(a - 1.0, contour=v), gp_Vec(0.0, 0.0, b - a + 2.0)).Shape() for v in band.voids]
            pieces.append(_cut(prism, voids) if voids else prism)
        cur = max(cur, b)
    pieces += [m.solid(P.diag + 10.0) for m in mats]
    if not pieces:
        raise ValueError("no material to account for")
    base = pieces[0]
    for p in pieces[1:]:
        op = BRepAlgoAPI_Fuse(base, p)
        op.SetFuzzyValue(1e-6)
        op.Build()
        if not op.IsDone():
            raise RuntimeError("boolean fuse failed")
        base = op.Shape()
    reach = P.diag + 10.0
    tools = []
    for req in reqs:
        if req.kind == "hole":
            d, r, _, _, r_max = _hole_geometry(req)
            for e in req.entries:
                E: V3 = (float(e[0]), float(e[1]), float(e[2]))
                # an inclined entry face rises r·tanθ over the hole's edge: start the tool beyond it (no lip)
                backoff = 1.0 + _entry_rise(P, E, d, r_max)
                start = _sub(E, _mul(d, backoff))
                length = backoff + (reach if req.through else float(req.depth))
                tools.append(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start), gp_Dir(*d)), r, length).Shape())
                if req.counterbore is not None:
                    tools.append(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start), gp_Dir(*d)), req.counterbore.diameter / 2.0,
                                                          backoff + req.counterbore.depth).Shape())
                if req.countersink is not None:
                    rs = req.countersink.diameter / 2.0
                    h = (rs - r) / math.tan(math.radians(req.countersink.angle) / 2.0)
                    tools.append(BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(*E), gp_Dir(*d)), rs, r, h).Shape())
                    tools.append(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start), gp_Dir(*d)), rs, backoff).Shape())
        elif req.kind == "pocket":
            z0 = zlo - 1.0 if req.through else float(req.entry_z - req.depth)
            tools.append(BRepPrimAPI_MakePrism(_xy_face(z0, contour=req.contour), gp_Vec(0.0, 0.0, zhi + 1.0 - z0)).Shape())
    tools += [c.solid(reach) for c in shapes if c.removal]
    out = _cut(base, tools) if tools else base
    for a in (s for s in shapes if s.addition):  # raised letters (generation reliability M), after the removals
        op = BRepAlgoAPI_Fuse(out, a.solid(reach))
        op.SetFuzzyValue(1e-6)
        op.Build()
        if not op.IsDone():
            raise RuntimeError("boolean fuse failed")
        out = op.Shape()
    return out


def _entry_rise(P: _Probe, E: V3, d: V3, radius: float) -> float:
    """How far the entry face of a hole rises over the hole's widest radius (r · tan θ, θ between the drilling
    direction and the face normal): the planar face through the entry point nearest to it (the entry itself is in
    the drilled void). 0 for a perpendicular entry or when no planar face passes through the entry."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*E)).Vertex()
    best: tuple[float, V3] | None = None
    for pf in _planar_faces(P):
        if abs(_dot(pf.normal, _sub(E, pf.center))) > 1e-3:
            continue
        dist = BRepExtrema_DistShapeShape(vertex, pf.face)
        if dist.IsDone() and dist.Value() <= radius + 1.0 and (best is None or dist.Value() < best[0]):
            best = (dist.Value(), pf.normal)
    if best is None:
        return 0.0
    c = abs(_dot(best[1], d))
    if c > math.cos(math.radians(0.01)):
        return 0.0
    return radius * math.sqrt(max(0.0, 1.0 - c * c)) / max(c, 0.05)


def _solids_of(shape: Any) -> list[Any]:
    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def _volume(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def _account(P: _Probe, reqs: Sequence[Any], tol: Tolerance) -> tuple[VolumeAccounting, list[UnrequestedFeature]]:
    """Material accounting: the solid against the reference solid of the requirement list. Pieces of the
    difference thicker than the linear tolerance are unaccounted material (slivers within the tolerance of
    passed items are not). Skipped when fillets / chamfers are present (no requirement kind states them)."""
    lin = tol.linear
    measured = _volume(P.solid)
    breaks = _edge_breaks(P, reqs, tol)
    if breaks:
        return VolumeAccounting(status="skipped", reason="edge breaks present: " + "; ".join(breaks[:4]), measured_mm3=_r(measured, 3)), []
    found: list[UnrequestedFeature] = []
    try:
        # text requirements (generation reliability M): booleans between thousands of identical glyph faces take
        # minutes, so the letters' slabs are masked out of both solids and the letters are accounted on their own
        # (material inside the masks: measured vs glyph area × depth)
        texts = [s for s in w3.shapes_of(reqs) if isinstance(s, w3.TextShape)]
        ref = _reference_solid(P, reqs, tol, letters=not texts)
        solid = P.solid
        expected = _volume(ref)
        if texts:
            masks = [t.mask() for t in texts]
            ref_v = expected
            ref, solid = _cut(ref, masks), _cut(P.solid, masks)
            want = sum(t.volume() for t in texts)
            got = (measured - _volume(solid)) - (ref_v - _volume(ref))
            expected = ref_v + want
            if abs(got - want) > max(1e-3, 0.005 * abs(want)):
                found.append(UnrequestedFeature(kind="volume", volume=_r(got - want, 3), message=(
                    f"lettering: the letters hold {got:.4g} mm³ of material, the text requirements imply {want:.4g} mm³ "
                    f"({', '.join(t.req.id for t in texts)}); check the text, character height, depth and font of the text ops")))
        missing = _solids_of(_cut(ref, [solid]))
        extra = _solids_of(_cut(solid, [ref]))
    except Exception as exc:  # pragma: no cover - OCCT failure on unusual geometry
        log.warning("volume accounting failed: %s", exc)
        return VolumeAccounting(status="not_evaluated", reason=f"reference solid failed: {exc}", measured_mm3=_r(measured, 3)), []
    removed = added = 0.0
    for pieces, what in ((missing, "removed"), (extra, "added")):
        for s in pieces:
            v = _volume(s)
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(s, props)
            area = float(props.Mass())
            if v < 1e-3 or area <= 0 or v / area <= 0.5 * lin:
                continue  # a sliver within the tolerance of the checked features
            box = Bnd_Box()
            BRepBndLib.AddOptimal_s(s, box, False, False)
            bb = _box_list(box)
            if what == "removed":
                removed += v
                msg = (f"unaccounted removal: {v:.1f} mm³ of material at {_box_text(bb)} is missing although no requirement "
                       "removes it (an unrequested pocket, slot, step or window?); remove the cut or add the requirement its "
                       "source states")
            else:
                added += v
                msg = (f"unaccounted material: {v:.1f} mm³ at {_box_text(bb)} that no requirement implies (an unrequested boss, "
                       "rib or a missing cut?); remove it or add the requirement its source states")
            found.append(UnrequestedFeature(kind="volume", volume=_r(v, 3), bbox=bb, message=msg))
    acc = VolumeAccounting(status="failed" if found else "passed", measured_mm3=_r(measured, 3), expected_mm3=_r(expected, 3),
                           unaccounted_removed_mm3=_r(removed, 3), unaccounted_added_mm3=_r(added, 3),
                           reason="" if found else "the solid equals the requirement list's material within the tolerance")
    return acc, found[:8]


def _notes(reqs: Sequence[Any]) -> NotesCoverage:
    cov = NotesCoverage()
    for req in reqs:
        if req.kind == "note":
            if req.category == "thread":
                cov.uncovered.append(
                    NoteEntry(requirement_id=req.id, category=req.category, text=req.text, reason="threads are not modelled as geometry")
                )
            else:
                cov.unknown.append(
                    NoteEntry(
                        requirement_id=req.id, category=req.category, text=req.text, reason="not geometry; coverage is decided by the drawing"
                    )
                )
        elif req.kind == "hole" and req.thread:
            cov.uncovered.append(
                NoteEntry(
                    requirement_id=req.id,
                    category="thread",
                    text=f"{req.thread} on Ø{req.diameter:g} × {len(req.entries)}",
                    reason="threads are not modelled as geometry",
                )
            )
    return cov


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _check_w3(P: _Probe, req: Any, reqs: Sequence[Any], tol: Tolerance) -> CheckItem:
    """A prism / revolve / sweep / cut requirement: the solid against the occupancy the whole list implies, just
    inside and just outside every outline edge (lines and arcs) at several stations, inside the region, at the end
    faces and the floor. Probes whose expectation changes within ¾ of the offset (corners, touching features) are
    skipped; every remaining mismatch is a failure with its position."""
    shape = w3.shapes_of([req])[0]
    occ = w3.occupancy([r for r in reqs if r.kind in GEOMETRIC_KINDS], P.lo, P.hi)
    delta = max(tol.probe_min, 2 * tol.linear)
    if req.kind == "text":  # letters are thin (0.15 mm etching): probes stay inside the slab and the strokes
        delta = max(0.01, min(delta, abs(float(req.depth)) / 3.0))
    reach = P.diag + 10.0
    checked = 0
    bad: list[str] = []
    points: list[tuple[V3, str]] = []
    for a, b, what in shape.probe_pairs(delta, reach):
        points += [(a, f"just inside {what}"), (b, f"just outside {what}")]
    points += shape.samples(reach)
    inside = P.inside
    if req.kind == "coil":  # long swept B-spline faces: the classifier alone misjudges some points
        from .coil_occ import RobustInside

        inside = RobustInside(P.solid)
    for p, what in points:
        want = w3.certain(occ, p, 0.75 * delta)
        if want is None:
            continue
        checked += 1
        got = inside(p)
        if got != want:
            bad.append(f"{what} at {_pt(p)}: expected {'material' if want else 'empty'}, found {'material' if got else 'empty'}")
    desc = {"prism": "prism", "revolve": "revolved section", "sweep": "swept section", "cut": "cut", "coil": "coil",
            "text": "lettering"}[req.kind]
    head = f"{req.id} {desc}"
    failures = bad[:6] + ([f"… and {len(bad) - 6} more probe mismatches"] if len(bad) > 6 else [])
    if checked == 0:
        msg = f"{head} not evaluated: no unambiguous probe point (the requirement overlaps others everywhere?)"
        return CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=msg)
    return _item(req, failures, head, f"{checked} probes match (edges, inside, end faces / floor)",
                 expected={"probe_offset_mm": delta}, measured={"probes": checked, "mismatches": len(bad)},
                 tolerance={"probe_offset": delta})


def _check_one(P: _Probe, req: Any, tol: Tolerance, reqs: Sequence[Any] = ()) -> list[CheckItem]:
    if req.kind in w3.W3_KINDS:
        if req.kind == "cut" and not req.through and req.depth is None:
            return [CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=f"{req.id} cut not evaluated: depth unknown")]
        if req.kind == "coil" and req.pitch is None and req.length is None:
            return [CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated",
                              message=f"{req.id} coil not evaluated: pitch / length unknown")]
        if req.kind == "text":
            shape = w3.TextShape(req)
            if shape.error is not None or not len(shape.segs):
                return [CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated",
                                  message=f"{req.id} lettering not evaluated: {shape.error or 'the font gives no outline'}")]
        return [_check_w3(P, req, reqs or [req], tol)]
    if req.kind == "envelope":
        return [_check_envelope(P, req, tol)]
    if req.kind == "hole":
        if not req.entries or (not req.through and req.depth is None):
            missing = "entries" if not req.entries else "depth"
            msg = f"{req.id} hole Ø{req.diameter:g} not evaluated: {missing} unknown"
            return [CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=msg)]
        return [_check_hole(P, req, k, e, tol) for k, e in enumerate(req.entries)]
    if req.kind == "pocket":
        if not req.through and req.depth is None:
            msg = f"{req.id} pocket not evaluated: depth unknown"
            return [CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=msg)]
        return [_check_pocket(P, req, tol)]
    return [_check_band(P, req, tol)]


class SurveyCylinder(BaseModel):
    """A full (360°) round cylinder of the solid: a bore (``concave``) or a boss / round outline."""

    diameter: float
    direction: list[float]  # unit, dominant component positive
    point: list[float]  # a point on the axis
    t0: float
    t1: float
    concave: bool


class SurveyOutline(BaseModel):
    """An inner outline of a horizontal planar face that is not a single circle (pocket rim, through opening, boss
    footprint): its XY bounding box ``[x0, y0, x1, y1]`` and the face's Z."""

    z: float
    bbox: list[float]


class FeatureSurvey(BaseModel):
    """What a drawing of this solid must dimension, found on the STEP bytes alone (no requirement list): the
    annotation check (cad/annotation_check.py) requires a Ø per hole group and round boss, a size and a datum
    position per inner outline, and the datum positions of every hole."""

    error: str | None = None
    bbox_min: list[float] | None = None
    bbox_max: list[float] | None = None
    holes: list[SurveyCylinder] = Field(default_factory=list)
    bosses: list[SurveyCylinder] = Field(default_factory=list)
    outlines: list[SurveyOutline] = Field(default_factory=list)


def survey_features(step_bytes: bytes, lettering: Sequence[Any] = (), *, require_complete_bores: bool = False) -> FeatureSurvey:
    """Full round bores and bosses and the inner outlines of horizontal faces of the one solid in ``step_bytes``.
    ``lettering``: regions of the part's text ops (:func:`app.cad.text_geometry.text_regions`); faces and outlines
    inside them are letters, never features to dimension (generation reliability M).
    ``require_complete_bores`` conservatively refuses a manufacturing depth survey if any cylindrical
    wall is incomplete: omitting an interrupted far wall could shorten the apparent drilling reach.
    Default drawing surveys continue to report only full round bores."""
    solid, summary, error = _load_solid(step_bytes)
    if error is not None:
        return FeatureSurvey(error=error)
    P = _Probe(solid)
    if require_complete_bores and any(b.coverage < 0.98 for b in P.bores()):
        return FeatureSurvey(error="incomplete cylindrical walls cannot establish drilling reach")
    out = FeatureSurvey(bbox_min=_rv(P.lo, 6), bbox_max=_rv(P.hi, 6))
    for b in P.holes():
        out.holes.append(SurveyCylinder(diameter=_r(2 * b.radius, 6), direction=_rv(b.direction, 9), point=_rv(b.origin, 6),
                                        t0=_r(b.t0, 6), t1=_r(b.t1, 6), concave=True))
    seen: set[tuple] = set()
    for r, d, o, concave, cov in _cyl_groups(P):
        key = (_r(r, 4), tuple(_r(c, 4) for c in d), tuple(_r(c, 3) for c in o))
        if concave or cov < 0.98 or key in seen:
            continue
        seen.add(key)
        out.bosses.append(SurveyCylinder(diameter=_r(2 * r, 6), direction=_rv(d, 9), point=_rv(o, 6), t0=0.0, t1=0.0, concave=False))
    cos_tol = math.cos(math.radians(0.01))
    boxes: set[tuple[float, ...]] = set()
    def lettered(points: Sequence[Any]) -> bool:
        return bool(lettering) and bool(points) and any(all(r.contains(q) for q in points) for r in lettering)

    for pf in _planar_faces(P):
        if abs(pf.normal[2]) < cos_tol:
            continue
        if lettered(_face_points(pf.face)):
            continue
        outer = BRepTools.OuterWire_s(pf.face)
        exp = TopExp_Explorer(pf.face, TopAbs_WIRE)
        while exp.More():
            wire = exp.Current()
            exp.Next()
            if wire.IsSame(outer) or lettered(_face_points(wire)):
                continue
            circles = set()
            straight = False
            eexp = TopExp_Explorer(wire, TopAbs_EDGE)
            while eexp.More():
                c = BRepAdaptor_Curve(_cast_edge(eexp.Current()))
                eexp.Next()
                if c.GetType() == GeomAbs_Circle:
                    circ = c.Circle()
                    loc = circ.Location()
                    circles.add((_r(loc.X(), 4), _r(loc.Y(), 4), _r(circ.Radius(), 4)))
                else:
                    straight = True
            if not straight and len(circles) == 1:
                continue  # a round hole / pocket / boss footprint: a cylinder of the survey
            box = Bnd_Box()
            BRepBndLib.AddOptimal_s(wire, box, False, False)
            lo, hi = box.CornerMin(), box.CornerMax()
            bb = (_r(lo.X(), 4), _r(lo.Y(), 4), _r(hi.X(), 4), _r(hi.Y(), 4))
            if bb in boxes or bb[2] - bb[0] < 1e-6 or bb[3] - bb[1] < 1e-6:
                continue
            boxes.add(bb)
            out.outlines.append(SurveyOutline(z=_r(pf.center[2], 6), bbox=list(bb)))
    out.outlines.sort(key=lambda o: tuple(o.bbox))
    return out


def check_requirements(
    step_bytes: bytes,
    reqs: Sequence[Any],
    *,
    tol: Tolerance = Tolerance(),
    mode: CheckMode = "create",
) -> CheckReport:
    """Check exported STEP bytes against a frozen requirement list.

    ``status`` is ``passed`` only with ≥1 geometric requirement, every item
    passed and (create / reconstruct) nothing unrequested: no unexplained
    round bore, no unexplained recess face / wall and, when the solid has no
    fillets or chamfers, no unaccounted material (``accounting``). Modify mode
    checks the stated (changed) features only. A STEP that is not
    exactly one valid positive-volume solid is ``failed`` with every
    requirement ``not_evaluated``. Unknown critical values (a hole without
    entries, a blind feature without depth) give ``not_evaluated`` items, and
    so does an unexpected kernel exception while checking one requirement.
    """
    reqs = parse_requirements(reqs)
    report = CheckReport(
        step_sha256=hashlib.sha256(step_bytes).hexdigest(),
        requirements_sha256=requirements_sha256(reqs),
        mode=mode,
        tolerance=tol,
        status="not_evaluated",
        notes=_notes(reqs),
    )
    geometric = [r for r in reqs if r.kind in GEOMETRIC_KINDS]
    solid, report.solid, error = _load_solid(step_bytes)
    if error is not None:
        report.items.append(CheckItem(kind="solid", status="failed", message=error, failures=[error], measured=report.solid.model_dump()))
        for req in geometric:
            report.items.append(
                CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=f"{req.id} not evaluated: {error}")
            )
        report.status = "failed"
        return report
    P = _Probe(solid)
    if mode != "modify" and not any(r.kind == "envelope" for r in reqs):
        msg = f"{mode} mode needs an envelope requirement (overall size of the part)"
        report.items.append(CheckItem(kind="coverage", status="failed", message=msg, failures=[msg]))
    for req in geometric:
        try:
            report.items.extend(_check_one(P, req, tol, geometric))
        except Exception as exc:  # pragma: no cover - OCCT failure on unusual geometry
            log.exception("conformance check of %s failed", req.id)
            report.items.append(
                CheckItem(requirement_id=req.id, kind=req.kind, status="not_evaluated", message=f"{req.id} not evaluated: checker error {exc}")
            )
    if mode != "modify":
        try:
            report.unrequested = (_unrequested_holes(P, reqs, tol) + _unrequested_recesses(P, reqs, tol)
                                  + _unrequested_surfaces(P, reqs, tol))
        except Exception as exc:  # pragma: no cover - OCCT failure on unusual geometry
            log.exception("coverage check failed")
            msg = f"coverage not evaluated: checker error {exc}"
            report.items.append(CheckItem(kind="coverage", status="not_evaluated", message=msg))
        statuses = {i.status for i in report.items}
        if statuses <= {"passed"} and not report.unrequested and geometric:
            report.accounting, volume = _account(P, reqs, tol)
            report.unrequested += volume
        else:
            report.accounting = VolumeAccounting(status="not_evaluated", reason="other checks did not pass")
    statuses = {i.status for i in report.items}
    if "failed" in statuses or report.unrequested:
        report.status = "failed"
    elif not geometric or "not_evaluated" in statuses:
        report.status = "not_evaluated"
    else:
        report.status = "passed"
    return report
