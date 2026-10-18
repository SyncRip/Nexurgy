"""AI modeling agent: request text (+ answers, current model) → requirements + ops → kernel build → check.

The model proposes a COMPLETE op list (app/cad/ops.py is the contract) and the
requirements the user stated (app/cad/requirements.py, with citations); the CAD
kernel, injected as ``build``, makes the geometry and the independent checker,
injected as ``check``, measures the exported STEP against the requirements
(.trellis/tasks/archive/2026-10/10-08-cad-ai/design.md §1–§2, §6). Flow::

    prompt → model call → validate (envelope, op ids, ModelSpec, requirements, grounding)
           → [repair call with validator problems]
           → gate: a critical requirement field missing / assumed → needs_clarification (nothing built)
           → build → [repair call with kernel errors]
           → check (exported STEP vs requirements) → [repair call with the failed items] → outcome

Validation includes, in modify mode, coverage: every op the reply adds or changes (except fillets, chamfers and
suppression / label only edits) must be covered by a geometric requirement of its kind, so a change is never saved
unchecked. A checker that cannot evaluate the solid (an exception, no exported STEP, an item it could not measure) is
a failed check, never a pass: one repair, then ``requirements_not_met`` with the error.

Private context (``data`` / ``constraints``, the requester's AI workspace blocks) never reaches the outcome: a reply
that repeats it is a validation problem, and steps, call records, errors and questions are scrubbed of it
(:func:`app.ai.prompts.scrub_private`).

``max_repairs`` bounds validation repairs, ``max_build_repairs`` bounds build and
check repairs together, so one run makes at most ``1 + max_repairs +
max_build_repairs`` model calls. Model mistakes never raise: they end as
``status="failed"`` with the errors (a check that still fails ends with error code
``requirements_not_met`` and the report). A deterministic kernel failure
(``retry.KERNEL_UNSUPPORTED_CODES``, e.g. ``unsupported_font``) is never repaired: it
ends ``failed`` at once with that code, not retryable. Provider failures raise
:class:`ProviderError` after recording; ``err.partial`` holds the
:class:`AgentOutcome` so far (steps + ai_calls records).

Reconstruct mode (drawing → STEP, ``drawing=DrawingInput``): the same loop with
the reconstruct prompt, the sheet image, a reply that may also be
``unsupported`` (+ reason: nothing is built), grounding against the drawing
evidence index and an extra server gate from the caller (drawing dimensions that
conflict with their view's scale → ``needs_clarification``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.cad.ops import OP_ID_RE, HoleOp, ModelSpec, Operation, PatternOp, hole_positions, next_op_id, remap_refs
from app.cad.requirements import (
    DESIGN_MODELING_SYSTEM,
    DefinitionSourceContext,
    DesignDefinitionContext,
    design_requirement_problems,
    GEOMETRIC_KINDS,
    REQ_ID_RE as REQ_ID,
    CriticalGap,
    RequirementList,
    Requirements,
    default_question,
    gate,
    ground,
    parse_requirements,
    requirements_sha256,
    safe_id,
)

from .. import policy as exec_policy
from . import prompts
from .timeouts import CAD_MODEL_CALL_S
from .providers.base import ImagePart, Message, ModelProvider, ProviderError, clip
from .providers.schema_dialects import drop_nulls, schema_stats
from .retry import KERNEL_UNSUPPORTED_CODES
from .trace import AgentStep, AiCallRecord, Tracer

MAX_PROBLEMS = 20
REQUIREMENTS_NOT_MET = "requirements_not_met"
UNSUPPORTED = "unsupported"
_CJK = re.compile(r"[\u3400-\u9fff]")
Mode = Literal["create", "modify", "reconstruct"]
Status = Literal["built", "needs_clarification", "failed", "unsupported"]


class OpError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op_id: str | None = None
    code: str
    message: str


class BuildFeedback(BaseModel):
    """What the agent needs from a kernel build (a subset of ``BuildResult``)."""

    model_config = ConfigDict(extra="ignore")
    ok: bool
    errors: list[OpError] = Field(default_factory=list)
    warnings: list[OpError] = Field(default_factory=list)
    metrics: dict[str, Any] | None = None


class CurrentModel(BaseModel):
    """The revision being modified."""

    model_config = ConfigDict(extra="forbid")
    spec: ModelSpec
    metrics: dict[str, Any] | None = None  # e.g. {"bbox": {...}, "volume_mm3": ...}
    holes: list[dict[str, Any]] = Field(default_factory=list)
    part_name: str | None = None
    material: str | None = None
    used_ids: list[str] = Field(default_factory=list)  # every id the part ever used


class TemplateParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="Parameter name from the catalog, e.g. size, length")
    value: float | str = Field(description='Number in mm / degrees, or a choice such as "M8"')


class TemplateUse(BaseModel):
    """A part template (app/cad/templates.py) the server expands into the first ops of a NEW part."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="Template id from the catalog (e.g. hex_bolt, flat_washer, o_ring, bent_bracket)")
    params: list[TemplateParam] = Field(default_factory=list,
                                        description="Only the parameters the request states; the others keep their defaults")

    def param_dict(self) -> dict[str, Any]:
        return {p.name: p.value for p in self.params}


