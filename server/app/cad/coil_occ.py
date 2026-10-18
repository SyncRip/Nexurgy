"""OCCT construction of a coil (:mod:`app.cad.coil`) with OCP only — the kernel's ``coil`` op and the requirement
checker's reference solid for the volume accounting (no build123d, so :mod:`app.cad.conformance_w3` can use it).

The centre line is exact: a degree-2 B-spline in the turn parameter t, drawn in the (angle, height) parameters of the
cylinder of the mean radius (angle linear in t, height piecewise quadratic and C1, one quadratic Bézier per span of
the pitch law), turned into a 3D edge. The section is placed normal to it at the start with its first axis radially
outward, and swept in the Frenet frame (``BRepOffsetAPI_MakePipeShell``); ground ends are the intersection with the
slab 0 ≤ z ≤ length of the coil frame.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepLib import BRepLib
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
from OCP.collections import Array1_double, Array1_gp_Pnt2d, Array1_int
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_BSplineCurve
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.TopAbs import TopAbs_IN, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from .coil import CoilLaw
from .sketch import Frame


class CoilBuildError(Exception):
    pass


def centre_curve(law: CoilLaw) -> Any:
    """The centre line in the (angle, height) parameters of the coil's cylinder (exact degree-2 B-spline in t)."""
    ts = law.breaks()
    w = law.hand * 2.0 * math.pi
    pts = [(law.theta(ts[0]), law.z(ts[0]))]
    for a, b in zip(ts, ts[1:]):
        h = (b - a) / 2.0
        pts.append((law.theta(a) + h * w, law.z(a) + h * law.pitch_at(a)))
    pts.append((law.theta(ts[-1]), law.z(ts[-1])))
    poles = Array1_gp_Pnt2d(1, len(pts))
    for i, (u, v) in enumerate(pts):
        poles.SetValue(i + 1, gp_Pnt2d(u, v))
    knots = Array1_double(1, len(ts))
    mults = Array1_int(1, len(ts))
    for i, t in enumerate(ts):
        knots.SetValue(i + 1, t)
        mults.SetValue(i + 1, 3 if i in (0, len(ts) - 1) else 1)
    return Geom2d_BSplineCurve(poles, knots, mults, 2)


def _section_wire(law: CoilLaw, f: Frame, kind: str) -> Any:
    T, U, V = law.frame(0.0)
    o, t, u, v = f.point(*law.point(0.0)), f.vector(*T), f.vector(*U), f.vector(*V)
    if kind == "circle":
        circ = gp_Circ(gp_Ax2(gp_Pnt(*o), gp_Dir(*t), gp_Dir(*u)), law.s_rad / 2.0)
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire()
    a, b = law.s_rad / 2.0, law.s_ax / 2.0
    poly = BRepBuilderAPI_MakePolygon()
    for su, sv in ((-a, -b), (a, -b), (a, b), (-a, b)):
        poly.Add(gp_Pnt(*(o[k] + su * u[k] + sv * v[k] for k in range(3))))
    poly.Close()
    return poly.Wire()


def _solids(shape: Any) -> list[Any]:
    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


class RobustInside:
    """Point-in-solid by majority vote of the solid classifier and ray parity along four skew directions. The
    classifier alone misjudges some points inside long swept B-spline faces (its ray grazes a seam); a coil check must
    not fail on that."""

    DIRS = ((0.5773502692, 0.5773502692, 0.5773502692), (-0.2672612419, 0.5345224838, 0.8017837257),
            (0.8164965809, -0.4082482905, 0.4082482905), (-0.4472135955, -0.8944271910, 0.0))

    def __init__(self, shape: Any):
        self.shape = getattr(shape, "wrapped", shape)
        self._cls = BRepClass3d_SolidClassifier(self.shape)
        self._int = IntCurvesFace_ShapeIntersector()
        self._int.Load(self.shape, 1e-7)

    def __call__(self, p: Sequence[float]) -> bool:
        pt = gp_Pnt(float(p[0]), float(p[1]), float(p[2]))
        self._cls.Perform(pt, 1e-7)
        yes = int(self._cls.State() == TopAbs_IN)
        no = 1 - yes
        for d in self.DIRS:  # stop as soon as three of the five votes agree
            self._int.Perform(gp_Lin(pt, gp_Dir(*d)), 0.0, 1e9)
            hits = sum(1 for k in range(self._int.NbPnt()) if self._int.WParameter(k + 1) > 1e-9)
            yes, no = yes + hits % 2, no + 1 - hits % 2
            if yes >= 3 or no >= 3:
                break
        return yes >= 3


def coil_solid(law: CoilLaw, f: Frame, kind: str) -> Any:
    """The coil as one OCCT solid in the frame ``f`` (origin on the axis in the seating plane, z = axis, x = start
    reference); ``kind`` is the section kind (circle / rect). Raises :class:`CoilBuildError`."""
    surf = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(*f.origin), gp_Dir(*f.z), gp_Dir(*f.x)), law.radius)
    edge = BRepBuilderAPI_MakeEdge(centre_curve(law), surf).Edge()
    if not BRepLib.BuildCurves3d_s(edge, 1e-7, MaxSegment=2000):
        raise CoilBuildError("the coil centre line could not be built")
    pipe = BRepOffsetAPI_MakePipeShell(BRepBuilderAPI_MakeWire(edge).Wire())
    pipe.SetMode(True)  # Frenet: the section keeps its place relative to the axis along the helix
    pipe.Add(_section_wire(law, f, kind), False, False)
    pipe.Build()
    if not pipe.IsDone() or not pipe.MakeSolid():
        raise CoilBuildError("the coil sweep did not close into a solid")
    shape = pipe.Shape()
    if law.ground:
        slab = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*f.origin), gp_Dir(*f.z), gp_Dir(*f.x)), law.radius + law.s_rad + 1.0,
                                        law.length).Shape()
        common = BRepAlgoAPI_Common(shape, slab)
        common.Build()
        if not common.IsDone():
            raise CoilBuildError("grinding the coil ends failed")
        shape = common.Shape()
    solids = _solids(shape)
    if len(solids) != 1:
        raise CoilBuildError(f"the coil gives {len(solids)} solids instead of one")
    return solids[0]
