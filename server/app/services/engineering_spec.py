"""Engineering specs (W2): sources → chunks the server really sent → AI extraction → validated spec versions.

Contract: docs/development/mechanical-engineering/w0-contract.zh-CN.md §5 (spec JSON), §2 (execution policy),
§7 (budgets); acceptance AC-I01–I05.

- A spec is a project-scoped lineage; every version is immutable once finished. ``create_spec`` /
  ``answer_questions`` / ``reextract`` only create a version in status ``extracting`` (policy frozen,
  sources pinned by exact file version id + sha256, direct text stored as a blob); ``run_extraction`` does
  the work (the API runs it as a background job; W8 may call it synchronously).
- Reading is real and recorded per chunk: a chunk is ``sent`` only when the provider call that carried it
  returned (``ai_call_id``); otherwise it stays ``pending`` / ``unread`` / ``failed`` with a reason. More
  chunks than the budget allows fail the version before any call (stage ``over_budget``) with every
  unread part listed — nothing is truncated silently.
- Structure first (generation reliability A, design §6.1): before any chunk, ONE skeleton call (two for a long digest;
  units ``skeleton:<n>``) reads the structural digest of all sources (``source_reader.structure_digest``) and returns
  every part / assembly with its drawing number or standard number + size, names, kind, parent and quantity, and the
  assembly-level interfaces. Its components get their definition keys at once; the reading record is
  ``planning.skeleton`` (digest size per source, what was cut, the calls), the result ``content.skeleton`` /
  ``content.interfaces``. A continuation reuses the skeleton its parent read from the same digest.
- Long sources (S7): every chunk is read on its own in the compact chunk format (``ai/spec_agent.ChunkReply``), up
  to ``limits.SPEC_READ_CONCURRENCY`` at once (model calls in worker threads; the chunk rows, call records and every
  database write stay on the thread that owns the session), each with the skeleton as ``<known>``. The server merges
  the findings deterministically by key (``services/spec_merge.py``: the skeleton's definitions and tree, identity by
  drawing number / standard + size, conflicts keep every value, stable keys). Original assembly sheets and the complete
  graph then receive a bounded structure review; Turbo scalar values are adopted through ``spec_resolve``.
  Answers return only the facts they set, applied by the server to the
  current spec.
- The model's evidence is checked by the server: the chunk must be one it really sent, the quote must be
  found in that chunk's text (or the drawing entity in its evidence index) and the locator is written by
  the server. Replies that do not match their schema go back to the model (≤ 2 repairs) and then fail the version
  (stage ``parse`` / ``validate``); facts whose evidence cannot be located are demoted (standard: unknown + question;
  turbo: an ``ai_turbo`` assumption).
- Policy: standard runs the ``spec.clarification_gate`` business check (``policy.run_check``): open
  questions block only the definitions / instances / relations they list in ``affects`` (``readiness``);
  AI assumptions are converted into questions. Turbo skips the gate (``skipped_turbo``): adopted values are
  ``policy.Assumption`` records (author ``ai_turbo``), conflicts keep every reading. Person answers are
  bound to a question id, become a text source of their own and produce a new version.
- Failures record ``{stage, code, message, detail, call_ids, retryable, recovery, recovery_message}`` (AC-I05) with
  stage context_build | provider_select | provider_call | parse | validate | over_budget | interrupted; ``recovery`` is
  a fixed code (:data:`RECOVERY_CODES`: continue_extract | re_extract | split_sources) the UI turns into an action.
- Resume (S3): every chunk stores the findings its call returned (``findings`` + ``call_id``) as soon as it is read.
  ``continue_extraction`` makes a new version (origin ``continue``) of a failed one that reuses the parent's chunks
  whose status is ``sent``, whose text hash is unchanged and whose findings were read with the current prompt version
  (their findings, not a new call), reads only the failed / unread chunks and merges; when only the resolve call failed,
  only the merge and that call are redone. A failed answer version is continued by applying the same answers again
  (the answer call is the only call it makes).

Service entry points for other packages (W4 assemblies, W8 orchestration): ``create_spec``,
``run_extraction`` / ``run_extraction_job``, ``answer_questions``, ``reextract``, ``continue_extraction``,
``revise_version`` (generation supplement A-04: a new version of the same spec with new material / a revision note),
``load_version``, ``get_content``, ``readiness``, ``quantities``, ``buildable_definitions``, ``spec_digest``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import policy as pol
from ..ai import limits, retry as call_retry
from ..ai import spec_agent as agent
from ..ai.providers.base import ImagePart, Message, ModelProvider, ProviderError, clip, schema_budget_text
from ..ai.trace import Tracer
from ..ai.timeouts import CAD_MODEL_CALL_S
from ..db import session_scope
from ..errors import AppError, not_found
from ..models import (
    AiCall,
    AiOperation,
    EngineeringSpec,
    EngineeringSpecChunk,
    EngineeringSpecVersion,
    File,
    FileVersion,
    User,
)
from ..security import ProjectAccess
from ..storage import read_blob, save_blob
from . import source_reader as sr
from . import spec_identity, spec_merge, spec_structure_review
from .audit import audit
from .worker_heartbeat import background_heartbeat

log = logging.getLogger("partroute.engineering_spec")

MAX_REPAIRS = 2
#: Per request, including quiet reasoning; the enclosing extraction/run deadline can only shorten it.
CHUNK_CALL_TIMEOUT_S = CAD_MODEL_CALL_S
#: Resolve and answer calls carry only the open items / answered questions (S7), never the whole spec.
SPEC_CALL_TIMEOUT_S = CAD_MODEL_CALL_S
#: How often the reading loop refreshes the version's heartbeat while chunk calls run in the workers (seconds).
HEARTBEAT_S = 30.0
MAX_FILE_SOURCES = 20
#: Direct text accepted by the API (ai/limits.py); longer than the chunk budget still gets an explicit over_budget failure.
MAX_DIRECT_TEXT = limits.MATERIAL_TEXT_CHARS
STALE_AFTER = timedelta(minutes=15)
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
GATE = "spec.clarification_gate"
#: Shown with every failure (``recovery_message``) so the person knows what to do next.
RECOVERY = {
    "over_budget": "Split the material into several specs or remove sources; nothing was sent to the model.",
    "context_build": "Fix the sources listed (upload a readable version) and re-extract.",
    "provider_select": "Configure or enable a model usable for planning in Settings → Models, then re-extract.",
    "provider_call": "Continue: the chunks already read are kept and only the rest is sent; if it keeps failing check the model "
                     "configuration, key and quota.",
    "parse": "Continue (the chunks already read are kept); if the model keeps returning malformed JSON re-extract with another model.",
    "validate": "Continue (the chunks already read are kept) or re-extract with another model; the problem list says what did not "
                "pass the server checks.",
    "interrupted": "The extraction stopped before it finished (server restart); continue from the chunks already read.",
    "not_retryable": "This failure does not go away by trying again: fix the model configuration or the sources, then re-extract.",
}
#: Recovery codes (design §3.1) per failure stage, for a retryable failure. A failure that is not retryable is ``re_extract``
#: (start over after fixing the cause); the chunk budget is ``split_sources``.
RECOVERY_CODES = {"over_budget": "split_sources", "context_build": "re_extract", "provider_select": "re_extract",
                  "provider_call": "continue_extract", "parse": "continue_extract", "validate": "continue_extract",
                  "interrupted": "continue_extract"}
CONTINUE = "continue_extract"
KEY_PATTERNS = {"definitions": r"^D-\d{2,}$", "instances": r"^I-\d{2,}$", "relations": r"^M-\d{2,}$", "facts": r"^F-\d{3,}$",
                "manufacturing_notes": r"^N-\d{2,}$", "questions": r"^Q-\d{2,}$", "conflicts": r"^C-\d{2,}$", "assumptions": r"^A-\d{2,}$"}
_KEY_FIELD = {"definitions": "key", "instances": "key", "relations": "key", "facts": "id", "manufacturing_notes": "id", "questions": "id",
              "conflicts": "id", "assumptions": "id"}


# --- request bodies (shared with api/engineering_specs.py) -----------------------------------------------


class SpecCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=MAX_DIRECT_TEXT)
    file_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_FILE_SOURCES)
    # Explicit per request (W0 §2); None = standard. Unknown values are refused, never mapped.
    execution_policy: str | None = Field(default=None, max_length=20)
    model_config_id: uuid.UUID | None = None


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1, max_length=60)
    answer: str = Field(min_length=1, max_length=limits.DESCRIPTION_CHARS)


class AnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[AnswerIn] = Field(min_length=1, max_length=50)


class ReextractIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None = the policy frozen on the version re-extracted (never the "current default").
    execution_policy: str | None = Field(default=None, max_length=20)
    model_config_id: uuid.UUID | None = None


# --- small helpers --------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


RECOVERY_CODE_SET = frozenset({"continue_extract", "re_extract", "split_sources"})


def recovery_code(stage: str | None, code: str | None, retryable: bool) -> str:
    """The recovery code of a spec failure (design §3.1): continue_extract | re_extract | split_sources."""
    if stage == "over_budget":
        return "re_extract" if code == "context_over_budget" else "split_sources"
    if not retryable:
        return "re_extract"
    return RECOVERY_CODES.get(stage or "", "re_extract")


def recovery_message(stage: str | None, code: str | None, retryable: bool) -> str:
    if code == "source_frame_unresolved":
        return "原文坐标映射涉及轴向反射，需要先支持模型坐标系转换，再重新提取。直接重试无法解决，不能以零旋转代替。"
    if stage not in ("over_budget", "context_build", "provider_select") and not retryable:
        return RECOVERY["not_retryable"]
    return RECOVERY.get(stage or "", RECOVERY["provider_call"])


def failure_info(err: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored failure in the current form. Rows written before recovery codes (``recovery`` was a sentence) get the
    code, and the sentence is kept as ``recovery_message``."""
    if not err or err.get("recovery") in RECOVERY_CODE_SET:
        return err
    out = dict(err)
    retryable = bool(err.get("retryable"))
    out["recovery_message"] = err.get("recovery_message") or err.get("recovery") or recovery_message(err.get("stage"), err.get("code"),
                                                                                                    retryable)
    out["recovery"] = recovery_code(err.get("stage"), err.get("code"), retryable)
    return out


def _parse_policy(value: str | None) -> str:
    try:
        return pol.parse_policy(value)
    except ValueError as exc:
        raise AppError(422, "invalid_policy", str(exc), {"execution_policy": value}) from exc


def _record_calls(db: Session, calls: list[Any], *, ai_operation_id: uuid.UUID | None = None) -> list[str]:
    """ai_calls rows for call records (no node run / operation); returns their ids in order."""
    from . import ai as ai_svc

    if not calls:
        return []
    before = {id(o) for o in db.new}
    ai_svc.record_calls(db, calls, None, ai_operation_id=ai_operation_id)
    rows = [o for o in db.new if isinstance(o, AiCall) and id(o) not in before]
    db.flush()
    return [str(r.id) for r in rows]


def _lock_spec(db: Session, spec_id: uuid.UUID) -> EngineeringSpec:
    spec = db.scalar(select(EngineeringSpec).where(EngineeringSpec.id == spec_id).with_for_update())
    if spec is None:
        raise not_found("engineering spec")
    return spec


# --- loading (project-scoped; non-members get 404 through security.project_access) ---------------------


def load_spec(db: Session, user: User, spec_id: uuid.UUID, action: str = "view") -> tuple[EngineeringSpec, ProjectAccess]:
    from ..security import project_access

    spec = db.get(EngineeringSpec, spec_id)
    if spec is None:
        raise not_found("engineering spec")
    return spec, project_access(db, user, spec.project_id, action)


def load_version(db: Session, user: User, version_id: uuid.UUID, action: str = "view") -> tuple[EngineeringSpecVersion, ProjectAccess]:
    from ..security import project_access

    v = db.get(EngineeringSpecVersion, version_id)
    if v is None:
        raise not_found("engineering spec version")
    return v, project_access(db, user, v.project_id, action)


def get_content(db: Session, version_id: uuid.UUID, project_id: uuid.UUID) -> dict[str, Any]:
    """The W0 §5 spec JSON of a finished version of this project (W4 / W8). 404 elsewhere; 409 while not usable."""
    v = db.get(EngineeringSpecVersion, version_id)
    if v is None or v.project_id != project_id:
        raise not_found("engineering spec version")
    status, _ = effective_status(v)
    if status not in ("ready", "needs_answers"):
        raise AppError(409, "spec_not_ready", f"spec version is {status}", {"status": status, "version_id": str(v.id)})
    return v.content


# --- create / answer / re-extract ------------------------------------------------------------------------


def _title_from(text: str, labels: list[str]) -> str:
    line = next((ln.strip(" #\t") for ln in text.splitlines() if ln.strip()), "")
    return (line or (labels[0] if labels else "") or "工程规格")[:200]


def _project_file_version(db: Session, project_id: uuid.UUID, fv_id: uuid.UUID) -> tuple[FileVersion, File]:
    fv = db.get(FileVersion, fv_id)
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != project_id or f.archived_at is not None:
        raise not_found("file version")
    return fv, f


def _text_input(db: Session, source_id: str, text_: str, label: str, *, answer_of: str | None = None) -> dict[str, Any]:
    sha = save_blob(db, text_.encode("utf-8"), TEXT_CONTENT_TYPE)
    return {"id": source_id, "kind": "text", "label": label, "name": "", "file_version_id": None, "file_id": None, "version": None,
            "sha256": sha, "bytes": len(text_.encode("utf-8")), "content_type": TEXT_CONTENT_TYPE, "answer_of": answer_of,
            "read_status": "unread", "ranges": [], "error": None}


def _file_input(source_id: str, fv: FileVersion, f: File) -> dict[str, Any]:
    return {"id": source_id, "kind": "file_version", "label": f"{f.name} v{fv.version}", "name": fv.original_name,
            "file_version_id": str(fv.id), "file_id": str(f.id), "version": fv.version, "sha256": fv.blob_sha256, "bytes": None,
            "content_type": None, "answer_of": None, "read_status": "unread", "ranges": [], "error": None}


def _freeze_model(db: Session, model_config_id: uuid.UUID | None) -> dict[str, Any]:
    """W1: the explicit (or default) configuration frozen into the version's planning record before any reading, so
    the background job never re-picks the default later. When nothing is usable the precise error is kept instead and
    the job fails at provider_select with it."""
    from . import ai as ai_svc

    try:
        return {"model_snapshot": ai_svc.freeze_model(db, agent.PROVIDER_PURPOSE, model_config_id)}
    except ProviderError as exc:
        return {"model_snapshot": None, "model_error": {"code": exc.code, "message": clip(exc.message, 500), "retryable": bool(exc.retryable)}}


def _new_version(db: Session, spec: EngineeringSpec, *, origin: str, policy: str, created_by: uuid.UUID, sources: list[dict[str, Any]],
                 parent: EngineeringSpecVersion | None = None, model_config_id: uuid.UUID | None = None,
                 answers: list[dict[str, Any]] | None = None, frozen_planning: dict | None = None) -> EngineeringSpecVersion:
    spec = _lock_spec(db, spec.id)
    last = db.scalar(select(func.max(EngineeringSpecVersion.version_no)).where(EngineeringSpecVersion.spec_id == spec.id)) or 0
    v = EngineeringSpecVersion(spec_id=spec.id, project_id=spec.project_id, version_no=last + 1, origin=origin,
                               parent_version_id=parent.id if parent else None, execution_policy=policy, status="extracting",
                               model_config_id=model_config_id, sources=sources, answers=answers or [], created_by=created_by,
                               heartbeat_at=_now(), content={}, checks=[], validation={}, planning=frozen_planning if frozen_planning is not None else _freeze_model(db, model_config_id))
    db.add(v)
    spec.updated_at = _now()
    db.flush()
    return v


def create_spec(db: Session, acc: ProjectAccess, body: SpecCreateIn, *, frozen_planning: dict | None = None) -> tuple[EngineeringSpec, EngineeringSpecVersion]:
    """A new spec and its first version (status extracting). The policy is frozen here, before any reading: the request's
    own choice, else the project default (generation reliability WP09: a form never silently overrides it)."""
    from . import execution_policy as exec_policy

    pid = acc.project.id
    policy = _parse_policy(body.execution_policy) if body.execution_policy else exec_policy.project_default(db, pid)
    if not body.text.strip() and not body.file_version_ids:
        raise AppError(422, "source_required", "paste text or choose at least one project file")
    files = [_project_file_version(db, pid, fid) for fid in dict.fromkeys(body.file_version_ids)]
    if body.model_config_id is not None:
        from ..models import ModelConfigRow

        if db.get(ModelConfigRow, body.model_config_id) is None:
            raise not_found("model config")
    db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"engineering_spec.number:{pid}"})
    number = (db.scalar(select(func.max(EngineeringSpec.number)).where(EngineeringSpec.project_id == pid)) or 0) + 1
    title = body.title.strip() or _title_from(body.text, [f.name for _, f in files])
    spec = EngineeringSpec(project_id=pid, number=number, title=title, created_by=acc.user.id)
    db.add(spec)
    db.flush()
    sources: list[dict[str, Any]] = []
    if body.text.strip():
        sources.append(_text_input(db, sr.new_source_id(1), body.text, "直接输入的文本"))
    for fv, f in files:
        sources.append(_file_input(sr.new_source_id(len(sources) + 1), fv, f))
    v = _new_version(db, spec, origin="create", policy=policy, created_by=acc.user.id, sources=sources, model_config_id=body.model_config_id, frozen_planning=frozen_planning)
    audit(db, action="engineering_spec.create", subject_type="engineering_spec", subject_id=spec.id, user_id=acc.user.id, project_id=pid,
          data={"version_id": str(v.id), "execution_policy": policy, "sources": len(sources), "text_chars": len(body.text)})
    return spec, v


def decision_questions(content: dict[str, Any] | None, policy: str | None) -> dict[str, dict[str, Any]]:
    """The AI decisions of a Turbo version a person may correct through the answer flow, as questions keyed by the
    assumption id (generation supplement D-03: a person's change of a decision makes a new version; the decision is
    replaced there by the person's answer, the answered version keeps it as it was). Empty for a standard version."""
    if not pol.is_turbo(policy):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for a in (content or {}).get("assumptions") or []:
        if not isinstance(a, dict) or not a.get("id") or (a.get("author") or "ai_turbo") not in pol.NEEDS_CONFIRMATION:
            continue
        value = f"{a.get('adopted')}{(' ' + str(a['unit'])) if a.get('unit') else ''}"
        out[str(a["id"])] = {"id": str(a["id"]), "subject": a.get("subject") or "", "question": f"更正 AI 判断 {a['id']}：{a.get('subject')} 原采用 {value}",
                             "affects": list(a.get("affected") or []), "candidates": [a.get("adopted"), *(a.get("alternatives") or [])][:20],
                             "origin": "corrected_decision"}
    return out


def answer_questions(db: Session, acc: ProjectAccess, parent: EngineeringSpecVersion, body: AnswersIn) -> EngineeringSpecVersion:
    """A new version applying person answers, each bound to an open question of ``parent`` — or, on a Turbo version, to
    one of its AI decisions (a person's correction, :func:`decision_questions`). The parent never changes."""
    status, _ = effective_status(parent)
    if status not in ("ready", "needs_answers"):
        raise AppError(409, "spec_not_answerable", f"version {parent.version_no} is {status}", {"status": status})
    questions = {**decision_questions(parent.content, parent.execution_policy), **{q["id"]: q for q in parent.content.get("questions") or []}}
    seen: set[str] = set()
    for a in body.answers:
        q = questions.get(a.question_id)
        if q is None:
            raise AppError(422, "unknown_question", f"{a.question_id} is not a question of version {parent.version_no}",
                           {"question_id": a.question_id})
        if q.get("answer"):
            raise AppError(409, "question_already_answered", f"{a.question_id} is already answered", {"question_id": a.question_id})
        if a.question_id in seen:
            raise AppError(422, "duplicate_answer", f"{a.question_id} is answered twice", {"question_id": a.question_id})
        seen.add(a.question_id)
    spec = db.get(EngineeringSpec, parent.spec_id)
    assert spec is not None
    sources = [dict(s) for s in parent.sources]
    answers: list[dict[str, Any]] = []
    at = _now().isoformat()
    for a in body.answers:
        q = questions[a.question_id]
        sid = sr.new_source_id(len(sources) + 1)
        statement = f"回答 {a.question_id}（{q.get('subject')}）：{a.answer.strip()}"
        sources.append(_text_input(db, sid, statement, f"回答 {a.question_id}", answer_of=a.question_id))
        answers.append({"question_id": a.question_id, "answer": a.answer.strip(), "by": str(acc.user.id), "by_name": acc.user.name,
                        "at": at, "source": sid})
    v = _new_version(db, spec, origin="answer", policy=parent.execution_policy, created_by=acc.user.id, sources=sources, parent=parent,
                     model_config_id=parent.model_config_id, answers=answers)
    audit(db, action="engineering_spec.answer", subject_type="engineering_spec", subject_id=spec.id, user_id=acc.user.id,
          project_id=spec.project_id, data={"version_id": str(v.id), "parent_version_id": str(parent.id), "questions": sorted(seen)})
    return v


def reextract(db: Session, acc: ProjectAccess, parent: EngineeringSpecVersion, body: ReextractIn) -> EngineeringSpecVersion:
    """Read the same pinned sources again into a new version (retry after a failure, or another policy / model).
    Answer sources stay with the versions that applied them."""
    _require_extracted_source(parent)
    policy = _parse_policy(body.execution_policy) if body.execution_policy else parent.execution_policy
    status, _ = effective_status(parent)
    if status == "extracting":
        raise AppError(409, "spec_extracting", "this version is still being extracted")
    spec = db.get(EngineeringSpec, parent.spec_id)
    assert spec is not None
    v = _new_version(db, spec, origin="reextract", policy=policy, created_by=acc.user.id, sources=_fresh_sources(parent), parent=parent,
                     model_config_id=body.model_config_id or parent.model_config_id)
    audit(db, action="engineering_spec.reextract", subject_type="engineering_spec", subject_id=spec.id, user_id=acc.user.id,
          project_id=spec.project_id, data={"version_id": str(v.id), "parent_version_id": str(parent.id), "execution_policy": policy})
    return v


REVISION_NOTE_LABEL = "修订说明"
#: The precedence line the server writes before a revision note / a person's answer that a revision reads (review fix:
#: they are not equal-weight text next to what they override); rewritten on every later revision.
_STATEMENT_HEAD = re.compile(r"^【(?:修订说明|人的回答)[^】\n]*】[^\n]*\n")


