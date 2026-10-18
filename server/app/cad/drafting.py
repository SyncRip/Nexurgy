"""Standalone 2D drafting (W6): entity model, edit commands, rule checks, DXF export and DXF import.

A drawing document version holds a :class:`DraftState`: sheets (paper size, scale, title block, BOM table),
layers, entities with stable ids, block definitions, image underlays with their calibration, per-annotation
provenance, conflicts, assumptions and open questions. Model space is in millimetres, y up; a sheet border is
drawn at ``origin`` and covers ``paper size / scale`` model millimetres (1:2 → scale 0.5).

Entities (``type``): line, polyline (optional bulges), circle, arc, text, mtext, leader, dimension (linear /
aligned / angular / diameter / radius), hatch (closed loops, pattern or solid) and insert (block reference).
Every entity has an ``origin``: ``source`` (faithfully redrawn from a source file), ``ai`` (proposed by a model
from text), ``person`` (drawn by a person) or ``generated``; ``modified`` marks a source / AI entity a person
changed afterwards, so a faithful re-drawing and an inferred reconstruction stay distinguishable.

Annotation provenance (dimensions) keeps four things apart: the ``original`` reading displayed in the source
drawing (never rewritten), the ``measured`` value of the definition points (recomputed for every version), the
current ``override`` text and who set it (source / person / ai), and the ``assumption`` id when Turbo adopted a
value. A displayed number that disagrees with the measurement is flagged ``reading_mismatch``.

Edit commands (:data:`Command`) are pure functions of the state; the service applies them to the base version
and saves the result as a new immutable version. :func:`check_rules` is the ``drafting.rules`` business check
(standard only; the service calls it through ``policy.run_check``). Structural validity
(:func:`validate_structure`: unique ids, known layers / blocks, finite coordinates) always applies.

DXF export (:func:`export_dxf`, R2010, deterministic bytes) writes native editable entities. Constraints of the
DXF → DWG R2000 pipeline (``cad/dwg.py`` and its LibreDWG + ezdwg readbacks, verified empirically for W6) are met
by construction and reported in ``encoding``: hidden layers are written frozen (an "off" layer does not read back
in ezdwg), diameter / radius dimensions are aligned DIMENSIONs with a ``%%c<>`` / ``R<>`` text (the DIAMETER /
RADIUS subtypes do not read back), angular dimensions are two-line angular DIMENSIONs whose second line ends on
the dimension arc, and title-block / BOM values are plain TEXT (ATTRIB crashes the converter). Our own entity ids
and the sheet / title-block / BOM roles travel as XDATA (application ``PARTROUTE``) so a re-import restores them.

DXF import (:func:`import_dxf`) maps supported entities (LINE, LWPOLYLINE / 2D POLYLINE, CIRCLE, ARC, TEXT,
MTEXT, LEADER, DIMENSION, HATCH, INSERT + block definitions, SOLID / TRACE as solid hatch, SPLINE / ELLIPSE as
flagged polyline approximations, ATTRIB values as texts) and lists every other object with its count as
preserved / not preserved. Dimension readings come from ``cad/drawing_evidence`` (displayed text, measurement,
DIMLFAC) so the redraw keeps the source reading next to the measured value.

Pure library: no ORM / DB / service imports.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

import ezdxf
from ezdxf import const as dxfconst
from ezdxf.enums import TextEntityAlignment
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from ..ai import limits
from .dxf_write import fixed_metadata

DRAFTING_VERSION = "partroute-drafting/1"
APPID = "PARTROUTE"
MAX_COORD = 1.0e7
MAX_ENTITIES = 20_000
MAX_BLOCKS = 500
MAX_COMMANDS = 500
TOL = 1e-6
ATTACH_TOL = 0.05  # mm: a dimension definition point counts as attached within this distance of geometry
DIMSTYLE = "PR_DRAFT"
TEXT_STYLE_NAME = "PR_TEXT"

_CFG = ConfigDict(extra="forbid", allow_inf_nan=False)

# DWG R2000 (code page ANSI_1252) cannot hold layer / block names beyond ASCII: the pipeline writes them as \U+
# escapes that neither reader decodes. The DWG therefore gets reversible ASCII aliases ("PRU$" + one "$XXXXX" per
# non-ASCII character); the DXF keeps the real names and the import decodes aliases back.
ALIAS_PREFIX = "PRU$"


def encode_name(name: str) -> str:
    if all(ord(ch) < 128 for ch in name):
        return name
    return ALIAS_PREFIX + "".join(ch if ord(ch) < 128 and ch != "$" else f"${ord(ch):05X}" for ch in name)


def decode_name(name: str) -> str:
    if not name.startswith(ALIAS_PREFIX):
        return name
    return re.sub(r"\$([0-9A-F]{5})", lambda m: chr(int(m.group(1), 16)), name[len(ALIAS_PREFIX):])


def needs_ascii_names(state: DraftState) -> bool:
    names = [lay.name for lay in state.layers] + [b.name for b in state.blocks]
    return any(encode_name(n) != n for n in names)


class DraftingError(ValueError):
    """A command or input that cannot be applied (structure, unknown id, invalid geometry)."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- #
# Entity model
# --------------------------------------------------------------------------- #


def _check_point(p: tuple[float, float]) -> tuple[float, float]:
    x, y = float(p[0]), float(p[1])
    if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > MAX_COORD or abs(y) > MAX_COORD:
        raise ValueError(f"coordinate out of range: {p}")
    return (x, y)


Point = Annotated[tuple[float, float], AfterValidator(_check_point)]
Origin = Literal["source", "ai", "person", "generated"]
EntityId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,39}$")]
_NAME_RE = r"^[^\s*<>/\\\":;?|=`,][^<>/\\\":;?*|=`,]{0,63}$"

LINETYPES: dict[str, tuple[list[float], str]] = {
    "CENTER": ([12.0, 8.0, -1.5, 1.0, -1.5], "Center ____ _ ____"),
    "HIDDEN": ([3.0, 2.0, -1.0], "Hidden __ __ __"),
    "DASHED": ([9.0, 6.0, -3.0], "Dashed ______  ______"),
    "PHANTOM": ([20.0, 12.0, -2.0, 2.0, -2.0, 2.0, -2.0], "Phantom ____ _ _ ____"),
    "DOT": ([2.0, 0.0, -2.0], "Dot . . . ."),
}
LINETYPE_NAMES = ("CONTINUOUS", *LINETYPES)
LINEWEIGHTS = (-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211)
HATCH_PATTERNS = ("ANSI31", "ANSI32", "ANSI33", "ANSI34", "ANSI35", "ANSI36", "ANSI37", "ANSI38", "NET", "DOTS")
FRAME_LAYERS = {"PR_FRAME": 7, "PR_TITLE": 7, "PR_BOM": 7}
NOTES_LAYER = "PR_NOTES"  # conflict notes (generated from the document's conflicts)


def _linetype(v: str | None) -> str | None:
    if v is None:
        return None
    up = v.strip().upper() or "CONTINUOUS"
    if up in ("BYLAYER", "BYBLOCK"):
        return None
    if up not in LINETYPE_NAMES:
        raise ValueError(f"linetype {v!r} is not one of {', '.join(LINETYPE_NAMES)}")
    return up


class Layer(BaseModel):
    model_config = _CFG
    name: str = Field(min_length=1, max_length=64, pattern=_NAME_RE)
    color: int = Field(7, ge=1, le=255)
    linetype: str = "CONTINUOUS"
    lineweight: int = -3
    visible: bool = True
    locked: bool = False
    description: str = Field("", max_length=200)

    @model_validator(mode="after")
    def _norm(self) -> Layer:
        self.linetype = _linetype(self.linetype) or "CONTINUOUS"
        if self.lineweight not in LINEWEIGHTS:
            raise ValueError(f"lineweight {self.lineweight} is not a DXF lineweight")
        return self


class _Ent(BaseModel):
    model_config = _CFG
    id: EntityId
    layer: str = Field("0", min_length=1, max_length=64)
    color: int | None = Field(None, ge=1, le=256)  # None = BYLAYER
    linetype: str | None = None  # None = BYLAYER
    origin: Origin = "person"
    modified: bool = False
    source_ref: str | None = Field(None, max_length=120)

    @model_validator(mode="after")
    def _lt(self) -> _Ent:
        self.linetype = _linetype(self.linetype)
        return self


class Line(_Ent):
    type: Literal["line"] = "line"
    p1: Point
    p2: Point


class Polyline(_Ent):
    type: Literal["polyline"] = "polyline"
    points: list[Point] = Field(min_length=2, max_length=5000)
    bulges: list[float] | None = None
    closed: bool = False

    @model_validator(mode="after")
    def _b(self) -> Polyline:
        if self.bulges is not None:
            if len(self.bulges) != len(self.points):
                raise ValueError("bulges must have one value per point")
            if not all(math.isfinite(b) and abs(b) <= 1e3 for b in self.bulges):
                raise ValueError("bulge out of range")
            if not any(self.bulges):
                self.bulges = None
        return self


class Circle(_Ent):
    type: Literal["circle"] = "circle"
    center: Point
    radius: float = Field(ge=0, le=MAX_COORD)


class Arc(_Ent):
    type: Literal["arc"] = "arc"
    center: Point
    radius: float = Field(ge=0, le=MAX_COORD)
    start_angle: float = Field(ge=-1e6, le=1e6)
    end_angle: float = Field(ge=-1e6, le=1e6)


class Text(_Ent):
    type: Literal["text"] = "text"
    insert: Point
    text: str = Field(max_length=limits.DRAFTING_TEXT_CHARS)
    height: float = Field(3.5, gt=0, le=10_000)
    rotation: float = Field(0.0, ge=-1e6, le=1e6)
    align: Literal["left", "center", "right"] = "left"


class MText(_Ent):
    type: Literal["mtext"] = "mtext"
    insert: Point
    text: str = Field(max_length=limits.DRAFTING_MTEXT_CHARS)
    height: float = Field(3.5, gt=0, le=10_000)
    width: float | None = Field(None, gt=0, le=MAX_COORD)
    rotation: float = Field(0.0, ge=-1e6, le=1e6)


class Leader(_Ent):
    """Arrow at ``points[0]``; the text (MTEXT in the DXF) sits at the last point."""

    type: Literal["leader"] = "leader"
    points: list[Point] = Field(min_length=2, max_length=50)
    text: str = Field("", max_length=limits.DRAFTING_TEXT_CHARS)
    height: float = Field(3.5, gt=0, le=10_000)


DimType = Literal["linear", "aligned", "angular", "diameter", "radius"]


class Dimension(_Ent):
    """A native dimension. Fields by ``dimtype``:

    - linear: ``p1``, ``p2`` measured along ``angle`` (0 horizontal, 90 vertical); ``location`` on the dim line;
    - aligned: ``p1``, ``p2`` measured along their direction; ``location`` on the dim line;
    - angular: vertex ``center``, ``p1`` on the first ray, ``p2`` on the second ray, ``location`` on the arc;
    - diameter / radius: ``center``, ``radius`` and ``angle`` (direction of the dimension line, degrees).

    ``text_override`` None = the measured value; ``"<>"`` inside it stands for the measured value
    (``"4×Ø<>"``); a literal number that differs from the measurement is kept and flagged."""

    type: Literal["dimension"] = "dimension"
    dimtype: DimType
    p1: Point | None = None
    p2: Point | None = None
    center: Point | None = None
    radius: float | None = Field(None, gt=0, le=MAX_COORD)
    angle: float = Field(0.0, ge=-1e6, le=1e6)
    location: Point | None = None
    text_override: str | None = Field(None, max_length=200)
    dimlfac: float = Field(1.0, gt=0, le=1e4)  # displayed = measured × dimlfac (scaled detail views)
    refs: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _fields(self) -> Dimension:
        need = {
            "linear": ("p1", "p2", "location"),
            "aligned": ("p1", "p2", "location"),
            "angular": ("center", "p1", "p2", "location"),
            "diameter": ("center", "radius"),
            "radius": ("center", "radius"),
        }[self.dimtype]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.dimtype} dimension needs {', '.join(missing)}")
        return self


class HatchLoop(BaseModel):
    """A closed boundary: a polygon (``points`` + optional ``bulges``) or a full circle (``center`` + ``radius``)."""

    model_config = _CFG
    points: list[Point] | None = Field(None, max_length=5000)
    bulges: list[float] | None = None
    center: Point | None = None
    radius: float | None = Field(None, ge=0, le=MAX_COORD)

    @model_validator(mode="after")
    def _form(self) -> HatchLoop:
        if (self.points is None) == (self.center is None):
            raise ValueError("a hatch loop is either points or a circle (center + radius)")
        if self.center is not None and self.radius is None:
            raise ValueError("a circular hatch loop needs a radius")
        if self.points is not None and self.bulges is not None and len(self.bulges) != len(self.points):
            raise ValueError("bulges must have one value per point")
        return self


class Hatch(_Ent):
    type: Literal["hatch"] = "hatch"
    loops: list[HatchLoop] = Field(min_length=1, max_length=200)
    pattern: str = "ANSI31"
    scale: float = Field(1.0, gt=0, le=1000)
    angle: float = Field(0.0, ge=-360, le=360)
    solid: bool = False
    boundary_refs: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _pat(self) -> Hatch:
        self.pattern = (self.pattern or "ANSI31").upper()
        if self.pattern == "SOLID":
            self.solid = True
            self.pattern = "ANSI31"
        if self.pattern not in HATCH_PATTERNS:
            raise ValueError(f"hatch pattern {self.pattern!r} is not one of {', '.join(HATCH_PATTERNS)} / SOLID")
        return self


class Insert(_Ent):
    type: Literal["insert"] = "insert"
    block: str = Field(min_length=1, max_length=64)
    insert: Point
    rotation: float = Field(0.0, ge=-1e6, le=1e6)
    xscale: float = Field(1.0, ge=-1e4, le=1e4)
    yscale: float = Field(1.0, ge=-1e4, le=1e4)

    @model_validator(mode="after")
    def _scale(self) -> Insert:
        if abs(self.xscale) < 1e-9 or abs(self.yscale) < 1e-9:
            raise ValueError("block scale must not be zero")
        return self


Entity = Annotated[Union[Line, Polyline, Circle, Arc, Text, MText, Leader, Dimension, Hatch, Insert], Field(discriminator="type")]
BlockEntity = Annotated[Union[Line, Polyline, Circle, Arc, Text, MText, Hatch], Field(discriminator="type")]
ENTITY_TYPES = ("line", "polyline", "circle", "arc", "text", "mtext", "leader", "dimension", "hatch", "insert")
GEOMETRY_TYPES = ("line", "polyline", "circle", "arc")
_ENTITY_ADAPTER: TypeAdapter[Any] = TypeAdapter(Entity)


