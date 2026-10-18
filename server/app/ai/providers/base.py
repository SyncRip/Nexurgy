"""Provider-neutral types for model adapters.

Every adapter (Gemini, OpenAI, OpenAI-compatible, fake) implements
:class:`ModelProvider`. Adapters only promise "a JSON object came back";
callers (the agents) always validate the object with Pydantic. See
``app/ai/PROVIDER_NOTES.md`` for the per-provider protocol choices.

Secrets: a :class:`ModelConfig` never holds a key. An environment reference
(``secret_ref`` such as ``env:GEMINI_API_KEY``) is resolved at call time by
:func:`resolve_secret`; a direct key (stored encrypted, see ``ai/credentials.py``)
is decrypted by the server's resolver (``services/ai.py``) and bound to the
adapter with :meth:`ModelProvider.bind_secret`. Either way the key lives on the
adapter instance only and is redacted from any provider message that might
echo it. Nothing in this package logs keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import zlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProviderKind = Literal["gemini", "openai", "openai_compatible"]
CapabilityName = Literal["text", "vision", "tools", "structured"]
CAPABILITY_NAMES: tuple[str, ...] = ("text", "vision", "tools", "structured")

ProviderErrorCode = Literal[
    "missing_key",
    "auth_failed",
    "model_not_found",
    "permission_denied",
    "quota_exceeded",
    "rate_limited",
    "invalid_request",
    "unsupported_capability",
    "simulation_forbidden",
    "network",
    "timeout",
    "bad_response",
    "unknown",
    # Credential resolution (W1): stable codes the resolver raises before any provider request.
    "master_key_missing",
    "decrypt_failed",
    "credential_changed",
    "config_disabled",
    "credential_cleared",
    "credential_revoked",
    "model_default_missing",
    "model_snapshot_missing",
    "model_probe_required",
    "model_probe_pending",
    "model_probe_failed",
    "runtime_unavailable",
    # Generation reliability J (design §2.1): a model request was not started because the run / step stopped first.
    "budget_exhausted",
    "context_over_budget",
    "cancelled",
    "lease_lost",
]

_RETRYABLE_BY_DEFAULT = {"rate_limited", "network", "timeout"}

# --- failure classes (generation reliability J, .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.1) ------------
# ``code`` says what the provider / SDK reported; ``failure_class`` says what kind of failure it is and so what to do:
# only the transient classes are retried (bounded, ai/retry.py), the others name the recovery a person or the run takes.

FailureClass = Literal[
    "connection", "rate_limited", "server_error", "overloaded", "stream_broken", "no_response",
    "auth", "permission", "quota", "bad_request",
    # not provider errors: model content, CAD kernel, capability, and the stops of a run / step
    "unparsable", "geometry_error", "unsupported", "cancelled", "budget", "lease_lost",
]
FAILURE_CLASSES: tuple[str, ...] = (
    "connection", "rate_limited", "server_error", "overloaded", "stream_broken", "no_response",
    "auth", "permission", "quota", "bad_request",
    "unparsable", "geometry_error", "unsupported", "cancelled", "budget", "lease_lost",
)
#: Transient: retried automatically (at most ai/retry.MAX_ATTEMPTS attempts, backoff, Retry-After).
RETRYABLE_CLASSES = frozenset({"connection", "rate_limited", "server_error", "overloaded", "stream_broken", "no_response"})
#: Stops: never retried in place; the run continues later (what is done is kept).
STOP_CLASSES = frozenset({"cancelled", "budget", "lease_lost"})
#: The recovery action of each class (design §2.1 table).
RECOVERY_BY_CLASS: dict[str, str] = {
    **{c: "retry" for c in RETRYABLE_CLASSES},
    "auth": "configure_model", "permission": "configure_model", "quota": "configure_model",
    "bad_request": "fix_input",
    "unparsable": "retry_definition", "geometry_error": "retry_definition",
    "unsupported": "change_input",
    **{c: "continue" for c in STOP_CLASSES},
}

_CLASS_BY_CODE: dict[str, str] = {
    "missing_key": "auth", "auth_failed": "auth", "master_key_missing": "auth", "decrypt_failed": "auth",
    "credential_changed": "auth", "credential_cleared": "auth", "credential_revoked": "auth",
    "permission_denied": "permission", "config_disabled": "permission", "model_not_found": "permission",
    "quota_exceeded": "quota", "rate_limited": "rate_limited",
    "invalid_request": "bad_request", "unsupported_capability": "bad_request",
    "network": "connection", "timeout": "no_response",
    "bad_response": "unparsable", "unknown": "bad_request",
    "budget_exhausted": "budget", "cancelled": "cancelled", "lease_lost": "lease_lost",
}


def classify(code: str | None, http_status: int | None = None) -> str:
    """The failure class of a provider error code (and HTTP status when there was one): 5xx / 408 are ``server_error``
    whatever the SDK called them, 429 is ``rate_limited`` unless it is a quota; the rest follows the code. Stream
    failures (``overloaded``, ``stream_broken``, ``no_response`` after the stream opened) are set by the adapters, which
    know whether any output had arrived."""
    if http_status is not None and (http_status >= 500 or http_status == 408) and code not in ("quota_exceeded",):
        return "server_error"
    if http_status == 429 and code != "quota_exceeded":
        return "rate_limited"
    return _CLASS_BY_CODE.get(code or "", "bad_request")

# Inline attachments travel base64-encoded inside the request body. 20 MB of
# raw bytes is a conservative ceiling that every supported API accepts
# (Gemini PDFs: 50 MB, OpenAI files: 50 MB per request).
MAX_INLINE_BYTES = 20 * 1024 * 1024

IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
PDF_MIME_TYPE = "application/pdf"
ATTACHMENT_MIME_TYPES = IMAGE_MIME_TYPES | {PDF_MIME_TYPE}


class ProviderError(Exception):
    """A provider (not model-content) failure, mapped to a stable code.

    ``partial`` is set by agents to the outcome recorded so far (steps and
    ai_calls records) so callers can persist them before retrying.

    ``failure_class`` (design §2.1): what kind of failure this is, derived from the code and HTTP status unless the
    adapter knows better (a stream that broke after partial output is ``stream_broken``, one that reported an error
    before any output ``overloaded``). ``attempts`` is set by the tracer when it gives up: how many requests the
    logical call made.
    """

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        provider: str | None = None,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        failure_class: FailureClass | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code: ProviderErrorCode = code
        self.message = message
        self.retryable = code in _RETRYABLE_BY_DEFAULT if retryable is None else retryable
        self.provider = provider
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.failure_class: str = failure_class or classify(code, http_status)
        self.partial: Any = None
        # Provider request id when the provider reported one (kept in the call ledger, never a secret).
        self.request_id: str | None = None
        self.attempts: int | None = None
        # A terminal provider failure may still report billed usage (for example an output cutoff).
        # Keep each reported dimension for the trace; missing counts remain unknown.
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out

    @property
    def recovery(self) -> str:
        return RECOVERY_BY_CLASS.get(self.failure_class, "retry")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable,
                               "failure_class": self.failure_class}
        if self.provider:
            out["provider"] = self.provider
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.attempts is not None:
            out["attempts"] = self.attempts
        return out


class Capabilities(BaseModel):
    """What a configured model can do. Declared by the admin, refined by tests."""

    model_config = ConfigDict(extra="forbid")

    text: bool = True
    vision: bool = False
    tools: bool = False
    structured: bool = True

    def missing(self, required: set[str] | Sequence[str]) -> list[str]:
        return [c for c in CAPABILITY_NAMES if c in set(required) and not getattr(self, c)]


class CredentialState(BaseModel):
    """Where a config's key comes from and whether it is usable (never the key itself).

    ``source`` env: ``ModelConfig.secret_ref`` names the server variable; direct: an encrypted
    key stored by the server (``version`` = its credential version, ``key_version`` = the
    master key that encrypted it). ``status`` cleared / revoked / none: no usable key."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["env", "direct"] = "env"
    version: int | None = None
    status: Literal["active", "cleared", "revoked", "none"] = "active"
    key_version: str | None = None


