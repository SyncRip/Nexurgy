"""Cross-project public template library (design §5.1; contract §2.8).

A project shares one of its published template versions as a public template
(v1) and may append later versions. Other projects import a specific public
version as a new project template (published as project v1) and upgrade only
explicitly; publishing a new public version never changes any project, its
template versions or its runs (A18).
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError, conflict, forbidden, not_found
from ..models import Project, ProjectMember, PublicTemplate, PublicTemplateVersion, User, Workflow
from ..security import project_access
from . import feature_flags as features
from . import templates as tpl
from .audit import audit


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _user_brief(db: Session, user_id: uuid.UUID | None) -> dict | None:
    u = db.get(User, user_id) if user_id else None
    return {"id": str(u.id), "name": u.name} if u else None


def can_manage(user: User, pt: PublicTemplate) -> bool:
    return user.is_admin or pt.created_by == user.id


def latest_public(db: Session, public_template_id: uuid.UUID) -> PublicTemplateVersion | None:
    return db.scalar(select(PublicTemplateVersion).where(PublicTemplateVersion.public_template_id == public_template_id,
                                                         PublicTemplateVersion.status == "published")
                     .order_by(PublicTemplateVersion.version_no.desc()).limit(1))


def load_template(db: Session, public_template_id: uuid.UUID, *, lock: bool = False) -> PublicTemplate:
    stmt = select(PublicTemplate).where(PublicTemplate.id == public_template_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    pt = db.scalar(stmt)
    if pt is None or pt.archived_at is not None:
        raise not_found("public template")
    return pt


def load_public_version(db: Session, version_id: uuid.UUID) -> tuple[PublicTemplateVersion, PublicTemplate]:
    pv = db.get(PublicTemplateVersion, version_id)
    pt = db.get(PublicTemplate, pv.public_template_id) if pv else None
    if pv is None or pt is None or pt.archived_at is not None:
        raise not_found("public template version")
    return pv, pt


# --- views ---------------------------------------------------------------------


def public_version_out(db: Session, pv: PublicTemplateVersion, *, content: bool = False) -> dict:
    out = {
        "id": str(pv.id),
        "public_template_id": str(pv.public_template_id),
        "version_no": pv.version_no,
        "status": pv.status,
        "hash": pv.hash,
        "notes": pv.notes,
        "published_by": _user_brief(db, pv.published_by),
        "published_at": _iso(pv.published_at),
        "deprecated_at": _iso(pv.deprecated_at),
        "source_template_version_id": str(pv.source_template_version_id) if pv.source_template_version_id else None,
    }
    if content:
        out.update({"graph": pv.graph, "input_schema": pv.input_schema, "output_schema": pv.output_schema, "policy": pv.policy})
    return out


def _member_projects(db: Session, user: User) -> set[uuid.UUID]:
    return set(db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)).all())


def template_out(db: Session, user: User, pt: PublicTemplate, members: set[uuid.UUID] | None = None) -> dict:
    members = _member_projects(db, user) if members is None else members
    project = db.get(Project, pt.source_project_id) if pt.source_project_id else None
    latest = latest_public(db, pt.id)
    count = db.scalar(select(func.count()).select_from(PublicTemplateVersion).where(PublicTemplateVersion.public_template_id == pt.id)) or 0
    return {
        "id": str(pt.id),
        "name": pt.name,
        "description": pt.description,
        "created_by": _user_brief(db, pt.created_by),
        "created_at": _iso(pt.created_at),
        # The source project is shown to its members only (project ids never leak to non-members).
        "source_project": {"id": str(project.id), "code": project.code} if project and project.id in members else None,
        "latest_version": public_version_out(db, latest) if latest else None,
        "version_count": int(count),
        "can_manage": features.enabled("template_library") and can_manage(user, pt),
    }


def template_detail(db: Session, user: User, pt: PublicTemplate) -> dict:
    versions = db.scalars(select(PublicTemplateVersion).where(PublicTemplateVersion.public_template_id == pt.id)
                          .order_by(PublicTemplateVersion.version_no.desc())).all()
    return {**template_out(db, user, pt), "versions": [public_version_out(db, v, content=True) for v in versions]}


def list_templates(db: Session, user: User, q: str | None = None) -> list[dict]:
    stmt = select(PublicTemplate).where(PublicTemplate.archived_at.is_(None)).order_by(PublicTemplate.created_at.desc())
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(PublicTemplate.name.ilike(like) | PublicTemplate.description.ilike(like))
    members = _member_projects(db, user)
    return [template_out(db, user, pt, members) for pt in db.scalars(stmt).all()]


# --- sharing ------------------------------------------------------------------------


def _shareable(db: Session, user: User, template_version_id: uuid.UUID):
    v = tpl.load_version(db, user, template_version_id, "template")
    if v.status == "deprecated":
        raise conflict("template_version_deprecated", f"v{v.version_no} is deprecated; share a published version", version_no=v.version_no)
    return v


def share(db: Session, user: User, *, template_version_id: uuid.UUID, name: str | None, description: str | None) -> PublicTemplate:
    """Share a project's published version as a new public template (v1). Caller commits."""
    v = _shareable(db, user, template_version_id)
    features.require_enabled("template_library")
    features.guard_graph(v.graph)
    project = db.get(Project, v.project_id)
    pt = PublicTemplate(name=(name or v.name).strip()[:200] or v.name, description=description if description is not None else v.description,
                        source_project_id=v.project_id, created_by=user.id)
    db.add(pt)
    db.flush()
    pv = _new_version(db, user, pt, v, 1, v.notes or f"Shared from {project.code if project else 'a project'} v{v.version_no}")
    audit(db, action="library.share", subject_type="public_template", subject_id=pt.id, user_id=user.id, project_id=v.project_id,
          data={"template_version_id": str(v.id), "public_version_id": str(pv.id), "hash": v.hash})
    return pt


