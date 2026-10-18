"""AI-native main agent: turns, sub-agents, wake-up, confirmation and undo (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md).

A *turn* of the main agent is an ``ask`` operation with ``request.runtime = "orchestrator"`` (one per turn; no new kind).
``send`` routes covered ``auto`` and read-only ``ask`` requests here (:func:`routed`) while the main runtime is enabled,
including requests whose frozen model is unavailable; ``execute`` leaves the turn ``pending`` and the assistant worker runs it
(:func:`run_next_turn`): claim (one active turn per conversation), context build (the ask build + ``<work>`` / ``<event>``),
then the agent — the tool agent with role ``orchestrator`` on a real model, the deterministic planner
(``ai/orchestrator_agent.plan_next``) in simulation — calling the same tools through :class:`TurnControl`. Each tool call
is a *step* of ``op.result.steps``, written in its own short transaction (the 2 s polling shows the timeline live).

Sub-agents are ordinary operations (``model_create`` / ``model_modify`` / ``drawing_generate``) started by
:func:`spawn_child` with ``request.parent_op_id``. When one with a run finishes (succeeded / failed), ``_on_terminal``
queues it (:func:`queue_wake`) and ``_touch_conversations`` — after every operation lock — drains the queue
(:func:`drain_wake`): a ``sub_agent_finished`` notice and the next turn, at most once per child. The worker follows active
sub-agents (:func:`follow_children`) so that this happens with no page open.

Locks: operation → conversation, as everywhere in the workspace. A send supersedes a waiting confirmation with SKIP
LOCKED (it holds the conversation lock), so it never waits on a turn row another request holds.
"""

from __future__ import annotations

import logging
import copy
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..ai import evidence, orchestrator_agent, retry as call_retry, team_prompt
from ..ai.harness.base import AttemptSpec, AttemptStopped, HarnessEvent, stopped
from ..ai.orchestrator_agent import ORCHESTRATOR_PROMPT_VERSION, OrchestratorReply
from ..ai.providers.base import ProviderError, clip
from ..ai.tool_registry import ORCHESTRATOR_TOOL_SPECS
from ..ai.timeouts import MAIN_QUEUE_STALE_S, MAIN_TURN_S
from ..config import get_settings
from ..db import new_session, session_scope
from ..errors import AppError, conflict
from ..models import AiConversation, AiMessage, AiOperation, AiTaskDraft, ModelRevision, Part, Task, User
from . import assistant as asst
from . import feature_flags as features
from . import orchestrator_tools as tools
from .assistant_tool_inputs import ChildInput, design_part_id
from .worker_heartbeat import background_heartbeat

log = logging.getLogger("partroute.orchestrator")

RUNTIME = "orchestrator"
#: Model requests, tool calls and wall time of one turn (design §2.3).
MAX_STEPS = 8
MAX_TOOL_CALLS = 10
MAX_DURATION_S = MAIN_TURN_S
#: A running turn whose steps stopped moving this long ago was interrupted (its worker died).
TURN_STALE = timedelta(minutes=15)
#: A turn still queued after this long has no worker to run it (turns wait for others: MAX_DURATION_S each).
TURN_QUEUE_STALE = timedelta(seconds=MAIN_QUEUE_STALE_S)
#: Step statuses (design §2.3).
STEP_STATUSES = ("running", "ok", "failed", "waiting_confirm", "waiting_design", "declined", "superseded", "undone")


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except (TypeError, ValueError):
        return None


def is_turn(op: AiOperation) -> bool:
    return op.kind == "ask" and (op.request or {}).get("runtime") == RUNTIME


def _turn_filter() -> tuple:
    return AiOperation.kind == "ask", AiOperation.request["runtime"].astext == RUNTIME


def root_of(op: AiOperation) -> uuid.UUID:
    return _uuid((op.request or {}).get("root_op_id")) or op.id


# --- routing (decision 2) ---------------------------------------------------------------------------------------


def _enabled() -> bool:
    """``assistant_runtime=native`` is the rollback to read-only answers (acceptance 6); ``assistant_orchestrator=false``
    also stops the main Agent; new auto requests remain read-only answers."""
    s = get_settings()
    return s.assistant_runtime != "native" and s.assistant_orchestrator


def orchestrator_runtime(snapshot: dict | None) -> bool:
    """Whether a main agent can run on this frozen assistant snapshot: the labelled simulated model, or an OpenAI-family
    configuration that calls tools (and the main agent is enabled)."""
    from . import ai as ai_svc

    if not _enabled() or not ai_svc.is_snapshot(snapshot):
        return False
    assert snapshot is not None
    if snapshot.get("simulated"):
        ai_svc.require_simulation_allowed()
        return True
    from ..ai.harness.tool_agent import TOOL_FAMILIES  # the configurations the tool agent speaks to

    return snapshot.get("provider") in TOOL_FAMILIES and bool((snapshot.get("capabilities") or {}).get("tools"))


def available(db: Session) -> bool:
    """``capabilities.orchestrator``: an ``auto`` request of this workspace goes to the main agent now."""
    from . import ai as ai_svc

    return _enabled() and orchestrator_runtime(ai_svc.try_freeze_model(db, "assistant", None))


def routed(operation: str) -> bool:
    """Route by request semantics, never by whether a configured model happens to be usable.

    Missing capabilities or failed probes belong to the frozen turn's diagnostic. They must not bypass
    main-agent confirmation by sending the request into an older direct operation.
    """
    return operation in ("auto", "ask") and _enabled()


def require_enabled(op: AiOperation) -> None:
    """A turn is only retried / confirmed while the main agent is on (the rollback stops chains under way too)."""
    if is_turn(op) and not _enabled():
        raise conflict("orchestrator_disabled", "the main agent is turned off on this server; send the request again")


def default_autonomy(db: Session, user: User) -> str:
    """Initialize the conversation from the account preference; each request freezes its own copy."""
    from .preferences import freeze

    return freeze(db, user.id)["ai_autonomy"]


def as_turn(snapshot: dict) -> dict:
    """A send snapshot as a turn's (the root is the turn itself: :func:`root_turn` once it has an id)."""
    return {**snapshot, "operation": "ask", "runtime": RUNTIME, "trigger": {"type": "request"}}


def root_turn(op: AiOperation) -> None:
    """A turn the person's request started is its own root (``request.root_op_id``); other operations are left alone."""
    if is_turn(op) and ((op.request or {}).get("trigger") or {}).get("type") == "request":
        op.request = {**op.request, "root_op_id": str(op.id)}


def before_send(db: Session, conv: AiConversation) -> None:
    """Under the conversation lock, before a new request is written: 409 agent_busy while a turn is pending / running
    (decision 10); a turn waiting for a confirmation is superseded by the new message (decision 7)."""
    busy = db.scalar(select(AiOperation.id).where(AiOperation.conversation_id == conv.id, AiOperation.status.in_(("pending", "running")),
                                                  *_turn_filter()).limit(1))
    if busy is not None:
        raise conflict("agent_busy", "the main agent is still working on this conversation; wait for it or stop it",
                       operation_id=str(busy))
    waiting = db.scalars(select(AiOperation).where(AiOperation.conversation_id == conv.id, AiOperation.status == "waiting_input",
                                                   *_turn_filter()).with_for_update(skip_locked=True)).all()
    for op in waiting:
        from . import design_plans
        design_plans.close(db.get(AiOperation, root_of(op)))
        _close_waiting(op, "superseded")
        op.status, op.question = "succeeded", None
        asst._on_terminal(db, op)


def _close_waiting(op: AiOperation, status: str) -> None:
    """The steps of a turn still waiting for the person become ``status`` (superseded / declined)."""
    res = dict(op.result or {})
    res["steps"] = [{**s, "status": status, "finished_at": _now().isoformat()} if s.get("status") in ("waiting_confirm", "waiting_design") else s
                    for s in res.get("steps") or []]
    op.result = res


# --- sub-agents and wake-up (decisions 4, 5) ---------------------------------------------------------------------

#: The request fields a woken turn carries over from its root (the person's words and context; not their materials,
#: which the root turn already read).
_CARRIED = ("text", "requested_operation", "target", "references", "constraints", "model_config_id", "execution_policy",
            "policy_source", "policy_source_version", "requested_execution_policy", "model_snapshot",
            "model_snapshots", "preferences_snapshot", "team_snapshot", "project_version", "read_only", "materials", "revise", "design_ref", "reuse_earlier_builds")


