"""AI workspace operations on assemblies (W8; w0-contract §3 ``assembly_create`` / ``assembly_modify``).

- ``assembly_create``: the request text (+ referenced project files, or an exact referenced spec version) becomes a
  project-level workflow run of the chain graph engineering_spec → assembly_from_spec → assembly_drawing →
  delivery_package, with the operation's frozen policy and model snapshot (W1 / W7). The operation follows the run;
  its result carries the chain values (spec, assembly, drawings, delivery), assumptions and open questions.
- ``assembly_modify``: a change request on an exact assembly revision (a version-pinned ``assembly_revision``
  reference) is planned by the model as W4 edit ops, previewed with ``assemblies.impact`` (nothing written) and
  shown with its impact. Applying it creates a new revision against that base (409 when the assembly moved on); a
  plan that removes instances or mates is never applied without an explicit confirmation.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import limits
from ..errors import AppError, conflict, not_found
from ..models import Project, AiOperation, Assembly, AssemblyRevision, ModelRevision, NodeRun, Part, WorkflowRun
from ..security import ProjectAccess, project_access
from . import execution_policy as policy_svc
from .assistant_tool_inputs import extraction_text, source_text
from .failure_causes import public_error

#: Node ids of the chain graph an assembly_create operation starts.
CHAIN_NODES = ("spec", "assembly", "drawings", "delivery")
REMOVING_OPS = ("remove_instance", "remove_mate")
#: The request text a chain's spec step takes directly (a longer text is a material file; ai/limits.py).
MAX_SPEC_TEXT = limits.CHAT_TEXT_CHARS


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# =====================================================================================================
# References and facts
# =====================================================================================================


def load_revision_in_project(db: Session, revision_id: Any, project_id: uuid.UUID) -> tuple[AssemblyRevision, Assembly] | None:
    rev = db.get(AssemblyRevision, _uuid(revision_id)) if _uuid(revision_id) else None
    asm = db.get(Assembly, rev.assembly_id) if rev is not None else None
    if rev is None or asm is None or asm.project_id != project_id:
        return None
    return rev, asm


def _euler(m: list[list[float]]) -> list[float] | None:
    """[rx, ry, rz] degrees (X, then Y, then Z) of a rigid transform, None when it does not round-trip."""
    from ..cad import assembly_model as am

    ry = math.degrees(math.asin(max(-1.0, min(1.0, -m[2][0]))))
    rx = math.degrees(math.atan2(m[2][1], m[2][2]))
    rz = math.degrees(math.atan2(m[1][0], m[0][0]))
    back = am.rotation(rx, ry, rz)
    if any(abs(back[i][j] - m[i][j]) > 1e-6 for i in range(3) for j in range(3)):
        return None
    return [round(v, 4) + 0.0 for v in (rx, ry, rz)]


def _fmt3(v: list[float]) -> str:
    return "(" + ", ".join(f"{x:g}" for x in v) + ")"


def assembly_revision_facts(db: Session, revision_id: Any, project_id: uuid.UUID, *, full: bool = True) -> str | None:
    """The facts of one exact assembly revision for a model: direct instances (stable ids, definitions with their
    exact revisions, placements), mates, flattened BOM, policy / check state and whether a newer revision exists.
    None when the revision is not in this project."""
    from . import assemblies as asm_svc

    found = load_revision_in_project(db, revision_id, project_id)
    if found is None:
        return None
    rev, asm = found
    comps, mates = asm_svc.revision_state(db, rev)
    latest = asm_svc.latest_revision(db, asm.id)
    lines = [f"Assembly {asm.number} \"{asm.name}\" revision A{rev.rev_no} (assembly_id {asm.id}, revision_id {rev.id}); policy "
             f"{rev.execution_policy}; check status {rev.check_status}; units mm, right-handed, Z up"]
    if latest is not None and latest.id != rev.id:
        lines.append(f"NOTE: a newer revision A{latest.rev_no} exists; edits apply only to the latest revision")
    lines.append("instances (direct children of this revision; ids are stable):")
    for c in comps:
        if c.kind == "part":
            prev = db.get(ModelRevision, c.model_revision_id)
            part = db.get(Part, prev.part_id) if prev is not None else None
            head = (f"part {part.number if part else '?'} \"{part.name if part else '?'}\" R{prev.rev_no if prev else '?'} "
                    f"(part_id {prev.part_id if prev else '?'}, revision_id {c.model_revision_id})")
        else:
            crev = db.get(AssemblyRevision, c.child_revision_id)
            casm = db.get(Assembly, crev.assembly_id) if crev is not None else None
            head = (f"assembly {casm.number if casm else '?'} \"{casm.name if casm else '?'}\" A{crev.rev_no if crev else '?'} "
                    f"(assembly_id {crev.assembly_id if crev else '?'}, revision_id {c.child_revision_id})")
        m = [list(r) for r in c.transform]
        rot = _euler(m)
        place = f"translate {_fmt3([m[0][3], m[1][3], m[2][3]])} " + (f"rotate {_fmt3(rot)}" if rot is not None else f"rotation {m}")
        flags = ", ".join(x for x in ("fixed" if c.fixed else "", "suppressed" if c.suppressed else "") if x)
        lines.append(f"- {c.instance_id} \"{c.name}\": {head}; {place}" + (f"; {flags}" if flags else ""))
    if mates:
        lines.append("mates:")
        for mt in mates:
            b = f" ~ {mt.b.get('instance')}.{mt.b.get('datum')}" if mt.b else ""
            lines.append(f"- {mt.mate_id} {mt.kind} {mt.a.get('instance')}.{mt.a.get('datum')}{b} {mt.params or ''} [{mt.status}]".rstrip())
    flat = (rev.bom or {}).get("flattened") or []
    if flat:
        lines.append("BOM (flattened, totals from the instance tree):")
        for line in flat[: None if full else 60]:
            lines.append(f"- {line.get('number')} \"{line.get('name')}\" x{line.get('total_quantity')} R{line.get('rev_no')} "
                         f"material {line.get('material') or 'unknown'} procurement {line.get('procurement')} representation {line.get('representation')}")
    return "\n".join(lines)


def _record_manifest(db: Session, op: AiOperation, items: list[Any], *, purpose: str, model_ref: Any, note: str = "",
                     extra: dict[str, Any] | None = None) -> None:
    """The operation's context manifest (items without their text) in the same shape as the workspace builds."""
    from . import assistant_context as actx

    cfg = actx._resolve_config(db, purpose, model_ref)
    input_limit, output_reserve, safety, available, limit_source = actx._budget(cfg)
    used = sum(i.tokens_est for i in items if i.included)
    budget = {"input_limit": input_limit, "output_reserve": output_reserve, "safety": safety, "available": available, "required_est": used,
              "used_est": used, "limit_source": limit_source, "estimated": True, **({"note": note} if note else {}), **(extra or {})}
    actx.record_build(db, op, actx.ContextBuild(ok=True, items=items, budget=budget, error=None, summary_through_seq=None, model_config=cfg,
                                                model_label=actx._model_label(cfg), purpose=purpose))


