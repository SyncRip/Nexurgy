"""Checkable requirement list (design §1 of .trellis/tasks/archive/2026-10/10-08-cad-ai/design.md).

The modeling agent extracts what the user / drawing / source STEP actually
states into a list of requirements with citations. The list is frozen and is
the check target of :mod:`app.cad.conformance`; it is separate from the ops
(``ModelSpec``) that try to satisfy it.

Units are mm and degrees, world +Z up (same frame as ``ops``). Pure pydantic:
no geometry kernel, no DB.

Server-side rules on top of the schema:

- :func:`gate` — a critical field (:data:`CRITICAL_FIELDS`) that is missing or
  listed in ``assumed`` makes the reply not acceptable: ask the user
  (:func:`default_question` when the model gave no question).
- :func:`ground` — citations must be real: quotes are substrings of the
  request / an answer (after :func:`normalize_text`), critical numbers and
  positions appear in their cited evidence unless ``derived`` (with a
  derivation from cited numbers) / ``assumed``, through / blind is stated by
  the evidence, drawing citations exist in the evidence index with the same
  value, ``source_geometry`` is read from the current model's op itself.
- :func:`requirements_sha256` — identity of a frozen list.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .capabilities import Capability
from .ops import Counterbore, Countersink, Vec2, Vec3, canonical_json
from .sketch import PathSpec, SketchProfile, Workplane

RequirementKind = Literal["envelope", "hole", "pocket", "band", "note", "prism", "revolve", "sweep", "cut", "coil", "text"]
EvidenceSource = Literal["request", "clarification", "drawing_dimension", "drawing_text", "source_geometry", "template", "design_plan"]
NoteCategory = Literal["material", "tolerance", "roughness", "thread", "treatment", "general"]

REQ_ID_RE = re.compile(r"^R[1-9]\d*$")

# Field names allowed in ``derived`` / ``assumed`` per kind (canonical form).
FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "envelope": ("size", "size.x", "size.y", "size.z", "min"),
    "hole": ("diameter", "direction", "entries", "through", "depth", "counterbore", "countersink", "thread"),
    "pocket": ("contour", "entry_z", "through", "depth"),
    "band": ("z0", "z1", "outer", "voids"),
    "note": ("category", "text"),
    # W3 geometry library kinds (any plane / axis, line and arc outlines)
    "prism": ("plane", "outline", "voids", "s0", "s1"),
    "revolve": ("axis_origin", "axis", "section", "angle"),
    "sweep": ("plane", "path", "section"),
    "cut": ("plane", "outline", "through", "depth"),
    # generation supplement: a section swept along a helix (springs, split lock washers)
    "coil": ("axis_origin", "axis", "mean_diameter", "section", "turns", "pitch", "length", "end_form", "handedness"),
    # generation reliability M: one line of raised / engraved lettering
    "text": ("plane", "text", "height", "depth", "at", "rotation", "font"),
}

# Server table (not the model's choice): a missing or assumed critical field
# blocks the build and asks the user. ``through/depth`` is one decision: is the
# feature through, and if not, how deep.
CRITICAL_FIELDS: dict[str, tuple[str, ...]] = {
    "envelope": ("size.x", "size.y", "size.z"),
    "hole": ("diameter", "entries", "through/depth"),
    "pocket": ("contour", "through/depth"),
    "band": ("z0", "z1", "outer"),
    "note": (),
    "prism": ("outline", "s1"),
    "revolve": ("section",),
    "sweep": ("path", "section"),
    "cut": ("outline", "through/depth"),
    "coil": ("mean_diameter", "section", "turns", "pitch/length"),
    "text": ("text", "depth"),
}

_AXIS_ALIASES = {"0": "x", "1": "y", "2": "z", "x": "x", "y": "y", "z": "z"}


def canonical_field(kind: str, name: str) -> str:
    """Canonical ``derived`` / ``assumed`` field name, or raise ``ValueError``.

    Accepts small variations a model writes: ``size[0]`` / ``size_x`` /
    ``size.X`` → ``size.x``; ``entries[2]`` → ``entries``; ``contour.size`` →
    ``contour``; ``through/depth`` stays as is (means both).
    """
    raw = name.strip().lower().replace(" ", "")
    if raw == "through/depth" and "through" in FIELD_NAMES.get(kind, ()):
        return raw
    if raw == "pitch/length" and kind == "coil":
        return raw
    m = re.match(r"^size(?:\[([0-2])\]|[._]([0-2xyz]))$", raw)
    if m and kind == "envelope":
        return f"size.{_AXIS_ALIASES[m.group(1) or m.group(2)]}"
    head = re.split(r"[.\[]", raw, maxsplit=1)[0]
    if head in FIELD_NAMES.get(kind, ()) and (head == raw or head != "size"):
        return head
    allowed = ", ".join(FIELD_NAMES.get(kind, ()))
    raise ValueError(f"unknown field {name!r} for a {kind} requirement (use one of: {allowed})")


def _covers(names: Iterable[str], field: str) -> bool:
    """Whether a canonical name list (``derived`` / ``assumed``) covers ``field``."""
    names = set(names)
    if field.startswith("size."):
        return field in names or "size" in names
    if field == "through/depth":
        return bool(names & {"through", "depth", "through/depth"})
    if field in ("through", "depth"):
        return field in names or "through/depth" in names
    if field == "pitch/length":
        return bool(names & {"pitch", "length", "pitch/length"})
    if field in ("pitch", "length"):
        return field in names or "pitch/length" in names
    return field in names


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class Evidence(BaseModel):
    """A citation for the values of one requirement."""

    model_config = ConfigDict(extra="forbid")

    source: EvidenceSource = Field(
        description=(
            "request = the user's request text; clarification = one of the user's answers; "
            "drawing_dimension / drawing_text = an entity of the attached drawing evidence index; "
            "source_geometry = a feature of the source STEP; template = the part template the reply uses (its table sizes); "
            "design_plan = a requirement of the server-bound AI design plan, referenced by definition/requirement ID"
        )
    )
    ref: str = Field(
        min_length=1,
        description=(
            "request / clarification: a short LITERAL quote copied from that text, containing the numbers you use "
            "(e.g. \"Ø20 盲孔深 8\") and, for a hole or pocket, the words that say through / blind (通孔, THRU, 盲孔深 8); "
            "drawing_dimension / drawing_text: the entity id from the evidence index; source_geometry: the op id of the "
            "current model (the server reads that op's own parameters); template: the template id (the server reads the "
            "expanded template's own numbers)"
        ),
    )
    value: str | None = Field(
        default=None,
        description=(
            "The value as written in the source (e.g. \"Ø20\", \"8\"); for drawing evidence it must equal the index value; "
            "for source_geometry only a note for readers (the server reads the op, never this text)"
        ),
    )


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description='Requirement id "R1", "R2", … unique in the list; keep ids stable when you revise the list')
    label: str = Field(min_length=1, description='Short human label, e.g. "Ø20 blind hole, 8 deep"')
    evidence: list[Evidence] = Field(
        default_factory=list,
        description=(
            "Citations for the stated values; at least one unless every critical field is assumed or unknown. "
            "Default []"
        ),
    )
    derived: list[str] = Field(
        default_factory=list,
        description=(
            "Fields whose values you computed from cited numbers instead of reading them literally "
            "(e.g. [\"depth\"] when the floor is at Z 7 on a 15 plate: depth = 15 - 7). Never through: through / blind "
            "must be stated by the evidence or listed in assumed. Default []"
        ),
    )
    derivation: str | None = Field(
        default=None,
        description=(
            "How the derived fields were computed, with the numbers: every input number from the cited evidence / the "
            "request / the answers, and the result (e.g. \"x = ±(100/2 − 15) = ±35\"); required when derived is not empty"
        ),
    )
    assumed: list[str] = Field(
        default_factory=list,
        description=(
            "Fields no source states and you had to choose. Never assume a critical field silently: listing a critical "
            "field here (envelope size; hole diameter / entries / through or depth; pocket contour / through or depth; "
            "band z0 / z1 / outer) means the user is asked first. Default []"
        ),
    )

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not REQ_ID_RE.match(v):
            raise ValueError('requirement id must look like "R1", "R2", …')
        return v

    @model_validator(mode="after")
    def _check_fields(self) -> "_Req":
        kind = self.kind  # type: ignore[attr-defined]
        self.derived = _dedupe(canonical_field(kind, n) for n in self.derived)
        self.assumed = _dedupe(canonical_field(kind, n) for n in self.assumed)
        if self.derived and not (self.derivation or "").strip():
            raise ValueError(f"{self.id}: derived is {self.derived} but derivation is empty; explain how they were computed")
        if not self.evidence:
            crit = CRITICAL_FIELDS[kind]
            if not crit or not all(_covers(self.assumed, f) or f in _missing_fields(self) for f in crit):
                raise ValueError(
                    f"{self.id}: evidence is empty; cite the request / answer / drawing entity for its values "
                    "(only a requirement whose critical fields are all assumed or unknown may have no evidence)"
                )
        return self


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _check_pos(v: float, name: str) -> None:
    if not (math.isfinite(v) and v > 0):
        raise ValueError(f"{name} must be a positive number of mm")


class RectContour(BaseModel):
    """Axis-aligned rectangle in the XY plane."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["rect"] = "rect"
    center: Vec2 = Field(description="Centre (X, Y), mm")
    size: Vec2 = Field(description="[width along X, length along Y], mm")
    corner_radius: float = Field(default=0.0, description="Radius of the 4 vertical corners, mm (default 0 = sharp)")

    @model_validator(mode="after")
    def _check(self) -> "RectContour":
        _check_pos(self.size[0], "rect size[0]")
        _check_pos(self.size[1], "rect size[1]")
        if self.corner_radius < 0 or self.corner_radius * 2 > min(self.size) + 1e-9:
            raise ValueError("corner_radius must be >= 0 and at most half the shorter side")
        return self


