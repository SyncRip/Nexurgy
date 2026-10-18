"""AI orchestration of the general mechanical engineering chain (W8; docs/development/mechanical-engineering/
architecture.zh-CN.md §1, §4, §8; w0-contract.zh-CN.md §2, §5).

One service layer that every entry calls — the workflow nodes (``engineering_spec``, ``assembly_from_spec``,
``assembly_drawing``, ``delivery_package``), the AI workspace ``assembly_create`` operation (which starts a run of
those nodes) and the collaboration agent tools of the same names — so permissions, policy, sources, states and
products are the same whatever the entry (AC-E01):

- :func:`resolve_spec`: text / project files → a W2 engineering spec version extracted with the frozen policy and
  planning model, or an exact existing version passed through (never a version of another project; a Turbo spec feeds a
  standard run only after a person confirmed its assumptions, S5). Extraction failures end as a failed version with its
  record; nothing is invented.
- :func:`build_assembly`: every part definition under the spec's root is built on its own — a W3 template when the
  definition names one, else the shared modeling core (``ai_modeling.run_modeling``) creating a new part from a
  request composed of that definition's facts, units, notes and evidence — then W4 ``create_from_spec_version`` and
  the W4 checks (Turbo records them skipped). Standard: a definition blocked by open spec questions or by a modeling
  clarification is not built and its questions are listed; Turbo adopts values and a failed definition does not stop
  the independent ones. Generation supplement B: whatever was built makes a partial assembly (no success ratio): the
  missing definitions, the sub-assemblies left empty, the relations touching them and the instances that lost their only
  pose are listed in the coverage block (target vs actual, ``spec_ref.coverage``) and the node fails with its products
  (``assembly_partial``). Only when nothing can be placed (or the step was cancelled) no assembly is created; the built
  parts are kept and listed. Continuing rebuilds only what is missing and makes the next revision of the same assembly.
  Turbo (supplement F): the kept instances without a position get one recorded AI placement decision before the
  assembly is made; only what it cannot place is arranged for display (never installed).
- :func:`make_drawings`: the W5 package of the exact assembly revision through a guarded import of
  ``services.assembly_drawings.generate``; without it the step fails with ``capability_unavailable`` (no fake output).
- :func:`deliver`: a delivery manifest + zip of the exact sources: assembly STEP, every unique part's stored STEP, the
  frozen BOM (hierarchical + flattened; JSON / CSV) and the drawing package files, each with sha256 and source
  version, plus units, representation levels, assumptions and verification. Anything required and absent is listed
  under ``missing`` and the manifest is ``partial``; only a manifest with every required file is ``complete``.
- Generation supplement E (layered delivery, BHD1 §8.7): :func:`make_level_drawings` draws the root package (GA + the
  detail sheet of every part definition, all procurement kinds) and one package per distinct sub-assembly revision (GA +
  BOM, its own title block); :func:`deliver` with ``subassembly_packages`` then adds each sub-assembly's STEP and
  package, requires every level's STEP and DWG, and writes the ``levels`` summary. Generation reliability R: each
  package is a work unit — checked against cancel / lease / the run's time budget before it starts, committed when it
  is made, reused on a retry, reported in the step's ``units``; a package never started is a missing DWG.
"""

from __future__ import annotations

import contextvars
import csv
import hashlib
import io
import json
import logging
import re
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session
from pydantic import ValidationError

from .. import policy as pol
from ..cad.requirements import DefinitionSourceContext
from ..errors import AppError, not_found
from ..models import (
    AiCall,
    Assembly,
    AssemblyRevision,
    DeliveryManifest,
    EngineeringSpec,
    EngineeringSpecVersion,
    ModelRevision,
    Part,
    User,
)
from ..security import ProjectAccess
from ..storage import read_blob, save_blob
from .audit import audit
from .assistant_tool_inputs import ResolvedDesignPlan
from .definition_source_references import DefinitionSourceReferenceCache, DefinitionSourceReferences, has_text_sources
from ..cad.requirements import DesignDefinitionContext, DESIGN_MODELING_SYSTEM

log = logging.getLogger("partroute.orchestration")

DELIVERY_SCHEMA = "partroute.delivery_manifest/1"
#: A definition is not started when less than this is left of the node's runtime (one modeling call ≤ 180 s + repairs).
MODELING_RESERVE_S = 600
#: J: a definition is not started when less than this is left of the run's time budget (one modeling request's own time
#: limit, ``modeling_agent`` ``timeout_s``): starting it would only time out and spend the budget.
RUN_BUDGET_RESERVE_S = 240
#: Leave time to persist the assembly after its final mate solve and avoid
#: starting optional geometry checks when only the write reserve remains.
ASSEMBLY_WRITE_RESERVE_S = 15
_ZIP_TIME = (2000, 1, 1, 0, 0, 0)
#: Spec deliverables (W0 §5) → the delivery items that carry them.
DELIVERABLE_ITEMS = {"assembly_step": "assembly_step", "part_step": "part_step", "bom": "bom", "assembly_dwg": "drawings",
                     "detail_dwg": "drawings", "process": "process", "quote": "quote"}


def run_cancel_check(run_id: uuid.UUID | str | None) -> Callable[[], bool] | None:
    """``cancelled`` for :func:`build_assembly`: reads the run's cancel flag in a short session of its own (the caller's
    session holds the run as it was when the step started; the engine lets a running step finish). Canvas, workspace
    and collaboration runs are all workflow runs."""
    if not run_id:
        return None
    rid = uuid.UUID(str(run_id))

    def check() -> bool:
        from ..db import session_scope
        from ..models import WorkflowRun

        with session_scope() as db:
            return bool(db.scalar(select(WorkflowRun.cancel_requested).where(WorkflowRun.id == rid)))

    return check


