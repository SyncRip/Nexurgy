"""Generation supplement I (A-04 / IN-08): a chain whose spec step could not read a source continues in the SAME run with
replacement or additional files (docs/agent/generation-workflow-supplement.zh-CN.md §3 A-04, §8.2 IN-08).

The run's frozen graph never changes (it stays the original input snapshot). Continuing with files records an *input
revision* on the run — ``run.inputs["input_revisions"]``, append-only: which node, the effective file set, what was
added and what was dropped (the sources the step could not read), the spec version the step left, who and when — and
the node's effective input ``run.inputs["node_inputs"][node_id]`` that the spec step reads when it runs again
(:func:`node_override`). The step then makes a new version of the spec the failed attempt left (its sources minus the
unread files plus the new ones, ``engineering_spec.revise_version``; reason ``replace_source``), or a new spec when the
attempt made none (a file it could not open at all) — unless the step revises a referenced spec version (``revise``):
then that version stays the base and the new file set and the revision note make its next version (review fix) — and
the steps after it run again (``engine.retry_node``). Products
made before stay as they are; the old spec version keeps its record of what was not read.

The same body twice is no new input: :func:`replace_sources` refuses a file set equal to the one in force
(409 ``input_unchanged``), and the assistant's continue returns the operation as it is when that set is already
running (``services/assistant.continue_operation``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..errors import AppError, conflict, not_found
from ..models import File, FileVersion, NodeRun, WorkflowRun

#: run.inputs keys: the effective input per node, and every input revision (append-only).
NODE_INPUTS = "node_inputs"
INPUT_REVISIONS = "input_revisions"
#: Spec step failures a replacement / additional file can fix (design §2.2: source_unread → replace_source; spec_no_parts →
#: fix_input, e.g. the file with the parts list was not referenced).
REPLACEABLE = ("replace_source", "fix_input")
MAX_FILES = 20


def node_override(run: WorkflowRun, node_id: str) -> dict[str, Any] | None:
    """The effective input of a node set by the latest input revision (None: the frozen graph's input)."""
    got = ((run.inputs or {}).get(NODE_INPUTS) or {}).get(node_id)
    return got if isinstance(got, dict) else None


def revisions(run: WorkflowRun) -> list[dict[str, Any]]:
    return [r for r in (run.inputs or {}).get(INPUT_REVISIONS) or [] if isinstance(r, dict)]


def _graph_node(run: WorkflowRun, node_id: str) -> dict[str, Any]:
    return next((n for n in (run.graph_snapshot or {}).get("nodes") or [] if n.get("id") == node_id), {})


def current_files(run: WorkflowRun, nr: NodeRun) -> list[str]:
    """The file versions the node read last: the latest input revision, else its frozen params plus the files its last
    attempt received from connected file inputs."""
    over = node_override(run, nr.node_id)
    if over is not None:
        return [str(f) for f in over.get("file_version_ids") or []]
    params = _graph_node(run, nr.node_id).get("params") or {}
    files = [str(f.get("file_version_id")) for f in (nr.inputs or {}).get("files") or [] if isinstance(f, dict) and f.get("file_version_id")]
    files += [str(f) for f in params.get("file_version_ids") or []]
    return list(dict.fromkeys(files))


def unread_files(error: dict[str, Any] | None) -> list[str]:
    """File versions a spec step failure lists as not read (``details.sources``: failed / unread / not among the sources)."""
    out = []
    for s in ((error or {}).get("details") or {}).get("sources") or []:
        if isinstance(s, dict) and s.get("file_version_id") and (s.get("read_status") or "unread") in ("failed", "unread"):
            out.append(str(s["file_version_id"]))
    return list(dict.fromkeys(out))


def _readable_file(db: Session, project_id: uuid.UUID, fid: str) -> dict[str, Any]:
    try:
        fv = db.get(FileVersion, uuid.UUID(str(fid)))
    except ValueError:
        fv = None
    f = db.get(File, fv.file_id) if fv is not None else None
    if fv is None or f is None or f.project_id != project_id:
        raise not_found("file version")
    if f.archived_at is not None:
        raise AppError(422, "file_archived", f"{f.name} is archived; choose a readable file", {"file_version_id": str(fv.id)})
    return {"file_version_id": str(fv.id), "name": f.name, "version": fv.version}


def replace_sources(db: Session, run: WorkflowRun, nr: NodeRun, file_version_ids: list[Any], *, by: uuid.UUID,
                    origin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record an input revision of the failed spec step ``nr`` (the caller holds the run lock and retries the node).

    The new file set: the files the step read last, minus the ones its failure lists as unread, plus ``file_version_ids``
    (files of the run's project, not archived). 422 ``files_required`` without any file; 409 ``not_replaceable`` when the
    step did not fail with a recovery a file can fix; 409 ``input_unchanged`` when the set is the one in force."""
    if nr.node_type != "engineering_spec" or nr.status != "failed":
        raise conflict("not_replaceable", "only a failed spec step takes replacement files", node_id=nr.node_id, status=nr.status)
    err = dict(nr.error or {})
    recovery = err.get("recovery") or (err.get("details") or {}).get("recovery")
    if recovery not in REPLACEABLE:
        raise conflict("not_replaceable", "this failure is not fixed by another file: " + str(err.get("message") or err.get("code") or ""),
                       recovery=recovery, failure_code=err.get("code"))
    wanted = list(dict.fromkeys(str(f) for f in file_version_ids or []))
    if not wanted:
        raise AppError(422, "files_required", "choose the file(s) to continue with")
    if len(wanted) > MAX_FILES:
        raise AppError(422, "too_many_files", f"at most {MAX_FILES} files", {"max": MAX_FILES})
    described = {fid: _readable_file(db, run.project_id, fid) for fid in wanted}
    before = current_files(run, nr)
    dropped = [f for f in unread_files(err) if f not in wanted]
    files = [f for f in before if f not in dropped] + [f for f in wanted if f not in before]
    if sorted(files) == sorted(before) and not dropped:
        raise conflict("input_unchanged", "these files are the input already in force; choose a different file", file_version_ids=files)
    spec_value = (nr.outputs or {}).get("spec") if isinstance((nr.outputs or {}).get("spec"), dict) else {}
    base = spec_value.get("spec_version_id") or ((err.get("details") or {}).get("spec_version_id"))
    entry = {
        "n": len(revisions(run)) + 1, "node_id": nr.node_id, "at": datetime.now(UTC).isoformat(), "by": str(by),
        "file_version_ids": files, "requested": wanted, "added": [described[f] for f in wanted if f not in before],
        "dropped": dropped, "previous_file_version_ids": before, "base_spec_version_id": str(base) if base else None,
        "failure": {"code": err.get("code"), "recovery": recovery}, **({"origin": origin} if origin else {}),
    }
    inputs = dict(run.inputs or {})
    inputs[INPUT_REVISIONS] = [*revisions(run), entry]
    inputs[NODE_INPUTS] = {**(inputs.get(NODE_INPUTS) or {}), nr.node_id: {k: entry[k] for k in (
        "n", "file_version_ids", "added", "dropped", "base_spec_version_id")}}
    run.inputs = inputs
    db.flush()
    return entry
