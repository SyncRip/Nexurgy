"""Model configs from the database → provider adapters; AI call records.

Simulation (a deterministic fake model) is used ONLY when
PARTROUTE_AI_SIMULATION=true. Every simulated result is labelled
`simulated: true` in revisions, steps and ai_calls, and never counts as a
real model run.

One resolver (W1, architecture §7) serves every use: ``provider_for`` (all real calls), the
connection test, ``/system/capabilities``, the model list readiness and the context budgets.
An AI operation, a planning attempt and a run resolve the explicit or default configuration
into an exact **snapshot** before their first context build (``freeze_model``)::

    {"v": 1, "purpose", "requested_config_id", "config_id", "config_version", "label", "provider",
     "model_id", "base_url", "capabilities", "credential": {"source", "version", "env_generation"?},
     "resolved_at"}

(or ``{"v": 1, "purpose", "requested_config_id", "simulated": true}``). A snapshot holds no key and
no reference value; it points at a credential version. ``provider_for(db, purpose, snapshot)`` builds
the provider of exactly that snapshot and enforces the live restrictions: a disabled configuration,
a cleared / revoked credential version and a changed environment value stop it with
config_disabled / credential_cleared / credential_revoked / credential_changed; a replaced (not
revoked) older direct key keeps serving the snapshots made with it. Later edits of the
configuration only affect new snapshots. Nothing silently switches model, provider or key.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, object_session

from ..ai import registry
from ..ai.providers.base import Capabilities, CredentialState, ModelConfig, ModelProvider, ProviderError, resolve_secret
from ..ai.registry import (
    REQUIRED_CAPABILITIES,
    config_status,
    credential_issue,
    select_config,
)
from ..config import get_settings
from ..models import AiCall, ModelConfigRow, ModelCredential
from . import model_credentials as creds

#: A config id, or a frozen snapshot (dict) from :func:`freeze_model` — the third argument of ``provider_for``.
ModelRef = uuid.UUID | str | dict | None
SNAPSHOT_VERSION = 1
CORE_PURPOSES = ("assistant", "modeling", "intake", "planning")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


# --- configs ---------------------------------------------------------------------------------------


def to_config(row: ModelConfigRow, cred: ModelCredential | None = None) -> ModelConfig:
    """The pydantic mirror of a row, with its credential state (never the key)."""
    if cred is None and row.credential_source == "direct" and row.credential_version is not None:
        sess = object_session(row)
        cred = creds.credential(sess, row.id, row.credential_version) if sess is not None and row.id is not None else None
    state = CredentialState(
        source=row.credential_source or "env",  # type: ignore[arg-type]
        version=row.credential_version,
        status=row.credential_status or "active",  # type: ignore[arg-type]
        key_version=cred.key_version if cred is not None else None,
    )
    from .model_probes import projection

    return ModelConfig(
        default_for=list(row.default_for or []), probe=projection(row, object_session(row)),
        id=str(row.id) if row.id else None,
        label=row.label,
        provider=row.provider,  # type: ignore[arg-type]
        model_id=row.model_id,
        base_url=row.base_url,
        secret_ref=row.secret_ref,
        capabilities=row.capabilities or {},
        is_default=row.is_default,
        enabled=row.enabled,
        config_version=row.config_version,
        credential=state,
    )


def enabled_configs(db: Session) -> list[ModelConfig]:
    return [to_config(r) for r in db.scalars(select(ModelConfigRow).where(ModelConfigRow.enabled.is_(True))).all()]


def all_configs(db: Session) -> list[ModelConfig]:
    return [to_config(r) for r in db.scalars(select(ModelConfigRow)).all()]


# --- snapshots -------------------------------------------------------------------------------------


def is_snapshot(ref: Any) -> bool:
    return isinstance(ref, dict) and ref.get("v") == SNAPSHOT_VERSION and (bool(ref.get("config_id")) or ref.get("simulated") is True)


def require_simulation_allowed() -> None:
    """Simulation is a dev/test facility, including replay of an already frozen simulated model."""
    if get_settings().env == "prod":
        raise ProviderError("simulation_forbidden", "simulation is not allowed in production", retryable=False)


def _choose(db: Session, purpose: str, preferred_id: uuid.UUID | str | None) -> ModelConfig:
    """Offline explicit/purpose/global assignment; unavailable assignments raise a stable ProviderError."""
    pid = str(preferred_id) if preferred_id else None
    return select_config(all_configs(db), purpose=purpose, preferred_id=pid)  # type: ignore[arg-type]


def _snapshot_of(db: Session, cfg: ModelConfig, purpose: str, requested: uuid.UUID | str | None) -> dict[str, Any]:
    row = db.get(ModelConfigRow, uuid.UUID(str(cfg.id)))
    assert row is not None
    cred = creds.ensure_current(db, row)
    if cred is None:
        raise ProviderError("missing_key", f"model config '{row.label}' has no usable key", retryable=False)
    credential: dict[str, Any] = {"source": cred.source, "version": cred.version}
    if cred.source == "env":
        assert cred.env_ref is not None
        value = resolve_secret(cred.env_ref)  # missing_key names the variable, never the value
        credential["env_generation"] = creds.env_generation(db, cred.env_ref, value)
    return {
        "v": SNAPSHOT_VERSION,
        "purpose": purpose,
        "requested_config_id": str(requested) if requested else None,
        "config_id": str(row.id),
        "config_version": row.config_version,
        "label": row.label,
        "provider": row.provider,
        "model_id": row.model_id,
        "base_url": row.base_url,
        "capabilities": Capabilities.model_validate(row.capabilities or {}).model_dump(),
        "credential": credential,
        "resolved_at": _now_iso(),
    }


def _simulated_snapshot(purpose: str, requested: uuid.UUID | str | None) -> dict[str, Any]:
    require_simulation_allowed()
    return {"v": SNAPSHOT_VERSION, "purpose": purpose, "requested_config_id": str(requested) if requested else None,
            "simulated": True, "resolved_at": _now_iso()}


def freeze_model(db: Session, purpose: str, preferred_id: uuid.UUID | str | None) -> dict[str, Any]:
    """Resolve the explicit (or default) configuration for ``purpose`` into an exact snapshot.

    Raises ProviderError (stable code) when nothing usable is configured; with simulation on, the
    snapshot is the labelled simulated model instead. Writes only credential bookkeeping rows
    (env generation, a missing version row); the caller commits."""
    if get_settings().ai_simulation:
        require_simulation_allowed()
    try:
        cfg = _choose(db, purpose, preferred_id)
        return _snapshot_of(db, cfg, purpose, preferred_id)
    except ProviderError:
        if get_settings().ai_simulation:
            return _simulated_snapshot(purpose, preferred_id)
        raise


def freeze_model_bundle(db: Session, *, purposes=None, preferred=None) -> dict[str, Any]:
    """Capture every retained purpose, including a failed selection. Never defer default selection."""
    entries = {}
    for purpose in (CORE_PURPOSES if purposes is None else purposes):
        try:
            entries[purpose] = {"snapshot": freeze_model(db, purpose, (preferred or {}).get(purpose))}
        except ProviderError as exc:
            entries[purpose] = {"error": exc.to_dict()}
    return {"v": SNAPSHOT_VERSION, "entries": entries}


def refreeze_failed(db: Session, bundle: Any, *, preferred=None) -> Any:
    """A copy of ``bundle`` whose failed selections are resolved again; frozen snapshots stay exactly as they were.

    A failed slot froze no model, so a retry has nothing immutable to keep there. Re-resolving it lets the recovery the
    error named (run detection, enter a key, assign a default) take effect instead of replaying the old failure."""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("entries"), dict):
        return bundle
    entries = {}
    for purpose, entry in bundle["entries"].items():
        if isinstance(entry, dict) and entry.get("error"):
            try:
                entry = {"snapshot": freeze_model(db, purpose, (preferred or {}).get(purpose))}
            except ProviderError as exc:
                entry = {"error": exc.to_dict()}
        entries[purpose] = entry
    return {**bundle, "entries": entries}


def bundle_model_ref(bundle: Any, purpose: str, *, legacy_snapshot: Any = None) -> dict[str, Any]:
    """Only read a frozen bundle; failure/missing slots never consult current defaults."""
    entry = (bundle.get("entries") or {}).get(purpose) if isinstance(bundle, dict) else None
    if isinstance(entry, dict) and entry.get("error"):
        error = entry["error"]
        raise ProviderError(error.get("code") or "model_snapshot_missing", error.get("message") or "model selection failed",
                            retryable=bool(error.get("retryable")), provider=error.get("provider"))
    snap = entry.get("snapshot") if isinstance(entry, dict) else None
    if not is_snapshot(snap):
        snap = legacy_snapshot
    if not is_snapshot(snap):
        raise ProviderError("model_snapshot_missing", "the original model snapshot is missing; start a new request", retryable=False)
    cfg = snapshot_config(snap)
    if cfg is not None:
        registry.require_capabilities(cfg, REQUIRED_CAPABILITIES.get(purpose, set()))
    return {**deepcopy(snap), "purpose": purpose}


def try_freeze_model(db: Session, purpose: str, preferred_id: uuid.UUID | str | None) -> dict[str, Any] | None:
    """:func:`freeze_model`, or None when nothing is usable: the use site then resolves and fails with the precise error."""
    try:
        return freeze_model(db, purpose, preferred_id)
    except ProviderError:
        return None


def frozen_ref(snapshot: Any, fallback_id: uuid.UUID | str | None) -> ModelRef:
    """What to pass to ``provider_for`` / context builds: the frozen snapshot when there is one, else the config id."""
    return snapshot if is_snapshot(snapshot) else fallback_id


def snapshot_config(snap: dict[str, Any]) -> ModelConfig | None:
    """The configuration a snapshot froze (None for the simulated model). No key, no reference."""
    if snap.get("simulated"):
        return None
    c = snap.get("credential") or {}
    return ModelConfig(
        id=str(snap["config_id"]), label=str(snap.get("label") or ""), provider=snap["provider"], model_id=snap["model_id"],
        base_url=snap.get("base_url"), capabilities=snap.get("capabilities") or {}, is_default=False, enabled=True,
        config_version=snap.get("config_version"),
        credential=CredentialState(source=c.get("source") or "env", version=c.get("version"), status="active"),
    )


def _live_check(db: Session, config_id: str, version: int | None, *, require_enabled: bool = True) -> tuple[ModelConfigRow, ModelCredential]:
    row = db.get(ModelConfigRow, uuid.UUID(config_id))
    if row is None:
        raise ProviderError("config_disabled", "the model configuration of this request no longer exists", retryable=False)
    if require_enabled and not row.enabled:
        raise ProviderError("config_disabled", f"model configuration '{row.label}' was disabled", retryable=False)
    cred = creds.credential(db, row.id, version)
    if cred is None:
        raise ProviderError("credential_cleared", f"the key of '{row.label}' used by this request no longer exists", retryable=False)
    if cred.status in ("cleared", "revoked"):
        raise ProviderError(f"credential_{cred.status}", f"the key of '{row.label}' used by this request was {cred.status}",  # type: ignore[arg-type]
                            retryable=False)
    return row, cred


def _guard(config_id: str, version: int | None):
    """Live restriction checked before every model request of a snapshot provider (own short session)."""

    def check() -> None:
        from ..db import session_scope

        with session_scope() as s:
            _live_check(s, config_id, version)

    return check


def provider_from_snapshot(db: Session, snap: dict[str, Any], purpose: str | None = None, *,
                           guard: bool = True) -> tuple[ModelProvider, ModelConfig | None]:
    """The provider of exactly this snapshot, or ProviderError with a stable code (no request is made).

    ``guard`` False (connection tests of a saved, possibly disabled configuration): the enabled flag is not
    required and no per-request guard is bound; cleared / revoked credentials still stop it."""
    if snap.get("simulated"):
        require_simulation_allowed()
        return registry.build_simulated_provider((purpose or snap.get("purpose") or "modeling")), None  # type: ignore[arg-type]
    c = snap.get("credential") or {}
    row, cred = _live_check(db, str(snap["config_id"]), c.get("version"), require_enabled=guard)
    cfg = snapshot_config(snap)
    assert cfg is not None
    need = REQUIRED_CAPABILITIES.get(purpose or "", set())
    missing = cfg.capabilities.missing(need)
    if missing:
        raise ProviderError("unsupported_capability", f"model config '{cfg.label}' ({cfg.model_id}) lacks: {', '.join(missing)}",
                            provider=cfg.provider, retryable=False)
    if cred.source == "env":
        assert cred.env_ref is not None
        value = resolve_secret(cred.env_ref)
        if creds.env_generation(db, cred.env_ref, value, record=False) != c.get("env_generation"):
            raise ProviderError("credential_changed", f"the server's key behind '{cfg.label}' changed since this request started; "
                                "start it again to use the new key", retryable=False)
        secret = value
        cfg = cfg.model_copy(update={"secret_ref": cred.env_ref})
    else:
        secret = creds.decrypt(cred)
    provider = registry.build_provider(cfg)  # looked up at call time (tests patch registry.build_provider)
    provider.bind_secret(secret)
    if guard:
        provider.bind_guard(_guard(str(row.id), cred.version))
    return provider, cfg


def snapshot_for_test(db: Session, row: ModelConfigRow) -> dict[str, Any]:
    """Snapshot of one saved configuration for its connection test (enabled or not; no capability requirement).
    Raises ProviderError with the credential's stable code when it has no usable key."""
    cfg = to_config(row)
    problem = credential_issue(cfg)
    if problem is not None:
        raise problem
    return _snapshot_of(db, cfg, "test", row.id)