class AgentReply(BaseModel):
    """Structured output requested from the model (its JSON Schema is sent)."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ops", "needs_clarification", "unsupported"] = Field(
        description='"ops" with a complete op list when every critical fact is known; "needs_clarification" when a '
                    "critical fact (size, thickness, hole diameter / position / through or depth, pocket size / depth) is missing; "
                    '"unsupported" only when a concrete feature cannot be represented by the operations (name it and the missing '
                    "capability) or the request is several parts / an assembly (built by the assembly workflow): nothing is built. "
                    "A part category (springs, gears, purchased parts) is never a reason"
    )
    template: TemplateUse | None = Field(
        default=None,
        description="New part only: a part template the server expands into the first ops (then ops holds only extra ops, or []); "
                    "null otherwise",
    )
    reason: str = Field(default="", description='For "unsupported": one sentence naming the feature and the missing capability; otherwise ""')
    questions: list[str] = Field(description="Specific questions (with units) when status is needs_clarification, else []")
    assumptions: list[str] = Field(description="Non-critical details you chose yourself (never a critical fact)")
    requirements: Requirements = Field(
        description="What the request, the answers and (when modifying) the current model state: one envelope for a new part, "
                    "the holes / pockets / bands and notes, each with evidence (exact quotes); when modifying only the changed "
                    "or added features. [] when asking questions"
    )
    ops: list[Operation] = Field(description="The COMPLETE new operation list (not a delta); [] when asking questions")
    changes: list[str] = Field(description="One line per created / changed / removed op, starting with its id")


class ReconstructReply(AgentReply):
    """Structured output of a reconstruct run (drawing → STEP): the modeling reply plus ``unsupported``."""

    status: Literal["ops", "needs_clarification", "unsupported"] = Field(  # type: ignore[assignment]
        description='"ops" with a complete op list when every critical fact is on the sheet (or in the request / answers); '
                    '"needs_clarification" when a critical fact is missing, unreadable or contradictory; "unsupported" only for '
                    "an assembly / weldment sheet, a sheet with no part, or a concrete feature the operations cannot represent "
                    "(named with the missing capability, e.g. a flat pattern, a freeform casting surface): nothing is built"
    )


_TURBO_STATUS = ('"ops" with a complete op list: in Turbo every critical fact the request does not state (size, thickness, hole '
                 'diameter / position / through or depth, pocket size / depth) is adopted by you and recorded in "assumptions"; '
                 '"unsupported" only when a concrete feature cannot be represented by the operations (name it and the missing '
                 "capability) or the request is several parts / an assembly. Nobody answers questions in Turbo")
_TURBO_ASSUMPTIONS = ("Every value you adopted (critical ones included), one line each in the decision form "
                      '"<requirement id>.<field> = <value with unit>; trigger: missing|ambiguous|conflict; basis: <specific reason>; '
                      'affects: <ids>"')


class TurboAgentReply(AgentReply):
    """Turbo (generation supplement D-01, real-model smoke 2026-10-12): the reply cannot ask — ``needs_clarification``
    is not in the schema, so a model that would ask has to adopt the values (or name an unsupported feature)."""

    status: Literal["ops", "unsupported"] = Field(description=_TURBO_STATUS)  # type: ignore[assignment]
    questions: list[str] = Field(description="Always [] in Turbo (nobody answers)")
    assumptions: list[str] = Field(description=_TURBO_ASSUMPTIONS)


class TurboReconstructReply(ReconstructReply):
    """The reconstruct reply under Turbo (see :class:`TurboAgentReply`)."""

    status: Literal["ops", "unsupported"] = Field(description=_TURBO_STATUS)  # type: ignore[assignment]
    questions: list[str] = Field(description="Always [] in Turbo (nobody answers)")
    assumptions: list[str] = Field(description=_TURBO_ASSUMPTIONS)


@dataclass
class DrawingInput:
    """The drawing side of a reconstruct run (built by services/ai_reconstruct.py).

    ``block`` is the body of the <drawing> block (sheet, units, the literal evidence index, the view-scale
    classification); ``values`` maps every citable evidence id to its displayed value (grounding) and
    ``kinds`` to "dimension" / "text"; ``gate(requirements, answers)`` returns extra server gaps (dicts with
    at least ``requirement_id`` and ``question``) that make the reply ask the user instead of building."""

    block: str
    sheet_label: str
    values: Mapping[str, str]
    kinds: Mapping[str, str]
    images: list[ImagePart] = field(default_factory=list)
    gate: Callable[[list[Any], list[str]], list[dict[str, Any]]] | None = None
    lang: Literal["zh", "en"] = "zh"
    detail: dict[str, Any] = field(default_factory=dict)
    # Displayed values of the sheet a derivation may use as inputs (conflicting dimensions excluded).
    derivation_sources: tuple[str, ...] = ()
    # The request is a machine-extracted component summary, not a new user-authored feature specification.
    assembly_component: bool = False


class _ReplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["ops", "needs_clarification", "unsupported"]
    template: TemplateUse | None = None
    reason: str = ""
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    requirements: list[Any] = Field(default_factory=list)
    ops: list[Any] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)


class OpDiff(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)


class AgentOutcome(BaseModel):
    status: Status
    mode: Mode
    spec: ModelSpec | None = None
    questions: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    build: BuildFeedback | None = None
    diff: OpDiff | None = None
    id_remap: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    repairs: int = 0
    simulated: bool = False
    # Requirement contract (JSON dumps of app.cad.requirements models): the grounded list of the last
    # accepted reply, its sha256, the critical gaps that made the server ask, and the check report
    # (app.cad.conformance.CheckReport dump) of the last build; None when no check ran.
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    requirements_sha256: str | None = None
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    check: dict[str, Any] | None = None
    # W3: metadata of the part template the reply used (app/cad/templates.instantiate), stored with the revision
    template: dict[str, Any] | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    calls: list[AiCallRecord] = Field(default_factory=list)


def scope_problem(spec: ModelSpec, reqs: list[Any], base: ModelSpec | None = None) -> str | None:
    """Server check of the supported feature table. The first release refused inclined holes and tilted bodies; the
    W3 geometry library (docs/development/mechanical-engineering/capabilities.zh-CN.md G01–G08) supports any
    workplane / direction, cones, arcs, bent plates and (generation supplement) helical coils, and the checker measures
    them (prism / revolve / sweep / cut / coil requirements), so nothing geometric is refused here any more. No part
    category is refused: the reply declares ``unsupported`` only for a concrete feature the operations cannot represent
    (naming the missing capability) or for several parts / an assembly (the assembly workflow builds those)."""
    return None


#: Geometry-library models whose class docstrings the prompt already explains (their schema descriptions are dropped).
_W3_DEFS = {"Workplane", "DatumRef", "SketchProfile", "Loop", "PathSpec", "LineSeg", "ArcSeg", "CutOp", "RevolveOp", "SweepOp",
            "BentPlateOp", "ShellOp", "LinearLayout", "CircularLayout", "PatternOp", "PrismReq", "RevolveReq", "SweepReq", "CutReq",
            "CoilOp", "CoilReq", "TextOp", "TextReq",
            "TemplateUse", "TemplateParam"}


def _compact(node: Any, in_properties: bool = False) -> Any:
    """Equivalent, shorter schema: no generated ``title`` keys (property names are kept), and fixed-length number
    tuples written as ``items`` instead of ``prefixItems``. The geometry library made the reply schema large enough
    that every token of the context budget counts."""
    if isinstance(node, dict):
        items = node.get("prefixItems")
        if (not in_properties and isinstance(items, list) and items and all(i == {"type": "number"} for i in items)
                and node.get("minItems") == node.get("maxItems") == len(items)):
            node = {k: v for k, v in node.items() if k != "prefixItems"} | {"items": {"type": "number"}}
        return {k: _compact(v, k == "properties") for k, v in node.items() if in_properties or k != "title"}
    if isinstance(node, list):
        return [_compact(v) for v in node]
    return node


def _reply_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    for name, d in (schema.get("$defs") or {}).items():
        if name in _W3_DEFS and isinstance(d, dict):
            d.pop("description", None)
    return _compact(schema)


def agent_reply_json_schema() -> dict[str, Any]:
    return _reply_schema(AgentReply)


def reconstruct_reply_json_schema() -> dict[str, Any]:
    return _reply_schema(ReconstructReply)


def turbo_reply_json_schema(*, drawing: bool) -> dict[str, Any]:
    return _reply_schema(TurboReconstructReply if drawing else TurboAgentReply)


CheckFn = Callable[[ModelSpec, list[Any], Mode], Any]


def run_modeling_agent(
    provider: ModelProvider,
    request_text: str,
    current: CurrentModel | None,
    build: Callable[[ModelSpec], BuildFeedback | Mapping[str, Any]],
    *,
    check: CheckFn | None = None,
    answers: list[dict[str, str]] | None = None,
    max_repairs: int = 1,
    max_build_repairs: int = 2,
    part_name: str | None = None,
    material: str | None = None,
    temperature: float | None = None,
    # Total cap per model call. Calls stream (S1), so large op lists are no longer cut by the gateway at ~120 s.
    timeout_s: float = CAD_MODEL_CALL_S,
    context: str | None = None,
    data: str | None = None,
    constraints: list[str] | None = None,
    drawing: DrawingInput | None = None,
    policy: str | None = None,
    decide: list[str] | None = None,
    source_contexts: tuple[DefinitionSourceContext, ...] = (),
    design_context: DesignDefinitionContext | None = None,
) -> AgentOutcome:
    """Run the agent once. See the module docstring for the flow and error policy.

    ``policy`` = ``"turbo"`` (W7): the Turbo block is appended to the request; grounding (``model.grounding``), the
    critical and drawing gates (``model.gate``) and the conformance check are skipped through ``app.policy.run_check``
    (never called); a ``needs_clarification`` reply ends the run as ``needs_clarification`` with its questions, and the
    caller (``services/ai_modeling.run_modeling``) asks once more with ``decide`` = those questions (generation
    supplement D: rendered as "decide this yourself and record it", ``turbo.modeling_decide_block``). Standard runs are
    unchanged.

    ``answers`` are the user's answers to earlier questions (``{"question", "answer"}``), rendered as
    <answers> and accepted as ``clarification`` evidence. ``check(spec, requirements, mode)`` runs after
    every successful build and returns the check report (``CheckReport`` or its dict) of the bytes that
    build exported; a ``failed`` report is a repair (build budget), then ``requirements_not_met``.
    Without ``check`` no conformance check runs (unit tests of the agent loop).

    AI workspace blocks, all rendered before the request and never replacing the
    full current model: ``context`` (the target selection / target facts) as
    <context>, ``data`` (other references, conversation memory; reference
    material only) as <data>, ``constraints`` (pinned rules) as <constraints>.

    ``drawing`` switches to reconstruct mode (no ``current`` model): the reconstruct prompt with the
    <drawing> block, the sheet image when given, the reply may be ``unsupported``, citations are grounded
    against ``drawing.values`` and ``drawing.gate`` can ask on top of the critical-field gate. The
    request text is then the user's optional notes.
    """
    if drawing is not None and current is not None:
        raise ValueError("a reconstruct run creates a new design; it has no current model")
    tracer = Tracer()
    turbo = exec_policy.is_turbo(policy)
    mode: Mode = "reconstruct" if drawing is not None else ("modify" if current is not None else "create")
    base = current.spec.base if current is not None else None
    used_ids = set(current.used_ids) if current is not None else set()
    answers = [a for a in answers or [] if isinstance(a, dict) and str(a.get("answer") or "").strip()]
    answer_texts = [str(a["answer"]) for a in answers]
    lang: Literal["zh", "en"] = "zh" if _CJK.search(request_text or "") else "en"
    if drawing is not None and not (request_text or "").strip():
        lang = drawing.lang
    call_purpose = "reconstruct" if drawing is not None else "modeling"
    images = list(drawing.images) if drawing is not None else None
    needles = prompts.private_needles(constraints, data, public=[request_text or "", *answer_texts])

    def outcome(status: Status, **kw: Any) -> AgentOutcome:
        out = AgentOutcome(status=status, mode=mode, simulated=provider.simulated,
                           steps=list(tracer.steps), calls=list(tracer.calls), **kw)
        return scrub_outcome(out, needles)

    schema = (turbo_reply_json_schema(drawing=drawing is not None) if turbo else
              reconstruct_reply_json_schema() if drawing is not None else agent_reply_json_schema())
    with tracer.step("prompt") as st:
        if drawing is not None:
            system, version = prompts.RECONSTRUCT_SYSTEM_PROMPT, prompts.RECONSTRUCT_PROMPT_VERSION
            if drawing.assembly_component:
                system += "\n\n" + prompts.ASSEMBLY_COMPONENT_EVIDENCE_SYSTEM
                version += "+assembly-component-1"
            user = prompts.reconstruct_user_message(drawing.block, sheet_label=drawing.sheet_label, notes=request_text,
                                                    part_name=part_name, material=material, context=context, data=data,
                                                    constraints=constraints, answers=answers)
        else:
            system, version = prompts.MODELING_SYSTEM_PROMPT, prompts.MODELING_PROMPT_VERSION
            user = prompts.modeling_user_message(request_text, current, part_name=part_name, material=material, context=context,
                                                 data=data, constraints=constraints, answers=answers)
        if design_context is not None:
            system += "\n\n" + DESIGN_MODELING_SYSTEM
            user += design_context.prompt()
            version += "+design-1"
        if turbo:
            from .turbo import MODELING_TURBO_BLOCK, MODELING_TURBO_SYSTEM, modeling_decide_block

            # the system rules say "ask when a critical fact is missing"; Turbo overrides them at the same level
            system, version = f"{system}\n\n{MODELING_TURBO_SYSTEM}", f"{version}+turbo"
            user = f"{user}\n\n{MODELING_TURBO_BLOCK}"
            if decide:  # generation supplement D-01: the one extra request after a reply asked anyway
                user = f"{user}\n\n{modeling_decide_block(decide)}"
        st.detail = {
            "mode": mode, "prompt_version": version,
            "request_chars": len(request_text or ""), "context_chars": len((context or "").strip()),
            "answers": len(answers),
            "current_ops": len(current.spec.ops) if current else 0,
            "next_free_op_id": prompts.next_free_id(current), "schema": schema_stats(schema),
            "provider": provider.provider_name, "model_id": provider.model_id, "simulated": provider.simulated,
        }
        if turbo:
            st.detail["execution_policy"] = "turbo"
            if decide:
                st.detail["turbo_decide"] = len(decide)
        if drawing is not None:
            st.detail.update({"drawing_chars": len(drawing.block), "images": len(drawing.images),
                              "citable_ids": len(drawing.values), **drawing.detail})
        elif not (request_text or "").strip():
            st.ok = False
            st.detail["error"] = "empty request"
    if drawing is None and not (request_text or "").strip():
        if turbo:  # nothing to adopt a value for: an input problem, never a question to a person
            return outcome("failed", errors=["empty request text"],
                           error={"code": "empty_request", "message": "the request is empty", "retryable": False})
        return outcome("needs_clarification", questions=["What should be modeled or changed?"],
                       errors=["empty request text"])

    if not provider.capabilities.text:
        err = ProviderError("unsupported_capability", f"model {provider.model_id} is not configured for text generation",
                            provider=provider.provider_name, retryable=False)
        err.partial = outcome("failed", errors=[err.message], error=err.to_dict())
        raise err

    messages = [Message(role="user", content=user)]
    validation_repairs = build_repairs = 0
    check_error_repaired = False
    accepted: dict[str, Any] = {}  # requirements of the last reply that passed validation (kept on failure)
    last_check: dict[str, Any] | None = None
    prev_sha: str | None = None
    try:
        while True:
            purpose = call_purpose if validation_repairs + build_repairs == 0 else f"{call_purpose}_repair"
            # Keep the complete CAD union and every server gate; only compatible adapters change its transport.
            with provider.prefer_prompt_schema():
                res = tracer.model_call(
                    provider, purpose=purpose, system=system, system_version=version,
                    messages=messages, json_schema=schema,
                    schema_name="partroute_reconstruct" if drawing is not None else "partroute_model_ops",
                    images=images, temperature=temperature, timeout_s=timeout_s,
                )
            gaps: list[CriticalGap] = []
            extra_gaps: list[dict[str, Any]] = []
            with tracer.step("validation") as st:
                parsed = _validate(res.data, res.parse_error, current, base, used_ids, mode=mode,
                                   request_text=request_text, answers=answer_texts, drawing=drawing, private=needles,
                                   policy=policy, part_name=part_name, source_contexts=source_contexts, design_context=design_context)
                st.ok = not parsed.problems
                reply_status = parsed.envelope.status if parsed.envelope else None
                st.detail = {"status": reply_status,
                             "ops": len(parsed.spec.ops) if parsed.spec else None,
                             "requirements": len(parsed.reqs) if parsed.envelope else None,
                             "problems": parsed.problems[:10], "problem_count": len(parsed.problems)}
                if parsed.remap:
                    st.detail["id_remap"] = parsed.remap
                if parsed.envelope is not None and parsed.envelope.status == "ops" and parsed.reqs:
                    # Server gate (not the model's choice): a missing or assumed critical field asks the user, even
                    # when other parts of the reply need repair (the answer changes the reply anyway). Reconstruct runs
                    # add the drawing gate (cited dimensions that conflict with their view's scale).
                    reqs_now = parsed.reqs
                    gated = exec_policy.run_check("model.gate", policy, lambda: {
                        "gaps": gate(reqs_now),
                        "extra": list(drawing.gate(reqs_now, answer_texts)) if drawing is not None and drawing.gate is not None else None})
                    gaps = list(gated.get("gaps") or [])
                    if gated.get("extra") is not None:  # asked together: one round of questions
                        extra = list(gated["extra"])
                        # A gap the user already answered but the reply did not apply is the model's mistake: repair.
                        extra_gaps = [g for g in extra if not g.get("repair")]
                        parsed.problems += [str(g.get("problem") or g.get("question")) for g in extra if g.get("repair")]
                        st.ok = not parsed.problems
                        st.detail.update({"problems": parsed.problems[:10], "problem_count": len(parsed.problems)})
                    if gaps or extra_gaps:
                        st.ok = True
                        st.detail.update({"status": "needs_clarification", "reply_status": "ops",
                                          "gaps": [g.model_dump() for g in gaps] + extra_gaps})
                    elif not parsed.problems:
                        sha = requirements_sha256(parsed.reqs)
                        st.detail["requirements_sha256"] = sha
                        if prev_sha is not None and sha != prev_sha:
                            st.detail["requirements_changed"] = True
                        prev_sha = sha
            if gaps or extra_gaps:
                env = parsed.envelope
                assert env is not None
                questions = gap_questions(gaps, env.questions, lang) if gaps else []
                questions += [str(g["question"]) for g in extra_gaps if g.get("question") and g["question"] not in questions]
                return outcome("needs_clarification", questions=questions[:10],
                               assumptions=_clean_lines(env.assumptions), gaps=[g.model_dump() for g in gaps] + extra_gaps,
                               repairs=validation_repairs + build_repairs,
                               requirements=RequirementList.dump_python(parsed.reqs, mode="json"),
                               requirements_sha256=requirements_sha256(parsed.reqs))
            if parsed.problems:
                if validation_repairs < max_repairs:
                    validation_repairs += 1
                    _add_repair(tracer, messages, res.text, "validation", parsed.problems, current, used_ids,
                                drawing=drawing is not None)
                    continue
                return outcome("failed", errors=parsed.problems, repairs=validation_repairs + build_repairs,
                               questions=parsed.envelope.questions if parsed.envelope else [], check=last_check, **accepted)
            env = parsed.envelope
            assert env is not None
            if env.status == "needs_clarification":
                # Turbo never asks a person either: the questions go back to run_modeling, which asks exactly once more
                # with each question turned into "decide this yourself and record it" (generation supplement D-01).
                return outcome("needs_clarification", questions=_clean_lines(env.questions),
                               assumptions=_clean_lines(env.assumptions), repairs=validation_repairs + build_repairs)
            if env.status == "unsupported":
                reason = clip(env.reason.strip(), 500)
                return outcome("unsupported", errors=[reason], assumptions=_clean_lines(env.assumptions),
                               repairs=validation_repairs + build_repairs,
                               error={"code": UNSUPPORTED, "message": reason, "retryable": False})
            reqs = parsed.reqs
            accepted = {"requirements": RequirementList.dump_python(reqs, mode="json"), "requirements_sha256": prev_sha}

            spec = parsed.spec
            assert spec is not None
            scope = scope_problem(spec, reqs, current.spec if current else None)
            if scope is not None:  # a server decision: out of the first-release feature table, nothing is built
                with tracer.step("validation") as st:  # the scope decision is part of validating the reply
                    st.ok = False
                    st.detail = {"status": "unsupported", "scope": scope}
                return outcome("unsupported", errors=[scope], assumptions=_clean_lines(env.assumptions),
                               repairs=validation_repairs + build_repairs,
                               error={"code": UNSUPPORTED, "message": scope, "retryable": False})
            diff = _diff(current.spec if current else None, spec)
            warnings = [f"op ids renumbered (ids are never reused): {', '.join(f'{a}→{b}' for a, b in parsed.remap.items())}"] if parsed.remap else []
            if current is not None:
                types = {op.id: op.type for op in current.spec.ops}
                warnings += [f"{i} ({types[i]}) removed" for i in diff.removed]
            with tracer.step("build") as st:
                feedback = _run_build(build, spec)
                st.ok = feedback.ok
                st.detail = {"ok": feedback.ok, "errors": [e.model_dump() for e in feedback.errors[:10]],
                             "warnings": len(feedback.warnings), "metrics": _brief_metrics(feedback.metrics),
                             "diff": diff.model_dump()}
            common = dict(spec=spec, changes=_remap_text(_clean_lines(env.changes), parsed.remap),
                          assumptions=_clean_lines(env.assumptions), build=feedback, diff=diff,
                          id_remap=parsed.remap, warnings=warnings, template=parsed.template, **accepted)
            if not feedback.ok:
                problems = [f"{e.op_id or 'model'}: {e.code}: {e.message}" for e in feedback.errors][:MAX_PROBLEMS] or ["build failed without details"]
                fixed = next((e for e in feedback.errors if e.code in KERNEL_UNSUPPORTED_CODES), None)
                if fixed is not None:  # deterministic (e.g. no installed font covers the text): no repair round, never retried
                    return outcome("failed", errors=problems, repairs=validation_repairs + build_repairs, check=last_check,
                                   error={"code": fixed.code, "message": f"{fixed.op_id or 'model'}: {fixed.message}", "retryable": False},
                                   **common)
                if build_repairs < max_build_repairs:
                    build_repairs += 1
                    _add_repair(tracer, messages, res.text, "build", problems, current, used_ids | {op.id for op in spec.ops})
                    continue
                return outcome("failed", errors=problems, repairs=validation_repairs + build_repairs, check=last_check, **common)

            report: dict[str, Any] | None = None
            check_error: str | None = None
            if check is not None and not turbo and any(r.kind in GEOMETRIC_KINDS for r in reqs):
                with tracer.step("check") as st:
                    report = _run_check(check, spec, reqs, mode)
                    check_error = checker_error(report)
                    failed = report.get("status") == "failed"
                    st.ok = not failed and check_error is None
                    st.detail = check_step_detail(report)
                    if check_error is not None:
                        st.detail["checker_error"] = check_error
                    if not st.ok:  # kept for inspection: the run has no revision of it
                        st.detail["report"] = report
                        st.detail["spec"] = spec.model_dump(mode="json")
                last_check = report
            if report is not None and check_error is not None and report.get("status") != "failed":
                # The checker could not measure this solid: never a pass (and never a saved revision). One repair (the
                # model may avoid the degenerate geometry), then requirements_not_met with the checker's error.
                problems = [f"the independent check could not evaluate the exported solid: {clip(check_error, 300)}"]
                if build_repairs < max_build_repairs and not check_error_repaired:
                    build_repairs += 1
                    check_error_repaired = True
                    _add_repair(tracer, messages, res.text, "check_error", problems, current, used_ids | {op.id for op in spec.ops})
                    continue
                repairs = validation_repairs + build_repairs
                return outcome("failed", errors=problems, repairs=repairs, check=report,
                               error={"code": REQUIREMENTS_NOT_MET, "retryable": False,
                                      "message": f"the requirements could not be checked after {repairs} repair(s): "
                                                 + clip(check_error, 300)}, **common)
            if report is not None and report.get("status") == "failed":
                problems = report_problems(report)[:MAX_PROBLEMS] or ["the requirement check failed without details"]
                if build_repairs < max_build_repairs:
                    build_repairs += 1
                    _add_repair(tracer, messages, res.text, "check", problems, current, used_ids | {op.id for op in spec.ops})
                    continue
                repairs = validation_repairs + build_repairs
                message = (f"the model does not meet the stated requirements after {repairs} repair(s): "
                           + "; ".join(clip(p, 200) for p in problems[:3]))
                return outcome("failed", errors=problems, repairs=repairs, check=report,
                               error={"code": REQUIREMENTS_NOT_MET, "message": message, "retryable": False}, **common)
            return outcome("built", repairs=validation_repairs + build_repairs, check=report, **common)
    except ProviderError as e:
        e.partial = outcome("failed", errors=[e.message], error=e.to_dict(), repairs=validation_repairs + build_repairs,
                            check=last_check, **accepted)
        raise


def gap_questions(gaps: list[CriticalGap], model_questions: list[str], lang: Literal["zh", "en"]) -> list[str]:
    """Questions for the critical gaps: the model's own questions that name a gap's requirement (id or label),
    then a server question (:func:`default_question`) for every gap no model question names."""
    asked = _clean_lines(model_questions)
    out: list[str] = []
    covered: set[str] = set()
    for gap in gaps:
        mine = [q for q in asked if gap.requirement_id in q or (gap.label and gap.label in q)]
        if mine:
            covered.add(gap.requirement_id)
            out += [q for q in mine if q not in out]
    for gap in gaps:
        if gap.requirement_id in covered:
            continue
        q = default_question(gap, lang)
        if q not in out:
            out.append(q)
    return out[:10]


def report_problems(report: Mapping[str, Any]) -> list[str]:
    """Failure lines of a check report dict (``CheckReport.problems()``): failed items, then unrequested features."""
    items = [str(i.get("message") or "") for i in report.get("items") or [] if isinstance(i, Mapping) and i.get("status") == "failed"]
    extra = [str(u.get("message") or "") for u in report.get("unrequested") or [] if isinstance(u, Mapping)]
    return [p for p in items + extra if p]


def check_step_detail(report: Mapping[str, Any]) -> dict[str, Any]:
    """Compact ``check`` step detail (counts, first problems, bindings)."""
    items = [i for i in report.get("items") or [] if isinstance(i, Mapping)]
    counts: dict[str, int] = {}
    for i in items:
        counts[str(i.get("status"))] = counts.get(str(i.get("status")), 0) + 1
    out: dict[str, Any] = {"status": report.get("status"), "items": counts, "unrequested": len(report.get("unrequested") or []),
                           "problems": [clip(p, 300) for p in report_problems(report)[:10]],
                           "step_sha256": report.get("step_sha256"), "requirements_sha256": report.get("requirements_sha256"),
                           "validator_version": report.get("validator_version"), "mode": report.get("mode")}
    if report.get("error"):
        out["error"] = report["error"]
    return out


def checker_error(report: Mapping[str, Any] | None) -> str | None:
    """Why the checker could not give a verdict on a list WITH geometric requirements (an exception, no exported STEP,
    an item it could not evaluate), or None. Such a report is a failed check, never a pass: the gate has already made
    sure every critical value is known, so a ``not_evaluated`` item here is an error of the check itself."""
    if report is None:
        return None
    if report.get("error"):
        return str(report["error"])
    if report.get("status") in ("passed", "failed"):
        return None
    items = [i for i in report.get("items") or [] if isinstance(i, Mapping) and i.get("status") == "not_evaluated"]
    if items:
        return "; ".join(str(i.get("message") or "not evaluated") for i in items[:3])
    return f"the check returned no verdict (status {report.get('status')!r})"


def scrub_outcome(out: AgentOutcome, needles: list[str]) -> AgentOutcome:
    """The outcome with every private needle removed from what reaches run-level records: steps, call records (all
    turns and the response, not only the private blocks), errors, questions, gaps, changes, assumptions, warnings and
    the check report. Requirements are not rewritten (they are hashed); a reply repeating private text never passes
    validation, so accepted requirements hold none."""
    if not needles:
        return out
    sp = prompts.scrub_private
    steps = [st.model_copy(update={"detail": sp(st.detail, needles)}) for st in out.steps]
    calls = [c.model_copy(update={"request": sp(c.request, needles), "response": sp(c.response, needles),
                                  "error": sp(c.error, needles)}) for c in out.calls]
    return out.model_copy(update={
        "steps": steps, "calls": calls, "errors": sp(out.errors, needles), "error": sp(out.error, needles),
        "questions": sp(out.questions, needles), "gaps": sp(out.gaps, needles), "changes": sp(out.changes, needles),
        "assumptions": sp(out.assumptions, needles), "warnings": sp(out.warnings, needles), "check": sp(out.check, needles),
    })


def _run_check(check: CheckFn, spec: ModelSpec, reqs: list[Any], mode: Mode) -> dict[str, Any]:
    try:
        rep = check(spec, reqs, mode)
    except Exception as e:  # noqa: BLE001 - a checker crash is reported (not_evaluated), never raised
        return {"status": "not_evaluated", "error": clip(f"{type(e).__name__}: {e}", 400), "items": [], "unrequested": []}
    if rep is None:
        return {"status": "not_evaluated", "error": "no exported STEP to check", "items": [], "unrequested": []}
    if isinstance(rep, BaseModel):
        return rep.model_dump(mode="json")
    return dict(rep)


class _Parsed(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    envelope: _ReplyEnvelope | None = None
    spec: ModelSpec | None = None
    reqs: list[Any] = Field(default_factory=list)
    remap: dict[str, str] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    template: dict[str, Any] | None = None


def template_param_problems(use: TemplateUse, request_text: str, answers: Iterable[str]) -> list[str]:
    """Every parameter the reply sets on a template must be stated by the request or an answer (a number written
    there, a size name such as "M8" quoted literally, or a choice by one of its stated words: the spring templates' end
    form "closed_ground" by "closed and ground", handedness "left" by "left-hand" (or their Chinese equivalents)); unstated parameters keep the
    template defaults."""
    from app.cad.requirements import extract_numbers, normalize_text, number_in
    from app.cad.templates import TEMPLATES

    tpl = TEMPLATES.get(use.id)
    aliases = {(p.name, a): words for p in (tpl.params if tpl else ()) for a, words in p.aliases}
    texts = [request_text or "", *answers]
    numbers = [n for t in texts for n in extract_numbers(t)]
    norm = [normalize_text(t) for t in texts]
    out = []
    for name, value in use.param_dict().items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if not number_in(float(value), numbers):
                out.append(f"template {safe_id(use.id)} parameter {safe_id(name)} = {value:g} is not stated by the request or an "
                           "answer; leave it out (default) or ask")
        elif isinstance(value, str) and value.strip() and not any(normalize_text(w) in t for w in (value, *aliases.get((name, value), ()))
                                                                  for t in norm):
            out.append(f"template {safe_id(use.id)} parameter {safe_id(name)} is not written in the request or an answer; leave "
                       "it out (default) or ask")
    return out


def _template_expansion(use: TemplateUse, mode: Mode, request_text: str, answers: list[str]) -> tuple[list[Any], dict[str, Any] | None,
                                                                                                          dict[str, str], list[str]]:
    """(template op dicts, template meta, ``template`` evidence values, problems) of a reply's template."""
    from app.cad.templates import TemplateError, instantiate

    if mode != "create":
        return [], None, {}, ["template: only a new part described in text can start from a template (not a modification or a "
                              "drawing reconstruction); set template to null and write the ops"]
    try:
        spec, meta = instantiate(use.id, use.param_dict())
    except TemplateError as e:
        return [], None, {}, [f"template: {e.message}"]
    lines = [f"template {use.id} v{meta['template_version']}: {meta['spec_label']}"]
    lines += _number_lines({"params": {k: v for k, v in meta["params"].items() if isinstance(v, (int, float))}})
    for op in spec.ops:
        lines.append(_op_text(op))
    return ([op.model_dump(mode="json") for op in spec.ops], meta, {use.id: "\n".join(lines)},
            template_param_problems(use, request_text, answers))


