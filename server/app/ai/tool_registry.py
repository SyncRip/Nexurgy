"""Typed tool registry: the single source of the agent tools (docs/agent/README.zh-CN.md §4.3; execution-layer design
§6.1, §7.1).

One flat Pydantic argument model per tool (without discriminated unions; its fields mirror exactly what
``agent_tools`` reads from ``args``) and its :class:`ToolSpec`: ``kind`` (``write`` / ``read`` /
``review_only`` / ``end``), ``needs_part``, ``produces_assets``. Everything else is generated from here:

- ``agent_tools.TOOL_NAMES`` / ``REVIEW_ONLY_TOOLS`` / ``PART_TOOLS`` (the server tools and their permission sets);
- ``run_tool``'s argument check (:func:`check_args`: ``ok=false`` / ``invalid_args`` before the tool runs);
- the native loop's tool rule (:func:`native_tool_text`, byte-identical to the pre-registry prompt);
- the ``tool_agent`` tools (``harness/tool_agent.py``) and ``GET /agent-tools``.

The two end tools ``deliver`` / ``report_blocked`` mirror ``tool_agent``'s output tools; their semantic check is
``collaboration_agent.deliverable_problems``. Pure library (``app/ai``): no database imports, so the prompts in
``app/ai`` can be generated from it.

Every server tool writes (a version, a candidate, a usage or approval record), so all run one after the other inside an
attempt. P4 adds two groups of ``kind=read`` tools that never change data (they may overlap inside an attempt):

- the review evidence tools (:data:`EVIDENCE_TOOL_SPECS`, listed by ``GET /agent-tools``): offered to every review /
  pre-review attempt on ``tool_agent``, never part of a profile's ``allowed_tools``; they read only the versions of the
  submission under review and the node's bound inputs (``services/review_evidence.py``, pure part ``ai/evidence.py``);
- the AI workspace ask agent's lookups (:data:`ASSISTANT_TOOL_SPECS`, ``services/assistant_tools.py``): project, part,
  revision, file and knowledge reads inside the conversation's project. Not agent-profile tools, so not listed.

AI native (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §2.2): the main agent's tools
(:data:`ORCHESTRATOR_TOOL_SPECS`, ``services/orchestrator_tools.py``) — the ask look-ups, ``get_operation_status`` and the
write tools (parts and sub-agents). Not agent-profile tools either.
"""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import limits
from .harness.base import ToolSpec


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentArgs(_Args):
    title: str = Field(default="Document", max_length=280, description="Document title; the file is saved as <title>.md")
    content: str = Field(min_length=1, max_length=400_000, description="Markdown content of the document")
    note: str = Field(default="", max_length=2000, description="Short note stored with the file version")


class ExportArgs(_Args):
    asset_version_ids: list[uuid.UUID] | None = Field(default=None, max_length=200,
                                                      description="Versions to bundle; default: this node's candidates and bound inputs")
    include: list[Literal["step", "glb", "svg", "dxf", "dwg", "drawing", "plan", "quote", "document"]] | None = Field(
        default=None, description="File kinds to include; default all")


class Model3DArgs(_Args):
    text: str = Field(default="", max_length=limits.TOOL_TEXT_CHARS,
                      description="The model to create or the change to make (required unless a STEP file is only imported)")
    base_asset_version_id: uuid.UUID | None = Field(default=None, description="Part model version to modify; default the latest candidate or bound model")
    file_version_id: uuid.UUID | None = Field(default=None, description="STEP file version to import; default a bound or candidate STEP document")


class DrawingReconstructArgs(_Args):
    file_version_id: uuid.UUID | None = Field(default=None, description="DXF / DWG file version to reconstruct")
    asset_version_id: uuid.UUID | None = Field(default=None, description="Document version holding a DXF / DWG, or a drawing version")
    sheet: str = Field(default="", max_length=200, description="Frame id or drawing number when the file has several sheets")
    notes: str = Field(default="", max_length=limits.LONG_NOTE_CHARS, description="Placement, units and other hints for the reconstruction")


