"""2D sketches, paths and workplanes of the geometry library (W3).

Pure Python (pydantic + math, no geometry kernel), so ops validation, the kernel (point placement only), datums, the
independent feature checks (:mod:`app.cad.feature_checks`) and the requirement checker can share one definition of
what a profile *means*, while the kernel builds the B-rep with build123d and the checks measure it with OCP.

Conventions (units mm and degrees; see ``docs/development/mechanical-engineering/geometry-ops.zh-CN.md``):

- A sketch loop is ``start`` plus segments. ``line`` goes straight to ``to``; ``arc`` goes from the current point to
  ``to`` on the circle around ``center`` (``ccw`` = counter-clockwise seen from the plane normal, i.e. from +u
  toward +v). A single arc whose ``to`` equals ``start`` is a full circle. A loop that does not end on ``start`` is
  closed with a straight line. Loops may run either way round; holes are separate loops inside the outer loop.
- A path (sweeps) is ``start`` plus segments too, but open and tangent-continuous.
- A frame maps plane coordinates ``(u, v, w)`` to world: ``origin + u·x + v·y + w·z``; ``z`` is the plane normal and
  ``y = z × x`` (right-handed, as build123d ``Plane``). The named planes are build123d's: ``XY`` (u = X, v = Y,
  normal +Z), ``XZ`` (u = X, v = Z, normal −Y), ``YZ`` (u = Y, v = Z, normal +X). A :class:`Workplane` gives any
  origin / normal / in-plane x direction; without ``x_dir`` the x axis is the dominant-axis rule of
  ``ops.hole_frame`` (so normal +Z, −Y, +X reproduce XY, XZ, YZ).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]

EPS_LEN = 1e-6  # mm: shorter segments are degenerate
CLOSE_SNAP = 1e-6  # mm: a loop ending this close to its start is closed without an extra line
MAX_SEGMENTS = 256
TANGENT_TOL_DEG = 0.05  # sweep paths: largest corner between consecutive segments


def arc_radius_tol(r: float) -> float:
    """How far the end of an arc may be off the circle through its start (rounded coordinates)."""
    return max(1e-3, 1e-4 * r)


# --------------------------------------------------------------------------- #
# Small vector helpers
# --------------------------------------------------------------------------- #


def _add3(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul3(a: Sequence[float], s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot3(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross3(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit3(v: Sequence[float]) -> Vec3:
    n = math.sqrt(dot3(v, v))
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return (v[0] / n, v[1] / n, v[2] / n)


def default_x(normal: Sequence[float]) -> Vec3:
    """In-plane x axis for a plane with this normal: the dominant-axis rule of ``ops.hole_frame`` (Z-dominant → X,
    X-dominant → Y, Y-dominant → X, projected into the plane)."""
    d = unit3(normal)
    ax, ay, az = abs(d[0]), abs(d[1]), abs(d[2])
    if az >= ax and az >= ay:
        ref: Vec3 = (1.0, 0.0, 0.0)
    elif ax >= ay:
        ref = (0.0, 1.0, 0.0)
    else:
        ref = (1.0, 0.0, 0.0)
    return unit3(_add3(ref, _mul3(d, -dot3(ref, d))))


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Frame:
    """An orthonormal right-handed frame (``z`` = plane normal, ``y = z × x``)."""

    origin: Vec3
    x: Vec3
    y: Vec3
    z: Vec3

    @staticmethod
    def make(origin: Sequence[float], z: Sequence[float], x: Sequence[float] | None = None) -> "Frame":
        zz = unit3(z)
        if x is None:
            xx = default_x(zz)
        else:
            xp = _add3(x, _mul3(zz, -dot3(x, zz)))
            if math.sqrt(dot3(xp, xp)) < 1e-9:
                raise ValueError("x_dir is parallel to the normal")
            xx = unit3(xp)
        return Frame(tuple(float(c) for c in origin), xx, cross3(zz, xx), zz)  # type: ignore[arg-type]

    def point(self, u: float, v: float, w: float = 0.0) -> Vec3:
        o, x, y, z = self.origin, self.x, self.y, self.z
        return (o[0] + u * x[0] + v * y[0] + w * z[0], o[1] + u * x[1] + v * y[1] + w * z[1], o[2] + u * x[2] + v * y[2] + w * z[2])

    def vector(self, u: float, v: float, w: float = 0.0) -> Vec3:
        x, y, z = self.x, self.y, self.z
        return (u * x[0] + v * y[0] + w * z[0], u * x[1] + v * y[1] + w * z[1], u * x[2] + v * y[2] + w * z[2])

    def local(self, p: Sequence[float]) -> Vec3:
        d = (p[0] - self.origin[0], p[1] - self.origin[1], p[2] - self.origin[2])
        return (dot3(d, self.x), dot3(d, self.y), dot3(d, self.z))

    def moved(self, u: float = 0.0, v: float = 0.0, w: float = 0.0) -> "Frame":
        return Frame(self.point(u, v, w), self.x, self.y, self.z)

    def rotated(self, deg: float) -> "Frame":
        """Rotated about its normal (counter-clockwise seen from the normal)."""
        if abs(deg) < 1e-12:
            return self
        a = math.radians(deg)
        x = unit3(_add3(_mul3(self.x, math.cos(a)), _mul3(self.y, math.sin(a))))
        return Frame(self.origin, x, cross3(self.z, x), self.z)

    def flipped(self) -> "Frame":
        """Normal reversed, x kept (y follows)."""
        z = _mul3(self.z, -1.0)
        return Frame(self.origin, self.x, cross3(z, self.x), z)


NAMED_PLANES: dict[str, Frame] = {
    "XY": Frame((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "XZ": Frame((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "YZ": Frame((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def _finite(v: Iterable[float], what: str) -> None:
    if not all(math.isfinite(float(c)) for c in v):
        raise ValueError(f"{what} must be finite numbers")


class Workplane(BaseModel):
    """An arbitrary plane: ``origin``, ``normal`` and the in-plane ``x_dir`` (optional; projected into the plane)."""

    model_config = ConfigDict(extra="forbid")
    origin: Vec3 = (0.0, 0.0, 0.0)
    normal: Vec3 = (0.0, 0.0, 1.0)
    x_dir: Vec3 | None = None

    @model_validator(mode="after")
    def _check(self) -> "Workplane":
        _finite(self.origin, "origin")
        _finite(self.normal, "normal")
        if dot3(self.normal, self.normal) < 1e-12:
            raise ValueError("normal must be a non-zero vector")
        if self.x_dir is not None:
            _finite(self.x_dir, "x_dir")
            n, x = unit3(self.normal), self.x_dir
            if dot3(x, x) < 1e-12:
                raise ValueError("x_dir must be a non-zero vector")
            if abs(dot3(n, unit3(x))) > math.cos(math.radians(1.0)):
                raise ValueError("x_dir must not be parallel to the normal (it is projected into the plane)")
        return self

    def frame(self) -> Frame:
        return Frame.make(self.origin, self.normal, self.x_dir)


DATUM_REF_RE = re.compile(r"^(origin|F-\d{2,}\.[A-Za-z0-9_.\[\]]{1,40})$")


class DatumRef(BaseModel):
    """A plane taken from a named datum of an earlier op (``F-01.top``, ``F-03.end`` …, see :mod:`app.cad.datums`):
    its origin and normal (the outward normal for a face datum), shifted by ``offset`` along the normal; ``flip``
    reverses the normal. A datum the earlier ops no longer produce fails the build with ``datum_missing``."""

    model_config = ConfigDict(extra="forbid")
    ref: str
    offset: float = 0.0
    flip: bool = False

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not DATUM_REF_RE.match(v):
            raise ValueError('ref must name a datum like "F-01.top", "F-02.axis" or "origin"')
        return v

    @property
    def op_id(self) -> str | None:
        return None if self.ref == "origin" else self.ref.split(".", 1)[0]


PlaneName = Literal["XY", "XZ", "YZ"]
PlaneSpec = Union[PlaneName, Workplane, DatumRef]


class LineSeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["line"] = "line"
    to: Vec2


class ArcSeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["arc"] = "arc"
    to: Vec2
    center: Vec2
    ccw: bool = True


Segment = Annotated[Union[LineSeg, ArcSeg], Field(discriminator="kind")]


class Loop(BaseModel):
    """A closed loop (a hole of a sketch profile)."""

    model_config = ConfigDict(extra="forbid")
    start: Vec2
    segments: list[Segment] = Field(min_length=1, max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def _check(self) -> "Loop":
        resolve_loop(self.start, self.segments, what="hole loop")
        return self


class SketchProfile(BaseModel):
    """A closed outline of lines and arcs (``start`` + ``segments``) with optional inner loops (``holes``)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["sketch"] = "sketch"
    start: Vec2
    segments: list[Segment] = Field(min_length=1, max_length=MAX_SEGMENTS)
    holes: list[Loop] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _check(self) -> "SketchProfile":
        outer = resolve_loop(self.start, self.segments, what="outline")
        inner = [resolve_loop(h.start, h.segments, what=f"hole loop {i + 1}") for i, h in enumerate(self.holes)]
        check_holes(outer, inner)
        return self

    def loops(self) -> tuple[list["Seg"], list[list["Seg"]]]:
        return resolve_loop(self.start, self.segments), [resolve_loop(h.start, h.segments) for h in self.holes]