def _validate(
    data: dict[str, Any] | None,
    parse_error: str | None,
    current: CurrentModel | None,
    base: Any,
    used_ids: set[str],
    *,
    mode: Mode = "create",
    request_text: str = "",
    answers: list[str] | None = None,
    drawing: DrawingInput | None = None,
    private: list[str] | None = None,
    policy: str | None = None,
    part_name: str | None = None,
    source_contexts: tuple[DefinitionSourceContext, ...] = (),
    design_context: DesignDefinitionContext | None = None,
) -> _Parsed:
    if data is None:
        return _Parsed(problems=[f"the reply was not a JSON object ({parse_error or 'no detail'})"])
    data = drop_nulls(data)
    raw_ops = data.get("ops") if isinstance(data.get("ops"), list) else []
    try:
        env = _ReplyEnvelope.model_validate(data)
    except ValidationError as e:
        return _Parsed(problems=format_validation_errors(e, raw_ops))
    if env.status == "unsupported":
        if not env.reason.strip():
            return _Parsed(envelope=env, problems=['status is unsupported but reason is empty; name the feature that cannot be '
                                                   'represented and the missing capability'])
        return _Parsed(envelope=env)
    echo = private_echo_problems(env, private or [])
    if env.status == "needs_clarification":
        if not _clean_lines(env.questions):
            return _Parsed(envelope=env, problems=["status is needs_clarification but questions is empty"])
        return _Parsed(envelope=env, problems=echo)
    t_ops: list[Any] = []
    t_meta: dict[str, Any] | None = None
    t_values: dict[str, str] = {}
    t_problems: list[str] = []
    if env.template is not None:
        t_ops, t_meta, t_values, t_problems = _template_expansion(env.template, mode, request_text, answers or [])
    reqs, req_problems = validate_requirements(env.requirements, mode=mode, current=current, request_text=request_text,
                                               answers=answers or [],
                                               drawing_values=drawing.values if drawing is not None else None,
                                               drawing_kinds=drawing.kinds if drawing is not None else None,
                                               derivation_sources=drawing.derivation_sources if drawing is not None else (),
                                               derived_through=drawing is not None, policy=policy, template_values=t_values or None,
                                               part_name=part_name, source_contexts=source_contexts, design_context=design_context)
    req_problems = echo + t_problems + req_problems
    raw = t_ops + list(env.ops)
    if not raw:
        return _Parsed(envelope=env, reqs=reqs,
                       problems=["status is ops but the ops list is empty; return the complete op list", *req_problems])
    ops, remap = normalize_op_ids(raw, current.spec if current else None, used_ids)
    try:
        spec = ModelSpec.model_validate({"base": base.model_dump(mode="json") if base else None, "ops": ops})
    except ValidationError as e:
        return _Parsed(envelope=env, reqs=reqs, remap=remap, problems=format_validation_errors(e, ops) + req_problems)
    if mode == "modify" and current is not None:  # coverage only serves the conformance check: skipped with it in Turbo
        cur_spec = current.spec
        req_problems += exec_policy.run_check("model.grounding", policy,
                                              lambda: {"problems": coverage_problems(cur_spec, spec, reqs)}).get("problems") or []
    if t_meta is not None:
        t_meta = {**t_meta, "op_ids": [op["id"] for op in ops[:len(t_ops)]]}
    return _Parsed(envelope=env, spec=spec, reqs=reqs, remap=remap, problems=req_problems[:MAX_PROBLEMS], template=t_meta)


