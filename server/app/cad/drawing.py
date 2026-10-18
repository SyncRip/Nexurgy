"""2D engineering drawing packages (SVG + DXF per page) from the exact solid.

Source geometry: a successful :class:`BuildResult` that still holds its
in-memory solid (``result.shape``; build in the same process) or a
:class:`ModelSpec` (+ base STEP bytes), rebuilt here with ``export=False``.
Every page comes from the same exact B-rep that produced the revision's STEP,
never from a mesh — except the views of coil geometry (a part with helical
faces: spring, split lock washer), whose exact hidden-line removal takes about
a minute per view: they are projected from a triangulated copy of that B-rep
(:mod:`app.cad.hlr_poly`, chord ≤ 1e-4 × the part size, outline put on the
exact bounding box), which the sheet notes state.

Package (``make_drawing_package``), A3 landscape sheets:

- Page 1 ``overview``: front / top / right / iso (``config.views``), exact
  hidden-line removal (visible solid, hidden dashed; iso visible only),
  overall dimensions, hole position dimensions measured from a stated datum
  (the part's bounding-box min corner, baseline dimensions in the view that
  looks down the hole axis), a native Ø DIMENSION across every hole group
  (each bore diameter, in the view looking down the axis; a group of N
  identical bores is one pattern dimension ``N× Ø…`` whose record lists every
  member's axis, see ``annotation_check.pattern_prefix``) and every round
  boss / outline (unless a section page dimensions it), size (width × length,
  ``size`` records) and datum positions of every inner outline of a
  horizontal face (pocket, through opening, boss footprint), hole callouts
  (``4× Ø8 THRU``, ``Ø6 ↧10``, ``⌴Ø11 ↧4``, ``⌵Ø12×90°``, on layer CALLOUT)
  and a notes block with the manufacturing requirement coverage (covered /
  not shown / unknown).
- Page 2+ ``section`` (``config.section``: ``"auto"`` | ``"none"`` |
  ``{"plane": "XZ"|"YZ", "offset": mm}``), only when the part has depth
  information a projection cannot carry: blind holes, counterbores / stepped
  bores, pockets or steps along Z. The exact solid is split by the cutting
  plane, the kept half is projected with HLR (hidden edges are not drawn in a
  section), faces lying on the plane are hatched (DXF HATCH ANSI31 on layer
  HATCH, SVG pattern), and the cutting line with arrows (direction of sight,
  towards the kept material) and letters is drawn on a top view on the same
  page. Section dimensions: overall height, Z levels of steps from the part
  bottom, pocket / blind / counterbore depths from their local entry, bore
  and boss diameters. ``auto`` puts the plane through as many section-worthy
  features as possible (hole axes first, else the bbox centre) and adds a
  further section (B-B, C-C) only for features the first one misses.

DXF (R2010, paper mm, y up, one file per page) mirrors the SVG on layers
VISIBLE / HIDDEN / CENTER / HATCH / CUTLINE / DIM / LEADER / TEXT / BORDER.
Dimensions are DIMENSION entities only (no loose text or lines on layer DIM);
their native value (stored measurement × DIMLFAC of style ``PARTROUTE``, with
DIMLFAC = 1 / sheet scale) equals the displayed model length, and the text is
never a literal number (``<>``, or ``%%c<>`` for diameters). Linetypes HIDDEN
and CENTER are defined; text uses the TTF style ``PR_TEXT`` (see
``TEXT_STYLE``). Bytes are deterministic (fixed ezdxf metadata). Entity types
stay within LINE / LWPOLYLINE / CIRCLE / ARC / TEXT / MTEXT / DIMENSION /
HATCH / SOLID / INSERT / POINT for the DXF→DWG R2000 recipe.

``make_drawing`` keeps its signature and returns page 1 of the package.
"""

from __future__ import annotations

import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal
from xml.sax.saxutils import escape, quoteattr

import build123d as bd
import ezdxf
from ezdxf import const as dxfconst
from ezdxf.enums import TextEntityAlignment
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.gp import gp_Ax2, gp_Dir, gp_Pln, gp_Pnt
from OCP.TopAbs import TopAbs_Orientation, TopAbs_State
from pydantic import BaseModel, ConfigDict, Field

from . import hlr_poly
from .annotation_check import POSITION_DIM_CAP, pattern_prefix
from .dxf_write import fixed_metadata
from .kernel import build, hole_callout_text
from .ops import ModelSpec
from .results import BuildResult, DrawingConfig, DrawingOutput, HoleRecord, Issue, ViewInfo
from .text_geometry import text_regions

DRAWING_VERSION = "partroute-drawing/3"

SHEET_W, SHEET_H = 420.0, 297.0
MARGIN = 10.0
TITLE_W, TITLE_H = 180.0, 40.0
STANDARD_SCALES = [50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01]
TEXT_H = 3.5
SMALL_H = 1.8
NOTE_H = 2.8
NOTE_STEP = 4.6
# DXF text heights are cap heights (CAD convention for TrueType styles); SVG
# font-size is the em size: em = cap height / CAP_RATIO (Arial: 1467 / 2048).
CAP_RATIO = 0.716

# dimension style (paper mm)
DIM_TXT = 3.0
ARROW = 2.5
DIM_EXO = 1.0
DIM_EXE = 1.5
DIM_GAP = 0.8
DIM_DEC = 2
DIM_FIRST = 8.0  # first dimension line, mm from the view outline
DIM_STEP = 7.0  # further stacked dimension lines
VIEW_PAD = 6.0  # minimum free margin around a view
LABEL_SPACE = TEXT_H + 5.0
CUT_EXT = 6.0  # cutting line overhang beyond the view outline
CUT_ARROW = 7.0
MAX_POSITION_DIMS = POSITION_DIM_CAP  # per view axis (the annotation check warns, not fails, past it)
MAX_SECTIONS = 3
HATCH_SCALE = 0.75  # ANSI31 pattern scale (line spacing 3.175 mm × scale on paper)
HATCH_SPACING = 3.175 * HATCH_SCALE

# Text style. One TTF face that covers Latin-1 (Ø × ° ±), ↧ (U+21A7),
# ⌴ ⌵ (U+2334/2335) and CJK, so every string can use the same style and the
# DXF→DWG R2000 recipe needs no big font. Checked with fontTools against the
# glyph tables of Arial Unicode MS; SimSun / Noto Sans CJK lack ↧ ⌴ ⌵ and
# DejaVu Sans lacks CJK. Viewers without the font substitute it.
TEXT_STYLE: dict[str, str] = {
    "name": "PR_TEXT",
    "font_file": "arialuni.ttf",
    "family": "Arial Unicode MS",
    "covers": "Latin-1 (Ø × ° ±), arrows (↧), misc technical (⌴ ⌵), CJK unified ideographs",
    "svg_font_family": "'Arial Unicode MS','DejaVu Sans','Noto Sans','Noto Sans CJK SC','Source Han Sans SC',"
                       "'SimSun',Arial,Helvetica,sans-serif",
}
DIMSTYLE = "PARTROUTE"

# view name -> (direction towards the viewer, screen-up vector)
VIEW_DIRS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
}
VIEW_LABELS = {"front": "FRONT", "top": "TOP", "right": "RIGHT", "iso": "ISOMETRIC"}
# screen axes of the orthographic views as model axes
VIEW_AXES = {"front": ("x", "z"), "top": ("x", "y"), "right": ("y", "z")}
# model axis index a hole must be drilled along to be seen end-on in the view
VIEW_LOOK_AXIS = {"top": 2, "right": 0, "front": 1}
# (row, col) cells, row 0 at the top of the sheet
LAYOUTS = {
    "third": {"top": (0, 0), "iso": (0, 1), "front": (1, 0), "right": (1, 1)},
    "first": {"right": (0, 0), "front": (0, 1), "iso": (1, 0), "top": (1, 1)},
}
SECTION_LETTERS = "ABC"


class DrawingError(ValueError):
    """The source cannot be drawn (failed build, no geometry)."""


# --------------------------------------------------------------------------- #
# Public models
# --------------------------------------------------------------------------- #


class SectionPlane(BaseModel):
    """An explicit cutting plane: ``XZ`` at ``y = offset`` (viewed towards
    +Y, front-like) or ``YZ`` at ``x = offset`` (viewed towards -X,
    right-like). Model mm."""

    model_config = ConfigDict(extra="forbid")

    plane: Literal["XZ", "YZ"]
    offset: float


class PackageConfig(DrawingConfig):
    """``DrawingConfig`` plus the section choice of the drawing package."""

    section: Literal["auto", "none"] | SectionPlane = "auto"


class PageView(BaseModel):
    """A placed view. ``bbox`` is in sheet mm, origin top-left, y down."""

    name: str
    kind: Literal["projection", "section"]
    label: str
    scale: float
    bbox: list[float]


class DimensionRecord(BaseModel):
    """What one DIMENSION entity states.

    ``value`` is the model length (= native DXF value = displayed number),
    ``text`` the displayed text, ``p1`` / ``p2`` the definition points in
    sheet mm (y down, like the SVG), ``handle`` the DXF entity handle.
    ``kind``: overall (envelope), position (hole centre / outline edge from
    the datum, ``ref.datum`` / ``ref.coord``), height (overall height in a
    section), level (Z of a step from the datum, ``ref.datum_z`` / ``ref.z``),
    depth (from a local entry along ``axis``, ``ref.entry`` / ``ref.floor``),
    diameter (bore or boss, ``Ø``; ``ref.diameter`` and a point ``ref.x/y/z``
    on the axis with its direction ``ref.dx/dy/dz``), size (width of an inner
    outline along ``axis``, ``ref.lo`` / ``ref.hi``). ``pattern``: a diameter
    stated once for N identical bores (text ``N× Ø…``): the axis point of each
    member, along ``ref.dx/dy/dz`` (empty for a single feature)."""

    id: str
    page: int
    view: str
    kind: Literal["overall", "position", "height", "level", "depth", "diameter", "size"]
    axis: Literal["x", "y", "z"] | None = None
    value: float
    text: str
    measured: str
    feature: str | None = None
    handle: str = ""
    p1: list[float]
    p2: list[float]
    ref: dict[str, float] = Field(default_factory=dict)
    pattern: list[list[float]] = Field(default_factory=list)


class SectionInfo(BaseModel):
    """Cutting plane of a section page (model mm)."""

    letter: str
    plane: Literal["XZ", "YZ"]
    offset: float
    view_direction: list[float]  # direction of sight (towards the kept material)
    kept: str  # e.g. "y >= 0"
    features: list[str]


class DrawingPage(BaseModel):
    index: int
    role: Literal["overview", "section"]
    title: str
    svg: bytes = Field(default=b"", exclude=True)
    dxf: bytes = Field(default=b"", exclude=True)
    scale: float
    views: list[PageView]
    dimensions: list[DimensionRecord]
    callouts: list[str] = Field(default_factory=list)
    hatches: int = 0
    section: SectionInfo | None = None


class DrawingPackage(BaseModel):
    """All pages of one drawing. ``coverage`` lists the manufacturing
    requirements (notes, threads) as covered / uncovered / unknown."""

    pages: list[DrawingPage]
    coverage: dict[str, list[dict[str, Any]]]
    warnings: list[Issue] = Field(default_factory=list)
    build_key: str
    generator: dict[str, Any]
    sheet: dict[str, Any]


# --------------------------------------------------------------------------- #
# Geometry → 2D primitives
# --------------------------------------------------------------------------- #


class _Prim:
    """2D primitive in view coordinates (model mm, y up, part centre at 0)."""

    __slots__ = ("kind", "pts", "center", "radius", "a0", "a1")

    def __init__(self, kind: str, pts: list[tuple[float, float]] | None = None, center: tuple[float, float] | None = None,
                 radius: float = 0.0, a0: float = 0.0, a1: float = 0.0):
        self.kind = kind  # "line" | "poly" | "circle" | "arc"
        self.pts = pts or []
        self.center = center
        self.radius = radius
        self.a0 = a0
        self.a1 = a1