# =====================================================================================================
# assembly_create
# =====================================================================================================


def chain_graph(*, text: str, file_version_ids: list[str], spec_version_id: str | None, title: str,
                model_config_id: str | None, revise: bool = False, reuse_earlier_builds: bool = True,
                design_from_requirements: bool = False) -> dict[str, Any]:
    """The chain graph of an assembly_create operation (a project-level run; no part). ``revise`` (generation supplement
    A-04): the spec step makes a new version of ``spec_version_id`` with the files and the text as a revision note."""
    spec_params: dict[str, Any] = {"title": title[:200]}
    if design_from_requirements:
        spec_params["design_from_requirements"] = True
    if spec_version_id:
        spec_params["spec_version_id"] = spec_version_id
        if revise:
            spec_params.update(revise=True, text=text[:MAX_SPEC_TEXT], file_version_ids=list(file_version_ids))
    else:
        spec_params["text"] = text[:MAX_SPEC_TEXT]
        spec_params["file_version_ids"] = list(file_version_ids)
    asm_params: dict[str, Any] = {"reuse_earlier_builds": reuse_earlier_builds}
    if model_config_id:
        spec_params["model_config_id"] = model_config_id
        asm_params["model_config_id"] = model_config_id

    def node(nid: str, ntype: str, x: float, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"id": nid, "type": ntype, "position": {"x": x, "y": 120}, "params": params or {}}

    def edge(src: str, sp: str, dst: str, dp: str) -> dict[str, Any]:
        return {"id": f"{src}.{sp}->{dst}.{dp}", "source": src, "source_port": sp, "target": dst, "target_port": dp}

    return {"nodes": [node("spec", "engineering_spec", 0, spec_params), node("assembly", "assembly_from_spec", 280, asm_params),
                      # generation supplement E: every level (assembly, each sub-assembly, each part) drawn and delivered
                      node("drawings", "assembly_drawing", 560, {"subassemblies": True}), node("delivery", "delivery_package", 840)],
            "edges": [edge("spec", "spec", "assembly", "spec"), edge("assembly", "assembly", "drawings", "assembly"),
                      edge("assembly", "assembly", "delivery", "assembly"), edge("drawings", "drawings", "delivery", "drawings")],
            "groups": []}


