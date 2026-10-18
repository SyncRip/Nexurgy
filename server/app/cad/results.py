"""Result models of the CAD library.

These are plain Pydantic models with no geometry-kernel dependency, so API
schemas and services can import them without loading build123d / OCP.
Binary artifacts live in fields excluded from every dump; use ``summary()``
(or ``model_dump``) for a JSON-safe view. See design.md §2.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

FeatureStatus = Literal["ok", "error", "suppressed", "skipped"]
ViewName = Literal["front", "top", "right", "iso"]


class Issue(BaseModel):
    """An error or warning, attributed to an op id when it has one.
    ``details`` carries machine-readable context for some codes (e.g.
    ``hole_missed``: ``missed_instances``, ``missed_positions``)."""

    op_id: str | None = None
    code: str
    message: str
    details: dict[str, Any] | None = None


class BBox(BaseModel):
    min: list[float]
    max: list[float]
    size: list[float]


class Metrics(BaseModel):
    valid: bool
    solids: int
    faces: int
    edges: int
    volume_mm3: float
    area_mm2: float
    bbox: BBox
    center_of_mass: list[float]


class FeatureRecord(BaseModel):
    """Per-op build status.

    ``status`` is ``ok`` / ``error`` / ``suppressed`` as in design.md, plus
    ``skipped`` for ops after the first failing op (they were not replayed).
    ``detail`` carries op-specific numbers (e.g. ``edge_count`` for fillet and
    chamfer, ``solids`` for an imported base).
    """

    op_id: str
    type: str
    status: FeatureStatus
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


class HoleRecord(BaseModel):
    """An explicit hole feature (from a HoleOp), one record per op.

    ``axis`` is the unit drilling direction, ``entry`` the op's ``at`` point,
    ``count`` the number of instances (pattern included) and ``positions`` the
    entry point of every instance. ``depth`` is measured from the entry point
    along ``axis`` (null for through holes); blind holes have a flat bottom.
    """

    op_id: str
    diameter: float
    depth: float | None
    through: bool
    axis: list[float]
    entry: list[float]
    count: int
    counterbore: dict[str, float] | None = None
    countersink: dict[str, float] | None = None
    positions: list[list[float]] = Field(default_factory=list)


class DetectedHole(BaseModel):
    """A hole-like cylindrical bore found on an imported base solid.

    NOT an explicit feature: it is a geometric guess (full 360° inner
    cylinder) and must be confirmed before use in planning or quoting.
    Coaxial counterbores are reported as separate bores. ``axis`` points
    from ``entry`` into the material when that could be decided.
    """

    source: str = "F-00"
    confidence: Literal["heuristic"] = "heuristic"
    diameter: float
    length: float
    through: bool | None
    axis: list[float]
    count: int
    positions: list[list[float]]


class BuildArtifacts(BaseModel):
    """Exports of the final solid, all from the same in-memory Part.

    ``glb`` follows glTF conventions (metres, +Y up via a root-node rotation);
    ``mesh_json`` stays in mm with +Z up (world frame of the ops).
    """

    step: bytes = b""
    glb: bytes = b""
    mesh_json: bytes = b""


class BuildResult(BaseModel):
    """Outcome of one kernel build (design.md §2).

    On failure (``ok=False``) ``metrics`` / ``artifacts`` describe the last
    good state before the failing op (or are empty when there is none), so the
    editor can still show something; never persist them as a ready revision.
    ``detected_holes`` is only filled for an imported base.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    build_key: str
    kernel_version: str
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    metrics: Metrics | None = None
    features: list[FeatureRecord] = Field(default_factory=list)
    holes: list[HoleRecord] = Field(default_factory=list)
    detected_holes: list[DetectedHole] = Field(default_factory=list)
    artifacts: BuildArtifacts = Field(default_factory=BuildArtifacts, exclude=True)

    # In-memory build123d shape of the geometry described by ``metrics``; not
    # serialised. ``drawing.make_drawing`` uses it to avoid a rebuild.
    _shape: Any = PrivateAttr(default=None)
    # Per-op trace of the replay (kernel.OpTrace: solid before / after, tools, frames) for the independent feature
    # checks (cad/feature_checks.py); never serialised.
    _trace: Any = PrivateAttr(default=None)

    @property
    def shape(self) -> Any:
        """The in-memory solid (None after deserialisation or when empty)."""
        return self._shape

    @property
    def trace(self) -> list[Any]:
        """The replay trace (empty after deserialisation)."""
        return list(self._trace or [])

    def summary(self) -> dict[str, Any]:
        """JSON-safe dump without artifact bytes (sizes are included)."""
        data = self.model_dump(mode="json")
        data["artifact_sizes"] = {
            "step": len(self.artifacts.step),
            "glb": len(self.artifacts.glb),
            "mesh_json": len(self.artifacts.mesh_json),
        }
        return data


