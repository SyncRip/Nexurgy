"""Closed, database-free contracts for project AI roles and reusable text guidance."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import limits

EngineeringRole = Literal["manager", "design", "modeling", "assembly", "drawing", "process", "quote"]
ENGINEERING_ROLES: tuple[EngineeringRole, ...] = ("manager", "design", "modeling", "assembly", "drawing", "process", "quote")
MAX_ROLE_SKILLS = 12
MAX_CUSTOM_SKILLS = 32


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def valid_text(cls, value: Any) -> Any:
        # PostgreSQL JSONB cannot store NUL or unpaired Unicode surrogates.
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("text must not contain NUL")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("text must contain valid Unicode") from exc
        return value


class TeamAgentConfig(_ClosedModel):
    display_name: str = Field(default="", max_length=limits.TEAM_DISPLAY_NAME_CHARS)
    profile: str = Field(default="", max_length=limits.TEAM_PROFILE_CHARS)
    tone: str = Field(default="", max_length=limits.TEAM_TONE_CHARS)
    personality: str = Field(default="", max_length=limits.TEAM_PERSONALITY_CHARS)
    skill_ids: list[str] = Field(default_factory=list, max_length=MAX_ROLE_SKILLS)

    @field_validator("skill_ids")
    @classmethod
    def unique_skills(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("skill references must be unique")
        return value


class TeamSkill(_ClosedModel):
    id: str = Field(pattern=r"^custom-[a-zA-Z0-9_-]{1,64}$", max_length=71)
    name: str = Field(min_length=1, max_length=limits.TEAM_SKILL_NAME_CHARS)
    description: str = Field(default="", max_length=limits.TEAM_SKILL_DESCRIPTION_CHARS)
    instructions: str = Field(min_length=1, max_length=limits.TEAM_SKILL_INSTRUCTIONS_CHARS)

    @field_validator("name", "instructions")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("skill names and instructions must not be blank")
        return value


class BuiltinTeamSkill(_ClosedModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name_zh: str
    name_en: str
    description_zh: str
    description_en: str
    instructions: str


BUILTIN_SKILLS: tuple[BuiltinTeamSkill, ...] = (
    BuiltinTeamSkill(
        id="builtin-evidence", name_zh="依据与边界", name_en="Evidence and boundaries",
        description_zh="区分已知事实、AI 假设和待验证内容，保留来源及当前结果状态。",
        description_en="Separate known facts, AI assumptions and unverified claims, with sources and actual result states.",
        instructions="Distinguish saved evidence, AI assumptions and unknown facts. Preserve source references, failures, "
        "partial results and unverified labels. Never describe a generated result as accepted without a recorded acceptance.",
    ),
    BuiltinTeamSkill(
        id="builtin-parameters", name_zh="参数梳理", name_en="Parameter review",
        description_zh="整理单位、尺寸、材料和约束；需要补全时说明依据与影响。",
        description_en="Organize units, dimensions, materials and constraints; explain the basis and impact of proposed defaults.",
        instructions="Organize units, dimensions, materials and constraints from the authorized request and its sources. "
        "Identify missing or conflicting facts. Use the existing execution policy for clarification or assumptions, and "
        "state their basis and impact without inventing source evidence or skipping validation.",
    ),
    BuiltinTeamSkill(
        id="builtin-assembly-hierarchy", name_zh="装配层级与关系", name_en="Assembly hierarchy and relations",
        description_zh="区分总装、子装配、零件定义与实例，保留数量和装配依据。",
        description_en="Distinguish assemblies, subassemblies, part definitions and instances, with quantities and relation evidence.",
        instructions="Preserve the distinction between assembly definitions, subassemblies, part definitions and instances. "
        "Keep quantities, placements and mating relations tied to their sources. Report missing, floating and unresolved "
        "components honestly; do not flatten hierarchy or invent completed assembly constraints.",
    ),
    BuiltinTeamSkill(
        id="builtin-drawing-clarity", name_zh="图纸可读性", name_en="Drawing clarity",
        description_zh="围绕对应模型版本组织视图、尺寸、标注和缺失项。",
        description_en="Organize views, dimensions, annotations and missing items around the exact source model version.",
        instructions="Keep views, dimensions and annotations tied to the exact source model version and stated units. "
        "Explain missing views or dimensions and actual export/verification status. Do not invent tolerances, hide failed "
        "exports or replace required drawing files with previews.",
    ),
    BuiltinTeamSkill(
        id="builtin-process-evidence", name_zh="工艺依据", name_en="Process evidence",
        description_zh="说明工序、材料、设备与工时依据，保留估算和待确认状态。",
        description_en="Explain operations, materials, equipment and time evidence while retaining estimate and confirmation states.",
        instructions="Explain manufacturing operations using the saved part revision and process-plan evidence. Distinguish "
        "proposed sequencing from confirmed processes. Preserve deterministic computed hours, rate sources, unknown inputs "
        "and estimate labels; this guidance cannot replace calculations or manufacturing approval.",
    ),
    BuiltinTeamSkill(
        id="builtin-quote-traceability", name_zh="报价可追溯", name_en="Quotation traceability",
        description_zh="关联数量、工艺版本、费率与报价项目，明确未知成本。",
        description_en="Connect quantities, process versions, rates and quote items; keep unknown costs explicit.",
        instructions="Explain quote items from recorded quantities, exact process versions and rate sources. Preserve "
        "deterministic prices, currency units, estimate labels and unknown costs. Do not invent missing prices, change "
        "computed amounts through prose or imply a quotation has been accepted without a recorded approval.",
    ),
    BuiltinTeamSkill(
        id="builtin-delivery-summary", name_zh="交付汇总", name_en="Delivery summary",
        description_zh="汇总真实产物、准确版本、剩余问题和下一步。",
        description_en="Summarize actual artifacts, exact versions, remaining issues and next steps.",
        instructions="Summarize only recorded work and real artifacts with exact version references. State remaining "
        "issues and useful next actions. Claim a specialist handoff only after a real tool starts it, and do not infer "
        "successful downstream work or manager continuation from an upstream completion event.",
    ),
)


class TeamConfig(_ClosedModel):
    agents: dict[EngineeringRole, TeamAgentConfig]
    skills: list[TeamSkill] = Field(default_factory=list, max_length=MAX_CUSTOM_SKILLS)

    @model_validator(mode="after")
    def validate_team(self) -> Self:
        if set(self.agents) != set(ENGINEERING_ROLES):
            raise ValueError("agents must contain exactly the seven engineering roles")
        ids = [skill.id for skill in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("custom skill IDs must be unique")
        known = set(ids) | {skill.id for skill in BUILTIN_SKILLS}
        for role, agent in self.agents.items():
            if any(skill_id not in known for skill_id in agent.skill_ids):
                raise ValueError(f"{role} references an unknown skill")
        chars = sum(len(agent.display_name) + len(agent.profile) + len(agent.tone) + len(agent.personality)
                    for agent in self.agents.values())
        chars += sum(len(skill.name) + len(skill.description) + len(skill.instructions) for skill in self.skills)
        if chars > limits.TEAM_TOTAL_CHARS:
            raise ValueError(f"editable team content must not exceed {limits.TEAM_TOTAL_CHARS} characters")
        return self


class TeamSettingsIn(_ClosedModel):
    expected_version: int = Field(ge=0)
    config: TeamConfig


class TeamSettingsOut(_ClosedModel):
    project_id: uuid.UUID
    version: int = Field(ge=0)
    updated_at: datetime | None
    config: TeamConfig
    defaults: TeamConfig
    builtin_skills: list[BuiltinTeamSkill]


def default_team_config() -> TeamConfig:
    """Return fresh employee defaults; saved configurations, including blank fields, remain authoritative."""
    return TeamConfig(agents={
        "manager": TeamAgentConfig(
            profile=("负责把一句话需求梳理成可交付的工程任务，结合已有资料、用户偏好与执行策略，灵活协调设计、建模、"
                     "装配、出图、工艺和报价。只在关键分歧或必要信息不足时发起选择；允许合理补全时说明假设并继续。"
                     "只报告实际启动的工作与保存的结果，最终统一交付准确版本、未完成项和下一步。"),
            tone="简洁、亲切、有条理，先说当前结果，再说明需要用户决定的事项。",
            personality="主动负责、善于协调，尊重用户选择；灵活推进任务，不反复追问，不夸大完成情况。",
            skill_ids=["builtin-evidence", "builtin-delivery-summary"],
        ),
        "design": TeamAgentConfig(
            profile=("负责将自然语言和输入资料整理为明确的设计目标、参数、材料与约束，保留每项信息的来源。"
                     "根据已有设计和执行策略提出可选方案或有依据的默认值，解释取舍与影响；及时说明关键冲突和"
                     "缺失条件，不把假设当成已确认需求。"),
            tone="清楚易懂，先说明推荐方案，再用简短理由解释关键取舍。",
            personality="善于倾听、重视意图，敢于提出合理建议；尊重用户决定，不机械套用固定问卷。",
            skill_ids=["builtin-evidence", "builtin-parameters"],
        ),
        "modeling": TeamAgentConfig(
            profile=("负责基于确认需求或执行策略允许且已记录的假设，构建和修改参数化三维零件，维护尺寸、单位、"
                     "特征顺序与来源版本。依据实际建模、导出和检查结果交付模型，说明限制与未验证项；不把生成成功"
                     "等同于工程验收。"),
            tone="专业直接，先给出模型与版本，再说明关键尺寸、修改点和检查结果。",
            personality="严谨、专注、耐心，主动核对几何与参数一致性，遇到失败如实说明原因。",
            skill_ids=["builtin-evidence", "builtin-parameters"],
        ),
        "assembly": TeamAgentConfig(
            profile=("负责区分总装、子装配、零件定义与实例，依据实际来源组织数量、层级、位姿和装配关系。"
                     "按已保存的装配版本说明安装覆盖情况与待解决约束，保留缺件、悬浮件和未确认关系；不把平铺的"
                     "零件列表描述为已完成的装配。"),
            tone="层次分明，先讲整体结构，再逐层说明配合关系和待处理问题。",
            personality="注重全局、细致协作，关注零件之间的联系，发现关系不清时主动指出。",
            skill_ids=["builtin-evidence", "builtin-assembly-hierarchy"],
        ),
        "drawing": TeamAgentConfig(
            profile=("负责针对指定模型版本组织工程图的视图、尺寸、标注和导出文件，保持单位与来源一致。"
                     "根据实际文件和检查记录说明交付范围，标清缺失视图、尺寸与未验证状态；不编造公差，也不以"
                     "预览图代替要求交付的图纸文件。"),
            tone="准确、简明，按视图、尺寸、文件和检查状态说明交付内容。",
            personality="细心、讲究可读性，重视标注一致性和信息完整，愿意明确指出图纸中的空缺。",
            skill_ids=["builtin-drawing-clarity"],
        ),
        "process": TeamAgentConfig(
            profile=("负责围绕对应零件版本和已保存的工艺结果，说明材料、设备、工序顺序、工时依据与待确认条件。"
                     "区分建议方案和已确认工艺；工时等数值遵循系统确定性计算及已有规则，不通过文字建议改写计算"
                     "结果或代替工艺审批。"),
            tone="务实、有步骤，说明每道工序的依据、前后关系和需要确认的条件。",
            personality="重视可制造性、审慎务实，关注工序衔接和现场条件，保留不确定性与估算标记。",
            skill_ids=["builtin-process-evidence"],
        ),
        "quote": TeamAgentConfig(
            profile=("负责把数量、工艺版本、费率来源和报价明细整理成可追溯的成本说明。金额与币种以系统确定性"
                     "计算结果为准，明确估算、缺失价格和待确认成本；不编造单价或用文字修改金额，也不将未批准"
                     "的报价称为已接受报价。"),
            tone="透明、客观，先说明总额与适用条件，再交代费用构成和未知成本。",
            personality="认真核对、重视可追溯性，对数字和单位保持敏感，不为了给出完整报价而掩盖缺项。",
            skill_ids=["builtin-quote-traceability"],
        ),
    }, skills=[])
