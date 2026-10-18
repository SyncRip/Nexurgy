"""Bounded plan data; authority and source snapshots are supplied only by the server."""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator, with_config
from typing_extensions import NotRequired, TypedDict

from ..cad.requirements import (BandReq, CoilReq, CutReq, EnvelopeReq, HoleReq, NoteReq, PocketReq, PrismReq,
                               Requirements, RevolveReq, SweepReq, TextReq)
from .spec_agent import Endpoint, Placement, SpecDefinition, SpecDeliverable, SpecFact, SpecInstance, SpecInterface, SpecRelation

Scalar = StrictStr | StrictInt | StrictFloat | StrictBool
FieldPath = Annotated[list[StrictStr | StrictInt], Field(min_length=1, max_length=12)]
DesignAction = Literal['customize', 'delegate_remaining', 'accept_recommended']


class _M(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class DesignRef(_M):
    plan_id: uuid.UUID
    version_id: uuid.UUID
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    schema_version: Literal[1] = 1


class DesignDecision(_M):
    id: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    value: Scalar
    unit: str | None = Field(default=None, max_length=30)
    reason: str = Field(default='', max_length=1000)
    origin: Literal['ai_design', 'user_input', 'user_choice'] = 'ai_design'


class DesignConstraint(_M):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    source_quote: str = Field(min_length=1, max_length=1000)
    field_path: FieldPath
    value: Scalar
    unit: str | None = Field(default=None, max_length=30)


class DesignOption(_M):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    value: Scalar
    reason: str = Field(default='', max_length=1000)


class RecommendedOption(_M):
    kind: Literal['option'] = 'option'
    option_id: str = Field(min_length=1, max_length=80)


class RecommendedValue(_M):
    kind: Literal['value'] = 'value'
    value: Scalar


class DesignQuestion(_M):
    id: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=1000)
    field_path: FieldPath
    input_kind: Literal['string', 'number', 'boolean']
    unit: str | None = Field(default=None, max_length=30)
    minimum: float | None = None
    maximum: float | None = None
    options: list[DesignOption] = Field(default_factory=list, max_length=12)
    recommended: Annotated[RecommendedOption | RecommendedValue, Field(discriminator='kind')] | None = None
    allow_custom: bool = True
    required: bool = False

    @model_validator(mode='after')
    def valid_options(self):
        ids = [o.id for o in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate option id')
        if isinstance(self.recommended, RecommendedOption) and self.recommended.option_id not in ids:
            raise ValueError('recommended option must exist')
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('minimum exceeds maximum')
        return self


class DesignPlanContent(_M):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default='', max_length=4000)
    root: str = Field(min_length=1, max_length=120)
    definitions: list[SpecDefinition] = Field(min_length=1, max_length=32)
    instances: list[SpecInstance] = Field(max_length=128)
    relations: list[SpecRelation] = Field(default_factory=list, max_length=256)
    facts: list[SpecFact] = Field(default_factory=list, max_length=512)
    interfaces: list[SpecInterface] = Field(default_factory=list, max_length=128)
    deliverables: list[SpecDeliverable] = Field(default_factory=list, max_length=7)
    decisions: list[DesignDecision] = Field(default_factory=list, max_length=64)
    hard_constraints: list[DesignConstraint] = Field(default_factory=list, max_length=128)
    part_requirements: dict[str, Requirements]
    locked_placements: list[str] = Field(default_factory=list, max_length=128)
    questions: list[DesignQuestion] = Field(default_factory=list, max_length=12)


# The planner exposes execution grammar directly. Historical/internal plan decoding
# continues to use the canonical source-compatible types above.
DefinitionId = Annotated[str, Field(pattern=r'^D-\d{2,}$', max_length=120)]
InstanceId = Annotated[str, Field(pattern=r'^I-\d{2,}$', max_length=120)]
FactId = Annotated[str, Field(pattern=r'^F-\d{3,}$', max_length=60)]
RequirementId = Annotated[str, Field(pattern=r'^R[1-9]\d*$')]


class PlannerDefinition(SpecDefinition):
    key: DefinitionId
    fact_ids: list[FactId] = Field(default_factory=list)
    template: None = None