def _edge_prims(edge: Any) -> _Prim | None:
    gt = edge.geom_type
    if gt == bd.GeomType.LINE:
        a, b = edge.start_point(), edge.end_point()
        if (a - b).length < 1e-9:
            return None
        return _Prim("line", [(a.X, a.Y), (b.X, b.Y)])
    curve = BRepAdaptor_Curve(edge.wrapped)
    disc = GCPnts_TangentialDeflection(curve, 0.05, 0.005, 3)
    pts = [(disc.Value(i).X(), disc.Value(i).Y()) for i in range(1, disc.NbPoints() + 1)]
    if gt == bd.GeomType.CIRCLE:
        c = curve.Circle().Location()
        r = curve.Circle().Radius()
        center = (c.X(), c.Y())
        if edge.is_closed:
            return _Prim("circle", pts, center, r)
        p0, pm, p1 = pts[0], pts[len(pts) // 2], pts[-1]
        ccw = (pm[0] - p0[0]) * (p1[1] - pm[1]) - (pm[1] - p0[1]) * (p1[0] - pm[0]) > 0
        ang0 = math.degrees(math.atan2(p0[1] - center[1], p0[0] - center[0]))
        ang1 = math.degrees(math.atan2(p1[1] - center[1], p1[0] - center[0]))
        if not ccw:
            ang0, ang1 = ang1, ang0
        return _Prim("arc", pts, center, r, ang0, ang1)
    return _Prim("poly", pts)


def _view_frame(name: str) -> tuple[bd.Vector, bd.Vector, bd.Vector]:
    d, up = VIEW_DIRS[name]
    n = bd.Vector(*d).normalized()
    y = bd.Vector(*up)
    y = (y - n * y.dot(n)).normalized()
    x = y.cross(n)
    return x, y, n


def _project(shape: Any, name: str, center: bd.Vector, diag: float, hidden: bool = True) -> tuple[list[_Prim], list[_Prim]]:
    _, up, n = _view_frame(name)
    origin = center + n * (diag * 2.0 + 100.0)
    visible, hid_edges = shape.project_to_viewport((origin.X, origin.Y, origin.Z), (up.X, up.Y, up.Z), (center.X, center.Y, center.Z))
    vis = [p for p in (_edge_prims(e) for e in visible) if p is not None]
    hid = [p for p in (_edge_prims(e) for e in hid_edges) if p is not None] if (hidden and name != "iso") else []
    return vis, hid


def _poly_prim(pts: list[tuple[float, float]]) -> _Prim:
    return _Prim("line" if len(pts) == 2 else "poly", list(pts))


def _chain(prims: list[_Prim], tol: float = 1e-6) -> list[_Prim]:
    """Line / polyline pieces that meet end to end joined into one polyline each (circles and arcs kept). Used for
    lettered parts only: a projected letter is hundreds of short edges, and the DWG converter's time grows steeply
    with the entity count (a nameplate's overview went from 24 000 entities and 105 s to a few thousand)."""
    out = [p for p in prims if p.kind not in ("line", "poly") or len(p.pts) < 2]
    open_ = [list(p.pts) for p in prims if p.kind in ("line", "poly") and len(p.pts) >= 2]

    def key(pt: tuple[float, float]) -> tuple[int, int]:
        return (round(pt[0] / tol), round(pt[1] / tol))

    ends: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, pts in enumerate(open_):
        ends[key(pts[0])].append(i)
        ends[key(pts[-1])].append(i)
    used = [False] * len(open_)

    def take(at: tuple[float, float]) -> list[tuple[float, float]] | None:
        for j in ends.get(key(at), []):
            if not used[j]:
                used[j] = True
                pts = open_[j]
                return pts if key(pts[0]) == key(at) else pts[::-1]
        return None

    for i, pts in enumerate(open_):
        if used[i]:
            continue
        used[i] = True
        line = list(pts)
        while (nxt := take(line[-1])) is not None:
            line += nxt[1:]
        while (prv := take(line[0])) is not None:
            line = prv[::-1][:-1] + line
        out.append(_poly_prim(line))
    return out


def _extent2(name: str, lo: Any, hi: Any, center: bd.Vector) -> tuple[float, float, float, float]:
    """2D extent of a 3D box in an axis-aligned view (exact there: the screen axes are model axes)."""
    x, y, _ = _view_frame(name)
    pts = [bd.Vector(a, b, c) - center for a in (lo[0], hi[0]) for b in (lo[1], hi[1]) for c in (lo[2], hi[2])]
    us, vs = [p.dot(x) for p in pts], [p.dot(y) for p in pts]
    return (min(us), min(vs), max(us), max(vs))


def _camera(name: str, center: bd.Vector, diag: float) -> gp_Ax2:
    """The camera of :func:`_project` (build123d ``project_to_viewport``) as a ``gp_Ax2``."""
    _, up, n = _view_frame(name)
    origin = center + n * (diag * 2.0 + 100.0)
    ax = gp_Ax2(gp_Pnt(origin.X, origin.Y, origin.Z), gp_Dir(n.X, n.Y, n.Z))
    ax.SetYDirection(gp_Dir(up.X, up.Y, up.Z))
    return ax


def _poly_project(meshed: Any, name: str, center: bd.Vector, diag: float, lin: float, hidden: bool = True,
                  box: tuple[Any, Any] | None = None) -> tuple[list[_Prim], list[_Prim]]:
    """:func:`_project` for coil geometry: polygonal HLR of a triangulated copy (:mod:`app.cad.hlr_poly`); in the
    axis-aligned views the outline is put on the exact bounding box ``box`` (lo, hi) where the mesh falls short of it."""
    ((vis, hid),) = hlr_poly.hide([meshed], [0], _camera(name, center, diag), lin)
    if box is not None and name in VIEW_AXES:
        ext = _extent2(name, box[0], box[1], center)
        vis, hid = hlr_poly.snap(vis, ext, hlr_poly.SNAP * lin), hlr_poly.snap(hid, ext, hlr_poly.SNAP * lin)
    return [_poly_prim(p) for p in vis], ([_poly_prim(p) for p in hid] if (hidden and name != "iso") else [])


def _prim_points(prims: list[_Prim]) -> list[tuple[float, float]]:
    pts = [p for pr in prims for p in pr.pts]
    for pr in prims:
        if pr.kind == "circle" and pr.center:
            cx, cy = pr.center
            pts += [(cx - pr.radius, cy), (cx + pr.radius, cy), (cx, cy - pr.radius), (cx, cy + pr.radius)]
    return pts


def _bbox2(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _extreme(pts: list[tuple[float, float]], which: str) -> tuple[float, float]:
    """Outermost point: ``left`` / ``right`` (lowest among ties) or
    ``bottom`` / ``top`` (leftmost among ties)."""
    tol = 1e-6
    if which in ("left", "right"):
        sign = 1.0 if which == "right" else -1.0
        best = max(sign * p[0] for p in pts)
        cands = [p for p in pts if sign * p[0] >= best - tol]
        return min(cands, key=lambda p: (p[1], p[0]))
    sign = 1.0 if which == "top" else -1.0
    best = max(sign * p[1] for p in pts)
    cands = [p for p in pts if sign * p[1] >= best - tol]
    return min(cands, key=lambda p: (p[0], p[1]))


def _wire_points(wire: Any, to2d: Any) -> list[tuple[float, float]]:
    """Ordered points of a wire (edges in wire order, orientation applied)."""
    ex = BRepTools_WireExplorer(wire.wrapped)
    out: list[tuple[float, float]] = []
    while ex.More():
        edge = ex.Current()
        curve = BRepAdaptor_Curve(edge)
        disc = GCPnts_TangentialDeflection(curve, 0.05, 0.005, 2)
        pts = [disc.Value(i) for i in range(1, disc.NbPoints() + 1)]
        if edge.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
            pts.reverse()
        for p in pts:
            q = to2d(bd.Vector(p.X(), p.Y(), p.Z()))
            if not out or math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) > 1e-7:
                out.append(q)
        ex.Next()
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < 1e-7:
        out.pop()
    return out


# --------------------------------------------------------------------------- #
# Part analysis: coaxial bores / bosses, horizontal faces, section features
# --------------------------------------------------------------------------- #


class _Classifier:
    """Point-in-solid test on the full part (set up once)."""

    def __init__(self, shape: Any):
        self._cls = BRepClass3d_SolidClassifier(shape.wrapped)

    def inside(self, p: bd.Vector) -> bool:
        self._cls.Perform(gp_Pnt(float(p.X), float(p.Y), float(p.Z)), 1e-7)
        return self._cls.State() == TopAbs_State.TopAbs_IN


@dataclass
class _Seg:
    kind: str  # "cyl" | "cone"
    t0: float
    t1: float
    r0: float  # radius at t0
    r1: float  # radius at t1

    @property
    def r(self) -> float:
        return max(self.r0, self.r1)


@dataclass
class _Stack:
    """Coaxial chain of full (360°) cylinder / cone segments: a bore when
    ``inner`` (material outside), a boss / shaft segment chain otherwise.
    ``t`` runs along ``d`` from ``o``; ``into`` is +1 when the material lies
    towards +t from the entry end ``entry_t``."""

    inner: bool
    d: bd.Vector
    o: bd.Vector
    segs: list[_Seg]
    entry_t: float = 0.0
    into: float = -1.0
    through: bool | None = None
    op_id: str | None = None
    name: str = ""

    @property
    def t_lo(self) -> float:
        return min(s.t0 for s in self.segs)

    @property
    def t_hi(self) -> float:
        return max(s.t1 for s in self.segs)

    def at(self, t: float) -> bd.Vector:
        return self.o + self.d * t

    def cyls_from_entry(self) -> list[_Seg]:
        cyls = [s for s in self.segs if s.kind == "cyl"]
        return sorted(cyls, key=lambda s: abs((s.t0 + s.t1) / 2.0 - self.entry_t))

    def depth_of(self, t: float) -> float:
        return abs(t - self.entry_t)

    def far_t(self, seg: _Seg) -> float:
        return seg.t1 if abs(seg.t1 - self.entry_t) > abs(seg.t0 - self.entry_t) else seg.t0

    def near_t(self, seg: _Seg) -> float:
        return seg.t0 if abs(seg.t1 - self.entry_t) > abs(seg.t0 - self.entry_t) else seg.t1

    def signature(self) -> tuple:
        prof = tuple((s.kind, round(s.r0, 3), round(s.r1, 3), round(self.depth_of(s.t0), 3), round(self.depth_of(s.t1), 3))
                     for s in sorted(self.segs, key=lambda s: self.depth_of((s.t0 + s.t1) / 2.0)))
        dirn = tuple(round(abs(c), 3) for c in (self.d.X, self.d.Y, self.d.Z))
        return ("stack", dirn, self.through, prof)

    def worthy(self) -> bool:
        """Needs a section: blind, or more than one diameter."""
        radii = {round(s.r, 4) for s in self.segs if s.kind == "cyl"}
        return self.through is False or len(radii) > 1


@dataclass
class _HFace:
    face: Any
    z: float
    up: bool  # outward normal +Z
    owner: _Stack | None
    area: float
    center: bd.Vector

    def signature(self) -> tuple:
        return ("face", round(self.z, 3), self.up, round(self.area, 2))


@dataclass
class _Part:
    shape: Any
    holes: list[HoleRecord]
    key: str
    has_base: bool
    bmin: bd.Vector
    bmax: bd.Vector
    center: bd.Vector
    diag: float
    cls: _Classifier
    stacks: list[_Stack] = field(default_factory=list)
    bosses: list[_Stack] = field(default_factory=list)
    hfaces: list[_HFace] = field(default_factory=list)
    proj: dict[str, tuple[list[_Prim], list[_Prim]]] = field(default_factory=dict)
    coil: bool = False  # helical faces: polygonal HLR (:mod:`app.cad.hlr_poly`)
    lin: float = 0.0  # its linear deflection (mm)
    mesh: Any = None  # its triangulated copy (lazy)
    lettering: list[Any] = field(default_factory=list)  # text op regions (app.cad.text_geometry.TextRegion)

    def lettered(self, face: Any) -> bool:
        """A face of the letters of a text op (every vertex and its centre inside a text region): drawn, never
        analysed as a bore, step, pocket floor or outline."""
        if not self.lettering:
            return False
        pts = [face.center()] + [bd.Vector(v.X, v.Y, v.Z) for v in face.vertices()]
        return any(all(r.contains(p) for p in pts) for r in self.lettering)

    @property
    def size(self) -> tuple[float, float, float]:
        s = self.bmax - self.bmin
        return (s.X, s.Y, s.Z)

    def projection(self, name: str, hidden: bool) -> tuple[list[_Prim], list[_Prim]]:
        if name not in self.proj:
            if self.coil:
                self.proj[name] = _poly_project(self.meshed(), name, self.center, self.diag, self.lin,
                                                box=((self.bmin.X, self.bmin.Y, self.bmin.Z), (self.bmax.X, self.bmax.Y, self.bmax.Z)))
            else:
                self.proj[name] = _project(self.shape, name, self.center, self.diag)
            if self.lettering:  # letters: hundreds of short edges joined into polylines (DWG entity count)
                vis, hid = self.proj[name]
                self.proj[name] = (_chain(vis), _chain(hid))
        vis, hid = self.proj[name]
        return vis, (hid if hidden else [])

    def to2d(self, view: str, p: bd.Vector) -> tuple[float, float]:
        x, y, _ = _view_frame(view)
        rel = p - self.center
        return (rel.dot(x), rel.dot(y))

    def meshed(self) -> Any:
        if self.mesh is None:
            self.mesh = hlr_poly.mesh(self.shape, self.lin)
        return self.mesh


def _canon_axis(d: bd.Vector, o: bd.Vector) -> tuple[bd.Vector, bd.Vector]:
    comps = [d.X, d.Y, d.Z]
    dom = max(range(3), key=lambda i: abs(comps[i]))
    if comps[dom] < 0:
        d = -d
    o = o - d * o.dot(d)
    return d, o


def _radius_at(p: bd.Vector, d: bd.Vector, o: bd.Vector) -> float:
    rel = p - o
    return (rel - d * rel.dot(d)).length


def _angular_cover(parts: list[dict[str, Any]], t0: float, t1: float) -> float:
    """Smallest angular coverage (radians) of a merged run of faces along its axis: at each sampled height the
    angular spans of the faces present there add up. A full bore exported as two 180° halves (SolidWorks and other
    writers split closed cylinders) covers 2π, a fillet or slot end does not."""
    if t1 - t0 < 1e-9:
        return 0.0
    eps = 1e-6 * max(1.0, abs(t1 - t0))
    samples = [t0 + (t1 - t0) * k / 8.0 for k in range(1, 8)]
    return min(sum(p["du"] for p in parts if p["t0"] - eps <= t <= p["t1"] + eps) for t in samples)


def _find_stacks(shape: Any, skip: Any = None) -> tuple[list[_Stack], list[_Stack]]:
    """Full 360° coaxial cylinder / cone chains: (bores, bosses). Faces for which ``skip(face)`` holds (letters of a
    text op) are left out."""
    pieces: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    axes: dict[tuple, tuple[bd.Vector, bd.Vector]] = {}
    for face in shape.faces():
        gt = face.geom_type
        if gt not in (bd.GeomType.CYLINDER, bd.GeomType.CONE):
            continue
        if skip is not None and skip(face):
            continue
        # Sweeps may wrap elementary surfaces in a rectangular trim. The face
        # adaptor exposes their actual cylinder/cone without discarding the trim.
        adaptor = BRepAdaptor_Surface(face.wrapped)
        surf = adaptor.Cylinder() if gt == bd.GeomType.CYLINDER else adaptor.Cone()
        ax = surf.Axis()
        d = bd.Vector(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        o = bd.Vector(ax.Location().X(), ax.Location().Y(), ax.Location().Z())
        d, o = _canon_axis(d, o)
        pnt = face.position_at(0.5, 0.5)
        normal = face.normal_at(pnt)
        radial = pnt - (o + d * (pnt - o).dot(d))
        if radial.length < 1e-9:
            continue
        inner = normal.dot(radial) < 0
        verts = [bd.Vector(v.X, v.Y, v.Z) for v in face.vertices()]
        if not verts:
            continue
        ts = [(v - o).dot(d) for v in verts]
        t0, t1 = min(ts), max(ts)
        if t1 - t0 < 1e-6:
            continue
        if gt == bd.GeomType.CYLINDER:
            r0 = r1 = float(surf.Radius())
            kind = "cyl"
        else:
            kind = "cone"
            r0 = min(_radius_at(v, d, o) for v, t in zip(verts, ts) if abs(t - t0) < 1e-6) if any(abs(t - t0) < 1e-6 for t in ts) else 0.0
            r1 = max(_radius_at(v, d, o) for v, t in zip(verts, ts) if abs(t - t1) < 1e-6) if any(abs(t - t1) < 1e-6 for t in ts) else 0.0
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face.wrapped)
        key = (tuple(round(c, 4) + 0.0 for c in (d.X, d.Y, d.Z)), tuple(round(c, 3) + 0.0 for c in (o.X, o.Y, o.Z)), inner)
        axes.setdefault(key, (d, o))
        pieces[key].append({"kind": kind, "t0": t0, "t1": t1, "r0": r0, "r1": r1, "du": u1 - u0, "len": abs(v1 - v0)})

    bores: list[_Stack] = []
    bosses: list[_Stack] = []
    for key in sorted(pieces):
        d, o = axes[key]
        inner = key[2]
        # merge faces of the same surface (same kind / radii) whose t-ranges touch
        merged: list[dict[str, Any]] = []
        for p in sorted(pieces[key], key=lambda p: (p["kind"], round(p["r0"], 4), round(p["r1"], 4), p["t0"])):
            last = merged[-1] if merged else None
            if (last is not None and last["kind"] == p["kind"] and abs(last["r0"] - p["r0"]) < 1e-4
                    and abs(last["r1"] - p["r1"]) < 1e-4 and p["t0"] <= last["t1"] + 1e-6):
                last["t1"] = max(last["t1"], p["t1"])
                last["parts"].append(p)
            else:
                merged.append({**p, "parts": [p]})
        segs: list[_Seg] = []
        for m in merged:
            if _angular_cover(m["parts"], m["t0"], m["t1"]) < 2 * math.pi * 0.98:
                continue  # partial cylinder (fillet, slot end, …)
            segs.append(_Seg(m["kind"], m["t0"], m["t1"], m["r0"], m["r1"]))
        segs.sort(key=lambda s: (s.t0, s.t1))
        chain: list[_Seg] = []
        for s in segs + [None]:  # type: ignore[list-item]
            if s is not None and (not chain or s.t0 <= max(c.t1 for c in chain) + 1e-4):
                chain.append(s)
                continue
            if chain and any(c.kind == "cyl" for c in chain):
                (bores if inner else bosses).append(_Stack(inner=inner, d=d, o=o, segs=list(chain)))
            chain = [s] if s is not None else []
    return bores, bosses


def _axis_ref(st: _Stack) -> dict[str, float]:
    """A point on a stack's axis and its direction (diameter records: the checker ties a Ø to a feature)."""
    return {"x": st.o.X, "y": st.o.Y, "z": st.o.Z, "dx": st.d.X, "dy": st.d.Y, "dz": st.d.Z}


def _pattern(part: _Part, st: _Stack) -> list[tuple[float, float, float]]:
    """Axis points of the bores identical to ``st`` (same signature, ``st`` included) when there are several: one Ø
    dimension states them all (``N× Ø…``); empty for a single bore."""
    same = sorted((s.o.X + 0.0, s.o.Y + 0.0, s.o.Z + 0.0) for s in part.stacks if s.signature() == st.signature())
    return same if len(same) > 1 else []


def _orth(d: bd.Vector) -> tuple[bd.Vector, bd.Vector]:
    ref = bd.Vector(1, 0, 0) if abs(d.X) < 0.9 else bd.Vector(0, 1, 0)
    u = (ref - d * ref.dot(d)).normalized()
    return u, d.cross(u)


def _end_open(stack: _Stack, t_end: float, sign: float, cls: _Classifier) -> bool:
    seg = min(stack.segs, key=lambda s: min(abs(s.t0 - t_end), abs(s.t1 - t_end)))
    r = seg.r0 if abs(seg.t0 - t_end) < abs(seg.t1 - t_end) else seg.r1
    r = max(r, 0.05)
    c = stack.at(t_end + sign * max(0.25 * r, 0.05))
    u, v = _orth(stack.d)
    probes = [c] + [c + (u * math.cos(a) + v * math.sin(a)) * (0.9 * r) for a in (k * math.pi / 4 for k in range(8))]
    return not any(cls.inside(p) for p in probes)


def _resolve_entries(part: _Part) -> None:
    for st in part.stacks:
        lo_open = _end_open(st, st.t_lo, -1.0, part.cls)
        hi_open = _end_open(st, st.t_hi, 1.0, part.cls)
        if hi_open and not lo_open:
            st.through, st.entry_t, st.into = False, st.t_hi, -1.0
        elif lo_open and not hi_open:
            st.through, st.entry_t, st.into = False, st.t_lo, 1.0
        elif lo_open and hi_open:
            lo_seg = min(st.segs, key=lambda s: s.t0)
            hi_seg = max(st.segs, key=lambda s: s.t1)
            if lo_seg.r0 > hi_seg.r1 + 1e-6:
                st.through, st.entry_t, st.into = True, st.t_lo, 1.0
            else:
                st.through, st.entry_t, st.into = True, st.t_hi, -1.0
        else:
            st.through = None  # enclosed void: no entry
    # explicit hole ops: their own entry point and op id
    for hole in part.holes:
        a = bd.Vector(*hole.axis)
        for pos in hole.positions or [hole.entry]:
            p = bd.Vector(*pos)
            # the first bore met when drilling from the entry point (another bore on
            # the same axis line, e.g. drilled from the other face, is not this hole)
            best: tuple[float, _Stack, bool] | None = None
            for st in part.stacks:
                if abs(abs(st.d.dot(a)) - 1.0) > 1e-6 or _radius_at(p, st.d, st.o) > 1e-3:
                    continue
                tp = (p - st.o).dot(st.d)
                if st.d.dot(a) < 0 and tp >= st.t_hi - 1e-3:
                    cand = (tp - st.t_hi, st, False)
                elif st.d.dot(a) > 0 and tp <= st.t_lo + 1e-3:
                    cand = (st.t_lo - tp, st, True)
                else:
                    continue
                if best is None or cand[0] < best[0]:
                    best = cand
            if best is None:
                continue
            _, st, at_lo = best
            if st.op_id is None:
                st.op_id = hole.op_id
            elif hole.op_id not in st.op_id.split("/"):
                st.op_id += "/" + hole.op_id
            if st.through is not None:
                st.entry_t = st.t_lo if at_lo else st.t_hi
                st.into = 1.0 if at_lo else -1.0


def _analyse(shape: Any, holes: list[HoleRecord], key: str, has_base: bool, lettering: list[Any] | None = None) -> _Part:
    from .bounds import tight_bounds

    lo, hi = tight_bounds(shape)  # the optimal box, tightened on trimmed free-form faces (ground coil ends)
    bmin, bmax = bd.Vector(*lo), bd.Vector(*hi)
    part = _Part(
        shape=shape, holes=holes, key=key, has_base=has_base, bmin=bmin, bmax=bmax,
        center=(bmin + bmax) * 0.5, diag=(bmax - bmin).length, cls=_Classifier(shape), lettering=list(lettering or []),
    )
    if hlr_poly.has_helical_faces(shape):  # springs, split lock washers: the exact HLR takes about a minute per view
        part.coil, part.lin = True, hlr_poly.deflection(part.diag)[0]
    part.stacks, part.bosses = _find_stacks(shape, part.lettered if part.lettering else None)
    _resolve_entries(part)
    part.stacks = [s for s in part.stacks if s.through is not None]
    for i, st in enumerate(part.stacks):
        st.name = f"bore {i + 1}"
    for i, st in enumerate(part.bosses):
        st.name = f"boss {i + 1}"
    tol = max(1e-4, 1e-6 * part.diag)
    for face in shape.faces():
        if face.geom_type != bd.GeomType.PLANE:
            continue
        c = face.center()
        n = face.normal_at(c)
        if abs(abs(n.Z) - 1.0) > 1e-6:
            continue
        if not (part.bmin.Z + tol < c.Z < part.bmax.Z - tol):
            continue
        if part.lettered(face):
            continue
        verts = [bd.Vector(v.X, v.Y, v.Z) for v in face.vertices()]
        owner = None
        for st in part.stacks:
            if abs(abs(st.d.Z) - 1.0) > 1e-6:
                continue
            rmax = max(s.r for s in st.segs)
            if (st.t_lo - tol <= c.Z <= st.t_hi + tol) and verts and all(_radius_at(v, st.d, st.o) <= rmax + tol for v in verts):
                owner = st
                break
        part.hfaces.append(_HFace(face=face, z=c.Z, up=n.Z > 0, owner=owner, area=face.area, center=c))
    return part


# --------------------------------------------------------------------------- #
# Section planning
# --------------------------------------------------------------------------- #


@dataclass
class _SectionPlan:
    letter: str
    plane: str  # "XZ" | "YZ"
    offset: float
    features: list[str] = field(default_factory=list)


def _plane_geom(plane: str, offset: float) -> tuple[bd.Vector, bd.Vector]:
    """(point, normal towards the kept material) of a cutting plane."""
    if plane == "XZ":
        return bd.Vector(0, offset, 0), bd.Vector(0, 1, 0)
    return bd.Vector(offset, 0, 0), bd.Vector(-1, 0, 0)


def _stack_in_plane(st: _Stack, plane: str, offset: float) -> bool:
    if plane == "XZ":
        return abs(st.d.Y) < 1e-6 and abs(st.o.Y - offset) < 1e-4
    return abs(st.d.X) < 1e-6 and abs(st.o.X - offset) < 1e-4


def _face_crossings(face: Any, plane: str, offset: float) -> list[tuple[bd.Vector, bd.Vector]]:
    """Segments where the cutting plane meets a face (3D end points)."""
    pt, nrm = _plane_geom(plane, offset)
    sec = BRepAlgoAPI_Section(face.wrapped, gp_Pln(gp_Pnt(pt.X, pt.Y, pt.Z), gp_Dir(nrm.X, nrm.Y, nrm.Z)))
    sec.Build()
    if not sec.IsDone():
        return []
    out = []
    for e in bd.Compound(sec.Shape()).edges():
        a, b = e.start_point(), e.end_point()
        if (a - b).length > 1e-6:
            out.append((bd.Vector(a.X, a.Y, a.Z), bd.Vector(b.X, b.Y, b.Z)))
    return out


def _worthy(part: _Part) -> list[tuple[tuple, str, Any]]:
    feats: list[tuple[tuple, str, Any]] = []
    for st in part.stacks:
        if st.worthy():
            feats.append((st.signature(), _stack_label(st), st))
    for hf in part.hfaces:
        if hf.owner is None:
            feats.append((hf.signature(), f"Z step / pocket floor at Z {_fmt(hf.z)}", hf))
    return feats


def _stack_label(st: _Stack) -> str:
    cyls = st.cyls_from_entry()
    dias = "/".join("Ø" + _fmt(2 * s.r) for s in cyls)
    tail = "THRU" if st.through else "↧" + _fmt(st.depth_of(st.far_t(cyls[-1]))) if cyls else ""
    who = st.op_id or st.name
    return f"{who} {dias} {tail}".strip()


def _covers(part: _Part, feat: Any, plane: str, offset: float) -> bool:
    if isinstance(feat, _Stack):
        return _stack_in_plane(feat, plane, offset)
    return bool(_face_crossings(feat.face, plane, offset))


def _plan_sections(part: _Part, cfg: PackageConfig, warnings: list[Issue]) -> list[_SectionPlan]:
    feats = _worthy(part)
    if isinstance(cfg.section, SectionPlane):
        plan = _SectionPlan("A", cfg.section.plane, float(cfg.section.offset))
        plan.features = [label for _, label, f in feats if _covers(part, f, plan.plane, plan.offset)]
        return [plan]
    if cfg.section == "none":
        if feats:
            warnings.append(Issue(code="section_omitted", message="section disabled although the part has depth features: "
                                  + "; ".join(sorted({label for _, label, _ in feats})[:6])))
        return []
    if not feats:
        return []
    cands: set[tuple[str, float]] = {("XZ", round(part.center.Y, 6)), ("YZ", round(part.center.X, 6))}
    for _, _, f in feats:
        if isinstance(f, _Stack):
            if abs(f.d.Y) < 1e-6:
                cands.add(("XZ", round(f.o.Y, 6)))
            if abs(f.d.X) < 1e-6:
                cands.add(("YZ", round(f.o.X, 6)))
        else:
            cands.add(("XZ", round(f.center.Y, 6)))
            cands.add(("YZ", round(f.center.X, 6)))
    # a plane on the part boundary cuts nothing
    cands = {(p, o) for p, o in cands
             if (part.bmin.Y + 1e-4 < o < part.bmax.Y - 1e-4 if p == "XZ" else part.bmin.X + 1e-4 < o < part.bmax.X - 1e-4)}
    remaining: dict[tuple, list[tuple[str, Any]]] = defaultdict(list)
    for sig, label, f in feats:
        remaining[sig].append((label, f))
    cover_cache: dict[tuple[str, float, int], bool] = {}

    def covered(plane: str, off: float, f: Any) -> bool:
        k = (plane, off, id(f))
        if k not in cover_cache:
            cover_cache[k] = _covers(part, f, plane, off)
        return cover_cache[k]

    plans: list[_SectionPlan] = []
    while remaining and len(plans) < MAX_SECTIONS and cands:
        def score(c: tuple[str, float]) -> tuple:
            plane, off = c
            classes = sum(1 for items in remaining.values() if any(covered(plane, off, f) for _, f in items))
            inst = sum(1 for items in remaining.values() for _, f in items if covered(plane, off, f))
            axes = sum(1 for st in part.stacks if _stack_in_plane(st, plane, off))
            centre = part.center.Y if plane == "XZ" else part.center.X
            return (classes, inst, axes, -round(abs(off - centre), 6), plane == "XZ", -off)

        best = max(sorted(cands), key=score)
        if score(best)[0] == 0:
            break
        plane, off = best
        plan = _SectionPlan(SECTION_LETTERS[len(plans)], plane, off)
        for sig in sorted(remaining, key=str):
            hit = [label for label, f in remaining[sig] if covered(plane, off, f)]
            if hit:
                plan.features.append(hit[0] + (f" (+{len(remaining[sig]) - 1} identical)" if len(remaining[sig]) > 1 else ""))
                del remaining[sig]
        plans.append(plan)
        cands.discard(best)
    if remaining:
        labels = sorted(label for items in remaining.values() for label, _ in items)
        warnings.append(Issue(code="section_incomplete", message="no section shows: " + "; ".join(labels[:8])
                              + (" …" if len(labels) > 8 else ""), details={"features": labels}))
    return plans


# --------------------------------------------------------------------------- #
# Dimension plans and view contents
# --------------------------------------------------------------------------- #


@dataclass
class _DimPlan:
    kind: str
    axis: str | None
    orient: str  # "h": distance along screen x, "v": along screen y
    side: str  # "top" | "bottom" | "left" | "right"
    p1: tuple[float, float]  # view coords (model mm, part centre at 0)
    p2: tuple[float, float]
    measured: str
    prefix: str = ""  # "Ø" for diameters
    feature: str | None = None
    ref: dict[str, float] = field(default_factory=dict)
    at: float | None = None  # side "inline": the dimension line's view coordinate (v for "h", u for "v")
    pattern: list[tuple[float, float, float]] = field(default_factory=list)  # diameters: axis points of identical bores

    @property
    def value(self) -> float:
        return abs(self.p2[0] - self.p1[0]) if self.orient == "h" else abs(self.p2[1] - self.p1[1])


@dataclass
class _Callout:
    target: tuple[float, float]  # hole centre, view coords
    radius: float  # model mm
    lines: list[str]


@dataclass
class _VC:
    """Content of one view before placement (view coords, model mm)."""

    name: str
    label: str
    kind: str  # "projection" | "section"
    vis: list[_Prim]
    hid: list[_Prim]
    bbox: tuple[float, float, float, float]
    loops: list[list[list[tuple[float, float]]]] = field(default_factory=list)
    dims: list[_DimPlan] = field(default_factory=list)
    callouts: list[_Callout] = field(default_factory=list)
    marks: list[tuple[float, float, float]] = field(default_factory=list)  # centre marks (u, v, r)
    clines: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    cut: dict[str, Any] | None = None
    dimmed: set[Any] = field(default_factory=set)  # section views: bore signatures / boss ids given a Ø here


def _fmt(v: float) -> str:
    s = f"{v:.{DIM_DEC}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def _fmt_len(v: float) -> str:  # kept for compatibility
    return _fmt(v)


def _text_width(s: str, h: float) -> float:
    """Estimated advance of ``s`` at cap height ``h`` (mm). Fixed factors
    measured on Arial Unicode MS, so layout never depends on the fonts of the
    host (deterministic bytes everywhere)."""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o >= 0x2E80:
            w += 1.40 * h
        elif ch.isupper():
            w += 0.95 * h
        elif ch.isdigit():
            w += 0.78 * h
        elif ch.islower():
            w += 0.69 * h
        elif o > 0x7F:
            w += 0.85 * h
        else:
            w += 0.40 * h
    return w


@dataclass
class _Outline:
    """Inner outline (not a single circle) of a horizontal planar face: a pocket rim, a through opening or the
    footprint of a boss, as its XY bounding box (model mm)."""

    x0: float
    y0: float
    x1: float
    y1: float
    z: float
    name: str = ""


def _outlines(part: _Part) -> list[_Outline]:
    seen: set[tuple[float, float, float, float]] = set()
    out: list[_Outline] = []
    for face in part.shape.faces():
        if face.geom_type != bd.GeomType.PLANE:
            continue
        c = face.center()
        if abs(abs(face.normal_at(c).Z) - 1.0) > 1e-6:
            continue
        if part.lettered(face):
            continue
        for w in face.inner_wires():
            if part.lettering and any(all(r.contains(v) for v in w.vertices()) for r in part.lettering):
                continue  # the footprint of a letter (raised) or its rim (engraved)
            edges = w.edges()
            if edges and all(e.geom_type == bd.GeomType.CIRCLE for e in edges):
                circles = {(round(e.arc_center.X, 4), round(e.arc_center.Y, 4), round(e.radius, 4)) for e in edges}
                if len(circles) == 1:
                    continue  # a round hole / pocket / boss: a bore or boss stack carries it
            bb = w.bounding_box()
            key = (round(bb.min.X, 4), round(bb.min.Y, 4), round(bb.max.X, 4), round(bb.max.Y, 4))
            if key in seen or key[2] - key[0] < 1e-6 or key[3] - key[1] < 1e-6:
                continue
            seen.add(key)
            out.append(_Outline(*key, z=round(c.Z, 4)))
    out.sort(key=lambda o: (o.x0, o.y0, o.x1, o.y1))
    for i, o in enumerate(out):
        o.name = f"outline {i + 1}"
    return out


def _hole_instances(part: _Part) -> list[tuple[bd.Vector, bd.Vector, float, str]]:
    """(entry point, drilling axis, diameter, owner) of every hole instance:
    explicit hole ops plus bores found on the solid that no op explains."""
    out: list[tuple[bd.Vector, bd.Vector, float, str]] = []
    for h in part.holes:
        a = bd.Vector(*h.axis)
        for pos in h.positions or [h.entry]:
            out.append((bd.Vector(*pos), a, h.diameter, h.op_id))
    for st in part.stacks:
        if st.op_id is not None:
            continue
        cyls = st.cyls_from_entry()
        out.append((st.at(st.entry_t), st.d * st.into, 2 * cyls[-1].r, st.name))
    return out


def _geom_callouts(part: _Part) -> list[tuple[str, list[str], _Stack, int]]:
    """Callouts for bores that no hole op explains (imported base, cuts)."""
    groups: dict[tuple, list[_Stack]] = defaultdict(list)
    for st in part.stacks:
        if st.op_id is None:
            groups[st.signature()].append(st)
    out = []
    for sig in sorted(groups, key=str):
        sts = groups[sig]
        st = sts[0]
        cyls = st.cyls_from_entry()
        n = len(sts)
        cnt = pattern_prefix(n)
        last = cyls[-1]
        tail = "THRU" if st.through else f"↧{_fmt(st.depth_of(st.far_t(last)))}"
        if len(cyls) == 2 and cyls[0].r > cyls[1].r:
            lines = [f"{cnt}Ø{_fmt(2 * last.r)} {tail}", f"⌴Ø{_fmt(2 * cyls[0].r)} ↧{_fmt(st.depth_of(st.far_t(cyls[0])))}"]
        else:
            lines = [cnt + ", ".join(f"Ø{_fmt(2 * s.r)} ↧{_fmt(st.depth_of(st.far_t(s)))}" for s in cyls[:-1])
                     + (", " if len(cyls) > 1 else "") + f"Ø{_fmt(2 * last.r)} {tail}"]
        cone = next((s for s in st.segs if s.kind == "cone"), None)
        if cone is not None and min(abs(cone.t0 - st.entry_t), abs(cone.t1 - st.entry_t)) < 1e-6:
            lines.append(f"⌵Ø{_fmt(2 * cone.r)}")
        prefix = "F-00" if part.has_base else "GEOM"
        out.append((prefix, lines, st, n))
    return out


def _diameter_dims(part: _Part, vcs: dict[str, _VC], dimmed: set[Any]) -> None:
    """A native Ø DIMENSION for every hole group (one instance per bore signature, each cylinder diameter; inline
    across the circle in the view looking down the axis) and every round boss / outline (below that view), unless a
    section page already dimensions it (``dimmed``)."""
    view_of = {0: "right", 1: "front", 2: "top"}
    groups: dict[tuple, list[_Stack]] = defaultdict(list)
    for st in part.stacks:
        groups[st.signature()].append(st)
    for sig in sorted(groups, key=str):
        sts = groups[sig]
        comps = (sts[0].d.X, sts[0].d.Y, sts[0].d.Z)
        dom = max(range(3), key=lambda i: abs(comps[i]))
        view = view_of[dom]
        if abs(comps[dom]) < 0.999 or view not in vcs:
            continue  # oblique / missing view: callout and NOTES only
        vc = vcs[view]
        st = min(sts, key=lambda s: (round(-part.to2d(view, s.o)[0], 4), round(-part.to2d(view, s.o)[1], 4)))
        u, v = part.to2d(view, st.o)
        who = st.op_id or st.name
        members = _pattern(part, st)
        # cylinder diameters (unless a section dimensions this bore) and the surface Ø of a countersink cone
        radii = [] if sig in dimmed else sorted({round(s.r, 6) for s in st.segs if s.kind == "cyl"})
        sinks = sorted({round(s.r, 6) for s in st.segs if s.kind == "cone"} - set(radii))
        for k, r in enumerate(radii + sinks):
            what = f"{who} countersink diameter" if r in sinks else f"{who} bore diameter"
            vc.dims.append(_DimPlan("diameter", None, "h", "inline", (u - r, v), (u + r, v), what,
                                    prefix=pattern_prefix(len(members)) + "Ø", feature=who, ref={"diameter": 2 * r, **_axis_ref(st)},
                                    at=v + (0.0 if k == 0 else 0.75 * r), pattern=members))
    done: set[tuple] = set()
    for st in part.bosses:
        if id(st) in dimmed:
            continue
        comps = (st.d.X, st.d.Y, st.d.Z)
        dom = max(range(3), key=lambda i: abs(comps[i]))
        view = view_of[dom]
        if abs(comps[dom]) < 0.999 or view not in vcs:
            continue
        vc = vcs[view]
        u, v = part.to2d(view, st.o)
        for r in sorted({round(s.r, 6) for s in st.segs if s.kind == "cyl"}):
            key = (view, round(u, 4), round(v, 4), r)
            if key in done:
                continue
            done.add(key)
            vc.dims.append(_DimPlan("diameter", None, "h", "bottom", (u - r, v), (u + r, v), f"{st.name} outer diameter", prefix="Ø",
                                    feature=st.name, ref={"diameter": 2 * r, **_axis_ref(st)}))


def _callout_lines(hole: HoleRecord) -> list[str]:
    first = hole_callout_text(hole.diameter, hole.depth, hole.count)
    lines = [first]
    if hole.counterbore:
        lines.append(f"⌴Ø{_fmt(hole.counterbore['diameter'])} ↧{_fmt(hole.counterbore['depth'])}")
    if hole.countersink:
        lines.append(f"⌵Ø{_fmt(hole.countersink['diameter'])}×{_fmt(hole.countersink.get('angle', 90.0))}°")
    return lines


def _overview_views(part: _Part, cfg: PackageConfig, warnings: list[Issue], dimmed: set[Any] | None = None) -> tuple[list[_VC], list[str]]:
    names: list[str] = []
    for v in cfg.views:
        if v not in names:
            names.append(v)
    vcs: dict[str, _VC] = {}
    for name in names:
        vis, hid = part.projection(name, cfg.hidden_lines)
        vcs[name] = _VC(name=name, label=VIEW_LABELS[name], kind="projection", vis=vis, hid=hid, bbox=_bbox2(_prim_points(vis + hid)))

    # ---- overall dimensions
    size = part.size
    if cfg.dimensions:
        dims: list[tuple[str, str]] = []
        if "front" in vcs:
            dims.append(("front", "h"))
        elif "top" in vcs:
            dims.append(("top", "h"))
        if "front" in vcs:
            dims.append(("front", "v"))
        elif "right" in vcs:
            dims.append(("right", "v"))
        if "top" in vcs:
            dims.append(("top", "v"))
        elif "right" in vcs:
            dims.append(("right", "h"))
        for view, orient in dims:
            vc = vcs[view]
            pts = _prim_points(vc.vis + vc.hid)
            axis = VIEW_AXES[view][0 if orient == "h" else 1]
            if orient == "h":
                p1, p2, side = _extreme(pts, "left"), _extreme(pts, "right"), "bottom"
            else:
                p1, p2, side = _extreme(pts, "bottom"), _extreme(pts, "top"), "left"
            vc.dims.append(_DimPlan("overall", axis, orient, side, p1, p2, f"overall {axis.upper()} (bounding box)",
                                    ref={"size": size["xyz".index(axis)]}))

    # ---- Ø of every hole group and round boss, size of every inner outline (view looking down them)
    dimmed = dimmed or set()
    outlines = _outlines(part)
    if cfg.dimensions:
        _diameter_dims(part, vcs, dimmed)
        if "top" in vcs:
            vc = vcs["top"]
            for o in outlines:
                ym, xm = o.y0 + 0.3 * (o.y1 - o.y0), o.x0 + 0.3 * (o.x1 - o.x0)
                pa, pb = part.to2d("top", bd.Vector(o.x0, ym, o.z)), part.to2d("top", bd.Vector(o.x1, ym, o.z))
                vc.dims.append(_DimPlan("size", "x", "h", "inline", pa, pb, f"{o.name} width along X", feature=o.name,
                                        ref={"lo": o.x0, "hi": o.x1}, at=pa[1]))
                pa, pb = part.to2d("top", bd.Vector(xm, o.y0, o.z)), part.to2d("top", bd.Vector(xm, o.y1, o.z))
                vc.dims.append(_DimPlan("size", "y", "v", "inline", pa, pb, f"{o.name} length along Y", feature=o.name,
                                        ref={"lo": o.y0, "hi": o.y1}, at=pa[0]))

    # ---- hole / outline positions from the datum, centre marks, callouts
    instances = _hole_instances(part)
    for view, look in VIEW_LOOK_AXIS.items():
        if view not in vcs:
            continue
        vc = vcs[view]
        hs = []
        for p, a, dia, owner in instances:
            comps = (a.X, a.Y, a.Z)
            if abs(abs(comps[look]) - 1.0) > 1e-3:
                continue
            u, v = part.to2d(view, p)
            hs.append((u, v, dia, owner, p))
            vc.marks.append((u, v, dia / 2.0))
        if view == "top":  # outline edges nearest the datum (no centre mark)
            for o in outlines:
                p = bd.Vector(o.x0, o.y0, o.z)
                u, v = part.to2d(view, p)
                hs.append((u, v, 0.0, o.name, p))
        if not hs or not cfg.dimensions:
            continue
        u0, v0 = vc.bbox[0], vc.bbox[1]
        ax_h, ax_v = VIEW_AXES[view]
        datum = {"x": part.bmin.X, "y": part.bmin.Y, "z": part.bmin.Z}
        for orient, axis in (("h", ax_h), ("v", ax_v)):
            seen: dict[float, tuple[float, float, str, bd.Vector]] = {}
            for u, v, _, owner, p in sorted(hs, key=lambda h: (h[0], h[1]) if orient == "h" else (h[1], h[0])):
                k = round(u if orient == "h" else v, 4)
                if (k - (u0 if orient == "h" else v0)) > 1e-3 and k not in seen:
                    seen[k] = (u, v, owner, p)
            keys = sorted(seen)
            if len(keys) > MAX_POSITION_DIMS:
                warnings.append(Issue(code="position_dims_truncated", message=f"{view} view: {len(keys)} distinct hole {axis.upper()} positions, "
                                      f"the first {MAX_POSITION_DIMS} are dimensioned"))
                keys = keys[:MAX_POSITION_DIMS]
            side = "top" if orient == "h" else "left"
            for k in keys:
                u, v, owner, p = seen[k]
                coord = {"x": p.X, "y": p.Y, "z": p.Z}[axis]
                vc.dims.append(_DimPlan("position", axis, orient, side, (u0, v0), (u, v),
                                        f"hole {owner} centre {axis.upper()} from datum (part min {axis.upper()})", feature=owner,
                                        ref={"datum": datum[axis], "coord": coord}))

    callouts: list[str] = []
    entries: list[tuple[str, list[str], list[bd.Vector], bd.Vector, float]] = []
    for hole in part.holes:
        text = hole_callout_text(hole.diameter, hole.depth, hole.count, hole.counterbore, hole.countersink)
        callouts.append(f"{hole.op_id}: {text}")
        entries.append((hole.op_id, _callout_lines(hole), [bd.Vector(*p) for p in (hole.positions or [hole.entry])],
                        bd.Vector(*hole.axis), hole.diameter))
    for prefix, lines, st, _ in _geom_callouts(part):
        callouts.append(f"{prefix}: {' '.join(lines)}")
        same = [s for s in part.stacks if s.op_id is None and s.signature() == st.signature()]
        cyl = st.cyls_from_entry()[-1]
        entries.append((prefix, lines, [s.at(s.entry_t) for s in same], st.d * st.into, 2 * cyl.r))
    for _, lines, positions, axis, dia in entries:
        comps = (axis.X, axis.Y, axis.Z)
        dom = max(range(3), key=lambda i: abs(comps[i]))
        view = {0: "right", 1: "front", 2: "top"}[dom]
        if abs(comps[dom]) < 0.999 or view not in vcs:
            continue  # oblique holes / missing view: NOTES list only
        vc = vcs[view]
        u1, v1 = vc.bbox[2], vc.bbox[3]
        pts2 = [part.to2d(view, p) for p in positions]
        target = min(pts2, key=lambda q: (q[0] - u1) ** 2 + (q[1] - v1) ** 2)
        vc.callouts.append(_Callout(target, dia / 2.0, lines))
    return [vcs[n] for n in names], callouts


def _section_view(part: _Part, plan: _SectionPlan, cfg: PackageConfig, warnings: list[Issue]) -> _VC | None:
    pt, keep = _plane_geom(plan.plane, plan.offset)
    lo, hi = (part.bmin.Y, part.bmax.Y) if plan.plane == "XZ" else (part.bmin.X, part.bmax.X)
    if not (lo + 1e-6 < plan.offset < hi - 1e-6):
        warnings.append(Issue(code="section_misses_part", message=f"section {plan.letter}-{plan.letter}: plane {plan.plane} at "
                              f"{_fmt(plan.offset)} does not cut the part ({_fmt(lo)} … {_fmt(hi)})"))
        return None
    try:
        half = part.shape.split(bd.Plane(origin=(pt.X, pt.Y, pt.Z), z_dir=(keep.X, keep.Y, keep.Z)), keep=bd.Keep.TOP)
    except Exception as exc:  # pragma: no cover - OCCT failure
        warnings.append(Issue(code="section_failed", message=f"section {plan.letter}-{plan.letter}: split failed: {exc}"))
        return None
    if isinstance(half, list) and part.coil:  # a coil cut along its axis: one piece per half turn
        half = bd.Compound(half) if half else None
    if half is None or getattr(half, "volume", 0.0) <= 1e-9:
        warnings.append(Issue(code="section_failed", message=f"section {plan.letter}-{plan.letter}: no material behind the plane"))
        return None
    view = "front" if plan.plane == "XZ" else "right"
    if part.coil:
        vis, _ = _poly_project(hlr_poly.mesh(half, part.lin), view, part.center, part.diag, part.lin, hidden=False)
    else:
        vis, _ = _project(half, view, part.center, part.diag, hidden=False)
    x_ax, y_ax, n = _view_frame(view)

    def to2d(p: bd.Vector) -> tuple[float, float]:
        rel = p - part.center
        return (rel.dot(x_ax), rel.dot(y_ax))

    loops: list[list[list[tuple[float, float]]]] = []
    for f in half.faces():
        if f.geom_type != bd.GeomType.PLANE:
            continue
        c = f.center()
        if f.normal_at(c).dot(n) < 1.0 - 1e-6 or abs((c - pt).dot(keep)) > 1e-5:
            continue
        face_loops = [_wire_points(f.outer_wire(), to2d)] + [_wire_points(w, to2d) for w in f.inner_wires()]
        face_loops = [lp for lp in face_loops if len(lp) >= 3]
        if face_loops:
            loops.append(face_loops)
    loops.sort(key=lambda fl: (round(min(p[0] for p in fl[0]), 4), round(min(p[1] for p in fl[0]), 4)))
    pts = _prim_points(vis) + [p for fl in loops for lp in fl for p in lp]
    vc = _VC(name=f"section_{plan.letter}", label=f"SECTION {plan.letter}-{plan.letter}", kind="section", vis=vis, hid=[],
             bbox=_bbox2(pts), loops=loops)
    if not loops:
        warnings.append(Issue(code="section_empty", message=f"section {plan.letter}-{plan.letter}: no material on the cutting plane"))
    _section_dims(part, plan, vc, view, cfg)
    return vc


def _section_dims(part: _Part, plan: _SectionPlan, vc: _VC, view: str, cfg: PackageConfig) -> None:
    x_ax, y_ax, n = _view_frame(view)
    pt, keep = _plane_geom(plan.plane, plan.offset)
    w = (pt - part.center).dot(n)

    def to2d(p: bd.Vector) -> tuple[float, float]:
        rel = p - part.center
        return (rel.dot(x_ax), rel.dot(y_ax))

    def on_plane(u: float, v: float) -> bd.Vector:
        return part.center + x_ax * u + y_ax * v + n * w

    pts = _prim_points(vc.vis)
    if not pts:
        return
    u0, v0, u1, v1 = _bbox2(pts)
    # centre lines of every bore / boss whose axis lies in the plane
    for st in part.stacks + part.bosses:
        if _stack_in_plane(st, plan.plane, plan.offset):
            vc.clines.append((to2d(st.at(st.t_lo)), to2d(st.at(st.t_hi))))
    if not cfg.dimensions:
        return
    datum_pt = _extreme(pts, "bottom")
    top_pt = _extreme(pts, "top")
    datum_z = datum_pt[1] + part.center.Z
    vc.dims.append(_DimPlan("height", "z", "v", "left", datum_pt, top_pt, "overall height in the section",
                            ref={"datum_z": datum_z, "z": top_pt[1] + part.center.Z}))
    eps = max(0.02, 1e-4 * part.diag)

    # ---- bores with their axis in the plane (identical bores: dimensioned once)
    seen_sigs: set[tuple] = set()
    for st in part.stacks:
        if not _stack_in_plane(st, plan.plane, plan.offset) or st.signature() in seen_sigs:
            continue
        seen_sigs.add(st.signature())
        vc.dimmed.add(st.signature())
        a0, a1 = to2d(st.at(0.0)), to2d(st.at(1.0))
        vertical = abs(a1[0] - a0[0]) < 1e-9  # axis runs up / down the sheet
        mid = to2d(st.at((st.t_lo + st.t_hi) / 2.0))
        who = st.op_id or st.name
        axis_name = "xyz"[max(range(3), key=lambda k: abs((st.d.X, st.d.Y, st.d.Z)[k]))]

        def coord(t: float, st: _Stack = st, axis_name: str = axis_name) -> float:
            p3 = st.at(t)
            return (p3.X, p3.Y, p3.Z)["xyz".index(axis_name)]

        def end_side(t: float, vertical: bool = vertical, mid: tuple[float, float] = mid, st: _Stack = st) -> str:
            q = to2d(st.at(t))
            if vertical:
                return "top" if q[1] >= mid[1] else "bottom"
            return "right" if q[0] >= mid[0] else "left"

        members = _pattern(part, st)
        for entry_t, segs, core in _bore_groups(st):
            for s in segs + ([core] if core is not None else []):
                near = s.t0 if abs(s.t0 - entry_t) <= abs(s.t1 - entry_t) else s.t1
                q = to2d(st.at(near))
                side = end_side(entry_t)
                if vertical:
                    p1, p2 = (q[0] - s.r, q[1]), (q[0] + s.r, q[1])
                else:
                    p1, p2 = (q[0], q[1] - s.r), (q[0], q[1] + s.r)
                vc.dims.append(_DimPlan("diameter", None, "h" if vertical else "v", side, p1, p2, f"{who} bore diameter",
                                        prefix=pattern_prefix(len(members)) + "Ø", feature=who,
                                        ref={"diameter": 2 * s.r, **_axis_ref(st)}, pattern=members))
            if not segs:
                continue
            e2 = to2d(st.at(entry_t))
            r_entry = segs[0].r
            for i, s in enumerate(segs):
                far = s.t1 if abs(s.t1 - entry_t) > abs(s.t0 - entry_t) else s.t0
                q = to2d(st.at(far))
                what = "floor" if (not st.through and i == len(segs) - 1) else "shoulder"
                ref = {"entry": coord(entry_t), "floor": coord(far), **_axis_ref(st)}
                text = f"{who} Ø{_fmt(2 * s.r)} {what} depth from its entry"
                if vertical:
                    vc.dims.append(_DimPlan("depth", axis_name, "v", "right", (e2[0] + r_entry, e2[1]), (q[0] + s.r, q[1]),
                                            text, feature=who, ref=ref))
                else:
                    side, sg = ("top", 1.0) if e2[1] > 0 else ("bottom", -1.0)
                    vc.dims.append(_DimPlan("depth", axis_name, "h", side, (e2[0], e2[1] + sg * r_entry), (q[0], q[1] + sg * s.r),
                                            text, feature=who, ref=ref))
    for st in part.bosses:
        if not _stack_in_plane(st, plan.plane, plan.offset):
            continue
        vc.dimmed.add(id(st))
        a0, a1 = to2d(st.at(0.0)), to2d(st.at(1.0))
        vertical = abs(a1[0] - a0[0]) < 1e-9
        for s in [s for s in st.segs if s.kind == "cyl"]:
            lo2, hi2 = to2d(st.at(s.t0)), to2d(st.at(s.t1))
            if vertical:
                (bl, bh) = sorted((lo2, hi2), key=lambda q: q[1])
                down = _clear(part, on_plane, [(bl[0] - s.r, bl[1]), (bl[0] + s.r, bl[1])], (0.0, -1.0), bl[1] - v0, eps)
                up = _clear(part, on_plane, [(bh[0] - s.r, bh[1]), (bh[0] + s.r, bh[1])], (0.0, 1.0), v1 - bh[1], eps)
                q, side = (bl, "bottom") if down or not up else (bh, "top")
                vc.dims.append(_DimPlan("diameter", None, "h", side, (q[0] - s.r, q[1]), (q[0] + s.r, q[1]),
                                        f"{st.name} outer diameter", prefix="Ø", feature=st.name,
                                        ref={"diameter": 2 * s.r, **_axis_ref(st)}))
            else:
                (bl, bh) = sorted((lo2, hi2), key=lambda q: q[0])
                left = _clear(part, on_plane, [(bl[0], bl[1] - s.r), (bl[0], bl[1] + s.r)], (-1.0, 0.0), bl[0] - u0, eps)
                q, side = (bl, "left") if left else (bh, "right")
                vc.dims.append(_DimPlan("diameter", None, "v", side, (q[0], q[1] - s.r), (q[0], q[1] + s.r),
                                        f"{st.name} outer diameter", prefix="Ø", feature=st.name,
                                        ref={"diameter": 2 * s.r, **_axis_ref(st)}))

    # ---- horizontal faces cut by the plane: pocket depths and step levels
    levels: dict[float, tuple[float, float, str]] = {}
    span = max(v1 - v0, 1e-6)
    for hf in part.hfaces:
        if hf.owner is not None:
            continue
        segs = _face_crossings(hf.face, plan.plane, plan.offset)
        if not segs:
            continue
        us = [to2d(p)[0] for a, b in segs for p in (a, b)]
        vf = hf.z - part.center.Z
        ul, ur = min(us), max(us)
        s = 1.0 if hf.up else -1.0
        left_wall = part.cls.inside(on_plane(ul - eps, vf + s * eps))
        right_wall = part.cls.inside(on_plane(ur + eps, vf + s * eps))
        if left_wall and right_wall:
            rim_l = _scan(part, on_plane, ul - eps, vf + s * eps, s, span)
            rim_r = _scan(part, on_plane, ur + eps, vf + s * eps, s, span)
            # the shallower wall is the entry; on a tie the right wall (depths sit on the right)
            rim, wall_u = (rim_l, ul) if abs(rim_l - vf) < abs(rim_r - vf) - 1e-6 else (rim_r, ur)
            vc.dims.append(_DimPlan("depth", "z", "v", "right", (wall_u, rim), (wall_u, vf),
                                    f"pocket floor at Z {_fmt(hf.z)}: depth from its rim", feature=f"pocket@Z{_fmt(hf.z)}",
                                    ref={"entry": rim + part.center.Z, "floor": hf.z, "x": hf.center.X, "y": hf.center.Y}))
        else:
            k = round(vf, 4)
            # dimension from the side the face is nearer to (short extension lines)
            cand = (ul, vf, "left") if ul - u0 <= u1 - ur else (ur, vf, "right")
            if k not in levels or _side_gap(cand, u0, u1) < _side_gap(levels[k], u0, u1):
                levels[k] = cand
    datum_right = _extreme([p for p in pts if abs(p[1] - datum_pt[1]) < 1e-6] or pts, "right")
    for k in sorted(levels):
        u, v, side = levels[k]
        if v - datum_pt[1] > 1e-3 and top_pt[1] - v > 1e-3:
            vc.dims.append(_DimPlan("level", "z", "v", side, datum_pt if side == "left" else datum_right, (u, v),
                                    f"step face Z {_fmt(v + part.center.Z)} from the part bottom",
                                    ref={"datum_z": datum_z, "z": v + part.center.Z}))
    # de-duplicate (same kind, value and definition points)
    seen: set[tuple] = set()
    uniq: list[_DimPlan] = []
    for d in vc.dims:
        key = (d.kind, d.orient, round(d.value, 4), tuple(round(c, 3) for c in d.p1 + d.p2))
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    vc.dims = uniq


def _bore_groups(st: _Stack) -> list[tuple[float, list[_Seg], _Seg | None]]:
    """How a bore is dimensioned: ``(entry t, cylinders whose far end gets a
    depth from that entry, ordered from it, core cylinder or None)``. A blind
    bore has one group from its entry (the last far end is the floor). A
    through bore is split at its narrowest cylinder (the core, THRU, no
    depth): wider steps above it are measured from the upper end, those below
    from the lower end, e.g. counterbores from both faces."""
    cyls = sorted((s for s in st.segs if s.kind == "cyl"), key=lambda s: (s.t0, s.t1))
    if not st.through:
        return [(st.entry_t, st.cyls_from_entry(), None)]
    core = min(range(len(cyls)), key=lambda i: (round(cyls[i].r, 6), i))
    upper = cyls[core + 1:][::-1]
    lower = cyls[:core]
    groups: list[tuple[float, list[_Seg], _Seg | None]] = []
    core_end = st.t_hi if abs(st.entry_t - st.t_hi) <= abs(st.entry_t - st.t_lo) else st.t_lo
    for end, segs in ((st.t_hi, upper), (st.t_lo, lower)):
        groups.append((end, segs, cyls[core] if end == core_end else None))
    return groups


def _side_gap(c: tuple[float, float, str], u0: float, u1: float) -> float:
    return c[0] - u0 if c[2] == "left" else u1 - c[0]


def _clear(part: _Part, on_plane: Any, starts: list[tuple[float, float]], direction: tuple[float, float], length: float,
           eps: float) -> bool:
    """True when extension lines from ``starts`` along ``direction`` to the
    view outline do not run through material."""
    for su, sv in starts:
        for k in range(1, 9):
            t = eps + (max(length, 0.0) + eps) * k / 8.0
            if part.cls.inside(on_plane(su + direction[0] * t, sv + direction[1] * t)):
                return False
    return True


def _scan(part: _Part, on_plane: Any, u: float, v_start: float, s: float, span: float) -> float:
    """Walk from (u, v_start) inside material in direction ``s`` until the
    material ends; returns that v (bisected to 1e-5 mm)."""
    step = span / 200.0
    inside_v = v_start
    out_v = None
    for k in range(1, 402):
        v = v_start + s * step * k
        if not part.cls.inside(on_plane(u, v)):
            out_v = v
            break
        inside_v = v
    if out_v is None:
        return inside_v
    a, b = inside_v, out_v
    for _ in range(40):
        m = (a + b) / 2.0
        if part.cls.inside(on_plane(u, m)):
            a = m
        else:
            b = m
        if abs(b - a) < 1e-6:
            break
    return round((a + b) / 2.0, 5)


# --------------------------------------------------------------------------- #
# Sheet writer: the same primitives to SVG and DXF
# --------------------------------------------------------------------------- #


def _n(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _has_cjk(s: str) -> bool:
    return any(ord(ch) > 0x2E80 for ch in s)


class _Sheet:
    def __init__(self, title: str, scale: float):
        self.svg: dict[str, list[str]] = {k: [] for k in ("border", "hatch", "views", "center", "dims", "text")}
        self.scale = scale
        doc = ezdxf.new("R2010", setup=False)
        # ezdxf stamps creation time into its metadata; pin it so identical
        # input gives identical DXF bytes (write-time stamps: see dxf_bytes).
        doc.ezdxf_metadata()["CREATED_BY_EZDXF"] = f"{ezdxf.__version__} @ 2000-01-01T00:00:00+00:00"
        doc.units = ezdxf.units.MM
        doc.header["$MEASUREMENT"] = 1
        doc.header["$LTSCALE"] = 1.0
        doc.linetypes.add("HIDDEN", pattern=[3.0, 2.0, -1.0], description="Hidden __ __ __")
        doc.linetypes.add("CENTER", pattern=[12.0, 8.0, -1.5, 1.0, -1.5], description="Center ____ _ ____")
        style = doc.styles.add(TEXT_STYLE["name"], font=TEXT_STYLE["font_file"])
        style.set_extended_font_data(family=TEXT_STYLE["family"])
        layers = doc.layers
        layers.add("VISIBLE", color=7, lineweight=50)
        layers.add("HIDDEN", color=8, linetype="HIDDEN", lineweight=25)
        layers.add("CENTER", color=1, linetype="CENTER", lineweight=18)
        layers.add("HATCH", color=8, lineweight=18)
        layers.add("CUTLINE", color=1, linetype="CENTER", lineweight=25)
        layers.add("DIM", color=3, lineweight=18)
        layers.add("LEADER", color=3, lineweight=18)
        layers.add("TEXT", color=7, lineweight=25)
        layers.add("CALLOUT", color=3, lineweight=25)  # hole callouts on the views (never the notes block)
        layers.add("BORDER", color=7, lineweight=70)
        lfac = 1.0 / scale
        dimvars = {
            "dimlfac": lfac, "dimscale": 1.0, "dimtxt": DIM_TXT, "dimasz": ARROW, "dimexo": DIM_EXO, "dimexe": DIM_EXE,
            "dimgap": DIM_GAP, "dimtad": 1, "dimjust": 0, "dimtih": 0, "dimtoh": 0, "dimtix": 1, "dimatfit": 3,
            "dimdec": DIM_DEC, "dimzin": 8, "dimdsep": ord("."), "dimlunit": 2, "dimtxsty": TEXT_STYLE["name"],
        }
        doc.dimstyles.new(DIMSTYLE, dxfattribs=dimvars)
        doc.header["$DIMSTYLE"] = DIMSTYLE
        doc.header["$TEXTSTYLE"] = TEXT_STYLE["name"]
        for k in ("dimlfac", "dimscale", "dimtxt", "dimasz", "dimexo", "dimexe", "dimgap", "dimtad", "dimtih", "dimtoh",
                  "dimtix", "dimdec", "dimzin", "dimdsep", "dimlunit", "dimtxsty"):
            doc.header["$" + k.upper()] = dimvars[k]
        self.doc = doc
        self.msp = doc.modelspace()
        self.title = title
        self.dim_count = 0
        self.hatch_count = 0

    # -- coordinates: paper mm, y up (DXF); SVG flips y
    @staticmethod
    def sy(y: float) -> float:
        return SHEET_H - y

    def _d(self, pts: list[tuple[float, float]], closed: bool = False) -> str:
        return "M" + " L".join(f"{_n(x)} {_n(self.sy(y))}" for x, y in pts) + (" Z" if closed else "")

    def polyline(self, pts: list[tuple[float, float]], layer: str, group: str = "views", closed: bool = False,
                 svg_class: str | None = None, lineweight: int | None = None) -> None:
        if len(pts) < 2:
            return
        cls = svg_class or {"VISIBLE": "vis", "HIDDEN": "hid", "DIM": "dim", "LEADER": "dim", "BORDER": "border", "TEXT": "thin",
                            "CENTER": "ctr", "CUTLINE": "cut"}[layer]
        self.svg[group].append(f'<path class="{cls}" d="{self._d(pts, closed)}"/>')
        attribs: dict[str, Any] = {"layer": layer}
        if lineweight is not None:
            attribs["lineweight"] = lineweight
        if len(pts) == 2 and not closed:
            self.msp.add_line(pts[0], pts[1], dxfattribs=attribs)
        else:
            self.msp.add_lwpolyline(pts, close=closed, dxfattribs=attribs)

    def circle(self, c: tuple[float, float], r: float, layer: str) -> None:
        cls = "vis" if layer == "VISIBLE" else "hid"
        self.svg["views"].append(f'<circle class="{cls}" cx="{_n(c[0])}" cy="{_n(self.sy(c[1]))}" r="{_n(r)}"/>')
        self.msp.add_circle(c, r, dxfattribs={"layer": layer})

    def arc(self, pts: list[tuple[float, float]], c: tuple[float, float], r: float, a0: float, a1: float, layer: str) -> None:
        cls = "vis" if layer == "VISIBLE" else "hid"
        self.svg["views"].append(f'<path class="{cls}" d="{self._d(pts)}"/>')
        self.msp.add_arc(c, r, a0, a1, dxfattribs={"layer": layer})

    def arrow(self, tip: tuple[float, float], toward: tuple[float, float], layer: str = "LEADER", size: float = ARROW) -> None:
        """Filled arrowhead at ``tip`` pointing away from ``toward``."""
        dx, dy = tip[0] - toward[0], tip[1] - toward[1]
        ln = math.hypot(dx, dy) or 1.0
        ux, uy = dx / ln, dy / ln
        bx, by = tip[0] - ux * size, tip[1] - uy * size
        w = size * 0.3
        p1, p2 = (bx - uy * w, by + ux * w), (bx + uy * w, by - ux * w)
        self.svg["dims"].append(f'<path class="arrow" d="{self._d([tip, p1, p2], True)}"/>')
        self.msp.add_solid([tip, p1, p2], dxfattribs={"layer": layer})

    def text(self, s: str, x: float, y: float, h: float = TEXT_H, anchor: str = "start", rotate: float = 0.0,
             group: str = "text", bold: bool = False, layer: str = "TEXT", svg_class: str | None = None) -> None:
        tr = f' transform="rotate({_n(-rotate)} {_n(x)} {_n(self.sy(y))})"' if rotate else ""
        weight = ' font-weight="bold"' if bold else ""
        cls = f' class="{svg_class}"' if svg_class else ""
        self.svg[group].append(
            f'<text{cls} x="{_n(x)}" y="{_n(self.sy(y))}" font-size="{_n(h / CAP_RATIO)}" text-anchor="{anchor}"{weight}{tr}>{escape(s)}</text>'
        )
        # baseline alignment, like the SVG text anchor
        align = {"start": TextEntityAlignment.LEFT, "middle": TextEntityAlignment.CENTER,
                 "end": TextEntityAlignment.RIGHT}[anchor]
        t = self.msp.add_text(s, height=h, rotation=rotate, dxfattribs={"layer": layer, "style": TEXT_STYLE["name"]})
        t.set_placement((x, y), align=align)

    def hatch(self, loops: list[list[tuple[float, float]]], tag: str) -> None:
        """Section hatch of one cut face: first loop outer, others holes."""
        d = " ".join(self._d(lp, True) for lp in loops)
        self.svg["hatch"].append(f'<path class="hatch" data-face={quoteattr(tag)} fill-rule="evenodd" d="{d}"/>')
        # hatch style 0 (normal, odd parity) + flagged islands: holes in the cut
        # face stay unhatched in every reader (ezdxf's default style 1 needs
        # the outermost flag to honour islands)
        h = self.msp.add_hatch(color=256, dxfattribs={"layer": "HATCH"})
        for i, lp in enumerate(loops):
            flags = dxfconst.BOUNDARY_PATH_EXTERNAL if i == 0 else dxfconst.BOUNDARY_PATH_OUTERMOST
            h.paths.add_polyline_path(lp, is_closed=True, flags=flags)
        h.set_pattern_fill("ANSI31", color=256, scale=HATCH_SCALE, style=0)
        self.hatch_count += 1

    def linear_dim(self, rec_id: str, kind: str, p1: tuple[float, float], p2: tuple[float, float], vertical: bool, at: float,
                   prefix: str) -> tuple[str, str, float, tuple[float, float], tuple[float, float]]:
        """One dimension: a DIMENSION entity in the DXF (native value = paper
        distance × DIMLFAC = model length; text ``<>`` / ``%%c<>``) and the
        same graphics in the SVG. ``at`` is the x (vertical) or y
        (horizontal) of the dimension line. Returns (handle, text, value, p1,
        p2) with the definition points in ascending order."""
        # definition points in ascending order along the measured direction
        # (a reversed pair flips "text above" and the arrow fit in CAD renderers)
        if (vertical and p1[1] > p2[1]) or (not vertical and p1[0] > p2[0]):
            p1, p2 = p2, p1
        if vertical:
            a, b = (at, p1[1]), (at, p2[1])
            value = abs(p2[1] - p1[1]) / self.scale
        else:
            a, b = (p1[0], at), (p2[0], at)
            value = abs(p2[0] - p1[0]) / self.scale
        label = prefix + _fmt(value)
        dim = self.msp.add_linear_dim(
            base=a, p1=p1, p2=p2, angle=90.0 if vertical else 0.0, text=prefix.replace("Ø", "%%c") + "<>",
            dimstyle=DIMSTYLE, dxfattribs={"layer": "DIM"},
        )
        dim.render()
        handle = dim.dimension.dxf.handle
        parts: list[str] = []
        for p, q in ((p1, a), (p2, b)):
            if vertical:
                sgn = 1.0 if q[0] >= p[0] else -1.0
                if abs(q[0] - p[0]) > DIM_EXO:
                    parts.append(f'<path class="dim" d="{self._d([(p[0] + sgn * DIM_EXO, p[1]), (q[0] + sgn * DIM_EXE, q[1])])}"/>')
            else:
                sgn = 1.0 if q[1] >= p[1] else -1.0
                if abs(q[1] - p[1]) > DIM_EXO:
                    parts.append(f'<path class="dim" d="{self._d([(p[0], p[1] + sgn * DIM_EXO), (q[0], q[1] + sgn * DIM_EXE)])}"/>')
        parts.append(f'<path class="dim" d="{self._d([a, b])}"/>')
        for tip, other in ((a, b), (b, a)):
            dx, dy = tip[0] - other[0], tip[1] - other[1]
            ln = math.hypot(dx, dy) or 1.0
            ux, uy = dx / ln, dy / ln
            bx, by = tip[0] - ux * ARROW, tip[1] - uy * ARROW
            w = ARROW * 0.3
            parts.append(f'<path class="arrow" d="{self._d([tip, (bx - uy * w, by + ux * w), (bx + uy * w, by - ux * w)], True)}"/>')
        if vertical:
            tx, ty = at - DIM_GAP, (a[1] + b[1]) / 2.0
            parts.append(f'<text x="{_n(tx)}" y="{_n(self.sy(ty))}" font-size="{_n(DIM_TXT / CAP_RATIO)}" text-anchor="middle" '
                         f'transform="rotate(-90 {_n(tx)} {_n(self.sy(ty))})">{escape(label)}</text>')
        else:
            tx, ty = (a[0] + b[0]) / 2.0, at + DIM_GAP
            parts.append(f'<text x="{_n(tx)}" y="{_n(self.sy(ty))}" font-size="{_n(DIM_TXT / CAP_RATIO)}" text-anchor="middle">{escape(label)}</text>')
        attrs = (f'class="dimension" id="{rec_id}" data-handle="{handle}" data-kind="{kind}" data-value="{_n(value)}" '
                 f'data-p1="{_n(p1[0])} {_n(self.sy(p1[1]))}" data-p2="{_n(p2[0])} {_n(self.sy(p2[1]))}"')
        self.svg["dims"].append(f"<g {attrs}>" + "".join(parts) + "</g>")
        self.dim_count += 1
        return handle, label, value, p1, p2

    def svg_bytes(self) -> bytes:
        style = (
            ".vis{stroke:#000;stroke-width:0.5;fill:none;stroke-linecap:round;stroke-linejoin:round}"
            ".hid{stroke:#000;stroke-width:0.25;fill:none;stroke-dasharray:2 1}"
            ".ctr{stroke:#000;stroke-width:0.18;fill:none;stroke-dasharray:8 1.5 1 1.5}"
            ".cut{stroke:#000;stroke-width:0.25;fill:none;stroke-dasharray:8 1.5 1 1.5}"
            ".cutend{stroke:#000;stroke-width:0.7;fill:none}"
            ".dim{stroke:#000;stroke-width:0.18;fill:none}"
            ".thin{stroke:#000;stroke-width:0.25;fill:none}"
            ".border{stroke:#000;stroke-width:0.7;fill:none}"
            ".arrow{fill:#000;stroke:none}"
            ".hatch{fill:url(#ansi31);stroke:none}"
            ".hl{stroke:#000;stroke-width:0.18}"
            f"text{{font-family:{TEXT_STYLE['svg_font_family']};fill:#000}}"
            ".dimension text{paint-order:stroke;stroke:#fff;stroke-width:0.6}"
        )
        sp = _n(HATCH_SPACING)
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(SHEET_W)}mm" height="{_n(SHEET_H)}mm" '
            f'viewBox="0 0 {_n(SHEET_W)} {_n(SHEET_H)}">',
            f"<title>{escape(self.title)}</title>",
            f"<style>{style}</style>",
            f'<defs><pattern id="ansi31" patternUnits="userSpaceOnUse" width="{sp}" height="{sp}" patternTransform="rotate(-45)">'
            f'<line class="hl" x1="0" y1="{_n(HATCH_SPACING / 2)}" x2="{sp}" y2="{_n(HATCH_SPACING / 2)}"/></pattern></defs>',
            f'<rect x="0" y="0" width="{_n(SHEET_W)}" height="{_n(SHEET_H)}" fill="#fff"/>',
        ]
        for group in ("border", "hatch", "views", "center", "dims", "text"):
            parts.append(f'<g id="{group}">')
            parts.extend(self.svg[group])
            parts.append("</g>")
        parts.append("</svg>")
        return ("\n".join(parts) + "\n").encode("utf-8")

    def dxf_bytes(self) -> bytes:
        stream = io.StringIO()
        # ezdxf's documented switch for fixed $TDCREATE/$TDUPDATE, GUIDs and the WRITTEN_BY marker: a
        # process-global option, set and restored under the lock shared with the DWG adapter (dxf_write.py).
        with fixed_metadata():
            self.doc.write(stream)
        return stream.getvalue().encode("utf-8")