def _canon(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =====================================================================================================
# Engineering spec
# =====================================================================================================


@dataclass
class SpecResult:
    version: EngineeringSpecVersion
    spec: EngineeringSpec
    created: bool  # extracted by this step (False: an existing version was pinned)

    @property
    def status(self) -> str:
        from .engineering_spec import effective_status

        return effective_status(self.version)[0]

    @property
    def usable(self) -> bool:
        return self.status in ("ready", "needs_answers")


def load_spec_version(db: Session, project_id: uuid.UUID, spec_version_id: uuid.UUID | str, policy: str) -> tuple[EngineeringSpecVersion,
                                                                                                                   EngineeringSpec]:
    """An exact usable spec version of this project for a run under ``policy`` (404 elsewhere; 409 spec_not_ready).

    S5: a Turbo spec holds AI-adopted values; a standard run takes it over only once a person confirmed every one of
    them (409 ``upstream_assumptions_unconfirmed`` lists them all with the entry that confirms them in one go; after
    that the same step runs again). The old dead end ``spec_policy_mismatch`` (re-extract) is gone."""
    from .engineering_spec import effective_status
    from .verification import require_confirmed_spec

    try:
        vid = uuid.UUID(str(spec_version_id))
    except ValueError:
        raise not_found("engineering spec version")
    v = db.get(EngineeringSpecVersion, vid)
    spec = db.get(EngineeringSpec, v.spec_id) if v is not None else None
    if v is None or spec is None or v.project_id != project_id:
        raise not_found("engineering spec version")
    status, err = effective_status(v)
    if status not in ("ready", "needs_answers"):
        raise AppError(409, "spec_not_ready", f"engineering spec version {v.version_no} is {status}",
                       {"status": status, "version_id": str(v.id), "error": err})
    if (v.planning or {}).get("design"):
        from .verification import spec_decisions, CORRECTED

        corrected = [d for d in spec_decisions(db, v).values() if d.get("decision") == "corrected"]
        if corrected:
            raise AppError(409, CORRECTED, "A person corrected this AI design; create a new spec version before building it",
                           {"spec_version_id": str(v.id), "corrected": corrected, "recovery": "new_spec_version"})
    if not pol.is_turbo(policy):
        require_confirmed_spec(db, v)
    return v, spec


def known_values(db: Session, v: EngineeringSpecVersion, policy: str) -> dict[str, str]:
    """How each value the spec adopted reaches a downstream step (S5: no re-asking inside one chain), by assumption id:
    ``turbo`` — adopted upstream in this Turbo chain, a known fact to use as given; ``confirmed`` — a person confirmed it
    (a standard run after the takeover). A value without either never reaches a standard step (see load_spec_version).
    A value a person corrected is never passed on as given (it is an engineering change made as a new spec version,
    like 409 ``upstream_assumptions_corrected``), not even inside a Turbo chain."""
    from .verification import spec_decisions

    ids = [str(a.get("id")) for a in (v.content or {}).get("assumptions") or [] if isinstance(a, dict) and a.get("id")]
    if not ids:
        return {}
    decided = spec_decisions(db, v)
    out: dict[str, str] = {}
    design = (v.planning or {}).get("design") or {}
    designed_ids = set(design.get("assumption_ids") or []) if design.get("authorized") is True else set()
    designed_ids &= {str(a.get("id")) for a in (v.content or {}).get("assumptions") or [] if a.get("author") == "ai_design"}
    for aid in ids:
        d = decided.get(aid)
        if d is not None and d.get("decision") == "confirmed":
            out[aid] = "confirmed"
        elif d is None and aid in designed_ids:
            out[aid] = "design"
        elif d is None and pol.is_turbo(policy):
            out[aid] = "turbo"
    return out


def revisable_version(db: Session, project_id: uuid.UUID, spec_version_id: uuid.UUID | str) -> tuple[EngineeringSpecVersion, EngineeringSpec]:
    """The spec version a revision starts from (generation supplement A-04): any finished version of this project (404
    elsewhere); 409 ``spec_extracting`` while it is still being read. Its policy and confirmations do not matter: the
    revision is extracted again under the run's policy."""
    from .engineering_spec import effective_status

    try:
        vid = uuid.UUID(str(spec_version_id))
    except ValueError:
        raise not_found("engineering spec version")
    v = db.get(EngineeringSpecVersion, vid)
    spec = db.get(EngineeringSpec, v.spec_id) if v is not None else None
    if v is None or spec is None or v.project_id != project_id:
        raise not_found("engineering spec version")
    if effective_status(v)[0] == "extracting":
        raise AppError(409, "spec_extracting", f"engineering spec version {v.version_no} is still being extracted", {"version_id": str(v.id)})
    return v, spec


def resolve_spec(db: Session, *, access: ProjectAccess, policy: str, text: str = "", file_version_ids: list[Any] | tuple = (),
                 spec_version_id: uuid.UUID | str | None = None, title: str = "", model_ref: Any = None, revise: bool = False,
                 drop_file_version_ids: list[Any] | tuple = (), revise_reason: str = "revise", extract: bool = True,
                 authorized_design: ResolvedDesignPlan | None = None) -> SpecResult:
    """A usable spec version for the chain. With ``spec_version_id`` that exact version (see :func:`load_spec_version`);
    otherwise a new spec from the text / project file versions, extracted now (synchronously) with ``policy`` and the
    frozen planning model ``model_ref`` (a W1 snapshot, or a config id). A failed extraction returns the failed version
    (its ``error`` says stage, code, calls and recovery); the caller decides what that means for its step.

    ``revise`` with ``spec_version_id`` (generation supplement A-04 / IN-07, the person's explicit choice; IN-08 after a
    source could not be read): a new version of that spec — its sources plus ``file_version_ids`` (minus
    ``drop_file_version_ids``), the text as a revision note (``engineering_spec.revise_version``) — extracted now the
    same way. The version revised never changes."""
    from . import ai as ai_svc
    from . import attachments
    from . import engineering_spec as spec_svc

    if authorized_design is not None:
        from .design_spec import compile_design_spec
        if spec_version_id or revise or file_version_ids or drop_file_version_ids:
            raise AppError(422, "design_source_mismatch", "an authorized new design cannot be mixed with source revision inputs")
        if text and text != authorized_design.source_snapshot.get("text"):
            raise AppError(422, "design_source_mismatch", "design compilation must retain the original human source")
        spec, version = compile_design_spec(db, access, authorized_design, policy=policy,
            frozen_planning={"model_snapshot": model_ref} if ai_svc.is_snapshot(model_ref) else {})
        return SpecResult(version=version, spec=spec, created=True)
    if spec_version_id is not None and not revise:
        v, spec = load_spec_version(db, access.project.id, spec_version_id, policy)
        return SpecResult(version=v, spec=spec, created=False)
    snapshot = model_ref if ai_svc.is_snapshot(model_ref) else None
    config_id = None if snapshot is not None else (uuid.UUID(str(model_ref)) if model_ref else None)
    if spec_version_id is not None:
        parent, spec = revisable_version(db, access.project.id, spec_version_id)
        dropped = {str(x) for x in drop_file_version_ids}
        every_file = [str(s["file_version_id"]) for s in parent.sources or [] if s.get("file_version_id")
                      and str(s["file_version_id"]) not in dropped] + [str(f) for f in file_version_ids]
        text, dedupe = attachments.dedupe_text(db, text or "", list(dict.fromkeys(every_file)))
        v = spec_svc.revise_version(db, access, parent, text=text or "", file_version_ids=list(file_version_ids),
                                    drop_file_version_ids=list(drop_file_version_ids), policy=policy, model_config_id=config_id,
                                    reason=revise_reason, input_dedupe=dedupe,
                                    frozen_planning={"model_snapshot": snapshot} if snapshot is not None else None)
        if snapshot is not None:  # the chain's own frozen snapshot, as for a new spec below
            v.planning = {**{k: val for k, val in (v.planning or {}).items() if k != "model_error"}, "model_snapshot": snapshot}
            db.flush()
        if extract:
            spec_svc.run_extraction(db, v.id)
        return SpecResult(version=v, spec=spec, created=True)
    # Generation supplement A item 5: what the pasted text repeats of an attached text file is read once, from the file.
    text, dedupe = attachments.dedupe_text(db, text or "", [str(f) for f in file_version_ids])
    body = spec_svc.SpecCreateIn(title=title[:200], text=text or "", file_version_ids=[uuid.UUID(str(f)) for f in file_version_ids],
                                 execution_policy=policy, model_config_id=config_id)
    spec, v = spec_svc.create_spec(db, access, body, frozen_planning={"model_snapshot": snapshot} if snapshot is not None else None)
    if snapshot is not None or dedupe is not None:
        # W2 freezes a snapshot into the version's planning record when it is created; the chain's own frozen
        # snapshot (taken when the run / operation started) replaces it so the spec uses exactly the run's model.
        planning = {k: val for k, val in (v.planning or {}).items() if k != "model_error" or snapshot is None}
        if snapshot is not None:
            planning["model_snapshot"] = snapshot
        if dedupe is not None:
            planning["input_dedupe"] = dedupe
        v.planning = planning
        db.flush()
    if extract:
        spec_svc.run_extraction(db, v.id)
    return SpecResult(version=v, spec=spec, created=True)


def spec_call_ids(v: EngineeringSpecVersion) -> list[uuid.UUID]:
    out = []
    for raw in (v.planning or {}).get("ai_call_ids") or (v.error or {}).get("call_ids") or []:
        try:
            out.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return out


def attach_spec_calls(db: Session, v: EngineeringSpecVersion, *, node_run_id: uuid.UUID | None, agent_profile_id: uuid.UUID | None = None,
                      execution_id: uuid.UUID | None = None, ids: list[Any] | None = None, ai_operation_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Bind the extraction's ai_calls rows (written by W2 without a run) to the node run / agent execution that asked for
    them, so the run ledger and the attempt budget count them. Returns usage records ({tokens_in, tokens_out, …}).
    ``ids``: these rows instead of the version's (the pre-check's alias calls, generation supplement H)."""
    ids = spec_call_ids(v) if ids is None else [uuid.UUID(str(x)) for x in ids]
    if not ids:
        return []
    values: dict[str, Any] = {}
    if node_run_id is not None:
        values["node_run_id"] = node_run_id
    if agent_profile_id is not None:
        values["agent_profile_id"] = agent_profile_id
    if execution_id is not None:
        values["agent_execution_id"] = execution_id
    if ai_operation_id is not None:
        values["ai_operation_id"] = ai_operation_id
    if values:
        db.execute(update(AiCall).where(AiCall.id.in_(ids), AiCall.node_run_id.is_(None)).values(**values))
    rows = db.scalars(select(AiCall).where(AiCall.id.in_(ids))).all()
    return [{"tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "purpose": r.purpose, "simulated": r.simulated,
             "status": r.status} for r in rows]


def spec_brief(db: Session, v: EngineeringSpecVersion, spec: EngineeringSpec | None = None) -> dict[str, Any]:
    """The ``engineering_spec`` port value / tool data: identity, state, readiness, open questions and assumptions."""
    from .engineering_spec import effective_status, readiness, version_brief

    spec = spec or db.get(EngineeringSpec, v.spec_id)
    status, err = effective_status(v)
    content = v.content or {}
    usable = status in ("ready", "needs_answers")
    rd = readiness(content, v.execution_policy) if usable else None
    out: dict[str, Any] = {
        "type": "engineering_spec", "spec_id": str(v.spec_id), "spec_version_id": str(v.id),
        "code": f"SP-{spec.number}" if spec else None, "title": spec.title if spec else "", "version_no": v.version_no,
        "status": status, "execution_policy": v.execution_policy, "simulated": bool(v.simulated),
        "counts": version_brief(v)["counts"], "path": f"/specs/{v.spec_id}?version={v.id}",
        "open_questions": [{k: q.get(k) for k in ("id", "subject", "question", "affects")}
                           for q in content.get("questions") or [] if not q.get("answer")],
        "assumptions": [{k: a.get(k) for k in ("id", "subject", "field", "adopted", "unit", "author", "basis", "trigger", "affected")}
                        for a in content.get("assumptions") or [] if isinstance(a, dict)],
    }
    if rd is not None:
        out["readiness"] = {"blocked_definitions": rd["blocked_definitions"], "buildable_definitions": rd["buildable_definitions"],
                            "gate": rd["gate"]}
    if err:
        out["error"] = {k: err.get(k) for k in ("stage", "code", "message", "recovery", "call_ids", "retryable")}
    # Generation supplement A-02: what the spec step really read, per source (kind, exact file version, name, read state
    # read / partial / unread / failed, ranges, error), and how the inputs were prepared (a pasted text read once).
    from .attachments import source_entry

    out["sources"] = [source_entry(s) for s in v.sources or [] if isinstance(s, dict)]
    if (v.planning or {}).get("input_dedupe"):
        out["input_dedupe"] = v.planning["input_dedupe"]
    if v.origin == "rule":  # derived by a server rule (Turbo structure repair): from which version and what it did
        out["derived"] = {"from_version_id": (v.planning or {}).get("derived_from"), **((v.validation or {}).get("derived") or {})}
    if isinstance((v.planning or {}).get("revision"), dict):  # A-04: a new version of the same spec (revised / sources replaced)
        out["revision"] = v.planning["revision"]
    return out


# =====================================================================================================
# Parts and assembly
# =====================================================================================================


@dataclass
class DefinitionOutcome:
    key: str
    name: str
    kind: str
    quantity: int = 0
    status: str = "pending"  # built | blocked | needs_clarification | failed | not_started
    source: str | None = None  # template | ai_modeling
    part_id: str | None = None
    part_number: str | None = None
    revision_id: str | None = None
    rev_no: int | None = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    simulated: bool = False
    check: dict[str, Any] | None = None
    # S3: built by an earlier attempt of this step (or the run it continues) and still valid; no new modeling call.
    reused: bool = False
    # Generation supplement H (B-04): reused from a build of another version of the same spec (or another run of this
    # one) whose definition request is the same: {spec_version_id, revision_id}.
    reused_from: dict[str, Any] | None = None
    # Generation reliability C (design §6.3): an earlier revision of this definition NOT reused (:func:`reuse_blocked`):
    # {revision_id, spec_version_id?, code, capabilities, not_created, reason}; the definition was modeled again
    reuse_skipped: dict[str, Any] | None = None

    def out(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})} | {"status": self.status, "key": self.key}


#: Recovery codes of the assembly modeling step (design §3.1): rebuild only what is missing (``retry_definition``),
#: answer the open spec questions first, configure a usable model (a new run), or change the spec.
BUILD_RECOVERY_TEXT = {
    "retry_definition": "Continue: completed definitions are kept; failed or not started definitions and remaining assembly placement are retried.",
    "answer_questions": "Answer the questions on the spec page (a new spec version), then run again with that version.",
    "configure_model": "Configure or enable a usable modeling model in Settings → Models, then start the request again.",
    "fix_spec": "The parts that could be built are kept; fix what the assembly step reports in the spec and run again.",
    pol.PROTOCOL_RECOVERY: "A modeling step asked a question inside a Turbo run, which Turbo never puts to a person: state the "
                           "missing value in the spec (a new spec version) or run this step again in standard.",
}


def build_scope(node_run_id: uuid.UUID | str | None) -> str | None:
    """The progress scope of one assembly modeling step (S3): its node run, which a retry runs again in place."""
    return f"node_run:{node_run_id}" if node_run_id else None


def _failure_info(code: str, message: str, *, stage: str, retryable: bool, recovery: str, **extra: Any) -> dict[str, Any]:
    return {"stage": stage, "code": code, "message": message, "retryable": retryable, "recovery": recovery, **extra}


def _build_steps(db: Session, spec_version_id: uuid.UUID, scopes: list[str]) -> dict[str, list[Any]]:
    """definition key → progress rows of these scopes, in scope order (the step's own scope first)."""
    from ..models import AssemblyBuildStep

    if not scopes:
        return {}
    rows = db.scalars(select(AssemblyBuildStep).where(AssemblyBuildStep.spec_version_id == spec_version_id,
                                                      AssemblyBuildStep.scope.in_(scopes))).all()
    out: dict[str, list[Any]] = {}
    for r in sorted(rows, key=lambda r: scopes.index(r.scope)):
        out.setdefault(r.definition_key, []).append(r)
    return out


def _still_built(db: Session, row: Any, project_id: uuid.UUID, policy: str) -> tuple[Part, ModelRevision] | None:
    """The part and exact revision an earlier attempt built for a definition, when they still exist unchanged in this
    project under the same policy (a revision is immutable; an archived part or a failed revision is not reused)."""
    if row.status != "built" or row.revision_id is None or row.part_id is None or row.execution_policy != policy:
        return None
    rev = db.get(ModelRevision, row.revision_id)
    part = db.get(Part, row.part_id)
    if rev is None or part is None or rev.part_id != part.id or rev.status != "ready" or part.project_id != project_id \
            or part.archived_at is not None:
        return None
    return part, rev


def reuse_blocked(db: Session, rev: ModelRevision, *, spec_version_id: Any = None) -> dict[str, Any] | None:
    """Why an earlier, still valid part revision is modeled again instead of reused (generation reliability C, design
    §6.3; brief WP05: a deterministic omission is worth another attempt only after the input, a capability or a decision
    changed), else None. The decision is :func:`app.cad.capabilities.reuse_blocker` on the revision's build record: an
    omission needs a capability the kernel builds now but did not when the revision was built (``capability_added``),
    or the record has no capability information and lists omissions (``capabilities_unrecorded``). The input is the
    definition fingerprint, checked before; nothing here reads the wording of a requirement."""
    from ..cad.capabilities import reuse_blocker
    from .revisions import build_record_of

    why = reuse_blocker(build_record_of(db, rev))
    if why is None:
        return None
    caps = ", ".join(why["capabilities"])
    reason = (f"revision {rev.rev_no} was built without {caps} and left out what needs it; the kernel builds {caps} now"
              if why["code"] == "capability_added" else
              f"revision {rev.rev_no} lists {len(why['not_created'])} item(s) not created and its build record does not say which "
              f"capability they need")
    return {"revision_id": str(rev.id), **({"spec_version_id": str(spec_version_id)} if spec_version_id else {}), **why,
            "reason": reason}


def _record_step(db: Session, *, rows: list[Any], scope: str | None, project_id: uuid.UUID, spec_version_id: uuid.UUID, policy: str,
                 o: DefinitionOutcome, node_run_id: uuid.UUID | None, fingerprint: str | None = None) -> None:
    """Upsert the definition's progress row of this scope (what it built, or why not; the definition's
    :func:`definition_fingerprint` so a build of another version can reuse it).

    A scope may be shared by concurrent builds (the agent tools' scope is one node task's rework round: a lease-lost
    execution and its replacement): a row another build inserted meanwhile is taken and updated (savepoint; on the
    unique key ``uq_assembly_build_steps_key`` the row is selected again), never an IntegrityError for the step."""
    from sqlalchemy.exc import IntegrityError

    from ..models import AssemblyBuildStep

    if scope is None:
        return

    def fill(row: Any) -> None:
        row.execution_policy, row.status, row.source = policy, o.status, o.source
        row.part_id = uuid.UUID(o.part_id) if o.part_id else None
        row.revision_id = uuid.UUID(o.revision_id) if o.revision_id else None
        row.error = dict(o.error) if o.error else None
        row.outcome = {"assumptions": list(o.assumptions or []), "check": o.check, "notes": list(o.notes or []),
                       **({"fingerprint": fingerprint} if fingerprint else {}), **({"reused_from": o.reused_from} if o.reused_from else {}),
                       **({"reuse_skipped": o.reuse_skipped} if o.reuse_skipped else {})}
        row.simulated = bool(o.simulated)
        row.attempts = int(row.attempts or 0) + (0 if o.reused else 1)
        row.node_run_id = node_run_id

    row = next((r for r in rows if r.scope == scope), None)
    if row is not None:
        fill(row)
        db.flush()
        return
    row = AssemblyBuildStep(project_id=project_id, spec_version_id=spec_version_id, scope=scope, definition_key=o.key, attempts=0)
    fill(row)
    sp = db.begin_nested()  # flushes everything else first: only this insert is undone on a conflict
    try:
        db.add(row)
        db.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        row = db.scalar(select(AssemblyBuildStep).where(AssemblyBuildStep.scope == scope, AssemblyBuildStep.spec_version_id == spec_version_id,
                                                        AssemblyBuildStep.definition_key == o.key)
                        .execution_options(populate_existing=True))
        if row is None:
            raise
        fill(row)
        db.flush()
    rows.insert(0, row)


@dataclass
class AssemblyBuild:
    spec_version_id: str
    policy: str
    definitions: list[DefinitionOutcome]
    revision: AssemblyRevision | None = None
    assembly: Assembly | None = None
    unplaced: list[str] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    calls: list[Any] = field(default_factory=list)
    scope: str | None = None  # S3 progress scope (``node_run:<id>``); None: progress not recorded
    # Generation supplement B: target vs actual (services/spec_coverage.coverage; status none without an assembly)
    coverage: dict[str, Any] | None = None
    # S3 continue: the build was identical to the partial revision it continues (that revision is returned, no new one)
    reused_revision: bool = False
    # Generation supplement F (Turbo): the AI placement decision of the instances without a position, and the build notes
    placement: dict[str, Any] | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)
    # Generation reliability J (design §2.2): why no further definition was started (cancel_requested, lease_lost,
    # runtime_budget = the step's runtime cap, budget = the run's call / time budget,
    # provider_failures = repeated exhausted service failures in this attempt), None when every one was tried
    stop_reason: str | None = None

    @property
    def built(self) -> list[DefinitionOutcome]:
        return [d for d in self.definitions if d.status == "built"]

    def units(self) -> dict[str, Any]:
        """J (§2.2): at the end of the step, what was kept, what was never started, what failed (with its failure class)
        and what waits for answers, why it stopped, and the continue action (None: nothing to continue; ``raise_budget``:
        the run's budget is used up — retry the step with a larger one, the kept definitions are reused)."""
        failed = [{"key": d.key, "code": (d.error or {}).get("code"), "failure_class": (d.error or {}).get("failure_class")}
                  for d in self.definitions if d.status == "failed"]
        action = (self.failure or {}).get("recovery")
        # C (design §6.3): earlier revisions passed over (modeled again), with the reason
        skipped = [{"key": d.key, **{k: d.reuse_skipped.get(k) for k in ("revision_id", "code", "capabilities", "reason")}}
                   for d in self.definitions if d.reuse_skipped]
        return {"kept": [d.key for d in self.built], "not_started": [d.key for d in self.definitions if d.status == "not_started"],
                "failed": failed, "waiting": [d.key for d in self.definitions if d.status in ("blocked", "needs_clarification")],
                "stopped_by": self.stop_reason, "continue": "raise_budget" if self.stop_reason == "budget" and action else action,
                **({"reuse_skipped": skipped} if skipped else {})}

    @property
    def complete(self) -> bool:
        return self.revision is not None and (self.coverage or {}).get("status", "complete") == "complete"

    def brief(self, db: Session) -> dict[str, Any]:
        """The ``assembly`` port value: the revision (when built, also a partial one), every definition's outcome and
        the coverage (target vs actual; ``complete`` only when nothing of the target is missing)."""
        out: dict[str, Any] = {"type": "assembly", "spec_version_id": self.spec_version_id, "execution_policy": self.policy,
                               "complete": self.complete, "definitions": [d.out() for d in self.definitions],
                               "parts": [{"key": d.key, "part_id": d.part_id, "revision_id": d.revision_id, "rev_no": d.rev_no,
                                          "number": d.part_number, "name": d.name} for d in self.built],
                               "unplaced_definitions": self.unplaced,
                               "progress": {"built": len(self.built), "reused": sum(1 for d in self.definitions if d.reused),
                                            "total": len(self.definitions), "scope": self.scope},
                               "simulated": any(d.simulated for d in self.definitions),
                               "coverage": self.coverage, "units": self.units()}
        if self.placement is not None:
            out["placement"] = self.placement
        if self.notes:
            out["notes"] = self.notes
        # how many AI / rule decisions the product rests on (the revision's and its built parts'; design §2.3)
        decided = [{**a, "owner": {"type": "model_revision", "id": d.revision_id}} for d in self.built for a in d.assumptions or []
                   if isinstance(a, dict)]
        if self.revision is not None:
            decided += [{**a, "owner": {"type": "assembly_revision", "id": str(self.revision.id)}} for a in self.revision.assumptions or []
                        if isinstance(a, dict)]
        out["decisions"] = {k: v for k, v in pol.decisions_summary(decided).items() if k != "items"}
        if self.revision is not None:
            rev, asm = self.revision, self.assembly or db.get(Assembly, self.revision.assembly_id)
            out.update({"assembly_id": str(rev.assembly_id), "assembly_revision_id": str(rev.id), "rev_no": rev.rev_no,
                        "label": f"A{rev.rev_no}", "number": asm.number if asm else None, "name": asm.name if asm else None,
                        "stats": rev.stats or {}, "check_status": rev.check_status, "verification": rev.verification,
                        "bom_sha256": rev.bom_sha256, "path": f"/assemblies/{rev.assembly_id}?rev={rev.id}"})
        if self.failure is not None:
            out["failure"] = self.failure
        return out


def _free_part_number(db: Session, project_id: uuid.UUID, number: str, skip: Collection[str] = ()) -> str:
    """A free part number (claimed for this transaction, so concurrent builds of one spec never collide), none of
    ``skip`` (promised to other definitions of this build)."""
    from .assemblies import _free_part_number as free

    return free(db, project_id, (number or "P").strip() or "P", skip)


def _facts_of(content: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [f for f in content.get("facts") or [] if isinstance(f, dict) and str(f.get("subject") or "").split(".")[0] == key]


def _material_of(content: dict[str, Any], d: dict[str, Any]) -> str:
    """The part material from the definition or its material fact (catalog id when known); never a default alloy."""
    from ..domain import find_material

    raw = d.get("material")
    if not raw:
        fact = next((f for f in _facts_of(content, d["key"]) if str(f.get("subject")).endswith(".material")
                     and f.get("value") not in (None, "") and f.get("status") != "unknown"), None)
        raw = fact.get("value") if fact else None
    if not raw:
        return "unknown"
    m = find_material(str(raw))
    return (m.id if m else str(raw))[:40]


def _fmt(v: Any, unit: str | None) -> str:
    return f"{v} {unit}" if unit and unit not in ("°", "deg") else f"{v}{unit or ''}"


_KNOWN_FACT = {"turbo": "adopted upstream in this Turbo run — a known fact, use it as given",
               "confirmed": "confirmed by a person",
               "design": "AI-selected design input, unconfirmed; model the supplied value exactly"}


def _assumption_of(content: dict[str, Any], f: dict[str, Any]) -> str | None:
    """The id of the spec assumption a fact rests on (its derivation, else an assumption on the same subject)."""
    ids = {str(a.get("id")): a for a in content.get("assumptions") or [] if isinstance(a, dict)}
    if f.get("derivation") and str(f["derivation"]) in ids:
        return str(f["derivation"])
    return next((aid for aid, a in ids.items() if a.get("subject") == f.get("subject")), None)


def _fact_context(f: dict[str, Any]) -> str:
    """Retain semantic derivation and source location; a located entity is not a verified interpretation."""
    context = {k: f[k] for k in ("id", "kind", "derivation") if f.get(k) is not None}
    evidence = [{k: v for k, v in e.items() if k != "context"} for e in f.get("evidence") or [] if isinstance(e, dict)]
    if evidence:
        context["evidence"] = evidence
    return " — " + json.dumps(context, ensure_ascii=False, separators=(",", ":")) if context else ""


def definition_source_contexts(content: dict[str, Any], d: dict[str, Any]) -> tuple[DefinitionSourceContext, ...]:
    """Only server-owned context of this definition's verified facts; old specs naturally have none."""
    contexts: dict[tuple[str, int, int, str], DefinitionSourceContext] = {}
    for fact in _facts_of(content, d["key"]):
        if fact.get("status") not in ("asserted", "derived"):
            continue
        for evidence in fact.get("evidence") or []:
            if not isinstance(evidence, dict) or evidence.get("verified") is not True or not evidence.get("context"):
                continue
            try:
                ctx = DefinitionSourceContext.model_validate(evidence["context"])
            except ValidationError:
                continue
            if (ctx.definition != d["key"] or ctx.name != d.get("name") or ctx.source != evidence.get("source")
                    or ctx.chunk != evidence.get("chunk") or ctx.anchor_quote != evidence.get("quote")):
                continue
            # Different facts can cite different phrases in the same passage. Each verified anchor remains
            # available to the exact requirement that cites it, independently of the facts' ordering.
            contexts.setdefault((ctx.source, ctx.start, ctx.end, ctx.anchor_quote), ctx)
    return tuple(contexts.values())


def _coordinate_fact(f: dict[str, Any]) -> bool:
    """Frame mappings and placements give context without importing another part's feature dimensions."""
    field = str(f.get("subject") or "").split(".", 1)[-1]
    if re.search(r"(?:^|_)(?:counts?|qty|quantit(?:y|ies)|totals?)(?:_|$)", field, re.I):
        return False
    # Feature fields such as hole_coordinates and its Chinese equivalent still carry direct geometry references.
    # Only explicit frame definitions and qualified axis/frame mappings belong to this context.
    return bool(re.search(r"(?:^|_)(?:frames?|coordinate_systems?|placements?|orientations?|poses?|transforms?)(?:_|$)", field, re.I)
                or re.fullmatch(r"(?:local|parent|global|root|world|assembly|part)_coordinates?", field, re.I)
                or re.search(r"(?:^|_)(?:axis|axes|coordinates?|frames?|parameters?|parent|global|root|world|assembly|instance|local)"
                             r"(?:_[a-z0-9]+)*_mappings?(?:_|$)", field, re.I)
                or re.search(r"坐标系|坐标映射|轴映射|基准系|装配位置|放置|姿态", field))


def _definition_dependencies(content: dict[str, Any], key: str) -> tuple[set[str], dict[str, list[str]]]:
    """Direct feature references plus the coordinate chain through connected definitions and parent frames.

    Shared names identify only actual neighbours; they never copy every similarly named part into a request.
    Coordinate traversal follows its own references and parents rather than walking every branch of the mate graph.
    """
    defs = {str(d["key"]): d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}
    insts = {str(i["key"]): i for i in content.get("instances") or [] if isinstance(i, dict) and i.get("key")}
    root = str(content.get("root") or "D-00")
    neighbours: dict[str, dict[str, list[str]]] = {}
    parents: dict[str, dict[str, list[str]]] = {}
    for ik, inst in insts.items():
        dk, parent = str(inst.get("definition") or ""), str(inst.get("parent") or root)
        parents.setdefault(dk, {}).setdefault(parent, []).append(ik)

    def definition(end: dict[str, Any]) -> str:
        instance = str(end.get("instance") or "")
        return instance[1:] if instance.startswith("@") else str((insts.get(instance) or {}).get("definition") or "")

    for relation in content.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        a, b = (definition(relation.get(side) or {}) for side in ("a", "b"))
        if a not in defs or b not in defs or a == b:
            continue
        for own, other in ((a, b), (b, a)):
            neighbours.setdefault(own, {}).setdefault(other, []).append(str(relation.get("key") or "?"))
    names: dict[str, set[str]] = {}
    for dk, d in defs.items():
        for value in [dk, d.get("name"), d.get("number"), *(d.get("aliases") or [])]:
            name = str(value or "").strip()
            if len(name) >= 2:
                names.setdefault(name, set()).add(dk)

    def references(own: str, facts: list[dict[str, Any]]) -> set[str]:
        text = " ".join(json.dumps(f.get("value"), ensure_ascii=False) + " " + " ".join(
            str(e.get("quote") or "") for e in f.get("evidence") or [] if isinstance(e, dict)) for f in facts)
        related: set[str] = set()
        for name, owners in names.items():
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])" if name.isascii() else re.escape(name)
            if not re.search(pattern, text):
                continue
            related |= owners if len(owners) == 1 else owners & set(neighbours.get(own) or {})
        return related - {own}

    facts = _facts_of(content, key)
    direct = references(key, [f for f in facts if not _coordinate_fact(f)])
    reasons: dict[str, list[str]] = {}

    def add(other: str, reason: str) -> None:
        if other in defs and other != key:
            why = reasons.setdefault(other, [])
            if reason not in why:
                why.append(reason)

    for other, relations in neighbours.get(key, {}).items():
        add(other, f"connected to {key} by {', '.join(dict.fromkeys(relations))}")
    for other in direct:
        add(other, f"referenced by {key}")
    pending = [key]
    seen: set[str] = set()
    while pending:
        own = pending.pop(0)
        if own in seen:
            continue
        seen.add(own)
        for parent, children in parents.get(own, {}).items():
            add(parent, f"parent frame of {own} ({', '.join(children)})")
        for other in references(own, [f for f in _facts_of(content, own) if _coordinate_fact(f)]):
            add(other, f"coordinate reference from {own}")
        pending += [other for other in defs if other in reasons and other not in seen and other not in pending]
    return direct, reasons


def definition_request(content: dict[str, Any], d: dict[str, Any], quantity: int, *, version_label: str = "",
                       known: dict[str, str] | None = None, source_references: DefinitionSourceReferences | None = None) -> str:
    """The modeling request of one spec definition, composed from what the spec holds about it: name, kind,
    representation, material, its facts (status and evidence), adopted assumptions, template, notes and manufacturing
    notes, plus separately verified complete original documents when available. Original documents remain reference
    material, not additional canonical facts. Overall size is restated only when length, width and height are all stated in mm.

    ``known`` (S5, :func:`known_values`): assumption id → ``turbo`` (adopted upstream in this Turbo chain) or
    ``confirmed`` (a person confirmed it). Those values are passed down as known facts, so a downstream step neither
    asks about them again nor adopts another value."""
    known = known or {}
    key = d["key"]
    kind = {"standard_part": "standard part", "part": "part"}.get(d.get("kind") or "part", d.get("kind") or "part")
    lines = [f"Create one {kind} for an assembly: \"{d.get('name') or key}\" (engineering spec {version_label} definition {key}"
             + (f", drawing no. {d['number']}" if d.get("number") else "") + ")."]
    lines.append("Units mm; right-handed, Z up. Model the part once in its own frame (origin at its datum); the assembly places "
                 f"its {quantity} instance(s), so never model several copies.")
    lines.append("Drawing parameters such as u/v/a/b do not automatically mean X/Y/Z. Choose a right-handed part frame "
                 "compatible with any assembly-axis mapping stated in the sources. If swapping parameter axes introduces "
                 "a reflection, transform the feature geometry during part construction; assembly placement only "
                 "translates and rotates and cannot mirror the part.")
    lines.append("Before choosing that frame, compose the complete chain of parameter-to-parent mappings through the "
                 "connected definitions to the root assembly frame, including axis signs and translations. Do not "
                 "equate an intermediate part's parameter axes with root XYZ or swap this part's axes from just one "
                 "neighbouring mapping. Preserve source feature coordinates under the composed mapping.")
    rep = d.get("representation") or "exact"
    if rep != "exact":
        lines.append(f"Representation: {rep} (a {rep} shape is enough; say what is simplified).")
    facts = _facts_of(content, key)
    has_material_fact = any(str(f.get("subject")).endswith(".material") for f in facts)
    lines.append(f"Material: {d.get('material') or ('see the facts' if has_material_fact else 'not stated')}.")
    dims: dict[str, float] = {}
    mine = [a for a in content.get("assumptions") or [] if isinstance(a, dict) and str(a.get("subject") or "").split(".")[0] == key]
    kinds = {known.get(str(a.get("id"))) for a in mine} - {None}
    if "turbo" in kinds:
        lines.append("Values marked \"adopted upstream\" were adopted earlier in this Turbo run: they are known facts. Use them as "
                     "given; never ask about them and never adopt another value for them.")
    if "confirmed" in kinds:
        lines.append("Values marked \"confirmed by a person\" were adopted by AI and confirmed by a person: treat them as stated.")
    if "design" in kinds:
        lines.append("AI-selected design inputs below were selected and frozen by the upstream engineering design stage under explicit human "
                     "delegation. They remain unconfirmed design decisions, not human-approved safety facts. For THIS modeling step they "
                     "are exact supplied geometry inputs: cite their literal values from this request, use them as given, and do not "
                     "select them again or put already-supplied fields in requirement.assumed. Keep truly missing critical values unresolved. "
                     "The original AI decisions remain unconfirmed in the upstream ledger and the delivery report.")
    if facts:
        lines.append("Claims extracted from the spec (status in brackets, provenance after —). When an original component "
                     "sheet is supplied, reconcile these claims with its actual views before creating requirements. "
                     "Evidence verified=true means the entity or quote was located, not that feature ownership was verified. "
                     "Different field names can describe the same feature; interface or mating dimensions do not by "
                     "themselves authorize additional holes or cuts on unspecified faces.")
        for f in facts:
            fld = str(f.get("subject")).split(".", 1)[1] if "." in str(f.get("subject")) else str(f.get("subject"))
            status = str(f.get("status"))
            aid = _assumption_of(content, f) if status == "assumed" and known else None
            if aid is not None and known.get(aid):
                status = f"{_KNOWN_FACT[known[aid]]}, {aid}"
            lines.append(f"- {fld} = {_fmt(f.get('value'), f.get('unit'))} [{status}]" + _fact_context(f))
            if f.get("unit") == "mm" and fld in ("length", "width", "height") and isinstance(f.get("value"), (int, float)):
                dims[fld] = float(f["value"])
    for a in mine:
        how = known.get(str(a.get("id")))
        label = (f"adopted upstream by {a.get('author')} ({a.get('id')}: {a.get('basis')}) — a known fact of this Turbo run, use it as given"
                 if how == "turbo" else
                 f"AI-selected design input, unconfirmed ({a.get('id')}: {a.get('basis')}); use the supplied value exactly" if how == "design" else
                 f"confirmed by a person ({a.get('id')}, adopted by {a.get('author')}: {a.get('basis')})" if how == "confirmed" else
                 f"assumed by {a.get('author')}, {a.get('id')}: {a.get('basis')}")
        lines.append(f"- {a.get('field') or str(a.get('subject')).split('.', 1)[-1]} = {_fmt(a.get('adopted'), a.get('unit'))} [{label}]")
    if len(dims) == 3:
        lines.append(f"Overall size: {dims['length']:g}x{dims['width']:g}x{dims['height']:g} mm (length x width x height, the facts above).")
    # Where the instances sit, so coordinates stated in the assembly frame are not mistaken for another frame
    # (real-model round 2026-10-10: a gasket stated in assembly coordinates asked which frame they were in).
    insts = [i for i in content.get("instances") or [] if isinstance(i, dict) and i.get("definition") == key]
    placed = [(i, i.get("placement") or {}) for i in insts]
    if placed:
        lines.append("Placement in the parent assembly: " + "; ".join(
            f"{i.get('key')} translate {pl.get('translate')} rotate {pl.get('rotate')}" for i, pl in placed[:12]) + ".")
        if all(pl and not any(pl.get("translate") or [0, 0, 0]) and not any(pl.get("rotate") or [0, 0, 0]) for _, pl in placed):
            lines.append("Every instance sits at the parent origin without rotation, so coordinates the spec states in the assembly "
                         "frame are this part's own coordinates: model the part exactly there (do not re-centre it).")
    direct, coordinate_dependencies = _definition_dependencies(content, key)
    for other in content.get("definitions") or []:
        name = str(other.get("name") or "").strip()
        other_key = str(other.get("key") or "")
        if other_key not in direct:
            continue
        related = [f for f in _facts_of(content, other_key) if not _coordinate_fact(f)]
        if related:
            lines.append(f"Facts of \"{name}\" ({other_key}) that the facts above refer to:")
            lines += [f"- {str(f.get('subject')).split('.', 1)[-1]} = {_fmt(f.get('value'), f.get('unit'))} [{f.get('status')}]"
                      + _fact_context(f) for f in related]
    coordinate_lines: list[str] = []
    for other in content.get("definitions") or []:
        other_key = str(other.get("key") or "")
        related = [f for f in _facts_of(content, other_key) if _coordinate_fact(f)]
        placements = [{"instance": i.get("key"), "parent": i.get("parent") or content.get("root") or "D-00",
                       "placement": i["placement"]} for i in content.get("instances") or []
                      if isinstance(i, dict) and i.get("definition") == other_key and i.get("placement")]
        if other_key not in coordinate_dependencies or not (related or placements):
            continue
        coordinate_lines.append(f"Coordinate facts of \"{other.get('name') or other_key}\" ({other_key}; "
                                + "; ".join(coordinate_dependencies[other_key]) + "):")
        coordinate_lines += [f"- {f.get('subject')} = {_fmt(f.get('value'), f.get('unit'))} [{f.get('status')}]"
                             + _fact_context(f) for f in related]
        if placements:
            coordinate_lines.append("- Stated placements: " + json.dumps(placements, ensure_ascii=False, separators=(",", ":")))
    if coordinate_lines:
        lines.append(f"Coordinate and placement reference context (root {content.get('root') or 'D-00'}). These facts "
                     "describe reference frames, not additional geometry to create in this part:")
        lines += coordinate_lines
    tpl = d.get("template") or {}
    if tpl.get("id"):
        lines.append(f"The spec names part template {tpl['id']} with parameters {json.dumps(tpl.get('params') or {}, ensure_ascii=False)}.")
    notes = [n for n in content.get("manufacturing_notes") or [] if isinstance(n, dict) and n.get("subject") == key]
    if notes or d.get("notes"):
        lines.append("Notes:")
        lines += [f"- {str(n.get('text'))}" for n in notes]
        if d.get("notes"):
            lines.append(f"- {str(d['notes'])}")
    context_passages: set[tuple[str, int, int]] = set()
    for source_context in definition_source_contexts(content, d):
        passage = (source_context.source, source_context.start, source_context.end)
        if passage in context_passages:
            continue
        context_passages.add(passage)
        # This is prompt evidence, not a mechanism for reconstructing trusted context from request text.
        lines.append("Definition source context: " + source_context.model_dump_json())
    if source_references is not None:
        lines.append(source_references.prompt())
    out = "\n".join(lines)
    return out


