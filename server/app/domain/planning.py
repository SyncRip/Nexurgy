"""Deterministic process planning for any machined part described by a FeatureSummary.

``plan_process(summary, params) -> ProcessPlan``

Same inputs -> same plan, byte for byte. No clock, randomness or IO. AI may
suggest or explain, but every time and quantity here comes from the formulas
below and cites the constants it used (``rates.CONSTANTS``, machine and
material tables).

Route (only the operations that apply are emitted)
--------------------------------------------------
OP10  saw blank                        SAW-01
OP20  milling, rough + finish          VMC-01, setups = setup_directions x VMC setup
OP30-n drilling, one op per hole group VMC-01, inside the OP20 setups
OP40  edge finishing (fillets/chamfers) VMC-01, inside the OP20 setups
OP50  deburr                           BENCH-01
OP55  surface treatment (subcontract)  only when PlanParams.surface_treatment is set
OP60  final inspection                 CMM-01 if any critical tolerance, else BENCH-01

Formulas (k = material machinability, 6061 = 1.0; every rate is multiplied by k)
-------------------------------------------------------------------------------
Blank      dim_i = ceil((bbox_i + 2 x allowance) / increment) x increment;
           V_blank = product(dims); mass = V_blank x density / 1e6 (kg, 4 decimals)
OP10       t = R-SAW-BASE + section / (R-SAW-RATE x k), section = product of the two
           smallest blank dims (the saw cuts the bar across its longest axis)
OP20       V_mill = max(V_blank - V_part - V_holes, 0)
           t = V_mill[cm³] / (R-MILL-MRR x k) + A_finish / (R-MILL-FINISH x k)
               + setup_directions x R-MILL-LOAD
           A_finish = max(A_part - hole wall area, 0) (drilled walls are not finish-milled)
           V_part unknown -> bbox volume x R-EST-FILL (estimated)
           A_part unknown -> bbox envelope area + hole wall area (estimated)
OP30-n     depth L: given depth; through hole without depth -> min (or max) bbox dim;
           drilled (R-DRILL-DMIN <= D <= R-DRILL-DMAX):
             t_hole = R-DRILL-POS + (L + R-DRILL-TIP x D + R-DRILL-APPROACH) / (vf(D) x k)
                      x (R-DRILL-PECK-FACTOR if L/D > R-DRILL-PECK-LD)
           interpolated (D > R-DRILL-DMAX, warning gap):
             t_hole = R-DRILL-POS + pi x D x (L + approach) / pitch / (R-INTERP-VF x k)
           + counterbore R-CBORE-EXTRA / k, countersink R-CSINK-EXTRA / k per hole
           t = n x t_hole + tools x R-TOOL-CHANGE  (tools = 1 + cb + csk)
OP40       t = tools x R-TOOL-CHANGE + (fillets x R-EDGE-FILLET + chamfers x R-EDGE-CHAMFER) / k
OP50       t = R-DEBURR-BASE + holes x R-DEBURR-HOLE + A_part[dm²] x R-DEBURR-AREA
OP60       t = base + holes x R-INSP-HOLE + non-critical notes x R-INSP-NOTE
               + critical notes x R-INSP-CRITICAL; base = CMM or manual
Programming (one-off per part number) = R-PROG-BASE + setup_directions x R-PROG-SETUP
           + (hole groups + edge tools) x R-PROG-TOOLGROUP

Each op's cycle time is rounded half-up to 0.01 min; costing prices the rounded value.

Gaps
----
``summary.unsupported`` items are copied (warning by default: "not covered by this
plan or price"; blocking with ``PlanParams.block_on_unsupported``). Rules add gaps
for unknown material, invalid geometry, holes outside the drill table, holes too
deep for standard drills, tolerances beyond shop capability, blank larger than
machine travel, incompatible surface treatment, etc. Any blocking gap ->
``priceable=False`` (the plan is still produced and shown).
"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .hashing import canonical_holes, dec, fmt_num, norm_float, plan_inputs_hash, round_half_up
from .materials import MATERIAL_TABLE_VERSION, Material, find_material
from .rates import (
    CONSTANTS,
    MACHINE_TABLE_VERSION,
    MACHINES,
    PLAN_RULES_VERSION,
    RULES_VERSION,
    SUBCONTRACT_TABLE_VERSION,
    SUBCONTRACTS,
    SourceRef,
    SurfaceTreatment,
    cite,
    cite_density,
    cite_machinability,
    cite_machine_setup,
    cite_machine_travel,
    cite_subcontract,
    const,
    drill_feed,
)
from .schemas import FeatureSummary, HoleFeature, ToleranceNote

GapSeverity = Literal["blocking", "warning"]
InputValue = float | int | str | bool | None


class Gap(BaseModel):
    """A coverage gap: something the rules cannot plan or price reliably."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: GapSeverity
    origin: Literal["summary", "rules"]
    rule_id: str | None = None
    subject: str | None = None
    message_zh: str
    message_en: str


