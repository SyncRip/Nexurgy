"""Workflow node definitions: ports, parameters and retry policy (design.md §7).

Handlers live in workflow/handlers.py and are attached by node type; this
module only describes nodes so that graph validation and the canvas palette
(GET /workflow-node-types) share one source of truth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..ai import limits

# W8: engineering_spec (a W2 spec version), assembly (a W4 assembly revision, or the parts built when it could not be),
# drawing_package (a W5 assembly drawing package), delivery (a delivery manifest).
PortType = Literal["text", "file", "model", "drawing", "spec", "plan", "quote", "engineering_spec", "assembly", "drawing_package",
                   "delivery", "manufacturing_plan", "manufacturing_quote", "any"]


@dataclass(frozen=True)
class Port:
    name: str
    type: PortType
    required: bool = True
    multiple: bool = False
    # W8: the node still runs when this input's upstream failed or was skipped (engine._activate_downstream); the
    # input is then what the failed node recorded (often nothing). delivery_package uses it to list what is missing.
    after_failure: bool = False


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextInputParams(_Params):
    text: str = Field(default="", max_length=limits.CHAT_TEXT_CHARS)


class FileInputParams(_Params):
    file_version_id: uuid.UUID | None = None


class ModelInputParams(_Params):
    revision_id: uuid.UUID | None = None


class Model3DParams(_Params):
    text: str = Field(default="", max_length=limits.CHAT_TEXT_CHARS)
    model_config_id: uuid.UUID | None = None
    # AI workspace (services/assistant_context.py): ONLY non-private, operation-specific business
    # facts: the target selection (CAD pick) / target facts. Rendered as a <context> block before
    # the request. The graph is visible to every project member and is copied by reruns, so the
    # private conversation context (memory, pinned constraints, other references) never goes here.
    # Internal: set by the assistant, not shown in the workflow editor (node_catalog drops it).
    context: str = Field(default="", max_length=limits.MODEL_CONTEXT_CHARS, json_schema_extra={"x-internal": True})
    # AI workspace operation that started this run. The handler reads the operation's private
    # context (its owner-only context build) only while operation.run_id is this node's run; a
    # copied graph or another run gets nothing. Internal, like `context`.
    assistant_operation_id: uuid.UUID | None = Field(default=None, json_schema_extra={"x-internal": True})


class DrawingReconstructParams(_Params):
    """Drawing → STEP (services/ai_reconstruct.py). The drawing comes from the ``file`` input (a DXF / DWG
    file version), the ``drawing`` input, or ``drawing_id`` (a drawing of the run's project) when nothing is
    connected."""

    sheet: str = Field(default="", max_length=200)  # frame id (S29) or drawing number; asked when the file has several sheets
    notes: str = Field(default="", max_length=limits.CHAT_TEXT_CHARS)  # the user's notes (placement, units, …); cited as "request"
    model_config_id: uuid.UUID | None = None
    drawing_id: uuid.UUID | None = None
    # AI workspace (as Model3DParams): the public target block and the operation that started this run.
    context: str = Field(default="", max_length=limits.MODEL_CONTEXT_CHARS, json_schema_extra={"x-internal": True})
    assistant_operation_id: uuid.UUID | None = Field(default=None, json_schema_extra={"x-internal": True})


class SectionPlaneParam(_Params):
    """Explicit cutting plane (same shape as cad.drawing.SectionPlane; kept here to avoid the CAD import)."""

    plane: Literal["XZ", "YZ"]
    offset: float


class DrawingParams(_Params):
    """Forwarded to services.drawings.generate as the package config. Not editor params (yet):
    hidden_lines / dimensions (workspace and agent tool) and title overrides (workspace only)."""

    views: list[Literal["front", "top", "right", "iso"]] = Field(default_factory=lambda: ["front", "top", "right", "iso"])
    scale: float | Literal["auto"] = "auto"
    projection: Literal["third", "first"] = "third"
    hidden_lines: bool = True
    dimensions: bool = True
    section: Literal["auto", "none"] | SectionPlaneParam = "auto"


class IntakeParams(_Params):
    model_config_id: uuid.UUID | None = None
    hints: str = Field(default="", max_length=limits.INTAKE_HINTS_CHARS)
    material: str | None = None


class ProcessPlanParams(_Params):
    allowance_mm: float = Field(default=3.0, ge=0, le=50)


class QuoteParams(_Params):
    quantities: list[Annotated[int, Field(ge=1, le=1_000_000)]] = Field(default_factory=lambda: [1, 10, 50], min_length=1, max_length=8)
    margin_pct: float = Field(default=15.0, ge=0, le=200)
    tax_pct: float = Field(default=13.0, ge=0, le=50)
    validity_days: int = Field(default=30, ge=1, le=365)


class HumanReviewParams(_Params):
    stage: Literal["design", "process", "quote"] = "design"
    instructions: str = Field(default="", max_length=2000)


class ExportParams(_Params):
    # svg / dxf / dwg each select a connected drawing; its package is exported whole (every page, every
    # format, check reports, manifest.json), never a subset.
    include: list[Literal["step", "glb", "svg", "dxf", "dwg", "plan", "quote"]] = Field(
        default_factory=lambda: ["step", "svg", "dxf", "plan", "quote"]
    )


# --- collaboration (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §3.3) --------------
# A business_task node is one real Task with its executor, final reviewer,
# acceptance criteria and output slots. Edges only express dependency; the
# slot mapping lives in input_slots. Its step completes when the task is accepted.

AssetTypeName = Literal["part_model", "drawing", "process_plan", "quote", "document", "intake", "process_module",
                        # W8: products of the engineering chain tools (agent_tools engineering_spec … delivery_package)
                        "assembly_model", "assembly_drawing", "delivery_manifest"]
SLOT_PATTERN = r"^[a-z][a-z0-9_:.-]{0,79}$"


class ActorParam(_Params):
    """A person (project member) or an agent profile; exactly one id (services.actors.ActorRef)."""

    kind: Literal["human", "agent"]
    user_id: uuid.UUID | None = None
    agent_profile_id: uuid.UUID | None = None


class CriterionParam(_Params):
    description: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)
    required: bool = True
    allowed_types: list[AssetTypeName] = Field(default_factory=list, max_length=6)
    # services.tasks.RuleSpec shape; validated against it at planning and start.
    rule_spec: dict[str, Any] = Field(default_factory=dict)
    manual_required: bool = False


class SlotParam(_Params):
    slot_key: str = Field(pattern=SLOT_PATTERN)
    asset_type: AssetTypeName
    required: bool = True


class InputSlotParam(_Params):
    slot_key: str = Field(pattern=SLOT_PATTERN)
    asset_type: AssetTypeName
    # Either an upstream node's output slot (the node must be a direct predecessor) or a fixed version.
    source_node_id: str | None = Field(default=None, max_length=60)
    source_slot_key: str | None = Field(default=None, pattern=SLOT_PATTERN)
    fixed_asset_version_id: uuid.UUID | None = None
    required: bool = True


class RecipeParam(_Params):
    instructions: str = Field(default="", max_length=limits.LONG_NOTE_CHARS)
    tools: list[str] = Field(default_factory=list, max_length=12)


# --- general mechanical engineering chain (W8, services/orchestration.py) ------------------


class EngineeringSpecParams(_Params):
    """Text and / or file inputs → a W2 spec version (extracted with the run's policy and frozen planning model), or an
    exact existing version (``spec_version_id``) passed through."""

    text: str = Field(default="", max_length=limits.CHAT_TEXT_CHARS)
    title: str = Field(default="", max_length=200)
    spec_version_id: uuid.UUID | None = None
    # Generation supplement A-04 (IN-07): with ``spec_version_id`` and new text / files, make a new version of that spec
    # (its sources + the new files, the text as a revision note) instead of refusing the mix (ambiguous_input).
    revise: bool = False
    # Workspace opt-in; handler additionally verifies the linked operation's original human authorization.
    design_from_requirements: bool = Field(default=False, json_schema_extra={"x-internal": True})
    model_config_id: uuid.UUID | None = None
    # Set by the AI workspace (referenced project files); the canvas connects file_input nodes instead.
    file_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20, json_schema_extra={"x-internal": True})


class ConceptDesignParams(_Params):
    model_config_id: uuid.UUID | None = None


class ManufacturingExportParams(_Params):
    pass


class AssemblyFromSpecParams(_Params):
    """Spec version → every part definition built (W3 template, else AI modeling of a new part) → W4 assembly revision."""

    number: str = Field(default="", max_length=80)
    model_config_id: uuid.UUID | None = None
    run_checks: bool = True  # standard: interference after creation (W4); Turbo records skipped_turbo
    # False models fresh definitions across runs; a retry of this same node still resumes its completed work.
    reuse_earlier_builds: bool = True
    # Part definitions modeled at the same time (None: ``limits.MODELING_CONCURRENCY``; 1: one after another)
    modeling_concurrency: int | None = Field(default=None, ge=1, le=limits.MODELING_CONCURRENCY_MAX)


class AssemblyDrawingParams(_Params):
    """Forwarded to services.assembly_drawings.generate (W5) as the package config."""

    config: dict[str, Any] = Field(default_factory=dict)
    # Generation supplement E (layered delivery, BHD1 §8.7): also one package per distinct sub-assembly revision (its own
    # GA + BOM + title block), and the root package draws the detail sheet of every part definition (all procurement
    # kinds unless config.detail_procurements names them). The delivery then requires every level's STEP and DWG.
    # Off by default for hand-built graphs (one package, as before); the built-in chain template and the assistant's
    # assembly_create chain turn it on.
    subassemblies: bool = False


class DeliveryParams(_Params):
    bom_formats: list[Literal["json", "csv"]] = Field(default_factory=lambda: ["json", "csv"], min_length=1)
    # generation reliability M (design §2.7): the protocol of every delivered STEP, frozen with the run's graph
    step_protocol: Literal["AP203", "AP214"] = "AP203"


class BusinessTaskParams(_Params):
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=limits.OBJECTIVE_CHARS)
    executor: ActorParam
    # Required by standard collaboration runs (services.collaboration.validate_contract: reviewer_required); a Turbo
    # run has no acceptance subject (W7), so its draft may leave it empty.
    reviewer: ActorParam | None = None
    pre_reviewer_agent_id: uuid.UUID | None = None
    criteria: list[CriterionParam] = Field(min_length=1, max_length=50)
    output_slots: list[SlotParam] = Field(min_length=1, max_length=20)
    input_slots: list[InputSlotParam] = Field(default_factory=list, max_length=20)
    recipe: RecipeParam = Field(default_factory=RecipeParam)


@dataclass(frozen=True)
class NodeDef:
    type: str
    category: Literal["input", "cad", "engineering", "manufacturing", "review", "output", "collaboration"]
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    params: type[_Params]
    max_attempts: int = 1
    uses_ai: bool = False
    # Graph-level rule beyond per-port "required" (checked in graph.validate).
    rule: str | None = None
    doc: str = ""
    extra: dict = field(default_factory=dict)

    def input(self, name: str) -> Port | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> Port | None:
        return next((p for p in self.outputs if p.name == name), None)


NODE_DEFS: dict[str, NodeDef] = {
    d.type: d
    for d in [
        NodeDef("text_input", "input", (), (Port("text", "text"),), TextInputParams, rule="text_required",
                doc="Free text, e.g. a design request or a change request."),
        NodeDef("file_input", "input", (), (Port("file", "file"),), FileInputParams, rule="file_required",
                doc="A project file version: STEP / STL / OBJ / GLB model or a customer drawing."),
        NodeDef("model_input", "input", (), (Port("model", "model"),), ModelInputParams, rule="revision_required",
                doc="An existing model revision of the run's part."),
        NodeDef(
            "model_3d", "cad",
            (Port("file", "file", required=False), Port("text", "text", required=False), Port("model", "model", required=False)),
            (Port("model", "model"),),
            Model3DParams, max_attempts=2, uses_ai=True, rule="model_3d_inputs",
            doc="File only: import and inspect (no AI; STEP is audited first, assemblies are refused). Text only: AI creates a model. "
                "File or model + text: AI modifies it. AI results are checked against the stated requirements on the exported STEP; "
                "missing critical facts are asked first.",
        ),
        NodeDef("drawing", "cad", (Port("model", "model"),), (Port("drawing", "drawing"),), DrawingParams,
                doc="Cloud-generated drawing package from the exact revision: overview + section pages (SVG + DXF, DWG R2000 when the converter is available), annotation-checked, with a hashed manifest."),
        NodeDef(
            "drawing_reconstruct", "cad",
            (Port("file", "file", required=False), Port("drawing", "drawing", required=False)),
            (Port("model", "model"),),
            DrawingReconstructParams, max_attempts=2, uses_ai=True, rule="drawing_reconstruct_inputs",
            doc="2D drawing → STEP: a DXF or native DWG (converted to a derived DXF with provenance) is read into a literal "
                "evidence index; one sheet is chosen (asked when several), the AI proposes requirements citing the drawing's "
                "dimensions and texts plus the operations; missing units, conflicting dimensions and missing critical facts are "
                "asked first, out-of-scope parts are refused; the exported STEP is checked against the requirements.",
        ),
        NodeDef("drawing_intake", "manufacturing", (Port("file", "file"),), (Port("spec", "spec"),), IntakeParams,
                max_attempts=2, uses_ai=True,
                doc="Organise a customer drawing into manufacturing data (estimated); needs human check."),
        NodeDef(
            "process_plan", "manufacturing",
            (Port("model", "model", required=False), Port("spec", "spec", required=False)),
            (Port("plan", "plan"),),
            ProcessPlanParams, rule="model_or_spec",
            doc="Deterministic process plan from the model revision or the intake data.",
        ),
        NodeDef("quote", "manufacturing", (Port("plan", "plan"),), (Port("quote", "quote"),), QuoteParams,
                doc="Deterministic quotation (integer cents) from a process plan."),
        NodeDef("human_review", "review", (Port("subject", "any"),), (Port("subject", "any"),), HumanReviewParams,
                doc="Waits for a person with the stage's role to approve or reject; passes the subject through."),
        NodeDef("export", "output", (Port("items", "any", multiple=True),), (Port("bundle", "file"),), ExportParams,
                doc="Bundle artifacts (STEP, whole drawing packages, plan, quote) into a downloadable zip."),
        NodeDef(
            "engineering_spec", "engineering",
            (Port("text", "text", required=False), Port("files", "file", required=False, multiple=True)),
            (Port("spec", "engineering_spec"),),
            EngineeringSpecParams, uses_ai=True, rule="engineering_spec_inputs",
            doc="Engineering description and / or project files → engineering spec version: definitions, instances, relations, "
                "facts with evidence, open questions (standard) or assumptions (Turbo). Or pass an existing spec version through.",
        ),
        NodeDef(
            "concept_design", "engineering", (Port("text", "text"),), (Port("spec", "engineering_spec"),),
            ConceptDesignParams, uses_ai=True,
            doc="Plan a new concept from the original request under run-scoped design authority; preserve AI-origin decisions and geometry checks.",
        ),
        NodeDef(
            "assembly_process_plan", "manufacturing", (Port("assembly", "assembly", after_failure=True),),
            (Port("plans", "manufacturing_plan"),), ProcessPlanParams,
            doc="Machining estimates for the exact frozen BOM revisions; purchase and unsupported parts remain explicit exclusions.",
        ),
        NodeDef(
            "assembly_quote", "manufacturing", (Port("plans", "manufacturing_plan", after_failure=True),),
            (Port("quotes", "manufacturing_quote"),), QuoteParams,
            doc="Draft estimates from exact upstream plans and BOM quantities using synthetic example rates; unknown costs remain unknown.",
        ),
        NodeDef(
            "manufacturing_export", "output",
            (Port("assembly", "assembly", after_failure=True), Port("drawings", "drawing_package", after_failure=True),
             Port("plans", "manufacturing_plan", after_failure=True),
             Port("quotes", "manufacturing_quote", after_failure=True)),
            (Port("bundle", "file"),), ManufacturingExportParams,
            doc="Geometry delivery plus CSV, HTML and JSON manufacturing estimates, with exact sources and hashed files.",
        ),
        NodeDef(
            "assembly_from_spec", "engineering",
            (Port("spec", "engineering_spec"),), (Port("assembly", "assembly"),),
            AssemblyFromSpecParams, uses_ai=True,
            doc="Builds every part definition of the spec (template, else AI modeling of a new part), then the static assembly "
                "(shared definitions, nested sub-assemblies, placements, mates, BOM) and its checks. Standard: a definition blocked "
                "by open questions stops with the questions; Turbo: independent definitions still build. With something missing, "
                "what was built makes a partial assembly (marked, with its coverage); never a complete assembly with a missing "
                "definition.",
        ),
        NodeDef(
            "assembly_drawing", "engineering",
            # generation supplement §2.4: a partial assembly (the step failed with its revision) is drawn as such
            (Port("assembly", "assembly", after_failure=True),), (Port("drawings", "drawing_package"),),
            AssemblyDrawingParams,
            doc="General arrangement and detail drawing package (DWG / DXF / SVG pages, balloons, BOM table) of the exact "
                "assembly revision. A partial assembly is drawn and marked as such; without an assembly revision the step "
                "fails with assembly_missing. With subassemblies on, also one package per sub-assembly revision and a detail "
                "sheet for every part definition (layered delivery).",
        ),
        NodeDef(
            "delivery_package", "engineering",
            (Port("assembly", "assembly", after_failure=True), Port("drawings", "drawing_package", required=False, after_failure=True)),
            (Port("delivery", "delivery"),),
            DeliveryParams,
            doc="Delivery manifest + zip: assembly STEP, unique part STEP set, hierarchical and flattened BOM, drawing package "
                "files, each with its hash and source version; missing items listed. Complete only when every required file exists. "
                "After a layered drawing step: every sub-assembly STEP and package and every part detail DWG too, with a "
                "per-level summary.",
        ),
        NodeDef(
            "business_task", "collaboration",
            (Port("inputs", "any", required=False, multiple=True),), (Port("outputs", "any"),),
            BusinessTaskParams, max_attempts=3, rule="business_task",
            doc="One business task of a collaboration workflow: a real Task with an executor, a final reviewer, "
                "acceptance criteria and output slots. The step completes only when the task is accepted.",
        ),
    ]
}


def _editor_schema(params: type[BaseModel]) -> dict:
    """Params JSON schema without internal fields (``x-internal``: set by services, not by people)."""
    schema = params.model_json_schema()
    props = schema.get("properties") or {}
    hidden = [k for k, v in props.items() if isinstance(v, dict) and v.get("x-internal")]
    for k in hidden:
        props.pop(k)
    if hidden and isinstance(schema.get("required"), list):
        schema["required"] = [k for k in schema["required"] if k not in hidden]
    return schema


def node_catalog() -> list[dict]:
    """Serializable palette for the canvas."""
    out = []
    for d in NODE_DEFS.values():
        out.append(
            {
                "type": d.type,
                "category": d.category,
                "inputs": [p.__dict__ for p in d.inputs],
                "outputs": [p.__dict__ for p in d.outputs],
                "params_schema": _editor_schema(d.params),
                "max_attempts": d.max_attempts,
                "uses_ai": d.uses_ai,
                "doc": d.doc,
            }
        )
    return out