def provider_for(db: Session, purpose: str, preferred_id: ModelRef) -> tuple[ModelProvider, ModelConfig | None]:
    """Pick the node's config (or the default) and build its adapter.

    ``preferred_id`` is a config id (resolved now, like before) or a frozen snapshot from
    :func:`freeze_model` (built exactly, with the live restrictions). Raises ProviderError
    (missing_key / config_disabled / credential_changed / …) with a message the UI can show
    when nothing usable is configured.
    """
    if is_snapshot(preferred_id):
        return provider_from_snapshot(db, preferred_id, purpose)  # type: ignore[arg-type]
    snap = freeze_model(db, purpose, preferred_id)  # type: ignore[arg-type]
    return provider_from_snapshot(db, snap, purpose)


def config_for(db: Session, purpose: str, ref: ModelRef) -> ModelConfig | None:
    """The config ``provider_for`` would use (None = simulated / nothing usable). No network, no key."""
    if is_snapshot(ref):
        return snapshot_config(ref)  # type: ignore[arg-type]
    try:
        return _choose(db, purpose, ref if not isinstance(ref, dict) else None)
    except ProviderError:
        return None


# --- frozen snapshots of runs and roles ------------------------------------------------------------

#: Node types that call a model, and the purpose they resolve.
AI_NODE_PURPOSE = {"model_3d": "modeling", "drawing_reconstruct": "modeling", "drawing_intake": "intake",
                   # W8: spec extraction chooses its model like planning (ai/spec_agent.PROVIDER_PURPOSE); parts are modeled
                   "engineering_spec": "planning", "concept_design": "planning", "assembly_from_spec": "modeling"}
