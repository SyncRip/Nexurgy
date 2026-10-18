"""Manufacturing feature summary: the only geometry input to planning and costing.

Built by services from a kernel BuildResult (source="model") or from a
customer-drawing intake (source="drawing_intake", estimated=True).
See .trellis/tasks/archive/2026-10/10-02-v04-platform/design.md §3.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class HoleFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diameter: float = Field(gt=0)
    depth: float | None = Field(default=None, gt=0)  # None = through
    through: bool
    count: int = Field(ge=1)
    counterbore: bool = False
    countersink: bool = False


class ToleranceNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    critical: bool = False


class FeatureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["model", "drawing_intake"]
    estimated: bool = False
    material: str
    bbox_mm: tuple[Annotated[float, Field(ge=0)], Annotated[float, Field(ge=0)], Annotated[float, Field(ge=0)]]
    volume_mm3: float | None = Field(default=None, ge=0)
    area_mm2: float | None = Field(default=None, ge=0)
    holes: list[HoleFeature] = Field(default_factory=list)
    fillets: int = 0
    chamfers: int = 0
    setup_directions: int = Field(default=1, ge=1)
    unsupported: list[str] = Field(default_factory=list)
    tolerances: list[ToleranceNote] = Field(default_factory=list)