class Block(BaseModel):
    model_config = _CFG
    name: str = Field(min_length=1, max_length=64, pattern=_NAME_RE)
    base: Point = (0.0, 0.0)
    entities: list[BlockEntity] = Field(default_factory=list, max_length=5000)
    description: str = Field("", max_length=200)
    origin: Origin = "person"


# Title block fields (paper layout in _TITLE_ROWS) and BOM columns.
TITLE_FIELDS = ("title", "drawing_number", "material", "scale", "sheet", "revision", "drawn_by", "checked_by", "date",
                "company", "project", "units", "weight", "notes")
BOM_COLUMNS = ("item", "number", "name", "qty", "material", "note")
SHEET_SIZES: dict[str, tuple[float, float]] = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
                                               "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}


class Reading(BaseModel):
    """A value as displayed in a source (drawing text, dimension text, request sentence)."""

    model_config = _CFG
    text: str = Field(max_length=400)
    value: float | None = None
    source: str | None = None  # source id, e.g. "S-1"
    locator: str | None = Field(None, max_length=200)  # e.g. "handle 2F"


class Sheet(BaseModel):
    model_config = _CFG
    id: str = Field(pattern=r"^S\d{1,3}$")
    name: str = Field("", max_length=120)
    size: Literal["A4", "A3", "A2", "A1", "A0", "custom"] = "A3"
    orientation: Literal["landscape", "portrait"] = "landscape"
    width: float | None = Field(None, gt=0, le=10_000)  # paper mm (custom size; else derived)
    height: float | None = Field(None, gt=0, le=10_000)
    scale: float = Field(1.0, gt=0, le=1000)  # paper mm per model mm (1:2 -> 0.5)
    origin: Point = (0.0, 0.0)  # model-space lower-left corner of the border
    draw_frame: bool = True  # False: the source drawing's own border / title block are kept as entities
    title_block: dict[str, str] = Field(default_factory=dict)
    title_readings: dict[str, Reading] = Field(default_factory=dict)
    bom_columns: list[str] = Field(default_factory=lambda: list(BOM_COLUMNS), max_length=8)
    bom: list[dict[str, str]] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _size(self) -> Sheet:
        if self.size != "custom":
            w, h = SHEET_SIZES[self.size]
            if self.orientation == "portrait":
                w, h = h, w
            self.width, self.height = w, h
        elif self.width is None or self.height is None:
            raise ValueError("a custom sheet needs width and height")
        unknown = set(self.title_block) - set(TITLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown title block field(s): {', '.join(sorted(unknown))}")
        if any(len(v) > 200 for v in self.title_block.values()):
            raise ValueError("title block values are at most 200 characters")
        for row in self.bom:
            bad = set(row) - set(self.bom_columns)
            if bad:
                raise ValueError(f"BOM row has unknown column(s): {', '.join(sorted(bad))}")
            if any(len(v) > 200 for v in row.values()):
                raise ValueError("BOM cells are at most 200 characters")
        return self

    @property
    def model_size(self) -> tuple[float, float]:
        assert self.width is not None and self.height is not None
        return self.width / self.scale, self.height / self.scale


class Calibration(BaseModel):
    model_config = _CFG
    status: Literal["unknown", "calibrated", "assumed"] = "unknown"
    p1_px: Point | None = None
    p2_px: Point | None = None
    distance_mm: float | None = Field(None, gt=0, le=MAX_COORD)
    mm_per_px: float | None = Field(None, gt=0, le=1e4)
    assumption_id: str | None = None
    by: str | None = None
    at: str | None = None


class Underlay(BaseModel):
    """An image placed in model space as located evidence (never exported as drawing geometry)."""

    model_config = _CFG
    id: str = Field(pattern=r"^U-\d{1,4}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field("", max_length=300)
    content_type: str = Field("image/png", max_length=80)
    width_px: int = Field(gt=0, le=100_000)
    height_px: int = Field(gt=0, le=100_000)
    file_version_id: str | None = None
    origin: Point = (0.0, 0.0)  # model-space position of the image's lower-left corner
    rotation: float = Field(0.0, ge=-360, le=360)
    opacity: float = Field(0.5, ge=0, le=1)
    visible: bool = True
    calibration: Calibration = Field(default_factory=Calibration)


class OverrideRecord(BaseModel):
    model_config = _CFG
    text: str | None
    author: Literal["source", "person", "ai"]
    by: str | None = None
    at: str | None = None


class AnnotationProvenance(BaseModel):
    """Where an annotation's value comes from; the four origins are never merged."""

    model_config = _CFG
    original: Reading | None = None  # displayed in the source drawing (import); never rewritten
    override: OverrideRecord | None = None  # current text override and who set it
    assumption: str | None = None  # assumption id when Turbo adopted the value
    measured: float | None = None  # native measurement of the definition points (this version)
    unit: Literal["mm", "deg"] = "mm"
    displayed: str = ""
    consistent: bool | None = None  # a displayed number equals the measurement (None: nothing to compare)
    flags: list[str] = Field(default_factory=list)


class ConflictValue(BaseModel):
    model_config = _CFG
    value: float | str | None = None
    text: str = Field("", max_length=400)
    kind: Literal["displayed", "measured", "stated", "source"] = "stated"
    source: str | None = None
    locator: str | None = Field(None, max_length=200)


class Conflict(BaseModel):
    model_config = _CFG
    id: str = Field(pattern=r"^C-\d{1,4}$")
    subject: str = Field(max_length=120)
    kind: Literal["reading_mismatch", "duplicate_reading", "source_conflict"] = "source_conflict"
    values: list[ConflictValue] = Field(min_length=2, max_length=20)
    note: str = Field("", max_length=500)
    status: Literal["open", "resolved"] = "open"
    resolution: dict[str, Any] | None = None


class DraftState(BaseModel):
    model_config = _CFG
    units: Literal["mm"] = "mm"
    sheets: list[Sheet] = Field(default_factory=list, max_length=20)
    layers: list[Layer] = Field(default_factory=list, max_length=500)
    entities: list[Entity] = Field(default_factory=list, max_length=MAX_ENTITIES)
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)
    underlays: list[Underlay] = Field(default_factory=list, max_length=20)
    next_entity_no: int = Field(1, ge=1)
    provenance: dict[str, AnnotationProvenance] = Field(default_factory=dict)
    conflicts: list[Conflict] = Field(default_factory=list, max_length=500)
    assumptions: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    open_questions: list[str] = Field(default_factory=list, max_length=100)

    def entity(self, entity_id: str) -> Any:
        for e in self.entities:
            if e.id == entity_id:
                return e
        raise DraftingError("entity_not_found", f"no entity {entity_id!r}", {"id": entity_id})

    def layer(self, name: str) -> Layer | None:
        return next((lay for lay in self.layers if lay.name == name), None)

    def block(self, name: str) -> Block | None:
        return next((b for b in self.blocks if b.name == name), None)

    def sheet(self, sheet_id: str) -> Sheet:
        for s in self.sheets:
            if s.id == sheet_id:
                return s
        raise DraftingError("sheet_not_found", f"no sheet {sheet_id!r}", {"id": sheet_id})


DEFAULT_LAYERS = (
    Layer(name="0"),
    Layer(name="OUTLINE", color=7, lineweight=50),
    Layer(name="CENTER", color=1, linetype="CENTER", lineweight=18),
    Layer(name="HIDDEN", color=8, linetype="HIDDEN", lineweight=25),
    Layer(name="DIM", color=3, lineweight=18),
    Layer(name="TEXT", color=7, lineweight=25),
    Layer(name="HATCH", color=8, lineweight=18),
)


def new_state(sheet: Sheet | None = None) -> DraftState:
    return DraftState(sheets=[sheet or Sheet(id="S1")], layers=[lay.model_copy() for lay in DEFAULT_LAYERS])


def parse_entity(data: dict[str, Any]) -> Any:
    try:
        return _ENTITY_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise DraftingError("invalid_entity", _first_error(exc), {"errors": _errors(exc)}) from exc


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid"))


def _errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}" for e in exc.errors()[:12]]


def next_id(state: DraftState) -> str:
    taken = {e.id for e in state.entities}
    while True:
        eid = f"E-{state.next_entity_no:04d}"
        state.next_entity_no += 1
        if eid not in taken:
            return eid


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

P2 = tuple[float, float]


def _sub(a: P2, b: P2) -> P2:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: P2, b: P2) -> P2:
    return (a[0] + b[0], a[1] + b[1])


def _len(v: P2) -> float:
    return math.hypot(v[0], v[1])


def _dist(a: P2, b: P2) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _unit(deg: float) -> P2:
    r = math.radians(deg)
    return (math.cos(r), math.sin(r))


def _rot(p: P2, c: P2, deg: float) -> P2:
    if not deg:
        return p
    r = math.radians(deg)
    cs, sn = math.cos(r), math.sin(r)
    x, y = p[0] - c[0], p[1] - c[1]
    return (c[0] + x * cs - y * sn, c[1] + x * sn + y * cs)


def _clean(v: float) -> float:
    """Round float noise from transforms (1e-9) so stored coordinates stay readable and hash stably."""
    r = round(v, 9)
    return 0.0 if r == 0 else r


def _cp(p: P2) -> P2:
    return (_clean(p[0]), _clean(p[1]))


def _norm_angle(a: float) -> float:
    return a % 360.0


def _arc_sweep(start: float, end: float) -> float:
    s = (end - start) % 360.0
    return 360.0 if abs(s) < 1e-12 and start != end else s


def _on_arc(angle: float, start: float, end: float) -> bool:
    sweep = _arc_sweep(start, end)
    return (angle - start) % 360.0 <= sweep + 1e-9


def _seg_dist(p: P2, a: P2, b: P2) -> float:
    ab = _sub(b, a)
    L2 = ab[0] ** 2 + ab[1] ** 2
    if L2 < 1e-18:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / L2))
    return _dist(p, (a[0] + t * ab[0], a[1] + t * ab[1]))


def _bulge_arc(a: P2, b: P2, bulge: float) -> tuple[P2, float, float, float]:
    """(center, radius, start angle, end angle) of a bulged segment from a to b (CCW for bulge > 0)."""
    chord = _dist(a, b)
    theta = 4.0 * math.atan(bulge)
    r = chord / (2.0 * math.sin(abs(theta) / 2.0))
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    d = math.sqrt(max(r * r - (chord / 2.0) ** 2, 0.0))
    ux, uy = (b[0] - a[0]) / chord, (b[1] - a[1]) / chord
    sign = 1.0 if bulge > 0 else -1.0
    if abs(theta) > math.pi:
        sign = -sign
    c = (mid[0] - uy * d * sign, mid[1] + ux * d * sign)
    sa = math.degrees(math.atan2(a[1] - c[1], a[0] - c[0]))
    ea = math.degrees(math.atan2(b[1] - c[1], b[0] - c[0]))
    if bulge < 0:
        sa, ea = ea, sa
    return c, r, sa, ea


def _poly_segments(points: list[P2], bulges: list[float] | None, closed: bool):
    n = len(points)
    last = n if closed else n - 1
    for i in range(last):
        a, b = points[i], points[(i + 1) % n]
        bulge = bulges[i] if bulges else 0.0
        yield a, b, bulge


def _poly_length(points: list[P2], bulges: list[float] | None, closed: bool) -> float:
    total = 0.0
    for a, b, bulge in _poly_segments(points, bulges, closed):
        chord = _dist(a, b)
        if bulge and chord > TOL:
            theta = 4.0 * math.atan(bulge)
            r = chord / (2.0 * math.sin(abs(theta) / 2.0))
            total += abs(theta) * r
        else:
            total += chord
    return total


def _polygon_area(points: list[P2]) -> float:
    n = len(points)
    return 0.5 * sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1] for i in range(n))


def distance_to(e: Any, p: P2) -> float:
    """Distance from ``p`` to a geometry entity (line / polyline / circle / arc); inf for other types."""
    if e.type == "line":
        return _seg_dist(p, e.p1, e.p2)
    if e.type == "circle":
        return abs(_dist(p, e.center) - e.radius)
    if e.type == "arc":
        ang = math.degrees(math.atan2(p[1] - e.center[1], p[0] - e.center[0]))
        if _on_arc(ang, e.start_angle, e.end_angle):
            return abs(_dist(p, e.center) - e.radius)
        ends = [_add(e.center, (e.radius * math.cos(math.radians(a)), e.radius * math.sin(math.radians(a))))
                for a in (e.start_angle, e.end_angle)]
        return min(_dist(p, q) for q in ends)
    if e.type == "polyline":
        best = math.inf
        for a, b, bulge in _poly_segments(list(e.points), e.bulges, e.closed):
            if bulge and _dist(a, b) > TOL:
                c, r, sa, ea = _bulge_arc(a, b, bulge)
                ang = math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))
                d = abs(_dist(p, c) - r) if _on_arc(ang, sa, ea) else min(_dist(p, a), _dist(p, b))
            else:
                d = _seg_dist(p, a, b)
            best = min(best, d)
        return best
    return math.inf


def snap_points(e: Any) -> list[tuple[str, P2]]:
    """Object snaps of an entity: endpoint, midpoint, center (line / polyline / circle / arc)."""
    out: list[tuple[str, P2]] = []
    if e.type == "line":
        out += [("endpoint", e.p1), ("endpoint", e.p2), ("midpoint", ((e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2))]
    elif e.type == "polyline":
        pts = list(e.points)
        out += [("endpoint", p) for p in pts]
        for a, b, bulge in _poly_segments(pts, e.bulges, e.closed):
            if not bulge:
                out.append(("midpoint", ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))
    elif e.type == "circle":
        out.append(("center", e.center))
        out += [("quadrant", _add(e.center, (e.radius * c, e.radius * s))) for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1))]
    elif e.type == "arc":
        out.append(("center", e.center))
        for a in (e.start_angle, e.end_angle):
            out.append(("endpoint", _add(e.center, (e.radius * math.cos(math.radians(a)), e.radius * math.sin(math.radians(a))))))
    return out


def entity_points(e: Any) -> list[P2]:
    """Characteristic points of any entity (bounding boxes, selection)."""
    t = e.type
    if t == "line":
        return [e.p1, e.p2]
    if t in ("polyline", "leader"):
        return list(e.points)
    if t in ("circle", "arc"):
        c, r = e.center, e.radius
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if t in ("text", "mtext"):
        return [e.insert]
    if t == "insert":
        return [e.insert]
    if t == "dimension":
        pts = [p for p in (e.p1, e.p2, e.center, e.location) if p is not None]
        if e.center is not None and e.radius:
            pts += [(e.center[0] - e.radius, e.center[1] - e.radius), (e.center[0] + e.radius, e.center[1] + e.radius)]
        return pts
    if t == "hatch":
        pts: list[P2] = []
        for lp in e.loops:
            if lp.points is not None:
                pts += list(lp.points)
            elif lp.center is not None and lp.radius is not None:
                pts += [(lp.center[0] - lp.radius, lp.center[1] - lp.radius), (lp.center[0] + lp.radius, lp.center[1] + lp.radius)]
        return pts
    return []


