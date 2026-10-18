"""ORM models. Table design: .trellis/tasks/archive/2026-10/10-02-v04-platform/design.md §4.

Enumerations are stored as text with CHECK constraints so that adding a value
is a one-line migration. Every id is a UUID; times are timestamptz.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

ROLES = ("owner", "designer", "process", "quoter", "viewer")
PART_STATUSES = ("draft", "in_design", "in_review", "released", "archived")
FILE_KINDS = ("model_step", "model_mesh", "drawing_pdf", "drawing_image", "drawing_dxf", "document", "other")
REVISION_SOURCES = ("manual", "ai", "import", "copy")
APPROVAL_STAGES = ("design", "process", "quote")
APPROVAL_SUBJECTS = ("model_revision", "drawing", "process_plan", "quote", "intake")
RUN_STATUSES = ("queued", "running", "waiting", "succeeded", "partial", "failed", "cancelled")
NODE_STATUSES = ("pending", "ready", "running", "waiting_human", "succeeded", "failed", "cancelled", "skipped")
PROVIDERS = ("gemini", "openai", "openai_compatible")
PROJECT_LIFECYCLE = ("draft", "active", "on_hold", "completed", "cancelled")
TASK_STATUSES = ("todo", "in_progress", "in_review", "done", "cancelled")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")
ASSET_TYPES = ("part_model", "drawing", "process_plan", "quote", "document", "intake", "process_module",
               "assembly_model", "assembly_drawing", "drawing_document", "engineering_spec", "delivery_manifest")
ASSET_AVAILABILITY = ("building", "ready", "failed")
ASSET_PUBLISH = ("draft", "published", "deprecated")
SUBMISSION_STATUSES = ("pending", "approved", "rejected", "withdrawn")
REQUEST_KINDS = ("engineering", "task_acceptance", "milestone_review")
REQUEST_STATUSES = ("pending", "approved", "rejected", "withdrawn", "superseded")
# P3 / P4 (docs/archive/project-task-asset-design.zh-CN.md §5, §4.1, §6.1)
MEMBER_CAPABILITIES = ("template_manager",)
TEMPLATE_VERSION_STATUSES = ("published", "deprecated")
MILESTONE_STATUSES = ("open", "in_review", "accepted")
REVIEW_STATUSES = ("pending", "approved", "rejected", "withdrawn", "superseded")
# Agent collaboration (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §2)
AGENT_PURPOSES = ("execute", "review", "planner", "general")
EXECUTION_MODES = ("full_auto", "semi_auto")
ACTOR_KINDS = ("human", "agent")
WORKFLOW_KINDS = ("tool", "collaboration")
PLANNING_SOURCES = ("ai", "manual")
# completed_unverified (W7 Turbo): the node's real outputs are saved and released downstream without any review,
# approval or acceptance (never counted as accepted).
NODE_TASK_PHASES = ("blocked", "ready", "executing", "submitted", "in_review", "rework", "accepted", "failed", "cancelled",
                    "completed_unverified")
AGENT_EXECUTION_ROLES = ("execute", "pre_review", "review", "planner")
AGENT_EXECUTION_STATUSES = ("running", "delivered", "blocked", "failed", "superseded", "cancelled")
# P1 execution layer (.trellis/tasks/10-11-agent-execution-layer/design.md §6): executor kinds, named by role (not framework).
AGENT_RUNTIMES = ("native", "tool_agent", "workspace_agent")
# Timeline event types of one attempt (design §6.4); P3 adds command / file_change / confirmation.
AGENT_EVENT_TYPES = ("system", "model_call", "message", "tool_call", "tool_result", "question", "deliver", "blocked", "verdict", "status",
                     # P3 (a1c0ffee0024): the workspace agent's terminal commands, file changes and confirmations
                     "command", "file_change", "confirmation")
RESOURCE_KINDS = ("input", "candidate", "formal", "failed", "superseded", "evidence")
ARCHIVE_STATUSES = ("none", "pending", "archived", "failed")
COLLECTION_STATUSES = ("active", "deprecated")
INDEX_STATUSES = ("pending", "indexing", "ready", "partial", "failed")
ITEM_INDEX_STATUSES = ("pending", "ready", "failed", "not_applicable")
ITEM_KINDS = ("formal", "history", "candidate", "failed", "evidence")
KNOWLEDGE_USAGES = ("reference", "baseline")
USAGE_CONFIRMATIONS = ("none", "pending", "confirmed", "rejected")
# Unified AI workspace (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §2)
AI_OPERATION_KINDS = ("task_draft", "model_create", "model_modify", "drawing_intake", "ask", "drawing_generate", "drawing_reconstruct",
                      "workflow_edit", "assembly_create", "assembly_modify", "workflow_create", "spec_extract", "drafting_create", "drafting_modify")
AI_OPERATION_STATUSES = ("pending", "running", "waiting_input", "succeeded", "failed", "cancelled")
AI_MESSAGE_ROLES = ("user", "assistant")
# card: a run / workflow the conversation follows (AI work panel, .trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §2.4).
AI_MESSAGE_KINDS = ("request", "operation", "answer", "choice", "notice", "card")
# AI native (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §1.6): what the main agent asks before a write tool —
# nothing (auto), only before creating a task (confirm_task, the default) or before every write (step).
AI_AUTONOMY = ("auto", "confirm_task", "step")
AI_TASK_DRAFT_STATUSES = ("open", "used", "discarded")

# General mechanical engineering (docs/development/mechanical-engineering/w0-contract.zh-CN.md).
# Execution policy is chosen per request and frozen before context building / planning; standard is the default.
EXECUTION_POLICIES = ("standard", "turbo")
# S5: where a run's frozen policy came from (explicit > inherited from the source version > project default > standard).
POLICY_SOURCES = ("explicit", "inherited", "project_default", "default")
# Three separate state dimensions (architecture §2): execution (RUN_STATUSES incl. partial), engineering check, acceptance.
CHECK_STATUSES = ("not_requested", "skipped_turbo", "pending", "passed", "failed", "unknown")
ACCEPTANCE_STATUSES = ("not_requested", "pending", "accepted", "rejected")
REPRESENTATION_LEVELS = ("exact", "nominal", "simplified")
PROCUREMENT_KINDS = ("make", "buy", "standard", "unknown")
FACT_STATUSES = ("asserted", "derived", "assumed", "conflicting", "unknown")
# Explicit target kinds for AI / workflow references (the legacy part routes keep working).
TARGET_KINDS = ("part_revision", "assembly_revision", "drawing_document_version")
# W1 model credentials (architecture §7): where a config's key comes from and the state of one credential version.
CREDENTIAL_SOURCES = ("env", "direct")
CREDENTIAL_STATUSES = ("active", "replaced", "cleared", "revoked")
CONFIG_CREDENTIAL_STATUSES = ("active", "cleared", "revoked", "none")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# Actor columns (design §2.1): a row is acted on by exactly one actor, a user
# (kind human, the historical default) or an agent profile (kind agent).
def _kind_col() -> Mapped[str]:
    return mapped_column(String(10), default="human", server_default="human", nullable=False)


def _agent_col() -> Mapped[uuid.UUID | None]:
    return mapped_column(ForeignKey("agent_profiles.id", ondelete="SET NULL"))


def _actor_check(kind: str, human: str, agent: str, name: str) -> CheckConstraint:
    return CheckConstraint(
        f"({kind} = 'human' AND {human} IS NOT NULL AND {agent} IS NULL) OR ({kind} = 'agent' AND {agent} IS NOT NULL AND {human} IS NULL)",
        name=name,
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("ai_autonomy IN ('auto', 'step')", name="ck_users_ai_autonomy"),
        CheckConstraint("preferences_version > 0", name="ck_users_preferences_version"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_autonomy: Mapped[str] = mapped_column(String(20), default="auto", server_default="auto", nullable=False)
    preferences_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = _created()


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(_in("status", ("active", "archived")), name="ck_projects_status"),
        CheckConstraint(_in("lifecycle", PROJECT_LIFECYCLE), name="ck_projects_lifecycle"),
        CheckConstraint(_in("default_execution_policy", EXECUTION_POLICIES), name="ck_projects_default_policy"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # Management lifecycle (design §4.1); archive stays a separate fact (archived_at / status).
    lifecycle: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    # Current stage (P4): advanced only by an approved milestone review or a reopen.
    current_milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_milestones.id", ondelete="SET NULL", use_alter=True, name="fk_projects_current_milestone")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # New projects start with Turbo enabled. New requests use the saved project setting;
    # changing it never rewrites the policy frozen on existing operations or runs.
    default_execution_policy: Mapped[str] = mapped_column(String(20), default="turbo", server_default="turbo", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectEngineeringTeam(Base):
    """Current project preferences; execution roots store their own resolved immutable copy."""

    __tablename__ = "project_engineering_teams"
    __table_args__ = (CheckConstraint("version > 0", name="ck_project_engineering_teams_version"),)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_project_members_role"),)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Extra grants on top of the role (MEMBER_CAPABILITIES), e.g. template_manager.
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = _created()


class Part(Base):
    __tablename__ = "parts"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_parts_project_number"),
        CheckConstraint(_in("status", PART_STATUSES), name="ck_parts_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    material: Mapped[str] = mapped_column(String(40), default="AL6061-T651", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list, nullable=False)
    # Folder is a display grouping only; engineering relations always use ids.
    folder: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    # Highest op number ever used in this part, so ids are never reused.
    op_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Blob(Base):
    __tablename__ = "blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class File(Base):
    __tablename__ = "files"
    __table_args__ = (CheckConstraint(_in("kind", FILE_KINDS), name="ck_files_kind"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_file_versions_file_version"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"), nullable=False)
    original_name: Mapped[str] = mapped_column(String(300), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class ModelRevision(Base):
    __tablename__ = "model_revisions"
    __table_args__ = (
        UniqueConstraint("part_id", "rev_no", name="uq_model_revisions_part_rev"),
        CheckConstraint(_in("source", REVISION_SOURCES), name="ck_model_revisions_source"),
        CheckConstraint(_in("status", ("ready", "failed")), name="ck_model_revisions_status"),
        # blob authorisation: the derived DXF of a DWG source (checks.source.dwg.derived_dxf.blob_sha256)
        Index("ix_model_revisions_checks_gin", "checks", postgresql_using="gin", postgresql_ops={"checks": "jsonb_path_ops"}),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    rev_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    base: Mapped[dict | None] = mapped_column(JSONB)
    ops: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    build_key: Mapped[str | None] = mapped_column(String(64))
    kernel_version: Mapped[str | None] = mapped_column(String(80))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    features: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    holes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Hole-like bores found on an imported base: heuristic, never features.
    detected_holes: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    step_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    glb_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    mesh_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    # True for STL/OBJ/GLB reference imports: preview and bbox only, no STEP.
    mesh_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    material: Mapped[str] = mapped_column(String(40), nullable=False)
    request_text: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[dict | None] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="SET NULL"))
    source_file_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="SET NULL"))
    # Accepted geometry requirements (cad/requirements.py) and the server check reports bound to this
    # revision's STEP bytes: {"conformance": CheckReport, "step_audit": StepAudit}. None = not evaluated.
    requirements: Mapped[list | None] = mapped_column(JSONB)
    checks: Mapped[dict | None] = mapped_column(JSONB)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class Drawing(Base):
    __tablename__ = "drawings"
    __table_args__ = (
        CheckConstraint(_in("kind", ("generated", "uploaded")), name="ck_drawings_kind"),
        # blob authorisation: page files and check reports named in the package (JSONB containment)
        Index("ix_drawings_pages_gin", "pages", postgresql_using="gin", postgresql_ops={"pages": "jsonb_path_ops"}),
        Index("ix_drawings_manifest_gin", "manifest", postgresql_using="gin", postgresql_ops={"manifest": "jsonb_path_ops"}),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    model_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="SET NULL"))
    svg_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    dxf_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    views: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Drawing package (design §4): ordered pages with their files, the manifest and its hash, and the
    # annotation / DWG check reports. svg_sha / dxf_sha stay as the page-1 alias for older readers.
    pages: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    checks: Mapped[dict | None] = mapped_column(JSONB)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class Intake(Base):
    """Customer-drawing information organised for planning (drawing-only path)."""

    __tablename__ = "intakes"
    id: Mapped[uuid.UUID] = _uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    file_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="SET NULL"))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)  # FeatureSummary (estimated)
    extraction: Mapped[dict | None] = mapped_column(JSONB)  # raw AI extraction with confidences
    method: Mapped[str] = mapped_column(String(20), nullable=False)  # ai / manual
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class ProcessPlan(Base):
    __tablename__ = "process_plans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    model_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="SET NULL"))
    intake_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("intakes.id", ondelete="SET NULL"))
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(160), nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[uuid.UUID] = _uuid_pk()
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    process_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("process_plans.id", ondelete="CASCADE"), nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(_in("stage", APPROVAL_STAGES), name="ck_approvals_stage"),
        CheckConstraint(_in("subject_type", APPROVAL_SUBJECTS), name="ck_approvals_subject"),
        CheckConstraint(_in("decision", ("approved", "rejected")), name="ck_approvals_decision"),
        Index("ix_approvals_subject", "subject_type", "subject_id", "stage"),
        _actor_check("actor_kind", "user_id", "agent_id", "ck_approvals_actor"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Actor: a user with the stage's role, or (full-auto collaboration) an independent review agent.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_kind: Mapped[str] = _kind_col()
    agent_id: Mapped[uuid.UUID | None] = _agent_col()
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class ModelConfigRow(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        CheckConstraint(_in("provider", PROVIDERS), name="ck_model_configs_provider"),
        CheckConstraint(_in("credential_source", CREDENTIAL_SOURCES), name="ck_model_configs_credential_source"),
        CheckConstraint(_in("credential_status", CONFIG_CREDENTIAL_STATUSES), name="ck_model_configs_credential_status"),
        CheckConstraint("default_for <@ ARRAY['assistant','modeling','intake','planning']::varchar[]", name="ck_model_configs_default_for"),
        Index("uq_model_configs_global_default", text("(1)"), unique=True, postgresql_where=text("is_default")),
        *(Index(f"uq_model_configs_default_{purpose}", text("(1)"), unique=True,
                postgresql_where=text(f"'{purpose}' = ANY(default_for)"))
          for purpose in ("assistant", "modeling", "intake", "planning")),
        # secret_ref is the environment reference of an env credential only; a direct key lives in model_credentials.
        CheckConstraint("(credential_source = 'env' AND (secret_ref IS NOT NULL OR credential_status <> 'active')) "
                        "OR (credential_source = 'direct' AND secret_ref IS NULL)", name="ck_model_configs_secret_ref"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_for: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, server_default="{}", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_test: Mapped[dict | None] = mapped_column(JSONB)
    # P1: the latest capability diagnosis (tool calling, parallel calls, strict, usage, streaming), bound to config_version.
    last_diagnose: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    # W1 (architecture §7): env | direct; the active credential version (model_credentials.version), NULL when
    # cleared / revoked; config_version is bumped by every connection-relevant change (provider, model, base URL,
    # capabilities, credential) and binds snapshots and connection tests.
    credential_source: Mapped[str] = mapped_column(String(10), default="env", server_default="env", nullable=False)
    credential_status: Mapped[str] = mapped_column(String(12), default="active", server_default="active", nullable=False)
    credential_version: Mapped[int | None] = mapped_column(Integer)
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint(_in("kind", WORKFLOW_KINDS), name="ck_workflows_kind"),
        Index("uq_workflows_import_request", "project_id", "created_by", "import_request_id", unique=True,
              postgresql_where=text("import_request_id IS NOT NULL")),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # tool = the node-based tool template; collaboration = a business task graph (collaboration_drafts).
    kind: Mapped[str] = mapped_column(String(20), default="tool", server_default="tool", nullable=False)
    graph: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(60))
    # Draft input policy (P3): {"mode": "standard" | "exploratory", "input_rules": {...}}; frozen per published version.
    policy: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # Imported from the public template library (P4); upgrades are explicit.
    source_public_template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("public_templates.id", ondelete="SET NULL"))
    import_source: Mapped[dict | None] = mapped_column(JSONB)
    import_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Part workflow (R1.2): the part's own working copy, started from a template and edited for that
    # part by workflow members. NULL = a project template. Part workflows run their current graph;
    # every run is still an immutable snapshot, and they are never published (save as a template instead).
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowTemplateVersion(Base):
    """Immutable published version of a project workflow template (design §5.1).
    Editing the draft never changes a published version; runs pin one."""

    __tablename__ = "workflow_template_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version_no", name="uq_template_versions_workflow_no"),
        CheckConstraint(_in("status", TEMPLATE_VERSION_STATUSES), name="ck_template_versions_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    graph: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    policy: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Draft edit counter (workflows.version) the version was published from.
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    import_source: Mapped[dict | None] = mapped_column(JSONB)
    source_public_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("public_template_versions.id", ondelete="SET NULL", use_alter=True, name="fk_template_versions_source_public")
    )
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    published_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    published_at: Mapped[datetime] = _created()
    deprecated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deprecation_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PublicTemplate(Base):
    """Cross-project template library entry (design §5.1, P4). Projects import
    a specific published version; they never follow library changes silently."""

    __tablename__ = "public_templates"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicTemplateVersion(Base):
    __tablename__ = "public_template_versions"
    __table_args__ = (
        UniqueConstraint("public_template_id", "version_no", name="uq_public_template_versions_no"),
        CheckConstraint(_in("status", TEMPLATE_VERSION_STATUSES), name="ck_public_template_versions_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    public_template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("public_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    graph: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    policy: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_template_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_template_versions.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    published_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    published_at: Mapped[datetime] = _created()
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_workflow_runs_policy"),
        CheckConstraint(f"policy_source IS NULL OR {_in('policy_source', POLICY_SOURCES)}", name="ck_workflow_runs_policy_source"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_workflow_runs_status"),
        CheckConstraint(_in("origin", ("workflow", "workspace")), name="ck_workflow_runs_origin"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"), index=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflows.id", ondelete="SET NULL"), index=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer)
    # Pinned published template version (P3); NULL = draft test run, workspace run or legacy run.
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_template_versions.id", ondelete="SET NULL"), index=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    graph_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Task context (design §5.2): optional; historical runs keep task_id NULL.
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    task_baseline_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_baseline_snapshots.id", ondelete="SET NULL"))
    rerun_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # Frozen at creation (standard | turbo); never switched in place (ME-06).
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    # S5: where the frozen policy came from (POLICY_SOURCES); NULL for runs started before S5.
    policy_source: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Collaboration pause (design §4 C06): workers and the agent runtime skip a paused run.
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    # W1: exact model configurations frozen at start (services/ai.freeze_run_models): {"v": 1, "entries": {key: snapshot}}.
    model_snapshot: Mapped[dict | None] = mapped_column(JSONB)


class NodeRun(Base):
    __tablename__ = "node_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_node_runs_run_node"),
        CheckConstraint(_in("status", NODE_STATUSES), name="ck_node_runs_status"),
        Index("ix_node_runs_ready", "status", "lease_until"),
        # blob authorisation: the derived DXF a reconstruction advertises in its outputs
        Index("ix_node_runs_outputs_gin", "outputs", postgresql_using="gin", postgresql_ops={"outputs": "jsonb_path_ops"}),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(60), nullable=False)
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AiCall(Base):
    __tablename__ = "ai_calls"
    id: Mapped[uuid.UUID] = _uuid_pk()
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"), index=True)
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"))
    # Collaboration: the agent profile and execution the call belongs to.
    agent_profile_id: Mapped[uuid.UUID | None] = _agent_col()
    agent_execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_executions.id", ondelete="SET NULL"))
    # AI workspace: calls made directly by an assistant operation (ask); run steps keep node_run_id.
    ai_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_operations.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(60), nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # Set when an agent (not a person) performed the action.
    agent_id: Mapped[uuid.UUID | None] = _agent_col()
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(80), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------------------
# Project management layer (docs/archive/project-task-asset-design.zh-CN.md, P1–P2)
# ---------------------------------------------------------------------------


class ProjectMilestone(Base):
    """Ordered stage of a project. Advanced only by an approved milestone review (P4)."""

    __tablename__ = "project_milestones"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_milestones_project_seq"),
        CheckConstraint(_in("status", MILESTONE_STATUSES), name="ck_project_milestones_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Stage deliverable requirements, a closed rule: {"required_asset_types": ["quote", ...]}.
    requirements: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", nullable=False)
    accepted_review_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("milestone_reviews.id", ondelete="SET NULL", use_alter=True, name="fk_milestones_accepted_review")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = _created()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_tasks_project_number"),
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_tasks_status"),
        CheckConstraint(_in("priority", TASK_PRIORITIES), name="ck_tasks_priority"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_milestones.id", ondelete="SET NULL"))
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    # Required for its milestone's stage review (P4); optional tasks never block advancement.
    required: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="todo", nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_baseline_snapshots.id", ondelete="SET NULL", use_alter=True, name="fk_tasks_current_baseline")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TaskCollaborator(Base):
    __tablename__ = "task_collaborators"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (CheckConstraint("predecessor_id <> successor_id", name="ck_task_dependencies_self"),)
    predecessor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    successor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)


class AcceptanceCriterion(Base):
    """A condition for task acceptance. rule_spec is a closed, explainable rule
    (asset types, required approvals, estimate/simulation policy, consistency
    slot); it never contains executable user code."""

    __tablename__ = "acceptance_criteria"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allowed_types: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    rule_spec: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    manual_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = _created()


class Asset(Base):
    """Catalogue entry for one engineering object. Content lives in the
    specialised tables; versions map one-to-one onto specialised rows."""

    __tablename__ = "assets"
    __table_args__ = (CheckConstraint(_in("type", ASSET_TYPES), name="ck_assets_type"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"), index=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"), unique=True)
    # Stable key for "one asset per part and type" (e.g. part:<id>:part_model) or file:<id>.
    natural_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetVersion(Base):
    __tablename__ = "asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version_no", name="uq_asset_versions_asset_no"),
        CheckConstraint(_in("availability", ASSET_AVAILABILITY), name="ck_asset_versions_availability"),
        CheckConstraint(_in("publish_state", ASSET_PUBLISH), name="ck_asset_versions_publish"),
        # Each work package that adds a target column appends it here and in its migration (W6:
        # drawing_document_version_id, migration a1c0ffee0017 rebuilds the constraint from the live column list).
        CheckConstraint(
            "num_nonnulls(model_revision_id, drawing_id, process_plan_id, quote_id, file_version_id, intake_id, process_module_id, "
            "assembly_revision_id, drawing_document_version_id, assembly_drawing_id, delivery_manifest_id) = 1",
            name="ck_asset_versions_one_target",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_versions.id", ondelete="SET NULL"))
    model_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="CASCADE"), unique=True)
    drawing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drawings.id", ondelete="CASCADE"), unique=True)
    process_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("process_plans.id", ondelete="CASCADE"), unique=True)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"), unique=True)
    file_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="CASCADE"), unique=True)
    intake_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("intakes.id", ondelete="CASCADE"), unique=True)
    process_module_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("process_modules.id", ondelete="CASCADE"), unique=True)
    # W4: assembly_model assets (one asset per assembly, one version per assembly revision).
    assembly_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"), unique=True)
    # W8: delivery_manifest assets (one version per delivery manifest).
    delivery_manifest_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_manifests.id", ondelete="CASCADE"), unique=True)
    # W6 standalone drafting: one asset version per immutable drawing document version.
    drawing_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drawing_document_versions.id", ondelete="CASCADE"), unique=True)
    # W5: assembly_drawing assets (one asset per assembly, one version per drawing package version).
    assembly_drawing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_drawings.id", ondelete="CASCADE"), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    availability: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)
    publish_state: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # simulated / estimated / imported / from_failed_run / mesh_reference …
    labels: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class AssetDependency(Base):
    """Immutable derivation edge, e.g. drawing D4 derived_from model R3."""

    __tablename__ = "asset_dependencies"
    __table_args__ = (CheckConstraint("from_version_id <> to_version_id", name="ck_asset_dependencies_self"),)
    from_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), primary_key=True)
    to_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), primary_key=True, index=True)
    relation: Mapped[str] = mapped_column(String(40), nullable=False)


class TaskBaselineSnapshot(Base):
    __tablename__ = "task_baseline_snapshots"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_task_baselines_task_seq"),
        _actor_check("adopted_by_kind", "adopted_by", "adopted_by_agent_id", "ck_task_baselines_actor"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    adopted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    adopted_by_kind: Mapped[str] = _kind_col()
    adopted_by_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    adopted_at: Mapped[datetime] = _created()


class TaskBaselineItem(Base):
    __tablename__ = "task_baseline_items"
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_baseline_snapshots.id", ondelete="CASCADE"), primary_key=True)
    slot_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id"), nullable=False)


class RunInputBinding(Base):
    __tablename__ = "run_input_bindings"
    __table_args__ = (UniqueConstraint("run_id", "port_key", name="uq_run_inputs_run_port"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    port_key: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


class RunOutput(Base):
    __tablename__ = "run_outputs"
    __table_args__ = (UniqueConstraint("node_run_id", "output_key", "asset_version_id", name="uq_run_outputs_idempotent"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_runs.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    output_key: Mapped[str] = mapped_column(String(60), nullable=False)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = _created()


class TaskDeliverableDraft(Base):
    __tablename__ = "task_deliverable_drafts"
    __table_args__ = (
        UniqueConstraint("task_id", "asset_version_id", name="uq_deliverables_task_version"),
        _actor_check("selected_by_kind", "selected_by", "selected_by_agent_id", "ck_deliverables_actor"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id"), nullable=False)
    criterion_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    selected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    selected_by_kind: Mapped[str] = _kind_col()
    selected_by_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    selected_at: Mapped[datetime] = _created()


class TaskSubmission(Base):
    """Immutable acceptance submission: frozen criteria, versions, checks."""

    __tablename__ = "task_submissions"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_submissions_task_seq"),
        CheckConstraint(_in("status", SUBMISSION_STATUSES), name="ck_submissions_status"),
        Index("uq_submissions_one_pending", "task_id", unique=True, postgresql_where=text("status = 'pending'")),
        _actor_check("submitted_by_kind", "submitted_by", "submitted_by_agent_id", "ck_submissions_actor"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_baseline_snapshots.id"))
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_by_kind: Mapped[str] = _kind_col()
    submitted_by_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    submitted_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubmissionItem(Base):
    __tablename__ = "submission_items"
    id: Mapped[uuid.UUID] = _uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id"), nullable=False)
    criterion_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("acceptance_criteria.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    check_result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ApprovalRequest(Base):
    """A pending decision on an exact target version (asset version or task
    submission). Engineering requests may come from a human_review node."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(_in("kind", REQUEST_KINDS), name="ck_approval_requests_kind"),
        CheckConstraint(_in("status", REQUEST_STATUSES), name="ck_approval_requests_status"),
        CheckConstraint("num_nonnulls(asset_version_id, submission_id, milestone_review_id) = 1", name="ck_approval_requests_one_target"),
        Index("ix_approval_requests_inbox", "project_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(20))  # design / process / quote for engineering requests
    asset_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_versions.id"))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="CASCADE"))
    milestone_review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("milestone_reviews.id", ondelete="CASCADE"))
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), default="policy-2026.10-1", nullable=False)
    # NULL = any member whose role may decide this stage (engineering requests from runs).
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalDecision(Base):
    """Append-only decision record."""

    __tablename__ = "approval_decisions"
    __table_args__ = (_actor_check("actor_kind", "actor_id", "actor_agent_id", "ck_approval_decisions_actor"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_kind: Mapped[str] = _kind_col()
    actor_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Collaboration (design §4 C05): [{criterion_id, result: pass|fail|unknown, reason, evidence_asset_version_ids}]
    criterion_decisions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    created_at: Mapped[datetime] = _created()


class ImpactNotice(Base):
    __tablename__ = "impact_notices"
    __table_args__ = (CheckConstraint(_in("status", ("open", "resolved", "dismissed")), name="ck_impact_notices_status"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="SET NULL"))
    from_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_baseline_snapshots.id", ondelete="SET NULL"))
    to_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_baseline_snapshots.id", ondelete="SET NULL"))
    # Stage-level impact (P4): a reopened task or stage affecting an accepted review.
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_milestones.id", ondelete="SET NULL"))
    milestone_review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("milestone_reviews.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="warning", nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolution_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = _created()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# P3 / P4: stage reviews, process modules, outbox (docs/archive/project-task-asset-design.zh-CN.md)
# ---------------------------------------------------------------------------


class MilestoneReview(Base):
    """Immutable stage review: the frozen set of accepted task submissions
    (plus extra stage deliverables) a stage advancement is based on."""

    __tablename__ = "milestone_reviews"
    __table_args__ = (
        UniqueConstraint("milestone_id", "seq", name="uq_milestone_reviews_seq"),
        CheckConstraint(_in("status", REVIEW_STATUSES), name="ck_milestone_reviews_status"),
        Index("uq_milestone_reviews_one_pending", "milestone_id", unique=True, postgresql_where=text("status = 'pending'")),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    milestone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_milestones.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    submitted_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MilestoneReviewItem(Base):
    __tablename__ = "milestone_review_items"
    __table_args__ = (
        CheckConstraint(_in("kind", ("task_submission", "extra_asset")), name="ck_milestone_review_items_kind"),
        CheckConstraint("num_nonnulls(submission_id, asset_version_id) >= 1", name="ck_milestone_review_items_target"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("milestone_reviews.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id"), index=True)
    asset_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_versions.id"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ProcessModule(Base):
    """Immutable version content of a reusable process module asset (design §6.1, §9.3).
    Documentation only in this version: applicability, parameters, steps and
    validation scope are explicit; nothing here is executed."""

    __tablename__ = "process_modules"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # {"materials": [...], "size_range_mm": {"max": [x, y, z], "min": [x, y, z]?}, "equipment": [...], "prerequisites": "", "limitations": ""}
    applicability: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    parameters: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # {"scope": "...", "evidence": "...", "validated_plan_ids": [...]}
    validation: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    source_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("process_plans.id", ondelete="SET NULL"))
    source_steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class OutboxEvent(Base):
    """Durable event written in the same transaction as the change it reports
    (design §10.3). A dispatcher turns events into notifications idempotently;
    a failed dispatch never undoes the change."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_pending", "processed_at", "created_at"),
        CheckConstraint("(kind = 'ai.operation.completed' AND ai_operation_id IS NOT NULL AND ai_generation IS NOT NULL AND ai_generation > 0 "
                        "AND ai_event_seq IS NOT NULL AND ai_event_seq > 0 AND ai_phase IS NOT NULL AND ai_phase IN ('terminal','applied','undone') AND "
                        "ai_wake_state IS NOT NULL AND ai_wake_state IN ('pending','assigned','consumed','suppressed','delivery_failed','legacy_consumed')) OR "
                        "(kind <> 'ai.operation.completed' AND ai_operation_id IS NULL AND ai_generation IS NULL "
                        "AND ai_event_seq IS NULL AND ai_phase IS NULL AND ai_wake_state IS NULL "
                        "AND ai_wake_turn_id IS NULL AND ai_notice_seq IS NULL AND ai_consumed_at IS NULL)",
                        name="ck_outbox_events_ai_shape"),
        Index("uq_outbox_ai_generation_phase", "ai_operation_id", "ai_generation", "ai_phase", unique=True,
              postgresql_where=text("kind = 'ai.operation.completed'")),
        Index("uq_outbox_ai_sequence", "ai_operation_id", "ai_event_seq", unique=True,
              postgresql_where=text("kind = 'ai.operation.completed'")),
        Index("ix_outbox_ai_pending", "kind", "ai_wake_state", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Frozen service events retain their inputs without consuming retry attempts.
    deferred_feature: Mapped[str | None] = mapped_column(String(40))
    deferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_operations.id", ondelete="CASCADE"), index=True)
    ai_generation: Mapped[int | None] = mapped_column(Integer)
    ai_event_seq: Mapped[int | None] = mapped_column(Integer)
    ai_phase: Mapped[str | None] = mapped_column(String(20))
    ai_wake_state: Mapped[str | None] = mapped_column(String(30))
    ai_wake_turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_operations.id", ondelete="SET NULL"), index=True)
    ai_notice_seq: Mapped[int | None] = mapped_column(Integer)
    ai_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_notifications_event_user"),
        Index("ix_notifications_inbox", "user_id", "read_at", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_events.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    link: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Agent collaboration workflow (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §2)
# ---------------------------------------------------------------------------


class AgentProfile(Base):
    """Project-scoped agent: purpose, model, allowed tools, execution modes,
    knowledge scope and budget limits (design §3.2). Running instances keep
    their own snapshot (collaboration_runs.role_snapshots); editing a profile
    never changes them."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_agent_profiles_project_name"),
        CheckConstraint(_in("purpose", AGENT_PURPOSES), name="ck_agent_profiles_purpose"),
        CheckConstraint(_in("runtime", AGENT_RUNTIMES), name="ck_agent_profiles_runtime"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    role_prompt: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # NULL = the default model config at run time; never a credential.
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"))
    allowed_tools: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, server_default="{}", nullable=False)
    execution_modes: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list, server_default="{}", nullable=False)
    knowledge_scope: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # {max_steps, max_duration_s, max_attempts, max_reworks, max_cost_fen|null} (services.agent_profiles.AgentLimits)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # {in_fen_per_1k, out_fen_per_1k}; NULL = cost unknown (never shown as 0).
    pricing: Mapped[dict | None] = mapped_column(JSONB)
    # New profiles use native/tool_agent with closed options (services.agent_profiles.RUNTIME_OPTIONS).
    # workspace_agent and its saved options remain readable for historical records.
    runtime: Mapped[str] = mapped_column(String(20), default="native", server_default="native", nullable=False)
    runtime_options: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollaborationDraft(Base):
    """Planning state of a collaboration workflow (one per kind=collaboration
    workflow): the demand, mode, default actors, fixed inputs, missing facts
    and validation errors. Started instances never follow the draft."""

    __tablename__ = "collaboration_drafts"
    __table_args__ = (
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_collab_drafts_policy"),
        CheckConstraint(_in("execution_mode", EXECUTION_MODES), name="ck_collab_drafts_mode"),
        CheckConstraint(_in("planning_source", PLANNING_SOURCES), name="ck_collab_drafts_source"),
        CheckConstraint(_in("executor_kind", ACTOR_KINDS), name="ck_collab_drafts_executor_kind"),
        CheckConstraint(_in("reviewer_kind", ACTOR_KINDS), name="ck_collab_drafts_reviewer_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), unique=True, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    demand: Mapped[str] = mapped_column(Text, default="", nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # Chosen before planning; changing it makes a new draft version and re-plans (architecture §3).
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    planning_source: Mapped[str] = mapped_column(String(10), default="ai", server_default="ai", nullable=False)
    planner_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    # Default executor / final reviewer for every node (per-node overrides live in the graph params).
    executor_kind: Mapped[str] = _kind_col()
    executor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    executor_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    reviewer_kind: Mapped[str] = _kind_col()
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewer_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    pre_reviewer_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    # [{slot_key, asset_version_id, content_hash, label}]
    fixed_inputs: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    knowledge_item_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list, server_default="{}", nullable=False)
    # [{field, question, node_id|null}] — facts the planner could not find; the draft cannot start while any exist.
    missing_information: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Turbo (W7): values the planner adopted instead of asking (policy.Assumption dumps); frozen into the run contract.
    assumptions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Last planning record: {provider, model_id, simulated, ai_call_ids, summary, at}
    planning: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    ready_to_start: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CollaborationRun(Base):
    """Collaboration instance metadata, one-to-one with a workflow run: the
    frozen mode and contract, role snapshots, pause / fork control and the
    archive state (design §2.2, §3.5)."""

    __tablename__ = "collaboration_runs"
    __table_args__ = (
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_collab_runs_policy"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_collab_runs_idempotency"),
        CheckConstraint(_in("execution_mode", EXECUTION_MODES), name="ck_collab_runs_mode"),
        CheckConstraint(_in("archive_status", ARCHIVE_STATUSES), name="ck_collab_runs_archive"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflows.id", ondelete="SET NULL"))
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_template_versions.id", ondelete="SET NULL"))
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collaboration_drafts.id", ondelete="SET NULL"))
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    contract: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # {agent_profile_id: profile snapshot incl. version} at start; profile edits never reach a running instance.
    role_snapshots: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Optimistic version of the control operations (pause / resume / fork).
    control_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(Text)
    forked_from_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    fork_reason: Mapped[str | None] = mapped_column(Text)
    archive_status: Mapped[str] = mapped_column(String(20), default="none", server_default="none", nullable=False)
    archive_error: Mapped[str | None] = mapped_column(Text)
    knowledge_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_collections.id", ondelete="SET NULL", use_alter=True, name="fk_collab_runs_knowledge_collection")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CollaborationNodeTask(Base):
    """One business node of a collaboration run = one Task (design §0, §3.6).
    `phase` is the node's internal stage; the business status is the Task's."""

    __tablename__ = "collaboration_node_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_collab_node_tasks_run_node"),
        CheckConstraint(_in("phase", NODE_TASK_PHASES), name="ck_collab_node_tasks_phase"),
        CheckConstraint(_in("executor_kind", ACTOR_KINDS), name="ck_collab_node_tasks_executor_kind"),
        CheckConstraint(_in("reviewer_kind", ACTOR_KINDS), name="ck_collab_node_tasks_reviewer_kind"),
        Index("ix_collab_node_tasks_queue", "phase", "lease_until"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(60), nullable=False)
    node_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_runs.id", ondelete="CASCADE"), unique=True, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, nullable=False)
    executor_kind: Mapped[str] = _kind_col()
    executor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    executor_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    reviewer_kind: Mapped[str] = _kind_col()
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewer_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    pre_reviewer_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    input_slots: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    output_slots: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    recipe: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Frozen from the executor's profile at start (design §3.9).
    limits: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), default="blocked", server_default="blocked", nullable=False)
    blocked_reason: Mapped[dict | None] = mapped_column(JSONB)
    # {steps, duration_ms, attempts, reworks, cost_fen|null, cost_unknown} — cumulative, never reset.
    budget: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    current_submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="SET NULL"))
    accepted_submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="SET NULL"))
    # [{slot_key, asset_version_id, content_hash, from_node_id|null}] / [{slot_key, asset_version_id, content_hash}]
    bound_inputs: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Answers a person gave to the questions an agent was blocked on: [{question, answer, by, at}]. The next attempt's
    # state and the CAD tools receive them (as a workflow node receives its clarification answers).
    clarifications: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    formal_outputs: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AgentExecution(Base):
    """One agent attempt on a node: an execution (rework_no counts business
    reworks, attempt_no technical attempts), a pre-review or a final review.
    Results are written back only under the lease token (design §0, §3.9)."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        CheckConstraint(_in("role", AGENT_EXECUTION_ROLES), name="ck_agent_executions_role"),
        CheckConstraint(_in("status", AGENT_EXECUTION_STATUSES), name="ck_agent_executions_status"),
        CheckConstraint(f"runtime IS NULL OR {_in('runtime', AGENT_RUNTIMES)}", name="ck_agent_executions_runtime"),
        Index("ix_agent_executions_node_task", "node_task_id", "created_at"),
        Index("ix_agent_executions_profile_created", "agent_profile_id", "created_at"),
        Index("ux_agent_executions_mcp_token_hash", "mcp_token_hash", unique=True, postgresql_where=text("mcp_token_hash IS NOT NULL")),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collaboration_node_tasks.id", ondelete="CASCADE"), nullable=False)
    agent_profile_id: Mapped[uuid.UUID | None] = _agent_col()
    profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rework_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    tool_calls: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    # {tokens_in, tokens_out, calls, cost_fen|null, cost_unknown}
    usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="SET NULL"))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approval_decisions.id", ondelete="SET NULL"))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_executions.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    # P1: the executor that ran the attempt (NULL before P1), its framework and version, its session id, and when a
    # person asked to stop it (the node is then blocked agent_stopped and the attempt's result is history only).
    runtime: Mapped[str | None] = mapped_column(String(20))
    runtime_impl: Mapped[str | None] = mapped_column(String(60))
    runtime_ref: Mapped[str | None] = mapped_column(String(200))
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Historical retired-runtime token metadata. Kept for old execution records;
    # new attempts never issue MCP tokens.
    mcp_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    mcp_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentExecutionEvent(Base):
    """One timeline event of an attempt (design §6.4): written under the lease in its own transaction, read by
    seq for the SSE stream and its resume. Payloads never hold prompts or keys; every event carries ``simulated``."""

    __tablename__ = "agent_execution_events"
    __table_args__ = (
        CheckConstraint(_in("type", AGENT_EVENT_TYPES), name="ck_agent_execution_events_type"),
        UniqueConstraint("execution_id", "seq", name="uq_agent_execution_events_seq"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_executions.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowResource(Base):
    """A use of an exact asset version in a collaboration run: input, candidate,
    formal, failed, superseded or evidence (design §2.3, §3.10). Rows are
    immutable; losing the formal status adds a `superseded` row."""

    __tablename__ = "workflow_resources"
    __table_args__ = (
        CheckConstraint(_in("kind", RESOURCE_KINDS), name="ck_workflow_resources_kind"),
        Index("ix_workflow_resources_project_created", "project_id", "created_at", "id"),
        Index("ix_workflow_resources_run_kind", "run_id", "kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    node_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collaboration_node_tasks.id", ondelete="SET NULL"))
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    agent_execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_executions.id", ondelete="SET NULL"))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_submissions.id", ondelete="SET NULL"))
    attempt: Mapped[int | None] = mapped_column(Integer)
    slot_key: Mapped[str | None] = mapped_column(String(80))
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # "metadata" is reserved on declarative classes; the column keeps the contract name.
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default="{}", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class KnowledgeCollection(Base):
    """Archive of one completed collaboration run (design §2.3, §4 C09)."""

    __tablename__ = "knowledge_collections"
    __table_args__ = (
        CheckConstraint(_in("status", COLLECTION_STATUSES), name="ck_knowledge_collections_status"),
        CheckConstraint(_in("index_status", INDEX_STATUSES), name="ck_knowledge_collections_index"),
        CheckConstraint(_in("created_by_kind", ACTOR_KINDS), name="ck_knowledge_collections_actor_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_collection_versions.id", ondelete="SET NULL", use_alter=True, name="fk_knowledge_collections_current_version")
    )
    index_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    index_error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_by_kind: Mapped[str] = _kind_col()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeCollectionVersion(Base):
    """Immutable manifest of an archive; the same manifest never makes a new version."""

    __tablename__ = "knowledge_collection_versions"
    __table_args__ = (
        UniqueConstraint("collection_id", "version_no", name="uq_knowledge_versions_no"),
        UniqueConstraint("collection_id", "manifest_hash", name="uq_knowledge_versions_hash"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    collection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_collections.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[datetime] = _created()


class KnowledgeItem(Base):
    """One archived resource of a manifest version; `reusable` = a currently
    valid formal output. Index state is independent of the file."""

    __tablename__ = "knowledge_items"
    __table_args__ = (
        UniqueConstraint("collection_version_id", "resource_id", name="uq_knowledge_items_version_resource"),
        CheckConstraint(_in("kind", ITEM_KINDS), name="ck_knowledge_items_kind"),
        CheckConstraint(_in("index_status", ITEM_INDEX_STATUSES), name="ck_knowledge_items_index"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    collection_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_collection_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_resources.id", ondelete="SET NULL"))
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    node_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collaboration_node_tasks.id", ondelete="SET NULL"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    slot_key: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reusable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    index_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class KnowledgeUsage(Base):
    """A reference to, or baseline adoption of, an archived item by a new task.
    Adoption fixes the source; it never carries the source's approvals."""

    __tablename__ = "knowledge_usages"
    __table_args__ = (
        CheckConstraint(_in("usage", KNOWLEDGE_USAGES), name="ck_knowledge_usages_usage"),
        CheckConstraint(_in("confirmation", USAGE_CONFIRMATIONS), name="ck_knowledge_usages_confirmation"),
        CheckConstraint(_in("used_by_kind", ACTOR_KINDS), name="ck_knowledge_usages_actor_kind"),
        Index("ix_knowledge_usages_target", "target_project_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_items.id", ondelete="CASCADE"), nullable=False, index=True)
    collection_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_collection_versions.id", ondelete="CASCADE"), nullable=False)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_versions.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    target_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    target_node_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collaboration_node_tasks.id", ondelete="SET NULL"))
    target_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    slot_key: Mapped[str | None] = mapped_column(String(80))
    usage: Mapped[str] = mapped_column(String(20), nullable=False)
    baseline_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_baseline_snapshots.id", ondelete="SET NULL"))
    used_by_kind: Mapped[str] = _kind_col()
    used_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_by_agent_id: Mapped[uuid.UUID | None] = _agent_col()
    reason: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    confirmation: Mapped[str] = mapped_column(String(20), default="none", server_default="none", nullable=False)
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# --- unified AI workspace (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §2) -------------
# A conversation is one person's record of work in one project (optionally a
# task). Each request message snapshots its target and references; an
# operation is one traceable request (it may start a run); a context build
# records what one model call was given. Runs, AI calls and messages keep
# their own jobs.