def _scale_text(s: float) -> str:
    if s >= 1:
        return f"{_fmt(s)}:1"
    return f"1:{_fmt(1.0 / s)}"


# --------------------------------------------------------------------------- #
# Page layout and rendering
# --------------------------------------------------------------------------- #


def _side_dims(vc: _VC) -> dict[str, list[_DimPlan]]:
    out: dict[str, list[_DimPlan]] = {"top": [], "bottom": [], "left": [], "right": []}
    for d in vc.dims:
        if d.side != "inline":
            out[d.side].append(d)
    for side, ds in out.items():
        # shortest innermost; then by position for a stable order
        ds.sort(key=lambda d: (round(d.value, 4), d.kind, round(d.p2[0], 4), round(d.p2[1], 4)))
    return out


def _margins(vc: _VC) -> dict[str, float]:
    sides = _side_dims(vc)
    m: dict[str, float] = {}
    for side, ds in sides.items():
        m[side] = DIM_FIRST + (len(ds) - 1) * DIM_STEP + DIM_TXT + 2.5 if ds else VIEW_PAD
    m["bottom"] += LABEL_SPACE
    if vc.callouts:
        width = max(_text_width(line, TEXT_H) for c in vc.callouts for line in c.lines) + 8.0
        m["right"] = max(m["right"], width)
        m["top"] = max(m["top"], 10.0)
    if vc.cut is not None:
        ext = CUT_EXT + CUT_ARROW / 2 + 4.0
        if vc.cut["orient"] == "h":
            m["left"] = max(m["left"], ext)
            m["right"] = max(m["right"], ext)
            m["top"] = max(m["top"], CUT_ARROW + 7.0)
        else:
            m["top"] = max(m["top"], ext)
            m["bottom"] = max(m["bottom"], ext + LABEL_SPACE)
            m["left"] = max(m["left"], CUT_ARROW + 7.0)
    return m