class ModelConfig(BaseModel):
    """Mirror of a ``model_configs`` row (design.md §4); the key never lives here."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str | None = None
    label: str
    provider: ProviderKind
    model_id: str = Field(min_length=1)
    base_url: str | None = None
    secret_ref: str | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)
    is_default: bool = False
    default_for: list[str] = Field(default_factory=list)
    probe: dict[str, Any] | None = None
    enabled: bool = True
    # Bumped on every connection-relevant change (provider, model, base URL, capabilities, credential).
    config_version: int | None = None
    # None: a plain environment reference (``secret_ref``), as before W1.
    credential: CredentialState | None = None

    @model_validator(mode="after")
    def _check(self) -> "ModelConfig":
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible configs need a base_url (e.g. https://host/v1)")
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must start with http:// or https://")
        return self


class Message(BaseModel):
    """One conversation turn. System instructions are passed separately."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class ImagePart(BaseModel):
    """An inline attachment: PNG / JPEG / WEBP image or a PDF."""

    model_config = ConfigDict(extra="forbid")

    mime_type: str
    data: bytes = Field(repr=False)
    name: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ImagePart":
        if self.mime_type not in ATTACHMENT_MIME_TYPES:
            raise ValueError(f"unsupported attachment type {self.mime_type}; use PNG, JPEG, WEBP or PDF")
        if not self.data:
            raise ValueError("attachment is empty")
        return self

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str | None = None, name: str | None = None) -> "ImagePart":
        """Build a part, sniffing the type from magic bytes when it is missing or generic."""
        sniffed = sniff_mime(data)
        if sniffed and (not mime_type or mime_type in {"application/octet-stream", "binary/octet-stream"}):
            mime_type = sniffed
        return cls(mime_type=mime_type or "application/octet-stream", data=data, name=name)

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == PDF_MIME_TYPE

    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"

    def summary(self) -> dict[str, Any]:
        """Metadata safe to store in call records (never the bytes)."""
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return PDF_MIME_TYPE
    return None


