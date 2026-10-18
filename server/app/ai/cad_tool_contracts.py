"""Internal, database-free inputs and outcomes for the fixed CAD planners.

These are not tool arguments. Identity, state, selection and model provenance are
resolved by the service before model I/O; only the Reply models are model output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..cad import drafting
from ..cad.drawing import PackageConfig
from ..domain.hashing import sha256_hex
from . import limits
from .trace import AiCallRecord

Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Policy = Literal['standard', 'turbo']
EntityId = Annotated[str, Field(min_length=1, max_length=160)]
Question = Annotated[str, Field(min_length=1, max_length=2000)]


class _Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


def drawing_state_hash(state: drafting.DraftState) -> str:
    return sha256_hex(state.model_dump(mode='json'))


class DrawingSelection(_Contract):
    ids: list[EntityId] = Field(min_length=1, max_length=5000)
    sheet_id: str | None = Field(None, min_length=1, max_length=160)

    @field_validator('ids')
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError('selection ids must be unique')
        return value


class DraftingEditInput(_Contract):
    document_id: UUID
    base_version_id: UUID
    base_version_no: int = Field(ge=1)
    state: drafting.DraftState
    state_sha256: Digest
    selection: DrawingSelection | None = None
    text: str = Field(min_length=1, max_length=limits.TOOL_TEXT_CHARS)
    execution_policy: Policy = 'standard'

    @model_validator(mode='after')
    def exact_state(self) -> DraftingEditInput:
        if drawing_state_hash(self.state) != self.state_sha256:
            raise ValueError('state_sha256 does not match the exact base state')
        if not self.text.strip():
            raise ValueError('text must not be blank')
        return self


class DraftingEditReply(_Contract):
    summary: str = Field('', max_length=2000)
    commands: list[dict[str, Any]] = Field(default_factory=list, max_length=drafting.MAX_COMMANDS)
    questions: list[Question] = Field(default_factory=list, max_length=20)


class ValidatedDrawingEditPlan(_Contract):
    schema_version: Literal[1] = 1
    document_id: UUID
    base_version_id: UUID
    base_version_no: int = Field(ge=1)
    state_sha256: Digest
    selection: DrawingSelection | None = None
    commands: list[dict[str, Any]] = Field(max_length=drafting.MAX_COMMANDS)
    affected_entity_ids: list[str] = Field(default_factory=list)
    removed_entity_ids: list[str] = Field(default_factory=list)
    summary: str = Field('', max_length=2000)
    changes: list[str] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list, max_length=20)


def edit_plan_hash(plan: ValidatedDrawingEditPlan) -> str:
    """Hash only executable content, exact identity and required decisions."""
    return sha256_hex({'kind': 'drafting_modify', **plan.model_dump(mode='json', exclude={
        'summary', 'changes', 'warnings', 'questions', 'affected_entity_ids'})})


@dataclass
class DraftingEditOutcome:
    plan: ValidatedDrawingEditPlan | None = None
    calls: list[AiCallRecord] = field(default_factory=list)
    simulated: bool = False
    repairs: int = 0
    questions: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None


class DrawingConfigInput(_Contract):
    revision_id: UUID
    revision_facts: dict[str, Any] = Field(default_factory=dict)
    requirements: str = Field(max_length=limits.TOOL_TEXT_CHARS)
    execution_policy: Policy = 'standard'


class ConfigSection(_Contract):
    plane: Literal['XZ', 'YZ']
    offset: float


class DrawingConfigValues(_Contract):
    """Only renderer-supported settings. Title/identity remain server-owned."""
    views: list[Literal['front', 'top', 'right', 'iso']] = Field(default_factory=lambda: ['front', 'top', 'right', 'iso'],
                                                              min_length=1, max_length=4)
    scale: Literal['auto'] | Annotated[float, Field(gt=0)] = 'auto'
    projection: Literal['third', 'first'] = 'third'
    hidden_lines: bool = True
    dimensions: bool = True
    section: Literal['auto', 'none'] | ConfigSection = 'auto'

    @field_validator('views')
    @classmethod
    def unique_views(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError('views must be unique')
        return value


class DrawingConfigCoverage(_Contract):
    requirement: str = Field(min_length=1, max_length=2000)
    field: Literal['views', 'scale', 'projection', 'hidden_lines', 'dimensions', 'section', 'unsupported']


class DrawingConfigReply(_Contract):
    config: DrawingConfigValues = Field(default_factory=DrawingConfigValues)
    coverage: list[DrawingConfigCoverage] = Field(default_factory=list, max_length=100)
    questions: list[Question] = Field(default_factory=list, max_length=20)
    unsupported: list[Question] = Field(default_factory=list, max_length=20)


@dataclass
class DrawingConfigOutcome:
    config: PackageConfig | None = None
    coverage: list[dict[str, Any]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    calls: list[AiCallRecord] = field(default_factory=list)
    simulated: bool = False
    repairs: int = 0
    error: dict[str, Any] | None = None


class PreparedTextDrawing(_Contract):
    """Validated once, saved without repeating generation; never user input."""
    state: drafting.DraftState
    text: str = Field(min_length=1, max_length=limits.DRAWING_TEXT_CHARS)
    source: dict[str, Any]
    ai: dict[str, Any]
    labels: list[str] = Field(default_factory=list)
    execution_policy: Policy


@dataclass
class TextDrawingOutcome:
    prepared: PreparedTextDrawing | None = None
    calls: list[AiCallRecord] = field(default_factory=list)
    simulated: bool = False
    repairs: int = 0
    questions: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
