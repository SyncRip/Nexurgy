"""Workflow edit agent of the AI workspace (``workflow_edit`` operations).

The context layer (``services/assistant_context.py``) builds the prompt: ``system`` is
``prompts.WORKFLOW_EDIT_SYSTEM_PROMPT``; the user turn holds <data> (memory, other
references), <node_catalog>, the workflow as <target kind="workflow">, the pinned
<constraints> and the <request> (:func:`user_message`); the AI work panel adds the
selected <focus_node>, a <run_context> or the base of a new workflow (:func:`with_blocks`).
The model returns a change
plan (:class:`WorkflowEditReply`); the server applies it to a copy of the base graph
(``workflow.edit.apply_changes``) and sends problems back within a repair budget.
Nothing here writes the workflow: the plan is applied only when the user confirms it
(``services/assistant.apply_workflow_edit``). No database access (``app/ai`` never
imports ``app.models``).

Structured output: a non-strict json_schema (``openai_compatible`` Chat Completions);
strict mode cannot express the change union with free-form ``params`` and the OpenAI
adapter falls back on its own (.trellis/tasks/archive/2026-10/10-08-cad-ai/design.md decision 1).

Provider failures do not raise: they come back as ``WorkflowEditOutcome.error`` with
every attempted call in ``calls``. The simulated responder (``PARTROUTE_AI_SIMULATION``)
answers this schema whatever the purpose: keyword rules only, labelled simulated.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..workflow.edit import MAX_CHANGES, Change, apply_changes, ordered_ids, parse_graph_lines, public_params, render_graph_lines
from ..workflow.graph import validate_graph
from ..workflow.nodes import NODE_DEFS
from .assistant_agent import ref_block
from .prompts import WORKFLOW_EDIT_PROMPT_VERSION, extract_request_text, guard_tags, workflow_edit_repair_message
from .providers.base import Message, ModelProvider, ProviderError, clip
from .providers.fake import FakeRequest
from .simulated import register_schema_responder
from .trace import Tracer

log = logging.getLogger("partroute.ai")

SCHEMA_NAME = "partroute_workflow_edit"
MAX_SUMMARY_CHARS = 4000
SIM_NOTE_ZH = "模拟结果，未调用模型。"
SIM_NOTE_EN = "Simulated plan, no model was called."


class WorkflowEditReply(BaseModel):
    """Structured output requested from the model (its JSON Schema is sent, non-strict)."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="What the plan changes and why, or the question when nothing can be changed; "
                                     "in the language of the request", max_length=MAX_SUMMARY_CHARS)
    changes: list[Change] = Field(description="Changes applied in order; [] when nothing should change", max_length=MAX_CHANGES)


def reply_json_schema() -> dict[str, Any]:
    return WorkflowEditReply.model_json_schema()


@dataclass
class WorkflowEditOutcome:
    summary: str
    changes: list[dict]
    graph_after: dict | None
    diff: dict | None
    warnings: list[dict]
    simulated: bool
    calls: list = field(default_factory=list)
    error: dict | None = None
    repairs: int = 0


def user_message(
    request_text: str,
    *,
    catalog: str,
    workflow: str | None,
    references: list[str] | None = None,
    constraints: list[str] | None = None,
    memory: list[str] | None = None,
) -> str:
    """User turn: <data> (memory → references) → <node_catalog> → the workflow (a <target> block made by
    ``assistant_agent.ref_block``) → <constraints> → <request>. Every body is guarded."""
    data: list[str] = []
    if memory:
        data.append("<memory>\n" + guard_tags("\n".join(memory)) + "\n</memory>")
    if references:
        data.append("<references>\n" + "\n".join(references) + "\n</references>")
    parts: list[str] = []
    if data:
        parts.append("<data>\n" + "\n".join(data) + "\n</data>")
    parts.append("<node_catalog>\n" + guard_tags(catalog.strip()) + "\n</node_catalog>")
    if workflow:
        parts.append(workflow)
    if constraints:
        parts.append("<constraints>\n" + guard_tags("\n".join(f"- {c}" for c in constraints)) + "\n</constraints>")
    parts.append("<request>\n" + guard_tags(request_text.strip()) + "\n</request>")
    return "\n".join(parts)


# --- AI work panel blocks (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §2.2, §2.5) ---------------------------


def _attr(value: Any) -> str:
    out = str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return out.replace("\r", " ").replace("\n", " ")