StructuredMode = Literal["json_schema_strict", "json_schema", "json_object", "prompt_only", "simulated"]


class StructuredResult(BaseModel):
    """Result of one structured-generation call.

    ``data`` is the parsed JSON object, or ``None`` with ``parse_error`` set
    when the model returned malformed JSON (a model mistake the agents repair,
    not a provider failure).
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    data: dict[str, Any] | None
    text: str
    parse_error: str | None = None
    provider: str
    model_id: str
    request_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    finish_reason: str | None = None
    structured_mode: StructuredMode
    notes: list[str] = Field(default_factory=list)
    simulated: bool = False
    #: Streaming adapters: milliseconds from sending the request to the first text piece (None when unknown).
    first_token_ms: int | None = None


class ConnectionTest(BaseModel):
    """Outcome of :meth:`ModelProvider.test_connection`.

    ``detected`` maps capability names to True / False, or None when the test
    could not determine it (e.g. tool calling is never probed).
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    ok: bool
    provider: str
    model_id: str
    latency_ms: int | None = None
    error_code: ProviderErrorCode | None = None
    error_message: str | None = None
    retryable: bool | None = None
    detected: dict[str, bool | None] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = False


class ListedModel(BaseModel):
    """One model an endpoint offers to a key (:meth:`ModelProvider.list_models`); ``id`` is a model_id."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None


#: At most this many listed models are returned (gateways can route hundreds).
LIST_MODELS_MAX = 500


def resolve_secret(secret_ref: str | None, *, env: Mapping[str, str] | None = None) -> str:
    """Resolve ``env:NAME`` or ``file:/path`` to the key. Never logs or echoes it.

    ``file:`` supports secrets mounted as files (Cloud Run + Secret Manager).
    Raises ProviderError(missing_key) with a message that names the reference,
    never the value.
    """
    if not secret_ref:
        raise ProviderError("missing_key", "this model configuration has no secret_ref (expected env:NAME)")
    scheme, _, target = secret_ref.partition(":")
    if scheme == "env" and target:
        value = (env if env is not None else os.environ).get(target, "")
        if not value.strip():
            raise ProviderError("missing_key", f"environment variable {target} is not set on the server")
        return value.strip()
    if scheme == "file" and target:
        path = Path(target)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise ProviderError("missing_key", f"secret file {target} is not readable on the server") from None
        if not value:
            raise ProviderError("missing_key", f"secret file {target} is empty")
        return value
    raise ProviderError("missing_key", "secret_ref must look like env:NAME or file:/path")


#: Shortest piece of a configured key that is removed wherever it appears (echoed prefixes, clipped keys).
MIN_KEY_FRAGMENT = 8
#: Key-shaped tokens of the major providers / gateways, also masked echoes such as ``sk-proj-ab…wxyz``.
_KEY_SHAPED = re.compile(r"\bsk[-_][A-Za-z0-9_\-*.…]{6,}|\bAIza[0-9A-Za-z_\-]{8,}|\b(?:gsk|xai|ghp|hf)[-_][A-Za-z0-9]{8,}")
#: "Incorrect API key provided: <token>" and similar echoes of whatever key a gateway received.
_KEY_ECHO = re.compile(r"(?i)(\bapi[\s_-]?key\b[^:=\n]{0,30}[:=]\s*)[^\s,;'\"]{6,}")


def _key_fragments(text: str, secret: str) -> list[tuple[int, int]]:
    """Spans of ``text`` that are a piece (>= MIN_KEY_FRAGMENT chars) of ``secret``, longest match from each start."""
    n = MIN_KEY_FRAGMENT
    if len(secret) < n:
        return []
    grams = {secret[i:i + n] for i in range(len(secret) - n + 1)}
    spans: list[tuple[int, int]] = []
    i = 0
    while i <= len(text) - n:
        if text[i:i + n] in grams:
            j = i + n
            while j < len(text) and text[i:j + 1] in secret:
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def redact(text: str | None, secrets: Sequence[str | None]) -> str:
    """Remove any secret value, any piece of one (>= 8 chars: echoed prefixes, clipped keys) and obvious
    key-shaped tokens from text."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
            spans = _key_fragments(out, s)
            for a, b in reversed(spans):
                out = out[:a] + "***" + out[b:]
    # API keys of the major providers, in case a proxy echoes a different key; bearer tokens of gateways.
    out = _KEY_SHAPED.sub("***", out)
    out = _KEY_ECHO.sub(r"\1***", out)
    out = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{12,}=*", r"\1***", out)
    return out


