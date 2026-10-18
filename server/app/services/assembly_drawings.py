"""Assembly drawing packages (W5 service): GA + detail sheets of one exact assembly revision, stored and exported.

``generate`` is the one producer (workspace route; W8 workflow / agent entries call it with ``node_run_id``):

1. resolve the exact revision with W4's ``resolve_geometry`` / ``bom_lines`` (W0 §4) and freeze the drawing
   configuration (title defaults from the assembly, the revision date and the user; DWG always required);
2. ``cad.assembly_drawing.make_assembly_drawing_package`` with the configured LibreDWG runtime, in a child
   process (:data:`ISOLATE`) bounded by a hard timeout derived from the W0 §7 sheet budget. Inside, a document
   that overran ``PAGE_BUDGET_S`` per sheet fails with ``page_budget_exceeded`` and its sheets are withheld; a
   failed page or format keeps the other pages and makes the package ``partial`` (a DXF or a preview never
   stands in for a failed DWG). The business checks (``drawing.annotation``, ``drawing.dwg_semantic``) run
   through ``policy.run_check``; the child's check ledger is replayed here so ``checks_ledger`` sees them;
3. one savepoint: every page file and check report as a blob (hash = manifest hash), the immutable
   :class:`~app.models.AssemblyDrawing` row and its ``assembly_drawing`` asset version derived from the
   revision's ``assembly_model`` version (W4's downstream / impact then report it stale).

``apply_overlay`` rebuilds the stored package from its manifest and blobs (``restore_package``, hash-verified),
applies layout / annotation edits and stores the result as the next version of the same package. Edits that
would change an engineering value raise 422 ``engineering_change_required``: the model / assembly is changed
instead and the drawings regenerated.

A partial assembly revision (generation supplement B-03, ``spec_ref.coverage`` partial) is drawn as such: the title
block and the first notes say PARTIAL ASSEMBLY in Chinese and English with the missing items and the AI decision count, the BOM
lists included and target quantities and keeps a row (target item number, no balloon) per missing item, and the
manifest carries the coverage. Every other revision is drawn exactly as before.

A package is stale when its revision is no longer the assembly's latest ready revision or a component of the
revision has a newer definition revision; stale packages stay readable and downloadable. ``package_entries`` is
the one export path (page files, check reports, ``manifest.json``), hash-verified on the way out.

Resume (S3): ``generation.progress`` records per document (generated | reused | failed) and per sheet what the
generation did. ``continue_package`` (and a retry of the same node run) makes a new package of the same revision,
frozen configuration and policy from a partial one: documents that came out whole are kept byte for byte with their
check records, sheets that only lack their DWG are converted again, failed documents are generated again
(``resumed_from_id``; one continuation per package). ``failure_info`` gives ``{stage, code, message, retryable,
recovery: retry_sheet}`` for a partial package.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import math
import multiprocessing
import pickle
import re
import time
import uuid
import zipfile
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import checks_ledger
from .. import policy as exec_policy
from ..cad import assembly_drawing as ad
from ..config import get_settings
from ..domain.hashing import canonical_json
from ..errors import AppError, bad_request, conflict, not_found
from ..models import Assembly, AssemblyDrawing, AssemblyRevision, Project, User
from ..storage import read_blob, save_blob
from . import assemblies, assets
from .audit import audit
from .drawings import PackageEntry, write_package

#: Run the CAD generation in a spawned child process with a hard timeout (W0 §7). Tests and callers that already
#: run inside an isolated worker may pass ``isolated=False`` (then only the per-document budget applies).
ISOLATE = True
#: Process start-up, imports, STEP reads and result transfer on top of the sheet budgets.
SPAWN_SLACK_S = 60.0
#: Detail documents count as two sheets (overview + section) when the hard limit is planned.
DETAIL_SHEETS_PLANNED = 2
REQUIRED_FORMATS = ["dxf", "svg", "dwg"]
REPORT_TYPE = "application/json"
MANIFEST_PATH = "manifest.json"
#: Config keys the service owns (never taken from a request).
SERVICE_KEYS = ("formats", "dwg_timeout_s")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
ENGINEERING_HINT = ("Layout edits never change the model. To change a dimension value, a quantity, an item or the geometry, "
                    "change the assembly or part model (a new revision) and regenerate the drawings.")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def _config(db: Session, asm: Assembly, rev: AssemblyRevision, user: User, config: dict[str, Any] | None) -> ad.AssemblyDrawingConfig:
    data = dict(config or {})
    for k in SERVICE_KEYS:
        data.pop(k, None)
    project = db.get(Project, asm.project_id)
    title = {"author": user.name, "date": rev.created_at.date().isoformat() if rev.created_at else "",
             "project": project.code if project else "", **(data.pop("title", None) or {})}
    try:
        return ad.AssemblyDrawingConfig.model_validate({**data, "title": title, "formats": REQUIRED_FORMATS,
                                                        "dwg_timeout_s": get_settings().dwg_timeout_s})
    except ValidationError as exc:
        raise AppError(422, "invalid_drawing_config", str(exc)[:500])


def _planned_sheets(bom: dict[str, list[Any]], cfg: ad.AssemblyDrawingConfig) -> int:
    rows = len(bom.get(cfg.bom_view) or [])
    ga = 1 + (math.ceil((rows - ad.MAX_GA_BOM_ROWS) / ad.MAX_BOM_SHEET_ROWS) if rows > ad.MAX_GA_BOM_ROWS else 0)
    details = sum(1 for ln in bom.get("flattened") or [] if ln.procurement in cfg.detail_procurements) if cfg.details else 0
    return ga + DETAIL_SHEETS_PLANNED * details


def hard_limit_s(planned_sheets: int, cfg: ad.AssemblyDrawingConfig) -> float:
    """The whole generation's wall-clock bound: the sheet budget plus one DWG conversion per planned sheet."""
    per_sheet = ad.PAGE_BUDGET_S + (cfg.dwg_timeout_s if "dwg" in cfg.formats else 0.0)
    return per_sheet * max(1, planned_sheets) + SPAWN_SLACK_S


