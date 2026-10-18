"""Revision diffs: op-level spec changes and geometric metric deltas.

Pure Python (no geometry kernel import).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ops import ModelSpec


class FieldChange(BaseModel):
    """One changed field. ``path`` is dotted (``profile.width``); vectors and
    point lists are compared as whole values (``at``, ``points``). A field that
    exists on one side only has ``None`` on the other."""

    path: str
    before: Any = None
    after: Any = None


class OpChange(BaseModel):
    """``added`` / ``removed`` / ``modified`` op, matched by id. ``F-00`` is
    the imported base. A changed position among the ops both revisions share
    is reported as a modification with path ``(order)``."""

    op_id: str
    change: Literal["added", "removed", "modified"]
    type: str
    label: str | None = None
    fields: list[FieldChange] = Field(default_factory=list)
    before: dict[str, Any] | None = None  # full op for removed
    after: dict[str, Any] | None = None  # full op for added


def _diff_values(before: Any, after: Any, path: str, out: list[FieldChange]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            sub = f"{path}.{key}" if path else key
            _diff_values(before.get(key), after.get(key), sub, out)
        return
    if before != after:
        out.append(FieldChange(path=path, before=before, after=after))


def diff_specs(old: ModelSpec, new: ModelSpec) -> list[OpChange]:
    """Op-level changes from ``old`` to ``new``.

    Output order: base change first, then ops in ``new`` order (added and
    modified), then removed ops in ``old`` order.
    """
    changes: list[OpChange] = []
    ob = old.base.model_dump(mode="json") if old.base else None
    nb = new.base.model_dump(mode="json") if new.base else None
    if ob != nb:
        if ob is None:
            changes.append(OpChange(op_id="F-00", change="added", type="base_step", after=nb))
        elif nb is None:
            changes.append(OpChange(op_id="F-00", change="removed", type="base_step", before=ob))
        else:
            fields: list[FieldChange] = []
            _diff_values(ob, nb, "", fields)
            changes.append(OpChange(op_id="F-00", change="modified", type="base_step", fields=fields))

    old_ops = {op.id: op.model_dump(mode="json") for op in old.ops}
    new_ops = {op.id: op.model_dump(mode="json") for op in new.ops}
    common_old = [op.id for op in old.ops if op.id in new_ops]
    common_new = [op.id for op in new.ops if op.id in old_ops]
    old_rank = {oid: i for i, oid in enumerate(common_old)}
    new_rank = {oid: i for i, oid in enumerate(common_new)}

    for op in new.ops:
        after = new_ops[op.id]
        if op.id not in old_ops:
            changes.append(OpChange(op_id=op.id, change="added", type=op.type, label=op.label, after=after))
            continue
        fields = []
        _diff_values(old_ops[op.id], after, "", fields)
        if old_rank[op.id] != new_rank[op.id]:
            fields.append(FieldChange(path="(order)", before=old_rank[op.id], after=new_rank[op.id]))
        if fields:
            changes.append(OpChange(op_id=op.id, change="modified", type=op.type, label=op.label, fields=fields))
    for op in old.ops:
        if op.id not in new_ops:
            changes.append(OpChange(op_id=op.id, change="removed", type=op.type, label=op.label, before=old_ops[op.id]))
    return changes


def _as_dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return dict(obj)


def _split(obj: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    """Accept Metrics, a metrics dict, a BuildResult or a result-like dict."""
    d = _as_dict(obj)
    if d is None:
        return None, None
    if "metrics" in d:
        holes = d.get("holes")
        return _as_dict(d["metrics"]), [_as_dict(h) for h in holes] if holes is not None else None  # type: ignore[misc]
    return d, None


def _delta(before: float | None, after: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"before": before, "after": after, "delta": None, "pct": None}
    if before is not None and after is not None:
        out["delta"] = round(after - before, 6)
        out["pct"] = round((after - before) / before * 100.0, 4) if before else None
    return out


_HOLE_KEYS = ("diameter", "depth", "through", "count", "axis", "entry", "counterbore", "countersink")


def diff_metrics(
    old: Any,
    new: Any,
    *,
    old_holes: list[Any] | None = None,
    new_holes: list[Any] | None = None,
) -> dict[str, Any]:
    """Geometric deltas between two builds.

    ``old`` / ``new`` may be ``Metrics``, metrics dicts, ``BuildResult`` objects
    or their JSON dumps (holes are then taken from them). Returns volume and
    area ``{before, after, delta, pct}``, ``bbox_size`` deltas per axis and
    ``holes: {added, removed, changed, total_before, total_after}`` (hole
    records matched by op id; ``total_*`` counts instances).
    """
    om, oh = _split(old)
    nm, nh = _split(new)
    if old_holes is not None:
        oh = [_as_dict(h) for h in old_holes]  # type: ignore[misc]
    if new_holes is not None:
        nh = [_as_dict(h) for h in new_holes]  # type: ignore[misc]
    om, nm = om or {}, nm or {}
    out: dict[str, Any] = {
        "volume_mm3": _delta(om.get("volume_mm3"), nm.get("volume_mm3")),
        "area_mm2": _delta(om.get("area_mm2"), nm.get("area_mm2")),
    }
    ob = (om.get("bbox") or {}).get("size")
    nb = (nm.get("bbox") or {}).get("size")
    out["bbox_size"] = {
        "before": ob,
        "after": nb,
        "delta": [round(b - a, 6) for a, b in zip(ob, nb)] if ob and nb else None,
    }
    out["faces"] = _delta(om.get("faces"), nm.get("faces"))
    if oh is not None or nh is not None:
        ohm = {h["op_id"]: h for h in (oh or [])}
        nhm = {h["op_id"]: h for h in (nh or [])}
        changed = []
        for oid in [k for k in nhm if k in ohm]:
            fields = [
                {"path": k, "before": ohm[oid].get(k), "after": nhm[oid].get(k)}
                for k in _HOLE_KEYS
                if ohm[oid].get(k) != nhm[oid].get(k)
            ]
            if fields:
                changed.append({"op_id": oid, "fields": fields})
        out["holes"] = {
            "added": [nhm[k] for k in nhm if k not in ohm],
            "removed": [ohm[k] for k in ohm if k not in nhm],
            "changed": changed,
            "total_before": sum(int(h.get("count", 1)) for h in ohm.values()),
            "total_after": sum(int(h.get("count", 1)) for h in nhm.values()),
        }
    return out
