"""Run state machine backed by PostgreSQL (design.md §7).

All run state lives in `workflow_runs` / `node_runs`; workers are stateless.
A worker claims a ready node with SELECT … FOR UPDATE SKIP LOCKED, runs its
handler outside the claim transaction, then records the outcome and activates
downstream nodes under a lock on the run row. Restarted workers reclaim nodes
whose lease expired.

A successful handler's products (specialised rows, asset versions,
run_outputs) commit in the same transaction as the node result, and only if
the node is still held by the same attempt (design §10.3, A09): a worker that
dies before recording, or whose lease was taken over, leaves nothing behind —
except the attempt's AI call records, which are written in their own
transaction (the calls were really made; the run ledger keeps every call).

While a handler runs, a heartbeat thread extends the node's lease (its own
short transaction, every lease / 5 seconds, never past ``started_at +
node_max_runtime_seconds``), so long nodes (model / reconstruct loops, DWG
conversion, evidence extraction) are not reclaimed and run twice. Handlers
may also call ``ctx.heartbeat()`` (rate-limited). The handler transaction
never writes the node run row before it records the outcome, so the
heartbeat never waits on it (it skips a locked row and tries again).

Generation reliability J (.trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.1, §2.2):

- Every run freezes a budget ``inputs.budget = {max_calls, max_seconds, node_max_seconds, source}`` when it starts
  (:func:`freeze_budget`; the request may carry ``max_calls`` / ``max_seconds``, default: no run-wide cap, each step
  bounded by ``node_max_runtime_seconds`` as before). While a handler runs, its model calls are checked against
  :class:`app.ai.retry.CallLimits` (the run's call / time budget, the step's runtime cap, the run's cancel flag, this
  attempt's lease) before every request — ``retry.limits_scope`` around the handler.
- ``max_calls`` is shared by the steps of the run: every request takes one from the run-wide counter
  ``inputs.calls_taken`` (an atomic update in a short transaction of its own, never below the run's recorded calls), so
  two steps on two workers cannot each spend the whole budget. ``max_seconds`` counts the time the run is active: its
  clock (``budget.clock``) keeps what it used and starts again when a stopped run continues (:func:`rebase_clock`: a
  node retry, a raised budget), so the idle time between a stop and the continue never counts.
- **Progress commits (departure from the rule above).** A handler that builds independent work units may call
  ``ctx.checkpoint()`` after each unit: it commits the handler's transaction so far (the unit's products — part
  revisions, build progress rows — and the AI call records) in a short transaction of its own, *only while this attempt
  still holds the node* (checked under a share lock on the node run row, which keeps a reclaim from slipping in between
  check and commit). A worker that dies or loses its lease afterwards keeps those units; the next attempt reuses them
  (``orchestration.build_assembly`` progress rows) and redoes only the rest. Everything after the last checkpoint
  still commits with the node result, or not at all. Two handlers checkpoint: ``assembly_from_spec`` (after each part
  definition built) and, since generation reliability R (design §5.3), the layered ``assembly_drawing``
  (``subassemblies`` on; after each drawing package made — the root's, then each sub-assembly's — through
  ``orchestration.make_level_drawings``): a retry of the node run reuses the complete packages it finds
  (``assembly_drawings.complete_package``) and draws only the rest. No other handler may call it.
- A failed handler is not attempted again by the engine when its last model call already went through the bounded
  retry for a transient class or was stopped by a limit (``retry.retries_spent``): the call-level and node-level
  retries never multiply.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.orm import Session

from ..ai import retry as call_retry
from ..ai.team_prompt import team_scope
from ..config import get_settings
from ..db import session_scope
from ..models import AiCall, NodeRun, Part, Project, User, WorkflowRun
from ..errors import not_found
from .graph import Graph, validate_graph
from .nodes import NODE_DEFS

log = logging.getLogger("partroute.workflow")

TERMINAL_NODE = {"succeeded", "failed", "cancelled", "skipped"}
# partial (W0 §2.2): a node failed for good, its dependants were skipped, and real products of other nodes exist.
TERMINAL_RUN = {"succeeded", "partial", "failed", "cancelled"}
# Nodes whose success is no product of the run (inputs, a review decision): they never make a failed run partial.
NON_PRODUCT_NODES = frozenset({"text_input", "file_input", "model_input", "human_review"})


class GraphInvalid(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__("workflow graph is invalid")
        self.errors = errors


class NodeFailure(Exception):
    """Raised by handlers for expected failures (bad input, model refused …)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None,
                 calls: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        # AI call records made before the failure: the handler transaction is rolled back, so the
        # engine writes them again with the failure (the run ledger must keep every model call).
        self.calls = list(calls or [])


@dataclass
class NodeOutcome:
    outputs: dict[str, Any] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    wait_human: bool = False
    # W8: the node made real products (rows, asset versions, call records) but did not reach its required result
    # ({code, message, details}). The products commit with the node result like a success, the node is recorded as
    # failed (no automatic retry: a retry would duplicate the products) and its dependants are skipped, except inputs
    # declared ``Port.after_failure`` (a delivery still lists what exists). A NodeFailure instead discards everything.
    failure: dict | None = None


