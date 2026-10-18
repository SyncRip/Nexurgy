"""Requirement-checker support for the W3 kinds (prism / revolve / sweep / cut, and coil) — part of
:mod:`app.cad.conformance`.

Like the rest of the checker this works on the exported STEP solid with OCP only and never sees the ops. Each W3
requirement becomes an analytic region (:class:`ReqShape`): point containment, "on the boundary surface", probe
pairs straddling every outline edge (lines and arcs) and an OCP reference solid built from the requirement alone
(prism / revolution of a face made from its loops; a sweep as prisms along lines and partial revolutions along arcs).

- The composite occupancy (:func:`occupancy`) is what the whole requirement list implies at a point: inside a
  material requirement (prism / revolve / sweep / band; or the part's bounding box filler of the original band
  logic when the list has no W3 material) and not inside a removal (cut / hole / pocket). Probes whose expectation
  changes within the probe offset are skipped (corners, touching features).
- The W3 items compare the solid with that occupancy just inside and just outside every outline edge at several
  stations, inside the region, at the end caps and below a blind floor.
- Coverage: faces and bores lying on a W3 requirement's boundary are explained (no unrequested recess / hole /
  edge break), and the reference solid of the volume accounting includes them.
- Coil (generation supplement): the region is the coil law of :mod:`app.cad.coil` (point location by Newton on the
  centre line); probes sit across the section edges at stations on constant-pitch stretches. Its reference solid is
  built by :mod:`app.cad.coil_occ` (the same OCP construction as the kernel's coil op), so the independent evidence for
  a coil is the analytic probes, not the accounting.
- Text (generation reliability M): the glyphs of the requirement's text in the font the kernel would choose
  (:mod:`app.cad.text_geometry`), as even-odd polygons in the text frame. Raised letters are an *addition* (material
  wherever the glyphs stand, on top of what the rest of the list leaves; the reference solid fuses them after the
  removals), engraved ones a removal. Probes straddle sampled letter outlines at mid-height; any face inside the letters'
  slab (text block x height) is explained.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.GC import GC_MakeArcOfCircle
from OCP.gp import gp_Ax1, gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer

from .requirements import CircleContour, PolygonContour, RectContour
from .sketch import Frame, Seg, SketchProfile, cross3, dot3, loop_distance, point_in_region, profile_loops, resolve_path, unit3

V3 = tuple[float, float, float]
W3_KINDS = ("prism", "revolve", "sweep", "cut", "coil", "text")


# --------------------------------------------------------------------------- #
# 2D regions from requirement contours
# --------------------------------------------------------------------------- #


def _shift(segs: list[Seg], c: Sequence[float]) -> list[Seg]:
    if abs(c[0]) < 1e-15 and abs(c[1]) < 1e-15:
        return segs
    out = []
    for s in segs:
        ctr = None if s.center is None else (s.center[0] + c[0], s.center[1] + c[1])
        out.append(Seg(s.kind, (s.p0[0] + c[0], s.p0[1] + c[1]), (s.p1[0] + c[0], s.p1[1] + c[1]), ctr, s.radius, s.a0, s.sweep))
    return out


def contour_loops(c: Any) -> tuple[list[Seg], list[list[Seg]]]:
    """(outer, holes) of a requirement contour in its plane coordinates."""
    if isinstance(c, SketchProfile):
        return c.loops()
    if isinstance(c, RectContour):
        outer, _ = profile_loops(SimpleNamespace(kind="rect", width=c.size[0], length=c.size[1], corner_radius=c.corner_radius))
        return _shift(outer, c.center), []
    if isinstance(c, CircleContour):
        r = c.diameter / 2.0
        return [Seg("arc", (c.center[0] + r, c.center[1]), (c.center[0] + r, c.center[1]), (c.center[0], c.center[1]), r, 0.0,
                    2 * math.pi)], []
    assert isinstance(c, PolygonContour)
    outer, _ = profile_loops(SimpleNamespace(kind="polygon", points=c.points))
    return outer, []


class Region:
    """Outline minus holes (and minus extra voids)."""

    def __init__(self, outline: Any, voids: Sequence[Any] = ()):
        self.outer, self.holes = contour_loops(outline)
        for v in voids:
            vo, _ = contour_loops(v)
            self.holes.append(vo)

    @property
    def loops(self) -> list[list[Seg]]:
        return [self.outer, *self.holes]

    def contains(self, u: float, v: float) -> bool:
        return point_in_region((u, v), self.outer, self.holes)

    def distance(self, u: float, v: float) -> float:
        return min(loop_distance((u, v), lp) for lp in self.loops)

    def edge_pairs(self, delta: float) -> list[tuple[tuple[float, float], tuple[float, float], str]]:
        """(just inside, just outside, label) at 1/4, 1/2, 3/4 of every segment."""
        out = []
        for li, lp in enumerate(self.loops):
            for si, s in enumerate(lp):
                if s.length < 4 * delta:
                    continue
                what = (f"{'outline' if li == 0 else f'void {li}'} segment {si + 1}"
                        + (f" (arc R{s.radius:g} centre ({s.center[0]:g}, {s.center[1]:g}))" if s.kind == "arc" and s.center else ""))
                for t in (0.25, 0.5, 0.75):
                    q = s.point(t)
                    tx, ty = s.tangent(t)
                    a = (q[0] - ty * delta, q[1] + tx * delta)
                    b = (q[0] + ty * delta, q[1] - tx * delta)
                    ia, ib = self.contains(*a), self.contains(*b)
                    if ia != ib:
                        out.append((a, b, what) if ia else (b, a, what))
        return out

    def samples(self, n: int = 4, margin: float = 0.05) -> list[tuple[float, float]]:
        from .sketch import loop_support

        xs_hi, xs_lo = loop_support(self.outer, (1, 0)), -loop_support(self.outer, (-1, 0))
        ys_hi, ys_lo = loop_support(self.outer, (0, 1)), -loop_support(self.outer, (0, -1))
        cand = []
        k = 13
        for i in range(k):
            for j in range(k):
                p = (xs_lo + (i + 0.5) * (xs_hi - xs_lo) / k, ys_lo + (j + 0.5) * (ys_hi - ys_lo) / k)
                if self.contains(*p):
                    d = self.distance(*p)
                    if d > margin:
                        cand.append((d, p))
        cand.sort(reverse=True)
        size = max(1e-6, min(xs_hi - xs_lo, ys_hi - ys_lo))
        out: list[tuple[float, float]] = []
        for _, p in cand:
            if len(out) >= n:
                break
            if all(math.dist(p, q) > 0.15 * size for q in out):
                out.append(p)
        return out


# --------------------------------------------------------------------------- #
# OCP construction from loops (independent of the kernel's build123d path)
# --------------------------------------------------------------------------- #


def _wire(segs: Sequence[Seg], to3: Callable[[float, float], V3], normal: V3, xdir: V3) -> Any:
    mk = BRepBuilderAPI_MakeWire()
    for s in segs:
        if s.kind == "line":
            mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*to3(*s.p0)), gp_Pnt(*to3(*s.p1))).Edge())
        elif abs(s.sweep) >= 2 * math.pi - 1e-9:
            assert s.center is not None
            circ = gp_Circ(gp_Ax2(gp_Pnt(*to3(*s.center)), gp_Dir(*normal), gp_Dir(*xdir)), s.radius)
            mk.Add(BRepBuilderAPI_MakeEdge(circ).Edge())
        else:
            arc = GC_MakeArcOfCircle(gp_Pnt(*to3(*s.p0)), gp_Pnt(*to3(*s.point(0.5))), gp_Pnt(*to3(*s.p1))).Value()
            mk.Add(BRepBuilderAPI_MakeEdge(arc).Edge())
    return mk.Wire()


def region_face(region: Region, to3: Callable[[float, float], V3], normal: V3, xdir: V3) -> Any:
    return loops_face(region.outer, region.holes, to3, normal, xdir)


def loops_face(outer: Sequence[Seg], holes: Sequence[Sequence[Seg]], to3: Callable[[float, float], V3],
               normal: V3, xdir: V3) -> Any:
    """Construct the analytic line/arc outline independently of the modeling kernel."""
    face = BRepBuilderAPI_MakeFace(_wire(outer, to3, normal, xdir), True).Face()
    for h in holes:
        hole = BRepBuilderAPI_MakeFace(_wire(h, to3, normal, xdir), True).Face()
        op = BRepAlgoAPI_Cut(face, hole)
        op.Build()
        exp = TopExp_Explorer(op.Shape(), TopAbs_FACE)
        face = exp.Current() if exp.More() else face
    return face


def fuse_all(shapes: Sequence[Any]) -> Any:
    base = shapes[0]
    for s in shapes[1:]:
        op = BRepAlgoAPI_Fuse(base, s)
        op.SetFuzzyValue(1e-6)
        op.Build()
        if not op.IsDone():
            raise RuntimeError("boolean fuse failed")
        base = op.Shape()
    return base


def common(a: Any, b: Any) -> Any:
    op = BRepAlgoAPI_Common(a, b)
    op.Build()
    return op.Shape()


# --------------------------------------------------------------------------- #
# Requirement shapes
# --------------------------------------------------------------------------- #


class ReqShape:
    """Analytic region of one W3 requirement. ``removal`` for cuts; ``addition`` for raised letters (material added on
    top of whatever the other requirements leave)."""

    removal = False
    addition = False

    def __init__(self, req: Any):
        self.req = req

    def contains(self, p: Sequence[float]) -> bool:
        raise NotImplementedError

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        raise NotImplementedError

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        """(world point just inside the region, just outside, label)."""
        raise NotImplementedError

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        """World points well inside the region."""
        raise NotImplementedError

    def solid(self, reach: float) -> Any:
        raise NotImplementedError


class PrismShape(ReqShape):
    def __init__(self, req: Any):
        super().__init__(req)
        self.f = req.plane.frame()
        self.region = Region(req.outline, req.voids)
        self.s0, self.s1 = float(req.s0), float(req.s1)

    def contains(self, p: Sequence[float]) -> bool:
        u, v, s = self.f.local(p)
        return self.s0 <= s <= self.s1 and self.region.contains(u, v)

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        u, v, s = self.f.local(p)
        if self.s0 - tol <= s <= self.s1 + tol and self.region.distance(u, v) <= tol:
            return True
        return (abs(s - self.s0) <= tol or abs(s - self.s1) <= tol) and (self.region.contains(u, v) or self.region.distance(u, v) <= tol)

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        L = self.s1 - self.s0
        out = []
        for frac in (0.25, 0.5, 0.75):
            s = self.s0 + frac * L
            out += [(self.f.point(a[0], a[1], s), self.f.point(b[0], b[1], s), f"{w} at s {s:g}") for a, b, w in self.region.edge_pairs(delta)]
        if L > 4 * delta:
            for u, v in self.region.samples(3):
                out.append((self.f.point(u, v, self.s0 + delta), self.f.point(u, v, self.s0 - delta), f"end face s {self.s0:g}"))
                out.append((self.f.point(u, v, self.s1 - delta), self.f.point(u, v, self.s1 + delta), f"end face s {self.s1:g}"))
        return out

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        sm = 0.5 * (self.s0 + self.s1)
        return [(self.f.point(u, v, sm), "inside the prism") for u, v in self.region.samples(4)]

    def solid(self, reach: float) -> Any:
        f = self.f.moved(w=self.s0)
        face = region_face(self.region, lambda u, v: f.point(u, v), f.z, f.x)
        return BRepPrimAPI_MakePrism(face, gp_Vec(*[c * (self.s1 - self.s0) for c in f.z])).Shape()


class RevolveShape(ReqShape):
    def __init__(self, req: Any):
        super().__init__(req)
        a = unit3(req.axis)
        ref = req.ref_dir if req.ref_dir is not None else Frame.make(req.axis_origin, a).x
        f = Frame.make(req.axis_origin, a, ref)
        self.o, self.a, self.y, self.z = f.origin, a, f.x, f.y  # axis, radial reference, third axis
        self.region = Region(req.section)
        self.angle = math.radians(float(req.angle))
        self.full = req.angle >= 360.0 - 1e-9

    def _local(self, p: Sequence[float]) -> tuple[float, float, float]:
        d = (p[0] - self.o[0], p[1] - self.o[1], p[2] - self.o[2])
        u = dot3(d, self.a)
        w = (d[0] - u * self.a[0], d[1] - u * self.a[1], d[2] - u * self.a[2])
        r = math.sqrt(dot3(w, w))
        phi = math.atan2(dot3(w, self.z), dot3(w, self.y)) % (2 * math.pi)
        return u, r, phi

    def _point(self, u: float, r: float, phi: float) -> V3:
        return tuple(self.o[k] + u * self.a[k] + r * (math.cos(phi) * self.y[k] + math.sin(phi) * self.z[k]) for k in range(3))  # type: ignore[return-value]

    def contains(self, p: Sequence[float]) -> bool:
        u, r, phi = self._local(p)
        return (self.full or phi <= self.angle) and self.region.contains(u, r)

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        u, r, phi = self._local(p)
        if self.full or phi <= self.angle + tol / max(r, 1e-6):
            if self.region.distance(u, r) <= tol:
                return True
        if not self.full:
            near_end = min(phi, abs(phi - self.angle), 2 * math.pi - phi) * max(r, 1e-6) <= tol
            return near_end and (self.region.contains(u, r) or self.region.distance(u, r) <= tol)
        return False

    def _phis(self) -> list[float]:
        return [self.angle * f for f in (0.125, 0.375, 0.625, 0.875)]

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        out = []
        for phi in self._phis():
            for a, b, w in self.region.edge_pairs(delta):
                if min(a[1], b[1]) <= 0:
                    continue  # on the axis: the mirrored section is on the other side
                out.append((self._point(a[0], a[1], phi), self._point(b[0], b[1], phi), f"section {w} at {math.degrees(phi):.0f}°"))
        return out

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        return [(self._point(u, r, phi), "inside the revolved section") for phi in self._phis()[:2] for u, r in self.region.samples(3)]

    def solid(self, reach: float) -> Any:
        face = region_face(self.region, lambda u, v: self._point(u, v, 0.0), cross3(self.a, self.y), self.a)
        return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(*self.o), gp_Dir(*self.a)), self.angle).Shape()


class SweepShape(ReqShape):
    def __init__(self, req: Any):
        super().__init__(req)
        self.pf = req.plane.frame()
        self.segs = resolve_path(req.path.start, req.path.segments)
        self.region = Region(req.section)

    def _pieces(self, p: Sequence[float]) -> list[tuple[float, float, float, Seg]]:
        """Per path segment: (fraction along it, u, v) of the point in that segment's section frame."""
        x, y, w = self.pf.local(p)
        out = []
        for s in self.segs:
            if s.kind == "line":
                tx, ty = s.tangent(0.0)
                dx, dy = x - s.p0[0], y - s.p0[1]
                along = dx * tx + dy * ty
                u = -dx * ty + dy * tx  # left of travel
                out.append((along / s.length, u, w, s))
            else:
                c = s.center
                assert c is not None
                rho = math.hypot(x - c[0], y - c[1])
                ang = math.atan2(y - c[1], x - c[0])
                d = (ang - s.a0) % (2 * math.pi) if s.sweep > 0 else (s.a0 - ang) % (2 * math.pi)
                if d > abs(s.sweep) + 1e-9 and d > math.pi + abs(s.sweep) / 2:
                    d -= 2 * math.pi  # just before the start
                u = s.radius - rho if s.sweep > 0 else rho - s.radius
                out.append((d / abs(s.sweep), u, w, s))
        return out

    def contains(self, p: Sequence[float]) -> bool:
        return any(0.0 <= t <= 1.0 and self.region.contains(u, v) for t, u, v, _ in self._pieces(p))

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        pcs = self._pieces(p)
        for t, u, v, s in pcs:
            span = tol / max(s.length, 1e-9)
            if -span <= t <= 1 + span and self.region.distance(u, v) <= tol:
                return True
        for (t, u, v, s), end in ((pcs[0], 0.0), (pcs[-1], 1.0)):
            if abs(t - end) * s.length <= tol and (self.region.contains(u, v) or self.region.distance(u, v) <= tol):
                return True
        return False

    def _section_at(self, s: Seg, t: float) -> Callable[[float, float], V3]:
        q = s.point(t)
        tx, ty = s.tangent(t)
        base = self.pf.point(q[0], q[1])
        left = self.pf.vector(-ty, tx)
        return lambda u, v: tuple(base[k] + u * left[k] + v * self.pf.z[k] for k in range(3))  # type: ignore[return-value]

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        out = []
        for i, s in enumerate(self.segs):
            to3 = self._section_at(s, 0.5)
            out += [(to3(*a), to3(*b), f"section {w} at path segment {i + 1}") for a, b, w in self.region.edge_pairs(delta)]
        return out

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        return [(self._section_at(s, 0.5)(u, v), "inside the swept section") for s in self.segs for u, v in self.region.samples(2)]

    def solid(self, reach: float) -> Any:
        pieces = []
        for s in self.segs:
            to3 = self._section_at(s, 0.0)
            tx, ty = s.tangent(0.0)
            T = self.pf.vector(tx, ty)
            left = self.pf.vector(-ty, tx)
            face = region_face(self.region, to3, T, left)
            if s.kind == "line":
                pieces.append(BRepPrimAPI_MakePrism(face, gp_Vec(*[c * s.length for c in T])).Shape())
            else:
                assert s.center is not None
                axis = self.pf.z if s.sweep > 0 else tuple(-c for c in self.pf.z)
                pieces.append(BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(*self.pf.point(*s.center)), gp_Dir(*axis)), abs(s.sweep)).Shape())
        return fuse_all(pieces)