#: Agent profile purpose → model purposes its calls use.
ROLE_PURPOSES = {"execute": ("agent_execute",), "review": ("agent_review",), "planner": ("planning",),
                 "general": ("agent_execute", "agent_review")}


def _entry_key(purpose: str, preferred_id: Any) -> str:
    return f"{purpose}:{preferred_id or 'default'}"


def freeze_run_models(db: Session, run: Any, *, seed: dict[str, Any] | None = None,
                      bundle: dict[str, Any] | None = None) -> None:
    """Freeze each graph choice once, preserving inherited failures as well as successful snapshots."""
    entries: dict[str, Any] = {}
    for n in (run.graph_snapshot or {}).get("nodes") or []:
        purpose = AI_NODE_PURPOSE.get(n.get("type"))
        if purpose is None:
            continue
        preferred = (n.get("params") or {}).get("model_config_id") or None
        key = _entry_key(purpose, preferred)
        if key in entries:
            continue
        try:
            if bundle is not None and not preferred:
                entries[key] = bundle_model_ref(bundle, purpose, legacy_snapshot=seed)
            elif is_snapshot(seed) and seed.get("purpose") == purpose and (seed.get("requested_config_id") or None) == (str(preferred) if preferred else None):
                entries[key] = deepcopy(seed)
            else:
                entries[key] = freeze_model(db, purpose, preferred)
        except ProviderError as exc:
            entries[key] = {"v": SNAPSHOT_VERSION, "error": exc.to_dict()}
    run.model_snapshot = {"v": SNAPSHOT_VERSION, "entries": entries}


