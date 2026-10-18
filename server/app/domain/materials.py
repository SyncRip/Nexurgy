"""Material table: physical properties (used by planning) and prices (used by costing).

All values are SYNTHETIC demo data and must be labelled as such in the UI.

Two independent versions are kept so that a price update does not invalidate
process plans (plans only depend on density and machinability):

* ``MATERIAL_TABLE_VERSION``  - densities, machinability factors, families.
  Part of the planning rules version (``rates.PLAN_RULES_VERSION``).
* ``MATERIAL_PRICE_VERSION``  - fen/kg prices with quote date and validity.
  Part of the quoting rules version (``rates.QUOTE_RULES_VERSION``).

Any edit to a number below must bump the corresponding version string, which
makes every stored plan/quote whose hash includes it stale.

Pure data + lookup; no IO, no clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

MATERIAL_TABLE_VERSION = "materials-2026.10-1"
MATERIAL_PROPERTY_SOURCE = (
    "Nominal handbook values (GB/T 3190, GB/T 699, GB/T 20878, supplier datasheets); "
    "machinability = shop removal-rate factor relative to 6061-T651 (synthetic demo data)"
)

MATERIAL_PRICE_VERSION = "matprice-2026.09.26"
MATERIAL_PRICE_QUOTED_AT = date(2026, 9, 26)
MATERIAL_PRICE_VALID_UNTIL = date(2026, 10, 31)
MATERIAL_PRICE_SOURCE = "Supplier quotation SQ-0926 (synthetic demo data)"

MaterialFamily = Literal["aluminium", "carbon_steel", "stainless_steel", "plastic"]


class UnknownMaterialError(LookupError):
    """Raised by :func:`get_material` when a material id/alias is not in the table.

    Planning never lets this escape: it records a blocking ``MATERIAL_UNKNOWN``
    gap instead, so the plan is still produced but cannot be priced.
    """

    def __init__(self, material: str) -> None:
        self.material = material
        known = ", ".join(sorted(MATERIALS))
        super().__init__(f"Unknown material {material!r}; known material ids: {known}")


@dataclass(frozen=True)
class Material:
    """One stock material.

    ``machinability`` scales every cutting rate (saw, roughing MRR, finishing
    area rate, drill feed, edge tools): effective_rate = baseline_rate x k.
    6061-T651 is the baseline (k = 1.0).
    """

    id: str
    name_zh: str
    name_en: str
    family: MaterialFamily
    density_g_cm3: float
    price_fen_per_kg: int
    machinability: float
    aliases: tuple[str, ...] = ()


MATERIALS: dict[str, Material] = {
    m.id: m
    for m in (
        Material(
            id="AL6061-T651",
            name_zh="6061-T651 铝合金",
            name_en="Aluminium 6061-T651",
            family="aluminium",
            density_g_cm3=2.70,
            price_fen_per_kg=3200,
            machinability=1.0,
            aliases=("6061", "6061-T6", "6061-T651", "AL6061", "AL6061-T6", "A6061"),
        ),
        Material(
            id="AL7075-T651",
            name_zh="7075-T651 铝合金",
            name_en="Aluminium 7075-T651",
            family="aluminium",
            density_g_cm3=2.81,
            price_fen_per_kg=5800,
            machinability=0.8,
            aliases=("7075", "7075-T6", "7075-T651", "AL7075", "AL7075-T6", "A7075"),
        ),
        Material(
            id="S45C",
            name_zh="S45C / 45# 碳钢",
            name_en="Carbon steel S45C / 1045",
            family="carbon_steel",
            density_g_cm3=7.85,
            price_fen_per_kg=780,
            machinability=0.3,
            aliases=("1045", "AISI1045", "C45", "45#", "45钢", "45 steel"),
        ),
        Material(
            id="SUS304",
            name_zh="SUS304 不锈钢",
            name_en="Stainless steel SUS304 / AISI 304",
            family="stainless_steel",
            density_g_cm3=7.93,
            price_fen_per_kg=2200,
            machinability=0.2,
            aliases=("304", "AISI304", "SS304", "06Cr19Ni10", "1.4301"),
        ),
        Material(
            id="POM",
            name_zh="POM 聚甲醛（赛钢）",
            name_en="POM / Delrin (acetal)",
            family="plastic",
            density_g_cm3=1.41,
            price_fen_per_kg=3500,
            machinability=1.2,
            aliases=("POM-C", "POM-H", "DELRIN", "ACETAL", "聚甲醛", "赛钢"),
        ),
    )
}


def _norm(text: str) -> str:
    """Normalise a material text for alias matching: drop spaces, '-', '_', '/', upper-case."""
    return re.sub(r"[\s\-_/]", "", text).upper()


_INDEX: dict[str, str] = {}
for _m in MATERIALS.values():
    for _key in (_m.id, *_m.aliases):
        _INDEX.setdefault(_norm(_key), _m.id)


def find_material(material: str) -> Material | None:
    """Return the material for an id or alias (case/space/hyphen-insensitive), else ``None``."""
    if material in MATERIALS:
        return MATERIALS[material]
    mid = _INDEX.get(_norm(material))
    return MATERIALS[mid] if mid else None


def get_material(material: str) -> Material:
    """Like :func:`find_material` but raises :class:`UnknownMaterialError` when not found."""
    m = find_material(material)
    if m is None:
        raise UnknownMaterialError(material)
    return m


__all__ = [
    "MATERIALS",
    "MATERIAL_PRICE_QUOTED_AT",
    "MATERIAL_PRICE_SOURCE",
    "MATERIAL_PRICE_VALID_UNTIL",
    "MATERIAL_PRICE_VERSION",
    "MATERIAL_PROPERTY_SOURCE",
    "MATERIAL_TABLE_VERSION",
    "Material",
    "MaterialFamily",
    "UnknownMaterialError",
    "find_material",
    "get_material",
]