def _layout(vcs: list[_VC], cells: dict[str, tuple[int, int]], area: tuple[float, float, float, float], cfg: PackageConfig,
            warnings: list[Issue], page_label: str,
            margins: dict[str, dict[str, float]] | None = None) -> tuple[float, dict[str, tuple[float, float]]]:
    """Scale and view origins. ``margins`` (per view name) replaces :func:`_margins` when a caller reserves extra
    paper around a view (assembly balloons); ``cfg`` only needs ``scale``."""
    margins = margins or {vc.name: _margins(vc) for vc in vcs}
    rows = sorted({cells[vc.name][0] for vc in vcs})
    cols = sorted({cells[vc.name][1] for vc in vcs})
    ax0, ay0, ax1, ay1 = area
    avail_w, avail_h = ax1 - ax0, ay1 - ay0

    def extents(s: float) -> tuple[dict[int, tuple[float, float]], dict[int, tuple[float, float]]]:
        cl: dict[int, tuple[float, float]] = {}
        rw: dict[int, tuple[float, float]] = {}
        for c in cols:
            members = [vc for vc in vcs if cells[vc.name][1] == c]
            left = max(-vc.bbox[0] * s + margins[vc.name]["left"] for vc in members)
            right = max(vc.bbox[2] * s + margins[vc.name]["right"] for vc in members)
            cl[c] = (left, right)
        for r in rows:
            members = [vc for vc in vcs if cells[vc.name][0] == r]
            up = max(vc.bbox[3] * s + margins[vc.name]["top"] for vc in members)
            down = max(-vc.bbox[1] * s + margins[vc.name]["bottom"] for vc in members)
            rw[r] = (up, down)
        return cl, rw

    def fits(s: float) -> bool:
        cl, rw = extents(s)
        return sum(a + b for a, b in cl.values()) <= avail_w + 1e-9 and sum(a + b for a, b in rw.values()) <= avail_h + 1e-9

    if cfg.scale == "auto":
        scale = next((s for s in STANDARD_SCALES if fits(s)), STANDARD_SCALES[-1])
        if not fits(scale):
            warnings.append(Issue(code="views_overflow", message=f"{page_label}: the views do not fit the sheet even at {_scale_text(scale)}"))
    else:
        scale = float(cfg.scale)
        if scale <= 0:
            raise DrawingError("scale must be positive")
        if not fits(scale):
            warnings.append(Issue(code="views_overflow", message=f"{page_label}: scale {_scale_text(scale)} is too large for the sheet; views may overlap"))
    cl, rw = extents(scale)
    pad_x = max(0.0, avail_w - sum(a + b for a, b in cl.values())) / (len(cols) + 1)
    pad_y = max(0.0, avail_h - sum(a + b for a, b in rw.values())) / (len(rows) + 1)
    cx: dict[int, float] = {}
    x = ax0 + pad_x
    for c in cols:
        cx[c] = x + cl[c][0]
        x += cl[c][0] + cl[c][1] + pad_x
    cy: dict[int, float] = {}
    y = ay1 - pad_y
    for r in rows:
        cy[r] = y - rw[r][0]
        y -= rw[r][0] + rw[r][1] + pad_y
    return scale, {vc.name: (cx[cells[vc.name][1]], cy[cells[vc.name][0]]) for vc in vcs}