def definition_fingerprint(content: dict[str, Any], d: dict[str, Any], quantity: int, *, known: dict[str, str] | None = None,
                           drawing_source: dict[str, Any] | None = None,
                           source_references: DefinitionSourceReferences | None = None) -> str:
    """What a built part of this definition rests on (generation supplement H, brief B-04 "reuse across spec versions must verify that parameters and dependencies
    remain compatible"): its modeling request (:func:`definition_request`, the version label left out), template id / params,
    material and kind. Equal across versions only when nothing the part is built from changed."""
    tpl = d.get("template") or {}
    return _canon({"request": definition_request(content, d, quantity, version_label="", known=known,
                                                source_references=source_references), "kind": d.get("kind") or "part",
                   "template": {"id": tpl.get("id"), "params": tpl.get("params") or {}} if tpl.get("id") else None,
                   "material": _material_of(content, d), "drawing_source": drawing_source,
                   **({"source_references": source_references.fingerprint_data()} if source_references is not None else {}),
                   **({"design": {"ref": content["design"]["ref"],
                                  "requirements_sha256": content["design"]["requirements_sha256"].get(d["key"])}}
                      if content.get("design") else {}),
                   "visual_component_evidence": [f.get("evidence") for f in content.get("facts", [])
                                                   if f.get("subject") == f"{d.get('key')}.visual_presence"]})


class _EarlierBuilds:
    """The definitions built for this project under the same policy by other builds of the same spec — its other
    versions (re-extracted, answered, derived) and other runs of this version — by definition key, this version's first,
    then newest first. Use the stored fingerprint; only legacy versions without original text or drawing sources can
    fall back to a fingerprint computed from their version content, once per version."""

    def __init__(self, db: Session, *, project_id: uuid.UUID, v: EngineeringSpecVersion, policy: str, exclude_scope: str | None) -> None:
        from ..models import AssemblyBuildStep

        self.db, self.project_id, self.policy = db, project_id, policy
        q = (select(AssemblyBuildStep).join(EngineeringSpecVersion, EngineeringSpecVersion.id == AssemblyBuildStep.spec_version_id)
             .where(EngineeringSpecVersion.spec_id == v.spec_id, AssemblyBuildStep.project_id == project_id,
                    AssemblyBuildStep.status == "built", AssemblyBuildStep.execution_policy == policy,
                    AssemblyBuildStep.revision_id.is_not(None)))
        if exclude_scope is not None:
            q = q.where(AssemblyBuildStep.scope != exclude_scope)
        rows = sorted(db.scalars(q).all(), key=lambda r: (r.spec_version_id != v.id, -(r.updated_at.timestamp() if r.updated_at else 0.0)))
        self.by_key: dict[str, list[Any]] = {}
        for r in rows:
            self.by_key.setdefault(r.definition_key, []).append(r)
        self._versions: dict[uuid.UUID, dict[str, Any]] = {}
        #: C (design §6.3): definition key → the first matching build passed over (:func:`reuse_blocked`)
        self.skipped: dict[str, dict[str, Any]] = {}

    def _fingerprint(self, row: Any) -> str | None:
        stored = (row.outcome or {}).get("fingerprint")
        if stored:
            return str(stored)
        from .engineering_spec import quantities

        cache = self._versions.get(row.spec_version_id)
        if cache is None:
            ver = self.db.get(EngineeringSpecVersion, row.spec_version_id)
            if ver is not None:
                from .assembly_drawing_evidence import source_fingerprint
                if source_fingerprint(ver)["sources"] or has_text_sources(self.db, ver):
                    # Legacy rows without a stored fingerprint predate the original-sheet
                    # or full-text modeling path. Recomputing today's fingerprint would falsely bless them.
                    return None
            content = (ver.content if ver is not None else None) or {}
            cache = {"content": content, "qty": quantities(content), "fp": {},
                     "known": known_values(self.db, ver, self.policy) if ver is not None else {},
                     "defs": {d["key"]: d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}}
            self._versions[row.spec_version_id] = cache
        key = row.definition_key
        if key not in cache["fp"]:
            d = cache["defs"].get(key)
            cache["fp"][key] = definition_fingerprint(cache["content"], d, int((cache["qty"].get(key) or {}).get("total") or 0),
                                                      known=cache["known"]) if d is not None else None
        return cache["fp"][key]

    def find(self, key: str, fingerprint: str) -> tuple[Any, Part, ModelRevision] | None:
        """The first still valid build of ``key`` with this fingerprint (a simulated one only in simulation mode) that
        :func:`reuse_blocked` does not pass over (the first one passed over is kept in :attr:`skipped`)."""
        from ..config import get_settings

        for row in self.by_key.get(key, []):
            if row.simulated and not get_settings().ai_simulation:
                continue
            if self._fingerprint(row) != fingerprint:
                continue
            hit = _still_built(self.db, row, self.project_id, self.policy)
            if hit is None:
                continue
            why = reuse_blocked(self.db, hit[1], spec_version_id=row.spec_version_id)
            if why is not None:
                self.skipped.setdefault(key, why)
                continue
            return row, hit[0], hit[1]
        return None


def _spec_part_props(db: Session, part: Part, user: User, d: dict[str, Any]) -> None:
    """The procurement / representation a spec definition states, set on its new part before its first revision (a
    template then fills only what is still open)."""
    from .assemblies import set_part_props

    proc = d.get("procurement") if d.get("procurement") in ("make", "buy", "standard") else (
        "standard" if d.get("kind") == "standard_part" else None)
    rep = d.get("representation") if d.get("representation") in ("nominal", "simplified") else None
    if proc or rep:
        set_part_props(db, part, user, procurement=proc, representation=rep, source={"spec_definition": d.get("key")})


def _template_revision(db: Session, *, project_id: uuid.UUID, user: User, d: dict[str, Any], content: dict[str, Any], number: str,
                       policy: str, node_run_id: uuid.UUID | None) -> tuple[ModelRevision | None, dict[str, Any] | None]:
    """A new part built from the W3 template the definition names. (None, error) when there is no template library or
    the template / parameters are refused (the caller then models the part)."""
    tpl = d.get("template") or {}
    if not tpl.get("id"):
        return None, None
    try:
        from ..cad import templates  # noqa: F401  (W3 geometry library)
    except ImportError:
        return None, {"code": "capability_unavailable", "message": "no part template library in this build"}
    from . import revisions

    sp = db.begin_nested()
    try:
        part = Part(project_id=project_id, number=_free_part_number(db, project_id, number), name=(d.get("name") or number)[:200],
                    material=_material_of(content, d), owner_id=user.id, description=f"spec definition {d['key']} (template {tpl['id']})")
        db.add(part)
        db.flush()
        _spec_part_props(db, part, user, d)  # the spec's stated values win over the template's defaults
        rev = revisions.create_template_revision(db, part_id=part.id, user=user, template_id=str(tpl["id"]), params=tpl.get("params") or {},
                                                 note=f"Template {tpl['id']} for spec definition {d['key']}", execution_policy=policy)
        if node_run_id is not None and rev.node_run_id is None:
            rev.node_run_id = node_run_id
        sp.commit()
        return rev, None
    except AppError as exc:
        sp.rollback()
        return None, {"code": exc.code, "message": exc.message, "details": exc.details or {}}


#: J: the stop a modeling failure of a stop class means for the rest of the build.
_CLASS_STOP = {"cancelled": "cancel_requested", "lease_lost": "lease_lost", "budget": "budget"}


@dataclass
class _DefinitionJob:
    """A definition left for AI modeling (:func:`build_assembly`): its outcome, the part number hint and the number promised
    to it, the modeling request. ``part`` holds plain values only, so the job can go to a worker thread."""

    seq: int
    o: DefinitionOutcome
    hint: str
    request: str
    part: Any  # ai_modeling.PartBrief
    drawing_bundle: Any = None  # assembly_drawing_evidence.DrawingBundle (plain values)
    source_contexts: tuple[DefinitionSourceContext, ...] = ()
    design_context: DesignDefinitionContext | None = None
    source_references: DefinitionSourceReferences | None = None


def _definition_unit(key: str) -> str:
    """J: the logical work unit of a definition's model calls."""
    return f"definition:{key}"


def _design_definition(provider: Any, cfg: Any, job: _DefinitionJob, policy: str) -> Any:
    """In a worker thread: the model calls and kernel builds of one definition (``ai_modeling.design``; no session, no ORM
    object), its calls under the unit ``definition:<key>``."""
    from ..ai import retry as call_retry
    from ..ai import prompts
    from ..ai.providers.base import schema_budget_text
    from ..ai.modeling_agent import agent_reply_json_schema, reconstruct_reply_json_schema, turbo_reply_json_schema
    from . import ai_modeling
    from .assistant_context import _budget, estimate_tokens

    with call_retry.unit_scope(_definition_unit(job.o.key)):  # J: the logical work unit of these model calls
        drawing = job.drawing_bundle.drawing if job.drawing_bundle is not None else None
        system = prompts.RECONSTRUCT_SYSTEM_PROMPT if drawing is not None else prompts.MODELING_SYSTEM_PROMPT
        if drawing is not None and drawing.assembly_component:
            system += "\n\n" + prompts.ASSEMBLY_COMPONENT_EVIDENCE_SYSTEM
        if drawing is not None:
            user_prompt = prompts.reconstruct_user_message(drawing.block, sheet_label=drawing.sheet_label, notes=job.request,
                                                          part_name=getattr(job.part, "name", None),
                                                          material=getattr(job.part, "material", None))
        else:
            user_prompt = prompts.modeling_user_message(job.request, None, part_name=getattr(job.part, "name", None),
                                                       material=getattr(job.part, "material", None))
        if pol.is_turbo(policy):
            from ..ai.turbo import MODELING_TURBO_BLOCK, MODELING_TURBO_SYSTEM
            system += "\n\n" + MODELING_TURBO_SYSTEM
            user_prompt += "\n\n" + MODELING_TURBO_BLOCK
        if job.design_context is not None:
            system += "\n\n" + DESIGN_MODELING_SYSTEM
            user_prompt += job.design_context.prompt()
        schema = (turbo_reply_json_schema(drawing=drawing is not None) if pol.is_turbo(policy) else
                  reconstruct_reply_json_schema() if drawing is not None else agent_reply_json_schema())
        schema_cfg = None if getattr(provider, "simulated", False) else cfg
        required = estimate_tokens(system + user_prompt + schema_budget_text(schema, schema_cfg))
        # Message framing and a conservative estimate for bounded 2,000 px sheet images.
        required += 256 + (4096 * len(drawing.images) if drawing is not None else 0)
        input_limit, _, _, available, limit_source = _budget(cfg)
        if required > available:
            return ai_modeling.ModelingResult(status="failed", label=ai_modeling.label_of(provider, cfg),
                error={"code": "context_over_budget", "retryable": False,
                "message": f"the complete definition and source references need about {required} tokens; about {available} available",
                "details": {"required_est": required, "available": available, "input_limit": input_limit,
                            "limit_source": limit_source, "estimated": True}})
        return ai_modeling.design(provider=provider, cfg=cfg, part=job.part, text=job.request, drawing=drawing, policy=policy,
                                  source_contexts=job.source_contexts, design_context=job.design_context)


def _modeling_failed(o: DefinitionOutcome, res: Any, turbo: bool) -> tuple[str, str | None] | None:
    """The outcome of a definition whose modeling built nothing (asked, or failed); the stop it means for the rest of the
    build (stop reason, message), else None."""
    from ..ai import retry as call_retry
    from ..ai.providers.base import STOP_CLASSES

    if res.status == "needs_clarification" and turbo:
        # S5: inside a Turbo chain a downstream question is a protocol error of that step, never a question for a person
        # (the upstream values are known facts); the independent definitions still build.
        o.status, o.error = "failed", pol.turbo_clarification_error(res.questions, step=f"modeling of {o.key}")
        return None
    if res.status == "needs_clarification":
        o.status = "needs_clarification"
        o.questions = [{"question": q, "source": "modeling"} for q in res.questions]
        if res.gaps:
            o.questions.append({"gaps": res.gaps, "source": "modeling"})
        return None
    err = res.error or {}
    o.status = "failed"
    # J (design §2.1): the failure class names the recovery; a limit that stopped the modeling stops new work
    # a provider error keeps the adapter's class (overloaded / server_error / stream_broken ≠ connection)
    cls = err.get("failure_class") or call_retry.outcome_class(err.get("code"), res.steps)
    recovery = ("configure_model" if cls in ("auth", "permission", "quota") else
                "fix_spec" if cls in ("unsupported", "bad_request") else "retry_definition")
    o.error = _failure_info(err.get("code") or "ai_failed", err.get("message") or "the model could not produce a buildable design",
                            stage="modeling", retryable=bool(err.get("retryable")) or cls in STOP_CLASSES, recovery=recovery,
                            **({"failure_class": cls} if cls else {}))
    if o.error["code"] == pol.TURBO_CLARIFICATION_REFUSED:  # the modeling core's own Turbo guard (S5 protocol error)
        o.error.update(protocol_error=True, retryable=False, recovery=pol.PROTOCOL_RECOVERY)
    return (_CLASS_STOP[cls], err.get("message")) if cls in STOP_CLASSES else None


