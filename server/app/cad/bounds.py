"""Tight bounding boxes and volumes of B-rep shapes (OCP only; used by the kernel metrics, part drawings and the
requirement checker).

``BRepBndLib.AddOptimal`` bounds every face by its surface over the face's whole UV box. That is exact for analytic
surfaces and for untrimmed faces, but a trimmed free-form face over-reaches: its UV box contains surface that the trim
removed (a coil's swept B-spline surface cut flat by ground ends reaches about one wire radius past the ground planes).
Such faces are bounded here by their edges (exact) and by the interior extremes of a sample grid of points inside the
face, refined by Newton steps — never beyond the optimal box. Shapes without such faces get exactly the optimal box.

Cost is paid only where it changes the answer: a side of a trimmed free-form face is refined only when its optimal box
reaches past everything the shape is known to reach (the exact boxes of its other faces and the edges of the trimmed
ones) by more than :data:`TOL`; the grid is classified with the fast polygonal ``IntTools_FClass2d`` and only the
chosen samples and the Newton steps with the exact face classifier. When more than :data:`MAX_REFINED_FACES` faces
would need refining (an imported free-form part), the shape gets OCCT's optimal box instead of seconds of sampling.

Volumes: the default Gauss integration of ``BRepGProp`` is off by up to about 0.5 % on long swept B-spline faces (a
coil); :func:`volume` integrates adaptively when a shape has a few free-form faces (at most
:data:`MAX_REFINED_FACES`) and keeps the default rule otherwise.
"""

from __future__ import annotations

import math
from typing import Any

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IntTools import IntTools_FClass2d
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

V3 = tuple[float, float, float]
Side = tuple[int, float]  # (axis 0..2, +1.0 max / -1.0 min)
_ANALYTIC = (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus)
GRID = 64  # samples per UV direction on a trimmed free-form face
MAX_REFINED_FACES = 8  # above this many faces to refine (or free-form faces for the volume) keep OCCT's default
TOL = 1e-4  # mm: a face side reaching no further than this past what the shape surely reaches is not refined
_SEEDS = 16  # grid samples per side tried with the exact classifier before giving the side up to the edges


def _w(shape: Any) -> Any:
    return getattr(shape, "wrapped", shape)


def _corners(box: Bnd_Box) -> tuple[list[float], list[float]]:
    a, b = box.CornerMin(), box.CornerMax()
    return [a.X(), a.Y(), a.Z()], [b.X(), b.Y(), b.Z()]


def _faces(shape: Any) -> list[Any]:
    out = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        out.append(TopoDS.Face(exp.Current()))
        exp.Next()
    return out


def _free_form(face: Any) -> bool:
    return BRepAdaptor_Surface(face, False).GetType() not in _ANALYTIC


def _over_reaching(face: Any) -> bool:
    return _free_form(face) and not BRep_Tool.NaturalRestriction_s(face)


def _edges_box(face: Any) -> tuple[list[float], list[float]]:
    ebox = Bnd_Box()
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while exp.More():
        BRepBndLib.AddOptimal_s(exp.Current(), ebox, False, False)
        exp.Next()
    return _corners(ebox) if not ebox.IsVoid() else ([math.inf] * 3, [-math.inf] * 3)


def _side(lo: list[float], hi: list[float], k: int, s: float) -> float:
    """s · (the box's coordinate on that side): larger = further out."""
    return hi[k] if s > 0 else -lo[k]