class PathSpec(BaseModel):
    """An open, tangent-continuous path of lines and arcs in a plane (sweeps)."""

    model_config = ConfigDict(extra="forbid")
    start: Vec2
    segments: list[Segment] = Field(min_length=1, max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def _check(self) -> "PathSpec":
        resolve_path(self.start, self.segments)
        return self


# --------------------------------------------------------------------------- #
# Resolved segments
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Seg:
    """A resolved segment. ``sweep`` is signed (+ = counter-clockwise), ±2π for a full circle."""

    kind: Literal["line", "arc"]
    p0: Vec2
    p1: Vec2
    center: Vec2 | None = None
    radius: float = 0.0
    a0: float = 0.0
    sweep: float = 0.0

    @property
    def length(self) -> float:
        if self.kind == "line":
            return math.dist(self.p0, self.p1)
        return abs(self.sweep) * self.radius

    def point(self, t: float) -> Vec2:
        if self.kind == "line":
            return (self.p0[0] + t * (self.p1[0] - self.p0[0]), self.p0[1] + t * (self.p1[1] - self.p0[1]))
        a = self.a0 + t * self.sweep
        c = self.center
        assert c is not None
        return (c[0] + self.radius * math.cos(a), c[1] + self.radius * math.sin(a))

    def tangent(self, t: float) -> Vec2:
        """Unit travel direction at parameter t."""
        if self.kind == "line":
            dx, dy = self.p1[0] - self.p0[0], self.p1[1] - self.p0[1]
            n = math.hypot(dx, dy)
            return (dx / n, dy / n)
        a = self.a0 + t * self.sweep
        s = 1.0 if self.sweep > 0 else -1.0
        return (-math.sin(a) * s, math.cos(a) * s)

    def reversed(self) -> "Seg":
        if self.kind == "line":
            return Seg("line", self.p1, self.p0)
        return Seg("arc", self.p1, self.p0, self.center, self.radius, self.a0 + self.sweep, -self.sweep)

    def as_dict(self) -> dict[str, Any]:
        """The segment in op form (``to`` / ``center`` / ``ccw``)."""
        if self.kind == "line":
            return {"kind": "line", "to": [self.p1[0], self.p1[1]]}
        assert self.center is not None
        return {"kind": "arc", "to": [self.p1[0], self.p1[1]], "center": [self.center[0], self.center[1]], "ccw": self.sweep > 0}


def _resolve(start: Sequence[float], segments: Sequence[Any], *, closed: bool, what: str) -> list[Seg]:
    if not segments:
        raise ValueError(f"{what}: needs at least one segment")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"{what}: at most {MAX_SEGMENTS} segments")
    cur: Vec2 = (float(start[0]), float(start[1]))
    _finite(cur, f"{what} start")
    out: list[Seg] = []
    for i, s in enumerate(segments):
        kind = s.kind if hasattr(s, "kind") else s["kind"]
        to_raw = s.to if hasattr(s, "to") else s["to"]
        to: Vec2 = (float(to_raw[0]), float(to_raw[1]))
        _finite(to, f"{what} segment {i + 1}")
        if kind == "line":
            if math.dist(cur, to) < EPS_LEN:
                raise ValueError(f"{what} segment {i + 1} (line) has zero length")
            out.append(Seg("line", cur, to))
        else:
            c_raw = s.center if hasattr(s, "center") else s["center"]
            ccw = bool(s.ccw if hasattr(s, "ccw") else s.get("ccw", True))
            c: Vec2 = (float(c_raw[0]), float(c_raw[1]))
            _finite(c, f"{what} segment {i + 1} centre")
            r0, r1 = math.dist(cur, c), math.dist(to, c)
            if r0 < 1e-3:
                raise ValueError(f"{what} segment {i + 1} (arc): the start point is on the centre (radius 0)")
            if abs(r0 - r1) > arc_radius_tol(r0):
                raise ValueError(f"{what} segment {i + 1} (arc): the start is {r0:g} from the centre but the end is {r1:g}; "
                                 "both ends must lie on one circle around center")
            a0 = math.atan2(cur[1] - c[1], cur[0] - c[0])
            if math.dist(cur, to) < EPS_LEN:
                if not closed or len(segments) != 1:
                    raise ValueError(f"{what} segment {i + 1} (arc) ends where it starts; a full circle must be the only "
                                     "segment of its loop")
                sweep = 2 * math.pi if ccw else -2 * math.pi
            else:
                a1 = math.atan2(to[1] - c[1], to[0] - c[0])
                d = (a1 - a0) % (2 * math.pi)
                sweep = d if ccw else d - 2 * math.pi
                if abs(sweep) < 1e-9:
                    raise ValueError(f"{what} segment {i + 1} (arc) has zero length")
            out.append(Seg("arc", cur, to, c, 0.5 * (r0 + r1), a0, sweep))
        cur = to
    if closed:
        s0: Vec2 = (float(start[0]), float(start[1]))
        gap = math.dist(cur, s0)
        if gap > CLOSE_SNAP:
            out.append(Seg("line", cur, s0))
        elif gap > 0:  # snap the last end point onto the start so the loop is exactly closed
            last = out[-1]
            out[-1] = Seg(last.kind, last.p0, s0, last.center, last.radius, last.a0, last.sweep)
    return out