def build_assembly(db: Session, *, project_id: uuid.UUID, user: User, spec_version: EngineeringSpecVersion, policy: str, model_ref: Any,
                   node_run_id: uuid.UUID | None = None, number: str = "", run_checks: bool = True,
                   on_calls: Callable[[list[Any]], None] | None = None, keep_going: Callable[[], bool] | None = None,
                   deadline: float | None = None, cancelled: Callable[[], bool] | None = None, scope: str | None = None,
                   reuse_earlier_builds: bool = True, checkpoint: Callable[[], bool] | None = None,
                   concurrency: int | None = None) -> AssemblyBuild:
    """Build every part definition under the spec's root, then the assembly (see the module docstring).

    ``on_calls`` records the AI call records of each modeling call as it happens (the entry's own ledger: node run or
    agent execution). ``keep_going`` (lease heartbeat), ``cancelled`` (the run / operation was cancelled) and
    ``deadline`` (monotonic; the node's runtime cap) stop starting new definitions; those are reported
    ``not_started`` (cancelled / lease lost: no assembly is made; out of runtime: a partial one). Never raises for a
    definition's failure; raises AppError only for an unusable spec version.

    Partial assembly (generation supplement B-01, design §3 B): the built subset goes to ``create_from_spec_version``
    with ``partial=True``; ``out.coverage`` is the coverage block and ``out.failure`` is ``assembly_partial`` while it is
    not complete. A retry of the same node run continues the partial revision it made (a new revision of that
    assembly; :func:`_partial_of_scope`).

    Turbo placement (generation supplement F: TB-03 / B-02.4 / D-01; G; reliability L): after the parts are built and
    before the assembly is created, the kept instances with neither a placement nor a relation get AI placement calls
    with the run's modeling model, top-down — the main sub-assemblies in the root frame first, then their insides — in
    batches (:func:`app.services.assembly_placement.decide`, recorded through ``on_calls``; none when nothing is
    missing). The model chooses mates (round 3, B), the build's solver computes the positions; each mate applied is an
    ``ai_turbo`` decision on the root revision; an instance counts as installed
    only when every sub-assembly instance above it is placed too (``coverage.placed`` / ``decided``; positioned only
    inside an unplaced sub-assembly: ``local_only``); the rest is arranged for display by rule, with the reason in
    ``out.notes``. A provider error or timeout never fails the build. Standard: no call (unchanged).

    Progress (S3): every definition's outcome is recorded under ``scope`` (default: the node run, see
    :func:`build_scope`). A definition that this scope already built and that is still valid (:func:`_still_built`) is
    reused as it is (``reused``), with no modeling call; only the failed, blocked and not started definitions are built
    again.

    Across versions (generation supplement H, B-04): before a definition is built, a part revision another build of the
    same spec made (another version — re-extracted, answered, rule-derived — or another run of this version) under the
    same policy is reused when its :func:`definition_fingerprint` is the same and it is still valid (``reused``,
    ``reused_from: {spec_version_id, revision_id}``, no model call). Legacy rows with text or drawing sources and no
    stored fingerprint rebuild; other legacy rows get it computed from their version's content. A definition whose
    request changed (e.g. it received an alias's facts) is built
    again. Every new progress row stores its fingerprint. ``reuse_earlier_builds`` False turns this off (the same-scope
    S3 reuse above stays): the agent tools pass it for a business rework (``rework_no`` > 0 of one node task,
    ``agent_tools.build_scope``) — the reviewer rejected what the earlier round built, so its parts are not handed back.

    Generation reliability C (design §6.3): neither reuse hands back a revision :func:`reuse_blocked` passes over — one
    whose build record lists an omission needing a capability the kernel has gained since, or a record without
    capability information that lists omissions; the definition is modeled again, with ``reuse_skipped`` (revision,
    code, reason) on its outcome, its progress row, a note and ``out.units()``. Everything else is reused as before.

    Generation reliability J (design §2.1, §2.2): before each new definition the call limits of the running step
    (``retry.current_limits()``: the run's call / time budget, the step's runtime cap, cancel, lease) are checked; when
    one stops new work the rest is ``not_started`` (``budget``: a partial assembly of what was built, like the runtime
    cap) and ``out.units()`` lists kept / not started / failed and the continue action. Each definition's model calls
    carry the unit ``definition:<key>``. A definition whose modeling failed with a failure class keeps it in its error
    (``failure_class``; auth / permission / quota → ``configure_model``, unsupported / bad request → ``fix_spec``); a
    definition this scope found ``unsupported`` for the same request (fingerprint) is not modeled again until its
    request changes. ``checkpoint`` (the workflow engine's ``ctx.checkpoint``) commits each definition built — the part
    revision, its progress row and the call records — while the attempt still holds the node, so a dead worker or a
    lost lease keeps them and the next attempt builds only the rest; False stops new definitions (``lease_lost``).

    Parallel modeling: reuse, the unsupported memory, blocked definitions and templates are settled first, in definition
    order (a template build is checked against the stops like a modeling start), and every definition left for AI
    modeling gets its part number then (numbering never depends on which modeling finishes first). Those are modeled up
    to ``concurrency`` at a time (None: ``limits.MODELING_CONCURRENCY``; 1: one after another): a worker thread runs only
    the model calls and kernel builds (``ai_modeling.design``, in a copy of this thread's context); the part, revision,
    progress row, call records and checkpoint are written in this thread as each result comes back — the session never
    leaves it. The stops are checked before each definition starts, with one request of the call budget set aside for
    each running definition that has not made its first one yet; after a stop none starts, the running ones finish and
    are kept. A definition a limit stopped before its first request is ``not_started``. A worker that raises stops
    new starts too; the running ones are kept, then the error is raised."""
    from ..ai import limits as ai_limits
    from ..ai import retry as call_retry
    from ..ai.providers.base import STOP_CLASSES, ProviderError
    from . import ai_modeling
    from . import assemblies as asm_svc
    from .assembly_drawing_evidence import DrawingEvidenceCache, source_fingerprint
    from .assembly_service_guard import DefinitionServiceGuard
    from ..cad.drawing_evidence import DrawingEvidenceError
    from .engineering_spec import quantities, readiness
    from .spec_coverage import coverage

    pol.parse_policy(policy)
    v, spec = load_spec_version(db, project_id, spec_version.id, policy)
    content = v.content or {}
    from . import design_spec
    design_context = design_spec.definition_contexts(db, user, v, node_run_id=node_run_id)
    turbo = pol.is_turbo(policy)
    known = known_values(db, v, policy)  # S5: upstream values reach each definition as known facts
    rd = readiness(content, policy)
    qty = quantities(content)
    tree = asm_svc.spec_tree(content)
    questions = {q["id"]: q for q in content.get("questions") or [] if isinstance(q, dict) and q.get("id")}
    defs = [d for d in content.get("definitions") or [] if d.get("key") in tree["definitions"] and d.get("kind") != "assembly"]
    root = next((d for d in content.get("definitions") or [] if d.get("key") == tree["root"]), {})
    base_number = (number or root.get("number") or f"SP{spec.number}-V{v.version_no}").strip()[:60]
    label = f"SP-{spec.number} v{v.version_no}"
    out = AssemblyBuild(spec_version_id=str(v.id), policy=policy,
                        definitions=[DefinitionOutcome(key=d["key"], name=d.get("name") or d["key"], kind=d.get("kind") or "part",
                                                       quantity=int((qty.get(d["key"]) or {}).get("total") or 0)) for d in defs],
                        unplaced=tree["unplaced"])
    by_key = {o.key: o for o in out.definitions}
    if not defs:
        msg = "the spec defines no part under its root assembly; nothing can be built"
        out.failure = {**_failure_info("spec_empty", msg, stage="assembly_modeling", retryable=False, recovery="fix_spec",
                                       recovery_message="check the spec page (what was read, open questions) and describe the parts "
                                                        "and the structure"),
                       "details": {"root": tree["root"], "definitions": len(content.get("definitions") or []), "recovery": "fix_spec"}}
        out.coverage = coverage(content, built=set(), placed={}, missing=[], assembled=False)
        return out
    provider: Any = None
    cfg: Any = None
    provider_error: dict[str, Any] | None = None
    stop_reason: str | None = None
    out.scope = scope or build_scope(node_run_id)
    progress = _build_steps(db, v.id, [out.scope] if out.scope else [])
    # Generation supplement H (B-04): builds of other versions of this spec / other runs of this version, by fingerprint
    elsewhere = _EarlierBuilds(db, project_id=project_id, v=v, policy=policy, exclude_scope=out.scope) if reuse_earlier_builds else None
    fingerprints: dict[str, str] = {}
    drawing_cache: DrawingEvidenceCache | None = None
    drawing_source = source_fingerprint(v)
    if not drawing_source["sources"]:
        drawing_source = None
    text_cache = DefinitionSourceReferenceCache(db, v)

    def record(o: DefinitionOutcome) -> None:
        _record_step(db, rows=progress.setdefault(o.key, []), scope=out.scope, project_id=project_id, spec_version_id=v.id,
                     policy=policy, o=o, node_run_id=node_run_id, fingerprint=fingerprints.get(o.key))

    def replay(o: DefinitionOutcome, row: Any, part: Part, rev: ModelRevision) -> None:
        o.status, o.source, o.reused, o.simulated = "built", row.source, True, bool(row.simulated)
        o.part_id, o.part_number, o.revision_id, o.rev_no = str(part.id), part.number, str(rev.id), rev.rev_no
        o.assumptions = list((row.outcome or {}).get("assumptions") or [])
        o.check, o.notes = (row.outcome or {}).get("check"), list((row.outcome or {}).get("notes") or [])

    limits = call_retry.current_limits()  # J: the running step's call limits (None outside the workflow engine)
    assembly_ends = [end for end in (deadline, limits.deadline if limits is not None else None) if end is not None]
    assembly_deadline = min(assembly_ends) if assembly_ends else None
    stop_message: str | None = None
    _STOP_REASON = {"cancelled": "cancel_requested", "lease_lost": "lease_lost", "calls": "budget", "run_time": "budget",
                    "node_time": "runtime_budget"}
    _STOP_CLASS = {"cancel_requested": "cancelled", "lease_lost": "lease_lost", "runtime_budget": "budget", "budget": "budget"}
    # the step's runtime cap keeps the modeling reserve; a run time budget needs one modeling request's time limit (each
    # request is capped to what is left and stopped before it would start late); an agent tool call only stops at its
    # attempt's deadline
    reserve = {"node": MODELING_RESERVE_S, "run": RUN_BUDGET_RESERVE_S}.get(limits.deadline_kind, 0.0) if limits is not None else 0.0
    workers = max(1, min(int(concurrency or ai_limits.MODELING_CONCURRENCY), ai_limits.MODELING_CONCURRENCY_MAX))

    def settle() -> None:
        """J: a definition was built/reused (or its modeling ran): commit while this attempt holds the node."""
        nonlocal stop_reason, stop_message
        if checkpoint is not None and not checkpoint() and stop_reason in (None, "provider_failures"):
            stop_reason = "lease_lost"
            stop_message = None

    last_assembly_stop_check = float("-inf")

    def assembly_cancelled(*, force: bool = False) -> bool:
        """Cancellation/lease loss stops writes; a runtime limit still allows a bounded partial save."""
        nonlocal stop_reason, last_assembly_stop_check
        if stop_reason in ("cancel_requested", "lease_lost"):
            return True
        now = time.monotonic()
        if not force and now - last_assembly_stop_check < 1.0:
            return False  # solver residuals must not open a database session per evaluation
        last_assembly_stop_check = now
        if cancelled is not None and cancelled():
            stop_reason = "cancel_requested"
        elif keep_going is not None and not keep_going():
            stop_reason = "lease_lost"
        return stop_reason in ("cancel_requested", "lease_lost")

    def placement_checkpoint() -> bool:
        nonlocal stop_reason
        if checkpoint is not None and not checkpoint():
            stop_reason = "lease_lost"
            return False
        return True

    def stopping(started: Collection[str] = ()) -> bool:
        """J: whether no new definition may start — cancel, lease, the step's runtime cap, the run's call / time budget
        (one request set aside for each ``started`` unit that has not made its first one). Sets ``stop_reason``; it holds
        for the rest."""
        nonlocal stop_reason, stop_message
        if stop_reason is None and cancelled is not None and cancelled():
            stop_reason = "cancel_requested"
        if stop_reason is None and keep_going is not None and not keep_going():
            stop_reason = "lease_lost"
        if stop_reason is None and deadline is not None and time.monotonic() > deadline - MODELING_RESERVE_S:
            stop_reason = "runtime_budget"
        if stop_reason is None and limits is not None and (hit := limits.stop_reason(reserve_s=reserve, running=started)) is not None:
            stop_reason, stop_message = _STOP_REASON.get(hit.kind, "budget"), hit.message
        return stop_reason is not None

    def not_started(o: DefinitionOutcome) -> None:
        """The step stopped before this definition was started."""
        failed_before = next((r for r in progress.get(o.key, []) if r.scope == out.scope and r.status == "failed"), None)
        if failed_before is not None:  # its failure stays on record: never overwritten with "not started"
            o.status, o.source, o.error = "failed", failed_before.source, dict(failed_before.error or {})
            o.notes.append("not attempted again: the step stopped before this definition; its earlier failure stands")
            return
        o.status = "not_started"
        if stop_reason == "provider_failures":
            o.error = _failure_info("provider_deferred", stop_message or "new work was deferred after repeated exhausted service failures",
                                    stage="assembly_modeling", retryable=True, recovery="retry_definition",
                                    deferred_reason="repeated_exhausted_service_failures")
            record(o)
            return
        code = "budget_exhausted" if stop_reason == "budget" else stop_reason
        o.error = _failure_info(code, stop_message or "the step stopped before this definition was built", stage="assembly_modeling",
                                retryable=True, recovery="retry_definition", failure_class=_STOP_CLASS[stop_reason])
        record(o)

    # 1. In definition order, in this thread: reuse, the unsupported memory, blocked definitions and templates (each
    # template build checked against the stops first) are settled; every definition left for AI modeling gets its part
    # number now, so numbering never depends on which modeling finishes first.
    jobs: list[_DefinitionJob] = []
    for d in defs:
        o = by_key[d["key"]]
        try:
            references = text_cache.for_definition(d, content)
        except AppError as exc:
            # Validate before any reuse: a cached part cannot conceal missing or corrupt source context.
            o.status, o.source = "failed", "ai_modeling"
            o.error = {**_failure_info(exc.code, exc.message, stage="source_reference", retryable=False, recovery="fix_spec"),
                       "details": exc.details}
            record(o)
            continue
        fingerprints[d["key"]] = definition_fingerprint(content, d, o.quantity, known=known, drawing_source=drawing_source,
                                                         source_references=references)
        earlier = next(((r, hit) for r in progress.get(d["key"], []) if (hit := _still_built(db, r, project_id, policy)) is not None), None)
        # C (design §6.3): an earlier attempt's revision that left out what the kernel builds now is passed over too
        passed = reuse_blocked(db, earlier[1][1], spec_version_id=earlier[0].spec_version_id) if earlier is not None else None
        if earlier is not None:
            stored_fingerprint = (earlier[0].outcome or {}).get("fingerprint")
            if ((drawing_source or references is not None or stored_fingerprint)
                    and stored_fingerprint != fingerprints[d["key"]]):
                passed = ({"reason": "the original text or reference reader changed", "code": "text_reference_changed"}
                          if references is not None else
                          {"reason": "the original drawing or evidence pipeline changed", "code": "drawing_evidence_changed"}
                          if drawing_source else
                          {"reason": "the definition input changed", "code": "definition_input_changed"})
        if earlier is not None and passed is None:  # built by an earlier attempt: kept as it is, no new model call
            row, (part, rev) = earlier
            replay(o, row, part, rev)
            o.reused_from = (row.outcome or {}).get("reused_from")
            o.reuse_skipped = (row.outcome or {}).get("reuse_skipped")
            record(o)
            settle()  # publish kept parts before a remaining modeling job can wait, fail or be cancelled
            continue
        blocked = not turbo and (rd["definitions"].get(d["key"]) or {}).get("state") == "blocked"
        found = None if blocked or elsewhere is None else elsewhere.find(d["key"], fingerprints[d["key"]])
        if found is not None:  # the same definition request was built for another version / run: reused, no model call
            row, part, rev = found
            replay(o, row, part, rev)
            o.reused_from = {"spec_version_id": str(row.spec_version_id), "revision_id": str(rev.id)}
            record(o)
            settle()
            continue
        o.reuse_skipped = passed or (elsewhere.skipped.get(d["key"]) if elsewhere is not None else None)
        if o.reuse_skipped is not None:
            o.notes.append(f"not reused: {o.reuse_skipped['reason']}")
        # J (AC18): a deterministic "unsupported" for the same definition request is not modeled again in this scope —
        # checked before the stops below, so a stop never turns that failure into "not started"
        same = next((r for r in progress.get(d["key"], []) if r.status == "failed" and (r.error or {}).get("failure_class") == "unsupported"
                     and (r.outcome or {}).get("fingerprint") == fingerprints[d["key"]]), None)
        if same is not None:
            o.status, o.source, o.error = "failed", same.source, {**dict(same.error), "repeated": True}
            o.notes.append("unsupported before for the same definition request; not modeled again until its input changes")
            continue
        state = rd["definitions"].get(d["key"]) or {}
        if not turbo and state.get("state") == "blocked":
            o.status = "blocked"
            o.questions = [{"id": q, "subject": (questions.get(q) or {}).get("subject"), "question": (questions.get(q) or {}).get("question"),
                            "source": "spec"} for q in state.get("questions") or []]
            record(o)
            continue
        number_hint = d.get("number") or f"{base_number}-{d['key']}"
        if (d.get("template") or {}).get("id") and stopping():  # a template build is a unit too (the AI ones: when started)
            not_started(o)
            continue
        rev, tpl_error = _template_revision(db, project_id=project_id, user=user, d=d, content=content, number=number_hint, policy=policy,
                                            node_run_id=node_run_id)
        if rev is not None:
            part = db.get(Part, rev.part_id)
            o.status, o.source = "built", "template"
            o.part_id, o.part_number, o.revision_id, o.rev_no = str(rev.part_id), part.number if part else None, str(rev.id), rev.rev_no
            record(o)
            settle()
            continue
        if tpl_error is not None:
            o.notes.append(f"template {(d.get('template') or {}).get('id')} not used: {tpl_error['code']} — {tpl_error['message']}")
        if provider is None and provider_error is None:
            try:
                provider, cfg = ai_modeling.select_provider(db, model_ref)
            except ProviderError as exc:
                provider_error = _failure_info(exc.code, exc.message, stage="provider_select", retryable=bool(exc.retryable),
                                               recovery="retry_definition" if exc.retryable else "configure_model")
        if provider_error is not None:
            o.status, o.source, o.error = "failed", "ai_modeling", dict(provider_error)
            record(o)
            continue
        bundle = None
        if drawing_source:
            if drawing_cache is None:
                drawing_cache = DrawingEvidenceCache(db, v, vision=bool(provider.capabilities.vision))
            try:
                bundle = drawing_cache.for_definition(d, content)
            except (AppError, DrawingEvidenceError) as exc:
                o.status, o.source = "failed", "ai_modeling"
                o.error = _failure_info(exc.code, str(exc), stage="drawing_evidence", retryable=False, recovery="fix_spec")
                record(o)
                continue
        part_number = _free_part_number(db, project_id, number_hint, skip=[j.part.number for j in jobs])
        jobs.append(_DefinitionJob(seq=len(jobs), o=o, hint=number_hint,
                                   request=definition_request(content, d, o.quantity, version_label=label, known=known,
                                                              source_references=references),
                                   part=ai_modeling.PartBrief(number=part_number, name=(d.get("name") or number_hint)[:200],
                                                              material=_material_of(content, d)), drawing_bundle=bundle,
                                   source_contexts=definition_source_contexts(content, d), design_context=design_context.get(d["key"]),
                                   source_references=references))

    service_guard = (DefinitionServiceGuard(provider.provider_name, provider.model_id, provider.config_id)
                     if provider is not None else None)

    def finish(job: _DefinitionJob, res: Any) -> None:
        """A definition's modeling came back: its part and revision, progress row and call records, in this thread."""
        nonlocal stop_reason, stop_message
        o = job.o
        err = res.error or {}
        cls = err.get("failure_class")
        if res.status == "failed" and not res.calls and cls in STOP_CLASSES:
            # a limit stopped the modeling before its first request (another step of the run took the last call, the run
            # was cancelled meanwhile): this definition was not started, and no further one starts
            if stop_reason is None:
                stop_reason, stop_message = _CLASS_STOP.get(cls, "budget"), err.get("message")
            not_started(o)
            return
        o.source = "ai_modeling"
        o.simulated = bool((res.label or {}).get("simulated"))
        note = f"spec {label} definition {o.key}"
        if res.design is not None:  # built and checked: the part and its revision (no part without a revision)
            if job.design_context is not None:
                design_spec.resolve_spec_design(db, user, v, node_run_id=node_run_id)
            sp = db.begin_nested()
            part = Part(project_id=project_id, number=_free_part_number(db, project_id, job.hint, skip=[j.part.number for j in jobs if j is not job]),
                        name=job.part.name, material=job.part.material, owner_id=user.id, description=note)
            db.add(part)
            db.flush()
            res = ai_modeling.save(db, res, part=part, user=user, base=None, node_run_id=node_run_id, note=note,
                                  source_file_version_id=job.drawing_bundle.source_file_version_id if job.drawing_bundle else None,
                                  extra_checks=job.drawing_bundle.checks if job.drawing_bundle else None)
            if res.revision is None:
                sp.rollback()
            else:
                sp.commit()
                o.status, o.part_id, o.part_number, o.revision_id, o.rev_no = "built", str(part.id), part.number, str(res.revision.id), res.revision.rev_no
                o.assumptions, o.check = list(res.assumptions or []), res.summary
        if o.status != "built" and (stop := _modeling_failed(o, res, turbo)) is not None and stop_reason is None:
            stop_reason, stop_message = stop
        if service_guard is not None:
            service_guard.observe(o.key, status=o.status, error=res.error, calls=res.calls or [])
            if service_guard.opened and stop_reason is None:
                stop_reason = "provider_failures"
                stop_message = ("new modeling and AI placement were deferred after two independent definitions exhausted "
                                "service retries; completed work is kept")
        record(o)
        calls = list(res.calls or [])
        out.calls += calls
        if on_calls is not None and calls:
            on_calls(calls)
        settle()

    # 2. The AI modeling of the rest, up to ``workers`` definitions at a time. A worker thread runs only the model calls and
    # kernel builds of one definition (ai_modeling.design, in a copy of this thread's context: call limits, work unit,
    # checks ledger) and returns its result; the part, revision, progress row, call records and checkpoint are written
    # here as each one returns. The stops are checked before each definition starts; after a stop (or a crash) none
    # starts, the running ones finish and are kept, then the crash is raised.
    queue = deque(jobs)
    crash: BaseException | None = None
    if queue:
        with ThreadPoolExecutor(max_workers=min(workers, len(queue)), thread_name_prefix="definition-model") as pool:
            running: dict[Future[Any], _DefinitionJob] = {}
            while True:
                while queue and crash is None and len(running) < workers and not stopping([_definition_unit(j.o.key) for j in running.values()]):
                    job = queue.popleft()
                    running[pool.submit(contextvars.copy_context().run, _design_definition, provider, cfg, job, policy)] = job
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in sorted(done, key=lambda f: running[f].seq):
                    job = running.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001 - raised once the running definitions are kept
                        crash = crash or exc
                        continue
                    finish(job, res)
    if crash is not None:
        raise crash
    for job in queue:
        not_started(job.o)

    out.stop_reason = stop_reason
    missing = [o for o in out.definitions if o.status != "built"]
    under_root = {i["key"]: i for i in tree["instances"]}
    items = [_definition_missing(o, [k for k, i in under_root.items() if i.get("definition") == o.key]) for o in missing]
    # Generation supplement B-01: whatever was built makes a partial assembly — unless nothing was, or the step was
    # stopped from outside (cancelled; the lease went to another worker): then no assembly, as before.
    if missing and (not out.built or stop_reason in ("cancel_requested", "lease_lost")):
        out.failure = _definitions_failure(out, missing, stop_reason)
        out.coverage = coverage(content, built={o.key for o in out.built}, placed={}, missing=items, assembled=False)
        return out
    bindings = {o.key: {"kind": "part", "revision_id": o.revision_id} for o in out.built}
    continue_from = _partial_of_scope(db, node_run_id, v.id, tree["root"], scope=out.scope)
    placement: dict[str, Any] | None = None
    placement_solve_cache: dict[str, Any] = {}  # this build only; the solver reuses only identical validated inputs
    if turbo:
        # Generation supplement F (TB-03, B-02.4, D-01): one AI decision for the kept instances without a position;
        # what it cannot place is arranged for display by rule (never installed); it never fails the build.
        from .assembly_placement import decide as decide_placements

        def modeling_provider() -> Any:
            if service_guard is not None and service_guard.error is not None:
                err = service_guard.error
                message = ("AI placement was not started: model work was deferred after exhausted service retries. "
                           "Last observed modeling service failure: " + (err.get("message") or "the model service failed"))
                raise ProviderError(err["code"], message,
                                    retryable=True, failure_class=err["failure_class"], provider=service_guard.provider)
            if provider is not None:
                return provider
            if provider_error is not None:
                raise ProviderError(provider_error["code"], provider_error["message"], retryable=bool(provider_error.get("retryable")))
            return ai_modeling.select_provider(db, model_ref)[0]

        def placement_calls(calls: list[Any]) -> None:
            # Persist each returned batch before its nonlinear solve begins.
            # PlacementDecision.calls is a summary, not a second ledger write.
            out.calls.extend(calls)
            if on_calls is not None and calls:
                on_calls(calls)

        decision = decide_placements(db, spec_version=v, content=content, bindings=bindings, policy=policy, provider_of=modeling_provider,
                                     missing=items, continue_from=continue_from, deadline=deadline,
                                     on_calls=placement_calls, checkpoint=placement_checkpoint, cancelled=assembly_cancelled,
                                     solve_cache=placement_solve_cache)
        if decision.pending:
            out.placement, placement = decision.brief(), decision.applied()  # round 3 (B): AI mates, solved by the build
            items += decision.missing  # generation reliability P: instances beyond the positions the sources give
        out.notes += decision.notes
    if assembly_cancelled(force=True):
        out.stop_reason = stop_reason
        code = "cancelled" if stop_reason == "cancel_requested" else "lease_lost"
        out.failure = _failure_info(code, "assembly creation stopped; finished parts and recorded placement calls are retained",
                                    stage="assembly", retryable=True, recovery="retry_definition")
        out.coverage = coverage(content, built={o.key for o in out.built}, placed={}, missing=items, assembled=False)
        return out
    sp = db.begin_nested()
    try:
        from ..cad import mates

        with mates.solve_budget(deadline=assembly_deadline - ASSEMBLY_WRITE_RESERVE_S if assembly_deadline is not None else None,
                                cancelled=assembly_cancelled, cache=placement_solve_cache):
            asm, rev, created = asm_svc.create_from_spec_version(db, project_id=project_id, user=user, spec_version_id=v.id, bindings=bindings,
                                                                 policy=policy, number=base_number, node_run_id=node_run_id, source="ai",
                                                                 partial=True, missing=items, continue_from=continue_from,
                                                                 placement=placement, build_notes=out.notes)
        if assembly_cancelled(force=True):
            sp.rollback()
            out.stop_reason = stop_reason
            code = "cancelled" if stop_reason == "cancel_requested" else "lease_lost"
            out.failure = _failure_info(code, "assembly creation interrupted; finished parts and recorded placement calls are retained",
                                        stage="assembly", retryable=True, recovery="retry_definition")
            out.coverage = coverage(content, built={o.key for o in out.built}, placed={}, missing=items, assembled=False)
            return out
        sp.commit()
    except AppError as exc:
        sp.rollback()
        if exc.code == "nothing_placeable":  # nothing can be placed: no assembly (the built parts are kept and delivered)
            out.coverage = (exc.details or {}).get("coverage") or coverage(content, built={o.key for o in out.built}, placed={},
                                                                             missing=items, assembled=False)
            out.failure = _definitions_failure(out, missing, stop_reason) if missing else _unplaceable_failure(out)
            return out
        out.failure = {**_failure_info(exc.code, exc.message, stage="assembly", retryable=exc.status >= 500, recovery="fix_spec",
                                       recovery_message=BUILD_RECOVERY_TEXT["fix_spec"]),
                       "details": {**(exc.details or {}), "built": len(out.built), "recovery": "fix_spec"}}
        out.coverage = coverage(content, built={o.key for o in out.built}, placed={}, missing=items, assembled=False)
        return out
    out.assembly, out.revision = asm, rev
    from .spec_coverage import project_modeling_diagnostics

    frozen_coverage = created.get("_coverage")
    out.coverage = project_modeling_diagnostics(frozen_coverage, items) if isinstance(frozen_coverage, dict) else frozen_coverage
    out.reused_revision = bool(created.get("_reused"))
    if out.scope and not out.reused_revision:  # generation supplement I: the build scope that made it (continued by scope)
        rev.spec_ref = {**(rev.spec_ref or {}), "build_scope": out.scope}
    physical = next((note.get("report") for note in reversed(out.notes)
                     if note.get("code") == "placement_physical_review"), None)
    if isinstance(physical, dict) and not out.reused_revision:
        from .verification import refresh_assembly_verification
        from .assembly_placement_checks import bind_saved_report

        # These are actual generation measurements, separate from policy-skipped business checks.
        # Their pass establishes only the reported material scope; source assumptions remain unconfirmed.
        physical = bind_saved_report(db, rev, physical)
        record = asm_svc._record({**physical, "check": "assembly.placement_geometry", "scope": "generation_feedback",
                                  "acceptance": "not_evaluated"}, user=user, policy=policy)
        rev.checks = asm_svc._merge_checks(rev.checks or {}, {"assembly.placement_geometry": record})
        rev.check_status = asm_svc._check_status(rev.checks, asm_svc._required(rev, rev.checks))
        refresh_assembly_verification(db, rev)
    from ..cad.assembly_checks import INTERFERENCE_BUDGET_S

    check_reserve = INTERFERENCE_BUDGET_S + ASSEMBLY_WRITE_RESERVE_S
    check_budget = assembly_deadline is None or time.monotonic() < assembly_deadline - check_reserve
    if run_checks and not out.reused_revision and not check_budget:
        note = {"code": "assembly_checks_deferred", "instances": [],
                "message": "the runtime budget left no room for additional geometry checks; the saved assembly remains unverified"}
        out.notes.append(note)
        rev.notes = [*(rev.notes or []), note]
    # The valid revision (complete or partial) must survive a later slow check.
    # Never commit inside its savepoint or duplicate already-recorded AI calls.
    if not placement_checkpoint():
        out.stop_reason = stop_reason
        out.failure = _failure_info("lease_lost", "the assembly was prepared but the node lease was lost before its checkpoint",
                                    stage="assembly", retryable=True, recovery="retry_definition")
        return out
    if assembly_cancelled(force=True):
        out.stop_reason = stop_reason
        code = "cancelled" if stop_reason == "cancel_requested" else "lease_lost"
        out.failure = _failure_info(code, "the assembly revision was saved; additional checks stopped with this attempt",
                                    stage="assembly_checks", retryable=True, recovery="run_checks")
        return out
    if run_checks and not out.reused_revision and check_budget:
        checks_sp = db.begin_nested()
        try:
            asm_svc.run_checks(db, rev.id, user, policy=policy)
            checks_sp.commit()
        except AppError as exc:
            checks_sp.rollback()
            out.failure = {**_failure_info(exc.code, exc.message, stage="assembly_checks", retryable=exc.status >= 500,
                                           recovery="run_checks"), "details": {"assembly_revision_id": str(rev.id)}}
            return out
    if out.coverage is not None and out.coverage.get("status") != "complete":
        out.failure = _partial_failure(out, missing)
    elif run_checks and not out.reused_revision and not check_budget and not turbo:
        out.failure = {**_failure_info("assembly_checks_deferred", "the assembly revision was saved without additional geometry checks",
                                       stage="assembly_checks", retryable=True, recovery="run_checks"),
                       "details": {"assembly_revision_id": str(rev.id)}}
    elif isinstance(physical, dict) and physical.get("status") != "passed":
        incomplete = physical.get("status") != "failed"
        out.failure = {**_failure_info("assembly_physical_check_incomplete" if incomplete else "assembly_physical_conflicts",
                                      "The assembly was saved, but its actual material examination is incomplete" if incomplete else
                                      "The assembly was saved with unresolved material intersections; automatic placement is not physical acceptance",
                                      stage="assembly_checks", retryable=False, recovery="fix_spec",
                                      recovery_message="Review the recorded source, geometry or runtime issue and start a new assembly build. "
                                                       "Business check reruns do not replace this generation feedback."),
                       "details": {"assembly_revision_id": str(rev.id), "physical_status": physical.get("status"),
                                   "interference_count": physical.get("interference_count"),
                                   "complete": physical.get("complete", False), "fingerprint": physical.get("fingerprint")}}
    return out