def _face_bounds(face: Any, blo: list[float], bhi: list[float], elo: list[float], ehi: list[float],
                 sides: set[Side]) -> tuple[list[float], list[float]]:
    """The face's optimal box (``blo``/``bhi``) with the given sides tightened to the furthest of its edges
    (``elo``/``ehi``) and its interior extremes."""
    surf = BRepAdaptor_Surface(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    cls = BRepClass_FaceClassifier()

    def inside(u: float, v: float) -> bool:
        if not (u0 <= u <= u1 and v0 <= v <= v1):
            return False
        cls.Perform(face, gp_Pnt2d(u, v), 1e-9)
        return cls.State() == TopAbs_IN

    fast = IntTools_FClass2d(face, 1e-7)
    samples: dict[Side, list[tuple[float, float, float]]] = {side: [] for side in sides}  # side -> (s·coord, u, v)
    for i in range(GRID):
        u = u0 + (i + 0.5) * (u1 - u0) / GRID
        for j in range(GRID):
            v = v0 + (j + 0.5) * (v1 - v0) / GRID
            if fast.Perform(gp_Pnt2d(u, v), False) != TopAbs_IN:
                continue
            p = surf.Value(u, v)
            c = (p.X(), p.Y(), p.Z())
            for (k, s), out in samples.items():
                out.append((s * c[k], u, v))
    lo, hi = list(blo), list(bhi)
    for (k, s), cand in samples.items():
        best = _side(elo, ehi, k, s)
        cand.sort(reverse=True)
        for val, u, v in cand[:_SEEDS]:  # the furthest sample the exact classifier confirms, refined
            if inside(u, v):
                best = max(best, _refine(surf, inside, k, s, u, v, val))
                break
        if math.isfinite(best):
            if s > 0:
                hi[k] = min(bhi[k], best)
            else:
                lo[k] = max(blo[k], -best)
    return lo, hi


def _refine(surf: Any, inside: Any, k: int, s: float, u: float, v: float, val: float) -> float:
    """Newton ascent of s·P_k(u, v) from a sample, staying inside the face; the best value reached."""
    P, Du, Dv, Duu, Dvv, Duv = gp_Pnt(), gp_Vec(), gp_Vec(), gp_Vec(), gp_Vec(), gp_Vec()
    for _ in range(40):
        surf.D2(u, v, P, Du, Dv, Duu, Dvv, Duv)
        g = (s * Du.Coord(k + 1), s * Dv.Coord(k + 1))
        a, b, c = s * Duu.Coord(k + 1), s * Duv.Coord(k + 1), s * Dvv.Coord(k + 1)
        det = a * c - b * b
        if a < 0 and det > 0:  # concave: Newton step to the stationary point
            du, dv = -(c * g[0] - b * g[1]) / det, -(a * g[1] - b * g[0]) / det
        else:  # gradient step of a modest size
            n = math.hypot(*g)
            if n < 1e-15:
                break
            scale = 1e-3 / n
            du, dv = g[0] * scale, g[1] * scale
        moved = False
        for _ in range(12):
            if inside(u + du, v + dv):
                pt = surf.Value(u + du, v + dv)
                nv = s * (pt.X(), pt.Y(), pt.Z())[k]
                if nv >= val - 1e-12:
                    u, v, val, moved = u + du, v + dv, max(val, nv), True
                    break
            du, dv = du / 2.0, dv / 2.0
        if not moved or math.hypot(du, dv) < 1e-12:
            break
    return val


def volume(shape: Any) -> float:
    """Volume of a shape: adaptive integration (relative 1e-7) when it has a few free-form faces (at most
    :data:`MAX_REFINED_FACES`), else the default rule."""
    s = _w(shape)
    props = GProp_GProps()
    free = sum(_free_form(f) for f in _faces(s))
    if 0 < free <= MAX_REFINED_FACES:
        BRepGProp.VolumeProperties_s(s, props, 1e-7)
    else:
        BRepGProp.VolumeProperties_s(s, props)
    return float(props.Mass())


def _optimal(shape: Any) -> tuple[V3, V3]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    lo, hi = _corners(box) if not box.IsVoid() else ([0.0] * 3, [0.0] * 3)
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def tight_bounds(shape: Any) -> tuple[V3, V3]:
    """(min, max) corners: the optimal box, tightened on trimmed free-form faces (see the module docstring)."""
    s = _w(shape)
    faces = _faces(s)
    if not any(_over_reaching(f) for f in faces):
        return _optimal(s)
    boxes = []  # (face, optimal lo, hi, edges lo, hi or None)
    known_lo, known_hi = [math.inf] * 3, [-math.inf] * 3  # what the shape surely reaches
    for f in faces:
        fb = Bnd_Box()
        BRepBndLib.AddOptimal_s(f, fb, False, False)
        if fb.IsVoid():
            continue
        flo, fhi = _corners(fb)
        edges = _edges_box(f) if _over_reaching(f) else None
        slo, shi = edges if edges is not None else (flo, fhi)
        known_lo = [min(known_lo[k], slo[k]) for k in range(3)]
        known_hi = [max(known_hi[k], shi[k]) for k in range(3)]
        boxes.append((f, flo, fhi, edges))
    if not boxes:
        return _optimal(s)
    todo = {}
    for i, (f, flo, fhi, edges) in enumerate(boxes):
        if edges is not None:
            sides = {(k, sg) for k in range(3) for sg in (1.0, -1.0)
                     if _side(flo, fhi, k, sg) > _side(known_lo, known_hi, k, sg) + TOL}
            if sides:
                todo[i] = sides
    if len(todo) > MAX_REFINED_FACES:  # many faces to refine: the faces' optimal boxes (= OCCT's optimal box)
        todo = {}
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for i, (f, flo, fhi, edges) in enumerate(boxes):
        if i in todo:
            flo, fhi = _face_bounds(f, flo, fhi, edges[0], edges[1], todo[i])
        lo = [min(lo[k], flo[k]) for k in range(3)]
        hi = [max(hi[k], fhi[k]) for k in range(3)]
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])
