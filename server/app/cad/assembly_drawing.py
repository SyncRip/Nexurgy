"""Assembly drawing packages (W5): general-arrangement sheets and detail sheets.

Pure CAD: consumes an :class:`~app.cad.assembly_model.AssemblyGeometry` plus the BOM lines of the same exact
assembly revision (W0 contract §4) and returns an :class:`AssemblyDrawingPackage` (no database, no storage).

General arrangement (document ``GA``), A3 landscape, one sheet (plus BOM continuation sheets when the BOM has
more than :data:`MAX_GA_BOM_ROWS` rows):

- projected views (``config.views``: front / top / right, iso optional) from one hidden-line removal of all
  placed components together. Solids are never fused: every occurrence is added to the HLR algorithm on its
  own and its visible / hidden edges are read back per occurrence, so component boundaries stay and the
  attribution (SVG ``<g class="component">``, page metadata) is exact. Centre lines on every round axis.
  Coil geometry (springs, split lock washers), whose exact HLR takes about a minute per view, and the components
  overlapping it in a view are drawn from one polygonal HLR of a mesh instead (:func:`_views_hlr`,
  :mod:`app.cad.hlr_poly`), stated in the sheet notes and in the manifest's ``hlr``.
- one real section through the assembly (``config.section``: ``auto`` | ``none`` | ``{plane, offset}``). Each
  component crossing the plane is cut on its own and its cut faces are hatched separately (DXF HATCH ANSI31
  on layer HATCH; adjacent components get different angle / spacing, see :data:`HATCH_STYLES`). Components the
  convention leaves unsectioned are drawn whole and recorded with their rule (:data:`SECTION_RULES`):
  standard parts, solid shafts / pins cut along their axis, configured or definition-level exceptions.
  The plane, the kept side and the direction of sight are stored in the page metadata; the cutting line with
  arrows and letters is drawn on a projection.
- dimensions as native DIMENSION entities: the overall envelope (always), the pitch between repeated
  instances of one definition (``pitch_dimensions``) and configured key / interface dimensions between datums
  or surface points of two instances (``key_dimensions``).
- balloons (item numbers): one BLOCK per BOM item (circle + item number) inserted with INSERT on layer BALLOON, a leader
  to a visible point of the item (inside its hatch in the section, else on its longest visible edge). Items
  map one-to-one to the rows of the BOM table drawn above the title block; a shared definition is one
  item with its quantity (``balloon_per_instance`` balloons every occurrence with the same item number).
- title block (assembly name / number / revision label), material and technical notes.

Detail documents (``D<item>``): one per unique leaf definition whose procurement is in
``detail_procurements`` (default make + unknown; an unknown one is recorded as a rule assumption). They reuse
:func:`app.cad.drawing.make_drawing_package` unchanged (overview + section sheets) with a note naming the
parent assembly, item, quantity and representation. Buy / standard parts appear in the BOM only; quantities
always come from the instance tree.

Every page is written as DXF + SVG and, per page, as native DWG R2000 through :func:`app.cad.dwg.write_dwg`.
A page or format that fails keeps the other pages and makes the package ``partial`` (a DXF or a preview is
never substituted for a failed DWG). Business checks go through :func:`app.policy.run_check`:
``drawing.annotation`` (the existing annotation check per page + the assembly check: balloons ↔ BOM,
hatch per cut component, envelope against an independent bounding box) and ``drawing.dwg_semantic`` (the
DWG readback). Turbo skips both (``skipped_turbo``) but still converts every page and fails closed on empty or
failed files.

Layout / annotation edits are a separate overlay (:func:`apply_layout_overlay`): move dimension text, move a
balloon, add a note. It produces a new package version from the previous one without touching geometry;
engineering edits (dimension values, quantities, items, materials, geometry) raise
:class:`EngineeringChangeRequired`.

Identical inputs give identical bytes and hashes (sorted occurrences, fixed DXF metadata, canonical JSON).
"""

from __future__ import annotations

import collections
import hashlib
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Literal

import build123d as bd
from OCP.BRepAdaptor import BRepAdaptor_Surface
import ezdxf
from ezdxf import const as dxfconst
from ezdxf import recover
from ezdxf.enums import TextEntityAlignment
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepLib import BRepLib
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import policy as exec_policy
from ..domain.hashing import canonical_json, sha256_hex
from . import drawing as dr
from . import hlr_poly
from .annotation_check import AnnotationItem, check_package
from .assembly_model import AssemblyGeometry, BomLine, Occurrence, PartDefinition, apply, apply_dir, is_rigid
from .drawing import DrawingError, PackageConfig, make_drawing_package
from .dwg import DwgError, write_dwg
from .dxf_write import fixed_metadata
from .kernel import KERNEL_VERSION, export_step_bytes
from .results import BuildResult, FeatureRecord, HoleRecord, Issue

__all__ = [
    "ASSEMBLY_DRAWING_VERSION",
    "ENGINEERING_OPS",
    "LAYOUT_OPS",
    "MANIFEST_SCHEMA",
    "SECTION_RULES",
    "AssemblyDrawingConfig",
    "AssemblyDrawingError",
    "AssemblyDrawingPackage",
    "AssemblyDrawingPage",
    "EngineeringChangeRequired",
    "KeyDimension",
    "LayoutOverlay",
    "OverlayError",
    "PointRef",
    "SectionSpec",
    "apply_layout_overlay",
    "generator_identity",
    "make_assembly_drawing_package",
    "restore_package",
    "validate_overlay",
]

ASSEMBLY_DRAWING_VERSION = "partroute-assembly-drawing/1"
MANIFEST_SCHEMA = "partroute.assembly_drawing_package/1"
ASM_CHECK_SCHEMA = "partroute.assembly_annotation_check/1"
DWG_TARGET = "AC1015"
FORMATS = ("dxf", "svg", "dwg")
CONTENT_TYPES = {"svg": "image/svg+xml", "dxf": "image/vnd.dxf", "dwg": "image/vnd.dwg"}
ANNOTATION_REPORT_PATH = "checks/annotation.json"

# W0 contract §7: drawing scope limits of one assembly revision.
MAX_OCCURRENCES = 2000
MAX_DEFINITIONS = 500
PAGE_BUDGET_S = 120.0

SHEET_W, SHEET_H, MARGIN = dr.SHEET_W, dr.SHEET_H, dr.MARGIN
TITLE_W, TITLE_H = dr.TITLE_W, dr.TITLE_H

BALLOON_R = 4.5  # paper mm
BALLOON_H = 3.5  # item number cap height
BALLOON_GAP = 6.0  # free paper between a view (and its dimensions) and the balloon column
BALLOON_SPACING = 2 * BALLOON_R + 1.5
DOT = 0.45  # half size of the leader end dot

BOM_ROW_H = 7.0
BOM_TEXT_H = 2.5
BOM_HEAD_H = 2.0
#: BOM table columns (header, width mm); the widths add up to the title block width.
BOM_COLUMNS: tuple[tuple[str, str, float], ...] = (
    ("item", "ITEM 序号", 17.0), ("number", "NUMBER 代号", 33.0), ("name", "NAME 名称", 46.0),
    ("qty", "QTY 数量", 14.0), ("material", "MATERIAL 材料", 32.0), ("remark", "REMARK 备注", 38.0),
)
#: The BOM table of a partial assembly (generation supplement B-03): included and target quantity side by side; a missing
#: row keeps its target item number, says why it is missing and gets no balloon.
PARTIAL_BOM_COLUMNS: tuple[tuple[str, str, float], ...] = (
    ("item", "ITEM 序号", 17.0), ("number", "NUMBER 代号", 28.0), ("name", "NAME 名称", 37.0),
    ("qty", "QTY 纳入", 14.0), ("target", "TARGET 目标", 20.0), ("material", "MATERIAL 材料", 27.0), ("remark", "REMARK 备注", 37.0),
)
PARTIAL_KIND = "PARTIAL ASSEMBLY 部分装配"
PARTIAL_TITLE = "部分装配 PARTIAL"  # the title block's kind cell (fits its 60 mm)
MAX_PARTIAL_NOTE_ITEMS = 6
MAX_GA_BOM_ROWS = 10
MAX_BOM_SHEET_ROWS = 30

#: Section hatch styles (ANSI31 angle offset, pattern scale). Each cut component gets its definition's
#: preferred style unless an adjacent cut component already has it (then the next free one).
HATCH_STYLES: tuple[tuple[float, float], ...] = ((0.0, 0.75), (90.0, 0.75), (0.0, 0.45), (90.0, 0.45), (0.0, 1.2),
                                                 (90.0, 1.2))
ADJACENT_TOL = 0.05  # model mm between cut faces of two components that touch
MAX_PITCH_INSTANCES = 4
POINT_ON_PART_TOL = 1e-3

#: Components drawn whole although the cutting plane crosses them, and why (recorded per component).
SECTION_RULES: dict[str, str] = {
    "config_not_sectioned": "listed in config.not_sectioned",
    "definition_not_sectioned": "the part definition sets meta.section = false",
    "standard_part_along_axis": "standard part cut along its axis: drawn unsectioned (GB/T 4458.6, ISO 128-44)",
    "standard_part_not_sectioned": "standard part without a single round axis: drawn unsectioned",
    "solid_shaft_along_axis": "solid body of revolution (shaft, pin) cut along its axis: drawn unsectioned",
}
#: Sheet wording of the rules (notes block).
SECTION_RULE_LABELS: dict[str, str] = {
    "config_not_sectioned": "EXCLUDED BY THE DRAWING CONFIGURATION",
    "definition_not_sectioned": "EXCLUDED BY ITS PART DEFINITION",
    "standard_part_along_axis": "STANDARD PART CUT ALONG ITS AXIS",
    "standard_part_not_sectioned": "STANDARD PART",
    "solid_shaft_along_axis": "SOLID SHAFT / PIN CUT ALONG ITS AXIS",
}

#: Overlay operations that only move or add annotation.
LAYOUT_OPS = ("move_dimension_text", "move_balloon", "add_note")
#: Operations that would change an engineering value; they need a model / assembly change instead.
ENGINEERING_OPS = ("set_dimension_value", "override_dimension_text", "edit_dimension", "delete_dimension",
                   "change_quantity", "change_item", "renumber_balloon", "change_material", "edit_bom",
                   "move_component", "change_geometry", "change_section", "edit_title")

Procurement = Literal["make", "buy", "standard", "unknown"]
ViewName = Literal["front", "top", "right", "iso"]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AssemblyDrawingError(ValueError):
    """The inputs cannot be drawn (structure, BOM ↔ instance tree, configuration). ``code`` is stable."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class EngineeringChangeRequired(AssemblyDrawingError):
    """A layout overlay tried to change an engineering value (dimension value, quantity, item, material,
    geometry). The service maps it to "use a model / assembly change"; nothing is generated."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("engineering_change_required", message, details)


class OverlayError(AssemblyDrawingError):
    """A layout overlay is malformed or names a page / dimension / balloon the package does not have."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("invalid_layout_overlay", message, details)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class SectionSpec(BaseModel):
    """Explicit cutting plane (model mm, world frame): ``XZ`` at ``y = offset`` viewed towards +Y (kept: y ≥
    offset) or ``YZ`` at ``x = offset`` viewed towards -X (kept: x ≤ offset), as in single-part drawings."""

    model_config = ConfigDict(extra="forbid")

    plane: Literal["XZ", "YZ"]
    offset: float


class PointRef(BaseModel):
    """A point of one placed instance. ``path``: the occurrence's instance-id path. Either ``datum``
    (``origin`` = the definition frame origin, ``bbox.min`` / ``bbox.max`` / ``bbox.center`` of the definition,
    or a named datum listed in ``PartDefinition.meta["datums"]``) or ``point`` (definition-frame mm; must lie on
    the part's surface, checked)."""

    model_config = ConfigDict(extra="forbid")

    path: list[str] = Field(min_length=1)
    datum: str | None = "origin"
    point: list[float] | None = None

    @field_validator("point")
    @classmethod
    def _three(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 3:
            raise ValueError("point must have 3 coordinates")
        return v


class KeyDimension(BaseModel):
    """An interface / mounting dimension: the distance along world ``axis`` between two instance points."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=40)
    a: PointRef
    b: PointRef
    axis: Literal["x", "y", "z"]
    view: Literal["front", "top", "right", "section"] | None = None
    label: str = ""


class AssemblyTitle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str = ""
    date: str = ""
    project: str = ""
    drawing_number: str | None = None  # default: the assembly number


class AssemblyDrawingConfig(BaseModel):
    """Everything that shapes the package. Frozen with the package (its hash is in the manifest)."""

    model_config = ConfigDict(extra="forbid")

    views: list[ViewName] = Field(default_factory=lambda: ["front", "top", "right"], min_length=1)
    projection: Literal["third", "first"] = "third"
    scale: Literal["auto"] | float = "auto"
    hidden_lines: bool = True
    section: Literal["auto", "none"] | SectionSpec = "auto"
    not_sectioned: list[str] = Field(default_factory=list)  # definition keys always drawn whole in the section
    always_sectioned: list[str] = Field(default_factory=list)  # definition keys always cut (overrides the rules)
    bom_view: Literal["flattened", "hierarchical"] = "flattened"
    balloon_per_instance: bool = False
    pitch_dimensions: bool = True
    key_dimensions: list[KeyDimension] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # technical requirements printed on the GA sheet
    details: bool = True
    detail_procurements: list[Procurement] = Field(default_factory=lambda: ["make", "unknown"])
    detail: dict[str, Any] = Field(default_factory=dict)  # PackageConfig fields of the detail sheets (no title)
    formats: list[Literal["dxf", "svg", "dwg"]] = Field(default_factory=lambda: list(FORMATS))
    dwg_timeout_s: float = Field(default=120.0, gt=0)
    title: AssemblyTitle = Field(default_factory=AssemblyTitle)

    @field_validator("formats")
    @classmethod
    def _dxf_required(cls, v: list[str]) -> list[str]:
        if "dxf" not in v:
            raise ValueError("formats must include dxf (the native source of every page)")
        return [f for f in FORMATS if f in v]

    @field_validator("detail")
    @classmethod
    def _detail_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        if "title" in v:
            raise ValueError("detail sheets take their title block from the part definition")
        PackageConfig.model_validate(v)
        return v


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class PageFile(BaseModel):
    format: Literal["dxf", "svg", "dwg"]
    path: str
    sha256: str
    bytes: int
    content_type: str
    version: str | None = None
    data: bytes = Field(default=b"", exclude=True, repr=False)


class AsmDimension(BaseModel):
    """One DIMENSION of a GA sheet (same shape as the single-part ``DimensionRecord`` where they overlap).
    ``p1`` / ``p2`` are sheet mm, y down (like the SVG); ``value`` is the model length; ``ref`` names what was
    measured (envelope size, instance paths, datums)."""

    id: str
    page: int
    view: str
    kind: Literal["overall", "pitch", "key"]
    axis: Literal["x", "y", "z"] | None = None
    value: float
    text: str
    measured: str
    feature: str | None = None
    handle: str = ""
    p1: list[float]
    p2: list[float]
    ref: dict[str, Any] = Field(default_factory=dict)
    text_at: list[float] | None = None  # sheet mm, y up: set when a layout overlay moved the text


class BalloonRecord(BaseModel):
    """One balloon. ``anchor`` / ``center``: sheet mm, y up (DXF)."""

    id: str
    item: str
    bom: Literal["flattened", "hierarchical"]
    level: int
    def_key: str
    def_kind: str
    quantity: int
    view: str
    occurrence: list[str]
    anchor: list[float]
    center: list[float]
    radius: float
    block: str
    handle: str
    leader_handle: str
    dot_handle: str
    on_hidden: bool = False


class HatchRecord(BaseModel):
    view: str
    occurrence: list[str]
    def_key: str
    item: str | None
    pattern: str = "ANSI31"
    angle: float
    scale: float
    style: int
    loops: int
    handles: list[str]
    adjacent: list[str] = Field(default_factory=list)  # "/"-joined paths of touching cut components


class SectionRecord(BaseModel):
    letter: str
    plane: Literal["XZ", "YZ"]
    offset: float
    chosen: Literal["auto", "config"]
    view: str  # the section view's name
    base_view: Literal["front", "right"]
    view_direction: list[float]  # direction of sight (towards the kept material)
    kept: str
    cut_on: str | None  # projection carrying the cutting line
    cut: list[dict[str, Any]] = Field(default_factory=list)
    not_sectioned: list[dict[str, Any]] = Field(default_factory=list)
    whole: list[list[str]] = Field(default_factory=list)  # entirely on the kept side (not crossed)
    omitted: list[list[str]] = Field(default_factory=list)  # entirely on the removed side
    rules: dict[str, str] = Field(default_factory=dict)


class BomRowRecord(BaseModel):
    item: str
    level: int
    def_key: str
    def_kind: str
    name: str
    number: str | None
    material: str | None
    procurement: str
    representation: str
    quantity: int
    total_quantity: int
    shown_quantity: int
    cells: dict[str, dict[str, str]] = Field(default_factory=dict)  # column → {text, handle}
    # partial assembly (generation supplement B-03): the target quantity and the row's state (partial / missing);
    # a missing row is not a BOM line of the revision (no balloon, not in the assembly checks)
    target_quantity: int | None = None
    status: Literal["partial", "missing"] | None = None


class AssemblyDrawingPage(BaseModel):
    """One sheet. ``dxf`` / ``svg`` / ``dwg`` give the file bytes (empty when the format is missing)."""

    index: int
    key: str
    document: str
    role: Literal["assembly", "bom", "detail"]
    title: str
    sheet: int
    sheets: int
    def_key: str | None = None
    item: str | None = None
    scale: float | None = None
    views: list[dr.PageView] = Field(default_factory=list)
    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    balloons: list[BalloonRecord] = Field(default_factory=list)
    hatches: list[HatchRecord] = Field(default_factory=list)
    hatch_count: int = 0
    section: dict[str, Any] | None = None
    bom_rows: list[BomRowRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    callouts: list[str] = Field(default_factory=list)
    files: list[PageFile] = Field(default_factory=list)
    status: Literal["complete", "partial", "failed"] = "complete"
    errors: list[dict[str, Any]] = Field(default_factory=list)
    dwg_report: dict[str, Any] | None = None
    overlay: list[dict[str, Any]] = Field(default_factory=list)

    def file(self, fmt: str) -> PageFile | None:
        return next((f for f in self.files if f.format == fmt), None)

    def _bytes(self, fmt: str) -> bytes:
        f = self.file(fmt)
        return f.data if f is not None else b""

    @property
    def dxf(self) -> bytes:
        return self._bytes("dxf")

    @property
    def svg(self) -> bytes:
        return self._bytes("svg")

    @property
    def dwg(self) -> bytes:
        return self._bytes("dwg")


class AssemblyDrawingPackage(BaseModel):
    """The package. ``manifest`` is canonical JSON (its sha256 is ``manifest_sha256``); ``reports`` holds the
    check report files listed in the manifest (path → bytes)."""

    version: int
    execution_policy: Literal["standard", "turbo"]
    completeness: Literal["complete", "partial"]
    pages: list[AssemblyDrawingPage]
    documents: list[dict[str, Any]]
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any]
    manifest_sha256: str
    reports: dict[str, bytes] = Field(default_factory=dict, exclude=True, repr=False)
    # S3: what this generation did per document (generated | reused | failed) and per page (generated | reused |
    # dwg_converted | failed); not part of the manifest (a continued package hashes like a fresh one).
    progress: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)

    def page(self, key: str) -> AssemblyDrawingPage:
        for p in self.pages:
            if p.key == key:
                return p
        raise KeyError(key)

    def files(self) -> list[tuple[str, bytes]]:
        """Every page file and report as (path, bytes), in manifest order."""
        out = [(f.path, f.data) for p in self.pages for f in p.files]
        out += sorted(self.reports.items())
        return out


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _item_key(item: str) -> tuple:
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", item))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"


def _r(v: float, nd: int = 4) -> float:
    return round(float(v), nd) + 0.0


def _pt(p: tuple[float, float]) -> list[float]:
    return [_r(p[0]), _r(p[1])]


def _fit(text: str, width: float, h: float) -> str:
    """``text`` cut (with …) so it fits ``width`` paper mm at cap height ``h`` and one DWG TEXT value."""
    text = " ".join(str(text or "").split())
    if dr._text_width(text, h) <= width and dr._text_units(text) <= dr.MAX_TEXT_UNITS:
        return text
    while text and (dr._text_width(text + "…", h) > width or dr._text_units(text + "…") > dr.MAX_TEXT_UNITS):
        text = text[:-1]
    return text + "…" if text else ""


def _bom_line(x: Any) -> BomLine:
    if isinstance(x, BomLine):
        return x
    if not isinstance(x, dict):
        raise AssemblyDrawingError("bom_invalid", f"BOM line must be a BomLine or a dict, not {type(x).__name__}")
    d = dict(x)
    d["instance_paths"] = tuple(tuple(str(s) for s in p) for p in (d.get("instance_paths") or ()))
    try:
        return BomLine(**{k: d[k] for k in BomLine.__dataclass_fields__ if k in d})
    except TypeError as exc:
        raise AssemblyDrawingError("bom_invalid", f"BOM line is incomplete: {exc}") from exc


def _solid(shape: Any) -> Any:
    if shape is None:
        return None
    if not isinstance(shape, bd.Shape):
        shape = bd.Shape.cast(shape)
    solids = shape.solids() if hasattr(shape, "solids") else []
    return solids[0] if len(solids) == 1 else shape


def _bbox(shape: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Tight bounding box (BRepBndLib optimal: no triangulation, no tolerance enlargement; tightened on trimmed
    free-form faces such as ground coil ends, :func:`app.cad.bounds.tight_bounds`)."""
    from .bounds import tight_bounds

    return tight_bounds(shape)


def _trsf(m: Any) -> gp_Trsf:
    t = gp_Trsf()
    t.SetValues(*(float(v) for v in m[0]), *(float(v) for v in m[1]), *(float(v) for v in m[2]))
    return t


def _poly_area(lp: list[tuple[float, float]]) -> float:
    return abs(sum(lp[i][0] * lp[(i + 1) % len(lp)][1] - lp[(i + 1) % len(lp)][0] * lp[i][1] for i in range(len(lp)))) / 2.0


def _seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln2 = dx * dx + dy * dy
    t = 0.0 if ln2 < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ln2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _prim_len(pr: dr._Prim) -> float:
    return sum(math.dist(pr.pts[i], pr.pts[i + 1]) for i in range(len(pr.pts) - 1))


def _prim_mid(pr: dr._Prim) -> tuple[float, float]:
    if pr.kind == "circle" and pr.center:
        a = math.radians(45.0)
        return (pr.center[0] + pr.radius * math.cos(a), pr.center[1] + pr.radius * math.sin(a))
    half = _prim_len(pr) / 2.0
    for i in range(len(pr.pts) - 1):
        seg = math.dist(pr.pts[i], pr.pts[i + 1])
        if seg >= half and seg > 0:
            t = half / seg
            return (pr.pts[i][0] + t * (pr.pts[i + 1][0] - pr.pts[i][0]), pr.pts[i][1] + t * (pr.pts[i + 1][1] - pr.pts[i][1]))
        half -= seg
    return pr.pts[len(pr.pts) // 2]


def _interior_point(loops: list[list[tuple[float, float]]]) -> tuple[tuple[float, float], float]:
    """A point inside a hatched region (outer loop minus holes): the middle of the widest even-odd span on a
    few scan lines. Returns (point, span width)."""
    pts = [p for lp in loops for p in lp]
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    best: tuple[tuple[float, float], float] = (pts[0], 0.0)
    for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
        y = y0 + frac * (y1 - y0)
        xs: list[float] = []
        for lp in loops:
            for i in range(len(lp)):
                (ax, ay), (bx, by) = lp[i], lp[(i + 1) % len(lp)]
                if (ay <= y < by) or (by <= y < ay):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            w = xs[k + 1] - xs[k]
            if w > best[1] + 1e-9:
                best = (((xs[k] + xs[k + 1]) / 2.0, y), w)
    return best


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass
class _Occ:
    index: int
    occ: Occurrence
    part: PartDefinition
    shape: Any  # world-placed shape
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.occ.path)

    @property
    def path_s(self) -> str:
        return "/".join(self.occ.path)


@dataclass
class _DefInfo:
    solid: Any
    bores: list[Any]
    bosses: list[Any]
    revolution: tuple[bd.Vector, bd.Vector, bool] | None  # (point, direction, solid without bores) in the def frame
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]
    hollow: bool  # has bores or clearly less volume than its box (cavity, pockets)
    coil: bool = False  # coil geometry (spring, split lock washer): polygonal HLR, see :func:`_views_hlr`


def _revolution_axis(shape: Any, bores: list[Any], bosses: list[Any]) -> tuple[bd.Vector, bd.Vector, bool] | None:
    """The common axis when the part is a body of revolution (every cylinder / cone on one axis line, every
    plane perpendicular to it), with a flag for a solid body (no bore)."""
    stacks = list(bosses) + list(bores)
    if not bosses:
        return None
    d0, o0 = bosses[0].d.normalized(), bosses[0].o

    def on_axis(d: bd.Vector, o: bd.Vector) -> bool:
        if d.normalized().cross(d0).length > 1e-6:
            return False
        rel = o - o0
        return (rel - d0 * rel.dot(d0)).length < 1e-4

    if not all(on_axis(st.d, st.o) for st in stacks):
        return None
    for face in shape.faces():
        gt = face.geom_type
        if gt == bd.GeomType.PLANE:
            if abs(abs(face.normal_at(face.center()).dot(d0)) - 1.0) > 1e-6:
                return None
        elif gt in (bd.GeomType.CYLINDER, bd.GeomType.CONE):
            adaptor = BRepAdaptor_Surface(face.wrapped)
            surf = adaptor.Cylinder() if gt == bd.GeomType.CYLINDER else adaptor.Cone()
            ax = surf.Axis()
            d = bd.Vector(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
            o = bd.Vector(ax.Location().X(), ax.Location().Y(), ax.Location().Z())
            if not on_axis(d, o):
                return None
        else:
            return None
    return (o0, d0, not bores)


def _def_info(part: PartDefinition) -> _DefInfo:
    solid = _solid(part.shape)
    bores, bosses = dr._find_stacks(solid)
    lo, hi = _bbox(solid)
    box_vol = max(1e-12, (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]))
    try:
        vol = float(solid.volume)
    except Exception:  # noqa: BLE001 - a shape without volume is drawn but never "hollow"
        vol = box_vol
    rev = _revolution_axis(solid, bores, bosses)
    hollow = bool(bores) or (rev is None and vol < 0.95 * box_vol)
    return _DefInfo(solid=solid, bores=bores, bosses=bosses, revolution=rev, lo=lo, hi=hi, hollow=hollow,
                    coil=hlr_poly.is_coil(solid, part.meta))


@dataclass
class _Ctx:
    geom: AssemblyGeometry
    cfg: AssemblyDrawingConfig
    occs: list[_Occ]
    defs: dict[str, _DefInfo]
    lines: dict[str, list[BomLine]]
    rows: list[BomLine]
    items: dict[str, str]  # definition key → flattened item
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]
    warnings: list[Issue]
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    # a partial assembly revision: {"coverage", "target_bom", "ai_decisions"} (services pass it in ``upstream.partial``)
    partial: dict[str, Any] | None = None
    meshes: dict[str, Any] = field(default_factory=dict)  # definition key → triangulated copy (polygonal HLR)
    poly_items: set[str] = field(default_factory=set)  # definition keys drawn with the polygonal HLR in some view

    @property
    def coil_defs(self) -> list[str]:
        return sorted(k for k, d in self.defs.items() if d.coil)

    @property
    def lin(self) -> float:
        return hlr_poly.deflection(self.diag)[0]

    @property
    def center(self) -> bd.Vector:
        return bd.Vector(*((a + b) / 2.0 for a, b in zip(self.lo, self.hi)))

    @property
    def diag(self) -> float:
        return math.dist(self.lo, self.hi)

    @property
    def size(self) -> dict[str, float]:
        return {a: self.hi[i] - self.lo[i] for i, a in enumerate("xyz")}

    def by_path(self, path: tuple[str, ...]) -> _Occ | None:
        return next((o for o in self.occs if o.path == path), None)

    def targets(self, line: BomLine) -> list[_Occ]:
        if line.instance_paths:
            return [o for o in self.occs if any(o.path[:len(p)] == tuple(p) for p in line.instance_paths)]
        return [o for o in self.occs if o.part.key == line.def_key]


