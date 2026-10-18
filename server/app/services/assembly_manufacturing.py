"""Manufacturing estimates and readable delivery reports for an exact workflow assembly.

The frozen BOM owns identity and multiplicity. Existing manufacturing and rollup
services own all time/cost arithmetic. These handlers never select latest plans,
quotes or revisions, and unknown purchase/labour costs never become zero.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from ..config import get_settings
from ..domain.costing import PlanMismatchError, QuoteParams as CostParams
from ..domain.planning import PlanParams
from ..errors import AppError
from ..models import (Assembly, AssemblyDrawing, AssemblyRevision, AssemblyRollup, AssetVersion, DeliveryManifest,
                      File, FileVersion, ModelRevision, NodeRun, Part, ProcessPlan, Project, ProjectMember, Quote, RunOutput, WorkflowRun)
from ..security import project_access
from ..workflow.engine import NodeContext, NodeFailure, NodeOutcome
from . import assemblies, assets, manufacturing, orchestration
from .features import summary_from_revision
from .lifecycle import require_active

ESTIMATE = {"estimated": True, "pricing_basis": "synthetic_demo_rates", "commercial_status": "draft"}
NOTICE = ("草稿估算 / Draft estimate — 使用合成示例费率 / synthetic example rates. "
          "外购件及装配人工未定价时保持未知；已知小计不代表总报价。 "
          "Purchase and assembly labour costs remain unknown until supplied; known subtotals are not total quotations.")
_UNSET = object()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _id(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise NodeFailure("manufacturing_source_mismatch", "invalid manufacturing source identity") from None


def _stop(ctx: NodeContext) -> None:
    if ctx.lease_lost:
        raise NodeFailure("lease_lost", "this attempt no longer owns its manufacturing node")
    reason = ctx.limits.stop_reason(calls=False) if ctx.limits is not None else None
    if reason is not None:
        raise NodeFailure(reason.code, reason.message, details={"stopped_by": reason.kind})


def _guard(ctx: NodeContext, action: str, node_type: str) -> None:
    _stop(ctx)
    run = ctx.db.get(WorkflowRun, ctx.run.id, populate_existing=True)
    node = ctx.db.get(NodeRun, ctx.node_run.id, populate_existing=True)
    if (run is None or node is None or node.run_id != run.id or run.project_id != ctx.project.id
            or run.created_by != ctx.user.id or node.node_type != node_type):
        raise NodeFailure("manufacturing_scope_mismatch", "manufacturing belongs to another workflow, project or owner")
    if run.cancel_requested or run.status == "cancelled" or run.paused:
        raise NodeFailure("cancelled", "the workflow was cancelled or paused")
    if node.status != "running" or node.attempt != ctx.attempt:
        raise NodeFailure("lease_lost", "this manufacturing attempt no longer owns its node")
    from ..workflow.nodes import NODE_DEFS

    definition = _definition(ctx, node)
    frozen_params = NODE_DEFS[node_type].params.model_validate(definition.get("params") or {}).model_dump(mode="json")
    if ctx.params.model_dump(mode="json") != frozen_params:
        raise NodeFailure("manufacturing_scope_mismatch", "parameters differ from the frozen workflow node")
    ctx.db.get(Project, run.project_id, populate_existing=True)
    ctx.db.get(ProjectMember, {"project_id": run.project_id, "user_id": ctx.user.id}, populate_existing=True)
    try:
        project_access(ctx.db, ctx.user, run.project_id, action)
        require_active(ctx.db, run.project_id, what="generating manufacturing estimates")
    except AppError as exc:
        raise NodeFailure(exc.code, exc.message, details=exc.details) from exc


def _lock(ctx: NodeContext) -> None:
    # Same-node attempts cannot race to make two plans/files; the engine still owns final lease/commit checks.
    ctx.db.execute(text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": f"manufacturing:{ctx.node_run.id}"})
    _stop(ctx)


def _definition(ctx: NodeContext, node: NodeRun) -> dict:
    found = [n for n in (ctx.run.graph_snapshot or {}).get("nodes", []) if n.get("id") == node.node_id]
    if len(found) != 1 or found[0].get("type") != node.node_type:
        raise NodeFailure("manufacturing_scope_mismatch", "node does not match its frozen workflow")
    return found[0]


def _source(ctx: NodeContext, node: NodeRun, port: str, node_type: str, supplied: Any = _UNSET) -> tuple[NodeRun, Any]:
    edges = [e for e in (ctx.run.graph_snapshot or {}).get("edges", []) if e.get("target") == node.node_id and e.get("target_port") == port]
    if len(edges) != 1:
        raise NodeFailure("manufacturing_source_mismatch", f"connect one exact {port} source")
    edge = edges[0]
    source = ctx.db.scalar(select(NodeRun).where(NodeRun.run_id == ctx.run.id, NodeRun.node_id == edge.get("source")))
    if source is None or source.node_type != node_type or source.status not in ("succeeded", "failed", "skipped", "cancelled"):
        raise NodeFailure("manufacturing_source_mismatch", f"{port} is not a terminal source of this workflow")
    _definition(ctx, source)
    value = (source.outputs or {}).get(edge.get("source_port"))
    if supplied is not _UNSET and supplied != value:
        raise NodeFailure("manufacturing_source_mismatch", f"{port} differs from the connected node's saved output")
    return source, value


def _assembly(ctx: NodeContext, producer: NodeRun, value: Any) -> AssemblyRevision:
    if not isinstance(value, dict) or value.get("type") != "assembly" or not value.get("assembly_revision_id"):
        raise NodeFailure("assembly_missing", "the workflow produced no assembly revision to manufacture")
    rev = ctx.db.get(AssemblyRevision, _id(value["assembly_revision_id"]))
    assembly = ctx.db.get(Assembly, rev.assembly_id) if rev else None
    if (rev is None or assembly is None or assembly.project_id != ctx.project.id or rev.node_run_id != producer.id
            or (value.get("assembly_id") and value["assembly_id"] != str(assembly.id))):
        raise NodeFailure("manufacturing_source_mismatch", "the assembly does not belong to the connected workflow source")
    lines = (rev.bom or {}).get("flattened")
    if (not isinstance(lines, list) or not lines or len(lines) > assemblies.MAX_DEFINITIONS
            or len({line.get("def_key") for line in lines}) != len(lines)):
        raise NodeFailure("invalid_frozen_bom", "the frozen BOM is empty, duplicated or too large")
    for line in lines:
        quantity = line.get("total_quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity > 1_000_000:
            raise NodeFailure("invalid_frozen_bom", "invalid frozen BOM quantity")
        revision = ctx.db.get(ModelRevision, _id(line.get("def_key")))
        part = ctx.db.get(Part, revision.part_id) if revision else None
        if (revision is None or part is None or part.project_id != ctx.project.id or str(part.id) != line.get("part_id")):
            raise NodeFailure("manufacturing_source_mismatch", "a BOM revision belongs to another part or project")
    return rev


def _identity(rev: AssemblyRevision, node: NodeRun, kind: str) -> dict:
    return {"type": kind, "assembly_id": str(rev.assembly_id), "assembly_revision_id": str(rev.id),
            "bom_sha256": rev.bom_sha256, "bom_snapshot_sha256": _digest(rev.bom), "node_run_id": str(node.id), **ESTIMATE}


def _line(line: dict) -> dict:
    return {"item": str(line["item"]), "part_id": line["part_id"], "model_revision_id": line["def_key"], "name": line["name"],
            "number": line.get("number"), "procurement": line["procurement"], "quantity_per_assembly": line["total_quantity"],
            "estimated": True}


def _record(ctx: NodeContext, kind: str, row: Any, key: str) -> None:
    version = assets.ensure_version(ctx.db, kind, row.id)
    assets.record_output(ctx.db, node_run=ctx.node_run, output_key=key, version=version)


def _outcome(ctx: NodeContext, key: str, batch: dict, code: str) -> NodeOutcome:
    ctx.step(ctx.node_run.node_type, complete=batch["complete"], items=len(batch["items"]), estimated=True)
    return NodeOutcome(outputs={key: batch}, steps=ctx.steps, failure=None if batch["complete"] else {
        "code": code, "message": "some manufactured items could not be estimated; available results and reasons are retained",
        "details": {"assembly_revision_id": batch["assembly_revision_id"]}})


def process_batch(ctx: NodeContext) -> NodeOutcome:
    _guard(ctx, "process", "assembly_process_plan")
    _lock(ctx)
    source, value = _source(ctx, ctx.node_run, "assembly", "assembly_from_spec", ctx.inputs.get("assembly"))
    rev = _assembly(ctx, source, value)
    params = PlanParams.model_validate(ctx.params.model_dump(mode="json")).model_dump(mode="json")
    items = []
    for line in rev.bom["flattened"]:
        _stop(ctx)
        entry = {**_line(line), "process_plan_id": None, "priceable": None, "status": "excluded", "reason_code": None}
        if line["procurement"] in ("buy", "standard"):
            entry["reason_code"] = "purchase_cost_required"
        elif line["procurement"] != "make":
            entry.update(status="unsupported", reason_code="procurement_unknown")
        elif line.get("representation", "exact") != "exact":
            entry.update(status="unsupported", reason_code="unsupported_representation")
        else:
            previous = ctx.db.scalars(select(ProcessPlan).where(ProcessPlan.node_run_id == ctx.node_run.id,
                ProcessPlan.model_revision_id == _id(line["def_key"]))).all()
            if len(previous) > 1 or (previous and previous[0].params != params):
                raise NodeFailure("manufacturing_source_mismatch", "a retry cannot replace an already saved plan's inputs")
            try:
                row = previous[0] if previous else manufacturing.create_plan(ctx.db, part=ctx.db.get(Part, _id(line["part_id"])),
                    user=ctx.user, model_revision_id=_id(line["def_key"]), params=params, node_run_id=ctx.node_run.id,
                    frozen_revision_material=True)
            except AppError as exc:
                entry.update(status="failed", reason_code=exc.code)
            else:
                _record(ctx, "process_plan", row, f"plans:{line['def_key']}")
                entry.update(status="planned", process_plan_id=str(row.id), priceable=bool(row.plan.get("priceable")),
                             reason_code=None if row.plan.get("priceable") else "not_priceable")
        items.append(entry)
    complete = all(i["status"] == "excluded" or (i["status"] == "planned" and i["priceable"]) for i in items)
    _guard(ctx, "process", "assembly_process_plan")
    return _outcome(ctx, "plans", {**_identity(rev, ctx.node_run, "manufacturing_plan"), "items": items, "complete": complete},
                    "manufacturing_plan_partial")


def _plans(ctx: NodeContext, node: NodeRun, value: Any) -> tuple[AssemblyRevision, dict[str, ProcessPlan]]:
    source, assembly = _source(ctx, node, "assembly", "assembly_from_spec")
    rev = _assembly(ctx, source, assembly)
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in _identity(rev, node, "manufacturing_plan").items()):
        raise NodeFailure("manufacturing_source_mismatch", "plan batch does not match its exact assembly and producer")
    rows: dict[str, ProcessPlan] = {}
    items = value.get("items") or []
    if len(items) != len(rev.bom["flattened"]):
        raise NodeFailure("manufacturing_source_mismatch", "plan batch does not cover its exact BOM")
    params = PlanParams.model_validate(_definition(ctx, node).get("params") or {}).model_dump(mode="json")
    for item, line in zip(items, rev.bom["flattened"]):
        if any(item.get(k) != v for k, v in _line(line).items()):
            raise NodeFailure("manufacturing_source_mismatch", "plan item differs from its frozen BOM")
        if item.get("process_plan_id"):
            row = ctx.db.get(ProcessPlan, _id(item["process_plan_id"]))
            revision = ctx.db.get(ModelRevision, _id(line["def_key"]))
            if (row is None or row.node_run_id != node.id or row.model_revision_id != revision.id
                    or str(row.part_id) != item["part_id"] or item.get("status") != "planned" or line["procurement"] != "make"
                    or item.get("priceable") != bool(row.plan.get("priceable"))
                    or row.params != params or row.plan.get("params") != params or row.plan.get("inputs_hash") != row.inputs_hash
                    or not row.plan.get("estimated") or "synthetic_demo_rates" not in row.plan.get("estimated_fields", [])
                    or row.summary != summary_from_revision(revision, revision.material).model_dump(mode="json")):
                raise NodeFailure("manufacturing_source_mismatch", "plan is not from this batch and exact model revision")
            rows[line["def_key"]] = row
        elif item.get("status") == "planned":
            raise NodeFailure("manufacturing_source_mismatch", "a planned item has no saved plan")
        if item.get("status") == "excluded" and line["procurement"] not in ("buy", "standard"):
            raise NodeFailure("manufacturing_source_mismatch", "manufactured parts cannot be silently excluded")
    complete = all(i.get("status") == "excluded" or (i.get("status") == "planned" and i.get("priceable")) for i in items)
    if value.get("complete") != complete:
        raise NodeFailure("manufacturing_source_mismatch", "plan completeness differs from its actual items")
    return rev, rows


def _quote_params(ctx: NodeContext, node: NodeRun, multiplier: int) -> dict:
    params = dict(_definition(ctx, node).get("params") or {})
    from ..workflow.nodes import QuoteParams

    params = QuoteParams.model_validate(params).model_dump(mode="json")
    params["quantities"] = sorted({q * multiplier for q in params["quantities"]})
    if max(params["quantities"]) > 1_000_000:
        raise NodeFailure("quantity_out_of_range", "assembly order quantity times BOM quantity exceeds 1,000,000")
    # The business date belongs to the run, so a retry across midnight cannot change a quote.
    params["quote_date"] = ctx.run.created_at.astimezone(ZoneInfo(get_settings().business_timezone)).date().isoformat()
    return CostParams.model_validate(params).model_dump(mode="json")


def _tier(row: AssemblyRollup) -> dict:
    return {"rollup_id": str(row.id), **{k: row.result[k] for k in ("quantity", "known_fen", "total_fen", "unknown_count", "complete")}}


def quote_batch(ctx: NodeContext) -> NodeOutcome:
    _guard(ctx, "quote", "assembly_quote")
    _lock(ctx)
    source, value = _source(ctx, ctx.node_run, "plans", "assembly_process_plan", ctx.inputs.get("plans"))
    rev, plans = _plans(ctx, source, value)
    quantities = _quote_params(ctx, ctx.node_run, 1)["quantities"]
    items = []
    for original in value["items"]:
        _stop(ctx)
        entry = {**original, "quote_id": None, "status": "excluded" if original["status"] == "excluded" else "unpriced"}
        plan = plans.get(original["model_revision_id"])
        if plan is not None and plan.plan.get("priceable"):
            try:
                params = _quote_params(ctx, ctx.node_run, original["quantity_per_assembly"])
                previous = ctx.db.scalars(select(Quote).where(Quote.node_run_id == ctx.node_run.id, Quote.process_plan_id == plan.id)).all()
                if len(previous) > 1 or (previous and previous[0].params != params):
                    raise NodeFailure("manufacturing_source_mismatch", "a retry cannot replace an already saved quote's inputs")
                row = previous[0] if previous else manufacturing.create_quote(ctx.db, plan_row=plan, user=ctx.user, params=params,
                    node_run_id=ctx.node_run.id, synthetic_estimate=True)
            except NodeFailure as exc:
                if exc.code != "quantity_out_of_range":
                    raise
                entry.update(status="unpriced", reason_code=exc.code)
            except AppError as exc:
                entry.update(status="failed", reason_code=exc.code)
            except PlanMismatchError:
                entry.update(status="failed", reason_code="plan_inputs_mismatch")
            else:
                _record(ctx, "quote", row, f"quotes:{original['model_revision_id']}")
                entry.update(status="quoted", quote_id=str(row.id), reason_code=None)
        elif plan is not None:
            entry["reason_code"] = "not_priceable"
        elif entry["status"] != "excluded" and not entry["reason_code"]:
            entry["reason_code"] = "plan_missing"
        items.append(entry)
    pinned = {i["model_revision_id"]: {"process_plan_id": i["process_plan_id"], "quote_id": i["quote_id"]} for i in items}
    tiers = []
    for quantity in quantities:
        _stop(ctx)
        result = assemblies.rollup(ctx.db, rev.id, ctx.user, quantity=quantity, pinned_sources=pinned, persist=False)
        row = ctx.db.scalar(select(AssemblyRollup).where(AssemblyRollup.revision_id == rev.id,
            AssemblyRollup.inputs_hash == result["inputs_hash"], AssemblyRollup.created_by == ctx.user.id).order_by(AssemblyRollup.created_at).limit(1))
        if row is None:
            result = assemblies.rollup(ctx.db, rev.id, ctx.user, quantity=quantity, pinned_sources=pinned)
            row = ctx.db.get(AssemblyRollup, _id(result["id"]))
        tiers.append(_tier(row))
    batch = {**_identity(rev, ctx.node_run, "manufacturing_quote"), "items": items, "quantities": quantities, "tiers": tiers,
             "complete": all(i["status"] in ("quoted", "excluded") for i in items), "price_complete": all(t["complete"] for t in tiers),
             "plan_node_run_id": str(source.id), "currency": "CNY"}
    _guard(ctx, "quote", "assembly_quote")
    return _outcome(ctx, "quotes", batch, "manufacturing_quote_partial")


def _quotes(ctx: NodeContext, node: NodeRun, value: Any, plan_node: NodeRun, plan_batch: dict,
            rev: AssemblyRevision) -> tuple[dict[str, Quote], list[AssemblyRollup]]:
    source, plans = _source(ctx, node, "plans", "assembly_process_plan")
    if source.id != plan_node.id or plans != plan_batch:
        raise NodeFailure("manufacturing_source_mismatch", "quote and export consume different plans")
    if (not isinstance(value, dict) or value.get("plan_node_run_id") != str(plan_node.id)
            or any(value.get(k) != v for k, v in _identity(rev, node, "manufacturing_quote").items())):
        raise NodeFailure("manufacturing_source_mismatch", "quote batch does not match its exact assembly and producer")
    items = value.get("items") or []
    if len(items) != len(plan_batch["items"]) or value.get("quantities") != _quote_params(ctx, node, 1)["quantities"]:
        raise NodeFailure("manufacturing_source_mismatch", "quote batch has a different BOM or order quantity")
    rows: dict[str, Quote] = {}
    for item, original in zip(items, plan_batch["items"]):
        if any(item.get(k) != original[k] for k in original if k not in ("status", "reason_code")):
            raise NodeFailure("manufacturing_source_mismatch", "quote item is not tied to its exact upstream plan")
        if item.get("quote_id"):
            row = ctx.db.get(Quote, _id(item["quote_id"]))
            if (row is None or row.node_run_id != node.id or str(row.process_plan_id) != item["process_plan_id"]
                    or str(row.part_id) != item["part_id"] or item.get("status") != "quoted"
                    or any(row.result.get(k) != v for k, v in ESTIMATE.items())
                    or row.params != _quote_params(ctx, node, item["quantity_per_assembly"])):
                raise NodeFailure("manufacturing_source_mismatch", "quote is not from this node and exact upstream plan")
            rows[item["model_revision_id"]] = row
        elif item.get("status") == "quoted":
            raise NodeFailure("manufacturing_source_mismatch", "a quoted item has no saved quote")
        if item.get("status") == "excluded" and original["status"] != "excluded":
            raise NodeFailure("manufacturing_source_mismatch", "manufactured parts cannot be silently excluded from quotes")
    if value.get("complete") != all(i.get("status") in ("quoted", "excluded") for i in items):
        raise NodeFailure("manufacturing_source_mismatch", "quote completeness differs from its actual items")
    pinned = {i["model_revision_id"]: {"process_plan_id": i["process_plan_id"], "quote_id": i["quote_id"]} for i in items}
    tiers = value.get("tiers") or []
    if [t.get("quantity") for t in tiers] != value["quantities"]:
        raise NodeFailure("manufacturing_source_mismatch", "quote rollup tiers do not match the order quantities")
    rolls = []
    for tier in tiers:
        row = ctx.db.get(AssemblyRollup, _id(tier.get("rollup_id")))
        if (row is None or row.revision_id != rev.id or row.created_by != ctx.user.id or _tier(row) != tier
                or row.params.get("pinned_sources") != pinned or row.params.get("quantity") != tier["quantity"]):
            raise NodeFailure("manufacturing_source_mismatch", "rollup is not from the exact selected plans and quotes")
        rolls.append(row)
    if value.get("price_complete") != all(r.result["complete"] for r in rolls):
        raise NodeFailure("manufacturing_source_mismatch", "quote completeness differs from its saved rollups")
    return rows, rolls


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str) and value and (value[0] in "=+-@" or value[0].isspace() or ord(value[0]) < 32):
        return "'" + value
    return value


def _csv(columns: list[str], rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return buffer.getvalue().encode("utf-8-sig")


def _html(title: str, columns: list[str], rows: list[dict], identity: dict) -> bytes:
    def escape(value: Any) -> str:
        return html.escape("未知 / Unknown" if value is None else str(value), quote=True)

    # The readable document leads with manufacturing facts. Full immutable IDs and
    # machine-readable values remain in the CSV/JSON and an expandable source block.
    process = "operation" in columns
    visible = (["item", "name", "quantity_per_assembly", "material", "operation", "machine_id", "setup_min", "cycle_min", "status", "reason_code", "gaps"]
               if process else ["row_kind", "order_quantity", "name", "quantity", "unit_fen", "total_fen", "known_fen", "unknown_count", "status", "reason_code"])
    headings = {"item": "序号 / Item", "name": "零件或装配 / Item name", "quantity_per_assembly": "每套数量 / Per assembly",
                "material": "材料 / Material", "operation": "工序 / Operation", "machine_id": "设备 / Machine",
                "setup_min": "准备时间（分钟） / Setup min", "cycle_min": "单件时间（分钟） / Cycle min",
                "gaps": "工艺限制 / Coverage warnings", "status": "状态 / Status", "reason_code": "待补充 / Missing information", "row_kind": "项目 / Category",
                "order_quantity": "订单套数 / Assembly order", "quantity": "需求数量 / Required quantity",
                "unit_fen": "单价（人民币元） / Unit CNY", "total_fen": "金额（人民币元） / Amount CNY",
                "known_fen": "已知小计（人民币元） / Known subtotal CNY", "unknown_count": "未定价项目 / Unpriced items"}
    labels = {"planned": "已生成工艺 / Planned", "quoted": "已计算估算 / Estimated", "excluded": "外购，不加工 / Purchased",
              "unsupported": "暂不支持 / Unsupported", "failed": "失败 / Failed", "unpriced": "无法定价 / Unpriced",
              "known": "已知估算 / Known estimate", "unknown": "未知 / Unknown", "complete": "完整 / Complete",
              "incomplete": "价格不完整 / Price incomplete", "part": "零件 / Part", "assembly_labour": "装配人工 / Assembly labour",
              "total": "汇总 / Summary", "purchase_cost_required": "待补外购价格 / Purchase price required",
              "assembly_labour_cost_required": "待补装配人工费 / Assembly labour cost required",
              "procurement_unknown": "待定自制或外购 / Procurement unknown", "unsupported_representation": "几何表达不支持加工 / Unsupported geometry",
              "not_priceable": "存在工艺阻塞项 / Manufacturing blockers", "quantity_out_of_range": "数量超出计算范围 / Quantity out of range",
              "plan_missing": "缺少工艺 / Process plan missing"}

    def display(row: dict, key: str) -> str:
        value = row.get(key)
        if value is None:
            return "未知 / Unknown" if key in ("unit_fen", "total_fen") and row.get("row_kind") != "total" else (
                "未知 / Unknown" if key == "total_fen" else "—")
        if key == "gaps":
            gaps = json.loads(value)
            return "; ".join(f"{g.get('message_zh', '')} / {g.get('message_en', '')}" for g in gaps) or "—"
        if key.endswith("_fen"):
            return f"{value // 100:,}.{value % 100:02d}"
        return labels.get(str(value), str(value)) if key in ("status", "reason_code", "row_kind") else str(value)

    table = "<thead><tr>" + "".join(f"<th>{escape(headings[c])}</th>" for c in visible) + "</tr></thead><tbody>"
    table += "".join("<tr>" + "".join(f"<td>{escape(display(row, c))}</td>" for c in visible) + "</tr>" for row in rows) + "</tbody>"
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<style>body{font:14px system-ui;margin:24px;color:#17212b}table{border-collapse:collapse}th,td{border:1px solid #cbd5e1;"
            "padding:7px;text-align:left;vertical-align:top}th{background:#eef2f6}pre{white-space:pre-wrap}p{max-width:80ch}</style>"
            f"<title>{escape(title)}</title><h1>{escape(title)}</h1><p>{escape(NOTICE)}</p>"
            f"<div style='overflow:auto'><table>{table}</table></div>"
            "<details><summary>精确版本、计算来源与完整性 / Exact versions, calculation sources and completeness</summary>"
            f"<pre>{escape(json.dumps(identity, ensure_ascii=False, sort_keys=True, indent=2))}</pre></details></html>").encode("utf-8")


def _reports(ctx: NodeContext, rev: AssemblyRevision | None, plan_batch: dict | None, quote_batch: dict | None,
             plans: dict[str, ProcessPlan], quotes: dict[str, Quote], rolls: list[AssemblyRollup]) -> orchestration.ManufacturingReports:
    sources = {"run_id": str(ctx.run.id), "assembly_revision_id": str(rev.id) if rev else None, "bom_sha256": rev.bom_sha256 if rev else None,
               "bom_snapshot_sha256": _digest(rev.bom) if rev else None, "plan_node_run_id": (plan_batch or {}).get("node_run_id"),
               "quote_node_run_id": (quote_batch or {}).get("node_run_id"),
               "process_plans": [{"id": str(p.id), "model_revision_id": str(p.model_revision_id), "inputs_hash": p.inputs_hash,
                                  "content_sha256": _digest({"summary": p.summary, "params": p.params, "plan": p.plan})} for p in plans.values()],
               "quotes": [{"id": str(q.id), "process_plan_id": str(q.process_plan_id), "inputs_hash": q.inputs_hash,
                           "content_sha256": _digest({"params": q.params, "result": q.result})} for q in quotes.values()],
               "rollups": [{"id": str(r.id), "inputs_hash": r.inputs_hash} for r in rolls]}
    summary = {**ESTIMATE, "price_complete": bool(quote_batch and quote_batch["price_complete"]),
               "process_complete": bool(plan_batch and plan_batch["complete"]),
               "quote_complete": bool(quote_batch and quote_batch["complete"]), "currency": "CNY",
               "tiers": (quote_batch or {}).get("tiers", []), "notice": NOTICE}
    files = []
    missing = []
    for kind, batch in (("process", plan_batch), ("quote", quote_batch)):
        if batch is None:
            continue
        rows = []
        if kind == "process":
            for item in batch["items"]:
                plan = plans.get(item["model_revision_id"])
                for op in (plan.plan.get("operations") or [{}]) if plan else [{}]:
                    rows.append({**item, "material": plan.summary["material"] if plan else None,
                        "operation": f"{op.get('label_zh', '')} / {op.get('label_en', '')}" if op else None,
                        "op_id": op.get("op_id"), "machine_id": op.get("machine_id"), "setup_min": op.get("setup_min"),
                        "cycle_min": op.get("cycle_min"), "formula": op.get("formula"), "rules_version": plan.rules_version if plan else None,
                        "gaps": json.dumps(plan.plan.get("gaps", []), ensure_ascii=False) if plan else None, "notice": NOTICE})
            columns = ["item", "name", "number", "procurement", "quantity_per_assembly", "status", "reason_code", "material",
                       "op_id", "operation", "machine_id", "setup_min", "cycle_min", "formula", "gaps", "rules_version",
                       "part_id", "model_revision_id", "process_plan_id", "estimated", "notice"]
            data = {"batch": batch, "plans": [manufacturing.plan_out(p) for p in plans.values()]}
            title = "加工工艺草稿 / Manufacturing process estimate"
        else:
            for roll in rolls:
                for line in roll.result["lines"] + roll.result["assembly_labour"]:
                    rows.append({**line, "order_quantity": roll.result["quantity"], "row_kind": "part" if "part_id" in line else "assembly_labour",
                        "known_fen": None, "reason_code": "assembly_labour_cost_required" if "part_id" not in line and line["status"] == "unknown"
                            else next((i["reason_code"] for i in batch["items"] if i["model_revision_id"] == line["def_key"]), None),
                        "basis": json.dumps(line.get("basis"), ensure_ascii=False), "estimated": True, "notice": NOTICE})
                rows.append({"row_kind": "total", "order_quantity": roll.result["quantity"], "status": "complete" if roll.result["complete"] else "incomplete",
                    "total_fen": roll.result["total_fen"], "known_fen": roll.result["known_fen"], "unknown_count": roll.result["unknown_count"],
                    "estimated": True, "notice": NOTICE})
            columns = ["row_kind", "order_quantity", "item", "name", "number", "procurement", "quantity", "status", "reason_code",
                       "unit_fen", "total_fen", "known_fen", "unknown_count", "basis", "part_id", "def_key", "process_plan_id", "quote_id", "estimated", "notice"]
            data = {"batch": batch, "quotes": [manufacturing.quote_out(q) for q in quotes.values()],
                    "rollups": [{**r.result, "id": str(r.id)} for r in rolls]}
            title = "估算报价草稿（人民币元） / Draft quotation (CNY)"
        identity = {"sources": sources, **summary}
        bodies = {"csv": _csv(columns, rows), "html": _html(title, columns, rows, identity),
                  "json": json.dumps({**identity, **data}, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")}
        files.extend(orchestration.ManufacturingReport(kind, f"manufacturing/{kind}.{ext}", body, sources) for ext, body in bodies.items())
        if not batch["complete"]:
            missing.append({"item": kind, "code": "manufacturing_incomplete", "message": f"some {kind} items could not be estimated; see the report"})
    return orchestration.ManufacturingReports(tuple(files), sources, summary, tuple(missing))


def _drawing(ctx: NodeContext, node: NodeRun, value: Any, rev: AssemblyRevision) -> tuple[Any, list[dict] | None]:
    source, assembly = _source(ctx, node, "assembly", "assembly_from_spec")
    if _assembly(ctx, source, assembly).id != rev.id:
        raise NodeFailure("manufacturing_source_mismatch", "drawings refer to a different assembly")
    if value is None:
        return None, [] if (_definition(ctx, node).get("params") or {}).get("subassemblies") else None
    if not isinstance(value, dict) or value.get("type") != "drawing_package":
        raise NodeFailure("manufacturing_source_mismatch", "invalid connected drawing output")
    pkg = ctx.db.get(AssemblyDrawing, _id(value["package_id"])) if value.get("package_id") else None
    if value.get("package_id") and (pkg is None or pkg.node_run_id != node.id or pkg.assembly_revision_id != rev.id
                                   or pkg.project_id != ctx.project.id or pkg.manifest_sha256 != value.get("manifest_sha256")):
        raise NodeFailure("manufacturing_source_mismatch", "drawing package is not from this node and assembly revision")
    subs = value.get("subassembly_packages")
    if subs is not None:
        valid = {s["assembly_revision_id"] for s in assemblies.subassembly_revisions(ctx.db, rev.id)}
        for sub in subs:
            if sub.get("assembly_revision_id") not in valid:
                raise NodeFailure("manufacturing_source_mismatch", "drawing subassembly is not in this assembly")
            if sub.get("package_id"):
                row = ctx.db.get(AssemblyDrawing, _id(sub["package_id"]))
                if (row is None or row.node_run_id != node.id or row.project_id != ctx.project.id
                        or str(row.assembly_revision_id) != sub["assembly_revision_id"] or row.manifest_sha256 != sub.get("manifest_sha256")):
                    raise NodeFailure("manufacturing_source_mismatch", "subassembly drawing is not from the exact connected source")
    return pkg, subs


def _bundle(ctx: NodeContext, delivery: DeliveryManifest, fv: FileVersion) -> NodeOutcome:
    m = delivery.manifest
    geometry_complete = not any(g["item"] not in ("process", "quote") for g in m["missing"])
    value = {"type": "file", "file_version_id": str(fv.id), "name": fv.original_name, "blob_sha256": fv.blob_sha256,
             "delivery_manifest_id": str(delivery.id), "manifest_sha256": delivery.manifest_sha256,
             **ESTIMATE, "price_complete": m["manufacturing"]["price_complete"], "geometry_complete": geometry_complete,
             "complete": delivery.completeness == "complete", "verification": m["verification"]}
    _record(ctx, "delivery_manifest", delivery, "delivery")
    _record(ctx, "document", fv, "bundle")
    ctx.step("manufacturing_export", complete=value["complete"], geometry_complete=geometry_complete, price_complete=value["price_complete"])
    return NodeOutcome(outputs={"bundle": value}, steps=ctx.steps, failure=None if value["complete"] else {
        "code": "manufacturing_delivery_partial", "message": "the package retains available files and lists missing deliverables",
        "details": {"delivery_manifest_id": str(delivery.id), "missing": m["missing"]}})



def _built_parts(ctx: NodeContext, node: NodeRun, value: dict) -> list[str]:
    """Parts actually adopted by this exact upstream node, including valid prior-build reuse.

    The node's saved RunOutputs bind reused revisions too; a same-project latest
    revision or an ID merely placed in an input is never sufficient authority.
    """
    parts = value.get("parts") or []
    if not isinstance(parts, list) or len(parts) > assemblies.MAX_DEFINITIONS:
        raise NodeFailure("manufacturing_source_mismatch", "invalid upstream part list")
    selected = []
    for item in parts:
        revision = ctx.db.get(ModelRevision, _id(item.get("revision_id")))
        part = ctx.db.get(Part, revision.part_id) if revision else None
        output = ctx.db.scalar(select(RunOutput.id).join(AssetVersion, AssetVersion.id == RunOutput.asset_version_id).where(
            RunOutput.run_id == ctx.run.id, RunOutput.node_run_id == node.id, RunOutput.output_key == "parts",
            AssetVersion.model_revision_id == revision.id)) if revision else None
        if (revision is None or part is None or part.project_id != ctx.project.id or output is None
                or str(part.id) != item.get("part_id") or revision.status != "ready"):
            raise NodeFailure("manufacturing_source_mismatch", "part is not a saved result of this exact upstream node")
        if str(revision.id) not in selected:
            selected.append(str(revision.id))
    return selected


def export_package(ctx: NodeContext) -> NodeOutcome:
    _guard(ctx, "quote", "manufacturing_export")
    _lock(ctx)
    asm_node, asm_value = _source(ctx, ctx.node_run, "assembly", "assembly_from_spec", ctx.inputs.get("assembly"))
    if not isinstance(asm_value, dict) or asm_value.get("type") != "assembly":
        raise NodeFailure("assembly_missing", "the upstream node produced no assembly or parts")
    rev = _assembly(ctx, asm_node, asm_value) if asm_value.get("assembly_revision_id") else None
    part_ids = _built_parts(ctx, asm_node, asm_value)
    plan_node, plan_value = _source(ctx, ctx.node_run, "plans", "assembly_process_plan", ctx.inputs.get("plans"))
    plans: dict[str, ProcessPlan] = {}
    if plan_value is not None:
        if rev is None:
            raise NodeFailure("manufacturing_source_mismatch", "process plans have no frozen assembly BOM")
        plan_rev, plans = _plans(ctx, plan_node, plan_value)
        if plan_rev.id != rev.id:
            raise NodeFailure("manufacturing_source_mismatch", "export and plans refer to different assemblies")
    quote_node, quote_value = _source(ctx, ctx.node_run, "quotes", "assembly_quote", ctx.inputs.get("quotes"))
    quotes: dict[str, Quote] = {}
    rolls: list[AssemblyRollup] = []
    if quote_value is not None:
        if plan_value is None or rev is None:
            raise NodeFailure("manufacturing_source_mismatch", "quotes have no matching process batch")
        quotes, rolls = _quotes(ctx, quote_node, quote_value, plan_node, plan_value, rev)
    drawing_node, drawing_value = _source(ctx, ctx.node_run, "drawings", "assembly_drawing", ctx.inputs.get("drawings"))
    if rev is None:
        drawing_asm, _ = _source(ctx, drawing_node, "assembly", "assembly_from_spec")
        if drawing_value is not None or drawing_asm.id != asm_node.id:
            raise NodeFailure("manufacturing_source_mismatch", "drawing source has no matching assembly")
        pkg, subs = None, [] if (_definition(ctx, drawing_node).get("params") or {}).get("subassemblies") else None
    else:
        pkg, subs = _drawing(ctx, drawing_node, drawing_value, rev)
    reports = _reports(ctx, rev, plan_value, quote_value, plans, quotes, rolls)
    fingerprint = _digest({"assembly": asm_value, "plans": plan_value, "quotes": quote_value, "drawings": drawing_value, "policy": ctx.policy})
    note = f"manufacturing:{ctx.node_run.id}:{fingerprint}"
    previous = ctx.db.scalar(select(FileVersion).join(File, File.id == FileVersion.file_id).join(AssetVersion, AssetVersion.file_version_id == FileVersion.id)
        .where(FileVersion.note == note, File.project_id == ctx.project.id, AssetVersion.node_run_id == ctx.node_run.id))
    if previous is not None:
        delivery = ctx.db.scalar(select(DeliveryManifest).where(DeliveryManifest.node_run_id == ctx.node_run.id,
            DeliveryManifest.zip_sha256 == previous.blob_sha256))
        if delivery is None:
            raise NodeFailure("manufacturing_source_mismatch", "saved manufacturing export lost its delivery manifest")
        _guard(ctx, "quote", "manufacturing_export")
        return _bundle(ctx, delivery, previous)
    from ..workflow.handlers import _current_coverage_projection, _upstream

    _stop(ctx)
    try:
        delivery = orchestration.deliver(ctx.db, project_id=ctx.project.id, user=ctx.user, policy=ctx.policy,
            assembly_revision_id=rev.id if rev else None, part_revision_ids=part_ids,
            drawing_package=pkg, drawing_error=drawing_node.error if pkg is None else None,
            drawings_required=True, spec_version_id=rev.spec_version_id if rev else asm_value.get("spec_version_id"),
            coverage=asm_value.get("coverage") if rev is None else None, node_run_id=ctx.node_run.id,
            coverage_projection=_current_coverage_projection(ctx, asm_value) if rev is not None else None,
            subassembly_packages=subs, upstream=_upstream(ctx), manufacturing_reports=reports)
    except AppError as exc:
        raise NodeFailure(exc.code, exc.message, details=exc.details) from exc
    _guard(ctx, "quote", "manufacturing_export")
    name = f"manufacturing_{rev.id}_A{rev.rev_no}.zip" if rev else f"manufacturing_{ctx.run.id}_partial.zip"
    file = File(project_id=ctx.project.id, kind="other", name=name, created_by=ctx.user.id)
    ctx.db.add(file)
    ctx.db.flush()
    version = FileVersion(file_id=file.id, version=1, blob_sha256=delivery.zip_sha256, original_name=file.name, note=note, created_by=ctx.user.id)
    ctx.db.add(version)
    ctx.db.flush()
    dependencies = [assets.ensure_version(ctx.db, "delivery_manifest", delivery.id)]
    dependencies += [assets.ensure_version(ctx.db, "process_plan", p.id) for p in plans.values()]
    dependencies += [assets.ensure_version(ctx.db, "quote", q.id) for q in quotes.values()]
    labels = sorted({"estimated", "draft", *[label for dep in dependencies for label in dep.labels]})
    if delivery.completeness != "complete":
        labels.append("partial")
    assets.register_version(ctx.db, asset_type="document", row=version, project_id=ctx.project.id, file_id=file.id,
        name=file.name, created_by=ctx.user.id, node_run_id=ctx.node_run.id, labels=labels,
        depends_on=[(dep.id, "derived_from") for dep in dependencies])
    return _bundle(ctx, delivery, version)