def bbox(state: DraftState) -> tuple[float, float, float, float] | None:
    pts = [p for e in state.entities for p in entity_points(e)]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- #
# Dimensions: measured value, displayed text, consistency
# --------------------------------------------------------------------------- #


def measure(dim: Dimension) -> tuple[float, Literal["mm", "deg"]]:
    """Native measurement of the definition points (mm, or degrees for angular), before DIMLFAC."""
    if dim.dimtype == "linear":
        assert dim.p1 is not None and dim.p2 is not None
        u = _unit(dim.angle)
        d = _sub(dim.p2, dim.p1)
        return abs(d[0] * u[0] + d[1] * u[1]), "mm"
    if dim.dimtype == "aligned":
        assert dim.p1 is not None and dim.p2 is not None
        return _dist(dim.p1, dim.p2), "mm"
    if dim.dimtype == "diameter":
        assert dim.radius is not None
        return 2.0 * dim.radius, "mm"
    if dim.dimtype == "radius":
        assert dim.radius is not None
        return dim.radius, "mm"
    assert dim.center is not None and dim.p1 is not None and dim.p2 is not None
    a1 = math.atan2(dim.p1[1] - dim.center[1], dim.p1[0] - dim.center[0])
    a2 = math.atan2(dim.p2[1] - dim.center[1], dim.p2[0] - dim.center[0])
    ang = abs(math.degrees(a2 - a1)) % 360.0
    return (360.0 - ang if ang > 180.0 else ang), "deg"


def fmt(v: float, decimals: int = 2) -> str:
    s = f"{round(v, decimals):.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _default_text(dim: Dimension) -> str:
    return {"diameter": "Ø<>", "radius": "R<>", "angular": "<>°"}.get(dim.dimtype, "<>")


def displayed_text(dim: Dimension) -> str:
    value, _ = measure(dim)
    shown = fmt(value * (dim.dimlfac if dim.dimtype != "angular" else 1.0))
    template = dim.text_override if dim.text_override not in (None, "") else _default_text(dim)
    return template.replace("<>", shown)


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?|\.\d+")


def numbers_in(text: str) -> list[tuple[float, int]]:
    """Numbers of a displayed text with their written decimals, skipping tolerance parts (+0.1, ±0.05)."""
    out: list[tuple[float, int]] = []
    for m in _NUMBER_RE.finditer(text):
        start = m.start()
        prev = text[start - 1] if start > 0 else ""
        if prev and prev in "+±":
            continue
        if prev and prev in "-−" and (start < 2 or text[start - 2] in " \t(/"):
            continue
        g = m.group(0)
        out.append((float(g), len(g.split(".")[1]) if "." in g else 0))
    return out


def reading_consistent(text: str, value: float) -> bool | None:
    nums = numbers_in(text)
    if not nums:
        return None
    return any(abs(n - value) <= 0.5 * 10 ** (-dec) + 1e-9 for n, dec in nums)


def refresh_provenance(state: DraftState) -> None:
    """Recompute measured / displayed / consistent for every dimension; drop records of deleted entities."""
    out: dict[str, AnnotationProvenance] = {}
    for e in state.entities:
        if e.type != "dimension":
            continue
        rec = state.provenance.get(e.id) or AnnotationProvenance()
        value, unit = measure(e)
        rec.measured = _clean(value)
        rec.unit = unit
        rec.displayed = displayed_text(e)
        shown_value = value * (e.dimlfac if unit == "mm" else 1.0)
        rec.consistent = reading_consistent(rec.displayed, shown_value)
        if e.text_override in (None, "") and rec.override is not None and rec.override.text not in (None, ""):
            rec.override = OverrideRecord(text=None, author=rec.override.author, by=rec.override.by, at=rec.override.at)
        flags = [f for f in rec.flags if f not in ("reading_mismatch", "original_mismatch")]
        if rec.consistent is False:
            flags.append("reading_mismatch")
        if rec.original is not None and rec.original.value is not None and \
                reading_consistent(rec.original.text, shown_value) is False:
            flags.append("original_mismatch")
        rec.flags = flags
        out[e.id] = rec
    state.provenance = out


# --------------------------------------------------------------------------- #
# Structure (always enforced, standard and Turbo)
# --------------------------------------------------------------------------- #


def check_size(entities: int, blocks: int = 0, **extra: Any) -> None:
    """The document size limits (``DraftState`` would refuse the stored version later): 422 ``too_many_entities``
    with the counts, before anything is converted further or saved."""
    if entities > MAX_ENTITIES or blocks > MAX_BLOCKS:
        what = f"{entities} entities (at most {MAX_ENTITIES})" if entities > MAX_ENTITIES else f"{blocks} blocks (at most {MAX_BLOCKS})"
        raise DraftingError("too_many_entities", f"the drawing has {what}; split it into several documents",
                            {"entities": entities, "max_entities": MAX_ENTITIES, "blocks": blocks, "max_blocks": MAX_BLOCKS, **extra})


def validate_structure(state: DraftState) -> list[str]:
    """Normalise and check structure. Missing layers referenced by entities are created (reported);
    duplicate ids, unknown blocks, duplicate names and more than :data:`MAX_ENTITIES` entities / :data:`MAX_BLOCKS`
    blocks raise :class:`DraftingError`. Returns notes."""
    notes: list[str] = []
    check_size(len(state.entities), len(state.blocks))
    dup = sorted(i for i, n in Counter(e.id for e in state.entities).items() if n > 1)
    if dup:
        raise DraftingError("duplicate_entity_id", f"duplicate entity id(s): {', '.join(dup[:10])}", {"ids": dup[:50]})
    names = [lay.name.upper() for lay in state.layers]
    if len(set(names)) != len(names):
        raise DraftingError("duplicate_layer", "layer names must be unique (case-insensitive)")
    if not any(lay.name == "0" for lay in state.layers):
        state.layers.insert(0, Layer(name="0"))
    bnames = [b.name.upper() for b in state.blocks]
    if len(set(bnames)) != len(bnames):
        raise DraftingError("duplicate_block", "block names must be unique (case-insensitive)")
    sids = [s.id for s in state.sheets]
    if len(set(sids)) != len(sids):
        raise DraftingError("duplicate_sheet", "sheet ids must be unique")
    uids = [u.id for u in state.underlays]
    if len(set(uids)) != len(uids):
        raise DraftingError("duplicate_underlay", "underlay ids must be unique")
    known = {lay.name for lay in state.layers}
    used = [e.layer for e in state.entities] + [be.layer for b in state.blocks for be in b.entities]
    for name in used:
        if name not in known:
            try:
                state.layers.append(Layer(name=name))
            except ValidationError as exc:
                raise DraftingError("invalid_layer", f"invalid layer name {name!r}", {"errors": _errors(exc)}) from exc
            known.add(name)
            notes.append(f"layer {name} created")
    blocks = {b.name for b in state.blocks}
    missing = sorted({e.block for e in state.entities if e.type == "insert" and e.block not in blocks})
    if missing:
        raise DraftingError("unknown_block", f"block(s) not defined: {', '.join(missing)}", {"blocks": missing})
    for e in state.entities:
        if e.id.startswith("E-"):
            m = re.fullmatch(r"E-(\d+)", e.id)
            if m:
                state.next_entity_no = max(state.next_entity_no, int(m.group(1)) + 1)
    return notes


# --------------------------------------------------------------------------- #
# Edit commands
# --------------------------------------------------------------------------- #


class _Cmd(BaseModel):
    model_config = _CFG


class AddEntities(_Cmd):
    op: Literal["add_entities"]
    entities: list[dict[str, Any]] = Field(min_length=1, max_length=2000)


class DeleteEntities(_Cmd):
    op: Literal["delete_entities"]
    ids: list[str] = Field(min_length=1, max_length=5000)


class Transform(_Cmd):
    """Rigid transform (move and / or rotate; no scaling) of entities; ``copy`` keeps the originals."""

    op: Literal["transform"]
    ids: list[str] = Field(min_length=1, max_length=5000)
    dx: float = Field(0.0, ge=-MAX_COORD, le=MAX_COORD)
    dy: float = Field(0.0, ge=-MAX_COORD, le=MAX_COORD)
    rotate: float = Field(0.0, ge=-3600, le=3600)
    center: Point | None = None
    copy_: bool = Field(False, alias="copy")


class UpdateEntity(_Cmd):
    """Change fields of one entity (geometry, text, style); ``id`` and ``type`` stay."""

    op: Literal["update_entity"]
    id: str
    set: dict[str, Any] = Field(min_length=1)


class SetEntityLayer(_Cmd):
    op: Literal["set_entity_layer"]
    ids: list[str] = Field(min_length=1, max_length=5000)
    layer: str = Field(min_length=1, max_length=64)


class UpsertLayer(_Cmd):
    op: Literal["upsert_layer"]
    layer: Layer


class SetLayerVisibility(_Cmd):
    op: Literal["set_layer_visibility"]
    name: str
    visible: bool


class EditText(_Cmd):
    op: Literal["edit_text"]
    id: str
    text: str = Field(max_length=limits.DRAFTING_MTEXT_CHARS)
    height: float | None = Field(None, gt=0, le=10_000)


class EditDimension(_Cmd):
    """Set (or clear with null) a dimension's text override; the original reading stays in provenance."""

    op: Literal["edit_dimension"]
    id: str
    text_override: str | None = Field(None, max_length=200)


class DefineBlock(_Cmd):
    op: Literal["define_block"]
    block: Block


class InsertBlock(_Cmd):
    op: Literal["insert_block"]
    block: str
    insert: Point
    rotation: float = 0.0
    xscale: float = 1.0
    yscale: float = 1.0
    layer: str = "0"


class SetTitleBlock(_Cmd):
    op: Literal["set_title_block"]
    sheet_id: str = "S1"
    fields: dict[str, str]


class SetBom(_Cmd):
    op: Literal["set_bom"]
    sheet_id: str = "S1"
    rows: list[dict[str, str]] = Field(max_length=200)
    columns: list[str] | None = Field(None, max_length=8)


class SetSheet(_Cmd):
    op: Literal["set_sheet"]
    sheet_id: str = "S1"
    set: dict[str, Any]


class SetUnderlay(_Cmd):
    op: Literal["set_underlay"]
    id: str
    set: dict[str, Any]


class Calibrate(_Cmd):
    """Two image points (pixels) and their known distance (mm) fix the underlay scale."""

    op: Literal["calibrate"]
    underlay_id: str
    p1_px: Point
    p2_px: Point
    distance_mm: float = Field(gt=0, le=MAX_COORD)


class ResolveConflict(_Cmd):
    op: Literal["resolve_conflict"]
    conflict_id: str
    value: float | str | None = None
    note: str = Field("", max_length=500)


class ConfirmAssumption(_Cmd):
    """A person's decision on an AI / rule assumption (W0 §2.2): without ``value`` the adopted value is confirmed; a
    different value records a correction, made by the edits saved in the same version."""

    op: Literal["confirm_assumption"]
    assumption_id: str
    value: float | str | list[float] | None = None
    note: str = Field("", max_length=500)


Command = Annotated[
    Union[AddEntities, DeleteEntities, Transform, UpdateEntity, SetEntityLayer, UpsertLayer, SetLayerVisibility, EditText,
          EditDimension, DefineBlock, InsertBlock, SetTitleBlock, SetBom, SetSheet, SetUnderlay, Calibrate, ResolveConflict,
          ConfirmAssumption],
    Field(discriminator="op"),
]
_COMMANDS_ADAPTER: TypeAdapter[Any] = TypeAdapter(list[Command])


def parse_commands(raw: list[dict[str, Any]]) -> list[Any]:
    if len(raw) > MAX_COMMANDS:
        raise DraftingError("too_many_commands", f"at most {MAX_COMMANDS} commands per save")
    try:
        return _COMMANDS_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise DraftingError("invalid_command", _first_error(exc), {"errors": _errors(exc)}) from exc


def transform_entity(e: Any, dx: float, dy: float, rotate: float, center: P2) -> Any:
    """A rigidly moved copy of ``e`` (rotation about ``center``, then translation)."""

    def tp(p: P2 | None) -> P2 | None:
        if p is None:
            return None
        q = _rot(p, center, rotate)
        return _cp((q[0] + dx, q[1] + dy))

    d = e.model_dump()
    t = e.type
    if t == "line":
        d.update(p1=tp(e.p1), p2=tp(e.p2))
    elif t in ("polyline", "leader"):
        d["points"] = [tp(p) for p in e.points]
    elif t == "circle":
        d["center"] = tp(e.center)
    elif t == "arc":
        d.update(center=tp(e.center), start_angle=_clean(_norm_angle(e.start_angle + rotate)),
                 end_angle=_clean(_norm_angle(e.end_angle + rotate)))
    elif t in ("text", "mtext"):
        d.update(insert=tp(e.insert), rotation=_clean(_norm_angle(e.rotation + rotate)) if rotate else e.rotation)
    elif t == "insert":
        d.update(insert=tp(e.insert), rotation=_clean(_norm_angle(e.rotation + rotate)) if rotate else e.rotation)
    elif t == "dimension":
        d.update(p1=tp(e.p1), p2=tp(e.p2), center=tp(e.center), location=tp(e.location))
        if rotate and e.dimtype in ("linear", "diameter", "radius"):
            d["angle"] = _clean(_norm_angle(e.angle + rotate))
    elif t == "hatch":
        loops = []
        for lp in e.loops:
            ld = lp.model_dump()
            if lp.points is not None:
                ld["points"] = [tp(p) for p in lp.points]
            else:
                ld["center"] = tp(lp.center)
            loops.append(ld)
        d["loops"] = loops
    return parse_entity(d)


def _mark_modified(e: Any) -> Any:
    if e.origin in ("source", "ai") and not e.modified:
        return e.model_copy(update={"modified": True})
    return e


def _selection_center(ents: list[Any]) -> P2:
    pts = [p for e in ents for p in entity_points(e)]
    if not pts:
        return (0.0, 0.0)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def apply_commands(state: DraftState, commands: list[Any], *, actor: str | None = None, at: str | None = None,
                   actor_kind: Literal["person", "ai"] = "person") -> tuple[DraftState, list[str]]:
    """Apply parsed commands to a copy of ``state``. Returns (new state, change notes). Raises DraftingError."""
    s = state.model_copy(deep=True)
    notes: list[str] = []
    for i, cmd in enumerate(commands):
        try:
            notes += _apply_one(s, cmd, actor=actor, at=at, actor_kind=actor_kind)
        except DraftingError as exc:
            exc.details.setdefault("command_index", i)
            exc.details.setdefault("op", getattr(cmd, "op", None))
            raise
        except ValidationError as exc:
            raise DraftingError("invalid_command", _first_error(exc), {"command_index": i, "op": cmd.op, "errors": _errors(exc)}) from exc
    notes += validate_structure(s)
    refresh_provenance(s)
    return s, notes