def _params_json(node_type: str, params: dict | None) -> str:
    shown = public_params(node_type, params)
    return json.dumps(shown, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str) if shown else "{}"


def focus_node_block(node: dict) -> str:
    """<focus_node>: the node the request is about (a workflow / run node reference), as it is in the graph being changed."""
    label = f' label="{_attr(node.get("label"))}"' if node.get("label") else ""
    body = ('"This node", "it", "这个", "该节点" in the request mean this node.\n'
            f"parameters: {_params_json(str(node.get('type')), node.get('params'))}")
    return f'<focus_node id="{_attr(node.get("id"))}" type="{_attr(node.get("type"))}"{label}>\n{guard_tags(body)}\n</focus_node>'


def run_context_block(title: str, run_version: int | None, base_version: int) -> str:
    """<run_context>: the request was made on a run; the plan changes the run's workflow for the next run only."""
    used = f"v{run_version}" if run_version is not None else "an unknown version"
    text = (f'The request was made on the run "{title}", which ran {used} of this workflow (current version v{base_version}). '
            "The run keeps its graph and results; the plan changes the workflow for the next run.")
    return "<run_context>\n" + guard_tags(text) + "\n</run_context>"


def template_target_block(template_key: str, title: str, head: str, graph: dict) -> str:
    """The base graph of a new part workflow (workflow_create) as the <target kind="workflow"> block, keyed
    ``template:<key>`` (it is not saved yet): the same rendering as a saved workflow's facts."""
    lines = [head, *render_graph_lines(graph)]
    _, errors = validate_graph(graph)
    if errors:
        lines.append(f"open validation problems ({len(errors)}):")
        lines.extend("* " + ", ".join(f"{k} {e[k]}" for k in ("code", "node_id", "edge_id", "port", "param") if e.get(k))
                     + f": {e.get('message')}" for e in errors[:20])
    return ref_block(f"template:{template_key}", "workflow", title, "\n".join(lines), tag="target")


def with_blocks(user: str, blocks: list[str]) -> str:
    """The user turn with extra blocks right after the workflow block (before <constraints>, else before <request>).
    Every other block body is guarded, so the first wrapper tag found at a line start is the real one."""
    if not blocks:
        return user
    extra = "\n".join(blocks)
    for tag in ("\n<constraints>\n", "\n<request>\n"):
        i = user.find(tag)
        if i != -1:
            return user[:i] + "\n" + extra + user[i:]
    return user + "\n" + extra