def validate_requirements(
    raw: list[Any],
    *,
    mode: Mode,
    current: CurrentModel | None,
    request_text: str,
    answers: list[str],
    drawing_values: Mapping[str, str] | None = None,
    drawing_kinds: Mapping[str, str] | None = None,
    derivation_sources: Iterable[str] = (),
    derived_through: bool = False,
    policy: str | None = None,
    template_values: Mapping[str, str] | None = None,
    part_name: str | None = None,
    source_contexts: tuple[DefinitionSourceContext, ...] = (),
    design_context: DesignDefinitionContext | None = None,
) -> tuple[list[Any], list[str]]:
    """Parse the reply's requirements and check them against the server rules: schema, one envelope for a new
    part, ``source_geometry`` only for ops of the current model (whose numbers come from that op itself, never from
    the written ``value``), and grounding (quotes are real, critical numbers and positions are cited, through / blind
    is stated, derivations compute from cited numbers: :func:`app.cad.requirements.ground`). Returns
    ``(requirements, problems)``.

    Reconstruct mode: ``drawing_values`` are the citable evidence ids and their displayed values, and
    ``drawing_kinds`` says whether an id is a dimension or a text (a ``drawing_dimension`` must cite a
    dimension id, a ``drawing_text`` a text id)."""
    try:
        reqs = parse_requirements(raw)
    except ValidationError as e:
        return [], format_requirement_errors(e, raw)
    problems: list[str] = []
    envelopes = [r for r in reqs if r.kind == "envelope"]
    if mode in ("create", "reconstruct") and not envelopes:
        problems.append('requirements: a new part needs one envelope requirement (kind "envelope": the overall size [x, y, z] '
                        "with evidence)")
    if len(envelopes) > 1:
        problems.append(f"requirements: {len(envelopes)} envelope requirements ({', '.join(r.id for r in envelopes)}); "
                        "give exactly one overall size")
    op_ids = ({op.id for op in current.spec.ops} | ({"F-00"} if current.spec.base is not None else set())) if current else set()
    for r in reqs:
        for k, ev in enumerate(r.evidence):
            if ev.source != "source_geometry":
                continue
            if current is None:
                problems.append(f"{r.id} evidence[{k}]: source_geometry cites a current model, but this is a new part; "
                                "cite the request or an answer")
            elif ev.ref not in op_ids:
                known = ", ".join(sorted(op_ids)) or "none"
                problems.append(f"{r.id} evidence[{k}]: source_geometry ref {safe_id(ev.ref)} is not an op of the current model "
                                f"(op ids: {known})")
    if drawing_kinds is not None:
        want = {"drawing_dimension": "dimension", "drawing_text": "text"}
        for r in reqs:
            for k, ev in enumerate(r.evidence):
                kind = drawing_kinds.get(ev.ref) if ev.source in want else None
                if kind is not None and kind != want[ev.source]:
                    problems.append(f"{r.id} evidence[{k}]: {ev.ref} is a drawing {kind}, not a {want[ev.source]}; "
                                    f"cite it with source \"drawing_{kind}\"")
    if design_context is not None:
        problems += design_requirement_problems(reqs, design_context)
    # Grounding is a business check (W0 §2.1 model.grounding): skipped in Turbo, where values may be adopted.
    problems += exec_policy.run_check("model.grounding", policy, lambda: {"problems": ground(
        reqs, request_text=request_text, answers=answers, drawing_values=drawing_values,
        geometry=geometry_values(current) if current is not None else None,
        derivation_sources=derivation_sources, derived_through=derived_through,
        template_values=template_values, part_name=part_name, source_contexts=source_contexts, design_context=design_context)}).get("problems") or []
    return reqs, problems[:MAX_PROBLEMS]


