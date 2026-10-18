"""Deterministic quotation from a process plan.

``compute_quote(plan, summary, params) -> QuoteResult``

Money rules (carried over from the v0.3 prototype):

* integer fen everywhere, half-up rounding (never banker's rounding);
* every cost line is rounded *before* summing, so the lines always add up to
  the unit cost that is shown;
* profit and tax are kept out of cost and shown separately;
* the quote date is an explicit input - the clock is never read here.

Per quantity tier q (each line cites its constants):

  material     = round(blank mass kg x price fen/kg)
  machining    = sum over machine ops of round(cycle_min x rate fen/h / 60)   (see op_costs)
  setup        = round((sum over ops of round(setup_min x rate / 60)
                        + round(programming_min x R-PROG-RATE / 60)) / q)
  subcontract  = round(max(area dm² x fen/dm², lot minimum / q))            (0 without OP55)
  loss         = round(R-SCRAP-RATE x (material + machining))
  unit_cost    = sum of the rounded lines
  profit       = round(unit_cost x margin_pct / 100)
  unit_ex_tax  = unit_cost + profit
  tax          = round(unit_ex_tax x tax_pct / 100);  unit_inc_tax = unit_ex_tax + tax
  order totals = unit amounts x q
  lead days    = R-LEAD-MATERIAL + ceil(max machine load / R-DAY-CAPACITY)
                 + max subcontract lead + R-LEAD-PACK,
                 machine load = sum(cycle_min x q + setup_min) per machine

Every line except setup and subcontract is independent of q, and those two
only decrease with q, so unit cost is non-increasing with quantity.

A plan with blocking gaps is refused: ``priceable=False``, no tiers, and the
blocking gaps as ``refusal_reasons``. A plan computed from different inputs
or rules than ``summary`` raises :class:`PlanMismatchError`.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .hashing import dec, fmt_num, fmt_yuan, plan_inputs_hash, quote_inputs_hash, to_fen
from .materials import (
    MATERIAL_PRICE_QUOTED_AT,
    MATERIAL_PRICE_VALID_UNTIL,
    MATERIAL_PRICE_VERSION,
    get_material,
)
from .planning import Gap, ProcessPlan
from .rates import (
    MACHINES,
    QUOTE_RULES_VERSION,
    RATE_TABLE_EFFECTIVE,
    RATE_TABLE_VERSION,
    SUBCONTRACTS,
    SourceRef,
    cite,
    cite_machine_rate,
    cite_price,
    cite_subcontract,
    const,
)
from .schemas import FeatureSummary

CostKey = Literal["material", "machining", "setup", "subcontract", "loss"]
AlertLevel = Literal["info", "warning", "blocking"]

COST_LABELS: dict[str, tuple[str, str]] = {
    "material": ("材料", "Material"),
    "machining": ("加工", "Machining"),
    "setup": ("装夹与编程（分摊）", "Setup & programming (amortised)"),
    "subcontract": ("外协", "Subcontract"),
    "loss": ("损耗", "Scrap / loss"),
}


class PlanMismatchError(ValueError):
    """The plan was not computed from this summary/params under the current planning rules."""


class QuoteParams(BaseModel):
    """Commercial inputs of a quote. Part of the quote ``inputs_hash``.

    ``quantities`` are normalised to sorted unique tiers.
    """

    model_config = ConfigDict(extra="forbid")

    quantities: list[int] = Field(min_length=1, max_length=20)
    margin_pct: float = Field(default=15.0, ge=0, le=200)
    tax_pct: float = Field(default=13.0, ge=0, le=100)
    currency: Literal["CNY"] = "CNY"
    quote_date: date
    validity_days: int = Field(default=30, ge=1, le=365)

    @field_validator("quantities")
    @classmethod
    def _tiers(cls, v: list[int]) -> list[int]:
        if any(q < 1 or q > 1_000_000 for q in v):
            raise ValueError("each quantity must be between 1 and 1,000,000")
        return sorted(set(v))


class CostLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: CostKey
    label_zh: str
    label_en: str
    fen: int
    formula: str
    sources: list[SourceRef]
    estimated: bool


class TierResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qty: int
    lines: list[CostLine]
    unit_cost_fen: int
    profit_fen: int
    unit_ex_tax_fen: int
    tax_fen: int
    unit_inc_tax_fen: int
    order_ex_tax_fen: int
    order_tax_fen: int
    order_inc_tax_fen: int
    lead_days: int
    lead_formula: str


class OpCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: str
    label_zh: str
    label_en: str
    machine_id: str
    rate_fen_per_h: int
    cycle_min: float
    cycle_fen: int
    setup_min: float
    setup_fen: int
    formula: str
    estimated: bool


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    level: AlertLevel
    message_zh: str
    message_en: str


class QuoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priceable: bool
    refusal_reasons: list[Gap]
    rules_version: str
    inputs_hash: str
    plan_inputs_hash: str
    table_versions: dict[str, str]
    currency: Literal["CNY"]
    quote_date: date
    valid_until: date
    material_price_valid_until: date
    estimated: bool
    estimated_fields: list[str]
    op_costs: list[OpCost]
    programming_fen: int
    batch_fixed_fen: int
    tiers: list[TierResult]
    alerts: list[Alert]


def _q(x: float | int | Decimal, nd: int = 2) -> str:
    return fmt_num(x, nd)


def _g(x: float) -> str:
    """Compact number: up to 3 decimals, trailing zeros dropped (110.0 -> '110')."""
    s = fmt_num(x, 3)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _plan_alerts(plan: ProcessPlan) -> list[Alert]:
    alerts: list[Alert] = []
    if plan.estimated:
        fields = ", ".join(plan.estimated_fields)
        drawing = plan.source == "drawing_intake"
        why_zh, why_en = ("仅有图纸", "drawing only") if drawing else ("缺少测量值", "missing measurements")
        alerts.append(Alert(
            code="ESTIMATED_INPUTS", level="warning",
            message_zh=f"部分数据为估算（{why_zh}）：{fields}",
            message_en=f"Some figures are estimates ({why_en}): {fields}",
        ))
    for g in plan.gaps:
        alerts.append(Alert(code=f"GAP_{g.code}", level=g.severity, message_zh=g.message_zh, message_en=g.message_en))
    for a in plan.assumptions:
        alerts.append(Alert(code=f"ASSUMPTION_{a.code}", level="info", message_zh=a.message_zh, message_en=a.message_en))
    return alerts


def compute_quote(plan: ProcessPlan, summary: FeatureSummary, params: QuoteParams) -> QuoteResult:
    """Price ``plan`` for every quantity tier in ``params`` (see module docstring for formulas)."""
    expected = plan_inputs_hash(summary, plan.params)
    if expected != plan.inputs_hash:
        raise PlanMismatchError(
            "Process plan does not match this feature summary/params under the current planning rules "
            f"(plan {plan.inputs_hash[:12]}…, expected {expected[:12]}…); re-plan before quoting"
        )

    valid_until = params.quote_date + timedelta(days=params.validity_days)
    alerts = _plan_alerts(plan)
    common = dict(
        rules_version=QUOTE_RULES_VERSION,
        inputs_hash=quote_inputs_hash(summary, plan.params, params),
        plan_inputs_hash=plan.inputs_hash,
        table_versions={**plan.table_versions, "rates": RATE_TABLE_VERSION, "material_prices": MATERIAL_PRICE_VERSION},
        currency=params.currency,
        quote_date=params.quote_date,
        valid_until=valid_until,
        material_price_valid_until=MATERIAL_PRICE_VALID_UNTIL,
        estimated=plan.estimated,
        estimated_fields=list(plan.estimated_fields),
    )

    blocking = [g for g in plan.gaps if g.severity == "blocking"]
    if not plan.priceable or blocking or plan.blank is None or plan.material_id is None:
        return QuoteResult(priceable=False, refusal_reasons=blocking, op_costs=[], programming_fen=0,
                           batch_fixed_fen=0, tiers=[], alerts=alerts, **common)

    mat = get_material(plan.material_id)
    blank = plan.blank
    assert blank.mass_kg is not None  # material is known, so planning computed the mass

    # --- per-op machine costs (rounded per op)
    op_costs: list[OpCost] = []
    for op in plan.operations:
        if op.machine_id is None:
            continue
        rate = MACHINES[op.machine_id].rate_fen_per_h
        cycle_fen = to_fen(dec(op.cycle_min) * rate / 60)
        setup_fen = to_fen(dec(op.setup_min) * rate / 60)
        op_costs.append(OpCost(
            op_id=op.op_id, label_zh=op.label_zh, label_en=op.label_en, machine_id=op.machine_id,
            rate_fen_per_h=rate, cycle_min=op.cycle_min, cycle_fen=cycle_fen, setup_min=op.setup_min,
            setup_fen=setup_fen, estimated=op.estimated,
            formula=(f"cycle {_q(op.cycle_min)} min × {fmt_yuan(rate)}/h ÷ 60 = {fmt_yuan(cycle_fen)}; "
                     f"setup {_q(op.setup_min)} min × {fmt_yuan(rate)}/h ÷ 60 = {fmt_yuan(setup_fen)}"),
        ))
    machining_fen = sum(o.cycle_fen for o in op_costs)
    setup_batch_fen = sum(o.setup_fen for o in op_costs)
    prog_rate = int(const("R-PROG-RATE"))
    programming_fen = to_fen(dec(plan.programming_min) * prog_rate / 60)
    batch_fixed_fen = setup_batch_fen + programming_fen

    material_fen = to_fen(dec(blank.mass_kg) * mat.price_fen_per_kg)
    scrap = dec(const("R-SCRAP-RATE"))
    loss_fen = to_fen(scrap * (material_fen + machining_fen))

    machine_ops = [o for o in plan.operations if o.machine_id is not None]
    sub_op = next((o for o in plan.operations if o.subcontract_id is not None), None)
    sub = SUBCONTRACTS[sub_op.subcontract_id] if sub_op and sub_op.subcontract_id else None
    area_dm2 = dec(sub_op.inputs.get("area_dm2", 0) or 0) if sub_op else Decimal(0)

    est_material = blank.estimated
    est_machining = any(o.estimated for o in machine_ops)
    est_setup = plan.source == "drawing_intake" or summary.estimated

    day_cap = int(const("R-DAY-CAPACITY"))
    lead_mat, lead_pack = int(const("R-LEAD-MATERIAL")), int(const("R-LEAD-PACK"))
    sub_lead = max((o.lead_days for o in plan.operations), default=0)

    tiers: list[TierResult] = []
    for qty in params.quantities:
        setup_fen = to_fen(dec(batch_fixed_fen) / qty)
        if sub is not None:
            per_piece = area_dm2 * sub.fen_per_dm2
            lot_share = dec(sub.lot_min_fen) / qty
            sub_fen = to_fen(max(per_piece, lot_share))
            sub_formula = (f"max({_q(area_dm2, 4)} dm² × {fmt_yuan(sub.fen_per_dm2)}/dm² = {fmt_yuan(to_fen(per_piece))}, "
                           f"lot minimum {fmt_yuan(sub.lot_min_fen)} ÷ {qty} = {fmt_yuan(to_fen(lot_share))}) "
                           f"= {fmt_yuan(sub_fen)}")
            sub_sources = cite_subcontract(sub.id)
        else:
            sub_fen, sub_formula, sub_sources = 0, "no subcontract operation", []
        lines = [
            CostLine(
                key="material", label_zh=COST_LABELS["material"][0], label_en=COST_LABELS["material"][1],
                fen=material_fen, estimated=est_material,
                formula=(f"{mat.id} blank {'×'.join(_g(d) for d in blank.dims_mm)} mm = "
                         f"{_q(blank.mass_kg, 4)} kg × {fmt_yuan(mat.price_fen_per_kg)}/kg = {fmt_yuan(material_fen)}"),
                sources=[cite_price(mat)] + blank.sources,
            ),
            CostLine(
                key="machining", label_zh=COST_LABELS["machining"][0], label_en=COST_LABELS["machining"][1],
                fen=machining_fen, estimated=est_machining,
                formula=(" + ".join(f"{o.op_id} {fmt_yuan(o.cycle_fen)}" for o in op_costs)
                         + f" = {fmt_yuan(machining_fen)} (Σ round(cycle × rate ÷ 60), see op costs)"),
                sources=[cite_machine_rate(o.machine_id) for o in op_costs],
            ),
            CostLine(
                key="setup", label_zh=COST_LABELS["setup"][0], label_en=COST_LABELS["setup"][1],
                fen=setup_fen, estimated=est_setup,
                formula=(f"(setups {fmt_yuan(setup_batch_fen)} + programming {_q(plan.programming_min)} min × "
                         f"{fmt_yuan(prog_rate)}/h ÷ 60 = {fmt_yuan(programming_fen)}) ÷ {qty} pcs = {fmt_yuan(setup_fen)}"),
                sources=cite("R-PROG-RATE") + plan.programming_sources,
            ),
            CostLine(
                key="subcontract", label_zh=COST_LABELS["subcontract"][0], label_en=COST_LABELS["subcontract"][1],
                fen=sub_fen, estimated=bool(sub_op and sub_op.estimated), formula=sub_formula, sources=sub_sources,
            ),
            CostLine(
                key="loss", label_zh=COST_LABELS["loss"][0], label_en=COST_LABELS["loss"][1],
                fen=loss_fen, estimated=est_material or est_machining,
                formula=(f"{fmt_num(scrap * 100, 1)}% × (material {fmt_yuan(material_fen)} + machining "
                         f"{fmt_yuan(machining_fen)}) = {fmt_yuan(loss_fen)}"),
                sources=cite("R-SCRAP-RATE"),
            ),
        ]
        unit_cost = sum(line.fen for line in lines)
        profit = to_fen(dec(unit_cost) * dec(params.margin_pct) / 100)
        unit_ex = unit_cost + profit
        tax = to_fen(dec(unit_ex) * dec(params.tax_pct) / 100)
        unit_inc = unit_ex + tax

        load: dict[str, Decimal] = {}
        for o in machine_ops:
            assert o.machine_id is not None
            load[o.machine_id] = load.get(o.machine_id, Decimal(0)) + dec(o.cycle_min) * qty + dec(o.setup_min)
        bottleneck_id, bottleneck = max(load.items(), key=lambda kv: (kv[1], kv[0])) if load else ("-", Decimal(0))
        mach_days = math.ceil(bottleneck / day_cap) if bottleneck > 0 else 0
        lead = lead_mat + mach_days + sub_lead + lead_pack
        lead_formula = (f"{lead_mat} (material) + ceil({_q(bottleneck)} min on {bottleneck_id} ÷ {day_cap} min/day) = "
                        f"{mach_days} + {sub_lead} (subcontract) + {lead_pack} (inspection/packing) = {lead} days")
        tiers.append(TierResult(
            qty=qty, lines=lines, unit_cost_fen=unit_cost, profit_fen=profit, unit_ex_tax_fen=unit_ex, tax_fen=tax,
            unit_inc_tax_fen=unit_inc, order_ex_tax_fen=unit_ex * qty, order_tax_fen=tax * qty,
            order_inc_tax_fen=unit_inc * qty, lead_days=lead, lead_formula=lead_formula,
        ))

    # --- commercial alerts
    if params.quote_date > MATERIAL_PRICE_VALID_UNTIL:
        alerts.append(Alert(
            code="MATERIAL_PRICE_EXPIRED", level="warning",
            message_zh=f"材料价格 {MATERIAL_PRICE_VERSION} 已于 {MATERIAL_PRICE_VALID_UNTIL} 过期，请重新询价",
            message_en=f"Material prices {MATERIAL_PRICE_VERSION} expired on {MATERIAL_PRICE_VALID_UNTIL}; re-quote material",
        ))
    elif valid_until > MATERIAL_PRICE_VALID_UNTIL:
        alerts.append(Alert(
            code="MATERIAL_PRICE_EXPIRES_BEFORE_VALIDITY", level="warning",
            message_zh=(f"材料价格 {MATERIAL_PRICE_VERSION} 有效至 {MATERIAL_PRICE_VALID_UNTIL}，早于报价有效期 "
                        f"{valid_until}；建议缩短有效期或重新询材料价"),
            message_en=(f"Material prices {MATERIAL_PRICE_VERSION} are valid until {MATERIAL_PRICE_VALID_UNTIL}, before the "
                        f"quote validity end {valid_until}; shorten validity or re-quote material"),
        ))
    if params.quote_date < MATERIAL_PRICE_QUOTED_AT:
        alerts.append(Alert(
            code="MATERIAL_PRICE_NOT_YET_QUOTED", level="warning",
            message_zh=f"报价日 {params.quote_date} 早于材料价格询价日 {MATERIAL_PRICE_QUOTED_AT}",
            message_en=f"Quote date {params.quote_date} is before the material price date {MATERIAL_PRICE_QUOTED_AT}",
        ))
    if params.quote_date < RATE_TABLE_EFFECTIVE:
        alerts.append(Alert(
            code="RATE_TABLE_NOT_EFFECTIVE", level="warning",
            message_zh=f"费率表 {RATE_TABLE_VERSION} 自 {RATE_TABLE_EFFECTIVE} 生效，晚于报价日 {params.quote_date}",
            message_en=(f"Rate table {RATE_TABLE_VERSION} is effective from {RATE_TABLE_EFFECTIVE}, "
                        f"after quote date {params.quote_date}"),
        ))
    floor = float(const("R-MARGIN-FLOOR"))
    if params.margin_pct < floor:
        alerts.append(Alert(
            code="MARGIN_BELOW_FLOOR", level="warning",
            message_zh=f"利润率 {fmt_num(params.margin_pct, 1)}% 低于本厂下限 {fmt_num(floor, 0)}%",
            message_en=f"Margin {fmt_num(params.margin_pct, 1)}% is below the shop floor {fmt_num(floor, 0)}%",
        ))

    return QuoteResult(priceable=True, refusal_reasons=[], op_costs=op_costs, programming_fen=programming_fen,
                       batch_fixed_fen=batch_fixed_fen, tiers=tiers, alerts=alerts, **common)


__all__ = [
    "Alert",
    "COST_LABELS",
    "CostLine",
    "OpCost",
    "PlanMismatchError",
    "QuoteParams",
    "QuoteResult",
    "TierResult",
    "compute_quote",
]