# --------------------------------------------------------------------------- #
# generation (isolated child process)
# --------------------------------------------------------------------------- #


def _safe_step_read_context(value: Any) -> dict[str, Any]:
    """Only identifiers and byte evidence may accompany a failed child STEP read."""
    if not isinstance(value, dict) or value.get("stage") != "step_read":
        return {}
    digest, size = value.get("step_sha256"), value.get("byte_count")
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(size) is not int or size < 0):
        return {}
    out: dict[str, Any] = {"stage": "step_read", "step_sha256": digest, "byte_count": size}
    key = value.get("definition_key")
    if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
        out["definition_key"] = key
    revision_id = value.get("model_revision_id")
    if isinstance(revision_id, str):
        try:
            out["model_revision_id"] = str(uuid.UUID(revision_id))
        except ValueError:
            pass
    expected = value.get("source_step_sha256")
    if isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected):
        out["source_step_sha256"] = expected
        out["sha_matches_source"] = expected == digest
    return out


def _child(conn: Any, payload: bytes) -> None:
    """Child process: rebuild the part shapes from their STEP bytes, generate, send the package back."""
    step_context: dict[str, Any] | None = None
    try:
        from ..cad.assembly_step import read_shape

        geom, steps, bom, cfg, pol, runtime, budget, upstream, reuse = pickle.loads(payload)
        parts = {}
        for key, part in geom.parts.items():
            data = steps[key]
            step_context = _safe_step_read_context({
                "stage": "step_read", "definition_key": key, "model_revision_id": key,
                "step_sha256": hashlib.sha256(data).hexdigest(), "byte_count": len(data),
                "source_step_sha256": (part.meta or {}).get("step_sha"),
            })
            shape = read_shape(data)
            step_context = None  # later generation failures must not inherit the last successfully read part
            parts[key] = dataclasses.replace(part, shape=shape)
        geom = dataclasses.replace(geom, parts=parts)
        with checks_ledger.scope() as ledger:
            pkg = ad.make_assembly_drawing_package(geom, bom, cfg, pol, dwg_runtime=runtime, page_budget_s=budget, upstream=upstream,
                                                   reuse=reuse)
        msg: tuple[Any, ...] = ("ok", pkg, list(ledger.invoked), list(ledger.skipped))
    except ad.AssemblyDrawingError as exc:
        msg = ("error", exc.code, exc.message, canonical_json(exc.details))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, never swallowed
        msg = ("crash", type(exc).__name__, str(exc)[:500])
        if step_context is not None:
            msg += (step_context,)
    try:
        conn.send_bytes(pickle.dumps(msg))
    finally:
        conn.close()


def _isolated(db: Session, geom: Any, bom: dict[str, Any], cfg: ad.AssemblyDrawingConfig, pol: str, runtime: Any,
              limit: float, upstream: dict[str, Any] | None = None, reuse: ad.AssemblyDrawingPackage | None = None) -> ad.AssemblyDrawingPackage:
    steps: dict[str, bytes] = {}
    for key, part in geom.parts.items():
        sha = (part.meta or {}).get("step_sha")
        if not sha:
            raise AppError(422, "definition_without_step", f"{part.name} has no STEP", {"def_key": key})
        try:
            steps[key] = read_blob(db, sha)[0]
        except FileNotFoundError:
            raise AppError(409, "source_missing", f"the STEP of {part.name} is missing from storage", {"def_key": key})
    payload = pickle.dumps((geom, steps, bom, cfg, pol, runtime, ad.PAGE_BUDGET_S, upstream, reuse))
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(send, payload), daemon=True, name="partroute-assembly-drawing")
    proc.start()
    send.close()
    try:
        if not recv.poll(limit):
            raise AppError(422, "drawing_budget_exceeded", f"the drawing package did not finish within {limit:.0f} s; nothing was saved",
                           {"budget_s": round(limit, 1), "page_budget_s": ad.PAGE_BUDGET_S})
        try:
            msg = pickle.loads(recv.recv_bytes())
        except EOFError:
            raise AppError(500, "drawing_generation_crashed", "the drawing process ended without a result",
                           {"exitcode": proc.exitcode})
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        recv.close()
    if msg[0] == "ok":
        for name in msg[2]:
            checks_ledger.record(name)
        for name in msg[3] if len(msg) > 3 else []:  # generation reliability N: the closed checks it skipped
            checks_ledger.record_skipped(name)
        return msg[1]
    if msg[0] == "error":
        raise ad.AssemblyDrawingError(msg[1], msg[2], json.loads(msg[3]))
    details = _safe_step_read_context(msg[3]) if len(msg) > 3 else {}
    raise AppError(500, "drawing_generation_failed", f"{msg[1]}: {msg[2]}", details)