def clip(text: str | None, limit: int = 500) -> str:
    s = text or ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON object from model text; tolerate code fences and stray prose."""
    s = (text or "").strip()
    if not s:
        return None, "empty response text"
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError as first:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            return None, f"response is not JSON: {first.msg} at char {first.pos}"
        try:
            data = json.loads(s[start : end + 1])
        except json.JSONDecodeError as e:
            return None, f"response is not valid JSON: {e.msg} at char {e.pos}"
    if not isinstance(data, dict):
        return None, f"expected a JSON object, got {type(data).__name__}"
    return data, None


def map_transport_error(exc: BaseException, provider: str) -> ProviderError | None:
    """Classify httpx / httpx2 / socket transport errors (google-genai uses httpx,
    openai 3.x uses httpx2). Returns None when ``exc`` is not a transport error."""
    for name in ("httpx", "httpx2"):
        try:
            mod = __import__(name)
        except ImportError:  # pragma: no cover
            continue
        if isinstance(exc, mod.TimeoutException):
            return ProviderError("timeout", "the model provider did not respond in time", provider=provider)
        if isinstance(exc, mod.TransportError):
            return ProviderError("network", f"cannot reach the model provider ({type(exc).__name__})", provider=provider)
    if isinstance(exc, TimeoutError):
        return ProviderError("timeout", "the model provider did not respond in time", provider=provider)
    if isinstance(exc, ConnectionError):
        return ProviderError("network", f"cannot reach the model provider ({type(exc).__name__})", provider=provider)
    return None


def check_attachments(images: Sequence[ImagePart] | None, provider: str, *, allow_pdf: bool = True) -> None:
    """Enforce the inline-size limit and PDF support before sending anything."""
    if not images:
        return
    total = sum(len(i.data) for i in images)
    if total > MAX_INLINE_BYTES:
        raise ProviderError(
            "invalid_request",
            f"attachments total {total / 1_048_576:.1f} MB, above the {MAX_INLINE_BYTES // 1_048_576} MB inline limit",
            provider=provider,
        )
    if not allow_pdf and any(i.is_pdf for i in images):
        raise ProviderError(
            "unsupported_capability",
            "this provider endpoint does not accept PDF input; upload page images (PNG/JPEG) instead",
            provider=provider,
        )


def schema_prompt_suffix(json_schema: dict[str, Any]) -> str:
    """Instructions appended when the schema is carried in the prompt instead of the native format."""
    return (
        "\n\nRespond with a single JSON object only (no prose, no code fences). "
        "It must validate against this JSON Schema:\n"
        + json.dumps(json_schema, separators=(",", ":"), ensure_ascii=False)
    )


def compatible_schema_mode(config: ModelConfig) -> StructuredMode:
    """Select a deployment-approved schema dialect for one saved compatible config."""
    from app.config import get_settings

    if config.id and config.id in get_settings().ai_compatible_non_strict_config_ids:
        return "json_schema"
    return "json_schema_strict"


def schema_budget_text(json_schema: dict[str, Any], config: ModelConfig | None) -> str:
    """Schema text counted by context budgets, including compatible-server prompt guidance.

    Compatible structured requests carry their selected dialect both in response_format and
    in the system prompt. Count both copies even when a gateway ignores the native one.
    JSON mode carries only the portable schema in the prompt. Other providers retain
    their existing conservative estimate from the source schema.
    """
    if config is None or config.provider != "openai_compatible":
        return json.dumps(json_schema, ensure_ascii=False)
    from .schema_dialects import SchemaDialectError, to_dialect

    structured = config.capabilities.structured
    try:
        strict = structured and compatible_schema_mode(config) == "json_schema_strict"
        schema = to_dialect(json_schema, "openai_strict" if strict else "portable")
    except SchemaDialectError:
        # Budgeting also serves simulated adapters. Keep this an estimate; the real
        # provider still reports its own unsupported-schema error without downgrading.
        schema = json_schema
    native = json.dumps(schema, ensure_ascii=False) if structured else ""
    return native + schema_prompt_suffix(schema)


def safe_schema_name(name: str) -> str:
    """OpenAI requires ^[A-Za-z0-9_-]{1,64}$ for schema names; use it everywhere."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name or "result")[:64]
    return cleaned or "result"


