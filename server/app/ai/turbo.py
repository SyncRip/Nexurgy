"""Turbo prompt variants (W7, docs/development/mechanical-engineering/architecture.zh-CN.md §3).

Turbo is chosen explicitly per request and frozen on the run. The model is told not to stop for engineering
clarification: it adopts a value for every missing / conflicting fact and says so, so the server can record the
choice as an assumption (``app.policy.Assumption``, author ``ai_turbo``) — never as a user confirmation.

Generation supplement D (.trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.3, §3 D): every adopted value is an
AI decision written with its trigger (missing / ambiguous / conflict), a concrete value with its unit, a specific basis
and what it affects (:data:`DECISION_RULES`); modeling writes it as one line per value that
:func:`parse_decision_line` reads back, and a reply that still asks gets exactly one more request in which every
question becomes "decide this yourself and record it" (:func:`modeling_decide_block`).

The blocks are appended to the standard user message (the standard prompts stay byte-identical); the simulated
responders look for :data:`POLICY_TAG` to follow the same contract.
"""

from __future__ import annotations

import re

POLICY_TAG = "<execution_policy>turbo</execution_policy>"
_TAG_RE = re.compile(r"<execution_policy>\s*turbo\s*</execution_policy>")
DECIDE_TAG = "<turbo_decide>"

#: How every Turbo decision is written (generation supplement D-02): what was adopted, why a decision was needed, the
#: specific basis and what it affects. Shared by the modeling, spec resolve and drafting prompts.
DECISION_RULES = """Every adopted value is an AI decision a person will read as "AI 判断：采用 …；原因：…；影响：…":
- trigger: "missing" (no source states it), "ambiguous" (it can be read more than one way) or "conflict" (sources state
  different values).
- the value: concrete, with its unit (mm, deg, pcs, a material grade, a model number); never empty, never "TBD".
- basis: the specific reason — the stated fact, peer value, standard or arithmetic it follows (e.g. "M6 clearance hole,
  ISO 273 medium series = 6.6 mm", "the BOM table is the later statement"). Never only "as usual", "by convention",
  "decided by the geometry" or "AI completed it"; never claim a source or standard you did not see.
- affects: the requirement / op / component / instance / entity ids the value flows into.
- A value the user stated without a conflict is kept as stated: never adopt another value for it."""

MODELING_TURBO_BLOCK = POLICY_TAG + """
Turbo execution (chosen by the user for this run): never answer "needs_clarification".
- For every critical fact the request does not state or states inconsistently (overall size, thickness, hole diameter /
  position / through or depth, pocket size / depth, band heights), adopt a reasonable engineering value, use it in the
  ops, write it into the requirement and list the field in that requirement's "assumed".
- Describe every adopted value in "assumptions", one line each, in exactly this form:
  "<requirement id>.<field> = <value with unit>; trigger: missing|ambiguous|conflict; basis: <specific reason>; affects: <ids>"
  e.g. "R2.depth = 8 mm; trigger: missing; basis: blind depth 1.5 × Ø6 leaves a 2 mm floor in the 10 mm plate; affects: R2, F-02".
  Never write that the user confirmed it.
""" + DECISION_RULES + """
- Stated facts keep their evidence. Answer "unsupported" only when a specific feature cannot be built, and name it."""

#: Appended to the modeling / reconstruct system prompt under Turbo (real-model smoke 2026-10-12: with the Turbo rules only in
#: the user message the model kept answering "needs_clarification", even after the decide request).
MODELING_TURBO_SYSTEM = """Execution policy for this request: Turbo. It overrides every rule above that says to ask: the reply
schema has no "needs_clarification". For each critical fact that is missing, ambiguous or conflicting, adopt one concrete
engineering value, build with it, list the field in the requirement's "assumed" and record the decision in "assumptions"
(trigger, value with unit, specific basis, affects). Every field listed in a requirement's "assumed" has its own decision
line that starts with exactly "<requirement id>.<field> =". Facts the request states are used exactly as stated."""

MODELING_TURBO_SYSTEM += """
For a simple object requested as a whole, preserve the major visible features that make it recognizable and complete.
Do not silently reduce the object to only its main body or envelope. When movement or a multi-part assembly was not
requested, you may choose a connected, one-piece static prototype: join its visible features to the body with finite
overlap, include those features in the ops, and disclose the fixed/fused representation as an assumption. A feature
being separate in a working product is not by itself a reason to omit it from the static model. Keep explicit assembly
or motion requirements intact; those still need the assembly workflow. Simplify fine detail, not the object's essential
visible structure."""

MODELING_TURBO_SYSTEM += """
Repeated visible components must keep their distinct shapes, count and spacing. If a connector is needed to keep a
static prototype connected, make it narrower than the visible components or overlap them with the main body. Do not
replace spaced components with a constant-section bar or cylinder across their whole span; that changes the object's
visible structure rather than merely fixing its movement."""

