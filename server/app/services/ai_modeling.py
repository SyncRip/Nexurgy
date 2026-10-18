"""Shared AI modeling core: request text (+ base revision, clarification answers) → checked revision.

One path for every entry (W6 parity): the workflow node ``model_3d`` (including
assistant CAD operations, which run through the workflow graph) and the retained
collaboration tool ``model_3d``. Every entry gets the same
requirement contract, the same server gate, the same independent check of the
exported STEP and the same failure codes (.trellis/tasks/archive/2026-10/10-08-cad-ai/design.md
§0.4, §0.7, §1, §2, §6):

- critical requirement facts missing / assumed → ``needs_clarification``, nothing built;
- check still failing after the repair budget → ``failed`` / ``requirements_not_met``,
  no revision (the failed spec and report stay in the steps); a checker that cannot
  evaluate the solid (exception, item not evaluated) is a failure too, never a save;
- in modify mode every changed op is covered by a requirement (agent validation), so a
  change is never saved unchecked; ``not_evaluated`` is only a modification that changed
  nothing measurable (fillets, chamfers, suppression);
- otherwise a revision whose ``requirements`` and ``checks.conformance`` are bound to
  exactly the STEP bytes it stores.

The drawing → STEP reconstruction (services/ai_reconstruct.py) runs through the same
function with ``drawing=`` (reconstruct mode of the agent): same gate, check and repair
budget, plus ``unsupported`` (nothing built) and the source provenance in ``checks``.

Turbo (W7; generation supplement D-01 / D-02): a reply that still asks gets exactly one more request in which every
question reads "decide this yourself and record it" (step ``turbo_decide``; both attempts' steps and calls are
returned); asking again after that fails with ``turbo_clarification_refused`` listing the questions. Every adopted
value becomes a decision record (:func:`turbo_assumptions`) with its trigger, basis and affected ids.

Callers record the returned call records themselves (node run vs agent execution),
so this module never writes ``ai_calls``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from .. import policy as exec_policy
from ..ai.modeling_agent import (
    REQUIREMENTS_NOT_MET,
    UNSUPPORTED,
    AgentOutcome,
    CurrentModel,
    DrawingInput,
    checker_error,
    report_problems,
)
from ..ai.modeling_engines import ModelingInput, get_engine
from ..ai.providers.base import ModelConfig, ModelProvider, ProviderError, clip
from ..cad.ops import ModelSpec, build_key, remap_refs
from ..cad.requirements import GEOMETRIC_KINDS, DefinitionSourceContext, DesignDefinitionContext
from ..errors import AppError
from ..models import ModelRevision, Part, User
from . import ai as ai_svc
from . import revisions as rev_svc

Status = Literal["built", "needs_clarification", "failed", "unsupported"]


@dataclass
class ModelingResult:
    """What one modeling request produced. ``error`` (failed / unsupported): ``code``, ``message``, ``retryable``,
    ``details`` and ``kind`` = provider (the model service failed) | outcome (the model could not produce an
    acceptable design, or said the part is out of scope) | service (saving the revision was refused) | source
    (the input drawing could not be read; reconstruct only)."""

    status: Status
    label: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    outcome: AgentOutcome | None = None
    revision: ModelRevision | None = None
    questions: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # Turbo (W7): the values the modeling agent adopted (policy.Assumption dumps); [] in standard runs.
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    # A built design :func:`design` made and :func:`save` has not stored yet (None once saved, and for a final result).
    design: Design | None = None


@dataclass(frozen=True)
class PartBrief:
    """What the model + kernel phase (:func:`design`) reads of the part: plain values, never an ORM object."""

    number: str
    name: str
    material: str | None
    op_counter: int = 0

    @classmethod
    def of(cls, part: Part) -> PartBrief:
        return cls(number=part.number, name=part.name, material=part.material, op_counter=int(part.op_counter or 0))


@dataclass
class Design:
    """A design that was built and passed its check, waiting for :func:`save`: the final spec (op ids shifted), the kernel
    result whose STEP was checked, the check report, the id shift, the policy and the revision's request text."""

    spec: ModelSpec
    result: Any
    report: dict[str, Any] | None
    offset: int
    policy: str
    request_text: str | None
    design_context: DesignDefinitionContext | None = None
    part_snapshot: PartBrief | None = None


