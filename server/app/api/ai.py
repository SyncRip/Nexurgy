from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..ai import registry
from ..ai import retry as call_retry
from ..ai.credentials import check_api_key
from ..ai.providers.base import ModelConfig, ProviderError, resolve_secret
from ..db import get_db
from ..errors import AppError, not_found
from ..models import AiCall, ModelConfigRow, NodeRun, User
from ..security import current_user, require_admin
from ..services import ai as ai_svc
from ..services import model_credentials as mc_svc
from ..services import model_probes
from ..services.audit import audit

router = APIRouter(tags=["ai"])


class CapabilitiesIn(BaseModel):
    text: bool = True
    vision: bool = False
    tools: bool = False
    structured: bool = True


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")


class CredentialIn(BaseModel):
    """Write-only credential input: exactly one of an environment variable name, a direct key, keep or clear.

    The key is never returned, logged or audited; validation errors name the field, never its value."""

    model_config = ConfigDict(extra="forbid")

    env_name: str | None = Field(default=None, max_length=84)
    api_key: SecretStr | None = Field(default=None, max_length=2000)
    keep: Literal[True] | None = None
    clear: Literal[True] | None = None


class ModelConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_for: list[Literal["assistant", "modeling", "intake", "planning"]] = Field(default_factory=list, max_length=4)
    label: str = Field(min_length=1, max_length=120)
    provider: Literal["gemini", "openai", "openai_compatible"]
    model_id: str = Field(min_length=1, max_length=120)
    base_url: str | None = Field(default=None, max_length=500)
    # Legacy form of an environment reference (env:GEMINI_API_KEY); same as credential.env_name.
    secret_ref: str | None = Field(default=None, pattern=r"^env:[A-Z][A-Z0-9_]{1,80}$")
    credential: CredentialIn | None = None
    capabilities: CapabilitiesIn = Field(default_factory=CapabilitiesIn)
    is_default: bool = False
    enabled: bool = True


class ModelConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_for: list[Literal["assistant", "modeling", "intake", "planning"]] | None = Field(default=None, max_length=4)
    expected_config_version: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    model_id: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, max_length=500)
    secret_ref: str | None = Field(default=None, pattern=r"^env:[A-Z][A-Z0-9_]{1,80}$")
    credential: CredentialIn | None = None
    capabilities: CapabilitiesIn | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # One credential version; omitted / null = every version that is not revoked yet (the current one included).
    version: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class _CredentialChange:
    kind: Literal["env", "direct", "clear"]
    env_ref: str | None = None
    api_key: str | None = field(default=None, repr=False)


def _credential_change(credential: CredentialIn | None, secret_ref: str | None, *, creating: bool) -> _CredentialChange | None:
    """The requested credential change (None = keep). Mutually exclusive inputs; errors name fields only."""
    given: list[str] = []
    if secret_ref is not None:
        given.append("secret_ref")
    if credential is not None:
        given += [k for k in ("env_name", "api_key", "keep", "clear") if getattr(credential, k) is not None]
    if len(given) > 1:
        raise AppError(422, "credential_conflict", "give exactly one of: an environment variable name, an API key, keep or clear",
                       {"fields": given})
    if not given or given[0] == "keep":
        if creating:
            raise AppError(422, "credential_required", "a new configuration needs a key: name an environment variable or enter the API key",
                           {"fields": ["credential.env_name", "credential.api_key"]})
        return None
    if given[0] == "clear":
        if creating:
            raise AppError(422, "credential_required", "a new configuration needs a key: name an environment variable or enter the API key",
                           {"fields": ["credential.env_name", "credential.api_key"]})
        return _CredentialChange("clear")
    if given[0] == "secret_ref":
        assert secret_ref is not None
        return _CredentialChange("env", env_ref=secret_ref)
    assert credential is not None
    if given[0] == "env_name":
        name = (credential.env_name or "").strip()
        if name.lower().startswith("env:"):
            name = name[4:]
        if not ENV_NAME_RE.match(name):
            raise AppError(422, "invalid_env_name", "the environment variable name must look like OPENAI_API_KEY",
                           {"field": "credential.env_name"})
        return _CredentialChange("env", env_ref=f"env:{name}")
    assert credential.api_key is not None
    key, problem = check_api_key(credential.api_key.get_secret_value())
    if problem is not None:
        msg = {"api_key_empty": "the API key is empty",
               "api_key_masked": "the masked placeholder is not a key; enter the new key or keep the stored one",
               "api_key_invalid": "the API key must be 8–1000 printable characters without spaces"}[problem]
        raise AppError(422, problem, msg, {"field": "credential.api_key"})
    return _CredentialChange("direct", api_key=key)


