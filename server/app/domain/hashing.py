"""Canonical numbers, canonical JSON and the ``inputs_hash`` of plans and quotes.

``inputs_hash = sha256(canonical_json({"summary": canonical_summary(summary),
                                       "params": params,
                                       "rules_version": rules_version}))``

Canonicalisation rules (design.md §3):

* object keys sorted, no whitespace, UTF-8 (``ensure_ascii=False``);
* floats rounded to 1e-6 (``FLOAT_DECIMALS``) so float noise below that cannot
  flip staleness; integral results are written as integers (20.0 -> 20) so
  ``20`` and ``20.0`` hash the same; ``-0.0`` -> ``0``;
* non-finite floats become the strings ``"nan"``, ``"inf"``, ``"-inf"`` (never crash);
* tuples -> lists, dates -> ISO strings, pydantic models -> ``model_dump(mode="json")``;
* in the summary, order-free collections are put in a canonical order:
  identical hole entries are merged (counts summed) and sorted, tolerance notes
  and unsupported items are sorted. Planning consumes the same canonical holes,
  so "same hash" implies "same plan".

Also hosts the half-up rounding / number formatting helpers shared by planning
and costing (money must not use Python's banker's rounding).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel

from .rates import PLAN_RULES_VERSION, QUOTE_RULES_VERSION
from .schemas import FeatureSummary, HoleFeature

FLOAT_DECIMALS = 6

# ----------------------------------------------------------------------------- numbers


def norm_float(x: float) -> float:
    """Round to ``FLOAT_DECIMALS`` (1e-6) and turn -0.0 into 0.0. Non-finite values pass through."""
    if not math.isfinite(x):
        return x
    r = round(float(x), FLOAT_DECIMALS)
    return 0.0 if r == 0 else r


def dec(x: float | int | Decimal) -> Decimal:
    """Exact decimal of a number's shortest repr (``0.1`` -> ``Decimal('0.1')``, not the binary expansion)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(int(x))


def round_half_up(x: float | int | Decimal, ndigits: int = 0) -> float:
    """Commercial rounding (0.5 -> 1) to ``ndigits`` decimals, returned as float."""
    return float(dec(x).quantize(Decimal(1).scaleb(-ndigits), rounding=ROUND_HALF_UP))


def to_fen(x: float | int | Decimal) -> int:
    """Round an amount in fen to an integer fen, half-up."""
    return int(dec(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_num(x: float | int | Decimal, ndigits: int = 2) -> str:
    """Fixed-decimal text with half-up rounding, consistent with :func:`round_half_up`."""
    return str(dec(x).quantize(Decimal(1).scaleb(-ndigits), rounding=ROUND_HALF_UP))


def fmt_yuan(fen: int) -> str:
    """Integer fen as yuan text, e.g. 199584 -> '¥1,995.84'."""
    sign = "-" if fen < 0 else ""
    return f"{sign}¥{abs(fen) // 100:,}.{abs(fen) % 100:02d}"


# ----------------------------------------------------------------------------- canonical JSON


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        r = norm_float(value)
        return int(r) if r.is_integer() else r
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot canonicalise value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON text: sorted keys, compact separators, normalised numbers."""
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(value: Any) -> str:
    """sha256 hex digest of :func:`canonical_json` of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- summary


def _hole_key(h: HoleFeature) -> tuple[float, float | None, bool, bool, bool]:
    # A non-finite depth carries no information; planning treats it like a missing depth.
    depth = norm_float(h.depth) if h.depth is not None and math.isfinite(h.depth) else None
    # Keep tiny diameters (< 1e-6) un-rounded so they stay > 0 (HoleFeature requires gt=0).
    diameter = norm_float(h.diameter) or h.diameter
    return (diameter, depth, h.through, h.counterbore, h.countersink)


def _hole_sort(k: tuple[float, float | None, bool, bool, bool]) -> tuple:
    d, depth, through, cb, csk = k
    return (d, 0 if through else 1, -1.0 if depth is None else depth, cb, csk)


def canonical_holes(holes: Iterable[HoleFeature]) -> list[HoleFeature]:
    """Merge identical hole entries (same diameter, depth, through, counterbore, countersink)
    by summing counts, and sort by (diameter, through first, depth, cb, csk).

    Diameters/depths are normalised to 1e-6. Planning drills one group per entry
    of this list, so the plan does not depend on the order holes were listed in.
    """
    counts: dict[tuple, int] = {}
    for h in holes:
        k = _hole_key(h)
        counts[k] = counts.get(k, 0) + h.count
    out: list[HoleFeature] = []
    for k in sorted(counts, key=_hole_sort):
        d, depth, through, cb, csk = k
        out.append(
            HoleFeature(diameter=d, depth=depth, through=through, count=counts[k], counterbore=cb, countersink=csk)
        )
    return out


def canonical_summary(summary: FeatureSummary) -> dict[str, Any]:
    """JSON-ready dict of the summary with order-free lists in canonical order."""
    data = summary.model_dump(mode="json")
    data["holes"] = [h.model_dump(mode="json") for h in canonical_holes(summary.holes)]
    data["tolerances"] = sorted(data["tolerances"], key=lambda t: (t["text"], t["critical"]))
    data["unsupported"] = sorted(data["unsupported"])
    return data


def inputs_hash(summary: FeatureSummary, params: Any, rules_version: str) -> str:
    """sha256 over canonical(summary + params + rules version) - design.md §3.

    ``params`` may be a pydantic model, a dict, or ``None``. Stable across runs,
    dict key order and float noise below 1e-6.
    """
    return sha256_hex({"summary": canonical_summary(summary), "params": params, "rules_version": rules_version})


def plan_inputs_hash(summary: FeatureSummary, plan_params: Any) -> str:
    """``inputs_hash`` of a process plan (planning rules version)."""
    return inputs_hash(summary, plan_params, PLAN_RULES_VERSION)


def quote_inputs_hash(summary: FeatureSummary, plan_params: Any, quote_params: Any) -> str:
    """``inputs_hash`` of a quote: covers the plan inputs, the quote params and the
    quoting rules version (planning rules + rates + material prices + subcontracts)."""
    return inputs_hash(summary, {"plan": plan_params, "quote": quote_params}, QUOTE_RULES_VERSION)


__all__ = [
    "FLOAT_DECIMALS",
    "canonical_holes",
    "canonical_json",
    "canonical_summary",
    "dec",
    "fmt_num",
    "fmt_yuan",
    "inputs_hash",
    "norm_float",
    "plan_inputs_hash",
    "quote_inputs_hash",
    "round_half_up",
    "sha256_hex",
    "to_fen",
]