def _config(config: AssemblyDrawingConfig | dict[str, Any] | None) -> AssemblyDrawingConfig:
    if config is None:
        return AssemblyDrawingConfig()
    if isinstance(config, AssemblyDrawingConfig):
        return config
    try:
        return AssemblyDrawingConfig.model_validate(config)
    except Exception as exc:  # pydantic ValidationError
        raise AssemblyDrawingError("invalid_drawing_config", str(exc)[:500]) from exc


def _lines(bom: Any) -> dict[str, list[BomLine]]:
    if not isinstance(bom, dict):
        raise AssemblyDrawingError("bom_invalid", "bom must be {'hierarchical': [...], 'flattened': [...]}")
    return {k: [_bom_line(x) for x in (bom.get(k) or [])] for k in ("hierarchical", "flattened")}


def _context(geom: AssemblyGeometry, lines: dict[str, list[BomLine]], cfg: AssemblyDrawingConfig) -> _Ctx:
    """Validates the structure (always, under every policy) and places every occurrence."""
    if not geom.occurrences:
        raise AssemblyDrawingError("assembly_empty", "the assembly revision has no placed occurrence")
    if len(geom.occurrences) > MAX_OCCURRENCES or len(geom.parts) > MAX_DEFINITIONS:
        raise AssemblyDrawingError("assembly_too_large", "the assembly exceeds the drawing limits",
                                   {"occurrences": len(geom.occurrences), "definitions": len(geom.parts),
                                    "limits": {"occurrences": MAX_OCCURRENCES, "definitions": MAX_DEFINITIONS}})
    defs: dict[str, _DefInfo] = {}
    occs: list[_Occ] = []
    seen: set[tuple[str, ...]] = set()
    for occ in sorted(geom.occurrences, key=lambda o: tuple(o.path)):
        path = tuple(occ.path)
        if path in seen:
            raise AssemblyDrawingError("duplicate_occurrence", f"occurrence path {'/'.join(path)} appears twice")
        seen.add(path)
        part = geom.parts.get(occ.part_key)
        if part is None:
            raise AssemblyDrawingError("unknown_part_definition", f"occurrence {'/'.join(path)} names definition "
                                       f"{occ.part_key!r}, which the assembly does not define")
        if part.shape is None:
            raise AssemblyDrawingError("part_shape_missing", f"definition {part.key} has no shape to draw")
        if not is_rigid(occ.world):
            raise AssemblyDrawingError("non_rigid_transform", f"occurrence {'/'.join(path)} has a non-rigid transform "
                                       "(a mirrored copy must be its own definition)")
        if part.key not in defs:
            defs[part.key] = _def_info(part)
        shape = defs[part.key].solid.moved(bd.Location(_trsf(occ.world)))
        lo, hi = _bbox(shape)
        occs.append(_Occ(len(occs), occ, part, shape, lo, hi))
    lo = tuple(min(o.lo[i] for o in occs) for i in range(3))
    hi = tuple(max(o.hi[i] for o in occs) for i in range(3))

    flat = lines["flattened"]
    if not flat:
        raise AssemblyDrawingError("bom_missing", "the flattened BOM is empty")
    counts = collections.Counter(o.part.key for o in occs)
    items: dict[str, str] = {}
    for ln in flat:
        if ln.def_kind != "part" or ln.def_key not in geom.parts:
            raise AssemblyDrawingError("bom_unknown_definition", f"flattened BOM item {ln.item} names {ln.def_key!r}, "
                                       "which is not a leaf part definition of this assembly")
        if ln.total_quantity != counts.get(ln.def_key, 0):
            raise AssemblyDrawingError("bom_quantity_mismatch", f"BOM item {ln.item} ({ln.def_key}) states "
                                       f"{ln.total_quantity}, the instance tree places {counts.get(ln.def_key, 0)}",
                                       {"item": ln.item, "def_key": ln.def_key, "bom": ln.total_quantity,
                                        "instances": counts.get(ln.def_key, 0)})
        if ln.def_key in items:
            raise AssemblyDrawingError("bom_duplicate_definition", f"definition {ln.def_key} has two flattened lines")
        items[ln.def_key] = ln.item
    missing = sorted(set(counts) - set(items))
    if missing:
        raise AssemblyDrawingError("bom_missing_definition", "placed definitions without a BOM line: " + ", ".join(missing),
                                   {"definitions": missing})
    rows = lines[cfg.bom_view]
    if not rows:
        raise AssemblyDrawingError("bom_missing", f"the {cfg.bom_view} BOM is empty")
    dup = [k for k, n in collections.Counter(r.item for r in rows).items() if n > 1]
    if dup:
        raise AssemblyDrawingError("bom_items_not_unique", f"BOM items repeat: {', '.join(sorted(dup, key=_item_key))}")
    ctx = _Ctx(geom=geom, cfg=cfg, occs=occs, defs=defs, lines=lines, rows=rows, items=items, lo=lo, hi=hi, warnings=[])
    for r in rows:
        if not ctx.targets(r):
            raise AssemblyDrawingError("bom_line_unplaced", f"BOM item {r.item} ({r.def_key}) matches no placed occurrence")
    return ctx


# --------------------------------------------------------------------------- #
# Hidden-line removal per component
# --------------------------------------------------------------------------- #


def _edges(compound: Any) -> list[Any]:
    out = []
    if compound is None or compound.IsNull():
        return out
    ex = TopExp_Explorer(compound, TopAbs_ShapeEnum.TopAbs_EDGE)
    while ex.More():
        out.append(TopoDS.Edge(ex.Current()))
        ex.Next()
    return out


def _hlr(shapes: list[Any], view: str, center: bd.Vector, diag: float, hidden: bool) -> list[tuple[list[dr._Prim], list[dr._Prim]]]:
    """One HLR of all shapes together (occlusion between components), edges read back per shape. Same camera
    as :func:`app.cad.drawing._project`, so view coordinates match single-part drawings."""
    _, up, n = dr._view_frame(view)
    origin = center + n * (diag * 2.0 + 100.0)
    algo = HLRBRep_Algo()
    for s in shapes:
        algo.Add(s.wrapped)
    ax = gp_Ax2(gp_Pnt(origin.X, origin.Y, origin.Z), gp_Dir(n.X, n.Y, n.Z))
    ax.SetYDirection(gp_Dir(up.X, up.Y, up.Z))
    algo.Projector(HLRAlgo_Projector(ax))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    out = []
    for s in shapes:
        w = s.wrapped
        vis_e = _edges(h.VCompound(w)) + _edges(h.Rg1LineVCompound(w)) + _edges(h.OutLineVCompound(w))
        hid_e = (_edges(h.HCompound(w)) + _edges(h.OutLineHCompound(w)) + _edges(h.Rg1LineHCompound(w))) if hidden else []
        prims: list[list[dr._Prim]] = []
        for group in (vis_e, hid_e):
            ps = []
            for e in group:
                BRepLib.BuildCurves3d_s(e, 1e-6)
                p = dr._edge_prims(bd.Edge(e))
                if p is not None:
                    ps.append(p)
            prims.append(ps)
        out.append((prims[0], prims[1]))
    return out


def _box2(view: str, center: bd.Vector, lo: Any, hi: Any) -> tuple[float, float, float, float]:
    to2d = _to2d(view, center)
    pts = [to2d((a, b, c)) for a in (lo[0], hi[0]) for b in (lo[1], hi[1]) for c in (lo[2], hi[2])]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _views_hlr(ctx: _Ctx, shapes: list[Any], occ_ids: list[int], view: str, hidden: bool,
               whole: list[bool]) -> list[tuple[list[dr._Prim], list[dr._Prim]]]:
    """HLR of one view. Without coil geometry: :func:`_hlr` (exact), unchanged. With it (the exact HLR of a coil takes
    about a minute per view) the view is split by the projected bounding boxes:

    - ``poly``: every coil occurrence and every other one whose projection overlaps a coil's. These are drawn from one
      polygonal HLR (:func:`app.cad.hlr_poly.hide`) that also loads every occurrence overlapping one of them, so
      occlusion among them — coil in front of a pin, a nut in front of a coil — is computed together.
    - the rest: read back from the exact HLR of all non-coil occurrences (a coil cannot hide, or be hidden by, an
      occurrence whose projection does not overlap its own).

    ``whole[i]``: ``shapes[i]`` is the placed occurrence (its definition's mesh is reused; in the axis-aligned views
    its outline is put on its exact bounding box, :func:`app.cad.hlr_poly.snap`), else a section half (meshed here)."""
    coil = [ctx.defs[ctx.occs[i].part.key].coil for i in occ_ids]
    if not any(coil):
        return _hlr(shapes, view, ctx.center, ctx.diag, hidden)
    n = len(shapes)
    boxes = [_box2(view, ctx.center, ctx.occs[i].lo, ctx.occs[i].hi) for i in occ_ids]
    pad = 1e-3 + 1e-6 * ctx.diag

    def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
        return a[0] <= b[2] + pad and b[0] <= a[2] + pad and a[1] <= b[3] + pad and b[1] <= a[3] + pad

    coils = [k for k in range(n) if coil[k]]
    poly = [k for k in range(n) if coil[k] or any(overlap(boxes[k], boxes[c]) for c in coils)]
    loaded = [k for k in range(n) if k in poly or any(overlap(boxes[k], boxes[p]) for p in poly)]
    out: list[tuple[list[dr._Prim], list[dr._Prim]] | None] = [None] * n
    rest = [k for k in range(n) if k not in poly]
    if rest:
        exact = [k for k in range(n) if not coil[k]]
        for k, res in zip(exact, _hlr([shapes[k] for k in exact], view, ctx.center, ctx.diag, hidden)):
            if k not in poly:
                out[k] = res
    lin = ctx.lin
    meshes = []
    for k in loaded:
        o = ctx.occs[occ_ids[k]]
        if whole[k]:
            if o.part.key not in ctx.meshes:
                ctx.meshes[o.part.key] = hlr_poly.mesh(ctx.defs[o.part.key].solid, lin)
            meshes.append(ctx.meshes[o.part.key].Moved(TopLoc_Location(_trsf(o.occ.world))))
        else:
            meshes.append(hlr_poly.mesh(shapes[k], lin))
    res = hlr_poly.hide(meshes, [loaded.index(k) for k in poly], dr._camera(view, ctx.center, ctx.diag), lin)
    for k, (vis, hid) in zip(poly, res):
        if whole[k] and view in dr.VIEW_AXES:
            vis, hid = hlr_poly.snap(vis, boxes[k], hlr_poly.SNAP * lin), hlr_poly.snap(hid, boxes[k], hlr_poly.SNAP * lin)
        out[k] = ([dr._poly_prim(p) for p in vis], [dr._poly_prim(p) for p in hid] if hidden else [])
        ctx.poly_items.add(ctx.occs[occ_ids[k]].part.key)
    return out  # type: ignore[return-value]