class CircleContour(BaseModel):
    """Circle in the XY plane."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["circle"] = "circle"
    center: Vec2 = Field(description="Centre (X, Y), mm")
    diameter: float = Field(description="Diameter, mm")

    @model_validator(mode="after")
    def _check(self) -> "CircleContour":
        _check_pos(self.diameter, "circle diameter")
        return self


class PolygonContour(BaseModel):
    """Closed straight-sided outline in the XY plane (absolute X, Y points, no repeat of the first point)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["polygon"] = "polygon"
    points: list[Vec2] = Field(min_length=3, max_length=256, description="Vertices (X, Y) in order, mm")

    @model_validator(mode="after")
    def _check(self) -> "PolygonContour":
        if abs(polygon_area(self.points)) < 1e-9:
            raise ValueError("polygon has zero area")
        return self


Contour = Annotated[Union[RectContour, CircleContour, PolygonContour], Field(discriminator="kind")]


class EnvelopeReq(_Req):
    """Overall size (axis-aligned bounding box) of the finished part, and its absolute pose when stated."""

    kind: Literal["envelope"] = "envelope"
    size: tuple[float | None, float | None, float | None] = Field(
        description=(
            "Overall size along world X, Y, Z in mm. null for an axis no source states (never guess: a null or "
            "assumed size asks the user)"
        )
    )
    min: Vec3 | None = Field(
        default=None,
        description=(
            "Lowest corner (X, Y, Z) of the bounding box when the source fixes the absolute position, e.g. "
            "\"origin at the centre of the bottom face\" of a 120×80×15 plate gives [-60, -40, 0]; null when free"
        ),
    )

    @field_validator("size")
    @classmethod
    def _size(cls, v: tuple[float | None, float | None, float | None]) -> tuple[float | None, float | None, float | None]:
        for n, x in zip("xyz", v):
            if x is not None:
                _check_pos(x, f"size.{n}")
        return v


class HoleReq(_Req):
    """Round hole(s) of one size drilled along one direction; one entry point per instance."""

    kind: Literal["hole"] = "hole"
    diameter: float = Field(description="Hole diameter in mm (for a tapped hole: the modelled bore, thread goes in thread)")
    direction: Vec3 = Field(
        default=(0.0, 0.0, -1.0),
        description="Drilling direction pointing into the material (default [0, 0, -1] = drilled from the top face down)",
    )
    entries: list[Vec3] = Field(
        description=(
            "Entry point of EVERY instance (where the drill enters the surface), world mm: \"4×Ø9\" → four points. "
            "[] when the positions are unknown (the user is asked)"
        )
    )
    through: bool = Field(
        description=(
            "true = through the material along direction; false = blind (needs depth). If no source says blind or "
            "through, pick one and list \"through\" in assumed (the user is asked) — never default to through"
        )
    )
    depth: float | None = Field(
        default=None,
        description="Blind depth in mm from the entry point along direction to the flat floor; null for a through hole",
    )
    counterbore: Counterbore | None = Field(default=None, description="Counterbore at the entry: diameter and depth from the entry, mm")
    countersink: Countersink | None = Field(
        default=None, description="Countersink at the entry: diameter at the surface (mm) and included angle (default 90°)"
    )
    thread: str | None = Field(
        default=None, description='Thread designation of a tapped hole, e.g. "M8" (threads are not modelled as geometry)'
    )

    @model_validator(mode="after")
    def _check(self) -> "HoleReq":
        _check_pos(self.diameter, "diameter")
        if sum(c * c for c in self.direction) < 1e-12:
            raise ValueError("direction must be a non-zero vector")
        if self.through and self.depth is not None:
            raise ValueError(f"{self.id}: a through hole has no depth (set depth to null, or through to false for a blind hole)")
        if self.depth is not None:
            _check_pos(self.depth, "depth")
        if self.counterbore and self.countersink:
            raise ValueError("a hole cannot have both a counterbore and a countersink")
        if self.counterbore:
            if self.counterbore.diameter <= self.diameter:
                raise ValueError("counterbore diameter must exceed the hole diameter")
            if self.depth is not None and self.counterbore.depth >= self.depth:
                raise ValueError("counterbore depth must be smaller than the hole depth")
        if self.countersink and self.countersink.diameter <= self.diameter:
            raise ValueError("countersink diameter must exceed the hole diameter")
        return self


class PocketReq(_Req):
    """A pocket / slot / window cut straight down (−Z) from a face, with a rect / circle / polygon outline."""

    kind: Literal["pocket"] = "pocket"
    contour: Contour = Field(description="Outline of the pocket in the XY plane, world mm")
    entry_z: float = Field(description="Z of the face the pocket is cut into (its open top), mm; the pocket goes down (−Z) from there")
    through: bool = Field(description="true = cut through the part; false = flat floor at entry_z − depth")
    depth: float | None = Field(default=None, description="Depth in mm below entry_z; null when through")

    @model_validator(mode="after")
    def _check(self) -> "PocketReq":
        if self.through and self.depth is not None:
            raise ValueError(f"{self.id}: a through pocket has no depth (set depth to null)")
        if self.depth is not None:
            _check_pos(self.depth, "depth")
        return self


class BandReq(_Req):
    """A Z range in which EVERY horizontal cross-section of the part is exactly outer minus voids.

    Use one band per step of a stepped shaft, sleeve, boss or block. Voids are all openings crossing the whole
    band (a sleeve bore, through holes inside the outline). A blind hole ending inside the range breaks this:
    split the band at its floor instead.
    """

    kind: Literal["band"] = "band"
    z0: float = Field(description="Lower Z of the band, mm")
    z1: float = Field(description="Upper Z of the band (> z0), mm")
    outer: Contour = Field(description="Outer outline of the material cross-section between z0 and z1 (XY, world mm)")
    voids: list[Contour] = Field(
        default_factory=list, description="Openings through the whole band inside outer (bores, through holes), XY, default []"
    )

    @model_validator(mode="after")
    def _check(self) -> "BandReq":
        if not self.z1 > self.z0:
            raise ValueError(f"{self.id}: band needs z0 < z1 (got {self.z0:g} .. {self.z1:g})")
        return self


class NoteReq(_Req):
    """A manufacturing requirement that is not geometry (material, tolerance, roughness, thread, treatment, …)."""

    kind: Literal["note"] = "note"
    category: NoteCategory = Field(description="material | tolerance | roughness | thread | treatment | general")
    text: str = Field(min_length=1, description="The requirement as stated, e.g. \"Q235A\", \"未注公差 GB/T 1804-m\"")
    # generation reliability C (design §6.3): the capability an omitted / simplified feature needs (cad/capabilities.py);
    # left out of the dump while unset, so older lists keep their sha256
    capability: Capability | None = Field(
        default=None, exclude_if=lambda v: v is None,
        description="Only on a note recording something the ops leave out or simplify: the capability it needs (text = "
                    "lettering, thread = thread form, coil = helix, knurl, weld_bead, gear_tooth = tooth flanks, casting_detail, "
                    "freeform = freeform surface, other); null on every other note")


# --------------------------------------------------------------------------- #
# W3 geometry library kinds: any plane / axis, line and arc outlines
# --------------------------------------------------------------------------- #

#: An outline in plane coordinates (u, v): rect / circle / polygon as for pockets and bands, or a sketch of lines
#: and arcs ({"kind":"sketch","start":[u,v],"segments":[{"kind":"line","to":[u,v]} | {"kind":"arc","to":[u,v],
#: "center":[u,v],"ccw":true}], "holes": []}).
PlaneContour = Annotated[Union[RectContour, CircleContour, PolygonContour, SketchProfile], Field(discriminator="kind")]

_PLANE_DESC = ("The plane in world mm: origin, normal and in-plane x_dir (u axis; default by the dominant-axis rule). "
               "Plane coordinates: point = origin + u·x_dir + v·(normal × x_dir) + s·normal")
_SAME = "as for the other kinds"


class _W3Req(_Req):
    """Same fields and rules as every requirement; short schema descriptions (the reply schema is size-limited)."""

    id: str = Field(description=_SAME)
    label: str = Field(min_length=1, description=_SAME)
    evidence: list[Evidence] = Field(default_factory=list, description=_SAME)
    derived: list[str] = Field(default_factory=list, description=_SAME + "; e.g. radii computed from stated diameters")
    derivation: str | None = Field(default=None, description=_SAME)
    assumed: list[str] = Field(default_factory=list, description=_SAME)


class PrismReq(_W3Req):
    """Material whose every cross-section parallel to ``plane`` between ``s0`` and ``s1`` (along the plane normal) is
    ``outline`` minus ``voids``: a plate or block of any outline (arcs, a bent bracket's section) in any orientation."""

    kind: Literal["prism"] = "prism"
    plane: Workplane = Field(description=_PLANE_DESC)
    outline: PlaneContour = Field(description="Outer outline in plane coordinates (u, v), mm")
    voids: list[PlaneContour] = Field(default_factory=list, description="Openings through the whole prism inside outline, default []")
    s0: float = Field(default=0.0, description="Start along the plane normal, mm (default 0 = on the plane)")
    s1: float = Field(description="End along the plane normal (> s0), mm")

    @model_validator(mode="after")
    def _check(self) -> "PrismReq":
        if not self.s1 > self.s0:
            raise ValueError(f"{self.id}: prism needs s0 < s1 (got {self.s0:g} .. {self.s1:g})")
        return self


