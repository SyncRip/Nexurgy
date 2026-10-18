"""Turbo: how the instances the spec leaves without a position are connected (generation supplement F: TB-03 / B-02.4 /
D-01; G: level by level, brief §8.7; generation reliability L: top-down, brief WP06 / AC13 / AC29; round 3, package B:
the model chooses connections, the solver computes positions; .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md
§2.6, §5.1, §6.2).

:func:`decide` runs in ``orchestration.build_assembly`` after the parts are built and before the assembly is created,
in Turbo only. It finds the instances the partial build keeps but could only arrange for display (no stated placement,
no kept relation positions them; ``assemblies.placement_gaps``) and decides them LEVEL BY LEVEL, TOP-DOWN, with
structured model calls (``ai/placement_agent``, the run's modeling model). The model answers with MATES — which datum
of the instance touches / lines up with which datum of a sibling or of an assembly frame — never with coordinates:

1. Before any call, every kept assembly definition is summarised in its own frame (:class:`_Layout`, deepest first):
   the bounding box of its children that already have a position (stated, solved from the kept spec relations, the
   lone child at the origin, a decision reused from the build this one continues), the outward datums of those children
   (``<child instance>/<datum>``), its frame datums and interfaces, and an ENVELOPE — a size estimate that also counts
   the built children without a position yet (stacked along +Z above the positioned ones), so a sub-assembly has a size
   even when nothing inside it is placed.
2. The calls go from the root down: the main sub-assembly instances (and the parts) in the root frame first — each
   sub-assembly connected by its frame datums (origin, X / Y / Z, XY / YZ / ZX), interfaces or positioned contents —
   then the parts inside each sub-assembly to its frame, one level deeper at a time. Assemblies of one height never
   contain each other, so their pending instances share calls (siblings together, sub-assembly instances in a batch of
   their own, at most BATCH_INSTANCES per call, MAX_CALLS calls per build).
3. The context of a call offers the datums the mates may name: each definition's built datums (parts: ``part_datums``;
   hole patterns compacted), each sub-assembly's frame datums / interfaces / positioned contents' datums, and the
   assembly FRAMES ``@<key>``: the parent's own frame datums and interfaces (``content.interfaces``, owner = that
   assembly), and the frames above it where its place in them is known (mapped into the parent frame; a sub-assembly
   used twice has no one place in the frames above it). A sub-assembly's frame says how it was attached in its parent.
4. After each call the mates are bound and every parent they touch is SOLVED (:mod:`app.cad.mates`, Turbo): spec
   relations (priority 100) + interfaces + the AI mates (priority by ``basis_kind``: stated 110 / derived 120 /
   estimated 130). A conflicting AI mate the solver drops is recorded as a dropped decision and not applied; an
   instance is decided when the solve POSITIONS it in its parent frame — no free translation left, a held turn about
   an axis is fine (:func:`app.cad.mates.grounded`) —, otherwise it stays local (a floating group, P; connected but
   free to move) or arranged. The solved poses are what the later calls see (``placement`` of the instances,
   ``frames``), so every level builds on the one above.
5. After the top-down pass, positioned children may expose interfaces their parent's earlier call could not use.
   Summarise those assemblies again and revisit only unresolved instances whose sibling assembly interfaces have
   gained named datums. Keep all existing mates and the same call / runtime budget; stop when no new interfaces are
   available. A prior quantity refusal can be reconsidered with new interfaces, but is cleared only by a grounded solve.

Review of round 3: the layout has no datums or solve of its own — an instance binds the build's datums
(``assemblies.part_datums`` / ``assemblies.assembly_datums``; a part's ``origin`` is only hidden from the model's list,
never from solving) and each level is solved by ``assemblies.solve_level`` with ``assemblies.held_fixed``, as the build
solves it, so what the calls see, ``decided`` and the decisions' ``solved`` agree with the build's coverage. Instances
the spec relations join but leave free to move are placed by the calls too (:meth:`_Layout.loose`); a spec relation
putting one instance on one axis of a pattern is a choice the call makes (``choose``). A ``fixed`` position is taken in
the frame it names and only when its numbers are written in the quoted sentence or an interface.

The decision goes to ``create_from_spec(placement=…)`` (:meth:`PlacementDecision.applied`; the stored spec version is
never changed): the AI mates become ``ai_turbo`` mates of their parent assembly revisions — the build solves them again
the same way, records the remaining degrees of freedom it holds and stores the transforms, so every position can be
rebuilt along the parent chain — and each mate is one ``ai_turbo`` decision on the root revision (field ``mate``). A
``fixed`` position is accepted only where a source sentence states it (field ``placement``). An instance is installed
(``coverage.placed``) only when every assembly instance above it up to the root is placed too
(``spec_coverage.chain_placement``); ``coverage.decided`` is the AI-placed subset; ``estimated`` the installed ones
whose connection is an estimate. What the model leaves unconnected, an invalid mate, a provider error or timeout, no
usable model, the call cap or too little runtime left: those instances fall back to the rule display arrangement
(``arranged``, never installed) and the build notes say why; a sub-assembly instance left without a position is a
``placement`` item of the coverage. The build never fails because of this step.

A retry of the same node run (S3 continue) reuses the mates (and stated positions) the partial revision it continues
decided for the instances still pending (no new call for them; D-03). Numeric AI positions of revisions made before
round 3 are not reused (they were computed by the model; the instances are connected again).

Round 2 (package P, WP06): every connection carries ``basis_kind`` (stated / derived / estimated) — estimated ones count
installed and are shown apart (``coverage.estimated``). The anchor of each floating relation group (siblings joined only
to each other by spec relations, none anchored to the parent frame) is pending too: once it is connected, the
relations carry its group. Instances the model finds beyond the installation positions the sources give become
``placement`` items ``quantity_exceeds_positions`` (``PlacementDecision.missing``). A parent's siblings beyond one batch
are cut into even chunks between definitions (:func:`_split`).

Standard: nothing is called; those instances stay missing until the spec says where they go.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import policy as pol
from ..ai import placement_agent as agent
from ..ai import retry as call_retry
from ..ai.providers.base import ProviderError, classify, clip
from ..cad import assembly_model as am
from ..cad.assembly_model import Datum
from ..models import AssemblyRevision, EngineeringSpecChunk, EngineeringSpecVersion, ModelRevision
from ..storage import read_blob
from . import source_reader as sr

log = logging.getLogger("partroute.assembly_placement")

#: One placement call (s); it is not started when less than PLACEMENT_RESERVE_S is left of the node's runtime. Round 2
#: (P): a decision writes what was known and why — 20 instances are about 5 000 output tokens, two minutes at the BHD1
#: rate (≈ 40 tokens/s), so the limit is 240 s.
PLACEMENT_TIMEOUT_S = 240
PLACEMENT_RESERVE_S = 260
FINALIZATION_RESERVE_S = 90
#: Real-model run BHD1 (2026-10-12): 131 instances in one reply ran past the call's time limit. The instances to place
#: are sent in batches of their own parents (siblings together), at most BATCH_INSTANCES per call and MAX_CALLS calls;
#: what is left after that is arranged by rule (and said so). Round 2 (P): a parent's pending siblings are cut into even
#: chunks of at most 20 between definitions (:func:`_split`).
BATCH_INSTANCES = 20
MAX_CALLS = 24  # initial traversal plus one bounded geometry/solver-feedback refinement per parent
MAX_INSTANCES = 60
MAX_FACTS = 40
MAX_NOTES = 20
MAX_RELATIONS = 40
MAX_SENTENCES = 30
#: Global coordinate sentences, given to the calls that place sub-assembly or root instances (after the first six
#: sentences that name the parts to place).
MAX_COORD_SENTENCES = 30
#: The prompt budget of a call that places sub-assembly or root instances (BHD1 real run 2026-10-13: at 12 000 characters
#: the global coordinate table was cut and every main sub-assembly stayed unplaced, "no root coordinate source").
# Complete child interfaces and their positioned supports may exceed the old 36k summary. The combined source,
# image, schema and output allowance is still checked against the configured provider window before every call.
ASSEMBLY_PROMPT_CHARS = 64_000
MAX_DRAWING_CONTEXT_CHARS = 180_000
#: Datum entries per definition / frame in the context (a hole pattern is one entry): the vocabulary of the mates.
MAX_DATUMS = 24
#: Datums are listed for the first definitions only (the ones placed here first): reading them may rebuild the part.
MAX_DATUM_DEFINITIONS = 40
#: Aggregate datum groups per sub-assembly (parallel feature axes stay complete), and the part definitions read
#: read are free; the assembly build reads them anyway where relations mate them).
MAX_AGGREGATE_DATUMS = 24
MAX_AGGREGATE_DATUM_PARTS = 40
MAX_CHUNKS = 40
_MAX_LEVELS = 16
_SENTENCE = re.compile(r"(?<=[。！？；!?;])|\n+|(?<=\.)\s+")
#: A global coordinate statement: "Z=120", "X = -35", "±0.5", "origin", "coordinates".
#: Global coordinate / datum sentences: the one pattern every reader of stated assembly geometry uses.
_COORD = sr.COORDINATE_STATEMENT


@dataclass
class PlacementDecision:
    """``pending``: instances without a position before the decision; ``mates``: the AI placement mates kept (relations
    for ``create_from_spec``, each with its decision, its frame datums and where the solve put its instance) and the ones
    the solve dropped (``dropped``: why); ``fixed``: instance key → a position a source states; ``poses``: instance key →
    {translate, rotate} of every instance with a known position in its parent (for mapping assembly frames); ``decided``:
    the pending instances the mates tie to their parent frame; ``notes``: build notes ({code, message, instances});
    ``calls``: the call records; ``missing`` (round 2, P): coverage items of the instances beyond the installation
    positions the sources give (``quantity_exceeds_positions``, one per definition, with the evidence)."""

    pending: list[str] = field(default_factory=list)
    mates: list[dict[str, Any]] = field(default_factory=list)
    fixed: dict[str, dict[str, Any]] = field(default_factory=dict)
    poses: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    decided: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    simulated: bool = False
    missing: list[dict[str, Any]] = field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        return {"pending": len(self.pending), "decided": len(self.decided), "reused": len(self.reused), "calls": len(self.calls),
                "arranged": len([k for k in self.pending if k not in self.decided]), "simulated": self.simulated,
                "mates": len([m for m in self.mates if not m.get("dropped")]), "dropped": len([m for m in self.mates if m.get("dropped")])}

    def applied(self) -> dict[str, Any] | None:
        """What ``create_from_spec(placement=…)`` applies (None when nothing was decided or found)."""
        if not self.mates and not self.fixed and not self.poses:
            return None
        return {"mates": list(self.mates), "fixed": dict(self.fixed), "poses": dict(self.poses)}


def _fallback(keys: list[str]) -> str:
    return f"{len(keys)} instance(s) arranged for display by rule, not installed: {', '.join(keys[:12])}" + (" …" if len(keys) > 12 else "")


def _placement_error(error: dict[str, Any], *, instances: list[str], calls: list[Any] | None = None) -> dict[str, Any]:
    """Retain the terminal request's observed class without exposing its prompt or response."""
    from .failure_causes import cause

    terminal = calls[-1] if calls else None
    cls = error.get("failure_class")
    if not cls and terminal is not None and terminal.status == "error":
        cls = terminal.failure_class
    cls = cls or classify(error.get("code"))
    recovery = ("configure_model" if cls in ("auth", "permission", "quota") else
                "fix_spec" if cls in ("unsupported", "bad_request") else "retry_definition")
    return cause({**error, "failure_class": cls, "recovery": recovery}, stage="placement", instances=instances) or {}


def decide(db: Session, *, spec_version: EngineeringSpecVersion, content: dict[str, Any], bindings: dict[str, dict[str, Any]],
           policy: str, provider_of: Callable[[], Any], missing: list[dict[str, Any]] | None = None,
           continue_from: AssemblyRevision | None = None, deadline: float | None = None,
           on_calls: Callable[[list[Any]], None] | None = None, checkpoint: Callable[[], bool] | None = None,
           cancelled: Callable[[], bool] | None = None, solve_cache: dict[str, Any] | None = None) -> PlacementDecision:
    """Reserve finalization time across model retries and numerical solves; checkpoint calls before solving."""
    from ..cad.mates import solve_budget

    parent = call_retry.current_limits()
    ends = [end for end in (deadline, parent.deadline if parent else None) if end is not None]
    effective_deadline = min(ends) if ends else None
    limits = _PlacementLimits(parent, effective_deadline, cancelled)
    with call_retry.limits_scope(limits), solve_budget(deadline=limits.deadline, cancelled=cancelled, cache=solve_cache):
        return _decide(db, spec_version=spec_version, content=content, bindings=bindings, policy=policy,
                       provider_of=provider_of, missing=missing, continue_from=continue_from, deadline=effective_deadline,
                       on_calls=on_calls, checkpoint=checkpoint, cancelled=cancelled)


class _PlacementLimits(call_retry.CallLimits):
    """An earlier deadline around the SAME request counter/lease, not a fresh run budget."""

    def __init__(self, parent: call_retry.CallLimits | None, deadline: float | None, cancelled: Callable[[], bool] | None):
        ends = [end - FINALIZATION_RESERVE_S for end in (deadline, parent.deadline if parent else None) if end is not None]
        super().__init__(deadline=min(ends) if ends else None, deadline_kind=parent.deadline_kind if parent else "node",
                         cancelled=cancelled)
        self.parent = parent

    def stop_reason(self, **kwargs: Any) -> call_retry.Stop | None:
        return super().stop_reason(**kwargs) or (self.parent.stop_reason(**kwargs) if self.parent is not None else None)

    def take(self, unit: str | None = None) -> ProviderError | None:
        stop = self.stop_reason(calls=False)
        if stop is not None:
            return stop.error()
        return self.parent.take(unit) if self.parent is not None else super().take(unit)