def _render_view(sheet: _Sheet, vc: _VC, origin: tuple[float, float], scale: float, page: int,
                 records: list[DimensionRecord]) -> PageView:
    ox, oy = origin

    def P(p: tuple[float, float]) -> tuple[float, float]:
        return (ox + p[0] * scale, oy + p[1] * scale)

    for i, face_loops in enumerate(vc.loops):
        sheet.hatch([[P(p) for p in lp] for lp in face_loops], f"{vc.name}:{i + 1}")
    for prims, layer in ((vc.hid, "HIDDEN"), (vc.vis, "VISIBLE")):
        for pr in prims:
            if pr.kind == "circle" and pr.center:
                sheet.circle(P(pr.center), pr.radius * scale, layer)
            elif pr.kind == "arc" and pr.center:
                sheet.arc([P(p) for p in pr.pts], P(pr.center), pr.radius * scale, pr.a0, pr.a1, layer)
            else:
                sheet.polyline([P(p) for p in pr.pts], layer)
    for a, b in vc.clines:
        pa, pb = P(a), P(b)
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        ln = math.hypot(dx, dy) or 1.0
        ux, uy = dx / ln, dy / ln
        sheet.polyline([(pa[0] - ux * 3.0, pa[1] - uy * 3.0), (pb[0] + ux * 3.0, pb[1] + uy * 3.0)], "CENTER", "center")
    for u, v, r in vc.marks:
        c = P((u, v))
        arm = r * scale + 1.5
        sheet.polyline([(c[0] - arm, c[1]), (c[0] + arm, c[1])], "CENTER", "center")
        sheet.polyline([(c[0], c[1] - arm), (c[0], c[1] + arm)], "CENTER", "center")

    px0, py0 = P((vc.bbox[0], vc.bbox[1]))
    px1, py1 = P((vc.bbox[2], vc.bbox[3]))

    # ---- cutting plane line (section pages, on the top view)
    if vc.cut is not None:
        letter = vc.cut["letter"]
        du, dv = vc.cut["dir"]
        if vc.cut["orient"] == "h":
            yv = oy + vc.cut["coord"] * scale
            e0, e1 = (px0 - CUT_EXT, yv), (px1 + CUT_EXT, yv)
            ends = [(e0, (e0[0] + 5.0, yv)), (e1, (e1[0] - 5.0, yv))]
        else:
            xv = ox + vc.cut["coord"] * scale
            e0, e1 = (xv, py0 - CUT_EXT), (xv, py1 + CUT_EXT)
            ends = [(e0, (xv, e0[1] + 5.0)), (e1, (xv, e1[1] - 5.0))]
        sheet.polyline([e0, e1], "CUTLINE", "center")
        for end, inner in ends:
            sheet.polyline([end, inner], "CUTLINE", "center", svg_class="cutend", lineweight=70)
            tip = (end[0] + du * CUT_ARROW, end[1] + dv * CUT_ARROW)
            sheet.polyline([end, tip], "CUTLINE", "center", svg_class="cutend", lineweight=35)
            sheet.arrow(tip, end, layer="CUTLINE", size=3.0)
            lx, ly = tip[0] + du * 2.0, tip[1] + dv * 2.0
            if dv < -0.5:
                ly -= 5.0
            sheet.text(letter, lx + (2.5 if abs(dv) > 0.5 else 0.0), ly, 5.0, "middle", bold=True)

    # ---- dimensions
    sides = _side_dims(vc)
    extent = {s: 0.0 for s in sides}
    for side, ds in sides.items():
        for i, d in enumerate(ds):
            off = DIM_FIRST + i * DIM_STEP
            p1, p2 = P(d.p1), P(d.p2)
            if side == "top":
                at = py1 + off
            elif side == "bottom":
                at = py0 - off
            elif side == "left":
                at = px0 - off
            else:
                at = px1 + off
            rid = f"P{page}-D{len(records) + 1}"
            handle, label, value, p1, p2 = sheet.linear_dim(rid, d.kind, p1, p2, d.orient == "v", at, d.prefix)
            records.append(DimensionRecord(
                id=rid, page=page, view=vc.name, kind=d.kind, axis=d.axis, value=round(value, 6), text=label,  # type: ignore[arg-type]
                measured=d.measured, feature=d.feature, handle=handle,
                p1=[round(p1[0], 4), round(SHEET_H - p1[1], 4)], p2=[round(p2[0], 4), round(SHEET_H - p2[1], 4)],
                ref={k: round(v, 6) for k, v in d.ref.items()}, pattern=[[round(c, 6) for c in m] for m in d.pattern],
            ))
            extent[side] = off + DIM_TXT + 1.0
    for d in sorted((d for d in vc.dims if d.side == "inline"),
                    key=lambda d: (d.kind, round(d.p1[0], 4), round(d.p1[1], 4), round(d.value, 4))):
        p1, p2 = P(d.p1), P(d.p2)
        assert d.at is not None
        at = oy + d.at * scale if d.orient == "h" else ox + d.at * scale
        rid = f"P{page}-D{len(records) + 1}"
        handle, label, value, p1, p2 = sheet.linear_dim(rid, d.kind, p1, p2, d.orient == "v", at, d.prefix)
        records.append(DimensionRecord(
            id=rid, page=page, view=vc.name, kind=d.kind, axis=d.axis, value=round(value, 6), text=label,  # type: ignore[arg-type]
            measured=d.measured, feature=d.feature, handle=handle,
            p1=[round(p1[0], 4), round(SHEET_H - p1[1], 4)], p2=[round(p2[0], 4), round(SHEET_H - p2[1], 4)],
            ref={k: round(v, 6) for k, v in d.ref.items()}, pattern=[[round(c, 6) for c in m] for m in d.pattern],
        ))

    # ---- hole callouts (leader to the instance nearest the top-right corner)
    y_off = 0.0
    for c in vc.callouts:
        hx, hy = P(c.target)
        rr = c.radius * scale
        tip = (hx + rr * math.cos(math.radians(45)), hy + rr * math.sin(math.radians(45)))
        lx = px1 + 6.0 + (extent["right"] if extent["right"] else 0.0)
        ly = py1 + 4.0 - y_off
        y_off += len(c.lines) * (TEXT_H + 1.5) + 3.0
        width = max(_text_width(s, TEXT_H) for s in c.lines)
        sheet.polyline([tip, (lx, ly), (lx + width, ly)], "LEADER", "dims")
        sheet.arrow(tip, (lx, ly))
        for i, line in enumerate(c.lines):
            ty = ly + 0.8 if i == 0 else ly - 1.0 - TEXT_H - (i - 1) * (TEXT_H + 1.2)
            sheet.text(line, lx + 0.5, ty, TEXT_H, group="dims", layer="CALLOUT", svg_class="callout")

    # ---- view label below the view and its dimensions
    below = extent["bottom"]
    if vc.cut is not None and vc.cut["orient"] == "v":
        below = max(below, CUT_EXT + 1.0)
    label_y = py0 - max(below, 2.0) - 4.0 - TEXT_H
    sheet.text(vc.label, (px0 + px1) / 2.0, label_y, TEXT_H, "middle", bold=True)
    return PageView(name=vc.name, kind=vc.kind, label=vc.label, scale=scale,  # type: ignore[arg-type]
                    bbox=[round(px0, 3), round(SHEET_H - py1, 3), round(px1, 3), round(SHEET_H - py0, 3)])