@dataclass
class NodeContext:
    db: Session
    run: WorkflowRun
    node_run: NodeRun
    params: Any
    inputs: dict[str, Any]
    user: User
    part: Part | None
    project: Project
    steps: list[dict] = field(default_factory=list)
    attempt: int = 0
    lease_lost: bool = False
    _beat_at: float = 0.0
    _beat_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # AI call rows this attempt added and not yet committed (engine._watch_calls); a checkpoint commits them.
    _made_calls: list = field(default_factory=list, repr=False)
    # J: the call limits of this attempt (also put in place for every tracer of the handler's thread).
    limits: call_retry.CallLimits | None = None

    @property
    def policy(self) -> str:
        """The run's frozen execution policy (standard | turbo); handlers never read a current default."""
        return getattr(self.run, "execution_policy", None) or "standard"

    def step(self, kind: str, ok: bool = True, **detail: Any) -> None:
        self.steps.append({"kind": kind, "ok": ok, "at": datetime.now(UTC).isoformat(), "detail": detail})

    def heartbeat(self, *, force: bool = False) -> bool:
        """Extend this attempt's lease in a short transaction of its own (at most every lease / 10 seconds
        unless ``force``). False once the node is no longer held by this attempt (reclaimed or finished):
        the handler may stop early; its outcome would be discarded anyway."""
        with self._beat_lock:
            now = time.monotonic()
            lease = get_settings().node_lease_seconds
            if not force and now - self._beat_at < max(1.0, lease / 10.0):
                return not self.lease_lost
            self._beat_at = now
        held = extend_lease(self.node_run.id, self.attempt)
        if held is False:
            self.lease_lost = True
        return not self.lease_lost

    def checkpoint(self) -> bool:
        """J (§2.2): commit what the handler made so far — the finished work units' products and AI call records — in
        a short transaction, only while this attempt still holds the node (status running, same attempt; checked under a
        share lock on the node run row so no reclaim slips in before the commit). False when it no longer does: nothing
        is committed (the rest is discarded with the outcome) and the handler should start no new unit."""
        if self.lease_lost:
            return False
        held = self.db.scalar(select(NodeRun.id).where(NodeRun.id == self.node_run.id, NodeRun.status == "running",
                                                       NodeRun.attempt == self.attempt).with_for_update(read=True))
        if held is None:
            self.lease_lost = True
            return False
        self.db.commit()
        self._made_calls.clear()  # durable now: never written again by the lost-attempt path
        return True


Handler = Callable[[NodeContext], NodeOutcome]


# --- run budget (generation reliability J, design §2.2) ----------------------------------------------------------------

#: Bounds of a requested run budget.
BUDGET_MAX_CALLS = 10_000
BUDGET_MAX_SECONDS = 7 * 24 * 3600


def freeze_budget(requested: dict | None) -> dict[str, Any]:
    """The run budget frozen at start: ``max_calls`` (real model requests of the whole run, retries included) and
    ``max_seconds`` (from the run's start) as requested — None means no run-wide cap — plus the per-step runtime cap that
    always applies (``node_max_seconds``) and where it came from. Raises ValueError for a value out of bounds."""
    req = dict(requested or {})
    out: dict[str, Any] = {"max_calls": None, "max_seconds": None, "node_max_seconds": get_settings().node_max_runtime_seconds,
                           "source": "default"}
    for key, hi in (("max_calls", BUDGET_MAX_CALLS), ("max_seconds", BUDGET_MAX_SECONDS)):
        v = req.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= hi:
            raise ValueError(f"budget.{key} must be an integer from 1 to {hi}")
        out[key], out["source"] = v, "request"
    return out


def raise_budget(run: WorkflowRun, requested: dict, *, user_id: uuid.UUID | None) -> dict[str, Any]:
    """A person continues a run that stopped on its budget (J): a cap named in ``requested`` replaces the frozen one — an
    explicit None clears it (no run-wide cap) — and a cap left out stays; the change is kept in ``budget.changes`` (never
    a silent switch) and the time clock starts again (:func:`rebase_clock`). Caller holds the run lock."""
    from ..services import feature_flags as features

    # Frozen runs drain within their existing budget; increasing it starts more work.
    features.guard_graph(run.graph_snapshot)
    if run.task_id is not None:
        features.require_enabled("tasks")
    old = run_budget(run)
    keys = ("max_calls", "max_seconds")
    new = freeze_budget({k: requested[k] if k in requested else old.get(k) for k in keys})
    new["source"] = "raised"
    if old.get("clock"):
        new["clock"] = old["clock"]
    new["changes"] = [*(old.get("changes") or []), {"at": _now().isoformat(), "by": str(user_id) if user_id else None,
                                                     "from": {k: old.get(k) for k in keys}, "to": {k: new.get(k) for k in keys}}]
    run.inputs = {**(run.inputs or {}), "budget": new}
    rebase_clock(run)
    return run_budget(run)


def run_budget(run: WorkflowRun) -> dict[str, Any]:
    """The run's frozen budget (runs started before J: the default, labelled ``legacy``)."""
    b = (run.inputs or {}).get("budget")
    return dict(b) if isinstance(b, dict) else {**freeze_budget(None), "source": "legacy"}


def budget_caps(run: WorkflowRun) -> dict[str, Any]:
    """The frozen caps alone (run lists, operation cards): no live counts."""
    b = run_budget(run)
    return {"max_calls": b.get("max_calls"), "max_seconds": b.get("max_seconds")}