def refreeze_failed_run_models(db: Session, run: Any) -> None:
    """Resolve the run's failed selections again before a retry (see :func:`refreeze_failed`); snapshots stay."""
    entries = dict(((getattr(run, "model_snapshot", None) or {}).get("entries") or {}))
    changed = False
    for key, entry in entries.items():
        if not (isinstance(entry, dict) and entry.get("error")):
            continue
        purpose, _, preferred = key.partition(":")
        try:
            entries[key] = freeze_model(db, purpose, None if preferred in ("", "default") else preferred)
        except ProviderError as exc:
            entries[key] = {"v": SNAPSHOT_VERSION, "error": exc.to_dict()}
        changed = True
    if changed:
        run.model_snapshot = {**run.model_snapshot, "entries": entries}


def run_model_ref(run: Any, purpose: str, preferred_id: uuid.UUID | str | None) -> ModelRef:
    """Resolve a node only from the run's original snapshot, never a newly selected default."""
    entries = ((getattr(run, "model_snapshot", None) or {}).get("entries") or {}) if run is not None else {}
    snap = entries.get(_entry_key(purpose, str(preferred_id) if preferred_id else None))
    if is_snapshot(snap):
        return snap
    if isinstance(snap, dict) and snap.get("error"):
        return bundle_model_ref({"entries": {purpose: snap}}, purpose)
    raise ProviderError("model_snapshot_missing", "this run has no original model snapshot; start a new run", retryable=False)


