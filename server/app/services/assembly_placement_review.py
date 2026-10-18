"""Bounded review of solved, source-driven assemblies using actual material feedback.

This is generation feedback, not a waiver of source conflicts or an engineering approval.
All proposals use the existing named-mate parser and transactional refinement guard.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

from ..ai import retry as call_retry
from ..ai import placement_agent as agent


MAX_REVIEW_CALLS = 6
GEOMETRY_BUDGET_S = 120.0
_CANDIDATE_STATE = ("ai", "rels", "dropped", "fixed", "fixed_info", "_solved", "_verified", "_seq",
                    "aggregates", "runtime_notes")


def _prepare(layout: Any) -> None:
    layout.summarize_all(again=True)
    # Summarization invalidates cached solves. Populate them explicitly before the read-only material check.
    for key in sorted(layout.insts, key=lambda k: (-layout.height(layout.parent(k)), k)):
        layout.pose(key)


def _regressed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A source review cannot introduce new material intersections or deepen an existing one."""
    if not before.get("complete"):
        return False  # a complete new measurement is more informative than an unexamined candidate
    if after.get("findings_truncated"):
        return True

    def pairs(report: dict[str, Any]) -> dict[str, float]:
        return {repr(sorted((tuple((p.get("a") or {}).get("path") or []),
                             tuple((p.get("b") or {}).get("path") or [])))): float(p["volume_mm3"])
                for p in report.get("interferences", [])}

    from ..cad.assembly_checks import VOLUME_TOL_MM3

    old = pairs(before)
    return (after.get("interference_count", len(after.get("interferences", []))) >
            before.get("interference_count", len(before.get("interferences", []))) or
            any(key not in old or volume > old[key] + VOLUME_TOL_MM3 for key, volume in pairs(after).items()))


