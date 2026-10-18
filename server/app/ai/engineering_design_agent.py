"""Explicitly delegated engineering design, separate from source extraction and CAD generation."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .providers.base import Message, ModelProvider, ProviderError
from .spec_agent import SpecReply
from .trace import Tracer

PROMPT_VERSION = "engineering-design-2026-10-17.2"
SCHEMA_NAME = "partroute_engineering_design"
# A complete assembly is substantially longer than one part's reply. Tracer still caps every attempt
# to the workflow's remaining runtime/call budget and applies the existing cancellation/lease checks.
CALL_TIMEOUT_S = 600


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    formula: str = Field(min_length=1, max_length=2000)
    substitution: str = Field(min_length=1, max_length=2000)
    result: str = Field(min_length=1, max_length=1000)
    limitations: str = Field(min_length=1, max_length=2000)


class DesignReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_quote: str = Field(min_length=1, max_length=1000)
    spec: SpecReply
    rationale: str = Field(min_length=1, max_length=8000)
    calculations: list[Calculation] = Field(min_length=1, max_length=30)
    motion: str = Field(min_length=1, max_length=4000)
    unverified: list[str] = Field(min_length=1, max_length=30)


SYSTEM = """You are the engineering design stage of PartRoute. The human explicitly delegated the choice of missing
design parameters. Design a constructible assembly from the exact human requirements and the extracted facts.
This is design synthesis, not evidence extraction. All your choices remain AI design assumptions, never human facts
or accepted safety validation. Do not claim that strength, motion, stability or manufacturing have been tested.
Return a complete typed spec with a real bill of parts, one definition per unique part, one instance per physical use,
and numerical placements for EVERY instance. This is not a prose-only concept or an image. All parts must be feasible
for ordinary solid modeling. Use the fewest distinct parts consistent with a functional, maintainable mechanism.
Emit compact JSON: omit optional null values, empty arrays and default-valued fields. Do not repeat the same geometry
in notes, facts, assumptions and rationale; describe each detail completely once in that part's facts or local-frame
notes. A design fact's concrete derivation is sufficient for the server to create its assumption record; explicit
assumptions should add distinct engineering decisions, not duplicate every fact. Reuse definitions for identical
components, but retain every physical instance and every feature needed for function, assembly and maintenance.
Keep calculations concise with the actual formula, substitutions, result and limitations; avoid repeated explanations.
The authorization_quote must be an exact contiguous quote from human_requirements explicitly delegating design.
Include fasteners, nuts, bearings/bushings, retainers, supports and mating holes needed for the design to work.
Do not replace an assembly with a bounding box. No hardcoded product template is provided.
Keep the original root key and all original definitions/instances/relations, all their factual values and quantities.
Only add new facts: do not repeat, modify or reuse ids of extracted facts. New facts have status=assumed, evidence=[],
derivation explaining the engineering choice. Provide assumptions with concrete values and specific bases.
Never invent source locators, source quotes or human confirmations. The server preserves the original facts/evidence.
Omit skeleton and unbound: these are source-extraction records preserved by the server, never design output.
Leave sources/evidence empty on new design assumptions, interfaces and notes. Do not guess template identifiers;
prefer the supplied supported_templates for components they can represent exactly at the declared representation.
Use only their actual ids/parameter names and their template_frames. Supply explicit geometry/interface parameters;
never select a template whose fixed geometry cannot provide required holes, local origin, or functionality. A template
reference replaces repeated descriptions of its generated shape; still record the chosen dimensions and design basis.
Use ordinary detailed part geometry when no listed template fits. Never invent a template.
Use keys D-00,D-01...; I-01...; M-01...; F-100...; A-100...; N-100...; Q-100.... Avoid existing ids.
Specify each part in its LOCAL frame: exact origin, axis, min/max extents, full shape, lengths, diameters, through/blind
holes, hole axes/positions/depths, mating fits/clearance, material and representation. Every manufactured part needs
complete geometry in its facts/notes, not just a name or envelope. Model each part once. Each instance placement is
translate [x,y,z] mm and Euler rotate [rx,ry,rz] degrees (X then Y then Z), in its parent frame, right-handed Z up.
Choose simple consistent datums so generated local solids and placements align. Prefer flat hierarchy when practical.
Preserve all human load, travel, usable-area and full-motion-envelope constraints. Assemble at requested motion position.
Check dimensional chains numerically: all travel endpoints fit the envelope, matching bores and shafts align, fasteners
have clearance, no intentional overlaps except nominal thread representation. Give explicit travel stops and retention.
Simplified standard parts may omit helical thread geometry but must retain functional diameters, bores and interfaces;
label simplifications and put nominal standard/size in BOM. Never claim a threadless shape validates self-locking.
Do calculations with formulas, substituted numbers, units, stated material/friction assumptions and limitations. Cover
requested load capacity, drive/holding behavior, motion, stability and envelope. Explain remaining checks. If holding
depends on assumed friction, provide a positive locking mechanism as appropriate and disclose testing needs.
All geometry choices must be settled here; downstream standard modeling may not guess missing critical dimensions.
Unresolved safety verification belongs in unverified, not as a claim of acceptance. An inconsistent human constraint
must remain a question rather than being silently relaxed. Output only JSON matching the schema."""


@dataclass
class DesignOutcome:
    content: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    calls: list[Any] = field(default_factory=list)
    error: dict[str, Any] | None = None


def run_design(provider: ModelProvider, human_text: str, original: dict[str, Any], *, timeout_s: float = CALL_TIMEOUT_S,
               available_tokens: int | None = None) -> DesignOutcome:
    from ..errors import AppError
    from ..services.engineering_design import compose_design, explicit_authorization
    from ..services.assistant_context import estimate_tokens
    from ..cad.templates import catalog

    tracer = Tracer(unit="engineering_design")
    templates = [{**{k: t[k] for k in ("id", "version", "representation", "procurement", "standard", "ports")},
                  "title": t["title"]["en"],
                  "params": [{**{k: v for k, v in p.items() if k != "label"}, "description": p["label"]["en"]}
                             for p in t["params"]]} for t in catalog()]
    # The local frames are the existing template module's public contract, not inferred assembly placements.
    frames = ("Round templates: axis +Z, bottom/seating face Z=0; bolt head bearing face Z=0 and shank toward -Z. "
              "Plates and boxes: centred on X/Y, bottom face Z=0. Bent bracket: outer virtual sharp of first bend "
              "at origin, base leg along +X on Z=0, width centred on Y=0. Threads are omitted in simplified bolt/nut "
              "templates; their plain nominal shank/bore must not be treated as verified threads or self-locking.")
    messages = [Message(role="user", content=json.dumps({"human_requirements": human_text, "extracted_spec": original,
                                                          "supported_templates": templates, "template_frames": frames},
                                                         ensure_ascii=False, separators=(",", ":")))]
    for attempt in range(3):
        if available_tokens is not None:
            required = estimate_tokens(SYSTEM + json.dumps(DesignReply.model_json_schema()) + "".join(m.content for m in messages)) + 256
            if required > available_tokens:
                return DesignOutcome(calls=list(tracer.calls), error={"code": "context_over_budget", "message": "Complete design context exceeds the frozen model budget",
                                                                     "details": {"required_est": required, "available": available_tokens}, "retryable": False})
        try:
            result = tracer.model_call(provider, purpose="engineering_design", system=SYSTEM, system_version=PROMPT_VERSION,
                                       messages=messages, json_schema=DesignReply.model_json_schema(), schema_name=SCHEMA_NAME,
                                       timeout_s=timeout_s)
        except ProviderError as exc:
            return DesignOutcome(calls=list(tracer.calls), error={"code": exc.code, "message": exc.message, "retryable": exc.retryable})
        try:
            reply = DesignReply.model_validate(result.data or {})
            if reply.authorization_quote not in human_text or not explicit_authorization(reply.authorization_quote):
                raise AppError(422, "design_authorization_missing", "Quote the exact human text that explicitly delegates design")
            content = compose_design(original, reply.model_dump(mode="json"))
            return DesignOutcome(content=content, report=reply.model_dump(mode="json", exclude={"spec"}), calls=list(tracer.calls))
        except (ValidationError, AppError) as exc:
            problem = str(exc) if isinstance(exc, ValidationError) else json.dumps({"message": exc.message, "details": exc.details}, ensure_ascii=False)
            if attempt == 2:
                return DesignOutcome(calls=list(tracer.calls), error={"code": "design_invalid", "message": "The proposed design failed structural validation",
                                                                   "details": {"problems": problem[:6000]}, "retryable": False})
            messages.extend([Message(role="assistant", content=result.text), Message(role="user", content="Repair the complete design JSON. Server validation:\n" + problem)])
    raise AssertionError("bounded design loop")