#: Longest single TEXT value, in DWG R2000 string units: non-ASCII characters are stored as
#: ``\U+XXXX`` escapes (7 units each) and LibreDWG truncates longer values on readback, which
#: fails the DWG export check. Note lines are wrapped below this even when they fit the paper.
MAX_TEXT_UNITS = 240


def _text_units(t: str) -> int:
    return sum(1 if ord(c) < 128 else 7 for c in t)


def _wrap(text: str, width: float, h: float) -> list[str]:
    """Greedy word wrap (CJK runs break anywhere). Widths are estimated on the
    wide side so a substituted CAD font still fits; a line also stays within
    ``MAX_TEXT_UNITS`` so it survives the native DWG export."""

    def wide(t: str) -> float:
        return _text_width(t, h) * 1.05 if _text_units(t) <= MAX_TEXT_UNITS else width + 1.0

    lines: list[str] = []
    cur = ""
    for w in re.split(r"(\s+)", text):
        if not w:
            continue
        cand = cur + w
        if cur and wide(cand) > width:
            lines.append(cur.rstrip())
            cur = w.lstrip()
        else:
            cur = cand
        while wide(cur) > width and len(cur) > 1:  # long words / CJK runs
            cut = len(cur)
            while cut > 1 and wide(cur[:cut]) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