def time_used(run: WorkflowRun, budget: dict[str, Any] | None = None) -> tuple[float, datetime | None]:
    """What the run's time budget has used: ``(seconds, since)`` — ``seconds`` up to the start of the span still counting
    (``since``; None when the run is not active: then ``seconds`` is the whole). The clock counts from the run's start and
    again from each continue (:func:`rebase_clock`); the time a stopped run waited for its continue never counts."""
    b = budget if budget is not None else run_budget(run)
    clock = b.get("clock") if isinstance(b.get("clock"), dict) else {}
    before = float(clock.get("before_s") or 0.0)
    since = datetime.fromisoformat(clock["from"]) if clock.get("from") else run.started_at
    if since is None:
        return before, None
    if run.finished_at is not None:
        return before + max(0.0, (run.finished_at - since).total_seconds()), None
    return before, since


def rebase_clock(run: WorkflowRun) -> None:
    """J: a stopped run continues (a node retry, a raised budget): its time clock keeps what the run used and counts again
    from now. Runs without a frozen budget (before J) have no clock."""
    b = (run.inputs or {}).get("budget")
    if not isinstance(b, dict):
        return
    before, since = time_used(run, b)
    now = _now()
    if since is not None:
        before += max(0.0, (now - since).total_seconds())
    run.inputs = {**(run.inputs or {}), "budget": {**b, "clock": {"from": now.isoformat(), "before_s": round(before, 3)}}}


def _reopen(run: WorkflowRun) -> None:
    """A node goes back to the queue: a finished run is active again — its time clock re-bases (the idle time since it
    finished never counts) — and no longer finished."""
    if run.finished_at is not None:
        rebase_clock(run)
    run.finished_at = None


def run_call_count(db: Session, run_id: uuid.UUID) -> int:
    """Real model requests recorded for the run so far (every node, every attempt)."""
    return int(db.scalar(select(func.count()).select_from(AiCall).join(NodeRun, NodeRun.id == AiCall.node_run_id)
                         .where(NodeRun.run_id == run_id)) or 0)


_TAKE_CALL = text(
    "UPDATE workflow_runs SET inputs = jsonb_set(inputs, '{calls_taken}', "
    "to_jsonb(GREATEST(COALESCE((inputs->>'calls_taken')::int, 0), :floor) + 1)) "
    "WHERE id = :id AND GREATEST(COALESCE((inputs->>'calls_taken')::int, 0), :floor) < :max "
    "RETURNING (inputs->>'calls_taken')::int")
_CALLS_TAKEN = text("SELECT GREATEST(COALESCE((inputs->>'calls_taken')::int, 0), :floor) FROM workflow_runs WHERE id = :id")


def _shared_calls(run_id: uuid.UUID) -> Callable[[int, bool], tuple[bool, int] | None]:
    """``CallLimits.share`` of a run: the run-wide request counter ``inputs.calls_taken``, taken with one atomic update in a
    short transaction of its own (visible to every worker at once; never below the calls the run recorded). None when
    the run row stays locked (the step's own count decides for that request)."""

    def share(max_calls: int, take: bool) -> tuple[bool, int] | None:
        try:
            with session_scope() as s:
                s.execute(text("SET LOCAL lock_timeout = '3s'"))
                args = {"id": run_id, "floor": run_call_count(s, run_id), "max": max_calls}
                if take and (taken := s.scalar(_TAKE_CALL, args)) is not None:
                    return True, int(taken)
                used = int(s.scalar(_CALLS_TAKEN, args) or 0)
                return (False if take else used < max_calls), used
        except Exception:  # noqa: BLE001 - a busy run row never fails the step
            log.warning("run %s: the shared call counter is unavailable; this step counts its own requests", run_id, exc_info=True)
            return None

    return share


def _cancel_check(run_id: uuid.UUID) -> Callable[[], bool]:
    """The run's cancel flag, read in a short session of its own (the engine lets a running step finish)."""

    def check() -> bool:
        with session_scope() as s:
            return bool(s.scalar(select(WorkflowRun.cancel_requested).where(WorkflowRun.id == run_id)))

    return check


def call_limits(db: Session, run: WorkflowRun, nr: NodeRun, ctx: NodeContext) -> call_retry.CallLimits:
    """The limits every model request of this attempt is checked against: the run's call budget (counting what the run
    used before), the earlier of the step's runtime cap and the run's time budget, the cancel flag and the lease."""
    budget = run_budget(run)
    now, mono = _now(), time.monotonic()
    started = nr.started_at or now
    deadline = mono + (started + timedelta(seconds=get_settings().node_max_runtime_seconds) - now).total_seconds()
    kind = "node"
    if budget.get("max_seconds"):
        used, since = time_used(run, budget)  # active time only (the clock re-bases on every continue)
        run_end = mono + int(budget["max_seconds"]) - used - ((now - since).total_seconds() if since is not None else 0.0)
        if run_end < deadline:
            deadline, kind = run_end, "run"
    max_calls = budget.get("max_calls")
    return call_retry.CallLimits(deadline=deadline, deadline_kind=kind, max_calls=max_calls,
                                 used_calls=run_call_count(db, run.id) if max_calls else 0,
                                 cancelled=_cancel_check(run.id), lease_ok=ctx.heartbeat,
                                 share=_shared_calls(run.id) if max_calls else None)


def _now() -> datetime:
    return datetime.now(UTC)


