"""OpenAI adapters (official ``openai`` SDK 3.x). Every model request is streamed (S1).

``OpenAIProvider`` — OpenAI itself, **Responses API**::

    client.responses.create(
        model=..., instructions=system, store=False, stream=True,
        input=[{"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,...", "detail": "high"},
            {"type": "input_file", "filename": "x.pdf", "file_data": "data:application/pdf;base64,..."},
            {"type": "input_text", "text": "..."}]}],
        text={"format": {"type": "json_schema", "name": ..., "schema": <openai_strict>, "strict": True}},
    )

``OpenAICompatibleProvider`` — any server speaking **Chat Completions** at a
configured ``base_url``::

    client.chat.completions.create(
        model=..., messages=[{"role": "system", ...}, ...],
        stream=True, stream_options={"include_usage": True},
        max_completion_tokens=<the model's output budget, ai/limits.py; omitted for an unlisted model>,
        response_format={"type": "json_schema", "json_schema": {"name": ..., "schema": <openai_strict>, "strict": True}},
    )

An explicit output limit uses the configured model budget instead of relying on a
gateway default (PROVIDER_NOTES.md). Both adapters stream replies and concatenate
the received text before parsing and validation.

Two time limits per call: an idle timeout (:data:`STREAM_IDLE_TIMEOUT_S` without
a new chunk once the stream has delivered its first chunk) and the caller's
``timeout_s`` as the cap on the whole call (request, wait for the first chunk,
streaming, mode-ladder retries). The stream is read on a daemon thread so both
limits hold even while the socket is silent or a gateway only sends SSE
keep-alive comments.

The OpenAI Responses adapter walks a mode ladder when the server rejects
structured-output parameters: strict json_schema → non-strict json_schema →
json_object. Compatible servers use strict json_schema unless a deployment explicitly
opts a saved config into portable json_schema; rejection is reported without downgrading. Models
configured without structured output start in json_object mode. The reached
mode is remembered per provider instance. The caller validates every reply
with Pydantic. Errors are mapped by :func:`map_openai_error`. Docs consulted:
see PROVIDER_NOTES.md.
A compatible server that rejects the output-limit parameter retries with max_tokens and then omits it;
that fallback is remembered per provider and preserves the shared model-table output budget.
"""

from __future__ import annotations

import contextvars
import json
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from .. import limits
from .base import (
    LIST_MODELS_MAX,
    ConnectionTest,
    ImagePart,
    ListedModel,
    Message,
    ModelConfig,
    ModelProvider,
    ProviderError,
    StructuredMode,
    StructuredResult,
    check_attachments,
    clip,
    compatible_schema_mode,
    map_transport_error,
    parse_json_object,
    redact,
    safe_schema_name,
    schema_prompt_suffix,
)
from .schema_dialects import SchemaDialectError, to_dialect
from ..timeouts import stream_idle_seconds

ClientFactory = Callable[[str, ModelConfig], Any]

#: A started stream that delivers no new chunk for this long is abandoned with a ``timeout`` error (S1).
#: Before the first chunk only the caller's ``timeout_s`` applies (a model may think a while before answering).
STREAM_IDLE_TIMEOUT_S = 60.0

#: Names a compatible server may take the output limit under, tried in order: the current OpenAI name (required by
#: OpenAI reasoning models, which reject ``max_tokens``), then the legacy one.
_LIMIT_PARAMS = ("max_completion_tokens", "max_tokens")
_QUOTA_HINTS = ("insufficient_quota", "quota", "credit", "billing", "spend", "usage_limit")
_SCHEMA_HINTS = ("schema", "response_format", "text.format", "json_object", "strict")
#: Match a request id embedded in an error message, for example "... (request id: 2026101112345678abcd)".
_REQUEST_ID_IN_TEXT = re.compile(r"(?i)\brequest[ _-]?id\b[\"']?\s*[:=：]\s*[\"']?([A-Za-z0-9][A-Za-z0-9._\-]{5,127})")


def _default_client_factory(api_key: str, config: ModelConfig) -> Any:
    from openai import OpenAI

    # No SDK retry (generation reliability J, design §2.1): the bounded retry runs in ``Tracer.model_call``
    # (ai/retry.py), so every real request is one ai_calls row with its attempt number.
    return OpenAI(api_key=api_key, base_url=config.base_url or None, max_retries=0)


def map_openai_error(
    exc: BaseException, *, provider: str = "openai", secrets: tuple[str | None, ...] = ()
) -> ProviderError:
    """Map openai SDK / httpx2 exceptions to a ProviderError with a clear message
    (and the provider request id, when one is known, on ``err.request_id``)."""
    err = _map_openai_error(exc, provider=provider, secrets=secrets)
    if err is not exc and err.request_id is None:
        rid = request_id_from_error(exc)
        if rid and redact(rid, secrets) == rid:
            err.request_id = rid
    return err