def _num(x: float) -> str:
    s = f"{float(x):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


_TEXT_KEYS = {"id", "label", "type", "kind", "mode", "plane", "axis", "group", "target", "suppressed"}


def _number_lines(obj: Any, prefix: str = "") -> list[str]:
    """``key: v, v`` lines of every number of a dumped op (ids, labels and other free text left out: an op's label was
    written by a model and is no evidence)."""
    out: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k not in _TEXT_KEYS or isinstance(v, (Mapping, list, tuple)):  # a Workplane "plane" / vector "axis" has numbers
                out += _number_lines(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, (list, tuple)):
        if obj and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in obj):
            out.append(f"{prefix}: " + ", ".join(_num(v) for v in obj))
        else:
            for v in obj:
                out += _number_lines(v, prefix)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(f"{prefix}: {_num(obj)}")
    return out


def _op_text(op: Any) -> str:
    """How the CURRENT model states one op, for ``source_geometry`` evidence: its numbers, and for a hole whether it
    is through or blind and every instance position; for an axis-aligned body its extents."""
    lines = [f"{op.type} {op.id}"] + _number_lines(op.model_dump(mode="json"))
    if isinstance(op, HoleOp):
        lines.append("through" if op.depth is None else f"blind, depth {_num(op.depth)}")
        lines += ["instance: " + ", ".join(_num(c) for c in p) for p in hole_positions(op)]
    elif op.type == "box" and not any(op.rotate):
        (ax, ay, az), (sx, sy, sz) = op.at, op.size
        lines.append(f"extent min: {_num(ax - sx / 2)}, {_num(ay - sy / 2)}, {_num(az)}; "
                     f"max: {_num(ax + sx / 2)}, {_num(ay + sy / 2)}, {_num(az + sz)}")
    elif op.type == "cylinder" and not any(op.rotate):
        (ax, ay, az), r, h = op.at, op.radius, op.height
        i = "xyz".index(op.axis)
        lo, hi = [ax - r, ay - r, az - r], [ax + r, ay + r, az + r]
        lo[i], hi[i] = (ax, ay, az)[i], (ax, ay, az)[i] + h
        lines.append("extent min: " + ", ".join(_num(v) for v in lo) + "; max: " + ", ".join(_num(v) for v in hi)
                     + f"; diameter {_num(2 * r)}")
    elif op.type == "extrude" and op.plane == "XY" and not any(op.rotate):
        lines.append(f"z range: {_num(op.offset)}, {_num(op.offset + op.height)}")
    return "\n".join(lines)


