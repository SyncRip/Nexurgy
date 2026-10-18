"""Shop context: machines, hourly rates, standard times, subcontracts, rule versions.

All values are SYNTHETIC demo data (label them so in the UI). Every number that
a formula uses is a :class:`Constant` (or a machine/subcontract/material field)
with an id, a unit, a human label and a source, so plans and quotes can cite it
(:class:`SourceRef`) next to the arithmetic.

Versions
--------
* ``RULES_VERSION``             planning formulas + standard-time constants (``R-*`` in RULES_VERSION).
* ``MACHINE_TABLE_VERSION``     machine list, setup defaults, travels.
* ``RATE_TABLE_VERSION``        money-side constants: hourly rates, programming rate,
                                scrap rate, lead-time constants (``R-*`` in RATE_TABLE_VERSION).
* ``SUBCONTRACT_TABLE_VERSION`` surface-treatment subcontracts.
* ``materials.MATERIAL_TABLE_VERSION`` / ``MATERIAL_PRICE_VERSION``.

``PLAN_RULES_VERSION`` (stored in ``process_plans.rules_version``) joins everything
a plan depends on; ``QUOTE_RULES_VERSION`` adds rates and material prices. Bump the
relevant version whenever a value here changes - that is what turns stored
plans/quotes stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .materials import (
    MATERIAL_PRICE_SOURCE,
    MATERIAL_PRICE_VERSION,
    MATERIAL_PROPERTY_SOURCE,
    MATERIAL_TABLE_VERSION,
    Material,
    MaterialFamily,
)

RULES_VERSION = "rules-2026.10-1"
MACHINE_TABLE_VERSION = "machines-2026.10-1"
RATE_TABLE_VERSION = "rates-2026.10-1"
RATE_TABLE_EFFECTIVE = date(2026, 10, 1)
SUBCONTRACT_TABLE_VERSION = "subcontracts-2026.10-1"

PLAN_RULES_VERSION = "+".join([RULES_VERSION, MACHINE_TABLE_VERSION, MATERIAL_TABLE_VERSION, SUBCONTRACT_TABLE_VERSION])
QUOTE_RULES_VERSION = "+".join([PLAN_RULES_VERSION, RATE_TABLE_VERSION, MATERIAL_PRICE_VERSION])

SHOP_STANDARD = "PartRoute demo shop standard-time sheet ST-2026.10 (synthetic)"
RATE_SHEET = "PartRoute demo rate sheet RS-2026.10, effective 2026-10-01 (synthetic)"
MACHINE_SHEET = "PartRoute demo machine register MR-2026.10 (synthetic)"
SUBCONTRACT_SHEET = "PartRoute demo subcontract price list SC-2026.10 (synthetic)"


class SourceRef(BaseModel):
    """A citation of one constant used by a formula: what it is, its value and where it came from."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    value: str
    unit: str
    source: str


# ----------------------------------------------------------------------------- constants


@dataclass(frozen=True)
class Constant:
    id: str
    value: float | int
    unit: str
    label_zh: str
    label_en: str
    source: str
    version: str


def _std(id: str, value: float | int, unit: str, zh: str, en: str) -> Constant:
    return Constant(id, value, unit, zh, en, SHOP_STANDARD, RULES_VERSION)


def _rate(id: str, value: float | int, unit: str, zh: str, en: str) -> Constant:
    return Constant(id, value, unit, zh, en, RATE_SHEET, RATE_TABLE_VERSION)