def _child_scope(kind: str, target: dict | None, references: list[dict], tool_input: dict,
                 *, spec_outputs: dict[str, dict] | None = None) -> dict:
    """The work identity within one human request, independent of a model's paraphrasing.

    Exact modification bases/selections and drawing variants stay distinct. Creation of a named standalone
    document is once per title in a root; a fresh human request can create another. Failed/partial work keeps its
    existing retry/Continue boundary instead of silently rebuilding all saved intermediate assets on a wake.
    """
    def identity(ref: dict | None) -> dict:
        return {k: ref[k] for k in ("kind", "object_id", "version", "node", "selection", "instance", "entities", "output")
                if ref and ref.get(k) is not None}

    if kind == "spec_extract":
        # The human text is already fixed by the root. A generated spec contains those same sources; using it
        # as the next base (or renaming the title) is not new input. Expand only outputs this root CREATED,
        # so different preexisting exact bases and additional file versions remain distinct work.
        sources: dict[str, dict] = {}

        def add_source(ref: dict | None, expanding: frozenset[str] = frozenset()) -> None:
            if not ref or ref.get("kind") not in ("file_version", "engineering_spec_version"):
                return
            key = str(ref.get("object_id") or "")
            original = (spec_outputs or {}).get(key) if ref["kind"] == "engineering_spec_version" else None
            if original is not None and key not in expanding:
                for source in [original.get("target"), *(original.get("references") or [])]:
                    add_source(source, expanding | {key})
                return
            value = identity(ref)
            sources[json.dumps(value, sort_keys=True)] = value

        for source in [target, *references]:
            add_source(source)
        return {"sources": [sources[key] for key in sorted(sources)],
                **({'design_ref': tool_input['design_ref']} if tool_input.get('design_ref') else {}),
                "source_operation_id": tool_input.get("source_operation_id")}

    scope: dict[str, Any] = {"target": identity(target)}
    if tool_input.get('design_ref'):
        scope['design_ref'] = tool_input['design_ref']
    reference_kinds = ({"file_version", "engineering_spec_version"} if kind in ("assembly_create", "spec_extract")
                       else {"part"} if kind in ("drawing_intake", "drawing_reconstruct") else set())
    if reference_kinds:
        scope["references"] = sorted((identity(r) for r in references if r.get("kind") in reference_kinds),
                                     key=lambda r: json.dumps(r, sort_keys=True))
    if kind == "drafting_create":
        scope["title"] = " ".join(str(tool_input.get("title") or "").split()).casefold()
    if kind == "drawing_generate":
        scope["requirements"] = " ".join(str(tool_input.get("requirements") or "").split())
    if kind == "drawing_reconstruct":
        scope["sheet"] = str(tool_input.get("sheet") or "").strip()
    if kind == "assembly_create":
        scope["source_operation_id"] = tool_input.get("source_operation_id")
    return scope


def _human_source_snapshot(root: AiOperation) -> dict:
    return {"operation_id": str(root.id), "message_id": str(root.message_id),
            "text": str((root.request or {}).get("text") or "")}


def _continued_engineering_source(db: Session, user: User, root: AiOperation, source_id: str) -> tuple[dict, dict]:
    """Bind a new human turn to exact prior input, never to the model's reconstruction of the conversation.

    Only source data crosses this boundary. Execution/model/preferences remain frozen by the new human request.
    Existing requests and messages are never edited, including when the source already continued an earlier turn.
    """
    previous = db.get(AiOperation, _uuid(source_id))
    if (previous is None or previous.conversation_id != root.conversation_id or previous.user_id != user.id
            or previous.project_id != root.project_id or previous.kind not in ("assembly_create", "spec_extract")):
        raise AppError(404, "source_operation_unavailable", "no eligible engineering source operation in this conversation")
    prior_message, current_message = db.get(AiMessage, previous.message_id), db.get(AiMessage, root.message_id)
    if prior_message is None or current_message is None or prior_message.seq >= current_message.seq:
        raise conflict("source_operation_not_prior", "continuation must refer to an earlier human request")
    if previous.status in asst.ACTIVE:
        raise conflict("source_operation_active", "the source operation is still active; inspect or continue its existing checkpoint")
    req = previous.request or {}
    source = copy.deepcopy(req.get("human_source"))
    if not isinstance(source, dict):
        origin = db.get(AiOperation, root_of(previous))
        if (origin is None or origin.conversation_id != root.conversation_id or origin.user_id != user.id
                or origin.project_id != root.project_id):
            raise AppError(404, "source_operation_unavailable", "the original engineering source is unavailable")
        source = _human_source_snapshot(origin)
    if "amendments" not in source:
        source["material_keys"] = [m["key"] for m in req.get("materials") or [] if m.get("new") and m.get("key")]
    amendment = _human_source_snapshot(root)
    amendment["material_keys"] = [m["key"] for m in (root.request or {}).get("materials") or [] if m.get("new") and m.get("key")]
    source["amendments"] = [*(source.get("amendments") or []), amendment]
    return source, req