def resolve_loop(start: Sequence[float], segments: Sequence[Any], *, what: str = "profile") -> list[Seg]:
    """Resolve and validate a closed loop (auto-closed with a line). Raises ``ValueError`` with a precise message."""
    segs = _resolve(start, segments, closed=True, what=what)
    area = loop_area(segs)
    if abs(area) < 1e-9:
        raise ValueError(f"{what}: the loop encloses no area")
    if polyline_self_intersects(polygonize(segs)):
        raise ValueError(f"{what}: the loop crosses itself")
    return segs


def resolve_path(start: Sequence[float], segments: Sequence[Any], *, what: str = "path") -> list[Seg]:
    """Resolve and validate an open, tangent-continuous path."""
    segs = _resolve(start, segments, closed=False, what=what)
    if math.dist(segs[0].p0, segs[-1].p1) < EPS_LEN:
        raise ValueError(f"{what}: a closed path is not supported for sweeps (use revolve for a ring)")
    cos_tol = math.cos(math.radians(TANGENT_TOL_DEG))
    for i, (a, b) in enumerate(zip(segs, segs[1:])):
        ta, tb = a.tangent(1.0), b.tangent(0.0)
        if ta[0] * tb[0] + ta[1] * tb[1] < cos_tol:
            ang = math.degrees(math.acos(max(-1.0, min(1.0, ta[0] * tb[0] + ta[1] * tb[1]))))
            raise ValueError(f"{what}: segments {i + 1} and {i + 2} meet at a {ang:.2f}° corner; a sweep path must be tangent "
                             "continuous (put an arc between them)")
    if polyline_self_intersects(polygonize(segs), closed=False):
        raise ValueError(f"{what}: the path crosses itself")
    return segs