class Assumption(BaseModel):
    """A documented rule-based assumption the plan relied on (not an error)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    rule_id: str | None = None
    message_zh: str
    message_en: str


class PlanParams(BaseModel):
    """User-adjustable planning parameters. Part of the plan ``inputs_hash``."""

    model_config = ConfigDict(extra="forbid")

    allowance_mm: float = Field(default=float(CONSTANTS["R-BLANK-ALLOW"].value), ge=0, le=50)
    stock_increment_mm: float = Field(default=float(CONSTANTS["R-BLANK-INCR"].value), gt=0, le=100)
    through_hole_depth_rule: Literal["min_bbox", "max_bbox"] = "min_bbox"
    surface_treatment: SurfaceTreatment | None = None
    block_on_unsupported: bool = False


class Blank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dims_mm: tuple[float, float, float]
    allowance_mm: float
    increment_mm: float
    volume_mm3: float
    material_id: str | None
    mass_kg: float | None
    estimated: bool
    formula: str
    sources: list[SourceRef]


class PartMeasures(BaseModel):
    """Geometry figures the plan used, with which of them are estimates."""

    model_config = ConfigDict(extra="forbid")

    bbox_mm: tuple[float, float, float]
    part_volume_mm3: float
    part_volume_estimated: bool
    area_mm2: float
    area_estimated: bool
    hole_groups: int
    hole_count: int
    holes_volume_mm3: float
    hole_wall_area_mm2: float


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: str
    name_key: str
    label_zh: str
    label_en: str
    machine_id: str | None
    subcontract_id: str | None = None
    setup_min: float
    cycle_min: float
    lead_days: int = 0
    formula: str
    sources: list[SourceRef]
    inputs: dict[str, InputValue]
    estimated: bool


class ToleranceSpec(BaseModel):
    """One machine-checkable requirement parsed from a tolerance note."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["plus_minus", "deviation", "it_grade", "roughness", "geometric"]
    fragment: str
    half_band_mm: float | None = None
    it_grade: int | None = None
    ra_um: float | None = None


class ProcessPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules_version: str
    inputs_hash: str
    table_versions: dict[str, str]
    params: PlanParams
    source: Literal["model", "drawing_intake"]
    material_input: str
    material_id: str | None
    material_name_zh: str | None
    material_name_en: str | None
    estimated: bool
    estimated_fields: list[str]
    measures: PartMeasures | None
    blank: Blank | None
    operations: list[Operation]
    programming_min: float
    programming_formula: str
    programming_sources: list[SourceRef]
    total_cycle_min: float
    total_setup_min: float
    gaps: list[Gap]
    assumptions: list[Assumption]
    priceable: bool


class _Ctx:
    """Mutable collector used while building one plan."""

    def __init__(self) -> None:
        self.gaps: list[Gap] = []
        self.assumptions: list[Assumption] = []
        self.estimated_fields: list[str] = []

    def gap(self, code: str, severity: GapSeverity, zh: str, en: str, *, rule_id: str | None = None,
            subject: str | None = None, origin: Literal["summary", "rules"] = "rules") -> None:
        self.gaps.append(Gap(code=code, severity=severity, origin=origin, rule_id=rule_id, subject=subject,
                             message_zh=zh, message_en=en))

    def assume(self, code: str, zh: str, en: str, rule_id: str | None = None) -> None:
        self.assumptions.append(Assumption(code=code, rule_id=rule_id, message_zh=zh, message_en=en))

    def est(self, field: str) -> None:
        if field not in self.estimated_fields:
            self.estimated_fields.append(field)


# ----------------------------------------------------------------------------- helpers


def _r2(x: float) -> float:
    """Minutes are rounded half-up to 0.01 min."""
    return round_half_up(x, 2)


def _r4(x: float) -> float:
    return round_half_up(x, 4)


def _g(x: float) -> str:
    """Compact number for labels: up to 3 decimals, trailing zeros dropped."""
    s = fmt_num(x, 3)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _f(x: float, nd: int = 2) -> str:
    return fmt_num(x, nd)


