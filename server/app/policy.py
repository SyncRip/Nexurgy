"""Execution policy shared by every entry point (W0 contract §2, architecture §3).

``standard`` is the default and keeps every existing gate. ``turbo`` skips the *business* checks (engineering
quality, requirement conformance, drawing quality, agent review, human approval, final acceptance) but never
permissions, project isolation, graph / structure validity, real file existence, tool execution or real build /
conversion success.

Fresh requests use the current project default; an explicit value is accepted only at feature-guarded entry points.
The source version remains provenance, not a new default. The result and project version are frozen before context
building / planning, then inherited unchanged by child, retry and wake operations.

Business checks go through :func:`run_check` so a skipped check is recorded as ``skipped_turbo`` (never as
passed) and tests can prove with :mod:`app.checks_ledger` that the checker was not called.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import checks_ledger

ExecutionPolicy = Literal["standard", "turbo"]
STANDARD: ExecutionPolicy = "standard"
TURBO: ExecutionPolicy = "turbo"
POLICIES: tuple[str, ...] = (STANDARD, TURBO)

PolicySource = Literal["explicit", "inherited", "project_default", "default"]
#: Where a frozen policy came from (design §3.4), in priority order.
POLICY_SOURCES: tuple[str, ...] = ("explicit", "inherited", "project_default", "default")

#: A step inside a Turbo chain answered "needs_clarification". Values adopted upstream are known facts and Turbo never
#: waits for a person, so this is a protocol error of that step (the run fails it), never a question to the user.
TURBO_CLARIFICATION_REFUSED = "turbo_clarification_refused"
#: Its recovery code (design §3.1): "continue" would ask again, so it is not offered; the person states the missing
#: value (spec / request) or runs the step in standard, where the question reaches a person.
PROTOCOL_RECOVERY = "run_standard"

CheckStatus = Literal["not_requested", "skipped_turbo", "pending", "passed", "failed", "unknown"]
AcceptanceStatus = Literal["not_requested", "pending", "accepted", "rejected"]


def parse_policy(value: str | None) -> ExecutionPolicy:
    """None / empty → standard. Anything else must be a known policy (no silent fallback)."""
    if value is None or value == "":
        return STANDARD
    if value not in POLICIES:
        raise ValueError(f"unknown execution_policy {value!r}")
    return value  # type: ignore[return-value]


def is_turbo(policy: str | None) -> bool:
    return policy == TURBO


@dataclass(frozen=True)
class PolicyChoice:
    """A request's frozen execution policy and where it came from (design §3.4).

    ``source_version`` is the exact version the request continues from (``{type, id, label, execution_policy}``), kept
    whenever there is one, also when the person picked another policy explicitly (the UI explains that consequence)."""

    policy: ExecutionPolicy
    source: PolicySource
    source_version: dict[str, Any] | None = None
    project_version: int | None = None

    def out(self) -> dict[str, Any]:
        return {"execution_policy": self.policy, "policy_source": self.source, "source_version": self.source_version,
                "project_version": self.project_version}


def parse_source(value: str | None) -> PolicySource | None:
    """A stored ``policy_source`` (None for rows from before S5); an unknown value is no source, never a guess."""
    return value if value in POLICY_SOURCES else None  # type: ignore[return-value]


def resolve_policy(explicit: str | None, *, source_version: dict[str, Any] | None = None,
                   project_default: str | None = None, project_version: int | None = None) -> PolicyChoice:
    """Resolve fresh work: feature-guarded explicit value, otherwise current project default.

    Source policy is provenance only. Continuations use frozen_choice, never this resolver."""
    if explicit not in (None, ""):
        return PolicyChoice(parse_policy(explicit), "explicit", source_version, project_version)
    # Fresh work takes the current project default. Source policy is provenance only;
    # retry/child callers use frozen_choice instead of resolving again.
    if project_default in POLICIES:
        return PolicyChoice(parse_policy(project_default), "project_default", source_version, project_version)
    return PolicyChoice(STANDARD, "default", source_version, project_version)


def frozen_choice(policy: str | None, source: str | None, source_version: dict[str, Any] | None = None, *,
                  project_version: int | None = None) -> PolicyChoice:
    """A choice resolved earlier (e.g. frozen on a workspace operation) passed on unchanged to the run it starts."""
    return PolicyChoice(parse_policy(policy), parse_source(source) or "explicit", source_version, project_version)


def turbo_clarification_error(questions: list[str] | None = None, *, step: str = "", stage: str = "modeling") -> dict[str, Any]:
    """The failure a Turbo chain records when a downstream step still asks (:data:`TURBO_CLARIFICATION_REFUSED`): a
    protocol error of that step in the shared failure shape (design §3.1), not retryable by "continue"
    (:data:`PROTOCOL_RECOVERY`), never turned into a question for a person."""
    return {"stage": stage, "code": TURBO_CLARIFICATION_REFUSED, "retryable": False, "recovery": PROTOCOL_RECOVERY, "protocol_error": True,
            "message": (f"{step}: " if step else "") + "asked for clarification inside a Turbo chain; upstream values are known "
                       "facts and Turbo never waits for answers",
            "questions": [str(q)[:300] for q in (questions or [])][:10]}


def skipped(check: str) -> dict[str, Any]:
    """The record a skipped business check leaves behind. Never contains a verdict."""
    return {"check": check, "status": "skipped_turbo"}


def run_check(check: str, policy: str | None, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a business check under standard; under turbo return :func:`skipped` without calling ``run``.

    ``check`` is a stable dotted name (e.g. ``"model.conformance"``, ``"assembly.interference"``,
    ``"drawing.annotation"``); the ledger records it whenever the checker really runs, and (scopes only) each skip.
    """
    if is_turbo(policy):
        checks_ledger.record_skipped(check)
        return skipped(check)
    checks_ledger.record(check)
    return run()


