"""Collaboration planner: a demand → a structured business-task plan
(.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §3.8, purpose ``planning``).

``run_planner`` asks the model for a :class:`PlanReply` (summary, nodes with
criteria / slots / tools, dependency edges, missing information,
assumptions), validates it and repairs once. It never guesses engineering
facts: anything the demand does not state goes into ``missing_information``,
and the caller refuses to start while that list is not empty (A02).

The simulated responder (``PARTROUTE_AI_SIMULATION=true``) is deterministic:
a manufacturing demand (Chinese terms for manufacturing, machining, parts, drawings or quotes, or STEP) becomes the
four-node modeling → drawing → process planning → quoting chain with missing items for an absent
size (AxBxC), material or quantity; anything else becomes the three-node
document chain source review → writing → delivery preparation. Simulated results carry
``simulated=True`` everywhere and never count as a real plan.

No database access here: the service passes summaries in and gets a plan out.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .providers.base import Message, ModelProvider, ProviderError, clip
from .providers.fake import FakeRequest
from .simulated import register_responder
from .trace import AgentStep, AiCallRecord, Tracer
from .turbo import is_turbo_text

PLANNING_PROMPT_VERSION = "planning-2026-10-08.1"
AssetTypeName = Literal["part_model", "drawing", "process_plan", "quote", "document", "intake", "process_module"]
ApprovalStage = Literal["design", "process", "quote"]

# Planning-time limits (design §3.3): the Graph hard limits stay 200 / 600.
MAX_PLAN_NODES = 50
MAX_PLAN_EDGES = 150


# --- protocol -------------------------------------------------------------------------


class PlanCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=4000)
    required: bool = True
    allowed_types: list[AssetTypeName] = Field(default_factory=list, max_length=6)
    require_approval_stage: ApprovalStage | None = None


class PlanSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_key: str = Field(min_length=1, max_length=80)
    asset_type: AssetTypeName


class PlanInputSlot(BaseModel):
    """An input the node reads: an upstream node's output slot, or (source_node_id
    null) a fixed input of the draft with the same slot_key."""

    model_config = ConfigDict(extra="forbid")
    slot_key: str = Field(min_length=1, max_length=80)
    asset_type: AssetTypeName
    source_node_id: str | None = Field(default=None, max_length=60)
    source_slot_key: str | None = Field(default=None, max_length=80)


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=10000)
    tools: list[str] = Field(default_factory=list, max_length=12)
    criteria: list[PlanCriterion] = Field(default_factory=list, max_length=50)
    output_slots: list[PlanSlot] = Field(default_factory=list, max_length=20)
    input_slots: list[PlanInputSlot] = Field(default_factory=list, max_length=20)


class PlanEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=60)
    target: str = Field(min_length=1, max_length=60)


class MissingInformation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    node_id: str | None = Field(default=None, max_length=60)


class PlanReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=4000)
    nodes: list[PlanNode] = Field(default_factory=list, max_length=MAX_PLAN_NODES)
    edges: list[PlanEdge] = Field(default_factory=list, max_length=MAX_PLAN_EDGES)
    missing_information: list[MissingInformation] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=50)


class AdoptedValue(BaseModel):
    """Turbo (W7): a value the planner chose for a fact the demand does not state (stored as an assumption)."""

    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=120)
    adopted: str | float | int | bool | None = None
    unit: str | None = Field(default=None, max_length=40)
    basis: str = Field(default="", max_length=2000)
    node_id: str | None = Field(default=None, max_length=60)


class TurboPlanReply(PlanReply):
    """The Turbo planning reply: the standard plan plus the values adopted instead of missing_information."""

    adopted_values: list[AdoptedValue] = Field(default_factory=list, max_length=50)


def plan_json_schema(turbo: bool = False) -> dict[str, Any]:
    """PlanReply schema (TurboPlanReply under Turbo) with every top-level field required so models fill them all."""
    schema = (TurboPlanReply if turbo else PlanReply).model_json_schema()
    schema["required"] = list(schema.get("properties", {}))
    return schema


# --- request / outcome ----------------------------------------------------------------


class FixedInputSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_key: str
    asset_type: str
    label: str = ""


class KnowledgeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    kind: str = ""
    slot_key: str | None = None
    asset_type: str = ""
    label: str = ""


class SourceContent(BaseModel):
    """Content the server really read for the planner (W2): an engineering spec digest or a document's text.
    ``read`` is content | partial | metadata; ``ranges`` say what part was read."""

    model_config = ConfigDict(extra="forbid")
    key: str
    kind: str
    label: str = ""
    read: str = "metadata"
    ranges: list[str] = Field(default_factory=list)
    text: str = ""


class PlanRequest(BaseModel):
    """Everything the planner may read (design §4 C01: demand, fixed-input
    summaries and visible knowledge summaries; never engineering facts it
    would have to guess). W2: ``sources`` carries real content (spec digest,
    document text) the server read, with its read state."""

    model_config = ConfigDict(extra="forbid")
    demand: str
    execution_mode: Literal["full_auto", "semi_auto"]
    fixed_inputs: list[FixedInputSummary] = Field(default_factory=list)
    knowledge: list[KnowledgeSummary] = Field(default_factory=list)
    sources: list[SourceContent] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    executor_tools: list[str] = Field(default_factory=list)
    has_part: bool = False
    # Frozen before planning (W7). Turbo: adopted_values instead of missing_information.
    execution_policy: Literal["standard", "turbo"] = "standard"


class PlanOutcome(BaseModel):
    """``status`` planned (``plan`` set) or failed (``errors`` say why).
    Provider failures raise ProviderError instead; the records are on ``err.partial``."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    status: Literal["planned", "failed"]
    plan: PlanReply | None = None
    errors: list[str] = Field(default_factory=list)
    repairs: int = 0
    simulated: bool = False
    provider: str = ""
    model_id: str = ""
    model_config_id: str | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    calls: list[AiCallRecord] = Field(default_factory=list)