@dataclass
class _Comp:
    occ: int
    vis: list[dr._Prim]
    hid: list[dr._Prim]
    loops: list[list[list[tuple[float, float]]]] = field(default_factory=list)  # cut faces (section views)
    style: int | None = None
    adjacent: list[int] = field(default_factory=list)

    @property
    def vis_len(self) -> float:
        return sum(_prim_len(p) for p in self.vis)

    @property
    def hid_len(self) -> float:
        return sum(_prim_len(p) for p in self.hid)

    @property
    def area(self) -> float:
        return sum(_poly_area(fl[0]) - sum(_poly_area(h) for h in fl[1:]) for fl in self.loops)


@dataclass
class _BalloonPlan:
    line: BomLine
    occ: int
    view: str
    anchor: tuple[float, float]  # view coords
    side: str
    on_hidden: bool


@dataclass
class _AsmVC(dr._VC):
    base: str = ""  # projection frame of the view ("front" for an XZ section, …)
    comps: list[_Comp] = field(default_factory=list)
    balloons: list[_BalloonPlan] = field(default_factory=list)
    keep: tuple[str, float, float] | None = None  # section views: (axis, offset, sign of the kept side)


def _to2d(view: str, center: bd.Vector):
    x, y, _ = dr._view_frame(view)

    def f(p: Any) -> tuple[float, float]:
        rel = bd.Vector(*p) - center if not isinstance(p, bd.Vector) else p - center
        return (rel.dot(x), rel.dot(y))

    return f


def _centre_lines(ctx: _Ctx, vc: _AsmVC, occ_ids: list[int]) -> None:
    """Centre lines of every round axis lying in the view plane (merged where collinear) and centre marks
    of axes along the line of sight."""
    _, _, n = dr._view_frame(vc.base)
    to2d = _to2d(vc.base, ctx.center)
    lines: dict[tuple, list[float]] = {}
    others: list[tuple[tuple[float, float], tuple[float, float]]] = []
    marks: dict[tuple, tuple[float, float, float]] = {}
    for i in occ_ids:
        o = ctx.occs[i]
        info = ctx.defs[o.part.key]
        for st in info.bores + info.bosses:
            d = bd.Vector(*apply_dir(o.occ.world, (st.d.X, st.d.Y, st.d.Z)))
            a0, a1 = st.at(st.t_lo), st.at(st.t_hi)
            p0 = apply(o.occ.world, (a0.X, a0.Y, a0.Z))
            p1 = apply(o.occ.world, (a1.X, a1.Y, a1.Z))
            r = max(s.r for s in st.segs)
            cos = abs(d.dot(n))
            if cos < 1e-6:
                a, b = to2d(p0), to2d(p1)
                if abs(a[1] - b[1]) < 1e-6:
                    key = ("h", _r(a[1]))
                    lo_, hi_ = sorted((a[0], b[0]))
                elif abs(a[0] - b[0]) < 1e-6:
                    key = ("v", _r(a[0]))
                    lo_, hi_ = sorted((a[1], b[1]))
                else:
                    others.append((a, b))
                    continue
                if key in lines:
                    lines[key] = [min(lines[key][0], lo_), max(lines[key][1], hi_)]
                else:
                    lines[key] = [lo_, hi_]
            elif cos > 1.0 - 1e-6:
                u, v = to2d(p0)
                mk = (_r(u), _r(v))
                if mk not in marks or marks[mk][2] < r:
                    marks[mk] = (u, v, r)
    for key in sorted(lines):
        lo_, hi_ = lines[key]
        vc.clines.append(((lo_, key[1]), (hi_, key[1])) if key[0] == "h" else ((key[1], lo_), (key[1], hi_)))
    vc.clines.extend(sorted(others))
    vc.marks.extend(marks[k] for k in sorted(marks))


def _projection_vc(ctx: _Ctx, name: str) -> _AsmVC:
    hidden = ctx.cfg.hidden_lines and name != "iso"
    per = _views_hlr(ctx, [o.shape for o in ctx.occs], [o.index for o in ctx.occs], name, hidden, [True] * len(ctx.occs))
    comps = [_Comp(o.index, vis, hid) for o, (vis, hid) in zip(ctx.occs, per)]
    vis = [p for c in comps for p in c.vis]
    hid = [p for c in comps for p in c.hid]
    vc = _AsmVC(name=name, label=dr.VIEW_LABELS[name], kind="projection", vis=vis, hid=hid,
                bbox=dr._bbox2(dr._prim_points(vis + hid)), base=name, comps=comps)
    if name != "iso":
        _centre_lines(ctx, vc, [o.index for o in ctx.occs])
    return vc


# --------------------------------------------------------------------------- #
# Section
# --------------------------------------------------------------------------- #


@dataclass
class _Plan:
    plane: str
    offset: float
    chosen: str


def _axis_of_plane(plane: str) -> int:
    return 1 if plane == "XZ" else 0


def _crossing(o: _Occ, plane: str, offset: float, tol: float = 1e-6) -> str:
    """``cut`` (crosses the plane), ``kept`` (entirely on the kept side) or ``removed``."""
    i = _axis_of_plane(plane)
    lo, hi = o.lo[i], o.hi[i]
    if lo < offset - tol and hi > offset + tol:
        return "cut"
    if plane == "XZ":
        return "kept" if lo >= offset - tol else "removed"
    return "kept" if hi <= offset + tol else "removed"


def _section_rule(ctx: _Ctx, o: _Occ, plane: str, offset: float) -> str | None:
    """Why a crossed component is drawn whole, or None when it is cut."""
    key = o.part.key
    if key in ctx.cfg.always_sectioned:
        return None
    if key in ctx.cfg.not_sectioned:
        return "config_not_sectioned"
    if (o.part.meta or {}).get("section") is False:
        return "definition_not_sectioned"
    rev = ctx.defs[key].revolution
    in_plane = False
    if rev is not None:
        p = apply(o.occ.world, (rev[0].X, rev[0].Y, rev[0].Z))
        d = apply_dir(o.occ.world, (rev[1].X, rev[1].Y, rev[1].Z))
        i = _axis_of_plane(plane)
        in_plane = abs(d[i]) < 1e-6 and abs(p[i] - offset) < 1e-4
    if o.part.procurement == "standard":
        if rev is None:
            return "standard_part_not_sectioned"
        return "standard_part_along_axis" if in_plane else None
    if rev is not None and rev[2] and in_plane:
        return "solid_shaft_along_axis"
    return None


def _plan_section(ctx: _Ctx) -> _Plan | None:
    sec = ctx.cfg.section
    if isinstance(sec, SectionSpec):
        return _Plan(sec.plane, float(sec.offset), "config")
    if sec == "none":
        return None
    cands: set[tuple[str, float]] = {("XZ", _r(ctx.center.Y, 6)), ("YZ", _r(ctx.center.X, 6))}
    for o in ctx.occs:
        cands.add(("XZ", _r((o.lo[1] + o.hi[1]) / 2.0, 6)))
        cands.add(("YZ", _r((o.lo[0] + o.hi[0]) / 2.0, 6)))
        rev = ctx.defs[o.part.key].revolution
        if rev is not None:
            p = apply(o.occ.world, (rev[0].X, rev[0].Y, rev[0].Z))
            d = apply_dir(o.occ.world, (rev[1].X, rev[1].Y, rev[1].Z))
            if abs(d[1]) < 1e-6:
                cands.add(("XZ", _r(p[1], 6)))
            if abs(d[0]) < 1e-6:
                cands.add(("YZ", _r(p[0], 6)))

    def score(c: tuple[str, float]) -> tuple:
        plane, off = c
        cut = [o for o in ctx.occs if _crossing(o, plane, off) == "cut" and _section_rule(ctx, o, plane, off) is None]
        centre = ctx.center.Y if plane == "XZ" else ctx.center.X
        return (sum(1 for o in cut if ctx.defs[o.part.key].hollow), len(cut), plane == "XZ", -_r(abs(off - centre), 6), -off)

    best = max(sorted(cands), key=score)
    if score(best)[1] == 0:
        ctx.warnings.append(Issue(code="section_not_drawn", message="no plane through the bounding-box or axis centres "
                                  "cuts a component that the section conventions allow to be sectioned"))
        return None
    return _Plan(best[0], best[1], "auto")


def _assign_styles(ctx: _Ctx, comps: list[_Comp]) -> None:
    """Hatch style per cut component: its definition's preferred style unless a touching cut component has it."""
    order = sorted({ctx.occs[c.occ].part.key for c in comps})
    pref = {k: i % len(HATCH_STYLES) for i, k in enumerate(order)}
    cut = [c for c in comps if c.loops]
    boxes = {c.occ: dr._bbox2([p for fl in c.loops for lp in fl for p in lp]) for c in cut}

    def near(a: _Comp, b: _Comp) -> bool:
        ba, bb = boxes[a.occ], boxes[b.occ]
        if ba[0] > bb[2] + ADJACENT_TOL or bb[0] > ba[2] + ADJACENT_TOL or ba[1] > bb[3] + ADJACENT_TOL or bb[1] > ba[3] + ADJACENT_TOL:
            return False
        for p, q in ((a, b), (b, a)):
            segs = [(lp[i], lp[(i + 1) % len(lp)]) for fl in q.loops for lp in fl for i in range(len(lp))]
            for fl in p.loops:
                for lp in fl:
                    for pt in lp:
                        if any(_seg_dist(pt, s0, s1) <= ADJACENT_TOL for s0, s1 in segs):
                            return True
        return False

    for i, a in enumerate(cut):
        for b in cut[i + 1:]:
            if near(a, b):
                a.adjacent.append(b.occ)
                b.adjacent.append(a.occ)
    for c in cut:
        used = {o.style for o in cut if o.occ in c.adjacent and o.style is not None}
        p = pref[ctx.occs[c.occ].part.key]
        cands = [p] + [(p + k) % len(HATCH_STYLES) for k in range(1, len(HATCH_STYLES))]
        c.style = next((s for s in cands if s not in used), p)


def _section_vc(ctx: _Ctx, plan: _Plan, letter: str) -> tuple[_AsmVC, SectionRecord] | None:
    base = "front" if plan.plane == "XZ" else "right"
    pt, keep = dr._plane_geom(plan.plane, plan.offset)
    _, _, n = dr._view_frame(base)
    to2d = _to2d(base, ctx.center)
    rec = SectionRecord(letter=letter, plane=plan.plane, offset=_r(plan.offset, 6), chosen=plan.chosen,  # type: ignore[arg-type]
                        view=f"section_{letter}", base_view=base, view_direction=[0.0, 1.0, 0.0] if plan.plane == "XZ" else [-1.0, 0.0, 0.0],
                        kept=f"y >= {dr._fmt(plan.offset)}" if plan.plane == "XZ" else f"x <= {dr._fmt(plan.offset)}",
                        cut_on=None)
    shapes: list[Any] = []
    present: list[int] = []
    loops_of: dict[int, list[list[list[tuple[float, float]]]]] = {}
    used_rules: set[str] = set()
    for o in ctx.occs:
        where = _crossing(o, plan.plane, plan.offset)
        item = ctx.items.get(o.part.key)
        if where == "removed":
            rec.omitted.append(list(o.path))
            continue
        shape = o.shape
        if where == "cut":
            rule = _section_rule(ctx, o, plan.plane, plan.offset)
            if rule is not None:
                used_rules.add(rule)
                rec.not_sectioned.append({"occurrence": list(o.path), "def_key": o.part.key, "item": item, "rule": rule})
            else:
                try:
                    half = o.shape.split(bd.Plane(origin=(pt.X, pt.Y, pt.Z), z_dir=(keep.X, keep.Y, keep.Z)), keep=bd.Keep.TOP)
                except Exception as exc:  # noqa: BLE001 - OCCT failure: drawn whole, reported
                    half = None
                    ctx.warnings.append(Issue(code="section_split_failed", message=f"{o.path_s}: {str(exc)[:200]}"))
                if isinstance(half, list) and ctx.defs[o.part.key].coil:  # a coil cut along its axis: one piece per half turn
                    half = bd.Compound(half) if half else None
                if half is None or getattr(half, "volume", 0.0) <= 1e-9:
                    ctx.warnings.append(Issue(code="section_split_failed", message=f"{o.path_s}: no material behind the plane; drawn whole"))
                else:
                    shape = half
                    face_loops = []
                    for f in half.faces():
                        if f.geom_type != bd.GeomType.PLANE:
                            continue
                        c = f.center()
                        if f.normal_at(c).dot(n) < 1.0 - 1e-6 or abs((c - pt).dot(keep)) > 1e-5:
                            continue
                        fl = [dr._wire_points(f.outer_wire(), to2d)] + [dr._wire_points(w, to2d) for w in f.inner_wires()]
                        fl = [lp for lp in fl if len(lp) >= 3]
                        if fl:
                            face_loops.append(fl)
                    face_loops.sort(key=lambda fl: (_r(min(p[0] for p in fl[0])), _r(min(p[1] for p in fl[0]))))
                    loops_of[o.index] = face_loops
                    rec.cut.append({"occurrence": list(o.path), "def_key": o.part.key, "item": item, "faces": len(face_loops)})
        else:
            rec.whole.append(list(o.path))
        shapes.append(shape)
        present.append(o.index)
    if not rec.cut:
        ctx.warnings.append(Issue(code="section_empty", message=f"section {letter}-{letter} ({plan.plane} at "
                                  f"{dr._fmt(plan.offset)}) cuts no component that may be sectioned; not drawn"))
        return None
    per = _views_hlr(ctx, shapes, present, base, False, [shape is ctx.occs[i].shape for i, shape in zip(present, shapes)])
    comps = [_Comp(i, vis, [], loops_of.get(i, [])) for i, (vis, _) in zip(present, per)]
    _assign_styles(ctx, comps)
    vis = [p for c in comps for p in c.vis]
    pts = dr._prim_points(vis) + [p for c in comps for fl in c.loops for lp in fl for p in lp]
    i = _axis_of_plane(plan.plane)
    vc = _AsmVC(name=rec.view, label=f"SECTION {letter}-{letter}", kind="section", vis=vis, hid=[], bbox=dr._bbox2(pts),
                base=base, comps=comps, keep=("xyz"[i], plan.offset, 1.0 if plan.plane == "XZ" else -1.0))
    _centre_lines(ctx, vc, present)
    rec.rules = {k: SECTION_RULES[k] for k in sorted(used_rules)}
    return vc, rec


def _cut_line(ctx: _Ctx, vcs: dict[str, _AsmVC], plan: _Plan, letter: str) -> str | None:
    """Put the cutting line on a projection that shows the plane edge-on (top first)."""
    pt, keep = dr._plane_geom(plan.plane, plan.offset)
    normal = bd.Vector(0, 1, 0) if plan.plane == "XZ" else bd.Vector(1, 0, 0)
    for name in ("top", "front", "right"):
        vc = vcs.get(name)
        if vc is None:
            continue
        x, y, n = dr._view_frame(name)
        if abs(normal.dot(n)) > 1e-6:
            continue
        rel = pt - ctx.center
        if abs(abs(normal.dot(y)) - 1.0) < 1e-6:
            vc.cut = {"orient": "h", "coord": rel.dot(y), "dir": (keep.dot(x), keep.dot(y)), "letter": letter}
        else:
            vc.cut = {"orient": "v", "coord": rel.dot(x), "dir": (keep.dot(x), keep.dot(y)), "letter": letter}
        return name
    return None


# --------------------------------------------------------------------------- #
# Dimensions
# --------------------------------------------------------------------------- #

_SCREEN = {"front": ("x", "z"), "top": ("x", "y"), "right": ("y", "z")}


def _overall_dims(ctx: _Ctx, vcs: dict[str, _AsmVC]) -> None:
    size = ctx.size
    picks: list[tuple[str, str, str]] = []
    if "front" in vcs:
        picks += [("front", "h", "x"), ("front", "v", "z")]
    else:
        if "top" in vcs:
            picks.append(("top", "h", "x"))
        if "right" in vcs:
            picks.append(("right", "v", "z"))
    if "top" in vcs:
        picks.append(("top", "v", "y"))
    elif "right" in vcs:
        picks.append(("right", "h", "y"))
    stated = {axis for _, _, axis in picks}
    for axis in "xyz":
        if axis not in stated:
            ctx.warnings.append(Issue(code="overall_dimension_missing", message=f"no projection shows the overall {axis.upper()}"))
    for view, orient, axis in picks:
        vc = vcs[view]
        pts = dr._prim_points(vc.vis + vc.hid)
        if orient == "h":
            p1, p2, side = dr._extreme(pts, "left"), dr._extreme(pts, "right"), "bottom"
        else:
            p1, p2, side = dr._extreme(pts, "bottom"), dr._extreme(pts, "top"), "left"
        vc.dims.append(dr._DimPlan("overall", axis, orient, side, p1, p2, f"overall {axis.upper()} (assembly envelope)",
                                   ref={"size": _r(size[axis], 6)}))


def _dim_view(ctx: _Ctx, vcs: dict[str, _AsmVC], axis: str, pts: list[tuple[float, float, float]], want: str | None) -> tuple[_AsmVC, str] | None:
    names: list[str] = []
    sec = next((v for v in vcs.values() if v.kind == "section"), None)
    if want == "section":
        names = [sec.name] if sec else []
    elif want:
        names = [want]
    else:
        if sec is not None and sec.keep is not None:
            i = "xyz".index(sec.keep[0])
            if all(sec.keep[2] * (p[i] - sec.keep[1]) >= -1e-6 for p in pts):
                names.append(sec.name)
        names += ["front", "top", "right"]
    for name in names:
        vc = vcs.get(name)
        if vc is None:
            continue
        screen = _SCREEN[vc.base]
        if axis in screen:
            return vc, ("h" if screen[0] == axis else "v")
    return None


def _add_axis_dim(ctx: _Ctx, vcs: dict[str, _AsmVC], kind: str, axis: str, a: tuple[float, float, float],
                  b: tuple[float, float, float], measured: str, ref: dict[str, Any], want: str | None, feature: str | None) -> bool:
    if abs(a["xyz".index(axis)] - b["xyz".index(axis)]) < 1e-6:
        ctx.warnings.append(Issue(code="dimension_zero", message=f"{measured}: zero distance along {axis.upper()}"))
        return False
    hit = _dim_view(ctx, vcs, axis, [a, b], want)
    if hit is None:
        ctx.warnings.append(Issue(code="dimension_view_missing", message=f"{measured}: no view shows {axis.upper()}"))
        return False
    vc, orient = hit
    to2d = _to2d(vc.base, ctx.center)
    vc.dims.append(dr._DimPlan(kind, axis, orient, "top" if orient == "h" else "left", to2d(a), to2d(b), measured,
                               feature=feature, ref=ref))
    return True