class DrawingArgs(_Args):
    model_asset_version_id: uuid.UUID | None = Field(default=None, description="Part model version to draw; default the latest candidate or bound model")
    views: list[str] | None = Field(default=None, max_length=8, description="Views, e.g. front, top, right, iso")
    scale: float | str | None = Field(default=None, description='"auto" or a model-to-paper factor (2 = 2:1, 0.5 = 1:2)')
    projection: Literal["third", "first"] | None = Field(default=None, description="View layout: third angle or first angle (ISO / GB)")
    hidden_lines: bool | None = None
    dimensions: bool | None = None
    section: str | None = Field(default=None, max_length=40, description="Section view option of the drawing package")


class ProcessPlanArgs(_Args):
    intake_asset_version_id: uuid.UUID | None = Field(default=None, description="Plan from this intake version instead of a model")
    model_asset_version_id: uuid.UUID | None = Field(default=None, description="Part model version; default the latest candidate or bound model")
    allowance_mm: float | None = Field(default=None, ge=0, le=50, description="Machining allowance in mm")


class QuoteArgs(_Args):
    plan_asset_version_id: uuid.UUID | None = Field(default=None, description="Process plan version; default the latest candidate or bound plan")
    quantities: list[int] | None = Field(default=None, min_length=1, max_length=8, description="Quantity tiers")
    margin_pct: float | None = Field(default=None, ge=0, le=200)
    tax_pct: float | None = Field(default=None, ge=0, le=50)
    validity_days: int | None = Field(default=None, ge=1, le=365)


class KnowledgeReferenceArgs(_Args):
    item_id: uuid.UUID = Field(description="Knowledge item listed in the state under knowledge_items")
    reason: str = Field(min_length=1, max_length=2000, description="Why this item is used")


class KnowledgeAdoptArgs(_Args):
    item_id: uuid.UUID = Field(description="Knowledge item listed in the state under knowledge_items")
    reason: str = Field(min_length=1, max_length=2000, description="Why this item is adopted")
    slot_key: str = Field(min_length=1, max_length=80, description="Input slot the item is adopted into as a baseline")


class EngineeringReviewArgs(_Args):
    asset_version_id: uuid.UUID = Field(description="Version to approve or reject")
    stage: Literal["design", "process", "quote"] | None = Field(default=None, description="Approval stage; default the version type's stage")
    decision: Literal["approved", "rejected"] = "approved"
    comment: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class EngineeringSpecArgs(_Args):
    text: str = Field(default="", max_length=100_000, description="Requirements text to extract a spec from")
    file_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20, description="Files to read (bound documents are added)")
    spec_version_id: uuid.UUID | None = Field(default=None, description="An existing spec version of this project, passed through")
    title: str = Field(default="", max_length=200)
    revise: bool | None = Field(default=None, description="With spec_version_id and new text / files: true makes a new version of "
                                                          "that spec (its sources + the new files, the text as a revision note); "
                                                          "false uses the exact version and leaves the new material out; omitted: "
                                                          "refused (spec_and_sources)")


class AssemblyFromSpecArgs(_Args):
    spec_version_id: uuid.UUID = Field(description="The spec_version_id engineering_spec returned")
    number: str = Field(default="", max_length=80, description="Assembly number")
    run_checks: bool = True


class AssemblyDrawingArgs(_Args):
    assembly_asset_version_id: uuid.UUID | None = Field(default=None, description="Assembly version to draw; default the latest candidate or bound assembly")
    config: dict[str, Any] | None = Field(default=None, description="Assembly drawing package options")
    subassemblies: bool = Field(default=True, description="Also one package per sub-assembly revision; the root package draws "
                                                          "every part's detail sheet (layered, as the built-in chain)")


class DeliveryPackageArgs(_Args):
    assembly_asset_version_id: uuid.UUID | None = Field(default=None, description="Assembly version; default the latest candidate or bound assembly")
    drawing_asset_version_id: uuid.UUID | None = Field(default=None, description="Assembly drawing version; default the latest candidate")
    require_drawings: bool = True
    subassemblies: bool = Field(default=True, description="Layered delivery: every sub-assembly's STEP and drawing package and every "
                                                          "part's detail sheet, each listed under levels (as the built-in chain)")


# --- P4: read-only evidence tools of a review (kind=read) ---------------------------------------------------------


class ListSubmissionArgs(_Args):
    pass