def _free_source_id(sources: list[dict[str, Any]]) -> str:
    used = {str(s.get("id")) for s in sources}
    n = len(sources) + 1
    while sr.new_source_id(n) in used:
        n += 1
    return sr.new_source_id(n)


def _note_versions(db: Session, parent: EngineeringSpecVersion) -> dict[str, int]:
    """Revision-note source id → number of the version that added it (``planning.revision.note_source`` up the parent
    chain; a note keeps its id in every later version, and the oldest record is the one that added it)."""
    out: dict[str, int] = {}
    v: EngineeringSpecVersion | None = parent
    seen: set[uuid.UUID] = set()
    while v is not None and v.id not in seen and len(seen) < 100:
        seen.add(v.id)
        note = ((v.planning or {}).get("revision") or {}).get("note_source")
        if note:
            out[str(note)] = v.version_no
        v = db.get(EngineeringSpecVersion, v.parent_version_id) if v.parent_version_id else None
    return out


def _statement_text(body: str, *, answer: bool, k: int, total: int, version: int | None) -> str:
    """A revision note / person answer as a revision reads it: what it is, its version and place, and the precedence
    (answers and notes override the material; among them the later one wins)."""
    latest = k == total
    if answer:
        head = f"【人的回答（第{k}条{'，最新' if latest else ''}）】"
    else:
        head = f"【修订说明（{f'v{version}，' if version else ''}第{k}条，{'最新' if latest else '较早'}）】"
    rule = ("人的回答和修订说明按先后排列，优先于资料原文和更早的修订说明；彼此不一致时以后面的为准"
            + ("（本条最新，以本条为准）。" if latest else "（本条与后面的修订说明或回答不一致之处，以后面的为准）。"))
    return f"{head}{rule}\n{_STATEMENT_HEAD.sub('', body, count=1).strip()}"


def _restate(db: Session, s: dict[str, Any], text_: str) -> None:
    sha = save_blob(db, text_.encode("utf-8"), TEXT_CONTENT_TYPE)
    s.update(sha256=sha, bytes=len(text_.encode("utf-8")))


def revise_version(db: Session, acc: ProjectAccess, parent: EngineeringSpecVersion, *, text: str = "",
                   file_version_ids: list[Any] | tuple = (), drop_file_version_ids: list[Any] | tuple = (),
                   policy: str | None = None, model_config_id: uuid.UUID | None = None, reason: str = "revise",
                   input_dedupe: dict[str, Any] | None = None, frozen_planning: dict | None = None) -> EngineeringSpecVersion:
    """Generation supplement A-04 (IN-07 / IN-08): a new version of the SAME spec from the parent's pinned sources plus new
    material (status extracting; ``run_extraction`` reads it). New file versions are pinned next to the old ones (a file
    the parent already has is not added twice); the instruction text becomes a revision note (a text source labelled
    ``Revision note``); ``drop_file_version_ids`` leaves out sources that could not be read and are replaced (IN-08). The
    person's earlier answers stay as text sources (statements read again with the rest). Review fix: every revision
    note and answer it reads starts with a precedence line (:func:`_statement_text`: "Revision note (v3, item 2, latest)" /
    "Human answer (item 1)" — notes and answers override the material and earlier notes, the later one wins), rewritten on
    every revision so only the newest is marked latest. The new version is a
    re-extraction of the parent (origin ``reextract``): the merge reuses the parent's keys and, under Turbo, its decisions
    that still answer an open item (``_merge``). ``planning.revision`` records the parent, what was added / dropped and
    why (``reason``: revise | replace_source). The parent never changes. 404 for a file outside the project."""
    _require_extracted_source(parent)
    run_policy = _parse_policy(policy) if policy else parent.execution_policy
    status, _ = effective_status(parent)
    if status == "extracting":
        raise AppError(409, "spec_extracting", "this version is still being extracted")
    spec = db.get(EngineeringSpec, parent.spec_id)
    assert spec is not None
    drop = {str(x) for x in drop_file_version_ids}
    sources = [s for s in _fresh_sources(parent, keep_answers=True) if str(s.get("file_version_id")) not in drop]
    dropped = [{"file_version_id": s.get("file_version_id"), "label": s.get("label")} for s in parent.sources or []
               if s.get("file_version_id") and str(s.get("file_version_id")) in drop]
    have = {str(s.get("file_version_id")) for s in sources if s.get("file_version_id")}
    added: list[dict[str, Any]] = []
    for fid in dict.fromkeys(str(x) for x in file_version_ids):
        if fid in have:
            continue
        fv, f = _project_file_version(db, parent.project_id, uuid.UUID(fid))
        src = _file_input(_free_source_id(sources), fv, f)
        sources.append(src)
        have.add(fid)
        added.append({"source": src["id"], "file_version_id": fid, "label": src["label"]})
    note = (text or "").strip()
    note_source = None
    if not sources and not note:
        raise AppError(422, "source_required", "the revised version would read nothing: reference a file or write the change")
    # earlier notes and answers, in the order they were given (sources are only ever appended), restated with precedence
    versions = _note_versions(db, parent)
    statements = [s for s in sources if s.get("kind") == "text" and s.get("sha256")
                  and (s.get("answer_of") or s.get("label") == REVISION_NOTE_LABEL or str(s.get("id")) in versions)]
    total = len(statements) + (1 if note else 0)
    for k, s in enumerate(statements, 1):
        try:
            body = read_blob(db, s["sha256"])[0].decode("utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue  # read as it is (the reading reports a missing blob)
        _restate(db, s, _statement_text(body, answer=bool(s.get("answer_of")), k=k, total=total, version=versions.get(str(s.get("id")))))
    v = _new_version(db, spec, origin="reextract", policy=run_policy, created_by=acc.user.id, sources=sources, parent=parent,
                     model_config_id=model_config_id or parent.model_config_id, frozen_planning=frozen_planning)
    if note:  # the new note knows its version number only now
        note_source = _free_source_id(sources)
        sources = [*sources, _text_input(db, note_source, _statement_text(note, answer=False, k=total, total=total, version=v.version_no),
                                         REVISION_NOTE_LABEL)]
        v.sources = sources
    revision = {"reason": reason, "of_version_id": str(parent.id), "of_version_no": parent.version_no, "added_files": added,
                "dropped_files": dropped, "note_source": note_source, "note_chars": len(note),
                "answers_kept": sum(1 for s in sources if s.get("answer_of"))}
    v.planning = {**(v.planning or {}), "revision": revision, **({"input_dedupe": input_dedupe} if input_dedupe else {})}
    db.flush()
    audit(db, action="engineering_spec.revise", subject_type="engineering_spec", subject_id=spec.id, user_id=acc.user.id,
          project_id=spec.project_id, data={"version_id": str(v.id), "parent_version_id": str(parent.id), "execution_policy": run_policy,
                                            "reason": reason, "added": len(added), "dropped": len(dropped), "note_chars": len(note)})
    return v


def derive_rule_version(db: Session, parent: EngineeringSpecVersion, *, content: dict[str, Any], created_by: uuid.UUID,
                        note: dict[str, Any], ai_call_ids: list[str] | None = None, simulated: bool = False,
                        planning_extra: dict[str, Any] | None = None) -> EngineeringSpecVersion:
    """A version a server rule derived from a finished one (origin ``rule``; generation supplement A-03: Turbo hangs
    definitions that hang nowhere under the root). Nothing is read again: the parent's pinned sources and its chunk rows
    (inherited, so the evidence stays checkable), the given content (its new decisions are ``rule`` assumptions in it),
    the clarification gate run again under the parent's policy. The parent never changes.

    Generation supplement H (``services/spec_aliases``): the Turbo alias / structure resolution derives its version the
    same way; its few planning-model calls are the version's ``planning.ai_call_ids`` (bound to the node run like every
    spec call) with ``planning_extra`` (provider, model, prompt version), and a simulated call marks it simulated."""
    status, _ = effective_status(parent)
    if status not in ("ready", "needs_answers"):
        raise AppError(409, "spec_not_ready", f"engineering spec version {parent.version_no} is {status}", {"status": status})
    spec = _lock_spec(db, parent.spec_id)
    last = db.scalar(select(func.max(EngineeringSpecVersion.version_no)).where(EngineeringSpecVersion.spec_id == spec.id)) or 0
    now = _now()
    # Generation reliability K (design §2.5): identity, the frozen baseline and the target changes this rule made
    vid = uuid.uuid4()
    content = spec_identity.freeze(content, version=SimpleNamespace(id=vid, version_no=last + 1), parent=parent,
                                   reason=str(note.get("rule") or "rule"), record=note)
    gate = pol.run_check(GATE, parent.execution_policy, lambda: _gate(content))
    open_q = [q for q in content.get("questions") or [] if not q.get("answer") and question_blocks(q)]
    planning = {**(planning_extra or {}), "derived_from": str(parent.id), "rule": note.get("rule"), "ai_call_ids": list(ai_call_ids or []),
                "finished_at": now.isoformat()}
    for k in ("model_snapshot", "revision", "input_dedupe"):  # what the parent was read from stays visible ("Revised from vN")
        if (parent.planning or {}).get(k):
            planning[k] = parent.planning[k]
    v = EngineeringSpecVersion(id=vid, spec_id=spec.id, project_id=spec.project_id, version_no=last + 1, origin="rule", parent_version_id=parent.id,
                               execution_policy=parent.execution_policy,
                               status="needs_answers" if (parent.execution_policy == "standard" and open_q) else "ready",
                               model_config_id=parent.model_config_id, sources=[dict(s) for s in parent.sources or []], answers=[],
                               created_by=created_by, heartbeat_at=None, content=content, checks=[gate],
                               validation={**(parent.validation or {}), "derived": note}, planning=planning,
                               simulated=bool(parent.simulated or simulated),
                               finished_at=now)
    db.add(v)
    spec.updated_at = now
    db.flush()
    for c in db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == parent.id).order_by(EngineeringSpecChunk.seq)).all():
        db.add(EngineeringSpecChunk(version_id=v.id, seq=c.seq, source_id=c.source_id, source_chunk=c.source_chunk, kind=c.kind, locator=c.locator,
                                    start=c.start, end=c.end, chars=c.chars, text_sha256=c.text_sha256, status=c.status, reason=c.reason,
                                    ai_call_id=c.ai_call_id, sent_at=c.sent_at, meta=dict(c.meta or {}), findings=c.findings, call_id=c.call_id,
                                    inherited_from_version_id=c.inherited_from_version_id or c.version_id))
    db.flush()
    audit(db, action="engineering_spec.derive", subject_type="engineering_spec", subject_id=spec.id, user_id=created_by,
          project_id=spec.project_id, data={"version_id": str(v.id), "parent_version_id": str(parent.id), "rule": note.get("rule")})
    return v


def continuation_of(db: Session, v: EngineeringSpecVersion) -> EngineeringSpecVersion | None:
    """The version that continued this failed one (``planning.continued_from``), if any."""
    return db.scalar(select(EngineeringSpecVersion).where(EngineeringSpecVersion.spec_id == v.spec_id,
                                                         EngineeringSpecVersion.planning["continued_from"].astext == str(v.id))
                     .order_by(EngineeringSpecVersion.version_no).limit(1))


def _require_extracted_source(version: EngineeringSpecVersion) -> None:
    from .design_spec import has_design

    if has_design(version):
        raise AppError(409, "design_plan_revision_required", "revise the authorized design plan before compiling a new design specification")


def _fresh_sources(parent: EngineeringSpecVersion, *, keep_answers: bool = False) -> list[dict[str, Any]]:
    """The parent's pinned sources with the read state reset (answer sources stay with the versions that applied them,
    unless ``keep_answers``: a revision reads the person's answers again as statements)."""
    sources = []
    for s in parent.sources:
        if s.get("answer_of") and not keep_answers:
            continue
        keep = {k: s.get(k) for k in ("id", "kind", "label", "name", "file_version_id", "file_id", "version", "sha256", "bytes",
                                      "content_type", "answer_of")}
        keep.update(read_status="unread", ranges=[], error=None)
        if keep["kind"] == "drawing":
            keep["kind"] = "file_version"
        sources.append(keep)
    return sources


def continue_extraction(db: Session, acc: ProjectAccess, parent: EngineeringSpecVersion) -> EngineeringSpecVersion:
    """Continue a failed extraction from where it stopped (S3): a new version with the same policy, sources and frozen
    model that reuses the parent's chunks already read (status sent, unchanged text hash, findings stored with the current
    prompt version) and reads only the rest, then merges. A failed answer version applies the same answers again. 409 when the version did
    not fail (``spec_not_failed``), is still extracting, or its failure is not one that continuing can fix
    (``not_continuable``, with the recovery code to use instead); 409 ``already_continued`` names an earlier continuation."""
    status, err = effective_status(parent)
    if status == "extracting":
        raise AppError(409, "spec_extracting", "this version is still being extracted")
    if status != "failed":
        raise AppError(409, "spec_not_failed", f"version {parent.version_no} is {status}; only a failed extraction can be continued "
                       "(re-extract to read the sources again)", {"status": status})
    info = failure_info(err) or {}
    if info.get("recovery") != CONTINUE:
        raise AppError(409, "not_continuable", "continuing would fail the same way: " + str(info.get("recovery_message") or ""),
                       {"recovery": info.get("recovery"), "stage": info.get("stage"), "code": info.get("code"),
                        "retryable": bool(info.get("retryable"))})
    spec = _lock_spec(db, parent.spec_id)
    done = continuation_of(db, parent)
    if done is not None:
        raise AppError(409, "already_continued", f"version {parent.version_no} was already continued as version {done.version_no}",
                       {"version_id": str(done.id), "version_no": done.version_no})
    if parent.origin == "answer":
        answered = db.get(EngineeringSpecVersion, parent.parent_version_id) if parent.parent_version_id else None
        if answered is None:
            raise AppError(409, "not_continuable", "the answered version no longer exists; answer the questions again", {"recovery": "re_extract"})
        v = _new_version(db, spec, origin="answer", policy=parent.execution_policy, created_by=acc.user.id,
                         sources=[dict(s) for s in parent.sources], parent=answered, model_config_id=parent.model_config_id,
                         answers=[dict(a) for a in parent.answers])
    else:
        v = _new_version(db, spec, origin="continue", policy=parent.execution_policy, created_by=acc.user.id,
                         sources=_fresh_sources(parent), parent=parent, model_config_id=parent.model_config_id)
    # Continuing reads with the model the parent was reading with (the same frozen snapshot), so reused and new findings
    # come from one model; a snapshot that can no longer be used fails at provider_select (then re-extract).
    snap = (parent.planning or {}).get("model_snapshot")
    from . import ai as ai_svc

    planning = {k: val for k, val in (v.planning or {}).items() if not (ai_svc.is_snapshot(snap) and k == "model_error")}
    if ai_svc.is_snapshot(snap):
        planning["model_snapshot"] = snap
    v.planning = {**planning, "continued_from": str(parent.id)}
    db.flush()
    audit(db, action="engineering_spec.continue", subject_type="engineering_spec", subject_id=spec.id, user_id=acc.user.id,
          project_id=spec.project_id, data={"version_id": str(v.id), "continued_from": str(parent.id), "stage": info.get("stage"),
                                            "code": info.get("code")})
    return v


# --- extraction run --------------------------------------------------------------------------------------


@dataclass
class _ChunkRef:
    seq: int
    source_id: str
    kind: str
    locator: str
    start: int | None
    end: int | None
    status: str
    text_sha256: str | None
    meta: dict[str, Any]
    row: EngineeringSpecChunk
    text: str | None = None
    data: bytes | None = None
    mime_type: str | None = None


class _Failed(Exception):
    def __init__(self, stage: str, code: str, message: str, *, detail: Any = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.stage, self.code, self.message, self.detail, self.retryable = stage, code, message, detail, retryable


@dataclass
class _Run:
    db: Session
    v: EngineeringSpecVersion
    commit: Callable[[], None] | None
    stop: Callable[[], None] | None = None
    ai_operation_id: uuid.UUID | None = None
    stage: str = "context_build"
    sources: dict[str, sr.Source] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    chunks: dict[int, _ChunkRef] = field(default_factory=dict)
    tracer: Tracer = field(default_factory=Tracer)
    recorded: int = 0
    call_ids: list[str] = field(default_factory=list)
    provider: ModelProvider | None = None
    #: Instance keys an answered quantity removed on purpose (the "keep every key" check allows them).
    removed: set[str] = field(default_factory=set)
    drawing_images: dict[tuple[str, str], tuple[bytes | None, dict[str, Any]]] = field(default_factory=dict)

    def save(self) -> None:
        self.v.heartbeat_at = _now()
        self.db.flush()
        if self.commit is not None:
            self.commit()

    def record(self) -> list[str]:
        new = self.tracer.calls[self.recorded:]
        self.recorded = len(self.tracer.calls)
        ids = _record_calls(self.db, new, ai_operation_id=self.ai_operation_id)
        self.call_ids.extend(ids)
        return ids

    def text_of(self, c: _ChunkRef) -> str:
        if c.text is None:
            if not c.text_sha256:
                return ""
            try:
                c.text = read_blob(self.db, c.text_sha256)[0].decode("utf-8", errors="replace")
            except (FileNotFoundError, OSError):
                c.text = ""
        return c.text


def run_extraction_job(version_id: uuid.UUID | str) -> None:
    """Background entry point: own session, commits after every chunk so progress is visible."""
    from ..db import session_scope

    try:
        with session_scope() as db:
            run_extraction(db, uuid.UUID(str(version_id)), commit=db.commit)
    except Exception:  # noqa: BLE001 - the version records its own failure; never raise into the server
        log.exception("engineering spec extraction %s crashed", version_id)


def run_extraction(db: Session, version_id: uuid.UUID, *, commit: Callable[[], None] | None = None,
                   stop: Callable[[], None] | None = None, ai_operation_id: uuid.UUID | None = None) -> EngineeringSpecVersion:
    """Read, extract, validate and finish one version (status extracting → ready / needs_answers / failed).
    Never raises for source / model problems: they end as a failed version with a structured error."""
    v = db.scalar(select(EngineeringSpecVersion).where(EngineeringSpecVersion.id == version_id).with_for_update())
    if v is None:
        raise not_found("engineering spec version")
    if v.status != "extracting":
        return v
    run = _Run(db=db, v=v, commit=commit, stop=stop, ai_operation_id=ai_operation_id)
    try:
        if stop:
            stop()
        _read_sources(run)
        run.save()
        parent = db.get(EngineeringSpecVersion, v.parent_version_id) if v.origin == "answer" and v.parent_version_id else None
        _check_budget(run)
        run.stage = "provider_select"
        run.provider = _select_provider(run)
        if v.origin == "answer" and parent is not None:
            reply = _apply_answers(run, parent)
        else:
            base = _keys_parent(db, v)
            skeleton = _read_skeleton(run, base)
            findings = _read_chunks(run, base, skeleton)
            reply = _merge(run, findings, base=base, skeleton=skeleton)
        if stop:
            stop()
        _finish(run, reply, parent)
        if stop:
            stop()  # Final validation cannot save a successful extraction after its caller has stopped.
    except _Failed as exc:
        _fail(run, exc.stage, exc.code, exc.message, detail=exc.detail, retryable=exc.retryable)
    except ProviderError as exc:
        run.record()
        _fail(run, run.stage if run.stage != "context_build" else "provider_call", exc.code, exc.message, retryable=bool(exc.retryable),
              from_provider=True)
    except Exception as exc:  # noqa: BLE001 - recorded on the version; the job itself must not crash the server
        log.exception("engineering spec extraction %s failed unexpectedly", v.id)
        _fail(run, run.stage, "internal_error", f"{type(exc).__name__}: {clip(str(exc), 300)}")
    db.flush()
    return v


def _keys_parent(db: Session, v: EngineeringSpecVersion) -> EngineeringSpecVersion | None:
    """The version whose keys the merge reuses and whose component names the chunk reads get as ``<known>``: the parent
    of a re-extraction; for a continuation, the parent of the extraction it continues (so it reads and merges exactly
    as that one would have)."""
    base = v
    while base.origin == "continue" and base.parent_version_id:
        nxt = db.get(EngineeringSpecVersion, base.parent_version_id)
        if nxt is None:
            return None
        base = nxt
    return db.get(EngineeringSpecVersion, base.parent_version_id) if base.parent_version_id else None


def _fail(run: _Run, stage: str, code: str, message: str, *, detail: Any = None, retryable: bool = False,
          from_provider: bool = False) -> None:
    v = run.v
    for c in run.chunks.values():
        if c.row.status == "pending":
            c.row.status, c.row.reason = "unread", (c.row.reason or "not_reached")
    _write_manifest(run)
    v.status = "failed"
    v.error = {"stage": stage, "code": code, "message": clip(message, 1000), "detail": detail, "call_ids": list(run.call_ids),
               "retryable": retryable, "recovery": recovery_code(stage, code, retryable),
               "recovery_message": recovery_message(stage, code, retryable)}
    diag = _diagnostic(run, stage, code, message, retryable, from_provider=from_provider)
    if diag is not None:
        v.error["diagnostic"] = diag
    v.planning = {**(v.planning or {}), "ai_call_ids": list(run.call_ids), "finished_at": _now().isoformat()}
    v.finished_at = _now()
    v.heartbeat_at = None
    run.db.flush()


def _diagnostic(run: _Run, stage: str, code: str, message: str, retryable: bool, *, from_provider: bool) -> dict[str, Any] | None:
    """The W1 ``ai/diagnostics.py`` payload of a model-path failure (code, stage, purpose, config, call ids, recovery).
    Failures that are not about the model (unreadable sources, the chunk budget, an interruption) get none."""
    from ..ai import diagnostics

    if code == "source_frame_unresolved":
        return None  # A recognized source-frame capability limit, not an invalid model reply.
    diag_stage: str | None
    if from_provider or stage == "provider_select":
        # a live restriction met before a request (cleared / revoked / disabled …) is a resolve failure too
        diag_stage = "resolve" if stage == "provider_select" else None
    elif stage in ("parse", "validate"):
        code, diag_stage = "invalid_plan", "validation"  # the reply did not pass the server's checks (as planning reports it)
    elif stage == "over_budget" and code == "context_over_budget":
        diag_stage = "context"
    else:
        return None
    snap = (run.v.planning or {}).get("model_snapshot")
    return diagnostics.diagnostic(code, message=message, stage=diag_stage, purpose=agent.PROVIDER_PURPOSE,
                                  snapshot=snap if isinstance(snap, dict) else None, call_ids=list(run.call_ids),
                                  retryable=None if stage == "provider_select" else retryable)


def _read_sources(run: _Run) -> None:
    """Read every source of the version and write its chunk rows (answer versions inherit the parent's)."""
    db, v = run.db, run.v
    seq = 0
    if v.origin == "answer" and v.parent_version_id:
        for c in db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.parent_version_id)
                            .order_by(EngineeringSpecChunk.seq)).all():
            row = EngineeringSpecChunk(version_id=v.id, seq=c.seq, source_id=c.source_id, source_chunk=c.source_chunk, kind=c.kind,
                                       locator=c.locator, start=c.start, end=c.end, chars=c.chars, text_sha256=c.text_sha256, status=c.status,
                                       reason=c.reason, ai_call_id=c.ai_call_id, sent_at=c.sent_at, meta=dict(c.meta or {}),
                                       findings=c.findings, call_id=c.call_id,
                                       inherited_from_version_id=c.inherited_from_version_id or c.version_id)
            db.add(row)
            run.chunks[c.seq] = _ChunkRef(c.seq, c.source_id, c.kind, c.locator, c.start, c.end, c.status, c.text_sha256, dict(c.meta or {}), row)
            seq = max(seq, c.seq)
    new_answers = {a["source"] for a in v.answers} if v.origin == "answer" else set()
    for s in v.sources:
        sid = s["id"]
        if v.origin == "answer" and sid not in new_answers:
            # Inherited source: the version that read it keeps its chunks; its manifest entry is reused.
            run.sources[sid] = _inherited_source(db, s)
            run.order.append(sid)
            continue
        src = _read_one(db, s)
        run.sources[sid] = src
        run.order.append(sid)
        for ch in src.chunks:
            seq += 1
            sha = None
            if ch.kind == "attachment":
                sha = s.get("sha256")
            elif ch.text:
                sha = save_blob(db, ch.text.encode("utf-8"), TEXT_CONTENT_TYPE)
            row = EngineeringSpecChunk(version_id=v.id, seq=seq, source_id=sid, source_chunk=ch.source_chunk, kind=ch.kind, locator=ch.locator[:300],
                                       start=ch.start, end=ch.end, chars=len(ch.text or ""), text_sha256=sha, status="pending",
                                       meta={**ch.meta, **({"mime_type": ch.mime_type} if ch.mime_type else {})})
            db.add(row)
            run.chunks[seq] = _ChunkRef(seq, sid, ch.kind, ch.locator, ch.start, ch.end, "pending", sha, dict(row.meta), row, text=ch.text,
                                        data=ch.data, mime_type=ch.mime_type)
    if v.origin == "continue" and v.parent_version_id:
        _reuse_read_chunks(run, v.parent_version_id)
    db.flush()
    _write_manifest(run)
    readable = [s for s in run.sources.values() if s.error is None and (s.chunks or any(c.source_id == s.id for c in run.chunks.values()))]
    if not readable:
        problems = [{"source": s.id, "label": s.label, **(s.error or {"code": "source_empty", "message": "nothing to read"})}
                    for s in run.sources.values()]
        raise _Failed("context_build", "no_readable_source", "none of the sources could be read", detail={"sources": problems})


