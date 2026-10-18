"""Pure context estimates shared by admission manifests and actual model requests.

These are conservative estimates, never provider usage. The policy and model
table remain the same as the assistant's existing budget contract.
"""
from __future__ import annotations

import math
from typing import Any

from . import limits

SAFETY_MIN = 1024
SAFETY_FRACTION = 0.05
SIMULATED_LIMITS = (48_000, 8_000)
UNKNOWN_LIMITS = (max(i for _, i, _ in limits.MODEL_LIMIT_TABLE), max(o for _, _, o in limits.MODEL_LIMIT_TABLE))


def estimate_tokens(text: str) -> int:
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return math.ceil((len(text) - non_ascii) / 4 + non_ascii / 1.6)


def model_limits(config: Any, *, table: tuple | None = None, simulated: tuple[int, int] = SIMULATED_LIMITS,
                 unknown: tuple[int, int] = UNKNOWN_LIMITS) -> tuple[int, int, str]:
    if config is None or config.model_id == "simulated":
        return *simulated, "builtin_table"
    known = limits.table_limits(config.model_id, table) or unknown
    capabilities = getattr(config, "capabilities", None)
    window = getattr(capabilities, "context_window", None)
    if isinstance(window, int) and window > 0:
        out = getattr(capabilities, "max_output_tokens", None)
        return window, out if isinstance(out, int) and 0 < out < window else min(known[1], window // 4), "capabilities"
    return *known, "builtin_table"


def input_budget(config: Any) -> tuple[int, int, int, int, str]:
    window, output, source = model_limits(config)
    safety = max(SAFETY_MIN, int(window * SAFETY_FRACTION))
    return window, output, safety, max(0, window - output - safety), source