def request_id_from_error(exc: BaseException) -> str | None:
    """Provider request id of a failed request: use the SDK response-header value when available,
    otherwise extract it from structured error fields or message text."""
    rid = getattr(exc, "request_id", None)
    if isinstance(rid, str) and rid:
        return rid[:200]
    texts: list[str] = []
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        texts.append(message)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("request_id", "requestId"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:200]
        texts.append(json.dumps(body, ensure_ascii=False, default=str))
    elif isinstance(body, str):
        texts.append(body)
    for text in texts:
        m = _REQUEST_ID_IN_TEXT.search(text)
        if m:
            return m.group(1).rstrip(".-")[:200]
    return None


def _map_openai_error(
    exc: BaseException, *, provider: str = "openai", secrets: tuple[str | None, ...] = ()
) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    import openai

    name = "OpenAI" if provider == "openai" else "OpenAI-compatible server"
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("timeout", f"{name} did not respond in time", provider=provider)
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("network", f"cannot reach {name}: {redact(clip(exc.message, 200), secrets)}", provider=provider)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        code = (exc.code or "").lower()
        msg = redact(clip(exc.message, 400), secrets)
        low = f"{code} {msg}".lower()
        rid = f", request id {exc.request_id}" if getattr(exc, "request_id", None) else ""
        label = f"HTTP {status}{' ' + code if code else ''}{rid}"
        if status == 401:
            return ProviderError("auth_failed", f"{name} rejected the API key ({label}): {msg}", provider=provider, http_status=status)
        if status == 403:
            return ProviderError("permission_denied", f"{name} denied access (model, project or region) ({label}): {msg}", provider=provider, http_status=status)
        if status == 404 or code == "model_not_found":
            return ProviderError("model_not_found", f"model or endpoint not found; check model_id and base_url ({label}): {msg}", provider=provider, http_status=status)
        if status == 429:
            retry_after = _retry_after(exc)
            if any(h in low for h in _QUOTA_HINTS):
                return ProviderError("quota_exceeded", f"{name} quota or credit exhausted ({label}): {msg}", provider=provider, http_status=status, retryable=False)
            return ProviderError("rate_limited", f"{name} rate limit reached ({label}): {msg}", provider=provider, http_status=status, retry_after_s=retry_after)
        if status == 408:
            return ProviderError("timeout", f"{name} timed out ({label}): {msg}", provider=provider, http_status=status)
        if status in (400, 422):
            if ("image" in low or "file" in low or "pdf" in low) and ("not supported" in low or "only supported" in low or "does not support" in low):
                return ProviderError("unsupported_capability", f"{name} model cannot take this attachment ({label}): {msg}", provider=provider, http_status=status)
            return ProviderError("invalid_request", f"{name} rejected the request ({label}): {msg}", provider=provider, http_status=status)
        if status >= 500:
            return ProviderError("network", f"{name} service error, try again later ({label}): {msg}", provider=provider, http_status=status, retryable=True)
        return ProviderError("unknown", f"{name} API error ({label}): {msg}", provider=provider, http_status=status)
    if isinstance(exc, openai.APIResponseValidationError):
        return ProviderError("bad_response", f"{name} returned data the SDK could not parse", provider=provider)
    net = map_transport_error(exc, provider)
    if net is not None:
        return net
    if isinstance(exc, openai.OpenAIError):
        return ProviderError("unknown", f"{name} SDK error: {redact(clip(str(exc), 300), secrets)}", provider=provider)
    return ProviderError("unknown", f"unexpected {name} error {type(exc).__name__}: {redact(clip(str(exc), 300), secrets)}", provider=provider)


def _retry_after(exc: Any) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except Exception:
        return None


def _is_schema_rejection(err: ProviderError) -> bool:
    return err.code == "invalid_request" and any(h in err.message.lower() for h in _SCHEMA_HINTS)


def _is_temperature_rejection(err: ProviderError) -> bool:
    return err.code == "invalid_request" and "temperature" in err.message.lower()


def _is_stream_options_rejection(err: ProviderError) -> bool:
    return err.code == "invalid_request" and ("stream_options" in err.message.lower() or "include_usage" in err.message.lower())


def _is_limit_rejection(err: ProviderError, param: str) -> bool:
    """A 400 about the output limit sent as ``param``: the parameter is unknown or the value too large (vLLM: "This
    model's maximum context length is N tokens. However, you requested M tokens (… in the completion)")."""
    low = err.message.lower()
    return err.code == "invalid_request" and (param in low or ("maximum context length" in low and "completion" in low))