def _replace(s: DraftState, new: Any) -> None:
    for i, e in enumerate(s.entities):
        if e.id == new.id:
            s.entities[i] = new
            return
    raise DraftingError("entity_not_found", f"no entity {new.id!r}", {"id": new.id})


def _apply_one(s: DraftState, cmd: Any, *, actor: str | None, at: str | None, actor_kind: str) -> list[str]:
    op = cmd.op
    origin = "ai" if actor_kind == "ai" else "person"
    if op == "add_entities":
        added = []
        for raw in cmd.entities:
            data = dict(raw)
            if not data.get("id"):
                data["id"] = next_id(s)
            # A new entity is the actor's: nobody can claim it was read from a source file.
            data.update(origin=origin, modified=False, source_ref=None)
            e = parse_entity(data)
            if any(x.id == e.id for x in s.entities):
                raise DraftingError("duplicate_entity_id", f"entity id {e.id} already exists", {"id": e.id})
            if e.type == "dimension" and e.text_override not in (None, ""):
                s.provenance[e.id] = AnnotationProvenance(
                    override=OverrideRecord(text=e.text_override, author="ai" if origin == "ai" else "person", by=actor, at=at))
            s.entities.append(e)
            added.append(e.id)
        return [f"added {', '.join(added)}"]
    if op == "delete_entities":
        ids = set(cmd.ids)
        missing = ids - {e.id for e in s.entities}
        if missing:
            raise DraftingError("entity_not_found", f"no entity {sorted(missing)[0]!r}", {"ids": sorted(missing)})
        s.entities = [e for e in s.entities if e.id not in ids]
        return [f"deleted {len(ids)} entit{'y' if len(ids) == 1 else 'ies'}"]
    if op == "transform":
        ents = [s.entity(i) for i in cmd.ids]
        center = cmd.center if cmd.center is not None else _selection_center(ents)
        if cmd.copy_:
            new_ids = []
            for e in ents:
                moved = transform_entity(e, cmd.dx, cmd.dy, cmd.rotate, center)
                copy = moved.model_copy(update={"id": next_id(s), "origin": origin, "modified": False, "source_ref": None})
                s.entities.append(copy)
                new_ids.append(copy.id)
            return [f"copied {len(new_ids)} → {', '.join(new_ids[:20])}"]
        for e in ents:
            _replace(s, _mark_modified(transform_entity(e, cmd.dx, cmd.dy, cmd.rotate, center)))
        return [f"moved {len(ents)}"]
    if op == "update_entity":
        e = s.entity(cmd.id)
        fixed = {"id", "type", "origin", "modified", "source_ref"} & set(cmd.set)
        if fixed:
            raise DraftingError("invalid_command", f"update_entity cannot change {', '.join(sorted(fixed))}")
        data = {**e.model_dump(), **cmd.set}
        new = _mark_modified(parse_entity(data))
        if new.type == "dimension" and "text_override" in cmd.set:
            _record_override(s, new.id, new.text_override, author=origin, actor=actor, at=at)
        _replace(s, new)
        return [f"updated {cmd.id}"]
    if op == "set_entity_layer":
        for i in cmd.ids:
            e = s.entity(i)
            _replace(s, _mark_modified(e.model_copy(update={"layer": cmd.layer})))
        return [f"layer {cmd.layer} for {len(cmd.ids)}"]
    if op == "upsert_layer":
        lay = cmd.layer
        for i, old in enumerate(s.layers):
            if old.name.upper() == lay.name.upper():
                s.layers[i] = lay
                return [f"layer {lay.name} updated"]
        s.layers.append(lay)
        return [f"layer {lay.name} added"]
    if op == "set_layer_visibility":
        lay = s.layer(cmd.name)
        if lay is None:
            raise DraftingError("layer_not_found", f"no layer {cmd.name!r}", {"name": cmd.name})
        lay.visible = cmd.visible
        return [f"layer {cmd.name} {'shown' if cmd.visible else 'hidden'}"]
    if op == "edit_text":
        e = s.entity(cmd.id)
        if e.type not in ("text", "mtext", "leader"):
            raise DraftingError("not_text", f"{cmd.id} is a {e.type}, not a text", {"id": cmd.id})
        upd: dict[str, Any] = {"text": cmd.text}
        if cmd.height is not None:
            upd["height"] = cmd.height
        _replace(s, _mark_modified(parse_entity({**e.model_dump(), **upd})))
        return [f"text {cmd.id}"]
    if op == "edit_dimension":
        e = s.entity(cmd.id)
        if e.type != "dimension":
            raise DraftingError("not_dimension", f"{cmd.id} is a {e.type}, not a dimension", {"id": cmd.id})
        _replace(s, _mark_modified(e.model_copy(update={"text_override": cmd.text_override or None})))
        _record_override(s, e.id, cmd.text_override or None, author=origin, actor=actor, at=at)
        return [f"dimension {cmd.id} text {'override' if cmd.text_override else 'measured'}"]
    if op == "define_block":
        b = cmd.block
        if b.name.upper().startswith("PR_") or b.name.startswith("*"):
            raise DraftingError("reserved_block_name", f"block name {b.name!r} is reserved")
        for i, old in enumerate(s.blocks):
            if old.name.upper() == b.name.upper():
                s.blocks[i] = b
                return [f"block {b.name} redefined"]
        s.blocks.append(b)
        return [f"block {b.name} defined"]
    if op == "insert_block":
        if s.block(cmd.block) is None:
            raise DraftingError("unknown_block", f"block {cmd.block!r} is not defined", {"block": cmd.block})
        e = parse_entity({"id": next_id(s), "type": "insert", "block": cmd.block, "insert": cmd.insert, "rotation": cmd.rotation,
                          "xscale": cmd.xscale, "yscale": cmd.yscale, "layer": cmd.layer, "origin": origin})
        s.entities.append(e)
        return [f"inserted {cmd.block} as {e.id}"]
    if op == "set_title_block":
        sh = s.sheet(cmd.sheet_id)
        fields = {**sh.title_block, **{k: v for k, v in cmd.fields.items()}}
        fields = {k: v for k, v in fields.items() if v != ""}
        _replace_sheet(s, sh.model_dump() | {"title_block": fields})
        return [f"title block {cmd.sheet_id}: {', '.join(sorted(cmd.fields))}"]
    if op == "set_bom":
        sh = s.sheet(cmd.sheet_id)
        upd = {"bom": cmd.rows}
        if cmd.columns is not None:
            upd["bom_columns"] = cmd.columns
        _replace_sheet(s, sh.model_dump() | upd)
        return [f"BOM {cmd.sheet_id}: {len(cmd.rows)} row(s)"]
    if op == "set_sheet":
        sh = s.sheet(cmd.sheet_id)
        if "id" in cmd.set:
            raise DraftingError("invalid_command", "set_sheet cannot change the sheet id")
        data = sh.model_dump() | cmd.set
        if "size" in cmd.set and cmd.set["size"] != "custom":
            data.pop("width", None)
            data.pop("height", None)
        _replace_sheet(s, data)
        return [f"sheet {cmd.sheet_id}: {', '.join(sorted(cmd.set))}"]
    if op == "set_underlay":
        u = _underlay(s, cmd.id)
        allowed = {"visible", "opacity", "origin", "rotation"}
        bad = set(cmd.set) - allowed
        if bad:
            raise DraftingError("invalid_command", f"underlay fields that can be set: {', '.join(sorted(allowed))}")
        _replace_underlay(s, Underlay.model_validate(u.model_dump() | cmd.set))
        return [f"underlay {cmd.id}"]
    if op == "calibrate":
        u = _underlay(s, cmd.underlay_id)
        px = _dist(cmd.p1_px, cmd.p2_px)
        if px < 1.0:
            raise DraftingError("calibration_points_too_close", "the two calibration points must be at least 1 px apart")
        cal = Calibration(status="calibrated", p1_px=cmd.p1_px, p2_px=cmd.p2_px, distance_mm=cmd.distance_mm,
                          mm_per_px=cmd.distance_mm / px, by=actor, at=at)
        _replace_underlay(s, u.model_copy(update={"calibration": cal}))
        if origin == "person":  # the assumed scale of this underlay is now a person's calibration
            _decide(s, [a for a in s.assumptions if a.get("id") == u.calibration.assumption_id or u.id in (a.get("affected") or [])],
                    cal.mm_per_px, via="calibrate", actor=actor, at=at)
        return [f"underlay {u.id} calibrated: {fmt(cal.mm_per_px or 0, 6)} mm/px"]
    if op == "resolve_conflict":
        for i, c in enumerate(s.conflicts):
            if c.id == cmd.conflict_id:
                prior = (c.resolution or {}).get("assumption_id")
                s.conflicts[i] = c.model_copy(update={"status": "resolved", "resolution": {
                    "by": "person" if origin == "person" else "ai", "value": cmd.value, "note": cmd.note, "user_id": actor, "at": at}})
                if origin == "person":  # an AI's choice among the readings is now a person's decision
                    _decide(s, [a for a in s.assumptions if a.get("id") == prior or c.id in (a.get("affected") or [])], cmd.value,
                            via="resolve_conflict", actor=actor, at=at, note=cmd.note)
                return [f"conflict {c.id} resolved"]
        raise DraftingError("conflict_not_found", f"no conflict {cmd.conflict_id!r}", {"id": cmd.conflict_id})
    if op == "confirm_assumption":
        if origin != "person":
            raise DraftingError("person_required", "only a person can confirm an assumption")
        found = [a for a in s.assumptions if a.get("id") == cmd.assumption_id]
        if not found:
            raise DraftingError("assumption_not_found", f"no assumption {cmd.assumption_id!r}", {"id": cmd.assumption_id})
        if (found[0].get("author") or "ai_turbo") == "person":
            raise DraftingError("not_an_ai_assumption", f"{cmd.assumption_id} was stated by a person; there is nothing to confirm")
        value = found[0].get("adopted") if "value" not in cmd.model_fields_set else cmd.value
        _decide(s, found, value, via="confirm_assumption", actor=actor, at=at, note=cmd.note)
        return [f"assumption {cmd.assumption_id} {found[0]['confirmation']['decision']}"]
    raise DraftingError("invalid_command", f"unknown op {op!r}")  # pragma: no cover - the union rejects it first


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    return str(a) == str(b) if a is not None and b is not None else a is b


def _decide(s: DraftState, found: list[dict[str, Any]], value: Any, *, via: str, actor: str | None, at: str | None, note: str = "") -> None:
    """Record a person's decision on AI / rule assumptions of this new version (W0 §2.2): the assumption stays as the
    AI / rule wrote it and carries ``confirmation`` (author person, who, when, value; confirmed = the adopted value,
    corrected = another value, made by this version). Earlier versions keep their undecided record."""
    for a in found:
        if (a.get("author") or "ai_turbo") == "person":
            continue
        decision = "confirmed" if _same_value(value, a.get("adopted")) else "corrected"
        a["confirmation"] = {"author": "person", "by": actor, "at": at, "value": value, "decision": decision, "via": via,
                             "note": note[:500]}


def _record_override(s: DraftState, eid: str, text: str | None, *, author: str, actor: str | None, at: str | None) -> None:
    rec = s.provenance.get(eid) or AnnotationProvenance()
    rec.override = OverrideRecord(text=text, author="ai" if author == "ai" else "person", by=actor, at=at)
    s.provenance[eid] = rec


def _replace_sheet(s: DraftState, data: dict[str, Any]) -> None:
    try:
        new = Sheet.model_validate(data)
    except ValidationError as exc:
        raise DraftingError("invalid_sheet", _first_error(exc), {"errors": _errors(exc)}) from exc
    s.sheets = [new if x.id == new.id else x for x in s.sheets]


def _underlay(s: DraftState, uid: str) -> Underlay:
    for u in s.underlays:
        if u.id == uid:
            return u
    raise DraftingError("underlay_not_found", f"no underlay {uid!r}", {"id": uid})


def _replace_underlay(s: DraftState, new: Underlay) -> None:
    s.underlays = [new if u.id == new.id else u for u in s.underlays]


def scale_status(state: DraftState, *, units_known: bool = True) -> str:
    """unknown (an underlay is not calibrated, or the source had no units), assumed, calibrated or native."""
    statuses = [u.calibration.status for u in state.underlays]
    if "unknown" in statuses or not units_known:
        return "unknown"
    if "assumed" in statuses:
        return "assumed"
    if statuses:
        return "calibrated"
    return "native"


# --------------------------------------------------------------------------- #
# drafting.rules: structure / rule checks (standard only, via policy.run_check)
# --------------------------------------------------------------------------- #

RULES_VERSION = "drafting-rules/1"


def _item(rule: str, status: str, message: str, ids: list[str] | None = None, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"rule": rule, "status": status, "message": message}
    if ids:
        out["ids"] = ids[:50]
    if details:
        out["details"] = details
    return out