class PlannerInstance(SpecInstance):
    key: InstanceId
    definition: DefinitionId
    parent: DefinitionId
    placement: Placement
    fact_ids: list[FactId] = Field(default_factory=list)


class PlannerEndpoint(Endpoint):
    instance: str = Field(pattern=r'^(I-\d{2,}|@D-\d{2,})$', max_length=120)
    datum: str = Field(default='origin', pattern=r'^(origin|X|Y|Z|XY|YZ|ZX|if\.[a-z0-9_]{1,60})$',
                       description='origin, uppercase frame X/Y/Z/XY/YZ/ZX, or if.<declared interface key>, e.g. if.shaft_axis')


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class EmptyRelationParams(TypedDict):
    pass


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class CoaxialParams(TypedDict):
    aligned: NotRequired[StrictBool]


SignedPlaneDistance = Annotated[StrictFloat, Field(description=(
    'Signed mm = dot(b.origin - a.origin, a.direction) after both datum poses in the parent assembly frame; '
    'signed, not absolute. flip changes normal alignment only.'))]


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class PlanarParams(TypedDict):
    flip: NotRequired[Annotated[StrictBool, Field(description=(
        'Reverse normal alignment: two planes default to opposed; any frame endpoint defaults to same-way.'))]]
    distance: NotRequired[SignedPlaneDistance]
    offset: NotRequired[SignedPlaneDistance]


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class OffsetParams(TypedDict):
    distance: Annotated[StrictFloat, Field(description=(
        'mm in the parent assembly frame. Both plane/frame: signed dot(b.origin-a.origin,a.direction). '
        'Only one plane: signed distance from that plane along its normal to the other origin. '
        'Two axes: nonnegative perpendicular separation from axis a. Otherwise: nonnegative Euclidean origin distance.'))]
    flip: NotRequired[StrictBool]


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class AngleParams(TypedDict):
    angle: StrictFloat


@with_config(ConfigDict(extra='forbid', allow_inf_nan=False))
class PatternParams(TypedDict):
    shift: NotRequired[StrictInt]


class _PlannerRelation(SpecRelation):
    key: str = Field(pattern=r'^M-\d{2,}$', max_length=120)
    a: PlannerEndpoint
    b: PlannerEndpoint
    fact_ids: list[FactId] = Field(default_factory=list)


class PlannerSimpleRelation(_PlannerRelation):
    kind: Literal['fixed', 'parallel', 'perpendicular']
    params: EmptyRelationParams = Field(default_factory=dict)


class PlannerCoaxialRelation(_PlannerRelation):
    kind: Literal['coaxial']
    params: CoaxialParams = Field(default_factory=dict)


class PlannerPlanarRelation(_PlannerRelation):
    kind: Literal['planar']
    params: PlanarParams = Field(default_factory=dict)

    @model_validator(mode='after')
    def one_distance(self):
        if 'distance' in self.params and 'offset' in self.params:
            raise ValueError('planar params may use distance or offset, never both')
        return self


class PlannerOffsetRelation(_PlannerRelation):
    kind: Literal['offset']
    params: OffsetParams


class PlannerAngleRelation(_PlannerRelation):
    kind: Literal['angle']
    params: AngleParams


class PlannerPatternRelation(_PlannerRelation):
    kind: Literal['pattern_match']
    params: PatternParams = Field(default_factory=dict)


PlannerRelation = Annotated[PlannerSimpleRelation | PlannerCoaxialRelation | PlannerPlanarRelation |
    PlannerOffsetRelation | PlannerAngleRelation | PlannerPatternRelation, Field(discriminator='kind')]


class PlannerFact(SpecFact):
    id: FactId
    subject: str = Field(pattern=r'^(D-\d{2,}|I-\d{2,}|M-\d{2,})\.[a-zA-Z_][a-zA-Z0-9_.\[\]-]*$',
                         max_length=200, description='Canonical key.field, e.g. D-01.length; never a natural-language label')


class PlannerInterface(SpecInterface):
    key: str = Field(pattern=r'^[a-z0-9_]{1,60}$', description=(
        'globally unique across the entire plan, including different owners; use a distinct owner-qualified key '
        'for each interface and reference it as if.<key>.'))
    owner: DefinitionId
    at: list[float] = Field(min_length=3, max_length=3)