@dataclass
class RequestSources:
    """What the spec step of an assembly request reads (:func:`request_spec_sources`)."""

    files: list[str]
    spec_version: str | None
    items: list[dict[str, Any]]
    #: Generation supplement A-04 / IN-07: a new version of ``spec_version`` with ``files`` and the text as a revision note.
    revise: bool = False
    #: The person's explicit choice next to a spec version: {choice: revise | keep, spec_version_id, spec_key, files, text_changes}.
    choice: dict[str, Any] | None = None


def request_spec_sources(db: Session, target: dict | None, references: list[dict], text: str, *,
                         revise: bool | None = None) -> RequestSources:
    """The file versions, spec version and manifest items of an assembly request's target and references (the send
    checks it before anything is written; the operation again when it starts). Files of another project / archived
    are refused by the spec service; a spec version is pinned exactly. Generation supplement A-04: a spec version with
    new files or change wording in the request is 422 ``spec_and_sources`` (services/attachments.py) unless the person
    chose: ``revise`` true makes a new version of that spec (its sources + the new files, the request text as a revision
    note), false builds from the exact version and drops the new material visibly (``choice.files``, manifest items left
    out with reason ``dropped_keep_spec``). Never silently the spec alone; files that version already read are nothing
    new and are left to it."""
    from .attachments import check_spec_and_sources, request_sources, spec_material_mix

    file_refs, spec_ref = request_sources(target, references)
    files = list(dict.fromkeys(str(r["object_id"]) for r in file_refs))
    spec_version = str(spec_ref["object_id"]) if spec_ref else None
    keys = {str(r["object_id"]): r.get("key") for r in file_refs}
    labels = {str(r["object_id"]): r.get("title") for r in file_refs}
    choice: dict[str, Any] | None = None
    if spec_version and revise is not None:
        known, new_files, changes = spec_material_mix(db, spec_version_id=spec_version, file_version_ids=files, text=text, instruction=True)
        choice = {"choice": "revise" if revise else "keep", "spec_version_id": spec_version, "spec_key": spec_ref.get("key") if spec_ref else None,
                  "files": [{"key": keys.get(f), "file_version_id": f, "label": labels.get(f)} for f in new_files], "text_changes": changes}
    else:
        known = check_spec_and_sources(db, spec_version_id=spec_version, file_version_ids=files, text=text, instruction=True,
                                       spec_key=spec_ref.get("key") if spec_ref else None, file_keys=keys)
    if spec_version:
        files = [f for f in files if f not in known]
    dropped = set(files) if choice is not None and not revise else set()
    items: list[dict[str, Any]] = [{"kind": "file_version", "key": r.get("key"), "label": r.get("title"),
                                    **({"dropped": True} if str(r["object_id"]) in dropped else {})}
                                   for r in file_refs if str(r["object_id"]) in files]
    if spec_ref:
        items.append({"kind": "engineering_spec_version", "key": spec_ref.get("key"), "label": spec_ref.get("version_label"),
                      **({"revised": True} if choice is not None and revise else {})})
    if dropped:
        files = []
    return RequestSources(files=files, spec_version=spec_version, items=items, revise=bool(choice is not None and revise), choice=choice)


def _sources(db: Session, op: AiOperation) -> RequestSources:
    req = op.request or {}
    rev = req.get("revise")
    return request_spec_sources(db, req.get("target"), list(req.get("references") or []), source_text(req),
                                revise=rev if isinstance(rev, bool) else None)