def check_rules(state: DraftState, *, units_known: bool = True) -> dict[str, Any]:
    """The ``drafting.rules`` report: closed hatch boundaries, dimensions attached to existing geometry, title
    block present, no zero-length entities, readings vs measurement, calibration, open conflicts, unsupported
    objects. ``status`` is failed when any item failed (warnings do not fail)."""
    items: list[dict[str, Any]] = []
    ents = {e.id: e for e in state.entities}
    geometry = [e for e in state.entities if e.type in GEOMETRY_TYPES]

    zero = []
    for e in state.entities:
        if e.type == "line" and _dist(e.p1, e.p2) < TOL:
            zero.append(e.id)
        elif e.type == "polyline" and _poly_length(list(e.points), e.bulges, e.closed) < TOL:
            zero.append(e.id)
        elif e.type in ("circle", "arc") and e.radius < TOL:
            zero.append(e.id)
        elif e.type == "arc" and abs(_arc_sweep(e.start_angle, e.end_angle)) < 1e-9:
            zero.append(e.id)
        elif e.type in ("text", "mtext") and not e.text.strip():
            zero.append(e.id)
        elif e.type == "dimension" and measure(e)[0] < TOL:
            zero.append(e.id)
    items.append(_item("entity.zero_length", "failed" if zero else "passed",
                       f"{len(zero)} zero-length / empty entit{'y' if len(zero) == 1 else 'ies'}" if zero else "no zero-length entities",
                       zero))

    open_loops = []
    for e in state.entities:
        if e.type != "hatch":
            continue
        bad = False
        for lp in e.loops:
            if lp.points is not None:
                pts = list(lp.points)
                if len(pts) >= 2 and _dist(pts[0], pts[-1]) < TOL:
                    pts = pts[:-1]
                if len(pts) < 3 or (not lp.bulges and abs(_polygon_area(pts)) < TOL):
                    bad = True
            elif not lp.radius or lp.radius < TOL:
                bad = True
        for ref in e.boundary_refs:
            b = ents.get(ref)
            if b is None or not ((b.type == "polyline" and b.closed) or b.type == "circle"):
                bad = True
        if bad:
            open_loops.append(e.id)
    items.append(_item("hatch.closed_boundary", "failed" if open_loops else "passed",
                       f"{len(open_loops)} hatch(es) without a closed boundary" if open_loops else "every hatch boundary is closed",
                       open_loops))

    detached: list[str] = []
    for e in state.entities:
        if e.type != "dimension":
            continue
        if any(r not in ents for r in e.refs):
            detached.append(e.id)
            continue
        if e.dimtype in ("diameter", "radius"):
            assert e.center is not None and e.radius is not None
            ok = any(g.type in ("circle", "arc") and _dist(g.center, e.center) <= ATTACH_TOL and abs(g.radius - e.radius) <= ATTACH_TOL
                     for g in geometry) or any(
                g.type == "polyline" and g.bulges and distance_to(g, (e.center[0] + e.radius, e.center[1])) <= ATTACH_TOL
                for g in geometry)
        else:
            pts = [e.p1, e.p2]
            ok = all(any(distance_to(g, p) <= ATTACH_TOL for g in geometry) for p in pts if p is not None)
        if not ok:
            detached.append(e.id)
    items.append(_item("dimension.attached", "failed" if detached else "passed",
                       f"{len(detached)} dimension(s) not on existing geometry" if detached else "every dimension is attached to geometry",
                       detached))

    missing_title = []
    for sh in state.sheets:
        if sh.draw_frame:
            if not (sh.title_block.get("title") and sh.title_block.get("drawing_number")):
                missing_title.append(sh.id)
        elif not sh.title_readings:
            missing_title.append(sh.id)
    if not state.sheets:
        missing_title.append("(no sheet)")
    items.append(_item("title_block.present", "failed" if missing_title else "passed",
                       f"title block without title / drawing number on {', '.join(missing_title)}" if missing_title
                       else "every sheet has a title block", missing_title))

    # An assumed scale (Turbo / rule) is no calibration: the rule fails until a person calibrates the underlay.
    uncal = [u.id for u in state.underlays if u.calibration.status != "calibrated"]
    assumed = [u.id for u in state.underlays if u.calibration.status == "assumed"]
    if state.underlays:
        items.append(_item("underlay.calibrated", "failed" if uncal else "passed",
                           f"image underlay(s) {', '.join(uncal)} not calibrated by a person (two points + a known distance)"
                           + (f"; {', '.join(assumed)} only has an assumed scale" if assumed else "") if uncal
                           else "every underlay was calibrated by a person", uncal))
    if not units_known:
        items.append(_item("units.known", "warning", "the source declares no drawing units; values are shown as stored"))

    mismatch = [eid for eid, p in state.provenance.items() if "reading_mismatch" in p.flags]
    items.append(_item("reading.matches_geometry", "warning" if mismatch else "passed",
                       f"{len(mismatch)} dimension text(s) differ from the measured geometry (kept and flagged)" if mismatch
                       else "displayed dimension values match the geometry", mismatch))
    open_c = [c.id for c in state.conflicts if c.status == "open"]
    if open_c:
        items.append(_item("conflict.open", "warning", f"{len(open_c)} open conflict note(s)", open_c))
    status = "failed" if any(i["status"] == "failed" for i in items) else "passed"
    return {"check": "drafting.rules", "status": status, "rules_version": RULES_VERSION, "items": items,
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "warnings": sum(1 for i in items if i["status"] == "warning")}


# --------------------------------------------------------------------------- #
# DXF export
# --------------------------------------------------------------------------- #

# Title block layout in paper mm: rows bottom-up, (width, field, label).
TITLE_W, TITLE_ROW_H = 180.0, 8.0
_TITLE_ROWS: list[list[tuple[float, str, str]]] = [
    [(60.0, "company", "单位 COMPANY"), (60.0, "project", "项目 PROJECT"), (60.0, "units", "单位制 UNITS")],
    [(45.0, "drawn_by", "设计 DRAWN"), (45.0, "checked_by", "审核 CHECKED"), (45.0, "date", "日期 DATE"), (45.0, "revision", "版次 REV")],
    [(60.0, "material", "材料 MATERIAL"), (40.0, "scale", "比例 SCALE"), (40.0, "sheet", "张次 SHEET"), (40.0, "weight", "重量 WEIGHT")],
    [(110.0, "title", "名称 TITLE"), (70.0, "drawing_number", "图号 DWG NO.")],
    # every TITLE_FIELDS key must have a cell, else its value is silently missing from the DXF / DWG
    [(180.0, "notes", "备注 NOTES")],
]
TITLE_H = TITLE_ROW_H * len(_TITLE_ROWS)
BOM_ROW_H = 7.0
BOM_WIDTHS = {"item": 12.0, "number": 38.0, "name": 50.0, "qty": 12.0, "material": 34.0, "note": 34.0}
BOM_LABELS = {"item": "序号", "number": "代号", "name": "名称", "qty": "数量", "material": "材料", "note": "备注"}
FRAME_MARGIN = 10.0


def scale_text(scale: float) -> str:
    if scale >= 1:
        return f"{fmt(scale, 3)}:1"
    return f"1:{fmt(1.0 / scale, 3)}"


@dataclass
class DxfExport:
    dxf: bytes
    encoding: list[dict[str, Any]] = field(default_factory=list)  # how model entities were written (DWG constraints)
    counts: dict[str, int] = field(default_factory=dict)


