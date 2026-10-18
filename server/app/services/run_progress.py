"""The execution process of a run as the person follows it: its stages, each stage's work units (spec chunks, part
definitions, …), the model calls behind them and the decisions taken on the way (assumptions, skipped checks, repairs).

Phase 1 of .trellis/tasks/10-17-agent-progress-visibility/design.md: built only from records the run already keeps —
node runs, assembly build steps, the AI call ledger, the spec version and the built revisions. Read only, and metadata
only, like ``GET /runs/{id}/ai-calls``: no prompt or reply text. A unit's work in progress shows once its stage
commits it (an assembly commits each definition as it is built; other stages when they end).
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, load_only

from .. import checks_ledger
from ..ai import diagnostics
from ..ai import retry as call_retry
from ..models import AiCall, AssemblyBuildStep, EngineeringSpecVersion, ModelRevision, NodeRun, Part, WorkflowRun
from . import run_progress_drawings
from .assemblies import spec_tree
from .engineering_spec import quantities

#: Bounds of one view; a run beyond them is cut and says so (``truncated``).
MAX_UNITS = 200
MAX_CALLS = 2000
MAX_DRAWINGS = 200
ACTIVE_NODE = ("pending", "ready", "running", "waiting_human")
_DecisionOwner = tuple[str, str]
_DecisionCopies = dict[tuple[_DecisionOwner, str], _DecisionOwner]
_SeenDecisions = dict[tuple[_DecisionOwner, str], dict[str, Any]]


@dataclass
class _Calls:
    """The model calls of one work unit, added up."""

    count: int = 0
    failed: int = 0
    repairs: int = 0
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    latency_ms: int = 0
    started_at: str | None = None
    last_error: dict[str, Any] | None = None
    purposes: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def add(self, row: Any) -> None:
        self.count += 1
        if row.purpose not in self.purposes:
            self.purposes.append(row.purpose)
        if row.purpose.endswith("_repair"):
            self.repairs += 1
        if not row.simulated:  # simulated calls are never summed as usage
            self.tokens_in = _usage_sum([self.tokens_in, row.tokens_in])
            self.tokens_out = _usage_sum([self.tokens_out, row.tokens_out])
        self.latency_ms += row.latency_ms or 0
        started = _call_started(row)
        if started and (self.started_at is None or started < self.started_at):
            self.started_at = started
        if row.status != "ok":
            self.failed += 1
            self.last_error = _call_error(row.error)
        self.details.append({
            "id": str(row.id), "purpose": row.purpose, "attempt": row.attempt,
            "status": row.status, "simulated": row.simulated, "started_at": started,
            "latency_ms": row.latency_ms, "tokens_in": None if row.simulated else row.tokens_in,
            "tokens_out": None if row.simulated else row.tokens_out,
            "request_id": row.response_request_id or row.error_request_id,
            "error": _call_error(row.error) if row.status != "ok" else None,
        })

    def merge(self, other: _Calls) -> None:
        for key in ("count", "failed", "repairs", "latency_ms"):
            setattr(self, key, getattr(self, key) + getattr(other, key))
        for key in ("tokens_in", "tokens_out"):
            setattr(self, key, _usage_sum([getattr(self, key), getattr(other, key)]))
        self.purposes = list(dict.fromkeys([*self.purposes, *other.purposes]))
        self.details = sorted([*self.details, *other.details], key=lambda c: (c["started_at"] or "", c["id"]))
        self.started_at = self.details[0]["started_at"] if self.details else None
        self.last_error = next((c["error"] for c in reversed(self.details) if c["error"]), None)

    @property
    def status(self) -> str:
        return "failed" if self.details and self.details[-1]["status"] != "ok" else "done"

    def out(self, *, details: bool = True) -> dict[str, Any]:
        return {"count": self.count, "failed": self.failed, "repairs": self.repairs, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "latency_ms": self.latency_ms, "started_at": self.started_at,
                "purposes": self.purposes, "last_error": self.last_error, "details": self.details if details else []}


def view(db: Session, run: WorkflowRun) -> dict[str, Any]:
    """The progress view of ``run`` (caller checked access, as for the run detail)."""
    nodes = list(db.scalars(select(NodeRun).where(NodeRun.run_id == run.id)).all())
    calls, simulated, calls_cut = _calls(db, run.id)
    specs = _spec_versions(db, run, nodes)
    steps = _build_steps(db, run, nodes, specs, calls)
    revision_ids = {s.revision_id for rows in steps.values() for s in rows.values() if s.revision_id}
    revision_ids.update(rid for nr in nodes if (rid := _model_revision_id(nr)) is not None)
    revisions = _revisions(db, run.project_id, revision_ids)
    drawings, drawings_cut = run_progress_drawings.view(db, run, _ordered(run, nodes), limit=MAX_DRAWINGS)
    stages, units_left = [], MAX_UNITS
    seen: _SeenDecisions = {}
    copies = _decision_copies(nodes, specs)
    for nr in _ordered(run, nodes):
        units = _units(nr, calls.get(nr.node_id, {}), steps.get(nr.id, {}), specs.get(nr.id), revisions)
        cut = len(units) > units_left
        units = units[:units_left]
        units_left -= len(units)
        for unit in units:
            unit["decisions"] = _unique_decisions(unit["decisions"], seen, ("unit", f"{nr.id}/{unit['key']}"), copies)
        stage_calls = _Calls()
        for c in calls.get(nr.node_id, {}).values():
            stage_calls.merge(c)
        stages.append({
            "node_id": nr.node_id,
            "node_type": nr.node_type,
            "label": _label(run, nr.node_id),
            "status": nr.status,
            "attempt": nr.attempt,
            "started_at": _iso(nr.started_at),
            "finished_at": _iso(nr.finished_at),
            "units": units,
            "units_truncated": cut,
            "calls": stage_calls.out(details=False),
            "decisions": _stage_decisions(nr, seen, copies),
            "error": _error(nr.error),
        })
    return {
        "run_id": str(run.id),
        "status": run.status,
        "policy": run.execution_policy,
        "simulated": simulated,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "totals": _totals(stages),
        "truncated": calls_cut or drawings_cut or any(s["units_truncated"] for s in stages),
        "stages": stages,
        "drawings": drawings,
    }


# --- sources -------------------------------------------------------------------------------------------------------


def _calls(db: Session, run_id: uuid.UUID) -> tuple[dict[str, dict[str, _Calls]], bool, bool]:
    """{node_id: {unit: calls}} in the order the units first called a model, whether any call was simulated, and
    whether the ledger was cut at :data:`MAX_CALLS`."""
    rows = db.execute(
        select(NodeRun.node_id, AiCall.id, AiCall.purpose, AiCall.status, AiCall.error, AiCall.latency_ms, AiCall.tokens_in,
               AiCall.tokens_out, AiCall.simulated, AiCall.created_at,
               AiCall.request["unit"].astext.label("unit"), AiCall.request["started_at"].astext.label("started_at"),
               AiCall.request["attempt"].label("attempt"),
               AiCall.response["request_id"].astext.label("response_request_id"),
               AiCall.error["request_id"].astext.label("error_request_id"))
        .join(NodeRun, NodeRun.id == AiCall.node_run_id)
        .where(NodeRun.run_id == run_id).order_by(AiCall.created_at, AiCall.id).limit(MAX_CALLS + 1)
    ).all()
    out: dict[str, dict[str, _Calls]] = defaultdict(dict)
    for r in sorted(rows[:MAX_CALLS], key=lambda row: (_call_started(row) or "", str(row.id))):
        out[r.node_id].setdefault(r.unit or r.purpose, _Calls()).add(r)
    for units in out.values():  # first call first (created_at is the commit's transaction start, not the call's)
        ordered = sorted(units.items(), key=lambda kv: kv[1].started_at or "")
        units.clear()
        units.update(ordered)
    return out, any(r.simulated for r in rows), len(rows) > MAX_CALLS


def _build_steps(db: Session, run: WorkflowRun, nodes: list[NodeRun], specs: dict[uuid.UUID, EngineeringSpecVersion],
                 calls: dict[str, dict[str, _Calls]]) -> dict[uuid.UUID, dict[str, AssemblyBuildStep]]:
    """Build records for only the definitions the bounded view can display, using their existing unique index."""
    out: dict[uuid.UUID, dict[str, AssemblyBuildStep]] = defaultdict(dict)
    wanted: list[tuple[str, uuid.UUID, str]] = []
    left = MAX_UNITS
    for nr in _ordered(run, nodes):
        definitions = _part_definitions(specs.get(nr.id)) if nr.node_type == "assembly_from_spec" else []
        for definition in definitions[:left]:
            wanted.append((f"node_run:{nr.id}", specs[nr.id].id, definition["key"]))
        keys = {f"definition:{d['key']}" for d in definitions}
        count = 1 if nr.node_type in ("model_3d", "drawing_reconstruct") else len(definitions) + len(set(calls.get(nr.node_id, {})) - keys)
        left = max(0, left - count)
        if not left:
            break
    if not wanted:
        return out
    # node_run_id alone has no index. The exact (scope, spec version, definition) tuple also avoids loading
    # hidden revisions or presenting a clipped-away build record as an unstarted visible definition.
    rows = db.scalars(select(AssemblyBuildStep).where(
        tuple_(AssemblyBuildStep.scope, AssemblyBuildStep.spec_version_id, AssemblyBuildStep.definition_key).in_(wanted),
        AssemblyBuildStep.node_run_id.in_([nr.id for nr in nodes]), AssemblyBuildStep.project_id == run.project_id,
    )).all()
    for step in rows:
        out[step.node_run_id].setdefault(step.definition_key, step)
    return out


def _spec_versions(db: Session, run: WorkflowRun, nodes: list[NodeRun]) -> dict[uuid.UUID, EngineeringSpecVersion]:
    """Resolve each assembly's exact input; a not-yet-started stage follows its own incoming spec edge."""
    by_node = {nr.node_id: nr for nr in nodes}
    wanted: dict[uuid.UUID, uuid.UUID] = {}
    for nr in nodes:
        if nr.node_type != "assembly_from_spec":
            continue
        value = (nr.inputs or {}).get("spec")
        if not isinstance(value, dict):
            value = (nr.outputs or {}).get("assembly")
        if not isinstance(value, dict):
            for edge in (run.graph_snapshot or {}).get("edges") or []:
                if edge.get("target") == nr.node_id and edge.get("target_port") == "spec":
                    parent = by_node.get(edge.get("source"))
                    value = (parent.outputs or {}).get(edge.get("source_port")) if parent else None
                    break
        version_id = _uuid(value.get("spec_version_id")) if isinstance(value, dict) else None
        if version_id is not None:
            wanted[nr.id] = version_id
    versions = {v.id: v for v in db.scalars(select(EngineeringSpecVersion).where(
        EngineeringSpecVersion.id.in_(set(wanted.values())), EngineeringSpecVersion.project_id == run.project_id,
    )).all()} if wanted else {}
    return {nid: versions[vid] for nid, vid in wanted.items() if vid in versions}