class ReadDocumentArgs(_Args):
    asset_version_id: uuid.UUID = Field(description="A document version of the submission or a bound input")
    page: int = Field(default=1, ge=1, le=1000, description="Page to read (about 6,000 characters each); the result gives the page count")


class ReadChecksArgs(_Args):
    asset_version_id: uuid.UUID = Field(description="A part model, drawing or assembly version of the submission or a bound input")


class CompareVersionsArgs(_Args):
    base_asset_version_id: uuid.UUID = Field(description="The reference version (e.g. a bound input or the earlier revision)")
    asset_version_id: uuid.UUID = Field(description="The version compared with it (same type)")


# --- P4: read-only lookups of the AI workspace ask agent (kind=read) -------------------------------------------


class ProjectOverviewArgs(_Args):
    query: str = Field(default="", max_length=200, description="Filter parts and files by number or name; empty lists the most recent")


class GetPartArgs(_Args):
    part_id: uuid.UUID = Field(description="A part of this project (from project_overview or the references)")


class GetRevisionArgs(_Args):
    revision_id: uuid.UUID = Field(description="A model revision of this project (from get_part or the references)")


class ReadFileArgs(_Args):
    file_version_id: uuid.UUID = Field(description="A file version of this project (from project_overview or the references)")
    page: int = Field(default=1, ge=1, le=1000, description="Page to read (about 6,000 characters each); the result gives the page count")


class SearchKnowledgeArgs(_Args):
    query: str = Field(min_length=1, max_length=200, description="Words to look for in the project's archived knowledge items")


# --- AI native: the main agent's tools (get_operation_status reads; the rest write) ------------------------------


class GetOperationStatusArgs(_Args):
    operation_id: uuid.UUID = Field(description="A sub-agent (operation) of this conversation, from a tool result or <work>")
    event_id: uuid.UUID | None = Field(default=None, description="Exact immutable completion event; absent reads current status")
    event_path: str = Field(default="", max_length=1024, description="JSON pointer from the completion projection; empty reads the full event")
    offset: int = Field(default=0, ge=0, description="Character offset into the selected frozen JSON text")
    chars: int = Field(default=1000, ge=1, le=4000, description="Maximum JSON text characters for this exact event fragment")


class CreatePartArgs(_Args):
    name: str = Field(min_length=1, max_length=200, description="Part name, e.g. 法兰盘 / flange")
    number: str = Field(default="", max_length=80, description="Part number; empty: the next free <project code>-<nnn>")
    material: str = Field(default="", max_length=40, description="Material, only when the person stated it")
    description: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class CreateModelArgs(_Args):
    part_id: uuid.UUID = Field(description="The part to model")
    description: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS, description="The person's own words describing the part")


class ModifyModelArgs(_Args):
    revision_id: uuid.UUID = Field(description="The exact model revision to change")
    change: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS, description="The change, in the person's own words")


class GenerateDrawingArgs(_Args):
    revision_id: uuid.UUID = Field(description="The exact model revision to draw (ready, its check not failed)")
    requirements: str = Field(default="", max_length=limits.TOOL_TEXT_CHARS)


class CreateProcessPlanArgs(_Args):
    revision_id: uuid.UUID = Field(description="The exact ready model revision to plan machining for")
    allowance_mm: float | None = Field(default=None, ge=0, le=50)


class CreateQuoteArgs(_Args):
    process_plan_id: uuid.UUID = Field(description="The exact saved process plan returned by create_process_plan")
    quantities: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=20)
    margin_pct: float = Field(default=15.0, ge=0, le=200)
    tax_pct: float = Field(default=13.0, ge=0, le=100)
    validity_days: int = Field(default=30, ge=1, le=365)

    @field_validator("quantities")
    @classmethod
    def valid_quantities(cls, values: list[int]) -> list[int]:
        if any(q < 1 or q > 1_000_000 for q in values):
            raise ValueError("each quantity must be between 1 and 1,000,000")
        return sorted(set(values))


class _SourceArgs(_Args):
    file_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    @field_validator("file_version_ids")
    @classmethod
    def unique_files(cls, value):
        return list(dict.fromkeys(value))


class PlanDesignArgs(_Args):
    description: str = Field(default='', max_length=2000,
                             description='Intent only. The server uses the exact original human goal and bound choices.')


