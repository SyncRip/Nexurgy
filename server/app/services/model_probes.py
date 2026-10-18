"""Generation-bound model discovery. Provider I/O never owns a database transaction."""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import diagnostics
from ..ai.providers.base import ProviderError
from ..config import get_settings
from ..db import session_scope
from ..models import ModelConfigRow
from . import model_credentials as credentials
from .audit import audit

DEADLINE_SECONDS = 300
CAPABILITIES = ("text", "structured", "vision", "tools")


def _now() -> datetime:
    return datetime.now(UTC)


def _expired(record: dict) -> bool:
    try:
        return datetime.fromisoformat(record["expires_at"]) <= _now()
    except (KeyError, TypeError, ValueError):
        return True


def _checks_out(checks: dict) -> dict:
    """Expose the stable UI contract without changing compatibility probe diagnostics."""
    result = {}
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        code = check.get("code") or check.get("error_code")
        code = code if isinstance(code, str) else None
        status = check.get("status")
        if status not in ("passed", "unsupported", "failed", "unknown"):
            if check.get("ok") is True:
                status = "passed"
            elif code in ("unsupported_capability", "runtime_unavailable"):
                status = "unsupported"
            elif check.get("ok") is False or code:
                status = "failed"
            else:
                status = "unknown"
        result[name] = {"status": status, "code": code}
    return result


def projection(row: ModelConfigRow, db: Session | None = None) -> dict:
    record = row.last_diagnose or {}
    fields = ("id", "status", "tested_config_version", "applied_config_version", "started_at", "finished_at", "expires_at")
    result = {key: record.get(key) for key in fields}
    result["status"] = record.get("status") if record.get("id") else "not_tested"
    result.update(stale=bool(record.get("id") and record.get("config_version") != row.config_version),
                  simulated=bool(record.get("simulated")),
                  detected={k: (record.get("detected") or {}).get(k) for k in CAPABILITIES},
                  checks=_checks_out(record.get("checks") or {}), error=record.get("error"))
    if db is not None and record.get("id") and row.credential_source == "env" and row.secret_ref:
        recorded = (record.get("credential") or {}).get("env_generation")
        if recorded is not None and credentials.current_env_generation(db, row.secret_ref, record=False) != recorded:
            result["stale"] = True
    generation_changed = (record.get("credential_status") != row.credential_status or record.get("enabled") != row.enabled)
    if result["status"] in ("queued", "running") and (_expired(record) or result["stale"] or generation_changed):
        result.update(status="interrupted", error={"code": "probe_interrupted", "message": "probe expired or configuration changed; retry detection"})
    return result


def queue(db: Session, row: ModelConfigRow, *, user_id: uuid.UUID | None = None, force: bool = False) -> str | None:
    """Caller holds the row/routing lock, commits, then schedules execute(id, attempt)."""
    current = projection(row, db)
    if not current["stale"] and current["status"] in ("queued", "running"):
        return str(current["id"])
    if not force and not current["stale"] and current["status"] == "passed":
        return None
    now = _now()
    credential = {"source": row.credential_source, "version": row.credential_version}
    if row.credential_source == "env" and row.secret_ref:
        credential["env_generation"] = credentials.current_env_generation(db, row.secret_ref)
    record = {"id": str(uuid.uuid4()), "status": "queued", "tested_config_version": row.config_version,
              "applied_config_version": None, "config_version": row.config_version, "credential": credential,
              "credential_status": row.credential_status, "enabled": row.enabled,
              "queued_at": now.isoformat(), "started_at": None, "finished_at": None,
              "expires_at": (now + timedelta(seconds=DEADLINE_SECONDS)).isoformat(),
              "simulated": False, "detected": {k: None for k in CAPABILITIES}, "checks": {}, "error": None,
              "requested_by": str(user_id) if user_id else None}
    row.last_diagnose = record
    return record["id"]


def _matches(db: Session, row: ModelConfigRow, record: dict, attempt: str) -> bool:
    credential = record.get("credential") or {}
    if (record.get("id") != attempt or record.get("tested_config_version") != row.config_version
            or credential.get("version") != row.credential_version
            or credential.get("source") != row.credential_source
            or record.get("credential_status") != row.credential_status or record.get("enabled") != row.enabled):
        return False
    if row.credential_source == "env" and row.secret_ref:
        return credentials.current_env_generation(db, row.secret_ref, record=False) == credential.get("env_generation")
    return True


def probe_provider(provider: Any, *, timeout_s: float) -> dict:
    """Synthetic vision/structured probe plus the real runtime's tool probe, all bounded."""
    from ..ai.harness.tool_agent import diagnose

    start = time.monotonic()
    connection = provider.test_connection(timeout_s=min(20.0, timeout_s / 4)).model_dump(mode="json")
    detected = {k: (connection.get("detected") or {}).get(k) for k in CAPABILITIES}
    result = {"ok": bool(connection.get("ok")), "detected": detected, "checks": {"connection": connection},
              "simulated": bool(connection.get("simulated")), "connection": connection}
    if not result["ok"]:
        result["error"] = {"code": connection.get("error_code") or "bad_response",
                           "message": connection.get("error_message") or "connection probe failed"}
        return result
    remaining = timeout_s - (time.monotonic() - start)
    if remaining <= 0:
        raise ProviderError("timeout", "probe deadline elapsed", retryable=True)
    tool = diagnose(provider, timeout_s=min(60.0, remaining / 3))
    result["requests"] = tool.get("requests", 0)
    result["checks"].update(tool.get("checks") or {})
    if tool.get("ok"):
        detected["tools"] = True
    elif (tool.get("error") or {}).get("code") in ("unsupported_capability", "invalid_request"):
        detected["tools"] = False
    else:
        # Transport/unknown failures are not evidence that tools are unsupported.
        result.update(ok=False, error=tool.get("error") or {"code": "bad_response", "message": "tool probe did not complete"})
    return result