def _pitch_dims(ctx: _Ctx, vcs: dict[str, _AsmVC]) -> None:
    groups: dict[str, list[_Occ]] = collections.defaultdict(list)
    for o in ctx.occs:
        groups[o.part.key].append(o)
    seen: dict[tuple, dr._DimPlan] = {}
    for key in sorted(groups, key=lambda k: (_item_key(ctx.items.get(k, "")), k)):
        os_ = groups[key]
        if not 2 <= len(os_) <= MAX_PITCH_INSTANCES:
            continue
        origins = [(apply(o.occ.world, (0.0, 0.0, 0.0)), o) for o in os_]
        axis = None
        for i in range(3):
            others = [j for j in range(3) if j != i]
            if all(abs(p[j] - origins[0][0][j]) < 1e-6 for p, _ in origins for j in others) and \
                    max(p[i] for p, _ in origins) - min(p[i] for p, _ in origins) > 1e-6:
                axis = "xyz"[i]
        if axis is None:
            continue
        i = "xyz".index(axis)
        origins.sort(key=lambda t: t[0][i])
        item = ctx.items.get(key)
        for (pa, oa), (pb, ob) in zip(origins, origins[1:]):
            same = (axis, tuple(_r(c) for c in pa), tuple(_r(c) for c in pb))
            if same in seen:  # another definition's instances share these origins (a repeated sub-assembly)
                seen[same].ref.setdefault("also", []).append({"def_key": key, "item": item, "from": oa.path_s, "to": ob.path_s})
                continue
            if _add_axis_dim(ctx, vcs, "pitch", axis, pa, pb, f"item {item} ({key}) instance origins along {axis.upper()}",
                             {"def_key": key, "item": item, "from": oa.path_s, "to": ob.path_s, "datum": "origin"}, None,
                             f"item {item}"):
                seen[same] = next(d for vc in vcs.values() for d in vc.dims if d.kind == "pitch" and d.ref.get("def_key") == key
                                  and d.ref.get("from") == oa.path_s)


def _ref_point(ctx: _Ctx, ref: PointRef, dim_id: str) -> tuple[tuple[float, float, float], str]:
    o = ctx.by_path(tuple(ref.path))
    if o is None:
        raise AssemblyDrawingError("key_dimension_unknown_instance", f"key dimension {dim_id}: no occurrence {'/'.join(ref.path)}")
    info = ctx.defs[o.part.key]
    if ref.point is not None:
        p = tuple(float(c) for c in ref.point)
        vtx = BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex()
        # distance to the boundary faces (to the solid itself, a point inside the material would be at 0)
        dist = BRepExtrema_DistShapeShape(vtx, bd.Compound(info.solid.faces()).wrapped)
        if not dist.IsDone() or dist.Value() > POINT_ON_PART_TOL:
            raise AssemblyDrawingError("key_dimension_point_off_part",
                                       f"key dimension {dim_id}: point {list(p)} is not on {o.part.key} "
                                       f"({dist.Value() if dist.IsDone() else 'n/a'} mm away)",
                                       {"dimension": dim_id, "path": list(ref.path), "point": list(p)})
        label = "point (" + ", ".join(dr._fmt(c) for c in p) + ")"
    else:
        datum = ref.datum or "origin"
        if datum == "origin":
            p = (0.0, 0.0, 0.0)
        elif datum in ("bbox.min", "bbox.max", "bbox.center"):
            p = info.lo if datum == "bbox.min" else info.hi if datum == "bbox.max" else \
                tuple((a + b) / 2.0 for a, b in zip(info.lo, info.hi))
        else:
            found = None
            for d in (o.part.meta or {}).get("datums") or []:
                name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
                if name == datum:
                    found = d.get("origin") if isinstance(d, dict) else getattr(d, "origin", None)
                    break
            if found is None:
                raise AssemblyDrawingError("key_dimension_unknown_datum",
                                           f"key dimension {dim_id}: {o.part.key} has no datum {datum!r}",
                                           {"dimension": dim_id, "datum": datum})
            p = tuple(float(c) for c in found)
        label = datum
    return apply(o.occ.world, p), f"{o.path_s} {label}"  # type: ignore[arg-type]


def _key_dims(ctx: _Ctx, vcs: dict[str, _AsmVC]) -> None:
    for kd in ctx.cfg.key_dimensions:
        a, la = _ref_point(ctx, kd.a, kd.id)
        b, lb = _ref_point(ctx, kd.b, kd.id)
        measured = f"{kd.id}: {kd.label + ' — ' if kd.label else ''}{la} to {lb} along {kd.axis.upper()}"
        ok = _add_axis_dim(ctx, vcs, "key", kd.axis, a, b, measured,
                           {"id": kd.id, "a": la, "b": lb, "label": kd.label}, kd.view, kd.id)
        if not ok:
            raise AssemblyDrawingError("key_dimension_not_drawn", f"key dimension {kd.id} cannot be drawn",
                                       {"dimension": kd.id, "warnings": [w.message for w in ctx.warnings[-1:]]})


# --------------------------------------------------------------------------- #
# Balloons (planning in view coordinates; placement after the layout)
# --------------------------------------------------------------------------- #


def _edge_anchor(comp: _Comp, prims: list[dr._Prim], others: list[_Comp]) -> tuple[float, float] | None:
    other_segs = [(pr.pts[i], pr.pts[i + 1]) for c in others for pr in c.vis for i in range(len(pr.pts) - 1)]
    cands = sorted(prims, key=lambda pr: (-_r(_prim_len(pr)), _r(_prim_mid(pr)[0]), _r(_prim_mid(pr)[1])))
    for pr in cands:
        m = _prim_mid(pr)
        if not any(_seg_dist(m, a, b) < 0.3 for a, b in other_segs):
            return m
    return _prim_mid(cands[0]) if cands else None


def _plan_balloons(ctx: _Ctx, vcs: list[_AsmVC]) -> None:
    order = {vc.name: k for k, vc in enumerate(vcs)}
    usable = [vc for vc in vcs if vc.base != "iso"]

    def best(occ_ids: list[int]) -> tuple[tuple, _AsmVC, _Comp] | None:
        found = None
        for vc in usable:
            for comp in vc.comps:
                if comp.occ not in occ_ids:
                    continue
                if comp.loops:
                    score = (2, _r(comp.area, 3))
                elif comp.vis:
                    score = (1, _r(comp.vis_len, 3))
                elif comp.hid:
                    score = (0, _r(comp.hid_len, 3))
                else:
                    continue
                key = (score, -order[vc.name], -occ_ids.index(comp.occ))
                if found is None or key > found[0]:
                    found = (key, vc, comp)
        return found

    for line in ctx.rows:
        targets = [o.index for o in ctx.targets(line)]
        groups = [[i] for i in targets] if ctx.cfg.balloon_per_instance else [targets]
        for ids in groups:
            hit = best(ids)
            if hit is None:
                raise AssemblyDrawingError("balloon_not_placeable", f"BOM item {line.item} has no edge in any view")
            (score, _, _), vc, comp = hit
            if score[0] == 2:
                anchor = _interior_point(max(comp.loops, key=lambda fl: _poly_area(fl[0])))[0]
            elif score[0] == 1:
                anchor = _edge_anchor(comp, comp.vis, [c for c in vc.comps if c.occ != comp.occ])
            else:
                anchor = _edge_anchor(comp, comp.hid, [])
                ctx.warnings.append(Issue(code="balloon_on_hidden_item", message=f"item {line.item}: no visible edge in "
                                          "any view; the leader points to a hidden edge"))
            assert anchor is not None
            vc.balloons.append(_BalloonPlan(line, comp.occ, vc.name, anchor, _balloon_side(vc, anchor), score[0] == 0))


BALLOON_SIDES = ("right", "left", "top")


def _balloon_side(vc: _AsmVC, anchor: tuple[float, float]) -> str:
    """The view side nearest to the anchor (the bottom side carries the overall dimensions and the label)."""
    x0, _, x1, y1 = vc.bbox
    dist = {"right": x1 - anchor[0], "left": anchor[0] - x0, "top": y1 - anchor[1]}
    return min(BALLOON_SIDES, key=lambda s: (_r(dist[s], 6), BALLOON_SIDES.index(s)))


def _margins(vc: _AsmVC) -> dict[str, float]:
    m = dr._margins(vc)
    for side in BALLOON_SIDES:
        if any(b.side == side for b in vc.balloons):
            dims = len([d for d in vc.dims if d.side == side])
            base = dr.DIM_FIRST + (dims - 1) * dr.DIM_STEP + dr.DIM_TXT + 1.0 if dims else 0.0
            m[side] = max(m[side], base + BALLOON_GAP + 2 * BALLOON_R + 2.0)
    return m


def _off_text(v: float, texts: list[tuple[float, float]]) -> float:
    """Move a balloon's ideal position along its side out of the text span of a dimension its leader crosses."""
    for c, h in sorted(texts):
        if c - h < v < c + h:
            v = c - h if v < c else c + h
    return v


def _spread(ideal: list[float], lo: float | None, hi: float) -> list[float]:
    """Positions in the order of ``ideal`` (ascending), at least :data:`BALLOON_SPACING` apart, as close to
    their ideal values as the spacing allows, pushed below ``hi`` (and above ``lo`` when it still fits)."""
    out: list[float] = []
    for v in ideal:
        out.append(v if not out else max(v, out[-1] + BALLOON_SPACING))
    if out and out[-1] > hi:
        shift = out[-1] - hi
        out = [v - shift for v in out]
        for j in range(len(out) - 2, -1, -1):
            out[j] = min(out[j], out[j + 1] - BALLOON_SPACING)
    if lo is not None and out and out[0] < lo and out[-1] + (lo - out[0]) <= hi:
        out = [v + (lo - out[0]) for v in out]
    return out


# --------------------------------------------------------------------------- #
# Sheet
# --------------------------------------------------------------------------- #


def _balloon_svg(bid: str, rec: dict[str, Any]) -> str:
    """SVG group of one balloon from its record (DXF coordinates, y up)."""
    sy = dr._Sheet.sy
    cx, cy = rec["center"]
    ax, ay = rec["anchor"]
    r = rec["radius"]
    dx, dy = ax - cx, ay - cy
    ln = math.hypot(dx, dy) or 1.0
    sx0, sy0 = cx + dx / ln * r, cy + dy / ln * r
    h = BALLOON_H if dr._text_width(rec["item"], BALLOON_H) <= 1.6 * r else BALLOON_H * 1.6 * r / dr._text_width(rec["item"], BALLOON_H)
    n = dr._n
    return (f'<g class="balloon" id="{bid}" data-item={_q(rec["item"])} data-def={_q(rec["def_key"])} '
            f'data-path={_q("/".join(rec["occurrence"]))} data-handle="{rec["handle"]}">'
            f'<path class="dim" d="M{n(sx0)} {n(sy(sy0))} L{n(ax)} {n(sy(ay))}"/>'
            f'<rect class="dot" x="{n(ax - DOT)}" y="{n(sy(ay) - DOT)}" width="{n(2 * DOT)}" height="{n(2 * DOT)}"/>'
            f'<circle class="bal" cx="{n(cx)}" cy="{n(sy(cy))}" r="{n(r)}"/>'
            f'<text x="{n(cx)}" y="{n(sy(cy - h / 2.0))}" font-size="{n(h / dr.CAP_RATIO)}" text-anchor="middle">'
            f'{_esc(rec["item"])}</text></g>')


def _q(s: str) -> str:
    from xml.sax.saxutils import quoteattr

    return quoteattr(str(s))


def _esc(s: str) -> str:
    from xml.sax.saxutils import escape

    return escape(str(s))


