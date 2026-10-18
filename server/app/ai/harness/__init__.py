"""Executors of one agent attempt (execution-layer design §6.1). ``get_harness`` picks one by the profile's runtime;
the framework-backed ones are imported lazily so the native path never loads them."""

from __future__ import annotations

from .base import AgentHarness


def get_harness(runtime: str) -> AgentHarness:
    if runtime == "tool_agent":
        from .tool_agent import ToolAgentHarness

        return ToolAgentHarness()
    if runtime == "native":
        from .native import NativeHarness

        return NativeHarness()
    raise ValueError(f"runtime_unavailable: the {runtime} executor is not supported")