class CutShape(ReqShape):
    removal = True

    def __init__(self, req: Any):
        super().__init__(req)
        self.f = req.plane.frame()
        self.region = Region(req.outline)
        self.depth = None if req.through else float(req.depth)

    def contains(self, p: Sequence[float]) -> bool:
        u, v, s = self.f.local(p)
        return s <= 0.0 and (self.depth is None or s >= -self.depth) and self.region.contains(u, v)

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        u, v, s = self.f.local(p)
        lo = -math.inf if self.depth is None else -self.depth - tol
        if lo <= s <= tol and self.region.distance(u, v) <= tol:
            return True
        if self.depth is not None and abs(s + self.depth) <= tol and (self.region.contains(u, v) or self.region.distance(u, v) <= tol):
            return True
        return False

    def _depth(self, reach: float) -> float:
        return self.depth if self.depth is not None else reach

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        d = self._depth(reach)
        out = []
        for frac in ((0.5,) if self.depth is None else (0.25, 0.75)):
            s = -d * frac if self.depth is not None else -min(d, 2.0 * delta + 0.5 * d)
            out += [(self.f.point(a[0], a[1], s), self.f.point(b[0], b[1], s), f"{w} at depth {-s:g}") for a, b, w in self.region.edge_pairs(delta)]
        if self.depth is not None:
            for u, v in self.region.samples(3):
                out.append((self.f.point(u, v, -self.depth + delta), self.f.point(u, v, -self.depth - delta), f"floor at depth {self.depth:g}"))
        return out

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        d = self._depth(reach)
        fr = (0.1, 0.5, 0.9) if self.depth is not None else (0.05,)
        return [(self.f.point(u, v, -d * k if self.depth is not None else -k * min(d, 10.0)), "inside the cut")
                for u, v in self.region.samples(3) for k in fr]

    def solid(self, reach: float) -> Any:
        f = self.f.moved(w=1.0)
        face = region_face(self.region, lambda u, v: f.point(u, v), f.z, f.x)
        length = 1.0 + self._depth(reach)
        return BRepPrimAPI_MakePrism(face, gp_Vec(*[-c * length for c in f.z])).Shape()