class CreateAssemblyArgs(_SourceArgs):
    description: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS,
                             description="The complete human source verbatim; preserve quantities, axes, placements, rotations and unknowns.")
    spec_version_id: uuid.UUID | None = None
    reuse_earlier_builds: bool = Field(default=True, description="False only when the person explicitly requests fresh modeling or forbids "
                                     "reusing previously built models. A new assembly alone does not imply false. "
                                     "Retries still retain this request's completed parts.")
    source_operation_id: uuid.UUID | None = Field(default=None, description="For a human follow-up or retry only: the prior assembly_create/spec_extract operation id in this conversation whose exact human source and files should be continued. Omit for independent new work.")
    design_from_requirements: bool = Field(default=False, description="True only when the human explicitly authorizes designing the mechanism, parts and missing dimensions from functional requirements; never for faithful reconstruction or extraction.")


class ModifyAssemblyArgs(_Args):
    revision_id: uuid.UUID
    change: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS)


class CreateWorkflowArgs(_Args):
    part_id: uuid.UUID
    description: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS)


class EditWorkflowArgs(_Args):
    workflow_id: uuid.UUID
    expected_version: int = Field(ge=1)
    change: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS)
    focus_node_id: str | None = Field(default=None, min_length=1, max_length=80)


class ReadDrawingArgs(_Args):
    part_id: uuid.UUID
    file_version_id: uuid.UUID
    hints: str = Field(default="", max_length=limits.INTAKE_HINTS_CHARS)


class ReconstructDrawingArgs(_Args):
    part_id: uuid.UUID
    source_ref_key: str = Field(min_length=1, max_length=500)
    sheet: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=limits.CHAT_TEXT_CHARS)


class ExtractSpecArgs(_SourceArgs):
    text: str = Field(default="", max_length=limits.TOOL_TEXT_CHARS,
                      description="The complete human source verbatim, without summarizing engineering facts; exact files remain sources.")
    title: str = Field(default="", max_length=200)
    base_spec_version_id: uuid.UUID | None = None
    source_operation_id: uuid.UUID | None = Field(default=None, description="For a human follow-up or retry only: the prior assembly_create/spec_extract operation id in this conversation whose exact human source and files should be continued. Omit for independent new work.")


class CreateDrawingDocumentArgs(_Args):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=limits.DRAWING_TEXT_CHARS)


class ModifyDrawingDocumentArgs(_Args):
    version_id: uuid.UUID
    change: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS)


# No docstrings on the models below: a docstring becomes the schema's description and goes to the model.


class Deliverable(BaseModel):  # one delivered version: native ExecuteReply.deliverables and the deliver end tool
    model_config = ConfigDict(extra="forbid")
    asset_version_id: uuid.UUID = Field(description="A version id this attempt produced with a tool, a bound input or an existing candidate")
    slot_key: str = Field(description="The output slot this version fills")
    criterion_ids: list[uuid.UUID] = Field(default_factory=list, description="Acceptance criteria this version is evidence for")
    note: str = Field(default="", max_length=limits.DESCRIPTION_CHARS)


class DeliverArgs(_Args):  # tool_agent's deliver(deliverables, note="") output tool
    deliverables: list[Deliverable] = Field(description="One entry per output slot")
    note: str = Field(default="", description="Short submission note")


class ReportBlockedArgs(_Args):  # tool_agent's report_blocked(reason, missing=None) output tool
    reason: str = Field(description="Why the task cannot go on")
    missing: list[str] | None = Field(default=None, description="Facts or inputs that are missing (questions for a person)")


_PRODUCES = {"document", "export", "model_3d", "drawing_reconstruct", "drawing", "process_plan", "quote", "assembly_from_spec",
             "assembly_drawing", "delivery_package"}
_REVIEW_ONLY = {"engineering_review"}  # only review / general profiles may hold it (an executor never approves its own work)
_NEEDS_PART = {"model_3d", "drawing_reconstruct", "drawing", "process_plan", "quote"}
#: The native prompt's tool rule: tool → (text after its argument list, arguments the rule does not list).
_NATIVE: dict[str, tuple[str, tuple[str, ...]]] = {
    "engineering_spec": ("returns data.spec_version_id", ("revise",)),
    "assembly_from_spec": ("produces part_model versions and an assembly_model", ("run_checks",)),
    "assembly_drawing": ("produces an assembly_drawing", ("config", "subassemblies")),
    "delivery_package": ("produces a delivery_manifest (data.delivery.completeness / missing say what is missing)", ("subassemblies",)),
}