# --- prompt ---------------------------------------------------------------------------

PLANNING_SYSTEM_PROMPT = """You are the planning agent of PartRoute, a mechanical engineering collaboration tool.
Turn one business demand into a directed acyclic graph of business tasks. Reply with JSON only, matching the schema.

Rules:
- Every node is one real task with a title, an objective, the tools it may use, at least one required acceptance
  criterion, at least one output slot and the input slots it reads from upstream nodes (source_node_id +
  source_slot_key must name an existing upstream node and one of its output slots with the same asset_type).
  An input slot with source_node_id null refers to a fixed input of the demand with the same slot_key.
- Edges only express dependency (source must finish and be accepted before target starts). Keep the graph small:
  at most {max_nodes} nodes and {max_edges} edges.
- Tools may only come from this list: {tools}. The executor of these tasks is allowed to use: {executor_tools}.
  Tools model_3d / drawing / process_plan / quote need a part attached to the demand (has_part = {has_part}).
  The engineering chain tools work on the project (no part needed): engineering_spec (description / documents → spec
  version), assembly_from_spec (spec version → part_model versions + one assembly_model), assembly_drawing
  (assembly_model → assembly_drawing), delivery_package (assembly_model [+ assembly_drawing] → delivery_manifest).
- Asset types: part_model, drawing, process_plan, quote, document, intake, process_module, assembly_model,
  assembly_drawing, delivery_manifest.
- Never invent engineering facts (dimensions, materials, quantities, tolerances, standards, customer capabilities).
  When the demand does not state a fact a task needs, add it to missing_information (field, question, node_id)
  and keep the plan; the person answers before anything starts.
- Record every assumption you made in assumptions. Keep summary to a few sentences.
""".strip()


def _user_message(req: PlanRequest) -> str:
    fixed = "\n".join(f"- slot_key: {f.slot_key}; asset_type: {f.asset_type}; label: {f.label}" for f in req.fixed_inputs) or "(none)"
    knowledge = "\n".join(
        f"- item_id: {k.item_id}; kind: {k.kind}; slot_key: {k.slot_key or '-'}; asset_type: {k.asset_type}; label: {k.label}"
        for k in req.knowledge
    ) or "(none)"
    out = (
        f"<execution_mode>{req.execution_mode}</execution_mode>\n"
        f"<demand>\n{req.demand}\n</demand>\n"
        f"<fixed_inputs>\n{fixed}\n</fixed_inputs>\n"
        f"<knowledge>\n{knowledge}\n</knowledge>"
    )
    if req.sources:
        blocks = "\n".join(f'<source key="{s.key}" kind="{s.kind}" read="{s.read}" ranges="{"; ".join(s.ranges)}" label="{s.label}">\n'
                           f"{s.text}\n</source>" for s in req.sources)
        out += f"\n<sources>\n{blocks}\n</sources>"
    if req.execution_policy == "turbo":  # standard messages stay byte-identical
        from .turbo import PLANNING_TURBO_BLOCK

        out += "\n" + PLANNING_TURBO_BLOCK
    return out