class RevolveReq(_W3Req):
    """Material of a section revolved about an axis: shafts, sleeves, rings, cones / tapers, arc transitions, blind
    bottoms, O-rings. ``section`` is drawn in (u = along the axis from axis_origin, v = radius ≥ 0)."""

    kind: Literal["revolve"] = "revolve"
    axis_origin: Vec3 = Field(description="A point on the axis (where u = 0), world mm")
    axis: Vec3 = Field(default=(0.0, 0.0, 1.0), description="Axis direction (u grows along it)")
    ref_dir: Vec3 | None = Field(default=None, description="Radial direction of angle 0 (only for angle < 360)")
    section: SketchProfile = Field(description="Closed section in (u axial, v radial ≥ 0) mm: lines and arcs")
    angle: float = Field(default=360.0, description="Revolved angle in degrees (default 360)")

    @model_validator(mode="after")
    def _check(self) -> "RevolveReq":
        if sum(c * c for c in self.axis) < 1e-12:
            raise ValueError("axis must be a non-zero vector")
        if not (0.0 < self.angle <= 360.0):
            raise ValueError("angle must be > 0 and <= 360")
        from .sketch import profile_extent

        if profile_extent(self.section, (0.0, -1.0)) > 1e-9:
            raise ValueError(f"{self.id}: the section's radial coordinate v must be >= 0")
        return self


class SweepReq(_W3Req):
    """Material of a section swept along a planar, tangent-continuous line / arc path (bent bars, rails, tubes).
    The section is drawn in (u = left of travel in the path plane, v = plane normal) at the path start."""

    kind: Literal["sweep"] = "sweep"
    plane: Workplane = Field(description="Path plane: " + _PLANE_DESC)
    path: PathSpec = Field(description="Open path in plane coordinates (u, v): start + line / arc segments, tangent continuous")
    section: PlaneContour = Field(description="Section in (u left of travel, v plane normal), mm")


class CutReq(_W3Req):
    """Material removed by an outline cut into a face of any orientation: entering at ``plane`` (origin on the face,
    normal pointing OUT of the material) and going against the normal, through or ``depth`` deep (flat floor)."""

    kind: Literal["cut"] = "cut"
    plane: Workplane = Field(description="Entry face plane: origin on the face, normal out of the material; " + _PLANE_DESC)
    outline: PlaneContour = Field(description="Outline of the cut in plane coordinates (u, v), mm")
    through: bool = Field(description="true = through all material ahead; false = flat floor at depth")
    depth: float | None = Field(default=None, description="Depth in mm from the plane against its normal; null when through")

    @model_validator(mode="after")
    def _check(self) -> "CutReq":
        if self.through and self.depth is not None:
            raise ValueError(f"{self.id}: a through cut has no depth (set depth to null)")
        if self.depth is not None:
            _check_pos(self.depth, "depth")
        return self


class CoilCircle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["circle"] = "circle"
    diameter: float = Field(description="Wire diameter d, mm")

    @model_validator(mode="after")
    def _check(self) -> "CoilCircle":
        _check_pos(self.diameter, "wire diameter")
        return self


class CoilRect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rect"] = "rect"
    width: float = Field(description="Size across the radius, mm")
    height: float = Field(description="Size along the axis, mm")

    @model_validator(mode="after")
    def _check(self) -> "CoilRect":
        _check_pos(self.width, "section width")
        _check_pos(self.height, "section height")
        return self


CoilWire = Annotated[Union[CoilCircle, CoilRect], Field(discriminator="kind")]


class CoilReq(_W3Req):
    """Material of a coil: a circular or rectangular wire section swept along a helix (compression springs, split lock
    washers). The coil sits on the plane through ``axis_origin`` normal to ``axis`` and rises along +axis; the end
    forms, length formulas and section placement are those of the coil op (app/cad/coil.py)."""

    kind: Literal["coil"] = "coil"
    axis_origin: Vec3 = Field(description="Centre of the coil's seating plane on its axis, world mm")
    axis: Vec3 = Field(default=(0.0, 0.0, 1.0), description="Coil axis (the coil rises along it)")
    mean_diameter: float = Field(description="Mean diameter D of the centre line, mm (= OD - d = ID + d)")
    section: CoilWire = Field(description='{"kind":"circle","diameter":d} | {"kind":"rect","width":across the radius,"height":along the axis}')
    turns: float = Field(description="Total turns (coils), closed end turns included")
    pitch: float | None = Field(default=None, description="Active pitch (centre-line rise per turn), mm; or give length")
    length: float | None = Field(default=None, description="Overall length along the axis (free length / height), mm; or give pitch")
    end_form: Literal["open", "closed", "closed_ground"] = Field(default="open", description="open | closed | closed_ground")
    handedness: Literal["right", "left"] = "right"
    start_angle: float = Field(default=0.0, description="Degrees about the axis where the wire starts (default 0)")

    @model_validator(mode="after")
    def _check(self) -> "CoilReq":
        if sum(c * c for c in self.axis) < 1e-12:
            raise ValueError("axis must be a non-zero vector")
        _check_pos(self.mean_diameter, "mean_diameter")
        _check_pos(self.turns, "turns")
        for name in ("pitch", "length"):
            v = getattr(self, name)
            if v is not None:
                _check_pos(v, name)
        if self.pitch is not None and self.length is not None:
            raise ValueError(f"{self.id}: give pitch or length, not both (the other follows from the end form)")
        if self.pitch is not None or self.length is not None:
            try:
                self.law()
            except ValueError as exc:
                raise ValueError(f"{self.id}: {exc}") from exc
        return self

    def section_size(self) -> tuple[float, float]:
        s = self.section
        return (s.diameter, s.diameter) if isinstance(s, CoilCircle) else (s.width, s.height)

    def law(self) -> Any:
        from .coil import coil_law

        s_rad, s_ax = self.section_size()
        return coil_law(mean_diameter=self.mean_diameter, s_rad=s_rad, s_ax=s_ax, turns=self.turns, pitch=self.pitch,
                        length=self.length, end_form=self.end_form, handedness=self.handedness, start_angle=self.start_angle)


class TextReq(_W3Req):
    """One line of lettering on a face (generation reliability M): raised (``depth`` > 0, standing on ``plane`` along its
    normal) or engraved (``depth`` < 0, cut into it), font size ``height``, the text block centred at ``at`` and turned
    ``rotation`` degrees; the glyphs are those of the text op (app/cad/text_geometry.py, same font choice)."""

    kind: Literal["text"] = "text"
    plane: Workplane = Field(description="The face the letters stand on / are cut into: origin on it, normal out of the material; "
                                         + _PLANE_DESC)
    text: str = Field(min_length=1, description="The line of text exactly as stated")
    height: float = Field(description="Character (font) height, mm")
    depth: float = Field(description="Raised height (> 0) or engraved depth (< 0), mm")
    at: Vec2 = Field(default=(0.0, 0.0), description="Centre of the text block in plane coordinates (u, v), mm")
    rotation: float = Field(default=0.0, description="Degrees counter-clockwise from u, seen from the normal (default 0)")
    font: str | None = Field(default=None, description="Font the source names, else null")

    @model_validator(mode="after")
    def _check(self) -> "TextReq":
        if not self.text.strip() or any(ord(c) < 32 for c in self.text):
            raise ValueError(f"{self.id}: text is one visible line (one requirement per line)")
        _check_pos(self.height, "height")
        if not (math.isfinite(self.depth) and abs(self.depth) > 0):
            raise ValueError(f"{self.id}: depth must be > 0 (raised) or < 0 (engraved)")
        return self


Requirement = Annotated[Union[EnvelopeReq, HoleReq, PocketReq, BandReq, NoteReq, PrismReq, RevolveReq, SweepReq, CutReq, CoilReq,
                              TextReq],
                        Field(discriminator="kind")]
GEOMETRIC_KINDS = ("envelope", "hole", "pocket", "band", "prism", "revolve", "sweep", "cut", "coil", "text")
W3_KINDS = ("prism", "revolve", "sweep", "cut", "coil", "text")


def _unique_ids(reqs: list[Any]) -> list[Any]:
    seen: set[str] = set()
    for r in reqs:
        if r.id in seen:
            raise ValueError(f"duplicate requirement id {r.id}")
        seen.add(r.id)
    return reqs


Requirements = Annotated[list[Requirement], AfterValidator(_unique_ids)]
"""Type to embed in reply models (``list[Requirement]`` with unique ids)."""

RequirementList: TypeAdapter[list[Any]] = TypeAdapter(Requirements)


class RequirementsDoc(BaseModel):
    """Structured-output root for a requirement list alone."""

    model_config = ConfigDict(extra="forbid")
    requirements: Requirements = Field(
        description=(
            "Every checkable requirement the sources state: one envelope, the holes / pockets / bands, and notes. "
            "Cite evidence; list derived and assumed fields honestly"
        )
    )


def requirements_json_schema() -> dict[str, Any]:
    """JSON schema (object root) for structured output of a requirement list."""
    return RequirementsDoc.model_json_schema()


def parse_requirements(data: Sequence[Any]) -> list[Any]:
    """Validate a list of requirement dicts / models into models."""
    return RequirementList.validate_python([r.model_dump() if isinstance(r, BaseModel) else r for r in data])


def requirements_sha256(reqs: Sequence[Any]) -> str:
    """sha256 of the canonical JSON of the (validated) list."""
    dumped = RequirementList.dump_python(parse_requirements(reqs), mode="json")
    return hashlib.sha256(canonical_json(dumped).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Geometry helpers for contours (pure Python; also used by the checker)
# --------------------------------------------------------------------------- #


def polygon_area(points: Sequence[Sequence[float]]) -> float:
    """Signed area (counter-clockwise positive)."""
    n = len(points)
    return 0.5 * sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1] for i in range(n))


