"""Deterministic stand-in provider for tests and the labelled "simulated" mode.

A :class:`FakeProvider` replays a script. Each script item is one of:

* ``dict`` — returned as the parsed JSON object;
* ``str`` — returned as raw model text (parsed like a real reply, so it can
  be malformed on purpose);
* ``ProviderError`` — raised, to simulate provider failures;
* a callable ``(FakeRequest) -> dict | str | ProviderError`` — evaluated per call.

Passing a single callable instead of a list makes it answer every call.
Every result carries ``simulated=True`` and ``structured_mode="simulated"`` so
callers can label it; simulated output never counts as a real model run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from .base import (
    Capabilities,
    ConnectionTest,
    ImagePart,
    Message,
    ModelConfig,
    ModelProvider,
    ProviderError,
    StructuredResult,
    parse_json_object,
)


@dataclass
class FakeRequest:
    """What the fake received; tests assert on these."""

    system: str
    messages: list[Message]
    json_schema: dict[str, Any]
    schema_name: str
    images: list[ImagePart] = field(default_factory=list)
    temperature: float | None = None
    timeout_s: float = 60

    @property
    def last_user_text(self) -> str:
        return next((m.content for m in reversed(self.messages) if m.role == "user"), "")


ScriptItem = Union[dict, str, ProviderError, Callable[[FakeRequest], Any]]


class FakeProvider(ModelProvider):
    """Scripted provider. ``requests`` records every call in order."""

    provider_name = "fake"

    def __init__(
        self,
        script: Sequence[ScriptItem] | Callable[[FakeRequest], Any],
        *,
        capabilities: Capabilities | None = None,
        model_id: str = "fake-model",
        config: ModelConfig | None = None,
    ) -> None:
        if config is None:
            config = ModelConfig(
                label="Simulated model",
                provider="openai_compatible",
                base_url="http://simulated.invalid/v1",
                model_id=model_id,
                capabilities=capabilities or Capabilities(text=True, vision=True, tools=False, structured=True),
            )
        elif capabilities is not None:
            config = config.model_copy(update={"capabilities": capabilities})
        super().__init__(config)
        self._responder = script if callable(script) else None
        self._script = list(script) if not callable(script) else []
        self.requests: list[FakeRequest] = []

    @property
    def simulated(self) -> bool:
        return True

    def resolve_secret(self) -> str:
        return ""  # the fake never needs a key

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
        req = FakeRequest(system, list(messages), json_schema, schema_name, list(images or []), temperature, timeout_s)
        self.requests.append(req)
        if self._responder is not None:
            item: Any = self._responder
        elif self._script:
            item = self._script.pop(0)
        else:
            raise ProviderError("bad_response", "simulated provider script is exhausted", provider=self.provider_name)
        if callable(item) and not isinstance(item, (dict, str, ProviderError)):
            item = item(req)
        if isinstance(item, ProviderError):
            raise item
        if isinstance(item, dict):
            text = json.dumps(item, ensure_ascii=False)
            data, parse_error = item, None
        elif isinstance(item, str):
            text = item
            data, parse_error = parse_json_object(item)
        else:
            raise TypeError(f"unsupported fake script item {type(item).__name__}")
        prompt_chars = len(system) + sum(len(m.content) for m in messages)
        return StructuredResult(
            data=data,
            text=text,
            parse_error=parse_error,
            provider=self.provider_name,
            model_id=self.model_id,
            request_id=f"sim-{len(self.requests)}",
            tokens_in=prompt_chars // 4,
            tokens_out=len(text) // 4,
            latency_ms=0,
            finish_reason="stop",
            structured_mode="simulated",
            simulated=True,
        )

    def test_connection(self, *, timeout_s: float = 20) -> ConnectionTest:
        caps = self.capabilities
        return ConnectionTest(
            ok=True,
            provider=self.provider_name,
            model_id=self.model_id,
            latency_ms=0,
            detected={"text": caps.text, "structured": caps.structured, "vision": caps.vision, "tools": caps.tools},
            details={"note": "simulated provider; no network call was made"},
            simulated=True,
        )