def run_attempt(config_id: uuid.UUID, attempt_id: str) -> None:
    from . import ai

    provider = None
    outcome: dict | None = None
    with session_scope() as db:
        row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
        if row is None:
            return
        record = dict(row.last_diagnose or {})
        if record.get("status") != "queued" or _expired(record) or not _matches(db, row, record, str(attempt_id)):
            return
        record.update(status="running", started_at=_now().isoformat())
        try:
            if get_settings().ai_simulation:
                ai.require_simulation_allowed()
                outcome = {"ok": False, "simulated": True, "detected": {k: None for k in CAPABILITIES}, "checks": {}}
            else:
                snap = ai.snapshot_for_test(db, row)
                record["credential"] = snap["credential"]
                # Discover capability instead of trusting the old checkbox values.
                snap["capabilities"] = {k: True for k in CAPABILITIES}
                provider, _ = ai.provider_from_snapshot(db, snap, guard=False)

                def guard() -> None:
                    with session_scope() as live:
                        ai._live_check(live, str(config_id), snap["credential"]["version"], require_enabled=True)
                        if snap["credential"].get("source") == "env":
                            current = credentials.current_env_generation(live, provider.config.secret_ref, record=False)
                            if current != snap["credential"].get("env_generation"):
                                raise ProviderError("credential_changed", "credential changed during probe", retryable=False)

                provider.bind_guard(guard)
        except ProviderError as exc:
            outcome = {"ok": False, "simulated": False, "error": exc.to_dict(), "checks": {}}
        row.last_diagnose = record
    # No session or row lock survives into provider I/O.
    if outcome is None:
        try:
            remaining = (datetime.fromisoformat(record["expires_at"]) - _now()).total_seconds()
            if remaining <= 0:
                raise ProviderError("timeout", "probe deadline elapsed", retryable=True)
            outcome = probe_provider(provider, timeout_s=remaining)
        except ProviderError as exc:
            outcome = {"ok": False, "simulated": False, "error": exc.to_dict(), "checks": {}}
        except Exception:
            # SDK transport exceptions must not expose credentials or leave an endless spinner.
            outcome = {"ok": False, "simulated": False, "error": {"code": "unknown", "message": "probe failed unexpectedly"}, "checks": {}}
    with session_scope() as db:
        row = db.scalar(select(ModelConfigRow).where(ModelConfigRow.id == config_id).with_for_update())
        if row is None or not _matches(db, row, row.last_diagnose or {}, str(attempt_id)):
            return
        if _expired(record):
            outcome = {"ok": False, "error": {"code": "probe_interrupted", "message": "probe deadline elapsed"}}
        simulated = bool(outcome.get("simulated"))
        detected = {k: (outcome.get("detected") or {}).get(k) for k in CAPABILITIES}
        capabilities = dict(row.capabilities or {})
        if outcome.get("ok") and not simulated:
            capabilities.update({k: v for k, v in detected.items() if isinstance(v, bool)})
        changed = capabilities != (row.capabilities or {})
        if changed:
            row.capabilities = capabilities
            row.config_version += 1
        finished = _now().isoformat()
        state = "simulated" if simulated else "passed" if outcome.get("ok") else "failed"
        if (outcome.get("error") or {}).get("code") == "probe_interrupted":
            state = "interrupted"
        row.last_diagnose = {**record, "requests": 0, **outcome, "status": state, "detected": detected, "simulated": simulated,
                             "at": finished, "finished_at": finished, "config_version": row.config_version,
                             "applied_config_version": row.config_version, "capabilities_changed": changed}
        connection = outcome.get("connection") or {"ok": bool(outcome.get("ok")), "detected": detected,
                                                    "error_code": (outcome.get("error") or {}).get("code")}
        row.last_test = {**connection, "at": finished, "config_version": row.config_version,
                         "credential": record["credential"], "simulated": simulated}
        if not connection.get("ok") and not simulated:
            code = connection.get("error_code") or (outcome.get("error") or {}).get("code")
            row.last_test = {**row.last_test, "diagnostic": diagnostics.diagnostic(
                code, config_id=str(row.id), config_version=row.config_version, retryable=connection.get("retryable"))}
        audit(db, action="model_config.probe", subject_type="model_config", subject_id=row.id,
              user_id=uuid.UUID(record["requested_by"]) if record.get("requested_by") else None,
              data={"attempt_id": attempt_id, "status": state, "config_version": row.config_version,
                    "capabilities_changed": changed})


def execute(config_id: uuid.UUID, attempt_id: str) -> None:
    run_attempt(config_id, attempt_id)
