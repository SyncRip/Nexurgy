"""Capability catalogue (generation reliability C, design §6.3; brief WP05: a deterministic "not supported" is worth
another attempt only after the input, a capability or a relevant decision changed).

One fixed list of what a part may need that the modeling ops might leave out or simplify. The modeling reply tags
every omission / simplification with one code (``NoteReq.capability``); the build record carries it into
``not_created[].capability`` together with the codes the kernel could build at that time (``capabilities``). A code is
available exactly when the op that builds it is in the op catalogue (:data:`app.cad.ops.Operation`): ``text`` ←
``TextOp``, ``coil`` ← ``CoilOp``; a new op registers its code in :data:`BUILT_BY`. ``other`` (nothing in the list
fits, or the reply did not say) is never available.

:func:`reuse_blocker` is the reuse rule: an earlier revision is modeled again instead of reused when one of its
omissions needs a capability that is available now but was not when it was built, or when its record has no
capability information and lists any omission (never decided from wording). Pure: no database, no kernel.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cache
from typing import Any, Literal, get_args

from .ops import CoilOp, Operation, TextOp

CATALOGUE_VERSION = 1
Capability = Literal["text", "thread", "coil", "knurl", "weld_bead", "gear_tooth", "casting_detail", "freeform", "other"]
CAPABILITIES: tuple[str, ...] = get_args(Capability)

#: What each code means (the reply schema's field description lists the codes, this is the reference).
MEANING: dict[str, str] = {
    "text": "raised / engraved lettering, characters, symbols",
    "thread": "a modelled thread form (external or internal)",
    "coil": "a section swept along a helix (springs, split lock washers)",
    "knurl": "knurling",
    "weld_bead": "weld beads / seams as geometry",
    "gear_tooth": "gear, sprocket or spline tooth flanks",
    "casting_detail": "casting drafts, cores, cast-on detail",
    "freeform": "freeform / organic surfaces",
    "other": "anything the list does not name",
}

#: The op of the catalogue that builds a code. A code without one is not available.
BUILT_BY: dict[str, type] = {"text": TextOp, "coil": CoilOp}


@cache
def _op_types() -> frozenset[str]:
    union = get_args(Operation)[0]  # Annotated[Union[...], Field(discriminator="type")]
    return frozenset(get_args(cls.model_fields["type"].annotation)[0] for cls in get_args(union))


def available() -> tuple[str, ...]:
    """The codes the kernel builds today (sorted)."""
    types = _op_types()
    return tuple(sorted(c for c, op in BUILT_BY.items() if get_args(op.model_fields["type"].annotation)[0] in types))


def of_op(op_type: Any) -> str:
    """The code an op type builds (``other`` for an op that is not one of :data:`BUILT_BY`)."""
    return next((c for c, op in BUILT_BY.items() if get_args(op.model_fields["type"].annotation)[0] == op_type), "other")


def record_block(codes: Iterable[str] | None = None) -> dict[str, Any]:
    """The ``capabilities`` block of a build record: catalogue version and the codes available to that build."""
    return {"version": CATALOGUE_VERSION, "available": sorted(available() if codes is None else set(codes))}


def reuse_blocker(record: Mapping[str, Any] | None, now: Iterable[str] | None = None) -> dict[str, Any] | None:
    """Why an earlier revision with this build record is modeled again instead of reused, else None.

    - ``capability_added``: an omission's capability is available ``now`` (default :func:`available`) but was not in the
      record's ``capabilities.available`` (built before the op existed); ``capabilities`` lists those codes.
    - ``capabilities_unrecorded``: the record has no capability information and lists omissions (``not_created``).

    Everything else is reused: an omission whose capability is still missing (a thread as a plain bore) or was already
    available (building again gives the same result)."""
    rec = record if isinstance(record, Mapping) else {}
    left = [g for g in rec.get("not_created") or [] if isinstance(g, Mapping)]
    if not left:
        return None
    block = rec.get("capabilities")
    then = block.get("available") if isinstance(block, Mapping) else None
    if not isinstance(then, list):
        return {"code": "capabilities_unrecorded", "capabilities": [],
                "not_created": [str(g.get("requirement") or "")[:200] for g in left][:5]}
    gained = (set(available() if now is None else now) - set(then)) & set(CAPABILITIES)
    hit = [g for g in left if g.get("capability") in gained]
    if not hit:
        return None
    return {"code": "capability_added", "capabilities": sorted({str(g["capability"]) for g in hit}),
            "not_created": [str(g.get("requirement") or "")[:200] for g in hit][:5], "built_with": dict(block)}


__all__ = ["BUILT_BY", "CAPABILITIES", "CATALOGUE_VERSION", "Capability", "MEANING", "available", "of_op", "record_block",
           "reuse_blocker"]