def _in_process(geom: Any, bom: dict[str, Any], cfg: ad.AssemblyDrawingConfig, pol: str, runtime: Any,
                upstream: dict[str, Any] | None = None, reuse: ad.AssemblyDrawingPackage | None = None) -> ad.AssemblyDrawingPackage:
    from .revisions import KERNEL_LOCK

    with KERNEL_LOCK:
        return ad.make_assembly_drawing_package(geom, bom, cfg, pol, dwg_runtime=runtime, page_budget_s=ad.PAGE_BUDGET_S, upstream=upstream,
                                                reuse=reuse)


def _runtime() -> Any:
    from ..cad.dwg import dwg_runtime

    return dwg_runtime(get_settings().libredwg_bin_dir)


def _runtime_identity(runtime: Any) -> dict[str, Any]:
    return {"available": runtime.available, "reason": runtime.reason, "libredwg_version": runtime.libredwg_version,
            "fingerprint": runtime.fingerprint, "independent_reader": runtime.independent_reader}


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def latest_ready_revision(db: Session, assembly_id: uuid.UUID) -> AssemblyRevision | None:
    return db.scalar(select(AssemblyRevision).where(AssemblyRevision.assembly_id == assembly_id, AssemblyRevision.status == "ready")
                     .order_by(AssemblyRevision.rev_no.desc()).limit(1))


def _upstream(db: Session, rev: AssemblyRevision) -> dict[str, Any]:
    """The verification state of the exact assembly revision a package is drawn from (the CAD layer folds it into
    the manifest's ``verification`` before hashing, so ``package.zip`` and ``export_package`` carry it)."""
    from . import verification

    v = rev.verification or {}

    def key(owner: Any, aid: Any) -> tuple[str, str, str]:
        owner = owner if isinstance(owner, dict) else {}
        return str(owner.get("type")), str(owner.get("id")), str(aid)

    # S5: a value a person confirmed on the revision (or on its spec version) is no longer listed as unconfirmed; a
    # decision counts for the value of the version that made it (ids repeat across the revision and its spec).
    decided = {key(c.get("owner"), c.get("id")) for c in v.get("confirmed_assumptions") or [] if isinstance(c, dict)}
    refs = [{"id": a.get("id"), "subject": a.get("subject"), "field": a.get("field"), "adopted": a.get("adopted"),
             "author": a.get("author") or "ai_turbo", "owner": owner,
             **({"confirmation": {"author": "person"}} if key(owner, a.get("id")) in decided else {})}
            for a, owner in verification.assembly_assumptions_with_owner(db, rev)]
    pending = sorted(name for name, slot in (rev.checks or {}).items()
                     if ((slot or {}).get("latest") or {}).get("status") in ("skipped_turbo", "pending", "failed", "unknown"))
    return {"assembly_revision_id": str(rev.id), "rev_no": rev.rev_no, "execution_policy": rev.execution_policy,
            "check_status": rev.check_status, "unverified": bool(v.get("unverified", rev.check_status != "passed")),
            "pending_checks": pending, "assumptions": refs}


def _partial(db: Session, rev: AssemblyRevision, bom: dict[str, list[Any]], upstream: dict[str, Any]) -> dict[str, Any] | None:
    """A partial assembly revision (generation supplement B-03): its coverage, the spec's full target BOM (so a missing
    item keeps its number) and how many AI decisions the drawn content rests on (the revision's and its parts'). None
    for every other revision (its drawings stay exactly as before)."""
    from ..models import ModelRevision
    from .verification import assumption_refs

    ref = rev.spec_ref if isinstance(rev.spec_ref, dict) else {}
    cov = ref.get("coverage")
    if not isinstance(cov, dict) or cov.get("status") != "partial":
        return None
    n = sum(1 for a in upstream.get("assumptions") or [] if (a.get("author") or "ai_turbo") != "person")
    seen: set[str] = set()
    for ln in bom.get("flattened") or []:
        if ln.def_key in seen:
            continue
        seen.add(ln.def_key)
        try:
            prev = db.get(ModelRevision, uuid.UUID(str(ln.def_key)))
        except ValueError:
            prev = None
        if prev is not None:
            n += sum(1 for a in assumption_refs(prev) if (a.get("author") or "ai_turbo") != "person")
    return {"coverage": cov, "target_bom": list(ref.get("target_bom") or []), "ai_decisions": n}


def _store_files(db: Session, pkg: ad.AssemblyDrawingPackage) -> None:
    for p in pkg.pages:
        for f in p.files:
            if save_blob(db, f.data, f.content_type) != f.sha256:
                raise AppError(500, "package_file_mismatch", f"{f.path} does not hash to its manifest entry")
    listed = {r["path"]: r["sha256"] for r in pkg.manifest.get("reports") or []}
    for path, data in pkg.reports.items():
        if save_blob(db, data, REPORT_TYPE) != listed.get(path):
            raise AppError(500, "package_file_mismatch", f"{path} does not hash to its manifest entry")


def _next_number(db: Session, assembly_id: uuid.UUID) -> int:
    db.scalar(select(Assembly.id).where(Assembly.id == assembly_id).with_for_update())  # serialises numbering
    return int(db.scalar(select(func.max(AssemblyDrawing.number)).where(AssemblyDrawing.assembly_id == assembly_id)) or 0) + 1