def select_provider(db: Session, model_config_id: ai_svc.ModelRef) -> tuple[ModelProvider, ModelConfig | None]:
    """The modeling provider (raises ProviderError when nothing usable is configured). ``model_config_id`` may be
    a frozen model snapshot of the run / role (services/ai.py), built exactly with its live restrictions."""
    return ai_svc.provider_for(db, "modeling", model_config_id)


def label_of(provider: ModelProvider, cfg: ModelConfig | None, simulated: bool | None = None) -> dict[str, Any]:
    # Keys must not collide with port names ("model"): downstream nodes read outputs["model"].
    return {"simulated": provider.simulated if simulated is None else simulated,
            "ai_model": cfg.model_id if cfg else "simulated", "ai_provider": cfg.provider if cfg else "simulated"}


def current_model(part: Part, base: ModelRevision | None) -> CurrentModel | None:
    if base is None:
        return None
    return CurrentModel(spec=rev_svc.spec_of(base), metrics=base.metrics, holes=base.holes, part_name=part.name, material=part.material,
                        used_ids=[f"F-{i:02d}" for i in range(1, part.op_counter + 1)])


def renumber(spec: ModelSpec, offset: int) -> ModelSpec:
    """Shift every op id by ``offset`` (a fresh design on a part with history: ids are never reused)."""
    mapping = {op.id: f"F-{int(op.id[2:]) + offset:02d}" for op in spec.ops}
    data = spec.model_dump(mode="json")
    for op in data["ops"]:
        op["id"] = mapping[op["id"]]
        remap_refs(op, mapping)  # transform / pattern targets and DatumRef planes (W3)
    return ModelSpec.model_validate(data)


def _template_meta(outcome: AgentOutcome, offset: int) -> dict[str, Any] | None:
    """The reply's part-template metadata with op ids shifted like the saved spec (W3)."""
    meta = outcome.template
    if not meta or not offset:
        return meta
    return {**meta, "op_ids": [f"F-{int(i[2:]) + offset:02d}" for i in meta.get("op_ids") or []]}


def redact_calls(calls: list[Any]) -> list[Any]:
    """Call records without the private prompt blocks: ai_calls rows belong to the run, the private
    context to the conversation owner (its context build keeps what the model was given)."""
    from ..ai.prompts import redact_private_blocks

    out = []
    for c in calls:
        request = dict(c.request or {})
        messages = [{**m, "content": redact_private_blocks(m.get("content") or "")} if m.get("role") == "user" else m
                    for m in request.get("messages") or []]
        if messages != request.get("messages"):
            request["messages"], request["private_context"] = messages, "omitted"
        out.append(c.model_copy(update={"request": request}))
    return out


def check_summary(requirements: list[dict[str, Any]] | None, report: dict[str, Any] | None) -> dict[str, Any]:
    """Compact, public summary of a requirement list and its check report (node outputs, tool results,
    assistant results): verdict, requirement counts, item counts, first problems, note coverage."""
    reqs = [r for r in requirements or [] if isinstance(r, dict)]
    by_kind: dict[str, int] = {}
    for r in reqs:
        by_kind[str(r.get("kind"))] = by_kind.get(str(r.get("kind")), 0) + 1
    out: dict[str, Any] = {
        "status": (report or {}).get("status") or "not_evaluated",
        "requirements": {"total": len(reqs), "geometric": sum(1 for r in reqs if r.get("kind") in GEOMETRIC_KINDS),
                         "by_kind": by_kind},
    }
    if report is None:
        out["reason"] = "no geometric requirement to check" if reqs else "no requirements"
        return out
    items: dict[str, int] = {}
    for i in report.get("items") or []:
        items[str(i.get("status"))] = items.get(str(i.get("status")), 0) + 1
    notes = report.get("notes") or {}

    def _texts(key: str) -> list[str]:
        return [f"{n.get('requirement_id')}: {n.get('text')}" for n in notes.get(key) or [] if isinstance(n, dict)][:20]

    out.update({
        "items": items,
        "unrequested": len(report.get("unrequested") or []),
        "problems": report_problems(report)[:5],
        "notes": {"covered": _texts("covered"), "uncovered": _texts("uncovered"), "unknown": _texts("unknown")},
        "step_sha256": report.get("step_sha256"),
        "requirements_sha256": report.get("requirements_sha256"),
        "validator_version": report.get("validator_version"),
        "mode": report.get("mode"),
    })
    if report.get("error"):
        out["error"] = report["error"]
    return out


