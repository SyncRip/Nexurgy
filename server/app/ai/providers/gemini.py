"""Gemini adapter (official ``google-genai`` SDK, Gemini Developer API).

Request shape (google-genai 2.28, ``client.models.generate_content``)::

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=<schema in the "gemini" dialect>,
        temperature=...,                       # omitted unless given
        http_options=types.HttpOptions(timeout=<ms>),
    )
    contents = [types.Content(role="user"|"model", parts=[Part.from_bytes(...), Part.from_text(...)])]

If the API rejects the schema (400 mentioning the schema) we retry once in
plain JSON mode with the schema embedded in the system prompt; the reply is
validated by the caller either way. Errors are mapped by
:func:`map_gemini_error`. Docs consulted: see PROVIDER_NOTES.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .base import (
    LIST_MODELS_MAX,
    ConnectionTest,
    ImagePart,
    ListedModel,
    Message,
    ModelConfig,
    ModelProvider,
    ProviderError,
    StructuredResult,
    check_attachments,
    clip,
    map_transport_error,
    parse_json_object,
    redact,
    schema_prompt_suffix,
)
from .schema_dialects import to_dialect

_BLOCK_FINISH = {
    "SAFETY", "RECITATION", "LANGUAGE", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
}

ClientFactory = Callable[[str, ModelConfig], Any]


def _default_client_factory(api_key: str, config: ModelConfig) -> Any:
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(
        base_url=config.base_url or None,
        # No SDK retry (generation reliability J, design §2.1): one request per attempt; the bounded retry runs in
        # ``Tracer.model_call`` (ai/retry.py) so every real request is recorded.
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def _enum_name(v: Any) -> str | None:
    if v is None:
        return None
    return str(getattr(v, "value", v)).split(".")[-1]


def map_gemini_error(exc: BaseException, *, secrets: tuple[str | None, ...] = ()) -> ProviderError:
    """Map google-genai / httpx exceptions to a ProviderError with a clear message."""
    if isinstance(exc, ProviderError):
        return exc
    p = "gemini"
    try:
        from google.genai import errors as gerrors
    except Exception:  # pragma: no cover - SDK import failure
        gerrors = None  # type: ignore[assignment]

    if gerrors is not None and isinstance(exc, gerrors.APIError):
        status_code = int(exc.code or 0)
        status = str(exc.status or "").upper()
        msg = redact(clip(exc.message or str(exc), 400), secrets)
        details = redact(str(exc.details or ""), secrets)
        low = (msg + " " + details).lower()
        label = f"Gemini HTTP {status_code}{' ' + status if status else ''}"
        retry_after = _retry_delay(exc.details)
        if status_code == 401 or status == "UNAUTHENTICATED" or "api_key_invalid" in low or "api key not valid" in low or "api key expired" in low:
            return ProviderError("auth_failed", f"Gemini rejected the API key ({label}): {msg}", provider=p, http_status=status_code)
        if status_code == 403 or status == "PERMISSION_DENIED":
            if "leaked" in low or "api key" in low:
                return ProviderError("auth_failed", f"Gemini rejected the API key ({label}): {msg}", provider=p, http_status=status_code)
            return ProviderError("permission_denied", f"the API key has no access to this model or API ({label}): {msg}", provider=p, http_status=status_code)
        if status_code == 404 or status == "NOT_FOUND":
            return ProviderError("model_not_found", f"model not found or not available to this key ({label}): {msg}", provider=p, http_status=status_code)
        if status_code == 402:
            return ProviderError("quota_exceeded", f"Gemini prepaid credit is depleted ({label}): {msg}", provider=p, http_status=status_code, retryable=False)
        if status_code == 429 or status == "RESOURCE_EXHAUSTED":
            if "perday" in low or "per day" in low or "daily" in low or "billing" in low or "credit" in low:
                return ProviderError("quota_exceeded", f"Gemini quota exhausted ({label}): {msg}", provider=p, http_status=status_code, retryable=False, retry_after_s=retry_after)
            return ProviderError("rate_limited", f"Gemini rate limit reached ({label}): {msg}", provider=p, http_status=status_code, retry_after_s=retry_after)
        if status == "FAILED_PRECONDITION":
            return ProviderError("permission_denied", f"Gemini precondition failed (billing or region not enabled?) ({label}): {msg}", provider=p, http_status=status_code)
        if status_code in (408, 504) or status == "DEADLINE_EXCEEDED":
            return ProviderError("timeout", f"Gemini did not finish in time ({label}): {msg}", provider=p, http_status=status_code)
        if 400 <= status_code < 500:
            return ProviderError("invalid_request", f"Gemini rejected the request ({label}): {msg}", provider=p, http_status=status_code)
        if status_code >= 500:
            return ProviderError("network", f"Gemini service error, try again later ({label}): {msg}", provider=p, http_status=status_code, retryable=True)
        return ProviderError("unknown", f"Gemini API error ({label}): {msg}", provider=p, http_status=status_code or None)

    net = map_transport_error(exc, p)
    if net is not None:
        return net
    if isinstance(exc, ValueError):
        return ProviderError("invalid_request", f"Gemini SDK refused the request: {redact(clip(str(exc), 300), secrets)}", provider=p)
    return ProviderError("unknown", f"unexpected Gemini error {type(exc).__name__}: {redact(clip(str(exc), 300), secrets)}", provider=p)


def _retry_delay(details: Any) -> float | None:
    """Extract google.rpc.RetryInfo retryDelay ("12s") from error details."""
    try:
        err = details.get("error", details) if isinstance(details, dict) else {}
        for d in err.get("details", []) or []:
            delay = d.get("retryDelay") if isinstance(d, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except Exception:
        return None
    return None


class GeminiProvider(ModelProvider):
    """Gemini via ``client.models.generate_content`` with native JSON schema output."""

    provider_name = "gemini"

    def __init__(
        self,
        config: ModelConfig,
        *,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__(config, env=env)
        self._client_factory = client_factory or _default_client_factory
        self._client: Any = None
        self._key: str | None = None
        self._schema_ok = config.capabilities.structured

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.resolve_secret()
            try:
                self._client = self._client_factory(key, self.config)
            except Exception as e:  # SDK constructor problems are config problems
                raise map_gemini_error(e, secrets=(key,)) from None
            self._key = key
        return self._client

    def build_request(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any],
        *,
        images: list[ImagePart] | None,
        temperature: float | None,
        timeout_s: float,
        native_schema: bool,
    ) -> dict[str, Any]:
        """Keyword arguments for ``client.models.generate_content``."""
        from google.genai import types

        contents: list[Any] = []
        last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
        for i, m in enumerate(messages):
            parts = []
            if i == last_user and images:
                parts.extend(types.Part.from_bytes(data=img.data, mime_type=img.mime_type) for img in images)
            parts.append(types.Part.from_text(text=m.content))
            contents.append(types.Content(role="model" if m.role == "assistant" else "user", parts=parts))
        config_kwargs: dict[str, Any] = {
            "system_instruction": system if native_schema else system + schema_prompt_suffix(json_schema),
            "response_mime_type": "application/json",
            "http_options": types.HttpOptions(timeout=int(timeout_s * 1000)),
        }
        if native_schema:
            config_kwargs["response_json_schema"] = to_dialect(json_schema, "gemini")
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        return {"model": self.model_id, "contents": contents, "config": types.GenerateContentConfig(**config_kwargs)}

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
        self.check_live()  # disabled config / cleared or revoked credential: stop before the request
        check_attachments(images, self.provider_name)
        client = self._get_client()
        notes: list[str] = []
        native = self._schema_ok
        started = time.monotonic()
        while True:
            req = self.build_request(
                system, messages, json_schema, images=images, temperature=temperature,
                timeout_s=timeout_s, native_schema=native,
            )
            try:
                resp = client.models.generate_content(**req)
                break
            except Exception as e:  # noqa: BLE001 - mapped below
                err = map_gemini_error(e, secrets=(self._key,))
                if native and err.code == "invalid_request" and "schema" in err.message.lower():
                    native = False
                    self._schema_ok = False
                    notes.append("Gemini rejected the response schema; retried in JSON mode with the schema in the prompt")
                    continue
                raise err from None
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._to_result(resp, latency_ms, native, notes)

    def _to_result(self, resp: Any, latency_ms: int, native: bool, notes: list[str]) -> StructuredResult:
        feedback = getattr(resp, "prompt_feedback", None)
        block = _enum_name(getattr(feedback, "block_reason", None)) if feedback else None
        if block and block != "BLOCKED_REASON_UNSPECIFIED":
            raise ProviderError("bad_response", f"Gemini blocked the prompt ({block})", provider=self.provider_name)
        candidates = getattr(resp, "candidates", None) or []
        finish = _enum_name(getattr(candidates[0], "finish_reason", None)) if candidates else None
        if finish in _BLOCK_FINISH:
            raise ProviderError("bad_response", f"Gemini stopped the response ({finish})", provider=self.provider_name)
        text = resp.text or ""
        if not text.strip():
            raise ProviderError("bad_response", f"Gemini returned no text (finish_reason={finish})", provider=self.provider_name)
        data, parse_error = parse_json_object(text)
        if data is None and finish == "MAX_TOKENS":
            raise ProviderError("bad_response", "Gemini output was cut off at the token limit", provider=self.provider_name)
        usage = getattr(resp, "usage_metadata", None)
        tokens_out = None
        if usage is not None and usage.candidates_token_count is not None:
            tokens_out = usage.candidates_token_count + (usage.thoughts_token_count or 0)
        return StructuredResult(
            data=data,
            text=text,
            parse_error=parse_error,
            provider=self.provider_name,
            model_id=getattr(resp, "model_version", None) or self.model_id,
            request_id=getattr(resp, "response_id", None),
            tokens_in=getattr(usage, "prompt_token_count", None) if usage is not None else None,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            finish_reason=finish,
            structured_mode="json_schema" if native else "prompt_only",
            notes=notes,
        )

    def list_models(self, *, timeout_s: float = 15) -> list[ListedModel]:
        """``models.list`` (free): the base models this key can call with generateContent."""
        client = self._get_client()
        from google.genai import types

        found: dict[str, str | None] = {}
        try:
            config = types.ListModelsConfig(page_size=100, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
            for m in client.models.list(config=config):
                actions = list(getattr(m, "supported_actions", None) or [])
                name = str(getattr(m, "name", None) or "").removeprefix("models/")
                if name and (not actions or "generateContent" in actions):
                    found[name] = getattr(m, "display_name", None)
                if len(found) >= LIST_MODELS_MAX:
                    break
        except Exception as e:  # noqa: BLE001
            raise map_gemini_error(e, secrets=(self._key,)) from None
        return [ListedModel(id=k, name=v) for k, v in sorted(found.items())]

    def test_connection(self, *, timeout_s: float = 20) -> ConnectionTest:
        """``models.get`` (free; checks key and model id) then a tiny structured probe."""
        details: dict[str, Any] = {}
        started = time.monotonic()
        try:
            client = self._get_client()
            from google.genai import types

            info = client.models.get(model=self.model_id, config=types.GetModelConfig(http_options=types.HttpOptions(timeout=int(timeout_s * 1000))))
            details = {
                "display_name": getattr(info, "display_name", None),
                "input_token_limit": getattr(info, "input_token_limit", None),
                "output_token_limit": getattr(info, "output_token_limit", None),
                "supported_actions": list(getattr(info, "supported_actions", None) or []),
            }
        except Exception as e:  # noqa: BLE001
            err = map_gemini_error(e, secrets=(self._key,))
            return ConnectionTest(
                ok=False, provider=self.provider_name, model_id=self.model_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=err.code, error_message=err.message, retryable=err.retryable,
            )
        actions = details.get("supported_actions") or []
        if actions and "generateContent" not in actions:
            return ConnectionTest(
                ok=False, provider=self.provider_name, model_id=self.model_id,
                error_code="unsupported_capability",
                error_message=f"model {self.model_id} does not support generateContent",
                detected={"text": False, "structured": None, "vision": None, "tools": None}, details=details,
            )
        result = self._probe(timeout_s)
        result.details = {**details, **result.details}
        return result
