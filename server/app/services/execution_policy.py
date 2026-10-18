"""Resolve current project policy once, preserving source-version provenance.

Every entry that starts work resolves its policy here before anything else runs (W7: frozen before context building /
planning) and records ``policy_source`` with it: workflow runs (``workflow_runs.policy_source``), AI workspace
operations (their request snapshot), assembly / drawing requests (audit). The source version is the exact version the
request continues from: the spec version an assembly is built from, the assembly revision a change / drawing package /
delivery starts from, the part revision a modeling change starts from, the run a rerun repeats.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from .. import policy as pol
from ..errors import AppError, not_found
from ..models import EngineeringSpecVersion, Project, User, WorkflowRun
from .audit import audit


def project_default(db: Session, project_id: uuid.UUID | None) -> str:
    """The project's "default Turbo" switch (standard = off)."""
    project = db.get(Project, project_id) if project_id else None
    value = getattr(project, "default_execution_policy", None)
    return value if value in pol.POLICIES else pol.STANDARD


def resolve(db: Session, project_id: uuid.UUID | None, explicit: str | None, *, source: Any = None,
            source_version: dict[str, Any] | None = None) -> pol.PolicyChoice:
    """Fresh request policy with the project version; source policy is provenance only.

    Explicit overrides are guarded at the API. Child/retry/wake callers pass their frozen choice."""
    from .verification import source_brief

    if source is not None and source_version is None:
        source_version = source_brief(db, source)
    project = db.get(Project, project_id) if project_id else None
    try:
        return pol.resolve_policy(explicit, source_version=source_version, project_default=project_default(db, project_id),
                                  project_version=project.version if project else None)
    except ValueError as exc:
        raise AppError(400, "invalid_execution_policy", str(exc))


def frozen_on(op: Any) -> dict[str, Any]:
    """``start_run`` keyword arguments that pass an AI operation's frozen policy and its source on to the run it starts
    (operations from before S5 carry no source: their policy was the request's own)."""
    req = getattr(op, "request", None) or {}
    return {"execution_policy": op.execution_policy, "policy_source": req.get("policy_source") or "explicit",
            "source_version": req.get("policy_source_version"), "project_version": req.get("project_version")}


def run_source(run: WorkflowRun) -> dict[str, Any]:
    """A rerun continues from the run it repeats."""
    return {"type": "run", "id": str(run.id), "label": run.title or "", "execution_policy": run.execution_policy}


def graph_source(db: Session, project_id: uuid.UUID, graph: dict[str, Any] | None) -> EngineeringSpecVersion | None:
    """A chain graph that pins exactly one spec version of this project (``engineering_spec`` node with
    ``spec_version_id`` and no new material) continues from it. Anything else (no pin, several, another project's,
    unknown id) has no source here; the node itself reports a bad pin."""
    found: set[str] = set()
    for n in (graph or {}).get("nodes") or []:
        if isinstance(n, dict) and n.get("type") == "engineering_spec":
            vid = (n.get("params") or {}).get("spec_version_id")
            if vid:
                found.add(str(vid))
    if len(found) != 1:
        return None
    try:
        v = db.get(EngineeringSpecVersion, uuid.UUID(found.pop()))
    except ValueError:
        return None
    return v if v is not None and v.project_id == project_id else None


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def request_source(db: Session, project_id: uuid.UUID, kind: str | None, target: dict[str, Any] | None,
                   references: list[dict[str, Any]] | None) -> Any:
    """The version an AI workspace request continues from (an ORM row, or None): an assembly revision target
    (assembly_modify), a part revision target or the part's latest revision (model_modify, drawing_generate), the one
    engineering spec version referenced by an assembly_create. Rows of another project are never a source."""
    from . import assemblies as asm_svc
    from .verification import load_source

    def load(source_type: str, raw: Any) -> Any:
        sid = _uuid(raw)
        if sid is None:
            return None
        try:
            return load_source(db, source_type, sid, project_id)
        except AppError:
            return None

    t = target or {}
    if t.get("kind") == "assembly_revision":
        return load("assembly_revision", t.get("object_id"))
    if t.get("kind") in ("part_revision", "cad_selection"):
        return load("model_revision", t.get("object_id"))
    if t.get("kind") == "part" and kind in ("model_modify", "drawing_generate"):
        pid = _uuid(t.get("object_id"))
        rev = asm_svc.latest_part_revision(db, pid) if pid else None
        return load("model_revision", rev.id) if rev is not None else None
    if kind == "assembly_create":
        refs = [r for r in ([t] + list(references or [])) if isinstance(r, dict) and r.get("kind") == "engineering_spec_version"]
        ids = {str(r.get("object_id")) for r in refs if r.get("object_id")}
        if len(ids) == 1:
            return load("engineering_spec_version", ids.pop())
    return None


def preview(db: Session, project_id: uuid.UUID, *, explicit: str | None = None, source_type: str | None = None,
            source_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    """What a request would be frozen with (UI before submitting): the policy, its source, the project default, and
    for a source version how many AI / rule assumptions upstream wait for a person (what a standard takeover needs)."""
    from . import assemblies as asm_svc
    from .verification import load_source, source_brief, upstream_assumptions

    if source_type == "run" and source_id:  # a rerun continues from the run it repeats (nothing upstream to list)
        run = db.get(WorkflowRun, uuid.UUID(str(source_id)))
        if run is None or run.project_id != project_id:
            raise not_found("run")
        choice = resolve(db, project_id, explicit, source_version=run_source(run))
        return {**choice.out(), "project_default": project_default(db, project_id), "upstream": None}
    if source_type == "part" and source_id:  # a modeling change of a part continues from its latest revision
        rev = asm_svc.latest_part_revision(db, uuid.UUID(str(source_id)))
        source_type, source_id = ("model_revision", rev.id) if rev is not None else (None, None)
    obj = load_source(db, source_type, source_id, project_id) if source_type and source_id else None
    brief = source_brief(db, obj) if obj is not None else None
    choice = resolve(db, project_id, explicit, source_version=brief)
    out: dict[str, Any] = {**choice.out(), "project_default": project_default(db, project_id), "upstream": None}
    if obj is not None:
        listing = upstream_assumptions(db, project_id=project_id, source_type=str(source_type), source_id=str(source_id))
        out["upstream"] = {**listing["counts"], "pending": listing["pending"], "blocked": listing["blocked"]}
    return out


def record(db: Session, user: User, project_id: uuid.UUID, choice: pol.PolicyChoice, *, subject_type: str, subject_id: Any,
           action: str) -> None:
    """Audit where the frozen policy of a request without a run came from (assembly / drawing package requests)."""
    audit(db, action="execution_policy.frozen", subject_type=subject_type, subject_id=subject_id, user_id=user.id, project_id=project_id,
          data={"for": action, **choice.out()})