CONSTANTS: dict[str, Constant] = {
    c.id: c
    for c in (
        # --- blank
        _std("R-BLANK-ALLOW", 3.0, "mm", "毛坯单边加工余量（默认）", "Machining allowance per side (default)"),
        _std("R-BLANK-INCR", 5.0, "mm", "毛坯尺寸向上取整步长（默认）", "Stock size round-up increment (default)"),
        _std("R-EST-FILL", 0.65, "ratio", "无体积时零件体积 = 包围盒体积 × 系数", "Part volume estimate = bbox volume x ratio"),
        # --- OP10 saw
        _std("R-SAW-BASE", 2.0, "min/pc", "锯切上下料与对刀", "Saw handling per piece"),
        _std("R-SAW-RATE", 3000, "mm²/min", "带锯切削面积率（6061 基准）", "Band-saw cutting area rate (6061 baseline)"),
        # --- OP20 milling
        _std("R-MILL-MRR", 50, "cm³/min", "立加粗加工去除率（6061 基准）", "VMC roughing removal rate (6061 baseline)"),
        _std("R-MILL-FINISH", 6000, "mm²/min", "精加工面积率（6061 基准）", "Finishing area rate (6061 baseline)"),
        _std("R-MILL-LOAD", 1.0, "min/setup/pc", "每装夹方向每件装卸", "Load/clamp/unload per setup direction per piece"),
        # --- OP30 drilling
        _std("R-DRILL-POS", 0.2, "min/hole", "每孔定位、快移与退刀", "Positioning, rapid and retract per hole"),
        _std("R-DRILL-APPROACH", 2.0, "mm", "钻孔切入行程", "Drill approach length"),
        _std("R-DRILL-TIP", 0.3, "×D", "钻尖长度系数", "Drill point length factor"),
        _std("R-DRILL-PECK-LD", 3.0, "L/D", "超过此深径比需啄钻", "Peck drilling above this L/D"),
        _std("R-DRILL-PECK-FACTOR", 1.5, "×", "啄钻时间系数", "Peck drilling time factor"),
        _std("R-DRILL-MAX-LD", 10.0, "L/D", "标准钻头最大深径比（超出需枪钻，不支持）",
             "Max L/D for standard drills (beyond = gun drilling, unsupported)"),
        _std("R-DRILL-DMIN", 1.0, "mm", "钻头表最小直径", "Smallest drill in drill table"),
        _std("R-DRILL-DMAX", 32.0, "mm", "钻头表最大直径（以上改螺旋插补铣）",
             "Largest drill in drill table (above: helical interpolation)"),
        _std("R-TOOL-CHANGE", 0.3, "min/tool/pc", "每把刀每件换刀时间", "Tool change per tool per piece"),
        _std("R-INTERP-VF", 800, "mm/min", "螺旋插补铣进给（6061 基准）", "Helical interpolation feed (6061 baseline)"),
        _std("R-INTERP-PITCH", 1.0, "mm/rev", "螺旋插补每圈下降量", "Helical interpolation pitch"),
        _std("R-CBORE-EXTRA", 0.4, "min/hole", "沉孔附加时间（6061 基准）", "Counterbore extra per hole (6061 baseline)"),
        _std("R-CSINK-EXTRA", 0.2, "min/hole", "锥形沉孔附加时间（6061 基准）", "Countersink extra per hole (6061 baseline)"),
        # --- OP40 edges
        _std("R-EDGE-FILLET", 0.4, "min/edge", "圆角每条边（6061 基准）", "Fillet per edge (6061 baseline)"),
        _std("R-EDGE-CHAMFER", 0.25, "min/edge", "倒角每条边（6061 基准）", "Chamfer per edge (6061 baseline)"),
        # --- OP50 deburr
        _std("R-DEBURR-BASE", 2.0, "min/pc", "去毛刺基础时间", "Deburr base per piece"),
        _std("R-DEBURR-HOLE", 0.15, "min/hole", "每孔去毛刺", "Deburr per hole"),
        _std("R-DEBURR-AREA", 0.3, "min/dm²", "按表面积去毛刺", "Deburr per dm² of surface"),
        # --- OP60 inspection
        _std("R-INSP-MANUAL-BASE", 2.0, "min/pc", "手工检验基础时间", "Manual inspection base per piece"),
        _std("R-INSP-CMM-BASE", 5.0, "min/pc", "三坐标检验基础时间", "CMM inspection base per piece"),
        _std("R-INSP-HOLE", 0.1, "min/hole", "每孔检验", "Inspection per hole"),
        _std("R-INSP-NOTE", 0.5, "min/note", "每条非关键公差要求", "Per non-critical tolerance note"),
        _std("R-INSP-CRITICAL", 3.0, "min/note", "每条关键公差要求（三坐标）", "Per critical tolerance note (CMM)"),
        # --- programming (one-off per part number)
        _std("R-PROG-BASE", 30, "min", "编程基础时间（每零件号一次）", "Programming base (once per part number)"),
        _std("R-PROG-SETUP", 15, "min/setup", "每装夹方向编程", "Programming per setup direction"),
        _std("R-PROG-TOOLGROUP", 5, "min/group", "每孔组或边缘刀具组编程", "Programming per hole group or edge tool"),
        # --- shop capability
        _std("R-TOL-MIN", 0.01, "mm", "本厂可保证的最小公差（± 半带宽）", "Tightest tolerance the shop can hold (± half band)"),
        _std("R-TOL-IT-BLOCK", 5, "IT", "IT 等级 ≤ 此值超出能力", "IT grade at or below this exceeds capability"),
        _std("R-TOL-IT-WARN", 6, "IT", "IT 等级 = 此值需确认精加工方式", "IT grade needing a finishing-method check"),
        _std("R-TOL-RA-MIN", 0.8, "μm", "铣削可达最小粗糙度 Ra", "Smallest Ra achievable by milling"),
        _std("R-SETUP-DIR-MAX", 6, "dirs", "三轴立加正交装夹方向上限", "Max orthogonal setup directions on a 3-axis VMC"),
        # --- money-side (rate sheet)
        _rate("R-PROG-RATE", 15000, "fen/h", "编程费率", "Programming rate"),
        _rate("R-SCRAP-RATE", 0.03, "ratio", "报废/损耗率（材料+加工）", "Scrap/loss rate (material + machining)"),
        _rate("R-DAY-CAPACITY", 816, "min/day", "瓶颈设备日产能（2 班 × 8 h × 85%）",
              "Bottleneck machine capacity (2 shifts x 8 h x 85%)"),
        _rate("R-LEAD-MATERIAL", 2, "days", "备料周期", "Material procurement lead time"),
        _rate("R-LEAD-PACK", 1, "days", "终检包装发运", "Final inspection, packing and dispatch"),
        _rate("R-MARGIN-FLOOR", 10, "%", "本厂利润率下限（提示）", "Shop margin floor (alert)"),
    )
}