class _AsmSheet(dr._Sheet):
    """The single-part sheet plus balloon blocks, BOM table layer and per-component hatch styles."""

    def __init__(self, title: str, scale: float):
        super().__init__(title, scale)
        self.doc.layers.add("BALLOON", color=3, lineweight=25)
        self.doc.layers.add("BOM", color=7, lineweight=25)
        self.styles_used: set[int] = set()
        self.blocks: dict[str, str] = {}

    def text_h(self, s: str, x: float, y: float, h: float, anchor: str = "start", layer: str = "TEXT",
               bold: bool = False, svg_class: str | None = None, group: str = "text") -> str:
        """``_Sheet.text`` returning the DXF handle (table cells are referenced by the page records)."""
        weight = ' font-weight="bold"' if bold else ""
        cls = f' class="{svg_class}"' if svg_class else ""
        self.svg[group].append(f'<text{cls} x="{dr._n(x)}" y="{dr._n(self.sy(y))}" font-size="{dr._n(h / dr.CAP_RATIO)}" '
                               f'text-anchor="{anchor}"{weight}>{_esc(s)}</text>')
        align = {"start": TextEntityAlignment.LEFT, "middle": TextEntityAlignment.CENTER, "end": TextEntityAlignment.RIGHT}[anchor]
        t = self.msp.add_text(s, height=h, dxfattribs={"layer": layer, "style": dr.TEXT_STYLE["name"]})
        t.set_placement((x, y), align=align)
        return t.dxf.handle

    def line_h(self, a: tuple[float, float], b: tuple[float, float], layer: str, svg_class: str, group: str) -> str:
        self.svg[group].append(f'<path class="{svg_class}" d="{self._d([a, b])}"/>')
        return self.msp.add_line(a, b, dxfattribs={"layer": layer}).dxf.handle

    def styled_hatch(self, loops: list[list[tuple[float, float]]], tag: str, style: int, path: str) -> str:
        angle, scale = HATCH_STYLES[style]
        self.styles_used.add(style)
        d = " ".join(self._d(lp, True) for lp in loops)
        self.svg["hatch"].append(f'<path class="hatch" data-face={_q(tag)} data-path={_q(path)} data-style="{style}" '
                                 f'style="fill:url(#hp{style})" fill-rule="evenodd" d="{d}"/>')
        h = self.msp.add_hatch(color=256, dxfattribs={"layer": "HATCH"})
        for i, lp in enumerate(loops):
            flags = dxfconst.BOUNDARY_PATH_EXTERNAL if i == 0 else dxfconst.BOUNDARY_PATH_OUTERMOST
            h.paths.add_polyline_path(lp, is_closed=True, flags=flags)
        h.set_pattern_fill("ANSI31", color=256, angle=angle, scale=scale, style=0)
        self.hatch_count += 1
        return h.dxf.handle

    def balloon_block(self, item: str) -> str:
        if item in self.blocks:
            return self.blocks[item]
        name = f"PR_BALLOON_{_safe(item)}"
        k = 2
        while name in self.doc.blocks:
            name = f"PR_BALLOON_{_safe(item)}_{k}"
            k += 1
        blk = self.doc.blocks.new(name)
        blk.add_circle((0.0, 0.0), BALLOON_R, dxfattribs={"layer": "BALLOON"})
        w = dr._text_width(item, BALLOON_H)
        h = BALLOON_H if w <= 1.6 * BALLOON_R else BALLOON_H * 1.6 * BALLOON_R / w
        t = blk.add_text(item, height=h, dxfattribs={"layer": "BALLOON", "style": dr.TEXT_STYLE["name"]})
        t.set_placement((0.0, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
        self.blocks[item] = name
        return name

    def balloon(self, bid: str, rec: dict[str, Any]) -> tuple[str, str, str, str]:
        """INSERT of the item's block, leader LINE and end dot (SOLID). Returns (block, insert, leader, dot)."""
        block = self.balloon_block(rec["item"])
        cx, cy = rec["center"]
        ax, ay = rec["anchor"]
        dx, dy = ax - cx, ay - cy
        ln = math.hypot(dx, dy) or 1.0
        start = (cx + dx / ln * BALLOON_R, cy + dy / ln * BALLOON_R)
        leader = self.msp.add_line(start, (ax, ay), dxfattribs={"layer": "LEADER"}).dxf.handle
        dot = self.msp.add_solid([(ax - DOT, ay - DOT), (ax + DOT, ay - DOT), (ax - DOT, ay + DOT), (ax + DOT, ay + DOT)],
                                 dxfattribs={"layer": "LEADER"}).dxf.handle
        ins = self.msp.add_blockref(block, (cx, cy), dxfattribs={"layer": "BALLOON"}).dxf.handle
        self.svg["dims"].append(_balloon_svg(bid, {**rec, "handle": ins, "radius": BALLOON_R}))
        return block, ins, leader, dot

    def svg_bytes(self) -> bytes:
        text = super().svg_bytes().decode("utf-8")
        css = ".bal{stroke:#000;stroke-width:0.35;fill:#fff}.dot{fill:#000;stroke:none}"
        pats = "".join(
            f'<pattern id="hp{s}" patternUnits="userSpaceOnUse" width="{dr._n(3.175 * HATCH_STYLES[s][1])}" '
            f'height="{dr._n(3.175 * HATCH_STYLES[s][1])}" patternTransform="rotate({dr._n(-(45.0 + HATCH_STYLES[s][0]))})">'
            f'<line class="hl" x1="0" y1="{dr._n(3.175 * HATCH_STYLES[s][1] / 2)}" x2="{dr._n(3.175 * HATCH_STYLES[s][1])}" '
            f'y2="{dr._n(3.175 * HATCH_STYLES[s][1] / 2)}"/></pattern>' for s in sorted(self.styles_used))
        text = text.replace("</style>", css + "</style>", 1).replace("</defs>", pats + "</defs>", 1)
        return text.encode("utf-8")


def _render_cut(sheet: _AsmSheet, vc: _AsmVC, ox: float, oy: float, scale: float, box: tuple[float, float, float, float]) -> None:
    """Cutting line, end strokes, arrows (direction of sight) and letters; as on single-part section sheets."""
    assert vc.cut is not None
    px0, py0, px1, py1 = box
    letter = vc.cut["letter"]
    du, dv = vc.cut["dir"]
    if vc.cut["orient"] == "h":
        yv = oy + vc.cut["coord"] * scale
        e0, e1 = (px0 - dr.CUT_EXT, yv), (px1 + dr.CUT_EXT, yv)
        ends = [(e0, (e0[0] + 5.0, yv)), (e1, (e1[0] - 5.0, yv))]
    else:
        xv = ox + vc.cut["coord"] * scale
        e0, e1 = (xv, py0 - dr.CUT_EXT), (xv, py1 + dr.CUT_EXT)
        ends = [(e0, (xv, e0[1] + 5.0)), (e1, (xv, e1[1] - 5.0))]
    sheet.polyline([e0, e1], "CUTLINE", "center")
    for end, inner in ends:
        sheet.polyline([end, inner], "CUTLINE", "center", svg_class="cutend", lineweight=70)
        tip = (end[0] + du * dr.CUT_ARROW, end[1] + dv * dr.CUT_ARROW)
        sheet.polyline([end, tip], "CUTLINE", "center", svg_class="cutend", lineweight=35)
        sheet.arrow(tip, end, layer="CUTLINE", size=3.0)
        lx, ly = tip[0] + du * 2.0, tip[1] + dv * 2.0
        if dv < -0.5:
            ly -= 5.0
        sheet.text(letter, lx + (2.5 if abs(dv) > 0.5 else 0.0), ly, 5.0, "middle", bold=True)


def _render_view(ctx: _Ctx, sheet: _AsmSheet, vc: _AsmVC, origin: tuple[float, float], scale: float, page: int,
                 dims_out: list[AsmDimension], balloons_out: list[BalloonRecord], hatches_out: list[HatchRecord]) -> dr.PageView:
    ox, oy = origin

    def P(p: tuple[float, float]) -> tuple[float, float]:
        return (ox + p[0] * scale, oy + p[1] * scale)

    # ---- hatches, one style per cut component
    for comp in vc.comps:
        if not comp.loops:
            continue
        o = ctx.occs[comp.occ]
        assert comp.style is not None
        handles = [sheet.styled_hatch([[P(p) for p in lp] for lp in fl], f"{vc.name}:{o.path_s}:{i + 1}", comp.style, o.path_s)
                   for i, fl in enumerate(comp.loops)]
        angle, hscale = HATCH_STYLES[comp.style]
        hatches_out.append(HatchRecord(view=vc.name, occurrence=list(o.path), def_key=o.part.key, item=ctx.items.get(o.part.key),
                                       angle=angle, scale=hscale, style=comp.style, loops=len(comp.loops), handles=handles,
                                       adjacent=sorted(ctx.occs[a].path_s for a in comp.adjacent)))
    # ---- edges per component (hidden first)
    for comp in vc.comps:
        o = ctx.occs[comp.occ]
        if not comp.vis and not comp.hid:
            continue
        sheet.svg["views"].append(f'<g class="component" data-path={_q(o.path_s)} data-def={_q(o.part.key)} '
                                  f'data-item={_q(ctx.items.get(o.part.key, ""))}>')
        for prims, layer in ((comp.hid, "HIDDEN"), (comp.vis, "VISIBLE")):
            for pr in prims:
                if pr.kind == "circle" and pr.center:
                    sheet.circle(P(pr.center), pr.radius * scale, layer)
                elif pr.kind == "arc" and pr.center:
                    sheet.arc([P(p) for p in pr.pts], P(pr.center), pr.radius * scale, pr.a0, pr.a1, layer)
                else:
                    sheet.polyline([P(p) for p in pr.pts], layer)
        sheet.svg["views"].append("</g>")
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
    if vc.cut is not None:
        _render_cut(sheet, vc, ox, oy, scale, (px0, py0, px1, py1))

    # ---- dimensions (stacked per side, shortest innermost)
    sides = dr._side_dims(vc)
    extent = {s: 0.0 for s in sides}
    dim_texts: dict[str, list[tuple[float, float]]] = {s: [] for s in sides}  # (centre along the side, half length)
    for side, ds in sides.items():
        for i, d in enumerate(ds):
            off = dr.DIM_FIRST + i * dr.DIM_STEP
            at = {"top": py1 + off, "bottom": py0 - off, "left": px0 - off, "right": px1 + off}[side]
            rid = f"P{page}-D{len(dims_out) + 1}"
            handle, label, value, p1, p2 = sheet.linear_dim(rid, d.kind, P(d.p1), P(d.p2), d.orient == "v", at, d.prefix)
            k = 1 if d.orient == "v" else 0
            dim_texts[side].append(((p1[k] + p2[k]) / 2.0, dr._text_width(label, dr.DIM_TXT) / 2.0 + 1.5))
            dims_out.append(AsmDimension(
                id=rid, page=page, view=vc.name, kind=d.kind, axis=d.axis, value=_r(value, 6), text=label,  # type: ignore[arg-type]
                measured=d.measured, feature=d.feature, handle=handle, p1=[_r(p1[0]), _r(SHEET_H - p1[1])],
                p2=[_r(p2[0]), _r(SHEET_H - p2[1])], ref=dict(d.ref)))
            extent[side] = off + dr.DIM_TXT + 1.0

    # ---- balloons: a column left / right of the view or a row above it (beyond its dimensions), ordered along
    # the side by anchor position so the leaders of one side never cross
    for side in BALLOON_SIDES:
        plans = [b for b in vc.balloons if b.side == side]
        if not plans:
            continue
        anchors = [P(b.anchor) for b in plans]
        along = 0 if side == "top" else 1
        order = sorted(range(len(plans)), key=lambda k: (_r(anchors[k][along]), _r(anchors[k][1 - along]), plans[k].line.item))
        ideal = [_off_text(anchors[k][along], dim_texts[side]) for k in order]
        if side == "top":
            fixed = py1 + extent["top"] + BALLOON_GAP + BALLOON_R
            pos = _spread(ideal, px0 - BALLOON_R, px1 + 2 * BALLOON_R)
            centers = [(x, fixed) for x in pos]
        else:
            fixed = px1 + extent[side] + BALLOON_GAP + BALLOON_R if side == "right" else px0 - extent[side] - BALLOON_GAP - BALLOON_R
            pos = _spread(ideal, py0 - 2 * BALLOON_R, py1 + 8.0)
            centers = [(fixed, y) for y in pos]
        for k, (x, y) in zip(order, centers):
            b = plans[k]
            o = ctx.occs[b.occ]
            bid = f"P{page}-B{len(balloons_out) + 1}"
            rec = {"item": b.line.item, "def_key": b.line.def_key, "occurrence": list(o.path), "center": [_r(x), _r(y)],
                   "anchor": _pt(anchors[k]), "radius": BALLOON_R}
            block, ins, leader, dot = sheet.balloon(bid, rec)
            balloons_out.append(BalloonRecord(
                id=bid, item=b.line.item, bom=ctx.cfg.bom_view, level=b.line.level, def_key=b.line.def_key,
                def_kind=b.line.def_kind, quantity=b.line.total_quantity if ctx.cfg.bom_view == "flattened" else b.line.quantity,
                view=vc.name, occurrence=list(o.path), anchor=rec["anchor"], center=rec["center"], radius=BALLOON_R,
                block=block, handle=ins, leader_handle=leader, dot_handle=dot, on_hidden=b.on_hidden))

    below = extent["bottom"]
    if vc.cut is not None and vc.cut["orient"] == "v":
        below = max(below, dr.CUT_EXT + 1.0)
    sheet.text(vc.label, (px0 + px1) / 2.0, py0 - max(below, 2.0) - 4.0 - dr.TEXT_H, dr.TEXT_H, "middle", bold=True)
    return dr.PageView(name=vc.name, kind=vc.kind, label=vc.label, scale=scale,  # type: ignore[arg-type]
                       bbox=[round(px0, 3), round(SHEET_H - py1, 3), round(px1, 3), round(SHEET_H - py0, 3)])


def _remark(line: BomLine) -> str:
    parts = [line.procurement.upper()]
    if line.representation != "exact":
        parts.append(line.representation.upper())
    return " · ".join(parts)


MISSING_PREFIX = "missing:"
_STAGE_TEXT = {"extraction": "NOT RECOGNISED", "structure": "NOT UNDER ROOT", "modeling": "NO GEOMETRY", "placement": "NOT PLACED",
               "assembly": "LEFT OUT"}


def _partial_bom(ctx: _Ctx) -> bool:
    return ctx.partial is not None and ctx.cfg.bom_view == "flattened"


def _table_rows(ctx: _Ctx) -> list[BomLine]:
    """The BOM table rows: the BOM lines and, for a partial assembly (flattened view), one row per target item with nothing
    included, under its target item number (generation supplement B-03)."""
    if not _partial_bom(ctx):
        return list(ctx.rows)
    have = {r.item for r in ctx.rows}
    extra = [BomLine(item=str(t["item"]), level=0, def_key=f"{MISSING_PREFIX}{t.get('key')}", def_kind="part",
                     name=str(t.get("name") or t.get("key") or ""), number=t.get("number"), material=None, procurement="unknown",
                     representation="exact", quantity=0, total_quantity=0)
             for t in (ctx.partial or {}).get("target_bom") or [] if isinstance(t, dict) and t.get("status") == "missing"
             and str(t.get("item")) not in have]
    return sorted([*ctx.rows, *extra], key=lambda r: _item_key(r.item))


def _target_of(ctx: _Ctx, line: BomLine) -> dict[str, Any] | None:
    for t in (ctx.partial or {}).get("target_bom") or []:
        if isinstance(t, dict) and (t.get("revision_id") == line.def_key or f"{MISSING_PREFIX}{t.get('key')}" == line.def_key):
            return t
    return None


def _bom_table(ctx: _Ctx, sheet: _AsmSheet, rows: list[BomLine]) -> list[BomRowRecord]:
    """BOM table above the title block: header row at the bottom, items upwards (GB/T 10609.2 order). A partial assembly adds
    the target quantity; a missing row keeps its target item number and states why (no balloon points to it)."""
    partial = _partial_bom(ctx)
    columns = PARTIAL_BOM_COLUMNS if partial else BOM_COLUMNS
    x0, y0 = SHEET_W - MARGIN - TITLE_W, MARGIN + TITLE_H
    xs = [x0]
    for _, _, w in columns:
        xs.append(xs[-1] + w)
    top = y0 + BOM_ROW_H * (len(rows) + 1)
    for x in xs:
        sheet.polyline([(x, y0), (x, top)], "BORDER", "border", svg_class="thin", lineweight=25)
    for k in range(len(rows) + 2):
        y = y0 + k * BOM_ROW_H
        sheet.polyline([(x0, y), (xs[-1], y)], "BORDER", "border", svg_class="thin", lineweight=25 if 0 < k < len(rows) + 1 else 50)
    for (_, head, w), x in zip(columns, xs):
        sheet.text_h(_fit(head, w - 1.5, BOM_HEAD_H), x + w / 2.0, y0 + (BOM_ROW_H - BOM_HEAD_H) / 2.0, BOM_HEAD_H, "middle", layer="BOM")
    out: list[BomRowRecord] = []
    flat = ctx.cfg.bom_view == "flattened"
    for k, line in enumerate(rows):
        yb = y0 + BOM_ROW_H * (k + 1) + (BOM_ROW_H - BOM_TEXT_H) / 2.0
        qty = line.total_quantity if flat else line.quantity
        indent = "" if flat else "· " * max(0, line.level - 1)
        missing_row = line.def_key.startswith(MISSING_PREFIX)
        values = {"item": line.item, "number": line.number or "", "name": indent + line.name, "qty": str(qty),
                  "material": line.material or ("—" if line.def_kind == "assembly" or missing_row else "UNKNOWN"), "remark": _remark(line)}
        target_q: int | None = None
        status: Literal["partial", "missing"] | None = None
        if partial:
            t = _target_of(ctx, line) or {}
            target_q = int(t.get("quantity") or qty)
            values["target"] = str(target_q)
            if missing_row:
                status = "missing"
                values["remark"] = f"缺失 · {_STAGE_TEXT.get(str(t.get('stage')), 'NOT BUILT')}"
            elif qty < target_q:
                status = "partial"
                values["remark"] = f"部分 · {_remark(line)}"
        cells: dict[str, dict[str, str]] = {}
        for (col, _, w), x in zip(columns, xs):
            h = BOM_TEXT_H
            while h > 0.75 * BOM_TEXT_H + 1e-9 and dr._text_width(values[col], h) > w - 2.0:
                h = round(h - 0.1, 2)  # a slightly smaller text before any truncation
            text = _fit(values[col], w - 2.0, h)
            if not text:
                continue
            centred = col in ("item", "qty", "target")
            handle = sheet.text_h(text, x + (w / 2.0 if centred else 1.0), yb, h, "middle" if centred else "start", layer="BOM")
            cells[col] = {"text": text, "handle": handle}
        out.append(BomRowRecord(item=line.item, level=line.level, def_key=line.def_key, def_kind=line.def_kind, name=line.name,
                                number=line.number, material=line.material, procurement=line.procurement,
                                representation=line.representation, quantity=line.quantity, total_quantity=line.total_quantity,
                                shown_quantity=qty, cells=cells, target_quantity=target_q, status=status))
    return out


def _title_block(ctx: _Ctx, sheet: _AsmSheet, scale: float | None, sheet_no: int, sheets: int, kind: str) -> None:
    g, t = ctx.geom, ctx.cfg.title
    x0, y0 = SHEET_W - MARGIN - TITLE_W, MARGIN
    rh = TITLE_H / 5.0
    sheet.polyline([(x0, y0), (x0 + TITLE_W, y0), (x0 + TITLE_W, y0 + TITLE_H), (x0, y0 + TITLE_H)], "BORDER", "border", closed=True)
    proj = "THIRD ANGLE" if ctx.cfg.projection == "third" else "FIRST ANGLE"
    rows: list[list[tuple[float, str, str]]] = [  # bottom row first: (width, label, value)
        [(180.0, "ASSEMBLY REVISION (SOURCE)", g.key)],
        [(60.0, "DRAWN", t.author), (30.0, "DATE", t.date), (30.0, "UNITS", "mm"), (60.0, "SHEET", f"{sheet_no} / {sheets}")],
        [(90.0, "MATERIAL", "SEE BOM 见明细表"), (90.0, "PROJECT", t.project)],
        [(60.0, "DRAWING NO.", t.drawing_number or g.number or ""), (30.0, "REV", g.revision_label),
         (30.0, "SCALE", dr._scale_text(scale) if scale else "—"), (60.0, "PROJECTION", proj)],
        [(120.0, "ASSEMBLY NAME", g.name), (60.0, "", kind)],
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
                sheet.text(label, xc + 1.2, yb + rh - dr.SMALL_H - 0.9, dr.SMALL_H)
            if value:
                big = ri == 4
                h = 3.2 if big else 2.6
                sheet.text(_fit(value, w - 2.4, h), xc + 1.2 if label else xc + w / 2.0, yb + 1.4, h,
                           "start" if label else "middle", bold=big or not label)
            xc += w


def _ga_cells(projection: str, names: list[str]) -> dict[str, tuple[int, int]]:
    if projection == "third":
        base, extra = {"top": (0, 0), "front": (1, 0), "right": (1, 1)}, [(0, 1), (0, 2), (1, 2)]
    else:
        base, extra = {"right": (0, 0), "front": (0, 1), "top": (1, 1)}, [(1, 0), (0, 2), (1, 2)]
    cells = {n: base[n] for n in names if n in base}
    free = [c for c in extra if c not in cells.values()]
    for n in names:
        if n not in cells:
            cells[n] = free.pop(0)
    return cells


@dataclass
class _GA:
    vcs: list[_AsmVC]
    section: SectionRecord | None


def _ga_views(ctx: _Ctx) -> _GA:
    names: list[str] = []
    for v in ctx.cfg.views:
        if v not in names:
            names.append(v)
    vcs = {n: _projection_vc(ctx, n) for n in names}
    section: SectionRecord | None = None
    plan = _plan_section(ctx)
    if plan is not None:
        made = _section_vc(ctx, plan, "A")
        if made is not None:
            svc, section = made
            section.cut_on = _cut_line(ctx, vcs, plan, "A")
            if section.cut_on is None:
                ctx.warnings.append(Issue(code="cutting_line_missing", message="no projection shows the cutting plane edge-on"))
            vcs[svc.name] = svc
    ordered = [vcs[n] for n in names] + [v for v in vcs.values() if v.kind == "section"]
    _overall_dims(ctx, vcs)
    if ctx.cfg.pitch_dimensions:
        _pitch_dims(ctx, vcs)
    _key_dims(ctx, vcs)
    _plan_balloons(ctx, ordered)
    return _GA(ordered, section)


def _partial_notes(ctx: _Ctx) -> list[str]:
    """Partial-assembly notes first on the GA sheet (generation supplement B-03): what the revision holds of its target, what is
    missing (target item, stage, reason; the full list is in the package / delivery coverage) and the AI decision count."""
    p = ctx.partial or {}
    cov = p.get("coverage") or {}
    t, placed, shown = cov.get("target") or {}, cov.get("placed") or {}, cov.get("arranged") or {}
    head = (f"{PARTIAL_KIND}: {placed.get('instances', 0)} OF {t.get('instances', 0)} PART INSTANCES INSTALLED, "
            f"{(cov.get('built') or {}).get('definitions', 0)} OF {t.get('definitions', 0)} PART DEFINITIONS WITH GEOMETRY"
            + (f", {shown.get('instances')} ONLY ARRANGED FOR DISPLAY (NOT INSTALLED)" if shown.get("instances") else "") + ".")
    items = {str(x.get("key")): str(x.get("item")) for x in p.get("target_bom") or [] if isinstance(x, dict)}
    missing = [m for m in cov.get("missing") or [] if isinstance(m, dict)]
    lines = []
    for m in missing[:MAX_PARTIAL_NOTE_ITEMS]:
        ref = items.get(str(m.get("key"))) or items.get(str(m.get("definition")))
        lines.append((f"ITEM {ref} " if ref else "") + f"{m.get('name') or m.get('key')} ({str(m.get('stage') or '').upper()}: {m.get('code')})")
    notes = [head]
    if missing:
        more = len(missing) - len(lines)
        notes.append(f"MISSING 缺件 ({len(missing)}): " + "; ".join(lines) + (f"; +{more} MORE" if more > 0 else "")
                     + ". SEE THE COVERAGE LIST (report/coverage.json).")
    n = int(p.get("ai_decisions") or 0) + len(ctx.assumptions)
    if n:
        notes.append(f"AI DECISIONS 含 AI 判断: {n}; NOT CONFIRMED BY A PERSON UNLESS RECORDED (report/ai_decisions.md).")
    return notes


def _ga_notes(ctx: _Ctx, section: SectionRecord | None, bom_sheets: list[int]) -> list[str]:
    s = ctx.size
    notes = _partial_notes(ctx) if ctx.partial is not None else []
    notes += [f"DIMENSIONS IN mm. OVERALL ENVELOPE {dr._fmt(s['x'])} × {dr._fmt(s['y'])} × {dr._fmt(s['z'])} (X × Y × Z).",
             f"ITEM NUMBERS (序号) REFER TO THE {ctx.cfg.bom_view.upper()} BOM (明细表"
             + (f" ON SHEET {', '.join(str(i) for i in bom_sheets)}" if bom_sheets else "")
             + f"); QUANTITIES FROM THE INSTANCE TREE OF ASSEMBLY REVISION {ctx.geom.revision_label}."]
    if section is not None:
        sight = "+Y" if section.plane == "XZ" else "-X"
        notes.append(f"SECTION {section.letter}-{section.letter}: PLANE {section.plane} AT {'Y' if section.plane == 'XZ' else 'X'} = "
                     f"{dr._fmt(section.offset)} ({section.chosen.upper()}), VIEWED TOWARDS {sight}; EACH CUT COMPONENT HATCHED "
                     "SEPARATELY (ANSI31; ADJACENT PARTS DIFFER IN ANGLE OR SPACING); HIDDEN EDGES NOT SHOWN.")
        if section.not_sectioned:
            by_rule: dict[str, list[str]] = collections.defaultdict(list)
            for ns in section.not_sectioned:
                by_rule[ns["rule"]].append(str(ns["item"]))
            notes.append("NOT SECTIONED BY CONVENTION: " + "; ".join(
                f"ITEM {', '.join(sorted(set(v), key=_item_key))} ({SECTION_RULE_LABELS[k]})" for k, v in sorted(by_rule.items())) + ".")
    reps = sorted({(ln.item, ln.representation) for ln in ctx.lines["flattened"] if ln.representation != "exact"}, key=lambda t: _item_key(t[0]))
    if reps:
        notes.append("REPRESENTATION: " + "; ".join(f"ITEM {i} {r.upper()} (NOT EXACT GEOMETRY)" for i, r in reps) + ".")
    keys = [kd for kd in ctx.cfg.key_dimensions if kd.label]
    if keys:
        notes.append("INTERFACE DIMENSIONS: " + "; ".join(f"{kd.id} {kd.label}" for kd in keys) + ".")
    if ctx.poly_items:  # which items the polygonal HLR drew (coils, and what overlaps them in a view)
        def listed(keys: Any) -> str:
            return ", ".join(sorted({ctx.items[k] for k in keys if k in ctx.items}, key=_item_key))

        coils = [k for k in ctx.poly_items if ctx.defs[k].coil]
        others = [k for k in ctx.poly_items if not ctx.defs[k].coil]
        notes.append(dr.coil_note(ctx.lin, f" (ITEM {listed(coils)}" + (f"; WITH ITEM {listed(others)} WHERE THEY OVERLAP IN A VIEW"
                                                                        if others else "") + ")"))
    notes.append("MATERIAL: SEE BOM 见明细表.")
    notes += [f"TECHNICAL REQUIREMENT 技术要求: {n}" for n in ctx.cfg.notes]
    return [f"{i + 1}. {n}" for i, n in enumerate(notes)]


def _sheet_page(sheet: _AsmSheet) -> tuple[bytes, bytes]:
    return sheet.dxf_bytes(), sheet.svg_bytes()


def _ga_document(ctx: _Ctx) -> list[AssemblyDrawingPage]:
    ga = _ga_views(ctx)
    rows = _table_rows(ctx)  # BOM lines (+ the missing target items of a partial assembly)
    on_ga = len(rows) <= MAX_GA_BOM_ROWS
    chunks = [] if on_ga else [rows[i:i + MAX_BOM_SHEET_ROWS] for i in range(0, len(rows), MAX_BOM_SHEET_ROWS)]
    sheets = 1 + len(chunks)
    notes = _ga_notes(ctx, ga.section, list(range(2, sheets + 1)))
    _, notes_top = dr._notes_lines(notes)
    table_top = MARGIN + TITLE_H + (BOM_ROW_H * (len(rows) + 1) if on_ga else 0.0)
    area = (MARGIN, max(notes_top, table_top) + 6.0, SHEET_W - MARGIN, SHEET_H - MARGIN)
    cells = _ga_cells(ctx.cfg.projection, [vc.name for vc in ga.vcs])
    margins = {vc.name: _margins(vc) for vc in ga.vcs}
    scale, origins = dr._layout(ga.vcs, cells, area, ctx.cfg, ctx.warnings, "GA sheet 1", margins=margins)  # type: ignore[arg-type]
    name = " ".join(x for x in (ctx.geom.number or "", ctx.geom.name) if x) or "assembly"
    sheet = _AsmSheet(f"{name} — GENERAL ARRANGEMENT", scale)
    sheet.polyline([(MARGIN, MARGIN), (SHEET_W - MARGIN, MARGIN), (SHEET_W - MARGIN, SHEET_H - MARGIN), (MARGIN, SHEET_H - MARGIN)],
                   "BORDER", "border", closed=True)
    dims: list[AsmDimension] = []
    balloons: list[BalloonRecord] = []
    hatches: list[HatchRecord] = []
    views = [_render_view(ctx, sheet, vc, origins[vc.name], scale, 1, dims, balloons, hatches) for vc in ga.vcs]
    dr._notes_block(sheet, notes)
    bom_rows = _bom_table(ctx, sheet, rows) if on_ga else []
    _title_block(ctx, sheet, scale, 1, sheets, PARTIAL_TITLE if ctx.partial is not None else "PartRoute GA 总装图")
    dxf, svg = _sheet_page(sheet)
    pages = [AssemblyDrawingPage(
        index=0, key="GA-1", document="GA", role="assembly", title="GENERAL ARRANGEMENT", sheet=1, sheets=sheets, scale=scale,
        views=views, dimensions=[d.model_dump(mode="json") for d in dims], balloons=balloons, hatches=hatches,
        hatch_count=sheet.hatch_count, section=ga.section.model_dump(mode="json") if ga.section else None, bom_rows=bom_rows,
        notes=notes, files=_files(dxf, svg))]
    for k, chunk in enumerate(chunks):
        bs = _AsmSheet(f"{name} — BOM {k + 1}", 1.0)
        bs.polyline([(MARGIN, MARGIN), (SHEET_W - MARGIN, MARGIN), (SHEET_W - MARGIN, SHEET_H - MARGIN), (MARGIN, SHEET_H - MARGIN)],
                    "BORDER", "border", closed=True)
        rec = _bom_table(ctx, bs, chunk)
        bnotes = [f"1. BOM (明细表) OF ASSEMBLY {name} REV {ctx.geom.revision_label}, SHEET {k + 2} OF {sheets}."]
        if ctx.partial is not None:
            bnotes.append(f"2. {PARTIAL_KIND}: MISSING ROWS KEEP THEIR TARGET ITEM NUMBER AND HAVE NO BALLOON.")
        dr._notes_block(bs, bnotes)
        _title_block(ctx, bs, None, k + 2, sheets, PARTIAL_TITLE if ctx.partial is not None else "PartRoute BOM 明细表")
        dxf, svg = _sheet_page(bs)
        pages.append(AssemblyDrawingPage(index=0, key=f"GA-{k + 2}", document="GA", role="bom", title=f"BOM {k + 1}", sheet=k + 2,
                                         sheets=sheets, bom_rows=rec, notes=bnotes, files=_files(dxf, svg)))
    return pages


def _files(dxf: bytes, svg: bytes) -> list[PageFile]:
    return [PageFile(format="dxf", path="", sha256=_sha(dxf), bytes=len(dxf), content_type=CONTENT_TYPES["dxf"],
                     version=_dxf_version(dxf), data=dxf),
            PageFile(format="svg", path="", sha256=_sha(svg), bytes=len(svg), content_type=CONTENT_TYPES["svg"], data=svg)]


_ACADVER = re.compile(rb"\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})")


def _dxf_version(data: bytes) -> str | None:
    m = _ACADVER.search(data[:4096])
    return m.group(1).decode() if m else None


# --------------------------------------------------------------------------- #
# Detail documents (existing single-part pipeline)
# --------------------------------------------------------------------------- #


@dataclass
class _Detail:
    line: BomLine
    part: PartDefinition
    package: Any  # drawing.DrawingPackage
    step: bytes


def _detail_document(ctx: _Ctx, line: BomLine) -> _Detail:
    part = ctx.geom.parts[line.def_key]
    solid = ctx.defs[part.key].solid
    holes = [HoleRecord.model_validate(h) for h in (part.meta or {}).get("holes") or []]
    # text ops (generation reliability M): the letters are drawn but left out of the feature analysis
    lettering = [FeatureRecord.model_validate(f) for f in (part.meta or {}).get("features") or [] if isinstance(f, dict) and f.get("type") == "text"]
    src = BuildResult(ok=True, build_key=part.key, kernel_version=KERNEL_VERSION, holes=holes, features=lettering)
    src._shape = solid
    t = ctx.cfg.title
    det_cfg = PackageConfig.model_validate({**ctx.cfg.detail, "title": {
        "part_number": part.number or "", "part_name": part.name, "revision": str((part.meta or {}).get("revision_label") or ""),
        "material": part.material or "UNKNOWN", "author": t.author, "date": t.date, "project": t.project}})
    g = ctx.geom
    notes = [f"DETAIL OF ASSEMBLY {' '.join(x for x in (g.number or '', g.name) if x)} REV {g.revision_label}: ITEM {line.item}, "
             f"QTY {line.total_quantity} PER ASSEMBLY.",
             f"PROCUREMENT {line.procurement.upper()}; REPRESENTATION {line.representation.upper()}"
             + ("" if line.representation == "exact" else " (NOT EXACT GEOMETRY)") + "."]
    if not part.material:
        notes.append("MATERIAL NOT STATED BY THE SOURCE (UNKNOWN).")
    pkg = make_drawing_package(src, det_cfg, notes=notes)
    return _Detail(line=line, part=part, package=pkg, step=export_step_bytes(solid))


def _detail_pages(d: _Detail) -> list[AssemblyDrawingPage]:
    pages = []
    n = len(d.package.pages)
    for p in d.package.pages:
        pages.append(AssemblyDrawingPage(
            index=0, key=f"D{d.line.item}-{p.index}", document=f"D{d.line.item}", role="detail",
            title=f"DETAIL ITEM {d.line.item} — {p.title}", sheet=p.index, sheets=n, def_key=d.part.key, item=d.line.item,
            scale=p.scale, views=list(p.views), dimensions=[x.model_dump(mode="json") for x in p.dimensions], hatch_count=p.hatches,
            section=p.section.model_dump(mode="json") if p.section else None, callouts=list(p.callouts), files=_files(p.dxf, p.svg)))
    return pages


# --------------------------------------------------------------------------- #
# DWG and checks
# --------------------------------------------------------------------------- #


def _dwg_summary(res: Any) -> dict[str, Any]:
    return {"target": res.target, "source_dxf_sha256": res.source.sha256, "dwg_sha256": res.output.sha256, "bytes": res.output.size,
            "version": res.output.version, "verified": res.verified,
            "readback": [{"reader": c.reader, "status": c.status, "entities": c.entities, "entities_matched": c.entities_matched,
                          "dimensions": c.dimensions, "dimensions_matched": c.dimensions_matched, "reason": c.reason}
                         for c in res.readback],
            "losses": sorted({x.code for x in res.losses})}


def _convert(page: AssemblyDrawingPage, pol: str, runtime: Any, timeout: float) -> dict[str, Any]:
    """DWG R2000 of one page. Standard: the conversion runs inside the ``drawing.dwg_semantic`` check (with the
    same-engine and independent readback); Turbo: the conversion only. Returns the semantic check record."""
    holder: dict[str, Any] = {}

    def semantic() -> dict[str, Any]:
        try:
            res = write_dwg(page.dxf, target=DWG_TARGET, runtime=runtime, timeout_s=timeout, require_independent=False)
        except DwgError as exc:
            holder["error"] = exc
            status = "failed" if exc.code == "dwg_readback_mismatch" else "unknown"
            return {"check": "drawing.dwg_semantic", "status": status, "code": exc.code}
        holder["res"] = res
        return {"check": "drawing.dwg_semantic", "status": "passed", "verified": res.verified,
                "readers": {c.reader: c.status for c in res.readback}}

    rec = exec_policy.run_check("drawing.dwg_semantic", pol, semantic)
    if rec["status"] == "skipped_turbo":
        try:
            holder["res"] = write_dwg(page.dxf, target=DWG_TARGET, runtime=runtime, timeout_s=timeout, require_independent=False,
                                      readback=False)
        except DwgError as exc:
            holder["error"] = exc
    res = holder.get("res")
    if res is not None and (not res.dwg or res.dwg[:6] != DWG_TARGET.encode() or _sha(res.dwg) != res.output.sha256):
        holder["error"] = DwgError("dwg_conversion_failed", "the converter returned an empty or mislabelled DWG")
        res = None
    if res is None:
        exc = holder["error"]
        page.errors.append({"format": "dwg", "code": exc.code, "message": exc.message[:300]})
        page.dwg_report = {"status": "failed", "code": exc.code}
        return rec
    page.files.append(PageFile(format="dwg", path="", sha256=res.output.sha256, bytes=len(res.dwg), content_type=CONTENT_TYPES["dwg"],
                               version=DWG_TARGET, data=res.dwg))
    page.dwg_report = {"status": "written", **_dwg_summary(res)}
    return rec


def _dxf_doc(data: bytes) -> Any:
    doc, _ = recover.read(io.BytesIO(data))
    return doc


def _check_assembly(ctx: _Ctx, pages: list[AssemblyDrawingPage], items: list[AnnotationItem]) -> None:
    """Assembly-specific annotation check of the GA document, from the files (never the generator's state)."""

    def add(iid: str, check: str, ok: bool, message: str, page: int | None = None, expected: Any = None, measured: Any = None) -> None:
        items.append(AnnotationItem(id=iid, check=check, page=page, status="passed" if ok else "failed", expected=expected,
                                    measured=measured, message=message))

    ga = next((p for p in pages if p.role == "assembly"), None)
    if ga is None:
        add("GA:present", "assembly_sheet", False, "the general-arrangement sheet is missing")
        return
    P = ga.index
    doc = _dxf_doc(ga.dxf)
    msp = doc.modelspace()
    inserts = [e for e in msp.query("INSERT") if e.dxf.layer.upper() == "BALLOON"]
    found: collections.Counter[str] = collections.Counter()
    for ins in inserts:
        blk = doc.blocks.get(ins.dxf.name)
        texts = [e.dxf.text for e in blk if e.dxftype() == "TEXT"] if blk is not None else []
        circles = [e for e in blk if e.dxftype() == "CIRCLE"] if blk is not None else []
        ok = len(texts) == 1 and len(circles) == 1
        add(f"P{P}:balloon:{ins.dxf.handle}", "balloon_block", ok,
            f"balloon INSERT {ins.dxf.name}: {len(circles)} circle(s), text {texts}", page=P)
        if texts:
            found[texts[0]] += 1
    expected = collections.Counter({r.item: (len(ctx.targets(r)) if ctx.cfg.balloon_per_instance else 1) for r in ctx.rows})
    add(f"P{P}:balloons:bom", "balloons_bom", found == expected,
        "every BOM item has its balloon(s) and every balloon names a BOM item" if found == expected
        else f"balloons {dict(sorted(found.items()))} vs BOM {dict(sorted(expected.items()))}",
        page=P, expected=dict(sorted(expected.items())), measured=dict(sorted(found.items())))
    by_handle = {e.dxf.handle: e for e in msp}
    for b in ga.balloons:
        ins = by_handle.get(b.handle)
        leader = by_handle.get(b.leader_handle)
        ok = ins is not None and ins.dxftype() == "INSERT" and leader is not None and leader.dxftype() == "LINE"
        if ok:
            end = (leader.dxf.end[0], leader.dxf.end[1])
            ok = math.dist(end, tuple(b.anchor)) < 1e-3
            view = next((v for v in ga.views if v.name == b.view), None)
            if view is not None:
                x0, y0, x1, y1 = view.bbox
                ok = ok and x0 - 0.5 <= b.anchor[0] <= x1 + 0.5 and (SHEET_H - y1) - 0.5 <= b.anchor[1] <= (SHEET_H - y0) + 0.5
        add(f"P{P}:balloon_record:{b.id}", "balloon_leader", ok, f"balloon {b.item}: INSERT, leader ending on the item inside view {b.view}",
            page=P)
    root = ET.fromstring(ga.svg)
    svg_balloons = [g for g in root.iter(f"{dr_svg_ns}g") if g.get("class") == "balloon"]
    add(f"P{P}:svg:balloons", "svg_dxf_parity", len(svg_balloons) == len(inserts),
        f"SVG balloons {len(svg_balloons)}, DXF balloon INSERTs {len(inserts)}", page=P)

    # BOM table rows: the TEXT entities say what the rows say; quantities match the instance tree
    table_pages = [p for p in pages if p.bom_rows]
    rows_seen = []
    for p in table_pages:
        d = doc if p is ga else _dxf_doc(p.dxf)
        hmap = {e.dxf.handle: e for e in d.modelspace().query("TEXT")}
        for r in p.bom_rows:
            bad = [c for c, v in r.cells.items() if hmap.get(v["handle"]) is None or hmap[v["handle"]].dxf.text != v["text"]]
            add(f"P{p.index}:bom:{r.item}", "bom_table", not bad, f"明细表 item {r.item}: " + ("cells match" if not bad else f"cells {bad} differ"),
                page=p.index)
            if r.status == "missing":  # a partial assembly's missing target item: no BOM line, no instance, no balloon
                continue
            rows_seen.append(r.item)
            if ctx.cfg.bom_view == "flattened":
                n = sum(1 for o in ctx.occs if o.part.key == r.def_key)
                add(f"P{p.index}:bom_qty:{r.item}", "bom_quantity", r.shown_quantity == n and r.cells.get("qty", {}).get("text") == str(n),
                    f"item {r.item} quantity {r.cells.get('qty', {}).get('text')} vs {n} placed instances", page=p.index, expected=n,
                    measured=r.shown_quantity)
    add("GA:bom:rows", "bom_table", sorted(rows_seen, key=_item_key) == sorted((r.item for r in ctx.rows), key=_item_key),
        f"明细表 rows {len(rows_seen)} vs BOM lines {len(ctx.rows)}")

    # section: one hatch set per cut component, adjacent components differ
    sec = ga.section
    if sec is not None:
        hatches = {e.dxf.handle: e for e in msp.query("HATCH")}
        style_of = {"/".join(h.occurrence): (h.angle, h.scale) for h in ga.hatches}
        for c in sec["cut"]:
            ps = "/".join(c["occurrence"])
            rec = next((h for h in ga.hatches if "/".join(h.occurrence) == ps), None)
            ok = rec is not None and rec.handles and all(
                (h := hatches.get(x)) is not None and h.dxf.layer.upper() == "HATCH" and str(h.dxf.pattern_name).upper() == "ANSI31"
                and abs(float(h.dxf.pattern_angle) - rec.angle) % 180.0 < 1e-6
                and abs(float(h.dxf.pattern_scale) - rec.scale) < 1e-9 for x in rec.handles)
            add(f"P{P}:section:hatch:{ps}", "section_hatch", bool(ok), f"cut component {ps} (item {c['item']}): "
                + (f"{len(rec.handles)} ANSI31 hatch(es) at {rec.angle}° offset, scale {rec.scale}" if rec else "no hatch"), page=P)
            if rec is not None:
                clash = [a for a in rec.adjacent if style_of.get(a) == (rec.angle, rec.scale)]
                add(f"P{P}:section:distinct:{ps}", "section_hatch_distinct", not clash,
                    f"{ps}: adjacent cut components with the same hatch: {clash}" if clash else f"{ps}: hatch differs from its neighbours",
                    page=P)
        texts = [e.dxf.text for e in msp.query("TEXT")]
        label = f"SECTION {sec['letter']}-{sec['letter']}"
        add(f"P{P}:section:labels", "section_labels", label in texts and texts.count(sec["letter"]) >= 2,
            f"'{label}' and two cutting-line letters", page=P)

    # envelope: overall dimensions against an independent tight bounding box of the placed solids
    from .bounds import tight_bounds

    boxes = [tight_bounds(o.shape) for o in ctx.occs]
    lo3 = [min(b[0][k] for b in boxes) for k in range(3)] if boxes else [0.0] * 3
    hi3 = [max(b[1][k] for b in boxes) for k in range(3)] if boxes else [0.0] * 3
    size = {"x": hi3[0] - lo3[0], "y": hi3[1] - lo3[1], "z": hi3[2] - lo3[2]}
    overall = [d for d in ga.dimensions if d["kind"] == "overall"]
    for axis in "xyz":
        mine = [d for d in overall if d["axis"] == axis]
        ok = bool(mine) and all(abs(d["value"] - size[axis]) <= 0.01 for d in mine)
        add(f"P{P}:envelope:{axis}", "envelope", ok, f"overall {axis.upper()} " + (", ".join(d["text"] for d in mine) or "missing")
            + f" vs bounding box {dr._fmt(size[axis])}", page=P, expected=_r(size[axis], 6), measured=mine[0]["value"] if mine else None)


dr_svg_ns = "{http://www.w3.org/2000/svg}"


def _entry_failures(entry: dict[str, Any]) -> int:
    """Failed items of one document entry of an annotation report (its sheets and, for the GA, the assembly check)."""
    items = list(((entry.get("sheets") or {}).get("items")) or []) + list(((entry.get("assembly") or {}).get("items")) or [])
    return sum(1 for i in items if isinstance(i, dict) and i.get("status") == "failed")


def _annotation(ctx: _Ctx, ga_pages: list[AssemblyDrawingPage], details: list[_Detail],
                documents: list[dict[str, Any]] | None = None, reused: dict[str, Any] | None = None) -> tuple[dict[str, Any], bytes]:
    """The ``drawing.annotation`` check of the package: the existing per-page annotation check on every sheet
    (detail sheets against their part's STEP) plus the assembly check of the GA document. A required document that
    produced no sheet (a failed GA or detail document) fails the check: nothing of it was checked. ``reused``: the
    report entries of documents taken unchanged (same bytes) from the package being continued (S3)."""
    docs: dict[str, Any] = {}
    failed = 0
    for doc in documents or []:
        if not doc.get("sheets"):
            docs[doc["key"]] = {"missing": True, "error": doc.get("error") or {"code": "no_sheets", "message": "no sheet was produced"}}
            failed += 1
    for key, entry in (reused or {}).items():
        docs[key] = entry
        failed += _entry_failures(entry)
    if ga_pages and "GA" not in (reused or {}):
        rep = check_package(ga_pages)
        asm: list[AnnotationItem] = []
        _check_assembly(ctx, ga_pages, asm)
        docs["GA"] = {"sheets": rep.summary(), "assembly": {"schema": ASM_CHECK_SCHEMA, "items": [i.model_dump(mode="json") for i in asm]}}
        failed += len(rep.failures()) + sum(1 for i in asm if i.status == "failed")
    for d in details:
        rep = check_package(d.package.pages, step_bytes=d.step, features=(d.part.meta or {}).get("features"))
        summary = rep.summary()
        # the reference STEP is exported in memory for this check only; its bytes are not stable across
        # rebuilds of the same definition, so the report names the definition instead of a volatile hash
        summary["step_sha256"] = None
        summary["reference"] = {"def_key": d.part.key, "step": "exported in memory from the definition shape"}
        docs[f"D{d.line.item}"] = {"sheets": summary}
        failed += len(rep.failures())
    status = "failed" if failed else "passed"
    report = _json({"schema": "partroute.assembly_drawing_checks/1", "check": "drawing.annotation", "status": status,
                    "failed": failed, "documents": docs})
    return {"check": "drawing.annotation", "status": status, "failed": failed}, canonical_json(report).encode("utf-8")


# --------------------------------------------------------------------------- #
# Package assembly
# --------------------------------------------------------------------------- #


def _runtime(runtime: Any) -> Any:
    if runtime is not None:
        return runtime
    from .dwg import dwg_runtime

    return dwg_runtime()


def _finish(pages: list[AssemblyDrawingPage], documents: list[dict[str, Any]], formats: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """Paths, page statuses and completeness. A required document without sheets or a sheet without a required
    format makes the package partial."""
    errors: list[dict[str, Any]] = []
    for p in pages:
        stem = f"pages/p{p.index:02d}_{_safe(p.key)}"
        for f in p.files:
            f.path = f"{stem}.{f.format}"
            if f.bytes == 0 or not f.data:
                p.errors.append({"format": f.format, "code": "empty_file", "message": f"{f.format} is empty"})
        p.files = [f for f in p.files if f.bytes > 0 and f.data]
        p.files.sort(key=lambda f: FORMATS.index(f.format))
        have = {f.format for f in p.files}
        missing = [f for f in formats if f not in have]
        p.status = "complete" if not missing and not p.errors else ("failed" if "dxf" in missing else "partial")
        for e in p.errors:
            errors.append({"page": p.key, "document": p.document, **e})
    for d in documents:
        if d.get("error"):
            errors.append({"page": None, "document": d["key"], **d["error"]})
    complete = all(p.status == "complete" for p in pages) and all(not d.get("error") and d["sheets"] > 0 for d in documents)
    return ("complete" if complete else "partial"), errors


def _manifest(ctx_src: dict[str, Any], pkg: dict[str, Any], pages: list[AssemblyDrawingPage]) -> tuple[dict[str, Any], str]:
    manifest = _json({
        **pkg,
        "pages": [{"index": p.index, "key": p.key, "document": p.document, "role": p.role, "title": p.title, "sheet": p.sheet,
                   "sheets": p.sheets, "def_key": p.def_key, "item": p.item, "status": p.status,
                   "files": [{k: v for k, v in f.model_dump(mode="json").items() if v is not None} for f in p.files],
                   "missing": [f for f in pkg["required"]["formats"] if p.file(f) is None],
                   "errors": p.errors, "dwg": p.dwg_report, "overlay": p.overlay or None} for p in pages],
        "source": ctx_src,
    })
    return manifest, sha256_hex(manifest)


def make_assembly_drawing_package(
    geom: AssemblyGeometry,
    bom: dict[str, Any],
    config: AssemblyDrawingConfig | dict[str, Any] | None = None,
    policy: str | None = "standard",
    *,
    dwg_runtime: Any = None,
    page_budget_s: float | None = None,
    upstream: dict[str, Any] | None = None,
    reuse: AssemblyDrawingPackage | None = None,
) -> AssemblyDrawingPackage:
    """Generate the GA + detail drawing package of one exact assembly revision.

    ``reuse`` (S3): a partial package of the same revision, configuration, policy and generator (the caller checks
    that and restores it hash-verified). Its documents that came out whole are kept byte for byte with their check
    records (:func:`_reusable_documents`); sheets that only lack their DWG are converted again; every other document
    is generated. ``package.progress`` says what happened to each document and page.

    ``upstream``: the verification state of that revision (see :func:`_verification`); it goes into the manifest's
    ``verification`` before hashing, so every export says whether the drawn model is verified.

    ``bom``: ``{"hierarchical": [BomLine], "flattened": [BomLine]}`` (objects or dicts) of the same revision.
    ``policy``: ``standard`` | ``turbo`` (W0 §2). ``dwg_runtime``: a :class:`~app.cad.results.DwgRuntime`
    (services pass the configured one; default: probe PATH). Raises :class:`AssemblyDrawingError` when the
    inputs cannot be drawn at all (structure, BOM ↔ instance tree, configuration); a page that fails is
    reported in the package (``completeness = "partial"``) instead.

    ``page_budget_s`` enforces the W0 §7 sheet budget: a document whose sheets took longer than that many
    seconds per sheet fails with ``page_budget_exceeded`` and its sheets are withheld (the package is partial).
    Without it an overrun is only a warning."""
    pol = exec_policy.parse_policy(policy)
    cfg = _config(config)
    lines = _lines(bom)
    ctx = _context(geom, lines, cfg)
    # generation supplement B-03: a partial assembly revision is drawn and marked as such (title block, notes, BOM)
    if isinstance((upstream or {}).get("partial"), dict):
        ctx.partial = dict(upstream["partial"])  # type: ignore[index]
        upstream = {k: v for k, v in (upstream or {}).items() if k != "partial"}

    # ---- the delivery list first: GA + one detail document per drawn definition
    detail_lines = []
    if cfg.details:
        for ln in sorted(lines["flattened"], key=lambda x: _item_key(x.item)):
            if ln.procurement in cfg.detail_procurements:
                detail_lines.append(ln)
                if ln.procurement == "unknown":
                    ctx.assumptions.append(exec_policy.Assumption(
                        id=f"A-{len(ctx.assumptions) + 1:02d}", subject=ln.number or ln.name or ln.def_key, field="procurement",
                        adopted="make (detail sheet drawn)",
                        basis=f"procurement of {ln.number or ln.name} is unknown; a detail sheet is drawn so no manufactured part is missed",
                        alternatives=["buy", "standard"], affected=[f"D{ln.item}", ln.def_key], author="rule", at=cfg.title.date,
                        trigger="missing").model_dump(mode="json"))
    documents: list[dict[str, Any]] = [{"key": "GA", "role": "assembly", "sheets": 0}]
    documents += [{"key": f"D{ln.item}", "role": "detail", "def_key": ln.def_key, "item": ln.item, "sheets": 0} for ln in detail_lines]

    kept, kept_checks = _reusable_documents(reuse, pol) if reuse is not None else ({}, {})
    prev_sem = dict(((reuse.checks.get("drawing.dwg_semantic") or {}).get("pages")) or {}) if reuse is not None else {}
    doc_progress: dict[str, str] = {}
    page_progress: dict[str, str] = {}
    pages: list[AssemblyDrawingPage] = []
    started = time.monotonic()
    if "GA" in kept:
        ga_pages = [_reused_page(p) for p in kept["GA"]]
    else:
        try:
            ga_pages = _ga_document(ctx)
        except AssemblyDrawingError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed sheet keeps the rest of the package
            ga_pages = []
            documents[0]["error"] = {"code": "sheet_generation_failed", "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
        over = _budget(ctx, "GA", started, len(ga_pages), page_budget_s)
        if over is not None and ga_pages:
            documents[0]["error"] = over
            ga_pages = []
    pages += ga_pages
    documents[0]["sheets"] = len(ga_pages)
    doc_progress["GA"] = "reused" if "GA" in kept else ("generated" if ga_pages else "failed")
    details: list[_Detail] = []
    for doc, ln in zip(documents[1:], detail_lines):
        if doc["key"] in kept:  # S3: generated whole by the package being continued
            dp = [_reused_page(p) for p in kept[doc["key"]]]
            doc["sheets"] = len(dp)
            pages += dp
            doc_progress[doc["key"]] = "reused"
            continue
        doc_progress[doc["key"]] = "failed"
        started = time.monotonic()
        try:
            det = _detail_document(ctx, ln)
        except Exception as exc:  # noqa: BLE001 - DrawingError or a kernel failure: this document only
            code = "drawing_failed" if isinstance(exc, DrawingError) else "sheet_generation_failed"
            doc["error"] = {"code": code, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
            continue
        dp = _detail_pages(det)
        over = _budget(ctx, doc["key"], started, len(dp), page_budget_s)
        if over is not None:
            doc["error"] = over
            continue
        details.append(det)
        doc["sheets"] = len(dp)
        pages += dp
        doc_progress[doc["key"]] = "generated"
    for i, p in enumerate(pages):
        p.index = i + 1
        for b in p.balloons:
            b.id = re.sub(r"^P\d+-", f"P{p.index}-", b.id)
        page_progress[p.key] = "reused" if doc_progress.get(p.document) == "reused" else "generated"
    if reuse is not None:  # warnings of the documents kept (layout notes); the regenerated ones add their own
        ctx.warnings += [w for w in reuse.warnings if (w.details or {}).get("document") in kept or not (w.details or {}).get("document")
                         and w.code != "page_budget_exceeded"]

    # ---- checks (standard) and DWG per page
    checks: dict[str, Any] = {}
    reports: dict[str, bytes] = {}
    ga_pages_final = [p for p in pages if p.document == "GA"]
    ann = exec_policy.run_check("drawing.annotation", pol, lambda: _annotation_record(ctx, ga_pages_final, details, reports, documents,
                                                                                      kept_checks))
    checks["drawing.annotation"] = ann
    runtime = _runtime(dwg_runtime) if "dwg" in cfg.formats else None
    sem: dict[str, Any] = {}
    if "dwg" in cfg.formats:
        for p in pages:
            if page_progress.get(p.key) == "reused" and p.file("dwg") is not None and p.key in prev_sem:
                sem[p.key] = prev_sem[p.key]  # the same DWG bytes and its recorded semantic check
                continue
            sem[p.key] = _convert(p, pol, runtime, cfg.dwg_timeout_s)["status"]
            if page_progress.get(p.key) == "reused":
                page_progress[p.key] = "dwg_converted"
        statuses = set(sem.values())
        overall = ("skipped_turbo" if statuses == {"skipped_turbo"} else "failed" if "failed" in statuses
                   else "unknown" if "unknown" in statuses or not statuses else "passed")
        checks["drawing.dwg_semantic"] = {"check": "drawing.dwg_semantic", "status": overall, "pages": sem}
    else:
        checks["drawing.dwg_semantic"] = {"check": "drawing.dwg_semantic", "status": "not_requested"}

    completeness, errors = _finish(pages, documents, cfg.formats)
    statuses = {c["status"] for c in checks.values() if c["status"] != "not_requested"}
    check_status = ("skipped_turbo" if pol == "turbo" else "failed" if "failed" in statuses
                    else "unknown" if "unknown" in statuses else "passed")
    verification = _verification(pol, check_status, checks, ctx.assumptions, upstream)
    pkg = _package(ctx, cfg, pol, pages, documents, completeness, errors, checks, verification, reports, version=1, overlay=None)
    for p in pages:
        if p.status != "complete":
            page_progress[p.key] = "failed"
    pkg.progress = {"documents": doc_progress, "pages": page_progress}
    return pkg


def _verification(pol: str, check_status: str, checks: dict[str, Any], assumptions: list[dict[str, Any]],
                  upstream: dict[str, Any] | None) -> dict[str, Any]:
    """The package's verification as stored in its manifest (so ``package.zip`` / exports carry it): the drawing
    checks, the package's own assumptions (``author`` rule) and, when given, the exact assembly revision it was drawn
    from. ``upstream`` = ``{"assembly_revision_id", "rev_no", "execution_policy", "check_status", "unverified",
    "pending_checks", "assumptions": [refs with owner]}`` (W0 §2.2: an unverified revision keeps the drawing
    unverified, and its assumptions are referenced)."""
    pending = sorted(k for k, c in checks.items() if c.get("status") in ("skipped_turbo", "pending", "failed", "unknown"))
    own = [{"id": a["id"], "subject": a["subject"], "field": a.get("field"), "adopted": a.get("adopted"), "author": a.get("author")}
           for a in assumptions]
    up = dict(upstream or {})
    refs = [dict(r) for r in up.pop("assumptions", None) or []]
    up_unconfirmed = [exec_policy.assumption_ref(r) for r in refs if exec_policy.needs_confirmation(r)]
    unconfirmed = [exec_policy.assumption_ref(a) for a in own if exec_policy.needs_confirmation(a)] + up_unconfirmed
    upstream_unverified = None
    if upstream is not None and (up.get("unverified") or up_unconfirmed):
        upstream_unverified = {"type": "assembly_revision", "id": up.get("assembly_revision_id"), "rev_no": up.get("rev_no"),
                               "check_status": up.get("check_status"), "pending_checks": list(up.get("pending_checks") or []),
                               "unconfirmed_assumptions": [r.get("id") for r in up_unconfirmed]}
    v = exec_policy.verification(pol, check_status, assumptions=own + refs, pending_checks=pending, unconfirmed_assumptions=unconfirmed,
                                 upstream_unverified=upstream_unverified)
    if upstream is not None:
        drawing_only = check_status != "passed" or any(exec_policy.needs_confirmation(a) for a in own)
        v.update(drawing_unverified=drawing_only, upstream=up)
    return v


def _budget(ctx: _Ctx, document: str, started: float, sheets: int, limit: float | None = None) -> dict[str, Any] | None:
    """W0 §7: at most :data:`PAGE_BUDGET_S` (or ``limit``) per sheet. OCCT cannot be interrupted here, so an overrun
    is found after the document: always a warning (package warnings, never the manifest: wall-clock time is not
    part of the deterministic output); with ``limit`` also the document error to record (its sheets are withheld).
    The service bounds the whole generation with a hard process timeout."""
    per_sheet = limit if limit is not None else PAGE_BUDGET_S
    spent = time.monotonic() - started
    if spent <= per_sheet * max(1, sheets):
        return None
    ctx.warnings.append(Issue(code="page_budget_exceeded", message=f"{document}: {spent:.0f} s for {max(1, sheets)} sheet(s), "
                              f"budget {per_sheet:.0f} s per sheet", details={"document": document, "seconds": round(spent, 1)}))
    if limit is None:
        return None
    return {"code": "page_budget_exceeded",
            "message": f"{document} took longer than its budget of {per_sheet:g} s per sheet; its sheets are withheld"}


def _annotation_record(ctx: _Ctx, ga_pages: list[AssemblyDrawingPage], details: list[_Detail], reports: dict[str, bytes],
                       documents: list[dict[str, Any]] | None = None, reused: dict[str, Any] | None = None) -> dict[str, Any]:
    rec, body = _annotation(ctx, ga_pages, details, documents, reused)
    reports[ANNOTATION_REPORT_PATH] = body
    return {**rec, "report_path": ANNOTATION_REPORT_PATH, "report_sha256": _sha(body)}


def _package(ctx: _Ctx | None, cfg: AssemblyDrawingConfig, pol: str, pages: list[AssemblyDrawingPage], documents: list[dict[str, Any]],
             completeness: str, errors: list[dict[str, Any]], checks: dict[str, Any], verification: dict[str, Any],
             reports: dict[str, bytes], *, version: int, overlay: dict[str, Any] | None,
             base: AssemblyDrawingPackage | None = None) -> AssemblyDrawingPackage:
    if ctx is not None:
        g = ctx.geom
        bom_dump = {k: [dict(_bom_dump(x)) for x in v] for k, v in ctx.lines.items()}
        source = {"assembly_revision_key": g.key, "assembly_name": g.name, "assembly_number": g.number,
                  "revision_label": g.revision_label, "part_definition_keys": sorted({o.part.key for o in ctx.occs}),
                  "bom_sha256": sha256_hex(bom_dump), "bom_view": cfg.bom_view, "config_sha256": sha256_hex(cfg.model_dump(mode="json")),
                  "occurrences": len(ctx.occs)}
        rep = {ln.def_key: ln.representation for ln in ctx.lines["flattened"]}
        proc = {ln.def_key: ln.procurement for ln in ctx.lines["flattened"]}
        bom_items = [{"item": r.item, "level": r.level, "def_key": r.def_key, "def_kind": r.def_kind, "quantity": r.quantity,
                      "total_quantity": r.total_quantity} for r in ctx.rows]
        warnings = list(ctx.warnings)
        assumptions = list(ctx.assumptions)
        coverage = (ctx.partial or {}).get("coverage")
    else:
        assert base is not None
        source = base.manifest["source"]
        rep, proc, bom_items = base.manifest["representation"], base.manifest["procurement"], base.manifest["bom"]["items"]
        warnings = list(base.warnings)
        assumptions = list(base.assumptions)
        coverage = base.manifest.get("coverage")
    ga = next((p for p in pages if p.role == "assembly"), None)
    pkg = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "execution_policy": pol,
        "units": "mm",
        "generator": generator_identity(),
        "sheet": {"size": "A3", "width_mm": SHEET_W, "height_mm": SHEET_H, "projection": cfg.projection},
        "required": {"documents": [{k: v for k, v in d.items() if k != "error"} for d in documents], "formats": list(cfg.formats)},
        "representation": rep,
        "procurement": proc,
        "bom": {"view": cfg.bom_view, "items": bom_items},
        "section": ga.section if ga is not None else None,
        "assumptions": assumptions,
        "checks": checks,
        "verification": verification,
        "completeness": completeness,
        "errors": errors,
        "reports": [{"path": k, "sha256": _sha(v), "bytes": len(v)} for k, v in sorted(reports.items())],
        "overlay": overlay,
    }
    if coverage is not None:  # a partial assembly revision: its coverage travels with the package (design §2.1)
        pkg["coverage"] = coverage
    hlr = _hlr_record(ctx) if ctx is not None else (base.manifest.get("hlr") if base is not None else None)
    if hlr is not None:  # coil geometry: drawn with the polygonal HLR (absent otherwise, so other manifests are unchanged)
        pkg["hlr"] = hlr
    manifest, sha = _manifest(source, pkg, pages)
    return AssemblyDrawingPackage(version=version, execution_policy=pol, completeness=completeness, pages=pages,  # type: ignore[arg-type]
                                  documents=[dict(d) for d in documents], errors=errors, warnings=warnings, checks=checks,
                                  verification=verification, assumptions=assumptions, manifest=manifest, manifest_sha256=sha,
                                  reports=reports)


def _hlr_record(ctx: _Ctx) -> dict[str, Any] | None:
    """Manifest record of the polygonal HLR (:mod:`app.cad.hlr_poly`), from the definitions alone (a resumed package
    that keeps its GA records it the same way)."""
    coils = ctx.coil_defs
    if not coils:
        return None
    return {"polygonal": {"coil_definitions": coils, "scope": "coil occurrences and, per view, the occurrences whose projection overlaps "
                          "theirs (GA); the coil part's own detail sheets", "linear_deflection_mm": _r(ctx.lin, 6),
                          "detail_linear_deflection": f"{hlr_poly.LIN_REL:g} × the part's bounding-box diagonal",
                          "angular_deflection_rad": hlr_poly.ANG_DEFL}, "exact": "every other edge (HLRBRep_Algo)"}


def generator_identity() -> dict[str, Any]:
    """The generator block of the manifest: a package made by another generator is never continued (S3)."""
    return _json({"assembly_drawing_version": ASSEMBLY_DRAWING_VERSION, "drawing_version": dr.DRAWING_VERSION, "dxf_version": "R2010",
                  "ezdxf": ezdxf.__version__, "kernel_version": KERNEL_VERSION, "dwg_target": DWG_TARGET,
                  "text_style": dict(dr.TEXT_STYLE), "dimstyle": dr.DIMSTYLE})


def _report_documents(pkg: AssemblyDrawingPackage) -> dict[str, Any]:
    """Document entries of a stored package's annotation report ({} when it has none, e.g. Turbo)."""
    body = pkg.reports.get(ANNOTATION_REPORT_PATH)
    if not body:
        return {}
    try:
        return dict(json.loads(body).get("documents") or {})
    except (ValueError, AttributeError):
        return {}


def _reusable_documents(prev: AssemblyDrawingPackage, pol: str) -> tuple[dict[str, list[AssemblyDrawingPage]], dict[str, Any]]:
    """Documents of the package being continued that are kept as they are (S3): produced all their sheets, every sheet
    has its DXF and SVG and, in standard, its annotation check record. Returns (key → pages, key → report entry). A
    sheet that only lacks its DWG is kept and only converted again."""
    if prev.execution_policy != pol:
        return {}, {}
    report = _report_documents(prev)
    pages: dict[str, list[AssemblyDrawingPage]] = {}
    entries: dict[str, Any] = {}
    for d in prev.documents:
        key = d.get("key")
        own = [p for p in prev.pages if p.document == key]
        if not key or d.get("error") or not d.get("sheets") or len(own) != d["sheets"]:
            continue
        if any(p.file("dxf") is None or p.file("svg") is None for p in own):
            continue
        if pol != "turbo":
            if key not in report or report[key].get("missing"):
                continue
            entries[key] = report[key]
        pages[key] = own
    return pages, entries


def _reused_page(p: AssemblyDrawingPage) -> AssemblyDrawingPage:
    """A sheet taken from the continued package: same files; a DWG that failed is dropped so it is converted again."""
    page = p.model_copy(deep=True)
    if page.file("dwg") is None:
        page.errors = [e for e in page.errors if e.get("format") != "dwg"]
        page.dwg_report = None
    page.status = "complete"
    return page


def _bom_dump(ln: BomLine) -> dict[str, Any]:
    return {k: (list(list(p) for p in v) if k == "instance_paths" else v) for k, v in ln.__dict__.items()}


def restore_package(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    pages: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
    read: Any,
) -> AssemblyDrawingPackage:
    """Rebuild a stored package (the input of :func:`apply_layout_overlay`).

    ``pages``: the page records as stored (``AssemblyDrawingPage.model_dump(mode="json")``, file bytes excluded);
    ``documents``: ``package.documents`` (with their errors); ``read(sha256) -> bytes`` returns a stored file.
    Checks, verification, assumptions, completeness and errors come from the manifest. Fails closed
    (``package_manifest_mismatch`` / ``package_file_mismatch``) when the manifest no longer hashes to its sha, a page
    record disagrees with its manifest entry, or a file's bytes do not match their recorded sha256 / size."""
    if sha256_hex(manifest) != manifest_sha256:
        raise AssemblyDrawingError("package_manifest_mismatch", "the stored manifest does not match its hash",
                                   {"manifest_sha256": manifest_sha256, "computed": sha256_hex(manifest)})
    entries = {int(p["index"]): p for p in manifest.get("pages") or []}
    restored: list[AssemblyDrawingPage] = []
    for rec in pages:
        page = AssemblyDrawingPage.model_validate(rec)
        entry = entries.get(page.index)
        listed = [(f["format"], f["path"], f["sha256"]) for f in (entry or {}).get("files") or []]
        if entry is None or entry.get("key") != page.key or listed != [(f.format, f.path, f.sha256) for f in page.files]:
            raise AssemblyDrawingError("package_manifest_mismatch", f"page {page.index} ({page.key}) differs from its manifest entry",
                                       {"page": page.key})
        for f in page.files:
            data = read(f.sha256)
            if _sha(data) != f.sha256 or len(data) != f.bytes:
                raise AssemblyDrawingError("package_file_mismatch", f"{f.path} does not match its manifest hash", {"path": f.path,
                                                                                                                   "sha256": f.sha256})
            f.data = data
        restored.append(page)
    if sorted(p.index for p in restored) != sorted(entries):
        raise AssemblyDrawingError("package_manifest_mismatch", "the stored pages do not cover the manifest",
                                   {"stored": sorted(p.index for p in restored), "manifest": sorted(entries)})
    restored.sort(key=lambda p: p.index)
    reports: dict[str, bytes] = {}
    for r in manifest.get("reports") or []:
        data = read(r["sha256"])
        if _sha(data) != r["sha256"]:
            raise AssemblyDrawingError("package_file_mismatch", f"{r['path']} does not match its manifest hash", {"path": r["path"]})
        reports[r["path"]] = data
    return AssemblyDrawingPackage(
        version=int(manifest["version"]), execution_policy=manifest["execution_policy"], completeness=manifest["completeness"],
        pages=restored, documents=[dict(d) for d in documents], errors=list(manifest.get("errors") or []),
        warnings=[Issue.model_validate(w) for w in warnings or []], checks=dict(manifest.get("checks") or {}),
        verification=dict(manifest.get("verification") or {}), assumptions=list(manifest.get("assumptions") or []),
        manifest=manifest, manifest_sha256=manifest_sha256, reports=reports)


# --------------------------------------------------------------------------- #
# Layout / annotation overlay
# --------------------------------------------------------------------------- #


class OverlayEdit(BaseModel):
    """One layout edit. Coordinates are sheet mm with the origin at the bottom-left corner (y up, as in the
    DXF). ``move_dimension_text``: ``page`` + ``dimension`` (record id or DXF handle) + ``to`` | ``by``;
    ``move_balloon``: ``page`` + ``item`` (+ ``occurrence`` when balloons are per instance) + ``to`` | ``by``;
    ``add_note``: ``page`` + ``text`` (+ ``at``, ``height``)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["move_dimension_text", "move_balloon", "add_note"]
    page: str
    dimension: str | None = None
    item: str | None = None
    occurrence: list[str] | None = None
    to: list[float] | None = None
    by: list[float] | None = None
    text: str | None = None
    at: list[float] | None = None
    height: float | None = Field(default=None, gt=0.5, le=10.0)


class LayoutOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_manifest_sha256: str | None = None
    edits: list[OverlayEdit] = Field(min_length=1)


_ENGINEERING_KEYS = {"value", "quantity", "qty", "material", "geometry", "offset", "plane", "new_item", "number"}


def validate_overlay(overlay: LayoutOverlay | dict[str, Any], package: AssemblyDrawingPackage) -> LayoutOverlay:
    """Parse an overlay against a package. Engineering edits raise :class:`EngineeringChangeRequired`; anything
    malformed or unknown raises :class:`OverlayError`."""
    if isinstance(overlay, LayoutOverlay):
        raw_edits = [e.model_dump() for e in overlay.edits]
        data: dict[str, Any] = overlay.model_dump()
    elif isinstance(overlay, dict):
        data = dict(overlay)
        raw_edits = list(data.get("edits") or [])
    else:
        raise OverlayError("an overlay is an object with 'edits'")
    for i, e in enumerate(raw_edits):
        if not isinstance(e, dict):
            raise OverlayError(f"edit {i} is not an object")
        op = e.get("op")
        given = {k for k, v in e.items() if v is not None}
        if op in ENGINEERING_OPS:
            raise EngineeringChangeRequired(f"edit {i} ({op}) changes an engineering value; request a model / assembly change",
                                            {"edit": i, "op": op})
        if op in ("move_dimension_text", "move_balloon") and (_ENGINEERING_KEYS | {"text"}) & given:
            raise EngineeringChangeRequired(f"edit {i} ({op}) carries {sorted((_ENGINEERING_KEYS | {'text'}) & given)}; "
                                            "dimension values and balloon items follow the model", {"edit": i, "op": op})
        if op == "add_note" and _ENGINEERING_KEYS & given:
            raise EngineeringChangeRequired(f"edit {i} (add_note) carries {sorted(_ENGINEERING_KEYS & given)}", {"edit": i})
        if op not in LAYOUT_OPS:
            raise OverlayError(f"edit {i}: unsupported operation {op!r} (layout operations: {', '.join(LAYOUT_OPS)})",
                               {"edit": i, "op": op})
    try:
        ov = LayoutOverlay.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise OverlayError(f"overlay does not validate: {str(exc)[:300]}") from exc
    if ov.base_manifest_sha256 is not None and ov.base_manifest_sha256 != package.manifest_sha256:
        raise OverlayError("the overlay was made for another package version",
                           {"expected": package.manifest_sha256, "overlay_base": ov.base_manifest_sha256})
    keys = {p.key: p for p in package.pages}
    for i, e in enumerate(ov.edits):
        p = keys.get(e.page)
        if p is None:
            raise OverlayError(f"edit {i}: no page {e.page!r}", {"edit": i})
        if p.file("dxf") is None or p.file("svg") is None:
            raise OverlayError(f"edit {i}: page {e.page} has no DXF / SVG to edit", {"edit": i})
        for name in ("to", "by", "at"):
            v = getattr(e, name)
            if v is not None and (len(v) != 2 or not all(math.isfinite(c) for c in v)):
                raise OverlayError(f"edit {i}: {name} must be two finite numbers", {"edit": i})
        if e.op in ("move_dimension_text", "move_balloon") and (e.to is None) == (e.by is None):
            raise OverlayError(f"edit {i}: give exactly one of 'to' and 'by'", {"edit": i})
        if e.op == "move_dimension_text":
            if not any(d.get("id") == e.dimension or d.get("handle") == e.dimension for d in p.dimensions):
                raise OverlayError(f"edit {i}: page {e.page} has no dimension {e.dimension!r}", {"edit": i})
        elif e.op == "move_balloon":
            hits = [b for b in p.balloons if b.item == e.item and (e.occurrence is None or b.occurrence == e.occurrence)]
            if len(hits) != 1:
                raise OverlayError(f"edit {i}: {len(hits)} balloon(s) match item {e.item!r} on {e.page}"
                                   + ("; name the occurrence" if len(hits) > 1 else ""), {"edit": i})
        elif e.op == "add_note":
            if not (e.text or "").strip():
                raise OverlayError(f"edit {i}: empty note", {"edit": i})
            if dr._text_units(e.text or "") > dr.MAX_TEXT_UNITS:
                raise OverlayError(f"edit {i}: note too long for one DWG text (split it)", {"edit": i})
    return ov


def _move_dim_text(doc: Any, root: ET.Element, page: AssemblyDrawingPage, e: OverlayEdit) -> dict[str, Any]:
    rec = next(d for d in page.dimensions if d.get("id") == e.dimension or d.get("handle") == e.dimension)
    dim = doc.entitydb[rec["handle"]]
    cur = dim.dxf.get("text_midpoint")
    cur = (float(cur[0]), float(cur[1])) if cur is not None else None
    if e.to is not None:
        to = (float(e.to[0]), float(e.to[1]))
    else:
        if cur is None:
            raise OverlayError(f"dimension {rec['id']} has no text position to move from")
        to = (cur[0] + float(e.by[0]), cur[1] + float(e.by[1]))  # type: ignore[index]
    old_block = dim.dxf.get("geometry")
    ovr = dim.override()
    ovr.set_location(to, leader=False, relative=False)
    ovr.commit()
    ovr.render()
    if old_block and old_block != dim.dxf.get("geometry") and old_block in doc.blocks:
        doc.blocks.delete_block(old_block, safe=False)
    for g in root.iter(f"{dr_svg_ns}g"):
        if g.get("class") == "dimension" and g.get("data-handle") == rec["handle"]:
            t = next(g.iter(f"{dr_svg_ns}text"), None)
            if t is not None:
                sx, sy = dr._n(to[0]), dr._n(SHEET_H - to[1])
                t.set("x", sx)
                t.set("y", sy)
                if t.get("transform"):
                    t.set("transform", f"rotate(-90 {sx} {sy})")
    rec["text_at"] = [_r(to[0]), _r(to[1])]
    return {"op": e.op, "dimension": rec["id"], "handle": rec["handle"], "to": rec["text_at"]}


def _move_balloon(doc: Any, root: ET.Element, page: AssemblyDrawingPage, e: OverlayEdit) -> dict[str, Any]:
    b = next(b for b in page.balloons if b.item == e.item and (e.occurrence is None or b.occurrence == e.occurrence))
    to = (float(e.to[0]), float(e.to[1])) if e.to is not None else (b.center[0] + float(e.by[0]), b.center[1] + float(e.by[1]))  # type: ignore[index]
    if math.dist(to, tuple(b.anchor)) <= b.radius + 0.5:
        raise OverlayError(f"balloon {b.item} would cover its own leader end")
    ins = doc.entitydb[b.handle]
    ins.dxf.insert = (to[0], to[1], 0.0)
    leader = doc.entitydb[b.leader_handle]
    dx, dy = b.anchor[0] - to[0], b.anchor[1] - to[1]
    ln = math.hypot(dx, dy) or 1.0
    leader.dxf.start = (to[0] + dx / ln * b.radius, to[1] + dy / ln * b.radius, 0.0)
    b.center = [_r(to[0]), _r(to[1])]
    hits = [(parent, g) for parent in root.iter() for g in parent
            if g.tag == f"{dr_svg_ns}g" and g.get("class") == "balloon" and g.get("data-handle") == b.handle]
    for parent, g in hits:
        new = ET.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg">{_balloon_svg(b.id, b.model_dump())}</svg>')[0]
        idx = list(parent).index(g)
        parent.remove(g)
        parent.insert(idx, new)
    return {"op": e.op, "item": b.item, "occurrence": b.occurrence, "to": b.center}


def _add_note(doc: Any, root: ET.Element, page: AssemblyDrawingPage, e: OverlayEdit, k: int) -> dict[str, Any]:
    h = e.height or dr.NOTE_H
    at = (float(e.at[0]), float(e.at[1])) if e.at is not None else (MARGIN + 4.0, SHEET_H - MARGIN - 6.0 - k * dr.NOTE_STEP)
    text = " ".join((e.text or "").split())
    t = doc.modelspace().add_text(text, height=h, dxfattribs={"layer": "TEXT", "style": dr.TEXT_STYLE["name"]})
    t.set_placement(at, align=TextEntityAlignment.LEFT)
    group = next((g for g in root.iter(f"{dr_svg_ns}g") if g.get("id") == "text"), root)
    el = ET.SubElement(group, f"{dr_svg_ns}text", {"class": "overlay-note", "x": dr._n(at[0]), "y": dr._n(SHEET_H - at[1]),
                                                   "font-size": dr._n(h / dr.CAP_RATIO), "text-anchor": "start"})
    el.text = text
    page.notes.append(text)
    return {"op": e.op, "text": text, "at": [_r(at[0]), _r(at[1])], "handle": t.dxf.handle}


def _write_dxf(doc: Any) -> bytes:
    stream = io.StringIO()
    with fixed_metadata():
        doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _write_svg(root: ET.Element) -> bytes:
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n").encode("utf-8")


def apply_layout_overlay(
    package: AssemblyDrawingPackage,
    overlay: LayoutOverlay | dict[str, Any],
    policy: str | None = None,
    *,
    dwg_runtime: Any = None,
    dwg_timeout_s: float = 120.0,
    upstream: dict[str, Any] | None = None,
) -> AssemblyDrawingPackage:
    """A new package version with layout / annotation edits applied to the existing sheets (``upstream``: the
    revision's verification state now, as for :func:`make_assembly_drawing_package`; default: the base version's).

    Only the edited sheets change: their DXF (DIMENSION text position, balloon INSERT + leader, added TEXT) and
    SVG are rewritten from the previous files, their DWG is converted again, and the annotation check re-reads
    them (standard). Geometry, dimension values, balloons ↔ BOM and every other sheet are carried over byte
    for byte; the manifest keeps the same source keys and records the overlay and the previous manifest hash.
    ``policy`` defaults to the package's policy."""
    pol = exec_policy.parse_policy(policy or package.execution_policy)
    ov = validate_overlay(overlay, package)
    pages = [p.model_copy(deep=True) for p in package.pages]
    for p, old in zip(pages, package.pages):  # deep copies drop excluded bytes: carry them over
        for f, of in zip(p.files, old.files):
            f.data = of.data
    by_key = {p.key: p for p in pages}
    edited: dict[str, tuple[Any, ET.Element]] = {}
    applied: list[dict[str, Any]] = []
    notes_added: collections.Counter[str] = collections.Counter()
    for e in ov.edits:
        p = by_key[e.page]
        if e.page not in edited:
            edited[e.page] = (_dxf_doc(p.dxf), ET.fromstring(p.svg))
        doc, root = edited[e.page]
        if e.op == "move_dimension_text":
            rec = _move_dim_text(doc, root, p, e)
        elif e.op == "move_balloon":
            rec = _move_balloon(doc, root, p, e)
        else:
            rec = _add_note(doc, root, p, e, notes_added[e.page])
            notes_added[e.page] += 1
        p.overlay.append(rec)
        applied.append({"page": e.page, **rec})
    for key, (doc, root) in sorted(edited.items()):
        p = by_key[key]
        dxf, svg = _write_dxf(doc), _write_svg(root)
        p.files = _files(dxf, svg)
        p.errors = [x for x in p.errors if x.get("format") not in ("dxf", "svg", "dwg")]
        p.dwg_report = None

    checks = json.loads(json.dumps(package.checks))
    reports = dict(package.reports)
    changed = [by_key[k] for k in sorted(edited)]

    def recheck() -> dict[str, Any]:
        rep = check_package(changed)
        failed = len(rep.failures())
        body = canonical_json(_json({"schema": "partroute.assembly_drawing_checks/1", "check": "drawing.annotation",
                                     "overlay_pages": sorted(edited), "status": "failed" if failed else "passed", "failed": failed,
                                     "sheets": rep.summary(),
                                     "carried_over": "unchanged sheets and the geometry checks of the previous version"})).encode()
        reports["checks/annotation_overlay.json"] = body
        prev = package.checks.get("drawing.annotation", {}).get("status")
        status = "failed" if failed or prev == "failed" else ("passed" if prev == "passed" else "unknown")
        return {"check": "drawing.annotation", "status": status, "failed": failed, "previous": prev,
                "report_path": "checks/annotation_overlay.json", "report_sha256": _sha(body)}

    checks["drawing.annotation"] = exec_policy.run_check("drawing.annotation", pol, recheck)
    required = list(package.manifest["required"]["formats"])
    if "dwg" in required and changed:
        runtime = _runtime(dwg_runtime)
        sem = dict((package.checks.get("drawing.dwg_semantic") or {}).get("pages") or {})
        for p in changed:
            sem[p.key] = _convert(p, pol, runtime, dwg_timeout_s)["status"]
        statuses = set(sem.values())
        checks["drawing.dwg_semantic"] = {"check": "drawing.dwg_semantic", "pages": sem, "status": (
            "skipped_turbo" if statuses == {"skipped_turbo"} else "failed" if "failed" in statuses
            else "unknown" if "unknown" in statuses or "skipped_turbo" in statuses else "passed")}
    documents = [dict(d) for d in package.documents]
    completeness, errors = _finish(pages, documents, required)
    statuses = {c["status"] for c in checks.values() if c["status"] != "not_requested"}
    check_status = ("skipped_turbo" if pol == "turbo" else "failed" if "failed" in statuses
                    else "unknown" if statuses - {"passed"} else "passed")
    if upstream is None and isinstance(package.verification.get("upstream"), dict):
        base_up = package.verification["upstream"]
        upstream = {**base_up, "assumptions": [a for a in package.verification.get("assumptions") or [] if a.get("owner")]}
    verification = _verification(pol, check_status, checks, list(package.assumptions), upstream)
    overlay_rec = {"base_manifest_sha256": package.manifest_sha256, "base_version": package.version,
                   "sha256": sha256_hex(ov.model_dump(mode="json")), "edits": applied, "pages": sorted(edited)}
    cfg = AssemblyDrawingConfig.model_validate({"projection": package.manifest["sheet"]["projection"],
                                                "bom_view": package.manifest["bom"]["view"], "formats": required})
    return _package(None, cfg, pol, pages, documents, completeness, errors, checks, verification, reports,
                    version=package.version + 1, overlay=overlay_rec, base=package)