def _now_step(kind: str, ok: bool = True, **detail: Any) -> dict[str, Any]:
    """A step in the handler format (NodeContext.step)."""
    return {"kind": kind, "ok": ok, "at": datetime.now(UTC).isoformat(), "detail": detail}


SKIPPED_MODEL_CHECKS = ("model.grounding", "model.gate", "model.conformance")


def _field_value(req: dict[str, Any], field_name: str) -> Any:
    """The value a requirement states for one canonical field (``size.x``, ``through/depth``, ``entries`` …)."""
    if field_name.startswith("size."):
        size = req.get("size") or [None, None, None]
        return size["xyz".index(field_name[-1])] if len(size) == 3 else None
    if field_name == "through/depth":
        return {"through": req.get("through"), "depth": req.get("depth")}
    return req.get(field_name)


_VALUE_UNIT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(mm|deg|°|pcs)\s*$")


def turbo_assumptions(requirements: list[dict[str, Any]] | None, model_lines: list[str] | None) -> list[dict[str, Any]]:
    """Assumption records (``app.policy.Assumption``) of a Turbo modeling result: every requirement field the model
    listed in ``assumed`` (the value the requirement holds, with the trigger, basis and affected ids of its decision
    line), every critical field it left empty (trigger ``missing``, no value: kept and shown with that problem), then
    any other line — a decision line about another object as a decision, anything else as a note. Never a user
    confirmation. Decision lines follow ``ai/turbo.MODELING_TURBO_BLOCK`` (:func:`app.ai.turbo.parse_decision_line`); a
    trigger the model did not write stays empty, never guessed (generation supplement D-02)."""
    from ..ai.turbo import parse_decision_line
    from ..cad.requirements import CRITICAL_FIELDS, _missing_fields, parse_requirements

    lines = [str(x) for x in model_lines or [] if str(x).strip()]
    parsed_lines = [parse_decision_line(line) for line in lines]
    used: set[int] = set()
    out: list[dict[str, Any]] = []

    def line_for(subject: str) -> dict[str, Any]:
        for i, p in enumerate(parsed_lines):
            if i not in used and p is not None and p["subject"] == subject:
                used.add(i)
                return p
        # lines about parts of the field ("R2.outer.center = …" for R2.outer, BHD1 real run): one decision, their reasons joined
        finer = [i for i, p in enumerate(parsed_lines) if i not in used and p is not None and str(p["subject"]).startswith(subject + ".")]
        if finer:
            used.update(finer)
            ps = [parsed_lines[i] for i in finer]
            return {"basis": "; ".join(f"{str(p['subject'])[len(subject) + 1:]}: {p['basis']}" for p in ps if p and p["basis"])[:500],
                    "trigger": next((p["trigger"] for p in ps if p and p["trigger"]), None),
                    "affected": list(dict.fromkeys(a for p in ps if p for a in p["affected"]))}
        for i, line in enumerate(lines):  # a line that starts with the subject but is not in the decision form
            if i not in used and line.startswith(subject):
                used.add(i)
                return {"basis": line[:500], "trigger": None, "affected": []}
        return {"basis": "", "trigger": None, "affected": []}

    def lengths(v: Any) -> bool:  # sizes, positions, depths (mm); never a flag
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            return True
        return isinstance(v, list) and bool(v) and all(lengths(x) for x in v)

    def add(subject: str, field_name: str, adopted: Any, basis: str, affected: list[str], trigger: Any = None,
            unit: str | None = None) -> None:
        out.append(exec_policy.Assumption(id=f"A-{len(out) + 1:02d}", subject=subject, field=field_name, adopted=adopted,
                                          unit=unit or ("mm" if lengths(adopted) else None), basis=str(basis or "")[:500],
                                          affected=list(dict.fromkeys(str(x)[:80] for x in affected if str(x).strip()))[:50],
                                          author="ai_turbo", trigger=trigger).model_dump(mode="json"))

    try:
        parsed = parse_requirements(list(requirements or []))
    except Exception:  # noqa: BLE001 - a list that does not parse has no field-level assumptions to report
        parsed = []
    for req in parsed:
        data = req.model_dump(mode="json")
        for f in data.get("assumed") or []:
            ln = line_for(f"{req.id}.{f}")
            add(f"{req.id}.{f}", f, _field_value(data, f), ln["basis"], [req.id, *ln["affected"]], ln["trigger"])
        for f in sorted(_missing_fields(req)):
            if f in CRITICAL_FIELDS.get(req.kind, ()) and not any(a["subject"] == f"{req.id}.{f}" for a in out):
                add(f"{req.id}.{f}", f, None, "the requirement leaves this critical field empty and the reply recorded no value for it",
                    [req.id], "missing")
    for i, line in enumerate(lines):
        if i in used or line.startswith("simulated reply"):
            continue
        p = parsed_lines[i]
        if p is not None and p["value"]:
            m = _VALUE_UNIT.match(str(p["value"]))
            value: Any = float(m.group(1)) if m else p["value"]
            add(str(p["subject"])[:200], str(p["subject"]).split(".", 1)[1][:120], value, p["basis"], list(p["affected"]), p["trigger"],
                unit=m.group(2) if m else None)
        else:
            add("model", "note", None, line[:500], [])
    return out