def spawn_child(db: Session, user: User, ctx: tools.ToolContext, *, kind: str, text: str, target: dict | None, task_id: Any,
                domain_input: ChildInput | None = None) -> AiOperation:
    """A sub-agent: an operation of ``kind`` on ``target`` recorded with the turn's message (no new user message, the
    message keeps its operation), its own card (an ``operation`` message), ``client_request_id = uuid5(turn, step)`` (a
    replayed step returns the same child), with the root policy and model bundle. Then run the operation's own business
    step (``assistant.execute``). Not while the turn is no longer running."""
    from ..policy import frozen_choice

    crid = uuid.uuid5(ctx.turn_id, f"step:{ctx.step}")
    conv = asst.lock_conversation(db, ctx.conversation_id)
    asst.require_open(conv)
    found = db.scalar(select(AiOperation).where(AiOperation.conversation_id == conv.id, AiOperation.client_request_id == crid))
    if found is not None:
        db.commit()
        return found
    turn = db.get(AiOperation, ctx.turn_id)
    assert turn is not None
    if turn.status != "running":
        raise conflict("turn_stopped", "the main agent's turn was stopped; no sub-agent is started")
    req = turn.request or {}
    references = list(req.get("references") or [])
    if domain_input is not None:
        references = list({r["key"]: r for r in [*domain_input.references, *references]}.values())
    tool_input = domain_input.payload() if domain_input is not None else {}
    resolved_design = None
    if tool_input.get('design_ref'):
        from .design_plans import resolve_authorized_design
        resolved_design = resolve_authorized_design(db, user, tool_input['design_ref'], conversation_id=conv.id,
                                                     root_operation_id=root_of(turn), tool_kind=kind)
        root = next(d for d in resolved_design.content.definitions if d.key == resolved_design.content.root)
        if kind in ("model_create", "assembly_create") and (root.kind == "assembly") != (kind == "assembly_create"):
            raise conflict("design_kind_mismatch", "the child operation must match the adopted design root kind")
        if kind == "model_create" and (references or not target or target.get("kind") != "part"
                or str(target.get("object_id")) != str(design_part_id(root_of(turn), resolved_design.ref))):
            raise conflict("design_scope_mismatch", "a part design may only create its own new part's model")
    # A completion wake is another turn of the same request, not permission to repeat its write. The conversation
    # lock serializes this check with creation, including distinct simultaneous model tool calls.
    candidates = db.scalars(select(AiOperation).where(
        AiOperation.conversation_id == conv.id, AiOperation.kind == kind,
        AiOperation.request["root_op_id"].astext == str(root_of(turn)),
        AiOperation.request["parent_op_id"].astext.is_not(None),
    ).order_by(AiOperation.created_at, AiOperation.id)).all()
    human_source = None
    materials = copy.deepcopy(req.get("materials") or [])
    constraints = list(req.get("constraints") or [])
    revise = req.get("revise")
    if kind in ("assembly_create", "spec_extract"):
        root = db.get(AiOperation, root_of(turn))
        assert root is not None and root.conversation_id == conv.id
        source_id = tool_input.get("source_operation_id")
        # A wake can omit the optional binding; it still belongs to the same frozen human request. Reuse a
        # binding only from this root, never guess one from unrelated conversation history.
        if not source_id and turn.id != root.id:
            bound = {(c.request.get("tool_input") or {}).get("source_operation_id") for c in candidates}
            if len(bound) > 1:
                raise conflict("source_operation_required", "select the original source operation for this continuation")
            source_id = next(iter(bound), None)
        if source_id:
            from .assistant_refs import AiRefInput, normalize_ref

            human_source, prior = _continued_engineering_source(db, user, root, source_id)
            tool_input["source_operation_id"] = source_id
            inherited = [r for r in [prior.get("target"), *(prior.get("references") or [])]
                         if isinstance(r, dict) and r.get("kind") in ("file_version", "engineering_spec_version")]
            for ref in inherited:
                # Access/lifecycle can change after the original request; validate now while preserving its
                # exact version and source identity, rather than resolving a current head.
                normalize_ref(db, user, ctx.project_id, AiRefInput.model_validate(ref))
            references = list({r["key"]: copy.deepcopy(r) for r in [*inherited, *references]}.values())
            if target is None and prior.get("target"):
                target = copy.deepcopy(prior["target"])
            materials = list({m["key"]: copy.deepcopy(m) for m in [*materials, *(prior.get("materials") or [])]}.values())
            constraints = list({json.dumps(c, sort_keys=True): c for c in [*(prior.get("constraints") or []), *constraints]}.values())
            if revise is None:
                from .attachments import request_sources, spec_material_mix

                files, base_spec = request_sources(target, references)
                old_files = {r["object_id"] for r in inherited if r["kind"] == "file_version"}
                added_files = [r["object_id"] for r in files if r["object_id"] not in old_files]
                # A keep/revise choice covered the earlier inputs only. A plain retry retains it; a new
                # engineering amendment or file needs the existing explicit human spec choice again.
                if base_spec:
                    _, new_files, text_changes = spec_material_mix(db, spec_version_id=base_spec["object_id"],
                        file_version_ids=added_files, text=str((root.request or {}).get("text") or ""), instruction=True)
                    if not new_files and not text_changes:
                        revise = prior.get("revise")
        else:
            human_source = _human_source_snapshot(root)
    spec_outputs = {str((previous.result or {})["spec_version_id"]): previous.request or {} for previous in candidates
                    if kind == "spec_extract" and (previous.result or {}).get("created") is True
                    and (previous.result or {}).get("spec_version_id")}
    scope = _child_scope(kind, target, references, tool_input, spec_outputs=spec_outputs)
    matching = [previous for previous in candidates
                if _child_scope(kind, (previous.request or {}).get("target"), list((previous.request or {}).get("references") or []),
                                (previous.request or {}).get("tool_input") or {}, spec_outputs=spec_outputs) == scope]
    if matching:
        previous = matching[0]
        # An explicit human Retry may create a replacement operation in this root; follow that authorized chain,
        # never select an unrelated legacy duplicate merely because it happened to be created later.
        retries = {row.retry_of: row for row in matching if row.retry_of}
        while previous.id in retries:
            previous = retries[previous.id]
        db.commit()
        return previous
    if resolved_design is not None and kind == "model_create":
        if db.scalar(select(ModelRevision.id).where(ModelRevision.part_id == design_part_id(root_of(turn), resolved_design.ref)).limit(1)):
            raise conflict("design_scope_mismatch", "a new part plan cannot overwrite an already modeled part")
    tid = _uuid(task_id)
    if tid is not None:
        asst._check_task_in_project(db, tid, conv.project_id)  # as a send: never a task of another project
        features.require_enabled("tasks")
    if kind == "task_draft":
        features.require_enabled("tasks")
    choice = frozen_choice(turn.execution_policy, req.get("policy_source"), req.get("policy_source_version"),
                           project_version=req.get("project_version"))
    # A failed selection of the root froze no model: the sub-agent resolves it again (as a retry does), so a configuration
    # fixed since the request (detection run, key entered, default assigned) applies to the rest of the chain instead of
    # replaying the old failure. Frozen snapshots are inherited exactly.
    from . import ai as ai_svc

    bundle = ai_svc.refreeze_failed(db, req.get("model_snapshots"),
                                    preferred={asst.OP_PURPOSE.get(turn.kind, "assistant"): req["model_config_id"]}
                                    if req.get("model_config_id") else None)
    entry = ((bundle or {}).get("entries") or {}).get(asst.OP_PURPOSE.get(kind, "assistant"), {})
    snapshot = {
        "text": text, "operation": kind, "requested_operation": kind, "target": target, "references": references,
        "constraints": constraints, "task_id": str(tid) if tid else None, "model_config_id": None,
        "client_request_id": str(crid), "execution_policy": choice.policy, "policy_source": choice.source,
        "policy_source_version": choice.source_version, "requested_execution_policy": req.get("requested_execution_policy"),
        "project_version": choice.project_version,
        "model_snapshot": entry.get("snapshot") if bundle is not None else req.get("model_snapshot"),
        "model_snapshots": bundle, "preferences_snapshot": req.get("preferences_snapshot"),
        "team_snapshot": copy.deepcopy(req.get("team_snapshot")),
        "read_only": req.get("read_only", False), "materials": materials,
        "revise": revise, "reuse_earlier_builds": req.get("reuse_earlier_builds"), "tool_input": tool_input,
        "parent_op_id": str(turn.id), "root_op_id": str(root_of(turn)), "spawned_by": {"operation_id": str(turn.id), "step": ctx.step},
    }
    if human_source is not None:
        # The tool's text is step intent, not authoritative engineering input. Read the immutable human root
        # directly, including on completion wakes; no model argument or ChildInput can supply this snapshot.
        snapshot["human_source"] = human_source
    elif kind == "model_create" and resolved_design is not None:
        root = db.get(AiOperation, root_of(turn))
        assert root is not None and root.conversation_id == conv.id
        snapshot["human_source"] = _human_source_snapshot(root)
        snapshot["text"] = snapshot["human_source"]["text"]
    msg = db.get(AiMessage, turn.message_id)
    assert msg is not None
    child = asst._create_operation(db, user, conv, msg, kind=kind, request=snapshot, task_id=tid, client_request_id=crid)
    db.commit()
    asst.execute(db, user, child.id)
    out = db.get(AiOperation, child.id)
    assert out is not None
    db.refresh(out)
    turn = db.get(AiOperation, ctx.turn_id)
    db.refresh(turn)  # type: ignore[arg-type]
    if turn is not None and turn.status == "cancelled" and out.status in asst.ACTIVE:
        out = asst.cancel(db, user, out.id)  # the turn was stopped while its sub-agent started: stop that run too
    return out


def child_brief(op: AiOperation) -> dict:
    """A sub-agent as the main agent and the work panel see it: status, error, question and its result in brief (model
    revision, label and check status; drawing ids) — never its context."""
    from sqlalchemy.orm import object_session
    from .cad_results import projected_card

    res = op.result if isinstance(op.result, dict) else {}
    db = object_session(op)
    card = projected_card(db, op) if db is not None else res.get("cad_result")
    card = card if isinstance(card, dict) else {}
    items = [i for i in res.get("items") or [] if isinstance(i, dict)]
    primary = next((i for i in items if (
        card.get("asset_version_id") and i.get("asset_version_id") == card["asset_version_id"]
        or card.get("version_id") and (i.get("ref") or {}).get("object_id") == card["version_id"])), None)
    # The revision a sub-agent *made* comes only from its own result; the revision it started from is apart (a failed
    # change must never look like a model to draw, acceptance 2).
    model = None
    if op.kind in ("model_create", "model_modify", "drawing_reconstruct"):
        models = [i for i in items if i.get("asset_type") == "part_model"]
        if card.get("object_type") == "part_model":
            model = primary
        elif not card and len(models) == 1:
            model = models[0]
    target = (op.request or {}).get("target") or {}
    rev = card.get("version_id") if card.get("object_type") == "part_model" else ((model or {}).get("ref") or {}).get("object_id")
    assembly = res.get("assembly")
    if op.kind == "assembly_create":
        value = next((s.get("value") for s in (res.get("chain") or {}).get("steps") or []
                      if s.get("node_id") == "assembly" and isinstance(s.get("value"), dict)), None)
        if value:
            assembly = {k: value[k] for k in ("assembly_id", "assembly_revision_id", "rev_no", "label", "complete") if k in value}
    if card.get("object_type") == "assembly":
        assembly = {**(assembly or {}), "assembly_id": card["object_id"],
                    "assembly_revision_id": card["version_id"], "label": card["version_label"]}
    # Keep all historical items on the operation; a completion advertises the current principal first,
    # without presenting its older versions as alternative outputs of this generation.
    principal_type = (primary or {}).get("asset_type")
    result_items = ([primary] if primary else []) + [i for i in items if i is not primary
                    and (not principal_type or i.get("asset_type") != principal_type)]
    base = target.get("object_id") if target.get("kind") in ("part_revision", "cad_selection") else None
    q = op.question if isinstance(op.question, dict) else None
    plan_only = op.kind in ("workflow_edit", "workflow_create", "assembly_modify", "drafting_modify")
    application = res.get("application") or {}
    applied_state = application.get("state") if isinstance(application, dict) else None
    if ((res.get("cad_result") or {}).get("undo") or {}).get("restored"):
        applied_state = "undone"
    outcome = applied_state if applied_state in ("applied", "undone") else "plan_ready" if plan_only and op.status == "succeeded" else op.status
    return {"operation_id": str(op.id), "root_operation_id": str(root_of(op)), "parent_operation_id": (op.request or {}).get("parent_op_id"),
            "kind": op.kind, "status": op.status, "outcome": outcome, "part_id": str(op.part_id) if op.part_id else None,
            "task_id": str(op.task_id) if op.task_id else None, "revision_id": rev,
            "revision_label": card.get("version_label") if card.get("object_type") == "part_model" else (model or {}).get("label"),
            "base_revision_id": base or res.get("base_revision_id"), "generation": op.generation,
            "latest_event_id": (res.get("completion_event") or {}).get("event_id"),
            "event_seq": (res.get("completion_event") or {}).get("event_seq"),
            "plan_hash": res.get("plan_hash"), "base_version_id": res.get("base_version_id"),
            "workflow_id": application.get("workflow_id") or res.get("workflow_id"),
            "document_id": res.get("document_id"), "drawing_document_version_id": res.get("drawing_document_version_id"),
            "spec_version_id": res.get("spec_version_id"), "spec": res.get("spec"), "assembly": assembly, "chain": res.get("chain"),
            "recovery": ((op.error or {}).get("details") or {}).get("recovery"),
            "application": application or None, "output_status": res.get("output_status"),
            "result_refs": [i["ref"] for i in result_items if i.get("ref")],
            "check_status": (model or {}).get("check_status"),
            "drawing_ids": [card["version_id"]] if card.get("object_type") == "part_drawing" else
                           [str(d.get("drawing_id")) for d in res.get("drawings") or [] if isinstance(d, dict) and d.get("drawing_id")],
            "error": _error_brief(op.error),
            "question": {"kind": q.get("kind"), "questions": list(q.get("questions") or [])} if q else None}