PLANNING_TURBO_BLOCK = POLICY_TAG + """
Turbo planning (chosen by the user): nothing waits for a person.
- Do not use missing_information: return it empty. For every fact the demand does not state (dimensions, material,
  quantity, tolerances, standards), adopt a reasonable value and record it in adopted_values (field, adopted, unit,
  basis, node_id). The values are labelled as assumptions, never as confirmed facts.
- Plan tasks an agent can execute with the listed tools; a task that needs a physical human action keeps an empty
  tools list (it will be reported as not automatable)."""

EXECUTE_TURBO_RULES = """Execution policy: Turbo (chosen by the user for this run).
- Never report blocked for a missing engineering fact: adopt a reasonable value, use it and name it in the note
  ("assumed: ..."). The CAD tools adopt missing values themselves and record them.
- Report blocked only when a required input or tool really does not exist; that ends the task as failed (nobody answers).
- There is no review or acceptance in this run: your outputs are delivered as "completed, not verified"."""


def is_turbo_text(text: str | None) -> bool:
    """Whether a prompt carries the Turbo policy block (simulated responders)."""
    return bool(text) and bool(_TAG_RE.search(text or ""))


def modeling_decide_block(questions: list[str]) -> str:
    """The one extra Turbo request after a reply asked anyway (generation supplement D-01): every question becomes
    "decide this yourself and record it"; nobody will answer them."""
    items = "\n".join(f"{i}. {' '.join(str(q).split())[:300]}" for i, q in enumerate(questions[:10], 1))
    return (f"{DECIDE_TAG}\nYour previous reply asked these questions. In Turbo nobody answers them: decide each one yourself, "
            "use the value in the ops and the requirements (list the field in \"assumed\") and record it in \"assumptions\" "
            "in the decision form above (trigger, value with unit, basis, affects). Do not answer \"needs_clarification\" again.\n"
            f"{items}\n</turbo_decide>")


def decide_questions(text: str | None) -> list[str]:
    """The questions of a :func:`modeling_decide_block` in a prompt (simulated responders)."""
    m = re.search(r"<turbo_decide>\n.*?\n((?:\d+\. .*\n?)+)</turbo_decide>", text or "", re.DOTALL)
    return [re.sub(r"^\d+\. ", "", ln).strip() for ln in m.group(1).splitlines() if ln.strip()] if m else []


# --- decision lines --------------------------------------------------------------------------------------------------

_LINE_HEAD = re.compile(r"^\s*(?P<subject>[A-Za-z][\w\-]*(?:\[[^\]]*\])?(?:\.[\w\-\[\]/]+)+)\s*=\s*(?P<rest>.+)$", re.DOTALL)
_LABEL = re.compile(r"[;；|]\s*(trigger|basis|reason|affects?|affected)\s*[:：=]\s*", re.IGNORECASE)
_TRIGGER_WORD = re.compile(r"\b(missing|ambiguous|conflict(?:ing)?)\b", re.IGNORECASE)
_SPLIT_IDS = re.compile(r"\s*[,，、;；]\s*|\s+and\s+")


def parse_decision_line(line: str) -> dict[str, object] | None:
    """One Turbo decision line ``"<subject> = <value>; trigger: …; basis: …; affects: …"`` → ``{subject, value, trigger,
    basis, affected}``. The older form ``"<subject> = <value>: <basis>"`` gives subject, value and basis with trigger None
    (never guessed). None when the line names no ``<object>.<field> = …``."""
    m = _LINE_HEAD.match(str(line or ""))
    if not m:
        return None
    parts = _LABEL.split(m.group("rest").strip())
    head, labelled = parts[0].strip(), parts[1:]
    fields: dict[str, str] = {}
    for key, val in zip(labelled[0::2], labelled[1::2]):
        k = key.lower()
        k = "basis" if k == "reason" else "affects" if k.startswith("affect") else k
        fields.setdefault(k, val.strip().rstrip(";；. "))
    value, basis = head, fields.get("basis", "")
    if not labelled:  # older form: "<value>: <basis>"
        for sep in (": ", "：", ":"):
            if sep in head:
                value, _, basis = head.partition(sep)
                break
    trig = _TRIGGER_WORD.search(fields.get("trigger", ""))
    trigger = trig.group(1).lower() if trig else None
    affected = [x.strip() for x in _SPLIT_IDS.split(fields.get("affects", "")) if x.strip()][:50]
    return {"subject": m.group("subject"), "value": value.strip().rstrip(";；").strip(), "trigger": "conflict" if trigger == "conflicting" else trigger,
            "basis": basis.strip(), "affected": affected}