def start_assembly_create(db: Session, user: Any, op: AiOperation) -> None:
    """Checks in contract order (design permission, active project, writable task), a context manifest of what the
    chain reads, then ``start_run`` of the chain graph with the operation's frozen model snapshot and policy."""
    from ..api.workflows import start_run
    from . import assistant_context as actx
    from .assistant import _model_ref, _touch_conversations, require_task_writable
    from .lifecycle import require_active

    req = op.request or {}
    text = source_text(req)
    acc: ProjectAccess = project_access(db, user, op.project_id, "design")
    src = _sources(db, op)
    files, spec_version, ref_items = src.files, src.spec_version, src.items
    if (not spec_version or src.revise) and len(text) > MAX_SPEC_TEXT:
        raise AppError(422, "text_too_long", f"put a description longer than {MAX_SPEC_TEXT} characters in a project file and reference it",
                       {"max": MAX_SPEC_TEXT, "length": len(text)})
    require_active(db, op.project_id, what="starting a run")
    tid = _uuid(req.get("task_id"))
    task = require_task_writable(db, user, tid, op.project_id) if tid else None
    # The manifest of this request: the text and files are read by the spec step (chunked, every chunk recorded on the
    # spec version), a referenced spec version is used exactly as it is.
    items = [actx._item("required", "request", "Request", text)]
    # S2: a material saved from the request (or still referenced from an earlier one) is a file source of the spec step,
    # read in chunks there (every chunk recorded on the spec version). This request's own paste is the whole text, so the
    # spec does not read its shortened request text again.
    materials = {m.get("key"): m for m in req.get("materials") or [] if isinstance(m, dict)}
    for it in ref_items:
        if it.get("dropped"):  # A-04: built from the exact spec version — this new file is left out, visibly
            items.append(actx._item("required", "reference", f"{it['kind']}: {it.get('label') or ''}".strip(), "", included=False,
                                    ref_key=it.get("key"), reason="dropped_keep_spec"))
            continue
        mat = materials.get(it.get("key"))
        item = actx._item("required", "material" if mat else "reference", f"{it['kind']}: {it.get('label') or ''}".strip(), "",
                          ref_key=it.get("key"), read="metadata" if it["kind"] == "file_version" else "content",
                          ranges=["read by the engineering spec step (see its chunks)"] if it["kind"] == "file_version" else
                          ["revised: a new version of this spec reads its sources again with the new material"] if it.get("revised") else
                          ["the exact spec version"])
        if mat:
            item.method = "spec_chunks"
        items.append(item)
    used_materials = [{"key": m.get("key"), "label": m.get("name"), "chars": m.get("chars"), "method": "spec_chunks"}
                      for m in materials.values()]
    # A material with a referenced spec version never gets here: it is new material next to an exact spec version, which
    # _sources refuses (spec_and_sources; it was material_not_included before generation supplement A).
    _record_manifest(db, op, items, purpose="modeling", model_ref=_model_ref(req), note="files are read in chunks by the engineering spec step",
                     extra=({"materials": used_materials} if used_materials else None))
    title = (text.splitlines()[0] if text else "Assembly")[:120]
    spec_text = extraction_text(req)
    design_ref = (req.get('tool_input') or {}).get('design_ref')
    if design_ref:
        from . import orchestration, ai as ai_svc
        from .design_plans import resolve_authorized_design
        resolved_design = resolve_authorized_design(db, user, design_ref, conversation_id=op.conversation_id,
            root_operation_id=uuid.UUID(req['root_op_id']), tool_kind='assembly_create')
        resolved = orchestration.resolve_spec(db, access=acc, policy=op.execution_policy,
            authorized_design=resolved_design, model_ref=ai_svc.bundle_model_ref(req.get('model_snapshots'), 'planning'))
        spec_version, spec_text, files = str(resolved.version.id), '', []
        title = resolved_design.content.title
    # The frozen human/API choice takes precedence over a model-selected tool argument. Historical requests retain
    # the old default; neither a retry nor an engineering document silently changes their build policy.
    reuse = req.get("reuse_earlier_builds")
    if not isinstance(reuse, bool):
        reuse = (req.get("tool_input") or {}).get("reuse_earlier_builds", True)
    graph = chain_graph(text=spec_text, file_version_ids=files, spec_version_id=spec_version, title=title, revise=src.revise,
                        reuse_earlier_builds=reuse,
                        model_config_id=str(req["model_config_id"]) if req.get("model_config_id") else None,
                        design_from_requirements=bool((req.get("tool_input") or {}).get("design_from_requirements")))
    run = start_run(db, user, project_id=op.project_id, graph_raw=graph, part_id=None, title=f"AI assembly: {title}"[:200],
                    origin="workspace", task=task, purpose=text, model_snapshot=req.get("model_snapshot"), model_snapshots=req.get("model_snapshots"),
                    preferences_snapshot=req.get("preferences_snapshot"), team_snapshot=req.get("team_snapshot"),
                    **policy_svc.frozen_on(op))
    op.run_id, op.status = run.id, "running"
    _ = acc
    _touch_conversations(db, {op.conversation_id})
    db.commit()