# --- streaming --------------------------------------------------------------------------------------


class _StreamDeadline(Exception):
    """Raised by :func:`_pump` when the idle timeout or the total cap passes."""

    def __init__(self, kind: Literal["idle", "total"]) -> None:
        super().__init__(kind)
        self.kind = kind


def _close_quietly(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - best effort; the stream is being abandoned
            pass


def _pump(open_stream: Callable[[], Any], *, idle_s: float, deadline: float) -> Iterator[Any]:
    """Yield the opened stream, then each of its chunks.

    Opening (the HTTP request) and reading happen on a daemon thread; this
    generator waits on a queue, so it raises :class:`_StreamDeadline` on time
    even when the read is blocked: ``total`` once ``deadline`` (a
    ``time.monotonic()`` instant) passes, ``idle`` when no chunk arrived for
    ``idle_s`` after the first one. Whatever opening or reading raised is
    re-raised here unchanged. On exit the stream is closed (best effort), which
    releases the connection and stops the server's generation.
    """
    q: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
    stop = threading.Event()
    opened: list[Any] = []

    def read() -> None:
        try:
            stream = open_stream()
            opened.append(stream)
            if stop.is_set():  # the caller gave up while the request was being sent
                return
            q.put(("open", stream))
            for chunk in stream:
                if stop.is_set():
                    break
                q.put(("chunk", chunk))
            q.put(("end", None))
        except BaseException as e:  # noqa: BLE001 - handed to the waiting caller
            q.put(("error", e))
        finally:
            if stop.is_set() and opened:
                _close_quietly(opened[0])

    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(read,), name="partroute-model-stream", daemon=True).start()
    last_chunk: float | None = None
    try:
        while True:
            limit, kind = deadline, "total"
            if last_chunk is not None and last_chunk + idle_s < deadline:
                limit, kind = last_chunk + idle_s, "idle"
            wait = limit - time.monotonic()
            if wait <= 0:
                raise _StreamDeadline(kind)  # type: ignore[arg-type]
            try:
                tag, item = q.get(timeout=wait)
            except queue.Empty:
                continue
            if tag == "error":
                raise item
            if tag == "end":
                return
            if tag == "chunk":
                last_chunk = time.monotonic()
            yield item
    finally:
        stop.set()
        if opened:
            _close_quietly(opened[0])


def _stream_request_id(stream: Any) -> str | None:
    """``x-request-id`` of the streamed HTTP response (``openai.Stream.response``)."""
    try:
        rid = stream.response.headers.get("x-request-id")
    except Exception:  # noqa: BLE001 - fakes / servers without the header
        return None
    return rid[:200] if isinstance(rid, str) and rid else None


@dataclass
class _Reply:
    text: str
    finish: str | None
    tokens_in: int | None
    tokens_out: int | None
    request_id: str | None
    model: str | None
    first_token_ms: int | None = None
    notes: list[str] = field(default_factory=list)