def run_modeling(
    db: Session,
    *,
    provider: ModelProvider,
    cfg: ModelConfig | None,
    part: Part,
    user: User,
    base: ModelRevision | None,
    text: str,
    answers: list[dict[str, str]] | None = None,
    context: str | None = None,
    private: dict[str, Any] | None = None,
    node_run_id: uuid.UUID | None = None,
    drawing: DrawingInput | None = None,
    source_file_version_id: uuid.UUID | None = None,
    extra_checks: dict[str, Any] | None = None,
    note: str = "",
    policy: str | None = None,
    design_context: DesignDefinitionContext | None = None,
) -> ModelingResult:
    """Run the modeling agent for ``part`` (create when ``base`` is None, else modify ``base``) and save the
    revision when the design is built and its check did not fail. Runs inside the caller's transaction; the
    caller records ``result.calls`` and maps ``result.status`` / ``result.error`` to its own outcome.

    Reconstruct (``drawing`` given, ``base`` None): ``text`` is the user's optional notes; the revision gets
    ``source_file_version_id`` and ``extra_checks`` (source provenance, evidence frame) next to the check.

    ``policy`` is the caller's frozen execution policy (run / operation / tool call; None = standard). Turbo
    (W7): grounding, the critical gate and the conformance check are skipped (``model.grounding``, ``model.gate``,
    ``model.conformance`` recorded as ``skipped_turbo`` in ``revision.checks``; never called), the agent adopts
    missing values instead of asking (``result.assumptions``), and a real built solid with STEP bytes is still
    required (a kernel failure stays a failure).

    Two phases: :func:`design` (model calls and kernel builds, no database) then :func:`save` (the revision). A caller
    that models several parts at once runs :func:`design` in worker threads and :func:`save` in the thread that owns the
    session (``orchestration.build_assembly``)."""
    current = current_model(part, base)
    part_snapshot = PartBrief.of(part)
    if design_context is not None:
        from ..ai import prompts
        from ..ai.modeling_agent import agent_reply_json_schema, turbo_reply_json_schema
        from ..ai.turbo import MODELING_TURBO_BLOCK, MODELING_TURBO_SYSTEM
        from ..cad.requirements import DESIGN_MODELING_SYSTEM
        from .assistant_context import _budget, estimate_tokens

        system = prompts.MODELING_SYSTEM_PROMPT + "\n\n" + DESIGN_MODELING_SYSTEM
        user_prompt = prompts.modeling_user_message(text, current, part_name=part.name, material=part.material,
            context=context, data=(private or {}).get("data"), constraints=(private or {}).get("constraints"), answers=answers)
        user_prompt += design_context.prompt()
        turbo = exec_policy.is_turbo(policy)
        if turbo:
            system += "\n\n" + MODELING_TURBO_SYSTEM
            user_prompt += "\n\n" + MODELING_TURBO_BLOCK
        schema = turbo_reply_json_schema() if turbo else agent_reply_json_schema()
        required = estimate_tokens(system + user_prompt + json.dumps(schema, ensure_ascii=False)) + 256
        input_limit, _, _, available, limit_source = _budget(cfg)
        if required > available:
            return ModelingResult(status="failed", label=label_of(provider, cfg), error={"code": "context_over_budget",
                "retryable": False, "message": "the complete adopted design and modeling context exceed the model input budget",
                "details": {"required_est": required, "available": available, "input_limit": input_limit,
                            "limit_source": limit_source, "estimated": True}})
    res = design(provider=provider, cfg=cfg, part=part_snapshot, text=text, current=current,
                 base_bytes=rev_svc.base_bytes(db, current.spec) if current is not None else None, answers=answers, context=context,
                 private=private, drawing=drawing, policy=policy, design_context=design_context)
    if design_context is not None and res.design is not None:
        res.design.part_snapshot = part_snapshot
    return save(db, res, part=part, user=user, base=base, node_run_id=node_run_id, source_file_version_id=source_file_version_id,
                extra_checks=extra_checks, note=note)