def _persist(db: Session, pkg: ad.AssemblyDrawingPackage, *, asm: Assembly, rev: AssemblyRevision, user: User, config: dict[str, Any],
             parent: AssemblyDrawing | None, node_run_id: uuid.UUID | None, generation: dict[str, Any],
             resumed_from: AssemblyDrawing | None = None) -> AssemblyDrawing:
    with db.begin_nested():
        _store_files(db, pkg)
        row = AssemblyDrawing(
            project_id=asm.project_id,
            assembly_id=asm.id,
            assembly_revision_id=rev.id,
            number=parent.number if parent is not None else _next_number(db, asm.id),
            version=pkg.version,
            parent_id=parent.id if parent is not None else None,
            execution_policy=pkg.execution_policy,
            completeness=pkg.completeness,
            check_status=pkg.verification.get("check_status", "unknown"),
            config=config,
            overlay=pkg.manifest.get("overlay"),
            pages=[p.model_dump(mode="json") for p in pkg.pages],
            documents=[dict(d) for d in pkg.documents],
            manifest=pkg.manifest,
            manifest_sha256=pkg.manifest_sha256,
            bom_sha256=(pkg.manifest.get("source") or {}).get("bom_sha256"),
            checks=pkg.checks,
            verification=dict(pkg.verification),  # the manifest's: drawing checks + the revision it was drawn from
            assumptions=pkg.assumptions,
            errors=pkg.errors,
            warnings=[w.model_dump(mode="json") for w in pkg.warnings],
            generation=generation,
            node_run_id=node_run_id,
            resumed_from_id=resumed_from.id if resumed_from is not None else None,
            created_by=user.id,
        )
        db.add(row)
        db.flush()
        assets.register_assembly_drawing(db, row)
        audit(db, action="assembly_drawing.overlay" if parent is not None else "assembly_drawing.generate", subject_type="assembly_drawing",
              subject_id=row.id, user_id=user.id, project_id=asm.project_id,
              data={"assembly_revision_id": str(rev.id), "number": row.number, "version": row.version, "pages": len(pkg.pages),
                    "completeness": row.completeness, "execution_policy": row.execution_policy, "manifest_sha256": row.manifest_sha256,
                    **({"resumed_from": str(resumed_from.id)} if resumed_from is not None else {})})
    return row


# --------------------------------------------------------------------------- #
# generate / overlay
# --------------------------------------------------------------------------- #


def _load_revision(db: Session, assembly_revision_id: uuid.UUID | str) -> tuple[AssemblyRevision, Assembly]:
    try:
        rid = uuid.UUID(str(assembly_revision_id))
    except ValueError:
        raise not_found("assembly revision")
    rev = db.get(AssemblyRevision, rid)
    if rev is None:
        raise not_found("assembly revision")
    asm = db.get(Assembly, rev.assembly_id)
    assert asm is not None
    return rev, asm


def generate(
    db: Session,
    *,
    assembly_revision_id: uuid.UUID | str,
    user: User,
    config: dict[str, Any] | None = None,
    policy: str | None = None,
    node_run_id: uuid.UUID | None = None,
    isolated: bool | None = None,
    resume_from: uuid.UUID | str | None = None,
    reuse_complete: bool = False,
) -> AssemblyDrawing:
    """Generate and store the drawing package of one exact assembly revision (W0 §2: ``policy`` is explicit,
    None = standard). Caller owns the transaction (this only flushes, inside one savepoint). Raises 422 when the
    revision cannot be drawn (``revision_not_ready``, ``invalid_drawing_config``, the CAD layer's codes such as
    ``bom_invalid``, or ``drawing_budget_exceeded`` when the hard limit ran out).

    S3: ``resume_from`` continues a partial package of this revision (:func:`continue_package`); without it, a retry
    of the same node run continues the partial package its earlier attempt stored (same configuration and policy).
    Only the documents / sheets that failed are produced again; ``generation.progress`` records each one.

    ``reuse_complete`` (generation supplement E, layered drawing step): a retry of the same node run returns the
    complete package an earlier attempt stored for this revision with the same configuration, policy and generator
    as it is (a retry after one sub-assembly package failed does not draw the others again; :func:`complete_package`
    is the same lookup without drawing)."""
    try:
        pol = exec_policy.parse_policy(policy)
    except ValueError as exc:
        raise bad_request("invalid_execution_policy", str(exc))
    rev, asm = _load_revision(db, assembly_revision_id)
    if rev.status != "ready":
        raise AppError(422, "revision_not_ready", f"assembly revision A{rev.rev_no} failed to build and cannot be drawn")
    cfg = _config(db, asm, rev, user, config)
    prev: AssemblyDrawing | None = None
    if resume_from is not None:
        prev = get_package(db, resume_from, for_update=True)
        reason = _not_continuable(db, prev, rev, cfg, pol)
        if reason is not None:
            raise conflict("package_not_continuable", reason[1], reason_code=reason[0], package_id=str(prev.id))
    elif node_run_id is not None:
        prev = _partial_of_attempt(db, rev, node_run_id, cfg, pol)
        if prev is None and reuse_complete:
            done = _complete_of_attempt(db, rev, node_run_id, cfg, pol)
            if done is not None:
                return done
    iso = ISOLATE if isolated is None else isolated
    geom = assemblies.resolve_geometry(db, rev.id, with_shapes=not iso)
    bom = assemblies.bom_lines(db, rev.id)
    planned = _planned_sheets(bom, cfg)
    limit = hard_limit_s(planned, cfg)
    runtime = _runtime()
    started = time.monotonic()
    upstream = _upstream(db, rev)
    partial = _partial(db, rev, bom, upstream)
    if partial is not None:  # drawn and marked as a partial assembly (title block, notes, BOM with target quantities)
        upstream = {**upstream, "partial": partial}
    reuse = restore(db, prev) if prev is not None else None
    try:
        pkg = (_isolated(db, geom, bom, cfg, pol, runtime, limit, upstream, reuse) if iso
               else _in_process(geom, bom, cfg, pol, runtime, upstream, reuse))
    except ad.AssemblyDrawingError as exc:
        raise AppError(422, exc.code, exc.message, exc.details)
    generation = {"seconds": round(time.monotonic() - started, 2), "isolated": iso, "planned_sheets": planned,
                  "page_budget_s": ad.PAGE_BUDGET_S, "hard_limit_s": round(limit, 1) if iso else None, "dwg_runtime": _runtime_identity(runtime),
                  "progress": {**(pkg.progress or {}), "resumed_from": str(prev.id) if prev is not None else None}}
    return _persist(db, pkg, asm=asm, rev=rev, user=user, config=cfg.model_dump(mode="json"), parent=None, node_run_id=node_run_id,
                    generation=generation, resumed_from=prev)