def contour_area(c: Any) -> float:
    if isinstance(c, RectContour):
        w, h = c.size
        return w * h - (4.0 - math.pi) * c.corner_radius**2
    if isinstance(c, CircleContour):
        return math.pi * c.diameter**2 / 4.0
    return abs(polygon_area(c.points))


def contour_perimeter(c: Any) -> float:
    if isinstance(c, RectContour):
        w, h = c.size
        return 2.0 * (w + h) - 8.0 * c.corner_radius + 2.0 * math.pi * c.corner_radius
    if isinstance(c, CircleContour):
        return math.pi * c.diameter
    pts = c.points
    return sum(math.dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))


def contour_centroid(c: Any) -> tuple[float, float]:
    if isinstance(c, (RectContour, CircleContour)):
        return (float(c.center[0]), float(c.center[1]))
    pts = c.points
    a = polygon_area(pts)
    n = len(pts)
    cx = sum((pts[i][0] + pts[(i + 1) % n][0]) * (pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]) for i in range(n))
    cy = sum((pts[i][1] + pts[(i + 1) % n][1]) * (pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]) for i in range(n))
    return (cx / (6.0 * a), cy / (6.0 * a))


# --------------------------------------------------------------------------- #
# Critical gate
# --------------------------------------------------------------------------- #


class CriticalGap(BaseModel):
    """A critical field the user must answer before anything is built."""

    requirement_id: str
    kind: RequirementKind
    label: str
    field: str
    reason: Literal["missing", "assumed"]


def _missing_fields(req: Any) -> set[str]:
    out: set[str] = set()
    if req.kind == "envelope":
        out |= {f"size.{n}" for n, v in zip("xyz", req.size) if v is None}
    elif req.kind == "hole":
        if not req.entries:
            out.add("entries")
        if not req.through and req.depth is None:
            out.add("through/depth")
    elif req.kind in ("pocket", "cut"):
        if not req.through and req.depth is None:
            out.add("through/depth")
    elif req.kind == "coil":
        if req.pitch is None and req.length is None:
            out.add("pitch/length")
    return out


def gate(reqs: Sequence[Any]) -> list[CriticalGap]:
    """Critical fields that are missing or assumed, in list order."""
    gaps: list[CriticalGap] = []
    for req in reqs:
        missing = _missing_fields(req)
        for f in CRITICAL_FIELDS[req.kind]:
            reason: Literal["missing", "assumed"] | None = None
            if f in missing:
                reason = "missing"
            elif _covers(req.assumed, f):
                reason = "assumed"
            if reason:
                gaps.append(CriticalGap(requirement_id=req.id, kind=req.kind, label=req.label, field=f, reason=reason))
    return gaps


_QUESTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("envelope", "size.x"): ("零件沿 X 方向的总尺寸是多少（mm）？", "what is the overall size of the part along X (mm)?"),
    ("envelope", "size.y"): ("零件沿 Y 方向的总尺寸是多少（mm）？", "what is the overall size of the part along Y (mm)?"),
    ("envelope", "size.z"): ("零件沿 Z 方向的总尺寸（高度/厚度）是多少（mm）？", "what is the overall size (height / thickness) of the part along Z (mm)?"),
    ("hole", "diameter"): ("孔的直径是多少（mm）？", "what is the hole diameter (mm)?"),
    ("hole", "entries"): ("每个孔的中心位置在哪里（相对哪个基准，mm）？", "where is the centre of each hole (mm, from which datum)?"),
    ("hole", "through/depth"): (
        "这个孔是通孔还是盲孔？如果是盲孔，从孔口起算的深度是多少（mm）？",
        "is this hole through or blind? If blind, how deep is it from the entry (mm)?",
    ),
    ("pocket", "contour"): ("这个槽/型腔的轮廓尺寸和位置是多少（mm）？", "what are the outline size and position of this pocket (mm)?"),
    ("pocket", "through/depth"): (
        "这个槽/型腔是贯穿的还是有底的？如果有底，深度是多少（mm）？",
        "does this pocket go through, or does it have a floor? If it has a floor, how deep is it (mm)?",
    ),
    ("band", "z0"): ("这一段从哪个高度开始（Z，mm）？", "at which height (Z, mm) does this section start?"),
    ("band", "z1"): ("这一段到哪个高度结束（Z，mm）？", "at which height (Z, mm) does this section end?"),
    ("band", "outer"): ("这一段的外轮廓形状和尺寸是多少（mm）？", "what are the outer shape and size of this section (mm)?"),
    ("prism", "outline"): ("这段型材/板的截面轮廓（含圆弧）尺寸和位置是多少（mm）？",
                           "what are the outline (with any arcs) and position of this profile / plate (mm)?"),
    ("prism", "s1"): ("这段型材/板沿法向的长度或厚度是多少（mm）？", "how long / thick is this profile along its normal (mm)?"),
    ("revolve", "section"): ("回转件的截面（各段轴向位置、直径、锥度与圆角）是多少（mm）？",
                             "what is the turned section (axial positions, diameters, tapers and radii, mm)?"),
    ("sweep", "path"): ("扫掠路径（直线段和圆弧半径）是多少（mm）？", "what is the sweep path (straight lengths and arc radii, mm)?"),
    ("sweep", "section"): ("扫掠截面的形状和尺寸是多少（mm）？", "what are the shape and size of the swept section (mm)?"),
    ("coil", "mean_diameter"): ("弹簧的中径（或外径 / 内径）是多少（mm）？", "what is the coil's mean diameter (or its outer / inner diameter, mm)?"),
    ("coil", "section"): ("弹簧丝的截面（线径或矩形截面尺寸）是多少（mm）？", "what is the wire section (wire diameter or rectangle size, mm)?"),
    ("coil", "turns"): ("弹簧的总圈数（含支承圈）是多少？", "how many turns does the coil have in total (end turns included)?"),
    ("coil", "pitch/length"): ("弹簧的节距或自由高度是多少（mm）？", "what is the coil's pitch or free length (mm)?"),
    ("cut", "outline"): ("这个切口/槽的轮廓尺寸和位置是多少（mm）？", "what are the outline size and position of this cut (mm)?"),
    ("cut", "through/depth"): ("这个切口是贯穿的还是有底的？如果有底，深度是多少（mm）？",
                               "does this cut go through, or does it have a floor? If it has a floor, how deep is it (mm)?"),
    ("text", "text"): ("这一行文字的内容是什么？", "what does this line of text say?"),
    ("text", "depth"): ("文字是凸起还是刻入？凸起高度或刻入深度是多少（mm）？",
                        "are the letters raised or engraved, and how high / deep (mm)?"),
}


def default_question(gap: CriticalGap, lang: Literal["zh", "en"] = "zh") -> str:
    """Server-generated question naming the gap's requirement and field."""
    zh, en = _QUESTIONS.get((gap.kind, gap.field), (f"请确认 {gap.field}。", f"please confirm {gap.field}."))
    if lang == "zh":
        return f"{gap.requirement_id}（{gap.label}）：{zh}"
    return f"{gap.requirement_id} ({gap.label}): {en}"


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #

_DIAMETER_SIGNS = "øØφΦϕ⌀∅"
_TIMES_SIGNS = "×✕✖╳*"
_MINUS_SIGNS = "−–—"
_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)")
_DXF_CODES = (("%%c", "ø"), ("%%d", "°"), ("%%p", "±"))
# A number that counts instances, not a size: "4×Ø9", "4-Ø9", "4 x M8", "4 holes", or Chinese counts of items / locations (normalised text).
_COUNT_AFTER = re.compile(r"\s*(?:[x-]\s*(?=ø|m\d)|(?:个|处|places?\b|holes?\b|pcs\b))")

#: Words (normalised) that state a through feature / a blind one (or its depth). Through or blind is grounded only by
#: these in the cited evidence (or, for blind, by the cited depth number); never by a default or a derivation.
THROUGH_WORDS = ("thru", "through", "通孔", "通槽", "贯通", "贯穿", "穿透", "打穿", "钻穿", "铣穿", "切穿", "直通", "通至", "通到",
                 "穿通", "打通", "window", "窗口", "cut-out", "cutout")
BLIND_WORDS = ("盲", "深", "depth", "deep", "blind", "↧", "floor", "孔底", "槽底")

#: Numbers a derivation may use without a citation (halving, inch conversion, whole turns). Angles written with a
#: degree sign ("60°") are not lengths and are left out of the check altogether.
DERIVATION_CONSTANTS = (0.0, 0.5, 1.0, 2.0, 25.4, 90.0, 180.0, 360.0)
_ANGLE = re.compile(r"\d+(?:\.\d+)?\s*(?:°|º|deg\b|degrees?\b)", re.IGNORECASE)
_DERIVED_TOL = 0.01  # mm: a derived value written rounded in the derivation still counts as present


def normalize_text(text: str) -> str:
    """Normalise for quote matching: NFKC (full-width → ASCII), casefold,
    Ø/φ/⌀ (and DXF ``%%c``) → ``ø``, ×/x/* → ``x``, minus signs → ``-``,
    whitespace collapsed."""
    s = unicodedata.normalize("NFKC", text or "")
    for code, ch in _DXF_CODES:
        s = re.sub(re.escape(code), ch, s, flags=re.I)
    for ch in _DIAMETER_SIGNS:
        s = s.replace(ch, "ø")
    for ch in _TIMES_SIGNS:
        s = s.replace(ch, "x")
    for ch in _MINUS_SIGNS:
        s = s.replace(ch, "-")
    s = s.casefold()
    return re.sub(r"\s+", " ", s).strip()