_TOOLS: list[tuple[str, type[BaseModel], str]] = [
    ("document", DocumentArgs, "Write a markdown document; saved as a new file version of this node (same title = new version)."),
    ("export", ExportArgs, "Bundle versions (STEP / GLB / drawing package / plan / quote / documents) into one zip file."),
    ("model_3d", Model3DArgs, "Create or modify the part's 3D model from text (requirements, server gate, independent STEP check), "
                              "or import a STEP file. Needs a part. May answer needs_clarification with questions."),
    ("drawing_reconstruct", DrawingReconstructArgs, "Reconstruct a single part from a DXF / DWG drawing (evidence index, sheet and units "
                                                    "questions, independent STEP check); assemblies are unsupported. Needs a part."),
    ("drawing", DrawingArgs, "Generate the 2D drawing package of a part model version. Needs a part."),
    ("process_plan", ProcessPlanArgs, "Create a machining process plan from a model or an intake. Needs a part."),
    ("quote", QuoteArgs, "Create a quote from a process plan. Needs a part."),
    ("knowledge_reference", KnowledgeReferenceArgs, "Use a knowledge item as a reference (recorded)."),
    ("knowledge_adopt", KnowledgeAdoptArgs, "Adopt a knowledge item as the baseline of an input slot (recorded; may need confirmation)."),
    ("engineering_review", EngineeringReviewArgs, "Record an engineering approval stage (review agents only)."),
    ("engineering_spec", EngineeringSpecArgs, "Extract an engineering spec from text / files (and bound documents), or pass an existing "
                                              "spec version through. Returns data.spec_version_id."),
    ("assembly_from_spec", AssemblyFromSpecArgs, "Build every part definition of a spec version and the assembly with its checks. "
                                                 "Reports open questions when a definition is missing."),
    ("assembly_drawing", AssemblyDrawingArgs, "Generate the assembly drawing package of an assembly version."),
    ("delivery_package", DeliveryPackageArgs, "Make the delivery manifest (data.delivery.completeness / missing say what is missing)."),
]

_END_TOOLS: list[tuple[str, type[BaseModel], str]] = [
    ("deliver", DeliverArgs, "Deliver the outputs: one entry per output slot, each a version this attempt produced, a candidate or a "
                             "bound input."),
    ("report_blocked", ReportBlockedArgs, "Report that the task cannot go on: why, and the facts or inputs that are missing "
                                          "(questions for a person)."),
]


_EVIDENCE_TOOLS: list[tuple[str, type[BaseModel], str]] = [
    ("list_submission", ListSubmissionArgs, "List what is under review: the submitted versions (type, label, the criteria each is "
                                            "evidence for, note), the bound inputs, the criteria with their frozen automatic "
                                            "checks, and which tool reads each version."),
    ("read_document", ReadDocumentArgs, "Read the text of a submitted or bound document version, one page at a time; returns the "
                                        "page, the page count and the section headings on the page."),
    ("read_checks", ReadChecksArgs, "Read the check and independent measurement report of a submitted or bound part model, "
                                    "drawing or assembly version: each checked item with expected / measured values, the "
                                    "bounding box, volume and holes."),
    ("compare_versions", CompareVersionsArgs, "Compare two versions of the same type from the submission or its bound inputs: key "
                                              "dimensions, holes, check results and (documents) changed text."),
]

_ASSISTANT_TOOLS: list[tuple[str, type[BaseModel], str]] = [
    ("project_overview", ProjectOverviewArgs, "The project and its parts (number, name, material, latest revision) and files, "
                                              "optionally filtered by a query."),
    ("get_part", GetPartArgs, "Facts of one part: material, status, description and its model revisions."),
    ("get_revision", GetRevisionArgs, "Facts of one model revision: ops, holes, bounding box, volume and the check status."),
    ("read_file", ReadFileArgs, "Read a text / Markdown file version of the project, one page at a time."),
    ("search_knowledge", SearchKnowledgeArgs, "Search the project's archived, reusable knowledge items; returns titles and excerpts."),
]