def _not_continuable(db: Session, prev: AssemblyDrawing, rev: AssemblyRevision, cfg: ad.AssemblyDrawingConfig,
                     pol: str) -> tuple[str, str] | None:
    """Why ``prev`` cannot be continued into a package of ``rev`` with ``cfg`` / ``pol`` (None: it can)."""
    if prev.assembly_revision_id != rev.id:
        return "other_revision", "the package was drawn from another assembly revision"
    if prev.completeness != "partial":
        return "complete", "the package is complete; nothing failed"
    if prev.version != 1 or prev.overlay:
        return "overlay", "a layout version cannot be continued; continue the package it was made from"
    if db.scalar(select(AssemblyDrawing.id).where(AssemblyDrawing.resumed_from_id == prev.id)) is not None:
        return "already_continued", "the package was already continued"
    if prev.execution_policy != pol:
        return "other_policy", "the package was made under another execution policy"
    if prev.config != cfg.model_dump(mode="json"):
        return "other_config", "the drawing configuration changed; generate a new package"
    if (prev.manifest or {}).get("generator") != ad.generator_identity():
        return "other_generator", "the drawing generator changed since the package was made; generate a new package"
    return None


def _partial_of_attempt(db: Session, rev: AssemblyRevision, node_run_id: uuid.UUID, cfg: ad.AssemblyDrawingConfig,
                        pol: str) -> AssemblyDrawing | None:
    """The partial package an earlier attempt of this node run stored, when it can be continued (a node retry)."""
    prev = db.scalar(select(AssemblyDrawing).where(AssemblyDrawing.node_run_id == node_run_id, AssemblyDrawing.assembly_revision_id == rev.id,
                                                   AssemblyDrawing.completeness == "partial", AssemblyDrawing.parent_id.is_(None))
                     .order_by(AssemblyDrawing.created_at.desc()).limit(1))
    if prev is None or _not_continuable(db, prev, rev, cfg, pol) is not None:
        return None
    return prev


def _complete_of_attempt(db: Session, rev: AssemblyRevision, node_run_id: uuid.UUID, cfg: ad.AssemblyDrawingConfig,
                         pol: str) -> AssemblyDrawing | None:
    """The complete package an earlier attempt of this node run stored for ``rev`` with this configuration, policy and
    generator (``generate(reuse_complete=True)``)."""
    rows = db.scalars(select(AssemblyDrawing).where(AssemblyDrawing.node_run_id == node_run_id, AssemblyDrawing.assembly_revision_id == rev.id,
                                                    AssemblyDrawing.completeness == "complete", AssemblyDrawing.parent_id.is_(None),
                                                    AssemblyDrawing.execution_policy == pol)
                      .order_by(AssemblyDrawing.created_at.desc())).all()
    want = cfg.model_dump(mode="json")
    return next((r for r in rows if r.config == want and (r.manifest or {}).get("generator") == ad.generator_identity()), None)


def complete_package(db: Session, *, assembly_revision_id: uuid.UUID | str, user: User, config: dict[str, Any] | None = None,
                     policy: str | None = None, node_run_id: uuid.UUID | None = None) -> AssemblyDrawing | None:
    """Generation reliability R (design §5.3): the complete package an earlier attempt of this node run already stored for
    this exact revision with the same frozen configuration, policy and generator — what ``generate(reuse_complete=True)``
    would hand back as it is — else None (it has to be drawn). Packages are keyed by (node run, assembly revision,
    configuration, policy, generator); a node retry (a workspace "continue" too) keeps its node run. Never draws; a
    configuration or revision ``generate`` would refuse gives None (``generate`` then reports it)."""
    if node_run_id is None:
        return None
    try:
        pol = exec_policy.parse_policy(policy)
        rev, asm = _load_revision(db, assembly_revision_id)
        cfg = _config(db, asm, rev, user, config)
    except (ValueError, AppError):
        return None
    return _complete_of_attempt(db, rev, node_run_id, cfg, pol)