class _WireSection(Region):
    """A coil's wire section centred on its centre line: analytic containment and distance (a loop's ray test is
    ambiguous exactly at the centre of a circle)."""

    def __init__(self, kind: str, s_rad: float, s_ax: float):
        super().__init__(CircleContour(center=(0.0, 0.0), diameter=s_rad) if kind == "circle"
                         else RectContour(center=(0.0, 0.0), size=(s_rad, s_ax)))
        self.kind, self.a, self.b = kind, s_rad / 2.0, s_ax / 2.0

    def contains(self, u: float, v: float) -> bool:
        if self.kind == "circle":
            return u * u + v * v <= self.a * self.a
        return abs(u) <= self.a and abs(v) <= self.b

    def distance(self, u: float, v: float) -> float:
        if self.kind == "circle":
            return abs(math.hypot(u, v) - self.a)
        du, dv = abs(u) - self.a, abs(v) - self.b
        if du <= 0 and dv <= 0:
            return min(-du, -dv)
        return math.hypot(max(du, 0.0), max(dv, 0.0))


class CoilShape(ReqShape):
    def __init__(self, req: Any):
        super().__init__(req)
        self.f = Frame.make(req.axis_origin, req.axis)
        self.law = req.law()
        s_rad, s_ax = req.section_size()
        self.kind = req.section.kind
        self.region = _WireSection(self.kind, s_rad, s_ax)

    def contains(self, p: Sequence[float]) -> bool:
        return self.law.contains(self.f.local(p), self.region.contains)

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        q = self.f.local(p)
        law = self.law
        if law.ground and (abs(q[2]) <= tol or abs(q[2] - law.length) <= tol):  # a ground face
            if any(-tol <= t <= law.turns + tol and (self.region.contains(u, v) or self.region.distance(u, v) <= tol)
                   for t, u, v in law.locate(q)):
                return True
        if law.ground and not (-tol <= q[2] <= law.length + tol):
            return False
        for t, u, v in law.locate(q):
            span = tol / law.speed(min(max(t, 0.0), law.turns))
            if -span <= t <= law.turns + span and self.region.distance(u, v) <= tol:
                return True
            for end in (0.0, law.turns):  # the wire's end caps
                if abs(t - end) <= span and (self.region.contains(u, v) or self.region.distance(u, v) <= tol):
                    return True
        return False

    def _to3(self, t: float) -> Callable[[float, float], V3]:
        return lambda u, v: self.f.point(*self.law.section_point(t, u, v))

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        out = []
        for t in self.law.stations(4):
            to3 = self._to3(t)
            out += [(to3(*a), to3(*b), f"coil section {w} at turn {t:.3g}") for a, b, w in self.region.edge_pairs(delta)]
        return out

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        return [(self._to3(t)(u, v), "inside the coil wire") for t in self.law.stations(4) for u, v in self.region.samples(2)]

    def solid(self, reach: float) -> Any:
        from .coil_occ import coil_solid

        return coil_solid(self.law, self.f, self.kind)