# --------------------------------------------------------------------------- #
# Exact measures of line / arc loops
# --------------------------------------------------------------------------- #


def _arc_terms(s: Seg) -> tuple[float, float, float]:
    """(∮ x dy − y dx, ∮ x² dy, ∮ y² dx) over one arc (exact)."""
    assert s.center is not None
    cx, cy, r = s.center[0], s.center[1], s.radius
    a0, a1 = s.a0, s.a0 + s.sweep
    d_sin, d_cos = math.sin(a1) - math.sin(a0), math.cos(a1) - math.cos(a0)
    green = r * r * s.sweep + cx * r * d_sin - cy * r * d_cos

    def i_cos2(a: float) -> float:
        return a / 2 + math.sin(2 * a) / 4

    def i_cos3(a: float) -> float:
        return math.sin(a) - math.sin(a) ** 3 / 3

    def i_sin2(a: float) -> float:
        return a / 2 - math.sin(2 * a) / 4

    def i_sin3(a: float) -> float:
        return -math.cos(a) + math.cos(a) ** 3 / 3

    x2dy = cx * cx * r * d_sin + 2 * cx * r * r * (i_cos2(a1) - i_cos2(a0)) + r ** 3 * (i_cos3(a1) - i_cos3(a0))
    y2dx = -(cy * cy * r * (-d_cos) + 2 * cy * r * r * (i_sin2(a1) - i_sin2(a0)) + r ** 3 * (i_sin3(a1) - i_sin3(a0)))
    return green, x2dy, y2dx


