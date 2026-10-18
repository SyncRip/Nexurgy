"""Typed modeling seam; only the existing built-in pipeline is enabled.

An engine proposes structured operations and returns their requirements, call
ledger and check outcome. The shared service owns deterministic STEP building,
independent checking and immutable revision saving. No engine may publish a
revision or replace those callbacks with its own acceptance claim.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..cad.ops import ModelSpec
from ..cad.requirements import DefinitionSourceContext, DesignDefinitionContext
from .modeling_agent import AgentOutcome, BuildFeedback, CheckFn, CurrentModel, DrawingInput, run_modeling_agent
from .providers.base import ModelProvider

BuildFn = Callable[[ModelSpec], BuildFeedback | Mapping[str, Any]]


@dataclass(frozen=True)
class ModelingInput:
    """Server-resolved request data, never an engine or credential selector."""

    text: str
    current: CurrentModel | None = None
    answers: list[dict[str, str]] | None = None
    part_name: str | None = None
    material: str | None = None
    context: str | None = None
    data: str | None = None
    constraints: list[str] | None = None
    drawing: DrawingInput | None = None
    policy: Literal["standard", "turbo"] = "standard"
    decide: list[str] | None = None
    source_contexts: tuple[DefinitionSourceContext, ...] = ()
    design_context: DesignDefinitionContext | None = None


class ModelingEngine(Protocol):
    name: str

    def generate(self, provider: ModelProvider, request: ModelingInput, *,
                 build: BuildFn, check: CheckFn | None) -> AgentOutcome: ...


class BuiltinModelingEngine:
    name = "builtin"

    def generate(self, provider: ModelProvider, request: ModelingInput, *,
                 build: BuildFn, check: CheckFn | None) -> AgentOutcome:
        if request.policy != "turbo" and check is None:
            raise ValueError("standard modeling requires the shared independent check")
        return run_modeling_agent(
            provider, request.text, request.current, build, check=check,
            answers=request.answers, part_name=request.part_name, material=request.material,
            context=request.context, data=request.data, constraints=request.constraints,
            drawing=request.drawing, policy=request.policy, decide=request.decide, source_contexts=request.source_contexts,
            design_context=request.design_context,
        )


_BUILTIN = BuiltinModelingEngine()


def get_engine(name: str = "builtin") -> ModelingEngine:
    """No external backend is enabled, discovered or loaded dynamically."""
    if name != _BUILTIN.name:
        raise ValueError(f"CAD engine {name!r} is not enabled")
    return _BUILTIN
