"""Modeling operations shared by the AI agent and the manual editor.

A model revision is an optional imported base solid plus an ordered list of
operations. The kernel replays the list from scratch on every build, so the
list is the single source of truth for the geometry. Units: mm and degrees,
world Z up. See .trellis/tasks/archive/2026-10/10-02-v04-platform/design.md §2.

The W3 geometry library (docs/development/mechanical-engineering/geometry-ops.zh-CN.md) adds line / arc sketch
profiles, arbitrary workplanes (``Workplane`` / ``DatumRef``), ``revolve``, ``sweep``, ``bent_plate``, ``cut``,
``shell`` and ``pattern``; the generation supplement adds ``coil`` (a section swept along a helix, :mod:`app.cad.coil`)
and the reliability round ``text`` (raised / engraved lettering, :mod:`app.cad.text_geometry`).
Old specs dump exactly as before (new optional fields of old ops are left out of the dump while unset; new op types are
only new union members), so their ``build_key`` does not change.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_serializer, model_validator

from .coil import CoilLaw, coil_law
from .sketch import (
    DatumRef,
    Frame,
    PathSpec,
    PlaneSpec,
    SketchProfile,
    Workplane,
    bent_plate_layout,
    dot3,
    profile_extent,
    resolve_path,
    unit3,
)

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]

OP_ID_RE = re.compile(r"^F-\d{2,}$")

# Hard limits keep AI output and hand-typed values inside what the kernel can
# build reliably. They are deliberately generous for small machined parts.
MAX_DIM = 5000.0
MIN_DIM = 0.01
MAX_OPS = 200


def _positive(v: float, name: str) -> float:
    if not (MIN_DIM <= v <= MAX_DIM):
        raise ValueError(f"{name} must be between {MIN_DIM} and {MAX_DIM} mm")
    return v


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Fields added to an existing op type after revisions were stored: left out of every dump while None, so an
    #: old op dumps (and hashes into ``build_key``) exactly as before.
    _OMIT_IF_NONE: ClassVar[tuple[str, ...]] = ()

    id: str
    label: str | None = None
    suppressed: bool = False

    @model_serializer(mode="wrap")
    def _omit_unset_new_fields(self, handler: Any) -> Any:
        data = handler(self)
        if self._OMIT_IF_NONE and isinstance(data, dict):
            for key in self._OMIT_IF_NONE:
                if key in data and data[key] is None:
                    del data[key]
        return data

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not OP_ID_RE.match(v) or v == "F-00":
            raise ValueError("op id must look like F-01, F-02 … (F-00 is reserved for an imported base)")
        return v


BodyMode = Literal["new", "add", "subtract", "intersect"]


class _BodyOp(_Op):
    mode: BodyMode = "add"
    rotate: Vec3 = (0.0, 0.0, 0.0)


class BoxOp(_BodyOp):
    type: Literal["box"] = "box"
    size: Vec3
    at: Vec3 = (0.0, 0.0, 0.0)  # centre of the bottom face

    @field_validator("size")
    @classmethod
    def _size(cls, v: Vec3) -> Vec3:
        for n, x in zip("xyz", v):
            _positive(x, f"size.{n}")
        return v


class CylinderOp(_BodyOp):
    type: Literal["cylinder"] = "cylinder"
    radius: float
    height: float
    at: Vec3 = (0.0, 0.0, 0.0)  # centre of the base circle
    axis: Literal["x", "y", "z"] = "z"

    @field_validator("radius", "height")
    @classmethod
    def _pos(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        return _positive(v, info.field_name)


class RectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rect"] = "rect"
    width: float
    length: float
    corner_radius: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> "RectProfile":
        _positive(self.width, "width")
        _positive(self.length, "length")
        if self.corner_radius < 0 or self.corner_radius * 2 >= min(self.width, self.length):
            raise ValueError("corner_radius must be >= 0 and less than half the shorter side")
        return self


class CircleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["circle"] = "circle"
    radius: float

    @field_validator("radius")
    @classmethod
    def _r(cls, v: float) -> float:
        return _positive(v, "radius")


class SlotProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["slot"] = "slot"
    length: float  # centre-to-centre length plus width, i.e. overall length
    width: float

    @model_validator(mode="after")
    def _check(self) -> "SlotProfile":
        _positive(self.length, "length")
        _positive(self.width, "width")
        if self.width >= self.length:
            raise ValueError("slot width must be smaller than its overall length")
        return self


class PolygonProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["polygon"] = "polygon"
    points: list[Vec2] = Field(min_length=3, max_length=256)


class RingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ring"] = "ring"
    outer_radius: float
    inner_radius: float

    @model_validator(mode="after")
    def _check(self) -> "RingProfile":
        _positive(self.outer_radius, "outer_radius")
        _positive(self.inner_radius, "inner_radius")
        if self.inner_radius >= self.outer_radius:
            raise ValueError("inner_radius must be smaller than outer_radius")
        return self


Profile = Annotated[
    Union[RectProfile, CircleProfile, SlotProfile, PolygonProfile, RingProfile, SketchProfile],
    Field(discriminator="kind"),
]


class ExtrudeOp(_BodyOp):
    type: Literal["extrude"] = "extrude"
    profile: Profile
    plane: PlaneSpec = "XY"  # a named plane, an arbitrary Workplane or a DatumRef of an earlier op
    offset: float = 0.0  # along the plane normal
    at: Vec2 = (0.0, 0.0)  # profile centre in plane coordinates
    height: float
    rotate_profile: float = 0.0  # degrees, in plane

    @field_validator("height")
    @classmethod
    def _h(cls, v: float) -> float:
        return _positive(v, "height")


class Counterbore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diameter: float
    depth: float

    @model_validator(mode="after")
    def _check(self) -> "Counterbore":
        _positive(self.diameter, "counterbore.diameter")
        _positive(self.depth, "counterbore.depth")
        return self


class Countersink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diameter: float
    angle: float = 90.0

    @model_validator(mode="after")
    def _check(self) -> "Countersink":
        _positive(self.diameter, "countersink.diameter")
        if not (0.0 < self.angle < 180.0):
            raise ValueError("countersink.angle must be between 0 and 180 degrees")
        return self


class LinearPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["linear"] = "linear"
    count: tuple[int, int]
    pitch: Vec2


class CircularPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["circular"] = "circular"
    count: int = Field(ge=2, le=360)
    radius: float

    @field_validator("radius")
    @classmethod
    def _r(cls, v: float) -> float:
        return _positive(v, "pattern.radius")


HolePattern = Annotated[Union[LinearPattern, CircularPattern], Field(discriminator="kind")]


class HoleOp(_Op):
    type: Literal["hole"] = "hole"
    _OMIT_IF_NONE: ClassVar[tuple[str, ...]] = ("plane", "flush_entry")

    at: Vec3  # entry point in world coordinates (plane coordinates (u, v, w) when ``plane`` is set)
    direction: Vec3 = (0.0, 0.0, -1.0)  # world (plane coordinates when ``plane`` is set: the default drills into it)
    diameter: float
    depth: float | None = None  # None = through
    counterbore: Counterbore | None = None
    countersink: Countersink | None = None
    pattern: HolePattern | None = None
    # W3: a local workplane; ``at`` / ``direction`` are then plane coordinates and patterns step along its u / v.
    plane: Workplane | None = None
    # W3: start the tool outside an inclined entry face so the mouth is cut clean (always on for a ``plane`` hole).
    # Unset keeps the original behaviour (the tool starts 0.01 mm before the entry point).
    flush_entry: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "HoleOp":
        _positive(self.diameter, "diameter")
        if self.depth is not None:
            _positive(self.depth, "depth")
        if self.counterbore and self.countersink:
            raise ValueError("a hole cannot have both a counterbore and a countersink")
        if self.counterbore and self.counterbore.diameter <= self.diameter:
            raise ValueError("counterbore diameter must exceed the hole diameter")
        if self.countersink and self.countersink.diameter <= self.diameter:
            raise ValueError("countersink diameter must exceed the hole diameter")
        if sum(c * c for c in self.direction) < 1e-12:
            raise ValueError("direction must be a non-zero vector")
        if isinstance(self.pattern, LinearPattern) and (self.pattern.count[0] < 1 or self.pattern.count[1] < 1):
            raise ValueError("linear pattern counts must be >= 1")
        return self


EdgeGroup = Literal["all", "vertical", "top", "bottom", "top_outer", "bottom_outer"]


class EdgeSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: EdgeGroup = "all"
    near: Vec3 | None = None
    max_count: int | None = Field(default=None, ge=1, le=500)


class FilletOp(_Op):
    type: Literal["fillet"] = "fillet"
    radius: float
    edges: EdgeSelector

    @field_validator("radius")
    @classmethod
    def _r(cls, v: float) -> float:
        return _positive(v, "radius")


class ChamferOp(_Op):
    type: Literal["chamfer"] = "chamfer"
    distance: float
    edges: EdgeSelector

    @field_validator("distance")
    @classmethod
    def _d(cls, v: float) -> float:
        return _positive(v, "distance")


class TransformOp(_Op):
    type: Literal["transform"] = "transform"
    target: str
    translate: Vec3 = (0.0, 0.0, 0.0)
    rotate: Vec3 = (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# W3 geometry library ops
# --------------------------------------------------------------------------- #

MAX_PATTERN_INSTANCES = 500


def _nonzero(v: Vec3, name: str) -> None:
    if not all(math.isfinite(c) for c in v) or sum(c * c for c in v) < 1e-12:
        raise ValueError(f"{name} must be a non-zero vector")


class CutOp(_Op):
    """Remove a profile prism from the solid, entering at a (work)plane and going against its normal: ``depth`` mm
    deep (flat floor), or through all material ahead when ``depth`` is None. Slots, pockets, windows and notches on
    any face, in any direction."""

    type: Literal["cut"] = "cut"
    profile: Profile
    plane: PlaneSpec = "XY"
    offset: float = 0.0  # entry face along the plane normal
    at: Vec2 = (0.0, 0.0)
    rotate_profile: float = 0.0
    depth: float | None = None  # None = through

    @field_validator("depth")
    @classmethod
    def _depth(cls, v: float | None) -> float | None:
        return None if v is None else _positive(v, "depth")


class RevolveOp(_BodyOp):
    """A sketch profile in (axial u, radial v ≥ 0) coordinates revolved about the axis through ``at`` along ``axis``;
    an ``angle`` below 360 sweeps counter-clockwise about +axis, starting in the half-plane of ``ref_dir``."""

    type: Literal["revolve"] = "revolve"
    profile: SketchProfile
    at: Vec3 = (0.0, 0.0, 0.0)
    axis: Vec3 = (0.0, 0.0, 1.0)
    ref_dir: Vec3 | None = None  # radial direction of angle 0 (default: dominant-axis rule, +X for a Z axis)
    angle: float = 360.0

    @model_validator(mode="after")
    def _check(self) -> "RevolveOp":
        _nonzero(self.axis, "axis")
        if self.ref_dir is not None:
            _nonzero(self.ref_dir, "ref_dir")
            if abs(dot3(unit3(self.axis), unit3(self.ref_dir))) > math.cos(math.radians(1.0)):
                raise ValueError("ref_dir must not be parallel to the axis")
        if not (0.0 < self.angle <= 360.0):
            raise ValueError("angle must be > 0 and <= 360 degrees")
        if profile_extent(self.profile, (0.0, -1.0)) > 1e-9:
            raise ValueError("revolve profile: the radial coordinate v must be >= 0 everywhere (the profile may touch the axis, "
                             "never cross it)")
        return self

    def frame(self) -> Frame:
        """Profile frame: origin ``at``, u along the axis, v along ``ref_dir`` (radial)."""
        a = unit3(self.axis)
        ref = self.ref_dir if self.ref_dir is not None else Frame.make(self.at, a).x
        f = Frame.make(self.at, a, ref)  # z = axis, x = radial
        return Frame(f.origin, f.z, f.x, f.y)


class SweepOp(_BodyOp):
    """A section profile swept along a planar, tangent-continuous line / arc ``path`` drawn on ``plane``. The section
    sits at the path start: its u axis lies in the path plane to the left of travel, its v axis is the plane normal."""

    type: Literal["sweep"] = "sweep"
    profile: Profile
    path: PathSpec
    plane: PlaneSpec = "XY"

    @model_validator(mode="after")
    def _check(self) -> "SweepOp":
        for i, s in enumerate(resolve_path(self.path.start, self.path.segments)):
            if s.kind != "arc":
                continue
            inward = profile_extent(self.profile, (1.0, 0.0) if s.sweep > 0 else (-1.0, 0.0))
            if inward >= s.radius - 1e-6:
                raise ValueError(f"path segment {i + 1}: arc radius {s.radius:g} is not larger than the section's extent "
                                 f"{inward:g} toward its centre (the sweep would fold onto itself)")
        return self


class BentPlateOp(_BodyOp):
    """A statically bent plate (the formed shape only, no unfolding): legs joined by bends of ``inner_radius``, drawn
    on ``plane`` from ``at`` along +u with the thickness toward +v, extruded ``width`` along the plane normal."""

    type: Literal["bent_plate"] = "bent_plate"
    thickness: float
    width: float
    legs: list[float] = Field(min_length=2, max_length=16)
    bends: list[float] = Field(min_length=1, max_length=15)  # degrees, + turns toward the thickness side
    inner_radius: float = 0.0
    leg_length: Literal["outside", "flat"] = "outside"
    plane: PlaneSpec = "XZ"
    offset: float = 0.0
    at: Vec2 = (0.0, 0.0)

    @model_validator(mode="after")
    def _check(self) -> "BentPlateOp":
        _positive(self.thickness, "thickness")
        _positive(self.width, "width")
        for i, length in enumerate(self.legs):
            _positive(length, f"legs[{i}]")
        if len(self.bends) != len(self.legs) - 1:
            raise ValueError(f"bends needs one angle between each pair of legs ({len(self.legs) - 1}), got {len(self.bends)}")
        for i, b in enumerate(self.bends):
            if not (1.0 <= abs(b) <= 175.0):
                raise ValueError(f"bends[{i}] must be between 1 and 175 degrees (either sign)")
        if not (0.0 <= self.inner_radius <= MAX_DIM):
            raise ValueError("inner_radius must be >= 0")
        bent_plate_layout(self.thickness, self.legs, self.bends, self.inner_radius, self.leg_length)
        return self


CoilSection = Annotated[Union[CircleProfile, RectProfile], Field(discriminator="kind")]


class CoilOp(_BodyOp):
    """A circular or rectangular section swept along a helix about ``axis`` through ``at`` (coil springs, split lock
    washers): ``mean_diameter`` of the centre line, ``turns`` in total, and ``pitch`` (active pitch) or ``length``
    (overall length along the axis), ``handedness``, ``start_angle`` (degrees from the frame's x axis: the
    dominant-axis rule, +X for a Z axis) and ``end_form`` open / closed / closed_ground. The coil sits on the plane
    through ``at`` normal to the axis and rises along +axis; a rect section is ``width`` across the radius by
    ``length`` along the axis (sharp corners). The law (end turns, ground ends, length formulas) is
    :mod:`app.cad.coil`."""

    type: Literal["coil"] = "coil"
    section: CoilSection
    mean_diameter: float
    turns: float
    pitch: float | None = None
    length: float | None = None
    handedness: Literal["right", "left"] = "right"
    start_angle: float = 0.0
    end_form: Literal["open", "closed", "closed_ground"] = "open"
    at: Vec3 = (0.0, 0.0, 0.0)
    axis: Vec3 = (0.0, 0.0, 1.0)

    @model_validator(mode="after")
    def _check(self) -> "CoilOp":
        _nonzero(self.axis, "axis")
        _positive(self.mean_diameter, "mean_diameter")
        for name in ("pitch", "length"):
            v = getattr(self, name)
            if v is not None:
                _positive(v, name)
        if isinstance(self.section, RectProfile) and self.section.corner_radius > 0:
            raise ValueError("coil: a rect section has sharp corners (corner_radius 0)")
        if not math.isfinite(self.start_angle) or abs(self.start_angle) > 3600.0:
            raise ValueError("start_angle must be a finite angle in degrees")
        self.law()
        return self

    def section_size(self) -> tuple[float, float]:
        """(size across the radius, size along the axis) of the section."""
        s = self.section
        if isinstance(s, CircleProfile):
            return 2.0 * s.radius, 2.0 * s.radius
        return s.width, s.length

    def law(self) -> CoilLaw:
        s_rad, s_ax = self.section_size()
        return coil_law(mean_diameter=self.mean_diameter, s_rad=s_rad, s_ax=s_ax, turns=self.turns, pitch=self.pitch,
                        length=self.length, end_form=self.end_form, handedness=self.handedness, start_angle=self.start_angle)

    def frame(self) -> Frame:
        """Coil frame: origin ``at`` (seating plane), z = axis, x = the start-angle reference (dominant-axis rule)."""
        return Frame.make(self.at, self.axis)


MAX_TEXT_CHARS = 120


class TextOp(_Op):
    """One line of lettering on ``plane`` (generation reliability M): raised (``depth`` > 0, material added from the
    plane along its normal) or engraved (``depth`` < 0, cut from the plane against its normal). ``height`` is the font
    size (em height, mm); ``at`` is the centre of the text block's bounding box in plane coordinates, ``rotation``
    degrees counter-clockwise seen from the normal (the text reads along the plane's u axis). ``font`` names the
    requested font; the kernel uses it when installed and covering the text, else the first installed CJK font
    (:mod:`app.cad.text_geometry`) and reports the substitution; no usable font fails ``unsupported_font``."""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    height: float
    depth: float
    plane: PlaneSpec = "XY"
    offset: float = 0.0
    at: Vec2 = (0.0, 0.0)
    rotation: float = 0.0
    font: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check(self) -> "TextOp":
        if not self.text.strip():
            raise ValueError("text must contain a visible character")
        if any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in self.text):
            raise ValueError("text is one line without control characters (one text op per line)")
        if not (0.1 <= self.height <= 1000.0):
            raise ValueError("height (font size, mm) must be between 0.1 and 1000")
        if not math.isfinite(self.depth) or not (MIN_DIM <= abs(self.depth) <= 100.0):
            raise ValueError("depth must be +0.01..100 (raised) or -0.01..-100 (engraved) mm")
        if not math.isfinite(self.rotation) or abs(self.rotation) > 3600.0:
            raise ValueError("rotation must be a finite angle in degrees")
        return self


class ShellOp(_Op):
    """Hollow the current solid inward to walls of ``thickness``. The outermost planar faces whose outward normal is
    one of ``open_faces`` are removed (an open top: ``[[0, 0, 1]]``); no open face gives a closed hollow body."""

    type: Literal["shell"] = "shell"
    thickness: float
    open_faces: list[Vec3] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _check(self) -> "ShellOp":
        _positive(self.thickness, "thickness")
        for i, d in enumerate(self.open_faces):
            _nonzero(d, f"open_faces[{i}]")
        return self


class LinearLayout(BaseModel):
    """Instances (i, j) at ``i·spacing·direction + j·spacing2·direction2`` for i < count, j < count2."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["linear"] = "linear"
    direction: Vec3
    spacing: float
    count: int = Field(ge=2, le=200)
    direction2: Vec3 | None = None
    spacing2: float = 0.0
    count2: int = Field(default=1, ge=1, le=200)

    @model_validator(mode="after")
    def _check(self) -> "LinearLayout":
        _nonzero(self.direction, "direction")
        _positive(abs(self.spacing), "spacing")
        if self.count2 > 1:
            if self.direction2 is None:
                raise ValueError("count2 > 1 needs direction2")
            _nonzero(self.direction2, "direction2")
            _positive(abs(self.spacing2), "spacing2")
            if abs(dot3(unit3(self.direction), unit3(self.direction2))) > math.cos(math.radians(1.0)):
                raise ValueError("direction2 must not be parallel to direction")
        return self


class CircularLayout(BaseModel):
    """Instance k rotated by ``k·step_angle`` (default 360 / count) about the axis through ``axis_origin``."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["circular"] = "circular"
    axis_origin: Vec3 = (0.0, 0.0, 0.0)
    axis: Vec3 = (0.0, 0.0, 1.0)
    count: int = Field(ge=2, le=360)
    step_angle: float | None = None

    @model_validator(mode="after")
    def _check(self) -> "CircularLayout":
        _nonzero(self.axis, "axis")
        step = self.step
        if not (1e-6 < abs(step) <= 360.0) or abs(step) * (self.count - 1) >= 360.0 - 1e-9:
            raise ValueError("step_angle × (count − 1) must stay below 360 degrees (instances may not repeat)")
        return self

    @property
    def step(self) -> float:
        return self.step_angle if self.step_angle is not None else 360.0 / self.count


PatternLayout = Annotated[Union[LinearLayout, CircularLayout], Field(discriminator="kind")]


class PatternOp(_Op):
    """Copies of an earlier body, hole or cut op at the ``layout`` instances (instance 0 is the target itself); every
    copy uses the target's own boolean (add / subtract) and must change the solid."""

    type: Literal["pattern"] = "pattern"
    target: str
    layout: PatternLayout

    @model_validator(mode="after")
    def _check(self) -> "PatternOp":
        n = self.layout.count * (self.layout.count2 if isinstance(self.layout, LinearLayout) else 1)
        if n > MAX_PATTERN_INSTANCES:
            raise ValueError(f"a pattern makes at most {MAX_PATTERN_INSTANCES} instances (got {n})")
        return self


Operation = Annotated[
    Union[BoxOp, CylinderOp, ExtrudeOp, HoleOp, FilletOp, ChamferOp, TransformOp, CutOp, RevolveOp, SweepOp, BentPlateOp,
          ShellOp, PatternOp, CoilOp, TextOp],
    Field(discriminator="type"),
]

BODY_TYPES = {"box", "cylinder", "extrude", "revolve", "sweep", "bent_plate", "coil"}
PATTERN_TARGET_TYPES = BODY_TYPES | {"hole", "cut"}
#: Op types introduced by the W3 geometry library (extrude / hole variants are recognised by :func:`w3_kind`).
W3_TYPES = ("cut", "revolve", "sweep", "bent_plate", "shell", "pattern", "coil")


def w3_kind(op: Any) -> str | None:
    """The W3 feature kind of an op (its independent check ``geometry.feature.<kind>``), or None for an op type /
    variant that existed before (box, cylinder, named-plane extrude of a basic profile, world hole, fillet …)."""
    if op.type in W3_TYPES:
        return op.type
    if op.type == "extrude" and (not isinstance(op.plane, str) or op.profile.kind == "sketch"):
        return "extrude"
    if op.type == "hole" and (op.plane is not None or op.flush_entry):
        return "hole"
    return None


def plane_refs(op: Any) -> list[str]:
    """Op ids an op's plane refers to through a :class:`DatumRef`."""
    p = getattr(op, "plane", None)
    return [p.op_id] if isinstance(p, DatumRef) and p.op_id is not None else []


def remap_refs(op: dict[str, Any], mapping: dict[str, str]) -> None:
    """Rewrite the op ids an op dict refers to (transform / pattern ``target``, DatumRef ``plane.ref``) in place."""
    if op.get("type") in ("transform", "pattern") and op.get("target") in mapping:
        op["target"] = mapping[op["target"]]
    plane = op.get("plane")
    if isinstance(plane, dict) and isinstance(plane.get("ref"), str) and "." in plane["ref"]:
        head, rest = plane["ref"].split(".", 1)
        if head in mapping:
            plane["ref"] = f"{mapping[head]}.{rest}"


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["step"] = "step"
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelSpec(BaseModel):
    """Everything the kernel needs to rebuild one revision."""

    model_config = ConfigDict(extra="forbid")

    base: StepBase | None = None
    ops: list[Operation] = Field(default_factory=list, max_length=MAX_OPS)

    @model_validator(mode="after")
    def _check_sequence(self) -> "ModelSpec":
        seen: set[str] = set()
        bodies = 0
        for op in self.ops:
            if op.id in seen:
                raise ValueError(f"duplicate op id {op.id}")
            seen.add(op.id)
            if op.type in BODY_TYPES:
                if op.mode == "new":
                    if self.base is not None or bodies > 0:
                        raise ValueError(f"{op.id}: mode 'new' is only allowed for the first body without an imported base")
                elif bodies == 0 and self.base is None and not op.suppressed:
                    raise ValueError(f"{op.id}: the first body must use mode 'new'")
                if not op.suppressed:
                    bodies += 1
            elif op.type == "transform":
                if op.target not in seen:
                    raise ValueError(f"{op.id}: transform target {op.target} must be an earlier op")
            elif op.type == "pattern":
                if op.target not in seen or op.target == op.id:
                    raise ValueError(f"{op.id}: pattern target {op.target} must be an earlier op (deleted or renumbered?)")
            for ref in plane_refs(op):
                if ref not in seen or ref == op.id:
                    raise ValueError(f"{op.id}: plane ref {ref} must name an earlier op (deleted or renumbered?); pick a datum "
                                     "of an op above it")
        return self


OPERATIONS_ADAPTER: TypeAdapter[list[Operation]] = TypeAdapter(list[Operation])


def canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_dump(spec: ModelSpec) -> dict:
    return spec.model_dump(mode="json", exclude_none=False)


def next_op_id(ops: list[Operation] | list[dict]) -> str:
    """Next free id. Ids are never reused within a part, so callers pass every
    id the part has ever used (including deleted ones) when they have them."""
    n = 0
    for op in ops:
        oid = op["id"] if isinstance(op, dict) else op.id
        m = OP_ID_RE.match(oid)
        if m:
            n = max(n, int(oid[2:]))
    return f"F-{n + 1:02d}"


def model_spec_json_schema() -> dict:
    """JSON schema for structured model output (AI agent)."""
    return ModelSpec.model_json_schema()


def build_key(spec: ModelSpec, kernel_version: str) -> str:
    payload = canonical_json({"kernel": kernel_version, "spec": spec_dump(spec)})
    return hashlib.sha256(payload.encode()).hexdigest()


# --- Geometry helpers (pure Python; shared by the kernel, drawing and UI code) ---


def _unit(v: Vec3) -> Vec3:
    n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return (v[0] / n, v[1] / n, v[2] / n)


def hole_frame(direction: Vec3) -> tuple[Vec3, Vec3, Vec3]:
    """In-plane axes ``(u, v, d)`` for a hole drilled along ``direction``.

    ``d`` is the unit drilling direction. ``u`` and ``v`` are world axes
    projected into the plane normal to ``d`` (Gram-Schmidt), chosen by the
    dominant component of ``d``: Z-dominant -> (X, Y), X-dominant -> (Y, Z),
    Y-dominant -> (X, Z). So a hole drilled along -Z patterns along world X/Y.
    """
    d = _unit(direction)
    ax, ay, az = abs(d[0]), abs(d[1]), abs(d[2])
    if az >= ax and az >= ay:
        refs: tuple[Vec3, Vec3] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    elif ax >= ay:
        refs = ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    else:
        refs = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0))

    def _reject(v: Vec3, *basis: Vec3) -> Vec3:
        out = list(v)
        for b in basis:
            dot = out[0] * b[0] + out[1] * b[1] + out[2] * b[2]
            out = [out[i] - dot * b[i] for i in range(3)]
        return _unit((out[0], out[1], out[2]))

    u = _reject(refs[0], d)
    v = _reject(refs[1], d, u)
    return u, v, d