def freeze_role_models(db: Session, model_config_id: uuid.UUID | str | None, profile_purpose: str | None) -> dict[str, Any]:
    """{model purpose: snapshot} of an agent role, frozen when a collaboration run starts."""
    out: dict[str, Any] = {}
    for purpose in ROLE_PURPOSES.get(profile_purpose or "general", ROLE_PURPOSES["general"]):
        snap = try_freeze_model(db, purpose, model_config_id)
        if snap is not None:
            out[purpose] = snap
    return out


def role_model_ref(role_snapshot: dict[str, Any], purpose: str) -> ModelRef:
    """A role's frozen snapshot for ``purpose`` (agent_execute also serves its tools' modeling calls), else its config id."""
    snaps = role_snapshot.get("model_snapshots") or {}
    snap = snaps.get(purpose) or (snaps.get("agent_execute") if purpose == "modeling" else None)
    if is_snapshot(snap):
        return snap
    raw = role_snapshot.get("model_config_id")
    if not raw and isinstance(role_snapshot.get("model_config"), dict):
        raw = role_snapshot["model_config"].get("id")
    return _uuid(raw)


# --- call records ----------------------------------------------------------------------------------


def call_error(data: dict[str, Any]) -> dict[str, Any] | None:
    """``ai_calls.error`` of a call record: code, message and (when reported) the provider request id. Generation
    reliability J: the failure class, its recovery action and what came next (``retry``: waited ``in_s`` before the next
    attempt, or ``stopped`` with why it was not retried)."""
    if not data.get("error"):
        return None
    out = {"code": data.get("error_code"), "message": data.get("error")}
    if data.get("request_id"):
        out["request_id"] = data["request_id"]
    from ..ai.providers.base import RECOVERY_BY_CLASS, classify

    cls = data.get("failure_class") or classify(data.get("error_code"))
    out["failure_class"] = cls
    out["recovery"] = RECOVERY_BY_CLASS.get(cls, "retry")
    if data.get("retry"):
        out["retry"] = data["retry"]
    return out


def provider_request_id(row: AiCall) -> str | None:
    """Provider request id of a stored call: the response's for a successful call, the error's for a failed one."""
    for blob in (row.response, row.error):
        rid = blob.get("request_id") if isinstance(blob, dict) else None
        if isinstance(rid, str) and rid:
            return rid
    return None