def _decide(db: Session, *, spec_version: EngineeringSpecVersion, content: dict[str, Any], bindings: dict[str, dict[str, Any]],
            policy: str, provider_of: Callable[[], Any], missing: list[dict[str, Any]] | None = None,
            continue_from: AssemblyRevision | None = None, deadline: float | None = None,
            on_calls: Callable[[list[Any]], None] | None = None, checkpoint: Callable[[], bool] | None = None,
            cancelled: Callable[[], bool] | None = None) -> PlacementDecision:
    """The AI placement decision of one Turbo build, level by level from the root down (see the module docstring).
    ``provider_of()`` returns the run's modeling provider (raises ProviderError when none is usable); ``deadline``:
    monotonic end of the node's runtime."""
    from .assemblies import placement_gaps

    out = PlacementDecision()
    checkpoint_stopped = False

    def record_calls(calls: list[Any]) -> bool:
        nonlocal checkpoint_stopped
        out.calls += list(calls)
        if calls and on_calls is not None:
            on_calls(list(calls))
        if calls and checkpoint is not None and not checkpoint():
            checkpoint_stopped = True
            layout.stopped = True
            out.notes.append({"code": "placement_checkpoint_stopped", "instances": [],
                              "message": "placement stopped because this attempt could not checkpoint its calls"})
        return not checkpoint_stopped
    if not pol.is_turbo(policy):
        return out
    drawing_sources = bool(db is not None and any(
        isinstance(source, dict) and source.get("file_version_id")
        and str(source.get("name") or "").casefold().endswith((".dwg", ".dxf"))
        for source in getattr(spec_version, "sources", None) or []))
    try:
        gaps = placement_gaps(db, spec=content, bindings=bindings, policy=policy, missing=missing)
    except Exception as exc:  # noqa: BLE001 - the assembly step itself reports a spec it cannot build
        log.warning("placement gaps of spec version %s: %s", spec_version.id, exc)
        return out
    out.pending = list(gaps["pending"])
    if not out.pending and not gaps.get("mated") and not drawing_sources:
        return out
    try:  # every frame summarised first (deepest first), then the calls from the root down
        layout = _Layout(db, content, bindings, gaps, deadline)
        layout.summarize_all()
        # review of round 3 (connected is not positioned): the instances the spec relations join but leave free to move
        # (only parallel to a frame, a slide along an axis) are connected by the calls too
        out.pending += layout.loose(out.pending)
    except Exception as exc:  # noqa: BLE001 - without the layout no mate can be bound: arranged by rule, said so
        log.exception("placement layout of spec version %s", spec_version.id)
        out.notes.append({"code": "placement_not_decided", "instances": out.pending,
                          "message": f"the placement layout could not be built ({type(exc).__name__}); {_fallback(out.pending)}"})
        return out
    if not out.pending and not drawing_sources:
        return out
    reused = layout.reuse(continue_from, out.pending)
    if reused:
        layout.summarize_all(again=True)  # with the connections reused
        out.pending += layout.loose(out.pending)
        out.reused = reused
        out.notes.append({"code": "placement_reused", "instances": reused,
                          "message": f"AI placement decisions of A{continue_from.rev_no if continue_from else '?'} reused for "
                                     f"{', '.join(reused)} (no new call)"})
    todo = [k for k in out.pending if k not in reused]

    def late() -> bool:
        return checkpoint_stopped or bool(cancelled and cancelled()) or (
            deadline is not None and time.monotonic() > deadline - PLACEMENT_RESERVE_S)

    def finish() -> PlacementDecision:
        layout.finish(out)
        if drawing_sources and not checkpoint_stopped and not any(
                note.get("code") == "placement_physical_review" for note in out.notes):
            from .assembly_placement_checks import check_layout
            from .assembly_placement_review import _store

            try:
                report = check_layout(layout, deadline=deadline, cancelled=cancelled)
            except Exception as exc:
                report = {"status": "unknown", "complete": False, "fingerprint": None,
                          "incomplete_reasons": [getattr(exc, "code", type(exc).__name__)]}
            _store(out, report, report.get("fingerprint"), [])
        remaining = [k for k in out.pending if k not in out.decided]
        if remaining and deadline is not None and time.monotonic() > deadline - PLACEMENT_RESERVE_S:
            out.notes.append({"code": "placement_runtime_budget", "instances": remaining,
                              "message": "placement stopped before the runtime cap; completed decisions and calls were retained"})
        # A first-pass refusal is history in the call record, not a current placement gap after refinement succeeds.
        placed = set(out.decided)
        notes = []
        for n in out.notes:
            keys = list(n.get("instances") or [])
            if keys and n.get("code") in ("placement_left_open", "placement_call_failed", "placement_not_decided", "placement_refinement_failed"):
                remaining = [k for k in keys if k not in placed]
                if not remaining:
                    continue
                n = {**n, "instances": remaining, "message": n["message"].replace(_fallback(keys), _fallback(remaining))}
                if isinstance(n.get("error"), dict):
                    n["error"] = {**n["error"], "instances": remaining}
            notes.append(n)
        out.notes = notes
        return out

    if not todo:
        return finish()
    if late():
        out.notes.append({"code": "placement_not_decided", "instances": todo,
                          "message": f"the step's runtime budget left no room for the AI placement decision; {_fallback(todo)}"})
        return finish()
    try:
        provider = provider_of()
    except ProviderError as exc:
        out.notes.append({"code": "placement_not_decided", "instances": todo,
                          "error": _placement_error(exc.to_dict(), instances=todo),
                          "message": f"no model for the AI placement decision ({exc.code}: {clip(exc.message, 200)}); {_fallback(todo)}"})
        return finish()
    try:  # the calls from the root down
        stages = layout.stages(content, todo)
    except Exception:  # noqa: BLE001 - without the levels it is one level (F), never a failed build
        log.exception("placement levels of spec version %s", spec_version.id)
        stages = [(0, _batches(content, todo))]
    drawing_cache = None
    if db is not None and getattr(spec_version, "sources", None):
        from .assembly_drawing_evidence import DrawingEvidenceCache

        drawing_cache = DrawingEvidenceCache(db, spec_version, vision=provider.capabilities.vision)
    made = 0
    rest: list[str] = []
    stop: str | None = None
    # what a stated position may quote besides its sentences: the numbers of the spec's interfaces
    stated = agent.stated_numbers([[f.get("at"), f.get("points")] for f in content.get("interfaces") or [] if isinstance(f, dict)])
    exceeds: dict[str, dict[str, Any]] = {}  # instance key → {reason, sources}: beyond the positions the sources give
    seen_interfaces: dict[str, set[tuple[Any, ...]]] = {}
    while stages:
        for _height, batches in stages:
            for batch_index, batch in enumerate(batches):
                if stop is None and (made >= MAX_CALLS or late()):
                    stop = (f"the AI placement decision is limited to {MAX_CALLS} calls" if made >= MAX_CALLS
                            else "the step's runtime budget left no room for more AI placement calls")
                if stop is not None:
                    rest += batch
                    continue
                # A context refusal is an attempt at these interfaces too. Otherwise a late retry that exceeds
                # its context budget keeps comparing against the older pre-child snapshot and loops unchanged.
                for k in batch:
                    seen_interfaces[k] = layout.child_interfaces(layout.parent(k))
                try:  # earlier levels' and batches' connections are solved: the positions they give are known here
                    context, references, endpoints = placement_context(db, spec_version, content, layout, batch)
                    big = any(r.get("keep") for r in references) or any(
                        d.get("kind") == "assembly" and d.get("datums") for d in context.get("definitions") or [])
                    drawing_text, images = _drawing_context(drawing_cache, layout, batch)
                    user = _model_message(provider, context, references, batch,
                                          limit=ASSEMBLY_PROMPT_CHARS if big else agent.MAX_PROMPT_CHARS,
                                          drawing_text=drawing_text, images=images)
                    endpoints = agent.visible_endpoints(user, endpoints)
                except Exception as exc:  # noqa: BLE001 - a context bug must not fail the build; it falls back and says so
                    log.exception("placement context of spec version %s", spec_version.id)
                    out.notes.append({"code": "placement_not_decided", "instances": batch,
                                      "message": f"the placement context could not be built ({getattr(exc, 'code', type(exc).__name__)}); {_fallback(batch)}"})
                    continue
                made += 1
                texts: dict[str, list[str]] = {}
                for r in references:
                    texts.setdefault(r["locator"], []).append(r["text"])
                res = agent.run_placement(provider, user, batch, locators=set(texts), endpoints=endpoints, texts=texts, numbers=stated,
                                          instances={str(i.get("key")) for i in context.get("instances") or []}, timeout_s=PLACEMENT_TIMEOUT_S,
                                          images=images)
                if not record_calls(res.calls):
                    stop = "placement checkpoint failed; no new placement work started"
                    rest += batch
                    continue
                out.simulated = out.simulated or res.simulated
                if res.error is not None:
                    out.notes.append({"code": "placement_call_failed", "instances": batch,
                                      "error": _placement_error(res.error, instances=batch, calls=res.calls),
                                      "message": f"the AI placement call failed ({res.error['code']}: {res.error['message']}); {_fallback(batch)}"})
                    continue
                out.notes += layout.accept(res)
                # A planar/coaxial relation group is connected, not necessarily rigid. Once its anchor is
                # actually grounded, give members with remaining joint motion their own bounded decision.
                extra = layout.loose(out.pending)
                if extra:
                    out.pending += extra
                    todo += extra
                    batches[batch_index + 1:batch_index + 1] = _batches(content, extra)
                exceeds.update(res.exceeds)
                for key, why in res.unplaced.items():
                    if key in res.exceeds:  # P: beyond the positions the sources give — its own coverage item, not "no basis"
                        continue
                    out.notes.append({"code": "placement_left_open", "instances": [key],
                                      "message": f"AI placement left {key} without a connection ({why}); "
                                                 + ("its spec relations leave it free to move: placed only locally, not installed" if key in layout.mated
                                                    else "arranged for display by rule, not installed")})
        if stop is not None or late() or all(layout.pose(k) is not None for k in todo):
            break
        # The first pass must stay top-down. Only solved child geometry can unlock an earlier unresolved batch;
        # repeating an unchanged prompt cannot. This also bounds retries of genuine source / quantity gaps.
        try:
            layout.summarize_all(again=True)
            retry = [k for k in todo if k in seen_interfaces and layout.pose(k) is None
                     and layout.child_interfaces(layout.parent(k)) - seen_interfaces[k]]
        except Exception:  # noqa: BLE001 - retain the first-pass decisions if a summary cannot be refreshed
            log.exception("placement refinement of spec version %s", spec_version.id)
            break
        if not retry:
            break
        stages = layout.stages(content, retry)
    exceeds = {k: v for k, v in exceeds.items() if layout.pose(k) is None}
    if rest:
        out.notes.append({"code": "placement_call_budget" if made >= MAX_CALLS else "placement_not_decided",
                          "instances": rest, "message": f"{stop}; {_fallback(rest)}"})
    if stop is None:
        made = _refine(db, spec_version, content, layout, out, provider, seen_interfaces, made, late, drawing_cache, record_calls)
    if drawing_sources and drawing_cache is not None and not checkpoint_stopped:
        from .assembly_placement_review import review_layout

        try:
            review_layout(db, spec_version, content, layout, out, provider, made=made, late=late,
                          drawing_cache=drawing_cache, record_calls=record_calls, deadline=deadline, cancelled=cancelled)
        except Exception as exc:
            log.exception("final drawing assembly review of spec version %s", spec_version.id)
            out.notes.append({"code": "placement_physical_review", "instances": [],
                              "message": "Final material examination could not complete; the candidate remains unverified.",
                              "report": {"status": "unknown", "complete": False, "fingerprint": None,
                                         "incomplete_reasons": [getattr(exc, "code", type(exc).__name__)]}})
    exceeds = {k: value for k, value in exceeds.items() if layout.pose(k) is None}
    if exceeds:
        out.missing, notes = _exceeds_items(content, exceeds)
        out.notes += notes
    return finish()


def _drawing_context(cache: Any, layout: _Layout, batch: list[str]) -> tuple[str, list[Any]]:
    """Parent and target subassembly sheets; retain complete original evidence or refuse the call."""
    if cache is None:
        return "", []
    text, images = [], []
    definitions = list(dict.fromkeys(layout.parent(k) for k in batch))
    for key in batch:
        dk = (layout.insts.get(key) or {}).get("definition")
        if layout.is_asm(dk) and dk not in definitions:
            definitions.append(dk)
    seen = set()
    for dk in definitions:
        bundle = cache.for_definition(layout.defs.get(dk) or {}, layout.content)
        if bundle is not None:
            # Repeated instances and distinct definition aliases can select the same original sheet. Frame IDs
            # alone are not unique across source files. Unknown provenance must never collapse different sheets.
            detail = getattr(bundle.drawing, "detail", None) or {}
            fid, sid, frame = getattr(bundle, "source_file_version_id", None), detail.get("source_id"), detail.get("frame_id")
            if fid and frame:
                identity = ("file", str(fid), frame)
            elif sid and frame:
                identity = ("source", sid, frame)
            else:
                identity = ("definition", dk)
            if identity in seen:
                continue
            seen.add(identity)
            text.append(bundle.drawing.block)
            images.extend(bundle.drawing.images)
    block = "\n\n" + "\n\n".join(text) if text else ""
    if len(block) > MAX_DRAWING_CONTEXT_CHARS:
        from ..errors import AppError

        raise AppError(422, "context_over_budget", "original assembly sheet context exceeds the bounded placement input")
    return block, images