def _number_tokens(text: str) -> list[tuple[float, bool, bool]]:
    """``(value, diameter_marked, is_count)`` of every number in ``text`` (see :func:`extract_numbers` for the
    sign rules). ``diameter_marked``: written right after Ø / φ / ⌀; ``is_count``: an instance count ("4×Ø9")."""
    s = normalize_text(text)
    out: list[tuple[float, bool, bool]] = []
    for m in _NUMBER_RE.finditer(s):
        value = float(m.group(1))
        i = m.start()
        prev = s[i - 1] if i >= 1 else ""
        before = s[i - 2] if i >= 2 else ""
        j = i - 1
        while j >= 0 and s[j] == " ":
            j -= 1
        dia = j >= 0 and s[j] == "ø"
        # "Ø8 hole", "M8 holes", "R5" are sizes; only a bare number before a diameter, hole label or Chinese item-count word counts instances
        count = not dia and not prev.isalpha() and bool(_COUNT_AFTER.match(s, m.end()))
        if prev == "±":
            out.extend([(value, dia, count), (-value, dia, count)])
            continue
        if prev == "-" and not before.isalnum():
            value = -value
        out.append((value, dia, count))
    return out


def extract_numbers(text: str) -> list[float]:
    """Numbers written in ``text`` (Ø20, φ20, ⌀20, 20mm, R5, M10, 4×Ø9, 4-Ø9,
    120x80x15, 12.5, full-width digits). A minus sign counts only when it is
    not a range dash (``-7``, ``Z=-7``, ``(−30, 0)``; not ``10-20``); ``±48``
    gives both 48 and -48."""
    return [v for v, _, _ in _number_tokens(text)]


def _number_in(x: float, numbers: Iterable[float], tol: float = 0.0) -> bool:
    eps = max(tol, 1e-6 * max(1.0, abs(x)))
    for n in numbers:
        if abs(x - n) <= eps:
            return True
        if n >= 0 and abs(x + n) <= eps:  # unsigned text, sign from the frame convention
            return True
    return False


def _contour_parts(c: Any) -> tuple[list[tuple[str, float, bool]], list[float]]:
    """``([(what, value, is_diameter)], position coordinates)`` of a contour."""
    if isinstance(c, RectContour):
        return [("width", float(c.size[0]), False), ("length", float(c.size[1]), False)], [float(v) for v in c.center]
    if isinstance(c, CircleContour):
        return [("diameter", float(c.diameter), True)], [float(v) for v in c.center]
    if isinstance(c, SketchProfile):
        return [], _sketch_numbers(c.start, c.segments) + [x for h in c.holes for x in _sketch_numbers(h.start, h.segments)]
    return [], [float(v) for p in c.points for v in p]


def _sketch_numbers(start: Any, segments: Any) -> list[float]:
    """Every coordinate a sketch states: start, segment ends and arc centres (an arc radius is implied by them)."""
    out = [float(v) for v in start]
    for s in segments:
        out += [float(v) for v in s.to]
        if s.kind == "arc":
            out += [float(v) for v in s.center]
    return out


def _w3_sizes(req: Any) -> list[tuple[str, str, float, bool]]:
    """Size scalars of the W3 kinds (outline / section sizes, extents, depth, partial angle)."""
    out: list[tuple[str, str, float, bool]] = []
    if req.kind == "prism":
        out += [("s0", "s0", float(req.s0), False), ("s1", "s1", float(req.s1), False)]
        out += [("outline", f"outline {w}", v, d) for w, v, d in _contour_parts(req.outline)[0]]
    elif req.kind == "sweep":
        out += [("section", f"section {w}", v, d) for w, v, d in _contour_parts(req.section)[0]]
    elif req.kind == "cut":
        out += [("outline", f"outline {w}", v, d) for w, v, d in _contour_parts(req.outline)[0]]
        if not req.through and req.depth is not None:
            out.append(("depth", "depth", float(req.depth), False))
    elif req.kind == "revolve" and req.angle < 360.0:
        out.append(("angle", "angle", float(req.angle), False))
    elif req.kind == "text":
        out += [("height", "height", float(req.height), False), ("depth", "depth", abs(float(req.depth)), False)]
    elif req.kind == "coil":
        out.append(("mean_diameter", "mean diameter", float(req.mean_diameter), False))
        if isinstance(req.section, CoilCircle):
            out.append(("section", "wire diameter", float(req.section.diameter), False))
        else:
            out += [("section", "section width", float(req.section.width), False), ("section", "section height", float(req.section.height), False)]
        out.append(("turns", "turns", float(req.turns), False))
        if req.pitch is not None:
            out.append(("pitch", "pitch", float(req.pitch), False))
        if req.length is not None:
            out.append(("length", "length", float(req.length), False))
    return out


def _w3_positions(req: Any) -> list[tuple[str, str, list[float]]]:
    out: list[tuple[str, str, list[float]]] = []
    if req.kind in ("prism", "sweep", "cut", "text"):
        out.append(("plane", "plane origin", [float(v) for v in req.plane.origin]))
    if req.kind == "text":
        out.append(("at", "text position", [float(v) for v in req.at]))
    if req.kind in ("prism", "cut"):
        out.append(("outline", "outline position", _contour_parts(req.outline)[1]))
    if req.kind == "prism":
        vals: list[float] = []
        for v in req.voids:
            sizes, pos = _contour_parts(v)
            vals += [x for _, x, _ in sizes] + pos
        out.append(("voids", "void sizes / positions", vals))
    elif req.kind == "sweep":
        out.append(("path", "path coordinates", _sketch_numbers(req.path.start, req.path.segments)))
        out.append(("section", "section position", _contour_parts(req.section)[1]))
    elif req.kind == "revolve":
        out.append(("axis_origin", "axis origin", [float(v) for v in req.axis_origin]))
        out.append(("section", "section coordinates (u axial, v radius)", _contour_parts(req.section)[1]))
    elif req.kind == "coil":
        out.append(("axis_origin", "axis origin", [float(v) for v in req.axis_origin]))
    return out


def _critical_scalars(req: Any) -> list[tuple[str, str, float, bool]]:
    """``(field, what, value, is_diameter)`` of the size numbers that must be cited."""
    out: list[tuple[str, str, float, bool]] = []
    if req.kind == "envelope":
        for n, v in zip("xyz", req.size):
            if v is not None:
                out.append((f"size.{n}", f"size {n.upper()}", float(v), False))
    elif req.kind == "hole":
        out.append(("diameter", "diameter", float(req.diameter), True))
        if not req.through and req.depth is not None:
            out.append(("depth", "depth", float(req.depth), False))
    elif req.kind == "pocket":
        out.extend(("contour", f"contour {w}", v, d) for w, v, d in _contour_parts(req.contour)[0])
        if not req.through and req.depth is not None:
            out.append(("depth", "depth", float(req.depth), False))
    elif req.kind == "band":
        out.append(("z0", "z0", float(req.z0), False))
        out.append(("z1", "z1", float(req.z1), False))
        out.extend(("outer", f"outer {w}", v, d) for w, v, d in _contour_parts(req.outer)[0])
    else:
        out += _w3_sizes(req)
    return out


def _position_values(req: Any) -> list[tuple[str, str, list[float]]]:
    """``(field, what, values)`` of positions / poses that must be cited too (entries, contour centres, entry_z, min,
    voids): a default position is as much an invention as a default size."""
    out: list[tuple[str, str, list[float]]] = []
    if req.kind == "envelope" and req.min is not None:
        out.append(("min", "min", [float(v) for v in req.min]))
    elif req.kind == "hole":
        out.append(("entries", "entry coordinates", [float(v) for e in req.entries for v in e]))
    elif req.kind == "pocket":
        out.append(("contour", "contour position", _contour_parts(req.contour)[1]))
        out.append(("entry_z", "entry_z", [float(req.entry_z)]))
    elif req.kind == "band":
        out.append(("outer", "outer position", _contour_parts(req.outer)[1]))
        vals: list[float] = []
        for v in req.voids:
            sizes, pos = _contour_parts(v)
            vals += [x for _, x, _ in sizes] + pos
        out.append(("voids", "void sizes / positions", vals))
    else:
        out += _w3_positions(req)
    return out


def _field_values(req: Any, field: str) -> list[float]:
    """Numeric values of one canonical field (the outputs of a derivation)."""
    k = req.kind
    if k == "envelope":
        if field == "size":
            return [float(v) for v in req.size if v is not None]
        if field.startswith("size."):
            v = req.size["xyz".index(field[-1])]
            return [] if v is None else [float(v)]
        if field == "min":
            return [float(v) for v in req.min] if req.min is not None else []
    elif k == "hole":
        if field == "diameter":
            return [float(req.diameter)]
        if field == "entries":
            return [float(v) for e in req.entries for v in e]
        if field in ("depth", "through/depth"):
            return [] if req.depth is None else [float(req.depth)]
        if field == "counterbore" and req.counterbore is not None:
            return [float(req.counterbore.diameter), float(req.counterbore.depth)]
        if field == "countersink" and req.countersink is not None:
            return [float(req.countersink.diameter), float(req.countersink.angle)]
    elif k == "pocket":
        if field == "contour":
            sizes, pos = _contour_parts(req.contour)
            return [v for _, v, _ in sizes] + pos
        if field == "entry_z":
            return [float(req.entry_z)]
        if field in ("depth", "through/depth"):
            return [] if req.depth is None else [float(req.depth)]
    elif k == "band":
        if field in ("z0", "z1"):
            return [float(getattr(req, field))]
        if field == "outer":
            sizes, pos = _contour_parts(req.outer)
            return [v for _, v, _ in sizes] + pos
        if field == "voids":
            out: list[float] = []
            for v in req.voids:
                sizes, pos = _contour_parts(v)
                out += [x for _, x, _ in sizes] + pos
            return out
    elif k in ("prism", "revolve", "sweep", "cut", "coil", "text"):
        vals = [v for f, _, v, _ in _w3_sizes(req) if f == field or (field == "through/depth" and f == "depth")
                or (field == "pitch/length" and f in ("pitch", "length"))]
        vals += [x for f, _, xs in _w3_positions(req) if f == field for x in xs]
        if k == "revolve" and field == "axis":
            vals += [float(v) for v in req.axis]
        return vals
    return []


