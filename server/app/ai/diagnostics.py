"""Stable AI failure diagnostics (W1, README §5): what failed, where, with which configuration, how to recover.

``ai_unavailable`` alone cannot tell a missing key from an expired one, a gateway speaking another
protocol, a truncated reply or an invalid plan. Every AI failure surface (collaboration planning,
AI workspace operations, connection tests) attaches a :func:`diagnostic` payload::

    {"code", "stage", "purpose", "config_id", "config_version", "call_ids", "retryable", "recovery", "provider_code"}

``code`` is one of :data:`CODES`; ``stage`` says where it happened (resolve: picking the configuration and
its credential; context: building the request; model_call: the provider request; validation: checking
the reply; tool: CAD build / conversion; run: a run node); ``recovery`` lists action codes the UI turns
into hints. Codes describe the observed failure only; they never claim a root cause that was not seen.
Pure module: no DB, no network, no key material (messages are not part of the payload).
"""

from __future__ import annotations

from typing import Any, Literal

from .providers.base import ProviderError

Code = Literal[
    "missing_key", "master_key_missing", "decrypt_failed", "credential_changed", "config_disabled", "credential_cleared",
    "credential_revoked", "auth_failed", "permission_denied", "protocol_mismatch", "model_not_found", "rate_limited",
    "quota_exhausted", "network_error", "timeout", "context_overflow", "output_truncated", "invalid_plan", "cad_unsupported",
    "conversion_failed", "budget_exceeded", "simulation_forbidden", "unknown",
    "model_default_missing", "model_snapshot_missing", "model_probe_required", "model_probe_pending", "model_probe_failed", "runtime_unavailable",
]
CODES: tuple[str, ...] = (
    "missing_key", "master_key_missing", "decrypt_failed", "credential_changed", "config_disabled", "credential_cleared",
    "credential_revoked", "auth_failed", "permission_denied", "protocol_mismatch", "model_not_found", "rate_limited",
    "quota_exhausted", "network_error", "timeout", "context_overflow", "output_truncated", "invalid_plan", "cad_unsupported",
    "conversion_failed", "budget_exceeded", "simulation_forbidden", "unknown",
    "model_default_missing", "model_snapshot_missing", "model_probe_required", "model_probe_pending", "model_probe_failed", "runtime_unavailable",
)
Stage = Literal["resolve", "context", "model_call", "validation", "tool", "run"]

#: Codes raised while resolving the configuration / credential (before any provider request).
RESOLVE_CODES = frozenset({"missing_key", "master_key_missing", "decrypt_failed", "credential_changed", "config_disabled",
                           "credential_cleared", "credential_revoked", "simulation_forbidden", "model_default_missing",
                           "model_snapshot_missing", "model_probe_required", "model_probe_pending", "model_probe_failed", "runtime_unavailable"})

#: ProviderError code → diagnostic code (the remaining provider codes need the message, see :func:`code_of`).
_PROVIDER: dict[str, str] = {
    "simulation_forbidden": "simulation_forbidden",
    "missing_key": "missing_key", "master_key_missing": "master_key_missing", "decrypt_failed": "decrypt_failed",
    "credential_changed": "credential_changed", "config_disabled": "config_disabled", "credential_cleared": "credential_cleared",
    "credential_revoked": "credential_revoked", "auth_failed": "auth_failed", "permission_denied": "permission_denied",
    "model_not_found": "model_not_found", "quota_exceeded": "quota_exhausted", "rate_limited": "rate_limited",
    "unsupported_capability": "protocol_mismatch", "network": "network_error", "timeout": "timeout", "unknown": "unknown",
}
#: Application error codes (operations, runs, tools) → diagnostic code.
_APP: dict[str, str] = {
    "context_over_budget": "context_overflow", "invalid_plan": "invalid_plan", "plan_invalid": "invalid_plan",
    "unsupported": "cad_unsupported", "simulation_unsupported": "cad_unsupported", "cad_unsupported": "cad_unsupported",
    "conversion_failed": "conversion_failed", "dwg_conversion_failed": "conversion_failed", "dwg_write_failed": "conversion_failed",
    "dwg_read_failed": "conversion_failed", "budget_exceeded": "budget_exceeded", "cost_cap_exceeded": "budget_exceeded",
    "output_truncated": "output_truncated", "protocol_mismatch": "protocol_mismatch", "quota_exhausted": "quota_exhausted",
    "network_error": "network_error",
}