def _new_version(db: Session, user: User, pt: PublicTemplate, v, no: int, notes: str) -> PublicTemplateVersion:
    features.require_enabled("template_library")
    features.guard_graph(v.graph)
    pv = PublicTemplateVersion(
        public_template_id=pt.id,
        version_no=no,
        graph=copy.deepcopy(v.graph),
        input_schema=copy.deepcopy(v.input_schema),
        output_schema=copy.deepcopy(v.output_schema),
        policy=copy.deepcopy(v.policy),
        hash=v.hash,
        notes=(notes or "").strip(),
        source_template_version_id=v.id,
        status="published",
        published_by=user.id,
    )
    db.add(pv)
    db.flush()
    return pv


def add_version(db: Session, user: User, public_template_id: uuid.UUID, *, template_version_id: uuid.UUID, notes: str) -> PublicTemplateVersion:
    """Append a public version from a project's published version (creator or admin). Caller commits."""
    pt = load_template(db, public_template_id, lock=True)  # serialises version_no
    if not can_manage(user, pt):
        raise forbidden("only the creator of the public template or an administrator can publish new versions")
    v = _shareable(db, user, template_version_id)
    features.require_enabled("template_library")
    features.guard_graph(v.graph)
    latest = latest_public(db, pt.id)
    if latest is not None and latest.hash == v.hash:
        raise conflict("no_changes", f"the content is identical to public version v{latest.version_no}", version_no=latest.version_no)
    no = (db.scalar(select(func.max(PublicTemplateVersion.version_no)).where(PublicTemplateVersion.public_template_id == pt.id)) or 0) + 1
    pv = _new_version(db, user, pt, v, no, notes)
    audit(db, action="library.publish", subject_type="public_template", subject_id=pt.id, user_id=user.id, project_id=v.project_id,
          data={"template_version_id": str(v.id), "public_version_id": str(pv.id), "version_no": no, "hash": v.hash})
    return pv