#: Glyph faces and outline segments per (text, font, height, font dirs): one check builds the text shapes several times.
_GLYPHS: dict[tuple[Any, ...], tuple[list[Any], Any, list[tuple[int, int]], str | None]] = {}


class TextShape(ReqShape):
    """Lettering: see the module doc. Without a usable font the region is empty (``error`` says why): the item is then
    not evaluated."""

    MAX_PAIRS = 32

    def __init__(self, req: Any):
        super().__init__(req)
        from . import text_geometry as tg

        self.f = req.plane.frame().moved(float(req.at[0]), float(req.at[1]), 0.0).rotated(float(req.rotation))
        self.depth = float(req.depth)
        self.removal, self.addition = self.depth < 0, self.depth > 0
        self.s0, self.s1 = (0.0, self.depth) if self.depth > 0 else (self.depth, 0.0)
        key = (req.text, req.font, float(req.height), tg.font_dirs())
        hit = _GLYPHS.get(key)
        if hit is None:
            error: str | None = None
            faces: list[Any] = []
            try:
                faces = tg.text_faces(req.text, tg.resolve_font(req.text, req.font), float(req.height))
            except Exception as exc:  # no usable font, no outline
                error = str(getattr(exc, "message", exc))
            segs: list[tuple[float, float, float, float]] = []
            ranges: list[tuple[int, int]] = []  # the segments of each glyph face
            for face in faces:
                first = len(segs)
                for wire in face.wires():
                    for edge in wire.edges():
                        n = 2 if edge.geom_type.name == "LINE" else 9
                        pts = [edge.position_at(i / (n - 1)) for i in range(n)]
                        segs += [(a.X, a.Y, b.X, b.Y) for a, b in zip(pts, pts[1:]) if abs(a.X - b.X) + abs(a.Y - b.Y) > 1e-12]
                ranges.append((first, len(segs)))
            hit = (faces, np.array(segs, dtype=float).reshape(-1, 4), ranges, error)
            if len(_GLYPHS) > 64:
                _GLYPHS.clear()
            _GLYPHS[key] = hit
        self.faces, self.segs, self.ranges, self.error = hit
        if len(self.segs):
            self.half_w = float(np.abs(self.segs[:, [0, 2]]).max())
            self.half_h = float(np.abs(self.segs[:, [1, 3]]).max())
        else:
            self.half_w = self.half_h = 0.0

    def _in2d(self, u: float, v: float) -> bool:
        if not len(self.segs) or abs(u) > self.half_w or abs(v) > self.half_h:
            return False
        x0, y0, x1, y1 = self.segs[:, 0], self.segs[:, 1], self.segs[:, 2], self.segs[:, 3]
        cross = (y0 > v) != (y1 > v)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0 + (v - y0) * (x1 - x0) / (y1 - y0)
        return bool(int((cross & (u < xi)).sum()) % 2)

    def contains(self, p: Sequence[float]) -> bool:
        u, v, s = self.f.local(p)
        return self.s0 <= s <= self.s1 and self._in2d(u, v)

    def on_boundary(self, p: Sequence[float], tol: float) -> bool:
        if not len(self.segs):
            return False
        u, v, s = self.f.local(p)
        return self.s0 - tol <= s <= self.s1 + tol and abs(u) <= self.half_w + tol and abs(v) <= self.half_h + tol

    def _pair(self, g: Any, delta: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """(just inside, just outside) the letters across segment ``g``, None when both or neither side is inside."""
        mx, my = (g[0] + g[2]) / 2.0, (g[1] + g[3]) / 2.0
        L = math.hypot(g[2] - g[0], g[3] - g[1])
        nx, ny = -(g[3] - g[1]) / L, (g[2] - g[0]) / L
        a, b = (mx + nx * delta, my + ny * delta), (mx - nx * delta, my - ny * delta)
        ia, ib = self._in2d(*a), self._in2d(*b)
        return None if ia == ib else ((a, b) if ia else (b, a))

    def _pairs2d(self, delta: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        long = [g for g in self.segs if math.hypot(g[2] - g[0], g[3] - g[1]) > 2.0 * delta]
        step = max(1, len(long) // self.MAX_PAIRS)
        return [pr for g in long[::step][: self.MAX_PAIRS] if (pr := self._pair(g, delta)) is not None]

    def _face_points(self, delta: float) -> list[tuple[float, float]]:
        """One point just inside every glyph face (across its longest outline segment): a missing or different letter
        is probed even where the sampled outline pairs miss it."""
        out = []
        for a, b in self.ranges:
            seg = self.segs[a:b]
            if not len(seg):
                continue
            g = seg[int(np.argmax(np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])))]
            pr = self._pair(g, delta)
            if pr is not None:
                out.append(pr[0])
        return out

    def probe_pairs(self, delta: float, reach: float) -> list[tuple[V3, V3, str]]:
        sm = 0.5 * (self.s0 + self.s1)
        return [(self.f.point(a[0], a[1], sm), self.f.point(b[0], b[1], sm), "letter outline") for a, b in self._pairs2d(delta)]

    def samples(self, reach: float) -> list[tuple[V3, str]]:
        sm = 0.5 * (self.s0 + self.s1)
        return [(self.f.point(a[0], a[1], sm), "inside a letter") for a in self._face_points(min(0.05, max(0.01, abs(self.depth) / 3.0)))]

    def volume(self) -> float:
        """Signed material of the letters: glyph area × depth (+ raised, − engraved)."""
        return sum(float(f.area) for f in self.faces) * self.depth

    def mask(self, margin: float = 0.2) -> Any:
        """A box around the letters' slab (text block + ``margin``, 0.01 mm beyond both slab faces): the volume
        accounting compares the solid and the reference with it removed (the letters are checked on their own)."""
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

        e = 0.01
        corner = self.f.point(-self.half_w - margin, -self.half_h - margin, self.s0 - e)
        ax = gp_Ax2(gp_Pnt(*corner), gp_Dir(*self.f.z), gp_Dir(*self.f.x))
        return BRepPrimAPI_MakeBox(ax, 2 * (self.half_w + margin), 2 * (self.half_h + margin), self.s1 - self.s0 + 2 * e).Shape()

    def solid(self, reach: float) -> Any:
        import build123d as bd

        if not self.faces:
            raise ValueError(f"{self.req.id}: no letters ({self.error or 'no outline'})")
        start = self.f.moved(w=1.0) if self.removal else self.f
        loc = bd.Plane(origin=start.origin, x_dir=start.x, z_dir=start.z).location
        length = -(1.0 + abs(self.depth)) if self.removal else self.depth
        vec = bd.Vector(*start.z) * length
        return bd.Compound([bd.Solid.extrude(face.moved(loc), vec) for face in self.faces]).wrapped


def shapes_of(reqs: Sequence[Any]) -> list[ReqShape]:
    out: list[ReqShape] = []
    for r in reqs:
        if r.kind == "prism":
            out.append(PrismShape(r))
        elif r.kind == "revolve":
            out.append(RevolveShape(r))
        elif r.kind == "sweep":
            out.append(SweepShape(r))
        elif r.kind == "cut":
            out.append(CutShape(r))
        elif r.kind == "coil":
            out.append(CoilShape(r))
        elif r.kind == "text":
            out.append(TextShape(r))
    return out


# --------------------------------------------------------------------------- #
# Composite occupancy of the whole requirement list
# --------------------------------------------------------------------------- #


def _contour_has(c: Any, x: float, y: float) -> bool:
    outer, holes = contour_loops(c)
    return point_in_region((x, y), outer, holes)


def occupancy(reqs: Sequence[Any], lo: V3, hi: V3) -> Callable[[Sequence[float]], bool]:
    """Material the requirement list implies at a point (see the module docstring)."""
    shapes = shapes_of(reqs)
    mats = [s for s in shapes if not s.removal and not s.addition]
    cuts = [s for s in shapes if s.removal]
    adds = [s for s in shapes if s.addition]
    bands = [r for r in reqs if r.kind == "band"]
    holes = [r for r in reqs if r.kind == "hole"]
    pockets = [r for r in reqs if r.kind == "pocket"]

    def in_band(p: Sequence[float]) -> bool:
        return any(b.z0 <= p[2] <= b.z1 and _contour_has(b.outer, p[0], p[1]) and not any(_contour_has(v, p[0], p[1]) for v in b.voids)
                   for b in bands)

    def material(p: Sequence[float]) -> bool:
        if any(m.contains(p) for m in mats) or in_band(p):
            return True
        if mats:
            return False
        if any(b.z0 <= p[2] <= b.z1 for b in bands):
            return False
        return all(lo[k] <= p[k] <= hi[k] for k in range(3))

    def in_hole(p: Sequence[float]) -> bool:
        for h in holes:
            d = unit3(h.direction)
            r = h.diameter / 2.0
            for e in h.entries:
                w = (p[0] - e[0], p[1] - e[1], p[2] - e[2])
                t = dot3(w, d)
                if t < 0 or (not h.through and h.depth is not None and t > h.depth):
                    continue
                rad = math.sqrt(max(0.0, dot3(w, w) - t * t))
                if rad <= r:
                    return True
                if h.counterbore is not None and t <= h.counterbore.depth and rad <= h.counterbore.diameter / 2.0:
                    return True
                if h.countersink is not None:
                    rs = h.countersink.diameter / 2.0
                    if rad <= rs - t * math.tan(math.radians(h.countersink.angle) / 2.0):
                        return True
        return False

    def in_pocket(p: Sequence[float]) -> bool:
        for pk in pockets:
            top = pk.entry_z
            bot = -math.inf if pk.through or pk.depth is None else pk.entry_z - pk.depth
            if bot <= p[2] <= top and _contour_has(pk.contour, p[0], p[1]):
                return True
        return False

    def occ(p: Sequence[float]) -> bool:
        if any(a.contains(p) for a in adds):  # raised letters stand on whatever the rest leaves
            return True
        return material(p) and not (any(c.contains(p) for c in cuts) or in_hole(p) or in_pocket(p))

    return occ


def certain(occ: Callable[[Sequence[float]], bool], p: Sequence[float], h: float) -> bool | None:
    """occ(p) when it does not change within ``h`` along X, Y, Z (else None: an ambiguous spot)."""
    v = occ(p)
    for k in range(3):
        for sgn in (-1.0, 1.0):
            q = list(p)
            q[k] += sgn * h
            if occ(q) != v:
                return None
    return v


def explained(points: Sequence[Sequence[float]], shapes: Sequence[ReqShape], tol: float) -> bool:
    """Every point lies on the boundary of some W3 requirement."""
    return bool(points) and bool(shapes) and all(any(s.on_boundary(p, tol) for s in shapes) for p in points)