def _schema_problems(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors()[:12]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "reply"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


def run_workflow_edit(provider: ModelProvider, system: str, user: str, base_graph: dict, *, timeout_s: int = 90,
                      max_repairs: int = 2) -> WorkflowEditOutcome:
    """One plan: a structured call, then up to ``max_repairs`` repair calls while the reply does not match
    the schema or does not apply cleanly to ``base_graph``. Never raises for provider or model problems."""
    tracer = Tracer()
    schema = reply_json_schema()
    messages = [Message(role="user", content=user)]

    def fail(code: str, message: str, repairs: int, **details: Any) -> WorkflowEditOutcome:
        return WorkflowEditOutcome(summary="", changes=[], graph_after=None, diff=None, warnings=[], simulated=provider.simulated,
                                   calls=list(tracer.calls), error={"code": code, "message": message, "details": details},
                                   repairs=repairs)

    if not provider.capabilities.text:
        return fail("unsupported_capability", f"model {provider.model_id} is not configured for text generation", 0)
    repairs = 0
    while True:
        try:
            res = tracer.model_call(
                provider, purpose="assistant", system=system, system_version=WORKFLOW_EDIT_PROMPT_VERSION,
                messages=messages, json_schema=schema, schema_name=SCHEMA_NAME, timeout_s=timeout_s,
            )
        except ProviderError as e:
            return fail(e.code, e.message, repairs, retryable=e.retryable)
        problems: list[str] = []
        code = "bad_response"
        reply: WorkflowEditReply | None = None
        if res.data is None:
            problems = [f"the reply was not a JSON object ({res.parse_error or 'no detail'})"]
        else:
            try:
                reply = WorkflowEditReply.model_validate(res.data)
            except ValidationError as e:
                problems = _schema_problems(e)
        if reply is not None:
            try:
                edit = apply_changes(base_graph, list(reply.changes))
            except Exception:  # noqa: BLE001 - a checker bug must not lose the calls already made
                log.exception("workflow edit plan check failed")
                return fail("internal_error", "the plan could not be checked", repairs)
            if not edit.problems:
                summary = reply.summary.strip() or ("No change." if not reply.changes else "")
                return WorkflowEditOutcome(
                    # Top-level nulls dropped; a null inside set_params.params (reset to default) is kept.
                    summary=summary, changes=[{k: v for k, v in c.model_dump(mode="json").items() if v is not None}
                                              for c in reply.changes],
                    graph_after=edit.graph, diff=edit.diff, warnings=edit.warnings, simulated=provider.simulated,
                    calls=list(tracer.calls), repairs=repairs,
                )
            problems, code = edit.problems, "invalid_plan"
        if repairs >= max_repairs:
            what = "did not match the plan format" if code == "bad_response" else "could not be applied to the workflow"
            return fail(code, f"the model's change plan {what} after {repairs} repair(s)", repairs,
                        problems=[clip(p, 300) for p in problems[:12]])
        repairs += 1
        messages = messages + [
            Message(role="assistant", content=clip(res.text, 30_000)),
            Message(role="user", content=workflow_edit_repair_message([clip(p, 300) for p in problems[:12]])),
        ]


# --- simulated responder (PARTROUTE_AI_SIMULATION) ------------------------------------------------

_CJK = re.compile(r"[㐀-鿿]")
_TARGET_RE = re.compile(r'<target key="[^"]*" kind="workflow" title="[^"]*">\n(.*?)\n</target>', re.DOTALL)
_REMOVE = re.compile(r"删除|删掉|去掉|移除|\b(remove|delete|drop)\b", re.IGNORECASE)
_REVIEW = re.compile(r"评审|审批|审核|\b(review|approval|approve)\b", re.IGNORECASE)
_STAGE = {"process_plan": "process", "quote": "quote"}


def _mentioned(text: str, nodes: list[dict]) -> dict | None:
    """The node whose id (as a word) or label appears in the request; the longest match wins."""
    best: tuple[int, dict] | None = None
    for n in nodes:
        for name in (n["id"], n.get("label") or ""):
            if not name:
                continue
            hit = (re.search(r"(?<![A-Za-z0-9_.:-])" + re.escape(name) + r"(?![A-Za-z0-9_.:-])", text, re.IGNORECASE)
                   if name.isascii() else name in text)
            if hit and (best is None or len(name) > best[0]):
                best = (len(name), n)
    return best[1] if best else None


def _free_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# AI work panel: "before <node>" placement, named review stages, the focus node, a model switch, new workflows.
_BEFORE = re.compile(r"在\s*([^\s，。,.;；!?！？]{1,12}?)\s*之?前|\bbefore\s+(?:the\s+)?([A-Za-z0-9_.:-]+)", re.IGNORECASE)
#: Words for node types (first match wins), including the Chinese phrase meaning "before the quote node".
_TYPE_WORDS = (("报价", ("quote",)), ("quote", ("quote",)), ("工艺", ("process_plan",)), ("process", ("process_plan",)),
               ("出图", ("drawing",)), ("图纸", ("drawing",)), ("drawing", ("drawing",)), ("建模", ("model_3d", "drawing_reconstruct")),
               ("model", ("model_3d", "drawing_reconstruct")), ("导出", ("export",)), ("export", ("export",)))
_STAGE_WORDS = re.compile(r"(设计|工艺|报价)\s*(?:审核|评审|审批)|\b(design|process|quote)\s+(?:review|approval)", re.IGNORECASE)
_STAGE_OF = {"设计": "design", "工艺": "process", "报价": "quote", "design": "design", "process": "process", "quote": "quote"}
_REVIEW_LABEL = {"design": ("设计审核", "Design review"), "process": ("工艺审核", "Process review"), "quote": ("报价审核", "Quote review")}
_MODEL_SWITCH = re.compile(r"(?:换|改用|改成|切换|使用|用)[^，。,.;；!?！？]{0,10}模型|\b(?:another|other|different)\s+model\b|"
                           r"\bswitch\b[^.;!?]{0,20}\bmodel\b", re.IGNORECASE)
_FOCUS_RE = re.compile(r'<focus_node id="([^"]*)" type="([^"]*)"')
_PARAMS_LINE = re.compile(r'^- (\S+): [a-z0-9_]+(?: "[^"]*")?(?: (\{.*\}))?$')
_CONFIG_LINE = re.compile(r"^- ([0-9a-fA-F-]{36}): ")


def _sink(nodes: list[dict], edges: list[dict]) -> dict | None:
    has_out = {e["source"] for e in edges}
    order = ordered_ids(nodes, edges)
    sinks = [i for i in order if i not in has_out]
    return next((n for n in nodes if n["id"] == (sinks[-1] if sinks else order[-1])), None) if order else None


def _before_node(text: str, nodes: list[dict], edges: list[dict]) -> dict | None:
    """Resolve "before X" (or its Chinese equivalent) by node id / label, else by a type word (first such node in graph order)."""
    m = _BEFORE.search(text)
    if m is None:
        return None
    word = m.group(1) or m.group(2) or ""
    hit = _mentioned(word, nodes)
    if hit is not None:
        return hit
    order = {i: k for k, i in enumerate(ordered_ids(nodes, edges))}
    for w, types in _TYPE_WORDS:
        if w in word.lower():
            found = sorted((n for n in nodes if n["type"] in types), key=lambda n: order.get(n["id"], 0))
            if found:
                return found[0]
    return None


def _plan_review(text: str, nodes: list[dict], edges: list[dict], zh: bool, focus: dict | None = None) -> tuple[str, list[dict]]:
    named = _STAGE_WORDS.search(text)
    stage_named = _STAGE_OF[(named.group(1) or named.group(2)).lower()] if named else None
    before = _before_node(text, nodes, edges)
    if before is not None:
        # Between the node and what feeds it: the review gates only that edge.
        incoming = [e for e in edges if e["target"] == before["id"]]
        src = next((n for n in nodes if incoming and n["id"] == incoming[0]["source"]), None)
        if src is None:
            return ((f"「{before['id']}」前面没有上游节点，无法插入评审。" if zh else f"Nothing feeds {before['id']}; no review was added."), [])
        if src["type"] == "human_review":
            return ((f"「{before['id']}」前面已经有评审节点「{src['id']}」了。" if zh else
                     f"{before['id']} already follows the review {src['id']}."), [])
        anchor, port, downstream = src, incoming[0]["source_port"], [incoming[0]]
    else:
        anchor = _mentioned(text, nodes) or focus or _sink(nodes, edges)
        if anchor is None:
            return ("工作流里还没有节点，无法确定评审放在哪里。" if zh else "The workflow has no node to put a review after."), []
        d = NODE_DEFS.get(anchor["type"])
        if anchor["type"] == "human_review" or d is None or not d.outputs:
            return ((f"「{anchor['id']}」后面不需要再加评审。" if zh else f"No review was added after {anchor['id']}."), [])
        port = d.outputs[0].name
        downstream = [e for e in edges if e["source"] == anchor["id"] and e["source_port"] == port]
        if any(n["type"] == "human_review" for n in nodes if n["id"] in {e["target"] for e in downstream}):
            return ((f"「{anchor['id']}」后面已经有评审节点了。" if zh else f"{anchor['id']} is already followed by a review."), [])
    rid = _free_id("review", {n["id"] for n in nodes})
    stage = stage_named or _STAGE.get(anchor["type"], "design")
    label = _REVIEW_LABEL[stage][0 if zh else 1] if stage_named else ("评审" if zh else "Review")
    changes: list[dict] = [
        {"op": "add_node", "node_id": rid, "type": "human_review", "label": label, "params": {"stage": stage}, "after": anchor["id"]},
        {"op": "add_edge", "source": anchor["id"], "source_port": port, "target": rid, "target_port": "subject"},
    ]
    for e in downstream:  # the review gates what used to follow the anchor
        changes.append({"op": "remove_edge", "edge_id": e["id"]})
        changes.append({"op": "add_edge", "source": rid, "source_port": "subject", "target": e["target"], "target_port": e["target_port"]})
    gated = "、".join(e["target"] for e in downstream) if zh else ", ".join(e["target"] for e in downstream)
    if zh:
        summary = f"在「{anchor['id']}」之后加一个人工评审（{stage}）" + (f"，{gated} 改为在评审通过后运行。" if downstream else "。")
    else:
        summary = f"Adds a human review ({stage}) after {anchor['id']}" + (f"; {gated} now runs after the approval." if downstream else ".")
    return summary, changes


def _model_configs(first: str) -> list[str]:
    """The model_config_id values the node catalog lists (in its order)."""
    start, end = first.find("<node_catalog>"), first.find("</node_catalog>")
    lines = first[start:end].splitlines() if start != -1 and end != -1 else []
    head = next((i for i, ln in enumerate(lines) if ln.startswith("model_config_id values")), None)
    return [m.group(1) for ln in (lines[head + 1:] if head is not None else []) if (m := _CONFIG_LINE.match(ln))]


def _node_params(target: str, node_id: str) -> dict:
    for ln in target.splitlines():
        m = _PARAMS_LINE.match(ln.strip())
        if m and m.group(1) == node_id and m.group(2):
            try:
                return json.loads(m.group(2))
            except ValueError:
                return {}
    return {}


def _plan_model_switch(node: dict | None, current: dict, configs: list[str], zh: bool) -> tuple[str, list[dict]]:
    """set_params model_config_id of the node: the first other configuration the catalog lists, else back to the default."""
    if node is None:
        return ("没有指明要换模型的节点（在画布上选中它，或写出节点 id）。" if zh else
                "Which node? Select it on the canvas or name its id."), []
    d = NODE_DEFS.get(node["type"])
    if d is None or "model_config_id" not in d.params.model_fields:
        return (f"「{node['id']}」不调用模型，没有可换的模型。" if zh else f"{node['id']} does not call a model."), []
    now = current.get("model_config_id")
    other = next((c for c in configs if c != now), None)
    if other is None and now is None:
        return ("没有其他可用的模型配置；先在模型设置里添加一个。" if zh else "No other model configuration is set up; add one first."), []
    change = {"op": "set_params", "node_id": node["id"], "params": {"model_config_id": other}}
    if other is None:
        return (f"「{node['id']}」改回默认模型配置。" if zh else f"{node['id']} goes back to the default model configuration."), [change]
    return (f"「{node['id']}」改用模型配置 {other}。" if zh else f"{node['id']} now uses the model configuration {other}."), [change]


def simulated_workflow_edit_responder(req: FakeRequest) -> dict[str, Any]:
    """Deterministic plan from keywords; always labelled simulated. A repair request gets an empty plan (the
    rules cannot do better than their first answer)."""
    first = req.messages[0].content if req.messages else ""
    text = extract_request_text(first)
    zh = bool(_CJK.search(text))

    def plan(summary: str, changes: list[dict]) -> dict[str, Any]:
        return {"summary": (SIM_NOTE_ZH + summary) if zh else f"{SIM_NOTE_EN} {summary}", "changes": changes}

    if len(req.messages) > 1:
        return plan("规则无法给出能应用的修改。" if zh else "The rules could not derive a change that applies.", [])
    m = _TARGET_RE.search(first)
    target = m.group(1) if m else ""
    nodes, edges = parse_graph_lines(target)
    fm = _FOCUS_RE.search(first)
    focus = next((n for n in nodes if fm and n["id"] == fm.group(1).replace("&quot;", '"').replace("&amp;", "&")), None)
    if _REMOVE.search(text):
        n = _mentioned(text, nodes) or focus
        if n is not None:
            return plan(f"删除节点「{n['id']}」及其连线。" if zh else f"Removes node {n['id']} and its edges.",
                        [{"op": "remove_node", "node_id": n["id"]}])
    if _MODEL_SWITCH.search(text):
        n = _mentioned(text, nodes) or focus
        return plan(*_plan_model_switch(n, _node_params(target, n["id"]) if n else {}, _model_configs(first), zh))
    if _REVIEW.search(text):
        return plan(*_plan_review(text, nodes, edges, zh, focus))
    if m is not None and m.group(0).startswith('<target key="template:'):
        # workflow_create: the request only asks for the workflow; the template is used as it is.
        return plan("按模板建立流程，未作调整。" if zh else "Creates the workflow from the template as it is.", [])
    return plan("没有从请求中识别出可做的修改（模拟规则只认识“加评审”“删除 + 节点 id”和“改用另一个模型”）。" if zh else
                'No change could be derived from the request (the simulated rules know "add a review", "remove <node id>" and '
                '"use another model").', [])


register_schema_responder(SCHEMA_NAME, simulated_workflow_edit_responder)