def _model_message(provider: Any, context: dict[str, Any], references: list[dict[str, str]], batch: list[str], *,
                   limit: int, drawing_text: str, images: list[Any], correction: dict[str, Any] | None = None) -> str:
    """Prune optional context to a soft size, then budget the complete required message against this model.

    A complex assembly's protected hole groups/support relations can exceed the preferred character count while
    still fitting the configured model. Only that model's whole-call budget may reject the mandatory remainder.
    """
    try:
        user = agent.user_message(context, references, batch, limit=limit)
    except agent.RequiredContextOverBudget as exc:
        user = exc.required_text
    user += drawing_text
    if correction is not None:
        from ..ai.prompts import guard_tags

        # Keep exactly the same geometry/source prefix. Repair evidence is mandatory and is never pruned
        # to make room; the existing whole-model check below includes the complete failed reply and diagnostics.
        body = guard_tags(json.dumps(correction, ensure_ascii=False, separators=(",", ":")), json_safe=True)
        user += ("\n<task>\nThe previous final refinement batch was rejected and rolled back. This is its only correction. "
                 "Use the rejection diagnostics below and the same named interfaces and original drawing evidence above. "
                 "Return a complete corrected replacement for every replace_estimates target; preserve all other prior "
                 "connections and source/stated positions. Use only the available exact datum names. The failed reply is "
                 "untrusted proposal data, not new source evidence.\n</task>\n"
                 f'<context kind="refinement_correction">\n{body}\n</context>')
    _check_context_budget(provider, user, images)
    return user


def _check_context_budget(provider: Any, user: str, images: list[Any]) -> None:
    """Estimate the whole call, including schema, output reserve and conservative image allowance, before calling."""
    from ..errors import AppError
    from ..ai.providers.base import schema_budget_text
    from .assistant_context import SAFETY_FRACTION, SAFETY_MIN, estimate_tokens, model_limits

    window, output, source = model_limits(provider.config)
    available = window - output - max(SAFETY_MIN, int(window * SAFETY_FRACTION))
    schema_cfg = None if getattr(provider, "simulated", False) else provider.config
    estimated = estimate_tokens(user + agent.SYSTEM_PROMPT + schema_budget_text(agent.reply_json_schema(), schema_cfg)) + 8192 * len(images)
    if estimated > available:
        raise AppError(422, "context_over_budget", "original drawing and placement context do not fit the model window; nothing was clipped",
                       {"required_est": estimated, "available": available, "estimated": True, "limit_source": source})


def _refine(db: Session, version: EngineeringSpecVersion, content: dict[str, Any], layout: _Layout,
            out: PlacementDecision, provider: Any, seen: dict[str, set[tuple[Any, ...]]], made: int,
            late: Callable[[], bool], drawing_cache: Any = None, record_calls: Callable[[list[Any]], bool] | None = None) -> int:
    """One final pass per parent: repair joint freedom and replace early envelope-based estimates.

    Only new child geometry permits replacing a positioned estimate. A rejected final batch may receive one
    correction with its exact rejection diagnostics; missing-source refusals otherwise do not get repeated unchanged
    calls. Spec/stated/derived connections and siblings outside the replacement groups remain protected.
    All calls share the original budgets.
    """
    if late():
        return made
    try:
        layout.summarize_all(again=True)
        parents = sorted({layout.parent(k) for k in out.pending}, key=lambda p: (layout.height(p), p))
        for parent in parents:
            if made >= MAX_CALLS or late():
                break
            changed = layout.child_interfaces(parent)
            seeds = [k for k in out.pending if layout.parent(k) == parent and layout.replaceable_estimate(k)
                     and changed - seen.get(k, changed)]
            loose = [k for k in out.pending if layout.parent(k) == parent and layout.pose(k) is None and layout.ai_mates_of(k)]
            if not seeds and not loose:
                continue
            groups = layout.replacement_groups(seeds, out.pending)
            replace = [key for group in groups for key in group]
            # The joint motion may come from a positioned supporting sibling, not the pending fastener itself.
            supports = layout.supports(loose)
            batches, skipped = _refinement_batches(groups, loose + supports, layout.insts)
            for group in skipped:
                out.notes.append({"code": "placement_refinement_group_skipped", "instances": group,
                                  "reason": "batch_instance_budget", "limit": BATCH_INSTANCES,
                                  "message": "the complete estimated dependency group exceeds the placement batch limit; "
                                             "previous decisions retained without splitting the group"})
            for batch in batches:
                if made >= MAX_CALLS or late():
                    break
                replacing = [k for k in batch if k in replace]
                context, references, endpoints = placement_context(db, version, content, layout, batch)
                replacing_mates = {key: [mate["key"] for mate in layout.replaceable_mates(key)] for key in replacing}
                replace_keys = {key for keys in replacing_mates.values() for key in keys}
                context["refinement"] = {"replace_estimates": replacing,
                                         "replace_estimated_mates": replacing_mates,
                                         "protected_mates": [r["key"] for r in context["relations"] if r["key"] not in replace_keys],
                                         "replacement_groups": [group for group in groups if group[0] in replacing],
                                         "repair_supports": [k for k in batch if k in supports],
                                         "reason": "final positioned child geometry and actual solver feedback"}
                for instance in context["instances"]:
                    if instance["key"] in replacing:
                        instance["positioned_by"] = "replace only listed estimated mates using final geometry; all other existing mates are retained"
                    elif instance["key"] in supports:
                        instance["positioned_by"] = "support: preserve current mates; add orientation only if needed to anchor pending siblings"
                drawing_text, images = _drawing_context(drawing_cache, layout, batch)
                texts: dict[str, list[str]] = {}
                for ref in references:
                    texts.setdefault(ref["locator"], []).append(ref["text"])
                correction = None
                for attempt in range(2):
                    limits = call_retry.current_limits()
                    limit_stop = limits.stop_reason() if limits is not None else None
                    if made >= MAX_CALLS or late() or limit_stop is not None:
                        if correction is not None:
                            out.notes.append({"code": "placement_refinement_correction_skipped", "instances": batch,
                                              "message": "refinement correction was not started; previous decisions retained",
                                              "reason": ("placement_call_budget" if made >= MAX_CALLS else
                                                         limit_stop.code if limit_stop is not None else "runtime_or_cancellation")})
                        break
                    user = _model_message(provider, context, references, batch, limit=ASSEMBLY_PROMPT_CHARS,
                                          drawing_text=drawing_text, images=images, correction=correction)
                    visible = agent.visible_endpoints(user, endpoints)
                    made += 1
                    result = agent.run_placement(provider, user, batch, locators=set(texts), endpoints=visible, texts=texts,
                                                 instances={i["key"] for i in context["instances"]}, timeout_s=PLACEMENT_TIMEOUT_S,
                                                 images=images)
                    if record_calls is not None:
                        if not record_calls(result.calls):
                            return made  # no solve or another batch after losing the durable checkpoint
                    else:
                        out.calls += result.calls
                    out.simulated = out.simulated or result.simulated
                    stopped = limits.stop_reason(calls=False) if limits is not None else None
                    if late() or stopped is not None:
                        return made  # cancellation/lease loss/runtime expiry during the call cannot apply its proposal
                    if result.error is not None:
                        remaining = [k for k in batch if layout.pose(k) is None]
                        if remaining:
                            out.notes.append({"code": "placement_refinement_failed", "instances": remaining,
                                              "error": _placement_error(result.error, instances=remaining, calls=result.calls),
                                              "message": f"final placement refinement failed ({result.error['code']}); previous decisions retained"})
                        break
                    notes = layout.refine(result, replacing)
                    out.notes += [{**note, "refinement_attempt": attempt + 1} for note in notes]
                    rejected = next((note for note in notes if note["code"] == "placement_refinement_retained"), None)
                    if rejected is None:
                        if correction is not None:
                            out.notes.append({"code": "placement_refinement_corrected", "instances": batch,
                                              "message": "the corrected final batch passed the original atomic placement guards"})
                        layout.summarize_all(again=True)
                        break
                    if attempt or rejected.get("diagnostics", {}).get("solve_interrupted"):
                        break
                    if result.raw_reply is None:
                        out.notes.append({"code": "placement_refinement_correction_skipped", "instances": batch,
                                          "reason": "complete_reply_unavailable",
                                          "message": "the complete failed reply is unavailable; previous decisions retained"})
                        break
                    correction = {"attempt": 1, "failed_reply": copy.deepcopy(result.raw_reply),
                                  "rejection": copy.deepcopy(rejected)}
    except Exception as exc:  # noqa: BLE001 - retain the initial solution; failures never become a completed coverage claim
        log.exception("final placement refinement of spec version %s", version.id)
        out.notes.append({"code": "placement_refinement_failed", "instances": [],
                          "message": f"final placement refinement could not finish ({getattr(exc, 'code', type(exc).__name__)}); "
                                     "initial decisions and coverage retained"})
    return made


def _refinement_batches(groups: list[list[str]], additional: list[str], insts: dict[Any, dict[str, Any]]) -> tuple[list[list[str]], list[list[str]]]:
    """Pack whole replacement groups; an oversized group cannot re-enter as a loose/additive support target."""
    if not groups:
        return _split(list(dict.fromkeys(additional)), insts), []
    reserved = {key for group in groups for key in group}
    skipped = [group for group in groups if len(group) > BATCH_INSTANCES]
    units = [group for group in groups if len(group) <= BATCH_INSTANCES]
    units += [[key] for key in dict.fromkeys(additional) if key not in reserved]
    batches: list[list[str]] = []
    current: list[str] = []
    for unit in units:
        if current and len(current) + len(unit) > BATCH_INSTANCES:
            batches.append(current)
            current = []
        current += unit
    if current:
        batches.append(current)
    return batches, skipped


def _exceeds_items(content: dict[str, Any], exceeds: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generation reliability round 2 (P, WP06): the instances the AI found beyond the installation positions the sources
    give (e.g. eight rivets for a plate with four holes) as ``placement`` items ``quantity_exceeds_positions`` — one per
    definition, with each instance's evidence (the model's reason and the source locators) — and one build note each.
    They stay in the assembly arranged for display (never installed); the spec quantity is for a person to fix."""
    from .spec_coverage import missing_item

    insts = {i.get("key"): i for i in content.get("instances") or [] if isinstance(i, dict)}
    defs = {d.get("key"): d for d in content.get("definitions") or [] if isinstance(d, dict)}
    by_def: dict[str, list[str]] = {}
    for k in exceeds:
        by_def.setdefault(str((insts.get(k) or {}).get("definition") or k), []).append(k)
    items, notes = [], []
    for dk, keys in by_def.items():
        d = defs.get(dk) or {}
        why = "; ".join(dict.fromkeys(exceeds[k]["reason"] for k in keys))
        parents = list(dict.fromkeys(str((insts.get(k) or {}).get("parent") or content.get("root")) for k in keys))
        items.append(missing_item(stage="placement", key=dk, name=str(d.get("name") or dk), kind=str(d.get("kind") or "part"),
                                  code="quantity_exceeds_positions",
                                  reason=clip(f"{len(keys)} instance(s) beyond the installation positions the sources give: {why}", 600),
                                  quantity=len(keys), instances=keys, recovery="fix_spec", definition=dk, parents=parents,
                                  evidence=[{"instance": k, "reason": exceeds[k]["reason"], "sources": list(exceeds[k].get("sources") or [])}
                                            for k in keys]))
        notes.append({"code": "placement_quantity_exceeds", "instances": keys,
                      "message": clip(f"{len(keys)} instance(s) of {d.get('name') or dk} ({', '.join(keys[:8])}) exceed the installation positions "
                                      f"the sources give ({why}); arranged for display by rule, not installed", 600)})
    return items, notes


def _batches(content: dict[str, Any], todo: list[str]) -> list[list[str]]:
    """The instances to place, grouped by parent (siblings in one call), in batches of at most BATCH_INSTANCES; the
    sub-assembly instances of a parent come first, in a batch of their own (the parts beside them are placed after). A
    parent's siblings beyond one batch are cut by :func:`_split`; small groups of other parents share a call."""
    insts = {i.get("key"): i for i in content.get("instances") or [] if isinstance(i, dict)}
    kinds = {d.get("key"): d.get("kind") for d in content.get("definitions") or [] if isinstance(d, dict)}
    groups: dict[Any, list[str]] = {}
    for k in todo:
        i = insts.get(k) or {}
        asm = kinds.get(i.get("definition")) == "assembly"
        groups.setdefault((i.get("parent") or content.get("root"), not asm), []).append(k)
    groups = dict(sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])))
    out: list[list[str]] = []
    cur: list[str] = []
    for (_, parts_group), keys in groups.items():
        if cur and not parts_group:  # sub-assembly instances never share a call with other instances
            out.append(cur)
            cur = []
        for part in _split(keys, insts):
            if cur and len(cur) + len(part) > BATCH_INSTANCES:
                out.append(cur)
                cur = []
            cur += part
        if not parts_group and cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _split(keys: list[str], insts: dict[Any, dict[str, Any]]) -> list[list[str]]:
    """The pending siblings of one parent (spec order) in as few chunks as BATCH_INSTANCES allows, as even as possible
    and cut between definitions (the instances of one definition — a bolt set — stay together unless they alone exceed
    a chunk). Spec order puts the base parts a BOM lists first in the first chunk; the later chunks are decided with
    them as known placements (generation reliability round 2, P: one 30-instance terminal set was declined wholesale)."""
    if len(keys) <= BATCH_INSTANCES:
        return [list(keys)]
    target = math.ceil(len(keys) / math.ceil(len(keys) / BATCH_INSTANCES))
    runs: list[list[str]] = []  # consecutive instances of one definition, none longer than a chunk
    for k in keys:
        if runs and len(runs[-1]) < target and (insts.get(runs[-1][-1]) or {}).get("definition") == (insts.get(k) or {}).get("definition"):
            runs[-1].append(k)
        else:
            runs.append([k])
    out: list[list[str]] = []
    cur: list[str] = []
    for run in runs:
        if cur and (len(cur) + len(run) > BATCH_INSTANCES or len(cur) >= target):
            out.append(cur)
            cur = []
        cur += run
    if cur:
        out.append(cur)
    return out


def _round(v: Any) -> list[float]:
    return [round(float(x), 3) + 0.0 for x in v]