# ---------------------------------------------------------------------------------------------------------------------
# Turbo boundary and object states (generation reliability N, .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.8)
# ---------------------------------------------------------------------------------------------------------------------

#: The business checks Turbo closes (W0 §2.1 check names; ``geometry.feature.*`` stands for every feature kind). Under
#: Turbo none of them is called: :func:`run_check` returns ``skipped_turbo``. Generation failure detection is not among
#: them and stays on (a failed build, no geometry, a target format not written, an empty or mislabelled DWG).
TURBO_CLOSED_CHECKS: tuple[str, ...] = (
    "model.grounding", "model.gate", "model.conformance", "geometry.feature.*",
    "drawing.annotation", "drawing.dwg_semantic",
    "assembly.interference", "assembly.clearance", "assembly.mate_residual", "assembly.bom", "assembly.spec",
    "drafting.rules", "spec.clarification_gate",
    "review.agent", "review.human", "acceptance.final",
)


def closed_checks(policy: str | None) -> list[str]:
    """The checks ``policy`` closes: all of :data:`TURBO_CLOSED_CHECKS` under Turbo, none under standard."""
    return list(TURBO_CLOSED_CHECKS) if is_turbo(policy) else []


def is_closed(check: str, policy: str | None) -> bool:
    """``check`` is one ``policy`` closes (a ``prefix.*`` entry covers every name under the prefix)."""
    return any(check.startswith(c[:-1]) if c.endswith(".*") else check == c for c in closed_checks(policy))


#: What a generated object is (design §2.8), shown apart from its check status (CHECK_STATUSES), never in its place:
#: verified (generated, every required verification passed with evidence), generated (verification not required or not
#: run), generated_ai (generated with AI decisions nobody confirmed yet: review advised — information, not a failure),
#: partial (some of it exists, the rest can be continued), blocked (not generated or the run was stopped), display_only
#: (an instance only arranged for viewing), simulated (from a simulated provider, real verification still to come).
OBJECT_STATES: tuple[str, ...] = ("verified", "generated", "generated_ai", "partial", "blocked", "display_only", "simulated")