def _review_plan(layout: Any, candidates: list[str], report: dict[str, Any], *, made: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select whole batches with room for final ancestor reviews, then execute children first.

    Selection prefers measured repair targets. Root receives one slot when it has an eligible batch; additional
    ancestor slots are charged to a child batch before selecting it. A protected ancestor has no automatic action
    to reserve. The plan never enlarges either call budget and records every unselected scope explicitly.
    """
    from . import assembly_placement as placement

    budget = max(0, min(MAX_REVIEW_CALLS, placement.MAX_CALLS - made))
    budget_reason = "shared_call_budget" if placement.MAX_CALLS - made < MAX_REVIEW_CALLS else "review_call_budget"
    targets = report.get("repair_targets") or {}
    by_parent: dict[str, list[str]] = {}
    for key in candidates:
        by_parent.setdefault(layout.parent(key), []).append(key)
    jobs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for parent, members in sorted(by_parent.items()):
        aimed = set(targets.get(parent) or [])
        groups = layout.replacement_groups(members, candidates)
        groups.sort(key=lambda group: (not bool(aimed.intersection(group)), tuple(sorted(group))))
        batches, oversized = placement._refinement_batches(groups, [], layout.insts)
        for group in oversized:
            if len(group) <= placement.MAX_INSTANCES:
                # Final review keeps a connected group intact beyond the first pass's soft target count.
                # placement_context and _model_message still enforce full support closure and model-window bounds.
                batches.append(group)
            else:
                skipped.append({"parent": parent, "instances": group, "status": "skipped", "reason": "atomic_group_over_budget"})
        for batch in batches:
            jobs.append({"parent": parent, "instances": batch,
                         "groups": [group for group in groups if group[0] in batch],
                         "repair_targets": sorted(aimed.intersection(batch))})

    # A repeated assembly definition can have several ancestors. Follow every actual containment edge, not names.
    edges: dict[str, dict[str, set[str]]] = {}
    for key, instance in layout.insts.items():
        definition = instance.get("definition")
        if definition:
            edges.setdefault(str(definition), {}).setdefault(layout.parent(key), set()).add(key)

    def ancestors(parent: str) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        seen, todo = {parent}, [parent]
        while todo:
            child = todo.pop()
            for owner, holders in edges.get(child, {}).items():
                if owner == parent:
                    continue
                found.setdefault(owner, set()).update(holders)
                if owner not in seen:
                    seen.add(owner)
                    todo.append(owner)
        return found

    ranked = sorted(range(len(jobs)), key=lambda i: (not bool(jobs[i]["repair_targets"]),
                    -len(jobs[i]["repair_targets"]), -layout.height(jobs[i]["parent"]),
                    jobs[i]["parent"], tuple(jobs[i]["instances"])))
    by_frame: dict[str, list[int]] = {}
    for index in ranked:
        by_frame.setdefault(jobs[index]["parent"], []).append(index)
    selected: set[int] = set()
    root = str(layout.root)
    if budget and by_frame.get(root):
        selected.add(by_frame[root][0])
    reasons: dict[int, str] = {}
    unavailable: dict[str, set[str]] = {}
    # First repair actual material conflicts, then spend residual capacity on source-only review.
    ordering = [i for i in ranked if jobs[i]["repair_targets"]]
    ordering += sorted((i for i in ranked if not jobs[i]["repair_targets"]),
                       key=lambda i: (layout.height(jobs[i]["parent"]), jobs[i]["parent"], tuple(jobs[i]["instances"])))
    for index in ordering:
        if index in selected:
            continue
        closure = {index}
        for parent, holders in ancestors(jobs[index]["parent"]).items():
            choices = by_frame.get(parent, [])
            if not choices:
                unavailable.setdefault(parent, set()).update(holders)
                continue
            # Prefer the ancestor batch containing the affected subassembly occurrence; otherwise its repair batch.
            relevant = [i for i in choices if holders.intersection(jobs[i]["instances"])]
            if relevant:
                closure.update(relevant)  # every affected repeated occurrence may occupy a different ancestor batch
            else:
                closure.add(next((i for i in choices if i in selected), choices[0]))
        if len(selected | closure) <= budget:
            selected |= closure
        else:
            reasons[index] = "ancestor_review_budget" if len(selected) < budget and len(closure - selected) > 1 else budget_reason
    for parent, keys in targets.items():
        remaining = set(keys) - set(by_parent.get(parent) or [])
        if remaining:
            unavailable.setdefault(parent, set()).update(remaining)
    for parent, keys in sorted(unavailable.items()):
        skipped.append({"parent": parent, "instances": sorted(keys), "status": "skipped", "reason": "no_replaceable_mates"})
    for index, job in enumerate(jobs):
        job["batch_id"] = index
        if index not in selected:
            skipped.append({**_attempt(job), "status": "skipped", "reason": reasons.get(index, budget_reason)})
    planned = sorted((jobs[i] for i in selected), key=lambda job: (layout.height(job["parent"]), job["parent"],
                     not bool(job["repair_targets"]), job["batch_id"]))
    return planned, skipped


def _attempt(job: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(job[key]) for key in ("parent", "instances", "batch_id", "repair_targets") if key in job}


def _skip_remaining(attempts: list[dict[str, Any]], jobs: list[dict[str, Any]], reason: str) -> None:
    attempts.extend({**_attempt(job), "status": "skipped", "reason": reason} for job in jobs)


def review_layout(db: Any, version: Any, content: dict[str, Any], layout: Any, out: Any, provider: Any,
                  *, made: int, late: Callable[[], bool], drawing_cache: Any,
                  record_calls: Callable[[list[Any]], bool], deadline: float | None = None,
                  cancelled: Callable[[], bool] | None = None) -> int:
    """Review final child geometry once per bounded batch; preserve immutable source relations.

    A final original-sheet review can identify an arrangement error without a collision.
    Physical findings are never removed merely because a model calls an overlap intentional.
    """
    from . import assembly_placement as placement
    from .assembly_placement_checks import check_layout

    geometry_spent = 0.0

    def inspect() -> dict[str, Any]:
        nonlocal geometry_spent
        started = time.monotonic()
        try:
            return check_layout(layout, deadline=deadline, cancelled=cancelled,
                                budget_s=max(0.0, GEOMETRY_BUDGET_S - geometry_spent))
        finally:
            geometry_spent += time.monotonic() - started

    _prepare(layout)
    try:
        report = inspect()
    except Exception as exc:
        report = {"status": "unknown", "complete": False, "fingerprint": None,
                  "incomplete_reasons": [getattr(exc, "code", type(exc).__name__)]}
    initial_fingerprint = report.get("fingerprint")
    candidates = [key for key in out.pending if layout.replaceable_mates(key)]
    jobs, attempts = _review_plan(layout, candidates, report, made=made)
    review_calls = 0
    for job_index, job in enumerate(jobs):
        parent, batch, groups = job["parent"], job["instances"], job["groups"]
        limits = call_retry.current_limits()
        stopped = limits.stop_reason() if limits is not None else None
        reason = ("runtime_stopped" if late() else "shared_call_budget" if made >= placement.MAX_CALLS else
                  "review_call_budget" if review_calls >= MAX_REVIEW_CALLS else stopped.kind if stopped is not None else None)
        if reason:
            _skip_remaining(attempts, jobs[job_index:], reason)
            break
        if any(not layout.replaceable_mates(key) for key in batch):
            attempts.append({**_attempt(job), "status": "skipped", "reason": "no_longer_replaceable"})
            continue
        prior: dict[str, Any] | None = None
        try:
            drawing_text, images = placement._drawing_context(drawing_cache, layout, batch)
            if not drawing_text and not images:
                attempts.append({**_attempt(job), "status": "skipped", "reason": "missing_original_source"})
                continue
            context, references, endpoints = placement.placement_context(db, version, content, layout, batch)
            selected_groups = [group for group in groups if group[0] in batch]
            replaceable = {key: [mate["key"] for mate in layout.replaceable_mates(key)] for key in batch}
            changing = {key for keys in replaceable.values() for key in keys}
            protected = [mate["key"] for mate in context["relations"] if mate["key"] not in changing]
            context["refinement"] = {
                "replace_estimates": batch, "replacement_groups": selected_groups,
                "replace_estimated_mates": replaceable, "protected_mates": protected,
                "repair_supports": [], "reason": "final original-sheet arrangement and measured solid feedback",
            }
            context["physical_review"] = {
                "scope": "Review the final solved geometry against the attached original views and sections. "
                         "Check relative sides, axial stations, orientation, support contact, insertion and complete "
                         "hole patterns. Return corrected estimated mates only when a correction is supported. "
                         "Return no mates if the arrangement needs no correction. Never alter protected mates or "
                         "source dimensions. Unresolved source conflicts and intentional fits remain unconfirmed.",
                "status": report.get("status"), "complete": report.get("complete", False),
                "fingerprint": report.get("fingerprint"),
                "interferences": [item for item in report.get("interferences", [])
                                  if (item.get("repair_scope") or {}).get("parent_definition") == parent],
                "incomplete_reasons": report.get("incomplete_reasons", []),
                "material_check_scope": "Volumetric intersections only; no collision does not prove source conformity.",
            }
            for instance in context["instances"]:
                if instance["key"] in batch:
                    instance["positioned_by"] = "Final review: only the explicitly listed estimated mates may change."
            user = placement._model_message(provider, context, references, batch,
                                            limit=placement.ASSEMBLY_PROMPT_CHARS,
                                            drawing_text=drawing_text, images=images)
            visible = agent.visible_endpoints(user, endpoints)
            texts: dict[str, list[str]] = {}
            for reference in references:
                texts.setdefault(reference["locator"], []).append(reference["text"])
            made += 1
            review_calls += 1
            result = agent.run_placement(provider, user, batch, locators=set(texts), endpoints=visible, texts=texts,
                                         instances={i["key"] for i in context["instances"]},
                                         timeout_s=placement.PLACEMENT_TIMEOUT_S, images=images)
            if not record_calls(result.calls):
                attempts.append({**_attempt(job), "status": "stopped", "reason": "checkpoint_lost"})
                _skip_remaining(attempts, jobs[job_index + 1:], attempts[-1]["reason"])
                _store(out, report, initial_fingerprint, attempts)
                return made
            out.simulated = out.simulated or result.simulated
            stopped = limits.stop_reason(calls=False) if limits is not None else None
            if late() or stopped is not None:
                attempts.append({**_attempt(job), "status": "stopped", "reason": "runtime_stopped"})
                _skip_remaining(attempts, jobs[job_index + 1:], attempts[-1]["reason"])
                _store(out, report, initial_fingerprint, attempts)
                return made
            if result.error is not None:
                attempts.append({**_attempt(job), "status": "failed", "error": result.error})
                continue
            if not result.mates:
                attempts.append({**_attempt(job), "status": "unchanged",
                                 "unresolved": dict(result.unplaced), "problems": list(result.problems)})
                continue
            prior = {key: copy.deepcopy(getattr(layout, key)) for key in _CANDIDATE_STATE if hasattr(layout, key)}
            notes = layout.refine(result, batch)
            accepted = any(note.get("code") == "placement_refined" for note in notes)
            attempts.append({**_attempt(job), "status": "applied" if accepted else "retained",
                             "before_fingerprint": report.get("fingerprint")})
            if accepted:
                try:
                    _prepare(layout)
                    trial = inspect()
                except Exception as exc:
                    trial = {"status": "unknown", "complete": False, "fingerprint": None,
                             "incomplete_reasons": [getattr(exc, "code", type(exc).__name__)]}
                reason = ("physical_recheck_incomplete" if not trial.get("complete") else
                          "physical_check_regressed" if _regressed(report, trial) else None)
                if reason:
                    for key, value in prior.items():
                        setattr(layout, key, value)
                    attempts[-1].update(status="retained", reason=reason, trial_report=trial)
                    out.notes.append({"code": "placement_refinement_retained", "instances": batch,
                                      "message": "Physical recheck did not establish a non-degraded candidate; previous mates and findings retained.",
                                      "reason": reason})
                else:
                    report = trial
                    attempts[-1]["after_fingerprint"] = report.get("fingerprint")
                    out.notes.extend(notes)
            else:
                out.notes.extend(notes)
            prior = None
        except Exception as exc:  # optional feedback must retain the candidate and concrete incomplete evidence
            if prior is not None:
                for key, value in prior.items():
                    setattr(layout, key, value)
            attempts.append({**_attempt(job), "status": "failed",
                             "reason": getattr(exc, "code", type(exc).__name__)})
    _store(out, report, initial_fingerprint, attempts)
    return made


def _store(out: Any, report: dict[str, Any], initial_fingerprint: str | None,
           attempts: list[dict[str, Any]]) -> None:
    reviewed = [item for item in attempts if item["status"] in ("applied", "unchanged", "retained")]
    open_scopes = [item for item in attempts if item["status"] not in ("applied", "unchanged", "retained")]
    parents = sorted({str(item["parent"]) for item in attempts})
    coverage = {"scope": "original_drawing_review", "complete": bool(attempts) and not open_scopes,
                "status": "not_run" if not attempts else "partial" if open_scopes else "complete",
                "reviewed_batches": len(reviewed), "unreviewed_scopes": [
                    {**_attempt(item), "reason": item.get("reason") or (item.get("error") or {}).get("code") or item["status"]}
                    for item in open_scopes],
                "parents": [{"parent": parent, "reviewed_batches": sum(item["parent"] == parent for item in reviewed),
                             "complete": not any(item["parent"] == parent for item in open_scopes)} for parent in parents],
                "meaning": "Review coverage only; not source conformity, engineering acceptance or human confirmation."}
    saved_report = {**copy.deepcopy(report), "source_review": coverage}
    out.notes.append({"code": "placement_physical_review", "instances": [],
                      "message": "Final solid intersection feedback; source decisions still require confirmation.",
                      "report": saved_report, "initial_fingerprint": initial_fingerprint,
                      "attempts": copy.deepcopy(attempts)})