def _apply_credential(db: Session, row: ModelConfigRow, change: _CredentialChange | None, user: User) -> dict[str, Any] | None:
    """Write the change as a new credential version; returns audit metadata (never the key)."""
    if change is None:
        return None
    if change.kind == "env":
        assert change.env_ref is not None
        return mc_svc.set_env(db, row, change.env_ref, user_id=user.id)
    if change.kind == "direct":
        assert change.api_key is not None
        return mc_svc.set_direct(db, row, change.api_key, user_id=user.id)
    return mc_svc.clear(db, row)


def _validate_config(row: ModelConfigRow) -> None:
    _checked_config(label=row.label, provider=row.provider, model_id=row.model_id, base_url=row.base_url,
                    capabilities=row.capabilities or {})


def _checked_config(**fields: Any) -> ModelConfig:
    """Field rules of ModelConfig (base URL form, compatible gateways need one). Messages never repeat the input."""
    try:
        return ModelConfig(**fields)
    except ValidationError as exc:
        raise AppError(422, "invalid_model_config", "; ".join(str(e.get("msg") or "") for e in exc.errors())[:300],
                       {"fields": [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()]}) from None


def _routing_lock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(7264530030)"))


def _set_routing(db: Session, row: ModelConfigRow, *, default_for=None, is_default=None) -> None:
    """Global advisory lock precedes row locks. Clear old owners before unique-index writes."""
    claimed = sorted(set(default_for)) if default_for is not None else list(row.default_for or [])
    others = db.scalars(select(ModelConfigRow).where(ModelConfigRow.id != row.id)
                        .order_by(ModelConfigRow.id).with_for_update()).all()
    for other in others:
        if is_default is True:
            other.is_default = False
        if default_for is not None:
            other.default_for = [p for p in (other.default_for or []) if p not in claimed]
    db.flush()
    row.default_for = claimed
    if is_default is not None:
        row.is_default = is_default


def _saved(db: Session, row: ModelConfigRow, background_tasks: BackgroundTasks, *, attempt: str | None) -> dict:
    config_id = row.id
    db.commit()
    result = ai_svc.config_out(row, db=db)
    db.close()
    if attempt:
        background_tasks.add_task(model_probes.execute, config_id, attempt)
    return result


#: Fields whose change alters the connection (bumps config_version); credentials always do.
CONNECTION_FIELDS = ("model_id", "base_url", "capabilities")
#: Fields copied (with their new values) into audit rows; keys and references are summarised separately.
AUDITED_FIELDS = ("label", "model_id", "base_url", "capabilities", "is_default", "enabled", "default_for")