def geometry_values(current: CurrentModel) -> dict[str, str]:
    """``source_geometry`` evidence of the model being modified: op id → the op as the current model states it
    (:func:`_op_text`); ``F-00`` (an imported base) → its measured bounding box and detected holes. Built by the
    server; the reply's ``value`` text is never read."""
    out = {op.id: _op_text(op) for op in current.spec.ops}
    if current.spec.base is not None:
        lines = ["imported base F-00"]
        bbox = (current.metrics or {}).get("bbox") if isinstance(current.metrics, Mapping) else None
        if isinstance(bbox, Mapping):
            lines += _number_lines({k: v for k, v in bbox.items() if k in ("min", "max", "size")})
        for h in current.holes or []:
            if isinstance(h, Mapping):
                lines += _number_lines({k: v for k, v in h.items() if k in ("diameter", "length", "positions", "count")})
                if h.get("through") is True:
                    lines.append("through")
                elif h.get("through") is False:
                    lines.append(f"blind, depth {_num(h.get('length') or 0)}")
        out["F-00"] = "\n".join(lines)
    return out


def private_echo_problems(env: Any, needles: list[str]) -> list[str]:
    """A reply that repeats private conversation context (pinned constraints, <data>) in text shown to the project
    (questions, assumptions, changes, requirements) is not acceptable. The problem never repeats the text."""
    if not needles:
        return []
    out: list[str] = []

    def strings(obj: Any) -> list[str]:
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, Mapping):
            return [x for v in obj.values() for x in strings(v)]
        if isinstance(obj, (list, tuple)):
            return [x for v in obj for x in strings(v)]
        return []

    where = [("questions", env.questions), ("assumptions", env.assumptions), ("changes", env.changes)]
    for i, r in enumerate(env.requirements or []):
        rid = r.get("id") if isinstance(r, Mapping) and isinstance(r.get("id"), str) and REQ_ID.match(r["id"]) else f"#{i + 1}"
        where.append((f"requirement {rid}", r))
    for name, obj in where:
        if any(prompts.contains_private(t, needles) for t in strings(obj)):
            out.append(f"{name}: repeats private conversation context (pinned constraints / reference data); that text is "
                       "shown to everyone on the project, so describe the feature in your own words and cite only the "
                       "request, the answers or the current model")
    return out