#: Added to the planning rules when the request carries read sources (W2).
SOURCES_RULE = ("- <sources> holds content the server actually read (an engineering spec digest with definitions, quantities, "
                "readiness and open questions, or document text). Plan from it; a source marked read=\"metadata\" was not read and "
                "read=\"partial\" only in the ranges given — never assume what was not read. Components a spec marks blocked wait for "
                "its open questions; the ready ones can be planned first.")


def _system_prompt(req: PlanRequest) -> str:
    prompt = PLANNING_SYSTEM_PROMPT.format(
        max_nodes=MAX_PLAN_NODES, max_edges=MAX_PLAN_EDGES, tools=", ".join(req.available_tools) or "(none)",
        executor_tools=", ".join(req.executor_tools) or "(none)", has_part=str(req.has_part).lower(),
    )
    return prompt + ("\n" + SOURCES_RULE if req.sources else "")


def repair_message(problems: list[str]) -> str:
    return "Your previous reply did not match the schema or the rules:\n" + "\n".join(f"- {p}" for p in problems[:10]) + \
        "\nReply again with the complete corrected JSON object only."


# --- validation -----------------------------------------------------------------------


def validate_plan(data: dict[str, Any] | None, parse_error: str | None,
                  model: type[PlanReply] = PlanReply) -> tuple[PlanReply | None, list[str]]:
    """Structural checks the schema cannot express (unique ids, known edge ends,
    acyclic, slot references). Returns (plan, problems)."""
    if data is None:
        return None, [f"the reply was not a JSON object: {parse_error or 'empty'}"]
    try:
        plan = model.model_validate(data)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:20]
    problems: list[str] = []
    ids: list[str] = [n.id for n in plan.nodes]
    if not ids:
        problems.append("nodes: the plan needs at least one node")
    if len(set(ids)) != len(ids):
        problems.append("nodes: node ids must be unique")
    known = set(ids)
    outputs = {n.id: {s.slot_key: s.asset_type for s in n.output_slots} for n in plan.nodes}
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in plan.edges:
        if e.source not in known or e.target not in known:
            problems.append(f"edges: {e.source} -> {e.target} names an unknown node")
            continue
        if e.source == e.target:
            problems.append(f"edges: {e.source} cannot depend on itself")
            continue
        preds[e.target].add(e.source)
    for n in plan.nodes:
        if not any(c.required for c in n.criteria):
            problems.append(f"nodes.{n.id}.criteria: at least one required criterion")
        if not n.output_slots:
            problems.append(f"nodes.{n.id}.output_slots: at least one output slot")
        seen: set[str] = set()
        for s in n.output_slots:
            if s.slot_key in seen:
                problems.append(f"nodes.{n.id}.output_slots: slot {s.slot_key} repeated")
            seen.add(s.slot_key)
        for s in n.input_slots:
            if s.source_node_id is None:
                continue
            if s.source_node_id not in known:
                problems.append(f"nodes.{n.id}.input_slots.{s.slot_key}: unknown source node {s.source_node_id}")
            elif s.source_slot_key not in outputs[s.source_node_id]:
                problems.append(f"nodes.{n.id}.input_slots.{s.slot_key}: {s.source_node_id} has no output slot {s.source_slot_key}")
            elif outputs[s.source_node_id][s.source_slot_key] != s.asset_type:
                problems.append(f"nodes.{n.id}.input_slots.{s.slot_key}: asset_type differs from the source slot")
            else:
                preds[n.id].add(s.source_node_id)  # an input reference is a dependency too
    # Cycle check over edges plus input references.
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return False
        if state.get(node) == 2:
            return True
        state[node] = 1
        ok = all(visit(p) for p in preds.get(node, ()))
        state[node] = 2
        return ok

    if not all(visit(i) for i in ids):
        problems.append("edges: the plan contains a cycle")
    return (plan if not problems else None), problems


# --- runner ---------------------------------------------------------------------------