class MeshInspection(BaseModel):
    """Inspection of a mesh reference file (STL / OBJ / GLB).

    Meshes are reference-only: they can be previewed and measured but can
    never become a model base or be exported to STEP. ``preview`` holds GLB
    bytes for GLB input (unchanged) and kernel-format ``mesh_json`` otherwise.
    ``volume_mm3`` is only given when the mesh looks closed.
    """

    reference_only: Literal[True] = True
    format: Literal["stl", "obj", "glb"]
    units_assumed: str
    triangles: int
    vertices: int
    bbox: BBox | None
    volume_mm3: float | None
    area_mm2: float | None
    watertight: bool
    warnings: list[Issue] = Field(default_factory=list)
    preview_format: Literal["glb", "mesh_json"]
    preview: bytes = Field(default=b"", exclude=True)


class TitleBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: str = ""
    part_name: str = ""
    revision: str = ""
    material: str = ""
    author: str = ""
    date: str = ""
    project: str = ""


class DrawingConfig(BaseModel):
    """Drawing request. ``scale`` is ``"auto"`` or a model-to-paper factor
    (2 = 2:1, 0.5 = 1:2). ``projection`` picks the view layout: ``third``
    (top above front, right on the right) or ``first`` (ISO / GB)."""

    model_config = ConfigDict(extra="forbid")

    views: list[ViewName] = Field(default_factory=lambda: ["front", "top", "right", "iso"], min_length=1)
    scale: Literal["auto"] | float = "auto"
    projection: Literal["third", "first"] = "third"
    hidden_lines: bool = True
    dimensions: bool = True
    title: TitleBlock = Field(default_factory=TitleBlock)
    units: Literal["mm"] = "mm"


class ViewInfo(BaseModel):
    """A placed view. ``bbox`` is in sheet mm, origin top-left, y down:
    ``[x_min, y_min, x_max, y_max]``."""

    name: ViewName
    scale: float
    bbox: list[float]


class DrawingOutput(BaseModel):
    svg: bytes = Field(default=b"", exclude=True)
    dxf: bytes = Field(default=b"", exclude=True)
    sheet: dict[str, Any]
    scale: float
    views: list[ViewInfo]
    callouts: list[str] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Native DWG adapter (cad/dwg.py, cad/DWG_NOTES.md)
#
# Every model below is JSON-serialisable with ``model_dump(mode="json")`` so a
# service can store it as the provenance of a native file. File bytes live in
# fields excluded from every dump.
# --------------------------------------------------------------------------- #


class DwgTool(BaseModel):
    """One LibreDWG command-line tool as found on this host."""

    name: str
    path: str | None = None
    version: str | None = None
    sha256: str | None = None


class DwgRuntime(BaseModel):
    """The DWG converter runtime (LibreDWG CLI tools, run as separate processes).

    ``available`` is False (with ``reason``) when a tool is missing or its
    version is not one the normalisation recipe was verified with.
    ``fingerprint`` hashes the tool and library binaries, so two hosts with the
    same fingerprint run the same build. ``independent_reader`` names the
    second, unrelated DWG decoder (ezdwg) or is None when it is not installed.
    """

    available: bool
    reason: str | None = None
    libredwg_version: str | None = None
    tools: list[DwgTool] = Field(default_factory=list)
    libraries: dict[str, str] = Field(default_factory=dict)
    host: str
    fingerprint: str | None = None
    independent_reader: str | None = None
    ezdxf_version: str