def extend_lease(node_run_id: uuid.UUID, attempt: int) -> bool | None:
    """Push ``lease_until`` of a running attempt to now + lease (capped at ``started_at +
    node_max_runtime_seconds``). True when extended, False when the node is no longer running under
    ``attempt``, None when its row is locked right now (skipped, never waited on; retried next beat)."""
    settings = get_settings()
    with session_scope() as db:
        nr = db.scalar(select(NodeRun).where(NodeRun.id == node_run_id)
                       .with_for_update(skip_locked=True, key_share=True))
        if nr is None:
            held = db.scalar(select(NodeRun.id).where(NodeRun.id == node_run_id, NodeRun.status == "running",
                                                      NodeRun.attempt == attempt))
            return None if held is not None else False
        if nr.status != "running" or nr.attempt != attempt:
            return False
        until = _now() + timedelta(seconds=settings.node_lease_seconds)
        if nr.started_at is not None:
            until = min(until, nr.started_at + timedelta(seconds=settings.node_max_runtime_seconds))
        if nr.lease_until is None or until > nr.lease_until:
            nr.lease_until = until
        return True


class _Heartbeat:
    """Background lease heartbeat for one handler call (stopped before the outcome is recorded)."""

    def __init__(self, ctx: NodeContext):
        self.ctx = ctx
        self.stop = threading.Event()
        lease = get_settings().node_lease_seconds
        self.interval = max(0.5, lease / 5.0)
        self.thread = threading.Thread(target=self._run, name=f"lease-{ctx.node_run.id}", daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                if not self.ctx.heartbeat(force=True):
                    return
            except Exception:  # noqa: BLE001 - a missed beat only shortens the lease
                log.exception("lease heartbeat of node run %s failed", self.ctx.node_run.id)

    def __enter__(self) -> "_Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self.thread.join(timeout=10.0)


_AI_CALL_COLUMNS = [c.key for c in inspect(AiCall).column_attrs]


def _watch_calls(db: Session, node_run_id: uuid.UUID) -> list[AiCall]:
    """Collect the AI call rows this attempt adds to its session (they are rolled back with a lost claim)."""
    added: list[AiCall] = []

    def on_pending(session: Session, obj: Any) -> None:
        if isinstance(obj, AiCall) and obj.node_run_id == node_run_id:
            added.append(obj)

    event.listen(db, "transient_to_pending", on_pending)
    return added


def _call_rows(calls: list[AiCall]) -> list[dict[str, Any]]:
    return [{k: getattr(c, k) for k in _AI_CALL_COLUMNS if getattr(c, k) is not None} for c in calls]


def _record_lost_calls(rows: list[dict[str, Any]], failed_calls: list, node_run_id: uuid.UUID) -> None:
    """AI calls of an attempt whose outcome is discarded (claim lost): written on their own, the outputs
    stay discarded."""
    if not rows and not failed_calls:
        return
    try:
        with session_scope() as db:
            for row in rows:
                db.add(AiCall(**row))
            if failed_calls:
                from ..services import ai as ai_svc

                ai_svc.record_calls(db, failed_calls, node_run_id)
    except Exception:  # noqa: BLE001 - never breaks the engine; the operator log keeps the trace
        log.exception("could not record the AI calls of a discarded attempt of node run %s", node_run_id)


def _graph(run: WorkflowRun) -> Graph:
    return Graph.model_validate(run.graph_snapshot)


def create_run(
    db: Session,
    *,
    graph: dict | Graph,
    project_id: uuid.UUID,
    part_id: uuid.UUID | None,
    user_id: uuid.UUID,
    origin: str = "workflow",
    title: str = "",
    workflow_id: uuid.UUID | None = None,
    workflow_version: int | None = None,
    inputs: dict | None = None,
    execution_policy: str | None = None,
) -> WorkflowRun:
    """Snapshot the graph and create one node_run per node. Caller commits.

    ``execution_policy`` is frozen on the run here (None = standard; an unknown value raises ValueError) and never
    changes afterwards: node handlers, agent tools and derived artifacts read it from the run."""
    from ..policy import parse_policy
    from ..services.feature_flags import guard_graph

    guard_graph(graph)
    policy = parse_policy(execution_policy)
    g, errors = validate_graph(graph)
    if errors or g is None:
        raise GraphInvalid(errors)
    from ..services.cad_admission import WRITE_NODES, admit_part

    if part_id is not None and any(node.type in WRITE_NODES for node in g.nodes):
        user = db.get(User, user_id)
        if user is None:
            raise not_found("user")
        admit_part(db, user, part_id, project_id)
    run = WorkflowRun(
        execution_policy=policy,
        project_id=project_id,
        part_id=part_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        origin=origin,
        title=title,
        graph_snapshot=g.model_dump(mode="json"),
        inputs=inputs or {},
        status="queued",
        created_by=user_id,
    )
    db.add(run)
    db.flush()
    targets = {e.target for e in g.edges}
    for n in g.nodes:
        d = NODE_DEFS[n.type]
        db.add(
            NodeRun(
                run_id=run.id,
                node_id=n.id,
                node_type=n.type,
                status="pending" if n.id in targets else "ready",
                max_attempts=d.max_attempts,
            )
        )
    db.flush()
    return run


def _nodes(db: Session, run_id: uuid.UUID) -> dict[str, NodeRun]:
    return {nr.node_id: nr for nr in db.scalars(select(NodeRun).where(NodeRun.run_id == run_id)).all()}


def run_outcome(nodes: list[NodeRun]) -> str:
    """Terminal status of a run whose nodes are all finished and not cancelled (W0 §2.2, architecture §8): succeeded
    when every node succeeded; otherwise partial when some node really produced something (a failed branch skipped
    only its dependants), else failed. Never an average over node counts: one failed required node is never green."""
    if all(n.status == "succeeded" for n in nodes):
        return "succeeded"
    produced = any(n.status == "succeeded" and n.node_type not in NON_PRODUCT_NODES for n in nodes)
    return "partial" if produced else "failed"


def refresh_run_status(db: Session, run: WorkflowRun) -> None:
    """Derive run status from node states. Caller holds the run lock."""
    nodes = _nodes(db, run.id)
    statuses = [n.status for n in nodes.values()]
    if any(s in ("running", "ready") for s in statuses):
        new = "running"
    elif any(s == "waiting_human" for s in statuses):
        new = "waiting"
    elif any(s == "pending" for s in statuses):
        # Pending nodes with nothing runnable upstream: should not happen, but
        # never leave a run hanging silently.
        new = "running"
    elif run.cancel_requested or any(s == "cancelled" for s in statuses):
        new = "cancelled"
    else:
        new = run_outcome(list(nodes.values()))
    if new != run.status:
        run.status = new
        if new in TERMINAL_RUN:
            run.finished_at = _now()
        if new == "running" and run.started_at is None:
            run.started_at = _now()


def _tolerates_failure(node_type: str, port: str) -> bool:
    """W8: an input declared ``Port.after_failure`` lets its node run after that upstream failed or was skipped
    (the input is then whatever the failed node recorded, usually nothing)."""
    d = NODE_DEFS.get(node_type)
    p = d.input(port) if d is not None else None
    return bool(p is not None and getattr(p, "after_failure", False))


def _activate_downstream(db: Session, run: WorkflowRun) -> None:
    """Move pending nodes to ready (all inputs done) or skipped (an input failed, unless that input tolerates it)."""
    g = _graph(run)
    nodes = _nodes(db, run.id)
    sources: dict[str, set[tuple[str, str]]] = {}
    for e in g.edges:
        sources.setdefault(e.target, set()).add((e.source, e.target_port))
    changed = True
    while changed:
        changed = False
        for nid, nr in nodes.items():
            if nr.status != "pending":
                continue
            ups = [(nodes[s], port) for s, port in sources.get(nid, set())]
            if any(u.status == "cancelled" or (u.status in ("failed", "skipped") and not _tolerates_failure(nr.node_type, port))
                   for u, port in ups):
                nr.status = "cancelled" if run.cancel_requested else "skipped"
                nr.finished_at = _now()
                changed = True
            elif all(u.status == "succeeded" or (u.status in ("failed", "skipped") and _tolerates_failure(nr.node_type, port))
                     for u, port in ups):
                nr.status = "cancelled" if run.cancel_requested else "ready"
                changed = True


def lock_run(db: Session, run_id: uuid.UUID) -> WorkflowRun:
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
    assert run is not None
    return run


def lock_run_for_retry(db: Session, user: User, run_id: uuid.UUID) -> WorkflowRun:
    """Authorize the current CAD retry actor and acquire project → part → run locks.

    Other run permissions remain with the calling workflow/assistant entry. A retry must not
    borrow its creator's design permission or hold a run while waiting for a lifecycle lock.
    """
    from ..services.cad_admission import WRITE_NODES, admit_part

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise not_found("run")
    writes_part = run.part_id is not None and db.scalar(select(NodeRun.id).where(
        NodeRun.run_id == run_id, NodeRun.node_type.in_(WRITE_NODES)).limit(1)) is not None
    if writes_part:
        admit_part(db, user, run.part_id, run.project_id, exclude_run_id=run_id)
    # A lifecycle transition may have changed the run while admission waited.
    db.expire(run)
    return lock_run(db, run_id)


def claim_next(db: Session, lease_seconds: int | None = None, max_candidates: int = 10) -> NodeRun | None:
    """Claim one ready node. Caller commits.

    Lock order run → node run, like every other writer of node runs (the
    decision / reopen / cancel paths hold the run lock and then update its node
    runs): the candidate is found without a lock, then its run and the node run
    are locked with SKIP LOCKED, so a busy run is skipped instead of waited on
    and a claim never holds a node run while waiting for its run."""
    lease = lease_seconds or get_settings().node_lease_seconds
    tried: list[uuid.UUID] = []
    for _ in range(max_candidates):
        stmt = (
            select(NodeRun.id, NodeRun.run_id)
            .join(WorkflowRun, WorkflowRun.id == NodeRun.run_id)
            .where(NodeRun.status == "ready", WorkflowRun.cancel_requested.is_(False), WorkflowRun.paused.is_(False))
            .order_by(NodeRun.updated_at)
            .limit(1)
        )
        if tried:
            stmt = stmt.where(NodeRun.id.notin_(tried))
        cand = db.execute(stmt).first()
        if cand is None:
            return None
        tried.append(cand.id)
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == cand.run_id).with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True))
        if run is None or run.cancel_requested or run.paused:
            continue
        nr = db.scalar(select(NodeRun).where(NodeRun.id == cand.id, NodeRun.status == "ready").with_for_update(skip_locked=True)
                       .execution_options(populate_existing=True))
        if nr is None:
            continue
        nr.status = "running"
        nr.attempt += 1
        nr.started_at = _now()
        nr.lease_until = _now() + timedelta(seconds=lease)
        nr.error = None
        refresh_run_status(db, run)
        return nr
    return None