class PlannerEvidence(_M):
    source: Literal['design_plan']
    ref: str = Field(pattern=r'^D-\d{2,}/R[1-9]\d*$', description='This exact owning definition/requirement, e.g. D-01/R1')
    value: None = None


class _PlannerRequirement(_M):
    id: RequirementId
    evidence: list[PlannerEvidence] = Field(min_length=1, max_length=1)
    derived: list[str] = Field(default_factory=list, description='Fields computed from the designed values; explain in derivation')
    derivation: str | None = Field(default=None, description='Calculation from the designed values, when derived is nonempty')
    assumed: list[str] = Field(default_factory=list, max_length=0, description='Empty: the plan must resolve all critical design values')


class PlannerEnvelope(_PlannerRequirement, EnvelopeReq):
    """Bounding box only. Also freeze a positive solid shape; this requirement does not define its outer contour."""

    min: tuple[float, float, float]


class PlannerHole(_PlannerRequirement, HoleReq):
    pass


class PlannerPocket(_PlannerRequirement, PocketReq):
    pass


class PlannerBand(_PlannerRequirement, BandReq):
    """Positive solid shape: every XY section in the Z interval is outer minus all full-interval voids."""


class PlannerNote(_PlannerRequirement, NoteReq):
    pass


class PlannerPrism(_PlannerRequirement, PrismReq):
    """Positive solid shape along plane.normal: outline minus all full-interval voids, from s0 to s1.

    Rect outline defines a block; circle outline defines a cylinder along any local axis. A bore is also a void.
    """


class PlannerRevolve(_PlannerRequirement, RevolveReq):
    pass


class PlannerSweep(_PlannerRequirement, SweepReq):
    pass


class PlannerCut(_PlannerRequirement, CutReq):
    pass


class PlannerCoil(_PlannerRequirement, CoilReq):
    pass


class PlannerText(_PlannerRequirement, TextReq):
    pass


PlannerRequirement = Annotated[PlannerEnvelope | PlannerHole | PlannerPocket | PlannerBand | PlannerNote | PlannerPrism |
    PlannerRevolve | PlannerSweep | PlannerCut | PlannerCoil | PlannerText, Field(discriminator='kind')]


class PlannerContent(DesignPlanContent):
    root: DefinitionId
    definitions: list[PlannerDefinition] = Field(min_length=1, max_length=32)
    instances: list[PlannerInstance] = Field(max_length=128)
    relations: list[PlannerRelation] = Field(default_factory=list, max_length=256)
    facts: list[PlannerFact] = Field(default_factory=list, max_length=512)
    interfaces: list[PlannerInterface] = Field(default_factory=list, max_length=128)
    part_requirements: dict[DefinitionId, Annotated[list[PlannerRequirement], Field(min_length=2, max_length=128)]] = Field(
        description='Each part needs exactly one envelope AND a positive solid shape (band/prism/revolve/sweep/coil), '
                    'plus its geometric features. Envelope is only the bounding box; hole/pocket/cut or notes cannot '
                    'define the outer body. Freeze all full-section openings in band/prism voids. No assembly keys.')
    locked_placements: list[Annotated[str, Field(pattern=r'^I-\d{2,}(/I-\d{2,})*$')]] = Field(default_factory=list, max_length=128)


class DesignPlannerReply(_M):
    content: PlannerContent
    wait_for_user: bool = False


class OptionAnswer(_M):
    question_id: str = Field(min_length=1, max_length=80)
    kind: Literal['option']
    option_id: str = Field(min_length=1, max_length=80)


class ValueAnswer(_M):
    question_id: str = Field(min_length=1, max_length=80)
    kind: Literal['value']
    value: Scalar


class DesignChoiceIn(_M):
    client_request_id: uuid.UUID
    design_ref: DesignRef
    question_set_id: uuid.UUID
    action: DesignAction
    answers: list[Annotated[OptionAnswer | ValueAnswer, Field(discriminator='kind')]] = Field(default_factory=list, max_length=12)
    free_text: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def distinct_answers(self):
        ids = [a.question_id for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate question answer')
        if self.action == 'customize' and not self.answers and not self.free_text.strip():
            raise ValueError('customize needs an answer or free text')
        return self