class DwgBlob(BaseModel):
    """Identity of one file in a conversion (input, intermediate or output)."""

    format: Literal["dwg", "dxf"]
    role: Literal["source", "intermediate", "derived", "raw_converter_output", "output"]
    sha256: str
    size: int
    version: str | None = None


class DwgStep(BaseModel):
    """One step of a conversion: an external process, an in-process transform
    (normalisation N*, sanitising S*) or a check. ``counts`` holds the
    step-specific numbers; process steps also carry the exit code and the
    LibreDWG diagnostics, normalised (numbers -> ``#``) and counted."""

    name: str
    kind: Literal["process", "transform", "check"]
    description: str = ""
    seconds: float = 0.0
    counts: dict[str, Any] = Field(default_factory=dict)
    argv: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    n_errors: int = 0
    n_warnings: int = 0
    errors: dict[str, int] = Field(default_factory=dict)
    warnings: dict[str, int] = Field(default_factory=dict)
    unstable_classes: dict[str, int] = Field(default_factory=dict)


class DwgQuarantinedEntity(BaseModel):
    """An entity removed from the derived DXF because it cannot be trusted
    (for example a non-finite coordinate). It is reported, never zero-filled."""

    handle: str
    type: str
    layer: str | None = None
    section: str
    reason: str
    codes: list[int] = Field(default_factory=list)


class DwgReadbackCheck(BaseModel):
    """Comparison of a written DWG, decoded by one reader, with the source DXF.

    ``skipped`` means the reader was not run (never that it passed).
    """

    reader: Literal["libredwg", "ezdwg"]
    reader_version: str | None = None
    status: Literal["passed", "failed", "skipped"]
    reason: str | None = None
    entities: int = 0
    entities_matched: int = 0
    dimensions: int = 0
    dimensions_matched: int = 0
    texts: int = 0
    mismatches: list[str] = Field(default_factory=list)
    seconds: float = 0.0


class DwgReadResult(BaseModel):
    """DWG -> DXF result. ``dxf`` (bytes, excluded from dumps) is the sanitised,
    derived DXF; it is an interpretation of the DWG, not the original."""

    source: DwgBlob
    version_status: Literal["provisional", "untested"]
    container: dict[str, Any] = Field(default_factory=dict)
    runtime: DwgRuntime
    steps: list[DwgStep] = Field(default_factory=list)
    load_mode: Literal["strict", "recover"]
    raw_dxf: DwgBlob
    derived_dxf: DwgBlob
    inventory: dict[str, Any] = Field(default_factory=dict)
    decoded_vs_emitted: list[dict[str, Any]] = Field(default_factory=list)
    quarantined: list[DwgQuarantinedEntity] = Field(default_factory=list)
    losses: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    seconds: float = 0.0
    dxf: bytes = Field(default=b"", exclude=True)


class DwgWriteResult(BaseModel):
    """DXF -> DWG result. Only returned when every readback that ran matched
    the source; ``verified`` is True only when the independent reader ran too.
    ``dwg`` holds the native file; ``intermediate_dxf`` is the normalised R2000
    DXF fed to the converter (an interim artefact, never a deliverable)."""

    target: Literal["AC1015"]
    source: DwgBlob
    intermediate: DwgBlob
    output: DwgBlob
    runtime: DwgRuntime
    steps: list[DwgStep] = Field(default_factory=list)
    readback: list[DwgReadbackCheck] = Field(default_factory=list)
    verified: bool
    losses: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    seconds: float = 0.0
    dwg: bytes = Field(default=b"", exclude=True)
    intermediate_dxf: bytes = Field(default=b"", exclude=True)