def _definition_missing(o: DefinitionOutcome, instances: list[str]) -> dict[str, Any]:
    """The coverage item of a part definition without geometry (stage ``modeling``)."""
    from .spec_coverage import missing_item

    err = o.error or {}
    if o.status in ("blocked", "needs_clarification"):
        qs = [q.get("id") or q.get("question") for q in o.questions if q.get("id") or q.get("question")]
        code = "spec_questions_open" if o.status == "blocked" else "modeling_questions"
        reason = ("open spec questions: " if o.status == "blocked" else "the modeling step asked: ") + "; ".join(str(q) for q in qs[:4])
        recovery = "answer_questions"
    elif o.status == "not_started":
        code, reason, recovery = str(err.get("code") or "not_started"), "the step stopped before this definition was built", "retry_definition"
    else:
        code = str(err.get("code") or "ai_failed")
        reason = str(err.get("message") or "the model could not produce a buildable design")
        recovery = str(err.get("recovery") or "retry_definition")
    return missing_item(stage="modeling", key=o.key, name=o.name, kind=o.kind, code=code, reason=reason, quantity=o.quantity,
                        instances=instances, recovery=recovery, **({"protocol_error": True} if err.get("protocol_error") else {}))


def _definitions_failure(out: AssemblyBuild, missing: list[DefinitionOutcome], stop_reason: str | None) -> dict[str, Any]:
    """No assembly because definitions are missing (nothing built, or the step was stopped): the failure as before."""
    waiting = [o for o in missing if o.status in ("blocked", "needs_clarification")]
    stopped_only = all(o.status == "not_started" for o in missing)
    code = "spec_questions_open" if waiting and len(waiting) == len(missing) else (
        "cancelled" if stop_reason == "cancel_requested" and stopped_only else
        "node_runtime_budget" if stop_reason == "runtime_budget" and stopped_only else
        "budget_exhausted" if stop_reason == "budget" and stopped_only else "definitions_failed")
    recovery = "answer_questions" if code == "spec_questions_open" else _redo_recovery(missing)
    message = f"{len(missing)} of {len(out.definitions)} part definitions were not built; no assembly was made"
    failure = {
        **_failure_info(code, message, stage="assembly_modeling", retryable=recovery == "retry_definition", recovery=recovery,
                        recovery_message=("the run was cancelled before these definitions were built; continue to build them"
                                          if code == "cancelled" else BUILD_RECOVERY_TEXT[recovery])),
        "details": {"definitions": [o.out() for o in missing], "built": len(out.built),
                    "reused": sum(1 for o in out.definitions if o.reused), "questions": [q for o in waiting for q in o.questions],
                    "recovery": recovery},
    }
    from .failure_causes import public_error

    return public_error(failure, causes=_placement_failure_causes(out)) or failure


def _redo_recovery(missing: list[DefinitionOutcome]) -> str:
    """Continuing rebuilds only what is missing; it cannot help while every definition left waits for a model
    configuration the frozen run snapshot does not have, nor when a definition hit a Turbo protocol error (it would ask
    again)."""
    redo = [o for o in missing if o.status in ("failed", "not_started")]
    if redo and all(o.status == "failed" and (o.error or {}).get("recovery") == "configure_model" for o in redo):
        return "configure_model"
    if redo and all(o.status == "failed" and (o.error or {}).get("recovery") == "fix_spec" for o in redo):
        return "fix_spec"  # J: deterministic (unsupported / bad request) — continuing would fail the same way
    if any(o.status == "failed" and (o.error or {}).get("protocol_error") for o in missing):
        return pol.PROTOCOL_RECOVERY
    return "retry_definition"


def _placement_failure_causes(out: AssemblyBuild) -> list[dict[str, Any]]:
    """Only unresolved notes carry errors: placement removes them after a successful refinement."""
    from .failure_causes import cause

    notes = [*out.notes, *((out.revision.notes or []) if out.revision is not None else [])]
    return [c for n in notes if isinstance(n.get("error"), dict)
            if (c := cause(n["error"], stage="placement", instances=n.get("instances"))) is not None]


def _partial_failure(out: AssemblyBuild, missing: list[DefinitionOutcome]) -> dict[str, Any]:
    """``assembly_partial`` (design §2.2 / §2.4): a partial assembly revision exists; the node fails with its products.
    Recovery: rebuild the failed / not started definitions when there are any, else answer the open questions or fix
    the spec (placements, relations, structure)."""
    cov = out.coverage or {}
    rev = out.revision
    redo = [o for o in missing if o.status in ("failed", "not_started")]
    waiting = [o for o in missing if o.status in ("blocked", "needs_clarification")]
    placement_causes = _placement_failure_causes(out)
    if redo:
        recovery = _redo_recovery(missing)
    elif waiting or any(m.get("recovery") == "answer_questions" for m in cov.get("missing") or []):
        recovery = "answer_questions"
    elif any(n.get("code") in ("solver_runtime_budget", "placement_runtime_budget", "placement_call_budget")
             for n in [*out.notes, *((rev.notes or []) if rev is not None else [])]):
        recovery = "retry_definition"  # continue this build's remaining placement, retaining its completed parts
    elif placement_causes and all(c.get("recovery") == "configure_model" for c in placement_causes):
        recovery = "configure_model"
    elif any(c.get("retryable") and c.get("recovery") == "retry_definition" for c in placement_causes):
        recovery = "retry_definition"
    else:
        recovery = "fix_spec"
    target, placed = (cov.get("target") or {}), (cov.get("placed") or {})
    by_ai = (cov.get("decided") or {}).get("instances")
    local = (cov.get("local_only") or {}).get("instances")  # generation reliability §2.6
    local_causes: list[str] = []
    missing_codes = {m.get("code") for m in cov.get("missing") or []}
    if "floating_group" in missing_codes:
        local_causes.append("floating groups are not anchored to their parent frame")
    if "subassembly_unplaced" in missing_codes:
        local_causes.append("a sub-assembly has no position")
    local_message = (f", {local} placed only locally" + (f" ({'; '.join(local_causes)})" if local_causes else "")) if local else ""
    message = (f"partial assembly A{rev.rev_no if rev else '?'}: {placed.get('instances', 0)} of {target.get('instances', 0)} part instances "
               f"installed ({(cov.get('built') or {}).get('definitions', 0)} of {target.get('definitions', 0)} definitions with geometry"
               + (f", {by_ai} placed by AI decision" if by_ai else "")
               + local_message
               + (f", {(cov.get('arranged') or {}).get('instances')} only arranged for display" if (cov.get('arranged') or {}).get('instances') else "")
               + f"); {len(cov.get('missing') or [])} missing item(s)")
    failure = {
        **_failure_info("assembly_partial", message, stage="assembly", retryable=recovery == "retry_definition", recovery=recovery,
                        recovery_message=BUILD_RECOVERY_TEXT[recovery]),
        "details": {"assembly_revision_id": str(rev.id) if rev else None, "rev_no": rev.rev_no if rev else None,
                    "coverage_status": cov.get("status"), "missing": len(cov.get("missing") or []),
                    "definitions": [o.out() for o in missing], "built": len(out.built), "reused": sum(1 for o in out.definitions if o.reused),
                    "questions": [q for o in waiting for q in o.questions], "recovery": recovery},
    }
    from .failure_causes import public_error

    return public_error(failure, causes=placement_causes) or failure


def _unplaceable_failure(out: AssemblyBuild) -> dict[str, Any]:
    """Every definition was built but no instance can be placed (standard: no placement or relation is usable)."""
    cov = out.coverage or {}
    recovery = "answer_questions" if any(m.get("recovery") == "answer_questions" for m in cov.get("missing") or []) else "fix_spec"
    return {**_failure_info("nothing_placeable", "no instance can be placed: the spec states no usable placement or relation; the built "
                            "parts are kept and delivered on their own", stage="assembly", retryable=False, recovery=recovery,
                            recovery_message=BUILD_RECOVERY_TEXT[recovery]),
            "details": {"built": len(out.built), "missing": len(cov.get("missing") or []), "recovery": recovery}}


def _partial_of_scope(db: Session, node_run_id: uuid.UUID | None, spec_version_id: uuid.UUID, root: str,
                      scope: str | None = None) -> AssemblyRevision | None:
    """The latest partial root revision an earlier attempt of this node run built from this spec version (S3 continue:
    the completed build becomes a new revision of the same assembly). A build scope of its own (generation supplement
    I, E2E-02: the agent tools' ``node_task:<id>:rework:<n>``) continues what that scope built (``spec_ref.build_scope``)
    instead, so another rework round of the same node run starts its own assembly."""
    q = select(AssemblyRevision).where(AssemblyRevision.spec_version_id == spec_version_id)
    if scope is not None and not scope.startswith("node_run:"):
        q = q.where(AssemblyRevision.spec_ref["build_scope"].astext == scope)
    elif node_run_id is not None:
        q = q.where(AssemblyRevision.node_run_id == node_run_id)
    else:
        return None
    rows = db.scalars(q.order_by(AssemblyRevision.created_at.desc(), AssemblyRevision.rev_no.desc())).all()
    return next((r for r in rows if (r.spec_ref or {}).get("definition") == root
                 and ((r.spec_ref or {}).get("coverage") or {}).get("status") == "partial"), None)


# =====================================================================================================
# Drawings (W5, guarded)
# =====================================================================================================


#: Drawing failures that another attempt can fix (a budget or a converter that ran out of time); a refused revision or
#: configuration (other 4xx) fails the same way again, so "continue" is not offered for it.
RETRYABLE_DRAWING_CODES = ("drawing_budget_exceeded", "dwg_timeout", "dwg_conversion_failed", "drawing_generation_failed")


def drawing_retryable(exc: AppError) -> bool:
    return exc.status >= 500 or exc.code in RETRYABLE_DRAWING_CODES


def drawing_generator() -> Callable[..., Any] | None:
    """W5 ``services.assembly_drawings.generate`` when this build has it."""
    try:
        from . import assembly_drawings  # type: ignore[attr-defined]
    except ImportError:
        return None
    return getattr(assembly_drawings, "generate", None)


def make_drawings(db: Session, *, assembly_revision_id: uuid.UUID, user: User, policy: str, config: dict[str, Any] | None = None,
                  node_run_id: uuid.UUID | None = None, **extra: Any) -> Any:
    """The W5 drawing package of the exact assembly revision. 501 ``capability_unavailable`` without W5 (no fake output)."""
    gen = drawing_generator()
    if gen is None:
        raise AppError(501, "capability_unavailable", "assembly drawing packages are not available in this build "
                       "(services.assembly_drawings.generate, W5)", {"capability": "assembly_drawing"})
    return gen(db, assembly_revision_id=assembly_revision_id, user=user, config=dict(config or {}), policy=policy, node_run_id=node_run_id,
               **extra)


#: Generation supplement E: the layered drawing step draws a detail sheet for every part definition whatever its
#: procurement (a standard / buy part is drawn from its model — possibly simplified, marked by its representation).
ALL_PROCUREMENTS = ["make", "buy", "standard", "unknown"]
#: Configuration keys that describe the root's own drawing; a sub-assembly package never takes them.
_ROOT_ONLY_KEYS = ("details", "detail_procurements", "detail", "key_dimensions", "title")