def resolve_inputs(db: Session, run: WorkflowRun, node_id: str) -> dict[str, Any]:
    g = _graph(run)
    nodes = _nodes(db, run.id)
    inputs: dict[str, Any] = {}
    node = next(n for n in g.nodes if n.id == node_id)
    d = NODE_DEFS[node.type]
    for e in g.edges:
        if e.target != node_id:
            continue
        value = (nodes[e.source].outputs or {}).get(e.source_port)
        port = d.input(e.target_port)
        if port and port.multiple:
            inputs.setdefault(e.target_port, []).append(value)
        else:
            inputs[e.target_port] = value
    return inputs


def execute_claimed(node_run_id: uuid.UUID, handlers: dict[str, Handler]) -> None:
    """Run the handler for a claimed node and record the result.

    Success: handler products and the node result commit together, after
    re-checking under the run lock that this attempt still holds the node;
    otherwise everything is rolled back (blobs are content-addressed and may
    stay). Failure: the handler transaction is rolled back and the failure is
    recorded in a fresh transaction.
    """
    with session_scope() as db:
        nr = db.get(NodeRun, node_run_id)
        if nr is None or nr.status != "running":
            return
        attempt = nr.attempt
        run = db.get(WorkflowRun, nr.run_id)
        assert run is not None
        g = _graph(run)
        gnode = next(n for n in g.nodes if n.id == nr.node_id)
        d = NODE_DEFS[nr.node_type]
        inputs = resolve_inputs(db, run, nr.node_id)
        # inputs are written with the outcome (success or failure): the handler transaction must not lock
        # the node run row, or the lease heartbeat could never extend it
        ctx = NodeContext(
            db=db,
            run=run,
            node_run=nr,
            params=d.params.model_validate(gnode.params),
            inputs=inputs,
            user=db.get(User, run.created_by),  # type: ignore[arg-type]
            part=db.get(Part, run.part_id) if run.part_id else None,
            project=db.get(Project, run.project_id),  # type: ignore[arg-type]
            attempt=attempt,
        )
        handler = handlers.get(nr.node_type)
        outcome: NodeOutcome | None = None
        failure: dict | None = None
        failed_calls: list = []
        retryable = False
        failed_with_products: uuid.UUID | None = None
        made_calls = _watch_calls(db, node_run_id)
        ctx._made_calls = made_calls
        ctx.limits = call_limits(db, run, nr, ctx)
        try:
            if handler is None:
                raise NodeFailure("handler_missing", f"no handler registered for {nr.node_type}")
            with _Heartbeat(ctx), call_retry.limits_scope(ctx.limits), team_scope((run.inputs or {}).get("team_snapshot")):
                outcome = handler(ctx)
            db.flush()
        except NodeFailure as exc:
            db.rollback()
            failure = {"code": exc.code, "message": exc.message, "details": exc.details}
            failed_calls = exc.calls
            # J: the bounded call-level retry already ran (or a limit stopped it): no node attempt on top
            retryable = exc.retryable and not call_retry.retries_spent(exc.calls)
        except Exception as exc:  # unexpected: keep the trace for operators, short message for users
            db.rollback()
            log.exception("node %s failed", node_run_id)
            failure = {"code": "internal_error", "message": str(exc)[:500], "details": {"trace": traceback.format_exc()[-2000:]}}
            retryable = True
        steps = (outcome.steps if outcome else []) or ctx.steps
        resolved_inputs = inputs
        lost_rows: list[dict[str, Any]] = []
        if failure is None and outcome is not None:
            try:
                held = _record_success(db, node_run_id, attempt, outcome, steps, resolved_inputs)
            except Exception as exc:  # e.g. a deadlock while locking: nothing of this attempt is kept
                lost_rows = _call_rows(made_calls)
                db.rollback()
                log.exception("could not record node %s", node_run_id)
                failure = {"code": "internal_error", "message": str(exc)[:500], "details": {"trace": traceback.format_exc()[-2000:]}}
                retryable = True
            else:
                if not held:
                    lost_rows = _call_rows(made_calls)
                    db.rollback()  # reclaimed or cancelled meanwhile: discard this attempt's products
                    _record_lost_calls(lost_rows, [], node_run_id)  # ... but not the record of its model calls
                    return
                failed_with_products = run.id if outcome.failure is not None else None
        if failure is None:
            if failed_with_products is not None:  # committed above (session_scope): products kept, node failed
                _after_node_failed(failed_with_products)
            return

    # Record the failure in a fresh transaction (the handler's was rolled back).
    failed_run = None
    with session_scope() as db:
        run_id = db.scalar(select(NodeRun.run_id).where(NodeRun.id == node_run_id))
        run = lock_run(db, run_id)
        nr = db.get(NodeRun, node_run_id)
        assert nr is not None
        lost = nr.status != "running" or nr.attempt != attempt
        if not lost:
            nr.steps = (nr.steps or []) + [{"attempt": nr.attempt, **s} for s in steps]
            nr.inputs = resolved_inputs  # the handler transaction was rolled back
            for row in lost_rows:  # calls of an attempt whose success could not be recorded
                db.add(AiCall(**row))
            if failed_calls:
                from ..services import ai as ai_svc

                ai_svc.record_calls(db, failed_calls, node_run_id)
            nr.lease_until = None
            nr.error = failure
            if retryable and nr.attempt < nr.max_attempts and not run.cancel_requested:
                nr.status = "ready"
            else:
                nr.status = "failed"
                nr.finished_at = _now()
            _activate_downstream(db, run)
            refresh_run_status(db, run)
            failed_run = run.id if nr.status == "failed" else None
    if lost:  # cancelled or reclaimed meanwhile: only the record of the calls this attempt made is kept
        _record_lost_calls(lost_rows, failed_calls, node_run_id)
        return
    if failed_run is not None:
        _after_node_failed(failed_run)


