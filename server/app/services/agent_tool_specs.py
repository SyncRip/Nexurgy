"""The tool registry's P1 import path. P2 moved the registry to ``app/ai/tool_registry.py`` (the native prompt in
``app/ai`` is generated from it, and ``app/ai`` never imports services); this module only re-exports it."""

from __future__ import annotations

from ..ai.tool_registry import (  # noqa: F401 - re-exports
    END_TOOL_SPECS,
    END_TOOLS,
    PART_TOOLS,
    REGISTRY,
    REVIEW_ONLY_TOOLS,
    TOOL_NAMES,
    TOOL_SPECS,
    check_args,
    specs_for,
)