def _revisions(db: Session, project_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[uuid.UUID, ModelRevision]:
    return {r.id: r for r in db.scalars(select(ModelRevision).join(Part, Part.id == ModelRevision.part_id)
            .where(ModelRevision.id.in_(ids), Part.project_id == project_id)
            .options(load_only(ModelRevision.id, ModelRevision.part_id, ModelRevision.rev_no, ModelRevision.status,
                               ModelRevision.metrics, ModelRevision.ops, ModelRevision.checks, ModelRevision.ai_summary)))} if ids else {}


# --- shaping -------------------------------------------------------------------------------------------------------


def _ordered(run: WorkflowRun, nodes: list[NodeRun]) -> list[NodeRun]:
    """Stages in the order of the run's graph."""
    order = {n.get("id"): i for i, n in enumerate((run.graph_snapshot or {}).get("nodes") or [])}
    return sorted(nodes, key=lambda nr: (order.get(nr.node_id, len(order)), nr.started_at or datetime.max.replace(tzinfo=UTC)))


def _label(run: WorkflowRun, node_id: str) -> str | None:
    node = next((n for n in (run.graph_snapshot or {}).get("nodes") or [] if n.get("id") == node_id), None)
    return (node or {}).get("label") or None


def _units(nr: NodeRun, calls: dict[str, _Calls], steps: dict[str, AssemblyBuildStep],
           spec: EngineeringSpecVersion | None, revisions: dict[uuid.UUID, ModelRevision]) -> list[dict[str, Any]]:
    """The stage's work units: the spec's part definitions for an assembly stage (built or not yet), then every other
    unit that called a model (spec chunks, placement, …)."""
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    if nr.node_type in ("model_3d", "drawing_reconstruct"):
        return [_model_unit(nr, calls, revisions.get(_model_revision_id(nr)))]
    if nr.node_type == "assembly_from_spec" and spec is not None:
        content = spec.content or {}
        quantity = quantities(content)
        for d in _part_definitions(spec):
            key = f"definition:{d['key']}"
            seen.add(key)
            units.append(_definition_unit(nr, key, d, quantity.get(d["key"], {}).get("total", 0),
                                          steps.get(d["key"]), calls.get(key), revisions))
    for key, c in calls.items():
        if key in seen:
            continue
        units.append({"key": key, "kind": key.split(":", 1)[0], "title": None, "quantity": None,
                      "status": c.status, "attempts": None,
                      "revision": None, "calls": c.out(), "decisions": _repair(c),
                      "error": c.last_error if c.status == "failed" else None})
    return units


def _part_definitions(spec: EngineeringSpecVersion | None) -> list[dict[str, Any]]:
    if spec is None:
        return []
    content = spec.content or {}
    reachable = spec_tree(content)["definitions"]
    return [d for d in content.get("definitions") or []
            if isinstance(d, dict) and d.get("kind") != "assembly" and d.get("key") in reachable]


def _model_revision_id(nr: NodeRun) -> uuid.UUID | None:
    value = (nr.outputs or {}).get("model")
    return _uuid(value.get("revision_id")) if isinstance(value, dict) and value.get("type") == "model" else None


def _model_unit(nr: NodeRun, calls: dict[str, _Calls], revision: ModelRevision | None) -> dict[str, Any]:
    combined = _Calls()
    for value in calls.values():
        combined.merge(value)
    if nr.status == "waiting_human":
        status = "needs_clarification"
    elif nr.status in ACTIVE_NODE:
        status = "pending"
    elif nr.status == "succeeded" and revision is not None:
        status = "built"
    else:
        status = "failed" if nr.status == "failed" else "not_started"
    return {"key": "model", "kind": "model", "title": None, "quantity": 1, "status": status,
            "attempts": nr.attempt, "revision": _revision_out(revision), "calls": combined.out() if combined.count else None,
            "decisions": [*_revision_decisions(revision), *_repair(combined)], "error": _error(nr.error)}


def _definition_unit(nr: NodeRun, key: str, d: dict, quantity: int, step: AssemblyBuildStep | None, calls: _Calls | None,
                     revisions: dict[uuid.UUID, ModelRevision]) -> dict[str, Any]:
    if step is not None:
        status = step.status
    else:  # not built yet: still to come while the stage runs, else it never started
        status = "pending" if nr.status in ACTIVE_NODE else "not_started"
    rev = revisions.get(step.revision_id) if step is not None and step.revision_id else None
    decisions: list[dict[str, Any]] = []
    outcome = step.outcome if step is not None and isinstance(step.outcome, dict) else {}
    for a in outcome.get("assumptions") or []:
        if isinstance(a, dict):
            owner = {"type": "model_revision", "id": str(rev.id), "rev_no": rev.rev_no} if rev else None
            decisions.append(_assumption({**a, "owner": a.get("owner") or owner}))
    decisions += _revision_decisions(rev)
    check = outcome.get("check") if isinstance(outcome.get("check"), dict) else {}
    if check.get("status") == "skipped_turbo":
        decisions.append({"kind": "check_skipped", "checks": [s.get("check") for s in check.get("skipped") or []
                                                              if isinstance(s, dict) and s.get("check")]})
    if calls is not None:
        decisions += _repair(calls)
    return {
        "key": key,
        "kind": "definition",
        "title": d.get("name") or d.get("key"),
        "quantity": quantity or None,
        "status": status,
        "attempts": step.attempts if step is not None else None,
        "source": step.source if step is not None else None,
        "revision": _revision_out(rev),
        "calls": calls.out() if calls is not None else None,
        "decisions": decisions,
        "error": _error(step.error) if step is not None else None,
    }


def _assumption(a: dict) -> dict[str, Any]:
    fields = ("id", "subject", "field", "adopted", "author", "unit", "trigger", "affected", "state",
              "problems", "confirmation", "replaced_by", "owner", "at", "alternatives", "sources")
    return {"kind": "assumption", "text": a.get("basis"), **{key: a.get(key) for key in fields}}


def _revision_out(revision: ModelRevision | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    operations = Counter(op["type"] for op in revision.ops or []
                         if isinstance(op, dict) and isinstance(op.get("type"), str) and not op.get("suppressed"))
    return {"id": str(revision.id), "part_id": str(revision.part_id), "label": f"R{revision.rev_no}",
            "status": revision.status, "size": ((revision.metrics or {}).get("bbox") or {}).get("size"),
            "operations": [{"op": op, "count": count} for op, count in operations.items()]}


def _revision_decisions(revision: ModelRevision | None) -> list[dict[str, Any]]:
    if revision is None:
        return []
    # Structured check decisions are authoritative; older revisions only kept the model's assumption summaries.
    assumptions = (revision.checks or {}).get("assumptions") or (revision.ai_summary or {}).get("assumptions") or []
    owner = {"type": "model_revision", "id": str(revision.id), "rev_no": revision.rev_no}
    return [_assumption({**(a if isinstance(a, dict) else {"basis": a}),
                         "owner": (a.get("owner") if isinstance(a, dict) else None) or owner})
            for a in assumptions if isinstance(a, (dict, str))]


def _repair(c: _Calls) -> list[dict[str, Any]]:
    return [{"kind": "repair", "count": c.repairs}] if c.repairs else []


def _stage_decisions(nr: NodeRun, seen: _SeenDecisions, copies: _DecisionCopies) -> list[dict[str, Any]]:
    """What the stage itself decided: its assumptions not owned by a unit's part revision (e.g. placement; a spec step's
    adopted values) and not already shown at an earlier stage, and the checks its ledger skipped (both known once the
    stage ended)."""
    out: list[dict[str, Any]] = []
    outputs = nr.outputs or {}
    spec = outputs.get("spec") if isinstance(outputs.get("spec"), dict) else {}
    for a in [*(outputs.get("assumptions") or []), *(spec.get("assumptions") or [])]:
        if not isinstance(a, dict):
            continue
        out.append(_assumption(a))
    ledger = checks_ledger.from_steps(nr.steps)
    skipped = sorted((ledger or {}).get("skipped") or {})
    if skipped:
        out.append({"kind": "check_skipped", "checks": skipped})
    scope = ("engineering_spec_version", str(spec["spec_version_id"])) if spec.get("spec_version_id") else ("node_run", str(nr.id))
    return _unique_decisions(out, seen, scope, copies)


def _decision_signature(decision: dict[str, Any]) -> str:
    return json.dumps([*(decision.get(k) for k in ("id", "subject", "field")),
                       decision.get("text") or decision.get("basis"), decision.get("adopted"), decision.get("unit")],
                      sort_keys=True, default=str)


def _decision_copies(nodes: list[NodeRun], specs: dict[uuid.UUID, EngineeringSpecVersion]) -> _DecisionCopies:
    """Only a verified source relationship makes equal records under different owners the same decision.

    Assembly revisions copy their exact input spec's assumptions. Deliveries repeat those same owner ids; model
    revisions have independent local ids and must never be merged merely because their wording is identical.
    """
    copies: _DecisionCopies = {}
    for nr in nodes:
        spec = specs.get(nr.id)
        assembly = (nr.outputs or {}).get("assembly")
        if spec is None or not isinstance(assembly, dict) or not assembly.get("assembly_revision_id"):
            continue
        owner = ("assembly_revision", str(assembly["assembly_revision_id"]))
        for assumption in (spec.content or {}).get("assumptions") or []:
            if isinstance(assumption, dict):
                copies[(owner, _decision_signature(assumption))] = ("engineering_spec_version", str(spec.id))
    return copies


def _merge_decision(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Enrich the first source projection without counting its inherited snapshots again.

    Source values and an already-final decision state stay authoritative. Older/thinner copies cannot erase
    metadata; a confirmation or replacement missing from the source projection must still become visible.
    Only these newly-created projection dictionaries are updated, never the saved source records.
    """
    for key, value in incoming.items():
        if key not in ("kind", "owner", "state") and existing.get(key) in (None, "", [], {}):
            existing[key] = value
    if existing.get("state") not in ("person", "confirmed", "corrected", "replaced") and incoming.get("state"):
        existing["state"] = incoming["state"]
    if existing.get("author") == "person":
        existing["state"] = "person"
    elif existing.get("replaced_by"):
        existing["state"] = "replaced"
    elif existing.get("state") not in ("confirmed", "corrected", "replaced"):
        confirmation = existing.get("confirmation")
        if isinstance(confirmation, dict) and confirmation.get("decision") in ("confirmed", "corrected"):
            existing["state"] = confirmation["decision"]


def _unique_decisions(decisions: list[dict[str, Any]], seen: _SeenDecisions,
                      scope: _DecisionOwner, copies: _DecisionCopies) -> list[dict[str, Any]]:
    out = []
    for decision in decisions:
        if decision["kind"] == "assumption":
            raw = decision.get("owner")
            owner = (str(raw["type"]), str(raw["id"])) if isinstance(raw, dict) and raw.get("type") and raw.get("id") else scope
            signature = _decision_signature(decision)
            identity = f"id:{decision['id']}" if decision.get("id") else f"content:{signature}"
            key = (copies.get((owner, signature), owner), identity)
            if key in seen:
                _merge_decision(seen[key], decision)
                continue
            seen[key] = decision
        out.append(decision)
    return out


def _error(err: Any) -> dict[str, Any] | None:
    """The public part of a failure: what failed, why, and the recovery the person can take."""
    if not isinstance(err, dict) or not err:
        return None
    # Even node/build messages can originate in provider exceptions and quote a private prompt. The failure's
    # code, category and recovery are the public explanation; neither message nor nested details is metadata.
    return {**{k: err.get(k) for k in ("code", "stage", "recovery", "failure_class") if isinstance(err.get(k), str)},
            "diagnostic_code": _diagnostic_code(err)}


def _call_error(err: Any) -> dict[str, Any]:
    # Match /ai-calls: provider exception messages may quote requests; only their public code/class may leave the ledger.
    return {"code": err.get("code") if isinstance(err, dict) else None, "failure_class": call_retry.row_class(err),
            "diagnostic_code": _diagnostic_code(err)}


def _diagnostic_code(err: Any) -> str | None:
    if not isinstance(err, dict):
        return None
    code = diagnostics.code_of(err.get("code") if isinstance(err.get("code"), str) else None,
                               err.get("message") if isinstance(err.get("message"), str) else None)
    return code if code != "unknown" else None


def _is_legacy_model_note(assumption: dict[str, Any]) -> bool:
    """Match the frontend's isLegacyModelNote: plain modeling notes are not structured decisions."""
    return (bool(assumption.get("basis") or assumption.get("text"))
            and assumption.get("adopted") is None
            and assumption.get("confirmation") is None and assumption.get("replaced_by") is None
            and not any(assumption.get(key) for key in (
                "subject", "field", "author", "unit", "trigger", "state", "problems", "affected", "alternatives", "sources")))


def _totals(stages: list[dict[str, Any]]) -> dict[str, int | None]:
    units = [u for s in stages for u in s["units"]]
    return {
        "calls": sum(s["calls"]["count"] for s in stages),
        "failed_calls": sum(s["calls"]["failed"] for s in stages),
        "tokens_in": _usage_sum([s["calls"]["tokens_in"] for s in stages]),
        "tokens_out": _usage_sum([s["calls"]["tokens_out"] for s in stages]),
        "assumptions": sum(1 for x in [*(d for u in units for d in u["decisions"]), *(d for s in stages for d in s["decisions"])]
                           if x["kind"] == "assumption" and not _is_legacy_model_note(x)),
        "units": sum(1 for u in units if u["kind"] in ("definition", "model")),
        "units_built": sum(1 for u in units if u["kind"] in ("definition", "model") and u["status"] == "built"),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _usage_sum(values: list[int | None]) -> int | None:
    return None if any(v is None for v in values) else sum(v for v in values if v is not None)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _call_started(row: Any) -> str | None:
    if isinstance(row.started_at, str):
        try:
            return datetime.fromisoformat(row.started_at).astimezone(UTC).isoformat()
        except ValueError:
            pass
    return _iso(row.created_at)