# Drill feed table, 6061 baseline: (d_min inclusive, d_max exclusive, vf mm/min).
# The last row includes its upper bound (R-DRILL-DMAX).
DRILL_FEED_TABLE: tuple[tuple[float, float, int], ...] = (
    (1.0, 3.0, 120),
    (3.0, 6.0, 200),
    (6.0, 12.0, 250),
    (12.0, 20.0, 200),
    (20.0, 32.0, 150),
)
DRILL_FEED_TABLE_ID = "R-DRILL-VF"


def const(id: str) -> float | int:
    """Value of constant ``id`` (KeyError for unknown ids - a programming error)."""
    return CONSTANTS[id].value


def cite(*ids: str) -> list[SourceRef]:
    """Citations for the given constant ids, in order."""
    out = []
    for i in ids:
        c = CONSTANTS[i]
        out.append(SourceRef(id=c.id, version=c.version, value=str(c.value), unit=c.unit, source=c.source))
    return out


def drill_feed(diameter: float) -> tuple[int, SourceRef]:
    """Baseline drill feed (mm/min) for a diameter within the drill table, plus its citation.

    Diameters below the table use the first row, above it the last row; planning
    raises gaps for those cases before relying on the value.
    """
    row = DRILL_FEED_TABLE[0] if diameter < DRILL_FEED_TABLE[0][0] else DRILL_FEED_TABLE[-1]
    for lo, hi, vf in DRILL_FEED_TABLE:
        if lo <= diameter < hi:
            row = (lo, hi, vf)
            break
    lo, hi, vf = row
    ref = SourceRef(
        id=f"{DRILL_FEED_TABLE_ID}[{lo:g}-{hi:g}]", version=RULES_VERSION, value=str(vf), unit="mm/min", source=SHOP_STANDARD
    )
    return vf, ref


# ----------------------------------------------------------------------------- machines

MachineKind = Literal["saw", "vmc", "drill_tap", "bench", "cmm"]


@dataclass(frozen=True)
class Machine:
    """A machine or work centre. ``rate_fen_per_h`` belongs to the rate sheet
    (RATE_TABLE_VERSION); the other fields to the machine register."""

    id: str
    kind: MachineKind
    name_zh: str
    name_en: str
    rate_fen_per_h: int
    setup_min: float
    travel_mm: tuple[float, float, float] | None = None
    note: str = ""


MACHINES: dict[str, Machine] = {
    m.id: m
    for m in (
        Machine("SAW-01", "saw", "带锯床", "Band saw", 6000, 5.0, note="Setup = stop + blade check per batch"),
        Machine("VMC-01", "vmc", "3 轴立式加工中心", "3-axis vertical machining centre", 9500, 40.0, (850.0, 500.0, 500.0),
                note="Setup minutes are per setup direction (fixture, zero, first-piece check)"),
        Machine("DTC-01", "drill_tap", "钻攻中心", "Drill/tap centre", 8000, 20.0, (500.0, 400.0, 300.0),
                note="Reserved for tapping; FeatureSummary has no thread features yet, so v0.4 rules drill on VMC-01"),
        Machine("BENCH-01", "bench", "钳工台（去毛刺/手检）", "Bench (deburr / manual inspection)", 5500, 0.0),
        Machine("CMM-01", "cmm", "三坐标测量机", "Coordinate measuring machine", 12000, 15.0, (700.0, 600.0, 500.0),
                note="Setup = alignment + program check per batch"),
    )
}