class _Collector:
    """Accumulates one streamed reply; subclasses read the chunk / event shapes."""

    def __init__(self, provider: str, label: str, started: float) -> None:
        self.provider = provider
        self.label = label
        self.started = started
        self.chunks = 0
        self.chars = 0
        self.parts: list[str] = []
        self.refusal: list[str] = []
        self.first_token_at: float | None = None

    def add(self, chunk: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def result(self, request_id: str | None) -> _Reply:  # pragma: no cover - abstract
        raise NotImplementedError

    def _text(self, piece: str | None) -> None:
        if piece:
            if self.first_token_at is None:
                self.first_token_at = time.monotonic()
            self.parts.append(piece)
            self.chars += len(piece)

    @property
    def first_token_ms(self) -> int | None:
        return None if self.first_token_at is None else int((self.first_token_at - self.started) * 1000)

    @property
    def progress(self) -> str:
        return f"{self.chunks} chunks, {self.chars} characters received"

    def broken_class(self, before_output: str) -> str:
        """Failure class of a stream that stopped early (design §2.1): ``stream_broken`` once any text arrived (the
        partial reply is dropped, never stitched to the next attempt), else ``before_output``."""
        return "stream_broken" if self.chars > 0 else before_output

    def unfinished(self, notes: list[str]) -> str:
        """Text of a stream that ended without its terminal chunk / event: kept when it is already a complete
        JSON object, otherwise a retryable error (never a parse error on partial JSON)."""
        text = "".join(self.parts)
        if self.chunks == 0:
            raise ProviderError("network", f"{self.label} stream ended without any reply data; retry",
                                provider=self.provider, retryable=True, failure_class="no_response")
        if parse_json_object(text)[0] is None:
            raise ProviderError("network", f"{self.label} stream ended before the reply was complete ({self.progress}); retry",
                                provider=self.provider, retryable=True, failure_class=self.broken_class("overloaded"))
        notes.append("stream ended without a finish reason; the reply was already complete JSON")
        return text


class _ChatCollector(_Collector):
    """Chat Completions chunks: ``choices[0].delta.content`` pieces, ``finish_reason``, ``usage`` on the last chunk."""

    def __init__(self, provider: str, label: str, started: float) -> None:
        super().__init__(provider, label, started)
        self.finish: str | None = None
        self.usage: Any = None
        self.model: str | None = None
        self.chunk_id: str | None = None

    def add(self, chunk: Any) -> None:
        self.chunks += 1
        self.chunk_id = self.chunk_id or getattr(chunk, "id", None)
        self.model = getattr(chunk, "model", None) or self.model
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = usage
        for choice in getattr(chunk, "choices", None) or []:
            if (getattr(choice, "index", 0) or 0) != 0:
                continue
            delta = getattr(choice, "delta", None)
            self._text(getattr(delta, "content", None))
            refusal = getattr(delta, "refusal", None)
            if refusal:
                self.refusal.append(refusal)
            finish = getattr(choice, "finish_reason", None)
            if finish:
                self.finish = finish

    def result(self, request_id: str | None) -> _Reply:
        if self.refusal:
            raise ProviderError("bad_response", f"model refused: {clip(''.join(self.refusal), 200)}", provider=self.provider)
        if self.finish == "content_filter":
            raise ProviderError("bad_response", "server stopped the response (content_filter)", provider=self.provider)
        notes: list[str] = []
        text = "".join(self.parts) if self.finish else self.unfinished(notes)
        # Reasoning can exhaust the limit before any text. Keep terminal usage and identity
        # for generate_structured's cutoff diagnostic, including a headerless chunk id.
        if not text.strip() and self.finish != "length":
            raise ProviderError("bad_response", f"server returned no text (finish_reason={self.finish})", provider=self.provider)
        return _Reply(
            text=text, finish=self.finish,
            tokens_in=getattr(self.usage, "prompt_tokens", None), tokens_out=getattr(self.usage, "completion_tokens", None),
            request_id=request_id or self.chunk_id, model=self.model, first_token_ms=self.first_token_ms, notes=notes,
        )


#: Responses API ``response.failed`` error codes that are transient (design §2.1) → their failure class.
_RESPONSES_TRANSIENT = {"server_error": "server_error", "rate_limit_exceeded": "rate_limited", "vector_store_timeout": "server_error"}


class _ResponsesCollector(_Collector):
    """Responses API events: ``response.output_text.delta`` pieces; the terminal event (completed / incomplete /
    failed) carries the full response with status, output and usage."""

    def __init__(self, provider: str, label: str, started: float, parse: Callable[..., tuple[Any, ...]]) -> None:
        super().__init__(provider, label, started)
        self.parse = parse
        self.final: Any = None
        self.model: str | None = None
        self.response_id: str | None = None

    def add(self, event: Any) -> None:
        self.chunks += 1
        kind = getattr(event, "type", None)
        if kind == "response.output_text.delta":
            self._text(getattr(event, "delta", None))
        elif kind == "response.refusal.delta":
            piece = getattr(event, "delta", None)
            if piece:
                self.refusal.append(piece)
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            self.final = getattr(event, "response", None)
        elif kind == "response.created":
            resp = getattr(event, "response", None)
            self.model = getattr(resp, "model", None) or self.model
            self.response_id = getattr(resp, "id", None) or self.response_id
        elif kind == "error":
            code = getattr(event, "code", None)
            # zero output so far: an overload / server-side failure before the reply started (design §2.1 overloaded)
            raise ProviderError("network", f"{self.label} reported an error mid-stream ({code or 'no code'}, {self.progress}): "
                                f"{clip(getattr(event, 'message', None) or 'no detail', 300)}", provider=self.provider, retryable=True,
                                failure_class=self.broken_class("overloaded"))

    def result(self, request_id: str | None) -> _Reply:
        if self.final is not None:
            text, finish, tokens_in, tokens_out, rid, model = self.parse(self.final, fallback_text="".join(self.parts))
            return _Reply(text=text, finish=finish, tokens_in=tokens_in, tokens_out=tokens_out, request_id=request_id or rid,
                          model=model or self.model, first_token_ms=self.first_token_ms)
        if self.refusal:
            raise ProviderError("bad_response", f"model refused: {clip(''.join(self.refusal), 200)}", provider=self.provider)
        notes: list[str] = []
        text = self.unfinished(notes)
        return _Reply(text=text, finish=None, tokens_in=None, tokens_out=None, request_id=request_id or self.response_id,
                      model=self.model, first_token_ms=self.first_token_ms, notes=notes)


class _OpenAIBase(ModelProvider):
    """Mode ladder, retries on rejected parameters, streaming with time limits, and the connection probe."""

    modes: tuple[StructuredMode, ...] = ()
    #: Instance-overridable (tests); see :data:`STREAM_IDLE_TIMEOUT_S`.
    stream_idle_timeout_s: float | None = None

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
        start = 0 if config.capabilities.structured else self.modes.index("json_object")
        self._mode_index = start
        # Each instance has its own preference; overlapping CAD/ordinary calls must not change one another.
        self._prompt_schema: contextvars.ContextVar[bool] = contextvars.ContextVar("prompt_schema", default=False)
        # Chat Completions: ask for usage on the last chunk until a server rejects stream_options.
        self._stream_usage = True
        # Chat Completions: the model's output budget (reasoning included) and the name it goes under; a server that
        # rejects a name gets the next one (_LIMIT_PARAMS), then no limit. None: an unlisted model, nothing is sent.
        self._output_limit = limits.output_token_limit(config.model_id, config.capabilities)
        self._limit_param: str | None = _LIMIT_PARAMS[0] if self._output_limit else None
        self._limit_lock = threading.Lock()

    @property
    def _label(self) -> str:
        return "OpenAI" if self.provider_name == "openai" else "OpenAI-compatible server"

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.resolve_secret()
            try:
                self._client = self._client_factory(key, self.config)
            except Exception as e:  # noqa: BLE001
                raise map_openai_error(e, provider=self.provider_name, secrets=(key,)) from None
            self._key = key
        return self._client

    def _open(self, client: Any, kwargs: dict[str, Any]) -> Any:  # pragma: no cover - abstract
        """Send the streaming request; returns the SDK stream (iterable of chunks / events)."""
        raise NotImplementedError

    def _collector(self, started: float) -> _Collector:  # pragma: no cover - abstract
        raise NotImplementedError

    def build_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

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
        check_attachments(images, self.provider_name, allow_pdf=self.provider_name == "openai")
        client = self._get_client()
        notes: list[str] = []
        started = time.monotonic()
        deadline = started + timeout_s  # the caller's timeout_s caps the whole call, ladder retries included
        prompt_schema = self._prompt_schema.get()
        mode_index = self.modes.index("json_object") if prompt_schema else self._mode_index
        while True:
            mode = self.modes[mode_index]
            try:
                kwargs = self.build_request(
                    system, messages, json_schema, schema_name=schema_name, images=images,
                    temperature=temperature, timeout_s=timeout_s, mode=mode,
                )
            except SchemaDialectError as e:
                mode_index = self._advance(notes, f"schema not expressible in {mode} ({e})", mode_index,
                                           remember=not prompt_schema)
                continue
            try:
                reply = self._stream_reply(client, kwargs, deadline=deadline, timeout_s=timeout_s)
                break
            except Exception as e:  # noqa: BLE001
                err = map_openai_error(e, provider=self.provider_name, secrets=(self._key,))
                if temperature is not None and _is_temperature_rejection(err):
                    temperature = None
                    notes.append("model rejected temperature; retried with the default")
                    continue
                if self._stream_usage and _is_stream_options_rejection(err):
                    self._stream_usage = False
                    notes.append("server rejected stream_options; retried without it (token usage may be unknown)")
                    continue
                rejected = next((param for param in _LIMIT_PARAMS if param in kwargs), None)
                if rejected is not None and _is_limit_rejection(err, rejected):
                    with self._limit_lock:
                        # Another in-flight request may already have advanced the shared fallback.
                        if self._limit_param == rejected:
                            following = _LIMIT_PARAMS[_LIMIT_PARAMS.index(rejected) + 1:]
                            self._limit_param = following[0] if following else None
                        next_param = self._limit_param
                    then = f"with {next_param}" if next_param else "without an output limit (the server's default applies)"
                    notes.append(f"server rejected {rejected}; retried {then}")
                    continue
                if _is_schema_rejection(err) and mode_index + 1 < len(self.modes):
                    mode_index = self._advance(notes, f"server rejected {mode}: {clip(err.message, 160)}", mode_index,
                                               remember=not prompt_schema)
                    continue
                raise err from None
        latency_ms = int((time.monotonic() - started) * 1000)
        notes.extend(reply.notes)
        data, parse_error = parse_json_object(reply.text)
        if data is None and reply.finish in {"length", "max_output_tokens"}:
            used = f"{reply.tokens_out} output tokens" if reply.tokens_out is not None else "output tokens not reported"
            sent = next((f"{p}={kwargs[p]}" for p in _LIMIT_PARAMS if p in kwargs), "no output limit sent, the server's default applied")
            raise self._with_request_id(ProviderError("bad_response", f"model output was cut off at the token limit ({used}; {sent})",
                                                      provider=self.provider_name, tokens_in=reply.tokens_in,
                                                      tokens_out=reply.tokens_out), reply.request_id)
        return StructuredResult(
            data=data, text=reply.text, parse_error=parse_error, provider=self.provider_name,
            model_id=reply.model or self.model_id, request_id=reply.request_id, tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out, latency_ms=latency_ms, finish_reason=reply.finish,
            structured_mode=mode, notes=notes, first_token_ms=reply.first_token_ms,
        )

    def _stream_reply(self, client: Any, kwargs: dict[str, Any], *, deadline: float, timeout_s: float) -> _Reply:
        """One streamed request. Errors while opening (HTTP status, connection) propagate unchanged so the
        caller's mode ladder sees them; once the stream is open every failure is a ProviderError carrying the
        response's request id."""
        attempt_started = time.monotonic()
        idle_s = stream_idle_seconds(timeout_s, self.stream_idle_timeout_s, short_idle_s=STREAM_IDLE_TIMEOUT_S)
        events = _pump(lambda: self._open(client, kwargs), idle_s=idle_s, deadline=deadline)
        try:
            try:
                stream = next(events)
            except _StreamDeadline:
                raise ProviderError("timeout", f"{self._label} sent no response within the {timeout_s:g} s time limit",
                                    provider=self.provider_name, failure_class="no_response") from None
            except StopIteration:  # pragma: no cover - _pump always yields the stream first
                raise ProviderError("bad_response", f"{self._label} returned no stream", provider=self.provider_name) from None
            request_id = _stream_request_id(stream)
            col = self._collector(attempt_started)
            try:
                for chunk in events:
                    col.add(chunk)
            except _StreamDeadline as t:
                if t.kind == "idle":
                    msg = (f"{self._label} stream stalled: no new data for {idle_s:g} s "
                           f"({col.progress}); retry")
                else:
                    msg = f"{self._label} reply exceeded the {timeout_s:g} s time limit ({col.progress})"
                raise self._with_request_id(ProviderError("timeout", msg, provider=self.provider_name,
                                                          failure_class=col.broken_class("no_response")), request_id) from None
            except Exception as e:  # noqa: BLE001
                raise self._with_request_id(self._broken(e, col), request_id) from None
            # An ordinary JSON/HTML body can look like an empty stream to the SDK. Preserve gateways
            # that deliver valid SSE under a mislabeled header; never expose server-controlled header text.
            headers = getattr(getattr(stream, "response", None), "headers", {})
            content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if col.chunks == 0 and content_type and content_type != "text/event-stream":
                raise self._with_request_id(ProviderError("unsupported_capability",
                    f"{self._label} returned a non-SSE Content-Type with no stream events for stream=true; "
                    "check the endpoint's streaming support", provider=self.provider_name, failure_class="bad_request"), request_id)
            try:
                return col.result(request_id)
            except ProviderError as e:
                raise self._with_request_id(e, request_id) from None
        finally:
            events.close()

    def _broken(self, exc: Exception, col: _Collector) -> ProviderError:
        """A stream that failed after it opened: a clear, retryable error (never a parse error on partial JSON)."""
        import openai

        if isinstance(exc, ProviderError):
            return exc
        secrets = (self._key,)
        net = map_transport_error(exc, self.provider_name)
        # APITimeoutError subclasses APIConnectionError: check it first.
        if isinstance(exc, openai.APITimeoutError) or (net is not None and net.code == "timeout"):
            return ProviderError("timeout", f"{self._label} stream timed out ({col.progress}); retry", provider=self.provider_name,
                                 failure_class=col.broken_class("no_response"))
        if isinstance(exc, openai.APIConnectionError) or net is not None:
            cause = exc.__cause__ or exc
            return ProviderError("network", f"{self._label} stream was interrupted ({type(cause).__name__}, {col.progress}); retry",
                                 provider=self.provider_name, retryable=True, failure_class=col.broken_class("connection"))
        if isinstance(exc, openai.APIError) and not isinstance(exc, openai.APIStatusError):
            # an SSE error event (gateways relay "overloaded" this way): before any output it is an overload
            err = ProviderError("network", f"{self._label} reported an error mid-stream ({col.progress}): "
                                f"{redact(clip(exc.message, 300), secrets)}", provider=self.provider_name, retryable=True,
                                failure_class=col.broken_class("overloaded"))
            err.request_id = request_id_from_error(exc)
            return err
        return map_openai_error(exc, provider=self.provider_name, secrets=secrets)

    @staticmethod
    def _with_request_id(err: ProviderError, request_id: str | None) -> ProviderError:
        if err.request_id is None and request_id:
            err.request_id = request_id
        return err

    def _advance(self, notes: list[str], why: str, mode_index: int, *, remember: bool) -> int:
        if mode_index + 1 >= len(self.modes):
            raise ProviderError("invalid_request", f"no structured-output mode accepted: {why}", provider=self.provider_name)
        mode_index += 1
        if remember:
            self._mode_index = max(self._mode_index, mode_index)
        notes.append(f"{why}; falling back to {self.modes[mode_index]}")
        return mode_index

    def test_connection(self, *, timeout_s: float = 20) -> ConnectionTest:
        """Streams like every other call (the shared probe goes through :meth:`generate_structured`)."""
        try:
            self._get_client()
        except ProviderError as e:
            return ConnectionTest(
                ok=False, provider=self.provider_name, model_id=self.model_id,
                error_code=e.code, error_message=e.message, retryable=e.retryable,
            )
        return self._probe(timeout_s)

    def list_models(self, *, timeout_s: float = 15) -> list[ListedModel]:
        """``GET {base_url}/models``: what this key may call (a gateway lists the models it routes)."""
        client = self._get_client()
        try:
            ids = {str(m.id) for m in client.models.list(timeout=timeout_s) if getattr(m, "id", None)}
        except Exception as e:  # noqa: BLE001
            raise map_openai_error(e, provider=self.provider_name, secrets=(self._key,)) from None
        return [ListedModel(id=i) for i in sorted(ids)[:LIST_MODELS_MAX]]


class OpenAIProvider(_OpenAIBase):
    """OpenAI Responses API with strict Structured Outputs."""

    provider_name = "openai"
    modes = ("json_schema_strict", "json_schema", "json_object")

    @contextmanager
    def prefer_prompt_schema(self) -> Iterator[None]:
        """Use JSON-object mode for this scope; the full portable schema remains in the instructions.

        CAD union decoding was observed to select sweep even for explicit box requests in strict mode.
        Context-local selection preserves concurrent calls, nested scopes and the remembered fallback mode.
        """
        token = self._prompt_schema.set(True)
        try:
            yield
        finally:
            self._prompt_schema.reset(token)

    def build_request(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any],
        *,
        schema_name: str,
        images: list[ImagePart] | None,
        temperature: float | None,
        timeout_s: float,
        mode: StructuredMode,
    ) -> dict[str, Any]:
        """Keyword arguments for ``client.responses.create``."""
        name = safe_schema_name(schema_name)
        if mode == "json_schema_strict":
            fmt: dict[str, Any] = {"type": "json_schema", "name": name, "schema": to_dialect(json_schema, "openai_strict"), "strict": True}
            instructions = system
        elif mode == "json_schema":
            fmt = {"type": "json_schema", "name": name, "schema": to_dialect(json_schema, "portable"), "strict": False}
            instructions = system
        else:
            fmt = {"type": "json_object"}
            instructions = system + schema_prompt_suffix(to_dialect(json_schema, "portable"))
        last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
        items: list[dict[str, Any]] = []
        if mode == "json_object":
            # Responses gateways may validate JSON mode against input only,
            # excluding the separate instructions field. Keep user text intact.
            items.append({"role": "developer", "content": "Return only a JSON object matching the schema in the instructions."})
        for i, m in enumerate(messages):
            if i == last_user and images:
                content: Any = [_responses_attachment(img) for img in images]
                content.append({"type": "input_text", "text": m.content})
            else:
                content = m.content
            items.append({"role": m.role, "content": content})
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "instructions": instructions,
            "input": items,
            "text": {"format": fmt},
            "store": False,
            "stream": True,
            # Per-read HTTP timeout; the total cap and the idle timeout are enforced around the stream.
            "timeout": timeout_s,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return kwargs

    def _open(self, client: Any, kwargs: dict[str, Any]) -> Any:
        return client.responses.create(**kwargs)

    def _collector(self, started: float) -> _Collector:
        return _ResponsesCollector(self.provider_name, self._label, started, self._parse)

    def _parse(self, resp: Any, *, fallback_text: str = "") -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
        """The final ``Response`` (from the stream's terminal event)."""
        status = getattr(resp, "status", None)
        if status == "failed":
            err = getattr(resp, "error", None)
            code = getattr(err, "code", None)
            message = f"OpenAI response failed ({code or 'no code'}): {clip(getattr(err, 'message', None) or 'no detail', 200)}"
            # J (design §2.1): the error inside the failed response says what failed — a server error or a rate limit is
            # transient (retried; after partial output a broken stream); anything else (invalid prompt / image …) stays
            # a content failure (bad_response → unparsable, not retried)
            if code in _RESPONSES_TRANSIENT:
                partial = bool(fallback_text.strip())
                raise ProviderError("rate_limited" if code == "rate_limit_exceeded" else "network", message, provider=self.provider_name,
                                    retryable=True, failure_class="stream_broken" if partial else _RESPONSES_TRANSIENT[code])
            raise ProviderError("bad_response", message, provider=self.provider_name)
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) == "message":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "type", None) == "refusal":
                        raise ProviderError("bad_response", f"model refused: {clip(getattr(c, 'refusal', ''), 200)}", provider=self.provider_name)
        finish = status
        incomplete = getattr(resp, "incomplete_details", None)
        if status == "incomplete":
            finish = getattr(incomplete, "reason", None) or "incomplete"
            if finish == "content_filter":
                raise ProviderError("bad_response", "OpenAI stopped the response (content_filter)", provider=self.provider_name)
        text = getattr(resp, "output_text", None) or fallback_text or ""
        if not text.strip():
            raise ProviderError("bad_response", f"OpenAI returned no text (status={status})", provider=self.provider_name)
        usage = getattr(resp, "usage", None)
        return (
            text,
            finish,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(resp, "_request_id", None) or getattr(resp, "id", None),
            getattr(resp, "model", None),
        )