def deprecate_version(db: Session, user: User, public_version_id: uuid.UUID) -> PublicTemplateVersion:
    """Deprecated public versions cannot be imported or upgraded to; projects that use them keep them. Idempotent."""
    pv, pt = load_public_version(db, public_version_id)
    if not can_manage(user, pt):
        raise forbidden("only the creator of the public template or an administrator can deprecate its versions")
    features.require_enabled("template_library")
    pv = db.scalar(select(PublicTemplateVersion).where(PublicTemplateVersion.id == public_version_id).with_for_update()
                   .execution_options(populate_existing=True))
    assert pv is not None
    if pv.status != "deprecated":
        pv.status = "deprecated"
        pv.deprecated_at = datetime.now(UTC)
        audit(db, action="library.deprecate", subject_type="public_template", subject_id=pt.id, user_id=user.id,
              data={"public_version_id": str(pv.id), "version_no": pv.version_no})
        db.flush()
    return pv


# --- import and upgrade --------------------------------------------------------------


def _importable(db: Session, public_version_id: uuid.UUID) -> tuple[PublicTemplateVersion, PublicTemplate]:
    pv, pt = load_public_version(db, public_version_id)
    if pv.status == "deprecated":
        raise conflict("template_version_deprecated", f"public v{pv.version_no} is deprecated", version_no=pv.version_no)
    return pv, pt


def import_version(db: Session, user: User, project_id: uuid.UUID, *, public_version_id: uuid.UUID, name: str | None) -> Workflow:
    """New project template whose draft is the public version's content, published as project v1. Caller commits."""
    project_access(db, user, project_id, "template")
    features.require_enabled("template_library")
    pv, pt = _importable(db, public_version_id)
    features.guard_graph(pv.graph)
    w = Workflow(project_id=project_id, name=(name or pt.name).strip()[:200] or pt.name, description=pt.description,
                 graph=copy.deepcopy(pv.graph), policy=tpl.normalize_policy(pv.policy), source_public_template_id=pt.id, created_by=user.id)
    db.add(w)
    db.flush()
    audit(db, action="workflow.import", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=project_id,
          data={"public_template_id": str(pt.id), "public_version_id": str(pv.id), "version_no": pv.version_no})
    tpl.publish(db, user, w.id, expected_version=w.version, notes=f"Imported from the template library: {pt.name} v{pv.version_no}",
                source_public_version_id=pv.id)
    return w


def upgrade(db: Session, user: User, workflow_id: uuid.UUID, *, public_version_id: uuid.UUID, expected_version: int | None) -> Workflow:
    """Replace the draft with a newer public version of the same template and
    publish it as a new project version. Older project versions and runs stay. Caller commits."""
    w = db.get(Workflow, workflow_id)
    if w is None:
        raise not_found("workflow")
    project_access(db, user, w.project_id, "template")
    features.require_enabled("template_library")
    pv, pt = _importable(db, public_version_id)
    features.guard_graph(pv.graph)
    if w.source_public_template_id is None or pv.public_template_id != w.source_public_template_id:
        raise AppError(422, "upgrade_wrong_template", "choose a version of the public template this workflow was imported from")
    info = tpl.library_info(db, w) or {}
    adopted = info.get("adopted_version_no")
    if adopted is not None and pv.version_no <= adopted:
        raise conflict("upgrade_not_newer", f"the workflow already uses public v{adopted}", adopted_version_no=adopted)
    w = tpl.lock_workflow(db, workflow_id)
    tpl.check_version(w, expected_version)
    w.graph = copy.deepcopy(pv.graph)
    w.policy = tpl.normalize_policy(pv.policy)
    w.version += 1
    db.flush()
    tpl.publish(db, user, w.id, expected_version=w.version, notes=f"Upgraded to {pt.name} v{pv.version_no} from the template library",
                source_public_version_id=pv.id)
    audit(db, action="workflow.upgrade", subject_type="workflow", subject_id=w.id, user_id=user.id, project_id=w.project_id,
          data={"public_template_id": str(pt.id), "public_version_id": str(pv.id), "from_version_no": adopted, "to_version_no": pv.version_no})
    return w