def _after_node_failed(run_id: uuid.UUID) -> None:
    """Hook: a node failed for good. Collaboration runs close the Tasks of the
    nodes that can no longer run (failed / skipped / cancelled steps), in their
    own transaction and lock order, so project completion is not blocked forever.
    Never breaks the engine."""
    try:
        from ..services import agent_runtime

        with session_scope() as db:
            agent_runtime.close_stranded_run(db, run_id)
    except Exception:  # noqa: BLE001 - the worker's maintenance sweep retries it
        log.exception("could not close the stranded tasks of run %s", run_id)


def _record_success(db: Session, node_run_id: uuid.UUID, attempt: int, outcome: NodeOutcome, steps: list[dict], inputs: dict) -> bool:
    """Write a successful outcome in the handler's transaction. False when the
    node is no longer running under `attempt` (caller rolls back)."""
    run_id = db.scalar(select(NodeRun.run_id).where(NodeRun.id == node_run_id))
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update().execution_options(populate_existing=True))
    nr = db.scalar(select(NodeRun).where(NodeRun.id == node_run_id).with_for_update().execution_options(populate_existing=True))
    if run is None or nr is None or nr.status != "running" or nr.attempt != attempt:
        return False
    nr.steps = (nr.steps or []) + [{"attempt": nr.attempt, **s} for s in steps]
    nr.inputs = inputs
    nr.lease_until = None
    nr.outputs = outcome.outputs
    if outcome.failure is not None:  # W8: products kept, required result not reached
        nr.status = "failed"
        nr.error = outcome.failure
        nr.finished_at = _now()
    elif outcome.wait_human:
        nr.status = "waiting_human"
        _on_waiting(db, run, nr)
    else:
        nr.status = "succeeded"
        nr.finished_at = _now()
    _activate_downstream(db, run)
    refresh_run_status(db, run)
    return True


