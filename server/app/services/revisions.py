"""Model revisions: build with the cloud kernel, store artifacts, version.

Manual edits, AI edits and imports all end here, so they share one revision
history, one validation path and one artifact store. Only successful builds
become revisions; STEP, GLB and mesh preview come from the same build.
"""

from __future__ import annotations

import json
import copy
import threading
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..cad.ops import OP_ID_RE, ModelSpec, StepBase
from ..config import get_settings
from ..errors import AppError, bad_request, conflict, not_found
from ..models import File, FileVersion, ModelRevision, Part, User
from ..storage import read_blob, save_blob
from . import assets
from .audit import audit

# OCCT is not reliably thread-safe across concurrent operations; serialise
# kernel work inside one process. Scale out with more worker processes.
KERNEL_LOCK = threading.Lock()


def kernel():
    from ..cad import kernel as k  # slow import (build123d / OCP)

    return k


def parse_spec(data: dict) -> ModelSpec:
    try:
        return ModelSpec.model_validate(data)
    except ValidationError as exc:
        raise AppError(
            422,
            "invalid_ops",
            "the modeling operations are invalid",
            {"errors": [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
        )


def spec_of(rev: ModelRevision) -> ModelSpec:
    return ModelSpec.model_validate({"base": rev.base, "ops": rev.ops})


def base_bytes(db: Session, spec: ModelSpec) -> bytes | None:
    """The bytes of the spec's imported base (None without one)."""
    if spec.base is None:
        return None
    try:
        data, _ = read_blob(db, spec.base.blob_sha256)
    except FileNotFoundError:
        raise bad_request("base_missing", "the imported base file is missing")
    return data


def kernel_build(spec: ModelSpec, base: bytes | None, *, export: bool = True):
    """The kernel build of ``spec`` on its imported base's bytes (read by the caller). No session: callable from a worker
    thread (``ai_modeling.design``); kernel work is serialised on :data:`KERNEL_LOCK`."""
    with KERNEL_LOCK:
        return kernel().build(spec, base, tolerance=get_settings().cad_tolerance, export=export)


def build_spec(db: Session, spec: ModelSpec, *, export: bool = True):
    return kernel_build(spec, base_bytes(db, spec), export=export)


def feature_check(spec: ModelSpec, result: Any, *, execution_policy: str | None = None) -> dict[str, Any] | None:
    """Independent W3 feature checks (cad/feature_checks.py) of a successful build, through ``policy.run_check``
    (``geometry.feature.<kind>``; Turbo records ``skipped_turbo``). None when the spec has no W3 feature."""
    from ..cad.feature_checks import check_features

    with KERNEL_LOCK:
        return check_features(spec, result, execution_policy=execution_policy)


def require_features_ok(report: dict[str, Any] | None) -> None:
    """A failed / unevaluable feature check refuses the save (422 ``feature_check_failed`` with what differed)."""
    if report is None or report.get("status") not in ("failed", "unknown"):
        return
    from ..cad.feature_checks import report_problems

    problems = report_problems(report)
    raise AppError(422, "feature_check_failed",
                   "the independent feature check does not confirm the built geometry: " + "; ".join(problems[:3]),
                   {"features": report, "problems": problems[:20]})


def datums_of(spec: ModelSpec, result: Any = None, template: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Semantic datums (cad/datums.py) as JSON: origin, op datums, template ports."""
    from ..cad.datums import datum_dict, datums_for

    return [datum_dict(d) for d in datums_for(spec, result, template=template)]


def carry_template(parent: ModelRevision | None, spec: ModelSpec) -> dict[str, Any] | None:
    """Template provenance of the parent, carried while its ops are still in the spec: unchanged ops keep the label
    as is, edited / removed ones are listed (the representation label stays honest about the origin). Ports were
    computed from the template parameters, so once a template op is edited or removed they no longer describe the
    geometry: they move to ``broken_ports`` (a mate on them reports broken, never re-bound; W0 §4)."""
    tpl = (parent.checks or {}).get("template") if parent is not None and isinstance(parent.checks, dict) else None
    if not isinstance(tpl, dict):
        return None
    before = {o.get("id"): o for o in parent.ops or []}
    now = {op.id: op.model_dump(mode="json") for op in spec.ops}
    ids = [i for i in tpl.get("op_ids") or [] if isinstance(i, str)]
    kept = [i for i in ids if i in now]
    if not kept:
        return None
    modified = sorted(set(tpl.get("modified_ops") or []) | {i for i in kept if now[i] != before.get(i)})
    removed = sorted(set(tpl.get("removed_ops") or []) | {i for i in ids if i not in now})
    out = {**tpl, "modified_ops": modified, "removed_ops": removed}
    if (modified or removed) and out.get("ports"):
        out["broken_ports"] = sorted({*(out.get("broken_ports") or []), *(str(p.get("name")) for p in out["ports"] if isinstance(p, dict))})
        out["ports"] = []
    return out


def check_step(step_bytes: bytes, requirements: list[Any], *, mode: str = "create") -> dict[str, Any]:
    """Independent requirement check (cad/conformance.py) of exported STEP bytes; the report as a dict.
    OCCT work, so it runs under KERNEL_LOCK like a build."""
    from ..cad.conformance import check_requirements

    with KERNEL_LOCK:
        return check_requirements(step_bytes, requirements, mode=mode).model_dump(mode="json")  # type: ignore[arg-type]


def audit_step_bytes(data: bytes):
    """Raw STEP source audit (cad/step_source.py) before any kernel import (design §3, N07)."""
    from ..cad.step_source import audit_step

    with KERNEL_LOCK:
        return audit_step(data)


#: Fields of a StepAudit kept in an import refusal (the per-solid list is summarised by its counts).
_AUDIT_BRIEF = ("audit_schema", "sha256", "size_bytes", "code", "message", "step_schema", "application_protocol", "originating_system",
                "length_unit", "products", "assemblies", "parts", "instances", "solids", "shells", "free_shells", "free_faces",
                "valid_solids", "volume_mm3", "bbox")


def audit_brief(step_audit: Any) -> dict[str, Any]:
    data = step_audit.model_dump(mode="json")
    out = {k: data.get(k) for k in _AUDIT_BRIEF}
    out["product_names"] = list(data.get("product_names") or [])[:10]
    out["warnings"] = [w.get("code") for w in data.get("warnings") or []]
    return out


#: sha256 of STEP bases whose source audit was accepted in this process (bounded; re-audited after eviction).
_AUDITED_BASES: dict[str, bool] = {}


def authorize_base(db: Session, project_id: uuid.UUID, spec: ModelSpec) -> None:
    """A caller-supplied STEP base (manual save, editor preview) must be a STEP of this project: the base or the STEP
    of one of its revisions, or a file version of one of its files. Anything else is 404, the same answer as an
    unknown hash (another project's blob is never read by hash). A base no revision of the project uses yet has not
    been through an import: its raw bytes are audited first (``audit_step``), and a refusal is a 422 with the audit
    code like an import (design §3, N07, N11)."""
    if spec.base is None:
        return
    sha = spec.base.blob_sha256
    used = db.scalar(
        select(ModelRevision.id).join(Part, Part.id == ModelRevision.part_id)
        .where(Part.project_id == project_id,
               (ModelRevision.step_sha == sha) | ModelRevision.base.contains({"blob_sha256": sha}))
        .limit(1)
    )
    if used is not None:
        return  # a base imported (and audited) in this project, or a STEP the kernel exported for it
    fv = db.scalar(
        select(FileVersion).join(File, File.id == FileVersion.file_id)
        .where(File.project_id == project_id, FileVersion.blob_sha256 == sha).limit(1)
    )
    if fv is None:
        raise not_found("base STEP")
    if _AUDITED_BASES.get(sha):
        return
    try:
        data, _ = read_blob(db, sha)
    except FileNotFoundError:
        raise bad_request("base_missing", "the imported base file is missing")
    src_audit = audit_step_bytes(data)
    if not src_audit.accepted:
        raise AppError(422, src_audit.code, src_audit.message, {"audit": audit_brief(src_audit), "file_version_id": str(fv.id)})
    if len(_AUDITED_BASES) > 256:
        _AUDITED_BASES.clear()
    _AUDITED_BASES[sha] = True


_PREVIEW_CACHE: dict[str, tuple[dict, bytes]] = {}
_PREVIEW_ORDER: list[str] = []


def preview(db: Session, spec: ModelSpec) -> dict[str, Any]:
    """Build without saving. Small in-process cache keyed by build_key."""
    k = kernel()
    from ..cad.ops import build_key

    key = build_key(spec, k.KERNEL_VERSION)
    if key not in _PREVIEW_CACHE:
        result = build_spec(db, spec)
        summary = result.summary()
        # W3: the independent feature check and the datums of the draft, so the editor shows them before saving
        summary["feature_check"] = feature_check(spec, result) if result.ok else None
        summary["datums"] = datums_of(spec, result)
        _PREVIEW_CACHE[key] = (summary, result.artifacts.mesh_json)
        _PREVIEW_ORDER.append(key)
        if len(_PREVIEW_ORDER) > 32:
            _PREVIEW_CACHE.pop(_PREVIEW_ORDER.pop(0), None)
    summary, mesh = _PREVIEW_CACHE[key]
    return {"result": summary, "mesh": json.loads(mesh) if mesh else None}


def _op_numbers(ops: list[dict]) -> set[int]:
    return {int(o["id"][2:]) for o in ops if OP_ID_RE.match(o.get("id", ""))}


def _check_new_ids(part: Part, parent: ModelRevision | None, spec: ModelSpec) -> None:
    """Ids never come back: an id absent from the parent must be brand new."""
    parent_ids = {o["id"] for o in (parent.ops if parent else [])}
    for op in spec.ops:
        if op.id not in parent_ids and int(op.id[2:]) <= part.op_counter:
            raise AppError(
                422,
                "op_id_reused",
                f"{op.id} was used before in this part; new operations must use ids after F-{part.op_counter:02d}",
                {"op_id": op.id, "next": f"F-{part.op_counter + 1:02d}"},
            )


def lock_part(db: Session, part_id: uuid.UUID) -> Part:
    # populate_existing: values read after the lock are the committed ones, not a stale identity-map copy.
    part = db.scalar(select(Part).where(Part.id == part_id).with_for_update().execution_options(populate_existing=True))
    if part is None:
        raise not_found("part")
    return part


def create_revision(
    db: Session,
    *,
    part_id: uuid.UUID,
    user: User,
    spec: ModelSpec,
    source: str,
    parent_id: uuid.UUID | None = None,
    note: str = "",
    request_text: str | None = None,
    ai_summary: dict | None = None,
    node_run_id: uuid.UUID | None = None,
    source_file_version_id: uuid.UUID | None = None,
    result=None,
    requirements: list[dict] | None = None,
    checks: dict | None = None,
    execution_policy: str | None = None,
    template: dict | None = None,
) -> ModelRevision:
    """Build (unless a successful `result` for this spec is given) and save.

    ``requirements`` (the accepted requirement list, JSON) and ``checks`` (server reports bound to this
    revision's STEP: ``conformance`` for AI revisions, ``step_audit`` for imports) are stored as given.

    W3: a spec with geometry-library features gets ``checks["features"]`` (cad/feature_checks.py through
    ``policy.run_check`` under ``execution_policy``, default standard); a failed / unevaluable report refuses the
    save (422 ``feature_check_failed``). ``template`` (meta of ``cad.templates.instantiate``) or the parent's
    template provenance (while its ops remain) is stored as ``checks["template"]``."""
    part = lock_part(db, part_id)
    parent = db.get(ModelRevision, parent_id) if parent_id else None
    if parent_id and (parent is None or parent.part_id != part.id):
        raise not_found("parent revision")
    _check_new_ids(part, parent, spec)
    if result is None:
        result = build_spec(db, spec)
    if not result.ok:
        raise AppError(
            422,
            "build_failed",
            "the cloud kernel could not build this model",
            {"errors": [e.model_dump() for e in result.errors]},
        )
    extra: dict[str, Any] = {}
    if not (isinstance(checks, dict) and "features" in checks):
        features = feature_check(spec, result, execution_policy=execution_policy)
        require_features_ok(features)
        if features is not None:
            extra["features"] = features
    tpl = template if template is not None else carry_template(parent, spec)
    if tpl is not None:
        extra["template"] = tpl
    if template is not None:
        _template_part_props(db, part, user, template)
    # generation reliability M (design §2.7): what this build made and did not make, and its representation level; a
    # substituted font is also a recorded rule decision (requested, used, impact) like any other deviation
    extra["build_record"] = _build_record(db, part, result, requirements, tpl)
    fonts = font_deviations(result.warnings)
    if fonts:
        extra["assumptions"] = [*((checks or {}).get("assumptions") or []), *fonts]
    if extra:
        checks = {**(checks or {}), **extra}
    a = result.artifacts
    rev_no = (db.scalar(select(func.max(ModelRevision.rev_no)).where(ModelRevision.part_id == part.id)) or 0) + 1
    data = result.model_dump(mode="json")
    rev = ModelRevision(
        part_id=part.id,
        rev_no=rev_no,
        parent_id=parent.id if parent else None,
        source=source,
        base=spec.base.model_dump(mode="json") if spec.base else None,
        ops=[o.model_dump(mode="json") for o in spec.ops],
        status="ready",
        build_key=result.build_key,
        kernel_version=result.kernel_version,
        metrics=data["metrics"],
        features=data["features"],
        holes=data["holes"],
        detected_holes=data.get("detected_holes", []),
        errors=[],
        warnings=data["warnings"],
        step_sha=save_blob(db, a.step, "model/step"),
        glb_sha=save_blob(db, a.glb, "model/gltf-binary"),
        mesh_sha=save_blob(db, a.mesh_json, "application/json"),
        material=part.material,
        request_text=request_text,
        ai_summary=ai_summary,
        note=note,
        node_run_id=node_run_id,
        source_file_version_id=source_file_version_id,
        requirements=requirements,
        checks=checks,
        created_by=user.id,
    )
    db.add(rev)
    numbers = _op_numbers(rev.ops)
    if numbers:
        part.op_counter = max(part.op_counter, max(numbers))
    if part.status == "draft":
        part.status = "in_design"
    db.flush()
    assets.register_revision(db, rev)  # same transaction as the revision (labels: imported / simulated)
    audit(db, action=f"revision.{source}", subject_type="model_revision", subject_id=rev.id, user_id=user.id, project_id=part.project_id, data={"rev_no": rev_no, "build_key": result.build_key})
    return rev


def restore(db: Session, *, part_id: uuid.UUID, user: User, base_revision_id: uuid.UUID,
            revision_id: uuid.UUID, execution_policy: str | None = None) -> ModelRevision:
    """Rebuild a historical model as a new revision, without resurrecting deleted feature identities."""
    from .. import policy
    from ..cad.ops import remap_refs
    from ..cad.requirements import requirements_sha256

    part = lock_part(db, part_id)
    head = db.scalar(select(ModelRevision).where(ModelRevision.part_id == part.id)
                     .order_by(ModelRevision.rev_no.desc()).limit(1))
    if head is None or head.id != base_revision_id:
        raise conflict("head_changed", "the model changed after the operation finished")
    target = db.get(ModelRevision, revision_id)
    if target is None or target.part_id != part.id:
        raise not_found("model revision")
    if target.status != "ready" or target.mesh_only:
        raise conflict("restore_unavailable", "this revision has no restorable exact model")

    ops = copy.deepcopy(target.ops or [])
    active_ids = {o["id"] for o in head.ops or []}
    next_id = part.op_counter
    mapping: dict[str, str] = {}
    for op in ops:
        if op["id"] not in active_ids:
            next_id += 1
            mapping[op["id"]] = f"F-{next_id:02d}"
    for op in ops:
        op["id"] = mapping.get(op["id"], op["id"])
        remap_refs(op, mapping)
    spec = parse_spec({"base": target.base, "ops": ops})
    built = build_spec(db, spec)
    if not built.ok:
        raise AppError(422, "build_failed", "the historical model could not be rebuilt",
                       {"errors": [e.model_dump() for e in built.errors]})
    pol = policy.parse_policy(execution_policy)
    reqs = copy.deepcopy(target.requirements)
    checks = {k: copy.deepcopy(v) for k, v in (target.checks or {}).items()
              if k not in ("conformance", "features", "build_record", "template", "execution_policy")}
    checks.update(execution_policy=pol, restored_from={"revision_id": str(target.id), "feature_id_map": mapping})
    if reqs is not None:
        report = policy.run_check("model.conformance", pol,
                                  lambda: check_step(built.artifacts.step, reqs, mode=checks.get("mode") or "create"))
        if report.get("status") not in ("passed", "skipped_turbo"):
            raise AppError(422, "requirements_not_met", "the restored model failed its requirement check", {"report": report})
        checks.update(conformance=report, requirements_sha256=requirements_sha256(reqs))
    template = copy.deepcopy((target.checks or {}).get("template"))
    if isinstance(template, dict):
        template["op_ids"] = [mapping.get(i, i) for i in template.get("op_ids", [])]
    rev = create_revision(db, part_id=part.id, user=user, spec=spec, source="manual", parent_id=head.id,
                          note=f"Restored R{target.rev_no}", result=built, requirements=reqs, checks=checks,
                          ai_summary=copy.deepcopy(target.ai_summary), execution_policy=pol, template=template,
                          source_file_version_id=target.source_file_version_id)
    rev.source_revision_id = target.id
    version = assets.version_for(db, "part_model", rev.id)
    assert version is not None
    version.labels = list(dict.fromkeys([*(version.labels or []), *assets.revision_labels(db, rev)]))
    audit(db, action="revision.restore", subject_type="model_revision", subject_id=rev.id, user_id=user.id,
          project_id=part.project_id, data={"restored_from_id": str(target.id), "parent_id": str(head.id), "feature_id_map": mapping})
    db.flush()
    return rev


def _build_record(db: Session, part: Part, result: Any, requirements: list[dict] | None, template: dict[str, Any] | None) -> dict[str, Any]:
    """The build record of a new revision (:mod:`app.cad.build_record`): the kernel's feature records and warnings
    (font substitutions), the accepted requirements, and the representation level of the template / part property;
    with the capability codes the kernel builds now (C, design §6.3: what a later reuse compares against)."""
    from ..cad.build_record import build_record, worst
    from ..cad.capabilities import available
    from ..models import AssemblyPartProps

    row = db.get(AssemblyPartProps, part.id)
    metrics = result.metrics
    usable = bool(result.ok and metrics is not None and metrics.valid and metrics.solids >= 1 and result.artifacts.step)
    return build_record(features=result.features, warnings=result.warnings, requirements=requirements, usable=usable,
                        representation=worst((template or {}).get("representation"), row.representation if row else None),
                        capabilities=available())


def font_deviations(warnings: Any) -> list[dict[str, Any]]:
    """One ``rule`` decision (policy.Assumption) per ``font_substituted`` warning of a build: the requested font was not
    installed (or lacked glyphs), the kernel used the first installed CJK font covering the text."""
    from ..policy import Assumption

    out = []
    for w in warnings or []:
        get = w.get if isinstance(w, dict) else (lambda k, w=w: getattr(w, k, None))
        if get("code") != "font_substituted":
            continue
        d = get("details") or {}
        op = get("op_id") or "?"
        why = "is not installed" if d.get("reason") == "not_installed" else "lacks glyphs of the text"
        out.append(Assumption(id=f"A-{op}-font", subject=f"{op}.font", field="font", adopted=d.get("used"), author="rule",
                              trigger="missing", alternatives=[d.get("requested")], affected=[op],
                              basis=f"the requested font {d.get('requested')} {why}; {d.get('used')} is the first installed CJK font "
                                    f"of the fallback order that covers \"{d.get('text') or ''}\"; impact: {d.get('impact')}").model_dump())
    return out


def build_record_of(db: Session, rev: ModelRevision) -> dict[str, Any]:
    """The revision's build record: the stored one (``checks["build_record"]``), or one derived from the stored
    feature records, warnings and requirements for a revision made before records existed (without a ``capabilities``
    block: what that build could make is not known); the representation is lowered to the part property / revision
    level when that was set later (a spec definition's level)."""
    from ..cad.build_record import build_record, with_representation
    from ..models import AssemblyPartProps

    checks = rev.checks if isinstance(rev.checks, dict) else {}
    tpl = checks.get("template") if isinstance(checks.get("template"), dict) else {}
    row = db.get(AssemblyPartProps, rev.part_id)
    rec = checks.get("build_record")
    if not isinstance(rec, dict):
        rec = build_record(features=rev.features, warnings=rev.warnings, requirements=rev.requirements, mesh_only=bool(rev.mesh_only),
                           usable=bool(rev.step_sha) and (rev.metrics or {}).get("valid") is not False)
    return with_representation(rec, tpl.get("representation"), row.representation if row else None, getattr(rev, "representation", None))


def _template_part_props(db: Session, part: Part, user: User, template: dict[str, Any]) -> None:
    """A part without part-level properties takes the representation level (exact / nominal / simplified) and the
    procurement of the template its revision was made from, so BOMs, assembly drawings and deliveries label it
    honestly (a spring is ``nominal``, not ``exact``). A field someone already set (procurement other than unknown,
    representation other than exact) is never overwritten."""
    from ..models import AssemblyPartProps
    from .assemblies import set_part_props

    row = db.get(AssemblyPartProps, part.id)
    rep, proc = template.get("representation"), template.get("procurement")
    # per field: a procurement / representation stated earlier (e.g. by the spec definition) stays
    proc = proc if proc in ("make", "buy", "standard") and (row is None or row.procurement == "unknown") else None
    rep = rep if rep in ("exact", "nominal", "simplified") and (row is None or row.representation == "exact") else None
    if rep or proc:
        set_part_props(db, part, user, procurement=proc, representation=rep, source={"template": template.get("template_id")})


def instantiate_template(template_id: str, params: dict[str, Any] | None) -> tuple[ModelSpec, dict[str, Any]]:
    """``cad.templates.instantiate`` with its errors as 422 (``template_unknown`` / ``template_param_invalid``)."""
    from ..cad.templates import TemplateError, instantiate

    try:
        return instantiate(template_id, params)
    except TemplateError as exc:
        raise AppError(422 if exc.code != "template_unknown" else 404, exc.code, exc.message, {"problems": exc.problems})


def create_template_revision(
    db: Session,
    *,
    part_id: uuid.UUID,
    user: User,
    template_id: str,
    params: dict[str, Any] | None,
    parent_id: uuid.UUID | None = None,
    mode: str = "replace",
    note: str = "",
    execution_policy: str | None = None,
) -> ModelRevision:
    """A revision from a part template (W3). ``replace``: the template's ops alone (no base) — a new design of the
    part; ``append``: the parent's ops (and base) followed by the template's ops (its first body joins with
    ``add``). Op ids continue after every id the part ever used; the template provenance (representation,
    procurement, material source, spec label, ports, op ids) is stored as ``checks["template"]``."""
    from ..cad.templates import renumbered

    part = lock_part(db, part_id)
    parent = db.get(ModelRevision, parent_id) if parent_id else None
    if parent_id and (parent is None or parent.part_id != part.id):
        raise not_found("parent revision")
    if mode not in ("replace", "append"):
        raise bad_request("invalid_mode", "mode must be replace or append")
    spec, meta = instantiate_template(template_id, params)
    first = max([part.op_counter] + [int(o["id"][2:]) for o in (parent.ops if parent else []) if OP_ID_RE.match(o.get("id", ""))]) + 1
    spec, meta = renumbered(spec, meta, first)
    if mode == "append" and parent is not None:
        tops = [o.model_dump(mode="json") for o in spec.ops]
        for o in tops:
            if o.get("mode") == "new":
                o["mode"] = "add"
        spec = parse_spec({"base": parent.base, "ops": list(parent.ops) + tops})
        if spec.base is not None:
            authorize_base(db, part.project_id, spec)
    return create_revision(db, part_id=part.id, user=user, spec=spec, source="manual", parent_id=parent.id if parent else None,
                           note=note or f"Template {meta['spec_label']}", template=meta, execution_policy=execution_policy)


def import_file_version(db: Session, *, part_id: uuid.UUID, user: User, file_version_id: uuid.UUID, node_run_id: uuid.UUID | None = None,
                        dxf_profile: dict[str, Any] | None = None) -> ModelRevision:
    """STEP becomes an editable base (F-00); STL/OBJ/GLB become mesh references; a DXF / DWG drawing becomes a plate
    only through the strict PROFILE import (``dxf_profile``: layer, thickness_mm, scale, base_z[, units]); without it
    422 drawing_needs_reconstruct (AI reconstruction is a separate operation)."""
    from . import drawing_input

    part = lock_part(db, part_id)
    fv = db.get(FileVersion, file_version_id)
    f = db.get(File, fv.file_id) if fv else None
    if fv is None or f is None or f.project_id != part.project_id:
        raise not_found("file version")
    name = fv.original_name.lower()
    data, _ = read_blob(db, fv.blob_sha256)
    if f.kind == "drawing_dxf" or drawing_input.sniff(name, data) is not None:
        return _import_profile(db, part=part, user=user, fv=fv, data=data, dxf_profile=dxf_profile, node_run_id=node_run_id)
    if dxf_profile is not None:
        raise bad_request("not_a_drawing", "dxf_profile applies to DXF / DWG drawings only")
    if f.kind == "model_step" or name.endswith((".step", ".stp")):
        # Audit the raw bytes first: an assembly, several bodies or no / an invalid solid is refused here,
        # before the kernel's import path could fuse or repair anything (design §0.6, §3).
        src_audit = audit_step_bytes(data)
        if not src_audit.accepted:
            raise AppError(422, src_audit.code, src_audit.message, {"audit": audit_brief(src_audit), "file_version_id": str(fv.id)})
        spec = ModelSpec(base=StepBase(blob_sha256=fv.blob_sha256), ops=[])
        with KERNEL_LOCK:
            result = kernel().build(spec, data, tolerance=get_settings().cad_tolerance)
        if not result.ok:
            raise AppError(422, "import_failed", "the STEP file could not be imported", {"errors": [e.model_dump() for e in result.errors]})
        return create_revision(db, part_id=part.id, user=user, spec=spec, source="import", note=f"Imported {fv.original_name}", node_run_id=node_run_id,
                               source_file_version_id=fv.id, result=result, checks={"step_audit": src_audit.model_dump(mode="json")})

    ext = next((e for e in ("stl", "obj", "glb") if name.endswith("." + e)), None)
    if ext is None:
        raise bad_request("unsupported_model_file", "only STEP, STL, OBJ and GLB files can be imported as models")
    from ..cad.importers import inspect_mesh

    insp = inspect_mesh(data, ext)  # type: ignore[arg-type]
    rev_no = (db.scalar(select(func.max(ModelRevision.rev_no)).where(ModelRevision.part_id == part.id)) or 0) + 1
    bbox = insp.bbox.model_dump() if insp.bbox else None
    preview_sha = save_blob(db, insp.preview, "model/gltf-binary" if insp.preview_format == "glb" else "application/json")
    rev = ModelRevision(
        part_id=part.id,
        rev_no=rev_no,
        source="import",
        base=None,
        ops=[],
        status="ready",
        build_key=None,
        kernel_version=None,
        metrics={
            "valid": False,
            "volume_mm3": insp.volume_mm3,
            "area_mm2": insp.area_mm2,
            "bbox": bbox,
            "triangles": insp.triangles,
            "watertight": insp.watertight,
            "units_assumed": insp.units_assumed,
        },
        features=[],
        holes=[],
        detected_holes=[],
        errors=[],
        warnings=[w.model_dump() for w in insp.warnings],
        step_sha=None,
        glb_sha=preview_sha if insp.preview_format == "glb" else None,
        mesh_sha=preview_sha if insp.preview_format == "mesh_json" else None,
        mesh_only=True,
        material=part.material,
        note=f"Mesh reference {fv.original_name} (not editable, no STEP)",
        node_run_id=node_run_id,
        source_file_version_id=fv.id,
        created_by=user.id,
    )
    db.add(rev)
    db.flush()
    assets.register_revision(db, rev)  # labels: mesh_reference, imported
    audit(db, action="revision.import_mesh", subject_type="model_revision", subject_id=rev.id, user_id=user.id, project_id=part.project_id)
    return rev


#: Deterministic check of a PROFILE import: the built solid against the importer's plain arithmetic.
PROFILE_CHECK_TOL = {"linear_mm": 0.01, "volume_rel": 1e-5}


def profile_check(metrics: Any, expected: Any) -> dict[str, Any]:
    """Built volume / bbox vs the report's expected values (cad/profile_import.ProfileExpected)."""
    m = metrics.model_dump() if hasattr(metrics, "model_dump") else dict(metrics or {})
    e = expected.model_dump() if hasattr(expected, "model_dump") else dict(expected or {})
    items: list[dict[str, Any]] = []
    vol, exp_vol = float(m.get("volume_mm3") or 0.0), float(e.get("volume_mm3") or 0.0)
    tol_v = max(1e-6, PROFILE_CHECK_TOL["volume_rel"] * abs(exp_vol))
    items.append({"name": "volume_mm3", "expected": exp_vol, "measured": vol, "tolerance": tol_v,
                  "status": "passed" if abs(vol - exp_vol) <= tol_v else "failed"})
    bbox = m.get("bbox") or {}
    for key, measured in (("bbox_min", bbox.get("min")), ("bbox_max", bbox.get("max"))):
        want = list(e.get(key) or [])
        got = list(measured or [])
        ok = len(want) == 3 and len(got) == 3 and all(abs(float(a) - float(b)) <= PROFILE_CHECK_TOL["linear_mm"] for a, b in zip(got, want))
        items.append({"name": key, "expected": want, "measured": got, "tolerance": PROFILE_CHECK_TOL["linear_mm"],
                      "status": "passed" if ok else "failed"})
    return {"schema": "partroute.profile_check/1", "status": "passed" if all(i["status"] == "passed" for i in items) else "failed",
            "items": items}


def _import_profile(db: Session, *, part: Part, user: User, fv: FileVersion, data: bytes, dxf_profile: dict[str, Any] | None,
                    node_run_id: uuid.UUID | None) -> ModelRevision:
    """Strict DXF PROFILE import (cad/profile_import.py, no AI): the closed contours of one layer extruded to a plate.
    A DWG is read with the LibreDWG adapter first (derived DXF + provenance). Refusals are 422 with the profile code;
    the revision is saved only when the built volume / bbox equal the importer's expected values."""
    from ..cad.profile_import import ProfileRefusal, profile_to_spec
    from . import drawing_input

    fmt = drawing_input.drawing_format(fv.original_name, data)
    if dxf_profile is None:
        raise AppError(422, "drawing_needs_reconstruct",
                       "a 2D drawing is not a 3D model: import a plate from its PROFILE layer (give dxf_profile with layer, "
                       "thickness_mm and scale), or reconstruct the part with AI (assistant operation drawing_reconstruct, or the "
                       "workflow node drawing_reconstruct)",
                       {"file_version_id": str(fv.id), "format": fmt,
                        "options": {"profile_import": {"dxf_profile": ["layer", "thickness_mm", "scale", "base_z", "units"]},
                                    "reconstruct": {"assistant_operation": "drawing_reconstruct", "workflow_node": "drawing_reconstruct"}}})
    dxf, dwg = drawing_input.dwg_to_dxf(db, data) if fmt == "dwg" else (data, None)
    source = drawing_input.provenance(fmt=fmt, name=fv.original_name, data=data, dxf=dxf, dwg=dwg, file_version_id=fv.id)
    optional = {k: dxf_profile[k] for k in ("layer", "base_z", "units") if dxf_profile.get(k) is not None}
    try:  # thickness and scale have no defaults: None is refused (profile_missing_parameter)
        imp = profile_to_spec(dxf, thickness_mm=dxf_profile.get("thickness_mm"), scale=dxf_profile.get("scale"), **optional)
    except ProfileRefusal as exc:
        raise AppError(422, exc.code, exc.message, {"profile": exc.report.model_dump(mode="json"), "file_version_id": str(fv.id),
                                                    "source": source})
    spec = imp.spec
    if part.op_counter > 0:  # ids are never reused on a part with history
        from .ai_modeling import renumber

        spec = renumber(spec, part.op_counter)
    result = build_spec(db, spec)
    if not result.ok:
        raise AppError(422, "import_failed", "the profile could not be built", {"errors": [e.model_dump() for e in result.errors],
                                                                                 "profile": imp.report.model_dump(mode="json")})
    report = imp.report.model_dump(mode="json")
    check = profile_check(result.metrics, imp.report.expected)
    if check["status"] != "passed":
        raise AppError(422, "profile_check_failed", "the built plate does not match the profile's expected volume / bounding box",
                       {"check": check, "profile": report})
    return create_revision(db, part_id=part.id, user=user, spec=spec, source="import",
                           note=f"Imported {fv.original_name} (PROFILE layer {imp.report.layer}, {imp.report.thickness_mm:g} mm, "
                                f"scale {imp.report.scale:g})",
                           node_run_id=node_run_id, source_file_version_id=fv.id, result=result,
                           checks={"profile": report, "profile_check": check, "source": source})


def revision_out(rev: ModelRevision, part: Part | None = None) -> dict[str, Any]:
    from .verification import revision_verification

    return {
        "verification": revision_verification(rev),
        "id": str(rev.id),
        "part_id": str(rev.part_id),
        "rev_no": rev.rev_no,
        "label": f"R{rev.rev_no}",
        "parent_id": str(rev.parent_id) if rev.parent_id else None,
        "source": rev.source,
        "status": rev.status,
        "base": rev.base,
        "ops": rev.ops,
        "build_key": rev.build_key,
        "kernel_version": rev.kernel_version,
        "metrics": rev.metrics,
        "features": rev.features,
        "holes": rev.holes,
        "detected_holes": rev.detected_holes,
        "warnings": rev.warnings,
        "step_sha": rev.step_sha,
        "glb_sha": rev.glb_sha,
        "mesh_sha": rev.mesh_sha,
        "mesh_only": rev.mesh_only,
        "editable": not rev.mesh_only,
        "material": rev.material,
        "request_text": rev.request_text,
        "ai_summary": rev.ai_summary,
        "requirements": rev.requirements,
        "checks": rev.checks,
        "check_status": check_status(rev),
        "note": rev.note,
        "source_revision_id": str(rev.source_revision_id) if rev.source_revision_id else None,
        "source_file_version_id": str(rev.source_file_version_id) if rev.source_file_version_id else None,
        "node_run_id": str(rev.node_run_id) if rev.node_run_id else None,
        "created_by": str(rev.created_by),
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
        "next_op_id": f"F-{(part.op_counter if part else 0) + 1:02d}" if part else None,
    }


def check_status(rev: ModelRevision) -> str | None:
    """Compact verdict of the revision's requirement check: passed / failed / not_evaluated (an AI revision whose
    requirements have nothing geometric to check), or None when the revision has no requirement contract (manual
    edits, imports, revisions from before the contract)."""
    report = rev.checks.get("conformance") if isinstance(rev.checks, dict) else None
    if isinstance(report, dict):
        return report.get("status")  # skipped_turbo for a Turbo revision (W7): never a verdict
    return "not_evaluated" if rev.requirements is not None else None


def diff(a: ModelRevision, b: ModelRevision) -> dict[str, Any]:
    from ..cad.diff import diff_metrics, diff_specs

    changes = [] if (a.mesh_only or b.mesh_only) else [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in diff_specs(spec_of(a), spec_of(b))]
    metrics = diff_metrics(a.metrics, b.metrics) if (a.metrics and b.metrics) else {}
    return {"from": str(a.id), "to": str(b.id), "ops": changes, "metrics": metrics}