def _reuse_read_chunks(run: _Run, parent_id: uuid.UUID) -> None:
    """Continue (S3): a chunk the parent really sent and whose findings it stored is not sent again when the new chunk is
    the same (same position, source and text hash: re-chunking, e.g. the S4 overlap, changes the hash and the chunk is read
    again) and its findings were read with the current prompt version (S7: older findings are in another format and are
    read again). It keeps the record of the call that read it (``ai_call_id``, ``sent_at``) and the version that did.

    Generation reliability K (design §2.4, AC06): a chunk is matched within the same pinned file version (the source's
    content hash in both versions) by its place in that source and its text, not by its position in the whole version:
    extra chunks of one source (a drawing's outside-frame groups) never make the chunks of later sources be read again."""
    parent = run.db.get(EngineeringSpecVersion, parent_id)
    pinned = {str(s.get("id")): s.get("sha256") for s in (parent.sources if parent is not None else None) or [] if isinstance(s, dict)}
    now = {str(s.get("id")): s.get("sha256") for s in run.v.sources or [] if isinstance(s, dict)}
    prev = {(c.source_id, c.source_chunk, c.kind, c.text_sha256): c
            for c in run.db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == parent_id)).all()
            if c.status == "sent" and c.findings is not None and c.text_sha256
            and (c.meta or {}).get("prompt_version") == agent.SPEC_PROMPT_VERSION}
    for c in run.chunks.values():
        if pinned.get(c.source_id) != now.get(c.source_id):
            continue  # another file version under the same source id: read again
        p = prev.get((c.source_id, c.row.source_chunk, c.kind, c.text_sha256))
        if p is None:
            continue
        row = c.row
        row.status, row.reason, row.ai_call_id, row.sent_at = "sent", None, p.ai_call_id, p.sent_at
        row.findings, row.call_id = p.findings, p.call_id
        image_meta = {k: val for k, val in (p.meta or {}).items() if k in ("image_sha256", "image_attached", "image_width", "image_height", "render_error")}
        row.meta = {**(row.meta or {}), **image_meta, "prompt_version": agent.SPEC_PROMPT_VERSION}
        c.meta = dict(row.meta)
        row.inherited_from_version_id = p.inherited_from_version_id or p.version_id
        c.status = "sent"


def _read_one(db: Session, s: dict[str, Any]) -> sr.Source:
    sid = s["id"]
    if s.get("kind") == "text" or (not s.get("file_version_id") and s.get("sha256")):
        try:
            data, _ = read_blob(db, s["sha256"])
        except (FileNotFoundError, OSError) as exc:
            return sr.Source(id=sid, kind="text", label=s.get("label") or sid, error={"code": "blob_missing", "message": str(exc)[:300]})
        return sr.text_source(sid, data.decode("utf-8"), label=s.get("label") or sid, answer_of=s.get("answer_of"))
    fv = db.get(FileVersion, sr.uuid_or_none(s.get("file_version_id")))
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None:
        return sr.Source(id=sid, kind="file_version", label=s.get("label") or sid, file_version_id=s.get("file_version_id"),
                         error={"code": "file_version_missing", "message": "the pinned file version no longer exists"})
    if fv.blob_sha256 != s.get("sha256"):
        return sr.Source(id=sid, kind="file_version", label=s.get("label") or sid, file_version_id=s.get("file_version_id"),
                         error={"code": "sha_mismatch", "message": "the file version's content changed since it was pinned"})
    return sr.file_version_source(db, sid, fv, f)


def _inherited_source(db: Session, s: dict[str, Any]) -> sr.Source:
    src = sr.Source(id=s["id"], kind=s.get("kind") or "text", label=s.get("label") or s["id"], name=s.get("name") or "",
                    file_version_id=s.get("file_version_id"), file_id=s.get("file_id"), version=s.get("version"), sha256=s.get("sha256") or "",
                    bytes=s.get("bytes") or 0, content_type=s.get("content_type") or "", answer_of=s.get("answer_of"),
                    error=s.get("error") if s.get("read_status") == "failed" else None, not_chunked=list(s.get("not_chunked") or []),
                    notes=list(s.get("notes") or []), coverage=s.get("coverage") if isinstance(s.get("coverage"), dict) else None)
    src.para_starts = list(s.get("_para_starts") or [])
    src.entities = set(s.get("_entities") or [])
    return src


def _write_manifest(run: _Run) -> None:
    """Read status / ranges per source, computed from the chunk rows (what the server really sent)."""
    by_source: dict[str, list[_ChunkRef]] = {}
    for c in sorted(run.chunks.values(), key=lambda c: c.seq):
        by_source.setdefault(c.source_id, []).append(c)
    out = []
    old = {s["id"]: s for s in run.v.sources}
    for sid in run.order or [s["id"] for s in run.v.sources]:
        src = run.sources.get(sid)
        base = dict(old.get(sid) or {})
        if src is not None:
            base.update({k: v for k, v in src.manifest().items() if v not in (None, "") or k in ("answer_of",)})
            base["_para_starts"] = src.para_starts[:5000]
            base["_entities"] = sorted(src.entities)[:20000]
        chunks = by_source.get(sid, [])
        statuses = [c.row.status for c in chunks]
        err = src.error if src is not None else base.get("error")
        if isinstance(base.get("coverage"), dict):
            # generation reliability K (design §2.4): source coverage and chunk progress side by side
            base["coverage"] = {**base["coverage"], "chunks": len(chunks), "chunks_sent": sum(1 for s in statuses if s == "sent")}
        base["read_status"] = sr.read_status(err, statuses, list(base.get("not_chunked") or []), base.get("coverage"))
        base["ranges"] = [c.locator for c in chunks if c.row.status == "sent"]
        base["unread"] = [{"chunk": c.seq, "locator": c.locator, "status": c.row.status, "reason": c.row.reason}
                          for c in chunks if c.row.status != "sent"]
        base["chunks"] = len(chunks)
        digest = (((run.v.planning or {}).get("skeleton") or {}).get("by_source") or {}).get(sid)
        if digest is not None:  # generation reliability A: what this source gave the structure digest
            base["skeleton"] = digest
        text_chunks = [c for c in chunks if c.kind == "text" and c.start is not None and c.end is not None]
        if text_chunks:  # overlapping chunks: every character read is counted once
            base["chars_read"] = sr.covered_chars([(int(c.start), int(c.end)) for c in text_chunks  # type: ignore[arg-type]
                                                   if c.row.status == "sent"])
        base["error"] = err
        out.append(base)
    run.v.sources = out


def _check_budget(run: _Run) -> None:
    """More chunks than the budget: fail before any call, listing every part that would stay unread (W0 §7)."""
    new = [c for c in sorted(run.chunks.values(), key=lambda c: c.seq) if c.row.inherited_from_version_id is None]
    if len(new) <= sr.MAX_CHUNKS:
        return
    beyond = new[sr.MAX_CHUNKS:]
    for c in new:
        c.row.reason = "over_budget" if c in beyond else "not_started"
    parts: dict[str, dict[str, Any]] = {}
    for c in beyond:
        p = parts.setdefault(c.source_id, {"source": c.source_id, "label": run.sources[c.source_id].label, "chunks": 0, "locators": []})
        p["chunks"] += 1
        p["locators"].append(c.locator)
    raise _Failed("over_budget", "source_over_budget",
                  f"the sources need {len(new)} chunks of about {sr.CHUNK_CHARS} characters; one spec version reads at most {sr.MAX_CHUNKS}",
                  detail={"chunks_needed": len(new), "max_chunks": sr.MAX_CHUNKS, "chunk_chars": sr.CHUNK_CHARS, "unread": list(parts.values())})


def _select_provider(run: _Run) -> ModelProvider:
    """The provider of the snapshot frozen when the version was created (W1); live restrictions still apply."""
    from . import ai as ai_svc

    frozen = dict(run.v.planning or {})
    snap = frozen.get("model_snapshot")
    try:
        err = frozen.get("model_error")
        if not ai_svc.is_snapshot(snap) and err:
            raise ProviderError(err.get("code") or "missing_key", err.get("message") or "no usable model configuration",
                                retryable=bool(err.get("retryable")))
        provider, cfg = ai_svc.provider_for(run.db, agent.PROVIDER_PURPOSE, ai_svc.frozen_ref(snap, run.v.model_config_id))
    except ProviderError as exc:
        raise _Failed("provider_select", exc.code, exc.message, retryable=bool(exc.retryable)) from exc
    if run.stop is not None:
        previous_guard = provider._guard
        def guarded():
            run.stop()
            if previous_guard is not None:
                previous_guard()
        provider.bind_guard(guarded)
    run.v.simulated = bool(provider.simulated)
    reused = [c for c in run.chunks.values() if c.row.inherited_from_version_id is not None and c.row.findings is not None
              and run.v.origin == "continue"]
    if reused:  # findings read by a simulated call stay labelled simulated in the version that reuses them
        sources = {c.row.inherited_from_version_id for c in reused}
        run.v.simulated = run.v.simulated or any(bool(getattr(run.db.get(EngineeringSpecVersion, s), "simulated", False)) for s in sources)
    # input_dedupe (generation supplement A item 5): how the inputs were prepared before the version was created.
    run.v.planning = {**{k: frozen[k] for k in ("model_snapshot", "continued_from", "input_dedupe", "revision") if k in frozen},
                      "provider": provider.provider_name,
                      "model_id": provider.model_id, "model_config_id": provider.config_id,
                      "purpose": agent.PROVIDER_PURPOSE, "simulated": bool(run.v.simulated), "prompt_version": agent.SPEC_PROMPT_VERSION,
                      "read_concurrency": max(1, int(limits.SPEC_READ_CONCURRENCY)),
                      "chunks_total": len([c for c in run.chunks.values() if c.row.inherited_from_version_id is None]), "chunks_sent": 0,
                      **({"chunks_reused": len(reused), "reused_call_ids": sorted({str(c.row.ai_call_id) for c in reused if c.row.ai_call_id})}
                         if reused else {}),
                      "ai_call_ids": [], "repairs": 0, "started_at": _now().isoformat()}
    return provider


def _heartbeat_spec(version_id: uuid.UUID, spec_id: uuid.UUID, project_id: uuid.UUID, started_at: str,
                    operation_id: uuid.UUID | None, generation: int | None, deadline: float) -> bool:
    """Only pulse a committed, exact extraction; no caller session or business writes cross threads."""
    if time.monotonic() >= deadline:
        return False
    with session_scope() as db:
        if operation_id is not None:
            owner_query = select(AiOperation).where(AiOperation.id == operation_id,
                AiOperation.project_id == project_id, AiOperation.generation == generation,
                AiOperation.status.in_(("pending", "running")),
                AiOperation.result["spec_version_id"].astext == str(version_id),
                AiOperation.result["spec_id"].astext == str(spec_id))
            op = db.scalar(owner_query.with_for_update(skip_locked=True, read=True))
            if op is None:
                return db.scalar(owner_query.with_only_columns(AiOperation.id)) is not None
        query = select(EngineeringSpecVersion).where(EngineeringSpecVersion.id == version_id,
            EngineeringSpecVersion.spec_id == spec_id, EngineeringSpecVersion.project_id == project_id,
            EngineeringSpecVersion.status == "extracting", EngineeringSpecVersion.planning["started_at"].astext == started_at)
        # Never wait for a spec lock while holding its operation. An extraction may own the spec
        # while recording its operation ledger; a skipped pulse simply retries on the next interval.
        version = db.scalar(query.with_for_update(skip_locked=True))
        if version is None:
            return db.scalar(query.with_only_columns(EngineeringSpecVersion.id)) is not None
        if time.monotonic() >= deadline:
            return False
        version.heartbeat_at = _now()
        return True


@contextmanager
def _serial_call_heartbeat(run: _Run) -> Iterator[None]:
    # Workflow-owned versions may not be committed. Their existing engine lease heartbeat owns liveness;
    # never commit that transaction or expose a partially extracted version merely to publish a pulse.
    if run.commit is None:
        yield
        return
    version_id, spec_id, project_id = run.v.id, run.v.spec_id, run.v.project_id
    started_at = (run.v.planning or {}).get("started_at")
    operation_id = run.ai_operation_id
    operation = run.db.get(AiOperation, operation_id) if operation_id is not None else None
    generation = operation.generation if operation is not None else None
    request_limits = run.tracer.limits or call_retry.current_limits()
    deadline = time.monotonic() + call_retry.MAX_ATTEMPTS * (CAD_MODEL_CALL_S + call_retry.RETRY_AFTER_MAX_S)
    if request_limits is not None and request_limits.deadline is not None:
        deadline = min(deadline, request_limits.deadline)
    if not started_at or operation_id is not None and operation is None:
        yield
        return
    with background_heartbeat(lambda: _heartbeat_spec(version_id, spec_id, project_id, started_at,
            operation_id, generation, deadline), name=f"spec-heartbeat-{version_id}"):
        yield


def _call(run: _Run, *, purpose: str, system: str, messages: list[Message], schema_name: str, json_schema: dict[str, Any],
          images: list[ImagePart] | None = None, unit: str | None = None) -> Any:
    assert run.provider is not None
    run.stage = "provider_call"
    run.save()
    prompt_version = agent.STRUCTURE_REVIEW_PROMPT_VERSION if purpose == "spec_structure_review" else agent.SPEC_PROMPT_VERSION
    with _serial_call_heartbeat(run):
        return run.tracer.model_call(run.provider, purpose=purpose, system=system, system_version=prompt_version, messages=messages,
                                    json_schema=json_schema, schema_name=schema_name, images=images,
                                    timeout_s=CHUNK_CALL_TIMEOUT_S if purpose == "spec_extract" else SPEC_CALL_TIMEOUT_S, unit=unit)


def _strip_private(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _strip_private(v) for k, v in data.items() if not str(k).startswith("_")}
    if isinstance(data, list):
        return [_strip_private(x) for x in data]
    return data


@dataclass
class _ReadOutcome:
    """What a worker returns for one chunk: its call records and the canonical findings, the schema problems left after
    the repairs, or the provider error / unexpected exception that stopped it."""

    calls: list[Any]
    data: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    error: ProviderError | None = None
    crash: Exception | None = None
    repairs: int = 0


def _read_worker(provider: ModelProvider, messages: list[Message], images: list[ImagePart] | None, call_limits: Any = None,
                 unit: str | None = None) -> _ReadOutcome:
    """One chunk read with its schema repairs, in a worker thread. Only the provider is used here (each call through a
    tracer of its own); the database session never leaves the thread that owns it. Generation reliability J: the step's
    call limits (taken in the owning thread) and the unit ``spec_chunk:<seq>`` go with the tracer."""
    tracer = Tracer(limits=call_limits, unit=unit)
    repairs = 0
    while True:
        try:
            res = tracer.model_call(provider, purpose="spec_extract", system=agent.CHUNK_SYSTEM_PROMPT, system_version=agent.SPEC_PROMPT_VERSION,
                                    messages=messages, json_schema=agent.chunk_json_schema(), schema_name=agent.SCHEMA_CHUNK, images=images,
                                    timeout_s=CHUNK_CALL_TIMEOUT_S)
        except ProviderError as exc:
            return _ReadOutcome(list(tracer.calls), error=exc, repairs=repairs)
        except Exception as exc:  # noqa: BLE001 - reported on the version by the owning thread
            return _ReadOutcome(list(tracer.calls), crash=exc, repairs=repairs)
        data, problems = agent.chunk_problems(res.data, res.parse_error)
        if not problems:
            return _ReadOutcome(list(tracer.calls), data=data, repairs=repairs)
        if repairs >= MAX_REPAIRS:
            return _ReadOutcome(list(tracer.calls), problems=problems, repairs=repairs)
        repairs += 1
        messages = messages + [Message(role="assistant", content=clip(res.text, 30_000)), Message(role="user", content=agent.repair_message(problems))]


def _store_read(run: _Run, c: _ChunkRef, out: _ReadOutcome) -> BaseException | None:
    """Owning thread: record the worker's calls, update the chunk row and store its findings at once (S3). Returns the
    failure to raise once the calls in flight have returned (None when the chunk was read)."""
    run.tracer.calls.extend(out.calls)
    ids = run.record()
    returned = out.data is not None or bool(out.problems) or out.repairs > 0
    if returned:  # sent: the call that carried the chunk returned
        c.row.status, c.row.sent_at = "sent", _now()
        c.row.ai_call_id = uuid.UUID(ids[0]) if ids else None
        run.v.planning = {**run.v.planning, "chunks_sent": int(run.v.planning.get("chunks_sent", 0)) + 1}
    run.v.planning = {**run.v.planning, "ai_call_ids": list(run.call_ids), "repairs": int(run.v.planning.get("repairs", 0)) + out.repairs}
    failure: BaseException | None = None
    if out.data is not None:
        visual_problems = _visual_chunk_problems(run, c, out.data)
        if visual_problems:
            c.row.status, c.row.reason = "failed", "visual_evidence_invalid"
            failure = _Failed("validate", "visual_evidence_invalid", "image evidence could not be located in the attached source",
                              detail={"chunk": c.seq, "problems": visual_problems[:20]}, retryable=True)
        else:
            c.row.findings = out.data
            c.row.call_id = uuid.UUID(ids[-1]) if ids else None
            c.row.meta = {**(c.row.meta or {}), "prompt_version": agent.SPEC_PROMPT_VERSION}
            c.meta = dict(c.row.meta)
    elif out.problems:
        failure = _Failed("parse", "chunk_reply_invalid", f"the reply for chunk {c.seq} ({c.locator}) did not match the schema",
                          detail={"chunk": c.seq, "source": c.source_id, "problems": out.problems[:20]}, retryable=True)
    else:
        c.row.status, c.row.reason = "failed", "provider_error" if out.error is not None else "internal_error"
        c.row.ai_call_id = uuid.UUID(ids[-1]) if ids else None
        failure = out.error if out.error is not None else out.crash
    c.status = c.row.status
    _write_manifest(run)
    run.save()
    return failure


def _drawing_region_problem(c: _ChunkRef, region: Any) -> str | None:
    meta = c.meta or {}
    if (c.kind != "drawing" or not meta.get("image_attached") or not meta.get("frame")
            or not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("image_sha256") or ""))):
        return "a drawing region requires the original frame image actually attached to this chunk"
    width, height = meta.get("image_width"), meta.get("image_height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return "the attached drawing image has no valid pixel dimensions"
    if (not isinstance(region, list) or len(region) != 4
            or any(isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) for x in region)):
        return "a drawing image region must contain four finite pixel coordinates [x,y,w,h]"
    x, y, w, h = region
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        return "the drawing image region is outside the attached image or has non-positive size"
    return None


def _visual_chunk_problems(run: _Run, c: _ChunkRef, data: dict[str, Any]) -> list[str]:
    """Fail before merge; invalid image evidence must never become a Turbo assumption."""
    val = _Validator(run, policy=run.v.execution_policy, parent=None)
    comps = data.get("comps") or []
    uses = data.get("uses") or []
    items = [*comps, *uses, *data.get("facts", []), *(f for d in comps for f in d.get("fs", []))]
    problems = []
    for item in items:
        if item.get("r") is None:
            continue
        _, problem = val.evidence({"source": c.source_id, "chunk": c.seq, "region": item["r"]})
        if problem:
            problems.append(problem)
    for comp in comps:
        if comp.get("r") is None:
            continue
        if comp.get("no") and sr.locate_quote(run.text_of(c), str(comp["no"])) is None:
            problems.append(f"visual component {comp.get('k')} must not invent drawing number {comp['no']}; omit no when not literal")
        parents = [u for u in uses if u.get("c") == comp.get("k") and u.get("p") != comp.get("k")]
        if not parents or not any(val.evidence({"source": c.source_id, "chunk": c.seq,
                                               "quote": u.get("q"), "entity": u.get("e"), "region": u.get("r")})[1] is None
                                  for u in parents):
            problems.append(f"visual component {comp.get('k')} has no located parent/count evidence")
    return problems