def continue_package(db: Session, package_id: uuid.UUID | str, user: User, *, node_run_id: uuid.UUID | None = None,
                     isolated: bool | None = None) -> AssemblyDrawing:
    """S3 (recovery ``retry_sheet``): a new package of the same revision with the same frozen configuration and policy
    that keeps the documents the partial package produced whole and generates only the failed ones (and converts the
    DWG of sheets that only lacked it). 409 ``package_not_continuable`` (complete, layout version, already continued,
    other generator)."""
    row = get_package(db, package_id, for_update=True)
    return generate(db, assembly_revision_id=row.assembly_revision_id, user=user, config=dict(row.config or {}),
                    policy=row.execution_policy, node_run_id=node_run_id, isolated=isolated, resume_from=row.id)


def _read(db: Session):
    def read(sha: str) -> bytes:
        try:
            return read_blob(db, sha)[0]
        except FileNotFoundError:
            raise AppError(409, "package_file_missing", "a package file is missing from storage", {"sha256": sha})

    return read


def restore(db: Session, row: AssemblyDrawing) -> ad.AssemblyDrawingPackage:
    """The stored package with its file bytes (hash-verified against the manifest)."""
    try:
        return ad.restore_package(manifest=row.manifest, manifest_sha256=row.manifest_sha256, pages=row.pages, documents=row.documents,
                                  warnings=row.warnings, read=_read(db))
    except ad.AssemblyDrawingError as exc:
        raise AppError(409, exc.code, exc.message, exc.details)


def child_of(db: Session, row: AssemblyDrawing) -> AssemblyDrawing | None:
    return db.scalar(select(AssemblyDrawing).where(AssemblyDrawing.parent_id == row.id))


def apply_overlay(db: Session, package_id: uuid.UUID | str, overlay: dict[str, Any], user: User, *,
                  node_run_id: uuid.UUID | None = None) -> AssemblyDrawing:
    """A new version of the package with layout / annotation edits (move a dimension text or a balloon, add a
    note). Only the latest version of a package can be edited (409 ``package_superseded``). Engineering edits
    raise 422 ``engineering_change_required`` with a hint to change the model instead; nothing is saved."""
    row = get_package(db, package_id, for_update=True)
    nxt = child_of(db, row)
    if nxt is not None:
        raise conflict("package_superseded", "this package version already has a newer layout version; edit the latest one",
                       latest_id=str(latest_in_lineage(db, row).id))
    rev, asm = _load_revision(db, row.assembly_revision_id)
    base = restore(db, row)
    runtime = _runtime()
    started = time.monotonic()
    try:
        pkg = ad.apply_layout_overlay(base, overlay, dwg_runtime=runtime, dwg_timeout_s=get_settings().dwg_timeout_s,
                                      upstream=_upstream(db, rev))
    except ad.EngineeringChangeRequired as exc:
        raise AppError(422, exc.code, exc.message, {**exc.details, "hint": ENGINEERING_HINT, "assembly_id": str(asm.id),
                                                    "assembly_revision_id": str(rev.id)})
    except ad.AssemblyDrawingError as exc:
        raise AppError(422, exc.code, exc.message, exc.details)
    generation = {"seconds": round(time.monotonic() - started, 2), "isolated": False, "overlay_of": str(row.id),
                  "dwg_runtime": _runtime_identity(runtime)}
    return _persist(db, pkg, asm=asm, rev=rev, user=user, config=row.config, parent=row, node_run_id=node_run_id, generation=generation)


# --------------------------------------------------------------------------- #
# reading, staleness
# --------------------------------------------------------------------------- #


def get_package(db: Session, package_id: uuid.UUID | str, *, for_update: bool = False) -> AssemblyDrawing:
    try:
        pid = uuid.UUID(str(package_id))
    except ValueError:
        raise not_found("assembly drawing")
    stmt = select(AssemblyDrawing).where(AssemblyDrawing.id == pid)
    row = db.scalar(stmt.with_for_update() if for_update else stmt)
    if row is None:
        raise not_found("assembly drawing")
    return row


def latest_in_lineage(db: Session, row: AssemblyDrawing) -> AssemblyDrawing:
    return db.scalar(select(AssemblyDrawing).where(AssemblyDrawing.assembly_id == row.assembly_id, AssemblyDrawing.number == row.number)
                     .order_by(AssemblyDrawing.version.desc()).limit(1)) or row


def list_packages(db: Session, *, assembly_id: uuid.UUID, revision_id: uuid.UUID | None = None) -> list[AssemblyDrawing]:
    stmt = select(AssemblyDrawing).where(AssemblyDrawing.assembly_id == assembly_id)
    if revision_id is not None:
        stmt = stmt.where(AssemblyDrawing.assembly_revision_id == revision_id)
    return list(db.scalars(stmt.order_by(AssemblyDrawing.number.desc(), AssemblyDrawing.version.desc())).all())