def _on_waiting(db: Session, run: WorkflowRun, nr: NodeRun) -> None:
    """Hook (project management layer, backend B): a human_review node that
    starts waiting opens an engineering approval request in the same
    transaction. Runs in a savepoint and never breaks node bookkeeping."""
    try:
        from ..services.approval_requests import on_node_waiting

        with db.begin_nested():
            on_node_waiting(db, run, nr)
    except Exception:  # noqa: BLE001 - the run page decision still works without a request
        log.exception("could not open an approval request for node run %s", nr.id)


def complete_waiting(db: Session, run: WorkflowRun, node_id: str, *, approved: bool, error: dict | None = None) -> NodeRun:
    """Finish a waiting_human node after a decision. Caller holds the run lock and commits."""
    nodes = _nodes(db, run.id)
    nr = nodes[node_id]
    nr.status = "succeeded" if approved else "failed"
    nr.error = None if approved else (error or {"code": "rejected", "message": "rejected by reviewer"})
    nr.finished_at = _now()
    _activate_downstream(db, run)
    refresh_run_status(db, run)
    return nr


def cancel_run(db: Session, run: WorkflowRun) -> None:
    """Cancel: ready/pending/waiting nodes stop now; a running node finishes but does not advance."""
    run.cancel_requested = True
    for nr in _nodes(db, run.id).values():
        if nr.status in ("ready", "pending", "waiting_human"):
            nr.status = "cancelled"
            nr.finished_at = _now()
    refresh_run_status(db, run)


def retry_node(db: Session, run: WorkflowRun, node_id: str) -> NodeRun:
    """Retry a failed node under lock_run_for_retry; skipped downstream nodes become pending again."""
    from ..services import ai as ai_svc
    from ..services.feature_flags import require_run_continuation

    require_run_continuation(db, run.id)
    nodes = _nodes(db, run.id)
    nr = nodes[node_id]
    if nr.status != "failed":
        raise ValueError("only failed nodes can be retried")
    if run.cancel_requested:
        raise ValueError("the run was cancelled")
    ai_svc.refreeze_failed_run_models(db, run)  # a failed model selection froze nothing; a fixed configuration applies
    nr.status = "ready"
    nr.error = None
    nr.max_attempts = nr.attempt + 1
    g = _graph(run)
    downstream: set[str] = set()
    frontier = [node_id]
    while frontier:
        cur = frontier.pop()
        for e in g.edges:
            if e.source == cur and e.target not in downstream:
                downstream.add(e.target)
                frontier.append(e.target)
    for nid in downstream:
        if nodes[nid].status == "skipped":
            nodes[nid].status = "pending"
            nodes[nid].finished_at = None
        elif nodes[nid].status in ("succeeded", "failed") and _ran_on_failed_input(g, nodes, nid, downstream | {node_id}):
            _reset_pending(nodes[nid])  # W8: it ran without this input (Port.after_failure); run it again with it
    _reopen(run)
    refresh_run_status(db, run)
    return nr