_NUMERIC_FIELDS = {"size", "size.x", "size.y", "size.z", "min", "diameter", "entries", "depth", "through/depth", "counterbore",
                   "countersink", "contour", "entry_z", "z0", "z1", "outer", "voids",
                   "plane", "outline", "s0", "s1", "axis_origin", "section", "angle", "path",
                   "mean_diameter", "turns", "pitch", "length", "pitch/length", "height", "at"}


def _values_match(claimed: str, actual: str) -> bool:
    if normalize_text(claimed) == normalize_text(actual):
        return True
    a, b = extract_numbers(claimed), extract_numbers(actual)
    return bool(a) and len(a) == len(b) and all(abs(x - y) <= 1e-6 * max(1.0, abs(y)) for x, y in zip(sorted(a), sorted(b)))


def _quote_in(quote: str, sources: Sequence[str]) -> bool:
    q = normalize_text(quote)
    if not q:
        return False
    q_tight = q.replace(" ", "")
    for src in sources:
        s = normalize_text(src)
        if q in s or q_tight in s.replace(" ", ""):
            return True
    return False


def _clip(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,23}$")


def safe_id(ref: str) -> str:
    """A model-written id for a problem message: echoed only when it looks like an id (never free text, which could
    carry quoted private context)."""
    return repr(ref) if _SAFE_ID.match(ref or "") else "(not an id)"


def _fmt_values(values: Iterable[float], n: int = 6) -> str:
    vals = list(dict.fromkeys(round(v, 6) for v in values))
    out = ", ".join(f"{v:g}" for v in vals[:n])
    return out + (", …" if len(vals) > n else "")


class _Cited:
    """What the (valid) evidence of one requirement provides: numbers, Ø-marked numbers, non-count numbers and the
    normalised texts (for through / blind words)."""

    def __init__(self) -> None:
        self.numbers: list[float] = []
        self.dia: list[float] = []
        self.plain: list[float] = []
        self.texts: list[str] = []
        self.request_quotes: list[str] = []

    def add(self, text: str) -> None:
        toks = _number_tokens(text)
        self.numbers += [v for v, _, _ in toks]
        self.dia += [v for v, d, _ in toks if d]
        self.plain += [v for v, _, c in toks if not c]
        self.texts.append(normalize_text(text))

    def says(self, words: Iterable[str]) -> bool:
        return any(w in t for t in self.texts for w in words)


def definition_source_header(name: str) -> re.Pattern[str]:
    """A literal, named part declaration, not a mention of that part in another part's description."""
    return re.compile(r"^[ \t]*(?:\d+[.)、][ \t]*)?" + re.escape(normalize_text(name))
                      + r"(?:\s*[x×]\s*\d+)?\s*[:：]", re.IGNORECASE)


DESIGN_MODELING_SYSTEM = """This run implements a separately authorized AI design, not source reconstruction.
The server-bound design block supplies complete critical geometry for this one definition. Preserve its exact
requirement IDs, kinds and typed values; cite source=design_plan and ref=definition/requirement ID. These planned
values are known design inputs, not missing user measurements, assumed fields or human confirmations. Do not
ask for them again and do not copy them into request evidence. Reject unsupported geometry honestly. This
instruction does not authorize changing requirements, omitting features, or bypassing independent STEP checks."""


class DesignDefinitionContext(BaseModel):
    """Server-resolved immutable design inputs, passed separately from model/user text.

    Construct only after resolving the persisted plan and its authority. This type is not a request or reply field.
    The digest is checked again when consumed so even mutation of a nested dictionary cannot change the contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_ref: dict[str, Any]
    spec_version_id: str
    definition_key: str = Field(pattern=r"^D-\d{2,}$")
    requirements: tuple[dict[str, Any], ...]
    interfaces: tuple[dict[str, Any], ...] = ()
    requirements_sha256: str = ""

    @model_validator(mode="after")
    def checked_requirements(self) -> DesignDefinitionContext:
        digest = requirements_sha256(self.requirements)
        if self.requirements_sha256 and self.requirements_sha256 != digest:
            raise ValueError("design requirements hash mismatch")
        object.__setattr__(self, "requirements_sha256", digest)
        return self

    def provenance(self) -> dict[str, Any]:
        return {"origin": "ai_design", "design_ref": dict(self.plan_ref), "spec_version_id": self.spec_version_id,
                "definition_key": self.definition_key, "requirements_sha256": self.requirements_sha256,
                "interfaces": list(self.interfaces)}

    def prompt(self) -> str:
        return ("\nAuthorized AI design inputs for this definition (not human measurements or confirmation). "
                "Implement every frozen requirement below, preserving its ID and typed geometry exactly. "
                "Cite source=design_plan and ref=definition/requirement ID as provided; omit evidence.value. "
                "Do not remove a requirement, relax a dimension or add unplanned geometry. "
                "The server independently checks the exported STEP.\n" + canonical_json(
                    {"definition": self.definition_key, "requirements": self.requirements}))


def design_requirement_problems(reqs: Sequence[Any], context: DesignDefinitionContext) -> list[str]:
    """A design model may implement the frozen requirements, not rewrite or silently omit them."""
    if requirements_sha256(context.requirements) != context.requirements_sha256:
        return ["design requirements hash mismatch"]
    expected = {r.id: r for r in parse_requirements(context.requirements)}
    actual = {r.id: r for r in reqs}
    problems = []
    if set(expected) != set(actual):
        problems.append("design requirement coverage differs: missing " + ",".join(sorted(set(expected) - set(actual)))
                        + "; extra " + ",".join(sorted(set(actual) - set(expected))))
    for key in expected.keys() & actual.keys():
        # Wording and evidence presentation are not geometry; every typed geometric field is frozen.
        exclude = {"label", "evidence", "derived", "derivation"}
        if canonical_json(expected[key].model_dump(exclude=exclude)) != canonical_json(actual[key].model_dump(exclude=exclude)):
            problems.append(f"{key}: geometry differs from its frozen design requirement")
        ref = f"{context.definition_key}/{key}"
        if not any(e.source == "design_plan" and e.ref == ref for e in actual[key].evidence):
            problems.append(f"{key}: cite its own server-bound design requirement")
    return problems


class DefinitionSourceContext(BaseModel):
    """Server-located original text belonging to one definition; never part of the model's reply schema.

    The spec service constructs this from a unique verified quote in the pinned, sent source. The typed value
    travels separately to grounding. Serializing it in the prompt does not confer this authority on user text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    definition: str = Field(pattern=r"^D-\d{2,}$")
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=20)
    chunk: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=400)
    anchor_quote: str = Field(min_length=2, max_length=400)

    @model_validator(mode="after")
    def located_text(self) -> DefinitionSourceContext:
        if self.end - self.start != len(self.text):
            raise ValueError("source context offsets must cover its exact original text")
        if not definition_source_header(self.name).match(normalize_text(self.text)):
            raise ValueError("source context must start with its own named definition")
        if not _quote_in(self.anchor_quote, [self.text]):
            raise ValueError("the located quote must belong to this source context")
        return self


# An annular *solid* states its central void; a ring profile, annular groove, split ring or another hole does not.
# Keep this independent of model-generated band requirements and of the general through-word vocabulary.
_ANNULAR_SOLID = re.compile(r"圆环形实体|\bannular\s+solid\b")
_ANNULAR_CONFLICT = re.compile(
    r"盲孔|有底|孔(?:的)?深(?:度)?|环形槽|开口|缺口|非圆环|不是|并非|不为|不贯|不通|未贯|未通|非贯|"
    r"偏心|偏置|偏移|旁孔|侧孔|径向|"
    r"\b(?:blind|depth|deep|groove|split|partial|c-shaped|eccentric|offset|radial)\b|\bside\s+(?:hole|bore)\b|"
    r"\bnot\s+(?:(?:an?|a)\s+)?(?:annular|through)\b")
_ANNULAR_EXTRA_GEOMETRY = re.compile(
    r"孔(?:中心|轴|位|入口|出口|口|沿)|"
    r"(?:[2-9]\d*|两|二|三|四|多个)\s*(?:个|处)?\s*(?:中心)?孔|"
    r"\b(?:hole|bore)\s+(?:is\s+)?(?:axis|cent(?:er|re)|located|entry|entrance|exit|along|at)\b")
_POSITIVE_NUMBER = r"(\d+(?:\.\d+)?)"
_SIGNED_NUMBER = r"(-?\d+(?:\.\d+)?)"
_CENTRAL_BORE = re.compile(r"(?:中心孔\s*(?:直径|ø)|(?:central|center|centre)\s+(?:hole|bore)\s*(?:diameter\s*|ø))"
                           + _POSITIVE_NUMBER)
_LOCAL_AXIS = re.compile(r"(?:局部(?:圆柱)?轴向|local\s+(?:cylinder\s+)?axis\s*)\s*([xyz])")
_BORE_REFERENCE = re.compile(r"(?:中心|该|此|本|这个)(?:中心)?孔|孔(?:的)?(?:中心|轴|位|入口|出口)|"
                             r"\b(?:central|center|centre)\s+(?:hole|bore)\b|\b(?:the|this)\s+(?:hole|bore)\b")


def _annular_context_qualified(text: str) -> bool:
    # The narrow proof understands one central-bore declaration only. A second statement about that hole needs
    # its own semantic validation; never discard it or let a positive substring inside its negation bypass it.
    # Assembly poses, the shaft passing through the wheel and global delivery notes are not extra bore claims.
    return bool(_ANNULAR_CONFLICT.search(text) or _ANNULAR_EXTRA_GEOMETRY.search(text)
                or len(list(_BORE_REFERENCE.finditer(text))) > 1)