def staleness(db: Session, rev: AssemblyRevision, *, latest: AssemblyRevision | None = None) -> dict[str, Any]:
    """Why a package bound to ``rev`` is out of date: a newer ready revision of the assembly, or components whose
    definitions have newer revisions (W4's "update available"). Nothing is rewritten."""
    latest = latest if latest is not None else latest_ready_revision(db, rev.assembly_id)
    reasons: list[dict[str, Any]] = []
    if latest is not None and latest.id != rev.id:
        reasons.append({"code": "newer_assembly_revision", "latest_revision_id": str(latest.id), "latest_rev_no": latest.rev_no})
    comps, _ = assemblies.revision_state(db, rev)
    updates = assemblies._update_available(db, comps)  # W4: direct components with a newer definition revision
    if updates:
        reasons.append({"code": "component_updates", "count": len(updates), "instances": sorted(updates)})
    return {"stale": bool(reasons), "reasons": reasons, "latest_revision_id": str(latest.id) if latest else None}


def _page_out(p: dict[str, Any], *, full: bool) -> dict[str, Any]:
    files = [{k: f.get(k) for k in ("format", "path", "sha256", "bytes", "content_type", "version")} for f in p.get("files") or []]
    out: dict[str, Any] = {k: p.get(k) for k in ("index", "key", "document", "role", "title", "sheet", "sheets", "def_key", "item", "scale",
                                                   "status")}
    dwg = p.get("dwg_report") or None
    out.update(files=files, errors=p.get("errors") or [], hatch_count=p.get("hatch_count") or len(p.get("hatches") or []),
               dwg={k: dwg.get(k) for k in ("status", "code", "verified", "version", "bytes")} if dwg else None)
    if full:
        out["views"] = p.get("views") or []
        out["dimensions"] = [{k: d.get(k) for k in ("id", "view", "kind", "axis", "value", "text", "measured", "handle", "text_at")}
                             for d in p.get("dimensions") or []]
        out["balloons"] = [{k: b.get(k) for k in ("id", "item", "bom", "level", "def_key", "def_kind", "quantity", "view", "occurrence",
                                                   "anchor", "center", "radius", "handle")} for b in p.get("balloons") or []]
        out["bom_rows"] = [{k: r.get(k) for k in ("item", "level", "def_key", "def_kind", "name", "number", "material", "procurement",
                                                   "representation", "quantity", "total_quantity", "shown_quantity")} for r in p.get("bom_rows") or []]
        out["notes"] = p.get("notes") or []
        out["callouts"] = p.get("callouts") or []
        out["section"] = p.get("section")
        out["overlay"] = p.get("overlay") or []
        out["dwg_report"] = dwg
    return out


def continuation_of(db: Session, row: AssemblyDrawing) -> AssemblyDrawing | None:
    return db.scalar(select(AssemblyDrawing).where(AssemblyDrawing.resumed_from_id == row.id))


def failure_info(db: Session, row: AssemblyDrawing) -> dict[str, Any] | None:
    """A partial package's failure info (design §3.1): what failed per document / sheet and whether continuing
    (recovery ``retry_sheet``: only the failed parts are produced again) is possible. None for a complete package."""
    if row.completeness != "partial":
        return None
    nxt = continuation_of(db, row)
    reason = None
    if nxt is not None:
        reason = "already_continued"
    elif row.version != 1 or row.overlay:
        reason = "overlay"
    elif (row.manifest or {}).get("generator") != ad.generator_identity():
        reason = "other_generator"
    failed = [{"document": e.get("document"), "page": e.get("page"), "format": e.get("format"), "code": e.get("code"),
               "message": e.get("message")} for e in row.errors or [] if isinstance(e, dict)]
    return {"stage": "assembly_drawing", "code": "drawing_package_partial",
            "message": f"{len(failed)} document / sheet failure(s); the other sheets are complete", "retryable": reason is None,
            "recovery": "retry_sheet", "not_continuable": reason, "continued_by": str(nxt.id) if nxt is not None else None,
            "failed": failed}