def _ran_on_failed_input(g: Graph, nodes: dict[str, NodeRun], nid: str, retried: set[str]) -> bool:
    """Whether node ``nid`` ran with a tolerated input whose upstream is now being retried."""
    return any(e.target == nid and e.source in retried and _tolerates_failure(nodes[nid].node_type, e.target_port) for e in g.edges)


def _reset_pending(nr: NodeRun) -> None:
    """A node step goes back to pending to run its handler again, with a fresh retry budget."""
    nr.status = "pending"
    nr.lease_until = None
    nr.finished_at = None
    nr.error = None
    nr.max_attempts = nr.attempt + NODE_DEFS[nr.node_type].max_attempts if nr.node_type in NODE_DEFS else nr.attempt + 1


def requeue_node(db: Session, run: WorkflowRun, node_id: str) -> NodeRun:
    """Collaboration: a node that is not finished goes back through its handler
    (pending; ready at once when every upstream step has succeeded). Caller holds the run lock."""
    from ..services.feature_flags import require_run_continuation

    require_run_continuation(db, run.id)
    nodes = _nodes(db, run.id)
    nr = nodes[node_id]
    if nr.status in ("pending", "ready", "running", "waiting_human"):
        _reset_pending(nr)
    _reopen(run)
    _activate_downstream(db, run)
    refresh_run_status(db, run)
    return nr


def reopen_node(db: Session, run: WorkflowRun, node_id: str, *, reprepare: bool = False) -> dict[str, list[str]]:
    """Collaboration reopen (design §4 C04): the node waits for a decision again
    and its formal outputs are dropped from NodeRun.outputs. With `reprepare`
    (its upstream is not accepted, or its inputs changed) the node goes back to
    pending instead and is prepared again once its upstream steps succeed.
    Downstream steps that were not accepted yet (pending / ready / running /
    waiting_human) go back to pending; succeeded downstream steps keep their
    state (the caller raises impact notices) and the walk stops there. Caller
    holds the run lock. Returns {"reset": [...], "accepted": [...]}."""
    from ..services.feature_flags import require_run_continuation

    require_run_continuation(db, run.id)
    nodes = _nodes(db, run.id)
    nr = nodes[node_id]
    if reprepare:
        _reset_pending(nr)
    else:
        nr.status = "waiting_human"
        nr.finished_at = None
        nr.error = None
    nr.outputs = {k: v for k, v in (nr.outputs or {}).items() if k not in ("outputs", "accepted_submission_id", "decision_id")}
    nr.steps = (nr.steps or []) + [{"kind": "reopened", "ok": True, "at": _now().isoformat(), "detail": {"reprepare": reprepare}}]
    g = _graph(run)
    reset: list[str] = []
    accepted: list[str] = []
    seen: set[str] = {node_id}
    frontier = [node_id]
    while frontier:
        cur = frontier.pop()
        for e in g.edges:
            if e.source != cur or e.target in seen:
                continue
            seen.add(e.target)
            down = nodes[e.target]
            if down.status == "succeeded":
                accepted.append(e.target)
                continue
            if down.status in ("pending", "ready", "running", "waiting_human"):
                _reset_pending(down)
                reset.append(e.target)
            frontier.append(e.target)
    _reopen(run)
    if reprepare:
        _activate_downstream(db, run)
    refresh_run_status(db, run)
    return {"reset": reset, "accepted": accepted}


def recover_expired(db: Session) -> int:
    """Return nodes whose worker died (lease expired) to the queue. Lock order
    run → node run (as claim_next and the decision paths); busy rows are skipped
    and picked up by the next sweep."""
    now = _now()
    candidates = db.execute(select(NodeRun.id, NodeRun.run_id).where(NodeRun.status == "running", NodeRun.lease_until < now)).all()
    n = 0
    for cand in candidates:
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == cand.run_id).with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True))
        if run is None:
            continue
        nr = db.scalar(select(NodeRun).where(NodeRun.id == cand.id, NodeRun.status == "running", NodeRun.lease_until < now)
                       .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if nr is None:
            continue
        n += 1
        nr.lease_until = None
        if nr.attempt < nr.max_attempts and not run.cancel_requested:
            nr.status = "ready"
            nr.error = {"code": "lease_expired", "message": "worker stopped; retrying"}
        else:
            nr.status = "failed"
            nr.error = {"code": "lease_expired", "message": "worker stopped and no attempts are left"}
            nr.finished_at = now
            _activate_downstream(db, run)
        refresh_run_status(db, run)
    return n


def run_until_idle(handlers: dict[str, Handler], max_steps: int = 200) -> int:
    """Synchronously drain the queue (tests and scripts)."""
    steps = 0
    while steps < max_steps:
        with session_scope() as db:
            nr = claim_next(db)
            nid = nr.id if nr else None
        if nid is None:
            break
        execute_claimed(nid, handlers)
        steps += 1
    return steps