RECOVERY: dict[str, list[str]] = {
    "model_default_missing": ["contact_admin"],
    "model_snapshot_missing": ["retry_new_snapshot"],
    "model_probe_required": ["test_config"],
    "model_probe_pending": ["retry_later"],
    "model_probe_failed": ["test_config"],
    "runtime_unavailable": ["choose_other_config"],
    "simulation_forbidden": ["contact_admin"],
    "missing_key": ["configure_key", "choose_other_config"],
    "master_key_missing": ["configure_master_key", "choose_other_config"],
    "decrypt_failed": ["restore_master_key", "replace_key"],
    "credential_changed": ["retry_new_snapshot"],
    "config_disabled": ["enable_config", "choose_other_config"],
    "credential_cleared": ["replace_key", "choose_other_config"],
    "credential_revoked": ["replace_key", "choose_other_config"],
    "auth_failed": ["replace_key", "test_config"],
    "permission_denied": ["check_model_access", "choose_other_config"],
    "protocol_mismatch": ["check_protocol", "test_config"],
    "model_not_found": ["check_model_id", "test_config"],
    "rate_limited": ["retry_later"],
    "quota_exhausted": ["check_quota", "choose_other_config"],
    "network_error": ["retry_later", "check_base_url"],
    "timeout": ["retry_later", "shorten_input"],
    "context_overflow": ["shorten_input", "choose_larger_model"],
    "output_truncated": ["shorten_input", "choose_larger_model", "retry"],
    "invalid_plan": ["retry", "edit_request"],
    "cad_unsupported": ["edit_request"],
    "conversion_failed": ["retry", "contact_admin"],
    "budget_exceeded": ["raise_budget"],
    "unknown": ["retry", "contact_admin"],
}
_RETRYABLE = frozenset({"rate_limited", "network_error", "timeout", "credential_changed", "output_truncated", "invalid_plan"})

_TRUNCATED = ("cut off", "token limit", "max_output_tokens", "max_tokens", "finish_reason=length", "incomplete")
_CONTEXT = ("context length", "context_length", "maximum context", "context window", "too many tokens", "prompt is too long",
            "input is too long", "reduce the length", "token count", "exceeds the maximum")
_PROTOCOL = ("response_format", "json_schema", "schema", "not supported", "unsupported", "unknown parameter", "unrecognized",
             "invalid url", "404 page", "<html")


def code_of(code: str | None, message: str | None = None) -> str:
    """Diagnostic code of a provider / application error code (the message refines invalid_request / bad_response)."""
    c = (code or "").strip()
    low = (message or "").lower()
    if c in _PROVIDER:
        return _PROVIDER[c]
    if c in _APP:
        return _APP[c]
    if c in CODES:
        return c
    if c in ("invalid_request", "bad_response"):
        # Older adapters used bad_response for an empty stream. This observes missing data,
        # without assuming the server's response content type or streaming capability.
        if c == "bad_response" and "sent no stream data" in low:
            return "network_error"
        if any(h in low for h in _TRUNCATED):
            return "output_truncated"
        if any(h in low for h in _CONTEXT):
            return "context_overflow"
        if c == "invalid_request" or any(h in low for h in _PROTOCOL):
            return "protocol_mismatch"
        return "unknown"
    return "unknown"


def is_ai_failure(code: str | None, details: dict[str, Any] | None = None) -> bool:
    """True when an operation / run error is an AI or model-configuration failure (diagnosable), not a business refusal."""
    c = (code or "").strip()
    if c == "ai_unavailable":
        return True
    return c in _PROVIDER or c in _APP or c in ("invalid_request", "bad_response") or c in CODES


def stage_of(code: str) -> str:
    if code in RESOLVE_CODES:
        return "resolve"
    if code == "context_overflow":
        return "context"
    if code == "invalid_plan":
        return "validation"
    if code in ("cad_unsupported", "conversion_failed"):
        return "tool"
    if code == "budget_exceeded":
        return "run"
    return "model_call"


def diagnostic(
    code: str | None,
    *,
    message: str | None = None,
    stage: str | None = None,
    purpose: str | None = None,
    snapshot: dict[str, Any] | None = None,
    config_id: str | None = None,
    config_version: int | None = None,
    call_ids: list[str] | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """The diagnostic payload. ``snapshot`` (a frozen model snapshot) fills config id / version and purpose."""
    diag = code_of(code, message)
    snap = snapshot if isinstance(snapshot, dict) else {}
    return {
        "code": diag,
        "stage": stage or stage_of(diag),
        "purpose": purpose or snap.get("purpose"),
        "config_id": config_id or snap.get("config_id"),
        "config_version": config_version if config_version is not None else snap.get("config_version"),
        "call_ids": [str(c) for c in (call_ids or [])][:20],
        "retryable": bool(retryable) if retryable is not None else diag in _RETRYABLE,
        "recovery": list(RECOVERY.get(diag, RECOVERY["unknown"])),
        "provider_code": code or None,
    }


def from_provider_error(exc: ProviderError, *, stage: str | None = None, purpose: str | None = None,
                        snapshot: dict[str, Any] | None = None, call_ids: list[str] | None = None) -> dict[str, Any]:
    code = code_of(exc.code, exc.message)
    return diagnostic(exc.code, message=exc.message, stage=stage or stage_of(code), purpose=purpose, snapshot=snapshot,
                      call_ids=call_ids, retryable=exc.retryable if code not in RESOLVE_CODES else code in _RETRYABLE)