def run_planner(
    provider: ModelProvider,
    req: PlanRequest,
    *,
    max_repairs: int = 1,
    temperature: float | None = None,
    timeout_s: float = 120,
) -> PlanOutcome:
    """Plan once, repair once on a structural problem. Provider errors propagate
    with ``err.partial`` holding the records so far."""
    tracer = Tracer()
    system = _system_prompt(req)
    turbo = req.execution_policy == "turbo"
    schema = plan_json_schema(turbo)
    reply_model: type[PlanReply] = TurboPlanReply if turbo else PlanReply
    messages = [Message(role="user", content=_user_message(req))]
    repairs = 0

    def outcome(status: str, **kw: Any) -> PlanOutcome:
        kw.setdefault("simulated", provider.simulated)
        return PlanOutcome(status=status, repairs=repairs, provider=provider.provider_name, model_id=provider.model_id,
                           model_config_id=provider.config_id, steps=tracer.steps, calls=tracer.calls, **kw)

    with tracer.step("prompt") as st:
        st.detail = {"version": PLANNING_PROMPT_VERSION, "demand_chars": len(req.demand), "fixed_inputs": len(req.fixed_inputs),
                     "knowledge": len(req.knowledge), "sources": [{"key": s.key, "read": s.read, "chars": len(s.text)} for s in req.sources]}
    try:
        while True:
            res = tracer.model_call(
                provider, purpose="planning" if repairs == 0 else "planning_repair", system=system,
                system_version=PLANNING_PROMPT_VERSION, messages=messages, json_schema=schema, schema_name="partroute_collaboration_plan",
                temperature=temperature, timeout_s=timeout_s,
            )
            with tracer.step("validation") as st:
                plan, problems = validate_plan(res.data, res.parse_error, reply_model)
                st.ok = not problems
                st.detail = {"problems": problems[:10], "problem_count": len(problems)}
            if problems:
                if repairs < max_repairs:
                    repairs += 1
                    with tracer.step("repair") as st:
                        st.detail = {"reason": "validation", "problems": problems[:10]}
                        messages.append(Message(role="assistant", content=clip(res.text, 30_000)))
                        messages.append(Message(role="user", content=repair_message(problems)))
                    continue
                return outcome("failed", errors=problems)
            assert plan is not None
            return outcome("planned", plan=plan, simulated=bool(res.simulated) or provider.simulated)
    except ProviderError as e:
        e.partial = outcome("failed", errors=[f"{e.code}: {e.message}"])  # type: ignore[attr-defined]
        raise


# --- simulated responder (design §3.8) -------------------------------------------------

_MANUFACTURING = re.compile(r"制造|加工|零件|STEP|图纸|报价|machin|manufactur|\bpart\b|drawing|quot", re.IGNORECASE)
_NUM = r"\d+(?:\.\d+)?"
_DIMS = re.compile(_NUM + r"\s*(?:mm)?\s*[x×X*]\s*" + _NUM + r"\s*(?:mm)?\s*[x×X*]\s*" + _NUM)
_MATERIAL = re.compile(r"材料|材质|铝|钢|铜|黄铜|不锈钢|塑料|尼龙|钛|6061|7075|304|316|45#|Q235|alumin|steel|brass|copper|nylon|titanium|plastic|material",
                       re.IGNORECASE)
_QUANTITY = re.compile(r"数量|\d+\s*(?:件|个|pcs|pieces|套|units?)\b|quantity|qty", re.IGNORECASE)
_SIM_NOTE = "simulated plan: no AI model was called"
# Turbo (W7): what the simulated planner adopts for a missing fact (labelled simulated; never a confirmed fact).
_SIM_ADOPTED: dict[str, dict[str, Any]] = {
    "dimensions": {"adopted": "100x60x10", "unit": "mm", "basis": "simulated Turbo plan: size not stated; a small machined plate"},
    "material": {"adopted": "6061-T6 aluminium", "unit": None, "basis": "simulated Turbo plan: material not stated; a common machining alloy"},
    "quantity": {"adopted": 1, "unit": "pcs", "basis": "simulated Turbo plan: quantity not stated; one piece"},
}


