"""Independent checks of the W3 geometry features (W0 contract §2.1: business check ``geometry.feature.<kind>``).

After a successful build the kernel leaves an :class:`app.cad.kernel.OpTrace` per op (solid before / after, the tool
solids it applied). For every op of a W3 feature kind (:func:`app.cad.ops.w3_kind`) this module states what the op
must have produced from its parameters alone and compares it with the B-rep, measured with OCP directly (GProp
volumes, optimal bounding boxes, point classification):

- closed-form areas, centroids and extents of line / arc profiles (:mod:`app.cad.sketch`) → extrude volume and
  bounding box;
- Pappus' theorems: revolve volume = A · 2π · r̄ · angle / 360, planar sweep volume = A × centroid path length;
- bent plate volume = w · (t · Σ flats + Σ |θ| · t · (r + t / 2)), with legs / bends walked independently of the
  outline generator;
- coil volume = the kept section area integrated along the centre line of :mod:`app.cad.coil` (area × length unless
  the ends are ground), with section-edge probes at stations of the stated helix;
- shell of a box = outer − inner; pattern = the sum of its disjoint copies;
- probes for what volumes cannot see: material just inside every profile edge (lines and arcs, 0.02 mm), the
  material outside it unchanged, emptiness inside a cut / hole / shell cavity, a clean hole mouth on an inclined
  face, a floor at the stated depth, walls of the stated thickness, material inside every pattern copy.

Each kind runs through ``policy.run_check`` (Turbo: ``skipped_turbo``, the checker is not called). The report is
stored as ``checks["features"]`` of the revision; a ``failed`` (or ``unknown``: the checker could not evaluate)
report refuses the save under ``standard``. Units mm, world frame of the ops.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from itertools import product
from typing import Any, Literal

from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_IN, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from pydantic import BaseModel, Field

from .. import policy as policy_mod
from . import datums as dt
from .ops import (
    BentPlateOp,
    CoilOp,
    CutOp,
    ExtrudeOp,
    HoleOp,
    ModelSpec,
    PatternOp,
    RevolveOp,
    ShellOp,
    SweepOp,
    hole_positions,
    hole_world_frame,
    w3_kind,
)
from .sketch import (
    Seg,
    cross3,
    default_x,
    loop_distance,
    loop_support,
    point_in_region,
    profile_area_centroid,
    profile_loops,
    resolve_path,
    sweep_centroid_length,
    unit3,
)

CHECK_SCHEMA = "partroute.feature_check/1"
CHECKER_VERSION = "partroute-feature-check-2"
VOLUME_REL = 1e-5  # relative volume tolerance (OCCT integrates exact surfaces to ~1e-10)
VOLUME_ABS = 1e-3  # mm³
LINEAR = 0.01  # mm, extents
EDGE_DELTA = 0.02  # mm, edge probes on either side of a profile edge

ItemStatus = Literal["passed", "failed", "not_evaluated"]
V3 = tuple[float, float, float]
Map = Callable[[float, float], V3]


class FeatureItem(BaseModel):
    """One checked op. ``failures`` holds one line per failed sub-check (expected vs measured)."""

    op_id: str
    kind: str
    status: ItemStatus
    expected: dict[str, Any] = Field(default_factory=dict)
    measured: dict[str, Any] = Field(default_factory=dict)
    tolerance: dict[str, float] = Field(default_factory=dict)
    probes: int = 0
    message: str
    failures: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# OCP measurements (independent of the build123d code paths of the kernel)
# --------------------------------------------------------------------------- #


def _w(shape: Any) -> Any:
    return getattr(shape, "wrapped", shape)


def volume(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(_w(shape), props)
    return float(props.Mass())


def volume_precise(shape: Any, eps: float = 1e-7) -> float:
    """Volume with adaptive integration (needed for swept B-spline faces such as coils)."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(_w(shape), props, eps)
    return float(props.Mass())


def bbox(shape: Any) -> tuple[V3, V3]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(_w(shape), box, False, False)
    lo, hi = box.CornerMin(), box.CornerMax()
    return (lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z())


class _Inside:
    def __init__(self, shape: Any):
        self._cls = BRepClass3d_SolidClassifier(_w(shape))

    def __call__(self, p: Sequence[float]) -> bool:
        self._cls.Perform(gp_Pnt(float(p[0]), float(p[1]), float(p[2])), 1e-7)
        return self._cls.State() == TopAbs_IN


def distance(shape: Any, p: Sequence[float]) -> float:
    """Distance from a point to the boundary (faces) of a solid, also for points inside it."""
    v = BRepBuilderAPI_MakeVertex(gp_Pnt(float(p[0]), float(p[1]), float(p[2]))).Vertex()
    best = math.inf
    exp = TopExp_Explorer(_w(shape), TopAbs_SHELL)
    while exp.More():
        d = BRepExtrema_DistShapeShape(v, exp.Current())
        if d.IsDone():
            best = min(best, float(d.Value()))
        exp.Next()
    return 0.0 if math.isinf(best) else best