@router.get("/model-configs")
def list_configs(_: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Every signed-in user can see which models exist and whether they are usable (never keys)."""
    rows = db.scalars(select(ModelConfigRow).order_by(ModelConfigRow.is_default.desc(), ModelConfigRow.label)).all()
    uses = ai_svc.last_uses(db, [r.id for r in rows])
    return [ai_svc.config_out(r, db=db, uses=uses.get(r.id)) for r in rows]


@router.post("/model-configs", status_code=201)
def create_config(body: ModelConfigIn, background_tasks: BackgroundTasks, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    change = _credential_change(body.credential, body.secret_ref, creating=True)
    assert change is not None
    _routing_lock(db)
    row = ModelConfigRow(label=body.label, provider=body.provider, model_id=body.model_id, base_url=body.base_url,
                         capabilities=body.capabilities.model_dump(), is_default=False, default_for=[], enabled=body.enabled,
                         config_version=1)
    _validate_config(row)
    if change.kind == "env":
        row.credential_source, row.secret_ref, row.credential_status = "env", change.env_ref, "active"
    else:  # the direct key is stored right below, once the row has its id
        row.credential_source, row.secret_ref, row.credential_status = "direct", None, "none"
    db.add(row)
    db.flush()
    cred = _apply_credential(db, row, change, user)
    _set_routing(db, row, default_for=body.default_for, is_default=body.is_default)
    audit(db, action="model_config.create", subject_type="model_config", subject_id=row.id, user_id=user.id,
          data={"fields": {k: getattr(row, k) for k in AUDITED_FIELDS}, "provider": row.provider, "credential": cred,
                "config_version": row.config_version})
    attempt = model_probes.queue(db, row, user_id=user.id, force=True) if row.enabled and row.credential_status == "active" else None
    return _saved(db, row, background_tasks, attempt=attempt)


@router.patch("/model-configs/{config_id}")
def patch_config(config_id: uuid.UUID, body: ModelConfigPatch, background_tasks: BackgroundTasks, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    _routing_lock(db)
    row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
    if row is None:
        raise not_found("model config")
    if body.expected_config_version is not None and body.expected_config_version != row.config_version:
        raise AppError(409, "version_conflict", "reload the model configuration", {"current_version": row.config_version})
    nulls = sorted(k for k in ("label", "model_id", "capabilities", "default_for", "enabled", "is_default")
                   if k in body.model_fields_set and getattr(body, k) is None)
    if nulls:
        raise AppError(422, "invalid_model_config", "these fields cannot be null", {"fields": nulls})
    change = _credential_change(body.credential, body.secret_ref, creating=False)
    data = body.model_dump(exclude_unset=True, exclude={"credential", "secret_ref", "expected_config_version", "default_for", "is_default"})
    if "capabilities" in data and data["capabilities"] is not None:
        data["capabilities"] = body.capabilities.model_dump()  # type: ignore[union-attr]
    changed = {k: v for k, v in data.items() if getattr(row, k) != v}
    for k, v in changed.items():
        setattr(row, k, v)
    _validate_config(row)
    cred = _apply_credential(db, row, change, user)
    if cred is not None or any(k in CONNECTION_FIELDS for k in changed):
        row.config_version += 1
    _set_routing(db, row, default_for=body.default_for, is_default=body.is_default)
    routing = {k: getattr(row, k) for k in ("default_for", "is_default") if k in body.model_fields_set}
    audit(db, action="model_config.update", subject_type="model_config", subject_id=row.id, user_id=user.id,
          data={"fields": {**{k: v for k, v in changed.items() if k in AUDITED_FIELDS}, **routing}, "credential": cred,
                "config_version": row.config_version})
    # Every saved usable configuration can recover an untested, stale or failed probe.
    # queue() reuses an in-flight attempt or a current pass without another provider call.
    should_probe = row.enabled and row.credential_status == "active"
    attempt = model_probes.queue(db, row, user_id=user.id) if should_probe else None
    return _saved(db, row, background_tasks, attempt=attempt)


class ProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_config_version: int = Field(ge=1)


@router.get("/model-configs/{config_id}")
def get_config(config_id: uuid.UUID, _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ModelConfigRow, config_id)
    if row is None:
        raise not_found("model config")
    return ai_svc.config_out(row, db=db)


@router.post("/model-configs/{config_id}/probe", status_code=202)
def probe_config(config_id: uuid.UUID, body: ProbeIn, background_tasks: BackgroundTasks,
                 user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
    if row is None:
        raise not_found("model config")
    if body.expected_config_version != row.config_version:
        raise AppError(409, "version_conflict", "reload the model configuration", {"current_version": row.config_version})
    attempt = model_probes.queue(db, row, user_id=user.id, force=True)
    return _saved(db, row, background_tasks, attempt=attempt)


@router.post("/model-configs/{config_id}/credentials/revoke")
def revoke_credential(config_id: uuid.UUID, body: RevokeIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Revoke one credential version (or every one not yet revoked): frozen operations using it stop at their next
    request. Replacing a key keeps the old version for operations already running; revoking stops them."""
    row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
    if row is None:
        raise not_found("model config")
    current_before = row.credential_version
    meta = mc_svc.revoke(db, row, body.version)
    if meta is not None and current_before is not None and row.credential_version is None:
        row.config_version += 1
    if meta is not None:
        audit(db, action="model_config.credential_revoke", subject_type="model_config", subject_id=row.id, user_id=user.id,
              data={"credential": meta, "config_version": row.config_version})
    db.commit()
    return ai_svc.config_out(row, db=db)


@router.post("/model-credentials/rekey")
def rekey_credentials(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Re-encrypt every stored key with the current master key (after adding a new first entry to
    PARTROUTE_CREDENTIAL_KEYS). Counts only; no key leaves the server."""
    try:
        result = mc_svc.rekey(db)
    except ProviderError as exc:
        raise AppError(503, exc.code, exc.message) from None
    audit(db, action="model_credentials.rekey", subject_type="model_credentials", subject_id="all", user_id=user.id,
          data={"current_key_version": result["current_key_version"], "rekeyed": result["rekeyed"],
                "unreadable": len(result["unreadable"])})
    db.commit()
    return result


@router.post("/model-configs/{config_id}/test")
def test_config(config_id: uuid.UUID, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return _compatibility_probe(config_id, user, db)


def _compatibility_probe(config_id: uuid.UUID, user: User, db: Session) -> dict:
    row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
    if row is None:
        raise not_found("model config")
    attempt = model_probes.queue(db, row, user_id=user.id, force=True)
    db.commit()
    db.close()
    if attempt:
        model_probes.run_attempt(config_id, attempt)
    from ..db import session_scope
    with session_scope() as fresh:
        return ai_svc.config_out(fresh.get(ModelConfigRow, config_id), db=fresh)


class ListModelsIn(BaseModel):
    """The dialog's provider and base URL, and a key: a new one (environment variable name or direct key), or
    ``credential.keep`` / no credential with ``config_id`` = the saved key of that configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["gemini", "openai", "openai_compatible"]
    base_url: str | None = Field(default=None, max_length=500)
    config_id: uuid.UUID | None = None
    credential: CredentialIn | None = None


@router.post("/model-configs/models")
def list_models(body: ListModelsIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Models the endpoint offers to the key (``GET {base_url}/models``, Gemini ``models.list``), so the model id can
    be picked instead of typed. A listing call only: nothing is generated, saved or audited, and the key is used for
    this call alone. Provider failures come back as ``ok: false`` with a stable code, like a connection test."""
    change = _credential_change(body.credential, None, creating=False)
    if change is not None and change.kind == "clear":
        raise AppError(422, "credential_required", "listing models needs a key", {"fields": ["credential"]})
    row: ModelConfigRow | None = None
    if change is None:
        if body.config_id is None:
            raise AppError(422, "credential_required", "name an environment variable, enter the API key or give a saved configuration",
                           {"fields": ["credential.env_name", "credential.api_key", "config_id"]})
        row = db.get(ModelConfigRow, body.config_id)
        if row is None:
            raise not_found("model config")
        if row.provider != body.provider:
            raise AppError(422, "provider_mismatch", "the saved key belongs to a configuration of another provider", {"field": "provider"})
    cfg = _checked_config(label="model list", provider=body.provider, model_id="model-list", base_url=body.base_url)
    try:
        if row is not None:
            saved, _ = ai_svc.provider_from_snapshot(db, ai_svc.snapshot_for_test(db, row), None, guard=False)
            secret = saved.resolve_secret()
        elif change is not None and change.kind == "env":
            secret = resolve_secret(change.env_ref)
        else:
            assert change is not None and change.api_key is not None
            secret = change.api_key
        provider = registry.build_provider(cfg)
        provider.bind_secret(secret)
        models = provider.list_models()
    except ProviderError as exc:
        return {"ok": False, "models": [], "error_code": exc.code, "error_message": exc.message, "retryable": exc.retryable}
    return {"ok": True, "models": [m.model_dump() for m in models], "error_code": None, "error_message": None, "retryable": None}


DIAGNOSE_CHECKS = ("tool_calling", "parallel_tool_calls", "no_strict", "usage", "streaming")


@router.post("/ai/model-configs/{config_id}/diagnose")
def diagnose_config(config_id: uuid.UUID, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return _compatibility_probe(config_id, user, db)


@router.get("/runs/{run_id}/ai-calls")
def run_ai_calls(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """Model-call ledger of a run (every node, every attempt, failed calls included), oldest first.

    Same permission as the run detail (project members; others 404). Metadata only: no request or
    response bodies (prompts may hold private assistant context and customer text)."""
    from .workflows import _load_run

    run = _load_run(db, user, run_id)
    rows = db.execute(
        select(AiCall, NodeRun.node_id).join(NodeRun, NodeRun.id == AiCall.node_run_id)
        .where(NodeRun.run_id == run.id).order_by(AiCall.created_at, AiCall.id)
    ).all()
    return [ai_call_out(c, node_id) for c, node_id in rows]


def ai_call_out(c: AiCall, node_id: str | None = None) -> dict:
    return {
        "id": str(c.id),
        "node_run_id": str(c.node_run_id) if c.node_run_id else None,
        "node_id": node_id,
        "agent_execution_id": str(c.agent_execution_id) if c.agent_execution_id else None,
        "purpose": c.purpose,
        "provider": c.provider,
        "model_id": c.model_id,
        "status": c.status,
        "error_code": (c.error or {}).get("code") if isinstance(c.error, dict) else None,
        # Generation reliability J: the logical work unit, the attempt (from 1) and the failure class of a failed request
        "unit": (c.request or {}).get("unit") if isinstance(c.request, dict) else None,
        "attempt": (c.request or {}).get("attempt") if isinstance(c.request, dict) else None,
        "failure_class": call_retry.row_class(c.error) if c.status != "ok" else None,
        "latency_ms": c.latency_ms,
        "tokens_in": c.tokens_in,
        "tokens_out": c.tokens_out,
        "provider_request_id": ai_svc.provider_request_id(c),
        "simulated": c.simulated,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