def get_machine(machine_id: str) -> Machine:
    return MACHINES[machine_id]


def cite_machine_rate(machine_id: str) -> SourceRef:
    m = MACHINES[machine_id]
    return SourceRef(id=f"{m.id}.rate", version=RATE_TABLE_VERSION, value=str(m.rate_fen_per_h), unit="fen/h", source=RATE_SHEET)


def cite_machine_setup(machine_id: str) -> SourceRef:
    m = MACHINES[machine_id]
    return SourceRef(id=f"{m.id}.setup", version=MACHINE_TABLE_VERSION, value=f"{m.setup_min:g}", unit="min/setup",
                     source=MACHINE_SHEET)


def cite_machine_travel(machine_id: str) -> SourceRef:
    m = MACHINES[machine_id]
    travel = "x".join(f"{t:g}" for t in m.travel_mm) if m.travel_mm else "-"
    return SourceRef(id=f"{m.id}.travel", version=MACHINE_TABLE_VERSION, value=travel, unit="mm", source=MACHINE_SHEET)


# ----------------------------------------------------------------------------- materials citations


def cite_density(m: Material) -> SourceRef:
    return SourceRef(id=f"{m.id}.density", version=MATERIAL_TABLE_VERSION, value=f"{m.density_g_cm3:g}", unit="g/cm³",
                     source=MATERIAL_PROPERTY_SOURCE)


def cite_machinability(m: Material) -> SourceRef:
    return SourceRef(id=f"{m.id}.machinability", version=MATERIAL_TABLE_VERSION, value=f"{m.machinability:g}", unit="k",
                     source=MATERIAL_PROPERTY_SOURCE)


def cite_price(m: Material) -> SourceRef:
    return SourceRef(id=f"{m.id}.price", version=MATERIAL_PRICE_VERSION, value=str(m.price_fen_per_kg), unit="fen/kg",
                     source=MATERIAL_PRICE_SOURCE)


# ----------------------------------------------------------------------------- subcontracts

SurfaceTreatment = Literal["anodize", "black_oxide", "passivate"]


@dataclass(frozen=True)
class Subcontract:
    """Surface treatment bought from a supplier, priced per dm² with a lot minimum.

    Per-piece price for a batch of q pieces = max(area_dm² x fen_per_dm2, lot_min_fen / q).
    """

    id: SurfaceTreatment
    name_zh: str
    name_en: str
    fen_per_dm2: int
    lot_min_fen: int
    lead_days: int
    families: tuple[MaterialFamily, ...]
    supplier: str


SUBCONTRACTS: dict[str, Subcontract] = {
    s.id: s
    for s in (
        Subcontract("anodize", "阳极氧化", "Anodizing", 90, 30000, 3, ("aluminium",), "Finisher S-03 (synthetic)"),
        Subcontract("black_oxide", "发黑", "Black oxide", 60, 20000, 3, ("carbon_steel",), "Finisher S-05 (synthetic)"),
        Subcontract("passivate", "钝化", "Passivation", 80, 25000, 2, ("stainless_steel",), "Finisher S-07 (synthetic)"),
    )
}


def cite_subcontract(sub_id: str) -> list[SourceRef]:
    s = SUBCONTRACTS[sub_id]
    src = f"{SUBCONTRACT_SHEET}; {s.supplier}"
    v = SUBCONTRACT_TABLE_VERSION
    return [
        SourceRef(id=f"SUB-{s.id}.fen_per_dm2", version=v, value=str(s.fen_per_dm2), unit="fen/dm²", source=src),
        SourceRef(id=f"SUB-{s.id}.lot_min", version=v, value=str(s.lot_min_fen), unit="fen/lot", source=src),
        SourceRef(id=f"SUB-{s.id}.lead", version=v, value=str(s.lead_days), unit="days", source=src),
    ]


__all__ = [
    "CONSTANTS",
    "Constant",
    "DRILL_FEED_TABLE",
    "MACHINES",
    "MACHINE_TABLE_VERSION",
    "Machine",
    "PLAN_RULES_VERSION",
    "QUOTE_RULES_VERSION",
    "RATE_TABLE_EFFECTIVE",
    "RATE_TABLE_VERSION",
    "RULES_VERSION",
    "SUBCONTRACTS",
    "SUBCONTRACT_TABLE_VERSION",
    "SourceRef",
    "Subcontract",
    "SurfaceTreatment",
    "cite",
    "cite_density",
    "cite_machinability",
    "cite_machine_rate",
    "cite_machine_setup",
    "cite_machine_travel",
    "cite_price",
    "cite_subcontract",
    "const",
    "drill_feed",
    "get_machine",
]