def chain_result(db: Session, op: AiOperation, run: WorkflowRun, nodes: list[NodeRun]) -> dict[str, Any]:
    """The chain part of an assembly_create result: each step's status and value, the assumptions the products rest
    on and every open question (spec questions, blocked definitions), as recorded by the run."""
    from ..api.workflows import run_assumptions

    by_id = {n.node_id: n for n in nodes}
    steps = []
    questions: list[dict[str, Any]] = []
    for nid in CHAIN_NODES:
        n = by_id.get(nid)
        if n is None:
            continue
        out = n.outputs or {}
        value = next((v for v in out.values() if isinstance(v, dict) and v.get("type") in ("engineering_spec", "assembly", "drawing_package",
                                                                                              "delivery")), None)
        err = public_error(n.error)
        steps.append({"node_id": nid, "node_type": n.node_type, "status": n.status, "value": value, "error": err})
        if value and value.get("type") == "engineering_spec":
            questions += [{"source": "spec", **q} for q in value.get("open_questions") or []]
        if value and value.get("type") == "assembly":
            for d in value.get("definitions") or []:
                questions += [{"source": q.get("source") or "modeling", "definition": d.get("key"), **q}
                              for q in d.get("questions") or [] if q.get("question") and q.get("source") != "spec"]
    return {"steps": steps, "assumptions": run_assumptions(db, run, nodes), "questions": questions,
            "execution_policy": run.execution_policy, "run_status": run.status}


# =====================================================================================================
# assembly_modify
# =====================================================================================================


def _target_revision(db: Session, op: AiOperation) -> tuple[AssemblyRevision, Assembly, dict[str, Any]]:
    target = (op.request or {}).get("target")
    if not target or target.get("kind") != "assembly_revision":
        raise AppError(422, "target_required", "choose the assembly revision to change (add it as the target)", {"expected": ["assembly_revision"]})
    found = load_revision_in_project(db, target.get("object_id"), op.project_id)
    if found is None:
        raise not_found("assembly revision")
    return found[0], found[1], target


def removals_of(ops: list[dict[str, Any]], impact: dict[str, Any]) -> list[dict[str, Any]]:
    """What a plan removes: instances, mates, suppressed instances and instances dropped by a lower quantity."""
    out = [{"op": o.get("op"), "instance": o.get("instance"), "mate_id": o.get("mate_id")} for o in ops if o.get("op") in REMOVING_OPS]
    out += [{"op": "suppress_instance", "instance": o.get("instance")} for o in ops
            if o.get("op") == "suppress_instance" and o.get("suppressed", True)]
    out += [{"op": "set_quantity", "removed": a.get("removed")} for a in impact.get("ops") or [] if a.get("op") == "set_quantity" and a.get("removed")]
    return out



def _check_selected_ops(target: dict, ops: list[dict]) -> None:
    path = (target.get("instance") or {}).get("path") or []
    if not path:
        return
    if len(path) != 1:
        raise conflict("nested_assembly_edit_unsupported", "edit the nested definition explicitly; a root instance path cannot be flattened")
    supported = {"move_instance", "rename_instance", "set_fixed", "set_display", "replace_definition", "suppress_instance", "remove_instance"}
    if any(op.get("op") not in supported or op.get("instance") != path[0] for op in ops):
        raise conflict("selection_scope_exceeded", "the plan changes objects outside the selected assembly instance")