def probe_png(rgb: tuple[int, int, int] = (220, 20, 20), size: int = 16) -> bytes:
    """A tiny solid-colour PNG used by connection tests to probe vision support."""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "color": {"type": "string", "enum": ["red", "green", "blue", "other"]},
    },
    "required": ["ok", "color"],
    "additionalProperties": False,
}
PROBE_SYSTEM = "You are a connectivity probe. Answer with the requested JSON only."
PROBE_TEXT = 'Return {"ok": true, "color": "other"}.'
PROBE_VISION_TEXT = 'Return {"ok": true, "color": <dominant colour of the attached image: red, green, blue or other>}.'


class ModelProvider(ABC):
    """Adapter interface (design.md §8)."""

    provider_name: ClassVar[str] = "base"

    def __init__(self, config: ModelConfig, *, env: Mapping[str, str] | None = None) -> None:
        self.config = config
        self._env = env
        self._bound_secret: str | None = None
        self._guard: Callable[[], None] | None = None

    @property
    def model_id(self) -> str:
        return self.config.model_id

    @property
    def config_id(self) -> str | None:
        return self.config.id

    @property
    def capabilities(self) -> Capabilities:
        return self.config.capabilities

    @property
    def simulated(self) -> bool:
        return False

    def resolve_secret(self) -> str:
        if self._bound_secret is not None:
            return self._bound_secret
        return resolve_secret(self.config.secret_ref, env=self._env)

    def bind_secret(self, secret: str) -> None:
        """Use this key instead of resolving ``secret_ref`` (the server resolver's direct / frozen keys)."""
        self._bound_secret = secret

    def bind_guard(self, guard: Callable[[], None]) -> None:
        """``guard()`` runs before every model request and raises ProviderError when the configuration
        was disabled or its credential cleared / revoked meanwhile (live restrictions, W1)."""
        self._guard = guard

    def check_live(self) -> None:
        if self._guard is not None:
            self._guard()

    @contextmanager
    def prefer_prompt_schema(self) -> Iterator[None]:
        """Scope a schema-transport compatibility preference without changing model configuration.

        Adapters opt in when native structured decoding is unsuitable for a caller's schema. The default
        keeps their existing protocol; every caller still validates the reply with the original schema.
        """
        yield

    @abstractmethod
    def generate_structured(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any],
        *,
        schema_name: str,
        images: list[ImagePart] | None = None,
        temperature: float | None = None,
        timeout_s: float = 60,
    ) -> StructuredResult:
        """Ask for one JSON object shaped by ``json_schema``.

        Images (or PDFs) are attached to the last user message. Raises
        ProviderError for provider failures; returns ``data=None`` with
        ``parse_error`` when the model's text is not a JSON object.
        """

    @abstractmethod
    def test_connection(self, *, timeout_s: float = 20) -> ConnectionTest:
        """Cheap live check: auth, model id, structured output, optional vision probe."""

    def list_models(self, *, timeout_s: float = 15) -> list[ListedModel]:
        """The models the endpoint offers to this key, sorted by id (a listing call, no generation).
        Raises ProviderError; providers that cannot list say so with ``unsupported_capability``."""
        raise ProviderError("unsupported_capability", f"{self.provider_name} cannot list its models",
                            provider=self.provider_name, retryable=False)

    def _probe(self, timeout_s: float) -> ConnectionTest:
        """Shared connection probe built on :meth:`generate_structured`."""
        detected: dict[str, bool | None] = {"text": None, "structured": None, "vision": None, "tools": None}
        details: dict[str, Any] = {}
        want_vision = self.capabilities.vision
        try:
            if want_vision:
                try:
                    res = self.generate_structured(
                        PROBE_SYSTEM,
                        [Message(role="user", content=PROBE_VISION_TEXT)],
                        PROBE_SCHEMA,
                        schema_name="connection_probe",
                        images=[ImagePart(mime_type="image/png", data=probe_png(), name="probe.png")],
                        timeout_s=timeout_s,
                    )
                    detected["vision"] = bool(res.data and res.data.get("color") == "red")
                except ProviderError as e:
                    if e.code not in {"invalid_request", "unsupported_capability"}:
                        raise
                    detected["vision"] = False
                    details["vision_error"] = e.message
                    res = None
                if res is None:
                    res = self.generate_structured(
                        PROBE_SYSTEM, [Message(role="user", content=PROBE_TEXT)], PROBE_SCHEMA,
                        schema_name="connection_probe", timeout_s=timeout_s,
                    )
            else:
                res = self.generate_structured(
                    PROBE_SYSTEM, [Message(role="user", content=PROBE_TEXT)], PROBE_SCHEMA,
                    schema_name="connection_probe", timeout_s=timeout_s,
                )
        except ProviderError as e:
            return ConnectionTest(
                ok=False, provider=self.provider_name, model_id=self.model_id,
                error_code=e.code, error_message=e.message, retryable=e.retryable,
                detected=detected, details=details, simulated=self.simulated,
            )
        detected["text"] = True
        detected["structured"] = res.data is not None and res.structured_mode in {"json_schema_strict", "json_schema", "simulated"}
        details.update({"structured_mode": res.structured_mode, "reported_model": res.model_id, "notes": res.notes})
        ok = res.data is not None and res.data.get("ok") is True
        return ConnectionTest(
            ok=ok,
            provider=self.provider_name,
            model_id=self.model_id,
            latency_ms=res.latency_ms,
            error_code=None if ok else "bad_response",
            error_message=None if ok else f"probe reply was not the expected JSON: {clip(res.parse_error or res.text, 200)}",
            retryable=None if ok else False,
            detected=detected,
            details=details,
            simulated=self.simulated,
        )