def object_state(*, generated: bool, complete: bool = True, display_only: bool = False, simulated: bool = False,
                 ai_decisions: int = 0, check_status: str | None = None, unverified: bool | None = None) -> str:
    """The state of one object (a delivery level entry, a whole delivery), design §2.8.

    ``generated``: its files / geometry exist. ``complete``: every required part of it exists (all its required formats,
    in the required protocol; an assembly holds its whole target). ``display_only``: an instance only arranged for
    viewing. ``simulated``: made from a simulated provider. ``ai_decisions``: AI / rule decisions it rests on that no
    person confirmed. ``check_status`` / ``unverified``: its verification (CHECK_STATUSES; the verification block's flag).

    Order: display_only > blocked > partial > simulated > generated_ai > verified > generated. A failed or skipped check
    never changes the state (it is shown on its own); only passed checks with nothing unconfirmed make it verified."""
    if display_only:
        return "display_only"
    if not generated:
        return "blocked"
    if not complete:
        return "partial"
    if simulated:
        return "simulated"
    if ai_decisions > 0:
        return "generated_ai"
    if check_status == "passed" and not unverified:
        return "verified"
    return "generated"


class Assumption(BaseModel):
    """A value Turbo (or an explicit person decision) adopted for a missing / conflicting fact.

    Shared shape for specs (W2), assemblies (W4), drafting (W6) and runtime (W7). ``author`` separates AI
    assumptions from person answers; an assumption is never shown as a confirmation.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # stable within its owner, e.g. "A-01"
    subject: str  # what it is about: fact id, object key or "<component>.<field>"
    field: str = ""
    adopted: Any = None
    unit: str | None = None
    basis: str = ""  # why this value (rule, source priority, typical practice)
    alternatives: list[Any] = Field(default_factory=list)
    affected: list[str] = Field(default_factory=list)  # object / branch keys the value flows into
    sources: list[str] = Field(default_factory=list)  # evidence locators the decision looked at
    author: Literal["ai_turbo", "ai_design", "rule", "person"] = "ai_turbo"
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Generation supplement D-02: why a decision was needed (None on records from before; never guessed afterwards).
    trigger: Literal["missing", "ambiguous", "conflict"] | None = None


#: Assumption authors whose values stay unconfirmed until a person confirms them (W0 §2.2).
NEEDS_CONFIRMATION: tuple[str, ...] = ("ai_turbo", "ai_design", "rule")


def confirmed(assumption: dict[str, Any]) -> bool:
    """A person's decision is recorded on the assumption (``confirmation.author == "person"``): a W6 version carries it
    in its own content; model revisions and drawings keep append-only confirmation records instead."""
    c = assumption.get("confirmation")
    return isinstance(c, dict) and c.get("author") == "person"


def needs_confirmation(assumption: dict[str, Any]) -> bool:
    """An AI / rule assumption without a person's decision (an entry without an author counts as AI)."""
    return (assumption.get("author") or "ai_turbo") in NEEDS_CONFIRMATION and not confirmed(assumption)