def record_calls(db: Session, calls: list[Any], node_run_id: uuid.UUID | None, *, ai_operation_id: uuid.UUID | None = None) -> list[AiCall]:
    """Write one ai_calls row per call record (returned; ids are set on flush). Run steps pass ``node_run_id``;
    calls made directly by an AI workspace operation (ask) pass ``ai_operation_id``."""
    rows: list[AiCall] = []
    for c in calls:
        data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
        cfg_id = data.get("model_config_id")
        rows.append(
            AiCall(
                node_run_id=node_run_id,
                ai_operation_id=ai_operation_id,
                model_config_id=uuid.UUID(cfg_id) if cfg_id else None,
                provider=data.get("provider", "unknown"),
                model_id=data.get("model_id", "unknown"),
                purpose=data.get("purpose", "unknown"),
                request=data.get("request") or {},
                response=data.get("response"),
                status=data.get("status", "error"),
                error=call_error(data),
                latency_ms=data.get("latency_ms"),
                tokens_in=data.get("tokens_in"),
                tokens_out=data.get("tokens_out"),
                simulated=bool(data.get("simulated")),
            )
        )
    db.add_all(rows)
    return rows


# --- output ----------------------------------------------------------------------------------------


def _test_out(db: Session, row: ModelConfigRow) -> dict[str, Any] | None:
    """``last_test`` with ``stale``: made for another config / credential version, or the env value changed since."""
    lt = row.last_test
    if not isinstance(lt, dict):
        return None
    out = dict(lt)
    stale = lt.get("config_version") != row.config_version
    tested = lt.get("credential") or {}
    if not stale and row.credential_source == "env" and tested.get("env_generation") is not None and row.secret_ref:
        stale = creds.current_env_generation(db, row.secret_ref, record=False) != tested.get("env_generation")
    out["stale"] = bool(stale)
    return out


def last_uses(db: Session, config_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, dict[str, Any]]]:
    """Latest real (not simulated) call per config and purpose: shown apart from readiness and the probe."""
    if not config_ids:
        return {}
    rows = db.execute(text(
        "SELECT DISTINCT ON (model_config_id, purpose) id, model_config_id, purpose, status, error, created_at "
        "FROM ai_calls WHERE model_config_id = ANY(:ids) AND simulated = false "
        "ORDER BY model_config_id, purpose, created_at DESC"), {"ids": list(config_ids)}).all()
    out: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    for rid, cid, purpose, status, error, at in rows:
        out.setdefault(cid, {})[purpose] = {"call_id": str(rid), "status": status,
                                            "error_code": (error or {}).get("code") if isinstance(error, dict) else None,
                                            "at": at.isoformat() if at else None}
    return out


def config_out(row: ModelConfigRow, *, db: Session | None = None, uses: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """A configuration as the API returns it: source, presence, versions and test state — never a key or ciphertext."""
    sess = db or object_session(row)
    cred_rows = creds.versions(sess, row.id) if sess is not None and row.id is not None else []
    current = next((c for c in cred_rows if c.version == row.credential_version), None)
    status = config_status(to_config(row, current))
    return {
        "id": str(row.id),
        "label": row.label,
        "provider": row.provider,
        "model_id": row.model_id,
        "base_url": row.base_url,
        # env:NAME of an environment credential (a variable name, not a key); null for a direct key.
        "secret_ref": row.secret_ref,
        "credential_source": row.credential_source,
        "credential_status": row.credential_status,
        "credential_version": row.credential_version,
        "config_version": row.config_version,
        "credential_versions": [
            {"version": c.version, "source": c.source, "status": c.status,
             "created_at": c.created_at.isoformat() if c.created_at else None,
             "replaced_at": c.replaced_at.isoformat() if c.replaced_at else None,
             "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
             "cleared_at": c.cleared_at.isoformat() if c.cleared_at else None}
            for c in cred_rows
        ],
        "capabilities": row.capabilities,
        "is_default": row.is_default,
        "default_for": list(row.default_for or []),
        "probe": to_config(row, current).probe,
        "ready_for": {purpose: config_status(to_config(row, current), purpose=purpose).ready for purpose in CORE_PURPOSES},
        "enabled": row.enabled,
        "key_present": status.key_present,
        "ready": status.ready,
        "issues": status.issues,
        "issue_codes": status.issue_codes,
        "last_test": _test_out(sess, row) if sess is not None else row.last_test,
        # P1 capability diagnosis (POST /ai/model-configs/{id}/diagnose), bound to the config version like last_test.
        "last_diagnose": ({**row.last_diagnose, "stale": row.last_diagnose.get("config_version") != row.config_version}
                          if isinstance(row.last_diagnose, dict) else None),
        "last_use": uses or {},
    }