class _Writer:
    def __init__(self, state: DraftState, *, ascii_names: bool = False):
        self.state = state
        self.nm = encode_name if ascii_names else (lambda n: n)
        doc = ezdxf.new("R2010", setup=False)
        doc.ezdxf_metadata()["CREATED_BY_EZDXF"] = f"{ezdxf.__version__} @ 2000-01-01T00:00:00+00:00"
        doc.units = ezdxf.units.MM
        doc.header["$MEASUREMENT"] = 1
        doc.header["$LTSCALE"] = 1.0
        for name, (pattern, desc) in LINETYPES.items():
            doc.linetypes.add(name, pattern=pattern, description=desc)
        from .drawing import TEXT_STYLE  # same TTF face as the model-driven drawing package (CJK, Ø, °)

        style = doc.styles.add(TEXT_STYLE_NAME, font=TEXT_STYLE["font_file"])
        style.set_extended_font_data(family=TEXT_STYLE["family"])
        doc.appids.new(APPID)
        sheet_scale = state.sheets[0].scale if state.sheets else 1.0
        k = 1.0 / sheet_scale
        dimvars = {"dimscale": k, "dimtxt": 3.0, "dimasz": 2.5, "dimexo": 1.0, "dimexe": 1.5, "dimgap": 0.8, "dimtad": 1,
                   "dimjust": 0, "dimtih": 0, "dimtoh": 0, "dimtix": 1, "dimatfit": 3, "dimdec": 2, "dimzin": 8,
                   "dimdsep": ord("."), "dimlunit": 2, "dimtxsty": TEXT_STYLE_NAME}
        doc.dimstyles.new(DIMSTYLE, dxfattribs=dimvars)
        doc.header["$DIMSTYLE"] = DIMSTYLE
        doc.header["$TEXTSTYLE"] = TEXT_STYLE_NAME
        self.doc = doc
        self.msp = doc.modelspace()
        self.encoding: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()

    def _attribs(self, e: Any) -> dict[str, Any]:
        a: dict[str, Any] = {"layer": self.nm(e.layer)}
        if e.color is not None:
            a["color"] = e.color
        if e.linetype is not None:
            a["linetype"] = e.linetype if e.linetype != "CONTINUOUS" else "Continuous"
        return a

    @staticmethod
    def _tag(entity: Any, eid: str, role: str, line: int | None = None) -> None:
        tags = [(1000, f"id={eid}"), (1000, f"role={role}")]
        if line is not None:
            tags.append((1000, f"line={line}"))
        entity.set_xdata(APPID, tags)

    def _mtext(self, layout: Any, text: str, attrs: dict[str, Any], *, eid: str | None, role: str) -> list[Any]:
        """One MTEXT, or one per line when the text has line breaks and characters beyond Latin-1: an MTEXT
        holding both does not read back from DWG R2000 (verified for W6). Lines keep their id + line number."""
        lines = text.split("\n")
        if len(lines) == 1 or all(ord(ch) < 256 for ch in text):
            mt = layout.add_mtext(text.replace("\n", "\\P"), dxfattribs=attrs)
            if eid is not None:
                self._tag(mt, eid, role)
            return [mt]
        out = []
        h = float(attrs.get("char_height", 3.5))
        ins = attrs["insert"]
        rot = math.radians(float(attrs.get("rotation", 0.0)))
        for i, ln in enumerate(lines):
            if not ln.strip() and i:
                continue  # a blank line is its line number (rebuilt on import); an empty MTEXT does not read back
            off = i * h * 5.0 / 3.0  # MTEXT default line spacing
            at = (ins[0] + off * math.sin(rot), ins[1] - off * math.cos(rot))
            mt = layout.add_mtext(ln, dxfattribs={**attrs, "insert": at})
            if eid is not None:
                self._tag(mt, eid, role if i == 0 else "mtext_line", line=i)
            out.append(mt)
        if eid is not None:
            self.encoding.append({"id": eid, "what": "multi-line text with CJK characters", "written_as": f"{len(lines)} MTEXT lines",
                                  "reason": "an MTEXT with line breaks and non-Latin-1 characters does not read back from DWG R2000"})
        return out

    def layers(self) -> None:
        doc = self.doc
        for lay in self.state.layers:
            lt = "Continuous" if lay.linetype == "CONTINUOUS" else lay.linetype
            if lay.name == "0":
                row = doc.layers.get("0")
                row.dxf.color = lay.color
                row.dxf.linetype = lt
                row.dxf.lineweight = lay.lineweight
            else:
                row = doc.layers.add(self.nm(lay.name), color=lay.color, linetype=lt, lineweight=lay.lineweight)
            if not lay.visible:
                row.freeze()  # an "off" layer (negative colour) does not read back from DWG R2000; frozen does
            if lay.locked:
                row.lock()
        if any(not lay.visible for lay in self.state.layers):
            self.encoding.append({"what": "hidden layers", "written_as": "frozen layers",
                                  "reason": "an off layer (negative colour) does not read back from DWG R2000"})
        if any(s.draw_frame for s in self.state.sheets):
            for name, color in FRAME_LAYERS.items():
                if name not in doc.layers:
                    doc.layers.add(name, color=color, lineweight=35 if name == "PR_FRAME" else 18)

    def write(self, layout: Any, e: Any, tag: bool = True) -> None:
        a = self._attribs(e)
        t = e.type
        out: list[Any] = []
        if t in ("text", "mtext") and not e.text.strip():
            self.encoding.append({"id": e.id, "what": "empty text", "written_as": "nothing", "reason": "an empty text shows nothing "
                                  "and does not read back from DWG R2000"})
            return
        if t == "line":
            out.append(layout.add_line(e.p1, e.p2, dxfattribs=a))
        elif t == "polyline":
            bul = e.bulges or [0.0] * len(e.points)
            out.append(layout.add_lwpolyline([(p[0], p[1], b) for p, b in zip(e.points, bul)], format="xyb", close=e.closed, dxfattribs=a))
        elif t == "circle":
            out.append(layout.add_circle(e.center, e.radius, dxfattribs=a))
        elif t == "arc":
            out.append(layout.add_arc(e.center, e.radius, e.start_angle, e.end_angle, dxfattribs=a))
        elif t == "text":
            txt = layout.add_text(e.text, height=e.height, rotation=e.rotation, dxfattribs={**a, "style": TEXT_STYLE_NAME})
            align = {"left": TextEntityAlignment.LEFT, "center": TextEntityAlignment.CENTER, "right": TextEntityAlignment.RIGHT}[e.align]
            txt.set_placement(e.insert, align=align)
            out.append(txt)
        elif t == "mtext":
            attrs = {**a, "style": TEXT_STYLE_NAME, "char_height": e.height, "insert": e.insert,
                     "attachment_point": dxfconst.MTEXT_TOP_LEFT}
            if e.width:
                attrs["width"] = e.width
            if e.rotation:
                attrs["rotation"] = e.rotation
            self._mtext(layout, e.text, attrs, eid=e.id if tag else None, role="entity")
        elif t == "leader":
            ld = layout.add_leader([tuple(p) for p in e.points], dimstyle=DIMSTYLE, dxfattribs=a)
            out.append(ld)
            if e.text:
                end = e.points[-1]
                prev = e.points[-2]
                right = end[0] >= prev[0]
                gap = e.height * 0.5
                self._mtext(layout, e.text, {
                    **a, "style": TEXT_STYLE_NAME, "char_height": e.height,
                    "insert": (end[0] + (gap if right else -gap), end[1] + e.height / 2.0),
                    "attachment_point": dxfconst.MTEXT_TOP_LEFT if right else dxfconst.MTEXT_TOP_RIGHT},
                    eid=e.id if tag else None, role="leader_text")
        elif t == "dimension":
            out.append(self._dimension(layout, e, a))
        elif t == "hatch":
            out.append(self._hatch(layout, e, a))
        elif t == "insert":
            out.append(layout.add_blockref(self.nm(e.block), e.insert, dxfattribs={**a, "rotation": e.rotation, "xscale": e.xscale,
                                                                          "yscale": e.yscale}))
        self.counts[t] += 1
        if tag:
            role = f"dim:{e.dimtype}" if t == "dimension" else "entity"
            for o in out:
                self._tag(o, e.id, role)

    def _dimension(self, layout: Any, e: Dimension, a: dict[str, Any]) -> Any:
        text = _dxf_dim_text(e.text_override if e.text_override not in (None, "") else _default_text(e))
        override = {"dimlfac": e.dimlfac} if abs(e.dimlfac - 1.0) > 1e-12 and e.dimtype != "angular" else None
        if e.dimtype == "linear":
            assert e.p1 and e.p2 and e.location
            dim = layout.add_linear_dim(base=e.location, p1=e.p1, p2=e.p2, angle=e.angle, text=text, dimstyle=DIMSTYLE,
                                        override=override, dxfattribs=a)
        elif e.dimtype == "aligned":
            assert e.p1 and e.p2 and e.location
            dim = layout.add_aligned_dim(p1=e.p1, p2=e.p2, distance=_aligned_distance(e.p1, e.p2, e.location), text=text,
                                         dimstyle=DIMSTYLE, override=override, dxfattribs=a)
        elif e.dimtype in ("diameter", "radius"):
            assert e.center and e.radius
            u = _unit(e.angle)
            if e.dimtype == "diameter":
                p1 = (e.center[0] - u[0] * e.radius, e.center[1] - u[1] * e.radius)
            else:
                p1 = e.center
            p2 = (e.center[0] + u[0] * e.radius, e.center[1] + u[1] * e.radius)
            dim = layout.add_aligned_dim(p1=p1, p2=p2, distance=0.0001, text=text, dimstyle=DIMSTYLE, override=override, dxfattribs=a)
            self.encoding.append({"id": e.id, "what": f"{e.dimtype} dimension", "written_as": "aligned DIMENSION across the "
                                  + ("circle" if e.dimtype == "diameter" else "radius") + f" with text {text!r}",
                                  "reason": "DIAMETER / RADIUS dimensions do not read back from DWG R2000 (ezdwg)"})
        else:
            assert e.center and e.p1 and e.p2 and e.location
            r = max(_dist(e.location, e.center), 1e-3)
            a2 = math.atan2(e.p2[1] - e.center[1], e.p2[0] - e.center[0])
            on_arc = (e.center[0] + r * math.cos(a2), e.center[1] + r * math.sin(a2))
            dim = layout.add_angular_dim_2l(base=on_arc, line1=(e.center, e.p1), line2=(e.center, on_arc), text=text,
                                            dimstyle=DIMSTYLE, dxfattribs=a)
            self.encoding.append({"id": e.id, "what": "angular dimension", "written_as": "two-line angular DIMENSION whose second "
                                  "line ends on the dimension arc", "reason": "DWG R2000 reads the arc location as the second line's end"})
        dim.render()
        return dim.dimension

    def _hatch(self, layout: Any, e: Hatch, a: dict[str, Any]) -> Any:
        h = layout.add_hatch(color=e.color or 256, dxfattribs={"layer": self.nm(e.layer)})
        for i, lp in enumerate(e.loops):
            flags = dxfconst.BOUNDARY_PATH_EXTERNAL if i == 0 else dxfconst.BOUNDARY_PATH_OUTERMOST
            if lp.points is not None:
                bul = lp.bulges or [0.0] * len(lp.points)
                h.paths.add_polyline_path([(p[0], p[1], b) for p, b in zip(lp.points, bul)], is_closed=True, flags=flags)
            else:
                assert lp.center is not None and lp.radius is not None
                ep = h.paths.add_edge_path(flags=flags)
                ep.add_arc(lp.center, lp.radius, 0.0, 360.0)
        if e.solid:
            h.set_solid_fill(color=e.color or 256, style=0)
        else:
            h.set_pattern_fill(e.pattern, color=e.color or 256, scale=e.scale, angle=e.angle, style=0)
        return h

    def blocks(self) -> None:
        for b in self.state.blocks:
            blk = self.doc.blocks.new(self.nm(b.name), base_point=b.base)
            for be in b.entities:
                self.write(blk, be, tag=False)

    def frames(self) -> None:
        for sh in self.state.sheets:
            if sh.draw_frame:
                self._frame(sh)

    def conflict_notes(self) -> None:
        """Every conflict as a one-line note on layer PR_NOTES (kept visible in CAD); its JSON travels as base64 XDATA so a
        re-import restores the conflict with all readings and sources."""
        if not self.state.conflicts:
            return
        if NOTES_LAYER not in self.doc.layers:
            self.doc.layers.add(NOTES_LAYER, color=1, lineweight=18)
        sh = self.state.sheets[0] if self.state.sheets else None
        k = 1.0 / sh.scale if sh else 1.0
        ox, oy = sh.origin if sh else (0.0, 0.0)
        top = (sh.height or 297.0) if sh else 297.0
        for i, c in enumerate(self.state.conflicts):
            parts = [f"{v.text or v.value}" + (f" ({v.kind}, {v.source})" if v.source else f" ({v.kind})") for v in c.values]
            line = f"{c.id} {c.subject}: " + " / ".join(parts) + (" [open]" if c.status == "open" else " [resolved]")
            t = self.msp.add_text(line[:250], height=2.5 * k, dxfattribs={"layer": NOTES_LAYER, "style": TEXT_STYLE_NAME})
            t.set_placement((ox + 15.0 * k, oy + (top - 15.0 - 5.0 * i) * k), align=TextEntityAlignment.LEFT)
            # base64: LibreDWG rewrites backslash escapes inside XDATA strings (\u -> \U+), so no backslashes here
            blob = base64.b64encode(json.dumps(c.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode()).decode()
            tags = [(1000, f"id={c.id}"), (1000, "role=conflict")] + [(1000, "j=" + blob[j:j + 200]) for j in range(0, len(blob), 200)]
            t.set_xdata(APPID, tags)

    def _frame(self, sh: Sheet) -> None:
        assert sh.width is not None and sh.height is not None
        k = 1.0 / sh.scale
        ox, oy = sh.origin

        def P(x: float, y: float) -> P2:
            return (ox + x * k, oy + y * k)

        def line(x0: float, y0: float, x1: float, y1: float, layer: str, role: str) -> None:
            ln = self.msp.add_line(P(x0, y0), P(x1, y1), dxfattribs={"layer": layer})
            self._tag(ln, f"{sh.id}.grid", role)

        def text(s: str, x: float, y: float, h: float, layer: str, role: str, eid: str) -> None:
            t = self.msp.add_text(s, height=h * k, dxfattribs={"layer": layer, "style": TEXT_STYLE_NAME})
            t.set_placement(P(x, y), align=TextEntityAlignment.LEFT)
            self._tag(t, eid, role)

        w, h = sh.width, sh.height
        border = self.msp.add_lwpolyline([P(0, 0), P(w, 0), P(w, h), P(0, h)], close=True, dxfattribs={"layer": "PR_FRAME"})
        self._tag(border, f"{sh.id}.border", f"sheet:{sh.id}:{sh.size}:{sh.orientation}:{fmt(sh.scale, 9)}:{fmt(w, 6)}:{fmt(h, 6)}")
        m = FRAME_MARGIN
        inner = self.msp.add_lwpolyline([P(m, m), P(w - m, m), P(w - m, h - m), P(m, h - m)], close=True,
                                        dxfattribs={"layer": "PR_FRAME", "lineweight": 50})
        self._tag(inner, f"{sh.id}.frame", f"frame:{sh.id}")
        x0, y0 = w - m - TITLE_W, m
        fields = dict(sh.title_block)
        derived = {key for key in ("scale", "units") if key not in fields}  # shown, but not stored as entered values
        fields.setdefault("scale", scale_text(sh.scale))
        fields.setdefault("units", "mm")
        line(x0, y0 + TITLE_H, x0 + TITLE_W, y0 + TITLE_H, "PR_TITLE", f"frame:{sh.id}")
        line(x0, y0, x0, y0 + TITLE_H, "PR_TITLE", f"frame:{sh.id}")
        for ri, row in enumerate(_TITLE_ROWS):
            yb = y0 + ri * TITLE_ROW_H
            if ri:
                line(x0, yb, x0 + TITLE_W, yb, "PR_TITLE", f"frame:{sh.id}")
            xc = x0
            for ci, (cw, key, label) in enumerate(row):
                if ci:
                    line(xc, yb, xc, yb + TITLE_ROW_H, "PR_TITLE", f"frame:{sh.id}")
                text(label, xc + 1.0, yb + TITLE_ROW_H - 2.6, 1.8, "PR_TITLE", f"frame:{sh.id}", f"{sh.id}.label")
                value = fields.get(key, "")
                if value:
                    text(value, xc + 1.0, yb + 1.2, 3.0 if key in ("title", "drawing_number") else 2.5, "PR_TITLE",
                         f"frame:{sh.id}" if key in derived else f"title:{sh.id}:{key}", f"{sh.id}.title.{key}")
                xc += cw
        if sh.bom:
            cols = [c for c in sh.bom_columns if c in BOM_WIDTHS] or list(BOM_COLUMNS)
            widths = [BOM_WIDTHS[c] for c in cols]
            total = sum(widths)
            xb = x0 + TITLE_W - total
            yb = y0 + TITLE_H
            rows = len(sh.bom) + 1
            for r in range(rows + 1):
                line(xb, yb + r * BOM_ROW_H, xb + total, yb + r * BOM_ROW_H, "PR_BOM", f"frame:{sh.id}")
            xc = xb
            for cw in [0.0, *widths]:
                xc += cw
                line(xc, yb, xc, yb + rows * BOM_ROW_H, "PR_BOM", f"frame:{sh.id}")
            xc = xb
            for c, cw in zip(cols, widths):
                text(BOM_LABELS.get(c, c), xc + 1.0, yb + 2.0, 2.5, "PR_BOM", f"frame:{sh.id}", f"{sh.id}.label")
                xc += cw
            for ri, row in enumerate(sh.bom, start=1):
                xc = xb
                for c, cw in zip(cols, widths):
                    v = row.get(c, "")
                    if v:
                        text(v, xc + 1.0, yb + ri * BOM_ROW_H + 2.0, 2.5, "PR_BOM", f"bom:{sh.id}:{ri}:{c}", f"{sh.id}.bom.{ri}.{c}")
                    xc += cw
        self.counts["frame"] += 1

    def bytes(self) -> bytes:
        stream = io.StringIO()
        with fixed_metadata():
            self.doc.write(stream)
        return stream.getvalue().encode("utf-8")


def _aligned_distance(p1: P2, p2: P2, loc: P2) -> float:
    """Signed offset of ``loc`` from the p1→p2 line (positive to the left), as ezdxf's aligned dims expect."""
    d = _sub(p2, p1)
    L = _len(d) or 1.0
    return ((loc[0] - p1[0]) * -d[1] + (loc[1] - p1[1]) * d[0]) / L or 0.0001


def _dxf_dim_text(text: str) -> str:
    return text.replace("Ø", "%%c").replace("°", "%%d").replace("±", "%%p")


def export_dxf(state: DraftState, *, ascii_names: bool = False) -> DxfExport:
    """Native DXF R2010 of the state (deterministic bytes for the same state). ``ascii_names`` writes layer / block
    names beyond ASCII as reversible aliases (the variant converted to DWG R2000)."""
    w = _Writer(state, ascii_names=ascii_names)
    if ascii_names and needs_ascii_names(state):
        w.encoding.append({"what": "layer / block names beyond ASCII", "written_as": f"{ALIAS_PREFIX}… aliases in the DWG",
                           "reason": "DWG R2000 (ANSI_1252) cannot hold them; the DXF keeps the real names, import decodes the aliases"})
    w.layers()
    w.blocks()
    for e in state.entities:
        w.write(w.msp, e)
    w.frames()
    w.conflict_notes()
    return DxfExport(dxf=w.bytes(), encoding=w.encoding, counts=dict(sorted(w.counts.items())))


# --------------------------------------------------------------------------- #
# DXF import
# --------------------------------------------------------------------------- #

_INSUNITS_MM = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4, 13: 0.001, 14: 100.0}


@dataclass
class DxfImport:
    state: DraftState
    units: dict[str, Any]
    units_known: bool
    unsupported: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    counts: dict[str, int]
    own_file: bool  # written by this module (XDATA ids)


class _Inventory:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(self, dxftype: str, preserved: bool, handling: str, handle: str | None = None, where: str = "model space") -> None:
        key = (dxftype, handling)
        row = self.rows.setdefault(key, {"type": dxftype, "count": 0, "preserved": preserved, "handling": handling,
                                         "where": where, "handles": []})
        row["count"] += 1
        if handle and len(row["handles"]) < 10:
            row["handles"].append(handle)

    def out(self) -> list[dict[str, Any]]:
        return sorted(self.rows.values(), key=lambda r: (r["preserved"], r["type"], r["handling"]))


def _xdata(e: Any) -> dict[str, str]:
    try:
        if not e.has_xdata(APPID):
            return {}
        out: dict[str, str] = {}
        for code, value in e.get_xdata(APPID):
            if code == 1000 and isinstance(value, str) and "=" in value:
                k, v = value.split("=", 1)
                out[k] = out.get(k, "") + v if k == "j" else v
        return out
    except Exception:  # noqa: BLE001 — malformed XDATA is ignored, the entity is imported by content
        return {}


def _plain(raw: str, *, mtext: bool) -> str:
    from .drawing_evidence import clean_text

    return clean_text(raw or "", mtext=mtext)[0]


def _dim_override(raw: str) -> str | None:
    """A DIMENSION's text group as our override (None = measured value)."""
    if raw in ("", "<>"):
        return None
    from .drawing_evidence import clean_text

    s, _ = clean_text(raw.replace("<>", _PLACEHOLDER), mtext=True)
    s = s.replace(_PLACEHOLDER, "<>")
    return s or None


_PLACEHOLDER = "\ue000"  # private-use character: survives text cleaning (control bytes do not)