def _demand_of(req: FakeRequest) -> str:
    text = req.last_user_text
    m = re.search(r"<demand>\n?(.*?)\n?</demand>", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _fixed_inputs_of(req: FakeRequest) -> list[dict[str, str]]:
    text = req.last_user_text
    m = re.search(r"<fixed_inputs>\n?(.*?)\n?</fixed_inputs>", text, re.DOTALL)
    out: list[dict[str, str]] = []
    for line in (m.group(1) if m else "").splitlines():
        fm = re.match(r"-\s*slot_key:\s*(\S+);\s*asset_type:\s*(\S+);", line.strip())
        if fm:
            out.append({"slot_key": fm.group(1), "asset_type": fm.group(2)})
    return out


def _crit(description: str, asset_type: str, stage: str | None = None) -> dict[str, Any]:
    return {"description": description, "required": True, "allowed_types": [asset_type], "require_approval_stage": stage}


def _node(nid: str, title: str, objective: str, tools: list[str], criteria: list[dict], outputs: list[tuple[str, str]],
          inputs: list[tuple[str, str, str | None, str | None]] = ()) -> dict[str, Any]:
    return {
        "id": nid, "title": title, "objective": objective, "tools": tools, "criteria": criteria,
        "output_slots": [{"slot_key": k, "asset_type": t} for k, t in outputs],
        "input_slots": [{"slot_key": k, "asset_type": t, "source_node_id": s, "source_slot_key": ss} for k, t, s, ss in inputs],
    }


def simulated_planning_responder(req: FakeRequest) -> dict[str, Any]:
    demand = _demand_of(req)
    fixed = _fixed_inputs_of(req)
    first_inputs = [(f["slot_key"], f["asset_type"], None, None) for f in fixed]
    if _MANUFACTURING.search(demand):
        missing: list[dict[str, Any]] = []
        if not _DIMS.search(demand):
            missing.append({"field": "dimensions", "question": "关键尺寸是多少（长×宽×高，mm，例如 120x80x10）？", "node_id": "n1"})
        if not _MATERIAL.search(demand):
            missing.append({"field": "material", "question": "零件材料是什么（例如 6061 铝合金、304 不锈钢）？", "node_id": "n1"})
        if not _QUANTITY.search(demand):
            missing.append({"field": "quantity", "question": "需要的数量是多少（件）？", "node_id": "n4"})
        nodes = [
            _node("n1", "建模", "按需求建立零件的三维模型（STEP）。", ["model_3d"],
                  [_crit("模型为真实修订版本并通过设计批准", "part_model", "design")], [("model", "part_model")], first_inputs),
            _node("n2", "图纸", "由模型生成正交视图图纸。", ["drawing"], [_crit("图纸来自当前模型修订", "drawing")],
                  [("drawing", "drawing")], [("model", "part_model", "n1", "model")]),
            _node("n3", "工艺", "由模型生成工艺方案。", ["process_plan"], [_crit("工艺方案通过工艺批准", "process_plan", "process")],
                  [("plan", "process_plan")], [("model", "part_model", "n1", "model")]),
            _node("n4", "报价", "按工艺方案生成报价。", ["quote"], [_crit("报价基于当前工艺方案并通过报价批准", "quote", "quote")],
                  [("quote", "quote")], [("plan", "process_plan", "n3", "plan")]),
        ]
        edges = [{"source": "n1", "target": "n2"}, {"source": "n1", "target": "n3"}, {"source": "n3", "target": "n4"}]
        out = {"summary": "制造链：建模 → 图纸 → 工艺 → 报价（模拟规划）。", "nodes": nodes, "edges": edges,
               "missing_information": missing, "assumptions": [_SIM_NOTE]}
        if is_turbo_text(req.last_user_text):  # W7: adopt instead of asking, labelled simulated
            out["missing_information"] = []
            out["adopted_values"] = [{**_SIM_ADOPTED[m["field"]], "field": m["field"], "node_id": m["node_id"]} for m in missing]
        return out
    nodes = [
        _node("n1", "资料核对", "核对需求与固定资料，整理要点与缺口清单。", ["document"], [_crit("核对记录为文档且覆盖全部固定资料", "document")],
              [("review_notes", "document")], first_inputs),
        _node("n2", "编写", "按核对要点编写交付文档初稿。", ["document"], [_crit("初稿为文档并回应需求目标", "document")],
              [("draft", "document")], [("review_notes", "document", "n1", "review_notes")]),
        _node("n3", "交付整理", "整理定稿与交付清单。", ["document"], [_crit("交付物为文档且与初稿一致", "document")],
              [("deliverable", "document")], [("draft", "document", "n2", "draft")]),
    ]
    edges = [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}]
    out = {"summary": "文档链：资料核对 → 编写 → 交付整理（模拟规划）。", "nodes": nodes, "edges": edges, "missing_information": [],
           "assumptions": [_SIM_NOTE]}
    if is_turbo_text(req.last_user_text):
        out["adopted_values"] = []
    return out


register_responder("planning", simulated_planning_responder)