NOTE_INDENT = 4.0


def _notes_lines(notes: list[str]) -> tuple[list[tuple[str, bool, float]], float]:
    """Wrapped notes (text, bold, x indent) and the top of the block (paper y)."""
    width = SHEET_W - 2 * MARGIN - TITLE_W - 10.0
    lines: list[tuple[str, bool, float]] = [("NOTES:", True, 0.0)]
    for s in notes:
        wrapped = _wrap(s, width - NOTE_INDENT, NOTE_H)
        lines += [(ln, False, 0.0 if i == 0 else NOTE_INDENT) for i, ln in enumerate(wrapped)]
    max_lines = 18
    if len(lines) > max_lines:
        more = len(lines) - max_lines + 1
        lines = lines[: max_lines - 1] + [(f"… {more} more line(s): see the package coverage list", False, NOTE_INDENT)]
    height = 6.0 + len(lines) * NOTE_STEP
    return lines, MARGIN + max(TITLE_H, height)


def _notes_block(sheet: _Sheet, notes: list[str]) -> None:
    """Writes the notes left of the title block."""
    lines, top = _notes_lines(notes)
    y = top - 5.0
    for s, bold, indent in lines:
        sheet.text(s, MARGIN + 4.0 + indent, y, 3.2 if bold else NOTE_H, bold=bold)
        y -= NOTE_STEP


def _title_block(sheet: _Sheet, config: DrawingConfig, scale: float, key: str, page: int, pages: int) -> None:
    t = config.title
    x0, y0 = SHEET_W - MARGIN - TITLE_W, MARGIN
    rh = TITLE_H / 5.0
    sheet.polyline([(x0, y0), (x0 + TITLE_W, y0), (x0 + TITLE_W, y0 + TITLE_H), (x0, y0 + TITLE_H)], "BORDER", "border", closed=True)
    projection = "THIRD ANGLE" if config.projection == "third" else "FIRST ANGLE"
    rows: list[list[tuple[float, str, str]]] = [  # bottom row first: (width, label, value)
        [(180.0, "MODEL BUILD", key[:16])],
        [(60.0, "DRAWN", t.author), (30.0, "DATE", t.date), (30.0, "UNITS", "mm"), (60.0, "SHEET", f"{page} / {pages}")],
        [(90.0, "MATERIAL", t.material), (90.0, "PROJECT", t.project)],
        [(60.0, "PART NO.", t.part_number), (30.0, "REV", t.revision), (30.0, "SCALE", _scale_text(scale)), (60.0, "PROJECTION", projection)],
        [(120.0, "PART NAME", t.part_name), (60.0, "", "PartRoute")],
    ]
    for ri, row in enumerate(rows):
        yb = y0 + ri * rh
        if ri:
            sheet.polyline([(x0, yb), (x0 + TITLE_W, yb)], "BORDER", "border", svg_class="thin", lineweight=25)
        xc = x0
        for ci, (w, label, value) in enumerate(row):
            if ci:
                sheet.polyline([(xc, yb), (xc, yb + rh)], "BORDER", "border", svg_class="thin", lineweight=25)
            if label:
                sheet.text(label, xc + 1.2, yb + rh - SMALL_H - 0.9, SMALL_H)
            if value:
                big = ri == 4
                sheet.text(value, xc + 1.2 if label else xc + w / 2.0, yb + 1.4, 3.2 if big else 2.6,
                           "start" if label else "middle", bold=big or not label)
            xc += w


def _compose_page(index: int, pages: int, role: str, title: str, vcs: list[_VC], cells: dict[str, tuple[int, int]],
                  cfg: PackageConfig, notes: list[str], key: str, warnings: list[Issue]) -> DrawingPage:
    t = cfg.title
    _, notes_top = _notes_lines(notes)
    area = (MARGIN, notes_top + 6.0, SHEET_W - MARGIN, SHEET_H - MARGIN)
    scale, origins = _layout(vcs, cells, area, cfg, warnings, f"sheet {index}")
    name = f"{t.part_number} {t.part_name}".strip() or "PartRoute drawing"
    sheet = _Sheet(title=f"{name} — {title}" if pages > 1 else name, scale=scale)
    sheet.polyline([(MARGIN, MARGIN), (SHEET_W - MARGIN, MARGIN), (SHEET_W - MARGIN, SHEET_H - MARGIN), (MARGIN, SHEET_H - MARGIN)],
                   "BORDER", "border", closed=True)
    records: list[DimensionRecord] = []
    views = [_render_view(sheet, vc, origins[vc.name], scale, index, records) for vc in vcs]
    _notes_block(sheet, notes)
    _title_block(sheet, cfg, scale, key, index, pages)
    return DrawingPage(index=index, role=role, title=title, svg=sheet.svg_bytes(), dxf=sheet.dxf_bytes(), scale=scale,  # type: ignore[arg-type]
                       views=views, dimensions=records, hatches=sheet.hatch_count)