def import_dxf(data: bytes, *, source_id: str = "S-1", budget_s: float | None = None) -> DxfImport:
    """Map a DXF onto a :class:`DraftState` (see the module docstring). Raises DraftingError for unreadable files,
    ``too_many_entities`` (with the counts) as soon as more than :data:`MAX_ENTITIES` entities are converted, and
    ``import_budget_exceeded`` when the conversion takes longer than ``budget_s`` seconds."""
    import time

    from .drawing_evidence import DrawingEvidenceError, extract_evidence, read_dxf_bytes

    started = time.monotonic()

    def over(stage: str) -> None:
        spent = time.monotonic() - started
        if budget_s is not None and spent > budget_s:
            raise DraftingError("import_budget_exceeded", f"the import took longer than its budget of {budget_s:g} s ({stage}); nothing was saved",
                                {"budget_s": budget_s, "seconds": round(spent, 1), "stage": stage})

    try:
        loaded = read_dxf_bytes(data)
    except DrawingEvidenceError as exc:
        raise DraftingError(getattr(exc, "code", "dxf_unreadable"), str(exc)) from exc
    doc = loaded.doc
    model_space = sum(1 for e in doc.modelspace() if e.dxftype() != "VIEWPORT")
    over("read")
    warnings = [w.model_dump(mode="json") for w in loaded.warnings]
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    k = _INSUNITS_MM.get(insunits)
    units_known = k is not None
    if k is None:
        k = 1.0
        warnings.append({"code": "units_unknown", "message": f"$INSUNITS={insunits}: no drawing units; values kept as stored (scale unknown)"})
    elif insunits != 4:
        warnings.append({"code": "units_converted", "message": f"drawing units $INSUNITS={insunits} converted to mm (×{k})"})
    units = {"insunits": insunits, "mm_per_unit": k if units_known else None}

    evidence = None
    try:
        evidence = extract_evidence(data)
    except Exception as exc:  # noqa: BLE001 — readings are an extra; the geometry import still runs
        warnings.append({"code": "evidence_unavailable", "message": f"dimension readings not extracted: {type(exc).__name__}: {str(exc)[:200]}"})
    over("dimension readings")
    dim_ev = {d.id: d for d in (evidence.dimensions if evidence else [])}

    state = DraftState(sheets=[], layers=[], entities=[], blocks=[])
    inv = _Inventory()
    counts: Counter[str] = Counter()
    own = False

    def S(p: Any) -> P2:
        return _cp((float(p[0]) * k, float(p[1]) * k))

    # layers
    for lay in doc.layers:
        name = decode_name(lay.dxf.name)
        try:
            lt = str(lay.dxf.get("linetype", "Continuous")).upper()
            state.layers.append(Layer(name=name, color=min(max(abs(int(lay.dxf.get("color", 7) or 7)), 1), 255),
                                      linetype=lt if lt in LINETYPE_NAMES else "CONTINUOUS",
                                      lineweight=int(lay.dxf.get("lineweight", -3)) if int(lay.dxf.get("lineweight", -3)) in LINEWEIGHTS else -3,
                                      visible=not (lay.is_off() or lay.is_frozen()), locked=lay.is_locked()))
            if lt not in LINETYPE_NAMES:
                inv.add("LINETYPE", False, f"linetype {lt} shown as continuous", where=f"layer {name}")
        except ValidationError:
            inv.add("LAYER", False, "layer name not supported; its entities use layer 0", where=name)
    if not any(lay.name == "0" for lay in state.layers):
        state.layers.insert(0, Layer(name="0"))
    known_layers = {lay.name for lay in state.layers}
    # Our generated frame layers come back with the frame (re-created on export); kept only if entities use them.
    state.layers = [lay for lay in state.layers if lay.name.upper() not in (*FRAME_LAYERS, NOTES_LAYER, "DEFPOINTS")]

    def layer_of(e: Any) -> str:
        name = decode_name(e.dxf.get("layer", "0") or "0")
        return name if name in known_layers else "0"

    def common(e: Any, origin: Origin = "source") -> dict[str, Any]:
        d: dict[str, Any] = {"layer": layer_of(e), "origin": origin, "source_ref": f"{source_id}:{e.dxf.handle}"}
        color = e.dxf.get("color", 256)
        if color not in (0, 256, None) and 1 <= int(color) <= 255:
            d["color"] = int(color)
        lt = str(e.dxf.get("linetype", "BYLAYER")).upper()
        if lt not in ("BYLAYER", "BYBLOCK") and lt in LINETYPE_NAMES:
            d["linetype"] = lt
        return d

    sheets: dict[str, dict[str, Any]] = {}
    title_values: dict[str, dict[str, str]] = defaultdict(dict)
    bom_values: dict[str, dict[int, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    leader_texts: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    provenance: dict[str, AnnotationProvenance] = {}

    def convert(e: Any, *, in_block: bool = False) -> list[dict[str, Any]]:
        """DXF entity -> entity dicts (without ids); records the inventory."""
        t = e.dxftype()
        c = common(e)
        h = e.dxf.handle
        where = "block" if in_block else "model space"
        try:
            if t == "LINE":
                return [{**c, "type": "line", "p1": S(e.dxf.start), "p2": S(e.dxf.end)}]
            if t == "LWPOLYLINE":
                pts = list(e.get_points("xyb"))
                if len(pts) < 2:
                    inv.add(t, False, "fewer than two vertices", h, where)
                    return []
                bul = [float(p[2]) for p in pts]
                return [{**c, "type": "polyline", "points": [S(p) for p in pts], "bulges": bul if any(bul) else None,
                         "closed": bool(e.closed)}]
            if t == "POLYLINE":
                if not e.is_2d_polyline:
                    inv.add(t, False, "3D polyline / mesh not supported", h, where)
                    return []
                verts = list(e.vertices)
                if len(verts) < 2:
                    inv.add(t, False, "fewer than two vertices", h, where)
                    return []
                bul = [float(v.dxf.get("bulge", 0.0)) for v in verts]
                return [{**c, "type": "polyline", "points": [S(v.dxf.location) for v in verts], "bulges": bul if any(bul) else None,
                         "closed": bool(e.is_closed)}]
            if t == "CIRCLE":
                return [{**c, "type": "circle", "center": S(e.dxf.center), "radius": _clean(float(e.dxf.radius) * k)}]
            if t == "ARC":
                return [{**c, "type": "arc", "center": S(e.dxf.center), "radius": _clean(float(e.dxf.radius) * k),
                         "start_angle": float(e.dxf.start_angle), "end_angle": float(e.dxf.end_angle)}]
            if t in ("TEXT", "MTEXT") and not _plain(e.dxf.text if t == "TEXT" else e.text, mtext=t == "MTEXT").strip() \
                    and not _xdata(e).get("id"):
                inv.add(t, False, "empty text (shows nothing) not kept", h, where)
                return []
            if t == "TEXT":
                halign = int(e.dxf.get("halign", 0) or 0)
                align = {1: "center", 2: "right", 4: "center"}.get(halign, "left")
                at = e.dxf.align_point if (halign or e.dxf.get("valign", 0)) and e.dxf.hasattr("align_point") else e.dxf.insert
                return [{**c, "type": "text", "insert": S(at), "text": _plain(e.dxf.text, mtext=False), "height": float(e.dxf.height) * k,
                         "rotation": float(e.dxf.get("rotation", 0.0)), "align": align}]
            if t == "MTEXT":
                height = float(e.dxf.get("char_height", 0.0) or 0.0) * k
                if height <= 0:
                    height = 3.5
                    inv.add(t, True, "no text height in the source; 3.5 mm used", h, where)
                d = {**c, "type": "mtext", "insert": S(e.dxf.insert), "text": _plain(e.text, mtext=True),
                     "height": height, "rotation": float(e.get_rotation())}
                if e.dxf.get("width"):
                    d["width"] = float(e.dxf.width) * k
                return [d]
            if t == "LEADER":
                verts = [S(v) for v in e.vertices]
                if len(verts) < 2:
                    inv.add(t, False, "fewer than two vertices", h, where)
                    return []
                return [{**c, "type": "leader", "points": verts}]
            if t == "DIMENSION":
                return _dimension(e, c)
            if t == "HATCH":
                return _hatch(e, c)
            if t == "INSERT":
                out = [{**c, "type": "insert", "block": decode_name(e.dxf.name), "insert": S(e.dxf.insert), "rotation": float(e.dxf.get("rotation", 0.0)),
                        "xscale": float(e.dxf.get("xscale", 1.0)), "yscale": float(e.dxf.get("yscale", 1.0))}]
                for att in e.attribs:
                    out.append({**common(att), "type": "text", "insert": S(att.dxf.insert), "text": _plain(att.dxf.text, mtext=False),
                                "height": float(att.dxf.height) * k, "rotation": float(att.dxf.get("rotation", 0.0))})
                    inv.add("ATTRIB", True, "value kept as TEXT (attribute definition not kept)", att.dxf.handle, where)
                if e.dxf.get("column_count", 1) > 1 or e.dxf.get("row_count", 1) > 1:
                    inv.add("MINSERT", False, "array repetition not kept (one insert)", h, where)
                return out
            if t in ("SPLINE", "ELLIPSE"):
                pts = [S(p) for p in e.flattening(0.01 / k if k else 0.01)]
                if len(pts) < 2:
                    inv.add(t, False, "could not be approximated", h, where)
                    return []
                inv.add(t, True, "approximated as a polyline (not the native curve)", h, where)
                return [{**c, "type": "polyline", "points": pts, "closed": False, "origin": "source"}]
            if t in ("SOLID", "TRACE"):
                v = [S(e.dxf.get(f"vtx{i}", (0, 0))) for i in range(4)]
                loop = [v[0], v[1], v[3], v[2]] if _dist(v[2], v[3]) > TOL else [v[0], v[1], v[2]]
                inv.add(t, True, "kept as a solid-filled hatch", h, where)
                return [{**c, "type": "hatch", "loops": [{"points": loop}], "solid": True}]
            if t == "MULTILEADER":
                try:
                    ctx = e.context
                    lines = [S(v) for ld in ctx.leaders for ln in ld.lines for v in ln.vertices]
                    txt = _plain(ctx.mtext.default_content, mtext=True) if ctx.mtext else ""
                    if len(lines) >= 2:
                        inv.add(t, True, "kept as a leader with its text", h, where)
                        return [{**c, "type": "leader", "points": lines, "text": txt}]
                except Exception:  # noqa: BLE001
                    pass
                inv.add(t, False, "multileader layout not readable", h, where)
                return []
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            inv.add(t, False, f"unreadable: {type(exc).__name__}", h, where)
            return []
        inv.add(t, False, "entity type not supported by the 2D editor", h, where)
        return []

    def _dimension(e: Any, c: dict[str, Any]) -> list[dict[str, Any]]:
        dt = e.dimtype
        dxf = e.dxf
        d: dict[str, Any] = {**c, "type": "dimension"}
        override = _dim_override(dxf.get("text", "") or "")
        if override is not None:
            d["text_override"] = override
        x = _xdata(e)
        role = x.get("role", "")
        try:
            dimlfac = float(e.get_dim_style().get("dimlfac", 1.0) or 1.0)
            ov = e.override().get("dimlfac", None)
            if ov:
                dimlfac = float(ov)
        except Exception:  # noqa: BLE001
            dimlfac = 1.0
        if abs(dimlfac - 1.0) > 1e-12 and dt not in (2, 5):
            d["dimlfac"] = dimlfac
        if dt in (0, 1):
            p1, p2, loc = S(dxf.defpoint2), S(dxf.defpoint3), S(dxf.defpoint)
            if role in ("dim:diameter", "dim:radius"):
                ang = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
                if role == "dim:diameter":
                    center = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
                    radius = _dist(p1, p2) / 2.0
                else:
                    center, radius = p1, _dist(p1, p2)
                d.update(dimtype=role.split(":")[1], center=_cp(center), radius=_clean(radius), angle=_clean(_norm_angle(ang)))
                if override in ("Ø<>", "R<>"):
                    d.pop("text_override", None)
                return [d]
            # ezdxf writes aligned dimensions as rotated linear ones (type 0); our XDATA role restores the kind.
            if dt == 1 or role == "dim:aligned":
                d.update(dimtype="aligned", p1=p1, p2=p2, location=loc)
            else:
                d.update(dimtype="linear", p1=p1, p2=p2, location=loc, angle=float(dxf.get("angle", 0.0)))
            return [d]
        if dt in (3, 4):
            far, near = S(dxf.defpoint), S(dxf.defpoint4)
            if dt == 3:
                center = ((far[0] + near[0]) / 2.0, (far[1] + near[1]) / 2.0)
                radius = _dist(far, near) / 2.0
                ang = math.degrees(math.atan2(near[1] - center[1], near[0] - center[0]))
            else:
                center, radius = far, _dist(far, near)
                ang = math.degrees(math.atan2(near[1] - far[1], near[0] - far[0]))
            d.update(dimtype="diameter" if dt == 3 else "radius", center=_cp(center), radius=_clean(radius), angle=_clean(_norm_angle(ang)))
            if override in ("Ø<>", "R<>"):
                d.pop("text_override", None)
            return [d]
        if dt == 2:
            l1a, l1b, l2a, l2b = S(dxf.defpoint2), S(dxf.defpoint3), S(dxf.defpoint4), S(dxf.defpoint)
            vertex = _intersect(l1a, l1b, l2a, l2b)
            if vertex is None:
                inv.add("DIMENSION", False, "angular dimension with parallel lines", dxf.handle)
                return []
            p1 = l1b if _dist(l1b, vertex) >= _dist(l1a, vertex) else l1a
            p2 = l2b if _dist(l2b, vertex) >= _dist(l2a, vertex) else l2a
            loc = S(dxf.defpoint5) if dxf.hasattr("defpoint5") else p2
            d.update(dimtype="angular", center=_cp(vertex), p1=p1, p2=p2, location=loc)
            if override == "<>°":
                d.pop("text_override", None)
            return [d]
        if dt == 5:
            d.update(dimtype="angular", center=S(dxf.defpoint4), p1=S(dxf.defpoint2), p2=S(dxf.defpoint3), location=S(dxf.defpoint))
            if override == "<>°":
                d.pop("text_override", None)
            return [d]
        inv.add("DIMENSION", False, f"dimension type {dt} (ordinate / arc length) not supported", dxf.handle)
        return []

    def _hatch(e: Any, c: dict[str, Any]) -> list[dict[str, Any]]:
        from ezdxf import path as ezpath
        from ezdxf.entities.boundary_paths import EdgePath, PolylinePath

        loops: list[dict[str, Any]] = []
        approximated = False
        for path in e.paths:
            if isinstance(path, PolylinePath):
                verts = list(path.vertices)
                bul = [float(v[2]) for v in verts]
                loops.append({"points": [S(v) for v in verts], "bulges": bul if any(bul) else None})
            elif isinstance(path, EdgePath):
                edges = list(path.edges)
                if len(edges) == 1 and edges[0].type.name == "ARC" and abs(_arc_sweep(edges[0].start_angle, edges[0].end_angle) - 360.0) < 1e-6:
                    loops.append({"center": S(edges[0].center), "radius": float(edges[0].radius) * k})
                    continue
                if any(ed.type.name != "LINE" for ed in edges):
                    approximated = True
                pts: list[P2] = []
                try:
                    flat = list(ezpath.from_hatch_edge_path(path).flattening(0.01))
                except Exception:  # noqa: BLE001 — an unreadable edge path is reported below
                    flat = []
                for p in flat:
                    q = S(p)
                    if not pts or _dist(pts[-1], q) > TOL:
                        pts.append(q)
                if len(pts) >= 2 and _dist(pts[0], pts[-1]) < TOL:
                    pts = pts[:-1]
                if len(pts) >= 3:
                    loops.append({"points": pts})
        if not loops:
            inv.add("HATCH", False, "no readable boundary", e.dxf.handle)
            return []
        if approximated:
            inv.add("HATCH", True, "edge boundary approximated by a polygon", e.dxf.handle)
        pat = (e.dxf.get("pattern_name", "ANSI31") or "ANSI31").upper()
        solid = bool(e.dxf.get("solid_fill", 0)) or pat == "SOLID"
        if not solid and pat not in HATCH_PATTERNS:
            inv.add("HATCH", True, f"pattern {pat} shown as ANSI31", e.dxf.handle)
            pat = "ANSI31"
        return [{**c, "type": "hatch", "loops": loops, "pattern": pat if not solid else "ANSI31", "solid": solid,
                 "scale": float(e.dxf.get("pattern_scale", 1.0) or 1.0), "angle": float(e.dxf.get("pattern_angle", 0.0) or 0.0) % 360.0}]

    # blocks used by model-space inserts (not anonymous dimension blocks / layouts)
    used_blocks = {e.dxf.name for e in doc.modelspace() if e.dxftype() == "INSERT"}
    stack = list(used_blocks)
    seen_blocks: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen_blocks:
            continue
        seen_blocks.add(name)
        blk = doc.blocks.get(name)
        if blk is None:
            continue
        ents: list[Any] = []
        for be in blk:
            bt = be.dxftype()
            if bt == "INSERT":
                inv.add("INSERT", False, "nested block reference inside a block not kept", be.dxf.handle, f"block {name}")
                continue
            if bt in ("DIMENSION", "LEADER"):
                inv.add(bt, False, "annotation inside a block not kept", be.dxf.handle, f"block {name}")
                continue
            if bt == "ATTDEF":
                inv.add("ATTDEF", False, "attribute definition not kept (values of inserts are kept as text)", be.dxf.handle, f"block {name}")
                continue
            for d in convert(be, in_block=True):
                d.pop("source_ref", None)
                d["id"] = f"B{len(ents) + 1}"
                try:
                    ents.append(_ENTITY_ADAPTER.validate_python(d))
                except ValidationError:
                    inv.add(bt, False, "invalid geometry", be.dxf.handle, f"block {name}")
        try:
            base = blk.block.dxf.get("base_point", (0, 0))
            state.blocks.append(Block(name=decode_name(name), base=S(base), entities=[x for x in ents if x.type in
                                      ("line", "polyline", "circle", "arc", "text", "mtext", "hatch")], origin="source"))
        except ValidationError:
            inv.add("BLOCK", False, f"block name {name!r} not supported; its inserts are dropped", where=name)
    valid_blocks = {b.name for b in state.blocks}

    extra_lines: dict[str, dict[int, str]] = defaultdict(dict)  # our multi-line MTEXT written one line per MTEXT
    restored_conflicts: list[Conflict] = []
    over("blocks")
    for n_seen, e in enumerate(doc.modelspace()):
        if n_seen % 256 == 0:
            over("model space")
        if len(pending) > MAX_ENTITIES:  # stop converting: the document could not be stored anyway
            check_size(len(pending), len(state.blocks), model_space_entities=model_space)
        x = _xdata(e)
        role = x.get("role", "")
        if role == "mtext_line" and x.get("id") and e.dxftype() == "MTEXT":
            own = True
            extra_lines[x["id"]][int(x.get("line", "0") or 0)] = _plain(e.text, mtext=True)
            continue
        if role == "conflict":
            own = True
            try:
                restored_conflicts.append(Conflict.model_validate(json.loads(base64.b64decode(x.get("j", "")).decode())))
            except (ValueError, ValidationError):
                inv.add("TEXT", False, "conflict note with unreadable data (kept as text only)", e.dxf.handle)
                pending.append({"data": {**common(e), "type": "text", "insert": S(e.dxf.insert), "text": _plain(e.dxf.text, mtext=False),
                                         "height": float(e.dxf.height) * k}, "handle": e.dxf.handle, "dxftype": "TEXT"})
            continue
        if role.startswith(("sheet:", "frame:", "title:", "bom:")) or role == "leader_text":
            own = True
            if role.startswith("sheet:"):
                parts = role.split(":")
                try:
                    sid, size, orient, sc, w, h = parts[1], parts[2], parts[3], float(parts[4]), float(parts[5]), float(parts[6])
                    pts = list(e.get_points("xy"))
                    sheets[sid] = {"id": sid, "size": size, "orientation": orient, "scale": sc, "width": w, "height": h,
                                   "origin": S(pts[0]), "draw_frame": True}
                except (IndexError, ValueError):
                    pass
            elif role.startswith("title:"):
                _, sid, key = role.split(":", 2)
                title_values[sid][key] = _plain(e.dxf.text, mtext=False)
            elif role.startswith("bom:"):
                _, sid, r, col = role.split(":", 3)
                bom_values[sid][int(r)][col] = _plain(e.dxf.text, mtext=False)
            elif role == "leader_text" and x.get("id"):
                leader_texts[x["id"]] = _plain(e.text, mtext=True)
            continue
        t = e.dxftype()
        if t == "INSERT" and decode_name(e.dxf.name) not in valid_blocks:
            inv.add("INSERT", False, f"reference to unsupported block {e.dxf.name!r}", e.dxf.handle)
            continue
        if t == "VIEWPORT":
            continue
        for i, d in enumerate(convert(e)):
            if i == 0 and x.get("id"):
                own = True
                d["id"] = x["id"]
            pending.append({"data": d, "handle": e.dxf.handle, "dxftype": t})
            counts[t] += 1

    # paper space: listed, not imported
    for layout in doc.layouts:
        if layout.is_modelspace:
            continue
        n = sum(1 for ent in layout if ent.dxftype() != "VIEWPORT")
        if n:
            inv.rows[("PAPER_SPACE", layout.name)] = {"type": "PAPER_SPACE", "count": n, "preserved": False,
                                                      "handling": f"layout {layout.name!r}: paper-space entities are not imported",
                                                      "where": "paper space", "handles": []}

    # Ids: our own XDATA ids are kept (first use wins); everything else gets the next free E-nnnn.
    explicit = {p["data"]["id"] for p in pending if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,39}", p["data"].get("id") or "")}
    used: set[str] = set()
    check_size(len(pending), len(state.blocks), model_space_entities=model_space)
    for n_done, p in enumerate(pending):
        if n_done % 256 == 0:
            over("entities")
        d = p["data"]
        eid = d.get("id")
        if not eid or eid in used or eid not in explicit:
            eid = next_id(state)
            while eid in explicit or eid in used:
                eid = next_id(state)
            d["id"] = eid
        used.add(eid)
        if d.get("type") == "leader" and eid in leader_texts:
            d["text"] = _join_lines(leader_texts[eid], extra_lines.get(eid, {}))
        elif d.get("type") == "mtext" and eid in extra_lines:
            d["text"] = _join_lines(d["text"], extra_lines[eid])
        try:
            ent = _ENTITY_ADAPTER.validate_python(d)
        except ValidationError as exc:
            inv.add(p["dxftype"], False, f"invalid geometry: {_first_error(exc)[:80]}", p["handle"])
            continue
        state.entities.append(ent)
        if ent.type == "dimension":
            ev = dim_ev.get(p["handle"])
            rec = AnnotationProvenance()
            if ev is not None:
                # the displayed number closest to the dimension's value ("4×Ø8" reads 8, not the count)
                value = min(ev.numbers, key=lambda n: abs(n - ev.value)) if ev.numbers and ev.value is not None else \
                    (ev.numbers[0] if ev.numbers else None)
                rec.original = Reading(text=ev.displayed or "", value=value, source=source_id, locator=f"handle {p['handle']}")
            if ent.text_override is not None:
                rec.override = OverrideRecord(text=ent.text_override, author="source")
            provenance[ent.id] = rec

    for sid, sh in sorted(sheets.items()):
        sh["title_block"] = {kk: v for kk, v in title_values.get(sid, {}).items() if kk in TITLE_FIELDS}
        rows = bom_values.get(sid, {})
        sh["bom"] = [rows[r] for r in sorted(rows)]
        if sh["size"] != "custom":
            sh.pop("width")
            sh.pop("height")
        try:
            state.sheets.append(Sheet.model_validate(sh))
        except ValidationError as exc:
            warnings.append({"code": "sheet_unreadable", "message": f"sheet {sid}: {_first_error(exc)}"})
    if not state.sheets:
        state.sheets.append(_sheet_from_evidence(evidence, state, source_id))

    state.provenance = provenance
    try:
        validate_structure(state)
    except DraftingError as exc:
        raise DraftingError("dxf_structure", f"the imported drawing is not consistent: {exc.message}", exc.details) from exc
    refresh_provenance(state)
    state.conflicts = restored_conflicts
    _import_conflicts(state, source_id)
    return DxfImport(state=state, units=units, units_known=units_known, unsupported=inv.out(), warnings=warnings,
                     counts=dict(sorted(counts.items())), own_file=own)


def _join_lines(first: str, rest: dict[int, str]) -> str:
    """Multi-line text written one MTEXT per line (blank lines skipped, line numbers kept) joined back."""
    if not rest:
        return first
    return "\n".join([first, *(rest.get(i, "") for i in range(1, max(rest) + 1))])


def _intersect(a1: P2, a2: P2, b1: P2, b2: P2) -> P2 | None:
    d1, d2 = _sub(a2, a1), _sub(b2, b1)
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-12:
        return None
    t = ((b1[0] - a1[0]) * d2[1] - (b1[1] - a1[1]) * d2[0]) / den
    return (a1[0] + t * d1[0], a1[1] + t * d1[1])


def _sheet_from_evidence(evidence: Any, state: DraftState, source_id: str) -> Sheet:
    """A sheet for a foreign drawing: its own border / title block stay entities (draw_frame False); title
    readings are kept with their text ids."""
    readings: dict[str, Reading] = {}
    origin: P2 = (0.0, 0.0)
    size, orientation = "custom", "landscape"
    width = height = None
    if evidence is not None and evidence.frames:
        f = evidence.frames[0]
        k = evidence.units.mm_per_unit or 1.0
        origin = (f.bbox[0] * k, f.bbox[1] * k)
        width, height = (f.bbox[2] - f.bbox[0]) * k, (f.bbox[3] - f.bbox[1]) * k
        if f.paper in SHEET_SIZES and (f.paper_factor or 1.0) == 1.0:
            size = f.paper
            orientation = f.orientation or ("landscape" if width >= height else "portrait")
        mapping = {"number": "drawing_number", "name": "title", "material": "material", "scale": "scale"}
        for key, fld in mapping.items():
            tf = getattr(f.title, key)
            if tf is not None and tf.value:
                readings[fld] = Reading(text=tf.value[:400], source=source_id, locator=f"text {tf.text_id}" if tf.text_id else tf.method)
    if width is None or height is None or width <= 0 or height <= 0:
        bb = bbox(state)
        if bb is not None:
            origin = (bb[0], bb[1])
            width, height = max(bb[2] - bb[0], 1.0), max(bb[3] - bb[1], 1.0)
        else:
            width, height = SHEET_SIZES["A3"]
    scale = 1.0
    if size == "custom" and max(width, height) > 5000.0:  # very large extents: a reduced sheet scale keeps paper sizes sane
        scale = 5000.0 / max(width, height)
    return Sheet(id="S1", size=size, orientation=orientation, width=width * scale if size == "custom" else None,
                 height=height * scale if size == "custom" else None, scale=scale, origin=_cp(origin), draw_frame=False,
                 title_block={k: v.text[:200] for k, v in readings.items()}, title_readings=readings)


def _import_conflicts(state: DraftState, source_id: str) -> None:
    """Conflict notes of a source drawing: a displayed reading that differs from the measured geometry, and two
    dimensions of the same span / circle with different readings. Both readings are kept with their sources."""
    n = max([int(c.id[2:]) for c in state.conflicts] or [0])
    covered = {(c.kind, c.subject) for c in state.conflicts}  # restored from our own conflict notes
    by_key: dict[tuple[Any, ...], list[tuple[str, AnnotationProvenance]]] = defaultdict(list)
    for e in state.entities:
        if e.type != "dimension":
            continue
        rec = state.provenance.get(e.id)
        if rec is None:
            continue
        reading = rec.original
        if ("reading_mismatch", e.id) not in covered and reading is not None and reading.value is not None and rec.measured is not None and \
                reading_consistent(reading.text, rec.measured * (e.dimlfac if rec.unit == "mm" else 1.0)) is False:
            n += 1
            state.conflicts.append(Conflict(
                id=f"C-{n:02d}", subject=e.id, kind="reading_mismatch",
                values=[ConflictValue(value=reading.value, text=reading.text, kind="displayed", source=reading.source, locator=reading.locator),
                        ConflictValue(value=rec.measured, text=fmt(rec.measured), kind="measured", source=source_id,
                                      locator="definition points")],
                note=f"displayed {reading.text!r} but the geometry measures {fmt(rec.measured)}"))
        if e.dimtype in ("linear", "aligned") and e.p1 and e.p2:
            ends = tuple(sorted([(round(e.p1[0], 3), round(e.p1[1], 3)), (round(e.p2[0], 3), round(e.p2[1], 3))]))
            key: tuple[Any, ...] = ("span", e.dimtype, round(e.angle % 180.0, 3) if e.dimtype == "linear" else 0.0, ends)
        elif e.dimtype in ("diameter", "radius") and e.center and e.radius:
            key = ("circle", round(e.center[0], 3), round(e.center[1], 3), round(e.radius, 3))
        else:
            continue
        by_key[key].append((e.id, rec))
    for key, group in by_key.items():
        readings = [(eid, r.original) for eid, r in group if r.original is not None and r.original.value is not None]
        values = {round(rd.value, 6) for _, rd in readings if rd is not None and rd.value is not None}
        subject = ",".join(eid for eid, _ in readings)
        if len(values) > 1 and ("duplicate_reading", subject) not in covered:
            n += 1
            state.conflicts.append(Conflict(
                id=f"C-{n:02d}", subject=subject, kind="duplicate_reading",
                values=[ConflictValue(value=rd.value, text=rd.text, kind="displayed", source=rd.source, locator=rd.locator)
                        for _, rd in readings if rd is not None],
                note="dimensions of the same geometry show different values"))


# --------------------------------------------------------------------------- #
# Content identity
# --------------------------------------------------------------------------- #


def content_dict(state: DraftState) -> dict[str, Any]:
    """The JSON of the drawing content (what a version freezes)."""
    return state.model_dump(mode="json")


def summary(state: DraftState) -> dict[str, Any]:
    c = Counter(e.type for e in state.entities)
    dims = Counter(e.dimtype for e in state.entities if e.type == "dimension")
    return {"entities": len(state.entities), "types": dict(sorted(c.items())), "dimensions": dict(sorted(dims.items())),
            "layers": len(state.layers), "hidden_layers": [lay.name for lay in state.layers if not lay.visible],
            "blocks": len(state.blocks), "sheets": len(state.sheets), "underlays": len(state.underlays),
            "conflicts_open": sum(1 for c in state.conflicts if c.status == "open"),
            "reading_mismatches": sum(1 for p in state.provenance.values() if "reading_mismatch" in p.flags)}
