"""Build providers from model configs; capability and readiness checks.

Nothing here touches the network or requires a key at import or build time:
a missing key surfaces as ProviderError(missing_key) on first use, so one
unavailable provider never blocks non-AI features. ``config_status`` reports
readiness without network (for /system/capabilities and the settings page);
``ModelProvider.test_connection`` does the live check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .providers.base import CAPABILITY_NAMES, ModelConfig, ModelProvider, ProviderError, resolve_secret
from .providers.fake import FakeProvider

Purpose = Literal["modeling", "intake", "planning", "agent_execute", "agent_review", "assistant"]

#: Capabilities each AI task needs. Structured output is preferred but not
#: required: adapters fall back to JSON mode and the agents validate anyway.
#: planning / agent_execute / agent_review: the collaboration planner, executor and reviewer agents.
#: assistant: the read-only Q&A agent of the AI workspace (ai/assistant_agent.py).
REQUIRED_CAPABILITIES: dict[str, set[str]] = {
    "modeling": {"text"},
    "intake": {"text", "vision"},
    "planning": {"text"},
    "agent_execute": {"text"},
    "agent_review": {"text"},
    "assistant": {"text"},
}


def build_provider(config: ModelConfig, *, env: Mapping[str, str] | None = None, client_factory: Any = None) -> ModelProvider:
    """Adapter for a config. ``client_factory(api_key, config)`` overrides the SDK client (tests)."""
    if config.provider == "gemini":
        from .providers.gemini import GeminiProvider

        return GeminiProvider(config, env=env, client_factory=client_factory)
    if config.provider == "openai":
        from .providers.openai_provider import OpenAIProvider

        return OpenAIProvider(config, env=env, client_factory=client_factory)
    if config.provider == "openai_compatible":
        from .providers.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(config, env=env, client_factory=client_factory)
    raise ProviderError("invalid_request", f"unknown provider {config.provider!r}")  # pragma: no cover


def build_simulated_provider(purpose: Purpose, config: ModelConfig | None = None) -> FakeProvider:
    """Explicitly labelled simulated provider (results carry simulated=True)."""
    from .simulated import simulated_provider

    return simulated_provider(purpose, config)


def check_capabilities(config: ModelConfig, required: Iterable[str]) -> list[str]:
    """Names of required capabilities the config does not declare."""
    req = set(required)
    unknown = req - set(CAPABILITY_NAMES)
    if unknown:
        raise ValueError(f"unknown capabilities: {sorted(unknown)}")
    return config.capabilities.missing(req)


def require_capabilities(config: ModelConfig, required: Iterable[str]) -> None:
    missing = check_capabilities(config, required)
    if missing:
        raise ProviderError(
            "unsupported_capability",
            f"model config '{config.label}' ({config.model_id}) lacks: {', '.join(missing)}",
            provider=config.provider, retryable=False,
        )


class ConfigStatus(BaseModel):
    """Offline readiness of one config (no network, never includes the key).

    ``issue_codes`` are the stable codes behind ``issues`` (config_disabled, missing_key,
    master_key_missing, decrypt_failed, credential_cleared, credential_revoked, unsupported_capability)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    config_id: str | None
    label: str
    provider: str
    model_id: str
    enabled: bool
    key_present: bool
    missing_capabilities: list[str] = Field(default_factory=list)
    ready: bool
    issues: list[str] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)


def credential_issue(config: ModelConfig, *, env: Mapping[str, str] | None = None) -> ProviderError | None:
    """Why the config's credential cannot be used right now (no network, no decryption), or None.

    env: the referenced variable must be set; direct: an active credential and the master key that
    encrypted it must be configured on this server."""
    cred = config.credential
    if cred is not None and cred.status in ("cleared", "revoked"):
        return ProviderError(f"credential_{cred.status}", f"the key of '{config.label}' was {cred.status}; enter a new key",  # type: ignore[arg-type]
                             retryable=False)
    if cred is None or cred.source == "env":
        try:
            resolve_secret(config.secret_ref, env=env)
        except ProviderError as e:
            return e
        return None
    if cred.status != "active" or cred.version is None:
        return ProviderError("missing_key", f"'{config.label}' has no key; enter one or name an environment variable", retryable=False)
    from .credentials import KEYS_ENV, keyring

    ring = keyring()
    if ring.error or ring.current is None:
        return ProviderError("master_key_missing", f"no usable credential master key on the server ({KEYS_ENV}); the stored key of "
                             f"'{config.label}' cannot be used", retryable=False)
    if cred.key_version not in ring.keys:
        return ProviderError("decrypt_failed", f"the stored key of '{config.label}' was encrypted with master key {cred.key_version}, "
                             f"which is not configured on this server ({KEYS_ENV})", retryable=False)
    return None


def config_status(config: ModelConfig, *, purpose: Purpose | None = None, env: Mapping[str, str] | None = None) -> ConfigStatus:
    issues: list[str] = []
    codes: list[str] = []
    if not config.enabled:
        issues.append("disabled")
        codes.append("config_disabled")
    problem = credential_issue(config, env=env)
    key_present = problem is None
    if problem is not None:
        issues.append(problem.message)
        codes.append(problem.code)
    probe = config.probe
    if probe is not None:
        state = probe.get("status")
        if probe.get("stale") or state in ("not_tested", "simulated"):
            codes.append("model_probe_required")
            issues.append("run detection for this configuration version")
        elif state in ("queued", "running"):
            codes.append("model_probe_pending")
            issues.append("model detection is in progress")
        elif state != "passed":
            codes.append("model_probe_failed")
            issues.append("model detection failed; retry detection")
        elif purpose == "assistant" and not config.capabilities.tools:
            codes.append("unsupported_capability")
            issues.append("the assistant tool runtime is not available for this model")
    missing = check_capabilities(config, REQUIRED_CAPABILITIES[purpose]) if purpose else []
    if missing:
        issues.append(f"lacks {', '.join(missing)} for {purpose}")
        codes.append("unsupported_capability")
    return ConfigStatus(
        config_id=config.id, label=config.label, provider=config.provider, model_id=config.model_id,
        enabled=config.enabled, key_present=key_present, missing_capabilities=missing,
        ready=not issues, issues=issues, issue_codes=codes,
    )


def select_config(
    configs: Iterable[ModelConfig],
    *,
    purpose: Purpose,
    preferred_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelConfig:
    """Authorized explicit override → purpose assignment → global default; never arbitrary fallback."""
    items = list(configs)
    chosen = (next((c for c in items if c.id == preferred_id), None) if preferred_id is not None
              else next((c for c in items if purpose in c.default_for), None)
              or next((c for c in items if c.is_default), None))
    if chosen is None:
        code = "invalid_request" if preferred_id is not None else "model_default_missing"
        raise ProviderError(code, "configure a model default for this purpose", retryable=False)
    status = config_status(chosen, purpose=purpose, env=env)
    if not status.ready:
        raise ProviderError(status.issue_codes[0] if status.issue_codes else "missing_key",
                            f"model config '{chosen.label}' is not usable for {purpose}: {'; '.join(status.issues)}",
                            provider=chosen.provider, retryable=False)
    return chosen
