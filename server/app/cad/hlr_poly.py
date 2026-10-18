"""Polygonal hidden-line removal for coil geometry (compression springs, split lock washers, other helical sweeps).

The exact HLR (``HLRBRep_Algo``) of a wire swept along a helix costs about a minute per view: the swept face is one
B-spline surface of high degree with a hundred poles per side, and every edge of the view is intersected with it. The
drawings therefore project shapes that contain coil geometry with ``HLRBRep_PolyAlgo`` on a triangulation of an
untouched copy (:func:`mesh`; the original shape and its STEP are never meshed), which takes well under a second:

- detection (:func:`is_coil`): an ``ok`` ``coil`` feature in the part's build record when the drawing has it, else a
  free-form face with a helical iso-line (:func:`has_helical_faces`: the line winds at least :data:`MIN_TURNS` turns
  about one axis while advancing along it). Shapes without such a face keep the exact HLR byte for byte.
- the mesh: linear deflection :data:`LIN_REL` × the size of what is drawn (at least :data:`LIN_MIN`), angular
  deflection :data:`ANG_DEFL`; ``BRepMesh`` runs single-threaded, so the result is deterministic.
- read-back (:func:`hide`): visible = sharp + smooth (G1) + outline edges, hidden = the same three sets, per loaded
  shape — the categories of the exact HLR. The segments are chained into polylines and thinned (Douglas-Peucker,
  tolerance :data:`SIMPLIFY` × the linear deflection).
- extents (:func:`snap`): mesh outlines lie up to the deflection inside the true surface, so in the axis-aligned views
  the points within :data:`SNAP` × the deflection of a shape's exact (tight) bounding box are put on it; envelope
  dimensions measured on the view then state the exact size.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
from OCP.gp import gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS

Pt = tuple[float, float]
Poly = list[Pt]

MIN_TURNS = 0.5  # a helical iso-line winds at least this many turns about its axis
MIN_ADVANCE = 0.05  # … while advancing along the axis by at least this fraction of its radius
ISO_SAMPLES = 97
MAX_FACES = 64  # free-form faces examined per shape (an imported free-form part is not searched exhaustively)
LIN_REL = 1e-4  # linear deflection relative to the size of what is drawn (100 mm → 0.01 mm)
LIN_MIN = 1e-3  # mm
ANG_DEFL = 0.1  # rad
SIMPLIFY = 0.25  # Douglas-Peucker tolerance, × the linear deflection
SNAP = 4.0  # snap distance to a shape's exact extent, × the linear deflection
_ANALYTIC = (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def _w(shape: Any) -> Any:
    return getattr(shape, "wrapped", shape)


def _helical(pts: np.ndarray) -> bool:
    """A sampled curve that winds at least :data:`MIN_TURNS` about one axis while advancing along it. The axis is the
    normal of the plane through the tips of the unit chord directions (a helix keeps a constant angle to its axis)."""
    d = np.diff(pts, axis=0)
    ln = np.linalg.norm(d, axis=1)
    d = d[ln > 1e-12]
    if len(d) < 8:
        return False
    t = d / np.linalg.norm(d, axis=1)[:, None]
    _, s, vt = np.linalg.svd(t - t.mean(axis=0), full_matrices=False)
    if s[0] < 0.2 * math.sqrt(len(t)):  # the direction hardly turns: not a coil
        return False
    a = vt[2]
    e1 = vt[0]
    e2 = np.cross(a, e1)
    h = pts @ a
    x, y = pts @ e1, pts @ e2
    m = np.column_stack([x, y, np.ones_like(x)])
    sol, *_ = np.linalg.lstsq(m, -(x * x + y * y), rcond=None)
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    r2 = cx * cx + cy * cy - sol[2]
    if not np.isfinite(r2) or r2 <= 1e-12:
        return False
    radius = math.sqrt(r2)
    ang = np.unwrap(np.arctan2(y - cy, x - cx))
    step = np.diff(ang)
    turns = abs(float(ang[-1] - ang[0])) / (2.0 * math.pi)
    one_way = max(int((step > 0).sum()), int((step < 0).sum())) >= 0.9 * len(step)
    return turns >= MIN_TURNS and one_way and float(h.max() - h.min()) >= MIN_ADVANCE * radius


def has_helical_faces(shape: Any) -> bool:
    """True when a free-form face of the shape has a helical iso-line (u or v)."""
    exp = TopExp_Explorer(_w(shape), TopAbs_FACE)
    seen = 0
    while exp.More() and seen < MAX_FACES:
        face = TopoDS.Face(exp.Current())
        exp.Next()
        surf = BRepAdaptor_Surface(face, False)
        if surf.GetType() in _ANALYTIC:
            continue
        seen += 1
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        if not all(math.isfinite(x) for x in (u0, u1, v0, v1)):
            continue
        k = np.linspace(0.0, 1.0, ISO_SAMPLES)
        for line in ([(u0 + f * (u1 - u0), (v0 + v1) / 2.0) for f in k], [((u0 + u1) / 2.0, v0 + f * (v1 - v0)) for f in k]):
            pts = np.array([(p.X(), p.Y(), p.Z()) for p in (surf.Value(u, v) for u, v in line)])
            if _helical(pts):
                return True
    return False


def coil_feature(meta: dict[str, Any] | None) -> bool:
    """The part's build record (``meta["features"]``, :class:`~app.cad.results.FeatureRecord` dicts) has an ``ok``
    ``coil`` op."""
    for f in (meta or {}).get("features") or []:
        get = f.get if isinstance(f, dict) else (lambda k, f=f: getattr(f, k, None))
        if get("type") == "coil" and get("status") == "ok":
            return True
    return False


def is_coil(shape: Any, meta: dict[str, Any] | None = None) -> bool:
    return coil_feature(meta) or has_helical_faces(shape)


# --------------------------------------------------------------------------- #
# Mesh and polygonal HLR
# --------------------------------------------------------------------------- #


def deflection(size: float) -> tuple[float, float]:
    """(linear mm, angular rad) deflection for drawing something ``size`` mm across."""
    return max(LIN_MIN, LIN_REL * float(size)), ANG_DEFL


def mesh(shape: Any, lin: float, ang: float = ANG_DEFL) -> Any:
    """A triangulated copy of the shape (the original keeps no triangulation)."""
    copy = BRepBuilderAPI_Copy(_w(shape), True, False).Shape()
    BRepMesh_IncrementalMesh(copy, lin, False, ang, False)
    return copy


def _segments(compound: Any, t: Pt) -> list[tuple[Pt, Pt]]:
    out: list[tuple[Pt, Pt]] = []
    if compound is None or compound.IsNull():
        return out
    exp = TopExp_Explorer(compound, TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge(exp.Current())
        a, b = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(e)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(e))
        if abs(a.X() - b.X()) + abs(a.Y() - b.Y()) > 1e-12:
            out.append(((a.X() + t[0], a.Y() + t[1]), (b.X() + t[0], b.Y() + t[1])))
        exp.Next()
    return out


def chain(segs: Sequence[tuple[Pt, Pt]], q: float = 1e-6) -> list[Poly]:
    """Segments joined into polylines at shared end points (ends of degree two); deterministic order."""
    def key(p: Pt) -> tuple[int, int]:
        return (round(p[0] / q), round(p[1] / q))

    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    uniq = []
    for a, b in segs:  # two edges drawn over each other (the front and back half of a circle seen edge-on): once
        k = tuple(sorted((key(a), key(b))))
        if k[0] != k[1] and k not in seen:
            seen.add(k)  # type: ignore[arg-type]
            uniq.append((a, b))
    segs = uniq
    at: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in enumerate(segs):
        at.setdefault(key(a), []).append(i)
        at.setdefault(key(b), []).append(i)
    used = [False] * len(segs)

    def walk(start: tuple[int, int], i: int) -> Poly:
        a, b = segs[i]
        pts = [a, b] if key(a) == start else [b, a]
        used[i] = True
        cur = key(pts[-1])
        while len(at[cur]) == 2:
            nxt = next((j for j in at[cur] if not used[j]), None)
            if nxt is None:
                break
            a, b = segs[nxt]
            used[nxt] = True
            pts.append(b if key(a) == cur else a)
            cur = key(pts[-1])
        return pts

    out: list[Poly] = []
    for k in sorted(at):
        if len(at[k]) != 2:
            for i in at[k]:
                if not used[i]:
                    out.append(walk(k, i))
    for i in range(len(segs)):  # closed loops
        if not used[i]:
            out.append(walk(key(segs[i][0]), i))
    return out


def simplify(pts: Poly, tol: float) -> Poly:
    """Douglas-Peucker (iterative)."""
    if len(pts) < 3:
        return list(pts)
    arr = np.asarray(pts)
    keep = np.zeros(len(arr), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(arr) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = arr[i], arr[j]
        ab = b - a
        n = math.hypot(ab[0], ab[1])
        seg = arr[i + 1:j]
        if n < 1e-12:
            dist = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            dist = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / n
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return [pts[i] for i in np.flatnonzero(keep)]


def snap(polys: list[Poly], box: tuple[float, float, float, float], tol: float) -> list[Poly]:
    """Points within ``tol`` of (or beyond) the exact extent ``box`` (x0, y0, x1, y1) put on it."""
    x0, y0, x1, y1 = box

    def s(v: float, lo: float, hi: float) -> float:
        if v <= lo + tol:
            return lo
        if v >= hi - tol:
            return hi
        return v

    return [[(s(x, x0, x1), s(y, y0, y1)) for x, y in p] for p in polys]


def hide(shapes: Sequence[Any], read: Sequence[int], ax: Any, lin: float) -> list[tuple[list[Poly], list[Poly]]]:
    """Polygonal HLR of the meshed ``shapes`` together (occlusion between all of them) for the camera ``ax``
    (a ``gp_Ax2``: location, direction towards the viewer, screen-up); (visible, hidden) polylines of each shape in
    ``read``, in the projection plane (the frame of :class:`OCP.HLRBRep.HLRBRep_HLRToShape`)."""
    projector = HLRAlgo_Projector(ax)
    algo = HLRBRep_PolyAlgo()
    for s in shapes:
        algo.Load(s)
    algo.Projector(projector)
    algo.Update()
    h = HLRBRep_PolyHLRToShape()
    h.Update(algo)
    # the polygonal result is only rotated into the view; the exact one is also centred on the camera location: shift
    # it by that, so both share one frame
    loc = ax.Location()
    tx, ty, _ = projector.Project(gp_Pnt(loc.X(), loc.Y(), loc.Z()))
    t = (-tx, -ty)
    out = []
    for i in read:
        s = shapes[i]
        vis = _segments(h.VCompound(s), t) + _segments(h.Rg1LineVCompound(s), t) + _segments(h.OutLineVCompound(s), t)
        hid = _segments(h.HCompound(s), t) + _segments(h.OutLineHCompound(s), t) + _segments(h.Rg1LineHCompound(s), t)
        out.append(tuple([simplify(p, SIMPLIFY * lin) for p in chain(g)] for g in (vis, hid)))
    return out  # type: ignore[return-value]
