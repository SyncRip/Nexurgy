"""Register pre-existing engineering rows in the asset catalogue (design §12).

`python -m app.backfill` — idempotent: rows that already have an asset version
are skipped, run outputs use their unique key. Rows are registered type by type
in creation order (documents → model revisions → intakes → drawings → plans →
quotes), so version numbers follow history and sources exist before the rows
derived from them. Every version starts as a draft; nothing is published,
approved or adopted as a task deliverable by the backfill.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Drawing, File, FileVersion, Intake, ModelRevision, NodeRun, ProcessPlan, Quote
from .services import assets


def _node_outputs(db: Session) -> int:
    """Historical runs: link produced rows to their node runs (RunOutput)."""
    n = 0
    node_runs: dict[uuid.UUID, NodeRun | None] = {}

    def node_run(nid: uuid.UUID) -> NodeRun | None:
        if nid not in node_runs:
            node_runs[nid] = db.get(NodeRun, nid)
        return node_runs[nid]

    for asset_type, model in (("part_model", ModelRevision), ("drawing", Drawing), ("intake", Intake), ("process_plan", ProcessPlan), ("quote", Quote)):
        for row in db.scalars(select(model).where(model.node_run_id.is_not(None))).all():
            nr = node_run(row.node_run_id)
            if nr is not None:
                assets.record_output(db, node_run=nr, output_key=assets.OUTPUT_KEY[asset_type], version=assets.version_for(db, asset_type, row.id))
                n += 1
    # Rows that reached a node without carrying node_run_id: manual intake entry, export bundles.
    for nr in db.scalars(select(NodeRun).where(NodeRun.node_type.in_(("drawing_intake", "export")), NodeRun.status == "succeeded")).all():
        out = nr.outputs or {}
        try:
            if nr.node_type == "drawing_intake" and isinstance(out.get("spec"), dict) and out["spec"].get("intake_id"):
                version = assets.ensure_version(db, "intake", out["spec"]["intake_id"])
                key = "spec"
            elif nr.node_type == "export" and isinstance(out.get("bundle"), dict) and out["bundle"].get("file_version_id"):
                version = assets.ensure_version(db, "document", out["bundle"]["file_version_id"])
                key = "bundle"
            else:
                continue
        except ValueError:  # malformed id in an old output
            continue
        if version is not None:
            assets.record_output(db, node_run=nr, output_key=key, version=version)
            n += 1
    return n


def backfill_assets(db: Session) -> dict[str, int]:
    """Register every unregistered specialised row; returns new-version counts per type.
    Safe to run repeatedly. Caller commits."""
    counts = {t: 0 for t in assets.KIND_FIELD}

    def reg(asset_type: str, rows) -> None:
        for row in rows:
            if assets.version_for(db, asset_type, row.id) is None:
                assets.register_row(db, asset_type, row)
                counts[asset_type] += 1

    reg("document", db.scalars(select(FileVersion).join(File, File.id == FileVersion.file_id).order_by(File.created_at, File.id, FileVersion.version)).all())
    reg("part_model", db.scalars(select(ModelRevision).order_by(ModelRevision.part_id, ModelRevision.rev_no)).all())
    for asset_type, model in (("intake", Intake), ("drawing", Drawing), ("process_plan", ProcessPlan), ("quote", Quote)):
        reg(asset_type, db.scalars(select(model).order_by(model.created_at, model.id)).all())
    counts["run_outputs_checked"] = _node_outputs(db)
    db.flush()
    return counts


def main() -> None:
    with session_scope() as db:
        counts = backfill_assets(db)
    print("asset backfill:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