def _error_brief(err: Any) -> dict | None:
    """A sub-agent's failure as the main agent sees it: code, message, the failed check items of its report (what the
    person has to fix, e.g. a dimension the drawing lacks) and the questions / unsupported requests it raised, at most
    five each."""
    if not isinstance(err, dict):
        return None
    report = (err.get("details") or {}).get("report") if isinstance(err.get("details"), dict) else None
    items = [str(i.get("message")) for i in (report or {}).get("items") or [] if isinstance(i, dict) and i.get("status") == "failed" and i.get("message")]
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    # A needs_input / unsupported failure is only actionable with what it asked or refused.
    asked = {k: [str(q) for q in details[k]][:5] for k in ("questions", "unsupported") if isinstance(details.get(k), list) and details[k]}
    return {"code": err.get("code"), "message": err.get("message"), **({"problems": items[:5]} if items else {}), **asked}


def queue_wake(db: Session, op: AiOperation) -> None:
    """Commit the immutable event with the child result; delivery happens after commit."""
    from . import operation_events

    if op.status in asst.TERMINAL and (op.request or {}).get("parent_op_id"):
        operation_events.emit(db, op)


def drain_wake(db: Session) -> None:
    """Compatibility entry point for the dedicated consumer (a fresh transaction only)."""
    from . import operation_events

    operation_events.drain(db)


def _turn_in(db: Session, conversation_id: uuid.UUID, statuses: tuple[str, ...], *, other_than: uuid.UUID | None = None) -> uuid.UUID | None:
    """A turn of the conversation in one of ``statuses`` (other than ``other_than``)."""
    q = select(AiOperation.id).where(AiOperation.conversation_id == conversation_id, AiOperation.status.in_(statuses), *_turn_filter())
    if other_than is not None:
        q = q.where(AiOperation.id != other_than)
    return db.scalar(q.limit(1))


def _turn_of(db: Session, conversation_id: uuid.UUID, crid: uuid.UUID) -> AiOperation | None:
    return db.scalar(select(AiOperation).where(AiOperation.conversation_id == conversation_id, AiOperation.client_request_id == crid))


def _queue_turn(db: Session, conv: AiConversation, root: AiOperation, trigger: dict, crid: uuid.UUID) -> AiOperation:
    """The next turn of a chain (pending; the worker runs it): the root request's words and context, the conversation's
    task now, recorded with the root's message. The caller holds the conversation lock and checked ``crid`` is new."""
    rreq = root.request or {}
    task_id = conv.task_id or _uuid(rreq.get("task_id"))
    req = {**{k: rreq.get(k) for k in _CARRIED}, "operation": "ask", "task_id": str(task_id) if task_id else None,
           "client_request_id": str(crid), "runtime": RUNTIME, "root_op_id": str(root.id), "trigger": trigger}
    req['design_ref'] = ((root.result or {}).get('design_plan_state') or {}).get('current_ref')
    user, msg = db.get(User, root.user_id), db.get(AiMessage, root.message_id)
    assert user is not None and msg is not None
    conv.last_activity_at = _now()
    return asst._create_operation(db, user, conv, msg, kind="ask", request=req, task_id=task_id, client_request_id=crid)


def follow_children() -> int:
    """Synchronize domains, commit, then deliver outbox events in a separate transaction."""
    from ..models import OutboxEvent
    from . import operation_events

    try:
        with session_scope() as db:
            touched: set[uuid.UUID] = set()
            emitted = select(OutboxEvent.id).where(OutboxEvent.ai_operation_id == AiOperation.id,
                OutboxEvent.ai_generation == AiOperation.generation, OutboxEvent.ai_phase == "terminal").exists()
            ops = db.scalars(select(AiOperation).where(AiOperation.request.has_key("parent_op_id"),
                or_(AiOperation.status.in_(asst.ACTIVE), ~emitted)).order_by(AiOperation.id)).all()
            for op in ops:
                if op.status in asst.ACTIVE:
                    asst.sync_operation(db, op, defer=touched)
                elif op.status in asst.TERMINAL:
                    queue_wake(db, op)
            asst._touch_conversations(db, touched)
            count = len(ops)
        with session_scope() as db:
            count += operation_events.drain(db)
        return count
    except Exception:  # noqa: BLE001 - the worker loop must go on
        log.exception("following sub-agents failed")
        return 0


def sync_turn(db: Session, op: AiOperation, *, defer: set[uuid.UUID] | None = None) -> AiOperation:
    """``sync_operation`` of a turn (it has no run): a pending turn older than STALE_PENDING or a running one whose steps
    stopped moving for TURN_STALE was interrupted; a turn waiting for a confirmation waits. The age is checked again under
    the lock (a turn the worker has just claimed is live)."""
    def stale(o: AiOperation) -> bool:
        limit, stamp = {"pending": (TURN_QUEUE_STALE, o.created_at), "running": (TURN_STALE, o.updated_at)}.get(o.status, (None, None))
        return limit is not None and stamp is not None and stamp < _now() - limit

    if not stale(op):
        return op
    op = asst._lock_op(db, op.id)
    if stale(op):
        op.status = "failed"
        op.error = {"code": "interrupted", "message": "the main agent's turn was interrupted; send the request again", "details": {}}
        asst._on_terminal(db, op, defer=defer)
    return op


# --- turn actions: cancel, confirm, undo (decisions 7, 10; design §2.4) ---------------------------------------------


def cancel_turn(db: Session, user: User, op: AiOperation) -> AiOperation:
    """Stop a turn: it stops before its next step, a waiting confirmation is closed, and its active sub-agents are
    cancelled too (their runs stop; what they finished stays)."""
    op = asst._lock_op(db, op.id)
    from . import design_plans
    asst.lock_conversation(db, op.conversation_id)
    design_plans.close(db.get(AiOperation, root_of(op), populate_existing=True))
    if op.status in asst.ACTIVE:
        _close_waiting(op, "superseded")
        op.status, op.question = "cancelled", None
        asst._on_terminal(db, op)
    # Queued continuations of this turn (woken by its sub-agents, or its confirmed step) stop with it.
    children = set(map(str, db.scalars(select(AiOperation.id).where(AiOperation.request["parent_op_id"].astext == str(op.id))).all()))
    for nxt in db.scalars(select(AiOperation).where(AiOperation.conversation_id == op.conversation_id, AiOperation.status == "pending",
                                                    *_turn_filter()).with_for_update(skip_locked=True)).all():
        if ((nxt.request or {}).get("trigger") or {}).get("operation_id") in children | {str(op.id)}:
            nxt.status = "cancelled"
            asst._on_terminal(db, nxt)
    db.commit()
    for child_id in db.scalars(select(AiOperation.id).where(AiOperation.conversation_id == op.conversation_id,
                                                            AiOperation.status.in_(asst.ACTIVE),
                                                            AiOperation.request["parent_op_id"].astext == str(op.id))).all():
        try:
            asst.cancel(db, user, child_id)
        except AppError:
            db.rollback()  # e.g. a sub-agent without a run: it finishes on its own and wakes nobody (the turn is cancelled)
    out = db.get(AiOperation, op.id)
    assert out is not None
    return out