_COVER_KINDS: dict[str, tuple[str, ...]] = {"hole": ("hole",), "cut": ("pocket", "band", "cut", "prism", "revolve"),
                                             "body": ("envelope", "band", "prism", "revolve", "sweep", "coil"),
                                             "text": ("text",)}
_COVER_TOL = 0.01  # mm


def _pattern_hole_positions(op: PatternOp, target: HoleOp) -> list[Any]:
    from app.cad.datums import apply, pattern_matrices

    return [apply(m, p) for m in pattern_matrices(op.layout) for p in hole_positions(target)]


def coverage_problems(old: ModelSpec, new: ModelSpec, reqs: list[Any]) -> list[str]:
    """Modify mode: every op the reply adds or changes must be covered by a geometric requirement of its kind, so the
    independent check measures it (a change is never saved unchecked). Exempt: fillets / chamfers, suppressed ops,
    edits of only label / suppressed. A hole op (or a pattern of one) is covered when every instance is an entry of a
    hole requirement; a subtracting body, a cut or a shell (or a transform / pattern of one) needs a removal or section
    requirement (pocket, band, cut, prism, revolve); any other body change a material requirement (envelope, band,
    prism, revolve, sweep)."""
    before = {op.id: op.model_dump(mode="json") for op in old.ops}
    by_id = {op.id: op for op in new.ops}
    kinds = {r.kind for r in reqs}
    entries = [e for r in reqs if r.kind == "hole" for e in r.entries]
    problems: list[str] = []
    for op in new.ops:
        now = op.model_dump(mode="json")
        prev = before.get(op.id)
        if prev == now or op.type in ("fillet", "chamfer") or op.suppressed:
            continue
        if prev is not None and {k for k in set(prev) | set(now) if prev.get(k) != now.get(k)} <= {"label", "suppressed"}:
            continue
        what = "added" if prev is None else "changed"
        hole_target = by_id.get(op.target) if isinstance(op, PatternOp) else None
        if isinstance(op, HoleOp) or isinstance(hole_target, HoleOp):
            pts = hole_positions(op) if isinstance(op, HoleOp) else _pattern_hole_positions(op, hole_target)  # type: ignore[arg-type]
            missing = [p for p in pts if not any(max(abs(a - b) for a, b in zip(p, e)) <= _COVER_TOL for e in entries)]
            if missing:
                at = ", ".join(_num(c) for c in missing[0])
                problems.append(
                    f"{op.id} (hole Ø{_num((op if isinstance(op, HoleOp) else hole_target).diameter)}) is {what} but "  # type: ignore[union-attr]
                    f"{len(missing)} of its {len(pts)} instance(s) are not "
                    f"entries of a hole requirement (first at ({at})); add the hole requirement the request states for it "
                    "(entries = every instance, through or depth, cited) so the change is checked — or, if the requirement "
                    "is right, fix the op"
                )
            continue
        body = by_id.get(op.target) if op.type in ("transform", "pattern") else op
        removal = getattr(body, "mode", None) == "subtract" or getattr(body, "type", None) in ("cut", "shell")
        group = "text" if op.type == "text" else "cut" if removal else "body"
        if not kinds & set(_COVER_KINDS[group]):
            basic = [k for k in _COVER_KINDS[group] if k not in ("cut", "prism", "revolve", "sweep", "coil")]
            extra = [k for k in _COVER_KINDS[group] if k not in basic]
            need = " or ".join(basic) + f" requirement (nor a {' / '.join(extra)} one)"
            problems.append(
                f"{op.id} ({op.type}) is {what} but no {need} states the result; add the requirement the "
                "request states for it (cited) so the change is checked"
            )
    return problems