def _finite_pos(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0


# ----------------------------------------------------------------------------- tolerances

_NUM = r"(\d+(?:\.\d+)?|\.\d+)"
_SIGN = r"[+\-−﹣–]"
_PM_RE = re.compile(r"(?:±|\+/[-−]|\+[-−])\s*" + _NUM)
_DEV_RE = re.compile(r"(" + _SIGN + r")\s*" + _NUM + r"\s*(?:/|\s)\s*(" + _SIGN + r"?)\s*" + _NUM)
_DEV0_RE = re.compile(r"(?<![\d.])0\s*/\s*(" + _SIGN + r")\s*" + _NUM)
_IT_RE = re.compile(r"IT\s?(\d{1,2})(?![\d.])")
# Fit after a nominal size ("Ø20 H7", "20g6"); letters exclude I, L, M (threads), O, Q, R (radii),
# S, T (tempers), X (multiplication) to avoid false matches.
_FIT_AFTER_RE = re.compile(r"(?<=\d)\s?(JS|js|[ABCDEFGHKNPUVYZabcdefghknpuvyz])(\d{1,2})(?![\d.])")
_FIT_ALONE_RE = re.compile(r"(?<![A-Za-z0-9.])(JS|js|[HhGgFfKk])(\d{1,2})(?![\d.])")
_RA_RE = re.compile(r"Ra\s*[=:：]?\s*" + _NUM)
_GDT_WORDS = (
    "位置度", "平面度", "垂直度", "平行度", "同轴度", "同心度", "圆柱度", "圆度", "对称度", "跳动", "轮廓度", "直线度",
    "倾斜度", "flatness", "perpendicularity", "parallelism", "position", "concentricity", "coaxiality",
    "circularity", "roundness", "cylindricity", "symmetry", "runout", "profile", "straightness", "angularity",
    "⌖", "⏥", "⊥", "∥", "◎", "⌭", "⌯", "↗", "⌓", "⌒",
)
_GDT_RE = re.compile(
    "(" + "|".join(re.escape(w) for w in _GDT_WORDS) + r")[^\d.]{0,12}?(?:Ø|⌀|φ|∅)?\s*" + _NUM, re.IGNORECASE
)


def _signed(sign: str, num: str) -> float:
    return -float(num) if sign and sign != "+" else float(num)


def parse_tolerance(text: str) -> list[ToleranceSpec]:
    """Extract machine-checkable requirements from a tolerance note.

    Recognised: ``±a`` (half band a), deviations ``+a/-b`` / ``+a -b`` / ``0/-b``
    (half band |upper-lower|/2), IT grades (``IT7``, ``Ø20 H7``, ``20g6``, ``H7``),
    roughness ``Ra x`` (μm) and geometric tolerances (``position tolerance Ø0.02``,
    ``flatness 0.01``; half band = zone/2). Anything else is not recognised and
    returns an empty list.
    """
    out: list[ToleranceSpec] = []
    for m in _PM_RE.finditer(text):
        out.append(ToleranceSpec(kind="plus_minus", fragment=m.group(0), half_band_mm=float(m.group(1))))
    for m in _DEV_RE.finditer(text):
        band = abs(_signed(m.group(1), m.group(2)) - _signed(m.group(3), m.group(4)))
        if band > 0:
            out.append(ToleranceSpec(kind="deviation", fragment=m.group(0), half_band_mm=norm_float(band / 2)))
    for m in _DEV0_RE.finditer(text):
        band = abs(_signed(m.group(1), m.group(2)))
        if band > 0:
            out.append(ToleranceSpec(kind="deviation", fragment=m.group(0), half_band_mm=norm_float(band / 2)))
    seen_it: set[int] = set()  # position of the grade digits, so overlapping patterns count once
    for rx, grp in ((_IT_RE, 1), (_FIT_AFTER_RE, 2), (_FIT_ALONE_RE, 2)):
        for m in rx.finditer(text):
            grade = int(m.group(grp))
            if 1 <= grade <= 18 and m.start(grp) not in seen_it:
                seen_it.add(m.start(grp))
                out.append(ToleranceSpec(kind="it_grade", fragment=m.group(0).strip(), it_grade=grade))
    for m in _RA_RE.finditer(text):
        out.append(ToleranceSpec(kind="roughness", fragment=m.group(0), ra_um=float(m.group(1))))
    for m in _GDT_RE.finditer(text):
        zone = float(m.group(2))
        if zone > 0:
            out.append(ToleranceSpec(kind="geometric", fragment=m.group(0), half_band_mm=norm_float(zone / 2)))
    return out


def _check_tolerances(notes: list[ToleranceNote], ctx: _Ctx) -> None:
    tol_min = float(const("R-TOL-MIN"))
    it_block = int(const("R-TOL-IT-BLOCK"))
    it_warn = int(const("R-TOL-IT-WARN"))
    ra_min = float(const("R-TOL-RA-MIN"))
    for note in notes:
        specs = parse_tolerance(note.text)
        if not specs and note.critical:
            ctx.gap("TOLERANCE_UNPARSED", "warning",
                    f"关键要求“{note.text}”无法被规则解析，需工艺工程师人工评估",
                    f"Critical requirement '{note.text}' could not be parsed by the rules; needs manual process review",
                    subject=note.text)
        for s in specs:
            if s.half_band_mm is not None and s.half_band_mm < tol_min - 1e-9:
                ctx.gap("TOLERANCE_BEYOND_CAPABILITY", "blocking",
                        f"“{note.text}”：公差 ±{_g(s.half_band_mm)} mm 严于本厂能力 ±{_g(tol_min)} mm（需磨削/外协）",
                        f"'{note.text}': tolerance ±{_g(s.half_band_mm)} mm is tighter than shop capability "
                        f"±{_g(tol_min)} mm (needs grinding/subcontract)",
                        rule_id="R-TOL-MIN", subject=note.text)
            elif s.it_grade is not None and s.it_grade <= it_block:
                ctx.gap("TOLERANCE_BEYOND_CAPABILITY", "blocking",
                        f"“{note.text}”：IT{s.it_grade} 超出本厂铣削/钻削能力",
                        f"'{note.text}': IT{s.it_grade} is beyond shop milling/drilling capability",
                        rule_id="R-TOL-IT-BLOCK", subject=note.text)
            elif s.it_grade is not None and s.it_grade == it_warn:
                ctx.gap("TOLERANCE_NEEDS_FINISHING", "warning",
                        f"“{note.text}”：IT{s.it_grade} 需铰孔/精镗，规则未单列该工序，请确认",
                        f"'{note.text}': IT{s.it_grade} needs reaming/fine boring, which the rules do not add as "
                        f"a separate operation; please confirm",
                        rule_id="R-TOL-IT-WARN", subject=note.text)
            elif s.ra_um is not None and s.ra_um < ra_min - 1e-9:
                ctx.gap("ROUGHNESS_BEYOND_CAPABILITY", "blocking",
                        f"“{note.text}”：Ra {_g(s.ra_um)} μm 低于铣削可达 Ra {_g(ra_min)} μm（需磨削/抛光）",
                        f"'{note.text}': Ra {_g(s.ra_um)} μm is finer than millable Ra {_g(ra_min)} μm "
                        f"(needs grinding/polishing)",
                        rule_id="R-TOL-RA-MIN", subject=note.text)


# ----------------------------------------------------------------------------- holes


def _hole_text(g: HoleFeature, depth: float) -> tuple[str, str]:
    zh = f"{g.count}×Ø{_g(g.diameter)} " + ("通孔" if g.through else f"深 {_g(depth)}")
    en = f"{g.count}× Ø{_g(g.diameter)} " + ("through" if g.through else f"depth {_g(depth)}")
    if g.counterbore:
        zh, en = zh + " 沉孔", en + " counterbore"
    if g.countersink:
        zh, en = zh + " 锥形沉孔", en + " countersink"
    return zh, en


def _rule_depth(bbox: tuple[float, float, float], rule: str) -> float:
    return min(bbox) if rule == "min_bbox" else max(bbox)


def _hole_depth(g: HoleFeature, bbox: tuple[float, float, float], params: PlanParams) -> tuple[float, bool]:
    """Depth used for time/volume and whether it was assumed by rule."""
    if _finite_pos(g.depth):
        return float(g.depth), False  # type: ignore[arg-type]
    return _rule_depth(bbox, params.through_hole_depth_rule), True


def _drill_op(op_id: str, g: HoleFeature, depth: float, k: float, est: bool, ctx: _Ctx) -> Operation:
    d = g.diameter
    n = g.count
    dmin, dmax = float(const("R-DRILL-DMIN")), float(const("R-DRILL-DMAX"))
    pos = float(const("R-DRILL-POS"))
    approach = float(const("R-DRILL-APPROACH"))
    tool_change = float(const("R-TOOL-CHANGE"))
    zh, en = _hole_text(g, depth)
    sources = cite("R-DRILL-POS", "R-DRILL-APPROACH", "R-TOOL-CHANGE")
    inputs: dict[str, InputValue] = {"diameter_mm": d, "depth_mm": _r4(depth), "count": n, "machinability": k,
                                     "through": g.through, "counterbore": g.counterbore, "countersink": g.countersink}
    ld = depth / d

    if d > dmax:
        vf = float(const("R-INTERP-VF"))
        pitch = float(const("R-INTERP-PITCH"))
        path = math.pi * d * (depth + approach) / pitch
        t_cut = path / (vf * k)
        method = (f"helical interpolation: pi × {_g(d)} × ({_g(depth)} + {_g(approach)}) / {_g(pitch)} = {_f(path)} mm "
                  f"÷ ({_g(vf)} mm/min × k {_f(k)}) = {_f(t_cut, 4)} min")
        sources += cite("R-DRILL-DMAX", "R-INTERP-VF", "R-INTERP-PITCH")
        inputs["method"] = "interpolate"
        ctx.gap("HOLE_INTERPOLATED", "warning",
                f"{op_id} Ø{_g(d)} 超出钻头表上限 Ø{_g(dmax)}，按螺旋插补铣计时，请确认镗孔/公差要求",
                f"{op_id} Ø{_g(d)} exceeds the drill table maximum Ø{_g(dmax)}; timed as helical interpolation "
                f"milling - confirm boring/tolerance needs",
                rule_id="R-DRILL-DMAX", subject=op_id)
    else:
        vf_base, vf_ref = drill_feed(d)
        tip = float(const("R-DRILL-TIP"))
        l_eff = depth + tip * d + approach
        t_cut = l_eff / (vf_base * k)
        peck = ld > float(const("R-DRILL-PECK-LD"))
        method = (f"drill: ({_g(depth)} + {_g(tip)}×{_g(d)} + {_g(approach)}) = {_f(l_eff)} mm "
                  f"÷ ({vf_base} mm/min × k {_f(k)}) = {_f(t_cut, 4)} min")
        if peck:
            factor = float(const("R-DRILL-PECK-FACTOR"))
            t_cut *= factor
            method += f" × peck {_g(factor)} (L/D {_f(ld)} > {_g(const('R-DRILL-PECK-LD'))}) = {_f(t_cut, 4)} min"
            sources += cite("R-DRILL-PECK-LD", "R-DRILL-PECK-FACTOR")
        sources += cite("R-DRILL-TIP") + [vf_ref]
        inputs["method"] = "drill"
        inputs["feed_mm_min"] = vf_base
        if d < dmin:
            ctx.gap("HOLE_TOO_SMALL", "blocking",
                    f"{op_id} Ø{_g(d)} 小于钻头表下限 Ø{_g(dmin)}（微孔加工不支持）",
                    f"{op_id} Ø{_g(d)} is below the drill table minimum Ø{_g(dmin)} (micro drilling unsupported)",
                    rule_id="R-DRILL-DMIN", subject=op_id)
            sources += cite("R-DRILL-DMIN")
        if ld > float(const("R-DRILL-MAX-LD")):
            ctx.gap("HOLE_TOO_DEEP", "blocking",
                    f"{op_id} 深径比 {_f(ld)} 超过标准钻头上限 {_g(const('R-DRILL-MAX-LD'))}（需枪钻，不支持）",
                    f"{op_id} L/D {_f(ld)} exceeds the standard-drill limit {_g(const('R-DRILL-MAX-LD'))} "
                    f"(gun drilling unsupported)",
                    rule_id="R-DRILL-MAX-LD", subject=op_id)
            sources += cite("R-DRILL-MAX-LD")

    extra = 0.0
    extra_txt = ""
    tools = 1
    if g.counterbore:
        cb = float(const("R-CBORE-EXTRA"))
        extra += cb / k
        extra_txt += f" + cbore {_g(cb)}/k"
        tools += 1
        sources += cite("R-CBORE-EXTRA")
    if g.countersink:
        cs = float(const("R-CSINK-EXTRA"))
        extra += cs / k
        extra_txt += f" + csink {_g(cs)}/k"
        tools += 1
        sources += cite("R-CSINK-EXTRA")
    t_hole = pos + t_cut + extra
    raw = n * t_hole + tools * tool_change
    cycle = _r2(raw)
    formula = (f"{method}; per hole t = {_g(pos)} + {_f(t_cut, 4)}{extra_txt} = {_f(t_hole, 4)} min; "
               f"cycle = {n} × {_f(t_hole, 4)} + {tools} tool(s) × {_g(tool_change)} = {_f(raw, 4)} → {_f(cycle)} min; "
               f"setup 0 (drilled in the OP20 setups)")
    inputs.update({"l_over_d": _r4(ld), "per_hole_min": _r4(t_hole), "tools": tools})
    return Operation(
        op_id=op_id, name_key="drilling", label_zh=f"钻孔 {zh}", label_en=f"Drill {en}", machine_id="VMC-01",
        setup_min=0.0, cycle_min=cycle, formula=formula, sources=sources, inputs=inputs, estimated=est,
    )


# ----------------------------------------------------------------------------- main entry


def plan_process(summary: FeatureSummary, params: PlanParams | None = None) -> ProcessPlan:
    """Build the deterministic process plan for ``summary`` under ``params``.

    Never raises for bad manufacturing data: problems become gaps (blocking ones
    set ``priceable=False``). See the module docstring for the formulas.
    """
    params = params or PlanParams()
    ctx = _Ctx()
    est_all = summary.estimated or summary.source == "drawing_intake"

    # --- coverage gaps reported by the summary builder
    for item in sorted(summary.unsupported):
        ctx.gap("UNSUPPORTED_FEATURE", "blocking" if params.block_on_unsupported else "warning",
                f"不支持的制造要求：{item}（未计入本工艺与报价，需人工补充）",
                f"Unsupported manufacturing requirement: {item} (not covered by this plan or price; add manually)",
                subject=item, origin="summary")

    # --- material
    mat: Material | None = find_material(summary.material)
    if mat is None:
        ctx.gap("MATERIAL_UNKNOWN", "blocking",
                f"材料“{summary.material}”不在材料表中：无法计算重量与材料费，工时按 k=1.0 占位",
                f"Material '{summary.material}' is not in the material table: no mass or material cost; "
                f"times use a placeholder k = 1.0",
                subject=summary.material)
        k = 1.0
        k_sources: list[SourceRef] = []
    else:
        k = mat.machinability
        k_sources = [cite_machinability(mat)]
        if mat.id != summary.material:
            ctx.assume("MATERIAL_ALIAS", f"材料“{summary.material}”按 {mat.id} 处理",
                       f"Material '{summary.material}' interpreted as {mat.id}")

    tolerances = sorted(summary.tolerances, key=lambda t: (t.text, t.critical))
    _check_tolerances(tolerances, ctx)

    if summary.setup_directions > int(const("R-SETUP-DIR-MAX")):
        ctx.gap("SETUP_DIRECTIONS_EXCEED_3AXIS", "warning",
                f"装夹方向 {summary.setup_directions} > {const('R-SETUP-DIR-MAX')}：存在斜向特征，需专用夹具或多轴设备，请确认",
                f"{summary.setup_directions} setup directions > {const('R-SETUP-DIR-MAX')}: angled features need "
                f"special fixtures or multi-axis machines; please confirm",
                rule_id="R-SETUP-DIR-MAX")

    bbox_raw = summary.bbox_mm
    if not all(_finite_pos(v) for v in bbox_raw):
        ctx.gap("INVALID_BBOX", "blocking",
                f"包围盒 {list(bbox_raw)} 无效（每个尺寸须为有限正数），无法规划",
                f"Bounding box {list(bbox_raw)} is invalid (each size must be finite and > 0); cannot plan",
                subject="bbox_mm")
        return _assemble(summary, params, ctx, mat, None, None, [], 0.0, "", [], est_all)

    bbox = (norm_float(bbox_raw[0]), norm_float(bbox_raw[1]), norm_float(bbox_raw[2]))
    x, y, z = bbox
    v_bbox = x * y * z
    a_env = 2 * (x * y + y * z + z * x)
    if est_all:
        for f in ("bbox_mm", "holes", "part_volume_mm3", "area_mm2"):
            ctx.est(f)

    # --- holes
    groups = canonical_holes(summary.holes)
    depths: list[tuple[float, bool]] = [_hole_depth(g, bbox, params) for g in groups]
    v_holes = sum(g.count * math.pi * (g.diameter / 2) ** 2 * d for g, (d, _) in zip(groups, depths, strict=True))
    a_walls = sum(g.count * math.pi * g.diameter * d for g, (d, _) in zip(groups, depths, strict=True))
    n_holes = sum(g.count for g in groups)
    if any(assumed for _, assumed in depths):
        rule_d = _rule_depth(bbox, params.through_hole_depth_rule)
        which_zh = "最小" if params.through_hole_depth_rule == "min_bbox" else "最大"
        which_en = "smallest" if params.through_hole_depth_rule == "min_bbox" else "largest"
        ctx.assume("HOLE_DEPTH_RULE",
                   f"未给深度的孔按包围盒{which_zh}尺寸 {_g(rule_d)} mm 计深度",
                   f"Holes without a depth are taken as deep as the {which_en} bbox dimension ({_g(rule_d)} mm)")
    for i, g in enumerate(groups):
        if not g.through and not _finite_pos(g.depth):
            ctx.gap("BLIND_HOLE_NO_DEPTH", "warning",
                    f"OP30-{i + 1} 盲孔 Ø{_g(g.diameter)} 未给深度，按通孔规则计",
                    f"OP30-{i + 1} blind hole Ø{_g(g.diameter)} has no depth; timed with the through-hole rule",
                    subject=f"OP30-{i + 1}")

    # --- part volume and area
    vol_est = est_all
    if summary.volume_mm3 is None:
        part_vol = v_bbox * float(const("R-EST-FILL"))
        vol_est = True
        ctx.est("part_volume_mm3")
        ctx.assume("VOLUME_ESTIMATED",
                   f"无零件体积：按包围盒体积 × {_g(const('R-EST-FILL'))} 估算为 {_f(part_vol)} mm³",
                   f"No part volume: estimated as bbox volume × {_g(const('R-EST-FILL'))} = {_f(part_vol)} mm³",
                   rule_id="R-EST-FILL")
    elif not _finite_pos(summary.volume_mm3):
        ctx.gap("INVALID_MEASUREMENT", "blocking", f"零件体积 {summary.volume_mm3} 无效",
                f"Part volume {summary.volume_mm3} is invalid", subject="volume_mm3")
        part_vol = v_bbox * float(const("R-EST-FILL"))
        vol_est = True
        ctx.est("part_volume_mm3")
    else:
        part_vol = norm_float(summary.volume_mm3)
        if part_vol > v_bbox * (1 + 1e-6):
            ctx.gap("VOLUME_EXCEEDS_BBOX", "blocking",
                    f"零件体积 {_f(part_vol)} mm³ 大于包围盒体积 {_f(v_bbox)} mm³，几何数据不一致",
                    f"Part volume {_f(part_vol)} mm³ exceeds bbox volume {_f(v_bbox)} mm³; geometry data inconsistent",
                    subject="volume_mm3")

    area_est = est_all
    if summary.area_mm2 is None or not _finite_pos(summary.area_mm2):
        if summary.area_mm2 is not None:
            ctx.gap("INVALID_MEASUREMENT", "blocking", f"表面积 {summary.area_mm2} 无效",
                    f"Surface area {summary.area_mm2} is invalid", subject="area_mm2")
        area = a_env + a_walls
        area_est = True
        ctx.est("area_mm2")
        ctx.assume("AREA_ESTIMATED",
                   f"无表面积：按包围盒外表面 {_f(a_env)} + 孔壁 {_f(a_walls)} = {_f(area)} mm² 估算",
                   f"No surface area: estimated as bbox envelope {_f(a_env)} + hole walls {_f(a_walls)} = {_f(area)} mm²")
    else:
        area = norm_float(summary.area_mm2)

    measures = PartMeasures(
        bbox_mm=bbox, part_volume_mm3=_r4(part_vol), part_volume_estimated=vol_est, area_mm2=_r4(area),
        area_estimated=area_est, hole_groups=len(groups), hole_count=n_holes, holes_volume_mm3=_r4(v_holes),
        hole_wall_area_mm2=_r4(a_walls),
    )

    # --- blank
    allow, incr = params.allowance_mm, params.stock_increment_mm
    dims = tuple(
        float(math.ceil(round((v + 2 * allow) / incr, 9)) * incr) for v in bbox
    )  # round(…, 9) keeps exact multiples (e.g. 100/5) from ceiling up on float noise
    dims = (norm_float(dims[0]), norm_float(dims[1]), norm_float(dims[2]))
    v_blank = dims[0] * dims[1] * dims[2]
    blank_sources = cite("R-BLANK-ALLOW", "R-BLANK-INCR")
    mass = None
    mass_txt = "mass n/a (material unknown)"
    if mat is not None:
        mass = round_half_up(dec(norm_float(v_blank)) * dec(mat.density_g_cm3) / 1_000_000, 4)
        mass_txt = f"mass = {_f(v_blank)} mm³ × {_g(mat.density_g_cm3)} g/cm³ / 1e6 = {_f(mass, 4)} kg"
        blank_sources.append(cite_density(mat))
    blank_formula = (
        "dims = ceil((bbox + 2 × {a}) / {i}) × {i}: ".format(a=_g(allow), i=_g(incr))
        + ", ".join(
            f"ceil(({_g(b)} + {_g(2 * allow)}) / {_g(incr)}) × {_g(incr)} = {_g(d)}"
            for b, d in zip(bbox, dims, strict=True)
        )
        + f"; V = {' × '.join(_g(d) for d in dims)} = {_f(v_blank)} mm³; {mass_txt}"
    )
    blank = Blank(dims_mm=dims, allowance_mm=allow, increment_mm=incr, volume_mm3=_r4(v_blank),
                  material_id=mat.id if mat else None, mass_kg=mass, estimated=est_all,
                  formula=blank_formula, sources=blank_sources)
    if est_all:
        ctx.est("blank.dims_mm")
        ctx.est("blank.mass_kg")

    vmc = MACHINES["VMC-01"]
    if vmc.travel_mm is not None:
        sb, st = sorted(dims, reverse=True), sorted(vmc.travel_mm, reverse=True)
        if any(b > t for b, t in zip(sb, st, strict=True)):
            ctx.gap("BLANK_EXCEEDS_TRAVEL", "blocking",
                    f"毛坯 {'×'.join(_g(d) for d in dims)} 超出 {vmc.id} 行程 {'×'.join(_g(t) for t in vmc.travel_mm)}",
                    f"Blank {'×'.join(_g(d) for d in dims)} exceeds {vmc.id} travel "
                    f"{'×'.join(_g(t) for t in vmc.travel_mm)}",
                    subject="blank")

    ops: list[Operation] = []
    dirs = summary.setup_directions

    # --- OP10 saw
    saw = MACHINES["SAW-01"]
    s_sorted = sorted(dims)
    section = s_sorted[0] * s_sorted[1]
    saw_base, saw_rate = float(const("R-SAW-BASE")), float(const("R-SAW-RATE"))
    raw = saw_base + section / (saw_rate * k)
    cyc = _r2(raw)
    ops.append(Operation(
        op_id="OP10", name_key="saw_blank",
        label_zh=f"下料：锯切毛坯 {'×'.join(_g(d) for d in dims)}", label_en=f"Saw blank {'×'.join(_g(d) for d in dims)}",
        machine_id=saw.id, setup_min=saw.setup_min, cycle_min=cyc,
        formula=(f"t = {_g(saw_base)} + section {_g(s_sorted[0])} × {_g(s_sorted[1])} = {_f(section)} mm² "
                 f"÷ ({_g(saw_rate)} mm²/min × k {_f(k)}) = {_f(raw, 4)} → {_f(cyc)} min; setup {_g(saw.setup_min)} min"),
        sources=cite("R-SAW-BASE", "R-SAW-RATE") + k_sources + [cite_machine_setup(saw.id)],
        inputs={"section_mm2": _r4(section), "machinability": k}, estimated=est_all,
    ))
    if est_all:
        ctx.est("OP10.cycle_min")

    # --- OP20 milling
    mrr, fin, load = float(const("R-MILL-MRR")), float(const("R-MILL-FINISH")), float(const("R-MILL-LOAD"))
    v_mill_raw = v_blank - part_vol - v_holes
    v_mill = max(v_mill_raw, 0.0)
    a_fin = max(area - a_walls, 0.0)
    t_rough = (v_mill / 1000) / (mrr * k)
    t_fin = a_fin / (fin * k)
    t_load = dirs * load
    raw = t_rough + t_fin + t_load
    cyc = _r2(raw)
    setup = dirs * vmc.setup_min
    op20_est = est_all or vol_est or area_est
    clamp_txt = "" if v_mill_raw >= 0 else " (negative, clamped to 0)"
    ops.append(Operation(
        op_id="OP20", name_key="milling",
        label_zh=f"铣削：{dirs} 个装夹方向，粗加工 + 精加工", label_en=f"Milling: {dirs} setup direction(s), rough + finish",
        machine_id=vmc.id, setup_min=setup, cycle_min=cyc,
        formula=(f"rough: V = {_f(v_blank)} − {_f(part_vol)} − holes {_f(v_holes)} = {_f(v_mill_raw)} mm³{clamp_txt} "
                 f"= {_f(v_mill / 1000, 3)} cm³ ÷ ({_g(mrr)} cm³/min × k {_f(k)}) = {_f(t_rough, 4)} min; "
                 f"finish: A = {_f(area)} − hole walls {_f(a_walls)} = {_f(a_fin)} mm² ÷ ({_g(fin)} mm²/min × k {_f(k)}) "
                 f"= {_f(t_fin, 4)} min; load: {dirs} × {_g(load)} = {_f(t_load, 4)} min; "
                 f"cycle = {_f(raw, 4)} → {_f(cyc)} min; setup = {dirs} × {_g(vmc.setup_min)} = {_g(setup)} min"),
        sources=cite("R-MILL-MRR", "R-MILL-FINISH", "R-MILL-LOAD") + k_sources
        + [cite_machine_setup(vmc.id), cite_machine_travel(vmc.id)]
        + (cite("R-EST-FILL") if vol_est and summary.volume_mm3 is None else []),
        inputs={"blank_volume_mm3": _r4(v_blank), "part_volume_mm3": _r4(part_vol), "holes_volume_mm3": _r4(v_holes),
                "removal_cm3": _r4(v_mill / 1000), "area_mm2": _r4(area), "finish_area_mm2": _r4(a_fin),
                "setup_directions": dirs, "machinability": k, "rough_min": _r4(t_rough), "finish_min": _r4(t_fin),
                "load_min": _r4(t_load), "part_volume_estimated": vol_est, "area_estimated": area_est},
        estimated=op20_est,
    ))
    if op20_est:
        ctx.est("OP20.cycle_min")

    # --- OP30-n drilling
    for i, (g, (depth, _assumed)) in enumerate(zip(groups, depths, strict=True)):
        op = _drill_op(f"OP30-{i + 1}", g, depth, k, est_all, ctx)
        op.sources = op.sources + k_sources
        ops.append(op)
        if est_all:
            ctx.est(f"{op.op_id}.cycle_min")

    # --- OP40 edges
    edge_tools = int(summary.fillets > 0) + int(summary.chamfers > 0)
    if edge_tools:
        tc, ef, ec = float(const("R-TOOL-CHANGE")), float(const("R-EDGE-FILLET")), float(const("R-EDGE-CHAMFER"))
        raw = edge_tools * tc + (summary.fillets * ef + summary.chamfers * ec) / k
        cyc = _r2(raw)
        ops.append(Operation(
            op_id="OP40", name_key="edge_finishing",
            label_zh=f"倒角/圆角：圆角 {summary.fillets} 处，倒角 {summary.chamfers} 处",
            label_en=f"Edge finishing: {summary.fillets} fillet(s), {summary.chamfers} chamfer(s)",
            machine_id=vmc.id, setup_min=0.0, cycle_min=cyc,
            formula=(f"t = {edge_tools} tool(s) × {_g(tc)} + ({summary.fillets} × {_g(ef)} + {summary.chamfers} × {_g(ec)}) "
                     f"÷ k {_f(k)} = {_f(raw, 4)} → {_f(cyc)} min; setup 0 (in the OP20 setups)"),
            sources=cite("R-TOOL-CHANGE", "R-EDGE-FILLET", "R-EDGE-CHAMFER") + k_sources,
            inputs={"fillets": summary.fillets, "chamfers": summary.chamfers, "tools": edge_tools, "machinability": k},
            estimated=est_all,
        ))
        if est_all:
            ctx.est("OP40.cycle_min")

    # --- OP50 deburr
    bench = MACHINES["BENCH-01"]
    db, dh, da = float(const("R-DEBURR-BASE")), float(const("R-DEBURR-HOLE")), float(const("R-DEBURR-AREA"))
    area_dm2 = area / 10000
    raw = db + dh * n_holes + da * area_dm2
    cyc = _r2(raw)
    ops.append(Operation(
        op_id="OP50", name_key="deburr", label_zh="去毛刺", label_en="Deburr",
        machine_id=bench.id, setup_min=bench.setup_min, cycle_min=cyc,
        formula=(f"t = {_g(db)} + {n_holes} holes × {_g(dh)} + {_f(area_dm2, 4)} dm² × {_g(da)} "
                 f"= {_f(raw, 4)} → {_f(cyc)} min"),
        sources=cite("R-DEBURR-BASE", "R-DEBURR-HOLE", "R-DEBURR-AREA"),
        inputs={"holes": n_holes, "area_dm2": _r4(area_dm2), "area_estimated": area_est},
        estimated=est_all or area_est,
    ))
    if est_all or area_est:
        ctx.est("OP50.cycle_min")

    # --- OP55 subcontract surface treatment
    if params.surface_treatment is not None:
        sub = SUBCONTRACTS[params.surface_treatment]
        if mat is not None and mat.family not in sub.families:
            ctx.gap("SURFACE_TREATMENT_INCOMPATIBLE", "blocking",
                    f"{sub.name_zh}不适用于 {mat.name_zh}（适用：{'、'.join(sub.families)}）",
                    f"{sub.name_en} is not applicable to {mat.name_en} (applies to: {', '.join(sub.families)})",
                    subject=sub.id)
        ops.append(Operation(
            op_id="OP55", name_key="surface_treatment", label_zh=f"{sub.name_zh}（外协）",
            label_en=f"{sub.name_en} (subcontract)", machine_id=None, subcontract_id=sub.id, setup_min=0.0,
            cycle_min=0.0, lead_days=sub.lead_days,
            formula=(f"priced per piece in the quote: max({_f(area_dm2, 4)} dm² × {sub.fen_per_dm2} fen/dm², "
                     f"{sub.lot_min_fen} fen lot minimum ÷ qty); lead {sub.lead_days} days"),
            sources=cite_subcontract(sub.id), inputs={"area_dm2": _r4(area_dm2), "area_estimated": area_est},
            estimated=est_all or area_est,
        ))

    # --- OP60 inspection
    n_crit = sum(1 for t in tolerances if t.critical)
    n_other = len(tolerances) - n_crit
    use_cmm = n_crit > 0
    insp = MACHINES["CMM-01" if use_cmm else "BENCH-01"]
    base_id = "R-INSP-CMM-BASE" if use_cmm else "R-INSP-MANUAL-BASE"
    ib, ih, inote, icrit = (float(const(base_id)), float(const("R-INSP-HOLE")), float(const("R-INSP-NOTE")),
                            float(const("R-INSP-CRITICAL")))
    raw = ib + ih * n_holes + inote * n_other + icrit * n_crit
    cyc = _r2(raw)
    ops.append(Operation(
        op_id="OP60", name_key="inspection",
        label_zh="终检（三坐标）" if use_cmm else "终检（手工量具）",
        label_en="Final inspection (CMM)" if use_cmm else "Final inspection (manual gauges)",
        machine_id=insp.id, setup_min=insp.setup_min, cycle_min=cyc,
        formula=(f"t = {_g(ib)} + {n_holes} holes × {_g(ih)} + {n_other} notes × {_g(inote)} + {n_crit} critical × {_g(icrit)} "
                 f"= {_f(raw, 4)} → {_f(cyc)} min; setup {_g(insp.setup_min)} min"),
        sources=cite(base_id, "R-INSP-HOLE", "R-INSP-NOTE", "R-INSP-CRITICAL") + [cite_machine_setup(insp.id)],
        inputs={"holes": n_holes, "notes": n_other, "critical": n_crit},
        estimated=est_all,
    ))
    if est_all:
        ctx.est("OP60.cycle_min")

    # --- programming (one-off)
    pb, ps, pg = float(const("R-PROG-BASE")), float(const("R-PROG-SETUP")), float(const("R-PROG-TOOLGROUP"))
    tool_groups = len(groups) + edge_tools
    prog = _r2(pb + ps * dirs + pg * tool_groups)
    prog_formula = (f"programming = {_g(pb)} + {dirs} setups × {_g(ps)} + {tool_groups} tool groups × {_g(pg)} "
                    f"= {_f(prog)} min (once per part number)")
    prog_sources = cite("R-PROG-BASE", "R-PROG-SETUP", "R-PROG-TOOLGROUP")

    return _assemble(summary, params, ctx, mat, measures, blank, ops, prog, prog_formula, prog_sources, est_all)


def _assemble(summary: FeatureSummary, params: PlanParams, ctx: _Ctx, mat: Material | None,
              measures: PartMeasures | None, blank: Blank | None, ops: list[Operation], prog: float,
              prog_formula: str, prog_sources: list[SourceRef], est_all: bool) -> ProcessPlan:
    priceable = not any(g.severity == "blocking" for g in ctx.gaps)
    estimated = est_all or bool(ctx.estimated_fields)
    return ProcessPlan(
        rules_version=PLAN_RULES_VERSION,
        inputs_hash=plan_inputs_hash(summary, params),
        table_versions={
            "rules": RULES_VERSION,
            "machines": MACHINE_TABLE_VERSION,
            "materials": MATERIAL_TABLE_VERSION,
            "subcontracts": SUBCONTRACT_TABLE_VERSION,
        },
        params=params,
        source=summary.source,
        material_input=summary.material,
        material_id=mat.id if mat else None,
        material_name_zh=mat.name_zh if mat else None,
        material_name_en=mat.name_en if mat else None,
        estimated=estimated,
        estimated_fields=list(ctx.estimated_fields),
        measures=measures,
        blank=blank,
        operations=ops,
        programming_min=prog,
        programming_formula=prog_formula,
        programming_sources=prog_sources,
        total_cycle_min=_r2(sum(o.cycle_min for o in ops)),
        total_setup_min=_r2(sum(o.setup_min for o in ops)),
        gaps=list(ctx.gaps),
        assumptions=list(ctx.assumptions),
        priceable=priceable,
    )


__all__ = [
    "Assumption",
    "Blank",
    "Gap",
    "GapSeverity",
    "Operation",
    "PartMeasures",
    "PlanParams",
    "ProcessPlan",
    "ToleranceSpec",
    "parse_tolerance",
    "plan_process",
]