def _cites_context_bore(cited: _Cited, context: DefinitionSourceContext) -> bool:
    bores = list(_CENTRAL_BORE.finditer(normalize_text(context.text)))
    # The model may quote a narrower range than extraction did. Match at the complete central-bore phrase,
    # never at a coincident number or unrelated axis quote; source ownership and offsets remain server-owned.
    return any(_quote_in(q, [context.text]) and any(
        _quote_in(bore.group(0), [q]) and (_quote_in(context.anchor_quote, [q])
                                         or _quote_in(bore.group(0), [context.anchor_quote]))
        for bore in bores) for q in cited.request_quotes)


def _annular_bore_qualified(req: Any, cited: _Cited, contexts: Sequence[DefinitionSourceContext],
                            part_name: str | None) -> bool:
    """A trusted same-bore qualification wins over a positive through keyword in that source."""
    if not part_name or req.kind != "hole":
        return False
    return any(context.name == part_name and _ANNULAR_SOLID.search(normalize_text(context.text))
               and _annular_context_qualified(normalize_text(context.text)) and _cites_context_bore(cited, context)
               for context in contexts)


def _annular_bore_stated(req: Any, cited: _Cited, contexts: Sequence[DefinitionSourceContext],
                         part_name: str | None) -> bool:
    """Recognize only the named annulus's one central bore, cited by this exact hole requirement."""
    if not part_name or req.kind != "hole" or len(req.entries) != 1:
        return False
    for context in contexts:
        text = normalize_text(context.text)
        if context.name != part_name or _annular_context_qualified(text):
            continue
        # The first full sentence declares the solid. Later sentences stay in the context to retain exclusions.
        statement = normalize_text(re.split(r"[。!?！？;；\n]", context.text, maxsplit=1)[0])
        if not _ANNULAR_SOLID.search(statement):
            continue
        bores, axes = list(_CENTRAL_BORE.finditer(statement)), list(_LOCAL_AXIS.finditer(statement))
        if (len(bores) != 1 or len(axes) != 1
                or len(list(_CENTRAL_BORE.finditer(text))) != 1 or len(list(_LOCAL_AXIS.finditer(text))) != 1):
            continue
        bore, axis = bores[0], axes[0].group(1)
        if not math.isclose(float(bore.group(1)), req.diameter, abs_tol=1e-7):
            continue
        # A numeric coincidence, a quote of the axis alone or a citation to another paragraph is insufficient.
        if not _cites_context_bore(cited, context):
            continue
        bounds = list(re.finditer(r"(?:局部\s*" + axis + r"\s*从|local\s+" + axis + r"\s+from\s*)"
                                  + _SIGNED_NUMBER + r"\s*(?:到|to)\s*" + _SIGNED_NUMBER, statement))
        if len(bounds) != 1:
            continue
        lo, hi = (float(v) for v in bounds[0].groups())
        if hi <= lo:
            continue
        index = "xyz".index(axis)
        entry, direction = req.entries[0], req.direction
        if any(abs(entry[i]) > 1e-7 or abs(direction[i]) > 1e-7 for i in range(3) if i != index):
            continue
        if ((math.isclose(entry[index], lo, abs_tol=1e-7) and direction[index] > 0)
                or (math.isclose(entry[index], hi, abs_tol=1e-7) and direction[index] < 0)):
            return True
    return False


# --- derivations ------------------------------------------------------------------------------------------------

_GENERIC_IDS = re.compile(r"\bF-\d{2,}\b|\bS\d{2,}(?:\.V\d+)?\b|\bV\d+\b")


def _strip_ids(text: str, ids: Iterable[str]) -> str:
    """The derivation without op / sheet / view ids and without the given ids (requirement ids, cited drawing entity
    ids that contain a letter): their digits are not numbers."""
    s = _GENERIC_IDS.sub(" ", text or "")
    toks = sorted({i for i in ids if i and re.search(r"[A-Za-z]", i)}, key=len, reverse=True)
    if toks:
        s = re.sub(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in toks) + r")(?![A-Za-z0-9])", " ", s)
    return s


_NUM_AT = re.compile(r"\d+(?:\.\d+)?")


def _expr_segments(text: str) -> list[list[tuple[str, Any]]]:
    """Token runs of arithmetic in ``text``: ("n", value) | ("op", "+-*/") | ("(", ) | (")", ) | ("±", );
    anything else (words, "=", ",", units) ends a run. "x" / "×" between two operands is a multiplication."""
    s = normalize_text(text)
    segs: list[list[tuple[str, Any]]] = [[]]
    i = 0
    while i < len(s):
        ch = s[i]
        cur = segs[-1]
        if ch == " ":
            i += 1
            continue
        m = _NUM_AT.match(s, i)
        if m:
            cur.append(("n", float(m.group(0))))
            i = m.end()
            continue
        if ch in "+-/*":
            cur.append(("op", ch))
        elif ch in "()":
            cur.append((ch, None))
        elif ch == "±":
            cur.append(("±", None))
        elif ch == "x" and cur and cur[-1][0] in ("n", ")") and re.match(r"\s*[\d(]", s[i + 1:]):
            cur.append(("op", "*"))
        elif cur:
            segs.append([])
        i += 1
    return [seg for seg in segs if seg]


def _parse_expr(toks: list[tuple[str, Any]], known: Any) -> float | None:
    """Value of a complete arithmetic expression whose numbers all satisfy ``known`` (± read as +), else None."""
    pos = 0

    def peek() -> tuple[str, Any] | None:
        return toks[pos] if pos < len(toks) else None

    def factor() -> float:
        nonlocal pos
        t = peek()
        if t is None:
            raise ValueError
        if t[0] == "op" and t[1] in "+-" or t[0] == "±":
            pos += 1
            v = factor()
            return -v if t == ("op", "-") else v
        if t[0] == "n":
            if not known(t[1]):
                raise ValueError
            pos += 1
            return float(t[1])
        if t[0] == "(":
            pos += 1
            v = expr()
            if peek() is None or peek()[0] != ")":  # type: ignore[index]
                raise ValueError
            pos += 1
            return v
        raise ValueError

    def term() -> float:
        nonlocal pos
        v = factor()
        while (t := peek()) is not None and t[0] == "op" and t[1] in "*/":
            pos += 1
            r = factor()
            if t[1] == "*":
                v *= r
            else:
                if abs(r) < 1e-12:
                    raise ValueError
                v /= r
        return v

    def expr() -> float:
        nonlocal pos
        v = term()
        while (t := peek()) is not None and t[0] == "op" and t[1] in "+-":
            pos += 1
            r = term()
            v = v + r if t[1] == "+" else v - r
        return v

    try:
        v = expr()
    except (ValueError, RecursionError):
        return None
    return v if pos == len(toks) and math.isfinite(v) else None


def _has_binary_op(toks: list[tuple[str, Any]]) -> bool:
    return any(t[0] == "op" and k > 0 and toks[k - 1][0] in ("n", ")") for k, t in enumerate(toks))


def _reachable(text: str, known: list[float]) -> list[float]:
    """Values of the arithmetic written in a derivation that uses only known numbers (two rounds: an intermediate
    result such as ``100/2 = 50`` may feed the next step)."""
    segs = _expr_segments(text)
    pool = list(known)
    found: list[float] = []
    for _ in range(2):
        def ok(x: float) -> bool:
            return _number_in(x, pool, _DERIVED_TOL)
        new: list[float] = []
        for seg in segs:
            n = len(seg)
            for i in range(n):
                for j in range(i + 2, min(n, i + 25) + 1):
                    sub = seg[i:j]
                    if not _has_binary_op(sub):
                        continue  # a plain (signed) number is no computation
                    v = _parse_expr(sub, ok)
                    if v is not None:
                        new.append(v)
        found += new
        pool += new
    return found


def _derivation_problems(req: Any, known: list[float], strip: Iterable[str]) -> list[str]:
    """``derived`` must be a real computation: numbers in the derivation come from the cited evidence / the request /
    the answers (or are the derived values themselves, or a constant), and each derived value is either cited, the
    result of arithmetic written in the derivation, or written there next to at least one cited input number."""
    fields = [f for f in req.derived if f in _NUMERIC_FIELDS]
    if not fields:
        return []
    outputs = [v for f in fields for v in _field_values(req, f)]
    text = _ANGLE.sub(" ", _strip_ids(req.derivation or "", strip))
    nums = extract_numbers(text)
    names = ", ".join(fields)
    if not nums:
        return [f"{req.id}: derived {names} but the derivation has no numbers; write the computation from cited numbers "
                "(e.g. \"x = ±(100/2 − 15) = ±35\"), or list the field in assumed when no source states it"]
    reach = _reachable(text, known + list(DERIVATION_CONSTANTS))
    problems: list[str] = []
    unknown = [n for n in nums if not (_number_in(n, known) or _number_in(n, DERIVATION_CONSTANTS)
                                       or _number_in(n, outputs, _DERIVED_TOL) or _number_in(n, reach, _DERIVED_TOL))]
    if unknown:
        problems.append(f"{req.id}: the derivation uses {_fmt_values(unknown)}, which is not in the cited evidence, the request "
                        "or the answers; every input number of a derivation must come from a cited source")
    inputs = [n for n in nums if _number_in(n, known) and not _number_in(n, outputs, _DERIVED_TOL)]
    bad = [v for v in outputs if abs(v) > 1e-12 and not _number_in(v, known) and not _number_in(v, reach, _DERIVED_TOL)
           and not (_number_in(v, nums, _DERIVED_TOL) and inputs)]
    if bad:
        problems.append(f"{req.id}: derived {names} value(s) {_fmt_values(bad)} cannot be reproduced from the derivation; "
                        "write how each follows from cited numbers (e.g. \"x = ±(100/2 − 15) = ±35\")")
    return problems