def hole_entry(op: HoleOp) -> Vec3:
    """``op.at`` in world coordinates (the plane maps it when the hole has a local ``plane``)."""
    if op.plane is None:
        return (float(op.at[0]), float(op.at[1]), float(op.at[2]))
    return op.plane.frame().point(*op.at)


def hole_world_frame(op: HoleOp) -> tuple[Vec3, Vec3, Vec3]:
    """``(u, v, d)`` in world coordinates: :func:`hole_frame` of the direction for a world hole; for a hole on a
    local plane, d = the direction mapped by the plane and u / v = the plane's x / y made perpendicular to d."""
    if op.plane is None:
        return hole_frame(op.direction)
    f = op.plane.frame()
    d = _unit(f.vector(*op.direction))

    def _reject(w: Vec3, *basis: Vec3) -> Vec3:
        out = list(w)
        for b in basis:
            k = out[0] * b[0] + out[1] * b[1] + out[2] * b[2]
            out = [out[i] - k * b[i] for i in range(3)]
        return _unit((out[0], out[1], out[2]))

    u = _reject(f.x, d)
    v = _reject(f.y, d, u)
    return u, v, d


def hole_positions(op: HoleOp) -> list[Vec3]:
    """Entry point of every hole instance, in world coordinates.

    With a local ``plane`` the entry and the pattern axes come from that plane (:func:`hole_world_frame`).

    - No pattern: ``[op.at]``.
    - Linear pattern: ``op.at`` is the first instance; instance ``(i, j)`` sits
      at ``at + i*pitch[0]*u + j*pitch[1]*v`` for ``i < count[0]``,
      ``j < count[1]`` (negative pitch walks the other way), ``u``/``v`` from
      :func:`hole_frame`. Order: j-major rows, i within a row.
    - Circular pattern: ``op.at`` is the pattern centre (no hole there);
      instance ``k`` sits at ``at + radius*(cos t*u + sin t*v)`` with
      ``t = 360*k/count`` degrees, starting on ``+u``.
    """
    at = hole_entry(op)

    def _clean(p: tuple[float, ...]) -> Vec3:
        return (round(p[0], 9) + 0.0, round(p[1], 9) + 0.0, round(p[2], 9) + 0.0)

    if op.pattern is None:
        return [_clean(tuple(float(c) for c in at))]
    u, v, _ = hole_world_frame(op)
    out: list[Vec3] = []
    if isinstance(op.pattern, LinearPattern):
        nx, ny = op.pattern.count
        px, py = op.pattern.pitch
        for j in range(ny):
            for i in range(nx):
                out.append(_clean(tuple(at[k] + i * px * u[k] + j * py * v[k] for k in range(3))))
    else:
        n, r = op.pattern.count, op.pattern.radius
        for k in range(n):
            t = math.radians(360.0 * k / n)
            c, s = math.cos(t), math.sin(t)
            out.append(_clean(tuple(at[m] + r * (c * u[m] + s * v[m]) for m in range(3))))
    return out