def _seg_terms(s: Seg) -> tuple[float, float, float]:
    if s.kind == "arc":
        return _arc_terms(s)
    (x0, y0), (x1, y1) = s.p0, s.p1
    return (x0 * y1 - x1 * y0, (y1 - y0) * (x0 * x0 + x0 * x1 + x1 * x1) / 3.0, (x1 - x0) * (y0 * y0 + y0 * y1 + y1 * y1) / 3.0)


def loop_area(segs: Sequence[Seg]) -> float:
    """Signed area (counter-clockwise positive)."""
    return 0.5 * sum(_seg_terms(s)[0] for s in segs)


def loop_area_centroid(segs: Sequence[Seg]) -> tuple[float, tuple[float, float]]:
    """(signed area, centroid) of the region a loop encloses (exact for lines and arcs)."""
    g = x2 = y2 = 0.0
    for s in segs:
        a, b, c = _seg_terms(s)
        g += a
        x2 += b
        y2 += c
    area = 0.5 * g
    if abs(area) < 1e-15:
        return 0.0, (0.0, 0.0)
    return area, (x2 / (2 * area), -y2 / (2 * area))


def loop_length(segs: Sequence[Seg]) -> float:
    return sum(s.length for s in segs)


def region_area_centroid(outer: Sequence[Seg], holes: Sequence[Sequence[Seg]] = ()) -> tuple[float, tuple[float, float]]:
    """(area > 0, centroid) of an outline minus its holes."""
    a, c = loop_area_centroid(outer)
    a = abs(a)
    mx, my = a * c[0], a * c[1]
    for h in holes:
        ah, ch = loop_area_centroid(h)
        ah = abs(ah)
        a -= ah
        mx -= ah * ch[0]
        my -= ah * ch[1]
    return a, ((mx / a, my / a) if a > 1e-15 else (0.0, 0.0))


def _angle_in(a: float, s: Seg) -> bool:
    """Whether polar angle ``a`` lies on the arc's sweep."""
    if abs(s.sweep) >= 2 * math.pi - 1e-12:
        return True
    if s.sweep > 0:
        return (a - s.a0) % (2 * math.pi) <= s.sweep + 1e-12
    return (s.a0 - a) % (2 * math.pi) <= -s.sweep + 1e-12


def seg_distance(p: Sequence[float], s: Seg) -> float:
    if s.kind == "line":
        (x0, y0), (x1, y1) = s.p0, s.p1
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2)) if L2 > 0 else 0.0
        return math.hypot(p[0] - x0 - t * dx, p[1] - y0 - t * dy)
    c = s.center
    assert c is not None
    a = math.atan2(p[1] - c[1], p[0] - c[0])
    if _angle_in(a, s):
        return abs(math.hypot(p[0] - c[0], p[1] - c[1]) - s.radius)
    return min(math.dist(p, s.p0), math.dist(p, s.p1))


def loop_distance(p: Sequence[float], segs: Sequence[Seg]) -> float:
    return min(seg_distance(p, s) for s in segs)


def _arc_y_pieces(s: Seg) -> list[tuple[float, float]]:
    """The arc split at its y extremes (θ = ±π/2): angle ranges (start, end) along the sweep, each y-monotone."""
    a0, a1 = s.a0, s.a0 + s.sweep
    lo, hi = min(a0, a1), max(a0, a1)
    cuts = [lo]
    k = math.ceil((lo - math.pi / 2) / math.pi)
    while True:
        t = math.pi / 2 + k * math.pi
        if t >= hi:
            break
        if t > lo:
            cuts.append(t)
        k += 1
    cuts.append(hi)
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]