def assumption_ref(assumption: dict[str, Any], owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """The short form listed in ``verification.unconfirmed_assumptions``."""
    out = {k: assumption.get(k) for k in ("id", "subject", "field", "adopted", "unit", "author")}
    if owner is not None or assumption.get("owner") is not None:
        out["owner"] = owner if owner is not None else assumption.get("owner")
    return out


def verification(policy: str | None, check_status: CheckStatus, acceptance_status: AcceptanceStatus = "not_requested",
                 assumptions: list[dict[str, Any]] | None = None, *, pending_checks: list[str] | None = None,
                 unconfirmed_assumptions: list[dict[str, Any]] | None = None,
                 upstream_unverified: dict[str, Any] | None = None) -> dict[str, Any]:
    """The verification block every derived artifact carries (model / assembly / drawing / BOM / delivery).

    An artifact is verified only when (a) every business check passed (a check skipped at creation counts only once a
    later standard run passed: ``pending_checks`` lists the rest) and (b) every ``ai_turbo`` / ``rule`` assumption it
    carries was confirmed by a person (``unconfirmed_assumptions``; default: the assumptions without a person's
    confirmation). A derived artifact is unverified while an upstream artifact is (``upstream_unverified``). Turbo
    output is always unverified at creation. Other provenance flags (estimated, simulated, assumed) are kept by the
    artifact itself.
    """
    pol = parse_policy(policy)
    items = list(assumptions or [])
    if unconfirmed_assumptions is None:
        unconfirmed_assumptions = [assumption_ref(a) for a in items if isinstance(a, dict) and needs_confirmation(a)]
    out = {
        "execution_policy": pol,
        "check_status": check_status,
        "acceptance_status": acceptance_status,
        "unverified": check_status != "passed" or bool(unconfirmed_assumptions) or bool(upstream_unverified),
        "assumptions": items,
        "pending_checks": list(pending_checks or []),
        "unconfirmed_assumptions": list(unconfirmed_assumptions),
    }
    if upstream_unverified:
        out["upstream_unverified"] = upstream_unverified
    return out


# ---------------------------------------------------------------------------------------------------------------------
# AI decisions (generation supplement D-02, .trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.3): every AI / rule
# assumption read as one decision — object, trigger, adopted value, basis, impact, source and state — so specs,
# assemblies, drawings and deliveries list them the same way: "AI decision: adopted value; rationale: why; impact: affected areas".
# ---------------------------------------------------------------------------------------------------------------------

#: Why a decision was needed.
DECISION_TRIGGERS: tuple[str, ...] = ("missing", "ambiguous", "conflict")
#: Where a decision can be used (generation reliability §2.6, ``consumed_by``).
CONSUMER_STAGES: tuple[str, ...] = ("spec", "modeling", "placement", "drawing", "export")
#: Phrases that name no decision ("handled as usual", "decided by the built geometry", "AI completed it"): a basis made
#: only of them is reported as ``generic_basis``.
_GENERIC_BASIS = re.compile(r"按常规|常规处理|由建成几何决定|由几何决定|AI ?已?自动补全|已自动补全|自动处理|as usual|by convention|"
                            r"decided by the (built )?geometry|auto[- ]?(completed|filled)|handled automatically", re.IGNORECASE)


#: Placeholder "values" that decide nothing (package D: a decision adopting "TBD" / "pending" has no value).
_PLACEHOLDER_VALUES = {"tbd", "tba", "n/a", "unknown", "待定", "未定", "未知", "待确认"}


def decision_problems(assumption: dict[str, Any]) -> list[str]:
    """What keeps a recorded decision from being readable and checkable: ``no_value`` (nothing adopted, or only a
    placeholder such as "TBD" / "pending"), ``no_basis`` / ``generic_basis`` (no specific reason), ``no_subject``.
    Informational: the record is kept as it is."""
    out: list[str] = []
    if not str(assumption.get("subject") or "").strip():
        out.append("no_subject")
    adopted = assumption.get("adopted")
    if adopted in (None, "", [], {}) or (isinstance(adopted, str) and adopted.strip().casefold() in _PLACEHOLDER_VALUES):
        out.append("no_value")
    basis = str(assumption.get("basis") or "").strip()
    if not basis:
        out.append("no_basis")
    elif _GENERIC_BASIS.search(basis) and len(_GENERIC_BASIS.sub("", basis).strip(" ，。,.;；:：-")) < 12:
        out.append("generic_basis")
    return out


def decision_state(assumption: dict[str, Any], decided: dict[str, Any] | None = None) -> str:
    """``adopted`` (in use, waiting for a person), ``confirmed`` / ``corrected`` (a person decided; ``decided`` is the
    owner's separate decision record when it keeps one), ``replaced`` (a later decision took its place), ``person``
    (a person's own answer, not an AI decision)."""
    if assumption.get("author") == "person":
        return "person"
    if assumption.get("replaced_by"):
        return "replaced"
    rec = decided or (assumption.get("confirmation") if isinstance(assumption.get("confirmation"), dict) else None)
    if rec and rec.get("decision") in ("confirmed", "corrected"):
        return str(rec["decision"])
    return "adopted"


def decision_record(assumption: dict[str, Any], owner: dict[str, Any] | None = None, *,
                    decided: dict[str, Any] | None = None) -> dict[str, Any]:
    """One assumption as a decision (the shape every list of AI decisions uses). ``consumed_by`` (generation
    reliability §2.6): where it was actually used, ``[{stage, object, ref}]`` with ``stage`` one of
    :data:`CONSUMER_STAGES` (a delivery fills it, ``assemblies.decision_consumers``; ``[]`` = not recorded here)."""
    a = assumption
    out = {"id": a.get("id"), "subject": a.get("subject"), "field": a.get("field") or "",
           "trigger": a.get("trigger") if a.get("trigger") in DECISION_TRIGGERS else None,
           "adopted": a.get("adopted"), "unit": a.get("unit"), "basis": a.get("basis") or "",
           "affected": list(a.get("affected") or []), "sources": list(a.get("sources") or []),
           "alternatives": list(a.get("alternatives") or []), "author": a.get("author") or "ai_turbo",
           "state": decision_state(a, decided), "problems": decision_problems(a),
           "consumed_by": [u for u in a.get("consumed_by") or [] if isinstance(u, dict) and u.get("stage") in CONSUMER_STAGES]}
    own = owner if owner is not None else a.get("owner")
    if own is not None:
        out["owner"] = own
    if a.get("basis_kind") in ("stated", "derived", "estimated"):  # generation reliability P: what a placement rests on
        out["basis_kind"] = a["basis_kind"]
    return out


def decisions_summary(assumptions: list[dict[str, Any]], *, decided: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The AI decisions of a product (person answers left out), deduplicated by (owner, id, subject, field):
    ``{count, by_trigger, needs_confirmation, with_problems, items}``. ``decided``: id → a person's decision record."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in assumptions or []:
        if not isinstance(a, dict) or a.get("author") == "person":
            continue
        key = repr((a.get("owner"), a.get("id"), a.get("subject"), a.get("field")))
        if key in seen:
            continue
        seen.add(key)
        items.append(decision_record(a, decided=(decided or {}).get(str(a.get("id")))))
    by_trigger: dict[str, int] = {}
    for d in items:
        k = d["trigger"] or "unstated"
        by_trigger[k] = by_trigger.get(k, 0) + 1
    return {"count": len(items), "by_trigger": by_trigger,
            "needs_confirmation": sum(1 for d in items if d["state"] == "adopted"),
            "with_problems": sum(1 for d in items if d["problems"]), "items": items}


_TRIGGER_ZH = {"missing": "缺失", "ambiguous": "歧义", "conflict": "冲突"}
_STAGE_ZH = {"spec": "规格", "modeling": "建模", "placement": "定位", "drawing": "出图", "export": "导出"}


def _uses_text(d: dict[str, Any]) -> str:
    """"Model P-001 R1 (D-04), place D07.00 A2 (I-04, I-05), export parts/P-001_R1.step" (generation reliability §2.6)."""
    out = []
    for u in d.get("consumed_by") or []:
        o = u.get("object") if isinstance(u.get("object"), dict) else {}
        what = o.get("label") or o.get("path") or o.get("id") or ""
        ref = u.get("ref")
        ref_text = "、".join(str(x) for x in ref) if isinstance(ref, list) else str(ref or "")
        out.append(f"{_STAGE_ZH.get(str(u.get('stage')), str(u.get('stage')))} {what}" + (f"（{ref_text}）" if ref_text else ""))
    return "、".join(out)


def _value_text(d: dict[str, Any]) -> str:
    v = d.get("adopted")
    text = v if isinstance(v, str) else repr(v) if v not in (None, "") else "（未给出采用值）"
    return f"{text} {d['unit']}" if d.get("unit") and d["unit"] not in ("°",) else f"{text}{d.get('unit') or ''}"


def decisions_markdown(summary: dict[str, Any], *, title: str = "AI 判断") -> str:
    """The delivery package's readable list: one line per decision — object, trigger, value, reason, impact, state."""
    lines = [f"# {title}", "", f"共 {summary.get('count', 0)} 项；待人确认 {summary.get('needs_confirmation', 0)} 项。"
             "AI 判断不是人工确认，也不是工程验收。", ""]
    for d in summary.get("items") or []:
        obj = f"{d.get('subject')}" + (f".{d['field']}" if d.get("field") and not str(d.get("subject")).endswith(str(d["field"])) else "")
        trig = _TRIGGER_ZH.get(d.get("trigger") or "", "未注明")
        impact = "、".join(str(x) for x in d.get("affected") or []) or "未注明"
        uses = _uses_text(d)
        lines.append(f"- **{obj}**（{trig}）AI 判断：采用 {_value_text(d)}；原因：{d.get('basis') or '未注明'}；影响：{impact}；"
                     + (f"使用于：{uses}；" if uses else "")
                     + f"状态：{d.get('state')}" + (f"；问题：{', '.join(d['problems'])}" if d.get("problems") else ""))
    return "\n".join(lines) + "\n"