def _steps(op: AiOperation) -> list[dict]:
    return [dict(s) for s in (op.result or {}).get("steps") or [] if isinstance(s, dict)]


def _put_step(op: AiOperation, step: dict) -> None:
    steps = [s for s in _steps(op) if s.get("index") != step["index"]] + [step]
    op.result = {**(op.result or {}), "steps": sorted(steps, key=lambda s: s["index"])}


def _ctx(op: AiOperation, step: int, limit_chars: int = tools.RESULT_CHARS_MAX) -> tools.ToolContext:
    return tools.ToolContext(turn_id=op.id, step=step, user_id=op.user_id, project_id=op.project_id, conversation_id=op.conversation_id,
                             limit_chars=limit_chars)


def confirm(db: Session, user: User, operation_id: uuid.UUID, accept: bool) -> AiOperation:
    """POST /ai/operations/{id}/confirm. Accept: the waiting step's tool runs now (same function, same user), the turn
    succeeds and the next turn is queued (``uuid5(turn, "confirm:<step>")``). Decline: the step is declined, the turn
    succeeds and a notice says so — no model call. 409 not_waiting for a turn that never waited; a decided confirmation
    returns the turn as it is (replay-safe: the step runs at most once). Commits."""
    op, conv, _ = asst.load_operation(db, user, operation_id)
    asst.require_open(conv)
    require_enabled(op)
    op = asst._lock_op(db, op.id)
    q = op.question if isinstance(op.question, dict) else {}
    if op.status != "waiting_input" or q.get("kind") != "confirm":
        if is_turn(op) and any(s.get("decision") for s in _steps(op)):
            db.commit()
            return op
        raise conflict("not_waiting", "this request is not waiting for a confirmation", status=op.status)
    index = int(q.get("step") or 0)
    step = next((s for s in _steps(op) if s.get("index") == index), None)
    if step is None:
        raise conflict("not_waiting", "the step to confirm is missing", status=op.status)
    step.update(decision="accepted" if accept else "declined")
    op.question = None
    if not accept:
        step.update(status="declined", finished_at=_now().isoformat())
        _put_step(op, step)
        op.status = "succeeded"
        asst._on_terminal(db, op)
        conv = asst.lock_conversation(db, op.conversation_id)
        asst._append(db, conv, role="assistant", kind="notice", operation_id=op.id,
                     payload={"event": "confirmation_declined", "operation_id": str(op.id), "step": index, "tool": step.get("tool")})
        db.commit()
        return op
    if _turn_in(db, op.conversation_id, ("pending", "running"), other_than=op.id) is not None:
        raise conflict("agent_busy", "the main agent is working on this conversation; confirm when it has finished")
    step.update(status="running", started_at=_now().isoformat())
    _put_step(op, step)
    op.status = "running"  # one active turn: the worker claims nothing else of this conversation meanwhile
    db.commit()
    with team_prompt.team_scope((op.request or {}).get("team_snapshot")):
        res = tools.run_tool(str(step["tool"]), q.get("args") or {}, _ctx(op, index))
    op = asst._lock_op(db, op.id)
    _put_step(op, _finished(step, res))
    if op.status == "running":
        op.status = "succeeded"
        asst._on_terminal(db, op)
        root = db.get(AiOperation, root_of(op))
        conv = asst.lock_conversation(db, op.conversation_id)
        crid = uuid.uuid5(op.id, f"confirm:{index}")
        if root is not None and conv.archived_at is None and _turn_of(db, conv.id, crid) is None:
            _queue_turn(db, conv, root, {"type": "confirmed", "operation_id": str(op.id), "step": index}, crid)
    db.commit()
    return op


def undo(db: Session, user: User, operation_id: uuid.UUID, index: int) -> AiOperation:
    """POST /ai/operations/{id}/steps/{index}/undo (design §2.4): the step's reverse action with that action's own
    permission (draft discarded, part archived, task cancelled with its runs, sub-agent stopped while active); the step
    becomes ``undone``. Finished versions are never deleted. 409 not_undoable otherwise; an undone step returns as is.
    Commits."""
    op, conv, _ = asst.load_operation(db, user, operation_id)
    asst.require_open(conv)
    step = next((s for s in _steps(op) if s.get("index") == index), None) if is_turn(op) else None
    if step is not None and step.get("status") == "undone":
        return op
    u = tools.UNDO.get(str((step or {}).get("tool")))
    oid = _uuid(((step or {}).get("refs") or {}).get(u.ref)) if u is not None else None
    target = db.get(u.model, oid) if u is not None and oid is not None else None
    if step is None or u is None or step.get("status") != "ok" or target is None or not u.can(db, target):
        raise conflict("not_undoable", "this step cannot be undone (now)", step=index)
    u.run(db, user, target)
    op = asst._lock_op(db, op.id)
    _put_step(op, {**step, "status": "undone", "undone_at": _now().isoformat()})
    asst._touch_conversations(db, {op.conversation_id})
    db.commit()
    return op


# --- output -------------------------------------------------------------------------------------------------------


def undoable(db: Session, ops: list[AiOperation]) -> dict[uuid.UUID, set[int]]:
    """turn id → indexes of its steps that can be undone now (one read per object type)."""
    wanted: dict[type, set[uuid.UUID]] = {}
    pending: list[tuple[uuid.UUID, int, Any, uuid.UUID]] = []
    for op in ops:
        if not is_turn(op):
            continue
        for s in _steps(op):
            u = tools.UNDO.get(str(s.get("tool")))
            oid = _uuid((s.get("refs") or {}).get(u.ref)) if u is not None and s.get("status") == "ok" else None
            if u is not None and oid is not None:
                wanted.setdefault(u.model, set()).add(oid)
                pending.append((op.id, int(s["index"]), u, oid))
    rows = {(m, r.id): r for m, ids in wanted.items() for r in db.scalars(select(m).where(m.id.in_(ids))).all()}
    out: dict[uuid.UUID, set[int]] = {}
    for op_id, index, u, oid in pending:
        row = rows.get((u.model, oid))
        if row is not None and u.can(db, row):
            if isinstance(row, AiOperation):
                from .cad_results import result_out as cad_result_out

                card = cad_result_out(db, row).get("cad_result")
                if card and not (card.get("undo") or {}).get("eligible"):
                    continue
                if not card and row.status == "cancelled":
                    continue
            out.setdefault(op_id, set()).add(index)
    return out


def result_out(op: AiOperation, undo_now: set[int] | None) -> Any:
    """An operation's result as returned; a turn's steps carry ``undo`` (the step can be undone now)."""
    if not is_turn(op) or not isinstance(op.result, dict):
        return op.result
    return {**op.result, "steps": [{**s, "undo": s.get("index") in (undo_now or set())} for s in _steps(op)]}


def _step_refs(ops: list[AiOperation], tool: str, key: str) -> list[uuid.UUID]:
    found = [_uuid((s.get("refs") or {}).get(key)) for o in ops if is_turn(o) for s in _steps(o) if s.get("tool") == tool and s.get("status") == "ok"]
    return list(dict.fromkeys(f for f in found if f is not None))