# --------------------------------------------------------------------------- #
# Manufacturing requirement coverage
# --------------------------------------------------------------------------- #

_GENERAL_TOL = re.compile(r"general|未注|一般公差|iso\s*2768|gb\s*/?\s*t\s*1804|it\s*\d{1,2}\b", re.I)
_FEATURE_TOL = re.compile(r"±|[+-]\s*0[.,]\d|\b[A-Za-z]{1,2}\d{1,2}\b|[⌀Øφ]\s*\d")
_GENERAL_RA = re.compile(r"其余|未注|全部|其他|\ball\b|general|remaining|unless", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").casefold()).strip()


def _req_dicts(requirements: list[Any] | None) -> list[dict[str, Any]]:
    out = []
    for r in requirements or []:
        if hasattr(r, "model_dump"):
            r = r.model_dump(mode="json")
        if isinstance(r, dict):
            out.append(r)
    return out


def _coverage(reqs: list[dict[str, Any]], cfg: PackageConfig) -> dict[str, list[dict[str, Any]]]:
    cov: dict[str, list[dict[str, Any]]] = {"covered": [], "uncovered": [], "unknown": []}
    material = cfg.title.material
    for r in reqs:
        rid = str(r.get("id") or "")
        kind = r.get("kind")
        if kind == "hole" and r.get("thread"):
            cov["uncovered"].append({"requirement_id": rid, "category": "thread", "text": str(r["thread"]),
                                     "reason": "thread callout not generated on the views"})
        if kind != "note":
            continue
        cat = str(r.get("category") or "general")
        text = str(r.get("text") or r.get("label") or "").strip()
        item: dict[str, Any] = {"requirement_id": rid, "category": cat, "text": text}
        if not text:
            cov["unknown"].append({**item, "reason": "note has no text"})
        elif cat == "material":
            if material and (_norm(text) in _norm(material) or _norm(material) in _norm(text)):
                cov["covered"].append({**item, "where": "title block MATERIAL"})
            elif not material:
                cov["covered"].append({**item, "where": "notes"})
            else:
                cov["uncovered"].append({**item, "reason": f"title block material '{material}' differs; listed in the notes"})
        elif cat in ("treatment", "general"):
            cov["covered"].append({**item, "where": "notes"})
        elif cat == "tolerance":
            if _GENERAL_TOL.search(text):
                cov["covered"].append({**item, "where": "notes (general tolerance)"})
            elif _FEATURE_TOL.search(text):
                cov["uncovered"].append({**item, "reason": "feature tolerance not attached to a dimension"})
            else:
                cov["unknown"].append({**item, "reason": "cannot tell whether the tolerance is general or feature-specific"})
        elif cat == "roughness":
            if _GENERAL_RA.search(text):
                cov["covered"].append({**item, "where": "notes (general surface finish)"})
            else:
                cov["uncovered"].append({**item, "reason": "surface finish symbol not drawn on the views"})
        elif cat == "thread":
            cov["uncovered"].append({**item, "reason": "thread callout not generated on the views"})
        else:
            cov["unknown"].append({**item, "reason": f"unknown note category '{cat}'"})
    return cov


def coil_note(lin: float, items: str = "") -> str:
    """Sheet note of a view drawn with the polygonal HLR (:mod:`app.cad.hlr_poly`)."""
    return (f"COIL GEOMETRY 螺旋件{items}: PROJECTED FROM A TESSELLATION (POLYGONAL HIDDEN-LINE REMOVAL, CHORD ≤ {lin:.3g} mm); "
            "OVERALL SIZES FROM THE EXACT MODEL.")


def _coverage_notes(cov: dict[str, list[dict[str, Any]]], supplied: bool) -> list[str]:
    if not supplied:
        return ["MANUFACTURING REQUIREMENTS: none supplied with the request (coverage unknown)."]
    out = []

    def fmt(item: dict[str, Any], extra: str) -> str:
        rid = item.get("requirement_id") or "-"
        return f"{rid} {item['category']}: {item['text']}" + (f" ({extra})" if extra else "")

    if cov["covered"]:
        out.append("REQUIREMENTS COVERED: " + "; ".join(fmt(i, i.get("where", "")) for i in cov["covered"]))
    if cov["uncovered"]:
        out.append("NOT SHOWN ON THE VIEWS (GAP): " + "; ".join(fmt(i, i.get("reason", "")) for i in cov["uncovered"]))
    if cov["unknown"]:
        out.append("UNKNOWN COVERAGE: " + "; ".join(fmt(i, i.get("reason", "")) for i in cov["unknown"]))
    if not out:
        out.append("MANUFACTURING REQUIREMENTS: none in the requirement list.")
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _lettering_notes(regions: list[Any]) -> list[str]:
    """One sheet note per text op: the letters are drawn from the model but never dimensioned as features."""
    out = []
    for r in regions:
        d = r.detail
        font = d.get("font") or {}
        used = font.get("used") or "?"
        sub = f" (REQUESTED {font.get('requested')}: SUBSTITUTED)" if font.get("substituted") else ""
        dep = float(d.get("depth") or 0.0)
        out.append(f"LETTERING {r.op_id} \"{r.text}\": {'RAISED' if dep > 0 else 'ENGRAVED'} {_fmt(abs(dep))}, CHARACTER HEIGHT "
                   f"{_fmt(float(d.get('height') or 0.0))}, FONT {used}{sub}; DRAWN FROM THE MODEL, NOT DIMENSIONED.")
    return out


def _resolve_source(source: BuildResult | ModelSpec, base_step: bytes | None) -> tuple[Any, list[HoleRecord], str, bool, list[Any]]:
    """(solid, hole records, build key, has an imported base, feature records)."""
    if isinstance(source, ModelSpec):
        result = build(source, base_step, export=False)
        has_base = source.base is not None
    elif isinstance(source, BuildResult):
        result = source
        has_base = any(f.op_id == "F-00" for f in result.features)
    else:
        raise TypeError("make_drawing expects a BuildResult or a ModelSpec")
    if not result.ok:
        msg = "; ".join(f"{e.op_id or '-'} {e.code}: {e.message}" for e in result.errors)
        raise DrawingError(f"cannot draw a failed build: {msg}")
    if result.shape is None:
        raise DrawingError("BuildResult has no in-memory solid (deserialised?); pass the ModelSpec instead")
    return result.shape, list(result.holes), result.build_key, has_base, list(result.features)


def _package_config(config: DrawingConfig | dict[str, Any] | None) -> PackageConfig:
    if config is None:
        return PackageConfig()
    if isinstance(config, PackageConfig):
        return config
    if isinstance(config, DrawingConfig):
        return PackageConfig.model_validate(config.model_dump())
    return PackageConfig.model_validate(config)


def make_drawing_package(
    source: BuildResult | ModelSpec,
    config: DrawingConfig | dict[str, Any] | None = None,
    *,
    base_step: bytes | None = None,
    requirements: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> DrawingPackage:
    """Generate the drawing package (overview page + section pages).

    ``config``: ``DrawingConfig``, :class:`PackageConfig` (adds ``section``)
    or a dict of either. ``requirements``: the frozen requirement list
    (design §1 JSON dicts or models); notes and hole threads become the
    coverage list printed on page 1. ``notes``: extra page-1 notes appended
    after the generated ones (assembly detail sheets: parent assembly, item,
    quantity, representation); ``None`` leaves the output unchanged. Raises
    :class:`DrawingError` for failed builds."""
    extra_notes = [str(s) for s in (notes or [])]
    cfg = _package_config(config)
    shape, holes, key, has_base, features = _resolve_source(source, base_step)
    warnings: list[Issue] = []
    lettering = text_regions(features)  # generation reliability M: letters are drawn, never analysed as features
    part = _analyse(shape, holes, key, has_base, lettering)
    reqs = _req_dicts(requirements)
    coverage = _coverage(reqs, cfg)

    plans = _plan_sections(part, cfg, warnings)
    sections: list[tuple[_SectionPlan, _VC]] = []
    for plan in plans:
        vc = _section_view(part, plan, cfg, warnings)
        if vc is not None:
            plan.letter = SECTION_LETTERS[len(sections)]
            vc.name, vc.label = f"section_{plan.letter}", f"SECTION {plan.letter}-{plan.letter}"
            sections.append((plan, vc))
    n_pages = 1 + len(sections)

    # ---- page 1: overview (Ø of features a section page already dimensions are not repeated)
    vcs, callouts = _overview_views(part, cfg, warnings, set().union(*(svc.dimmed for _, svc in sections)) if sections else set())
    size = part.size
    notes = [f"1. DIMENSIONS IN mm. OVERALL SIZE {' × '.join(_fmt(s) for s in size)}."]
    if callouts:
        notes.append("2. HOLES: " + "; ".join(callouts[:6]) + ("; …" if len(callouts) > 6 else ""))
    if any(d.kind == "position" for vc in vcs for d in vc.dims):
        notes.append(f"{len(notes) + 1}. HOLE POSITIONS FROM DATUM = PART MIN CORNER (MODEL X {_fmt(part.bmin.X)}, "
                     f"Y {_fmt(part.bmin.Y)}, Z {_fmt(part.bmin.Z)}).")
    if sections:
        notes.append(f"{len(notes) + 1}. " + "; ".join(f"SECTION {p.letter}-{p.letter} ON SHEET {i + 2}" for i, (p, _) in enumerate(sections)) + ".")
    notes += [f"{len(notes) + 1 + i}. {s}" for i, s in enumerate(_coverage_notes(coverage, requirements is not None))]
    if part.coil:
        notes.append(f"{len(notes) + 1}. {coil_note(part.lin)}")
    notes += [f"{len(notes) + 1 + i}. {s}" for i, s in enumerate(_lettering_notes(lettering))]
    notes += [f"{len(notes) + 1 + i}. {s}" for i, s in enumerate(extra_notes)]
    page1 = _compose_page(1, n_pages, "overview", "OVERVIEW", vcs, LAYOUTS[cfg.projection], cfg, notes, key, warnings)
    page1.callouts = callouts
    pages = [page1]

    # ---- section pages
    for i, (plan, svc) in enumerate(sections):
        vis, hid = part.projection("top", cfg.hidden_lines)
        top = _VC(name="top", label="TOP", kind="projection", vis=vis, hid=hid, bbox=_bbox2(_prim_points(vis + hid)))
        if plan.plane == "XZ":
            top.cut = {"orient": "h", "coord": plan.offset - part.center.Y, "dir": (0.0, 1.0), "letter": plan.letter}
            cells = {"top": (0, 0), svc.name: (1, 0)} if cfg.projection == "third" else {svc.name: (0, 0), "top": (1, 0)}
            vcs2 = [top, svc]
        else:
            top.cut = {"orient": "v", "coord": plan.offset - part.center.X, "dir": (-1.0, 0.0), "letter": plan.letter}
            fvis, fhid = part.projection("front", cfg.hidden_lines)
            front = _VC(name="front", label="FRONT", kind="projection", vis=fvis, hid=fhid, bbox=_bbox2(_prim_points(fvis + fhid)))
            if cfg.projection == "third":
                cells = {"top": (0, 0), "front": (1, 0), svc.name: (1, 1)}
            else:
                cells = {svc.name: (0, 0), "front": (0, 1), "top": (1, 1)}
            vcs2 = [top, front, svc]
        axis_name, sight = ("Y", "+Y") if plan.plane == "XZ" else ("X", "-X")
        kept = f"y >= {_fmt(plan.offset)}" if plan.plane == "XZ" else f"x <= {_fmt(plan.offset)}"
        notes2 = [
            f"1. SECTION {plan.letter}-{plan.letter}: CUTTING PLANE {plan.plane} AT {axis_name} = {_fmt(plan.offset)} (MODEL mm), "
            f"VIEWED TOWARDS {sight} (ARROWS ON THE TOP VIEW); MATERIAL ON THE PLANE HATCHED (ANSI31).",
            "2. HIDDEN EDGES ARE NOT SHOWN IN THE SECTION. LEVELS FROM DATUM = PART BOTTOM "
            f"(MODEL Z {_fmt(part.bmin.Z)}); DEPTHS FROM THE LOCAL ENTRY FACE.",
        ]
        if plan.features:
            notes2.append("3. SHOWN IN THIS SECTION: " + "; ".join(plan.features) + ".")
        page = _compose_page(i + 2, n_pages, "section", f"SECTION {plan.letter}-{plan.letter}", vcs2, cells, cfg, notes2, key, warnings)
        page.section = SectionInfo(
            letter=plan.letter, plane=plan.plane, offset=round(plan.offset, 6),  # type: ignore[arg-type]
            view_direction=[0.0, 1.0, 0.0] if plan.plane == "XZ" else [-1.0, 0.0, 0.0], kept=kept, features=plan.features,
        )
        pages.append(page)

    sheet_info = {"size": "A3", "width_mm": SHEET_W, "height_mm": SHEET_H, "projection": cfg.projection, "units": "mm"}
    generator = {"drawing_version": DRAWING_VERSION, "dxf_version": "R2010", "ezdxf": ezdxf.__version__,
                 "text_style": dict(TEXT_STYLE), "dimstyle": DIMSTYLE}
    if part.coil:
        generator["hlr"] = {"method": "polygonal", "reason": "helical faces", "linear_deflection_mm": part.lin,
                            "angular_deflection_rad": hlr_poly.ANG_DEFL}
    return DrawingPackage(pages=pages, coverage=coverage, warnings=warnings, build_key=key, generator=generator, sheet=sheet_info)


def make_drawing(
    source: BuildResult | ModelSpec,
    config: DrawingConfig | None = None,
    *,
    base_step: bytes | None = None,
) -> DrawingOutput:
    """Page 1 (overview sheet, SVG + DXF) of :func:`make_drawing_package`.

    ``source``: a successful ``BuildResult`` from ``kernel.build`` in this
    process (uses ``result.shape``), or a ``ModelSpec`` (rebuilt here; give
    ``base_step`` when the spec has a STEP base). Raises :class:`DrawingError`
    for failed builds. ``views[].bbox`` is in SVG sheet mm (y down).
    """
    pkg = make_drawing_package(source, config, base_step=base_step)
    page = pkg.pages[0]
    views = [ViewInfo(name=v.name, scale=v.scale, bbox=v.bbox) for v in page.views if v.name in VIEW_LABELS]  # type: ignore[arg-type]
    sheet = {**pkg.sheet, "page": 1, "pages": len(pkg.pages), "text_style": TEXT_STYLE["name"], "font": TEXT_STYLE["font_file"]}
    return DrawingOutput(svg=page.svg, dxf=page.dxf, sheet=sheet, scale=page.scale, views=views, callouts=page.callouts,
                         warnings=pkg.warnings)