def _digest_sources(run: _Run) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(source id, label, [(chunk kind, text without its overlap)]) of every text / drawing chunk, for the digest."""
    out = []
    for sid in run.order:
        src = run.sources.get(sid)
        texts = [(c.kind, run.text_of(c)[int((c.meta or {}).get("overlap") or 0):])
                 for c in sorted(run.chunks.values(), key=lambda c: c.seq) if c.source_id == sid and c.kind in ("text", "drawing")]
        if texts:
            out.append((sid, src.label if src is not None else sid, texts))
    return out


def _quote_locator(run: _Run) -> Callable[[str], tuple[str, int] | None]:
    """Where a verbatim quote stands: (source, chunk) of the first text / drawing chunk holding it (S4 notation-insensitive
    matching); None when no chunk does."""
    index = [(c.source_id, c.seq, sr.normalise_quote(run.text_of(c)))
             for c in sorted(run.chunks.values(), key=lambda c: c.seq) if c.kind in ("text", "drawing")]

    def locate(quote: str) -> tuple[str, int] | None:
        q = sr.normalise_quote(str(quote).strip())
        if len(q) < 2:
            return None
        return next(((sid, seq) for sid, seq, text in index if q in text), None)

    return locate


def _read_skeleton(run: _Run, base: EngineeringSpecVersion | None) -> spec_merge.Skeleton | None:
    """Generation reliability A (design §6.1): the structure reading before the chunks — ONE ``spec_skeleton`` call over
    the structural digest of all sources (two when the digest is longer than one call takes; the second gets the first's
    components as ``<known>``), units ``skeleton:<n>``, schema repairs like every spec call. A continuation reuses the
    replies its parent stored for the same digest (no call). The record (digest size per source, what the cut left out,
    calls, the replies) is ``planning.skeleton``; None when no source has a structural line (attachments only)."""
    digest = sr.structure_digest(_digest_sources(run))
    record: dict[str, Any] = {**digest.record(), "prompt_version": agent.SPEC_PROMPT_VERSION}
    if not digest.parts:
        run.v.planning = {**run.v.planning, "skeleton": {**record, "skipped": "no structural line in the sources (prose or attachments only)"}}
        return None
    record["digest_sha256"] = sr.sha256_text("\n\x00\n".join(digest.parts))
    replies: list[dict[str, Any]] | None = None
    if run.v.origin == "continue" and run.v.parent_version_id:
        parent = run.db.get(EngineeringSpecVersion, run.v.parent_version_id)
        done = ((parent.planning or {}) if parent is not None else {}).get("skeleton") or {}
        if (done.get("digest_sha256") == record["digest_sha256"] and done.get("prompt_version") == agent.SPEC_PROMPT_VERSION
                and len(done.get("replies") or []) == len(digest.parts)):
            replies = list(done["replies"])
            record.update(reused_from=str(parent.id), reused_call_ids=list(done.get("call_ids") or []))  # type: ignore[union-attr]
    if replies is None:
        replies = []
        before = len(run.call_ids)
        for n, part in enumerate(digest.parts, 1):
            first = [c for r in replies for c in r.get("comps") or []]
            known = agent.skeleton_known([{"key": c["k"], "name": c.get("n"), "number": c.get("no"), "size": c.get("sz"), "aliases": c.get("al") or [],
                                           "kind": c.get("t"), "uses": [{"parent": u["p"], "n": u.get("n") or 1} for u in c.get("pa") or []]}
                                          for c in first]) if first else ""
            msg = agent.skeleton_message(policy=run.v.execution_policy, digest=part, part=n, parts=len(digest.parts), known=known,
                                         note=digest.note if n == 1 else "")
            _check_call_budget(run, agent.SKELETON_SYSTEM_PROMPT, msg, f"reading part {n} of the structure digest",
                               schema=agent.skeleton_json_schema())
            keys = {c["k"] for c in first}
            reply = _structured_with_repairs(run, purpose="spec_skeleton", system=agent.SKELETON_SYSTEM_PROMPT, first=msg,
                                             schema_name=agent.SCHEMA_SKELETON, schema=agent.skeleton_json_schema(),
                                             check=lambda data, keys=keys: agent.skeleton_problems(data, None, keys),  # type: ignore[misc]
                                             soft=agent.skeleton_geometry_problems, unit=f"skeleton:{n}", budgeted=True)
            replies.append(reply or {})
        record["call_ids"] = list(run.call_ids[before:])
    skeleton = spec_merge.skeleton_from(replies, previous=base.content if base is not None else None, locate=_quote_locator(run))
    record.update(replies=replies, entries=len(skeleton.entries), interfaces=len(skeleton.interfaces), root=skeleton.root,
                  problems=skeleton.problems[:50])
    run.v.planning = {**run.v.planning, "skeleton": record, "ai_call_ids": list(run.call_ids)}
    _write_manifest(run)
    run.save()
    return skeleton


def _drawing_chunk_image(run: _Run, c: _ChunkRef) -> ImagePart | None:
    """The original frame beside its literal index, cached across split chunks.

    Keep the literal chunk kind so entity/quote grounding still runs. Rendering
    failures are visible metadata and do not pretend a picture was attached.
    """
    from ..cad.drawing_evidence import DrawingEvidenceError, render_frame_png, render_size

    frame_id = str(c.meta.get("frame") or "")
    src = run.sources.get(c.source_id)
    if not frame_id or src is None or src.drawing_evidence is None or src.drawing_dxf is None:
        return None
    key = (c.source_id, frame_id)
    if key not in run.drawing_images:
        try:
            frame = src.drawing_evidence.frame(frame_id)
            png = render_frame_png(src.drawing_dxf, frame, px=2000)
            width, height = render_size(frame, 2000)
            meta = {"image_sha256": save_blob(run.db, png, "image/png"), "image_attached": True,
                    "image_width": width, "image_height": height}
        except DrawingEvidenceError as exc:
            png = None
            meta = {"image_attached": False, "render_error": {"code": exc.code, "message": str(exc)[:300]}}
        run.drawing_images[key] = (png, meta)
    png, meta = run.drawing_images[key]
    c.meta = {**c.meta, **(c.row.meta or {}), **meta}
    c.row.meta = dict(c.meta)
    return ImagePart(mime_type="image/png", data=png, name=f"{c.source_id}-{frame_id}.png") if png else None


def _read_chunks(run: _Run, base: EngineeringSpecVersion | None, skeleton: spec_merge.Skeleton | None = None) -> list[spec_merge.ChunkFindings]:
    """Read every pending chunk, up to ``limits.SPEC_READ_CONCURRENCY`` at once (S7). Chunks are independent: each gets
    the skeleton (keys, names, numbers, other names, interfaces) as ``<known>`` — without one (attachments only) the
    component names / numbers of ``base`` (the version re-extracted). A chunk is marked sent only after its call
    returned, and its findings are stored as soon as it does (S3). After the first failure no further chunk is started;
    the calls in flight finish and are kept, then the failure is raised (the rest stays unread for a continuation).
    Returns the findings of every chunk read (reused ones included), in chunk order."""
    assert run.provider is not None
    provider = run.provider
    vision = bool(provider.capabilities.vision)
    known = (agent.skeleton_known(skeleton.entries, skeleton.interfaces) if skeleton is not None and skeleton.entries else
             agent.known_digest(list((base.content or {}).get("definitions") or [])[:300] if base is not None else []))
    queue = deque(c for c in sorted(run.chunks.values(), key=lambda c: c.seq)
                  if c.row.inherited_from_version_id is None and c.row.status == "pending")
    concurrency = max(1, int(limits.SPEC_READ_CONCURRENCY))
    failures: list[tuple[int, BaseException]] = []
    from ..ai.retry import current_limits

    call_limits = current_limits()  # J: the worker threads do not see the engine's scope; hand it over
    run.stage = "provider_call"
    run.save()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="spec-read") as pool:
        running: dict[Future[_ReadOutcome], _ChunkRef] = {}

        def start() -> None:
            while queue and len(running) < concurrency and not failures:
                c = queue.popleft()
                images = None
                if c.kind == "attachment":
                    if not vision:
                        c.row.status, c.row.reason = "unread", "model_lacks_vision"
                        continue
                    images = [ImagePart(mime_type=c.mime_type or "image/png", data=c.data or b"", name=c.locator)]
                elif c.kind == "drawing" and vision:
                    image = _drawing_chunk_image(run, c)
                    images = [image] if image is not None else None
                msg = agent.chunk_message(policy=run.v.execution_policy, known=known, seq=c.seq, source_id=c.source_id, kind=c.kind,
                                          locator=c.locator, text=run.text_of(c), overlap=int((c.meta or {}).get("overlap") or 0))
                if images and c.kind == "drawing":
                    msg += "\nThe attached image renders this original sheet; use its section/profile geometry to distinguish " \
                           "features, and cite the literal entity ids for dimensions. Paper positions are not product coordinates."
                    msg += f"\nOriginal PNG: {c.meta['image_width']} x {c.meta['image_height']} pixels, top-left origin. " \
                           "For image-only observations cite r=[x,y,w,h] in these pixels, st=derived and how; pixels are not mm."
                running[pool.submit(copy_context().run, _read_worker, provider, [Message(role="user", content=msg)],
                                    images, call_limits, f"spec_chunk:{c.seq}")] = c

        start()
        while running:
            done, _ = wait(list(running), timeout=HEARTBEAT_S, return_when=FIRST_COMPLETED)
            if not done:
                run.save()  # heartbeat while the calls run
                continue
            for fut in sorted(done, key=lambda f: running[f].seq):
                c = running.pop(fut)
                failure = _store_read(run, c, fut.result())
                if failure is not None:
                    failures.append((c.seq, failure))
            start()
    if failures:
        raise min(failures, key=lambda x: x[0])[1]
    findings = [spec_merge.ChunkFindings(seq=c.seq, source=c.source_id, data=dict(c.row.findings), kind=c.kind,
                                         declares_mm=c.kind == "text" and spec_merge.declares_mm(run.text_of(c)))
                for c in sorted(run.chunks.values(), key=lambda c: c.seq) if c.row.status == "sent" and isinstance(c.row.findings, dict)]
    if not findings:
        raise _Failed("context_build", "nothing_sent", "no chunk could be sent to the model",
                      detail={"unread": [{"chunk": c.seq, "locator": c.locator, "reason": c.row.reason} for c in run.chunks.values()]})
    return findings


def _check_call_budget(run: _Run, system: str, message: str, what: str, *,
                       schema: dict[str, Any] | None = None, images: list[ImagePart] | None = None) -> None:
    """A resolve / answer call must fit the model's window; never trimmed (W0 §7)."""
    from . import ai as ai_svc
    from .assistant_context import _budget, estimate_tokens

    cfg = None
    if run.provider is not None and run.provider.config_id:
        cfg = next((c for c in ai_svc.enabled_configs(run.db) if c.id == run.provider.config_id), None)
    need = estimate_tokens(system) + estimate_tokens(message)
    if schema is not None:
        schema_cfg = None if getattr(run.provider, "simulated", False) else getattr(run.provider, "config", cfg)
        need += estimate_tokens(schema_budget_text(schema, schema_cfg)) + 256
    # The original sheet renderer bounds these PNGs at 2,000 px. Match the
    # conservative image estimate used by per-part modeling.
    need += 4096 * len(images or [])
    input_limit, _output_reserve, _safety, available, limit_source = _budget(cfg)
    if need > available:
        raise _Failed("over_budget", "context_over_budget", f"{what} needs about {need} tokens; the model has about {available}",
                      detail={"required_est": need, "available": available, "input_limit": input_limit, "limit_source": limit_source,
                              "estimated": True})


@dataclass
class _StructureReviewInput:
    context: dict[str, Any]
    message: str
    images: list[ImagePart]
    chunks: set[int]
    image_chunks: set[int]
    metadata: dict[str, Any]