def point_in_loop(p: Sequence[float], segs: Sequence[Seg]) -> bool:
    """Exact even-odd test with a horizontal ray toward +x (half-open rule at vertices)."""
    px, py = float(p[0]), float(p[1])
    inside = False
    for s in segs:
        if s.kind == "line":
            (x0, y0), (x1, y1) = s.p0, s.p1
            if (y0 > py) != (y1 > py):
                x = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
                if x > px:
                    inside = not inside
            continue
        c = s.center
        assert c is not None
        r = s.radius
        for t0, t1 in _arc_y_pieces(s):
            y0, y1 = c[1] + r * math.sin(t0), c[1] + r * math.sin(t1)
            if (y0 > py) != (y1 > py):
                q = max(-1.0, min(1.0, (py - c[1]) / r))
                base = math.asin(q)
                cands = [base + 2 * math.pi * k for k in range(-3, 4)] + [math.pi - base + 2 * math.pi * k for k in range(-3, 4)]
                lo, hi = min(t0, t1), max(t0, t1)
                th = next((a for a in cands if lo - 1e-12 <= a <= hi + 1e-12), None)
                if th is not None and c[0] + r * math.cos(th) > px:
                    inside = not inside
    return inside


def point_in_region(p: Sequence[float], outer: Sequence[Seg], holes: Sequence[Sequence[Seg]] = ()) -> bool:
    return point_in_loop(p, outer) and not any(point_in_loop(p, h) for h in holes)


def loop_support(segs: Sequence[Seg], d: Sequence[float]) -> float:
    """max over the loop of (q · d) (exact)."""
    best = -math.inf
    for s in segs:
        for q in (s.p0, s.p1):
            best = max(best, q[0] * d[0] + q[1] * d[1])
        if s.kind == "arc":
            c = s.center
            assert c is not None
            n = math.hypot(d[0], d[1])
            if n > 0 and _angle_in(math.atan2(d[1], d[0]), s):
                best = max(best, c[0] * d[0] + c[1] * d[1] + s.radius * n)
    return best


def polygonize(segs: Sequence[Seg], max_step_deg: float = 2.0) -> list[Vec2]:
    """Vertices of a polyline approximation (arcs every ``max_step_deg``); the start point is not repeated."""
    pts: list[Vec2] = []
    for s in segs:
        pts.append(s.p0)
        if s.kind == "arc":
            n = max(2, min(720, math.ceil(abs(math.degrees(s.sweep)) / max_step_deg)))
            pts += [s.point(k / n) for k in range(1, n)]
    if segs and math.dist(segs[-1].p1, segs[0].p0) > CLOSE_SNAP:
        pts.append(segs[-1].p1)  # open path: keep its end point
    return pts


def _proper_cross(a: Vec2, b: Vec2, c: Vec2, d: Vec2) -> bool:
    def o(p: Vec2, q: Vec2, r: Vec2) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    eps = 1e-12
    o1, o2, o3, o4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
    return ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps))


def polyline_self_intersects(pts: Sequence[Vec2], closed: bool = True) -> bool:
    n = len(pts)
    m = n if closed else n - 1
    if m < 3:
        return False
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(m)]
    # sort by x-extent for a cheap sweep
    order = sorted(range(m), key=lambda i: min(edges[i][0][0], edges[i][1][0]))
    for oi, i in enumerate(order):
        a, b = edges[i]
        xmax = max(a[0], b[0])
        for j in order[oi + 1:]:
            c, d = edges[j]
            if min(c[0], d[0]) > xmax:
                break
            if abs(i - j) <= 1 or (closed and abs(i - j) == m - 1):
                continue
            if _proper_cross(a, b, c, d):
                return True
    return False


def check_holes(outer: Sequence[Seg], holes: Sequence[Sequence[Seg]]) -> None:
    """Hole loops must lie inside the outline and not overlap each other (polygon approximation)."""
    outer_poly = polygonize(outer)
    polys = [polygonize(h) for h in holes]
    for i, h in enumerate(holes):
        if not all(point_in_loop(q, outer) for q in polys[i]) or _loops_cross(outer_poly, polys[i]):
            raise ValueError(f"hole loop {i + 1} is not inside the outline")
        for j in range(i):
            if (any(point_in_loop(q, holes[j]) for q in polys[i]) or any(point_in_loop(q, h) for q in polys[j])
                    or _loops_cross(polys[i], polys[j])):
                raise ValueError(f"hole loops {j + 1} and {i + 1} overlap")


def _loops_cross(a: Sequence[Vec2], b: Sequence[Vec2]) -> bool:
    ea = [(a[i], a[(i + 1) % len(a)]) for i in range(len(a))]
    eb = [(b[i], b[(i + 1) % len(b)]) for i in range(len(b))]
    for p, q in ea:
        for r, s in eb:
            if _proper_cross(p, q, r, s):
                return True
    return False


# --------------------------------------------------------------------------- #
# Profiles of every kind (duck-typed on ``kind``: rect / circle / slot / polygon / ring / sketch)
# --------------------------------------------------------------------------- #