def plan_assembly_modify(db: Session, user: Any, op: AiOperation) -> None:
    """Plan (read only): facts of the exact target revision + the request → model → W4 edit ops, dry-run with
    ``assemblies.impact`` (repairs within budget). The context manifest is committed before the model call; the calls
    are recorded whatever the outcome. The plan is stored on the operation; nothing is applied."""
    from ..ai import assembly_edit_agent as agent
    from ..ai.providers.base import ProviderError
    from . import ai as ai_svc
    from . import assemblies as asm_svc
    from . import assistant_context as actx
    from .assistant import _lock_op, _model_ref, _on_terminal

    req = op.request or {}
    text: str = (req.get("text") or "").strip()
    project_access(db, user, op.project_id, "design")
    rev, asm, target = _target_revision(db, op)
    from .assistant_refs import assembly_instance_path
    path = (target.get("instance") or {}).get("path") or []
    if path:
        assembly_instance_path(db, rev.id, path, op.project_id)
        _check_selected_ops(target, [])
    facts = assembly_revision_facts(db, rev.id, op.project_id) or ""
    if path:
        facts += "\nThe person's exact selected instance path: " + "/".join(path) + ". Only this instance may change."
    constraints = [str(c.get("text") or "").strip() for c in req.get("constraints") or [] if isinstance(c, dict) and c.get("text")]
    user_msg = agent.user_message(text, target_key=str(target.get("key") or ""), title=f"{asm.number} A{rev.rev_no}", facts=facts,
                                  constraints=constraints)
    items = [actx._item("required", "system", "Assembly change rules", agent.SYSTEM_PROMPT),
             actx._item("required", "target", f"Target {asm.number} A{rev.rev_no}", facts, ref_key=target.get("key"), read="content",
                        ranges=["instances, mates and BOM of the exact revision"]),
             actx._item("required", "request", "Request", text)]
    items += [actx._item("required", "constraint", f"Constraint: {c}", c) for c in constraints]
    _record_manifest(db, op, items, purpose="assistant", model_ref=_model_ref(req))
    db.commit()  # the manifest stays on record whatever the model call does
    try:
        provider, _ = ai_svc.provider_for(db, "assistant", _model_ref(req))
    except ProviderError as exc:
        raise AppError(503, "ai_unavailable", "the AI model could not plan the change; check the model settings",
                       {"reason": exc.code, "detail": exc.message})

    from .assistant_cad_tools import stop_guard, bind_stop
    stop = stop_guard(op)
    bind_stop(provider, stop)
    db.commit()

    def preview(ops: list[dict[str, Any]]) -> list[str]:
        try:
            _check_selected_ops(target, ops)
            asm_svc.impact(db, assembly_id=asm.id, base_revision_id=rev.id, ops=ops)
        except AppError as exc:
            return [f"{exc.code}: {exc.message}"]
        finally:
            db.rollback()  # a dry run never keeps anything (counters are restored by impact; this is belt and braces)
        return []

    outcome = agent.run_assembly_edit(provider, user_msg, preview)
    ai_svc.record_calls(db, list(outcome.calls or []), None, ai_operation_id=op.id)
    db.commit()
    provider.check_live()
    from .lifecycle import require_active
    from .assistant_cad_tools import _save_lock
    require_active(db, op.project_id, what="saving an assembly plan")
    current = db.scalar(select(Assembly).where(Assembly.id == asm.id).with_for_update().execution_options(populate_existing=True))
    if current is None or current.archived_at is not None:
        raise not_found("assembly")
    op = _save_lock(db, user, op)
    if op.status != "pending":
        db.commit()
        return
    op.simulated = bool(outcome.simulated)
    if outcome.error:
        op.status, op.error = "failed", outcome.error
    else:
        rev, asm, _ = _target_revision(db, op)
        _check_selected_ops(target, outcome.ops)
        impact = asm_svc.impact(db, assembly_id=asm.id, base_revision_id=rev.id, ops=outcome.ops) if outcome.ops else None
        op.result = {
            "kind": "assembly_modify", "assembly_id": str(asm.id), "assembly_number": asm.number, "assembly_name": asm.name,
            "base_revision_id": str(rev.id), "base_rev_no": rev.rev_no, "summary": outcome.summary, "ops": outcome.ops,
            "questions": outcome.questions, "impact": impact, "removals": removals_of(outcome.ops, impact or {}),
            "execution_policy": op.execution_policy, "repairs": outcome.repairs, "application": None,
        }
        op.status = "succeeded"
    _on_terminal(db, op)
    db.commit()