def work_out(db: Session, conv: AiConversation, ops: list[AiOperation]) -> dict:
    """``detail.work``: what the main agent made in this conversation — its drafts, and the tasks and parts of its steps."""
    from .ai_drafts import draft_out

    drafts = db.scalars(select(AiTaskDraft).where(AiTaskDraft.conversation_id == conv.id).order_by(AiTaskDraft.created_at, AiTaskDraft.id)).all()
    task_ids, part_ids = _step_refs(ops, "create_task", "task_id"), _step_refs(ops, "create_part", "part_id")
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()} if task_ids else {}
    parts = {p.id: p for p in db.scalars(select(Part).where(Part.id.in_(part_ids))).all()} if part_ids else {}
    receipts = [r for op in ops if is_turn(op) for r in ((op.result or {}).get("manufacturing_receipts") or {}).values()]
    plans, quotes = {}, {}
    for receipt in receipts:
        data = receipt.get("data") or {}
        refs = data.get("refs") or {}
        common = {"asset_version_id": refs.get("asset_version_id"), "title": data.get("title") or ""}
        if receipt.get("tool") == "create_process_plan":
            plans[refs["process_plan_id"]] = {**common, "id": refs["process_plan_id"], "revision_id": refs.get("revision_id"),
                                             "priceable": data.get("priceable")}
        if receipt.get("tool") == "create_quote":
            quotes[refs["quote_id"]] = {**common, "id": refs["quote_id"], "process_plan_id": refs["process_plan_id"]}
    return {"drafts": [draft_out(d) for d in drafts],
            "tasks": [{"id": str(t.id), "number": t.number, "title": t.title, "status": t.status} for i in task_ids if (t := tasks.get(i))],
            "parts": [{"id": str(p.id), "number": p.number, "name": p.name} for i in part_ids if (p := parts.get(i))],
            "process_plans": list(plans.values()), "quotes": list(quotes.values()),
            "assumptions": [a for op in ops if is_turn(op) for a in (op.result or {}).get("assumptions") or []]}


def work_state(db: Session, conv: AiConversation, current: uuid.UUID) -> dict:
    """The conversation's work as plain data, for the planner and the ``<work>`` block: every turn with its steps, the
    drafts, the parts and tasks the main agent made, the sub-agents in brief."""
    ops = list(db.scalars(select(AiOperation).where(AiOperation.conversation_id == conv.id).order_by(AiOperation.created_at, AiOperation.id)).all())
    made = work_out(db, conv, ops)

    def turn(o: AiOperation) -> dict:
        req = o.request or {}
        target = req.get("target") or {}
        return {"id": str(o.id), "root_id": str(root_of(o)), "trigger": ((req.get("trigger") or {}).get("type") or "request"),
                "retry_of": str(o.retry_of) if o.retry_of else None,
                "text": req.get("text") or "", "target_part_id": target.get("object_id") if target.get("kind") == "part" else None,
                "target_revision_id": target.get("object_id") if target.get("kind") in ("part_revision", "cad_selection") else None,
                "steps": [{k: s.get(k) for k in ("index", "tool", "status", "error")} | {"refs": s.get("refs") or {}} for s in _steps(o)]}

    return {**made, "current": str(current), "turns": [turn(o) for o in ops if is_turn(o)],
            "sub_agents": [child_brief(o) for o in ops if (o.request or {}).get("parent_op_id")],
            "design_plan": next(((root.result or {}).get('design_plan') for o in ops if o.id == current
                                  for root in ops if root.id == root_of(o)), None)}


# --- running a turn (decision 3) ----------------------------------------------------------------------------------


def _new_step(index: int, name: str, args: dict) -> dict:
    from ..ai.harness.base import safe_args

    return {"index": index, "tool": name, "title": tools.step_title(name, args), "args": safe_args(args), "status": "running",
            "message": "", "refs": {}, "started_at": _now().isoformat(), "finished_at": None}


def _finished(step: dict, res: evidence.EvidenceResult) -> dict:
    data = res.data if isinstance(res.data, dict) else {}
    out = {**step, "status": "ok" if res.ok else "failed", "message": clip(res.message, 500), "refs": dict(data.get("refs") or step.get("refs") or {}),
           "title": str(data.get("title") or step.get("title") or ""), "finished_at": _now().isoformat()}
    if not res.ok:
        out["error"] = {"code": (res.error or {}).get("code") or "failed", "message": clip(str((res.error or {}).get("message") or res.message), 500)}
    if isinstance(data.get("evidence"), dict):
        out["evidence"] = {k: data["evidence"][k] for k in ("kind", "id", "title", "page", "pages", "count")
                           if data["evidence"].get(k) is not None}
    return out


def _save_step(turn_id: uuid.UUID, step: dict) -> None:
    """One step in its own short transaction (the conversation's polling shows it at once)."""
    with session_scope() as db:
        _put_step(asst._lock_op(db, turn_id), step)


def _status_of(turn_id: uuid.UUID) -> tuple[str | None, str]:
    """(turn status, conversation autonomy) now."""
    with session_scope() as db:
        op = db.get(AiOperation, turn_id)
        conv = db.get(AiConversation, op.conversation_id) if op is not None else None
        preferences = ((op.request or {}).get("preferences_snapshot") or {}) if op else {}
        return (op.status if op is not None else None), (preferences.get("ai_autonomy") or
                                                        (conv.autonomy if conv is not None else "step"))


def _assembly_handoff(db: Session, op: AiOperation, *, expected_child: uuid.UUID | None = None) -> tuple[AiOperation, OrchestratorReply] | None:
    """Finish only a verified assembly workflow started by this turn, never an unexecuted tool claim or an old child.

    The workflow owns its dependent modeling/drawing steps and wakes the main agent with its actual result. A final
    model summary adds no work. Recheck this proof when saving the reply; cancellation/failure can win meanwhile.
    """
    steps = _steps(op)
    if op.status != "running" or op.error or op.question or not steps or any(s.get("status") != "ok" for s in steps):
        return None
    step = steps[-1]
    child_id = _uuid((step.get("refs") or {}).get("operation_id"))
    if step.get("tool") != "create_assembly" or child_id is None or (expected_child and child_id != expected_child):
        return None
    child = db.get(AiOperation, child_id)
    conv = db.get(AiConversation, op.conversation_id)
    root = db.get(AiOperation, root_of(op))
    if child is None or conv is None or conv.archived_at is not None or root is None or root.status == "cancelled":
        return None
    req = child.request or {}
    if (child.kind != "assembly_create" or child.run_id is None or child.error
            or child.status not in ("running", "succeeded")
            or child.conversation_id != op.conversation_id or child.project_id != op.project_id or child.user_id != op.user_id
            or req.get("parent_op_id") != str(op.id) or req.get("root_op_id") != str(root_of(op))
            or req.get("spawned_by") != {"operation_id": str(op.id), "step": step["index"]}
            or child.client_request_id != uuid.uuid5(op.id, f"step:{step['index']}")):
        return None
    zh = tools._zh((op.request or {}).get("text") or "")
    if child.status == "running":
        message = ("装配子任务已启动，仍在运行。生成结果和校验状态会显示在任务卡片中。" if zh else
                   "The assembly task has started and is still running. Generated results and check status will appear in its card.")
    else:
        message = ("装配子任务已结束，请查看任务卡片中的实际生成结果和校验状态。" if zh else
                   "The assembly task has finished. See its card for the actual generated results and check status.")
    return child, OrchestratorReply(message=message)