_ORCHESTRATOR_TOOLS: list[tuple[str, type[BaseModel], str]] = [
    ("plan_design", PlanDesignArgs, "Plan a NEW concept using the exact human goal. Choose missing dimensions/materials, save visible design decisions and a real assembly graph. Default proceeds; explicit customization gets durable dynamic choices. Never for source reconstruction or existing-object edits."),
    ("get_operation_status", GetOperationStatusArgs, "Status of a sub-agent of this conversation: status, error, question, and its "
                                                     "result in brief (revision id, label and check status; drawing ids). "
                                                     "With event_id, read exact immutable completion JSON at event_path, paginated by offset/chars; "
                                                     "this reads the frozen generation even after the child reopens."),
    ("create_part", CreatePartArgs, "Create a part in the project (number allocated when empty). Returns part_id."),
    ("create_model", CreateModelArgs, "Start the 3D modeling sub-agent on the part (a model_create operation: requirements, server "
                                      "gate, independent check). Returns its operation_id at once; you are woken when it finishes."),
    ("modify_model", ModifyModelArgs, "Start the 3D modeling sub-agent to change an exact model revision (model_modify). Returns its "
                                      "operation_id at once; you are woken when it finishes."),
    ("generate_drawing", GenerateDrawingArgs, "Draw an exact checked model revision. Optional requirements plan supported drawing settings; empty uses deterministic defaults."),
    ("create_process_plan", CreateProcessPlanArgs, "Create and save a machining process plan from an exact ready model revision using measured geometry and shop rules. Returns process_plan_id and its exact asset version."),
    ("create_quote", CreateQuoteArgs, "Calculate and save an estimated quote from an exact process plan using shop cost rules; never invent prices. Missing quantities default to one prototype. Returns quote_id and its exact asset version."),
    ("create_assembly", CreateAssemblyArgs, "Create an assembly from the complete human source and exact source versions through the spec/parts/assembly/drawing chain. Preserve every quantity, axis and placement."),
    ("modify_assembly", ModifyAssemblyArgs, "Plan a change to an exact assembly revision. A person must apply the plan; selected instance scope is preserved."),
    ("create_workflow", CreateWorkflowArgs, "Plan a new workflow for a part. A person must apply the plan."),
    ("edit_workflow", EditWorkflowArgs, "Plan an edit to the exact numeric workflow version; existing runs stay unchanged. A person must apply."),
    ("read_drawing", ReadDrawingArgs, "Analyse an exact PDF or image drawing into estimated manufacturing information for a destination part; not a 3D reconstruction. Cannot read DWG or DXF. Incomplete readings require manual entry."),
    ("reconstruct_drawing", ReconstructDrawingArgs, "Reconstruct a single part from an exact frozen DXF / DWG reference into a checked model. Not for assemblies or a complete drawing set: use extract_spec first when scope is unknown. Preserve the source's part and selected sheet; standalone drawing documents are unsupported."),
    ("extract_spec", ExtractSpecArgs, "Read complete original DWG / DXF, PDF / image, or text sources into a persisted engineering specification, including sheets, parts, BOM and assembly relations. Use first for a full drawing set or when scope is unknown; choose part or assembly tools from the extracted evidence. Preserve the complete human source and exact file versions; never replace engineering facts with a summary. Also supports an explicit human revision of a base spec."),
    ("create_drawing_document", CreateDrawingDocumentArgs, "Create a standalone 2D drawing document from text, with actual exports and checks."),
    ("modify_drawing_document", ModifyDrawingDocumentArgs, "Plan supported 2D commands against an exact drawing version and selected entities. A person must apply; no automatic confirmation."),
]
_ORCHESTRATOR_READ = {"get_operation_status"}


def _spec(name: str, model: type[BaseModel], desc: str) -> ToolSpec:
    note, omit = _NATIVE.get(name, ("", ()))
    return ToolSpec(name=name, description=desc, args_model=model, kind="review_only" if name in _REVIEW_ONLY else "write",
                    needs_part=name in _NEEDS_PART, produces_assets=name in _PRODUCES, native_note=note, native_omit=omit)


