"""Development seed data: `uv run python -m app.seed`.

Creates demo users, one demo project and model configs that point at
GEMINI_API_KEY / OPENAI_API_KEY environment variables. Idempotent.

DEV_PASSWORD is a local development credential only. With PARTROUTE_ENV=prod
the seed refuses to run, except for a deployed demo (PARTROUTE_DEMO_SEED=true),
whose accounts then get PARTROUTE_SEED_PASSWORD instead (10+ characters, kept
in Secret Manager; the public development password is refused).
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import ModelConfigRow, Project, ProjectMember, User
from .security import hash_password
from .services import feature_flags

DEV_PASSWORD = "partroute-dev"

DEV_USERS = [
    # email, name, admin, role in the demo project (None = not a member)
    ("admin@partroute.test", "Ada Admin", True, "owner"),
    ("designer@partroute.test", "Dana Designer", False, "designer"),
    ("process@partroute.test", "Paul Process", False, "process"),
    ("quoter@partroute.test", "Quinn Quoter", False, "quoter"),
    ("viewer@partroute.test", "Vic Viewer", False, "viewer"),
    ("outsider@partroute.test", "Olive Outsider", False, None),
]


MIN_DEMO_PASSWORD = 10


def seed_password(settings) -> str:
    """Password for newly created demo accounts (see the module docstring)."""
    if settings.env != "prod":
        return settings.seed_password or DEV_PASSWORD
    if not settings.demo_seed:
        sys.exit("refusing to seed a production database (a demo deployment sets PARTROUTE_DEMO_SEED=true)")
    pw = settings.seed_password or ""
    if len(pw) < MIN_DEMO_PASSWORD or pw == DEV_PASSWORD:
        sys.exit(f"a demo deployment needs PARTROUTE_SEED_PASSWORD ({MIN_DEMO_PASSWORD}+ characters, not the development password)")
    return pw


def seed() -> None:
    settings = get_settings()
    password = seed_password(settings)
    with session_scope() as db:
        users: dict[str, User] = {}
        for email, name, admin, _ in DEV_USERS:
            u = db.scalar(select(User).where(User.email == email))
            if u is None:
                u = User(email=email, name=name, is_admin=admin, password_hash=hash_password(password))
                db.add(u)
                db.flush()
            users[email] = u

        project = db.scalar(select(Project).where(Project.code == "DEMO"))
        if project is None:
            project = Project(
                code="DEMO",
                name="Demo machining project",
                description="Seed project for local development. Data here is demo data.",
                created_by=users["admin@partroute.test"].id,
            )
            db.add(project)
            db.flush()
        for email, _, _, role in DEV_USERS:
            if role and db.get(ProjectMember, {"project_id": project.id, "user_id": users[email].id}) is None:
                db.add(ProjectMember(project_id=project.id, user_id=users[email].id, role=role))
        if feature_flags.enabled("stages"):
            from .models import ProjectMilestone
            from .services import stages

            if db.scalar(select(ProjectMilestone.id).where(ProjectMilestone.project_id == project.id).limit(1)) is None:
                stages.create_milestones(db, project, stages.stage_names("en"), users["admin@partroute.test"].id)
            elif project.current_milestone_id is None:
                first = db.scalar(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id, ProjectMilestone.status != "accepted")
                                  .order_by(ProjectMilestone.seq).limit(1))
                project.current_milestone_id = first.id if first else None
        _seed_templates(db, project, users)
        _seed_agent_profiles(db, project, users)

        # A second project the designer does not belong to (isolation checks).
        other = db.scalar(select(Project).where(Project.code == "OTHER"))
        if other is None:
            other = Project(code="OTHER", name="Isolated project", description="Only the outsider and admin are members.", created_by=users["outsider@partroute.test"].id)
            db.add(other)
            db.flush()
            db.add(ProjectMember(project_id=other.id, user_id=users["outsider@partroute.test"].id, role="owner"))
            db.add(ProjectMember(project_id=other.id, user_id=users["admin@partroute.test"].id, role="viewer"))

        if db.scalar(select(ModelConfigRow).limit(1)) is None:
            db.add(
                ModelConfigRow(
                    label="Gemini (default)",
                    provider="gemini",
                    model_id=settings.default_gemini_model,
                    secret_ref="env:GEMINI_API_KEY",
                    capabilities={"text": True, "vision": True, "tools": True, "structured": True},
                    is_default=True,
                )
            )
            db.add(
                ModelConfigRow(
                    label="OpenAI",
                    provider="openai",
                    model_id=settings.default_openai_model,
                    secret_ref="env:OPENAI_API_KEY",
                    capabilities={"text": True, "vision": True, "tools": True, "structured": True},
                )
            )
    # Register any pre-existing engineering rows in the asset catalogue (idempotent).
    from .backfill import backfill_assets

    with session_scope() as db:
        backfill_assets(db)
    print("seeded: users", ", ".join(e for e, *_ in DEV_USERS), "| projects DEMO, OTHER")


def _seed_templates(db, project: Project, users: dict[str, User]) -> None:
    """P3: the demo designer maintains templates; DEMO workflows that were never
    published get a v1 when they pass the publish checks (so they can run)."""
    from .errors import AppError
    from .models import Workflow, WorkflowTemplateVersion
    from .services import templates as tpl

    db.flush()
    designer = db.get(ProjectMember, {"project_id": project.id, "user_id": users["designer@partroute.test"].id})
    if designer is not None and designer.role != "viewer" and "template_manager" not in (designer.capabilities or []):
        designer.capabilities = [*(designer.capabilities or []), "template_manager"]
    for w in db.scalars(select(Workflow).where(Workflow.project_id == project.id, Workflow.archived_at.is_(None))).all():
        if db.scalar(select(WorkflowTemplateVersion.id).where(WorkflowTemplateVersion.workflow_id == w.id).limit(1)) is not None:
            continue
        g, errors, _ = tpl.publish_errors(w.graph)
        if g is None or errors:
            continue
        try:
            with db.begin_nested():
                tpl.publish(db, users["admin@partroute.test"], w.id, expected_version=None, notes="Initial version (seed)")
        except AppError:
            continue


# Collaboration (design §3.2): name, purpose, description, role prompt, tools, modes.
# All three use the default model configuration (model_config_id = NULL).
DEMO_AGENT_PROFILES = [
    ("文档执行 Agent", "execute", "Writes and organises project documents for business tasks.",
     "你是项目的文档执行 Agent：按任务目标与固定资料撰写文档，只交付真实的资产版本，不自报完成。",
     ["document", "export", "knowledge_reference", "knowledge_adopt"], ["full_auto", "semi_auto"]),
    ("文档验收 Agent", "review", "Independent final reviewer of document deliverables.",
     "你是独立的文档验收 Agent：逐项核对验收标准，给出 pass / fail / unknown 与证据，不替执行者补做。",
     ["engineering_review"], ["full_auto", "semi_auto"]),
    ("需求规划 Agent", "planner", "Turns a demand into a business task graph with missing facts listed.",
     "你是需求规划 Agent：把需求拆成可验收的任务图，缺少的工程事实列入澄清项，不猜测。",
     [], ["full_auto", "semi_auto"]),
]


def _seed_agent_profiles(db, project: Project, users: dict[str, User]) -> None:
    """Seed enabled collaboration demo profiles; preserve frozen history unchanged."""
    if not feature_flags.enabled("collaboration"):
        return
    from .models import AgentProfile
    from .services.agent_profiles import AgentLimits

    for name, purpose, description, role_prompt, tools, modes in DEMO_AGENT_PROFILES:
        if db.scalar(select(AgentProfile.id).where(AgentProfile.project_id == project.id, AgentProfile.name == name)) is not None:
            continue
        db.add(AgentProfile(
            project_id=project.id, name=name, purpose=purpose, description=description, role_prompt=role_prompt, model_config_id=None,
            allowed_tools=tools, execution_modes=modes, knowledge_scope={"scope": "project"}, limits=AgentLimits().model_dump(mode="json"),
            pricing=None, enabled=True, created_by=users["admin@partroute.test"].id,
        ))
    db.flush()


if __name__ == "__main__":
    seed()