class AiConversation(Base):
    __tablename__ = "ai_conversations"
    __table_args__ = (Index("ix_ai_conversations_user_project", "user_id", "project_id", "last_activity_at"),
                      CheckConstraint(_in("autonomy", AI_AUTONOMY), name="ck_ai_conversations_autonomy"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    # {"target_key": str | None, "references": [AiRef]} — what the next request will use.
    composer: Mapped[dict] = mapped_column(JSONB, default=lambda: {"target_key": None, "references": []},
                                           server_default=text("""'{"target_key": null, "references": []}'::jsonb"""), nullable=False)
    constraints: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    summary_through_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_seq: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    forked_from: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    autonomy: Mapped[str] = mapped_column(String(20), default="confirm_task", server_default="confirm_task", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AiMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_ai_messages_conversation_seq"),
        CheckConstraint(_in("role", AI_MESSAGE_ROLES), name="ck_ai_messages_role"),
        CheckConstraint(_in("kind", AI_MESSAGE_KINDS), name="ck_ai_messages_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # Request snapshot (user messages): never changed after the message is written.
    target: Mapped[dict | None] = mapped_column(JSONB)
    references: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_operations.id", ondelete="SET NULL", use_alter=True, name="fk_ai_messages_operation"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = _created()


class AiOperation(Base):
    __tablename__ = "ai_operations"
    __table_args__ = (
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_ai_operations_policy"),
        UniqueConstraint("conversation_id", "client_request_id", name="uq_ai_operations_request"),
        CheckConstraint(_in("kind", AI_OPERATION_KINDS), name="ck_ai_operations_kind"),
        CheckConstraint("generation > 0", name="ck_ai_operations_generation"),
        CheckConstraint(_in("status", AI_OPERATION_STATUSES), name="ck_ai_operations_status"),
        Index("ix_ai_operations_part_status", "part_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_messages.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    client_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    question: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    retry_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_operations.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiDesignPlanVersion(Base):
    """Immutable AI-authored design data, separate from human source and approval."""

    __tablename__ = "ai_design_plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_ai_design_plan_version"),
        UniqueConstraint("root_operation_id", "choice_id", name="uq_ai_design_plan_choice"),
        CheckConstraint("version > 0", name="ck_ai_design_plan_version"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_design_plan_versions.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    root_operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id"), nullable=False, index=True)
    authoring_operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id"), nullable=False)
    choice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_design_choices.id"))
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    authorization: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class WorkflowDesignPlanVersion(Base):
    """Immutable concept authority for one exact workflow node and original text input."""

    __tablename__ = "workflow_design_plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_workflow_design_plan_version"),
        UniqueConstraint("node_run_id", name="uq_workflow_design_plan_node"),
        CheckConstraint("version = 1", name="ck_workflow_design_plan_version"),
        CheckConstraint("authoring_attempt > 0", name="ck_workflow_design_plan_attempt"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    node_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("node_runs.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(60), nullable=False)
    authoring_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    authorization: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class AiDesignChoice(Base):
    """An immutable authenticated response to one exact design question set."""

    __tablename__ = "ai_design_choices"
    __table_args__ = (
        UniqueConstraint("root_operation_id", "client_request_id", name="uq_ai_design_choice_request"),
        UniqueConstraint("plan_version_id", name="uq_ai_design_choice_version"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    root_operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id"), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    client_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_overrides: Mapped[list] = mapped_column(JSONB, nullable=False)
    resume_operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class AiContextBuild(Base):
    """What one model call was given: layers, included / left-out items, budget (estimated) and usage (reported)."""

    __tablename__ = "ai_context_builds"
    id: Mapped[uuid.UUID] = _uuid_pk()
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_operations.id", ondelete="CASCADE"), nullable=False, index=True)
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"))
    model_label: Mapped[str] = mapped_column(String(160), default="", server_default="", nullable=False)
    budget: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    items: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)
    summary_through_seq: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    # The private part of a modeling context (pinned constraints, conversation memory, other references)
    # as JSON text. Read only by the model_3d handler of the operation's own run; never returned by an API.
    private_context: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class AiTaskDraft(Base):
    """A task draft the main agent stored (AI native, design §1.8): private to its conversation, editable while ``open``,
    turned into a task once (``used``, ``task_id``). ``operation_id`` / ``step_index``: the turn and step that made it
    (unique: a replayed step finds its draft). ``criteria``: the shape ``stages.draft_task`` returns."""

    __tablename__ = "ai_task_drafts"
    __table_args__ = (
        UniqueConstraint("operation_id", "step_index", name="uq_ai_task_drafts_step"),
        CheckConstraint(_in("status", AI_TASK_DRAFT_STATUSES), name="ck_ai_task_drafts_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_operations.id", ondelete="SET NULL"))
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    criteria: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    suggested_workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflows.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# =====================================================================================================
# General mechanical engineering — new tables, one section per work package. Add your package's
# classes directly under its own marker only, so parallel work merges cleanly (W0 contract §6).
# =====================================================================================================

# ---- W1 model credentials -----------------------------------------------------------------------------


class ModelCredential(Base):
    """One credential version of a model configuration (architecture §7). An env credential names the server
    variable (``env_ref``); a direct one holds the AES-256-GCM ciphertext of the key (ai/credentials.py), never the
    key. Old versions stay for frozen snapshots (replaced) or as history (cleared / revoked, ciphertext wiped)."""

    __tablename__ = "model_credentials"
    __table_args__ = (
        UniqueConstraint("model_config_id", "version", name="uq_model_credentials_version"),
        CheckConstraint(_in("source", CREDENTIAL_SOURCES), name="ck_model_credentials_source"),
        CheckConstraint(_in("status", CREDENTIAL_STATUSES), name="ck_model_credentials_status"),
        CheckConstraint("(source = 'env' AND env_ref IS NOT NULL AND ciphertext IS NULL) OR (source = 'direct' AND env_ref IS NULL)",
                        name="ck_model_credentials_payload"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    model_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    env_ref: Mapped[str | None] = mapped_column(String(200))
    algorithm: Mapped[str | None] = mapped_column(String(30))
    cipher_format: Mapped[int | None] = mapped_column(Integer)
    key_version: Mapped[str | None] = mapped_column(String(32))
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rekeyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelEnvGeneration(Base):
    """Server-side generations of an environment key: a keyed fingerprint of each value seen behind a reference
    → a generation number. Snapshots pin the generation; a different value is ``credential_changed``. Neither the
    value nor the fingerprint is ever returned by an API."""

    __tablename__ = "model_env_generations"
    __table_args__ = (
        UniqueConstraint("env_ref", "fingerprint", name="uq_model_env_generations_fp"),
        UniqueConstraint("env_ref", "generation", name="uq_model_env_generations_gen"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    env_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = _created()


class ModelCredentialMeta(Base):
    """Single row (id 1): the random salt of the environment-key fingerprints, created on first use."""

    __tablename__ = "model_credential_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint_salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _created()


# ---- W2 engineering spec ------------------------------------------------------------------------------
# A spec is a project-scoped lineage of immutable versions. A version freezes its sources (exact file
# version ids + sha256, direct text as a blob), the chunks the server actually sent to the model, the
# policy, the provider / calls and the spec JSON (W0 contract §5). Answers and re-extraction always make a
# new version (services/engineering_spec.py).

SPEC_VERSION_STATUSES = ("extracting", "ready", "needs_answers", "failed")
SPEC_VERSION_ORIGINS = ("create", "answer", "reextract", "continue", "rule")  # continue: S3, a failed extraction resumed; rule: a
# server rule derived it from a finished version (generation supplement A-03)
SPEC_CHUNK_STATUSES = ("pending", "sent", "failed", "unread")
SPEC_CHUNK_KINDS = ("text", "drawing", "attachment")


class EngineeringSpec(Base):
    __tablename__ = "engineering_specs"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_engineering_specs_project_number"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EngineeringSpecVersion(Base):
    __tablename__ = "engineering_spec_versions"
    __table_args__ = (
        UniqueConstraint("spec_id", "version_no", name="uq_engineering_spec_versions_no"),
        CheckConstraint(_in("status", SPEC_VERSION_STATUSES), name="ck_engineering_spec_versions_status"),
        CheckConstraint(_in("origin", SPEC_VERSION_ORIGINS), name="ck_engineering_spec_versions_origin"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_engineering_spec_versions_policy"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("engineering_specs.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="extracting", server_default="extracting", nullable=False)
    # Model config the request named (None: the default for the purpose); the one really used is in `planning`.
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"))
    # Spec JSON (W0 contract §5); empty until extraction finished.
    content: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # Source manifest: [{id, kind, label, file_version_id, file_id, version, name, sha256, bytes, content_type,
    #                    read_status, ranges, error, chunks, answer_of}] — read status is computed from the chunks.
    sources: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Business checks run on this version, e.g. [{"check": "spec.clarification_gate", "status": "pending", ...}].
    checks: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Server validation of the model output: {problems, repairs, demoted, converted}.
    validation: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # {provider, model_id, model_config_id, simulated, prompt_version, ai_call_ids, chunks_total, chunks_sent, repairs}
    planning: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # {stage, code, message, detail, call_ids, retryable, recovery}; stage: context_build | provider_select |
    # provider_call | parse | validate | over_budget | interrupted.
    error: Mapped[dict | None] = mapped_column(JSONB)
    # Answers applied by this version: [{question_id, answer, by, at, source}] (person answers, never assumptions).
    answers: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    # Set while extracting; a version whose heartbeat is too old is reported as interrupted.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EngineeringSpecChunk(Base):
    """One chunk of one source of a version, and whether the server really sent it to the model.

    ``status`` is written by the server from the provider call result (``sent`` + ``ai_call_id``), never from
    the model's own claim. ``inherited_from_version_id`` marks chunks an answer version reuses from the
    version that read them. The chunk text is stored content-addressed (``text_sha256`` → blobs)."""

    __tablename__ = "engineering_spec_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "seq", name="uq_engineering_spec_chunks_seq"),
        CheckConstraint(_in("status", SPEC_CHUNK_STATUSES), name="ck_engineering_spec_chunks_status"),
        CheckConstraint(_in("kind", SPEC_CHUNK_KINDS), name="ck_engineering_spec_chunks_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based within the version (the "chunk N" of locators)
    source_id: Mapped[str] = mapped_column(String(20), nullable=False)
    source_chunk: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based within its source
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    locator: Mapped[str] = mapped_column(String(300), nullable=False)
    start: Mapped[int | None] = mapped_column(Integer)  # text: character offset; drawing: first evidence line
    end: Mapped[int | None] = mapped_column(Integer)
    chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(60))
    ai_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_calls.id", ondelete="SET NULL"))
    inherited_from_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="SET NULL"))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # S3: what the model reported for this chunk (the reply that passed the schema) and the call that returned it
    # (after repairs this is a later call than ``ai_call_id``, the first call that carried the chunk). NULL: no usable
    # reply yet. A continuation reuses sent chunks with findings instead of sending them again.
    findings: Mapped[dict | None] = mapped_column(JSONB)
    call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_calls.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


# ---- W3 geometry library ------------------------------------------------------------------------------


# ---- W4 static assemblies -----------------------------------------------------------------------------
# Definitions are separate from instances: an assembly revision freezes its direct children, each one an
# exact part model revision or an exact child assembly revision; nested structure comes from child
# assembly revisions (reused, never copied). Revisions are immutable; edits create a new revision.

ASSEMBLY_REVISION_SOURCES = ("manual", "import", "ai", "copy")
ASSEMBLY_REVISION_STATUSES = ("ready", "failed")
ASSEMBLY_DEFINITION_KINDS = ("part", "assembly")
MATE_KINDS = ("fixed", "coaxial", "planar", "parallel", "perpendicular", "offset", "angle", "pattern_match")
MATE_STATUSES = ("pending", "solved", "redundant", "conflict", "broken", "invalid", "dropped", "suppressed", "skipped_turbo")


class Assembly(Base):
    __tablename__ = "assemblies"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_assemblies_project_number"),
        CheckConstraint(_in("status", PART_STATUSES), name="ck_assemblies_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft", nullable=False)
    # Highest instance / mate number ever issued, so stable ids are never reused across revisions.
    instance_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    mate_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssemblyRevision(Base):
    """One immutable baseline of an assembly definition (its direct children, placements and mates).

    ``step_sha`` / ``parts_zip_sha`` record the latest export (names / BOM properties are read live, so the
    service caches exports by those too, never by these columns alone); ``checks`` collects the
    check records run against this exact revision (append-only per check name)."""

    __tablename__ = "assembly_revisions"
    __table_args__ = (
        UniqueConstraint("assembly_id", "rev_no", name="uq_assembly_revisions_rev"),
        CheckConstraint(_in("source", ASSEMBLY_REVISION_SOURCES), name="ck_assembly_revisions_source"),
        CheckConstraint(_in("status", ASSEMBLY_REVISION_STATUSES), name="ck_assembly_revisions_status"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_assembly_revisions_policy"),
        CheckConstraint(_in("check_status", CHECK_STATUSES), name="ck_assembly_revisions_check"),
        CheckConstraint(_in("acceptance_status", ACCEPTANCE_STATUSES), name="ck_assembly_revisions_acceptance"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_assembly_revisions_parent"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    assembly_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assemblies.id", ondelete="CASCADE"), nullable=False, index=True)
    rev_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ready", server_default="ready", nullable=False)
    check_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested", nullable=False)
    acceptance_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested", nullable=False)
    # Canonical hash of the frozen content (children, placements, mates): the asset content hash.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Engineering spec version this revision was built from (W2 table; plain id, no FK across packages).
    spec_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    spec_ref: Mapped[dict | None] = mapped_column(JSONB)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    solve: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    checks: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    verification: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    assumptions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    bom: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    bom_sha256: Mapped[str | None] = mapped_column(String(64))
    step_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    parts_zip_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    source_file_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("file_versions.id", ondelete="SET NULL"))
    # Import / solver / edit notes ({code, message, subject, details}); never silently dropped content.
    notes: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    change: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class AssemblyComponent(Base):
    """One direct child instance of an assembly revision. ``instance_id`` is stable across revisions."""

    __tablename__ = "assembly_components"
    __table_args__ = (
        UniqueConstraint("revision_id", "instance_id", name="uq_assembly_components_instance"),
        CheckConstraint(_in("definition_kind", ASSEMBLY_DEFINITION_KINDS), name="ck_assembly_components_kind"),
        CheckConstraint(
            "(definition_kind = 'part' AND model_revision_id IS NOT NULL AND part_id IS NOT NULL AND child_revision_id IS NULL "
            "AND child_assembly_id IS NULL) OR (definition_kind = 'assembly' AND child_revision_id IS NOT NULL "
            "AND child_assembly_id IS NOT NULL AND model_revision_id IS NULL AND part_id IS NULL)",
            name="ck_assembly_components_definition",
        ),
        # reverse references: where is a part / part revision / assembly / assembly revision used
        Index("ix_assembly_components_model_revision", "model_revision_id"),
        Index("ix_assembly_components_part", "part_id"),
        Index("ix_assembly_components_child_revision", "child_revision_id"),
        Index("ix_assembly_components_child_assembly", "child_assembly_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    definition_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id"))
    model_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id"))
    child_assembly_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assemblies.id"))
    child_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_revisions.id"))
    # Rigid 4x4 row-major local transform (child frame -> this assembly's frame), cad/assembly_model.py.
    transform: Mapped[list] = mapped_column(JSONB, nullable=False)
    fixed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # {"mode": "explicit" | "mated" | "unplaced", "source": "import" | "user" | "spec" | "pattern" | "solver" | ...}
    placement: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    display: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)


class AssemblyMate(Base):
    """A static mate between semantic datums of two direct children (``b`` is null for fixed / ground).

    ``a`` / ``b`` = {"instance": id, "datum": name, "definition_revision_id": the exact definition the datum
    was bound to}. ``status`` / ``result`` are the solver outcome for this revision."""

    __tablename__ = "assembly_mates"
    __table_args__ = (
        UniqueConstraint("revision_id", "mate_id", name="uq_assembly_mates_mate"),
        CheckConstraint(_in("kind", MATE_KINDS), name="ck_assembly_mates_kind"),
        CheckConstraint(_in("status", MATE_STATUSES), name="ck_assembly_mates_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    mate_id: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    a: Mapped[dict] = mapped_column(JSONB, nullable=False)
    b: Mapped[dict | None] = mapped_column(JSONB)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="user", server_default="user", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)


class AssemblyPartProps(Base):
    """BOM attributes of a part used in assemblies: procurement (make / buy / standard / unknown) and
    representation (exact / nominal / simplified). Missing row = unknown procurement, exact representation."""

    __tablename__ = "assembly_part_props"
    __table_args__ = (
        CheckConstraint(_in("procurement", PROCUREMENT_KINDS), name="ck_assembly_part_props_procurement"),
        CheckConstraint(_in("representation", REPRESENTATION_LEVELS), name="ck_assembly_part_props_representation"),
    )
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parts.id", ondelete="CASCADE"), primary_key=True)
    procurement: Mapped[str] = mapped_column(String(20), default="unknown", server_default="unknown", nullable=False)
    representation: Mapped[str] = mapped_column(String(20), default="exact", server_default="exact", nullable=False)
    designation: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    source: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AssemblyRollup(Base):
    """Process / quote roll-up of one frozen assembly revision (its BOM x the parts' plans and quotes)."""

    __tablename__ = "assembly_rollups"
    __table_args__ = (CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_assembly_rollups_policy"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    bom_sha256: Mapped[str | None] = mapped_column(String(64))
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


# ---- W5 assembly drawings -----------------------------------------------------------------------------
# A drawing package (GA + detail sheets) of one exact assembly revision. Rows are immutable: a layout /
# annotation overlay creates the next version of the same package (``parent_id`` = the version it edited,
# one child per version so a lineage stays linear). Every page file and check report is a blob; ``pages``
# keeps the page records (views, dimensions, balloons, hatches, BOM rows) and each file's blob sha256.

PACKAGE_COMPLETENESS = ("complete", "partial")


class AssemblyDrawing(Base):
    __tablename__ = "assembly_drawings"
    __table_args__ = (
        UniqueConstraint("assembly_id", "number", "version", name="uq_assembly_drawings_version"),
        UniqueConstraint("parent_id", name="uq_assembly_drawings_parent"),
        UniqueConstraint("resumed_from_id", name="uq_assembly_drawings_resumed_from"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_assembly_drawings_policy"),
        CheckConstraint(_in("completeness", PACKAGE_COMPLETENESS), name="ck_assembly_drawings_completeness"),
        CheckConstraint(_in("check_status", CHECK_STATUSES), name="ck_assembly_drawings_check"),
        CheckConstraint("version >= 1 AND number >= 1", name="ck_assembly_drawings_numbers"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_assembly_drawings_parent"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    assembly_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assemblies.id", ondelete="CASCADE"), nullable=False, index=True)
    assembly_revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"), nullable=False,
                                                            index=True)
    # Package number within the assembly (AD<number>); version within the package (1 = generated, +1 per overlay).
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_drawings.id", ondelete="SET NULL"))
    execution_policy: Mapped[str] = mapped_column(String(20), nullable=False)
    completeness: Mapped[str] = mapped_column(String(20), nullable=False)
    check_status: Mapped[str] = mapped_column(String(20), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    overlay: Mapped[dict | None] = mapped_column(JSONB)
    pages: Mapped[list] = mapped_column(JSONB, nullable=False)
    documents: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bom_sha256: Mapped[str | None] = mapped_column(String(64))
    checks: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    verification: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    assumptions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    warnings: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Wall-clock time, budgets and the DWG runtime of the generation (kept out of the deterministic manifest).
    generation: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    # S3: the partial package this one continued (its complete documents / pages were reused, only the failed ones were
    # generated again; ``generation.progress`` says which). One continuation per package.
    resumed_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_drawings.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


# ---- W6 standalone drafting ---------------------------------------------------------------------------
# Standalone 2D drawing documents (capabilities §5, architecture §2 DrawingDocumentVersion). A document belongs
# to a project only: no part, no model revision. Versions are immutable (a trigger refuses UPDATE); every save,
# restore or calibration writes a new version against the head (optimistic concurrency, 409 on a stale base).

DRAWING_DOCUMENT_STATUSES = ("draft", "released")
DRAFTING_ORIGINS = ("empty", "text", "file", "image", "edit", "restore", "calibrate")
# Scale of the drawing content: native = units declared by the source (DXF $INSUNITS, text in mm); an image
# underlay is unknown until calibrated (two points + a known distance) or assumed (Turbo, with an Assumption).
SCALE_STATUSES = ("unknown", "calibrated", "assumed", "native")
# DXF is always written (else nothing is saved); partial = the DWG could not be written (evidence kept).
DRAFTING_OUTPUT_STATUSES = ("succeeded", "partial")


class DrawingDocument(Base):
    __tablename__ = "drawing_documents"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_drawing_documents_project_number"),
        CheckConstraint(_in("status", DRAWING_DOCUMENT_STATUSES), name="ck_drawing_documents_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft", nullable=False)
    head_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drawing_document_versions.id", ondelete="SET NULL", use_alter=True, name="fk_drawing_documents_head"))
    head_version_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DrawingDocumentVersion(Base):
    """One immutable version: frozen sources, sheets, layers, entities (stable ids), blocks, underlays, scale state,
    per-annotation provenance, conflicts, assumptions, the unsupported-object inventory, checks, files and manifest."""

    __tablename__ = "drawing_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_drawing_document_versions_no"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_drawing_document_versions_policy"),
        CheckConstraint(_in("origin", DRAFTING_ORIGINS), name="ck_drawing_document_versions_origin"),
        CheckConstraint(_in("scale_status", SCALE_STATUSES), name="ck_drawing_document_versions_scale"),
        CheckConstraint(_in("check_status", CHECK_STATUSES), name="ck_drawing_document_versions_check"),
        CheckConstraint(_in("acceptance_status", ACCEPTANCE_STATUSES), name="ck_drawing_document_versions_acceptance"),
        CheckConstraint(_in("output_status", DRAFTING_OUTPUT_STATUSES), name="ck_drawing_document_versions_output"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drawing_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drawing_document_versions.id"))
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drawing_document_versions.id"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    # Inputs: [{id, kind: text|file_version|image, file_version_id, name, sha256, format, ...}]
    sources: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    sheets: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    layers: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    entities: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    blocks: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    underlays: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    next_entity_no: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    scale_status: Mapped[str] = mapped_column(String(20), nullable=False)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # entity id -> {original reading, measured value, person override, AI assumption} (kept separately)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    conflicts: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    assumptions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    open_questions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # Source objects that could not be mapped: [{type, count, preserved, handling, ...}]; never dropped silently.
    unsupported: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    commands: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    check_status: Mapped[str] = mapped_column(String(20), nullable=False)
    acceptance_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested", nullable=False)
    checks: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    verification: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    output_status: Mapped[str] = mapped_column(String(20), nullable=False)
    files: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    dwg: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    dxf_sha: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"), nullable=False)
    dwg_sha: Mapped[str | None] = mapped_column(ForeignKey("blobs.sha256"))
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # simulated / imported / assumed …: source labels travel with the version and its asset version.
    labels: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, server_default="{}", nullable=False)
    ai: Mapped[dict | None] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()



# ---- W7 turbo runtime ---------------------------------------------------------------------------------

ARTIFACT_CHECK_SUBJECTS = ("model_revision", "drawing", "assembly_revision", "engineering_spec_version")
# Exactly one target column per subject type (S5 added assembly revisions and spec versions: assumption decisions).
ARTIFACT_CHECK_TARGETS = (
    "(subject_type = 'model_revision' AND model_revision_id IS NOT NULL AND drawing_id IS NULL "
    "AND assembly_revision_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'drawing' AND drawing_id IS NOT NULL AND model_revision_id IS NULL "
    "AND assembly_revision_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'assembly_revision' AND assembly_revision_id IS NOT NULL AND model_revision_id IS NULL "
    "AND drawing_id IS NULL AND spec_version_id IS NULL) OR "
    "(subject_type = 'engineering_spec_version' AND spec_version_id IS NOT NULL AND model_revision_id IS NULL "
    "AND drawing_id IS NULL AND assembly_revision_id IS NULL)"
)


class ArtifactCheck(Base):
    """An engineering check run later on one exact artifact (AC-T08), or a person's decision on one of its assumptions:
    append-only. The check recorded when the artifact was created (e.g. ``skipped_turbo`` in ``ModelRevision.checks``)
    is never rewritten; the artifact's verification reads the newest record bound to its current bytes
    (``subject_sha``)."""

    __tablename__ = "artifact_checks"
    __table_args__ = (
        CheckConstraint(_in("subject_type", ARTIFACT_CHECK_SUBJECTS), name="ck_artifact_checks_subject"),
        CheckConstraint(_in("status", CHECK_STATUSES), name="ck_artifact_checks_status"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_artifact_checks_policy"),
        CheckConstraint(ARTIFACT_CHECK_TARGETS, name="ck_artifact_checks_target"),
        Index("ix_artifact_checks_revision", "model_revision_id", "created_at"),
        Index("ix_artifact_checks_drawing", "drawing_id", "created_at"),
        Index("ix_artifact_checks_assembly_revision", "assembly_revision_id", "created_at"),
        Index("ix_artifact_checks_spec_version", "spec_version_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    model_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="CASCADE"))
    drawing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drawings.id", ondelete="CASCADE"))
    # S5: assumption decisions on an assembly revision / an engineering spec version.
    assembly_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="CASCADE"))
    spec_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="CASCADE"))
    # The exact bytes checked: the revision's step_sha / the drawing's manifest_sha256 / the assembly revision's
    # content_sha256 / the canonical hash of the spec version's content.
    subject_sha: Mapped[str | None] = mapped_column(String(64))
    check: Mapped[str] = mapped_column(String(60), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    report: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()



# ---- W8 integration -----------------------------------------------------------------------------------
# A delivery manifest lists the files a delivery requires and the files it really holds (assembly STEP, unique part
# STEP set, BOM, drawing package files) with their hashes and source versions. ``complete`` only when every required
# file exists; anything missing is listed and the manifest is ``partial``. Immutable.

DELIVERY_COMPLETENESS = ("complete", "partial")


class DeliveryManifest(Base):
    __tablename__ = "delivery_manifests"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_delivery_manifests_project_number"),
        CheckConstraint(_in("completeness", DELIVERY_COMPLETENESS), name="ck_delivery_manifests_completeness"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_delivery_manifests_policy"),
        CheckConstraint(_in("check_status", CHECK_STATUSES), name="ck_delivery_manifests_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)  # DL-<n> within the project
    # The exact sources (NULL assembly: the assembly was never built, the manifest lists the parts that were).
    assembly_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assembly_revisions.id", ondelete="SET NULL"), index=True)
    spec_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="SET NULL"))
    # W5 assembly drawing package (no foreign key: the package table belongs to W5); NULL when none was made.
    drawing_package_id: Mapped[uuid.UUID | None] = mapped_column()
    execution_policy: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard", nullable=False)
    completeness: Mapped[str] = mapped_column(String(20), nullable=False)
    check_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested", nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    zip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


# ---- S3 resume: per-definition progress of the W8 assembly modeling step ------------------------------
# One row per part definition of one build scope (``node_run:<id>``: a canvas / workspace / collaboration node run,
# retried in place by the engine). It records the exact part revision a definition produced, or why it was not built,
# so a retry of that scope skips definitions already built and still valid and builds only the rest.

ASSEMBLY_BUILD_STEP_STATUSES = ("built", "failed", "blocked", "needs_clarification", "not_started")


class AssemblyBuildStep(Base):
    __tablename__ = "assembly_build_steps"
    __table_args__ = (
        UniqueConstraint("scope", "spec_version_id", "definition_key", name="uq_assembly_build_steps_key"),
        CheckConstraint(_in("status", ASSEMBLY_BUILD_STEP_STATUSES), name="ck_assembly_build_steps_status"),
        CheckConstraint(_in("execution_policy", EXECUTION_POLICIES), name="ck_assembly_build_steps_policy"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    spec_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("engineering_spec_versions.id", ondelete="CASCADE"), nullable=False,
                                                       index=True)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    definition_key: Mapped[str] = mapped_column(String(60), nullable=False)
    execution_policy: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str | None] = mapped_column(String(20))  # template | ai_modeling
    part_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parts.id", ondelete="SET NULL"))
    revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_revisions.id", ondelete="SET NULL"))
    # {stage, code, message, retryable, recovery} of a definition that was not built (design §3.1).
    error: Mapped[dict | None] = mapped_column(JSONB)
    # The outcome as the step reported it (assumptions, check summary, notes), replayed when the definition is reused.
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    node_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
