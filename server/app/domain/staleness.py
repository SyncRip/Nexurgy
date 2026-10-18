"""Staleness of stored plans/quotes and the reasons for it.

A stored result is stale exactly when its ``inputs_hash`` differs from the hash
recomputed from the current design version, params and rules (design.md §3).
:func:`summary_diff` explains *why* in terms of the FeatureSummary, and flags
which changes are manufacturing changes (material, volume/area/bbox beyond
tolerance, holes, fillets/chamfers, setup directions, tolerances, unsupported).

Comparisons use the same 1e-6 normalisation as the hash, so float noise that
cannot change the hash is never reported.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from .hashing import canonical_holes, norm_float
from .schemas import FeatureSummary, HoleFeature

BBOX_TOL_MM = 0.01
VOLUME_REL_TOL = 1e-3
VOLUME_ABS_TOL_MM3 = 1.0
AREA_REL_TOL = 1e-3
AREA_ABS_TOL_MM2 = 1.0


class Change(BaseModel):
    """One difference between two summaries (or a non-geometric cause of staleness)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    before: Any = None
    after: Any = None
    manufacturing: bool
    message_zh: str
    message_en: str


class Staleness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stale: bool
    manufacturing: bool
    reasons: list[Change]


def _n(x: float | None) -> float | None:
    return None if x is None else norm_float(x)


def _beyond(a: float | None, b: float | None, rel: float, abs_tol: float) -> bool:
    if a is None or b is None:
        return a != b
    return abs(a - b) > max(abs_tol, rel * max(abs(a), abs(b)))


def _num(x: float) -> str:
    s = f"{x:.6f}"
    return s.rstrip("0").rstrip(".")


def _hole_label(h: HoleFeature) -> str:
    s = f"Ø{_num(h.diameter)} " + ("through" if h.through else f"depth {_num(h.depth)}" if h.depth is not None else "blind")
    if h.through and h.depth is not None:
        s += f" ({_num(h.depth)})"
    if h.counterbore:
        s += " counterbore"
    if h.countersink:
        s += " countersink"
    return s


def _hole_counts(holes: list[HoleFeature]) -> dict[str, int]:
    return {_hole_label(h): h.count for h in canonical_holes(holes)}


def summary_diff(old: FeatureSummary, new: FeatureSummary) -> list[Change]:
    """Field-by-field differences from ``old`` to ``new``.

    Holes are compared as groups (identical entries merged): one change per
    group whose count differs (0 = added/removed). Tolerance notes and
    unsupported items are compared as sorted lists.
    """
    out: list[Change] = []

    def add(field: str, before: Any, after: Any, manufacturing: bool, zh: str, en: str) -> None:
        out.append(Change(field=field, before=before, after=after, manufacturing=manufacturing, message_zh=zh, message_en=en))

    if old.source != new.source:
        add("source", old.source, new.source, False, f"数据来源 {old.source} → {new.source}",
            f"Source {old.source} → {new.source}")
    if old.estimated != new.estimated:
        add("estimated", old.estimated, new.estimated, False, f"估算标记 {old.estimated} → {new.estimated}",
            f"Estimated flag {old.estimated} → {new.estimated}")
    if old.material != new.material:
        add("material", old.material, new.material, True, f"材料 {old.material} → {new.material}",
            f"Material {old.material} → {new.material}")

    ob = [norm_float(v) for v in old.bbox_mm]
    nb = [norm_float(v) for v in new.bbox_mm]
    if ob != nb:
        mfg = any(abs(a - b) > BBOX_TOL_MM for a, b in zip(ob, nb, strict=True))
        add("bbox_mm", ob, nb, mfg, f"包围盒 {ob} → {nb}", f"Bounding box {ob} → {nb}")

    ov, nv = _n(old.volume_mm3), _n(new.volume_mm3)
    if ov != nv:
        mfg = _beyond(ov, nv, VOLUME_REL_TOL, VOLUME_ABS_TOL_MM3)
        add("volume_mm3", ov, nv, mfg, f"体积 {ov} → {nv} mm³", f"Volume {ov} → {nv} mm³")

    oa, na = _n(old.area_mm2), _n(new.area_mm2)
    if oa != na:
        mfg = _beyond(oa, na, AREA_REL_TOL, AREA_ABS_TOL_MM2)
        add("area_mm2", oa, na, mfg, f"表面积 {oa} → {na} mm²", f"Area {oa} → {na} mm²")

    oh, nh = _hole_counts(old.holes), _hole_counts(new.holes)
    for label in sorted(set(oh) | set(nh)):
        a, b = oh.get(label, 0), nh.get(label, 0)
        if a != b:
            what_zh = "新增" if a == 0 else "删除" if b == 0 else "数量变化"
            what_en = "added" if a == 0 else "removed" if b == 0 else "count changed"
            add(f"holes[{label}]", a, b, True, f"孔 {label}：{what_zh} {a} → {b}", f"Holes {label}: {what_en} {a} → {b}")

    for f in ("fillets", "chamfers", "setup_directions"):
        a, b = getattr(old, f), getattr(new, f)
        if a != b:
            add(f, a, b, True, f"{f} {a} → {b}", f"{f} {a} → {b}")

    ot = sorted((t.text, t.critical) for t in old.tolerances)
    nt = sorted((t.text, t.critical) for t in new.tolerances)
    if ot != nt:
        fmt = lambda ts: [f"{t}{' (critical)' if c else ''}" for t, c in ts]  # noqa: E731
        add("tolerances", fmt(ot), fmt(nt), True, f"公差要求 {fmt(ot)} → {fmt(nt)}", f"Tolerances {fmt(ot)} → {fmt(nt)}")

    ou, nu = sorted(old.unsupported), sorted(new.unsupported)
    if ou != nu:
        add("unsupported", ou, nu, True, f"不支持项 {ou} → {nu}", f"Unsupported items {ou} → {nu}")
    return out


def evaluate(stored_hash: str, current_hash: str, old_summary: FeatureSummary | None,
             new_summary: FeatureSummary | None) -> Staleness:
    """Staleness of a stored result.

    ``stale`` is decided by the hashes alone. Reasons come from
    :func:`summary_diff`; when the hashes differ but the summaries do not, the
    cause is a params or rules-version change (reported as a non-manufacturing
    change). A missing summary on either side is a manufacturing change.
    """
    if stored_hash == current_hash:
        return Staleness(stale=False, manufacturing=False, reasons=[])
    if old_summary is None or new_summary is None:
        reasons = [Change(field="summary", before=None if old_summary is None else "present",
                          after=None if new_summary is None else "present", manufacturing=True,
                          message_zh="缺少可比较的制造特征摘要", message_en="No feature summary to compare against")]
    else:
        reasons = summary_diff(old_summary, new_summary)
    if not reasons:
        reasons = [Change(field="params_or_rules", before=stored_hash, after=current_hash, manufacturing=False,
                          message_zh="参数或规则版本已变化（几何与制造要求未变）",
                          message_en="Parameters or rules version changed (geometry and requirements unchanged)")]
    return Staleness(stale=True, manufacturing=any(r.manufacturing for r in reasons), reasons=reasons)


__all__ = [
    "AREA_REL_TOL",
    "BBOX_TOL_MM",
    "Change",
    "Staleness",
    "VOLUME_REL_TOL",
    "evaluate",
    "summary_diff",
]