def profile_loops(p: Any) -> tuple[list[Seg], list[list[Seg]]]:
    """The loops of any extrude profile, in profile coordinates (rect / slot / circle / ring centred on 0)."""
    k = p.kind
    if k == "sketch":
        return p.loops()
    if k == "polygon":
        pts = [(float(u), float(v)) for u, v in p.points]
        return _resolve(pts[0], [{"kind": "line", "to": q} for q in pts[1:]], closed=True, what="polygon"), []
    if k == "circle":
        r = float(p.radius)
        return [Seg("arc", (r, 0.0), (r, 0.0), (0.0, 0.0), r, 0.0, 2 * math.pi)], []
    if k == "ring":
        R, r = float(p.outer_radius), float(p.inner_radius)
        return ([Seg("arc", (R, 0.0), (R, 0.0), (0.0, 0.0), R, 0.0, 2 * math.pi)],
                [[Seg("arc", (r, 0.0), (r, 0.0), (0.0, 0.0), r, 0.0, 2 * math.pi)]])
    if k == "slot":
        L, W = float(p.length), float(p.width)
        h, r = (L - W) / 2.0, W / 2.0
        segs = [Seg("line", (-h, -r), (h, -r)), Seg("arc", (h, -r), (h, r), (h, 0.0), r, -math.pi / 2, math.pi),
                Seg("line", (h, r), (-h, r)), Seg("arc", (-h, r), (-h, -r), (-h, 0.0), r, math.pi / 2, math.pi)]
        return segs, []
    # rect
    w, ln, cr = float(p.width), float(p.length), float(getattr(p, "corner_radius", 0.0) or 0.0)
    a, b = w / 2.0, ln / 2.0
    if cr <= 0:
        pts = [(-a, -b), (a, -b), (a, b), (-a, b)]
        return _resolve(pts[0], [{"kind": "line", "to": q} for q in pts[1:]], closed=True, what="rect"), []
    segs = [Seg("line", (-a + cr, -b), (a - cr, -b)), Seg("arc", (a - cr, -b), (a, -b + cr), (a - cr, -b + cr), cr, -math.pi / 2, math.pi / 2),
            Seg("line", (a, -b + cr), (a, b - cr)), Seg("arc", (a, b - cr), (a - cr, b), (a - cr, b - cr), cr, 0.0, math.pi / 2),
            Seg("line", (a - cr, b), (-a + cr, b)), Seg("arc", (-a + cr, b), (-a, b - cr), (-a + cr, b - cr), cr, math.pi / 2, math.pi / 2),
            Seg("line", (-a, b - cr), (-a, -b + cr)), Seg("arc", (-a, -b + cr), (-a + cr, -b), (-a + cr, -b + cr), cr, math.pi, math.pi / 2)]
    return segs, []


def profile_area_centroid(p: Any) -> tuple[float, tuple[float, float]]:
    """Exact area and centroid of any extrude profile (closed-form per kind)."""
    k = p.kind
    if k == "rect":
        cr = float(getattr(p, "corner_radius", 0.0) or 0.0)
        return float(p.width) * float(p.length) - (4.0 - math.pi) * cr * cr, (0.0, 0.0)
    if k == "circle":
        return math.pi * float(p.radius) ** 2, (0.0, 0.0)
    if k == "ring":
        return math.pi * (float(p.outer_radius) ** 2 - float(p.inner_radius) ** 2), (0.0, 0.0)
    if k == "slot":
        L, W = float(p.length), float(p.width)
        return (L - W) * W + math.pi * W * W / 4.0, (0.0, 0.0)
    outer, holes = profile_loops(p)
    return region_area_centroid(outer, holes)


# --------------------------------------------------------------------------- #
# Bent plate outline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BentPlateLayout:
    """Outline of a bent plate in its plane (u, v): the reference face starts at (0, 0) running along +u, the
    thickness lies to the left of travel (+v at the start). ``flats`` are the straight leg lengths, ``bend_centers``
    the arc centres, ``legs`` the start / end of each leg on the reference face with its heading (radians)."""

    start: Vec2
    segments: list[dict[str, Any]]
    flats: list[float]
    bend_centers: list[Vec2]
    legs: list[tuple[Vec2, Vec2, float]]


def _left(phi: float) -> Vec2:
    return (-math.sin(phi), math.cos(phi))