def _vol_tol(v: float) -> float:
    return VOLUME_ABS + VOLUME_REL * abs(v)


def _f(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


def _pt(p: Sequence[float]) -> str:
    return "(" + ", ".join(f"{round(c, 3) + 0.0:g}" for c in p) + ")"


def _r(x: float, n: int = 6) -> float:
    return round(float(x), n) + 0.0


class _Item:
    """Collects the sub-check results of one op."""

    def __init__(self, op: Any, kind: str):
        self.op, self.kind = op, kind
        self.failures: list[str] = []
        self.expected: dict[str, Any] = {}
        self.measured: dict[str, Any] = {}
        self.tolerance: dict[str, float] = {}
        self.probes = 0

    def volume(self, name: str, expected: float, measured: float, what: str) -> None:
        tol = _vol_tol(expected)
        self.expected[name], self.measured[name], self.tolerance[name] = _r(expected), _r(measured), _r(tol, 9)
        if abs(expected - measured) > tol:
            self.failures.append(f"{what}: expected {_f(expected)} mm³, measured {_f(measured)} mm³")

    def extent(self, axis: str, lo: float, hi: float, mlo: float, mhi: float, what: str) -> None:
        self.expected[f"{axis}_range"] = [_r(lo), _r(hi)]
        self.measured[f"{axis}_range"] = [_r(mlo), _r(mhi)]
        self.tolerance["linear"] = LINEAR
        if abs(lo - mlo) > LINEAR or abs(hi - mhi) > LINEAR:
            self.failures.append(f"{what} along {axis}: expected {_f(lo)}..{_f(hi)}, measured {_f(mlo)}..{_f(mhi)}")

    def probe(self, inside: Callable[[Sequence[float]], bool], p: Sequence[float], want: bool, what: str) -> None:
        self.probes += 1
        got = inside(p)
        if got != want:
            self.failures.append(f"{what} at {_pt(p)}: expected {'material' if want else 'empty'}, found "
                                 f"{'material' if got else 'empty'}")

    def done(self, ok_text: str) -> FeatureItem:
        head = f"{self.op.id} {self.kind}"
        self.tolerance.setdefault("edge_probe", EDGE_DELTA)
        if self.failures:
            more = f" (+{len(self.failures) - 3} more)" if len(self.failures) > 3 else ""
            return FeatureItem(op_id=self.op.id, kind=self.kind, status="failed", expected=self.expected, measured=self.measured,
                               tolerance=self.tolerance, probes=self.probes, failures=self.failures[:12],
                               message=f"{head}: " + "; ".join(self.failures[:3]) + more)
        return FeatureItem(op_id=self.op.id, kind=self.kind, status="passed", expected=self.expected, measured=self.measured,
                           tolerance=self.tolerance, probes=self.probes, message=f"{head}: {ok_text}")


# --------------------------------------------------------------------------- #
# Region samples and edge probes
# --------------------------------------------------------------------------- #


def _region_samples(outer: Sequence[Seg], holes: Sequence[Sequence[Seg]], n: int = 5) -> list[tuple[float, float]]:
    """Up to ``n`` points well inside a region (deepest grid points, spread over its box)."""
    xs_hi, xs_lo = loop_support(outer, (1, 0)), -loop_support(outer, (-1, 0))
    ys_hi, ys_lo = loop_support(outer, (0, 1)), -loop_support(outer, (0, -1))
    size = max(1e-6, min(xs_hi - xs_lo, ys_hi - ys_lo))
    cand: list[tuple[float, float, float]] = []
    k = 15
    for i in range(k):
        for j in range(k):
            p = (xs_lo + (i + 0.5) * (xs_hi - xs_lo) / k, ys_lo + (j + 0.5) * (ys_hi - ys_lo) / k)
            if point_in_region(p, outer, holes):
                dmin = min([loop_distance(p, outer)] + [loop_distance(p, h) for h in holes])
                if dmin > 2 * EDGE_DELTA:
                    cand.append((dmin, p[0], p[1]))
    cand.sort(reverse=True)
    picked: list[tuple[float, float]] = []
    for _, x, y in cand:
        if len(picked) >= n:
            break
        if all(math.dist((x, y), q) > 0.15 * size for q in picked):
            picked.append((x, y))
    return picked


def _edge_points(outer: Sequence[Seg], holes: Sequence[Sequence[Seg]], delta: float = EDGE_DELTA) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """(just inside, just outside) point pairs across every edge of a region (3 per segment), skipping spots where
    both or neither side is inside (corners, slivers)."""
    out = []
    for loop in [outer, *holes]:
        for s in loop:
            if s.length < 4 * delta:
                continue
            for t in (0.25, 0.5, 0.75):
                q = s.point(t)
                tx, ty = s.tangent(t)
                a = (q[0] - ty * delta, q[1] + tx * delta)
                b = (q[0] + ty * delta, q[1] - tx * delta)
                ia, ib = point_in_region(a, outer, holes), point_in_region(b, outer, holes)
                if ia != ib:
                    out.append((a, b) if ia else (b, a))
    return out


def _probe_edges(it: _Item, outer: Sequence[Seg], holes: Sequence[Sequence[Seg]], to_world: Map, mode: str,
                 before: Callable[[Sequence[float]], bool] | None, after: Callable[[Sequence[float]], bool], what: str,
                 keep: Callable[[tuple[float, float], tuple[float, float]], bool] | None = None) -> None:
    """Material just inside every profile edge (add) / emptiness there (subtract); the side outside unchanged."""
    want_in = mode != "subtract"
    for a, b in _edge_points(outer, holes):
        if keep is not None and not keep(a, b):
            continue
        it.probe(after, to_world(*a), want_in, f"just inside the {what} edge")
        pb = to_world(*b)
        it.probe(after, pb, before(pb) if before is not None else False, f"just outside the {what} edge")


def _placement(spec: ModelSpec, op: Any, frame: Any) -> Any:
    tf = [t for t in spec.ops if t.type == "transform" and not t.suppressed and t.target == op.id]
    return dt.placement(op, tf, dt.body_anchor(op, frame))


def _unplaced(spec: ModelSpec, op: Any) -> bool:
    return not any(abs(a) > 1e-12 for a in op.rotate) and not any(
        t.type == "transform" and not t.suppressed and t.target == op.id for t in spec.ops)


def _body_mode(op: Any) -> str | None:
    return {"new": "add", "add": "add", "subtract": "subtract"}.get(op.mode)


# --------------------------------------------------------------------------- #
# Per kind
# --------------------------------------------------------------------------- #


def _check_extrude(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: ExtrudeOp = tr.op
    it = _Item(op, "extrude")
    f = dt.placed_frame(op.plane, op.offset, op.at, op.rotate_profile, known)
    area, _ = profile_area_centroid(op.profile)
    it.expected["profile_area_mm2"] = _r(area)
    it.volume("tool_volume", area * op.height, volume(tr.tools[0]), "extruded volume (profile area × height)")
    outer, holes = profile_loops(op.profile)
    if _unplaced(spec, op):
        lo, hi = bbox(tr.tools[0])
        for i, axis in enumerate("XYZ"):
            ex, ey, ez = f.x[i], f.y[i], f.z[i]
            mx = f.origin[i] + loop_support(outer, (ex, ey)) + max(0.0, op.height * ez)
            mn = f.origin[i] - loop_support(outer, (-ex, -ey)) + min(0.0, op.height * ez)
            it.extent(axis, mn, mx, lo[i], hi[i], "extruded profile")
    mode = _body_mode(op)
    if mode is not None:
        m = _placement(spec, op, f)
        before = _Inside(tr.before) if tr.before is not None else None
        after = _Inside(tr.after)
        for w in (0.5 * op.height,):
            _probe_edges(it, outer, holes, lambda u, v, w=w: dt.apply(m, f.point(u, v, w)), mode, before, after, "profile")
        for (u, v) in _region_samples(outer, holes):
            it.probe(after, dt.apply(m, f.point(u, v, 0.5 * op.height)), mode == "add", "inside the extruded profile")
    return it.done(f"profile {_f(area)} mm² × {_f(op.height)} mm; edges, extents and material as stated")


def _check_revolve(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: RevolveOp = tr.op
    it = _Item(op, "revolve")
    outer, holes = op.profile.loops()
    area, (_, cv) = profile_area_centroid(op.profile)
    it.expected["profile_area_mm2"] = _r(area)
    it.expected["centroid_radius_mm"] = _r(cv)
    it.volume("tool_volume", area * 2 * math.pi * cv * op.angle / 360.0, volume(tr.tools[0]),
              "revolved volume (Pappus: area × 2π × centroid radius × angle / 360)")
    f = op.frame()
    if op.angle >= 360.0 and _unplaced(spec, op):
        lo, hi = bbox(tr.tools[0])
        for i, axis in enumerate("XYZ"):
            alpha = f.x[i]
            beta = math.sqrt(max(0.0, 1.0 - alpha * alpha))
            it.extent(axis, f.origin[i] - loop_support(outer, (-alpha, beta)), f.origin[i] + loop_support(outer, (alpha, beta)),
                      lo[i], hi[i], "revolved section")
    mode = _body_mode(op)
    if mode is not None:
        m = _placement(spec, op, None)
        before = _Inside(tr.before) if tr.before is not None else None
        after = _Inside(tr.after)
        for frac in (0.25, 0.75):
            ang = math.radians(op.angle * frac)
            radial = tuple(f.y[k] * math.cos(ang) + f.z[k] * math.sin(ang) for k in range(3))

            def to_world(u: float, v: float, radial: Any = radial) -> V3:
                return dt.apply(m, tuple(f.origin[k] + u * f.x[k] + v * radial[k] for k in range(3)))

            # an edge on the axis has the mirrored section on its other side: no outside probe there
            _probe_edges(it, outer, holes, to_world, mode, before, after, "section", keep=lambda a, b: min(a[1], b[1]) > 0)
            for (u, v) in _region_samples(outer, holes, n=3):
                it.probe(after, to_world(u, v), mode == "add", "inside the revolved section")
    return it.done(f"Pappus volume of a {_f(area)} mm² section at radius {_f(cv)} mm over {_f(op.angle)}°; section edges as stated")


def _check_sweep(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: SweepOp = tr.op
    it = _Item(op, "sweep")
    area, (cu, _) = profile_area_centroid(op.profile)
    pf = dt.base_frame(op.plane, known)
    segs = resolve_path(op.path.start, op.path.segments)
    length = sweep_centroid_length(segs, cu)
    it.expected["section_area_mm2"] = _r(area)
    it.expected["centroid_path_mm"] = _r(length)
    it.volume("tool_volume", area * length, volume(tr.tools[0]), "swept volume (section area × centroid path length)")
    mode = _body_mode(op)
    if mode is not None:
        m = _placement(spec, op, dt.sweep_frames(op, known)[1])
        outer, holes = profile_loops(op.profile)
        before = _Inside(tr.before) if tr.before is not None else None
        after = _Inside(tr.after)
        for s in segs:
            q = s.point(0.5)
            t = s.tangent(0.5)
            T = unit3(pf.vector(t[0], t[1]))
            left = cross3(pf.z, T)
            base = pf.point(q[0], q[1])

            def to_world(u: float, v: float, base: Any = base, left: Any = left) -> V3:
                return dt.apply(m, tuple(base[k] + u * left[k] + v * pf.z[k] for k in range(3)))

            _probe_edges(it, outer, holes, to_world, mode, before, after, "section")
    return it.done(f"section {_f(area)} mm² along a {_f(length)} mm centroid path; section edges as stated")


def _check_bent_plate(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: BentPlateOp = tr.op
    it = _Item(op, "bent_plate")
    t, r = op.thickness, op.inner_radius
    flats = []
    for i, length in enumerate(op.legs):
        fl = length
        if op.leg_length == "outside":
            for b in ([op.bends[i - 1]] if i > 0 else []) + ([op.bends[i]] if i < len(op.bends) else []):
                fl -= (r + t) * math.tan(math.radians(abs(b)) / 2.0)
        flats.append(fl)
    section = t * sum(flats) + sum(math.radians(abs(b)) * t * (r + t / 2.0) for b in op.bends)
    it.expected["flat_lengths_mm"] = [_r(x) for x in flats]
    it.volume("tool_volume", section * op.width, volume(tr.tools[0]), "bent plate volume (w · (t · Σ flats + Σ θ · t · (r + t/2)))")
    mode = _body_mode(op)
    if mode is not None:
        want = mode == "add"
        f = dt.bent_plate_frame(op, known)
        m = _placement(spec, op, f)
        after = _Inside(tr.after)
        # walk the reference face from the parameters, independently of the outline generator
        p, phi = (0.0, 0.0), 0.0
        w = op.width / 2.0
        for i, fl in enumerate(flats):
            lu, lv = -math.sin(phi), math.cos(phi)
            c0 = (p[0] + 0.5 * fl * math.cos(phi), p[1] + 0.5 * fl * math.sin(phi))
            for k, (off, exp) in enumerate(((EDGE_DELTA, want), (t - EDGE_DELTA, want), (-EDGE_DELTA, False), (t + EDGE_DELTA, False))):
                if not want and k >= 2:
                    continue
                q = (c0[0] + off * lu, c0[1] + off * lv)
                it.probe(after, dt.apply(m, f.point(q[0], q[1], w)), exp, f"leg {i + 1} at {_f(off)} mm across its thickness")
            p = (p[0] + fl * math.cos(phi), p[1] + fl * math.sin(phi))
            if i == len(flats) - 1:
                break
            th = math.radians(op.bends[i])
            ra = r + t if th > 0 else r
            sgn = 1.0 if th > 0 else -1.0
            c = (p[0] + sgn * ra * lu, p[1] + sgn * ra * lv)
            a0 = math.atan2(p[1] - c[1], p[0] - c[0]) if ra > 1e-12 else math.atan2(-sgn * lv, -sgn * lu)
            for rad, exp in ((r + EDGE_DELTA, want), (r + t - EDGE_DELTA, want), (r - EDGE_DELTA, False), (r + t + EDGE_DELTA, False)):
                if rad <= 0 or (not want and not exp):
                    continue
                q = (c[0] + rad * math.cos(a0 + th / 2.0), c[1] + rad * math.sin(a0 + th / 2.0))
                it.probe(after, dt.apply(m, f.point(q[0], q[1], w)), exp, f"bend {i + 1} at radius {_f(rad)} mm")
            p = (c[0] + ra * math.cos(a0 + th), c[1] + ra * math.sin(a0 + th)) if ra > 1e-12 else p
            phi += th
    return it.done(f"t {_f(t)}, flats {', '.join(_f(x) for x in flats)}, bends {', '.join(_f(b) for b in op.bends)}° at ri {_f(r)}")


COIL_VOLUME_REL = 1e-4  # the swept B-spline surfaces integrate to ~3e-6 (adaptive Gauss); a wrong turn count is far off


def _check_coil(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    """Coil: the kept section area integrated along the centre line (area × length unless ground) against the tool
    volume (adaptive integration: swept B-spline faces); probes across every section edge at stations on constant-pitch
    stretches (radial / axial position, handedness and pitch: the probes sit on the stated centre line), the centre
    line itself, and the two ends (wire caps, or the ground planes z = 0 and z = length)."""
    op: CoilOp = tr.op
    it = _Item(op, "coil")
    law = op.law()
    kind = op.section.kind
    f = op.frame()
    expected = law.volume(kind)
    it.expected["centre_line_mm"] = _r(law.arc_length())
    it.expected["active_pitch_mm"] = _r(law.pitch)
    it.expected["length_mm"] = _r(law.length)
    tol = VOLUME_ABS + COIL_VOLUME_REL * expected
    measured = volume_precise(tr.tools[0])
    it.expected["tool_volume"], it.measured["tool_volume"], it.tolerance["tool_volume"] = _r(expected), _r(measured), _r(tol, 9)
    if abs(expected - measured) > tol:
        what = "ground ends: kept section area along the centre line" if law.ground else "section area × centre-line length"
        it.failures.append(f"coil volume ({what}): expected {_f(expected)} mm³, measured {_f(measured)} mm³")
    mode = _body_mode(op)
    if mode is not None:
        from .coil_occ import RobustInside

        m = _placement(spec, op, None)
        before = RobustInside(tr.before) if tr.before is not None else None
        after = RobustInside(tr.after)
        outer, holes = profile_loops(op.section)
        want = mode == "add"
        for t in law.stations():
            def to_world(u: float, v: float, t: float = t) -> V3:
                return dt.apply(m, f.point(*law.section_point(t, u, v)))

            _probe_edges(it, outer, holes, to_world, mode, before, after, f"section at turn {_f(t)}")
            it.probe(after, to_world(0.0, 0.0), want, f"centre line at turn {_f(t)}")

        def outside(p: V3) -> bool:
            return before(p) if before is not None else False

        # the two ends where the law puts them (B-spline face bounding boxes are too loose for extents)
        if law.ground:  # flat at z = 0 and z = length, where the end turn's centre line is below the wire's half size
            for t, z_in, z_out, what in ((0.15, EDGE_DELTA, -EDGE_DELTA, "seating plane z = 0"),
                                         (law.turns - 0.15, law.length - EDGE_DELTA, law.length + EDGE_DELTA, "ground top")):
                x, y, _ = law.point(t)
                it.probe(after, dt.apply(m, f.point(x, y, z_in)), want, f"just inside the {what}")
                p_out = dt.apply(m, f.point(x, y, z_out))
                it.probe(after, p_out, outside(p_out), f"just outside the {what}")
        else:  # the end caps are the section planes at t = 0 and t = turns
            for t, sgn, what in ((0.0, -1.0, "start"), (law.turns, 1.0, "end")):
                P = law.point(t)
                T = law.frame(t)[0]
                it.probe(after, dt.apply(m, f.point(*(P[k] - sgn * EDGE_DELTA * T[k] for k in range(3)))), want,
                         f"just inside the wire {what}")
                p_out = dt.apply(m, f.point(*(P[k] + sgn * EDGE_DELTA * T[k] for k in range(3))))
                it.probe(after, p_out, outside(p_out), f"just beyond the wire {what}")
    return it.done(f"{'Ø' + _f(2 * op.section.radius) if kind == 'circle' else 'rect'} wire on Dm {_f(op.mean_diameter)}, "
                   f"{_f(op.turns)} turns, active pitch {_f(law.pitch)}, length {_f(law.length)} ({op.handedness}, {op.end_form}); "
                   "volume, ends and section edges as stated")


def _check_cut(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    from .conformance_w3 import loops_face

    op: CutOp = tr.op
    it = _Item(op, "cut")
    f = dt.cut_frame(op, known)
    area, _ = profile_area_centroid(op.profile)
    length = float(op.depth if op.depth is not None else tr.info.get("length") or 0.0)
    it.expected["profile_area_mm2"] = _r(area)
    it.volume("tool_volume", area * (length + 0.01), volume(tr.tools[0]), "cut prism volume (profile area × length)")
    removed = volume(tr.before) - volume(tr.after)
    it.measured["removed_mm3"] = _r(removed)
    if removed <= VOLUME_ABS:
        it.failures.append("the cut removed no material")
    outer, holes = profile_loops(op.profile)
    # Sparse interior samples cannot prove that earlier cuts left a completely solid stock.
    # Build the stated cutter from analytic loops, then measure only its overlap with the actual prior solid.
    cut_depth = op.depth
    if cut_depth is None:
        low, high = bbox(tr.before)
        cut_depth = max(-f.local(p)[2] for p in product(*zip(low, high))) + 1.0
    face = loops_face(outer, holes, lambda u, v: f.point(u, v, 0.01), f.z, f.x)
    cutter = BRepPrimAPI_MakePrism(face, gp_Vec(*[-c * (cut_depth + 0.01) for c in f.z])).Shape()
    common = BRepAlgoAPI_Common(_w(tr.before), cutter)
    common.Build()
    if not common.IsDone():
        raise RuntimeError("could not measure the stated cut's overlap with prior material")
    it.volume("removed_volume", volume(common.Shape()), removed, "removed volume inside the stated cut and prior material")
    before, after = _Inside(tr.before), _Inside(tr.after)
    depth = op.depth if op.depth is not None else length
    samples = _region_samples(outer, holes)
    for frac in (0.15, 0.5, 0.85):
        for (u, v) in samples:
            p = f.point(u, v, -depth * frac)
            if before(p):
                it.probe(after, p, False, "inside the cut")
    mid = 0.5 * depth
    _probe_edges(it, outer, holes, lambda u, v: f.point(u, v, -mid), "subtract", before, after, "cut profile")
    if op.depth is not None:
        for (u, v) in samples:
            q = f.point(u, v, -op.depth - EDGE_DELTA)
            if before(q):
                it.probe(after, q, True, f"just below the floor at depth {_f(op.depth)}")
            q = f.point(u, v, -op.depth + EDGE_DELTA)
            if before(q):
                it.probe(after, q, False, f"just above the floor at depth {_f(op.depth)}")
    return it.done(f"profile {_f(area)} mm², " + ("through" if op.depth is None else f"{_f(op.depth)} mm deep") + "; edges and floor as stated")


def _entry_normal(op: HoleOp, shape: Any, p: Sequence[float]) -> V3:
    if op.plane is not None:
        return op.plane.frame().z
    import build123d as bd

    pv = bd.Vector(*p)
    face = min(shape.faces(), key=lambda fc: fc.distance_to(pv))
    n = face.normal_at(pv)
    return (n.X, n.Y, n.Z)


def _check_hole(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: HoleOp = tr.op
    it = _Item(op, "hole")
    _, _, d = hole_world_frame(op)
    u0 = default_x(d)
    v0 = cross3(d, u0)
    r = op.diameter / 2.0
    before, after = _Inside(tr.before), _Inside(tr.after)
    for k, p in enumerate(hole_positions(op)):
        depth = op.depth
        ts = [0.3 * r, r, 2.0 * r] if depth is None else [0.02 * depth, 0.5 * depth, 0.98 * depth]
        for t in ts:
            q = tuple(p[i] + d[i] * t for i in range(3))
            if before(q):
                it.probe(after, q, False, f"on the axis of hole #{k}")
        n = unit3(_entry_normal(op, tr.before, p))
        c = abs(sum(n[i] * d[i] for i in range(3)))
        ext = r * math.sqrt(max(0.0, 1 - c * c)) / max(c, 0.05)
        for a in range(8):
            ang = a * math.pi / 4
            for t in (-0.5 * ext, 0.05 * r):
                q = tuple(p[i] + 0.9 * r * (math.cos(ang) * u0[i] + math.sin(ang) * v0[i]) + d[i] * t for i in range(3))
                if before(q):
                    it.probe(after, q, False, f"mouth of hole #{k} (no lip)")
        mid = 0.5 * (depth if depth is not None else r)
        for a in range(4):
            ang = a * math.pi / 2
            q = tuple(p[i] + (r + EDGE_DELTA) * (math.cos(ang) * u0[i] + math.sin(ang) * v0[i]) + d[i] * mid for i in range(3))
            if before(q):
                it.probe(after, q, True, f"wall of hole #{k} (Ø{_f(op.diameter)})")
        if depth is not None:
            q = tuple(p[i] + d[i] * (depth + EDGE_DELTA) for i in range(3))
            if before(q):
                it.probe(after, q, True, f"below the floor of hole #{k} (depth {_f(depth)})")
    return it.done(f"Ø{_f(op.diameter)} " + ("through" if op.depth is None else f"{_f(op.depth)} deep") + "; axis, wall, mouth as stated")


def _check_shell(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: ShellOp = tr.op
    it = _Item(op, "shell")
    t = op.thickness
    vb, va = volume(tr.before), volume(tr.after)
    lo, hi = bbox(tr.before)
    size = [hi[i] - lo[i] for i in range(3)]
    box_like = abs(vb - size[0] * size[1] * size[2]) <= _vol_tol(vb)
    opened: set[tuple[int, int]] = set()
    aligned = True
    for d in op.open_faces:
        n = unit3(d)
        k = max(range(3), key=lambda i: abs(n[i]))
        aligned &= abs(n[k]) > 1 - 1e-9
        opened.add((k, 1 if n[k] > 0 else -1))
    if box_like and aligned:
        inner = [size[i] - (2 - sum(1 for k, _ in opened if k == i)) * t for i in range(3)]
        it.expected["inner_mm"] = [_r(x) for x in inner]
        it.volume("volume", vb - (inner[0] * inner[1] * inner[2] if min(inner) > 0 else 0.0), va, "shell of a box (outer − inner)")
    else:
        it.measured["volume"] = _r(va)
        if not va < vb - VOLUME_ABS:
            it.failures.append(f"the shell removed no material ({_f(vb)} → {_f(va)} mm³)")
    inside_b, inside_a = _Inside(tr.before), _Inside(tr.after)
    opens = tr.info.get("open_faces", [])
    delta = max(EDGE_DELTA, min(0.2, 0.1 * t))
    for fc in tr.before.faces():
        if fc.geom_type.name != "PLANE":
            continue
        c = fc.center()
        n = fc.normal_at(c)
        cp, nv = (c.X, c.Y, c.Z), (n.X, n.Y, n.Z)
        if any(fc.is_same(o) for o in opens):
            q = tuple(cp[i] - nv[i] * delta for i in range(3))
            if inside_b(q) and distance(tr.before, tuple(cp[i] - nv[i] * (t + delta) for i in range(3))) >= t:
                it.probe(inside_a, q, False, "just inside an open face")
            continue
        for depth, want, what in ((delta, True, "wall material near the outer face"), (t - delta, True, f"inside the {_f(t)} mm wall"),
                                  (t + delta, False, f"beyond the {_f(t)} mm wall (cavity)")):
            q = tuple(cp[i] - nv[i] * depth for i in range(3))
            if not inside_b(q):
                continue
            if not want and distance(tr.before, q) < t + delta - 1e-6:
                continue  # a thin spot of the original solid: no cavity expected here
            it.probe(inside_a, q, want, what)
    return it.done(f"walls {_f(t)} mm, {len(op.open_faces)} open face(s)")


def _check_pattern(spec: ModelSpec, tr: Any, known: dict[str, Any]) -> FeatureItem:
    op: PatternOp = tr.op
    it = _Item(op, "pattern")
    mats = tr.info.get("matrices") or []
    n_layout = op.layout.count * (op.layout.count2 if op.layout.kind == "linear" else 1)
    it.expected["instances"], it.measured["instances"] = n_layout, len(mats) + 1
    if len(mats) + 1 != n_layout:
        it.failures.append(f"instance count: expected {n_layout}, built {len(mats) + 1}")
    mode = tr.info.get("mode", "add")
    after = _Inside(tr.after)
    for tool in tr.info.get("base_tools") or []:
        pts = _tool_points(tool)
        for k, m in enumerate(mats, start=1):
            for p in pts:
                it.probe(after, dt.apply(m, p), mode != "subtract", f"inside copy {k}")
    copies = tr.tools
    groups = _copy_groups(copies, len(mats))
    if 0 < len(groups) <= 64 and _disjoint([g for g in groups]):
        vb, va = volume(tr.before), volume(tr.after)
        expected = 0.0
        for g in groups:
            for c in g:
                common = BRepAlgoAPI_Common(_w(tr.before), _w(c))
                common.Build()
                vc = volume(common.Shape()) if common.IsDone() else 0.0
                expected += vc if mode == "subtract" else volume(c) - vc
        if mode == "subtract" and any(len(g) > 1 for g in groups):
            expected = None  # counterbore / countersink pieces overlap their bore: no simple sum
        if expected is not None:
            it.volume("volume_change", expected, abs(va - vb), "pattern volume change (sum of the disjoint copies)")
    return it.done(f"{n_layout} instances of {op.target} ({op.layout.kind}); every copy present")


def _copy_groups(copies: Sequence[Any], n: int) -> list[list[Any]]:
    """The pattern's tool pieces grouped per copy (the kernel lists them copy by copy)."""
    if n <= 0 or len(copies) % n:
        return []
    per = len(copies) // n
    return [list(copies[i * per:(i + 1) * per]) for i in range(n)]


def _tool_points(tool: Any) -> list[V3]:
    """A few points well inside a tool solid: its centroid and points toward its bbox corners."""
    inside = _Inside(tool)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(_w(tool), props)
    c = props.CentreOfMass()
    cp = (c.X(), c.Y(), c.Z())
    lo, hi = bbox(tool)
    cands = [cp] + [tuple(cp[i] + 0.4 * ((hi[i] if (k >> i) & 1 else lo[i]) - cp[i]) for i in range(3)) for k in range(8)]
    out = [p for p in cands if inside(p) and distance(tool, p) > 2 * EDGE_DELTA]
    return out[:3]  # type: ignore[return-value]


def _disjoint(groups: Sequence[Sequence[Any]]) -> bool:
    boxes = []
    for g in groups:
        bs = [bbox(s) for s in g]
        boxes.append((tuple(min(b[0][k] for b in bs) for k in range(3)), tuple(max(b[1][k] for b in bs) for k in range(3))))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if all(a[0][k] < b[1][k] - 1e-9 and b[0][k] < a[1][k] - 1e-9 for k in range(3)):
                return False
    return True


_CHECKS: dict[str, Callable[[ModelSpec, Any, dict[str, Any]], FeatureItem]] = {
    "extrude": _check_extrude,
    "revolve": _check_revolve,
    "sweep": _check_sweep,
    "bent_plate": _check_bent_plate,
    "coil": _check_coil,
    "cut": _check_cut,
    "hole": _check_hole,
    "shell": _check_shell,
    "pattern": _check_pattern,
}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _inactive(spec: ModelSpec, op: Any) -> bool:
    """A suppressed op, or a pattern of a suppressed target (the kernel builds nothing for it: ``target_suppressed``),
    is not requested: it has no trace and nothing to check."""
    if op.suppressed:
        return True
    if isinstance(op, PatternOp):
        target = next((o for o in spec.ops if o.id == op.target), None)
        return target is not None and target.suppressed
    return False


def feature_kinds(spec: ModelSpec) -> list[str]:
    """W3 feature kinds present among the active ops, in first-use order."""
    out: list[str] = []
    for op in spec.ops:
        k = None if _inactive(spec, op) else w3_kind(op)
        if k and k not in out:
            out.append(k)
    return out


def check_kind(kind: str, spec: ModelSpec, traces: Sequence[Any]) -> dict[str, Any]:
    """The independent check of one feature kind over its traced ops (the ``run`` of ``policy.run_check``)."""
    items: list[FeatureItem] = []
    for tr in traces:
        known = dt.op_datums(spec, upto=tr.op.id)
        try:
            items.append(_CHECKS[kind](spec, tr, known))
        except Exception as exc:  # noqa: BLE001 - an unevaluable feature is reported, never a pass
            items.append(FeatureItem(op_id=tr.op.id, kind=kind, status="not_evaluated",
                                     message=f"{tr.op.id} {kind} not evaluated: checker error {type(exc).__name__}: {exc}"))
    statuses = {i.status for i in items}
    status = "failed" if "failed" in statuses else ("unknown" if "not_evaluated" in statuses or not items else "passed")
    return {"check": f"geometry.feature.{kind}", "status": status, "items": [i.model_dump(mode="json") for i in items]}


def check_features(spec: ModelSpec, result: Any, *, execution_policy: str | None = None) -> dict[str, Any] | None:
    """The feature check report of one successful build, or None when the spec has no W3 feature. Every kind goes
    through ``policy.run_check("geometry.feature.<kind>", …)``; under Turbo each entry is ``skipped_turbo``."""
    kinds = feature_kinds(spec)
    if not kinds:
        return None
    pol = policy_mod.parse_policy(execution_policy)
    by_kind: dict[str, list[Any]] = {k: [] for k in kinds}
    for tr in getattr(result, "trace", None) or []:
        k = w3_kind(tr.op)
        if k in by_kind:
            by_kind[k].append(tr)
    checks = [policy_mod.run_check(f"geometry.feature.{kind}", pol,
                                   lambda kind=kind: check_kind(kind, spec, by_kind[kind]))  # type: ignore[misc]
              for kind in kinds]
    statuses = {c["status"] for c in checks}
    if statuses == {"skipped_turbo"}:
        status = "skipped_turbo"
    elif "failed" in statuses:
        status = "failed"
    elif "unknown" in statuses:
        status = "unknown"
    else:
        status = "passed"
    return {"schema": CHECK_SCHEMA, "checker_version": CHECKER_VERSION, "execution_policy": pol, "status": status,
            "build_key": getattr(result, "build_key", None), "checks": checks}


def report_problems(report: dict[str, Any] | None) -> list[str]:
    """Failure lines of a feature check report (failed / unevaluated items)."""
    out: list[str] = []
    for c in (report or {}).get("checks") or []:
        for i in c.get("items") or []:
            if i.get("status") in ("failed", "not_evaluated"):
                out.append(str(i.get("message")))
        if c.get("status") == "unknown" and not c.get("items"):
            out.append(f"{c.get('check')}: nothing was traced to check")
    return out