def design(
    *,
    provider: ModelProvider,
    cfg: ModelConfig | None,
    part: PartBrief,
    text: str,
    current: CurrentModel | None = None,
    base_bytes: bytes | None = None,
    answers: list[dict[str, str]] | None = None,
    context: str | None = None,
    private: dict[str, Any] | None = None,
    drawing: DrawingInput | None = None,
    policy: str | None = None,
    source_contexts: tuple[DefinitionSourceContext, ...] = (),
    design_context: DesignDefinitionContext | None = None,
) -> ModelingResult:
    """The model + kernel phase of :func:`run_modeling`: the modeling agent (Turbo: its one decide request), the builds
    and checks of its designs and the build of the final spec. No database and no ORM object: ``part`` and ``current``
    are plain values and ``base_bytes`` the imported base of ``current`` (read by the caller), so this may run in a worker
    thread. A design that is built and passed its check comes back ``built`` with :attr:`ModelingResult.design` set and
    no revision yet (:func:`save` stores it); every other result is final."""
    pol = exec_policy.parse_policy(policy)
    turbo = exec_policy.is_turbo(pol)
    offset = part.op_counter if current is None and part.op_counter > 0 else 0
    last: dict[str, Any] = {}

    def kernel_build(spec: ModelSpec) -> Any:  # a spec's base is the current model's (modeling_agent keeps it)
        return rev_svc.kernel_build(spec, base_bytes if spec.base is not None else None)

    def build(spec: ModelSpec) -> dict[str, Any]:
        # Build exactly what will be saved (a fresh design on a part with history gets its ids shifted),
        # so the checked STEP bytes are the stored ones.
        final = renumber(spec, offset) if offset else spec
        result = kernel_build(final)
        last["result"] = result
        return {"ok": result.ok, "errors": [e.model_dump() for e in result.errors],
                "warnings": [w.model_dump() for w in result.warnings],
                "metrics": result.metrics.model_dump() if result.metrics else None}

    def check(spec: ModelSpec, reqs: list[Any], mode: str) -> dict[str, Any] | None:
        result = last.get("result")
        if result is None or not result.ok or not result.artifacts.step:
            return None
        step = result.artifacts.step
        return exec_policy.run_check("model.conformance", pol, lambda: rev_svc.check_step(step, reqs, mode=mode))

    private = private or None
    earlier: tuple[list[dict[str, Any]], list[Any]] = ([], [])  # steps / calls of a Turbo reply that asked (D-01)
    decide: list[str] | None = None
    engine = get_engine()
    while True:
        try:
            outcome = engine.generate(provider, ModelingInput(
                text=text, current=current, answers=answers, part_name=part.name, material=part.material,
                context=context or None, data=(private or {}).get("data") or None,
                constraints=(private or {}).get("constraints") or None, drawing=drawing, policy=pol, decide=decide,
                source_contexts=source_contexts, design_context=design_context,
            ), build=build, check=None if turbo else check)
        except ProviderError as exc:
            partial = getattr(exc, "partial", None)
            steps = earlier[0] + ([s.model_dump() for s in partial.steps] if partial is not None else [])
            calls = earlier[1] + (list(partial.calls) if partial is not None else [])
            return ModelingResult(status="failed", label=label_of(provider, cfg), steps=steps, calls=redact_calls(calls) if private else calls,
                                  outcome=partial, error={"kind": "provider", "code": exc.code, "message": exc.message,
                                                          "retryable": exc.retryable, "failure_class": exc.failure_class,
                                                          "details": {}})
        if not (turbo and outcome.status == "needs_clarification" and decide is None):
            break
        # Turbo never waits for a person (generation supplement D-01): ask exactly once more, every question turned into
        # "decide this yourself and record it". The first attempt's steps and calls stay in the result.
        decide = list(outcome.questions) or ["The previous reply asked for clarification without a question: adopt every open value."]
        earlier = (earlier[0] + [s.model_dump() for s in outcome.steps] + [_now_step("turbo_decide", questions=decide[:10])],
                   earlier[1] + list(outcome.calls))
    calls = earlier[1] + list(outcome.calls)
    calls = redact_calls(calls) if private else calls
    steps = earlier[0] + [s.model_dump() for s in outcome.steps]
    label = label_of(provider, cfg, outcome.simulated)
    res = ModelingResult(status=outcome.status, label=label, steps=steps, calls=calls, outcome=outcome)

    if outcome.status == "needs_clarification" and turbo:  # it asked again after the one decide request: protocol error
        questions = list(outcome.questions or decide or [])
        res.status = "failed"
        res.error = {"kind": "outcome", "code": exec_policy.TURBO_CLARIFICATION_REFUSED, "retryable": False,
                     "message": "Turbo: the model still asked after it was told to decide and record the values itself: "
                                + "; ".join(clip(q, 160) for q in questions[:5]),
                     "details": {"questions": [clip(q, 300) for q in questions[:10]], "decide_requests": 1, "protocol_error": True,
                                 "recovery": exec_policy.PROTOCOL_RECOVERY}}
        return res
    if outcome.status == "needs_clarification":
        res.questions, res.gaps = list(outcome.questions), list(outcome.gaps)
        return res
    if outcome.status == "unsupported":
        reason = (outcome.error or {}).get("message") or "the part is out of scope"
        res.error = {"kind": "outcome", "code": UNSUPPORTED, "message": reason, "retryable": False,
                     "details": {"outcome": UNSUPPORTED, "reason": reason}}
        return res
    if outcome.status != "built" or outcome.spec is None:
        err = outcome.error or {}
        code = err.get("code", "ai_failed")
        details: dict[str, Any] = {"errors": outcome.errors}
        if code == REQUIREMENTS_NOT_MET:
            res.summary = details["check"] = check_summary(outcome.requirements, outcome.check)
        res.status = "failed"
        res.error = {"kind": "outcome", "code": code,
                     "message": err.get("message") or "; ".join(outcome.errors) or "the model could not produce a buildable design",
                     "retryable": bool(err.get("retryable")), "details": details}
        return res

    spec = outcome.spec
    if offset:
        spec = renumber(spec, offset)
        steps.append(_now_step("renumber_ops", offset=offset))
    outcome.spec = spec
    report = outcome.check
    result = last.get("result")
    # Reuse the agent's last build only if it is exactly this spec (it is: the build callback already
    # built the renumbered spec); otherwise build again and check the bytes that will be stored.
    if result is None or not result.ok or result.build_key != build_key(spec, result.kernel_version):
        result = kernel_build(spec)
        if result.ok and report is not None:
            try:
                step_bytes = result.artifacts.step
                report = exec_policy.run_check("model.conformance", pol,
                                               lambda: rev_svc.check_step(step_bytes, outcome.requirements, mode=outcome.mode))
            except Exception as exc:  # noqa: BLE001 - a checker crash fails the request, it never saves unchecked
                report = {"status": "not_evaluated", "error": clip(f"{type(exc).__name__}: {exc}", 400), "items": [],
                          "unrequested": []}
            error = checker_error(report)
            if report.get("status") == "failed" or error is not None:
                problems = report_problems(report) if report.get("status") == "failed" else [
                    f"the independent check could not evaluate the rebuilt solid: {clip(error or '', 300)}"]
                res.status, res.summary = "failed", check_summary(outcome.requirements, report)
                res.error = {"kind": "outcome", "code": REQUIREMENTS_NOT_MET, "retryable": False,
                             "message": "the rebuilt model does not meet the stated requirements: " + "; ".join(problems[:3]),
                             "details": {"errors": problems, "check": res.summary}}
                return res
    res.design = Design(spec=spec, result=result, report=report, offset=offset, policy=pol, design_context=design_context,
                        request_text=text or None if drawing is not None else text)
    return res