def apply_assembly_modify(db: Session, user: Any, operation_id: uuid.UUID, *, confirm_removals: bool = False,
                          plan_hash: str | None = None, expected_head: uuid.UUID | None = None) -> AiOperation:
    """Apply a planned change: a new revision of the assembly against the plan's base (409 revision_conflict when the
    assembly moved on; never rebased silently). 409 removals_need_confirmation unless ``confirm_removals`` when the plan
    removes anything; 409 already_applied / nothing_to_apply / plan_not_ready. Flushes; the caller commits."""
    from . import assemblies as asm_svc
    from .assistant import _lock_op, _touch_conversations, load_operation, require_open, lock_conversation
    from .audit import audit
    from .lifecycle import require_active

    op, conv, _ = load_operation(db, user, operation_id)
    require_open(conv)
    project_access(db, user, op.project_id, "design")
    require_active(db, op.project_id, what="applying an assembly plan")
    assembly_id = _uuid((op.result or {}).get("assembly_id"))
    db.scalar(select(Assembly).where(Assembly.id == assembly_id, Assembly.project_id == op.project_id).with_for_update()
              .execution_options(populate_existing=True))
    op = _lock_op(db, op.id)
    require_open(lock_conversation(db, op.conversation_id))
    from .assistant_cad_tools import check_plan
    check_plan(op, plan_hash=plan_hash, expected_head=expected_head)
    res = op.result if isinstance(op.result, dict) else None
    if op.kind != "assembly_modify":
        raise conflict("not_assembly_modify", "only an assembly change plan can be applied here", kind=op.kind)
    if op.status != "succeeded" or res is None or res.get("kind") != "assembly_modify":
        raise conflict("plan_not_ready", "this request has no plan to apply", status=op.status)
    app = res.get("application") if isinstance(res.get("application"), dict) else {}
    if app.get("state") == "applied":
        if not res.get("plan_hash"):
            raise conflict("already_applied", "this plan was already applied", revision_id=app.get("revision_id"))
        return op
    if not res.get("ops"):
        raise conflict("nothing_to_apply", "the plan changes nothing")
    if res.get("removals") and not confirm_removals:
        raise conflict("removals_need_confirmation", "the plan removes instances or mates; confirm the removal to apply it",
                       removals=res["removals"])
    project_access(db, user, op.project_id, "design")
    asm = db.get(Assembly, _uuid(res.get("assembly_id")))
    if asm is None or asm.project_id != op.project_id or asm.archived_at is not None:
        raise not_found("assembly")
    _check_selected_ops((op.request or {}).get("target") or {}, res["ops"])
    turbo = op.execution_policy == "turbo"
    rev = asm_svc.edit_revision(db, assembly_id=asm.id, user=user, base_revision_id=uuid.UUID(res["base_revision_id"]), ops=res["ops"],
                                policy=op.execution_policy, note=f"AI change: {res.get('summary') or ''}"[:2000], source="ai",
                                run_heavy_checks=not turbo)
    audit(db, action="ai.assembly_modify.apply", subject_type="assembly_revision", subject_id=rev.id, user_id=user.id, project_id=op.project_id,
          data={"operation_id": str(op.id), "base_revision_id": res["base_revision_id"], "rev_no": rev.rev_no, "ops": len(res["ops"]),
                "removals_confirmed": bool(res.get("removals")), "simulated": bool(op.simulated)})
    op.result = {**res, "application": {"state": "applied", "by": str(user.id), "at": _now(), "revision_id": str(rev.id), "rev_no": rev.rev_no,
                                        "check_status": rev.check_status, "removals_confirmed": bool(res.get("removals")),
                                        "plan_hash": res.get("plan_hash"), "expected_head": str(expected_head) if expected_head else None}}
    from .cad_results import record

    record(db, op)
    from . import operation_events
    operation_events.emit(db, op, phase="applied")
    _touch_conversations(db, {op.conversation_id})
    db.flush()
    return op
