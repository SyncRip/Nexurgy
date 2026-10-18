"""Drawing intake: customer drawing (image / PDF) → manufacturing data → FeatureSummary.

``run_intake_agent`` asks a vision model for material, envelope, holes,
tolerances, surface finish and quantity, each with a confidence, and computes
``missing`` / ``low_confidence`` lists for the human review step. Without a
vision-capable model it returns ``status="unsupported_capability"`` so the
caller can fall back to manual entry. Provider failures raise ProviderError
with ``err.partial`` set, like the modeling agent.

``to_feature_summary`` turns a reviewed intake (plus manual overrides) into a
``FeatureSummary(source="drawing_intake", estimated=True)``. Volume is an
estimate: bbox volume × a fill factor by shape (``FILL_FACTORS``); holes are
not subtracted. Surface-finish callouts are carried as tolerance notes
(FeatureSummary has no separate field); threads and other requirements become
``unsupported`` coverage gaps.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.materials import find_material
from app.domain.schemas import FeatureSummary, HoleFeature, ToleranceNote

from . import prompts
from .providers.base import ImagePart, Message, ModelProvider, ProviderError, clip
from .providers.schema_dialects import drop_nulls
from .trace import AgentStep, AiCallRecord, Tracer

LOW_CONFIDENCE = 0.5
Shape = Literal["prismatic", "turned", "plate", "other"]
FILL_FACTORS: dict[str, float] = {"plate": 0.85, "prismatic": 0.6, "turned": 0.6, "other": 0.5}
DEFAULT_FILL_FACTOR = 0.6


def _confidence(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if 1.0 < f <= 100.0:  # tolerate percentages
        f /= 100.0
    return min(max(f, 0.0), 1.0)


class _Conf(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confidence: float = Field(default=0.0, description="0..1")

    @field_validator("confidence", mode="before")
    @classmethod
    def _c(cls, v: Any) -> float:
        return _confidence(v)


class TextValue(_Conf):
    value: str | None = None
    source: str | None = None


class SizeValue(_Conf):
    x: float | None = Field(default=None, description="mm, longest extent in the main view")
    y: float | None = Field(default=None, description="mm")
    z: float | None = Field(default=None, description="mm, thickness / height")
    source: str | None = None


class HoleCallout(_Conf):
    diameter: float = Field(gt=0, description="mm; nominal diameter for threaded holes")
    count: int = Field(default=1, ge=1)
    through: bool = True
    depth: float | None = Field(default=None, description="mm, for blind holes")
    counterbore: bool = False
    countersink: bool = False
    thread: str | None = Field(default=None, description='e.g. "M6x1"')
    source: str | None = None


class NoteValue(_Conf):
    text: str
    critical: bool = False


class QuantityValue(_Conf):
    value: int | None = Field(default=None, ge=1)
    source: str | None = None


class IntakeResult(BaseModel):
    """Structured output requested from the vision model (lenient on omission)."""

    model_config = ConfigDict(extra="ignore")
    units_on_drawing: Literal["mm", "inch", "unknown"] = "unknown"
    part_name: TextValue = Field(default_factory=TextValue)
    drawing_number: TextValue = Field(default_factory=TextValue)
    material: TextValue = Field(default_factory=TextValue)
    overall_size_mm: SizeValue = Field(default_factory=SizeValue)
    shape: Shape | None = None
    holes: list[HoleCallout] = Field(default_factory=list)
    tolerances: list[NoteValue] = Field(default_factory=list)
    surface_finish: list[NoteValue] = Field(default_factory=list)
    quantity: QuantityValue = Field(default_factory=QuantityValue)
    other_requirements: list[str] = Field(default_factory=list)
    setup_directions: int | None = Field(default=None, ge=1)
    missing: list[str] = Field(default_factory=list)


class IntakeOutcome(BaseModel):
    status: Literal["extracted", "unsupported_capability", "failed"]
    result: IntakeResult | None = None
    missing: list[str] = Field(default_factory=list)
    low_confidence: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    repairs: int = 0
    simulated: bool = False
    steps: list[AgentStep] = Field(default_factory=list)
    calls: list[AiCallRecord] = Field(default_factory=list)


def intake_json_schema() -> dict[str, Any]:
    """IntakeResult schema with every top-level field required, so models fill
    them all (nullable ones with null). Validation stays lenient."""
    schema = IntakeResult.model_json_schema()
    schema["required"] = list(schema.get("properties", {}))
    return schema


def run_intake_agent(
    provider: ModelProvider,
    files: list[ImagePart],
    hints: str | None = None,
    *,
    max_repairs: int = 1,
    temperature: float | None = None,
    timeout_s: float = 180,
) -> IntakeOutcome:
    tracer = Tracer()

    def outcome(status: Literal["extracted", "unsupported_capability", "failed"], **kw: Any) -> IntakeOutcome:
        return IntakeOutcome(status=status, simulated=provider.simulated, steps=list(tracer.steps),
                             calls=list(tracer.calls), **kw)

    schema = intake_json_schema()
    with tracer.step("prompt") as st:
        system = prompts.INTAKE_SYSTEM_PROMPT
        user = prompts.intake_user_message(files, hints)
        st.detail = {"prompt_version": prompts.INTAKE_PROMPT_VERSION, "files": [f.summary() for f in files],
                     "hints_chars": len(hints or ""), "provider": provider.provider_name,
                     "model_id": provider.model_id, "simulated": provider.simulated}
        if not files:
            st.ok = False
    if not files:
        return outcome("failed", errors=["no drawing file was provided"])
    if not provider.capabilities.vision:
        msg = (f"model {provider.model_id} is not configured with vision; choose a vision-capable model "
               "or enter the drawing data manually")
        return outcome("unsupported_capability", errors=[msg],
                       error={"code": "unsupported_capability", "message": msg, "retryable": False})

    messages = [Message(role="user", content=user)]
    repairs = 0
    try:
        while True:
            try:
                res = tracer.model_call(
                    provider, purpose="drawing_intake" if repairs == 0 else "drawing_intake_repair",
                    system=system, system_version=prompts.INTAKE_PROMPT_VERSION, messages=messages,
                    json_schema=schema, schema_name="partroute_drawing_intake", images=files,
                    temperature=temperature, timeout_s=timeout_s,
                )
            except ProviderError as e:
                if e.code == "unsupported_capability":
                    return outcome("unsupported_capability", errors=[e.message], error=e.to_dict(), repairs=repairs)
                raise
            with tracer.step("validation") as st:
                result, problems = _validate(res.data, res.parse_error)
                st.ok = not problems
                st.detail = {"problems": problems[:10], "problem_count": len(problems)}
            if problems:
                if repairs < max_repairs:
                    repairs += 1
                    with tracer.step("repair") as st:
                        st.detail = {"reason": "validation", "problems": problems[:10]}
                        messages.append(Message(role="assistant", content=clip(res.text, 30_000)))
                        messages.append(Message(role="user", content=prompts.intake_repair_message(problems)))
                    continue
                return outcome("failed", errors=problems, repairs=repairs)
            assert result is not None
            missing, low = review_lists(result)
            return outcome("extracted", result=result, missing=missing, low_confidence=low, repairs=repairs)
    except ProviderError as e:
        e.partial = outcome("failed", errors=[e.message], error=e.to_dict(), repairs=repairs)
        raise


def _validate(data: dict[str, Any] | None, parse_error: str | None) -> tuple[IntakeResult | None, list[str]]:
    if data is None:
        return None, [f"the reply was not a JSON object ({parse_error or 'no detail'})"]
    try:
        return IntakeResult.model_validate(drop_nulls(data)), []
    except ValidationError as e:
        return None, [f"{'.'.join(str(x) for x in err['loc']) or 'reply'}: {err['msg']}" for err in e.errors()[:20]]


def review_lists(result: IntakeResult) -> tuple[list[str], list[str]]:
    """(missing, low_confidence) field names for the human review form."""
    missing: list[str] = []
    if not result.material.value:
        missing.append("material")
    for axis in "xyz":
        if getattr(result.overall_size_mm, axis) is None:
            missing.append(f"overall_size_mm.{axis}")
    if result.quantity.value is None:
        missing.append("quantity")
    for m in result.missing:
        m = m.strip()
        if m and m not in missing:
            missing.append(clip(m, 80))
    low: list[str] = []
    for name, field, present in (
        ("material", result.material, bool(result.material.value)),
        ("part_name", result.part_name, bool(result.part_name.value)),
        ("overall_size_mm", result.overall_size_mm, any(getattr(result.overall_size_mm, a) is not None for a in "xyz")),
        ("quantity", result.quantity, result.quantity.value is not None),
    ):
        if present and field.confidence < LOW_CONFIDENCE:
            low.append(name)
    low += [f"holes[{i}]" for i, h in enumerate(result.holes) if h.confidence < LOW_CONFIDENCE]
    low += [f"tolerances[{i}]" for i, t in enumerate(result.tolerances) if t.confidence < LOW_CONFIDENCE]
    return missing, low


class IntakeOverrides(BaseModel):
    """Values confirmed or entered by a person; each replaces the extracted one."""

    model_config = ConfigDict(extra="forbid")
    material: str | None = None
    bbox_mm: tuple[float, float, float] | None = None
    shape: Shape | None = None
    volume_mm3: float | None = Field(default=None, gt=0)
    area_mm2: float | None = Field(default=None, gt=0)
    holes: list[HoleFeature] | None = None
    fillets: int | None = Field(default=None, ge=0)
    chamfers: int | None = Field(default=None, ge=0)
    setup_directions: int | None = Field(default=None, ge=1)
    tolerances: list[ToleranceNote] | None = None
    unsupported: list[str] | None = None


class IntakeIncomplete(ValueError):
    """Required values are missing; ``missing`` names them for the review form."""

    def __init__(self, missing: list[str], message: str | None = None) -> None:
        super().__init__(message or f"drawing intake is missing: {', '.join(missing)}")
        self.missing = missing


def catalogue_material_id(text: str) -> str | None:
    """Material id for drawing text via the domain catalogue (aliases, case-insensitive)."""
    m = find_material(text)
    return m.id if m else None


def estimate_volume(bbox_mm: tuple[float, float, float], shape: str | None) -> float:
    """bbox volume × fill factor (plate 0.85, prismatic / turned 0.6, other 0.5)."""
    x, y, z = bbox_mm
    return x * y * z * FILL_FACTORS.get(shape or "", DEFAULT_FILL_FACTOR)


def to_feature_summary(
    intake: IntakeResult | IntakeOutcome,
    overrides: IntakeOverrides | Mapping[str, Any] | None = None,
    *,
    material_resolver: Callable[[str], str | None] | None = None,
) -> FeatureSummary:
    """Build the estimated FeatureSummary used for planning / quoting from a drawing.

    ``material_resolver`` maps drawing text (e.g. "AL6061-T6") to a material id;
    the default is the domain catalogue (``domain.materials.find_material``).
    Raises IntakeIncomplete when the material is unknown / not in the catalogue
    or the full envelope is missing; a person then supplies ``overrides``.
    """
    result = intake.result if isinstance(intake, IntakeOutcome) else intake
    if result is None:
        raise IntakeIncomplete(["material", "overall_size_mm"], "no intake result; enter the drawing data manually")
    ov = overrides if isinstance(overrides, IntakeOverrides) else IntakeOverrides.model_validate(dict(overrides or {}))

    missing: list[str] = []
    notes: list[str] = []
    material = ov.material
    if material is None and result.material.value:
        raw = result.material.value.strip()
        material = (material_resolver or catalogue_material_id)(raw)
        if material is None:
            missing.append("material")
            notes.append(f"material '{clip(raw, 60)}' is not in the catalogue; choose one")
    elif material is None:
        missing.append("material")

    size = result.overall_size_mm
    bbox = ov.bbox_mm or ((size.x, size.y, size.z) if None not in (size.x, size.y, size.z) else None)
    if bbox is None or any(v is None or v <= 0 for v in bbox):
        missing.append("overall_size_mm")
    if missing:
        raise IntakeIncomplete(missing, "drawing intake is missing: " + ", ".join(missing) + ("; " + "; ".join(notes) if notes else ""))
    assert bbox is not None and material is not None
    bbox_f = (float(bbox[0]), float(bbox[1]), float(bbox[2]))

    unsupported: list[str] = []
    if ov.holes is not None:
        holes = list(ov.holes)
    else:
        holes = []
        for h in result.holes:
            through = h.through  # an explicit THRU wins over a stray depth
            holes.append(HoleFeature(diameter=h.diameter, depth=None if through else h.depth, through=through,
                                     count=h.count, counterbore=h.counterbore, countersink=h.countersink))
            if not through and h.depth is None:
                unsupported.append(f"blind hole Ø{h.diameter:g} ×{h.count}: depth not stated")
            if h.thread:
                unsupported.append(f"thread {h.thread} ×{h.count} (estimated as plain Ø{h.diameter:g} hole)")
    unsupported += [clip(r, 200) for r in result.other_requirements if r.strip()]
    if ov.unsupported is not None:
        unsupported = list(ov.unsupported)

    if ov.tolerances is not None:
        tolerances = list(ov.tolerances)
    else:
        tolerances = [ToleranceNote(text=clip(t.text, 300), critical=t.critical) for t in result.tolerances if t.text.strip()]
        tolerances += [ToleranceNote(text=clip(f"surface finish: {s.text}", 300), critical=s.critical)
                       for s in result.surface_finish if s.text.strip()]

    shape = ov.shape or result.shape
    return FeatureSummary(
        source="drawing_intake",
        estimated=True,
        material=material,
        bbox_mm=bbox_f,
        volume_mm3=ov.volume_mm3 or round(estimate_volume(bbox_f, shape), 1),
        area_mm2=ov.area_mm2,
        holes=holes,
        fillets=ov.fillets or 0,
        chamfers=ov.chamfers or 0,
        setup_directions=max(1, ov.setup_directions or result.setup_directions or 1),
        unsupported=unsupported,
        tolerances=tolerances,
    )