def level_configs(config: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(root, sub-assembly) package configurations of the layered drawing step (generation supplement E).

    Rule (no detail sheet drawn twice): the root package carries the detail sheet of every part definition of the
    whole tree (``detail_procurements`` = all four kinds unless the configuration names them); a sub-assembly package
    is its own GA + BOM only (``details`` false). Settings that belong to the root's own sheet stay with the root:
    ``title.drawing_number`` (a sub-assembly takes its number, name and revision from its own assembly), the
    ``key_dimensions`` between root instance paths and an explicit section plane (a sub-assembly uses ``auto``)."""
    root = dict(config or {})
    root.setdefault("detail_procurements", list(ALL_PROCUREMENTS))
    sub = {k: v for k, v in root.items() if k not in _ROOT_ONLY_KEYS}
    if isinstance(sub.get("section"), dict):
        sub["section"] = "auto"
    title = {k: v for k, v in (root.get("title") or {}).items() if k != "drawing_number"}
    if title:
        sub["title"] = title
    sub["details"] = False
    return root, sub


#: Generation reliability R (design §5.3): a drawing package is not started with less than this left of the step's
#: runtime or the run's time budget (process start-up, a GA sheet and its DWG conversion).
DRAWING_PACKAGE_RESERVE_S = 60.0
#: R: the codes of a package that was not started (``completeness: not_started``) — the ones the build gives a definition
#: it did not start (budget, the step's runtime cap, cancel, lost lease). The delivery lists its DWG as missing.
DRAWING_STOP_CODES = ("budget_exhausted", "runtime_budget", "cancel_requested", "lease_lost")
_DRAWING_STOP = {"cancelled": "cancel_requested", "lease_lost": "lease_lost", "calls": "budget", "run_time": "budget",
                 "node_time": "runtime_budget"}
_DRAWING_STOP_CLASS = {"cancel_requested": "cancelled", "lease_lost": "lease_lost", "runtime_budget": "budget", "budget": "budget"}


@dataclass
class LevelDrawings:
    """What :func:`make_level_drawings` did. ``root``: the root package (None when it was not made); ``root_error``: why
    (also when it was not started); ``subs``: one entry per sub-assembly revision; ``root_unit``: the root's entry in the
    same shape as a sub-assembly's; ``stopped_by``: why no further package was started (``budget`` — the run's time
    budget —, ``runtime_budget`` — the step's runtime cap —, ``cancel_requested``, ``lease_lost``), None when every package
    was tried. Unpacks as ``(root, root_error, subs)``."""

    root: Any
    root_error: dict[str, Any] | None
    subs: list[dict[str, Any]]
    root_unit: dict[str, Any]
    stopped_by: str | None = None

    def __iter__(self):  # noqa: ANN204 - (root, root_error, subs), as before R (the agent tool unpacks three)
        return iter((self.root, self.root_error, self.subs))

    def units(self) -> dict[str, Any]:
        """R (design §2.2 / §5.3, the shape of ``AssemblyBuild.units``): per package (``<number> A<rev>``) what was kept
        (made or reused; ``reused``: from an earlier attempt of this node run, not drawn again; ``partial``: kept with a
        failed sheet or format), never started, failed (code, failure class), why the step stopped and the continue action
        (``raise_budget``: the run's time budget is used up — retry the step with a larger one; ``retry_sheet``: retrying
        draws only what is missing; ``fix_spec``: a refused revision / configuration fails the same way again)."""
        every = [self.root_unit, *self.subs]
        failed = [u for u in every if u.get("completeness") == "failed"]
        partial = [u["key"] for u in every if u.get("package_id") and u.get("completeness") != "complete"]
        if self.stopped_by == "budget":
            action: str | None = "raise_budget"
        elif self.stopped_by or partial or any((u.get("error") or {}).get("retryable", True) for u in failed):
            action = "retry_sheet"
        else:
            action = "fix_spec" if failed else None
        return {"kept": [u["key"] for u in every if u.get("package_id")], "reused": [u["key"] for u in every if u.get("reused")],
                "partial": partial, "not_started": [u["key"] for u in every if u.get("completeness") == "not_started"],
                "failed": [{"key": u["key"], "code": (u.get("error") or {}).get("code"),
                            "failure_class": (u.get("error") or {}).get("failure_class")} for u in failed],
                "stopped_by": self.stopped_by, "continue": action}


def _complete_package(db: Session, assembly_revision_id: Any, user: User, policy: str, config: dict[str, Any],
                      node_run_id: uuid.UUID | None) -> Any:
    """R: the complete package an earlier attempt of this node run stored for this revision and configuration (W5
    ``complete_package``), None when there is none or this build cannot look it up."""
    if node_run_id is None or drawing_generator() is None:
        return None
    from . import assembly_drawings  # type: ignore[attr-defined]

    lookup = getattr(assembly_drawings, "complete_package", None)
    if lookup is None:
        return None
    return lookup(db, assembly_revision_id=assembly_revision_id, user=user, config=dict(config or {}), policy=policy, node_run_id=node_run_id)


def make_level_drawings(db: Session, *, assembly_revision_id: uuid.UUID, user: User, policy: str, config: dict[str, Any] | None = None,
                        node_run_id: uuid.UUID | None = None, checkpoint: Callable[[], bool] | None = None) -> LevelDrawings:
    """Generation supplement E (BHD1 §8.7): the drawing packages of every level of an assembly revision — the root
    package (GA + the detail sheet of every part definition) and one package per distinct sub-assembly revision of the
    tree (its own GA, BOM and title block: number, name, revision), each through ``assembly_drawings.generate`` with its
    own manifest. Repeated instances of one sub-assembly revision share one package.

    Each package is made in its own savepoint: one that cannot be made is listed with its error and the root and the
    others are kept. Returns a :class:`LevelDrawings` that unpacks as (root package or None, root error or None, one
    entry per sub-assembly revision: ``{key, assembly_id, assembly_revision_id, number, name, rev_no, depth, instances,
    package_id, completeness, reused[, error]}``; a failed one has ``package_id`` None and completeness ``failed``).
    501 ``capability_unavailable`` (no W5) is raised: nothing can be drawn.

    Generation reliability R (design §5.3, WP07 / AC27) — each package is a work unit:

    - Reuse first: a complete package an earlier attempt of this node run stored for the same assembly revision and the
      same configuration (policy, generator) is taken as it is (``reused``; never drawn again, never stopped), and a
      partial one is continued (S3, only its failed sheets).
    - Before a package is started, the running step's limits (``retry.current_limits()``: the run's cancel flag, this
      attempt's lease, the earlier of the step's runtime cap and the run's time budget, :data:`DRAWING_PACKAGE_RESERVE_S`
      needed) are checked; once one stops new work, this and every later package is ``not_started`` (``error.code`` one
      of :data:`DRAWING_STOP_CODES`, retryable) and the ones made are kept.
    - After each package made, ``checkpoint`` (the workflow engine's ``ctx.checkpoint``) commits it — the package row,
      its files, its asset version — while the attempt still holds the node, so a dead worker or a lost lease keeps it
      and the next attempt reuses it; False stops new packages (``lease_lost``).
    - :meth:`LevelDrawings.units` reports kept / reused / not started / failed and the continue action."""
    from ..ai import retry as call_retry
    from . import assemblies as asm_svc

    root_cfg, sub_cfg = level_configs(config)
    if drawing_generator() is None:  # no W5: nothing can be drawn (raises capability_unavailable)
        make_drawings(db, assembly_revision_id=assembly_revision_id, user=user, policy=policy, config=root_cfg, node_run_id=node_run_id)
    limits = call_retry.current_limits()  # None outside the workflow engine (agent tools: nothing is checked here)
    rev = db.get(AssemblyRevision, assembly_revision_id)
    asm = db.get(Assembly, rev.assembly_id) if rev is not None else None
    out = LevelDrawings(root=None, root_error=None, subs=[], root_unit={
        "key": f"{(asm.number or asm.name) if asm else assembly_revision_id} A{rev.rev_no if rev else '?'}",
        "assembly_revision_id": str(assembly_revision_id), "package_id": None, "completeness": "failed", "reused": False})
    stop_message: str | None = None

    def stopped() -> dict[str, Any] | None:
        """The error of a package that is not started (cancel, lease, time), else None."""
        nonlocal stop_message
        if out.stopped_by is None and limits is not None and (
                hit := limits.stop_reason(reserve_s=DRAWING_PACKAGE_RESERVE_S, calls=False)) is not None:
            out.stopped_by, stop_message = _DRAWING_STOP.get(hit.kind, "budget"), hit.message
        if out.stopped_by is None:
            return None
        return {"code": "budget_exhausted" if out.stopped_by == "budget" else out.stopped_by,
                "message": stop_message or "the step stopped before this drawing package was started", "retryable": True,
                "failure_class": _DRAWING_STOP_CLASS[out.stopped_by]}

    def draw(revision_id: Any, cfg: dict[str, Any], unit: dict[str, Any]) -> Any:
        """One package into ``unit`` (reused, else drawn unless stopped); raises the drawing error."""
        nonlocal stop_message
        done = _complete_package(db, revision_id, user, policy, cfg, node_run_id)
        if done is None:
            halt = stopped()
            if halt is not None:
                unit.update(completeness="not_started", error=halt)
                return None
            with db.begin_nested():
                done = make_drawings(db, assembly_revision_id=revision_id, user=user, policy=policy, config=cfg, node_run_id=node_run_id,
                                     reuse_complete=True)
            if checkpoint is not None and not checkpoint() and out.stopped_by is None:
                out.stopped_by = "lease_lost"
                stop_message = "this attempt no longer holds the step (its lease went to another worker); no further package was started"
        else:
            unit["reused"] = True
        b = drawing_brief(done)
        unit.update(package_id=b["package_id"], completeness=b["completeness"], manifest_sha256=b["manifest_sha256"], pages=b["pages"],
                    files=b["files"])
        return done

    try:
        out.root = draw(assembly_revision_id, root_cfg, out.root_unit)
    except AppError as exc:
        if exc.code == "capability_unavailable":
            raise
        out.root_error = {"code": exc.code, "message": exc.message, "details": exc.details or {}, "retryable": drawing_retryable(exc)}
        out.root_unit["error"] = {k: out.root_error[k] for k in ("code", "message", "retryable")}
    if out.root is None and out.root_error is None:  # not started
        out.root_error = {**out.root_unit["error"], "details": {"stopped_by": out.stopped_by}}
    for s in asm_svc.subassembly_revisions(db, assembly_revision_id):
        entry: dict[str, Any] = {"key": f"{s['number'] or s['name']} A{s['rev_no']}",
                                 **{k: s[k] for k in ("assembly_id", "assembly_revision_id", "number", "name", "rev_no", "depth", "instances")}}
        entry.update(package_id=None, completeness="failed", reused=False)
        try:
            draw(uuid.UUID(s["assembly_revision_id"]), sub_cfg, entry)
        except AppError as exc:
            entry["error"] = {"code": exc.code, "message": exc.message, "retryable": drawing_retryable(exc)}
        except Exception as exc:  # noqa: BLE001 - one sub-assembly never loses the root package or the others
            entry["error"] = {"code": "drawing_generation_failed", "message": f"{type(exc).__name__}: {str(exc)[:300]}", "retryable": True}
        out.subs.append(entry)
    return out


def load_drawing_package(db: Session, package_id: uuid.UUID | str) -> Any:
    """The W5 package row by id (W5's ``get_package`` when it has one, else its ORM class), None when this build cannot
    read it. W8 never fabricates a package."""
    try:
        pid = uuid.UUID(str(package_id))
    except ValueError:
        return None
    try:
        from . import assembly_drawings  # type: ignore[attr-defined]
    except ImportError:
        assembly_drawings = None  # type: ignore[assignment]
    getter = getattr(assembly_drawings, "get_package", None) if assembly_drawings is not None else None
    if getter is not None:
        try:
            return getter(db, pid)
        except AppError:
            return None
    from .. import models

    for name in ("AssemblyDrawingPackage", "AssemblyDrawing"):
        cls = getattr(models, name, None)
        if cls is not None:
            return db.get(cls, pid)
    return None


def _package_entries(db: Session, pkg: Any) -> list[dict[str, Any]] | dict[str, Any]:
    """Every file of a W5 package (pages, check reports and its canonical manifest.json), each verified against its
    manifest hash by W5 (``package_entries``); without that helper the files are read from the manifest and verified
    here. A refusal comes back as {code, message, details}."""
    try:
        from . import assembly_drawings  # type: ignore[attr-defined]
    except ImportError:
        assembly_drawings = None  # type: ignore[assignment]
    helper = getattr(assembly_drawings, "package_entries", None) if assembly_drawings is not None else None
    if helper is not None:
        try:
            return [{"path": e.path, "sha256": e.sha256, "data": e.data, "kind": e.kind, "page": e.page, "format": e.format}
                    for e in helper(db, pkg)]
        except AppError as exc:
            return {"code": exc.code, "message": exc.message, "details": exc.details or {}}
    m = _pkg_manifest(pkg)
    out: list[dict[str, Any]] = []
    listed = [(f, p.get("index"), "page") for p in m.get("pages") or [] if isinstance(p, dict) for f in p.get("files") or [] if isinstance(f, dict)]
    listed += [(r, None, "report") for r in m.get("reports") or [] if isinstance(r, dict)]
    for f, page, kind in listed:
        sha = str(f.get("sha256") or "")
        try:
            data = read_blob(db, sha)[0]
        except (FileNotFoundError, OSError):
            return {"code": "package_file_missing", "message": f"{f.get('path')} is missing from storage", "details": {"sha256": sha}}
        if _sha(data) != sha:
            return {"code": "package_file_mismatch", "message": f"{f.get('path')} does not match its manifest hash", "details": {"sha256": sha}}
        out.append({"path": str(f.get("path")), "sha256": sha, "data": data, "kind": kind, "page": page, "format": f.get("format")})
    return out


def _pkg_manifest(pkg: Any) -> dict[str, Any]:
    m = getattr(pkg, "manifest", None)
    return m if isinstance(m, dict) else {}


def drawing_brief(pkg: Any) -> dict[str, Any]:
    """The ``drawing_package`` port value of a W5 package row (read defensively: W5 owns the row)."""
    m = _pkg_manifest(pkg)
    pages = m.get("pages") or []
    completeness = getattr(pkg, "completeness", None) or m.get("completeness") or "unknown"
    out = {"type": "drawing_package", "package_id": str(getattr(pkg, "id", "")), "completeness": completeness,
           "manifest_sha256": getattr(pkg, "manifest_sha256", None), "pages": len(pages),
           "files": sum(len(p.get("files") or []) for p in pages if isinstance(p, dict)),
           "execution_policy": m.get("execution_policy"), "verification": m.get("verification") or {}}
    if isinstance(m.get("coverage"), dict):  # drawn from a partial assembly revision (generation supplement B-03)
        out["coverage"] = m["coverage"]
    return out


# =====================================================================================================
# Delivery manifest
# =====================================================================================================


def _bom_csv(lines: list[dict[str, Any]], hierarchical: bool) -> bytes:
    cols = (["item", "level", "def_kind"] if hierarchical else ["item"]) + ["number", "name", "material", "procurement", "representation",
                                                                             "quantity", "total_quantity", "rev_no", "def_key"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for line in lines:
        w.writerow(["" if line.get(c) is None else line.get(c) for c in cols])
    return buf.getvalue().encode("utf-8-sig")


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in (name or ""))
    return out.strip("._")[:80] or "part"


@dataclass
class _Pack:
    files: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, bytes] = field(default_factory=dict)
    missing: list[dict[str, Any]] = field(default_factory=list)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)  # generation reliability M: STEP result per object key
    # review of M: re-exports of this delivery by (source sha256, protocol): the bytes, or why none could be written
    reexports: dict[tuple[str, str], bytes | str] = field(default_factory=dict)

    def add(self, path: str, data: bytes, kind: str, item: str, source: dict[str, Any], **extra: Any) -> None:
        if path in self.data:
            return
        self.data[path] = data
        self.files.append({"path": path, "sha256": _sha(data), "bytes": len(data), "kind": kind, "item": item, "source": source, **extra})

    def lack(self, item: str, code: str, message: str, **extra: Any) -> None:
        self.missing.append({"item": item, "code": code, "message": message, **extra})


@dataclass(frozen=True)
class ManufacturingReport:
    """Internal bytes built from validated saved rows, never an API-provided attachment."""

    item: str
    path: str
    data: bytes
    source: dict[str, Any]


@dataclass(frozen=True)
class ManufacturingReports:
    files: tuple[ManufacturingReport, ...]
    sources: dict[str, Any]
    summary: dict[str, Any]
    missing: tuple[dict[str, Any], ...] = ()


def _manufacturing_reports(pack: _Pack, supplement: ManufacturingReports) -> None:
    """A report group needs its real CSV, HTML and source JSON; missing groups remain missing."""
    seen: set[str] = set()
    size = 0
    for report in supplement.files:
        allowed = {f"manufacturing/{report.item}.{ext}" for ext in ("csv", "html", "json")}
        size += len(report.data)
        if (report.item not in ("process", "quote") or report.path not in allowed or report.path in seen
                or not report.data or len(report.data) > 16 * 1024 * 1024 or size > 64 * 1024 * 1024):
            raise AppError(422, "invalid_manufacturing_report", "manufacturing reports must be bounded, distinct CSV, HTML and JSON files")
        seen.add(report.path)
        pack.add(report.path, report.data, "manufacturing_report", report.item, report.source,
                 estimated=True, pricing_basis="synthetic_demo_rates", commercial_status="draft")
    for item in ("process", "quote"):
        if not {f"manufacturing/{item}.{ext}" for ext in ("csv", "html", "json")} <= seen:
            pack.lack(item, "manufacturing_report_missing", f"the {item} report set is incomplete")
    for gap in supplement.missing:
        if gap.get("item") not in ("process", "quote"):
            raise AppError(422, "invalid_manufacturing_report", "invalid manufacturing report gap")
        pack.lack(gap["item"], gap["code"], gap["message"])


_CHECK_STATUSES = ("not_requested", "skipped_turbo", "pending", "passed", "failed", "unknown")


def _delivery_source_checks(db: Session, rev: AssemblyRevision | None, part_revs: list[ModelRevision],
                            drawing_package: Any, level_packages: list[Any] | None = None) -> list[dict[str, Any]]:
    """S6: the check status of each delivered source as recorded on it — the assembly revision, every unique part
    revision (its verification, later standard checks included) and the drawing package (and, in a layered delivery,
    every sub-assembly package). Read-only."""
    from .verification import revision_verification

    def norm(s: Any) -> str:
        return s if s in _CHECK_STATUSES else "unknown"

    out: list[dict[str, Any]] = []
    if rev is not None:
        out.append({"type": "assembly_revision", "id": str(rev.id), "label": f"A{rev.rev_no}", "check_status": norm(rev.check_status)})
    seen: set[uuid.UUID] = set()
    for prev in part_revs:
        if prev.id in seen:
            continue
        seen.add(prev.id)
        part = db.get(Part, prev.part_id)
        out.append({"type": "model_revision", "id": str(prev.id), "label": f"{part.number if part else prev.part_id} R{prev.rev_no}",
                    "check_status": norm(revision_verification(prev, db).get("check_status"))})
    if drawing_package is not None:
        st = getattr(drawing_package, "check_status", None) or (_pkg_manifest(drawing_package).get("verification") or {}).get("check_status")
        out.append({"type": "drawing_package", "id": str(getattr(drawing_package, "id", "")), "label": "drawings", "check_status": norm(st)})
    for p in level_packages or []:
        st = getattr(p, "check_status", None) or (_pkg_manifest(p).get("verification") or {}).get("check_status")
        src = _pkg_manifest(p).get("source") or {}
        label = f"drawings {src.get('assembly_number') or src.get('assembly_name') or ''} {src.get('revision_label') or ''}".strip()
        out.append({"type": "drawing_package", "id": str(getattr(p, "id", "")), "label": label, "check_status": norm(st)})
    return out


def _next_number(db: Session, project_id: uuid.UUID) -> int:
    db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"delivery_manifest.number:{project_id}"})
    return (db.scalar(select(func.max(DeliveryManifest.number)).where(DeliveryManifest.project_id == project_id)) or 0) + 1


def _delivery_assumption_state(db: Session, rev: AssemblyRevision | None, part_revs: list[ModelRevision],
                               spec_v: EngineeringSpecVersion | None, assumptions: list[dict[str, Any]],
                               confirmed_out: list[Any] | None = None) -> tuple[list[Any], list[Any]]:
    """(unconfirmed, corrected) assumptions of a delivery, read from the person decisions of each source (the same
    verification blocks their own pages show). Drawing-package assumptions have no decision of their own: they stay
    unconfirmed while they need one. ``confirmed_out`` collects the confirmed ones (the AI decision list)."""
    from .verification import assembly_verification, revision_verification, spec_verification

    blocks = [assembly_verification(db, rev, upstream=False)] if rev is not None else []
    seen: set[uuid.UUID] = set()
    for prev in part_revs:
        if prev.id not in seen:
            seen.add(prev.id)
            blocks.append(revision_verification(prev, db))
    if spec_v is not None:
        blocks.append(spec_verification(db, spec_v))
    unconfirmed: list[Any] = []
    corrected: list[Any] = []
    keys: set[str] = set()
    for b in blocks:
        for a in b.get("unconfirmed_assumptions") or []:
            key = json.dumps(a, sort_keys=True, default=str)
            if key not in keys:
                keys.add(key)
                unconfirmed.append(a)
        corrected += list(b.get("corrected_assumptions") or [])
        if confirmed_out is not None:
            confirmed_out += list(b.get("confirmed_assumptions") or [])
    unconfirmed += [pol.assumption_ref(a) for a in assumptions
                    if (a.get("owner") or {}).get("type") == "assembly_drawing" and pol.needs_confirmation(a)]
    return unconfirmed, corrected


def _owner_key(owner: Any, aid: Any) -> tuple[str, str, str]:
    o = owner if isinstance(owner, dict) else {}
    return str(o.get("type")), str(o.get("id")), str(aid)


def delivery_decisions(assumptions: list[dict[str, Any]], confirmed: list[Any], corrected: list[Any]) -> dict[str, Any]:
    """The AI decisions of a delivery (``policy.decisions_summary``, design §2.3): every AI / rule assumption of its
    sources once — an assembly revision's copies of its spec's assumptions are the spec's — with the person decisions
    already recorded on their sources (confirmed / corrected). Each item keeps its ``consumed_by`` (generation
    reliability §2.6, ``assemblies.decision_consumers``)."""
    spec_own = {(a.get("id"), a.get("subject"), a.get("field")) for a in assumptions
                if (a.get("owner") or {}).get("type") == "engineering_spec_version"}
    decided = {_owner_key(c.get("owner"), c.get("id")): "confirmed" for c in confirmed if isinstance(c, dict)}
    decided.update({_owner_key(c.get("owner"), c.get("id")): "corrected" for c in corrected if isinstance(c, dict)})
    items = []
    for a in assumptions:
        if (a.get("owner") or {}).get("type") == "assembly_revision" and (a.get("id"), a.get("subject"), a.get("field")) in spec_own:
            continue
        state = decided.get(_owner_key(a.get("owner"), a.get("id")))
        items.append({**a, "confirmation": {**(a.get("confirmation") or {}), "decision": state}} if state else a)
    return pol.decisions_summary(items)


# --- generation supplement E: layered delivery (assembly / sub-assemblies / parts, each STEP and DWG) -------------------

LEVELS = ("assembly", "subassembly", "part")
LEVEL_STATUSES = ("generated", "missing", "failed", "protocol_unmet")
#: Drawing errors that mean "never made" (missing) rather than "tried and failed".
_NOT_MADE = ("drawings_missing", "drawings_not_given", "assembly_missing")
_PKG_KINDS = {"page": "drawing_file", "report": "drawing_report", "manifest": "drawing_manifest"}


def _dwg_of(m: dict[str, Any], documents: set[str], prefix: str) -> dict[str, Any]:
    """DWG status of some documents of a stored package manifest: ``generated`` when they have sheets, every sheet has
    its DWG and no document failed; else ``failed`` with the first error (paths of the DWGs that exist either way)."""
    pages = [p for p in m.get("pages") or [] if isinstance(p, dict) and p.get("document") in documents]
    paths = [f"{prefix}{f['path']}" for p in pages for f in p.get("files") or [] if isinstance(f, dict) and f.get("format") == "dwg"]
    lacking = [str(p.get("key")) for p in pages if not any(isinstance(f, dict) and f.get("format") == "dwg" for f in p.get("files") or [])]
    errs = [e for e in m.get("errors") or [] if isinstance(e, dict) and e.get("document") in documents
            and (e.get("page") is None or e.get("format") in (None, "dwg", "dxf"))]
    doc_err = next((e for e in errs if e.get("page") is None), None)
    if pages and not lacking and doc_err is None:
        return {"status": "generated", "paths": paths}
    e = doc_err or (errs[0] if errs else {})
    message = e.get("message") or (f"sheet(s) without DWG: {', '.join(lacking)}" if lacking else "the document has no sheet")
    return {"status": "failed", "paths": paths, "code": e.get("code") or "dwg_missing", "message": message}


def _detail_dwg(m: dict[str, Any], def_key: str) -> dict[str, Any]:
    """DWG status of the detail sheet(s) of one part definition in the root package."""
    docs = {str(d.get("key")) for d in (m.get("required") or {}).get("documents") or []
            if isinstance(d, dict) and d.get("role") == "detail" and str(d.get("def_key")) == def_key}
    if not docs:
        return {"status": "missing", "paths": [], "code": "detail_not_drawn",
                "message": "the drawing configuration drew no detail sheet for this part"}
    return _dwg_of(m, docs, "drawings/")


def _level_counts(levels: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lv in LEVELS:
        objs = levels.get(lv) or []
        out[lv] = {"objects": len(objs), **{fmt: {s: sum(1 for o in objs if (o.get(fmt) or {}).get("status") == s) for s in LEVEL_STATUSES}
                                            for fmt in ("step", "dwg")}}
    return out


def _deliver_step(pack: _Pack, key: str, path: str, data: bytes, kind: str, item: str, source: dict[str, Any], *, required: str,
                  label: str, lack: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generation reliability M (design §2.7): one STEP file of the delivery in the required protocol. The protocol is
    read from the file's header; a file in another protocol is re-exported by OCCT (read → write, never a header
    edit) and its source protocol / hash recorded. When no file in the required protocol can be written the object is
    ``protocol_unmet`` (listed under ``missing``) and the file kept as a labelled alternative (``<name>.<protocol>.step``,
    ``alternative: true``), never counted as the required protocol. Any failure of the re-export (an OCCT exception
    too) is such a ``protocol_unmet`` with its reason, never an aborted delivery; a source is re-exported once per
    delivery and protocol (``pack.reexports``). Returns the level's ``step`` entry (also kept in ``pack.steps[key]``)."""
    from ..cad.step_protocol import StepProtocolError, protocol_of, reexport
    from .revisions import KERNEL_LOCK

    proto = protocol_of(data)
    out, note = data, None
    if proto != required:
        src_sha = _sha(data)
        done = pack.reexports.get((src_sha, required))
        if done is None:
            try:
                with KERNEL_LOCK:
                    done = reexport(data, required)
            except StepProtocolError as exc:
                done = exc.message
            except Exception as exc:  # noqa: BLE001 - an OCCT failure is this object's protocol_unmet, not the delivery's end
                log.warning("STEP re-export of %s to %s failed", label, required, exc_info=True)
                done = f"the re-export failed: {type(exc).__name__}: {exc}"[:300]
            pack.reexports[(src_sha, required)] = done
        if isinstance(done, bytes):
            out = done
            note = {"protocol": proto, "sha256": src_sha}
        else:
            alt = f"{path[:-5] if path.endswith('.step') else path}.{proto or 'unknown'}.step"
            pack.add(alt, data, kind, item, {**source, "alternative_for": path}, protocol=proto, alternative=True)
            msg = (f"{label}: no {required} file could be written ({done}); the {proto or 'unknown-protocol'} file is kept as a "
                   "labelled alternative and does not count as " + required)
            pack.lack(item, "protocol_unmet", msg, required_protocol=required, alternative={"path": alt, "protocol": proto}, **(lack or {}))
            step = {"status": "protocol_unmet", "path": None, "protocol": proto, "required_protocol": required, "code": "protocol_unmet",
                    "message": msg, "alternative": {"path": alt, "protocol": proto, "sha256": _sha(data)}}
            pack.steps[key] = step
            return step
    pack.add(path, out, kind, item, {**source, **({"reexported_from": note} if note else {})}, protocol=required)
    step = {"status": "generated", "path": path, "sha256": _sha(out), "protocol": required, **({"reexported_from": proto} if note else {})}
    pack.steps[key] = step
    return step


def _drawing_package_binding_error(package: Any, *, project_id: uuid.UUID | str | None,
                                   assembly_revision_id: uuid.UUID | str | None) -> dict[str, Any] | None:
    """A package's stored ownership must match the exact object being delivered; labels and file hashes are insufficient."""
    try:
        matches = (uuid.UUID(str(getattr(package, "project_id", None))) == uuid.UUID(str(project_id))
                   and uuid.UUID(str(getattr(package, "assembly_revision_id", None))) == uuid.UUID(str(assembly_revision_id)))
    except (ValueError, TypeError, AttributeError):
        matches = False
    if matches:
        return None
    return {"code": "drawing_package_revision_mismatch",
            "message": "the drawing package does not belong to the exact assembly revision being delivered"}


def _rejected_drawing_error(binding_error: dict[str, Any], *, upstream: list[dict[str, Any]] | None = None,
                            drawing_error: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the observed drawing failure when stale outputs supplied a package from an earlier revision."""
    observed = next((item.get("error") for item in upstream or [] if isinstance(item, dict)
                     and item.get("port") == "drawings" and item.get("status") != "succeeded"
                     and isinstance(item.get("error"), dict) and isinstance(item["error"].get("code"), str)
                     and isinstance(item["error"].get("message"), str)), None) or drawing_error
    if isinstance(observed, dict) and isinstance(observed.get("code"), str) and isinstance(observed.get("message"), str):
        return {"code": observed["code"], "message": observed["message"],
                "details": {"rejected_drawing_package": dict(binding_error)}}
    return dict(binding_error)


def _deliver_levels(db: Session, pack: _Pack, *, rev: AssemblyRevision | None, asm: Assembly | None, part_revs: list[ModelRevision],
                    outside: set[uuid.UUID], root_pkg: Any, drawing_error: dict[str, Any] | None, drawings: bool,
                    subs: list[dict[str, Any]], cov: dict[str, Any] | None, step_protocol: str = "AP203") -> tuple[dict[str, Any], list[Any]]:
    """Generation supplement E (BHD1 §8.7): the per-level part of a layered delivery. Adds every sub-assembly
    revision's STEP (``assemblies/<number>_A<rev>.step``, exported from that revision) and drawing package
    (``drawings/<number>_A<rev>/…``), and lists under ``missing`` each object whose STEP or DWG is missing
    (``subassembly_step`` / ``subassembly_drawings`` / ``part_drawings``, one entry per object with its level, number and
    reason). Returns the ``levels`` summary — per level the objects with their STEP and DWG status (generated | missing
    | failed), file paths and reason codes; definitions the coverage lists as missing are listed too (stage, code) —
    and the sub-assembly packages delivered (for the source checks)."""
    from ..cad.build_record import with_representation
    from . import assemblies as asm_svc
    from .revisions import build_record_of

    def no_dwg() -> dict[str, Any]:
        if not drawings:
            return {"status": "missing", "paths": [], "code": "not_required", "message": "this delivery does not require drawings"}
        err = drawing_error or {}
        code = str(err.get("code") or "drawings_missing")
        return {"status": "missing" if code in _NOT_MADE or code in DRAWING_STOP_CODES else "failed", "paths": [], "code": code,
                "message": err.get("message") or "no drawing package was made"}

    def step_lack(item: str, key: str, value: str) -> dict[str, Any] | None:
        return next((x for x in pack.missing if x["item"] == item and (x.get("source") or {}).get(key) == value), None)

    levels: dict[str, list[dict[str, Any]]] = {lv: [] for lv in LEVELS}
    root_m = _pkg_manifest(root_pkg) if root_pkg is not None else None
    delivered: list[Any] = []
    # -- the assembly itself ---------------------------------------------------------------------------------------------
    if rev is not None and asm is not None:
        lack = next((x for x in pack.missing if x["item"] == "assembly_step"), None)
        step = pack.steps.get(f"assembly:{rev.id}") or {"status": "failed", "path": None, "code": (lack or {}).get("code") or "step_missing",
                                                         "message": (lack or {}).get("message")}
        dwg = _dwg_of(root_m, {"GA"}, "drawings/") if root_m is not None and drawings else no_dwg()
        if root_pkg is not None:
            dwg["package_id"] = str(getattr(root_pkg, "id", ""))
        levels["assembly"].append({"key": str(rev.id), "number": asm.number, "name": asm.name, "rev": f"A{rev.rev_no}", "rev_no": rev.rev_no,
                                   "assembly_id": str(asm.id), "revision_id": str(rev.id), "step": step, "dwg": dwg})
    else:
        levels["assembly"].append({"key": None, "number": None, "name": None, "rev": None, "rev_no": None,
                                   "step": {"status": "missing", "path": None, "code": "assembly_missing"},
                                   "dwg": {"status": "missing", "paths": [], "code": "assembly_missing"}})
    # -- every distinct sub-assembly revision of the tree ----------------------------------------------------------------
    drawn = {str(s.get("assembly_revision_id")): s for s in subs if isinstance(s, dict)}
    for s in asm_svc.subassembly_revisions(db, rev.id) if rev is not None else []:
        label = f"{s['number'] or s['name']} A{s['rev_no']}"
        folder = f"{_safe(str(s['number'] or s['name']))}_A{s['rev_no']}"
        src = {"assembly_id": s["assembly_id"], "assembly_revision_id": s["assembly_revision_id"], "number": s["number"], "rev_no": s["rev_no"]}
        try:
            data, sha = asm_svc.export_step(db, s["assembly_revision_id"], protocol=step_protocol)
        except AppError as exc:
            step = {"status": "failed", "path": None, "code": exc.code, "message": exc.message}
            pack.lack("subassembly_step", exc.code, f"{label}: {exc.message}", level="subassembly", source=src)
        else:
            srev = db.get(AssemblyRevision, uuid.UUID(s["assembly_revision_id"]))
            step = _deliver_step(pack, f"subassembly:{s['assembly_revision_id']}", f"assemblies/{folder}.step", data, "subassembly_step",
                                 "subassembly_step", {**src, "content_sha256": srev.content_sha256 if srev else None,
                                                      "parent_revision_id": s["parent_revision_id"]},
                                 required=step_protocol, label=label, lack={"level": "subassembly", "source": src})
        entry = drawn.get(s["assembly_revision_id"])
        pkg = load_drawing_package(db, entry["package_id"]) if drawings and entry and entry.get("package_id") else None
        if not drawings:
            dwg = no_dwg()
        elif pkg is not None and (binding_error := _drawing_package_binding_error(
                pkg, project_id=asm.project_id if asm is not None else None, assembly_revision_id=s["assembly_revision_id"])):
            dwg = {"status": "failed", "paths": [], "code": binding_error["code"], "message": binding_error["message"]}
        elif pkg is not None:
            prefix = f"drawings/{folder}/"
            files = _package_entries(db, pkg)
            if isinstance(files, dict):  # a page file / the manifest is missing or does not match its hash
                dwg = {"status": "failed", "paths": [], "code": files["code"], "message": files["message"]}
            else:
                for e in files:
                    pack.add(prefix + e["path"], e["data"], _PKG_KINDS.get(e["kind"], "drawing_file"), "subassembly_drawings",
                             {"package_id": str(pkg.id), **src, "page": e["page"], "format": e["format"], "sha256": e["sha256"]})
                dwg = _dwg_of(_pkg_manifest(pkg), {"GA"}, prefix)
                if dwg["status"] == "generated" and drawing_brief(pkg)["completeness"] != "complete":
                    dwg.update(code="drawing_package_partial", message="the package is partial (a sheet or a format other than the DWG failed)")
                delivered.append(pkg)
            dwg["package_id"] = str(pkg.id)
        elif entry and entry.get("error"):  # R: a package the step never started (budget, cancel, lease) is missing, not failed
            dwg = {"status": "missing" if entry.get("completeness") == "not_started" else "failed", "paths": [],
                   "code": entry["error"].get("code") or "drawing_failed", "message": entry["error"].get("message")}
        elif entry and entry.get("package_id"):
            dwg = {"status": "failed", "paths": [], "code": "drawing_package_unreadable", "message": "the package cannot be read in this build"}
        elif root_pkg is None:
            dwg = no_dwg()
        else:
            dwg = {"status": "missing", "paths": [], "code": "subassembly_not_drawn", "message": "the drawing step drew no package of it"}
        if drawings and (dwg["status"] != "generated" or dwg.get("code")):
            pack.lack("subassembly_drawings", dwg.get("code") or "drawing_failed", f"{label}: {dwg.get('message') or dwg['status']}",
                      level="subassembly", source=src)
        levels["subassembly"].append({"key": s["assembly_revision_id"], "number": s["number"], "name": s["name"], "rev": f"A{s['rev_no']}",
                                      "rev_no": s["rev_no"], "assembly_id": s["assembly_id"], "revision_id": s["assembly_revision_id"],
                                      "depth": s["depth"], "instances": s["instances"], "step": step, "dwg": dwg})
    # -- every part definition (one entry per part revision: repeated instances share one set of files) ------------------
    flat = {str(ln.get("def_key")): ln for ln in (rev.bom or {}).get("flattened") or [] if isinstance(ln, dict)} if rev is not None else {}
    seen: set[uuid.UUID] = set()
    for prev in part_revs:
        if prev.id in seen:
            continue
        seen.add(prev.id)
        part = db.get(Part, prev.part_id)
        number = part.number if part else str(prev.part_id)
        lack = step_lack("part_step", "model_revision_id", str(prev.id))
        step = pack.steps.get(f"part:{prev.id}") or {
            "status": "missing" if (lack or {}).get("code") in (None, "step_missing") else "failed", "path": None,
            "code": (lack or {}).get("code") or "step_missing", "message": (lack or {}).get("message")}
        if not drawings:
            dwg = no_dwg()
        elif prev.id in outside:
            dwg = {"status": "missing", "paths": [], "code": "not_in_assembly",
                   "message": "the part is not installed in the assembly revision; detail sheets are drawn for the assembly's parts"}
        elif root_m is None:
            dwg = no_dwg()
        else:
            dwg = _detail_dwg(root_m, str(prev.id))
        line = flat.get(str(prev.id)) or {}
        src = {"part_id": str(prev.part_id), "part_number": number, "model_revision_id": str(prev.id), "rev_no": prev.rev_no}
        if drawings and dwg["status"] != "generated":
            pack.lack("part_drawings", dwg.get("code") or "drawing_failed", f"{number} R{prev.rev_no}: {dwg.get('message') or dwg['status']}",
                      level="part", source=src)
        # generation reliability M: what the build made and did not make (a blank plate is no finished nameplate)
        record = build_record_of(db, prev)
        record = with_representation(record, line.get("representation"))
        levels["part"].append({"key": str(prev.id), "number": number, "name": part.name if part else None, "rev": f"R{prev.rev_no}",
                               "rev_no": prev.rev_no, "part_id": str(prev.part_id), "revision_id": str(prev.id),
                               "procurement": line.get("procurement"), "representation": record["representation"],
                               "quantity": line.get("total_quantity"), "in_assembly": prev.id not in outside, "step": step, "dwg": dwg,
                               "build_record": record})
    # -- definitions the coverage lists as missing (partial assembly): no files, listed with their stage and reason --------
    gaps = (cov or {}).get("missing") or [] if (cov or {}).get("status") != "complete" else []
    for m in gaps:
        if not isinstance(m, dict) or m.get("kind") == "relation":
            continue
        is_asm = m.get("kind") == "assembly"
        if m.get("stage") not in ("modeling", "structure", "extraction") and not (is_asm and m.get("stage") == "assembly"):
            continue  # instance-level items (placement) belong to a definition listed above
        code = str(m.get("code") or "missing")
        levels["subassembly" if is_asm else "part"].append({
            "key": m.get("key"), "spec_key": m.get("key"), "number": m.get("number"), "name": m.get("name"), "rev": None, "rev_no": None,
            "stage": m.get("stage"), "step": {"status": "missing", "path": None, "code": code},
            "dwg": {"status": "missing", "paths": [], "code": code}, "coverage": True})
    return {**levels, "counts": _level_counts(levels), "step_protocol_required": step_protocol}, delivered


def _delivery_coverage_projection(db: Session, *, revision: AssemblyRevision, spec_version_id: uuid.UUID | str | None,
                                  node_run_id: uuid.UUID | None, projection: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Accept only diagnostics from this delivery's exact upstream build attempt; revision coverage stays authoritative."""
    from ..models import NodeRun, WorkflowRun
    from .spec_coverage import accept_modeling_diagnostics

    frozen = (revision.spec_ref or {}).get("coverage")
    if not isinstance(frozen, dict) or not isinstance(projection, dict) or node_run_id is None:
        return frozen, None
    if (str(projection.get("assembly_revision_id")) != str(revision.id)
            or revision.spec_version_id is None
            or str(projection.get("spec_version_id")) != str(revision.spec_version_id)
            or str(spec_version_id) != str(revision.spec_version_id)
            or type(projection.get("attempt")) is not int or projection["attempt"] < 1):
        return frozen, None
    try:
        source_id = uuid.UUID(str(projection.get("node_run_id")))
    except (ValueError, TypeError, AttributeError):
        return frozen, None
    source, target = db.get(NodeRun, source_id), db.get(NodeRun, node_run_id)
    if (source is None or target is None or source.run_id != target.run_id
            or source.node_type != "assembly_from_spec" or source.status not in ("succeeded", "failed")
            or type(source.attempt) is not int or source.attempt != projection["attempt"]):
        return frozen, None
    run = db.get(WorkflowRun, target.run_id)
    edges = [e for e in ((run.graph_snapshot if run is not None else {}) or {}).get("edges") or []
             if e.get("source") == source.node_id and e.get("target") == target.node_id and e.get("target_port") == "assembly"]
    if len(edges) != 1:
        return frozen, None
    value = (source.outputs or {}).get(edges[0].get("source_port"))
    candidate = projection.get("coverage")
    if (not isinstance(value, dict) or value.get("type") != "assembly"
            or str(value.get("assembly_revision_id")) != str(revision.id)
            or str(value.get("spec_version_id")) != str(revision.spec_version_id)
            or not isinstance(candidate, dict)):
        return frozen, None
    binding = {"assembly_revision_id": str(revision.id), "spec_version_id": str(revision.spec_version_id),
               "node_run_id": str(source.id), "attempt": source.attempt}
    tag = value.get("coverage_diagnostics")
    if not isinstance(tag, dict) or type(tag.get("attempt")) is not int or tag != binding:
        return frozen, None
    # Compare typed JSON rather than Python equality (True must not equal the count 1).
    try:
        if json.dumps(value.get("coverage"), sort_keys=True, allow_nan=False) != json.dumps(candidate, sort_keys=True, allow_nan=False):
            return frozen, None
    except (ValueError, TypeError):
        return frozen, None
    accepted = accept_modeling_diagnostics(frozen, candidate)
    if accepted is None:
        return frozen, None
    return accepted, {"basis": "current_build", **binding}


def deliver(db: Session, *, project_id: uuid.UUID, user: User, policy: str, assembly_revision_id: uuid.UUID | str | None = None,
            part_revision_ids: list[Any] | None = None, drawing_package: Any = None, drawing_error: dict[str, Any] | None = None,
            drawings_required: bool = True, spec_version_id: uuid.UUID | str | None = None, upstream: list[dict[str, Any]] | None = None,
            node_run_id: uuid.UUID | None = None, bom_formats: list[str] | tuple = ("json", "csv"),
            coverage: dict[str, Any] | None = None, coverage_projection: dict[str, Any] | None = None,
            subassembly_packages: list[dict[str, Any]] | None = None,
            step_protocol: str = "AP203", manufacturing_reports: ManufacturingReports | None = None) -> DeliveryManifest:
    """Write the delivery manifest + zip of exact sources (see the module docstring). ``part_revision_ids``: the parts
    built when no assembly could be made (with an assembly: the built parts it does not install are delivered on their
    own, ``source.in_assembly`` false). ``drawing_error``: why there is no drawing package (e.g. capability_unavailable).
    ``upstream``: failures of earlier steps, recorded in the manifest. 422 ``nothing_to_deliver`` when not a single
    required file exists (an empty manifest is not a product).

    Generation supplement B-03: the manifest carries the build's ``coverage`` (the assembly revision's
    ``spec_ref.coverage``, else ``coverage`` — the assembly step's block when no assembly was made) and ``decisions``
    (the AI decisions of every source); each missing object of the coverage is listed under ``missing``
    (item ``assembly_coverage``), so an incomplete coverage keeps the delivery ``partial`` whatever files exist. The zip
    adds ``report/coverage.json``, ``report/ai_decisions.json`` and ``report/ai_decisions.md`` (listed in
    ``manifest.reports`` with their hashes).

    An exact upstream ``coverage_projection`` may refresh only matching modeling diagnostics for the current build.
    Its revision/spec/node/attempt binding and unchanged non-diagnostic coverage are verified; counts, missing
    inventory and completion always come from the saved revision. Direct callers without this context use frozen data.

    Generation supplement E (BHD1 §8.7): ``subassembly_packages`` (the layered drawing step's entries; ``[]`` when that
    step made none) makes the delivery layered — every sub-assembly revision of the tree adds its STEP and its drawing
    package, every part definition needs its detail DWG, each missing one is listed under ``missing``
    (``subassembly_step`` / ``subassembly_drawings`` / ``part_drawings``), and the manifest carries ``levels``: per level
    (assembly / subassembly / part) the objects with their STEP and DWG status and paths (:func:`_deliver_levels`).
    None (the default, every other caller): delivered as before.

    Generation reliability M (design §2.7): ``step_protocol`` (``AP203`` by default, ``AP214`` allowed) is frozen in the
    manifest as ``step_protocol_required``; every delivered STEP (assembly, sub-assemblies, parts) is in it, read from
    its header (each file entry and level ``step`` carry ``protocol``). The assembly and sub-assembly STEPs are exported
    in it directly (``export_step(protocol=…)``; a STEP the writer cannot make is listed under ``missing``). A stored
    part STEP in another protocol (an AP214 revision reused from before, or a part of several solids, which is never
    written in AP203) is re-exported by OCCT; when that fails the object is ``protocol_unmet`` with
    the other file kept as a labelled alternative (:func:`_deliver_step`). Each level part entry carries the part
    revision's ``build_record``."""
    from . import assets
    from .verification import assumption_refs

    run_policy = pol.parse_policy(policy)
    if step_protocol not in ("AP203", "AP214"):
        raise AppError(422, "invalid_step_protocol", f"step_protocol must be AP203 or AP214, not {step_protocol!r}")
    pack = _Pack()
    required: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    assumptions: list[dict[str, Any]] = []
    simulated = False
    rev: AssemblyRevision | None = None
    asm: Assembly | None = None
    spec_v: EngineeringSpecVersion | None = None
    if assembly_revision_id is not None:
        rev = db.get(AssemblyRevision, uuid.UUID(str(assembly_revision_id)))
        asm = db.get(Assembly, rev.assembly_id) if rev is not None else None
        if rev is None or asm is None or asm.project_id != project_id:
            raise not_found("assembly revision")
        if spec_version_id is None and rev.spec_version_id:
            spec_version_id = rev.spec_version_id
    if spec_version_id is not None:
        spec_v = db.get(EngineeringSpecVersion, uuid.UUID(str(spec_version_id)))
        if spec_v is not None and spec_v.project_id != project_id:
            spec_v = None
    if drawing_package is not None:
        binding_error = _drawing_package_binding_error(drawing_package, project_id=project_id,
                                                       assembly_revision_id=rev.id if rev is not None else None)
        if binding_error is not None:
            drawing_error = _rejected_drawing_error(binding_error, upstream=upstream, drawing_error=drawing_error)
            drawing_package = None
    wanted = {"assembly_step", "part_step", "bom"} | ({"drawings"} if drawings_required else set())
    for d in ((spec_v.content or {}).get("deliverables") or []) if spec_v is not None else []:
        if isinstance(d, dict) and d.get("required", True) and DELIVERABLE_ITEMS.get(d.get("kind")):
            wanted.add(DELIVERABLE_ITEMS[d["kind"]])
    if manufacturing_reports is not None:
        # Internal manufacturing exporter: both sets are promised even if the design did not ask explicitly.
        wanted.update(("process", "quote"))
    order = ["assembly_step", "part_step", "bom", "drawings", "process", "quote"]
    required = [{"item": k, "from_spec": spec_v is not None and any(DELIVERABLE_ITEMS.get(d.get("kind")) == k for d in
                                                                     (spec_v.content or {}).get("deliverables") or [] if isinstance(d, dict))}
                for k in order if k in wanted]
    layered = subassembly_packages is not None
    if layered:  # generation supplement E: each level on its own, STEP and DWG
        required += [{"item": k, "from_spec": False} for k in ("subassembly_step", "subassembly_drawings", "part_drawings")
                     if k == "subassembly_step" or "drawings" in wanted]

    # -- assembly STEP and BOM --------------------------------------------------------------------------------------
    part_revs: list[ModelRevision] = []
    representation: dict[str, str] = {}
    if rev is not None:
        from . import assemblies as asm_svc

        sources["assembly"] = {"assembly_id": str(asm.id), "number": asm.number, "name": asm.name, "revision_id": str(rev.id),  # type: ignore[union-attr]
                               "rev_no": rev.rev_no, "content_sha256": rev.content_sha256, "bom_sha256": rev.bom_sha256,
                               "execution_policy": rev.execution_policy, "check_status": rev.check_status, "stats": rev.stats or {}}
        try:
            data, _sha256 = asm_svc.export_step(db, rev.id, protocol=step_protocol)
            _deliver_step(pack, f"assembly:{rev.id}", f"assembly/{_safe(asm.number)}_A{rev.rev_no}.step", data, "assembly_step",  # type: ignore[union-attr]
                          "assembly_step", {"assembly_revision_id": str(rev.id), "rev_no": rev.rev_no, "content_sha256": rev.content_sha256},
                          required=step_protocol, label=f"{asm.number} A{rev.rev_no}")  # type: ignore[union-attr]
        except AppError as exc:
            pack.lack("assembly_step", exc.code, exc.message)
        bom = rev.bom or {}
        flat = bom.get("flattened") or []
        hier = bom.get("hierarchical") or []
        bom_src = {"assembly_revision_id": str(rev.id), "bom_sha256": rev.bom_sha256}
        if "json" in bom_formats:
            body = {"schema": bom.get("schema"), "assembly": sources["assembly"], "bom_sha256": rev.bom_sha256, "units": "mm",
                    "counts": bom.get("counts") or {}, "hierarchical": hier, "flattened": flat, "suppressed": bom.get("suppressed") or [],
                    "rules": bom.get("rules") or {}}
            pack.add("bom/bom.json", json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"), "bom_json", "bom", bom_src)
        if "csv" in bom_formats:
            pack.add("bom/bom_hierarchical.csv", _bom_csv(hier, True), "bom_csv_hierarchical", "bom", bom_src)
            pack.add("bom/bom_flattened.csv", _bom_csv(flat, False), "bom_csv_flattened", "bom", bom_src)
        for line in flat:
            prev = db.get(ModelRevision, uuid.UUID(str(line["def_key"]))) if line.get("def_key") else None
            if prev is not None:
                part_revs.append(prev)
                representation[str(line.get("number") or line.get("name"))] = line.get("representation") or "exact"
        assumptions += [{**a, "owner": {"type": "assembly_revision", "id": str(rev.id), "rev_no": rev.rev_no}}
                        for a in rev.assumptions or [] if isinstance(a, dict)]
        simulated = simulated or bool((rev.spec_ref or {}).get("simulated"))
    else:
        pack.lack("assembly_step", "assembly_missing", "no assembly revision was built")
        pack.lack("bom", "assembly_missing", "the BOM comes from the assembly's instance tree; no assembly was built")
        for rid in part_revision_ids or []:
            prev = db.get(ModelRevision, uuid.UUID(str(rid)))
            part = db.get(Part, prev.part_id) if prev is not None else None
            if prev is None or part is None or part.project_id != project_id:
                continue
            part_revs.append(prev)

    # generation supplement B-02: parts built for a partial assembly but not installed in it are delivered on their own
    outside: set[uuid.UUID] = set()
    if rev is not None:
        for rid in part_revision_ids or []:
            prev = db.get(ModelRevision, uuid.UUID(str(rid)))
            part = db.get(Part, prev.part_id) if prev is not None else None
            if prev is None or part is None or part.project_id != project_id or any(p.id == prev.id for p in part_revs):
                continue
            part_revs.append(prev)
            outside.add(prev.id)

    # -- unique part STEPs (each part revision's own stored STEP) ---------------------------------------------------------
    seen: set[uuid.UUID] = set()
    for prev in part_revs:
        if prev.id in seen:
            continue
        seen.add(prev.id)
        part = db.get(Part, prev.part_id)
        name = f"parts/{_safe(part.number if part else str(prev.part_id))}_R{prev.rev_no}.step"
        src = {"part_id": str(prev.part_id), "part_number": part.number if part else None, "model_revision_id": str(prev.id),
               "rev_no": prev.rev_no, "step_sha256": prev.step_sha, **({"in_assembly": False} if prev.id in outside else {})}
        if not prev.step_sha:
            pack.lack("part_step", "step_missing", f"{part.number if part else prev.part_id} R{prev.rev_no} has no STEP (mesh reference?)",
                      source=src)
            continue
        try:
            step = read_blob(db, prev.step_sha)[0]
        except (FileNotFoundError, OSError):
            pack.lack("part_step", "blob_missing", f"the STEP of {part.number if part else prev.part_id} R{prev.rev_no} is not in storage", source=src)
        else:
            if _sha(step) == prev.step_sha:
                _deliver_step(pack, f"part:{prev.id}", name, step, "part_step", "part_step", src, required=step_protocol,
                              label=f"{part.number if part else prev.part_id} R{prev.rev_no}", lack={"source": src})
            else:  # never deliver bytes that are not the revision's STEP
                pack.lack("part_step", "blob_mismatch", f"the stored STEP of {part.number if part else prev.part_id} R{prev.rev_no} "
                          "does not match its recorded sha256", source=src)
        simulated = simulated or bool(isinstance(prev.ai_summary, dict) and prev.ai_summary.get("simulated"))
        assumptions += assumption_refs(prev)
    if not part_revs:
        pack.lack("part_step", "parts_missing", "no part revision was built")

    # -- drawings (W5 package) ------------------------------------------------------------------------------------------
    if "drawings" in wanted:
        if drawing_package is not None:
            m = _pkg_manifest(drawing_package)
            pkg_id = str(getattr(drawing_package, "id", ""))
            sources["drawing_package"] = {"package_id": pkg_id, "manifest_sha256": getattr(drawing_package, "manifest_sha256", None),
                                          "completeness": drawing_brief(drawing_package)["completeness"], "version": m.get("version")}
            entries = _package_entries(db, drawing_package)
            if isinstance(entries, dict):  # W5 refused: a page file / the manifest is missing or does not match its hash
                pack.lack("drawings", entries["code"], entries["message"], **({"details": entries["details"]} if entries.get("details") else {}))
                entries = []
            kinds = {"page": "drawing_file", "report": "drawing_report", "manifest": "drawing_manifest"}
            for e in entries:
                src = {"package_id": pkg_id, "page": e["page"], "format": e["format"], "sha256": e["sha256"]}
                pack.add(f"drawings/{e['path']}", e["data"], kinds.get(e["kind"], "drawing_file"), "drawings", src)
            for page in m.get("pages") or []:
                if isinstance(page, dict) and (page.get("missing") or page.get("status") not in (None, "complete")):
                    pack.lack("drawings", "drawing_page_incomplete", f"page {page.get('key')} is {page.get('status')}",
                              page=page.get("key"), formats=page.get("missing") or [])
            for e in m.get("errors") or []:
                if isinstance(e, dict) and e.get("page") is None:
                    pack.lack("drawings", e.get("code") or "drawing_document_failed", e.get("message") or "a drawing document failed",
                              document=e.get("document"))
            assumptions += [{**a, "owner": {"type": "assembly_drawing", "id": pkg_id}} for a in m.get("assumptions") or [] if isinstance(a, dict)]
            if not entries:
                pack.lack("drawings", "drawing_files_missing", "the drawing package lists no files")
        else:
            err = drawing_error or {"code": "drawings_missing", "message": "no drawing package was made"}
            pack.lack("drawings", err.get("code") or "drawings_missing", err.get("message") or "no drawing package was made",
                      **({"details": err["details"]} if err.get("details") else {}))
    if manufacturing_reports is not None:
        _manufacturing_reports(pack, manufacturing_reports)
        sources["manufacturing"] = manufacturing_reports.sources
    else:
        for item in ("process", "quote"):
            if item in wanted:
                pack.lack(item, "not_in_pipeline", f"the spec asks for {item}; this delivery pipeline does not produce it yet")

    # -- assembly coverage (target vs actual): every missing object is missing from the delivery too ----------------------
    rev_cov = (rev.spec_ref or {}).get("coverage") if rev is not None else None
    cov = rev_cov if isinstance(rev_cov, dict) else (coverage if isinstance(coverage, dict) else None)
    if rev is not None and isinstance(rev_cov, dict) and coverage_projection is not None:
        cov, diagnostic_source = _delivery_coverage_projection(db, revision=rev, spec_version_id=spec_version_id,
                                                              node_run_id=node_run_id, projection=coverage_projection)
        if diagnostic_source is not None:
            sources["coverage_diagnostics"] = diagnostic_source
    if cov is not None and cov.get("status") != "complete":
        for m in cov.get("missing") or []:
            if isinstance(m, dict):
                pack.lack("assembly_coverage", str(m.get("code") or "missing"), f"{m.get('name') or m.get('key')}: {m.get('reason') or ''}".strip(),
                          stage=m.get("stage"), key=m.get("key"), kind=m.get("kind"), quantity=m.get("quantity"), recovery=m.get("recovery"))
        shown = int((cov.get("arranged") or {}).get("instances") or 0) if rev is not None else 0
        if shown:
            pack.lack("assembly_coverage", "arranged_only", f"{shown} part instance(s) are only arranged for display (a recorded rule "
                      "decision), not installed", quantity=shown)
        if cov.get("status") == "partial" and not cov.get("missing") and not shown:
            pack.lack("assembly_coverage", "assembly_partial", "the assembly does not hold its whole target")

    # -- generation supplement E: every level on its own (sub-assembly STEPs and packages, part detail DWGs) --------------
    levels: dict[str, Any] | None = None
    level_pkgs: list[Any] = []
    if layered:
        levels, level_pkgs = _deliver_levels(db, pack, rev=rev, asm=asm, part_revs=part_revs, outside=outside,
                                             root_pkg=drawing_package if "drawings" in wanted else None, drawing_error=drawing_error,
                                             drawings="drawings" in wanted, subs=list(subassembly_packages or []), cov=cov,
                                             step_protocol=step_protocol)
        sources["subassembly_drawing_packages"] = [
            {"package_id": str(p.id), "assembly_revision_id": str(getattr(p, "assembly_revision_id", "")),
             "manifest_sha256": getattr(p, "manifest_sha256", None), "completeness": drawing_brief(p)["completeness"],
             "version": _pkg_manifest(p).get("version")} for p in level_pkgs]
        for p in level_pkgs:
            assumptions += [{**a, "owner": {"type": "assembly_drawing", "id": str(p.id)}} for a in _pkg_manifest(p).get("assumptions") or []
                            if isinstance(a, dict)]

    if spec_v is not None:
        sv_spec = db.get(EngineeringSpec, spec_v.spec_id)
        sources["spec"] = {"spec_id": str(spec_v.spec_id), "code": f"SP-{sv_spec.number}" if sv_spec else None,
                           "spec_version_id": str(spec_v.id), "version_no": spec_v.version_no, "execution_policy": spec_v.execution_policy,
                           "simulated": bool(spec_v.simulated)}
        simulated = simulated or bool(spec_v.simulated)
        assumptions += [{**a, "owner": {"type": "engineering_spec_version", "id": str(spec_v.id), "version_no": spec_v.version_no}}
                        for a in (spec_v.content or {}).get("assumptions") or [] if isinstance(a, dict)]
    if not pack.files:
        raise AppError(422, "nothing_to_deliver", "no required file exists to deliver", {"missing": pack.missing, "upstream": upstream or []})

    check_status = rev.check_status if rev is not None else "not_requested"
    if check_status not in _CHECK_STATUSES:
        check_status = "unknown"
    if pol.is_turbo(run_policy):
        check_status = "skipped_turbo" if check_status in ("pending", "not_requested", "skipped_turbo") else check_status
    # S6 (failed > not verified > passed): the real check status of every delivered source travels with the manifest,
    # and a failed check of any of them makes the delivery failed — never only "not verified".
    source_checks = _delivery_source_checks(db, rev, part_revs, drawing_package if "drawings" in wanted else None, level_pkgs)
    if any(s["check_status"] == "failed" for s in source_checks):
        check_status = "failed"
    # S5: values a person already confirmed (on the spec version, the assembly or a part revision) are not unconfirmed
    # here; a corrected one keeps the delivery unverified (the correction needs a new upstream version).
    confirmed: list[Any] = []
    unconfirmed, corrected = _delivery_assumption_state(db, rev, part_revs, spec_v, assumptions, confirmed)
    verification = pol.verification(run_policy, check_status, "not_requested",  # type: ignore[arg-type]
                                    [{k: a.get(k) for k in ("id", "subject", "field", "adopted", "unit", "author", "owner", "basis", "trigger",
                                                          "affected", "alternatives", "sources") if k in a}
                                     for a in assumptions], unconfirmed_assumptions=unconfirmed)
    if corrected:
        verification["unverified"] = True
        verification["corrected_assumptions"] = corrected
    verification["source_checks"] = source_checks
    if pol.is_turbo(run_policy):
        # Turbo skipped acceptance: never verified, whatever the checks say; a failed check still reads failed.
        verification["unverified"] = True
    completeness = "complete" if not pack.missing else "partial"
    from .assemblies import decision_consumers  # generation reliability §2.6: where each decision was actually used

    # the parts whose detail sheet the delivered package holds (a part revision's own decision reaches that sheet)
    part_drawings: dict[str, str] = {}
    if drawing_package is not None and "drawings" in wanted and any(f.get("item") == "drawings" for f in pack.files):
        root_m = _pkg_manifest(drawing_package)
        part_drawings = {str(p.id): str(getattr(drawing_package, "id", "")) for p in part_revs
                         if _detail_dwg(root_m, str(p.id))["status"] == "generated"}
    decisions = delivery_decisions(decision_consumers(db, assumptions, rev=rev, part_revs=part_revs, files=pack.files,
                                                      part_drawings=part_drawings), confirmed, corrected)
    # generation reliability N (design §2.8): the state of every level object and of the delivery (shown apart from the
    # check status), and which checks the policy closed and how often one of them really ran (the checks ledger)
    from .verification import checks_report, level_states

    if levels is not None:
        root_pkg_id = getattr(drawing_package, "id", None) if "drawings" in wanted else None
        level_states(db, levels, decisions=decisions, source_checks=source_checks, coverage=cov,
                     root_package_id=str(root_pkg_id) if root_pkg_id else None)
    state = pol.object_state(generated=bool(pack.files), complete=completeness == "complete", simulated=simulated,
                             ai_decisions=int(decisions.get("needs_confirmation") or 0), check_status=check_status,
                             unverified=bool(verification.get("unverified")))
    checks = checks_report(db, policy=run_policy, node_run_id=node_run_id)
    # the readable reports of what the delivery holds and what AI decided (generation supplement B-03 / D-02)
    reports = {"report/coverage.json": json.dumps({"coverage": cov, "missing": [m for m in pack.missing if m["item"] == "assembly_coverage"],
                                                    "sources": sources}, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"),
               "report/ai_decisions.json": json.dumps(decisions, ensure_ascii=False, indent=1, sort_keys=True, default=str).encode("utf-8"),
               "report/ai_decisions.md": pol.decisions_markdown(decisions).encode("utf-8")}
    if rev is not None and (physical := ((rev.checks or {}).get("assembly.placement_geometry") or {}).get("latest")):
        reports["report/assembly_placement_geometry.json"] = json.dumps(
            {**physical, "assembly_revision_id": str(rev.id), "scope": "generation_feedback", "acceptance": "not_evaluated"},
            ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    if spec_v is not None and (design_report := (spec_v.planning or {}).get("design")):
        from .engineering_design import report_files

        reports.update(report_files(design_report))
    manifest = {
        "schema": DELIVERY_SCHEMA, "project_id": str(project_id), "units": "mm", "frame": "right-handed, Z up",
        "execution_policy": run_policy, "completeness": completeness, "simulated": simulated, "step_protocol_required": step_protocol,
        "state": state, "checks": checks,
        "sources": sources, "required": required, "files": pack.files, "missing": pack.missing,
        **({"manufacturing": manufacturing_reports.summary} if manufacturing_reports is not None else {}),
        "representation": representation, "assumptions": assumptions, "verification": verification,
        "coverage": cov, "decisions": decisions, **({"levels": levels} if levels is not None else {}),
        "reports": [{"path": p, "sha256": _sha(b), "bytes": len(b)} for p, b in reports.items()],
        "upstream": list(upstream or []), "rules": {"complete": "every required item has every file it needs and the assembly holds "
                                                                "its whole target (coverage)",
                                                     "part_step": "each unique part revision's own stored STEP",
                                                     "step_protocol": "every STEP is in step_protocol_required, read from its "
                                                                      "FILE_SCHEMA; another protocol is re-exported by OCCT "
                                                                      "(read → write, never relabelled); when that fails the object "
                                                                      "is protocol_unmet and the file is kept as a labelled "
                                                                      "alternative (<name>.<protocol>.step)",
                                                     **({"levels": "layered: each sub-assembly revision's own STEP and drawing package; "
                                                                   "each part definition's detail DWG (the root package draws them)"}
                                                        if layered else {}),
                                                     "bom": "frozen with the assembly revision (instance tree)"},
    }
    manifest_sha = _canon(manifest)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pack.files:
            info = zipfile.ZipInfo(f["path"], date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, pack.data[f["path"]])
        for path, body in reports.items():
            info = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, body)
        info = zipfile.ZipInfo("manifest.json", date_time=_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, json.dumps({**manifest, "manifest_sha256": manifest_sha}, ensure_ascii=False, indent=1, sort_keys=True))
    zip_sha = save_blob(db, buf.getvalue(), "application/zip")
    row = DeliveryManifest(project_id=project_id, number=_next_number(db, project_id), assembly_revision_id=rev.id if rev else None,
                           spec_version_id=spec_v.id if spec_v else None,
                           drawing_package_id=uuid.UUID(str(drawing_package.id)) if drawing_package is not None and getattr(drawing_package, "id", None) else None,
                           execution_policy=run_policy, completeness=completeness, check_status=check_status, simulated=simulated,
                           manifest=manifest, manifest_sha256=manifest_sha, zip_sha256=zip_sha, node_run_id=node_run_id, created_by=user.id)
    db.add(row)
    db.flush()
    assets.ensure_version(db, "delivery_manifest", row.id)
    audit(db, action="delivery_manifest.create", subject_type="delivery_manifest", subject_id=row.id, user_id=user.id, project_id=project_id,
          data={"completeness": completeness, "files": len(pack.files), "missing": len(pack.missing), "policy": run_policy})
    return row


def delivery_brief(row: DeliveryManifest) -> dict[str, Any]:
    """The ``delivery`` port value / tool data."""
    m = row.manifest or {}
    dec = m.get("decisions") if isinstance(m.get("decisions"), dict) else None
    return {"type": "delivery", "delivery_id": str(row.id), "number": f"DL-{row.number}", "completeness": row.completeness,
            "execution_policy": row.execution_policy, "check_status": row.check_status, "simulated": row.simulated,
            "files": len(m.get("files") or []), "missing": m.get("missing") or [], "manifest_sha256": row.manifest_sha256,
            "zip_sha256": row.zip_sha256, "verification": m.get("verification") or {},
            # generation reliability N: the delivery's object state and its check report (closed checks really run)
            "state": m.get("state"), "checks": m.get("checks"),
            # generation supplement B: what the delivered assembly holds of its target, and how many AI decisions it rests on
            "coverage": m.get("coverage"), "decisions": {k: v for k, v in dec.items() if k != "items"} if dec else None,
            # generation supplement E: per level (assembly / subassembly / part) the objects with their STEP and DWG status
            "levels": m.get("levels"),
            "assembly_revision_id": str(row.assembly_revision_id) if row.assembly_revision_id else None,
            "download": f"/delivery-manifests/{row.id}/download", "path": f"/delivery-manifests/{row.id}"}


def delivery_out(db: Session, row: DeliveryManifest) -> dict[str, Any]:
    from . import assets

    v = assets.version_for(db, "delivery_manifest", row.id)
    return {**delivery_brief(row), "id": str(row.id), "project_id": str(row.project_id), "manifest": row.manifest,
            "spec_version_id": str(row.spec_version_id) if row.spec_version_id else None,
            "drawing_package_id": str(row.drawing_package_id) if row.drawing_package_id else None,
            "asset_version_id": str(v.id) if v else None, "labels": assets.labels_of(db, v) if v else [],
            "stale": _stale(db, row), "created_by": str(row.created_by), "created_at": row.created_at.isoformat() if row.created_at else None}


def _stale(db: Session, row: DeliveryManifest) -> dict[str, Any] | None:
    """A delivery of an assembly revision that is no longer the latest is stale (it stays readable / downloadable)."""
    if row.assembly_revision_id is None:
        return None
    from . import assemblies as asm_svc

    rev = db.get(AssemblyRevision, row.assembly_revision_id)
    latest = asm_svc.latest_revision(db, rev.assembly_id) if rev is not None else None
    if rev is None or latest is None or latest.id == rev.id:
        return None
    return {"code": "newer_assembly_revision", "latest_revision_id": str(latest.id), "latest_rev_no": latest.rev_no}