def _structure_label(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _structure_identity_problem(run: _Run, review: _StructureReviewInput, content: dict[str, Any], decision: dict[str, Any]) -> str | None:
    """Validate the scopes of a catalog/detail/BOM inference; the model still owns its unverified engineering judgment."""
    proof = decision.get("identity_evidence")
    action = decision.get("action")
    if not proof and action == "exclude_catalog":
        return "catalog exclusion requires original catalog, installed detail and parent BOM evidence"
    if not proof:
        return None  # The normal identity guard still requires an explicit source equivalence.
    if action not in ("same_as", "exclude_catalog"):
        return "identity_evidence is only valid for same_as or exclude_catalog"
    defs = {d["key"]: d for d in content.get("definitions", [])}
    alias, target = defs.get(decision["child"]), defs.get(decision.get("target"))
    if alias is None or target is None:
        return "catalog identity inference has an unknown alias or target"
    if action == "exclude_catalog":
        problem = _catalog_exclusion_problem(run, content, alias, target)
        if problem:
            return problem
    normalize = _structure_label
    skeleton = next((s for s in content.get("skeleton", []) if s.get("key") == alias["key"]), {})
    catalog = proof["catalog"]
    locators = [f"{catalog['source']} · chunk {catalog['chunk']} ", f"{catalog['source']} · chunk {catalog['chunk']} ·"]
    def catalog_scope(source: Any) -> bool:
        # A provisional merge retains EvidenceRef dictionaries; final versions
        # serialize these as locator strings. Both must identify the same source
        # and chunk, never merely contain an item's name elsewhere in the file.
        if isinstance(source, dict):
            return source.get("source") == catalog["source"] and source.get("chunk") == catalog["chunk"]
        return any(str(source).startswith(loc) for loc in locators)

    if not any(catalog_scope(source) for source in skeleton.get("sources", [])):
        return "catalog evidence must cite this unplaced item's original skeleton declaration"

    def cited_text(evidence: dict[str, Any]) -> str:
        c = run.chunks.get(evidence["chunk"])
        if c is None:
            return ""
        if evidence.get("quote"):
            return str(evidence["quote"])
        ent = evidence.get("entity")
        return "\n".join(line for line in run.text_of(c).splitlines() if ent and re.search(
            rf"(?<![0-9A-Za-z/]){re.escape(str(ent))}(?![0-9A-Za-z/])", line))

    if not any(normalize(v) and normalize(v) in normalize(cited_text(catalog)) for v in (alias.get("name"), alias.get("number"))):
        return "catalog evidence must quote or cite the actual obsolete item identity, not just a count"
    if action == "exclude_catalog" and normalize(alias.get("name")) not in normalize(cited_text(catalog)):
        return "catalog exclusion must cite this alternative's complete name/variant, not a number shared by another variant"
    detail = proof["detail"]
    c = run.chunks.get(detail["chunk"])
    src = run.sources.get(detail["source"])
    frame = next((f for f in getattr(getattr(src, "drawing_evidence", None), "frames", [])
                  if c is not None and f.id == c.meta.get("frame")), None)
    from .drawing_identity import title_match
    if frame is None or title_match(target, number=frame.title.number.value if frame.title.number else None,
                                   name=frame.title.name.value if frame.title.name else None) != "canonical":
        return "detail evidence must be on the target's exact canonical drawing-number title sheet"
    title_values = [v.value for v in (frame.title.number, frame.title.name) if v is not None]
    title_ids = {v.text_id for v in (frame.title.number, frame.title.name) if v is not None}
    if detail.get("entity") not in title_ids and not any(normalize(v) in normalize(cited_text(detail)) for v in title_values if v):
        return "detail evidence must cite the target's actual title identity"
    parent_bom = proof["parent_bom"]
    c = run.chunks.get(parent_bom["chunk"])
    parents = {i.get("parent") for i in content.get("instances", []) if i.get("definition") == target["key"]}
    owners = {owner for sheet in review.metadata["sheets"] if c is not None and sheet["source"] == c.source_id
              and sheet["frame"] == c.meta.get("frame") for owner in sheet["assembly_definitions"]}
    if not parents.intersection(owners):
        return "parent_bom evidence must be on an actual immediate parent's assembly sheet"
    bom_text = normalize(cited_text(parent_bom))
    def identifies(component: dict[str, Any]) -> bool:
        name, number = normalize(component.get("name")), normalize(component.get("number"))
        # A parent number is not a match inside a child's longer drawing number.
        return bool(name and name in bom_text or number and re.search(
            rf"(?<![a-z0-9./-]){re.escape(number)}(?![a-z0-9./-])", bom_text))
    candidates = [target]
    if action == "exclude_catalog" and target.get("kind") == "assembly":
        candidates += [defs[i["definition"]] for i in content.get("instances", [])
                       if i.get("parent") == target["key"] and i.get("definition") in defs]
    if not any(identifies(member) for member in candidates):
        return "parent_bom evidence must identify the target member, not only an unrelated numeric cell"
    return None


def _catalog_exclusion_problem(run: _Run, content: dict[str, Any], entry: dict[str, Any], target: dict[str, Any]) -> str | None:
    """A source-role inference must not conceal an unread, detailed or actually used component."""
    from .drawing_identity import title_match
    key = entry["key"]
    problem = spec_identity.catalog_scope_problem(content, key, target["key"])
    if problem:
        return problem
    for source in run.sources.values():
        drawing = getattr(source, "drawing_evidence", None)
        if drawing is None:
            continue
        for frame in drawing.frames:
            if title_match(entry, number=frame.title.number.value if frame.title.number else None,
                           name=frame.title.name.value if frame.title.name else None):
                return "catalog exclusion refused: a compatible actual detail title exists in the original drawing set"
    for chunk in run.chunks.values():
        if chunk.kind == "drawing" and chunk.row.status != "sent":
            return "catalog exclusion requires every original drawing chunk to have been read"
        findings = chunk.row.findings or {}
        # The compact reader normally uses stable keys. Also catch an exact
        # component declaration under a local key; names alone cannot bind variants.
        keys = {key}
        for comp in findings.get("comps", []):
            if title_match(entry, number=comp.get("no"), name=comp.get("n")) == "canonical":
                keys.add(comp.get("k"))
        if any(use.get("p") in keys or use.get("c") in keys for use in findings.get("uses", [])):
            return "catalog exclusion refused: an original chunk contains a BOM/use claim for this entry"
    return None


def _drawing_title_bindings(run: _Run) -> list[dict[str, Any]]:
    """Pinned original titles with exact read-chunk evidence; no AI identity claim or guessed neighboring page."""
    titles: list[dict[str, Any]] = []
    for sid, source in run.sources.items():
        if source.drawing_evidence is None:
            continue
        for frame in source.drawing_evidence.frames:
            members = [c for c in run.chunks.values()
                       if c.source_id == sid and c.meta.get("frame") == frame.id and c.row.status == "sent"]
            evidence = []
            for field in ("number", "name"):
                value = getattr(frame.title, field, None)
                if value is None or not value.text_id:
                    continue
                pattern = re.compile(rf"(?<![0-9A-Za-z/]){re.escape(str(value.text_id))}(?![0-9A-Za-z/])")
                located = next((c for c in members if pattern.search(run.text_of(c))), None)
                if located is not None:
                    evidence.append({"source": sid, "chunk": located.seq, "entity": value.text_id,
                                     "quote": None, "region": None,
                                     "detail": f"original title {field}: {value.value}"})
            titles.append({"source": sid, "frame": frame.id,
                           "number": frame.title.number.value if frame.title.number else None,
                           "name": frame.title.name.value if frame.title.name else None,
                           "title_entities": {field: value.text_id for field in ("number", "name")
                                              if (value := getattr(frame.title, field, None)) is not None},
                           "chunk": evidence[0]["chunk"] if evidence else None,
                           "chunks": [c.seq for c in members], "title_evidence": evidence,
                           "source_sha256": source.sha256, "file_version_id": source.file_version_id})
    return titles


def _drawing_membership_bindings(run: _Run, skeleton: spec_merge.Skeleton | None,
                                 titles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Locate an own-parent BOM use in one original grouped number/name/count row."""
    if skeleton is None:
        return []
    from .drawing_identity import title_match, validated_title_updates
    findings = [spec_merge.ChunkFindings(c.seq, c.source_id, c.row.findings or {}, kind=c.kind)
                for c in run.chunks.values() if c.row.status == "sent"]
    updates = validated_title_updates(skeleton.entries, findings, titles)
    definitions = {e["key"]: {**e, **updates.get(e["key"], {})} for e in skeleton.entries}
    bindings = []
    for chunk in run.chunks.values():
        if chunk.kind != "drawing" or chunk.row.status != "sent":
            continue
        title = next((t for t in titles if t["source"] == chunk.source_id and t["frame"] == chunk.meta.get("frame")), None)
        if title is None:
            continue
        data = chunk.row.findings or {}
        components = {str(c["k"]): c for c in data.get("comps", []) if c.get("k")}
        rows = []
        for line in run.text_of(chunk).splitlines():
            if not line.startswith(f"drawing table row {title['frame']} "):
                continue
            cells = [(json.loads('"' + value + '"'), entity) for value, entity in
                     re.findall(r'"((?:\\.|[^"\\])*)" \[([^\]]+)\]', line)]
            if len(cells) >= 4:
                rows.append((line, cells))
        for use in data.get("uses", []):
            parent = definitions.get(str(use.get("p")))
            if parent is None or parent.get("kind") != "assembly" or title_match(
                    parent, number=title["number"], name=title["name"]) != "canonical":
                continue
            local = str(use.get("c"))
            comp, known = components.get(local, {}), definitions.get(local, {})
            child = {"number": comp.get("no") or known.get("number"), "name": comp.get("n") or known.get("name"),
                     "size": comp.get("sz") or known.get("size"), "kind": comp.get("t") or known.get("kind")}
            identity = spec_identity.identity_key(child)
            if not child["number"] or identity is None or identity.startswith("name:"):
                continue
            quantity = use.get("n", 1)
            matches = []
            for line, cells in rows:
                for index in range(len(cells) - 2):
                    (number, number_id), (name, name_id), (count, count_id) = cells[index:index + 3]
                    # The literal row supplies its size through its own member
                    # designation. A shared standard number alone is insufficient.
                    row_identity = spec_identity.identity_key({"number": number, "name": name})
                    if row_identity != identity or not count.strip().isdigit() or int(count.strip()) != quantity:
                        continue
                    if not identity.startswith("std:") and title_match(child, number=number, name=name) != "canonical":
                        continue  # A reused drawing number cannot validate another variant's BOM membership.
                    if known and not identity.startswith("std:") and title_match(known, number=number, name=name) != "canonical":
                        continue  # The proof must identify the final stable definition, not merely a mislabeled local key.
                    if use.get("e") != count_id and not (use.get("q") and sr.locate_quote(line, use["q"])):
                        continue
                    evidence = [{"source": chunk.source_id, "chunk": chunk.seq, "entity": entity,
                                 "quote": None, "region": None, "detail": f"own-parent BOM {role}"}
                                for role, entity in (("number", number_id), ("designation", name_id), ("quantity", count_id))]
                    matches.append({"source": chunk.source_id, "chunk": chunk.seq, "frame": title["frame"],
                        "parent": parent["key"], "local_child": local, "child_identity": identity,
                        "quantity": quantity, "row": line, "evidence": evidence,
                        "source_sha256": title["source_sha256"], "file_version_id": title["file_version_id"]})
            if len(matches) == 1:
                bindings.append(matches[0])
    return bindings


def _structure_review_context(run: _Run, content: dict[str, Any], record: dict[str, Any] | None = None) -> _StructureReviewInput | None:
    """Complete graph and relevant original sheets; geometry-only detail pages are outside this review's scope."""
    drawing_sources = {sid: src for sid, src in run.sources.items() if src.drawing_evidence is not None}
    assemblies = [d for d in content.get("definitions", []) if d.get("kind") == "assembly"]
    if not drawing_sources or not assemblies:
        return None  # Text-only specifications retain their existing extraction path.
    context = spec_structure_review.review_context(content, record)
    from .drawing_identity import title_match

    index: list[dict[str, Any]] = []
    selected: set[tuple[str, str]] = set()
    sheet_owners: dict[tuple[str, str], list[str]] = {}
    for sid, src in drawing_sources.items():
        for frame in src.drawing_evidence.frames:
            number = frame.title.number.value if frame.title.number else None
            name = frame.title.name.value if frame.title.name else None
            owners = [d["key"] for d in assemblies if title_match(d, number=number, name=name) == "canonical"]
            key = (sid, frame.id)
            index.append({"source": sid, "frame": frame.id, "number": number, "name": name,
                          "assembly_definitions": owners})
            if owners:
                selected.add(key)
                sheet_owners[key] = owners
    # A visual assembly without its own detail can be reviewed only on the exact
    # sheet already cited for it, never by guessing a neighboring page.
    assembly_keys = {d["key"] for d in assemblies}
    for fact in content.get("facts", []):
        if fact.get("subject") in {f"{k}.visual_presence" for k in assembly_keys}:
            for ev in fact.get("evidence", []):
                c = run.chunks.get(ev.get("chunk"))
                if c is not None and c.source_id == ev.get("source") and c.meta.get("frame"):
                    selected.add((c.source_id, c.meta["frame"]))
    if not selected:
        declared_in_drawing = False
        for entry in content.get("skeleton", []):
            if entry.get("key") not in assembly_keys:
                continue
            for locator in entry.get("sources", []):
                source_id = locator.get("source") if isinstance(locator, dict) else str(locator).split(" · ", 1)[0]
                declared_in_drawing |= source_id in drawing_sources
        if not declared_in_drawing:
            # An assembly described in text can include a separate part-detail
            # DXF; that does not create an absent assembly drawing to review.
            return None
        raise _Failed("context_build", "structure_sheets_missing", "no original assembly title sheet could be identified",
                      detail={"assemblies": sorted(assembly_keys)}, retryable=True)

    cited: set[int] = set()
    def references(value: Any) -> None:
        if isinstance(value, dict):
            seq = value.get("chunk")
            if isinstance(seq, int) and value.get("source"):
                c = run.chunks.get(seq)
                if c is not None and c.source_id == value["source"]:
                    cited.add(seq)
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
        elif isinstance(value, str):
            # Final skeleton provenance is a server-formatted locator string;
            # its original compact declarations do not retain EvidenceRef dicts.
            match = re.match(r"^([^\s·]+)\s+·\s+chunk\s+(\d+)\b", value)
            if match:
                c = run.chunks.get(int(match[2]))
                if c is not None and c.source_id == match[1]:
                    cited.add(c.seq)
    references(context.get("quantity_facts"))
    references(context.get("quantity_conflicts"))
    references(context.get("restated"))
    # Identity review needs the actual catalogue declaration, not an inferred
    # synonym or the target's similarly numbered detail.
    orphan_keys = {c["child"] for c in context["candidates"] if c["kind"] == "unplaced"}
    references([s for s in context.get("skeleton_claims", []) if s.get("key") in orphan_keys])
    # Candidate selection only chooses additional source pages, never merges
    # definitions. The complete graph/title index remains visible to the model.
    from .spec_aliases import candidates as identity_candidates
    defs = {d["key"]: d for d in content.get("definitions", [])}
    identity_targets = {key: identity_candidates(content, key) for key in sorted(orphan_keys)}
    target_keys = {k for keys in identity_targets.values() for k in keys}
    identity_frames = {(row["source"], row["frame"]) for row in index if any(
        title_match(defs[k], number=row["number"], name=row["name"]) == "canonical" for k in target_keys)}
    context["catalog_identity_candidates"] = identity_targets
    context["catalog_identity_candidate_scope"] = (
        "These are bounded hints (at most eight targets per orphan) used only to select extra literal detail pages. "
        "The complete graph and title index remain included. If another target lacks its required detail/BOM evidence "
        "in the supplied chunks, report unresolved; do not fabricate an identity citation.")

    chunks = [c for c in sorted(run.chunks.values(), key=lambda c: c.seq)
              if c.row.status == "sent" and (c.seq in cited or (c.source_id, c.meta.get("frame")) in selected | identity_frames)]
    images: list[ImagePart] = []
    image_chunks: set[int] = set()
    sheets: list[dict[str, Any]] = []
    assert run.provider is not None
    vision = bool(getattr(run.provider.capabilities, "vision", False))
    if not vision:
        raise _Failed("context_build", "model_lacks_vision", "the final drawing structure review needs a vision-capable planning model")
    for sid, frame_id in sorted(selected):
        members = [c for c in chunks if c.source_id == sid and c.meta.get("frame") == frame_id]
        if not members:
            raise _Failed("context_build", "structure_sheet_unread", "an original assembly sheet was not read",
                          detail={"source": sid, "frame": frame_id})
        first = members[0]
        image = _drawing_chunk_image(run, first)
        if image is None:
            raise _Failed("context_build", "structure_image_unavailable", "an original assembly image could not be attached",
                          detail={"source": sid, "frame": frame_id, "render_error": first.meta.get("render_error")}, retryable=True)
        images.append(image)
        for c in members:
            # Split chunks all refer to this same original frame, including on
            # resume where its transient render cache has been reconstructed.
            c.meta = {**c.meta, **(c.row.meta or {}),
                      **{k: first.meta[k] for k in ("image_attached", "image_sha256", "image_width", "image_height")}}
            c.row.meta = dict(c.meta)
            image_chunks.add(c.seq)
        src = run.sources[sid]
        sheets.append({"source": sid, "frame": frame_id, "assembly_definitions": sheet_owners.get((sid, frame_id), []),
                       "chunks": [c.seq for c in members], "image_name": image.name,
                       "image_sha256": first.meta["image_sha256"], "image_width": first.meta["image_width"],
                       "image_height": first.meta["image_height"], "source_sha256": src.sha256,
                       "file_version_id": src.file_version_id})
    context["drawing_title_index"] = index
    context["attachment_titles"] = _drawing_title_bindings(run)
    context["attached_sheets"] = sheets
    context["assemblies_without_own_title_sheet"] = sorted(assembly_keys - {k for owners in sheet_owners.values() for k in owners})
    context["raw_structural_findings"] = [
        {"source": c.source_id, "chunk": c.seq,
         "components": [{k: v for k, v in d.items() if k in ("k", "n", "no", "sz", "al", "t", "of", "q", "e", "r", "b")}
                        for d in (c.row.findings or {}).get("comps", [])],
         "uses": (c.row.findings or {}).get("uses", [])}
        for c in sorted(run.chunks.values(), key=lambda c: c.seq) if isinstance(c.row.findings, dict)
        and ((c.row.findings or {}).get("uses") or any(d.get("r") for d in (c.row.findings or {}).get("comps", [])))]
    from . import spec_visual_identity
    titled = {d["key"] for d in content.get("definitions", []) for row in index
              if title_match(d, number=row["number"], name=row["name"]) is not None}
    visual = spec_visual_identity.declarations(content, sheets, titled, _Validator(run, run.v.execution_policy, None).evidence)
    context["visual_declarations"] = visual
    context["candidates"].extend(spec_visual_identity.candidates(visual))
    blocks = ["Original image regions use the listed PNG width/height in pixels, top-left origin. Pixels are not mm."]
    blocks += [f'<chunk source="{c.source_id}" number="{c.seq}" frame="{c.meta.get("frame") or ""}">\n'
               f'{run.text_of(c)}\n</chunk>' for c in chunks]
    message = agent.structure_review_message(policy=run.v.execution_policy, context=context, sources="\n".join(blocks))
    metadata = {"sheets": sheets, "literal_chunks": [c.seq for c in chunks], "drawing_title_index": index,
                "context_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "message_chars": len(message), "prompt_version": agent.STRUCTURE_REVIEW_PROMPT_VERSION}
    return _StructureReviewInput(context, message, images, {c.seq for c in chunks}, image_chunks, metadata)


def _structure_member_problem(run: _Run, review: _StructureReviewInput, content: dict[str, Any], decision: dict[str, Any]) -> str | None:
    parent, child, target = (decision.get(k) for k in ("parent", "child", "target"))
    definitions = {d["key"]: d for d in content.get("definitions", [])}
    if parent not in definitions or child not in definitions or target not in definitions:
        return "member replacement needs three existing definition keys"
    old_identity, target_identity = (spec_identity.identity_key(definitions[k]) for k in (child, target))
    old = next((s for s in content.get("skeleton", []) if s.get("key") == child), {})
    if not any(p.get("parent") == parent for p in old.get("parents", [])):
        return "member replacement must retain an original skeleton binding for this parent and old child"
    rows = [b for b in review.context.get("source_memberships", [])
            if b.get("parent") == parent and b.get("child") == target and b.get("child_identity") == target_identity]
    if not rows:
        return "member replacement needs a verified original own-parent BOM row for the target identity"
    # An original row for both standards makes them legitimate separate uses.
    # Inspect every supplied chunk of the exact parent's sheet, independent of
    # whether the model extracted a corresponding old-standard use.
    for sheet in review.metadata.get("sheets", []):
        if parent not in sheet["assembly_definitions"]:
            continue
        for seq in sheet["chunks"]:
            for line in run.text_of(run.chunks[seq]).splitlines():
                if not line.startswith(f"drawing table row {sheet['frame']} "):
                    continue
                cells = [json.loads('"' + value + '"') for value in re.findall(r'"((?:\\.|[^"\\])*)" \[[^\]]+\]', line)]
                if any(spec_identity.identity_key({"number": cells[j], "name": cells[j + 1]}) == old_identity
                       for j in range(len(cells) - 1)):
                    return "the parent's original BOM independently names the old standard too; keep both memberships"
    for row in rows:
        citations = [e for e in decision.get("evidence", [])
                     if e.get("source") == row["source"] and e.get("chunk") == row["chunk"]]
        cells = {entity: json.loads('"' + value + '"') for value, entity in
                 re.findall(r'"((?:\\.|[^"\\])*)" \[([^\]]+)\]', row["row"])}
        if all(any(e.get("entity") == ev["entity"] or e.get("quote") and
                   sr.locate_quote(e["quote"], cells[ev["entity"]]) is not None for e in citations)
               for ev in row["evidence"]):
            return None
    return "member replacement must cite all three actual BOM cells: standard number, size/designation and quantity"


def _review_structure(run: _Run, content: dict[str, Any], record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    review = _structure_review_context(run, content, record)
    if review is None:
        return []
    defs = {d["key"]: d for d in content.get("definitions", [])}
    required = {(c.get("parent"), c["child"]) for c in review.context["candidates"]}
    validator = _Validator(run, run.v.execution_policy, None)
    located: list[dict[str, Any]] = []
    validated_identity: set[tuple[str, str]] = set()
    validated_catalog_exclusions: set[tuple[str, str]] = set()
    validated_member_replacements: set[tuple[str, str, str]] = set()
    validated_visual_duplicates: set[tuple[str, str, str]] = set()
    validated_assembly_attachments: set[tuple[str, str]] = set()
    validated_scope_bindings: list[dict[str, Any]] = []
    sentences = [{"text": line, "source": c.source_id, "locator": f"chunk {c.seq}"}
                 for c in run.chunks.values() if c.seq in review.chunks for line in run.text_of(c).splitlines()]

    def check(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            reply = agent.StructureReviewReply.model_validate(data).model_dump(mode="json")
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:20]
        problems: list[str] = []
        seen: set[tuple[str | None, str]] = set()
        located.clear()
        validated_identity.clear()
        validated_catalog_exclusions.clear()
        validated_member_replacements.clear()
        validated_visual_duplicates.clear()
        validated_assembly_attachments.clear()
        validated_scope_bindings.clear()
        for d in reply["decisions"]:
            pair = (d.get("parent"), d["child"])
            if pair in seen:
                problems.append(f"duplicate decision for {pair}")
            seen.add(pair)
            if d["action"] == "attach_assembly":
                seen.add((None, d["child"]))  # Resolve the existing unplaced candidate with its explicit new parent.
            if d["child"] not in defs or d.get("parent") is not None and d["parent"] not in defs:
                problems.append(f"unknown structure endpoint {pair}")
            if d.get("target") is not None and d["target"] not in defs:
                problems.append(f"unknown target {d['target']}")
            if d.get("identity_evidence"):
                for ev in d["identity_evidence"].values():
                    if ev not in d["evidence"]:
                        d["evidence"].append(ev)
                if len(d["evidence"]) > 20:
                    problems.append(f"{pair}: more than 20 evidence references; do not truncate provenance")
            if d.get("visual_identity_evidence"):
                for mapping in d["visual_identity_evidence"].get("dimension_correspondence", []):
                    for key in ("child_evidence", "target_evidence"):
                        if mapping[key] not in d["evidence"]:
                            d["evidence"].append(mapping[key])
                for key in ("child_declaration", "target_declaration", "same_component"):
                    ev = d["visual_identity_evidence"][key]
                    if ev not in d["evidence"]:
                        d["evidence"].append(ev)
                if d["action"] != "duplicate_visual" or len(d["evidence"]) > 20:
                    problems.append(f"{pair}: visual identity proof belongs only to duplicate_visual and cannot truncate provenance")
            from . import spec_assembly_attachment
            for ev in spec_assembly_attachment.proof_refs(d):
                if ev not in d["evidence"]:
                    d["evidence"].append(ev)
            if len(d["evidence"]) > 20:
                problems.append(f"{pair}: attachment/scope proof exceeds 20 evidence refs; do not truncate provenance")
            if (d.get("assembly_attachment_evidence") and d["action"] != "attach_assembly"
                    or d.get("distinct_installation_evidence") and d["action"] != "keep"):
                problems.append(f"{pair}: assembly attachment and distinct-installation evidence belong only to their explicit actions")
            if d["action"] != "unresolved" and not d["evidence"]:
                problems.append(f"{pair}: an adopted or kept structure interpretation needs source evidence")
            for ev in d["evidence"]:
                if ev["chunk"] not in review.chunks:
                    problems.append(f"{pair}: chunk {ev['chunk']} was not included in this review")
                    continue
                if ev.get("region") is not None and ev["chunk"] not in review.image_chunks:
                    problems.append(f"{pair}: the image for chunk {ev['chunk']} was not attached to this review")
                    continue
                stored, problem = validator.evidence(ev)
                if problem is None and ev.get("quote") and ev.get("entity"):
                    _, problem = validator.evidence({**ev, "quote": None})
                if problem:
                    problems.append(f"{pair}: {problem}")
                else:
                    located.append({"parent": d.get("parent"), "child": d["child"], "action": d["action"], "evidence": stored})
            if d.get("identity_evidence") or d["action"] == "exclude_catalog":
                problem = _structure_identity_problem(run, review, content, d)
                if problem:
                    problems.append(f"{pair}: {problem}")
                elif d.get("target"):
                    validated = validated_catalog_exclusions if d["action"] == "exclude_catalog" else validated_identity
                    validated.add((d["child"], d["target"]))
            if d["action"] == "replace_member":
                problem = _structure_member_problem(run, review, content, d)
                if problem:
                    problems.append(f"{pair}: {problem}")
                else:
                    validated_member_replacements.add((d["parent"], d["child"], d["target"]))
            if d["action"] == "duplicate_visual":
                from . import spec_visual_identity
                problem = spec_visual_identity.proof_problem(d, review.context.get("visual_declarations", []), validator.evidence)
                if problem:
                    problems.append(f"{pair}: {problem}")
                else:
                    validated_visual_duplicates.add((d["parent"], d["child"], d["target"]))
            if d["action"] == "attach_assembly":
                problem = spec_assembly_attachment.proof_problem(content, d, review.context)
                if problem:
                    problems.append(f"{pair}: {problem}")
                else:
                    validated_assembly_attachments.add((d["parent"], d["child"]))
        missing = required - seen
        if missing:
            problems.append("review every candidate explicitly, including keep/unresolved: " + str(sorted(missing, key=str)))
        if not problems:
            try:
                from . import spec_assembly_attachment
                if any(d["action"] == "attach_assembly" for d in reply["decisions"]):
                    validated_scope_bindings.extend(spec_assembly_attachment.bind_proposed_scope_rows(content, reply["decisions"], review.context,
                        [{"source": c.source_id, "chunk": c.seq, "text": run.text_of(c)}
                         for c in run.chunks.values() if c.seq in review.chunks]))
                preview, audit = spec_structure_review.apply_review(content, reply["decisions"], policy="turbo", sentences=sentences,
                    validated_identity=validated_identity, validated_catalog_exclusions=validated_catalog_exclusions,
                    validated_member_replacements=validated_member_replacements, validated_visual_duplicates=validated_visual_duplicates,
                    validated_assembly_attachments=validated_assembly_attachments,
                    attachment_context={**review.context, "proposed_scope_bindings": validated_scope_bindings})
                problems.extend(str(r["reason"]) for r in audit.get("rejected", []))
                # A graph-valid patch must also preserve the final reference
                # contract: no facts/relations/assumptions pointing at removed keys.
                _, structural, _ = validator.inspect(preview)
                problems.extend(structural)
            except ValueError as exc:
                problems.append(str(exc))
        return reply, problems

    # Persist the full compact input and image provenance even if the call is
    # refused for budget or the reply later fails validation.
    run.v.planning = {**run.v.planning, "structure_review": {"input": review.context, **review.metadata}}
    start = len(run.call_ids)
    reply = _structured_with_repairs(run, purpose="spec_structure_review", system=agent.STRUCTURE_REVIEW_SYSTEM_PROMPT,
                                     first=review.message, schema_name=agent.SCHEMA_STRUCTURE_REVIEW,
                                     schema=agent.structure_review_json_schema(), check=check, images=review.images,
                                     budgeted=True, unit="spec_structure_review") or {"decisions": []}
    run.v.planning = {**run.v.planning, "structure_review": {**run.v.planning["structure_review"], "reply": reply,
                        "located_evidence": located, "validated_identity": [list(p) for p in sorted(validated_identity)],
                        "validated_catalog_exclusions": [list(p) for p in sorted(validated_catalog_exclusions)],
                        "validated_member_replacements": [list(p) for p in sorted(validated_member_replacements)],
                        "validated_visual_duplicates": [list(p) for p in sorted(validated_visual_duplicates)],
                        "validated_assembly_attachments": [list(p) for p in sorted(validated_assembly_attachments)],
                        "validated_scope_bindings": copy.deepcopy(validated_scope_bindings),
                        "call_ids": run.call_ids[start:]}}
    return reply["decisions"]


def _guard_source_frames(run: _Run, merged: spec_merge.Merged) -> None:
    """Refuse a proven source reflection before treating its rotation as a missing scalar.

    This does not normalize geometry or provide a new placement authority. It is
    a structural source-capability refusal in both policies, outside the Turbo gate.
    """
    from .spec_frame_guard import reflected_source_mapping

    definitions = merged.content.get("definitions") or []
    by_key = {d["key"]: d for d in definitions}
    validator = _Validator(run, run.v.execution_policy, None)
    originals: dict[str, str] = {}
    problems: list[dict[str, Any]] = []
    for item in merged.missing_evidence:
        owner, _, field = item["subject"].partition(".")
        if owner not in by_key or field not in ("rot", "rotate", "rotation"):
            continue
        evidence, issue = validator.evidence(item["evidence"])
        if issue or not evidence.get("verified") or not evidence.get("quote"):
            continue
        chunk = run.chunks.get(evidence["chunk"])
        source = run.sources.get(evidence["source"])
        if (chunk is None or source is None or chunk.kind != "text" or chunk.row.status != "sent"
                or chunk.start is None or chunk.end is None):
            continue
        if source.id not in originals:
            try:
                raw, _ = read_blob(run.db, source.sha256)
                original = sr._decode_utf8(raw) if source.file_version_id else raw.decode("utf-8")
            except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
                raise _Failed("validate", "source_reference_missing", "the original coordinate source is unavailable",
                              detail={"source": source.id, "chunk": chunk.seq}) from exc
            if original is None or hashlib.sha256(raw).hexdigest() != source.sha256:
                raise _Failed("validate", "source_reference_mismatch", "the coordinate source differs from its pinned bytes",
                              detail={"source": source.id, "chunk": chunk.seq})
            originals[source.id] = original
        original = originals[source.id]
        chunk_text = run.text_of(chunk)
        if (not 0 <= chunk.start < chunk.end <= len(original) or original[chunk.start:chunk.end] != chunk_text
                or sr.sha256_text(chunk_text) != chunk.text_sha256):
            raise _Failed("validate", "source_reference_mismatch", "the coordinate evidence chunk differs from its original source",
                          detail={"source": source.id, "chunk": chunk.seq})
        span = sr.locate_quote(chunk_text, evidence["quote"])
        if span is None:
            continue
        absolute = (chunk.start + span[0], chunk.start + span[1])
        first = sr.locate_quote(original, evidence["quote"])
        if first != absolute or sr.locate_quote(original[absolute[1]:], evidence["quote"]) is not None:
            continue  # repeated statements cannot establish a unique source binding
        problem = reflected_source_mapping(original, absolute, owner, definitions)
        if (problem is not None and chunk.start <= problem["declaration_start"]
                < problem["declaration_end"] <= chunk.end):
            problems.append({"subject": item["subject"], "definition": owner, "source": source.id, "chunk": chunk.seq,
                             "source_sha256": source.sha256, "text_sha256": chunk.text_sha256, "quote": evidence["quote"],
                             **problem})
    if problems:
        raise _Failed("validate", "source_frame_unresolved",
                      "the source explicitly maps local coordinates with opposite handedness; this requires a verified "
                      "model-frame conversion, not an assumed zero rotation. No geometry or placement was normalized",
                      detail={"mappings": problems})


def _merge(run: _Run, findings: list[spec_merge.ChunkFindings], *, base: EngineeringSpecVersion | None,
           skeleton: spec_merge.Skeleton | None = None) -> dict[str, Any]:
    """Merge, review original assembly structure, resolve scalars, then apply the audited structure plan."""
    run.stage = "validate"
    policy = run.v.execution_policy
    previous = base.content if base is not None and base.content else None
    title_bindings = _drawing_title_bindings(run)
    membership_bindings = _drawing_membership_bindings(run, skeleton, title_bindings)
    m = spec_merge.merge(findings, policy=policy, previous=previous, skeleton=skeleton,
                         title_bindings=title_bindings, membership_bindings=membership_bindings)
    _guard_source_frames(run, m)
    structure_decisions = _review_structure(run, m.content, m.record)
    structural_subjects = {f"{d['parent']}.qty[{d['child']}]" for d in structure_decisions
                           if d["action"] in ("quantity", "rolled_up", "attach_assembly") and d.get("parent")}
    calls = 0
    if pol.is_turbo(policy) and m.adoptable:
        # A re-extraction reuses the decisions of the version it re-reads that still answer an open item (generation
        # supplement D-03); a value a person corrected there is decided again, never silently re-adopted.
        corrected: set[str] = set()
        if base is not None and previous:
            from .verification import spec_decisions

            corrected = {aid for aid, d in spec_decisions(run.db, base).items() if d.get("decision") == "corrected"}
        scalar_items = [it for it in m.adoptable if it["id"] not in structural_subjects]
        adopt = spec_merge.reusable_adoptions(previous, scalar_items, skip=corrected)
        rest = [it for it in scalar_items if it["id"] not in adopt]
        if rest:
            adopt.update({a["id"]: a for a in _resolve(run, open_items=rest).get("adopt") or []})
            calls += 1
        m = spec_merge.merge(findings, policy=policy, previous=previous, adopt=adopt, skeleton=skeleton,
                             title_bindings=title_bindings, membership_bindings=membership_bindings)
    run.stage = "validate"
    run.v.planning = {**run.v.planning, "merge": {**m.record, "resolve_calls": calls}}
    if "structure_review" in run.v.planning:
        # Source sentences only: a model's basis must not masquerade as an
        # explicit alias declaration for the deterministic identity guard.
        sentences = [{"text": line, "source": c.source_id, "locator": f"chunk {c.seq}"}
                     for c in run.chunks.values() if c.row.status == "sent"
                     for line in run.text_of(c).splitlines()]
        try:
            reviewed, applied = spec_structure_review.apply_review(m.content, structure_decisions, policy=policy,
                                                                   at=_now().isoformat(), sentences=sentences,
                validated_identity={tuple(p) for p in run.v.planning["structure_review"].get("validated_identity", [])},
                validated_catalog_exclusions={tuple(p) for p in run.v.planning["structure_review"].get("validated_catalog_exclusions", [])},
                validated_member_replacements={tuple(p) for p in run.v.planning["structure_review"].get("validated_member_replacements", [])},
                validated_visual_duplicates={tuple(p) for p in run.v.planning["structure_review"].get("validated_visual_duplicates", [])},
                validated_assembly_attachments={tuple(p) for p in run.v.planning["structure_review"].get("validated_assembly_attachments", [])},
                attachment_context={**run.v.planning["structure_review"].get("input", {}),
                    "proposed_scope_bindings": run.v.planning["structure_review"].get("validated_scope_bindings", [])})
        except ValueError as exc:
            raise _Failed("validate", "structure_review_invalid", str(exc), retryable=True) from exc
        run.v.planning = {**run.v.planning, "structure_review": {**run.v.planning["structure_review"], "applied_record": applied}}
        return reviewed
    return m.content


def _resolve(run: _Run, *, open_items: list[dict[str, Any]]) -> dict[str, Any]:
    """One small ``spec_resolve`` call (Turbo): only the open items and the facts they involve."""
    msg = agent.resolve_message(policy=run.v.execution_policy, open_items=open_items)
    _check_call_budget(run, agent.RESOLVE_SYSTEM_PROMPT, msg, f"resolving {len(open_items)} open item(s)",
                       schema=agent.resolve_json_schema())

    def check(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            reply = agent.ResolveReply.model_validate(data)
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:20]
        # An adoption for something that is not an open item is no repair problem: the merge drops it and notes a stated
        # value it would have replaced (generation supplement D-03, spec_merge "Turbo decisions").
        return reply.model_dump(mode="json"), []

    return _structured_with_repairs(run, purpose="spec_resolve", system=agent.RESOLVE_SYSTEM_PROMPT, first=msg,
                                    schema_name=agent.SCHEMA_RESOLVE, schema=agent.resolve_json_schema(), check=check, budgeted=True) or {}


def _apply_answers(run: _Run, parent: EngineeringSpecVersion) -> dict[str, Any]:
    """Answers as patches (S7): the model returns only the facts the answers set; the server applies them to the current
    spec (answered facts asserted, citing the answer chunk; an open conflict on the same subject resolved by the answer)."""
    pending = [c for c in sorted(run.chunks.values(), key=lambda c: c.seq) if c.row.inherited_from_version_id is None]
    by_source = {a["source"]: a for a in run.v.answers}
    content = parent.content or {}
    questions = {**decision_questions(content, parent.execution_policy), **{q["id"]: q for q in content.get("questions") or [] if q.get("id")}}
    facts = {f["subject"]: f for f in content.get("facts") or []}
    conflicts = {c["subject"]: c for c in content.get("conflicts") or [] if not c.get("resolution")}
    answers, answered = [], []
    chunk_of: dict[str, _ChunkRef] = {}
    for c in pending:
        a = by_source.get(c.source_id)
        if a is None:
            continue
        qid = a["question_id"]
        q = questions.get(qid) or {}
        subject = str(q.get("subject") or "")
        base = subject[:-5] if subject.endswith(".unit") else subject
        cur = facts.get(base)
        item: dict[str, Any] = {"question": qid, "subject": subject, "text": q.get("question"), "affects": q.get("affects") or [],
                                "candidates": q.get("candidates") or [],
                                "current": {k: cur.get(k) for k in ("value", "unit", "status")} if cur else None}
        if base in conflicts:
            item["readings"] = [{k: x.get(k) for k in ("value", "unit", "source")} for x in conflicts[base].get("values") or []]
        answered.append(item)
        answers.append((qid, c.seq, c.source_id, c.locator, run.text_of(c)))
        chunk_of[qid] = c
    msg = agent.answer_message(policy=run.v.execution_policy, answered=answered, answers=answers)
    _check_call_budget(run, agent.ANSWER_SYSTEM_PROMPT, msg, "applying the answers to the current spec",
                       schema=agent.answer_json_schema())
    objects = _objects(content)

    def check(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            reply = agent.AnswerReply.model_validate(data)
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:20]
        problems = []
        for f in reply.facts:
            own = f.s.split(".", 1)[0]
            q = questions.get(f.a) or {}
            subject = str(q.get("subject") or "")
            value = spec_merge.value_for(f.s, f.v)
            if f.a not in chunk_of:
                problems.append(f"fact {f.s}: {f.a} is not a question answered here (answered: {', '.join(sorted(chunk_of))})")
            elif "." not in f.s or own not in objects:
                problems.append(f"fact {f.s}: the subject must be '<definition|instance|relation key>.<field>' of the current spec")
            elif f.s not in (subject, subject[:-5] if subject.endswith(".unit") else subject) and own not in (q.get("affects") or []):
                problems.append(f"fact {f.s}: {f.a} is about {subject}; set only that subject or a field of a key it affects")
            elif value is None:
                problems.append(f"fact {f.s}: the value is missing")
            elif _is_number(value) and not (f.u or (facts.get(f.s) or {}).get("unit")):
                problems.append(f"fact {f.s}: the number {f.v} needs a unit (u)")
        return reply.model_dump(mode="json"), problems

    patch = _structured_with_repairs(run, purpose="spec_answer", system=agent.ANSWER_SYSTEM_PROMPT, first=msg, schema_name=agent.SCHEMA_ANSWER,
                                     schema=agent.answer_json_schema(), check=check, answer_chunks=pending, budgeted=True) or {}
    spec, run.removed = _patched(content, patch.get("facts") or [], chunk_of)
    return spec


def _patched(content: dict[str, Any], patches: list[dict[str, Any]], chunk_of: dict[str, _ChunkRef]) -> tuple[dict[str, Any], set[str]]:
    """The current spec (reply form) with the answered facts applied; every other key and value stays as it is."""
    spec = reply_form(content)
    facts = {f["subject"]: f for f in spec["facts"]}
    defs = {d["key"]: d for d in spec["definitions"]}
    removed: set[str] = set()
    top = max([int(f["id"][2:]) for f in spec["facts"] if re.match(r"^F-\d+$", str(f.get("id")))] or [0])
    for p in patches:
        qid, subject = p["a"], p["s"]
        c = chunk_of[qid]
        value = spec_merge.value_for(subject, p["v"])
        unit = spec_merge.norm_unit(p.get("u"))
        ev = {"source": c.source_id, "chunk": c.seq, "quote": str(p.get("q") or value)[:400], "entity": None, "region": None, "detail": ""}
        f = facts.get(subject)
        if f is None:
            top += 1
            f = {"id": f"F-{top:03d}", "subject": subject,
                 "kind": "quantity" if ".qty[" in subject else ("material" if subject.endswith(".material") else "dimension"),
                 "value": value, "unit": unit, "status": "asserted", "evidence": [ev], "derivation": f"answer to {qid}"}
            spec["facts"].append(f)
            facts[subject] = f
            own = subject.split(".", 1)[0]
            if own in defs:
                defs[own].setdefault("fact_ids", []).append(f["id"])
        else:
            f.update(value=value, unit=unit or f.get("unit"), status="asserted", evidence=list(f.get("evidence") or [])[:19] + [ev],
                     derivation=f"answer to {qid}")
        for conflict in spec["conflicts"]:
            # a person's answer settles the conflict, also one an AI decision had settled (a correction, D-03)
            if conflict["subject"] == subject and (not conflict.get("resolution") or "assumption" in conflict["resolution"]):
                conflict["resolution"] = {"answer": qid, "value": value}
        own = subject.split(".", 1)[0]
        if subject.endswith(".material") and own in defs and value is not None:
            defs[own]["material"] = str(value)[:120]
        qty = re.fullmatch(r"(?P<p>[^.]+)\.qty\[(?P<c>[^\]]+)\]", subject)
        if qty and _is_number(value) and int(value) >= 1 and qty["p"] in defs and qty["c"] in defs:
            removed |= _set_count(spec, qty["p"], qty["c"], int(value))
    return spec, removed


def _set_count(spec: dict[str, Any], parent: str, child: str, n: int) -> set[str]:
    """Quantities come only from instances (W0 §5): an answered count adds instances (new keys, no placement) or removes
    the last ones and the relations that touch them. Returns the removed instance keys."""
    mine = [i for i in spec["instances"] if i.get("parent") == parent and i.get("definition") == child]
    if len(mine) == n:
        return set()
    if len(mine) < n:
        top = max([int(i["key"][2:]) for i in spec["instances"] if re.match(r"^I-\d+$", str(i.get("key")))] or [0])
        name = next((d.get("name") or child for d in spec["definitions"] if d["key"] == child), child)
        for j in range(len(mine), n):
            top += 1
            spec["instances"].append({"key": f"I-{top:02d}", "definition": child, "parent": parent, "name": f"{name} #{j + 1}",
                                      "placement": None, "fact_ids": []})
        return set()
    gone = {i["key"] for i in mine[n:]}
    spec["instances"] = [i for i in spec["instances"] if i["key"] not in gone]
    spec["relations"] = [r for r in spec["relations"] if r["a"]["instance"] not in gone and r["b"]["instance"] not in gone]
    for q in spec["questions"]:
        q["affects"] = [k for k in q.get("affects") or [] if k not in gone] or [child]
    for a in spec["assumptions"]:
        a["affected"] = [k for k in a.get("affected") or [] if k not in gone]
    return gone


def _structured_with_repairs(run: _Run, *, purpose: str, system: str, first: str, schema_name: str, schema: dict[str, Any],
                             check: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, list[str]]],
                             answer_chunks: list[_ChunkRef] | None = None, unit: str | None = None,
                             soft: Callable[[dict[str, Any]], list[str]] | None = None,
                             images: list[ImagePart] | None = None, budgeted: bool = False) -> dict[str, Any] | None:
    """A skeleton / resolve / answer call with up to MAX_REPAIRS repairs; ``check`` returns (canonical reply, problems).
    ``soft``: problems of the canonical reply the model is asked to repair as well, but which never fail the call — when
    the repairs are used up the reply is returned and the caller drops what they concern (skeleton interfaces)."""
    messages = [Message(role="user", content=first)]
    repairs = 0
    while True:
        if budgeted:
            _check_call_budget(run, system, "\n".join(m.content for m in messages), purpose, schema=schema, images=images)
        try:
            res = _call(run, purpose=purpose, system=system, messages=messages, schema_name=schema_name, json_schema=schema,
                        unit=unit, images=images)
        except ProviderError:
            for c in answer_chunks or []:
                c.row.status, c.row.reason = "failed", "provider_error"
            raise
        ids = run.record()
        for c in answer_chunks or []:
            if c.row.status != "sent":
                c.row.status, c.row.sent_at = "sent", _now()
                c.row.ai_call_id = uuid.UUID(ids[-1]) if ids else None
        run.v.planning = {**run.v.planning, "ai_call_ids": list(run.call_ids)}
        run.stage = "parse"
        if res.data is None:
            out, problems, kind = None, [f"the reply was not a JSON object: {res.parse_error or 'empty'}"], "parse"
        else:
            run.stage = "validate"
            out, problems = check(res.data)
            kind = "validate"
        repairable = soft(out) if soft is not None and out is not None else []
        if not problems and (not repairable or repairs >= MAX_REPAIRS):
            return out
        if problems and repairs >= MAX_REPAIRS:
            raise _Failed("parse" if kind == "parse" else "validate", "bad_response" if kind == "parse" else "spec_invalid",
                          f"the model reply did not pass the server checks after {MAX_REPAIRS} repairs", detail={"problems": problems[:40]},
                          retryable=True)
        repairs += 1
        run.v.planning = {**run.v.planning, "repairs": int(run.v.planning.get("repairs", 0)) + 1}
        messages = messages + [Message(role="assistant", content=res.text if budgeted else clip(res.text, 30_000)),
                               Message(role="user", content=agent.repair_message(problems + repairable))]


# --- validation -------------------------------------------------------------------------------------------


def reply_form(content: dict[str, Any]) -> dict[str, Any]:
    """Stored content → the reply schema (server-owned fields removed: sources, locators, verified, answers, author / at;
    the identity, baseline and target history of generation reliability K are computed again when the version finishes)."""
    out = {k: v for k, v in (content or {}).items() if k not in ("sources", "identity", "baseline", "target_history")}
    out.setdefault("summary", "")

    def ev(e: dict[str, Any]) -> dict[str, Any]:
        return {k: e.get(k) for k in ("source", "chunk", "quote", "entity", "region")} | {"detail": e.get("detail") or ""}

    out["facts"] = [{**f, "evidence": [ev(e) for e in f.get("evidence") or [] if e.get("chunk")]} for f in content.get("facts") or []]
    out["manufacturing_notes"] = [{**n, "evidence": [ev(e) for e in n.get("evidence") or [] if e.get("chunk")]}
                                  for n in content.get("manufacturing_notes") or []]
    out["questions"] = [{k: v for k, v in q.items() if k != "answer"} for q in content.get("questions") or []]
    out["conflicts"] = [{**c, "values": [{k: v for k, v in x.items() if k in
                         ("value", "unit", "source", "chunk", "quote", "entity", "region", "detail", "status", "derivation")}
                        for x in c.get("values") or []]}
                        for c in content.get("conflicts") or []]
    out["assumptions"] = [{k: v for k, v in a.items() if k not in ("author", "at")} for a in content.get("assumptions") or []]
    for k in ("definitions", "instances", "relations", "deliverables"):
        out[k] = list(content.get(k) or [])
    return out


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def structure_problems(data: dict[str, Any]) -> list[str]:
    """Shared typed graph checks for design additions, without accepting any AI-authored source evidence.

    Extraction still uses the full validator and evidence finalizer. The design service preserves the
    already-validated original facts itself and rejects evidence on all newly selected values.
    """
    sources = {str(s["id"]): None for s in data.get("sources") or [] if isinstance(s, dict) and s.get("id")}
    _, structural, _ = _Validator(SimpleNamespace(sources=sources, chunks={}), "standard", None).inspect(reply_form(data))
    return structural


class _Validator:
    """Server checks of a consolidated / answered spec (see the module docstring)."""

    def __init__(self, run: _Run, policy: str, parent: EngineeringSpecVersion | None) -> None:
        self.run = run
        self.policy = policy
        self.parent = parent
        self._context_sources: dict[str, str | None] = {}

    def definition_context(self, evidence: dict[str, Any], definition: dict[str, Any],
                           definitions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Preserve a complete, bounded source block owned by the definition of a uniquely located quote.

        Nothing is inferred from a neighboring component or an ambiguous quote. The original evidence is untouched;
        this additional context is recomputed for new versions and is never supplied by a model reply.
        """
        from ..cad.requirements import DefinitionSourceContext, definition_source_header, normalize_text

        if not evidence.get("verified") or not evidence.get("quote") or definition.get("kind") not in ("part", "standard_part"):
            return None
        name = str(definition.get("name") or "")
        if not name or sum(d.get("name") == name for d in definitions) != 1:
            return None
        source_id = evidence["source"]
        source, chunk = self.run.sources.get(source_id), self.run.chunks.get(evidence["chunk"])
        if (source is None or chunk is None or chunk.kind != "text" or chunk.row.status != "sent"
                or chunk.start is None or chunk.end is None or not source.sha256):
            return None
        if source_id not in self._context_sources:
            try:
                raw, _ = read_blob(self.run.db, source.sha256)
                body = raw.decode("utf-8")
                self._context_sources[source_id] = body if hashlib.sha256(raw).hexdigest() == source.sha256 else None
            except (FileNotFoundError, OSError, UnicodeDecodeError):
                self._context_sources[source_id] = None
        body = self._context_sources[source_id]
        if body is None or body[chunk.start:chunk.end] != self.run.text_of(chunk):
            return None
        quote = str(evidence["quote"])
        normalized = sr.normalise_quote(quote)
        if not normalized or sr.normalise_quote(body).count(normalized) != 1:
            return None
        span = sr.locate_quote(body, quote)
        if span is None:
            return None
        names = [str(d.get("name")) for d in definitions if d.get("name")]
        headers: list[tuple[int, str | None]] = []
        for line in re.finditer(r"(?m)^[^\n]+", body):
            value = normalize_text(line.group())
            candidates = [n for n in names if definition_source_header(n).match(value)]
            owner = candidates[0] if len(candidates) == 1 else None
            # Normalization and optional quantity suffixes can make different names match the same heading.
            # Ambiguity still ends the previous block, but never assigns this block to an arbitrary first owner.
            if candidates or re.match(r"^[ \t]*\d+[.)、][ \t]*[^:：\n]{1,80}[:：]", value):
                headers.append((line.start(), owner))
        own_headers = [start for start, owner in headers if owner == name]
        if len(own_headers) != 1:
            return None
        start = own_headers[0]
        end = next((offset for offset, _ in headers if offset > start), len(body))
        if not (chunk.start <= start <= span[0] < span[1] <= end <= chunk.end) or end - start > 400:
            return None
        return DefinitionSourceContext(definition=definition["key"], name=name, source=source_id, chunk=chunk.seq,
                                       source_sha256=source.sha256, start=start, end=end, text=body[start:end],
                                       anchor_quote=quote).model_dump(mode="json")

    # -- evidence -----------------------------------------------------------------------------------------
    def evidence(self, e: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """(stored evidence, problem). The locator is the server's; ``verified`` = quote / entity found in a sent chunk."""
        src_id, seq = str(e.get("source") or ""), e.get("chunk")
        out = {"source": src_id, "chunk": seq, "locator": "", "quote": e.get("quote"), "entity": e.get("entity"), "region": e.get("region"),
               "detail": e.get("detail") or "", "verified": False}
        c = self.run.chunks.get(seq) if isinstance(seq, int) else None
        if src_id not in self.run.sources:
            return out, f"evidence cites unknown source {src_id}"
        if c is None or c.source_id != src_id:
            return out, f"evidence cites chunk {seq}, which is not a chunk of {src_id}"
        if c.row.status != "sent":
            return out, f"evidence cites chunk {seq} of {src_id}, which was not sent to the model ({c.row.status})"
        src = self.run.sources[src_id]
        loc = f"chunk {seq}"
        if c.kind == "drawing" and e.get("region") is not None:
            problem = _drawing_region_problem(c, e["region"])
            if problem:
                out["locator"] = loc
                return out, problem
            if e.get("quote") or e.get("entity"):
                return out, "an image region must be a separate reading from literal quote/entity evidence"
            meta = c.meta
            out.update(located=True, frame_id=meta["frame"], image_sha256=meta["image_sha256"],
                       image_width=meta["image_width"], image_height=meta["image_height"],
                       file_version_id=src.file_version_id, source_sha256=src.sha256)
            loc += f" · {meta['frame']} · image region " + ",".join(str(x) for x in e["region"]) + " px (visual, unverified)"
        elif c.kind == "attachment":
            # An image / PDF has no verifiable text (its stored sha is the raw file's): only a region locates
            # evidence there, never a quote or an entity, and it is never ``verified``.
            r = list(e.get("region") or [])
            if not r:
                out["locator"] = loc
                return out, (f"evidence in attachment chunk {seq} of {src_id} needs a region [x, y, w, h] px or [page]; "
                             "attachments have no verifiable text")
            loc += f" · {'page ' + str(int(r[0])) if len(r) == 1 else 'region ' + ','.join(str(round(x)) for x in r) + ' px'} · no scale"
            out["located"] = True
        elif e.get("quote"):
            span = sr.locate_quote(self.run.text_of(c), str(e["quote"]))
            if span is None and c.kind == "text":
                # S4: a statement that crosses a chunk boundary is whole in the next chunk (its overlap); cite the
                # adjacent sent chunk of the same source where the quote really is.
                for n in (self.run.chunks.get(seq + 1), self.run.chunks.get(seq - 1)):
                    if n is None or n.source_id != src_id or n.kind != "text" or n.row.status != "sent":
                        continue
                    span = sr.locate_quote(self.run.text_of(n), str(e["quote"]))
                    if span is not None:
                        out["cited_chunk"], out["chunk"], c, loc = seq, n.seq, n, f"chunk {n.seq}"
                        break
            if span is None:
                out["locator"] = loc
                return out, f"quote {clip(str(e['quote']), 60)!r} is not in chunk {seq} of {src_id}"
            if c.kind == "text" and c.start is not None:
                a, b = c.start + span[0], c.start + span[1]
                loc += f" · ¶{sr.paragraph_of(src, a) if src.para_starts else '?'} · chars {a}–{b}"
            else:
                loc += f" · {c.locator.split(' · ')[0]}"
            out["verified"] = True
        elif e.get("entity"):
            ent = str(e["entity"])
            ok = re.search(rf"(?<![0-9A-Za-z/]){re.escape(ent)}(?![0-9A-Za-z/])", self.run.text_of(c)) is not None
            loc += f" · {c.locator.split(' · ')[0]} · entity {ent}"
            if not ok:
                out["locator"] = loc
                return out, f"entity {ent} is not in chunk {seq} of {src_id}"
            out["verified"] = True
        else:
            out["locator"] = loc
            return out, f"evidence in chunk {seq} of {src_id} has no quote, entity or region"
        if e.get("detail"):
            loc += f" · {clip(str(e['detail']), 80)}"
        out["locator"] = loc
        return out, None

    # -- inspection ---------------------------------------------------------------------------------------
    def inspect(self, data: dict[str, Any]) -> tuple[agent.SpecReply | None, list[str], list[str]]:
        """(reply, structural problems, evidence problems)."""
        try:
            reply = agent.SpecReply.model_validate(data)
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:30], []
        s: list[str] = []
        ev: list[str] = []
        d = reply.model_dump(mode="json")
        keys: dict[str, set[str]] = {}
        for coll, rx in KEY_PATTERNS.items():
            seen: set[str] = set()
            for item in d[coll]:
                k = item[_KEY_FIELD[coll]]
                if not re.match(rx, k):
                    s.append(f"{coll}: key {k!r} must match {rx}")
                if k in seen:
                    s.append(f"{coll}: key {k} is used twice")
                seen.add(k)
            keys[coll] = seen
        defs = {x["key"]: x for x in d["definitions"]}
        insts = {x["key"]: x for x in d["instances"]}
        objects = set(defs) | set(insts) | keys["relations"]
        facts = keys["facts"]
        if d["root"] is not None:
            if d["root"] not in defs:
                s.append(f"root {d['root']} is not a definition")
            elif defs[d["root"]]["kind"] != "assembly":
                s.append(f"root {d['root']} must be an assembly definition")
        for x in d["definitions"]:
            for fid in x["fact_ids"]:
                if fid not in facts:
                    s.append(f"definition {x['key']}: unknown fact {fid}")
        children: dict[str, set[str]] = {}
        for x in d["instances"]:
            if x["definition"] not in defs:
                s.append(f"instance {x['key']}: unknown definition {x['definition']}")
            if x["parent"] is None or x["parent"] not in defs:
                s.append(f"instance {x['key']}: parent {x['parent']} is not a definition")
            elif defs[x["parent"]]["kind"] != "assembly":
                s.append(f"instance {x['key']}: parent {x['parent']} is not an assembly")
            else:
                children.setdefault(x["parent"], set()).add(x["definition"])
            for fid in x["fact_ids"]:
                if fid not in facts:
                    s.append(f"instance {x['key']}: unknown fact {fid}")
        if _has_cycle(children):
            s.append("instances: the assembly structure contains a cycle (a definition contains itself)")
        ifaces: dict[str, dict[str, Any]] = {}
        for x in d["interfaces"]:  # generation reliability A (design §6.2)
            if x["key"] in ifaces:
                s.append(f"interfaces: key {x['key']} is used twice")
            ifaces[x["key"]] = x
            if x["owner"] not in defs or defs[x["owner"]]["kind"] != "assembly":
                s.append(f"interface {x['key']}: owner {x['owner']} is not an assembly definition")
            if bad := agent.interface_problems(x["kind"], x["at"], x["dir"], x["points"]):
                s.append(f"interface {x['key']} ({x['kind']}): needs " + "; ".join(bad))
        for r in d["relations"]:
            for end in ("a", "b"):
                inst, datum = r[end]["instance"], r[end]["datum"]
                if inst.startswith("@"):  # an assembly's own frame
                    if inst[1:] not in defs or defs[inst[1:]]["kind"] != "assembly":
                        s.append(f"relation {r['key']}: {end}.instance {inst} is not an assembly's frame")
                elif inst not in insts:
                    s.append(f"relation {r['key']}: {end}.instance {inst} is not an instance")
                if datum.startswith("if.") and datum[3:] not in ifaces:
                    s.append(f"relation {r['key']}: {end}.datum {datum} is not an interface")
            if r["a"]["instance"] == r["b"]["instance"]:
                s.append(f"relation {r['key']}: both ends are the same instance")
        assumed_subjects = {a["subject"] for a in d["assumptions"]} | keys["assumptions"]
        conflict_subjects = {c["subject"] for c in d["conflicts"]}
        for f in d["facts"]:
            own = f["subject"].split(".", 1)[0]
            if "." not in f["subject"] or own not in objects:
                s.append(f"fact {f['id']}: subject {f['subject']!r} must be '<definition|instance|relation key>.<field>'")
            if _is_number(f["value"]) and f["status"] in ("asserted", "derived", "assumed") and not f["unit"]:
                s.append(f"fact {f['id']}: the number {f['value']} needs a unit")
            if f["status"] == "conflicting" and f["subject"] not in conflict_subjects:
                s.append(f"fact {f['id']}: status conflicting needs a conflict entry for {f['subject']}")
            if f["status"] == "assumed" and self.policy == "turbo" and not (
                    {f["id"], f["subject"]} & assumed_subjects or (f["derivation"] or "") in keys["assumptions"]):
                s.append(f"fact {f['id']}: status assumed needs an assumption (subject {f['subject']} or derivation A-xx)")
            verified = 0
            for e in f["evidence"]:
                stored, problem = self.evidence(e)
                if problem:
                    ev.append(f"fact {f['id']}: {problem}")
                else:
                    verified += 1 if stored["verified"] else 0
            if f["status"] == "asserted" and verified == 0 and not any(p.startswith(f"fact {f['id']}:") for p in ev):
                ev.append(f"fact {f['id']}: an asserted fact needs a verifiable quote or drawing entity (attachments have no verifiable text)")
            if f["status"] == "derived" and not (f["derivation"] or f["evidence"]):
                ev.append(f"fact {f['id']}: a derived fact needs its derivation or evidence")
        for n in d["manufacturing_notes"]:
            if n["subject"] and n["subject"] not in objects:
                s.append(f"note {n['id']}: subject {n['subject']} is not a definition, instance or relation")
            for fid in n["fact_ids"]:
                if fid not in facts:
                    s.append(f"note {n['id']}: unknown fact {fid}")
        for q in d["questions"]:
            if not q["affects"]:
                s.append(f"question {q['id']}: affects must list the definitions / instances / relations it blocks")
            for k in q["affects"]:
                if k not in objects:
                    s.append(f"question {q['id']}: affects {k}, which is not a definition, instance or relation")
        for c in d["conflicts"]:
            if len(c["values"]) < 2:
                s.append(f"conflict {c['id']}: a conflict keeps at least two readings")
            for x in c["values"]:
                if x["source"] not in self.run.sources:
                    s.append(f"conflict {c['id']}: unknown source {x['source']}")
        if self.policy == "turbo":
            for a in d["assumptions"]:
                if not a["basis"].strip():
                    s.append(f"assumption {a['id']}: basis is required")
                for k in a["affected"]:
                    if k not in objects and k not in facts:
                        s.append(f"assumption {a['id']}: affected {k} is not a known key")
        if self.parent is not None and self.parent.content:
            for coll in ("definitions", "instances"):
                gone = {x["key"] for x in self.parent.content.get(coll) or []} - {x["key"] for x in d[coll]} - set(getattr(self.run, "removed", ()))
                if gone:
                    s.append(f"{coll}: keys {sorted(gone)[:10]} of the current spec disappeared; keep every key")
            # A question id of the current spec keeps its subject: an id is never reused for another question
            # (an answered id would show the new question as answered). Dropped ids are restored in finalize.
            parent_q = self.parent_questions()
            for q in d["questions"]:
                pq = parent_q.get(q["id"])
                if pq is not None and q["subject"] != pq.get("subject"):
                    s.append(f"question {q['id']}: this id is the current spec's question about {pq.get('subject')}; keep its subject "
                             f"and give a new question a new id")
        return reply, s, ev

    def parent_questions(self) -> dict[str, dict[str, Any]]:
        if self.parent is None or not self.parent.content:
            return {}
        return {q["id"]: q for q in self.parent.content.get("questions") or [] if q.get("id")}

    # -- finalisation -------------------------------------------------------------------------------------
    def finalize(self, data: dict[str, Any], answers: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Stored content + validation record. Evidence gets server locators; unlocatable facts are demoted;
        standard converts AI assumptions into questions and adds questions for open conflicts / unknown facts."""
        reply, structural, _ = self.inspect(data)
        assert reply is not None and not structural
        d = reply.model_dump(mode="json")
        demoted: list[dict[str, Any]] = []
        converted: list[dict[str, Any]] = []
        added: list[str] = []
        dropped: list[dict[str, Any]] = []
        now = _now().isoformat()
        parent_q = self.parent_questions()
        # Ids the server allocates never take an id of the current spec (answered or not).
        q_ids = {q["id"] for q in d["questions"]} | set(parent_q) | set(answers)
        a_ids = {a["id"] for a in d["assumptions"]}

        def next_id(prefix: str, used: set[str], width: int = 2) -> str:
            n = 1
            while f"{prefix}-{n:0{width}d}" in used:
                n += 1
            used.add(f"{prefix}-{n:0{width}d}")
            return f"{prefix}-{n:0{width}d}"

        inst_of: dict[str, list[str]] = {}
        for i in d["instances"]:
            inst_of.setdefault(i["definition"], []).append(i["key"])

        def affected(subject: str) -> list[str]:
            own = subject.split(".", 1)[0]
            return [own] + inst_of.get(own, [])

        # Every question of the current spec survives: only a person's answer closes a question, so one the
        # model dropped comes back as it was (open, or with its earlier answer).
        restored: list[str] = []
        present = {q["id"] for q in d["questions"]}
        objects = _objects(d)
        for qid, pq in parent_q.items():
            if qid in present:
                continue
            d["questions"].append({"id": qid, "subject": pq.get("subject") or "", "question": pq.get("question") or "",
                                   "affects": [k for k in pq.get("affects") or [] if k in objects] or affected(pq.get("subject") or ""),
                                   "candidates": list(pq.get("candidates") or []), "origin": pq.get("origin") or "model"})
            restored.append(qid)

        definitions = {item["key"]: item for item in d["definitions"]}
        for f in d["facts"]:
            stored, problems = [], []
            for e in f["evidence"]:
                se, p = self.evidence(e)
                owner = definitions.get(f["subject"].split(".", 1)[0])
                if p is None and owner is not None and f["status"] in ("asserted", "derived"):
                    context = self.definition_context(se, owner, d["definitions"])
                    if context is not None:
                        se["context"] = context
                stored.append(se)
                if p:
                    problems.append(p)
            f["evidence"] = stored
            ok = any(e["verified"] for e in stored)
            needs = (f["status"] == "asserted" and not ok) or (f["status"] == "derived" and not (f["derivation"] or ok))
            if needs:
                demoted.append({"fact": f["id"], "subject": f["subject"], "value": f["value"], "unit": f["unit"], "status": f["status"],
                                "problems": problems[:5] or ["no verifiable evidence"]})
                if self.policy == "turbo":
                    aid = next_id("A", a_ids)
                    d["assumptions"].append({"id": aid, "subject": f["subject"], "field": f["subject"].split(".", 1)[-1], "adopted": f["value"],
                                             "unit": f["unit"], "basis": "the model reported this value, but its evidence could not be "
                                                                         "located in the chunks the server sent",
                                             "alternatives": [], "affected": affected(f["subject"]),
                                             "sources": [spec_merge.reading_locator(e) for e in stored][:20], "trigger": "missing"})
                    f["status"], f["derivation"] = "assumed", aid
                else:
                    f["status"], f["value"] = "unknown", None
        for n in d["manufacturing_notes"]:
            n["evidence"] = [self.evidence(e)[0] for e in n["evidence"]]
        for c in d["conflicts"]:
            for x in c["values"]:
                stored, problem = self.evidence({k: x.get(k) for k in ("source", "chunk", "quote", "entity", "region", "detail")})
                x.update({k: v for k, v in stored.items() if k not in ("source", "chunk", "quote", "entity", "region", "detail")})
                x["verified"] = stored["verified"] and problem is None
        if self.policy != "turbo":
            for a in d["assumptions"]:
                qid = next_id("Q", q_ids)
                adopted = f"{a['adopted']}{a['unit'] or ''}"
                d["questions"].append({"id": qid, "subject": a["subject"], "question": f"AI 建议采用 {adopted}（依据：{a['basis'] or '未说明'}），是否采用？",
                                       "affects": [k for k in a["affected"] if k in _objects(d)] or affected(a["subject"]),
                                       "candidates": [a["adopted"], *a["alternatives"]], "origin": "converted_assumption"})
                converted.append({"assumption": a["id"], "question": qid, "subject": a["subject"]})
                for f in d["facts"]:
                    if f["derivation"] == a["id"] or (f["status"] == "assumed" and f["subject"] == a["subject"]):
                        f["status"], f["value"] = "unknown", None
            d["assumptions"] = []
            for f in d["facts"]:
                if f["status"] == "assumed":
                    f["status"], f["value"] = "unknown", None
            open_subjects = {q["subject"] for q in d["questions"] if q["id"] not in answers}
            for c in d["conflicts"]:
                if c["resolution"] is None and c["subject"] not in open_subjects:
                    qid = next_id("Q", q_ids)
                    readings = "；".join(f"{x['value']}{x.get('unit') or ''}（{x['source']}）" for x in c["values"])
                    d["questions"].append({"id": qid, "subject": c["subject"], "question": f"{c['subject']} 有不同读数：{readings}。采用哪一个？",
                                           "affects": affected(c["subject"]), "candidates": [x["value"] for x in c["values"]], "origin": "server"})
                    open_subjects.add(c["subject"])
                    added.append(qid)
            # An unknown fact is asked about only when building needs it or its evidence was not found: a property the
            # source itself declares unknown (material grade, density, mass, finish, supplier, cost …) gets no question.
            demoted_ids = {x["fact"] for x in demoted}
            for f in d["facts"]:
                if (f["status"] == "unknown" and f["subject"] not in open_subjects and f"{f['subject']}.unit" not in open_subjects
                        and (question_blocks({"subject": f["subject"]}) or f["id"] in demoted_ids)):
                    qid = next_id("Q", q_ids)
                    d["questions"].append({"id": qid, "subject": f["subject"], "question": f"{f['subject']} 的值无法从资料中确认，请给出取值。",
                                           "affects": affected(f["subject"]), "candidates": [x["value"] for x in demoted if x["fact"] == f["id"]][:1],
                                           "origin": "server"})
                    open_subjects.add(f["subject"])
                    added.append(qid)
        else:
            dropped = self._turbo_decisions(d)
            for a in d["assumptions"]:
                a_full = pol.Assumption(**{k: a[k] for k in ("id", "subject", "field", "adopted", "unit", "basis", "alternatives", "affected",
                                                             "sources", "trigger")}, author="ai_turbo", at=now)
                a.clear()
                a.update(a_full.model_dump(mode="json"))
        for q in d["questions"]:
            pq = parent_q.get(q["id"])
            q["origin"] = q.get("origin") or (pq or {}).get("origin") or "model"
            # Answers attach only to the current spec's own questions (same id and subject).
            q["answer"] = answers.get(q["id"]) if pq is not None and pq.get("subject") == q["subject"] else None
            q["blocking"] = question_blocks(q)

        def located(evs: list[Any]) -> list[str]:
            """Skeleton / interface evidence as the locators the server checked (a stored string stays as it is)."""
            out = []
            for e in evs:
                if not isinstance(e, dict):
                    out.append(str(e))
                    continue
                st, _ = self.evidence(e)
                out.append(f"{st['source']} · {st['locator'] or 'chunk ' + str(st.get('chunk'))}"
                           + (f" “{clip(str(st['quote']), 80)}”" if st.get("quote") else "") + ("" if st["verified"] else " (not verified)"))
            return out[:20]

        content = {
            "units": "mm",
            "root": d["root"],
            "sources": [],
            "skeleton": [{**e, "sources": located(list(e.get("sources") or []))} for e in d["skeleton"]],
            "interfaces": [{**x, "sources": located(list(x.get("sources") or []))} for x in d["interfaces"]],
            "definitions": d["definitions"],
            "instances": d["instances"],
            "relations": d["relations"],
            "facts": d["facts"],
            "manufacturing_notes": d["manufacturing_notes"],
            "deliverables": d["deliverables"],
            "questions": d["questions"],
            "conflicts": d["conflicts"],
            "assumptions": d["assumptions"],
            "summary": d["summary"],
        }
        return content, {"demoted": demoted, "converted": converted, "added_questions": added, "restored_questions": restored,
                         "dropped_assumptions": dropped}

    def _turbo_decisions(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Turbo decisions of a finished spec (generation supplement D-02 / D-03), in place on the reply form ``d``:

        - an AI decision whose subject is now a stated fact (asserted / derived: a source states it without a conflict,
          or a person's answer replaced it) is dropped, with a note on its definition — a stated value is never
          overridden by an AI decision;
        - a decision that settled a conflict lists every reading with the server's locator in ``sources``.

        Returns the dropped decisions (``validation.dropped_assumptions``)."""
        facts = {f["subject"]: f for f in d["facts"]}
        conflicts = {c["subject"]: c for c in d["conflicts"]}
        defs = {x["key"]: x for x in d["definitions"]}
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for a in d["assumptions"]:
            f = facts.get(a["subject"])
            if f is not None and f["status"] in ("asserted", "derived") and f.get("value") is not None and f.get("derivation") != a["id"]:
                by_answer = str(f.get("derivation") or "").startswith("answer to ")
                dropped.append({"assumption": a["id"], "subject": a["subject"], "adopted": a.get("adopted"), "unit": a.get("unit"),
                                "stated": f["value"], "stated_unit": f.get("unit"), "reason": "answered" if by_answer else "stated_value",
                                **({"by": f["derivation"]} if by_answer else {})})
                own = defs.get(a["subject"].split(".", 1)[0]) or defs.get(d.get("root") or "")
                if own is not None:
                    stated = f"{f['value']}{(' ' + f['unit']) if f.get('unit') else ''}"
                    text = (f"AI 判断 {a['id']} 已由人的回答替代：{a['subject']} = {stated}（{f['derivation']}）。" if by_answer else
                            f"AI 判断 {a['id']} 未采用：{a['subject']} 资料已写明 {stated} 且没有冲突，保留资料值。")
                    own["notes"] = "\n".join(x for x in (own.get("notes") or "", text) if x)[:2000]
                continue
            c = conflicts.get(a["subject"])
            if c is not None and (c.get("resolution") or {}).get("assumption") == a["id"]:
                srcs = []
                for x in c["values"]:
                    stored, _ = self.evidence({k: x.get(k) for k in ("source", "chunk", "quote", "entity", "region", "detail")})
                    srcs.append(spec_merge.reading_locator({"source": f"{x['source']} · {stored['locator'] or 'chunk ' + str(x.get('chunk'))}",
                                                           "quote": x.get("quote"), "entity": x.get("entity"), "value": x.get("value"),
                                                           "unit": x.get("unit"), "region": x.get("region")}))
                a["sources"] = srcs[:20]
            kept.append(a)
        d["assumptions"] = kept
        return dropped


def _objects(d: dict[str, Any]) -> set[str]:
    return {x["key"] for x in d.get("definitions") or []} | {x["key"] for x in d.get("instances") or []} | {
        x["key"] for x in d.get("relations") or []}


def _has_cycle(children: dict[str, set[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        bad = any(visit(c) for c in children.get(n, ()))
        state[n] = 2
        return bad

    return any(visit(n) for n in list(children))


def _finish(run: _Run, reply: dict[str, Any], parent: EngineeringSpecVersion | None) -> None:
    v = run.v
    run.stage = "validate"
    answers: dict[str, dict[str, Any]] = {}
    if parent is not None and v.origin == "answer":
        for q in parent.content.get("questions") or []:
            if q.get("answer"):
                answers[q["id"]] = q["answer"]
        for a in v.answers:
            answers[a["question_id"]] = {k: a[k] for k in ("answer", "by", "by_name", "at", "source")}
    validator = _Validator(run, v.execution_policy, parent if v.origin == "answer" else None)
    _, structural, evidence = validator.inspect(reply)
    if structural:
        raise _Failed("validate", "spec_invalid", "the model reply did not pass the server checks", detail={"problems": structural[:40]},
                      retryable=True)
    content, record = validator.finalize(reply, answers)
    _write_manifest(run)
    content["sources"] = [{"id": s["id"], "kind": s.get("kind"), "file_version_id": s.get("file_version_id"), "label": s.get("label"),
                           "sha256": s.get("sha256"), "read_status": s.get("read_status"), "ranges": s.get("ranges") or [],
                           "error": s.get("error"), **({"answer_of": s["answer_of"]} if s.get("answer_of") else {})} for s in v.sources]
    # Generation reliability K (design §2.5): identity classes, the lineage's frozen baseline, target changes with reasons
    reason, evidence = _change_reason(v)
    structure_record = (v.planning.get("structure_review") or {}).get("applied_record")
    content = spec_identity.freeze(content, version=v, parent=lineage_parent(run.db, v), reason=reason, evidence=evidence,
                                   record=structure_record)
    v.content = content
    gate = pol.run_check(GATE, v.execution_policy, lambda: _gate(content))
    v.checks = [gate]
    v.validation = {"problems": evidence[:40], "repairs": int(v.planning.get("repairs", 0)), **record}
    open_q = [q for q in content["questions"] if not q.get("answer") and question_blocks(q)]
    v.status = "needs_answers" if (v.execution_policy == "standard" and open_q) else "ready"
    v.error = None
    v.planning = {**v.planning, "ai_call_ids": list(run.call_ids), "finished_at": _now().isoformat()}
    v.finished_at = _now()
    v.heartbeat_at = None
    run.db.flush()


def lineage_parent(db: Session, v: EngineeringSpecVersion) -> EngineeringSpecVersion | None:
    """The nearest earlier version of this spec lineage with content (a failed one in between — e.g. the extraction a
    continuation continues — has none): the target the new version is compared with (generation reliability K)."""
    seen: set[uuid.UUID] = set()
    pid = v.parent_version_id
    while pid is not None and pid not in seen and len(seen) < 50:
        seen.add(pid)
        p = db.get(EngineeringSpecVersion, pid)
        if p is None:
            return None
        if (p.content or {}).get("definitions"):
            return p
        pid = p.parent_version_id
    return None


def _change_reason(v: EngineeringSpecVersion) -> tuple[str, list[str]]:
    """Why a finished version's target may differ from its lineage parent's, and the evidence (generation reliability K,
    ``target_history``): a revision (its note and added / dropped files), answers, a re-extraction, a continuation."""
    rev = (v.planning or {}).get("revision") or {}
    if rev:
        ev = ([f"{rev['note_source']} · 修订说明"] if rev.get("note_source") else [])
        ev += [f"{a.get('source')} · {a.get('label')}" for a in rev.get("added_files") or [] if isinstance(a, dict)]
        ev += [f"dropped · {d.get('label')}" for d in rev.get("dropped_files") or [] if isinstance(d, dict)]
        return str(rev.get("reason") or "revise"), ev
    if v.origin == "answer":
        return "answer", [f"{a.get('source') or 'answer'} · {a.get('question_id')}" for a in v.answers or [] if isinstance(a, dict)]
    return {"reextract": "reextract", "continue": "continue"}.get(v.origin, "extract"), []


def _gate(content: dict[str, Any]) -> dict[str, Any]:
    rd = readiness(content, "standard")
    open_all = [q for q in content.get("questions") or [] if not q.get("answer")]
    open_q = [q["id"] for q in open_all if question_blocks(q)]
    return {"check": GATE, "status": "pending" if open_q else "passed", "open_questions": open_q,
            "open_non_blocking": [q["id"] for q in open_all if not question_blocks(q)], "blocked_definitions": rd["blocked_definitions"]}


# --- readiness, quantities, digest (pure; W4 / W8 use them) ------------------------------------------------


def question_blocks(q: dict[str, Any]) -> bool:
    """Whether an open question stops the definitions it affects from being built (standard policy). Decided from
    the subject's field name only (``spec_merge.field_blocks``), never from a flag the model wrote."""
    return spec_merge.field_blocks(str(q.get("subject") or ""))


def readiness(content: dict[str, Any], policy: str) -> dict[str, Any]:
    """Per definition / instance / relation: ready | blocked (| partial for an assembly with blocked descendants).

    Only open questions block, and only the keys their ``affects`` lists (plus what depends on them: the
    instances of a blocked definition, the relations touching a blocked instance). Turbo skips the gate."""
    defs = {d["key"]: d for d in content.get("definitions") or []}
    insts = content.get("instances") or []
    rels = content.get("relations") or []
    turbo = pol.is_turbo(policy)
    blocked: dict[str, set[str]] = {}
    if not turbo:
        for q in content.get("questions") or []:
            if q.get("answer") or not question_blocks(q):
                continue
            for k in q.get("affects") or []:
                blocked.setdefault(k, set()).add(q["id"])
    inst_state: dict[str, dict[str, Any]] = {}
    for i in insts:
        qs = set(blocked.get(i["key"], set())) | set(blocked.get(i["definition"], set()))
        inst_state[i["key"]] = {"state": "blocked" if qs else "ready", "questions": sorted(qs)}
    rel_state: dict[str, dict[str, Any]] = {}
    for r in rels:
        qs = set(blocked.get(r["key"], set()))
        for end in ("a", "b"):
            qs |= set(inst_state.get(r[end]["instance"], {}).get("questions", []))
        rel_state[r["key"]] = {"state": "blocked" if qs else "ready", "questions": sorted(qs)}
    children: dict[str, list[dict[str, Any]]] = {}
    for i in insts:
        children.setdefault(i.get("parent") or "", []).append(i)
    def_state: dict[str, dict[str, Any]] = {}

    def state_of(key: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if key in def_state:
            return def_state[key]
        own = sorted(blocked.get(key, set()))
        st = "blocked" if own else "ready"
        below: set[str] = set()
        if defs.get(key, {}).get("kind") == "assembly" and key not in stack:
            for i in children.get(key, []):
                child = state_of(i["definition"], stack + (key,))
                if inst_state[i["key"]]["state"] == "blocked" or child["state"] != "ready":
                    below |= set(inst_state[i["key"]]["questions"]) | set(child["questions"]) | set(child.get("below", []))
            if st == "ready" and below:
                st = "partial"
        out = {"state": st, "questions": own, "below": sorted(below)}
        def_state[key] = out
        return out

    for k in defs:
        state_of(k)
    return {
        "gate": "skipped_turbo" if turbo else "applied",
        "definitions": def_state,
        "instances": inst_state,
        "relations": rel_state,
        "buildable_definitions": sorted(k for k, s in def_state.items() if s["state"] == "ready" and defs[k].get("kind") != "assembly"),
        "blocked_definitions": sorted(k for k, s in def_state.items() if s["state"] == "blocked"),
    }


def buildable_definitions(content: dict[str, Any], policy: str) -> list[str]:
    """Part / standard-part definitions no open question blocks (what W4 may build first)."""
    return readiness(content, policy)["buildable_definitions"]


def quantities(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Quantity of every definition from the instance tree only (W0 §5): per parent and in total under the root.
    A definition with no path to the root has total 0 ("unplaced"); a cycle stops at the repeated definition."""
    defs = [d["key"] for d in content.get("definitions") or []]
    insts = content.get("instances") or []
    per_parent: dict[str, dict[str, int]] = {k: {} for k in defs}
    children: dict[str, list[str]] = {}
    for i in insts:
        per_parent.setdefault(i["definition"], {})
        per_parent[i["definition"]][i.get("parent") or ""] = per_parent[i["definition"]].get(i.get("parent") or "", 0) + 1
        children.setdefault(i.get("parent") or "", []).append(i["definition"])
    totals = {k: 0 for k in defs}
    root = content.get("root")
    roots = [root] if root in totals else [k for k in defs if k in children and all(k not in c for c in children.values())]

    def walk(key: str, mult: int, stack: tuple[str, ...]) -> None:
        if key in stack or len(stack) > 10:
            return
        for child in children.get(key, []):
            totals[child] = totals.get(child, 0) + mult
            walk(child, mult, stack + (key,))

    for r in roots:
        totals[r] = totals.get(r, 0) + 1
        walk(r, 1, ())
    return {k: {"total": totals.get(k, 0), "per_parent": per_parent.get(k, {}),
                "instances": [i["key"] for i in insts if i["definition"] == k]} for k in defs}


def _fmt_value(v: Any, unit: str | None) -> str:
    if v is None:
        return "?"
    return f"{v}{(' ' + unit) if unit and unit not in ('°',) else (unit or '')}"


def spec_digest(v: EngineeringSpecVersion, spec: EngineeringSpec | None = None, *, max_chars: int | None = None) -> tuple[str, bool]:
    """Text digest of a version for the planner / AI context: definitions with quantities and readiness, instances,
    relations, facts with evidence locators, open questions, conflicts, assumptions, deliverables, read state.
    Returns (text, truncated); a cut digest says how much was left out."""
    c = v.content or {}
    status, _ = effective_status(v)
    title = f"SP-{spec.number} {spec.title}" if spec else "Engineering spec"
    lines = [f"Engineering spec {title} v{v.version_no} (id {v.id}); policy {v.execution_policy}; status {status}"
             + ("; SIMULATED extraction" if v.simulated else "") + "; units mm, right-handed, Z up"]
    for s in v.sources or []:
        lines.append(f"source {s['id']} {s.get('kind')} \"{s.get('label')}\" sha256 {str(s.get('sha256') or '')[:12]}… read {s.get('read_status')}"
                     + (f"; unread {len(s.get('unread') or [])} chunk(s)" if s.get("unread") else "")
                     + (f"; error {s['error'].get('code')}" if s.get("error") else ""))
    if status not in ("ready", "needs_answers"):
        err = v.error or {}
        lines.append(f"not usable: {err.get('stage')} / {err.get('code')}: {err.get('message')}")
        return "\n".join(lines), False
    rd = readiness(c, v.execution_policy)
    qty = quantities(c)
    lines.append(f"root: {c.get('root') or '(none)'}")
    lines.append("definitions:")
    for d in c.get("definitions") or []:
        st = rd["definitions"].get(d["key"], {})
        lines.append(f"- {d['key']} {d.get('kind')} \"{d.get('name')}\"" + (f" no. {d['number']}" if d.get("number") else "")
                     + (f" material {d['material']}" if d.get("material") else "") + f" procurement {d.get('procurement')}"
                     + f" qty {qty.get(d['key'], {}).get('total', 0)}; {st.get('state', 'ready')}"
                     + (f" (waits for {', '.join(st.get('questions') or st.get('below') or [])})" if st.get("state") != "ready" else ""))
    if c.get("instances"):
        lines.append("instances:")
        for i in c["instances"]:
            pl = i.get("placement")
            where = (f"at ({', '.join(str(x) for x in pl.get('translate') or [])}) rotate ({', '.join(str(x) for x in pl.get('rotate') or [])})"
                     if pl else "placement not given")
            lines.append(f"- {i['key']} of {i['definition']} in {i.get('parent')} {where}")
    if c.get("relations"):
        lines.append("relations:")
        for r in c["relations"]:
            lines.append(f"- {r['key']} {r['kind']} {r['a']['instance']}.{r['a']['datum']} ~ {r['b']['instance']}.{r['b']['datum']}"
                         + (f" {json.dumps(r['params'], ensure_ascii=False)}" if r.get("params") else ""))
    lines.append("facts:")
    for f in c.get("facts") or []:
        ev = "; ".join(f"{e['source']} · {e.get('locator')}" + (f" “{clip(e['quote'], 80)}”" if e.get("quote") else "") for e in f.get("evidence") or [])
        lines.append(f"- {f['id']} {f['subject']} = {_fmt_value(f.get('value'), f.get('unit'))} [{f['status']}]" + (f" ({ev})" if ev else "")
                     + (f" derivation: {f['derivation']}" if f.get("derivation") else ""))
    open_q = [q for q in c.get("questions") or [] if not q.get("answer")]
    if open_q:
        lines.append("open questions (block only what they affect):")
        lines.extend(f"- {q['id']} {q['subject']}: {q['question']} affects {', '.join(q.get('affects') or [])}" for q in open_q)
    answered = [q for q in c.get("questions") or [] if q.get("answer")]
    if answered:
        lines.append("answered by a person:")
        lines.extend(f"- {q['id']} {q['subject']}: {q['answer'].get('answer')}" for q in answered)
    if c.get("conflicts"):
        lines.append("conflicts (every reading kept):")
        for x in c["conflicts"]:
            vals = ", ".join(f"{_fmt_value(val.get('value'), val.get('unit'))} ({val['source']})" for val in x["values"])
            lines.append(f"- {x['id']} {x['subject']}: {vals}" + (f" → {json.dumps(x['resolution'], ensure_ascii=False)}" if x.get("resolution") else ""))
    if c.get("assumptions"):
        lines.append("assumptions (AI-adopted values, NOT confirmed by a person):")
        for a in c["assumptions"]:
            lines.append(f"- {a['id']} {a['subject']} = {_fmt_value(a.get('adopted'), a.get('unit'))} by {a.get('author')}; basis: {a.get('basis')}")
    if c.get("manufacturing_notes"):
        lines.append("manufacturing notes:")
        lines.extend(f"- {n['id']} {n.get('subject') or '-'}: {clip(n['text'], 300)}" for n in c["manufacturing_notes"])
    if c.get("deliverables"):
        lines.append("deliverables: " + ", ".join(f"{x['kind']}{'' if x.get('required', True) else ' (optional)'}" for x in c["deliverables"]))
    out = "\n".join(lines)
    if max_chars is not None and len(out) > max_chars:
        return out[:max_chars].rstrip() + f"\n… (+{len(out) - max_chars} more characters of the spec digest not shown)", True
    return out, False


# --- planner input (collaboration planning, W2 part of the planner input) ----------------------------------

#: Budgets of the planner input (ai/limits.py).
PLANNER_SPEC_CHARS = limits.PLANNER_SPEC_CHARS
PLANNER_DOC_CHARS = limits.PLANNER_DOC_CHARS
PLANNER_TOTAL_CHARS = limits.PLANNER_TOTAL_CHARS


def planner_sources(db: Session, project_id: uuid.UUID, *, spec_version_id: uuid.UUID | None,
                    fixed_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the collaboration planner reads besides the demand: the spec digest of an exact version and the text of
    every text / Markdown fixed input, each with its read state (content | partial | metadata) and ranges. A spec
    version of another project is a 404; one that is not usable (extracting / failed) is a 409 before planning."""
    from ..models import AssetVersion

    out: list[dict[str, Any]] = []
    used = 0
    if spec_version_id is not None:
        v = db.get(EngineeringSpecVersion, spec_version_id)
        if v is None or v.project_id != project_id:
            raise not_found("engineering spec version")
        status, err = effective_status(v)
        if status not in ("ready", "needs_answers"):
            raise AppError(409, "spec_not_ready", f"engineering spec version {v.version_no} is {status}; plan from a finished version",
                           {"status": status, "error": err, "version_id": str(v.id)})
        spec = db.get(EngineeringSpec, v.spec_id)
        body, truncated = spec_digest(v, spec, max_chars=PLANNER_SPEC_CHARS)
        out.append({"key": f"engineering_spec_version:{v.id}", "kind": "engineering_spec_version",
                    "label": f"SP-{spec.number if spec else '?'} v{v.version_no} {spec.title if spec else ''}".strip(),
                    "read": "partial" if truncated else "content", "ranges": ["spec digest" + (" (cut)" if truncated else "")],
                    "chars": len(body), "text": body})
        used += len(body)
    for f in fixed_inputs:
        key, label = f"fixed:{f['slot_key']}", f"{f.get('asset_type') or ''} {f.get('label') or ''}".strip()
        av = db.get(AssetVersion, sr.uuid_or_none(f.get("asset_version_id")))
        fv = db.get(FileVersion, av.file_version_id) if av is not None and av.file_version_id else None
        content = sr.file_version_text(db, fv) if fv is not None else None
        if content is None:
            note = (f"{label}: only its type, label and version are known here (not a text file; build an engineering spec from it "
                    "to read a drawing).")
            out.append({"key": key, "kind": str(f.get("asset_type") or "document"), "label": label, "read": "metadata", "ranges": [],
                        "chars": 0, "text": note})
            continue
        limit = max(0, min(PLANNER_DOC_CHARS, PLANNER_TOTAL_CHARS - used))
        shown = content[:limit]
        body = shown + (f"\n… (+{len(content) - len(shown)} more characters not read here; build an engineering spec to read all of it)"
                        if len(shown) < len(content) else "")
        out.append({"key": key, "kind": "document", "label": label, "read": "content" if len(shown) == len(content) else "partial",
                    "ranges": [f"chars 0–{len(shown)} of {len(content)}"], "chars": len(shown), "text": body})
        used += len(shown)
    return out


# --- status and output -----------------------------------------------------------------------------------


def effective_status(v: EngineeringSpecVersion) -> tuple[str, dict[str, Any] | None]:
    """(status, error) as the API shows them: an extraction whose heartbeat stopped is reported as interrupted."""
    if v.status == "extracting" and v.heartbeat_at is not None and _now() - v.heartbeat_at > STALE_AFTER:
        return "failed", {"stage": "interrupted", "code": "extraction_interrupted",
                          "message": "the extraction stopped before it finished", "detail": {"last_heartbeat": _iso(v.heartbeat_at)},
                          "call_ids": list((v.planning or {}).get("ai_call_ids") or []), "retryable": True, "recovery": CONTINUE,
                          "recovery_message": RECOVERY["interrupted"]}
    return v.status, failure_info(v.error)


def _counts(v: EngineeringSpecVersion) -> dict[str, int]:
    c = v.content or {}
    return {"definitions": len(c.get("definitions") or []), "instances": len(c.get("instances") or []),
            "relations": len(c.get("relations") or []), "facts": len(c.get("facts") or []),
            "questions_open": sum(1 for q in c.get("questions") or [] if not q.get("answer")),
            "conflicts": len(c.get("conflicts") or []), "assumptions": len(c.get("assumptions") or []),
            "sources": len(v.sources or []), "sources_not_read": sum(1 for s in v.sources or [] if s.get("read_status") != "read")}


def version_brief(v: EngineeringSpecVersion) -> dict[str, Any]:
    status, error = effective_status(v)
    return {"id": str(v.id), "spec_id": str(v.spec_id), "version_no": v.version_no, "origin": v.origin, "status": status,
            "execution_policy": v.execution_policy, "simulated": v.simulated,
            "parent_version_id": str(v.parent_version_id) if v.parent_version_id else None, "counts": _counts(v),
            "error": {k: error.get(k) for k in ("stage", "code", "message", "retryable", "recovery")} if error else None,
            "created_by": str(v.created_by), "created_at": _iso(v.created_at), "finished_at": _iso(v.finished_at)}


def _chunk_out(c: EngineeringSpecChunk) -> dict[str, Any]:
    return {"seq": c.seq, "source_id": c.source_id, "source_chunk": c.source_chunk, "kind": c.kind, "locator": c.locator, "start": c.start,
            "end": c.end, "chars": c.chars, "status": c.status, "reason": c.reason, "ai_call_id": str(c.ai_call_id) if c.ai_call_id else None,
            "inherited_from_version_id": str(c.inherited_from_version_id) if c.inherited_from_version_id else None,
            "sent_at": _iso(c.sent_at), "text_sha256": c.text_sha256, "overlap": int((c.meta or {}).get("overlap") or 0),
            "findings_stored": c.findings is not None, "call_id": str(c.call_id) if c.call_id else None}


def _public_source(s: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in s.items() if not k.startswith("_")}


def version_out(db: Session, v: EngineeringSpecVersion) -> dict[str, Any]:
    status, error = effective_status(v)
    content = v.content or {}
    chunks = db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.id).order_by(EngineeringSpecChunk.seq)).all()
    usable = status in ("ready", "needs_answers")
    gate = next((c for c in v.checks or [] if c.get("check") == GATE), None)
    check_status = (gate or {}).get("status") or ("pending" if status == "extracting" else "not_requested")
    if check_status not in ("not_requested", "skipped_turbo", "pending", "passed", "failed", "unknown"):
        check_status = "unknown"
    nxt = continuation_of(db, v) if status == "failed" else None
    return {
        **version_brief(v),
        "error": error,
        "continued_by": {"id": str(nxt.id), "version_no": nxt.version_no} if nxt is not None else None,
        "content": content,
        "sources": [_public_source(s) for s in v.sources or []],
        "chunks": [_chunk_out(c) for c in chunks],
        "readiness": readiness(content, v.execution_policy) if usable else None,
        "quantities": quantities(content) if usable else None,
        "checks": list(v.checks or []),
        "validation": v.validation or {},
        "planning": v.planning or {},
        "answers": list(v.answers or []),
        "verification": pol.verification(v.execution_policy, check_status,  # type: ignore[arg-type]
                                          assumptions=[{"id": a["id"], "subject": a["subject"]} for a in content.get("assumptions") or []]),
        "model_config_id": str(v.model_config_id) if v.model_config_id else None,
    }


def spec_out(db: Session, spec: EngineeringSpec, *, with_versions: bool = False) -> dict[str, Any]:
    versions = db.scalars(select(EngineeringSpecVersion).where(EngineeringSpecVersion.spec_id == spec.id)
                          .order_by(EngineeringSpecVersion.version_no.desc())).all()
    creator = db.get(User, spec.created_by)
    out = {"id": str(spec.id), "project_id": str(spec.project_id), "number": spec.number, "code": f"SP-{spec.number}", "title": spec.title,
           "created_by": {"id": str(spec.created_by), "name": creator.name if creator else ""}, "created_at": _iso(spec.created_at),
           "updated_at": _iso(spec.updated_at), "versions_count": len(versions),
           "latest_version": version_brief(versions[0]) if versions else None}
    if with_versions:
        out["versions"] = [version_brief(v) for v in versions]
    return out


def list_specs(db: Session, project_id: uuid.UUID, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    q = select(EngineeringSpec).where(EngineeringSpec.project_id == project_id, EngineeringSpec.archived_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(EngineeringSpec.updated_at.desc()).limit(max(1, min(limit, 200))).offset(max(0, offset))).all()
    return {"items": [spec_out(db, s) for s in rows], "total": total}


def chunk_text(db: Session, v: EngineeringSpecVersion, seq: int) -> dict[str, Any]:
    """The exact text of one chunk as it was sent (evidence view). Attachments return no text."""
    c = db.scalar(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.id, EngineeringSpecChunk.seq == seq))
    if c is None:
        raise not_found("chunk")
    body = ""
    if c.kind != "attachment" and c.text_sha256:
        try:
            body = read_blob(db, c.text_sha256)[0].decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            body = ""
    return {**_chunk_out(c), "text": body}