class TurnControl:
    """``AttemptControl`` of one turn (like ``assistant_tools.AskControl``): deadline, model-request and tool-call limits,
    the turn's own status (a stopped turn stops before its next step). Every tool call is a step written before and after
    it runs; a write tool the conversation's autonomy asks about waits for the person instead (``waiting_confirm``; the
    agent is told to end its turn and every later write tool answers ``awaiting_confirmation``). Model calls are recorded
    on the turn at once."""

    def __init__(self, turn: AiOperation, *, limit_chars: int = tools.RESULT_CHARS_MAX, deadline: float | None = None) -> None:
        self.turn = turn
        self.limit_chars = limit_chars
        self.stop_reason: str | None = None
        self.deadline = min(time.monotonic() + MAX_DURATION_S, deadline) if deadline is not None else time.monotonic() + MAX_DURATION_S
        self.next_index = max((int(s["index"]) for s in _steps(turn)), default=-1) + 1
        self.calls = 0
        self.tool_count = 0
        self.question: dict | None = None
        self.handoff_operation_id: uuid.UUID | None = None
        self.tool_failed = False
        self._lock = threading.Lock()

    def handoff_reply(self) -> dict | None:
        """Optional tool-agent callback before its next model request; no new execution budget is consumed."""
        if self.question is not None or self.tool_failed:
            return None
        with session_scope() as db:
            op = db.get(AiOperation, self.turn.id)
            handoff = _assembly_handoff(db, op) if op is not None else None
            if handoff is None:
                return None
            child, reply = handoff
            self.handoff_operation_id = child.id
            return reply.model_dump(mode="json")

    def alive(self) -> bool:
        status, _ = _status_of(self.turn.id)
        if status != "running":
            self.stop_reason = "cancelled"
        elif time.monotonic() >= self.deadline:
            self.stop_reason = "timeout"
        elif self.calls > MAX_STEPS:
            self.stop_reason = "max_steps"
        elif self.tool_count > MAX_TOOL_CALLS:
            self.stop_reason = "max_tool_calls"
        else:
            self.stop_reason = None
        return self.stop_reason is None

    def call_tool(self, name: str, args: dict[str, Any]) -> evidence.EvidenceResult:
        # Tool callbacks may run on framework worker threads; inherit the saved turn explicitly.
        with team_prompt.team_scope((self.turn.request or {}).get("team_snapshot")):
            return self._call_tool(name, args)

    def _call_tool(self, name: str, args: dict[str, Any]) -> evidence.EvidenceResult:
        if not self.alive():
            raise AttemptStopped(self.stop_reason)
        if name in tools.TOOL_FEATURES and not features.enabled(tools.TOOL_FEATURES[name]):
            # Run the normal permission-first refusal before creating any confirmation request.
            self.tool_failed = True
            return tools.run_tool(name, args, _ctx(self.turn, self.next_index, self.limit_chars))
        spec = ORCHESTRATOR_TOOL_SPECS.get(name)
        writes = spec is not None and not spec.read_only
        if writes and (self.turn.request or {}).get("read_only"):
            self.tool_failed = True
            return tools.run_tool(name, args, _ctx(self.turn, self.next_index, self.limit_chars))
        with self._lock:
            self.tool_count += 1
            if writes and self.question is not None:
                if self.question.get('kind') == 'design_choice':
                    return evidence.fail(name, 'awaiting_design_choice', 'the saved design awaits the person\'s choice; end this turn')
                return evidence.fail(name, "awaiting_confirmation", "a step is waiting for the person's confirmation; end the turn with a "
                                                                   "short reply")
            index, self.next_index = self.next_index, self.next_index + 1
        step = _new_step(index, name, args or {})
        if writes and tools.needs_confirm(name, _status_of(self.turn.id)[1]):
            question = tools.confirm_question(name, dict(args or {}), _ctx(self.turn, index, self.limit_chars))
            with self._lock:
                self.question = question
            _save_step(self.turn.id, {**step, "status": "waiting_confirm", "title": question["title"] or step["title"]})
            return evidence.EvidenceResult(ok=False, tool=name, message="this step waits for the person's confirmation in the conversation; "
                                           "end the turn with one short sentence", error={"code": "confirmation_required", "message": question["summary"]})
        _save_step(self.turn.id, step)
        if name in ('plan_design', 'extract_spec'):
            # Synchronous planning and extraction share this already-running turn's deadline.
            with call_retry.deadline_scope(self.deadline):
                res = tools.run_tool(name, args, _ctx(self.turn, index, self.limit_chars))
        else:
            res = tools.run_tool(name, args, _ctx(self.turn, index, self.limit_chars))
        if not res.ok:
            self.tool_failed = True
        finished = _finished(step, res)
        design_question = res.data.get('design_question') if isinstance(res.data, dict) else None
        if res.ok and isinstance(design_question, dict) and design_question.get('kind') == 'design_choice':
            with self._lock:
                self.question = design_question
            finished['status'] = 'waiting_design'
        _save_step(self.turn.id, finished)
        return res

    def record_model_call(self, call: Any) -> str | None:
        from . import ai as ai_svc

        with session_scope() as db:
            rows = ai_svc.record_calls(db, [call], None, ai_operation_id=self.turn.id)
            db.flush()
            call_id = str(rows[0].id)
        with self._lock:
            self.calls += 1
        return call_id

    def emit(self, event: HarnessEvent) -> None:  # the steps are the turn's timeline
        return None


def run_next_turn() -> bool:
    """Worker: claim the oldest pending turn of a conversation with no running turn (``FOR UPDATE SKIP LOCKED`` on the turn
    and its conversation) and run it to its end; False when there is none. Never raises."""
    try:
        with session_scope() as db:
            turn_id = _claim(db)
    except Exception:  # noqa: BLE001
        log.exception("claiming a main-agent turn failed")
        return False
    if turn_id is None:
        return False
    try:
        _run(turn_id)
    except ProviderError as exc:
        _fail(turn_id, {"code": "ai_unavailable", "message": exc.message,
                        "details": {"reason": exc.code, "retryable": exc.retryable}})
    except AppError as exc:
        _fail(turn_id, {"code": exc.code, "message": exc.message, "details": exc.details or {}})
    except Exception:  # noqa: BLE001
        log.exception("main-agent turn %s failed", turn_id)
        _fail(turn_id, {"code": "internal_error", "message": "the main agent's turn failed unexpectedly", "details": {}})
    return True


def _fail(turn_id: uuid.UUID, error: dict) -> None:
    with session_scope() as db:
        asst._fail(db, turn_id, error)


def _claim(db: Session) -> uuid.UUID | None:
    """The next pending turn to run. A turn queued in an archived conversation, or while the main agent is turned off (the
    rollback), never runs: it is cancelled / failed here instead."""
    running = select(AiOperation.conversation_id).where(AiOperation.status == "running", *_turn_filter())
    for op in db.scalars(select(AiOperation).where(AiOperation.status == "pending", *_turn_filter(), AiOperation.conversation_id.not_in(running))
                         .order_by(AiOperation.created_at, AiOperation.id).limit(5).with_for_update(skip_locked=True)).all():
        conv = db.scalar(select(AiConversation).where(AiConversation.id == op.conversation_id).with_for_update(skip_locked=True))
        if conv is None or db.scalar(running.where(AiOperation.conversation_id == conv.id).limit(1)) is not None:
            continue
        root = db.get(AiOperation, root_of(op), populate_existing=True)
        root_cancelled = root is not None and root.id != op.id and root.status == "cancelled"
        from . import operation_events
        for completion in operation_events.context(db, op):
            parent_id = _uuid(completion.get("parent_operation_id"))
            parent = db.get(AiOperation, parent_id, populate_existing=True) if parent_id else None
            root_cancelled = root_cancelled or parent is None or parent.status == "cancelled"
        if conv.archived_at is not None or root_cancelled or not _enabled():
            op.status = "cancelled" if conv.archived_at is not None or root_cancelled else "failed"
            if op.status == "failed":
                op.error = {"code": "orchestrator_disabled", "message": "the main agent was turned off; send the request again", "details": {}}
            asst._on_terminal(db, op)
            continue
        op.status = "running"
        op.result = {"kind": "orchestrator", "steps": [], "trigger": (op.request or {}).get("trigger") or {"type": "request"}, "simulated": False}
        return op.id
    return None


def _event(db: Session, op: AiOperation) -> str:
    from . import completion_context, operation_events

    snapshots = operation_events.context(db, op)
    if snapshots:
        return completion_context.event_text(snapshots)
    trigger = (op.request or {}).get("trigger") or {}
    if trigger.get("event_id"):
        return ""  # Never replace an immutable event with the child's current state.
    ref = db.get(AiOperation, _uuid(trigger.get("operation_id"))) if trigger.get("operation_id") else None
    if ref is None:
        return ""
    step = next((s for s in _steps(ref) if s.get("index") == trigger.get("step")), None)
    return orchestrator_agent.event_text(trigger, child_brief(ref), step)


def _turn_build(db: Session, op: AiOperation, conv: AiConversation, notes: dict | None, work: dict) -> Any:
    """The ask build (memory, references, target, constraints, request) as a turn's: the main agent's rules and tools in
    its manifest instead of the ask's, and the ``<work>`` (and ``<event>``) blocks before the request."""
    from . import assistant_context as actx

    req = op.request or {}
    specs = tools.available_specs(read_only=bool(req.get("read_only")))
    tool_schema = json.dumps({"tools": [{"name": spec.name, "description": spec.description, "parameters": spec.json_schema()}
                                      for spec in specs], "output": OrchestratorReply.model_json_schema()}, ensure_ascii=False)
    build = actx.build_context(db, conversation=conv, kind="ask", text=req.get("text") or "", target=req.get("target"),
                               references=list(req.get("references") or []), constraints=list(req.get("constraints") or []),
                               model_config_id=asst._model_ref(req), materials=asst.material_keys(req), material_notes=notes,
                               system_prompt=orchestrator_agent.system_prompt(op.execution_policy), output_schema=tool_schema)
    if not build.ok:
        return build
    try:
        event = _event(db, op)
    except AppError as exc:
        build.ok = False
        build.error = {"code": exc.code, "message": exc.message, "details": exc.details or {}}
        return build
    blocks = [("Work in this conversation", orchestrator_agent.work_block(orchestrator_agent.work_text(work)))]
    if event:
        blocks.append(("Event", orchestrator_agent.event_block(event)))
    asst._with_blocks(build, blocks, None, insert=orchestrator_agent.with_turn_blocks)
    used, available = int(build.budget.get("used_est") or 0), int(build.budget.get("available") or 0)
    if build.ok and used > available:
        build.ok = False
        build.error = actx._over_budget("ask", build.items, int(build.budget.get("required_est") or 0), available,
                                       limit="input", model_label=build.model_label, extra={"used_est": used})
    return build