def package_out(db: Session, row: AssemblyDrawing, *, stale: dict[str, Any] | None = None, full: bool = True,
                rev: AssemblyRevision | None = None, child: AssemblyDrawing | None | bool = False) -> dict[str, Any]:
    rev = rev if rev is not None else db.get(AssemblyRevision, row.assembly_revision_id)
    assert rev is not None
    st = stale if stale is not None else staleness(db, rev)
    nxt = child_of(db, row) if child is False else child
    av = assets.version_for(db, "assembly_drawing", row.id)
    out: dict[str, Any] = {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "assembly_id": str(row.assembly_id),
        "assembly_revision_id": str(row.assembly_revision_id),
        "rev_no": rev.rev_no,
        "rev_label": f"A{rev.rev_no}",
        "number": row.number,
        "version": row.version,
        "label": f"AD{row.number}v{row.version}",
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "superseded_by": str(nxt.id) if isinstance(nxt, AssemblyDrawing) else None,
        "execution_policy": row.execution_policy,
        "completeness": row.completeness,
        "check_status": row.check_status,
        "verification": row.verification,
        "checks": row.checks,
        "stale": st["stale"],
        "stale_reasons": st["reasons"],
        "page_count": len(row.pages or []),
        "pages": [_page_out(p, full=full) for p in row.pages or []],
        "documents": row.documents,
        "errors": row.errors,
        "warnings": row.warnings,
        "manifest_sha256": row.manifest_sha256,
        "bom_sha256": row.bom_sha256,
        "overlay": row.overlay,
        "asset_version_id": str(av.id) if av else None,
        "node_run_id": str(row.node_run_id) if row.node_run_id else None,
        "resumed_from_id": str(row.resumed_from_id) if row.resumed_from_id else None,
        "progress": (row.generation or {}).get("progress"),
        "failure": failure_info(db, row),
        # generation supplement B-03: drawn from a partial assembly revision (its coverage, in the manifest too)
        "coverage": (row.manifest or {}).get("coverage") if isinstance(row.manifest, dict) else None,
        "created_by": str(row.created_by),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if full:
        out.update(config=row.config, assumptions=row.assumptions, manifest=row.manifest, generation=row.generation)
    return out


def packages_out(db: Session, rows: list[AssemblyDrawing]) -> list[dict[str, Any]]:
    """Brief list rows with staleness computed once per revision."""
    revs: dict[uuid.UUID, AssemblyRevision] = {}
    stale: dict[uuid.UUID, dict[str, Any]] = {}
    latest: dict[uuid.UUID, AssemblyRevision | None] = {}
    children = {r.parent_id: r for r in rows if r.parent_id}
    out = []
    for r in rows:
        if r.assembly_revision_id not in revs:
            rev = db.get(AssemblyRevision, r.assembly_revision_id)
            assert rev is not None
            if rev.assembly_id not in latest:
                latest[rev.assembly_id] = latest_ready_revision(db, rev.assembly_id)
            revs[r.assembly_revision_id] = rev
            stale[r.assembly_revision_id] = staleness(db, rev, latest=latest[rev.assembly_id])
        out.append(package_out(db, r, stale=stale[r.assembly_revision_id], full=False, rev=revs[r.assembly_revision_id],
                               child=children.get(r.id)))
    return out


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def _verified(db: Session, sha: str, what: str) -> bytes:
    try:
        data = read_blob(db, sha)[0]
    except FileNotFoundError:
        raise AppError(409, "package_file_missing", f"{what} is missing from storage", {"sha256": sha})
    if ad._sha(data) != sha:
        raise AppError(409, "package_file_mismatch", f"{what} does not match its manifest hash", {"sha256": sha})
    return data


def _manifest_bytes(row: AssemblyDrawing) -> bytes:
    body = canonical_json(row.manifest).encode("utf-8")
    if ad._sha(body) != row.manifest_sha256:
        raise AppError(409, "package_manifest_mismatch", "the stored manifest does not match its hash",
                       {"manifest_sha256": row.manifest_sha256, "computed": ad._sha(body)})
    return body


def package_entries(db: Session, row: AssemblyDrawing) -> list[PackageEntry]:
    """Every page file and check report listed in the manifest plus ``manifest.json`` (canonical JSON whose
    sha256 is ``manifest_sha256``), each verified against its hash."""
    body = _manifest_bytes(row)
    out: list[PackageEntry] = []
    for page in row.manifest.get("pages") or []:
        for f in page.get("files") or []:
            out.append(PackageEntry(f["path"], f["sha256"], _verified(db, f["sha256"], f"{page['key']} {f['format']}"), "page", page["index"],
                                    f["format"]))
    for r in row.manifest.get("reports") or []:
        out.append(PackageEntry(r["path"], r["sha256"], _verified(db, r["sha256"], r["path"]), "report"))
    out.append(PackageEntry(MANIFEST_PATH, row.manifest_sha256, body, "manifest"))
    return out


def package_folder(db: Session, row: AssemblyDrawing) -> str:
    """Folder / file stem of a package, e.g. ``GEN-01_A2_AD3v2``."""
    asm = db.get(Assembly, row.assembly_id)
    rev = db.get(AssemblyRevision, row.assembly_revision_id)
    stem = "_".join(x for x in (asm.number if asm else "assembly", f"A{rev.rev_no}" if rev else "", f"AD{row.number}v{row.version}") if x)
    return _SAFE.sub("-", stem).strip("-.") or "assembly-drawing"


def package_zip(db: Session, row: AssemblyDrawing) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        write_package(z, package_entries(db, row))
    return buf.getvalue()


def export_package(db: Session, z: zipfile.ZipFile, row: AssemblyDrawing) -> list[dict[str, Any]]:
    """The whole package into an open export zip under its folder (W8 delivery / agent export)."""
    lines = write_package(z, package_entries(db, row), package_folder(db, row))
    for line in lines:
        line.update(assembly_drawing_id=str(row.id), manifest_sha256=row.manifest_sha256)
    return lines


def page_file(db: Session, row: AssemblyDrawing, page_key: str, fmt: str) -> tuple[bytes, str, str, str]:
    """(bytes, file name, content type, sha256) of one page file; 404 when the page has no such format (a failed
    DWG is never replaced by another format)."""
    _manifest_bytes(row)
    page = next((p for p in row.manifest.get("pages") or [] if p.get("key") == page_key or str(p.get("index")) == page_key), None)
    if page is None:
        raise not_found("drawing page")
    f = next((x for x in page.get("files") or [] if x.get("format") == fmt), None)
    if f is None:
        raise AppError(404, "page_format_missing", f"page {page['key']} has no {fmt.upper()} file",
                       {"page": page["key"], "format": fmt, "missing": page.get("missing") or [], "errors": page.get("errors") or []})
    data = _verified(db, f["sha256"], f"{page['key']} {fmt}")
    name = f"{package_folder(db, row)}_{f['path'].rsplit('/', 1)[-1]}"
    return data, name, f.get("content_type") or "application/octet-stream", f["sha256"]