#: The server tools ``run_tool`` executes, in their stable order.
TOOL_SPECS: dict[str, ToolSpec] = {name: _spec(name, model, desc) for name, model, desc in _TOOLS}
#: The end tools every executor finishes with: always offered, never part of a profile's ``allowed_tools``.
END_TOOL_SPECS: dict[str, ToolSpec] = {name: ToolSpec(name=name, description=desc, args_model=model, kind="end")
                                       for name, model, desc in _END_TOOLS}
#: P4: the read-only evidence tools of review / pre-review attempts on tool_agent (always offered there, scoped to the
#: submission under review; never in ``allowed_tools``).
EVIDENCE_TOOL_SPECS: dict[str, ToolSpec] = {name: ToolSpec(name=name, description=desc, args_model=model, kind="read")
                                            for name, model, desc in _EVIDENCE_TOOLS}
#: P4: the AI workspace ask agent's read-only lookups (not agent-profile tools: not in ``REGISTRY``).
ASSISTANT_TOOL_SPECS: dict[str, ToolSpec] = {name: ToolSpec(name=name, description=desc, args_model=model, kind="read")
                                             for name, model, desc in _ASSISTANT_TOOLS}
#: AI native: the main agent's tools — the ask look-ups and get_operation_status (read), then the write tools. Not
#: agent-profile tools (not in ``REGISTRY``).
ORCHESTRATOR_TOOL_SPECS: dict[str, ToolSpec] = {
    **ASSISTANT_TOOL_SPECS,
    **{name: ToolSpec(name=name, description=desc, args_model=model, kind="read" if name in _ORCHESTRATOR_READ else "write")
       for name, model, desc in _ORCHESTRATOR_TOOLS},
}
#: Every agent-profile tool (``GET /agent-tools``): server tools, evidence tools, end tools last.
REGISTRY: dict[str, ToolSpec] = {**TOOL_SPECS, **EVIDENCE_TOOL_SPECS, **END_TOOL_SPECS}

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_SPECS)
END_TOOLS: tuple[str, ...] = tuple(END_TOOL_SPECS)
EVIDENCE_TOOLS: tuple[str, ...] = tuple(EVIDENCE_TOOL_SPECS)
ASSISTANT_TOOLS: tuple[str, ...] = tuple(ASSISTANT_TOOL_SPECS)
#: Only review / general profiles may hold these.
REVIEW_ONLY_TOOLS: frozenset[str] = frozenset(n for n, s in TOOL_SPECS.items() if s.kind == "review_only")
#: Tools that need the node's task to be attached to a part.
PART_TOOLS: frozenset[str] = frozenset(n for n, s in TOOL_SPECS.items() if s.needs_part)


def specs_for(names: frozenset[str] | set[str]) -> list[ToolSpec]:
    """The specs of the allowed server tools, in registry order."""
    return [spec for name, spec in TOOL_SPECS.items() if name in names]


def check_args(spec: ToolSpec, raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """(the arguments as the tool reads them, problems). The arguments are the model's JSON dump of what was given
    (unset fields left out, so each tool keeps its own defaults); each problem names its field."""
    if not isinstance(raw, dict):
        return None, ["args: the arguments must be a JSON object"]
    try:
        parsed = spec.args_model.model_validate(raw)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e.get('loc', ())) or 'args'}: {e.get('msg', 'invalid')}" for e in exc.errors()[:20]]
    return parsed.model_dump(mode="json", exclude_unset=True), []


def native_tool_text(specs: Iterable[ToolSpec] | None = None) -> str:
    """The native prompt's tool rule: every tool with a native note and its argument list (``?`` = optional), wrapped
    as the prompt always was. ``collaboration_agent.EXECUTE_SYSTEM_PROMPT`` embeds it (a golden test keeps the prompt
    byte-identical)."""
    items = []
    for s in TOOL_SPECS.values() if specs is None else specs:
        if not s.native_note:
            continue
        args = ", ".join(n + ("" if f.is_required() else "?") for n, f in s.args_model.model_fields.items() if n not in s.native_omit)
        items.append(f"{s.name} {{{args}}} {s.native_note}")
    text = ("- Engineering chain tools (project level): " + "; ".join(items)
            + ". When assembly_from_spec reports open questions, report blocked with them.")
    return textwrap.fill(text, width=124, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False)