def _heartbeat_turn(turn_id: uuid.UUID, generation: int, deadline: float) -> bool:
    """Refresh only this still-running generation; a locked row is retried on the next pulse."""
    if time.monotonic() >= deadline:
        return False
    with session_scope() as db:
        query = select(AiOperation).where(AiOperation.id == turn_id, AiOperation.generation == generation,
                                          AiOperation.status == "running", *_turn_filter())
        op = db.scalar(query.with_for_update(skip_locked=True))
        if op is None:
            return db.scalar(query.with_only_columns(AiOperation.id)) is not None
        if time.monotonic() >= deadline:
            return False
        op.updated_at = _now()
        return True


def _run(turn_id: uuid.UUID) -> None:
    with session_scope() as db:
        op = db.get(AiOperation, turn_id)
        assert op is not None
        snapshot = (op.request or {}).get("team_snapshot")
        generation = op.generation
    deadline = time.monotonic() + MAX_DURATION_S
    with team_prompt.team_scope(snapshot), background_heartbeat(
            lambda: _heartbeat_turn(turn_id, generation, deadline), name=f"turn-heartbeat-{turn_id}"):
        _run_scoped(turn_id, deadline=deadline)


def _run_scoped(turn_id: uuid.UUID, *, deadline: float | None = None) -> None:
    """One claimed turn: materials, the turn's build (recorded on it), then the agent; the end writes the answer."""
    from ..ai.harness import get_harness
    from . import ai as ai_svc
    from . import assistant_context as actx
    from .assistant_tools import result_chars

    db = new_session()
    try:
        op = db.get(AiOperation, turn_id)
        assert op is not None
        req = op.request or {}
        asst._check_workflow_versions(db, [req.get("target"), *(req.get("references") or [])])
        notes = asst._read_materials(db, op)  # S2: a chunked material is read first (calls recorded on the turn)
        conv = db.get(AiConversation, op.conversation_id)
        assert conv is not None
        work = work_state(db, conv, op.id)
        build = _turn_build(db, op, conv, notes, work)
        actx.record_build(db, op, build)
        from . import operation_events
        op.result = {**(op.result or {}), "completion_context": [e["event_id"] for e in operation_events.context(db, op)]}
        if not build.ok:
            db.commit()
            _fail(turn_id, build.error or {"code": "context_over_budget", "message": "the required context does not fit the model", "details": {}})
            return
        try:
            provider, _ = ai_svc.provider_for(db, "assistant", asst._model_ref(req))
        except ProviderError as exc:
            raise AppError(503, "ai_unavailable", "the AI model of the main agent is not usable; check the model settings",
                           {"reason": exc.code, "detail": exc.message})
        db.commit()  # nothing is held while the agent works
        ctl = TurnControl(op, limit_chars=result_chars(build), deadline=deadline)
    finally:
        db.close()
    trigger = req.get("trigger") or {"type": "request"}
    if provider.simulated:
        reply, payload, error = _simulate(ctl, op, provider, build, trigger)
    else:
        spec = AttemptSpec(role="orchestrator", runtime="tool_agent", provider=provider, tools=tools.available_specs(read_only=bool(req.get("read_only"))),
                           runtime_options={"parallel_tool_calls": True, "max_tool_calls": MAX_TOOL_CALLS},
                           limits={"max_steps": MAX_STEPS, "max_duration_s": max(0.0, ctl.deadline - time.monotonic()),
                                   "max_tool_calls": MAX_TOOL_CALLS},
                           instructions=orchestrator_agent.system_prompt(op.execution_policy), user_message=build.context_text, prompt_version=ORCHESTRATOR_PROMPT_VERSION)
        result = get_harness("tool_agent").run(spec, ctl)  # type: ignore[arg-type]
        reply = OrchestratorReply.model_validate(result.output) if result.status == "delivered" and result.output else None
        payload = {}
        error = None if reply is not None or result.status == "superseded" else (result.error or {"code": "bad_response",
                                                                                                    "message": "the main agent gave no reply"})
    _finish(turn_id, ctl, reply, payload, error, simulated=bool(provider.simulated))


def _simulate(ctl: TurnControl, op: AiOperation, provider: Any, build: Any, trigger: dict) -> tuple[OrchestratorReply | None, dict, dict | None]:
    """The labelled simulated main agent (decision 11): the deterministic planner's moves through the same control, or
    the existing simulated ask answer for a question without action intent."""
    from ..ai import assistant_agent
    from . import ai as ai_svc

    text = (op.request or {}).get("text") or ""
    try:
        while ctl.alive():  # the tool-call limit ends the loop through alive() like the tool agent's
            with session_scope() as db:
                conv = db.get(AiConversation, op.conversation_id)
                work = work_state(db, conv, op.id)  # type: ignore[arg-type]
            plan = orchestrator_agent.plan_next(text, work, trigger)
            if plan.ask or (op.request or {}).get("read_only"):
                outcome = assistant_agent.run_ask(provider, assistant_agent.ASK_SYSTEM_PROMPT, build.context_text, timeout_s=60)
                with session_scope() as db:
                    ai_svc.record_calls(db, list(outcome.calls or []), None, ai_operation_id=op.id)
                if outcome.error:
                    return None, {}, outcome.error
                return (OrchestratorReply(message=outcome.answer, suggested_operation=outcome.suggested_operation, follow_ups=outcome.follow_ups),
                        {"cited_keys": list(outcome.cited_keys or [])}, None)
            if plan.reply is not None:
                return plan.reply, {}, None
            ctl.call_tool(str(plan.tool), plan.args)
    except AttemptStopped:
        pass
    return None, {}, stopped(ctl)[1] or {"code": "agent_stopped", "message": "the main agent stopped before replying"}


def _finish(turn_id: uuid.UUID, ctl: TurnControl, reply: OrchestratorReply | None, payload: dict, error: dict | None, *,
            simulated: bool) -> None:
    """The end of a turn: its answer message and ``succeeded`` (``waiting_input`` with the question when a step waits for
    the person), or ``failed`` with the reason (steps already done stay). A turn stopped meanwhile is left as it is."""
    from .assistant_queries import consulted_of

    with session_scope() as db:
        op = asst._lock_op(db, turn_id)
        if op.status != "running":
            return
        op.simulated = simulated
        op.result = {**(op.result or {}), "simulated": simulated}
        if ctl.handoff_operation_id is not None:
            handoff = _assembly_handoff(db, op, expected_child=ctl.handoff_operation_id)
            reply = handoff[1] if handoff is not None else None
            if reply is None:
                error = {"code": "handoff_changed", "message": "the assembly handoff is no longer running or successful; check its task card"}
        if reply is None and ctl.question is not None:  # the stored question survives even if the agent gives no reply
            if ctl.question.get('kind') == 'design_choice':
                reply = OrchestratorReply(message='方案已保存，可以调整参数，或让我决定其余部分。'
                    if tools._zh((op.request or {}).get('text') or '') else
                    'The design is saved; customize it or delegate the remaining choices.')
            else:
                reply = orchestrator_agent.waiting_reply((op.request or {}).get("text") or "")
        if reply is None:
            op.status, op.error = "failed", error or {"code": "agent_stopped", "message": "the main agent stopped before replying", "details": {}}
            _close_waiting(op, "superseded")
            asst._on_terminal(db, op)
            return
        reply = reply.cleaned()
        conv = asst.lock_conversation(db, op.conversation_id)
        consulted = consulted_of([{"name": step["tool"], "ok": step.get("status") == "ok", "evidence": step.get("evidence"),
                                   "error": step.get("error")} for step in _steps(op) if step.get("tool") in tools.LOOKUPS])
        asst._append(db, conv, role="assistant", kind="answer", text=reply.message, operation_id=op.id,
                     payload={"runtime": RUNTIME, "cited_keys": payload.get("cited_keys") or [], "suggested_operation": reply.suggested_operation,
                              "follow_ups": reply.follow_ups, "consulted": consulted, "simulated": simulated})
        if ctl.question is not None:
            op.status, op.question = "waiting_input", ctl.question
            conv.last_activity_at = _now()
            from . import operation_events
            operation_events.complete_context(db, op)
            return
        op.status = "succeeded"
        from . import operation_events
        operation_events.complete_context(db, op)
        asst._on_terminal(db, op)