def bend_setback(thickness: float, inner_radius: float, angle_deg: float) -> float:
    """Outside setback of one bend: from the virtual sharp of the outer faces to the start of the bend."""
    return (inner_radius + thickness) * math.tan(math.radians(abs(angle_deg)) / 2.0)


def bent_plate_layout(thickness: float, legs: Sequence[float], bends: Sequence[float], inner_radius: float,
                      leg_length: Literal["outside", "flat"] = "outside") -> BentPlateLayout:
    """Closed outline of a statically bent plate (no unfolding / bend allowance: the shape only).

    ``legs`` are measured to the virtual sharps of the outer faces (``outside``, drawing convention) or as the
    straight (flat) part of each leg (``flat``); ``bends[i]`` is the angle between leg i and leg i + 1, positive
    toward the thickness side (+v), negative away from it. Raises ``ValueError`` when a leg is too short for its
    bend radii."""
    t, r = float(thickness), float(inner_radius)
    n = len(legs)
    if n < 2 or len(bends) != n - 1:
        raise ValueError("a bent plate needs at least two legs and one bend angle between each pair of legs")
    flats: list[float] = []
    for i, L in enumerate(legs):
        f = float(L)
        if leg_length == "outside":
            if i > 0:
                f -= bend_setback(t, r, bends[i - 1])
            if i < n - 1:
                f -= bend_setback(t, r, bends[i])
        if f < 1e-6:
            raise ValueError(f"leg {i + 1} ({float(L):g} mm, {leg_length}) is too short for its bend radius and thickness")
        flats.append(f)
    a_side: list[Seg] = []
    b_side: list[Seg] = []
    centers: list[Vec2] = []
    legs_out: list[tuple[Vec2, Vec2, float]] = []
    p: Vec2 = (0.0, 0.0)
    phi = 0.0
    for i in range(n):
        q = (p[0] + flats[i] * math.cos(phi), p[1] + flats[i] * math.sin(phi))
        lf = _left(phi)
        a_side.append(Seg("line", p, q))
        b_side.append(Seg("line", (p[0] + t * lf[0], p[1] + t * lf[1]), (q[0] + t * lf[0], q[1] + t * lf[1])))
        legs_out.append((p, q, phi))
        p = q
        if i == n - 1:
            break
        th = math.radians(float(bends[i]))
        left_turn = th > 0
        ra = r + t if left_turn else r
        rb = r if left_turn else r + t
        sgn = 1.0 if left_turn else -1.0
        c = (p[0] + sgn * ra * lf[0], p[1] + sgn * ra * lf[1])
        centers.append(c)
        a0 = math.atan2(p[1] - c[1], p[0] - c[0])
        q = (c[0] + ra * math.cos(a0 + th), c[1] + ra * math.sin(a0 + th)) if ra > 1e-12 else p
        if ra > 1e-12:
            a_side.append(Seg("arc", p, q, c, ra, a0, th))
        if rb > 1e-12:
            b0 = (c[0] + rb * math.cos(a0), c[1] + rb * math.sin(a0))
            b1 = (c[0] + rb * math.cos(a0 + th), c[1] + rb * math.sin(a0 + th))
            b_side.append(Seg("arc", b0, b1, c, rb, a0, th))
        p = q
        phi += th
    loop: list[Seg] = list(a_side)
    end_b = b_side[-1].p1
    loop.append(Seg("line", a_side[-1].p1, end_b))
    loop += [s.reversed() for s in reversed(b_side)]
    segments = [s.as_dict() for s in loop]  # closing line from B start back to (0, 0) is implicit
    return BentPlateLayout(start=(0.0, 0.0), segments=segments, flats=flats, bend_centers=centers, legs=legs_out)


# --------------------------------------------------------------------------- #
# Sweep sections
# --------------------------------------------------------------------------- #


def profile_extent(p: Any, d: Sequence[float]) -> float:
    """max over a profile of (q · d) for a unit 2D direction (exact for every kind)."""
    outer, _ = profile_loops(p)
    return loop_support(outer, d)


def sweep_centroid_length(path: Sequence[Seg], e_u: float) -> float:
    """Length of the curve the section centroid travels along a planar path when the centroid sits ``e_u`` to the
    left of the path (Pappus' second theorem: swept volume = section area × this length)."""
    total = 0.0
    for s in path:
        if s.kind == "line":
            total += s.length
        else:
            radius = s.radius - e_u if s.sweep > 0 else s.radius + e_u
            total += abs(s.sweep) * radius
    return total