def _spans_full_extent(req: Any, reqs: Sequence[Any], strip: Iterable[str]) -> bool:
    """A derived ``through`` is accepted when its derivation names the envelope size along the drilling axis (the bore
    spans the part, as its section shows)."""
    env = next((r for r in reqs if r.kind == "envelope"), None)
    if env is None or not req.derivation:
        return False
    d = getattr(req, "direction", None) or (0.0, 0.0, -1.0)
    axis = max(range(3), key=lambda i: abs(d[i]))
    extent = env.size[axis] if env.size and env.size[axis] is not None else None
    if extent is None or extent <= 0:
        return False
    return _number_in(float(extent), extract_numbers(_strip_ids(req.derivation, strip)), _DERIVED_TOL)


# --- grounding ----------------------------------------------------------------------------------------------------


def ground(
    reqs: Sequence[Any],
    *,
    request_text: str,
    answers: Iterable[str] = (),
    drawing_values: Mapping[str, str] | None = None,
    geometry: Mapping[str, str] | None = None,
    derivation_sources: Iterable[str] = (),
    derived_through: bool = False,
    template_values: Mapping[str, str] | None = None,
    part_name: str | None = None,
    source_contexts: Sequence[DefinitionSourceContext] = (),
    design_context: DesignDefinitionContext | None = None,
) -> list[str]:
    """Problems with the citations of ``reqs`` (empty = grounded).

    W3: ``template`` refs are read from ``template_values`` (template id → the numbers of the template instance the
    reply expands, serialised by the server like ``source_geometry``).

    - ``request`` / ``clarification`` refs are literal quotes of the request /
      an answer (after :func:`normalize_text`; whitespace-insensitive).
    - ``drawing_*`` refs exist in ``drawing_values`` (evidence id → displayed
      value) and their ``value`` (when given) matches it.
    - ``source_geometry`` refs are read from ``geometry`` (op id → the op as the
      CURRENT model states it, serialised by the caller); the model-written
      ``value`` is never evidence.
    - every critical number (sizes, diameter, depth) and every position (hole
      entries, contour centres / points, pocket entry_z, envelope min, band
      voids) not in ``derived`` / ``assumed`` appears among the numbers of the
      requirement's evidence; a diameter must be a Ø-marked number when the
      cited text marks any (a count such as "4×" is never a diameter). 0 is
      the datum and needs no citation.
    - through / blind of a hole or pocket is stated by its evidence (a through
      word, or a blind / depth word or the cited depth), unless assumed (then
      the gate asks); it is never derived.
    - ``derived`` fields come with a derivation whose input numbers are cited
      (see :func:`_derivation_problems`). ``derivation_sources`` are further
      stated texts whose numbers a derivation may use as inputs without citing
      them (reconstruct: the displayed values of the bound sheet, conflicting
      dimensions excluded); they never ground a critical value directly.
    - ``derived_through`` (reconstruct only: a drawing shows a through bore
      in its section): ``through`` may be listed in ``derived`` when the
      derivation states the part's full extent along the drilling axis (the
      envelope size along that axis), i.e. the bore spans the whole part.

    Problems never repeat quote text (a quote may be private context the model
    copied); they name the requirement, the evidence index and numbers.
    """
    answers = [a for a in answers if a]
    problems: list[str] = design_requirement_problems(reqs, design_context) if design_context is not None else []
    design_refs = {f"{design_context.definition_key}/{r['id']}": r for r in design_context.requirements} if design_context else {}
    design_grounded: set[str] = set()
    cited: dict[str, _Cited] = {}
    for req in reqs:
        c = cited[req.id] = _Cited()
        for k, ev in enumerate(req.evidence):
            where = f"{req.id} evidence[{k}]"
            if ev.source == "design_plan":
                expected = design_refs.get(ev.ref)
                if expected is None or ev.ref != f"{design_context.definition_key}/{req.id}":
                    problems.append(f"{where}: no server-bound design evidence for this definition and requirement")
                    continue
                if ev.value is not None:
                    problems.append(f"{where}: design evidence values are server-owned; omit value")
                    continue
                # Exact typed values/topology and complete requirement coverage were checked above. Do not turn
                # an AI-authored plan into request quotes or infer booleans from a bag of numeric/text tokens.
                design_grounded.add(req.id)
            elif ev.source == "request":
                if not _quote_in(ev.ref, [request_text]):
                    problems.append(f"{where}: the quote is not in the request text; copy the exact characters of the request "
                                    "(no paraphrase, no other block)")
                    continue
                c.add(ev.ref)
                c.request_quotes.append(ev.ref)
            elif ev.source == "clarification":
                if not answers:
                    problems.append(f"{where}: cites a clarification but there are no answers yet")
                    continue
                if not _quote_in(ev.ref, answers):
                    problems.append(f"{where}: the quote is not in any answer; copy the exact characters of one answer")
                    continue
                c.add(ev.ref)
            elif ev.source in ("drawing_dimension", "drawing_text"):
                if drawing_values is None:
                    problems.append(f"{where}: cites drawing entity {safe_id(ev.ref)} but no drawing evidence is attached")
                    continue
                if ev.ref not in drawing_values:
                    problems.append(f"{where}: drawing entity {safe_id(ev.ref)} does not exist in the evidence index")
                    continue
                actual = drawing_values[ev.ref]
                if ev.value is not None and not _values_match(ev.value, actual):
                    problems.append(f"{where}: value does not match drawing entity {ev.ref} (\"{_clip(actual)}\"); copy the "
                                    "displayed value exactly as listed")
                    continue
                c.add(actual)
            elif ev.source == "template":  # the expanded template's own numbers (server text), never the written value
                if not template_values or ev.ref not in template_values:
                    problems.append(f"{where}: cites template {safe_id(ev.ref)} but the reply uses no such template")
                    continue
                c.add(template_values[ev.ref])
            elif geometry is not None and ev.ref in geometry:  # source_geometry: the op itself, never the written value
                c.add(geometry[ev.ref])
    known = extract_numbers(request_text) + [n for a in answers for n in extract_numbers(a)] + \
        [n for c in cited.values() for n in c.numbers] + [n for t in derivation_sources for n in extract_numbers(t)]
    strip = [r.id for r in reqs] + [ev.ref for r in reqs for ev in r.evidence if ev.source != "request" and ev.source != "clarification"]
    for req in reqs:
        if req.id in design_grounded:
            continue
        if not req.evidence:
            continue  # only allowed when every critical field is assumed / missing (gate asks)
        c = cited[req.id]
        for field, what, value, is_dia in _critical_scalars(req):
            if _covers(req.derived, field) or _covers(req.assumed, field) or abs(value) < 1e-12:
                continue
            if is_dia and c.dia and not _number_in(value, c.dia):
                problems.append(
                    f"{req.id}: {what} {value:g} is not one of the Ø values of its cited evidence ({_fmt_values(c.dia)}); a count "
                    f"such as \"4×\" is not a diameter. Cite the text that states it, or list \"{field}\" in derived (with "
                    "derivation) or assumed"
                )
            elif not _number_in(value, c.plain if is_dia else c.numbers):
                problems.append(
                    f"{req.id}: {what} {value:g} does not appear in its cited evidence; cite the text that states it, "
                    f"or list \"{field}\" in derived (with derivation) or assumed"
                )
        for field, what, values in _position_values(req):
            if _covers(req.derived, field) or _covers(req.assumed, field):
                continue
            missing = [v for v in values if abs(v) > 1e-12 and not _number_in(v, c.numbers)]
            if missing:
                problems.append(
                    f"{req.id}: {what} {_fmt_values(missing)} do not appear in its cited evidence; cite the text that states "
                    f"them (e.g. the thickness for an entry on the top face), or list \"{field}\" in derived (with the "
                    "computation in derivation) or assumed"
                )
        if req.kind in ("hole", "pocket", "cut") and not _covers(req.assumed, "through/depth"):
            if req.through:
                stated = not _annular_bore_qualified(req, c, source_contexts, part_name) and (
                    c.says(THROUGH_WORDS) or _annular_bore_stated(req, c, source_contexts, part_name)
                    or (derived_through and "through" in req.derived and _spans_full_extent(req, reqs, strip)))
            else:
                stated = c.says(BLIND_WORDS) or (req.depth is not None and (_number_in(float(req.depth), c.numbers)
                                                                          or _covers(req.derived, "depth")))
            if not stated:
                word = "through" if req.through else "blind"
                extra = ("" if "through" not in req.derived else
                         " (a derived through must state the part's full length along the drilling axis in its derivation)"
                         if derived_through else " (through / blind is never derived)")
                problems.append(
                    f"{req.id}: {word} is not stated by its cited evidence{extra}; cite the words that say through or blind "
                    "(or the depth), or list \"through\" in assumed so the user is asked"
                )
        problems += _derivation_problems(req, known, strip)
    return problems


def quote_in(quote: str, sources: Sequence[str]) -> bool:
    """Whether ``quote`` is a literal quote of one of ``sources`` (normalised, whitespace-insensitive)."""
    return _quote_in(quote, sources)


def requirement_numbers(req: Any) -> list[float]:
    """Every size and position number of one requirement (sizes, diameter, depth, entries, contours, z range, min)."""
    return [v for _, _, v, _ in _critical_scalars(req)] + [v for _, _, vals in _position_values(req) for v in vals]


def number_in(x: float, numbers: Iterable[float]) -> bool:
    """``x`` among ``numbers`` (relative 1e-6; an unsigned number also matches its negative)."""
    return _number_in(x, numbers)