def save(
    db: Session,
    res: ModelingResult,
    *,
    part: Part,
    user: User,
    base: ModelRevision | None,
    node_run_id: uuid.UUID | None = None,
    source_file_version_id: uuid.UUID | None = None,
    extra_checks: dict[str, Any] | None = None,
    note: str = "",
) -> ModelingResult:
    """The database phase of :func:`run_modeling`: the revision of the design :func:`design` built, in the caller's
    transaction (and thread). A result without a pending design is final and returned as it is; a refused save is
    ``failed`` (kind service)."""
    d, outcome = res.design, res.outcome
    if d is None or outcome is None:
        return res
    res.design = None
    turbo = exec_policy.is_turbo(d.policy)
    try:
        if d.part_snapshot is not None:
            # A user may edit metadata while the provider works. Compare under the same lock used by
            # create_revision so its refreshed material cannot contradict the adopted design at save time.
            locked = rev_svc.lock_part(db, part.id)
            if (locked.name, locked.material) != (d.part_snapshot.name, d.part_snapshot.material):
                raise AppError(409, "design_part_metadata_changed", "the part name or material changed during modeling; create a new design request")
        checks: dict[str, Any] = {"conformance": d.report, "requirements_sha256": outcome.requirements_sha256, "mode": outcome.mode,
                                  "repairs": outcome.repairs, **(extra_checks or {})}
        if d.design_context is not None:
            checks["design"] = d.design_context.provenance()
        ai_summary: dict[str, Any] = {"changes": outcome.changes, "assumptions": outcome.assumptions,
                                      "diff": outcome.diff.model_dump() if outcome.diff else None, **res.label}
        if turbo:
            # Real build required (create_revision refuses a failed build); the business checks are recorded as
            # skipped, never as passed, next to the values adopted (reconstruct: the sheet / units first).
            res.assumptions = [*(checks.pop("drawing_assumptions", None) or []),
                               *turbo_assumptions(outcome.requirements, outcome.assumptions)]
            # model.conformance goes through run_check (skipped_turbo, never called) so the checks ledger sees the skip;
            # grounding / gate were met (and skipped) by the agent itself
            checks.update({"conformance": exec_policy.run_check("model.conformance", d.policy, dict),
                           "grounding": exec_policy.skipped("model.grounding"), "gate": exec_policy.skipped("model.gate"),
                           "execution_policy": d.policy, "assumptions": res.assumptions})
            ai_summary["execution_policy"] = d.policy
        rev = rev_svc.create_revision(
            db, part_id=part.id, user=user, spec=d.spec, source="ai", parent_id=base.id if base else None,
            request_text=d.request_text, note=note,
            ai_summary=ai_summary, node_run_id=node_run_id, result=d.result, requirements=outcome.requirements,
            source_file_version_id=source_file_version_id, checks=checks,
            template=_template_meta(outcome, d.offset),  # W3: part template provenance
            execution_policy=d.policy,  # W3 feature checks follow the frozen policy (skipped_turbo under Turbo)
        )
    except AppError as exc:
        res.status = "failed"
        res.error = {"kind": "service", "code": exc.code, "message": exc.message, "retryable": False, "details": dict(exc.details or {})}
        return res
    res.revision = rev
    res.summary = turbo_summary(outcome.requirements) if turbo else check_summary(outcome.requirements, d.report)
    return res


def turbo_summary(requirements: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The public check summary of a Turbo revision: requirement counts and ``skipped_turbo`` (never a verdict)."""
    out = check_summary(requirements, None)
    out.update({"status": "skipped_turbo", "reason": "Turbo: the requirement check was skipped",
                "skipped": [exec_policy.skipped(c) for c in SKIPPED_MODEL_CHECKS]})
    return out