def _responses_attachment(img: ImagePart) -> dict[str, Any]:
    if img.is_pdf:
        return {"type": "input_file", "filename": img.name or "drawing.pdf", "file_data": img.data_url()}
    return {"type": "input_image", "image_url": img.data_url(), "detail": "high"}


class OpenAICompatibleProvider(_OpenAIBase):
    """Chat Completions with a deployment-selected schema dialect at a configured base_url."""

    provider_name = "openai_compatible"
    modes = ("json_schema_strict", "json_object", "prompt_only")

    def __init__(self, config: ModelConfig, *, env: Mapping[str, str] | None = None,
                 client_factory: ClientFactory | None = None) -> None:
        self.modes = (compatible_schema_mode(config), "json_object", "prompt_only")
        super().__init__(config, env=env, client_factory=client_factory)

    def _advance(self, notes: list[str], why: str, mode_index: int, *, remember: bool) -> int:
        mode = self.modes[mode_index]
        if mode in {"json_schema_strict", "json_schema"}:
            required = "strict structured output" if mode == "json_schema_strict" else "structured output"
            raise ProviderError("invalid_request", f"{required} is required: {why}",
                                provider=self.provider_name, retryable=False)
        return super()._advance(notes, why, mode_index, remember=remember)

    def build_request(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any],
        *,
        schema_name: str,
        images: list[ImagePart] | None,
        temperature: float | None,
        timeout_s: float,
        mode: StructuredMode,
    ) -> dict[str, Any]:
        """Keyword arguments for ``client.chat.completions.create``."""
        strict = mode == "json_schema_strict"
        schema = to_dialect(json_schema, "openai_strict" if strict else "portable")
        # Some compatible gateways accept response_format but omit its schema when
        # forwarding to Gemini. Keep the selected wire contract and give the model
        # the same schema in its instructions; server validation still decides.
        sys_text = system + schema_prompt_suffix(schema)
        msgs: list[dict[str, Any]] = [{"role": "system", "content": sys_text}]
        last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
        for i, m in enumerate(messages):
            if i == last_user and images:
                content: Any = [{"type": "image_url", "image_url": {"url": img.data_url()}} for img in images]
                content.append({"type": "text", "text": m.content})
            else:
                content = m.content
            msgs.append({"role": m.role, "content": content})
        # timeout: per-read HTTP timeout; the total cap and the idle timeout are enforced around the stream.
        kwargs: dict[str, Any] = {"model": self.model_id, "messages": msgs, "stream": True, "timeout": timeout_s}
        if self._stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        limit_param = self._limit_param
        if limit_param is not None:
            kwargs[limit_param] = self._output_limit
        if mode in {"json_schema_strict", "json_schema"}:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": safe_schema_name(schema_name), "schema": schema, "strict": strict},
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if temperature is not None:
            kwargs["temperature"] = temperature
        return kwargs

    def _open(self, client: Any, kwargs: dict[str, Any]) -> Any:
        return client.chat.completions.create(**kwargs)

    def _collector(self, started: float) -> _Collector:
        return _ChatCollector(self.provider_name, self._label, started)