def format_requirement_errors(e: ValidationError, raw: list[Any]) -> list[str]:
    """Readable, requirement-addressed schema errors: ``requirement #2 (R2, hole) depth: …``."""
    lines: list[str] = []
    for err in e.errors()[:MAX_PROBLEMS]:
        loc = list(err.get("loc", ()))
        msg = str(err.get("msg", "invalid"))
        if loc and isinstance(loc[0], int):
            idx = loc[0]
            item = raw[idx] if idx < len(raw) and isinstance(raw[idx], dict) else {}
            rest = loc[1:]
            if rest and rest[0] == item.get("kind"):
                rest = rest[1:]
            path = ".".join(str(x) for x in rest)
            lines.append(f"requirement #{idx + 1} ({item.get('id', '?')}, {item.get('kind', '?')}){' ' + path if path else ''}: {msg}")
        else:
            path = ".".join(str(x) for x in loc)
            lines.append(f"requirements{'.' + path if path else ''}: {msg}")
    if len(e.errors()) > MAX_PROBLEMS:
        lines.append(f"… and {len(e.errors()) - MAX_PROBLEMS} more problems")
    return lines


def normalize_op_ids(
    raw_ops: list[Any], current: ModelSpec | None, used_ids: Iterable[str] = ()
) -> tuple[list[Any], dict[str, str]]:
    """Enforce the id rules deterministically instead of spending a repair call.

    Kept: an id of a current op with the same type, or a fresh id above every
    id the part has used. Renumbered (to the next free ids, in order): reused
    ids of removed ops, ids reused for a different op type, duplicates and
    malformed ids. Transform targets that pointed at a renumbered id follow it.
    """
    used = set(used_ids)
    cur_types = {op.id: op.type for op in current.ops} if current else {}
    floor = max((int(i[2:]) for i in used | set(cur_types) if OP_ID_RE.match(i)), default=0)
    seen: set[str] = set()
    out: list[Any] = []
    rename: list[int] = []
    for idx, op in enumerate(raw_ops):
        if not isinstance(op, dict):
            out.append(op)
            continue
        oid = op.get("id")
        keep = False
        if isinstance(oid, str) and oid not in seen and OP_ID_RE.match(oid) and oid != "F-00":
            keep = cur_types[oid] == op.get("type") if oid in cur_types else int(oid[2:]) > floor and oid not in used
        out.append(op if keep else dict(op))
        if keep:
            seen.add(oid)  # type: ignore[arg-type]
        else:
            rename.append(idx)
    n = max([floor] + [int(i[2:]) for i in seen])
    remap: dict[str, str] = {}
    for idx in rename:
        n += 1
        old = out[idx].get("id")
        out[idx]["id"] = f"F-{n:02d}"
        if isinstance(old, str) and old not in remap and old not in seen:
            remap[old] = out[idx]["id"]
    for op in out:
        if isinstance(op, dict) and remap:
            remap_refs(op, remap)  # transform / pattern targets and DatumRef planes follow renumbered ids
    return out, remap


def format_validation_errors(e: ValidationError, raw_ops: list[Any]) -> list[str]:
    """Readable, op-addressed messages for the repair prompt and the UI."""
    lines: list[str] = []
    for err in e.errors()[:MAX_PROBLEMS]:
        loc = list(err.get("loc", ()))
        msg = str(err.get("msg", "invalid"))
        if len(loc) >= 2 and loc[0] == "ops" and isinstance(loc[1], int):
            idx = loc[1]
            op = raw_ops[idx] if idx < len(raw_ops) and isinstance(raw_ops[idx], dict) else {}
            rest = loc[2:]
            if rest and rest[0] == op.get("type"):
                rest = rest[1:]
            where = f"op #{idx + 1} ({op.get('id', '?')}, {op.get('type', '?')})"
            path = ".".join(str(x) for x in rest)
            lines.append(f"{where}{' ' + path if path else ''}: {msg}")
        else:
            path = ".".join(str(x) for x in loc)
            lines.append(f"{path or 'reply'}: {msg}")
    if len(e.errors()) > MAX_PROBLEMS:
        lines.append(f"… and {len(e.errors()) - MAX_PROBLEMS} more problems")
    return lines


def _add_repair(
    tracer: Tracer,
    messages: list[Message],
    previous_text: str,
    kind: Literal["validation", "build", "check", "check_error"],
    problems: list[str],
    current: CurrentModel | None,
    used_ids: set[str],
    *,
    drawing: bool = False,
) -> None:
    known = [{"id": i} for i in used_ids] + ([{"id": op.id} for op in current.spec.ops] if current else [])
    with tracer.step("repair") as st:
        st.detail = {"reason": kind, "problems": [clip(p, 300) for p in problems[:10]]}
        messages.append(Message(role="assistant", content=clip(previous_text, 30_000)))
        messages.append(Message(role="user", content=prompts.modeling_repair_message(kind, problems, next_id=next_op_id(known),
                                                                                     drawing=drawing)))


def _run_build(build: Callable[[ModelSpec], Any], spec: ModelSpec) -> BuildFeedback:
    try:
        fb = build(spec)
    except Exception as e:  # noqa: BLE001 - a kernel crash on model output is reported, not raised
        return BuildFeedback(ok=False, errors=[OpError(code="kernel_exception", message=clip(f"{type(e).__name__}: {e}", 400))])
    if isinstance(fb, BuildFeedback):
        return fb
    if isinstance(fb, BaseModel):
        return BuildFeedback.model_validate(fb.model_dump(mode="json"))
    return BuildFeedback.model_validate(fb)


def _diff(old: ModelSpec | None, new: ModelSpec) -> OpDiff:
    before = {op.id: op.model_dump(mode="json") for op in old.ops} if old else {}
    after = {op.id: op.model_dump(mode="json") for op in new.ops}
    return OpDiff(
        added=[i for i in after if i not in before],
        removed=[i for i in before if i not in after],
        modified=[i for i in after if i in before and after[i] != before[i]],
        unchanged=[i for i in after if i in before and after[i] == before[i]],
    )


def _brief_metrics(m: dict[str, Any] | None) -> dict[str, Any] | None:
    if not m:
        return None
    bbox = m.get("bbox") if isinstance(m.get("bbox"), dict) else {}
    return {k: v for k, v in {"valid": m.get("valid"), "solids": m.get("solids"), "volume_mm3": m.get("volume_mm3"),
                              "bbox_size": bbox.get("size")}.items() if v is not None}


def _clean_lines(items: list[str]) -> list[str]:
    return [clip(s.strip(), 500) for s in items if isinstance(s, str) and s.strip()][:30]


_ID_TOKEN = re.compile(r"\bF-\d{2,}\b")


def _remap_text(lines: list[str], remap: dict[str, str]) -> list[str]:
    if not remap:
        return lines
    return [_ID_TOKEN.sub(lambda m: remap.get(m.group(0), m.group(0)), s) for s in lines]