def _placement_of(m: am.Matrix4) -> dict[str, list[float]]:
    """{translate, rotate} of a rigid transform (rotate in degrees about X, then Y, then Z: ``R = Rz·Ry·Rx``)."""
    s = max(-1.0, min(1.0, m[2][0]))
    ry = math.asin(-s)
    if abs(s) < 1 - 1e-9:
        rx, rz = math.atan2(m[2][1], m[2][2]), math.atan2(m[1][0], m[0][0])
    else:  # gimbal lock: the X rotation folds into Z
        rx, rz = 0.0, math.atan2(-m[0][1], m[1][1])
    return {"translate": _round((m[0][3], m[1][3], m[2][3])), "rotate": _round(math.degrees(a) for a in (rx, ry, rz))}


def _frame_matrix(d: Datum) -> am.Matrix4:
    """The rigid transform of a frame datum: its origin, its X (``x_dir``) and Z (``direction``) axes."""
    x, z = d.x_dir, d.direction
    y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    o = d.origin
    return ((x[0], y[0], z[0], o[0]), (x[1], y[1], z[1], o[1]), (x[2], y[2], z[2], o[2]), (0.0, 0.0, 0.0, 1.0))


def _corners(lo: Any, hi: Any, t: am.Matrix4) -> list[tuple[float, float, float]]:
    return [am.apply(t, (x, y, z)) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


def _outward(cands: list[tuple[Datum, list[float], list[float]]], lo: list[float], hi: list[float]) -> list[dict[str, Any]]:
    """A bounded vocabulary of child interfaces, including recessed contact faces.

    Prefer outer geometry, but reserve half the vocabulary for planes and spread those across children. Parallel
    feature axes of one child count as a complete group: the budget must not turn an eight-hole plate into seven.
    """
    tol = max(0.5, 0.01 * max(h - l_ for l_, h in zip(lo, hi)))
    ranked: list[tuple[int, int, Datum]] = []
    for d, clo, chi in cands:
        n, o = d.direction, d.origin
        k = max(range(3), key=lambda j: abs(n[j]))
        aligned = abs(n[k]) >= 0.95
        if d.kind == "axis":
            out = aligned and (abs(clo[k] - lo[k]) <= tol or abs(chi[k] - hi[k]) <= tol)
        elif d.kind == "plane":
            out = aligned and ((n[k] > 0 and abs(o[k] - hi[k]) <= tol) or (n[k] < 0 and abs(o[k] - lo[k]) <= tol))
        else:
            out = any(abs(o[j] - lo[j]) <= tol or abs(o[j] - hi[j]) <= tol for j in range(3))
        port = d.name.rsplit("/", 1)[-1].startswith(("port.", "if."))
        if out or port or d.kind in {"plane", "axis"}:
            ranked.append((0 if port else 1 if out else 2, len(ranked), d))
    picked: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    unique: list[Datum] = []
    for _, _, d in sorted(ranked, key=lambda x: (x[0], x[1])):
        # Coincident datums from distinct features/children remain different legal mate names.
        sig = (d.name, d.kind, tuple(round(x, 1) for x in d.origin), tuple(round(x, 3) for x in d.direction))
        if sig not in seen:
            seen.add(sig)
            unique.append(d)
    counts: dict[str, int] = {}
    planes = []
    for index, d in enumerate(unique):
        if d.kind == "plane":
            child = d.name.split("/", 1)[0]
            order = counts.get(child, 0)
            counts[child] = order + 1
            planes.append((order, index, d))
    reserved = [d for _, _, d in sorted(planes, key=lambda entry: entry[:2])][:MAX_AGGREGATE_DATUMS // 2]
    # A contact face's opposite face on the same feature is part of that interface package (for example the two
    # sides of a cover). Guards must not leave only the exterior face while hiding the seat used by fasteners.
    paired = [d for d in unique if d.kind == "plane" and d not in reserved and any(
        d.name.rpartition("/")[0] == r.name.rpartition("/")[0]
        and d.name.rsplit(".", 1)[0] == r.name.rsplit(".", 1)[0]
        and sum(a * b for a, b in zip(d.direction, r.direction)) < -0.9999 for r in reserved)]
    reserved += paired
    remainder = _datum_groups([d for d in unique if d not in reserved])
    remainder.sort(key=lambda group: group[0].kind != "axis")
    mandatory = [g for g in remainder if g[0].kind == "axis" or g[0].name.rsplit("/", 1)[-1].startswith(("port.", "if."))]
    optional = [g for g in remainder if g not in mandatory]
    # Axes can be recessed or arbitrarily rotated. Keep their complete named groups; the serialized call's
    # explicit character/model-window budget, not an incidental world-axis or item-order cutoff, bounds them.
    groups = mandatory + [[d] for d in reserved] + optional[:max(0, MAX_AGGREGATE_DATUMS - len(mandatory) - len(reserved))]
    for group in groups:
        for d in group:
            picked.append({"name": d.name, "kind": d.kind, "origin": _round(d.origin), "direction": _round(d.direction)})
    return picked


def _datum_groups(datums: list[Datum]) -> list[list[Datum]]:
    """Group only parallel feature axes on the same child; retain every actual constituent name."""
    names = {d.name for d in datums}
    groups: list[list[Datum]] = []
    axes: dict[tuple[Any, ...], list[Datum]] = {}
    for d in datums:
        if d.kind == "axis" and d.name.endswith(".axis") and d.name + "[0]" in names:
            continue  # an explicit pattern's zero alias, not a second physical axis
        owner, _, tail = d.name.rpartition("/")
        if d.kind == "axis" and (".axis" in tail or tail.startswith(("port.", "if."))):
            key = (owner, tuple(d.direction), tail.startswith(("port.", "if.")))
            if key not in axes:
                axes[key] = []
                groups.append(axes[key])
            axes[key].append(d)
        else:
            groups.append([d])
    return groups


def _compact(datums: list[Datum], limit: int = MAX_DATUMS, *, required: set[str] | None = None) -> list[list[Any]]:
    """Preserve complete axes/interfaces and explicit dependencies; ``limit`` only trims optional planes/points.

    Mandatory entries may exceed the soft item budget. The caller must enforce its whole-context budget, failing
    explicitly if needed rather than hiding a feature or a datum an existing relation still names.
    """
    rank = {"frame": 1, "axis": 2, "plane": 3, "point": 4}

    def order(d: Datum) -> tuple[int, int]:
        tail = d.name.rsplit("/", 1)[-1]
        return (0 if tail.startswith(("port.", "if.")) else rank.get(d.kind, 5), 0)

    entries: list[tuple[tuple[int, int], list[Any], bool]] = []
    groups = _datum_groups(datums)
    grouped_names = {d.name for ds in groups for d in ds}
    groups += [[d] for d in datums if d.name in (required or set()) and d.name not in grouped_names]
    for ds in groups:
        mandatory = any(d.kind in {"axis", "frame"} or d.name in (required or set())
                        or d.name.rsplit("/", 1)[-1].startswith(("port.", "if.")) for d in ds)
        if len(ds) == 1:
            d = ds[0]
            entries.append((order(d), [d.name, d.kind, _round(d.origin), _round(d.direction)], mandatory))
        else:
            matches = [re.fullmatch(r"(.+)\.axis\[(\d+)\]", d.name) for d in ds]
            if all(matches) and len({m.group(1) for m in matches if m}) == 1 \
                    and {int(m.group(2)) for m in matches if m} == set(range(len(ds))):
                ds = sorted(ds, key=lambda d: int(d.name.rsplit("[", 1)[1][:-1]))
                label: str | list[str] = f"{ds[0].name.rsplit('[', 1)[0]}[0..{len(ds) - 1}]"
            else:
                label = [d.name for d in ds]
            entries.append(((order(ds[0])[0], -1),
                            [label, "axes", [_round(d.origin) for d in ds], _round(ds[0].direction)], mandatory))
    ordered = sorted(entries, key=lambda x: x[0])
    budget = max(0, limit - sum(needed for _, _, needed in ordered))
    selected = []
    for _, entry, needed in ordered:
        if needed or budget:
            selected.append(entry)
            if not needed:
                budget -= 1
    return selected


class _Layout:
    """The assembly frames of one Turbo build (generation supplement G; reliability L: summarised before the calls,
    which go top-down; round 3, B: the AI mates are solved here, parent by parent, as the build will): the height of
    every kept assembly definition, the poses the spec fixes (a stated placement, the lone child at its parent's origin)
    or a source states (``fixed``), the kept spec relations and the AI mates per parent solved with
    :func:`app.cad.mates.solve` (positions only for the instances tied to the parent frame), and, per assembly
    definition, its bounding box, envelope (size estimate) and datums in its own frame (:meth:`summarize_all`), and where
    its frame sits in the root frame (:meth:`frames`). Pure reads; nothing is written."""

    def __init__(self, db: Session, content: dict[str, Any], bindings: dict[str, dict[str, Any]], gaps: dict[str, Any],
                 deadline: float | None):
        from .assemblies import spec_tree

        self.db, self.bindings, self.deadline, self.content = db, bindings, deadline, content
        self.tree = spec_tree(content)
        self.root: str = self.tree["root"]
        self.defs = {d["key"]: d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}
        kept = set(gaps.get("kept") or [])
        self.insts = {i["key"]: i for i in self.tree["instances"] if i["key"] in kept}
        self.children: dict[str, list[str]] = {}
        for k, i in self.insts.items():
            self.children.setdefault(i.get("parent") or self.root, []).append(k)
        self.at_origin = set(gaps.get("at_origin") or [])
        # A producer without rule-origin information explicitly supplies its own floating groups.
        self._rule_origins = "at_origin" in gaps
        self.mated = set(gaps.get("mated") or [])
        #: round 2 (P): floating relation groups — anchor → its members; member → its anchor. Once the anchor is
        #: connected to the parent frame, the spec relations carry the members (one solve).
        self.groups: dict[str, list[str]] = {}
        self.anchor_of: dict[str, str] = {}
        for g in gaps.get("floating") or []:
            a = g.get("anchor") if isinstance(g, dict) else None
            if a in self.insts:
                self.groups[a] = [m for m in g.get("members") or [] if m in self.insts]
                self.anchor_of.update({m: a for m in self.groups[a] if m != a})
        self.rels: dict[str, list[dict[str, Any]]] = {}
        for r in gaps.get("relations") or []:
            a = (r.get("a") or {}).get("instance")
            if a in self.insts:
                self.rels.setdefault(self.insts[a].get("parent") or self.root, []).append(r)
        #: review of round 3: spec relations putting an instance on ONE axis of a pattern (the call picks the axis)
        self.choices: list[dict[str, Any]] = [c for c in gaps.get("choices") or [] if c.get("instance") in self.insts]
        #: round 3 (B): the AI mates per parent (relations, ``source: ai_turbo``), the ones the solve dropped, the
        #: positions a source states (instance → transform / decision)
        self.ai: dict[str, list[dict[str, Any]]] = {}
        self.dropped: list[dict[str, Any]] = []
        self.fixed: dict[str, am.Matrix4] = {}
        self.fixed_info: dict[str, dict[str, Any]] = {}
        self._seq = 0
        #: assembly definition key → {"bbox": {min, max} | None, "datums": [...], "of": positioned children, "children": n}
        self.aggregates: dict[str, dict[str, Any]] = {}
        self._solved: dict[str, tuple[dict[str, am.Matrix4], Any]] = {}
        self._revs: dict[str, ModelRevision | None] = {}
        self._datums: dict[str, list[Datum]] = {}
        self._heights: dict[str, int] = {}
        self._reads = 0
        self.stopped = False
        self._verified: dict[str, tuple[str, tuple[dict[str, am.Matrix4], Any]]] = {}
        self.runtime_notes: dict[str, dict[str, Any]] = {}

    # --- structure -------------------------------------------------------------------------------------------------

    def is_asm(self, dk: str | None) -> bool:
        return dk == self.root or (self.defs.get(dk or "") or {}).get("kind") == "assembly"

    def parent(self, k: str) -> str:
        return (self.insts.get(k) or {}).get("parent") or self.root

    def height(self, dk: str, depth: int = 0) -> int:
        """0 for an assembly of parts only, else 1 + the highest assembly it holds (a cycle counts as 0)."""
        if dk in self._heights:
            return self._heights[dk]
        if depth > _MAX_LEVELS:
            return 0
        self._heights[dk] = 0  # a cycle stops here
        subs = {self.insts[k].get("definition") for k in self.children.get(dk) or [] if self.is_asm(self.insts[k].get("definition"))}
        h = 1 + max(self.height(str(s), depth + 1) for s in subs) if subs else 0
        self._heights[dk] = h
        return h

    def stages(self, content: dict[str, Any], todo: list[str]) -> list[tuple[int, list[list[str]]]]:
        """(height, batches) from the root down to the deepest assemblies (generation reliability L: the main
        sub-assemblies are placed in the root frame first, then their insides); only heights with pending instances."""
        top = self.height(self.root)
        by: dict[int, list[str]] = {}
        for k in todo:
            by.setdefault(self.height(self.parent(k)) if k in self.insts else top, []).append(k)
        return [(h, _batches(content, by[h])) for h in sorted(by, reverse=True)]

    def child_interfaces(self, parent: str) -> set[tuple[Any, ...]]:
        """Positioned-child interface signatures: a moved existing datum is new evidence too (never envelopes)."""
        return {(k, str(d["name"]), *d["origin"], *d["direction"]) for k in self.children.get(parent) or []
                for d in (self.aggregates.get(str(self.insts[k].get("definition"))) or {}).get("datums") or []}

    def leaves(self, dk: str, depth: int = 0) -> int:
        """Part instances under an assembly definition (each path once)."""
        if depth > _MAX_LEVELS:
            return 0
        return sum(self.leaves(str(self.insts[k].get("definition")), depth + 1) if self.is_asm(self.insts[k].get("definition")) else 1
                   for k in self.children.get(dk) or [])

    # --- geometry --------------------------------------------------------------------------------------------------

    def _late(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline - PLACEMENT_RESERVE_S

    def rev(self, dk: str | None) -> ModelRevision | None:
        if not dk:
            return None
        if dk not in self._revs:
            b = self.bindings.get(dk)
            self._revs[dk] = self.db.get(ModelRevision, uuid.UUID(str(b["revision_id"]))) if b and b.get("revision_id") else None
        return self._revs[dk]

    def part_datums(self, dk: str, *, force: bool = False) -> list[Datum] | None:
        """The named datums of a built part (its own frame); None when unknown or not read (budget / runtime)."""
        from . import assemblies as asm_svc

        if dk in self._datums:
            return self._datums[dk]
        if self.stopped or (self.deadline is not None and time.monotonic() >= self.deadline - FINALIZATION_RESERVE_S):
            return None  # even forced datum rebuilds must leave time to persist the partial assembly
        rev = self.rev(dk)
        if rev is None:
            return None
        if not force and str(rev.id) not in asm_svc._DATUMS:  # reading may rebuild the part
            if self._reads >= MAX_AGGREGATE_DATUM_PARTS or self._late():
                return None
            self._reads += 1
        try:
            found = list(asm_svc.part_datums(self.db, rev))
        except Exception:  # noqa: BLE001 - datums only help the decision
            found = []
        self._datums[dk] = found
        return found

    def box(self, dk: str | None, *, envelope: bool = False) -> tuple[list[float], list[float]] | None:
        """The bounding box of a definition in its own frame: a built part's, or an assembly's aggregate (``envelope``:
        its size estimate, counting the children without a position yet, when it has one)."""
        if self.is_asm(dk):
            agg = self.aggregates.get(str(dk)) or {}
            b = (agg.get("envelope") or agg.get("bbox")) if envelope else agg.get("bbox")
            return (list(b["min"]), list(b["max"])) if b else None
        rev = self.rev(dk)
        b = (rev.metrics or {}).get("bbox") if rev is not None and isinstance(rev.metrics, dict) else None
        return (list(b["min"]), list(b["max"])) if b and b.get("min") and b.get("max") else None

    def datums(self, dk: str, depth: int = 0, *, force: bool = True) -> dict[str, Datum]:
        """The datums a mate BINDS on an instance of ``dk`` — exactly the build's (review of round 3: one function, no
        second copy): a part's built datums (``assemblies.part_datums``, ``origin`` included: a spec relation may name
        it); a sub-assembly's (``assemblies.assembly_datums``): its frame datums, its interfaces and the datums of the
        contents positioned when it was summarised (:meth:`summarize_all`; contents placed later become available to
        unresolved instances above only after a refinement pass refreshes the summary)."""
        from .assemblies import DATUM_DEPTH, assembly_datums, interfaces_of, part_frame_datums

        if self.is_asm(dk):
            kids = [] if depth > DATUM_DEPTH else [((k,), self.datums(cdk, depth + 1, force=False).values(), t)
                                                   for k, cdk, t in (self.aggregates.get(dk) or {}).get("placed") or []]
            return assembly_datums(interfaces_of(self.content, dk), kids)
        found = self.part_datums(dk, force=force)
        return part_frame_datums(found) if found is not None else {}

    def shown(self, dk: str) -> list[Datum]:
        """The datums the context lists for ``dk`` — the vocabulary of the mates, a subset of :meth:`datums`: a part's
        frame and feature datums (all of them); a sub-assembly's frame datums, interfaces and the OUTWARD datums of its
        positioned contents."""
        from ..cad.datums import frame_datums, interface_datums
        from .assemblies import interfaces_of

        if self.is_asm(dk):
            agg = [Datum(name=str(d["name"]), kind=d["kind"], origin=tuple(d["origin"]), direction=tuple(d["direction"]))  # type: ignore[arg-type]
                   for d in (self.aggregates.get(dk) or {}).get("datums") or []]
            return frame_datums() + interface_datums(interfaces_of(self.content, dk)) + agg
        return list(self.datums(dk).values())

    def stated(self, k: str) -> am.Matrix4 | None:
        """The pose the spec or a source fixes without a relation: its placement, a stated position a call accepted, or
        the lone child at its parent's origin."""
        from .assemblies import _placement_matrix

        self._refresh_origins()
        i = self.insts.get(k) or {}
        if i.get("placement"):
            return _placement_matrix(i["placement"])
        if k in self.fixed:
            return self.fixed[k]
        return am.IDENTITY if k in self.at_origin else None

    def _refresh_origins(self) -> None:
        """Reapply the build's frame rules to the CURRENT relations, including accepted fixed source positions.

        A whole-content group loses its rule anchor as soon as it connects to a parent frame. That connection may
        still leave translation free; retaining the old identity would hide this freedom from placement calls.
        Derived state is recomputed after rollback too, rather than becoming another mutable accepted decision.
        """
        if not self._rule_origins:
            return
        from .assemblies import _relation_groups, relation_ends

        rels = [r for by_parent in (self.rels, self.ai) for entries in by_parent.values() for r in entries]
        related = {e for r in rels for e in relation_ends(r)}
        insts = {k: {**i, **({"placement": _placement_of(self.fixed[k])} if k in self.fixed else {})}
                 for k, i in self.insts.items()}
        siblings: dict[str, int] = {}
        for i in self.tree["instances"]:
            parent = i.get("parent") or self.root
            siblings[parent] = siblings.get(parent, 0) + 1
        origins = {k for k, i in insts.items() if not i.get("placement") and k not in related
                   and siblings.get(self.parent(k)) == 1}
        _relation_groups(insts, list(insts), rels, origins, rank={k: n for n, k in enumerate(insts)}, root=self.root)
        self.at_origin = origins

    def frame(self, parent: str, ref: str) -> tuple[dict[str, Datum] | None, str]:
        """The datums of the assembly frame ``ref`` (``@<key>``) in ``parent``'s frame (``assemblies.frame_datums`` with
        the positions known so far)."""
        from .assemblies import frame_datums

        return frame_datums(self.content, parent, ref, self.pose, tree=self.tree)

    def solve(self, parent: str) -> tuple[dict[str, am.Matrix4], Any]:
        """(poses of the children POSITIONED in ``parent``'s frame, the solve result | None), solved once per parent
        exactly as the build will (review of round 3: one solve, no second copy) — ``assemblies.held_fixed`` says which
        children are fixed, :meth:`datums` what each binds, ``assemblies.solve_level`` solves (an assembly frame a
        relation / mate names takes part with the datums it carries, ``frame``; AI mates rank below the spec relations;
        Turbo) and :func:`app.cad.mates.grounded` says which children the solve positions — no free translation left (a
        held turn about an axis is fine; connected is not positioned)."""
        if parent in self._solved:
            return self._solved[parent]
        from ..cad import mates as M
        from .assemblies import frame_side, held_fixed, is_frame_ref, solve_level

        kids = self.children.get(parent) or []
        poses = {k: t for k in kids if (t := self.stated(k)) is not None}
        self._solved[parent] = (poses, None)  # a frame above asked for while this level solves sees the stated poses
        rels = list(self.rels.get(parent) or []) + list(self.ai.get(parent) or [])
        if not rels or not kids:
            return self._solved[parent]
        try:
            ends = {e for r in rels for e in ((r.get("a") or {}).get("instance"), (r.get("b") or {}).get("instance")) if e}
            held = held_fixed(kids, set(poses), rels, set(self.groups) & set(self.fixed))
            insts = [M.SolveInstance(iid=k, transform=poses.get(k, am.IDENTITY), fixed=k in held, unplaced=k not in poses,
                                     datums=self.datums(str(self.insts[k].get("definition"))) if k in ends else None) for k in kids]
            mates = []
            for n, r in enumerate(rels):
                b = dict(r["b"]) if r.get("b") else None
                if is_frame_ref(b):
                    found, _ = self.frame(parent, str(b["instance"]))
                    side = frame_side(found, str(r.get("kind")), str(b.get("datum") or "origin")) if found is not None else None
                    if side is None:
                        continue  # not reachable yet (an instance above has no position): the relation waits
                    b["frame"] = side
                mates.append(M.SolveMate(mate_id=str(r.get("key") or f"R{n}"), kind=str(r.get("kind")), a=dict(r.get("a") or {}),
                                         b=b, params=dict(r.get("params") or {}), priority=int(r.get("priority") or 100), position=n))
            signature = repr((insts, mates))  # includes actual datum origins/directions and every fixed pose
            old = self._verified.get(parent)
            if old is not None and old[0] == signature:
                self._solved[parent] = old[1]
                return old[1]
            if self.stopped:
                return self._solved[parent]
            res = solve_level(insts, mates, turbo=True)
            tied = M.grounded(res)
            poses.update({k: res.transforms[k] for k in kids if k in tied})
            self._solved[parent] = (poses, res)
            if res.converged:
                self._verified[parent] = (signature, self._solved[parent])
            for note in res.notes:
                if note.get("code") in ("solver_runtime_budget", "solver_cancelled"):
                    self.runtime_notes[parent] = note
        except Exception:  # noqa: BLE001 - the build solves (and reports) the relations again itself
            log.warning("placement layout: relations of %s not solved", parent, exc_info=True)
        return self._solved[parent]

    def pose(self, k: str) -> am.Matrix4 | None:
        """Where an instance sits in its parent's frame: stated, or solved and tied to the parent frame; None when it is
        arranged, waits for its connection, or sits in a floating group (positioned only relative to its siblings)."""
        t = self.stated(k)
        return t if t is not None else self.solve(self.parent(k))[0].get(k)

    def ai_mates_of(self, k: str) -> list[dict[str, Any]]:
        return [r for r in self.ai.get(self.parent(k)) or [] if (r.get("a") or {}).get("instance") == k]

    def replaceable_mates(self, key: str) -> list[dict[str, Any]]:
        """Estimated relations of a positioned, non-fixed instance; all other relations remain protected."""
        if key not in self.insts or self.stated(key) is not None or self.pose(key) is None:
            return []
        return [r for r in self.ai_mates_of(key) if (r.get("decision") or {}).get("basis_kind") == "estimated"]

    def _estimated_group(self, key: str) -> bool:
        """A positioned set of AI mates with no stated, derived or spec connection to protect."""
        from .assemblies import relation_ends

        mine = self.ai_mates_of(key)
        return bool(mine and self.stated(key) is None and self.pose(key) is not None
                    and all((r.get("decision") or {}).get("basis_kind") == "estimated" for r in mine)
                    and not any(key in relation_ends(r) for r in self.rels.get(self.parent(key)) or []))

    def replaceable_estimate(self, key: str) -> bool:
        """An entirely estimated offset that can seed reconsideration when new child geometry is available."""
        return self._estimated_group(key) and any(
            abs(float((r.get("params") or {}).get("distance") or 0)) > 1e-6 for r in self.ai_mates_of(key))

    def replacement_groups(self, seeds: list[str], candidates: list[str]) -> list[list[str]]:
        """Include downstream siblings with replaceable estimates as indivisible groups, including mixed mounts.

        Only accepted relation endpoints establish dependency. Protected relations can connect a downstream mixed
        mount to its support, but only that mount's estimated mates may change. New child evidence is required for
        the original seeds, so a disconnected zero-offset estimate cannot start another modeling pass.
        """
        parents = {self.parent(key) for key in seeds}
        eligible = {key for key in candidates if self.parent(key) in parents and self.replaceable_mates(key)}
        followers: dict[str, set[str]] = {}
        for key in eligible:
            refs = {str((r.get("b") or {}).get("instance") or "") for r in self.ai_mates_of(key)}
            # Spec endpoints have no dependent/support ordering, unlike accepted AI mates whose a is the target.
            for relation in self.rels.get(self.parent(key)) or []:
                ends = {str((relation.get(side) or {}).get("instance") or "") for side in ("a", "b")}
                if key in ends:
                    refs |= ends - {key}
            for ref in refs:
                if ref in eligible and self.parent(ref) == self.parent(key):
                    followers.setdefault(ref, set()).add(key)
        reached: set[str] = set()
        todo = [key for key in seeds if key in eligible]
        while todo:
            key = todo.pop()
            if key not in reached:
                reached.add(key)
                todo.extend(followers.get(key, set()) - reached)
        # Shared descendants and cycles join their seeds into one atomic replacement component.
        neighbors = {key: set() for key in reached}
        for ref in reached:
            for key in followers.get(ref, set()) & reached:
                neighbors[ref].add(key)
                neighbors[key].add(ref)
        groups: list[list[str]] = []
        assigned: set[str] = set()
        for seed in seeds:
            if seed not in reached or seed in assigned:
                continue
            component: set[str] = set()
            todo = [seed]
            while todo:
                key = todo.pop()
                if key not in component:
                    component.add(key)
                    todo.extend(neighbors[key] - component)
            assigned |= component
            groups.append([key for key in dict.fromkeys(candidates) if key in component])
        return groups

    def supports(self, keys: list[str]) -> list[str]:
        """Positioned support siblings on the pending group's connection path, eligible for additive clocking."""
        seen, todo, found = set(keys), list(keys), []
        while todo:
            key = todo.pop(0)
            for rel in self.ai_mates_of(key):
                ref = str((rel.get("b") or {}).get("instance") or "")
                if ref in seen or ref not in self.insts or self.parent(ref) != self.parent(key):
                    continue
                seen.add(ref)
                todo.append(ref)
                if self.pose(ref) is not None and self.stated(ref) is None:
                    found.append(ref)
        return found

    def feedback(self, parent: str) -> dict[str, Any] | None:
        """Actual joint solver status, including supports whose own spin carries pending off-centre children."""
        _, solved = self.solve(parent)
        if solved is None:
            return None
        related = {side["instance"] for rel in (self.rels.get(parent) or []) + (self.ai.get(parent) or [])
                   for side in (rel.get("a"), rel.get("b")) if side}
        return {"parent": parent,
                "instances": [{"key": key, **solved.instances.get(key, {})} for key in self.children.get(parent) or [] if key in related],
                "rejected_mates": [{"kind": r["kind"], "a": r["a"], "b": r["b"], "params": r.get("params", {}),
                                    "reason": r["dropped"]} for r in self.dropped
                                   if self.parent(r["a"]["instance"]) == parent][-8:]}

    def hint(self, k: str) -> tuple[dict[str, list[float]] | None, str] | None:
        """(placement, how) of an instance without a stated placement the spec, a source or an earlier call positions
        (for the context); a floating group's anchor and its members say so (placement None while not connected), and so
        does an instance the spec relations leave free to move."""
        from .assemblies import relation_ends

        if (self.insts.get(k) or {}).get("placement"):
            return None
        t = self.pose(k)
        if k in self.fixed:
            return _placement_of(self.fixed[k]), "a source states its position (accepted earlier in this build; do not move)"
        if k in self.at_origin:
            return {"translate": [0.0, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0]}, (
                "the only child: at its parent's origin" if k not in self.mated else
                "anchors the relation group that is its parent's whole content: at the parent's origin (do not move)")
        mine = self.ai_mates_of(k)
        if mine:
            said = "; ".join(f"{r['kind']} {r['a']['datum']} → {r['b']['instance']} {r['b']['datum']}" for r in mine[:4])
            return ((_placement_of(t), f"connected earlier in this build ({said}): build on it, do not connect it again") if t is not None
                    else (None, f"connected earlier in this build ({said}) but not tied to the parent frame yet"))
        if k in self.groups:
            others = [m for m in self.groups[k] if m != k]
            return None, (f"anchors the group {', '.join(others[:8])} joined to it by the spec relations, none of them placed in the "
                          "parent frame: connect it first; the solver will check which members still need connections")
        if k in self.anchor_of:
            a = self.anchor_of[k]
            if t is not None:
                return _placement_of(t), f"joined to {a} by the spec relations: solved in the parent frame (do not move)"
            if self.pose(a) is not None:
                return None, (f"joined to grounded {a} by the spec relations, but the solver still leaves this member free: "
                              "add the connections that fix its remaining motion; keep its existing source relations")
            return None, (f"joined to {a} by the spec relations: anchor {a} first; this member is not yet positioned, "
                          "and those relations may leave relative motion")
        if k in self.mated and t is None:
            said = "; ".join(f"{r.get('kind')} {r['a'].get('datum')} → {r['b'].get('instance')} {r['b'].get('datum')}"
                             for r in self.rels.get(self.parent(k)) or [] if k in relation_ends(r))[:300]
            return None, (f"the spec relations join it ({said}) but leave it free to move: add the connections that fix its "
                          "position (they are kept; do not repeat them)")
        return (_placement_of(t), "solved from the spec relations (do not move)") if t is not None and k in self.mated else None

    # --- decisions -------------------------------------------------------------------------------------------------

    def refine(self, result: agent.PlacementOutcome, replace: list[str]) -> list[dict[str, Any]]:
        """Transactionally replace estimated relations; source/spec and additive support mates remain exact."""
        previous, previous_rels, previous_dropped, previous_fixed, previous_info = copy.deepcopy(
            (self.ai, self.rels, self.dropped, self.fixed, self.fixed_info))
        positioned = {k for k in self.insts if self.pose(k) is not None}
        previous_solved, previous_verified, previous_seq = copy.deepcopy((self._solved, self._verified, self._seq))
        replace_mates = {key: self.replaceable_mates(key) for key in dict.fromkeys(replace)}
        replace_keys = {m["key"] for mates in replace_mates.values() for m in mates}
        protected = {m["key"]: m for mates in previous.values() for m in mates if m["key"] not in replace_keys}
        protected_spec = [(parent, str(r.get("key") or f"R{n}"), r)
                          for parent, relations in previous_rels.items() for n, r in enumerate(relations)]

        def equation(mate: dict[str, Any]) -> tuple[Any, ...]:
            # A reply may repeat a protected relation; that is neither its replacement nor permission to remove estimates.
            params = {k: v for k, v in (mate.get("params") or {}).items() if v is not None
                      and not (k in ("distance", "flip") and not v)}
            return mate.get("kind"), mate.get("a"), mate.get("b"), params

        kept_equations = [equation(r) for r in protected.values()] + [equation(r) for _, _, r in protected_spec]
        proposal = copy.copy(result)
        proposal.mates = [m for m in result.mates if equation(m) not in kept_equations]
        # No evidence-backed numeric positions in this pass: use the existing source position and named mates.
        proposal.fixed = {}
        touched = {m["a"]["instance"] for m in proposal.mates}
        missing = sorted(set(replace) - touched)
        ineligible = sorted(key for key, mates in replace_mates.items() if not mates)
        if missing or ineligible:
            return [{"code": "placement_refinement_retained", "instances": replace,
                     "message": "refinement did not supply a replacement for every estimate; previous decisions retained",
                     "diagnostics": {"missing_replacements": missing, "reply_problems": list(result.problems),
                                     "nonreplaceable_instances": ineligible, "unplaced": dict(result.unplaced)}}]

        def restore() -> None:
            self.ai, self.rels, self.dropped = previous, previous_rels, previous_dropped
            self.fixed, self.fixed_info = previous_fixed, previous_info
            self._solved, self._verified, self._seq = previous_solved, previous_verified, previous_seq

        for parent, mates in self.ai.items():
            self.ai[parent] = [m for m in mates if m["key"] not in replace_keys]
        self._solved.clear()
        try:
            notes = self.accept(proposal)
            retained = {m["key"]: m for mates in self.ai.values() for m in mates}
            lost_protected = [m for key, m in protected.items() if retained.get(key) != m]
            for parent, key, relation in protected_spec:
                old_solve = previous_solved.get(parent, (None, None))[1]
                old_status = (old_solve.mates.get(key) or {}).get("status") if old_solve else None
                solved = self.solve(parent)[1]
                status = (solved.mates.get(key) or {}).get("status") if solved else None
                bad = {None, "dropped", "conflict", "broken", "invalid"}
                if relation not in self.rels.get(parent, []) or (old_status not in bad and status in bad):
                    lost_protected.append(relation)
            lost_position = sorted(k for k in positioned | set(replace) if self.pose(k) is None)
            interrupted = any(note["code"] == "placement_batch_not_applied" for note in notes)
            if lost_protected or lost_position or interrupted:
                diagnostics = {"reply_problems": list(dict.fromkeys(result.problems + [
                    problem for note in notes for problem in note.get("problems", [])])),
                    "unplaced": dict(result.unplaced), "lost_position": lost_position,
                    "lost_protected_mates": copy.deepcopy(lost_protected),
                    "rejected_mates": copy.deepcopy(self.dropped[len(previous_dropped):]),
                    "trial_notes": copy.deepcopy(notes), "solve_interrupted": interrupted,
                    "lost_instance_states": [{"key": key, "parent": self.parent(key),
                        **copy.deepcopy(solved.instances.get(key, {}))} for key in lost_position
                        if (solved := self._solved.get(self.parent(key), (None, None))[1]) is not None]}
                restore()
                return [{"code": "placement_refinement_retained", "instances": replace,
                         "message": ("refinement solve was interrupted; previous decisions retained" if interrupted else
                                     "refinement would drop a protected connection or leave a positioned instance loose; previous decisions retained"),
                         "diagnostics": diagnostics}]
        except Exception:
            restore()
            raise
        if replace:
            notes.append({"code": "placement_refined", "instances": replace,
                          "message": "early estimated offsets reconsidered against final positioned child geometry"})
        return notes

    def _key(self) -> str:
        self._seq += 1
        return f"AI-{self._seq:03d}"

    def accept(self, res: agent.PlacementOutcome) -> list[dict[str, Any]]:
        """Bind one reply: the stated positions, then the mates as relations of the parent of their instance (a frame
        side must be reachable from it, a sibling side must share it); every parent touched is solved again — an AI
        mate the solver drops as conflicting, or cannot bind, is taken out (a dropped decision / a problem). Returns the
        build notes."""
        from .assemblies import AI_MATE_PRIORITY, is_frame_ref

        saved = copy.deepcopy((self.ai, self.fixed, self.fixed_info, self.dropped, self._solved, self._verified, self._seq))
        notes: list[dict[str, Any]] = []
        problems = list(res.problems)
        touched: set[str] = set()
        for k, f in res.fixed.items():
            if k not in self.insts or f.get("translate") is None:
                continue
            # review of round 3: the coordinates are given in the frame the entry names (the parent's or one above it,
            # mapped down the one instance path) — never read as local ones
            p, ref = self.parent(k), str(f.get("frame") or f"@{self.parent(k)}")
            found, why = self.frame(p, ref)
            origin = (found or {}).get("origin")
            if origin is None:
                problems.append(f"{k}: fixed in {ref} — {why or 'the frame has no origin'}")
                continue
            stated = am.placement(tuple(f["translate"]), tuple(f["rotate"]))  # type: ignore[arg-type]
            self.fixed[k] = am.compose(_frame_matrix(origin), stated)
            self.fixed_info[k] = {**{x: y for x, y in f.items() if x != "frame"}, **_placement_of(self.fixed[k]),
                                  **({"stated_in": {"frame": ref, "at": list(f["translate"]), "rotate": list(f["rotate"])}}
                                     if ref != f"@{p}" else {})}
            touched.add(p)
        for m in res.mates:
            a, b = m["a"]["instance"], m["b"]["instance"]
            if a not in self.insts or a in self.fixed:
                continue
            p = self.parent(a)
            if is_frame_ref(m["b"]):
                found, why = self.frame(p, b)
                if found is None:
                    problems.append(f"{a}: {m['kind']} to {b} — {why}")
                    continue
            elif b not in self.insts or self.parent(b) != p:
                problems.append(f"{a}: {m['kind']} to {b} — not a sibling in {p}")
                continue
            self.ai.setdefault(p, []).append({"key": self._key(), "kind": m["kind"], "a": dict(m["a"]), "b": dict(m["b"]),
                                              "params": dict(m.get("params") or {}), "source": "ai_turbo",
                                              "priority": AI_MATE_PRIORITY.get(m.get("basis_kind"), AI_MATE_PRIORITY["estimated"]),
                                              "decision": {x: m[x] for x in ("trigger", "basis_kind", "basis", "alternatives", "affected", "sources")}})
            touched.add(p)
        for p in touched:
            for _ in range(len(self.ai.get(p) or []) + 1):
                self._solved.clear()  # positions above feed the frames below: everything is solved again on demand
                _, solved = self.solve(p)
                if solved is None:
                    break
                if any(n.get("code") in ("solver_runtime_budget", "solver_cancelled") for n in solved.notes):
                    # The new reply was recorded already. Its unverified constraints must not erase a prior valid batch.
                    self.ai, self.fixed, self.fixed_info, self.dropped, self._solved, self._verified, self._seq = saved
                    return [{"code": "placement_batch_not_applied", "instances": sorted(
                        set(res.fixed) | {str(m["a"]["instance"]) for m in res.mates}),
                        "message": "the new placement batch exceeded its solve budget; the previous verified decisions were retained"}]
                bad = [r for r in self.ai.get(p) or [] if (solved.mates.get(r["key"]) or {}).get("status") in ("dropped", "conflict", "broken", "invalid")]
                if not bad:
                    break
                for r in bad:
                    st = solved.mates[r["key"]]
                    self.ai[p].remove(r)
                    if st["status"] in ("dropped", "conflict"):
                        why = next((x["basis"] for x in solved.assumptions if x.get("subject") == r["key"]), st.get("message") or "conflict")
                        self.dropped.append({**r, "dropped": f"the solver dropped it: {why}", "solver": copy.deepcopy(st)})
                        notes.append({"code": "placement_mate_dropped", "instances": [r["a"]["instance"]],
                                      "message": f"AI mate {r['key']} ({r['kind']} {r['a']['instance']}.{r['a']['datum']} – {r['b']['instance']}."
                                                 f"{r['b']['datum']}) conflicts with higher-priority constraints ({why}); not applied"})
                    else:
                        problems.append(f"{r['a']['instance']}: {r['kind']} {r['key']} — {st.get('message') or st['status']}")
        self._solved.clear()
        if problems:
            notes.append({"code": "placement_reply_problems", "instances": [],
                          "message": clip("the AI placement reply had entries that were not used: " + "; ".join(problems[:12]), 900),
                          "problems": problems})
        return notes

    def reuse(self, rev: AssemblyRevision | None, pending: list[str]) -> list[str]:
        """The AI placement mates (and source-stated positions) the partial revision a retry continues decided for
        instances still pending, taken over without a call; their pending instances. Numeric AI positions of a revision
        made before round 3 (computed by the model) are not reused."""
        from .assemblies import AI_MATE_PRIORITY

        if rev is None:
            return []
        out: list[str] = []
        tag = f"(decided for A{rev.rev_no}, reused)"
        for a in rev.assumptions or []:
            if not isinstance(a, dict) or a.get("author") != "ai_turbo" or a.get("subject") not in pending:
                continue
            k, adopted = str(a["subject"]), a.get("adopted")
            basis = str(a.get("basis") or "")
            decision = {"trigger": a.get("trigger") if a.get("trigger") in ("missing", "ambiguous", "conflict") else "missing",
                        "basis_kind": a.get("basis_kind") if a.get("basis_kind") in agent.BASIS_KINDS else "estimated",
                        "basis": basis if "reused)" in basis else f"{basis} {tag}".strip(), "sources": list(a.get("sources") or []),
                        "alternatives": [str(x) for x in a.get("alternatives") or []],
                        "affected": [x for x in a.get("affected") or [] if x in self.insts and x != k]}
            if a.get("field") == "placement" and a.get("basis_kind") == "stated" and isinstance(adopted, dict) and k in self.insts:
                t, r = agent._triple(adopted.get("translate"), agent.MAX_TRANSLATE_MM), agent._triple(adopted.get("rotate"), agent.MAX_ROTATE_DEG)
                if t is not None and r is not None:
                    self.fixed[k], self.fixed_info[k] = am.placement(tuple(t), tuple(r)), {"translate": t, "rotate": r, **decision}  # type: ignore[arg-type]
                    out.append(k)
            elif a.get("field") == "mate" and isinstance(adopted, dict) and adopted.get("kind") and k in self.insts:
                rel = {"key": str(adopted.get("mate") or self._key()), "kind": adopted["kind"], "a": dict(adopted.get("a") or {}),
                       "b": dict(adopted.get("b") or {}), "params": dict(adopted.get("params") or {}), "source": "ai_turbo",
                       "priority": AI_MATE_PRIORITY.get(decision["basis_kind"], AI_MATE_PRIORITY["estimated"]), "decision": decision}
                if rel["a"].get("instance") == k and rel["b"].get("instance"):
                    self.ai.setdefault(self.parent(k), []).append(rel)
                    out.append(k)
        self._seq = max([self._seq] + [int(r["key"][3:]) for rs in self.ai.values() for r in rs if re.fullmatch(r"AI-\d+", r["key"])])
        self._solved.clear()
        return list(dict.fromkeys(out))

    def finish(self, out: PlacementDecision) -> None:
        """The decision as ``create_from_spec`` applies it: the AI mates kept (their frame datums mapped into their
        parent's frame, where the solve put their instance), the dropped ones, the stated positions, the known poses;
        which pending instances the mates tie to their parent frame, and a note for the ones they do not."""
        from .assemblies import frame_side, is_frame_ref

        if not self._late() and not self.stopped:
            self._solved.clear()
        mates: list[dict[str, Any]] = []
        for p, rels in self.ai.items():
            for r in rels:
                rel = {**r, "a": dict(r["a"]), "b": dict(r["b"])}
                if is_frame_ref(rel["b"]):
                    found, why = self.frame(p, str(rel["b"]["instance"]))
                    side = frame_side(found, str(rel["kind"]), str(rel["b"].get("datum"))) if found is not None else None
                    if side is None:
                        out.notes.append({"code": "placement_mate_dropped", "instances": [rel["a"]["instance"]],
                                          "message": f"AI mate {rel['key']} to {rel['b']['instance']} not applied: {why}"})
                        continue
                    rel["b"]["frame"] = side
                t = self.pose(str(rel["a"]["instance"]))
                if t is not None:
                    rel["solved"] = _placement_of(t)
                mates.append(rel)
        out.mates = mates + list(self.dropped)
        out.fixed = dict(self.fixed_info)
        out.poses = {k: _placement_of(t) for k in self.insts if (t := self.pose(k)) is not None}
        # placed by AI: the instances its mates place, and a pending one only used as a mate's reference
        placed = {str(r["a"]["instance"]) for r in mates} | {str(r["b"]["instance"]) for r in mates} | set(self.fixed)
        out.decided = [k for k in out.pending if k in placed and self.pose(k) is not None]
        loose = [k for k in out.pending if k in placed and k not in out.decided]
        if loose:
            out.notes.append({"code": "placement_not_grounded", "instances": loose,
                              "message": f"{len(loose)} instance(s) connected by AI mates only to siblings that are not tied to their "
                                         f"parent frame ({', '.join(loose[:12])}): placed only relative to each other (local), not installed"})
        out.notes += self.open_subassemblies(out.pending, set(out.decided))
        out.notes += list(self.runtime_notes.values())

    # --- frames ----------------------------------------------------------------------------------------------------

    def summarize_all(self, *, again: bool = False) -> None:
        """Bounding box, envelope and datums of every kept assembly definition but the root, deepest first (a nested
        sub-assembly is summarised before the one holding it). The solves are taken again afterwards: a mate on a
        sub-assembly's contents binds what was summarised (``again``: everything summarised anew, e.g. after reused
        connections). During refresh the previous summaries remain available to sibling / ancestor frame dependencies;
        each is replaced by its newly solved result, never retained as a substitute if that result is unpositioned."""
        if again:
            self._solved.clear()
        for h in range(self.height(self.root)):
            for dk in dict.fromkeys(str(i.get("definition")) for i in self.insts.values() if self.is_asm(i.get("definition"))):
                if dk != self.root and (again or dk not in self.aggregates) and self.height(dk) == h:
                    self.aggregates[dk] = self._aggregate(dk)
                    self._solved.clear()  # following summaries must bind the refreshed child / sibling datums
        self._solved.clear()

    def loose(self, pending: list[str]) -> list[str]:
        """Review of round 3 (connected is not positioned): the kept instances the spec relations join that the solve
        leaves free to move in their parent (a part only ``parallel`` to a frame, a slide along an axis) — not pending
        yet, no stated pose. A floating group's members wait for its anchor, then their actual remaining motion is
        checked; only genuinely positioned members are carried without another decision."""
        return [k for k in self.insts if k not in pending and k in self.mated
                and self.stated(k) is None and self.pose(k) is None
                and (k not in self.anchor_of or self.pose(self.anchor_of[k]) is not None)]

    def _own_datums(self, dk: str) -> list[Datum]:
        if self.is_asm(dk):
            return [Datum(name=str(d["name"]), kind=d["kind"], origin=tuple(d["origin"]), direction=tuple(d["direction"]))  # type: ignore[arg-type]
                    for d in (self.aggregates.get(dk) or {}).get("datums") or []]
        return [d for d in self.part_datums(dk) or [] if d.kind != "frame" or d.name.startswith("port.")]

    def _aggregate(self, dk: str) -> dict[str, Any]:
        """{bbox: union of the positioned children's boxes | None, envelope: size estimate | None, datums (the outward
        ones the context lists), placed: (child key, its definition, its pose) of the positioned children (whose datums
        a mate on the sub-assembly binds, :meth:`datums`), of: positioned children, children, contents}. The envelope
        takes the positioned children by their own envelopes where they are and stacks the children without a position
        along +Z above them, centred on the frame's Z axis (their places are decided after the sub-assembly is placed;
        the simulated responder lays them out the same way)."""
        from ..cad.mates import transformed

        kids = self.children.get(dk) or []
        sure: list[tuple[float, float, float]] = []
        est: list[tuple[float, float, float]] = []
        loose: list[tuple[list[float], list[float]]] = []
        cands: list[tuple[Datum, list[float], list[float]]] = []
        names: dict[str, int] = {}
        placed: list[tuple[str, str, am.Matrix4]] = []
        n = 0
        for k in kids:
            cdk = str(self.insts[k].get("definition"))
            name = str((self.defs.get(cdk) or {}).get("name") or cdk)
            names[name] = names.get(name, 0) + 1
            t, box, size = self.pose(k), self.box(cdk), self.box(cdk, envelope=True)
            if t is None:
                if size is not None:
                    loose.append(size)
                continue
            n += 1
            placed.append((k, cdk, t))
            if size is not None:
                est += _corners(size[0], size[1], t)
            if box is None:
                continue
            corners = _corners(box[0], box[1], t)
            sure += corners
            clo, chi = [min(c[j] for c in corners) for j in range(3)], [max(c[j] for c in corners) for j in range(3)]
            cands += [(transformed(d, t, f"{k}/{d.name}"), clo, chi) for d in self._own_datums(cdk)]
        out: dict[str, Any] = {"bbox": None, "envelope": None, "datums": [], "placed": placed, "of": n, "children": len(kids),
                               "contents": clip("; ".join(f"{nm} ×{c}" for nm, c in names.items()), 300)}
        if sure:
            lo, hi = [min(p[j] for p in sure) for j in range(3)], [max(p[j] for p in sure) for j in range(3)]
            out["bbox"] = {"min": _round(lo), "max": _round(hi)}
            out["datums"] = _outward(cands, lo, hi)
        pts = list(est)
        z = max(p[2] for p in est) if est else 0.0
        for blo, bhi in loose:
            w = [bhi[j] - blo[j] for j in range(3)]
            pts += [(-w[0] / 2, -w[1] / 2, z), (w[0] / 2, w[1] / 2, z + w[2])]
            z += w[2]
        if pts:
            env = {"min": _round([min(p[j] for p in pts) for j in range(3)]), "max": _round([max(p[j] for p in pts) for j in range(3)])}
            out["envelope"] = env if env != out["bbox"] else None
        return out

    def frames(self, dk: str, depth: int = 0) -> list[tuple[list[str], am.Matrix4 | None]]:
        """Where the frame of an assembly definition sits in the root frame: (instance path from the root, transform —
        None while an instance on the path has no position), one per path, at most 4."""
        if dk == self.root:
            return [([], am.IDENTITY)]
        if depth > _MAX_LEVELS:
            return []
        out: list[tuple[list[str], am.Matrix4 | None]] = []
        for k, i in self.insts.items():
            if i.get("definition") != dk:
                continue
            t = self.pose(k)
            for path, m in self.frames(self.parent(k), depth + 1):
                out.append((path + [k], am.compose(m, t) if m is not None and t is not None else None))
                if len(out) >= 4:
                    return out
        return out

    def attached(self, dk: str) -> list[str]:
        """How the instances of a sub-assembly definition were connected in their parents (spec relations and AI mates on
        them, or their stated position): what its frame axes and planes mean."""
        from .assemblies import relation_ends

        out: list[str] = []
        for k, i in self.insts.items():
            if i.get("definition") != dk:
                continue
            if i.get("placement") or k in self.fixed:
                out.append(f"{k}: at a stated position in {self.parent(k)}")
            for r in list(self.rels.get(self.parent(k)) or []) + list(self.ai.get(self.parent(k)) or []):
                if k in relation_ends(r):
                    who = "AI, " + str((r.get("decision") or {}).get("basis_kind")) if r.get("source") == "ai_turbo" else "spec"
                    out.append(f"{k}: {r.get('kind')} {r['a']['instance']}.{r['a'].get('datum')} – {r['b']['instance']}."
                               f"{r['b'].get('datum')} ({who}){': ' + clip(str((r.get('decision') or {}).get('basis')), 160) if r.get('decision') else ''}")
            if len(out) >= 6:
                break
        return out[:6]

    def open_subassemblies(self, pending: list[str], placed: set[str]) -> list[dict[str, Any]]:
        """One note per sub-assembly instance left without a position: all its parts stay arranged (not installed)."""
        notes = []
        for k in pending:
            dk = (self.insts.get(k) or {}).get("definition")
            if k in placed or k not in self.insts or not self.is_asm(dk):
                continue
            name = (self.defs.get(str(dk)) or {}).get("name") or dk
            notes.append({"code": "subassembly_not_placed", "instances": [k],
                          "message": f"sub-assembly instance {k} ({name}) has no position in {self.parent(k)}: its {self.leaves(str(dk))} "
                                     "part instance(s) are not installed — the ones placed inside it count as placed only locally "
                                     "(local_only), the others are arranged for display"})
        return notes


def _construction_context(revision: ModelRevision | None) -> dict[str, Any]:
    """Editable operations describe only their own contribution when a part starts from imported geometry."""
    result: dict[str, Any] = {}
    if getattr(revision, "id", None) is not None:
        result["revision_id"] = str(revision.id)
    ops = getattr(revision, "ops", None)
    if ops:
        result["model_operations"] = copy.deepcopy(ops)
    if getattr(revision, "base", None) is not None:
        result.update(construction_incomplete=True, base_geometry_unavailable=True,
                      construction_note="the imported base material is not represented by model_operations; "
                                        "these operations alone cannot establish the complete contact material or openings")
    elif not ops:
        result.update(construction_incomplete=True, unavailable="no editable feature construction for this built revision")
    return result


def _built_geometry(layout: _Layout, dk: str, seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Read-only feature construction and child poses, not a new endpoint vocabulary or a bounding-box solid."""
    definition = layout.defs.get(dk) or {}
    result: dict[str, Any] = {"definition": dk, "name": definition.get("name") or dk,
                              "kind": definition.get("kind") or "part"}
    if dk in seen or len(seen) >= _MAX_LEVELS:
        result["unavailable"] = "cyclic or over-depth geometry context"
    elif layout.is_asm(dk):
        children = []
        for key in layout.children.get(dk) or []:
            child = layout.insts[key]
            pose = layout.pose(key)
            children.append({"instance": key, "transform": copy.deepcopy(pose),
                             "position_known": pose is not None,
                             "geometry": _built_geometry(layout, str(child.get("definition")), seen | {dk})})
        result["children"] = children
    else:
        result.update(_construction_context(layout.rev(dk)))
    return result


def _mounting_geometry(layout: _Layout, dk: str) -> list[dict[str, Any]]:
    """Physical counterparts of an assembly frame's existing connections, for placing its contents.

    A virtual frame/contact plane does not describe material: a boss may fit into a counterpart's opening while
    the surrounding plate seats on its rim. Retain the actual constructions and relative solved poses so the model
    can distinguish those surfaces without assuming every extremal datum has overlapping material.
    """
    from .assemblies import relation_ends

    result = []
    for key, instance in layout.insts.items():
        if instance.get("definition") != dk:
            continue
        parent = layout.parent(key)
        connections: dict[str, list[dict[str, Any]]] = {}
        for relation in list(layout.rels.get(parent) or []) + list(layout.ai.get(parent) or []):
            ends = set(relation_ends(relation))
            if key not in ends:
                continue
            for other in sorted(ends - {key}):
                if other in layout.insts and layout.parent(other) == parent:
                    connections.setdefault(other, []).append({field: copy.deepcopy(relation[field])
                        for field in ("key", "kind", "a", "b", "params") if field in relation})
        own_pose = layout.pose(key)
        for other, relations in connections.items():
            other_pose = layout.pose(other)
            transform = am.compose(am.invert(own_pose), other_pose) if own_pose is not None and other_pose is not None else None
            result.append({"assembly_instance": key, "counterpart_instance": other, "connections": relations,
                           "counterpart_transform_in_this_frame": transform,
                           "geometry": _built_geometry(layout, str(layout.insts[other].get("definition")))})
    return result


def placement_context(db: Session, v: EngineeringSpecVersion, content: dict[str, Any], layout: _Layout,
                      todo: list[str]) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, dict[str, str]]]:
    """(context JSON, source sentences, endpoints) of one placement call. The context: the instances to place, their
    siblings and the sub-assembly instances above them (with the position each already has); the definitions they use
    (bbox and datums of the built parts; a sub-assembly's frame datums / interfaces / positioned contents' datums,
    bbox / envelope / contents in its frame); ``frames`` — the assembly frames a mate may name (the parent's own, and the
    ones above it where reachable, mapped into the parent frame; where each parent sits in the root, how a sub-assembly
    was attached); the spec relations and AI mates among them; the facts and notes of those definitions; the sentences of
    this version's source chunks that name a part or sub-assembly to place, and the global coordinate sentences when
    sub-assembly or root instances are placed or the parent's frame in the root is known. ``endpoints``: what a mate may
    name — instance key or ``@<assembly>`` → {datum name: kind} (every datum, also the ones the context lists compactly)."""
    from .assemblies import is_frame_ref, relation_ends
    from .engineering_spec import quantities

    root = layout.root
    defs = layout.defs
    qty = quantities(content)
    insts = layout.insts
    todo_set = set(todo)
    parents = {layout.parent(k) for k in todo if k in insts}
    shown = [k for k, i in insts.items() if k in todo_set or (i.get("parent") or root) in parents]
    above = set(parents)  # the sub-assembly instances the parents hang under (their frames), up to the root
    while above - {root}:
        up = [k for k, i in insts.items() if i.get("definition") in above and k not in shown]
        shown += up
        above = {insts[k].get("parent") or root for k in up}
    pool = []
    for parent in sorted(parents | {layout.parent(k) for k in shown}):
        for r in list(layout.rels.get(parent) or []) + list(layout.ai.get(parent) or []):
            pool.append({"key": r.get("key"), "kind": r.get("kind"),
                         "a": {x: (r.get("a") or {}).get(x) for x in ("instance", "datum")},
                         "b": {x: (r.get("b") or {}).get(x) for x in ("instance", "datum")} if r.get("b") else None,
                         **({"params": r["params"]} if r.get("params") else {}),
                         "by": ("AI (decided earlier)" if r.get("source") == "ai_turbo" else "spec")
                         + (" · frame" if is_frame_ref(r.get("b")) else "")})
    for choice in layout.choices:
        if choice.get("deferred_binding") and set(relation_ends(choice)) & set(shown):
            pool.append({**{k: v for k, v in choice.items() if k not in ("relation", "instance", "deferred_binding")},
                         "key": choice["relation"], "kind": choice["kind"],
                         "a": dict(choice["a"]), "b": dict(choice["b"]),
                         "by": "spec · deferred (not applied)", "deferred_binding": copy.deepcopy(choice["deferred_binding"])})
    needed = agent.required_instances({"definitions": list(defs.values()), "relations": pool,
        "instances": [{**insts[k], "parent": layout.parent(k), "placement": layout.pose(k)} for k in shown]}, todo)
    if len(needed) > MAX_INSTANCES:
        from ..errors import AppError

        raise AppError(422, "context_over_budget", "required placement targets and supporting instances exceed the bounded context; nothing was clipped")
    shown = sorted(shown, key=lambda k: (k not in todo_set, k not in needed, list(insts).index(k)))[:MAX_INSTANCES]
    required_names: dict[str, set[str]] = {}
    for relation in pool:
        for side in (relation.get("a"), relation.get("b")):
            if side and isinstance(side.get("datum"), str):
                required_names.setdefault(str(side.get("instance")), set()).add(side["datum"])
        for side, candidates in (relation.get("deferred_binding") or {}).get("candidates", {}).items():
            required_names.setdefault(str((relation.get(side) or {}).get("instance")), set()).update(candidates)
    endpoints: dict[str, dict[str, str]] = {}
    instances = []
    for k in shown:
        i = insts[k]
        hint = layout.hint(k)
        pl = i.get("placement") or (hint[0] if hint else None)
        instances.append({"key": k, "definition": i.get("definition"), "parent": i.get("parent") or root, **({"name": i["name"]} if i.get("name") else {}),
                          "placement": pl, **({"positioned_by": hint[1]} if hint else {}), "decide": k in todo_set,
                          **({"decided_earlier": True} if k in layout.fixed or layout.ai_mates_of(k) else {})})
    used = list(dict.fromkeys(i["definition"] for i in instances if i.get("definition")))
    first = {insts[k].get("definition") for k in todo if k in insts}
    sibling_defs = {insts[k].get("definition") for k in shown if (insts[k].get("parent") or root) in parents}
    used.sort(key=lambda dk: (dk not in first, dk not in sibling_defs))
    definitions = []
    with_datums = 0
    for dk in used:
        d = defs.get(dk) or {}
        entry: dict[str, Any] = {"key": dk, "name": d.get("name") or dk, "kind": d.get("kind") or "part",
                                 "quantity": int((qty.get(dk) or {}).get("total") or 0)}
        if d.get("number"):
            entry["number"] = d["number"]
        datums: list[Datum] = []
        if d.get("kind") == "assembly":
            agg = layout.aggregates.get(dk)
            says = ["datums = its frame datums, interfaces if.<key> and the outward datums of its positioned contents "
                    "(<instance>/<datum>)"]
            if agg and agg.get("bbox"):
                entry["bbox"] = agg["bbox"]
                says.append(f"bbox = union of the {agg['of']} of its {agg['children']} instances that already have a position")
            if agg and agg.get("envelope"):
                entry["envelope"] = agg["envelope"]
                says.append(f"envelope = size estimate that also counts the {agg['children'] - agg['of']} without a position yet "
                            "(stacked along +Z above the positioned ones; they are connected to its frame after this call)")
            entry["frame"] = "sub-assembly with its own frame: " + "; ".join(says)
            if agg and agg.get("contents"):
                entry["contents"] = agg["contents"]
            # A child datum's name and extrema do not identify its remaining material or a real seating shoulder.
            # Preserve the complete built hierarchy in this definition's frame, including unknown child positions.
            entry["built_geometry"] = _built_geometry(layout, dk)
            datums = layout.shown(dk)
            # A name like I-09/F-01.axis is usable only when the model also knows which built child I-09 is.
            exposed = {d.name.split("/", 1)[0] for d in datums if "/" in d.name}
            children = [{"instance": k, "definition": cdk, "name": (defs.get(cdk) or {}).get("name") or cdk,
                         **({"number": defs[cdk]["number"]} if (defs.get(cdk) or {}).get("number") else {})}
                        for k, cdk, _ in (agg or {}).get("placed") or [] if k in exposed]
            if children:
                entry["datum_children"] = children
        else:
            box = layout.box(dk)
            if box is not None:
                entry["bbox"] = {"min": _round(box[0]), "max": _round(box[1])}
            # Feature names alone do not describe remaining material; imported base contributions remain unknown.
            entry.update(_construction_context(layout.rev(dk)))
            if (dk in first or dk in sibling_defs or with_datums < MAX_DATUM_DEFINITIONS) and layout.rev(dk) is not None:
                datums = layout.shown(dk)  # may rebuild the part (cached): the definitions placed here always
                with_datums += 1
        needed_names = set().union(*(required_names.get(k, set()) for k in shown if insts[k].get("definition") == dk))
        missing_names = needed_names - {datum.name for datum in datums}
        if missing_names:
            complete = layout.datums(dk)
            datums += [complete[name] for name in sorted(missing_names) if name in complete]
        if datums:
            entry["datums"] = _compact(datums, MAX_AGGREGATE_DATUMS + 7 if d.get("kind") == "assembly" else MAX_DATUMS,
                                       required=needed_names | ({x.name for x in datums} if d.get("kind") == "assembly" else set()))
        if d.get("notes"):
            entry["notes"] = clip(str(d["notes"]), 300)
        definitions.append(entry)
        for k in shown:
            if insts[k].get("definition") == dk and datums:
                endpoints[k] = {name: kind for name, kind, _, _ in agent.expand_datums(entry["datums"])}
    # round 3 (B): the frames a mate may name — the parent's own and the ones above it where its place is known
    frames = []
    known_frame = False
    for p in sorted(parents):
        refs = [p] + _ancestors(layout, p)
        for n, a in enumerate(refs):
            found, why = layout.frame(p, f"@{a}")
            f: dict[str, Any] = {"ref": f"@{a}", "name": (defs.get(a) or {}).get("name") or a,
                                 "role": ("the parent's own frame" if n == 0 else f"an assembly above {p}, mapped into {p}'s frame")
                                 + (f" (for the instances of {p})" if len(parents) > 1 else "")}
            if found is None:
                f["unavailable"] = why
            else:
                f["datums"] = _compact(list(found.values()), MAX_DATUMS + 8, required=required_names.get(f"@{a}", set()))
                endpoints[f"@{a}"] = {name: kind for name, kind, _, _ in agent.expand_datums(f["datums"])}
            if n == 0 and p != root:
                paths = layout.frames(p)
                known_frame = known_frame or any(m is not None for _, m in paths)
                f["in_root"] = [{"path": path, **(_placement_of(m) if m is not None else
                                                 {"position": "unknown: an instance on this path has no position yet"})}
                                for path, m in paths]
                f["attached"] = layout.attached(p)
                mounting = _mounting_geometry(layout, p)
                if mounting:
                    f["mounting_geometry"] = mounting
            frames.append(f)
    keys = set(shown)
    relations = [r for r in pool if all(e in keys for e in relation_ends(r))]
    for c in layout.choices:  # review of round 3: one axis against a pattern of several — which axis is for this call
        if not c.get("deferred_binding") and c["instance"] in keys and c["instance"] in todo_set:
            relations.append({"key": c["relation"], "kind": c["kind"], "a": dict(c["one"]), "b": dict(c["pattern"]), "by": "spec · choose",
                              "choose": f"{c['pattern']['datum']}.axis[0..{len(c['axes']) - 1}]"})
    names = {dk: str((defs.get(dk) or {}).get("name") or "") for dk in used}
    facts = []
    for dk in used:
        for fct in content.get("facts") or []:
            if not isinstance(fct, dict) or str(fct.get("subject") or "").split(".")[0] != dk:
                continue
            ev = next((e for e in fct.get("evidence") or [] if isinstance(e, dict) and e.get("quote")), None)
            facts.append({"subject": fct.get("subject"), "value": clip(str(fct.get("value")), 120) if not isinstance(fct.get("value"), (int, float)) else fct.get("value"),
                          "unit": fct.get("unit"), "status": fct.get("status"), **({"quote": clip(str(ev["quote"]), 160)} if ev else {})})
    notes = [{"subject": n.get("subject"), "text": clip(str(n.get("text") or ""), 300)} for n in content.get("manufacturing_notes") or []
             if isinstance(n, dict) and (n.get("subject") in names or any(len(nm) >= 2 and nm in str(n.get("text") or "") for nm in names.values()))]
    mandatory = [r for r in relations if set(relation_ends(r)) & needed]
    optional = [r for r in relations if r not in mandatory]
    context = {"units": "mm", "root": root, "frames": frames, "definitions": definitions, "instances": instances,
               "relations": mandatory + optional[:max(0, MAX_RELATIONS - len(mandatory))],
               "facts": facts[:MAX_FACTS], "manufacturing_notes": notes[:MAX_NOTES]}
    feedback = [value for parent in sorted(parents) if (value := layout.feedback(parent)) is not None]
    if feedback:
        context["solver_feedback"] = feedback
    wanted = {str(x).strip() for k in todo if k in insts for x in (insts[k].get("name"), (defs.get(insts[k].get("definition")) or {}).get("name"),
                                                                   (defs.get(insts[k].get("definition")) or {}).get("number")) if x}
    coords = known_frame or any((defs.get(insts[k].get("definition")) or {}).get("kind") == "assembly"
                                or (insts[k].get("parent") or root) == root for k in todo if k in insts)
    return context, _sentences(db, v, {w for w in wanted if len(w) >= 2}, coords=coords), endpoints


def _ancestors(layout: _Layout, dk: str) -> list[str]:
    """The assembly definitions above ``dk`` (nearest first) along its instances' parents, up to the root."""
    out: list[str] = []
    cur = {dk}
    for _ in range(_MAX_LEVELS):
        up = {layout.parent(k) for k, i in layout.insts.items() if i.get("definition") in cur}
        up -= set(out) | {dk}
        if not up:
            break
        out += sorted(up)
        cur = up
    return out


def _sentences(db: Session, v: EngineeringSpecVersion, names: set[str], *, coords: bool = False) -> list[dict[str, str]]:
    """Sentences of this version's source text chunks that name one of ``names`` (in source order, each once) and,
    with ``coords``, up to MAX_COORD_SENTENCES global coordinate sentences after the first six of them."""
    if not names and not coords:
        return []
    rows = db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.id, EngineeringSpecChunk.kind != "attachment",
                                                         EngineeringSpecChunk.text_sha256.is_not(None))
                      .order_by(EngineeringSpecChunk.seq).limit(MAX_CHUNKS)).all()
    lowered = [n.casefold() for n in names]
    named: list[dict[str, str]] = []
    coord: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in rows:
        if len(named) >= MAX_SENTENCES and (not coords or len(coord) >= MAX_COORD_SENTENCES):
            break
        try:
            text = read_blob(db, c.text_sha256)[0].decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            continue
        for s in _SENTENCE.split(text):
            s = " ".join((s or "").split())
            if not s or s in seen:
                continue
            if len(named) < MAX_SENTENCES and any(n in s.casefold() for n in lowered):
                named.append({"locator": c.locator, "text": clip(s, 300)})
            elif coords and len(coord) < MAX_COORD_SENTENCES and _COORD.search(s):
                coord.append({"locator": c.locator, "text": clip(s, 300), "keep": True})
            else:
                continue
            seen.add(s)
    # coordinate sentences first and kept longest when the prompt is cut (they are what places a sub-assembly)
    return coord + named[:MAX_SENTENCES]
