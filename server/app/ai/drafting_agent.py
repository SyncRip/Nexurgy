"""Text → standalone 2D drawing agent, used by the drawing-document service.

The model returns a :class:`DraftingReply`: sheet, title block, entities (cad/drafting shapes without ids; an
optional ``key`` lets dimensions / hatches reference other entities), conflicts between stated readings,
assumptions and questions. :func:`build_state` validates every entity with ``cad/drafting`` and builds the
document state; problems go back to the model within a repair budget (W0 §7: two repairs, 180 s per call).

Policy rules the server enforces (W0 §2): under ``standard`` the model asks instead of assuming (an assumption
in the reply is a repair problem) and conflicting readings stay open conflict notes; under ``turbo`` the model
may adopt a value, which is recorded as an :class:`app.policy.Assumption` (author ``ai_turbo``) while both
original readings stay in the conflict note. Nothing here writes the database (``app/ai`` never imports
``app.models``).

The simulated responder (``PARTROUTE_AI_SIMULATION``) answers this schema by name with deterministic rules for
rectangular plates with round holes (size ``W×H``, ``N × Ød`` holes, centres ``(x, y)``, material / title /
drawing number, a second reading of the width as a conflict). It is labelled simulated and never counts as a real
model run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..cad import drafting
from ..policy import Assumption
from .prompts import extract_request_text, guard_tags
from .providers.base import Message, ModelProvider, ProviderError, clip, schema_budget_text
from .providers.fake import FakeRequest
from .simulated import register_schema_responder
from .trace import Tracer

log = logging.getLogger("partroute.ai")

SCHEMA_NAME = "partroute_drafting"
DRAFTING_PROMPT_VERSION = "drafting-2026-10-12.1"
MAX_AI_ENTITIES = 400
SIM_NOTE_ZH = "模拟结果，未调用模型。"
SIM_NOTE_EN = "Simulated drawing, no model was called."


class AiReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | str = Field(description="The value as stated")
    quote: str = Field(description="The exact words of the request that state it", max_length=400)


class AiConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(description="What the readings disagree about, e.g. 'outline width'", max_length=120)
    values: list[AiReading] = Field(min_length=2, max_length=10)
    note: str = Field("", max_length=500)
    adopted: float | str | None = Field(None, description="turbo only: the value the drawing uses (recorded as an assumption)")


class AiAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(max_length=120)
    field: str = Field("", max_length=80)
    adopted: Any = None
    unit: str | None = None
    basis: str = Field("", max_length=500)
    alternatives: list[Any] = Field(default_factory=list, max_length=10)
    # Generation supplement D-02: why a decision was needed and what it affects (entity keys, title-block keys).
    trigger: Literal["missing", "ambiguous", "conflict"] | None = None
    affected: list[str] = Field(default_factory=list, max_length=50,
                                description="entity keys / title-block keys (title_block.<key>) the value flows into")


class AiSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: Literal["A4", "A3", "A2", "A1", "A0"] = "A3"
    orientation: Literal["landscape", "portrait"] = "landscape"
    scale: float = Field(1.0, gt=0, le=100, description="paper mm per model mm: 1:2 → 0.5")
    origin: list[float] | None = Field(None, description="model-space lower-left corner of the sheet border [x, y]")


class DraftingReply(BaseModel):
    """Structured output requested from the model (non-strict json_schema)."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="What was drawn, in the language of the request", max_length=4000)
    sheet: AiSheet = Field(default_factory=AiSheet)
    title_block: dict[str, str] = Field(default_factory=dict, description=f"keys: {', '.join(drafting.TITLE_FIELDS)}")
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_AI_ENTITIES,
                                           description="cad entities (see the system prompt); optional 'key' for references")
    conflicts: list[AiConflict] = Field(default_factory=list, max_length=50)
    assumptions: list[AiAssumption] = Field(default_factory=list, max_length=50)
    questions: list[str] = Field(default_factory=list, max_length=20)


def reply_json_schema() -> dict[str, Any]:
    return DraftingReply.model_json_schema()


SYSTEM_PROMPT = """You are PartRoute's 2D drafting assistant. You turn a written request into a standalone engineering
drawing (no 3D model exists). Your whole reply is one JSON object matching the schema.

Coordinates: model space in millimetres, x to the right, y up. Use the origin the request states (e.g. the lower-left
corner of a plate); otherwise put the lower-left corner of the main outline at (0, 0).

Entities (field "type"; every point is [x, y]; angles in degrees, counter-clockwise from +x):
- {"type":"line","p1":[x,y],"p2":[x,y]}
- {"type":"polyline","points":[[x,y],...],"closed":true|false,"bulges":[...] optional, one per point}
- {"type":"circle","center":[x,y],"radius":r}
- {"type":"arc","center":[x,y],"radius":r,"start_angle":a,"end_angle":b}
- {"type":"text","insert":[x,y],"text":"...","height":3.5}
- {"type":"mtext","insert":[x,y],"text":"line 1\\nline 2","height":3.5}
- {"type":"leader","points":[[tip],[elbow],[end]],"text":"..."}
- {"type":"dimension","dimtype":"linear","p1":[x,y],"p2":[x,y],"location":[x,y],"angle":0 (horizontal) | 90 (vertical)}
- {"type":"dimension","dimtype":"aligned","p1":[x,y],"p2":[x,y],"location":[x,y]}
- {"type":"dimension","dimtype":"angular","center":[vertex],"p1":[on ray 1],"p2":[on ray 2],"location":[on the arc]}
- {"type":"dimension","dimtype":"diameter"|"radius","center":[x,y],"radius":r,"angle":45}
- {"type":"hatch","loops":[{"points":[[x,y],...]} | {"center":[x,y],"radius":r}],"pattern":"ANSI31","solid":false}
Every entity takes "layer": OUTLINE (visible edges), CENTER (centre lines), HIDDEN, DIM (dimensions, leaders), TEXT
(notes), HATCH. An optional "key" names an entity; dimensions may list "refs":[keys] and hatches "boundary_refs":[keys].

Dimensions are native: their definition points must lie on the drawn geometry (outline corners, centre-line ends,
circle centres via the centre lines). The displayed value is measured from the geometry; "text_override" is only for
prefixes / suffixes around the measured value, written with <> (e.g. "4×Ø<>"). Never type a number that differs from
the geometry.

Readings: when the request states two different values for the same thing, list both in "conflicts" with the exact
quotes. Never invent a value: material, positions, sizes and numbers come from the request.

Execution policy (the <policy> block, authoritative):
- standard: do not assume. Missing facts that the drawing needs go to "questions"; "assumptions" must be empty.
  Conflicts stay open notes (leave "adopted" null) and the geometry uses the first stated value.
- turbo: do not ask. Adopt a value for each missing / conflicting fact, set "adopted" on conflicts (with the reason in
  its "note") and record every other choice in "assumptions": subject, field, adopted (concrete, never empty or "TBD"),
  unit, trigger ("missing" | "ambiguous" | "conflict"), basis (the specific reason: the stated fact, peer value or
  standard it follows — never only "as usual" or "by convention"), alternatives and affected (the entity keys and
  "title_block.<key>" the value flows into). A value the request states without a conflict is never re-chosen.

Title block keys: title, drawing_number, material, scale, revision, drawn_by, date, company, project, notes. Leave a key
out rather than guessing it. Add centre lines for holes and the overall and position dimensions a machinist needs."""


def user_message(request_text: str, *, policy: str, title: str | None = None) -> str:
    parts = [f"<policy>{policy}</policy>"]
    if title:
        parts.append("<context>\n" + guard_tags(f"Document title: {title}") + "\n</context>")
    body = guard_tags(request_text.strip()).replace("<policy", "&lt;policy")
    parts.append("<request>\n" + body + "\n</request>")
    return "\n".join(parts)


def repair_message(problems: list[str]) -> str:
    lines = "\n".join(f"- {p}" for p in problems[:12])
    return ("Your drawing could not be used. Fix these problems and reply with the whole JSON object again:\n" + lines)


@dataclass
class DraftingOutcome:
    reply: dict[str, Any] | None
    state: drafting.DraftState | None
    summary: str
    simulated: bool
    calls: list = field(default_factory=list)
    error: dict[str, Any] | None = None
    repairs: int = 0
    questions: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_state(reply: DraftingReply, *, policy: str, source_id: str = "S-1", at: str | None = None) -> drafting.DraftState:
    """The document state of a reply. Raises :class:`drafting.DraftingError` with what to repair."""
    at = at or _now()
    if policy != "turbo" and reply.assumptions:
        raise drafting.DraftingError("assumption_in_standard", "standard mode: put missing facts in 'questions' instead of "
                                     f"assuming them ({', '.join(a.subject for a in reply.assumptions[:5])})")
    title = {k: v.strip()[:200] for k, v in reply.title_block.items() if k in drafting.TITLE_FIELDS and v and v.strip()}
    try:
        sheet = drafting.Sheet(id="S1", size=reply.sheet.size, orientation=reply.sheet.orientation, scale=reply.sheet.scale,
                               origin=tuple(reply.sheet.origin[:2]) if reply.sheet.origin and len(reply.sheet.origin) >= 2 else (0.0, 0.0),
                               title_block=title)
    except ValidationError as exc:
        raise drafting.DraftingError("invalid_sheet", f"sheet: {exc.errors()[0].get('msg')}") from exc
    state = drafting.new_state(sheet)
    keys: dict[str, str] = {}
    raw: list[dict[str, Any]] = []
    for i, ent in enumerate(reply.entities):
        if not isinstance(ent, dict):
            raise drafting.DraftingError("invalid_entity", f"entities[{i}] is not an object")
        d = {k: v for k, v in ent.items() if k not in ("id", "key", "origin", "modified", "source_ref")}
        d["id"] = drafting.next_id(state)
        key = ent.get("key")
        if isinstance(key, str) and key:
            if key in keys:
                raise drafting.DraftingError("duplicate_key", f"entities[{i}]: key {key!r} is used twice")
            keys[key] = d["id"]
        raw.append(d)
    for i, d in enumerate(raw):
        for ref_field in ("refs", "boundary_refs"):
            if ref_field in d:
                refs = d[ref_field] if isinstance(d[ref_field], list) else []
                unknown = [r for r in refs if r not in keys]
                if unknown:
                    raise drafting.DraftingError("unknown_ref", f"entities[{i}].{ref_field}: unknown key(s) {', '.join(map(str, unknown[:5]))}")
                d[ref_field] = [keys[r] for r in refs]
    try:
        cmds = drafting.parse_commands([{"op": "add_entities", "entities": raw}]) if raw else []
        state, _ = drafting.apply_commands(state, cmds, actor=None, at=at, actor_kind="ai")
    except drafting.DraftingError as exc:
        idx = exc.details.get("errors")
        raise drafting.DraftingError(exc.code, exc.message, {"errors": idx} if idx else {}) from exc
    n_conf = 0
    n_ass = 0
    assumptions: list[dict[str, Any]] = []
    for c in reply.conflicts:
        n_conf += 1
        conflict = drafting.Conflict(
            id=f"C-{n_conf:02d}", subject=c.subject[:120], kind="source_conflict", note=c.note,
            values=[drafting.ConflictValue(value=v.value, text=drafting.fmt(v.value) if isinstance(v.value, (int, float)) else str(v.value), kind="stated", source=source_id,
                                           locator=clip(f"request: “{v.quote}”", 200)) for v in c.values])
        if policy == "turbo" and c.adopted is not None:
            n_ass += 1
            aid = f"A-{n_ass:02d}"
            others = [v.value for v in c.values if str(v.value) != str(c.adopted)]
            # D-02: a conflict decision lists the other readings and where every reading stands (the request quotes)
            assumptions.append(Assumption(id=aid, subject=c.subject[:120], field="value", adopted=c.adopted, basis=c.note or
                                          "Turbo chose one of the conflicting readings", alternatives=others, trigger="conflict",
                                          affected=[conflict.id], sources=[f"{source_id} · {v.locator} = {v.text}" for v in conflict.values],
                                          author="ai_turbo", at=at).model_dump(mode="json"))
            conflict.resolution = {"by": "ai_turbo", "assumption_id": aid, "value": c.adopted, "confirmed": False}
        state.conflicts.append(conflict)
    if policy == "turbo":
        for a in reply.assumptions:
            n_ass += 1
            # entity keys become the drawing's entity ids; title-block keys and other names stay as written
            affected = list(dict.fromkeys(keys.get(k, k)[:80] for k in a.affected if str(k).strip()))
            assumptions.append(Assumption(id=f"A-{n_ass:02d}", subject=a.subject[:120], field=a.field, adopted=a.adopted, unit=a.unit,
                                          basis=a.basis, alternatives=list(a.alternatives), sources=[source_id], author="ai_turbo",
                                          trigger=a.trigger, affected=affected, at=at).model_dump(mode="json"))
    state.assumptions = assumptions
    state.open_questions = [q.strip()[:500] for q in reply.questions if q and q.strip()]
    if not state.entities:
        raise drafting.DraftingError("empty_drawing", "the reply has no entities")
    return state


def _schema_problems(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in err.get('loc', ())) or 'reply'}: {err.get('msg', 'invalid')}" for err in e.errors()[:12]]


def run_drafting(provider: ModelProvider, request_text: str, *, policy: str, title: str | None = None, timeout_s: int = 180,
                 max_repairs: int = 2, stop: Callable[[], None] | None = None) -> DraftingOutcome:
    """One drawing: a structured call, then up to ``max_repairs`` repair calls. Never raises for provider / model
    problems (they come back in ``error`` with every attempted call in ``calls``). A standard-mode reply with
    questions and no entities is returned with ``questions`` and error ``needs_input``."""
    from . import limits
    from .drafting_edit_agent import GuardedProvider

    stop = stop or (lambda: None)
    tracer = Tracer(unit="drafting_create")
    schema = reply_json_schema()
    messages = [Message(role="user", content=user_message(request_text, policy=policy, title=title))]

    def fail(code: str, message: str, repairs: int, **details: Any) -> DraftingOutcome:
        return DraftingOutcome(reply=None, state=None, summary="", simulated=provider.simulated, calls=list(tracer.calls),
                               error={"code": code, "message": message, "details": details}, repairs=repairs)

    if not provider.capabilities.text:
        return fail("unsupported_capability", f"model {provider.model_id} is not configured for text generation", 0)
    repairs = 0
    while True:
        try:
            stop()
            provider.check_live()
            if len(SYSTEM_PROMPT) + len(schema_budget_text(schema, None if provider.simulated else provider.config)) + sum(len(m.content) for m in messages) > limits.PLANNER_TOTAL_CHARS:
                return fail("context_over_budget", "the complete drafting context exceeds the planner budget", repairs)
            res = tracer.model_call(GuardedProvider(provider, stop), purpose="assistant", system=SYSTEM_PROMPT, system_version=DRAFTING_PROMPT_VERSION,
                                    messages=messages, json_schema=schema, schema_name=SCHEMA_NAME, timeout_s=timeout_s)
            stop()
            provider.check_live()
        except ProviderError as e:
            return fail(e.code, e.message, repairs, retryable=e.retryable)
        problems: list[str] = []
        reply: DraftingReply | None = None
        if res.data is None:
            problems = [f"the reply was not a JSON object ({res.parse_error or 'no detail'})"]
        else:
            try:
                reply = DraftingReply.model_validate(res.data)
            except ValidationError as e:
                problems = _schema_problems(e)
        if reply is not None:
            if not reply.entities and reply.questions:
                out = fail("needs_input", "the request lacks facts the drawing needs", repairs, questions=reply.questions[:20])
                out.questions = list(reply.questions[:20])
                out.summary = reply.summary
                return out
            try:
                state = build_state(reply, policy=policy)
                stop()
                provider.check_live()
                return DraftingOutcome(reply=reply.model_dump(mode="json"), state=state, summary=reply.summary.strip(),
                                       simulated=provider.simulated, calls=list(tracer.calls), repairs=repairs,
                                       questions=list(state.open_questions))
            except ProviderError as exc:
                return fail(exc.code, exc.message, repairs, retryable=exc.retryable)
            except drafting.DraftingError as exc:
                problems = [exc.message, *[str(x) for x in (exc.details.get("errors") or [])][:8]]
        if repairs >= min(2, max(0, max_repairs)):
            return fail("invalid_drawing", f"the model's drawing could not be used after {repairs} repair(s)", repairs,
                        problems=[clip(p, 300) for p in problems[:12]])
        repairs += 1
        messages = messages + [Message(role="assistant", content=clip(res.text, 30_000)),
                               Message(role="user", content=repair_message([clip(p, 300) for p in problems]))]


# --- simulated responder (PARTROUTE_AI_SIMULATION) ------------------------------------------------

_CJK = re.compile(r"[㐀-鿿]")
_NUM = r"(\d+(?:\.\d+)?)"
_SIZE = re.compile(_NUM + r"\s*(?:mm)?\s*[x×X*]\s*" + _NUM)
_DIA = re.compile(r"[Øø⌀φΦ]\s*" + _NUM)
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT = re.compile(r"(\d+|[一两二三四五六七八九十])\s*(?:个|只|x|×|-)?\s*(?:through[- ]?)?(?=\s*[Øø⌀φΦ]|\s*(?:holes?|孔))", re.I)
_POINT = re.compile(r"[（(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[)）]")
_TBD = re.compile(r"待定|未定|TBD|to be determined", re.I)
_MATERIAL = re.compile(r"(?:材料|材质)\s*[:：为是用]?\s*([A-Za-z0-9\-_.#]+|[一-鿿]{1,8}(?:钢|铝|铜|合金))|\bmaterial\s*[:=]?\s*([A-Za-z0-9\-_.]+)", re.I)
_TITLE = re.compile(r"(?:名称|标题|零件名)\s*[:：为是]\s*([^\s，,。；;]+)|\btitle\s*[:=]\s*([^\n,;]+)", re.I)
_NUMBER = re.compile(r"(?:图号)\s*[:：为是]?\s*([A-Za-z0-9\-_.]+)|\bdrawing\s*(?:no\.?|number)\s*[:=]?\s*([A-Za-z0-9\-_.]+)", re.I)
_CONFLICT = re.compile(r"(?:另一份?|另有|另外|另一处|冲突|不一致|conflict(?:ing)?|another|other (?:source|drawing))[^0-9\n]{0,30}?" + _NUM, re.I)


def _count(m: re.Match[str] | None) -> int | None:
    if m is None:
        return None
    g = m.group(1)
    return int(g) if g.isdigit() else _CN_DIGITS.get(g)


def _sheet_for(w: float, h: float) -> dict[str, Any]:
    for size, (pw, ph) in (("A4", (297.0, 210.0)), ("A3", (420.0, 297.0))):
        if w + 80 <= pw - 20 and h + 80 <= ph - 60:
            return {"size": size, "orientation": "landscape", "scale": 1.0, "pw": pw, "ph": ph}
    for scale in (0.5, 0.2, 0.1, 0.05, 0.02):
        if (w + 80) * scale <= 400 and (h + 80) * scale <= 237:
            return {"size": "A3", "orientation": "landscape", "scale": scale, "pw": 420.0, "ph": 297.0}
    return {"size": "A3", "orientation": "landscape", "scale": 0.01, "pw": 420.0, "ph": 297.0}


def simulated_drafting_responder(req: FakeRequest) -> dict[str, Any]:
    """Deterministic plate drawing from the request text; labelled simulated. A repair request gets the same answer."""
    first = req.messages[0].content if req.messages else ""
    text = extract_request_text(first)
    m_pol = re.search(r"<policy>(standard|turbo)</policy>", first)
    turbo = bool(m_pol and m_pol.group(1) == "turbo")
    zh = bool(_CJK.search(text))
    note = SIM_NOTE_ZH if zh else SIM_NOTE_EN

    size = _SIZE.search(text)
    if not size:
        q = "请给出板的外形尺寸（长 × 宽，mm）。（模拟模式只认识矩形板）" if zh else \
            "Give the plate's overall size (length × width, mm). (The simulated rules only draw rectangular plates.)"
        return {"summary": note + (" 未能识别外形尺寸。" if zh else " No overall size found."), "questions": [q], "entities": []}
    w, h = float(size.group(1)), float(size.group(2))
    entities: list[dict[str, Any]] = [{"key": "outline", "type": "polyline", "layer": "OUTLINE",
                                       "points": [[0, 0], [w, 0], [w, h], [0, h]], "closed": True}]
    questions: list[str] = []
    assumptions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    dia_m = _DIA.search(text)
    count = _count(_COUNT.search(text))
    centres = [(float(a), float(b)) for a, b in _POINT.findall(text)]
    holes: list[tuple[float, float]] = []
    d = float(dia_m.group(1)) if dia_m else None
    if d is not None:
        if centres:
            holes = centres
            if count and count != len(centres):
                questions.append(f"孔数写的是 {count}，但给出了 {len(centres)} 个孔心。" if zh else
                                 f"The request says {count} holes but gives {len(centres)} centres.")
        elif turbo:
            n = count or 1
            if n == 4:
                holes = [(10.0, 10.0), (w - 10.0, 10.0), (w - 10.0, h - 10.0), (10.0, h - 10.0)]
            else:
                holes = [(w * (i + 1) / (n + 1), h / 2.0) for i in range(n)]
            assumptions.append({"subject": "hole positions", "field": "centres", "adopted": [list(p) for p in holes], "unit": "mm",
                                "basis": "the request gives no hole centres; " + (
                                    f"4 holes 10 mm from each edge of the {fmt_num(w)} × {fmt_num(h)} plate" if n == 4 else
                                    f"{n} hole(s) evenly spaced on the horizontal centre line y = {fmt_num(h / 2)}") + " (simulated)",
                                "alternatives": [], "trigger": "missing", "affected": [f"hole{i + 1}" for i in range(len(holes))]})
        else:
            questions.append("孔的中心位置（x, y，mm）是多少？" if zh else "Where are the hole centres (x, y in mm)?")
    r = (d or 0) / 2.0
    for i, (x, y) in enumerate(holes):
        entities.append({"key": f"hole{i + 1}", "type": "circle", "layer": "OUTLINE", "center": [x, y], "radius": r})
        ext = r + 2.0
        entities.append({"key": f"ch{i + 1}", "type": "line", "layer": "CENTER", "p1": [x - ext, y], "p2": [x + ext, y]})
        entities.append({"key": f"cv{i + 1}", "type": "line", "layer": "CENTER", "p1": [x, y - ext], "p2": [x, y + ext]})

    entities.append({"type": "dimension", "dimtype": "linear", "layer": "DIM", "p1": [0, 0], "p2": [w, 0], "location": [w / 2, -12], "angle": 0,
                     "refs": ["outline"]})
    entities.append({"type": "dimension", "dimtype": "linear", "layer": "DIM", "p1": [0, 0], "p2": [0, h], "location": [-12, h / 2], "angle": 90,
                     "refs": ["outline"]})
    xs = sorted({x for x, _ in holes if abs(x) > 1e-9})
    ys = sorted({y for _, y in holes if abs(y) > 1e-9})
    for i, x in enumerate(xs):
        hy = next(y for hx, y in holes if hx == x)
        k = next(j for j, (hx, _) in enumerate(holes) if hx == x) + 1
        entities.append({"type": "dimension", "dimtype": "linear", "layer": "DIM", "p1": [0, 0], "p2": [x, hy],
                         "location": [x / 2, -20 - 7 * i], "angle": 0, "refs": ["outline", f"cv{k}"]})
    for i, y in enumerate(ys):
        hx = next(x for x, hy in holes if hy == y)
        k = next(j for j, (_, hy) in enumerate(holes) if hy == y) + 1
        entities.append({"type": "dimension", "dimtype": "linear", "layer": "DIM", "p1": [0, 0], "p2": [hx, y],
                         "location": [-20 - 7 * i, y / 2], "angle": 90, "refs": ["outline", f"ch{k}"]})
    if holes and d:
        n = len(holes)
        entities.append({"type": "dimension", "dimtype": "diameter", "layer": "DIM", "center": list(holes[0]), "radius": r, "angle": 45,
                         "text_override": f"{n}×Ø<>" if n > 1 else None, "refs": ["hole1"]})

    title: dict[str, str] = {}
    m = _TITLE.search(text)
    title["title"] = (m.group(1) or m.group(2)).strip() if m else (f"矩形板 {fmt_num(w)}×{fmt_num(h)}" if zh else f"Plate {fmt_num(w)}×{fmt_num(h)}")
    m = _NUMBER.search(text)
    if m:
        title["drawing_number"] = (m.group(1) or m.group(2)).strip()
    material = None
    mm = _MATERIAL.search(text)
    if mm and not _TBD.match((mm.group(1) or mm.group(2) or "")):
        material = (mm.group(1) or mm.group(2)).strip()
    elif _TBD.search(text) and re.search(r"材料|材质|material", text, re.I):
        material = "待定" if zh else "TBD"
    if material is None:
        if turbo:  # D-02: a concrete value with its basis, never "TBD"
            material = "Q235"
            assumptions.append({"subject": "material", "field": "material", "adopted": material, "trigger": "missing",
                                "basis": "the request names no material; Q235 carbon structural steel is the simulated rules' default "
                                         "for a plain plate (simulated)", "alternatives": ["6061-T6"], "affected": ["title_block.material"]})
        else:
            questions.append("材料是什么？（标题栏暂空）" if zh else "Which material? (left empty in the title block)")
    if material:
        title["material"] = material
        if material in ("待定", "TBD"):
            entities.append({"type": "text", "layer": "TEXT", "insert": [0, h + 12], "height": 3.5,
                             "text": "材料：待定" if zh else "MATERIAL: TBD"})

    for mc in _CONFLICT.finditer(text):
        other = float(mc.group(1))
        if abs(other - w) > 1e-9 and abs(other - h) > 1e-9:
            nearest = w if abs(other - w) <= abs(other - h) else h
            subject = ("外形宽度" if nearest == w else "外形高度") if zh else ("outline width" if nearest == w else "outline height")
            conflicts.append({"subject": subject, "values": [{"value": nearest, "quote": size.group(0)}, {"value": other, "quote": mc.group(0)}],
                              "note": ("两处读数不一致，图形按第一处绘制" if zh else "two readings disagree; the outline uses the first"),
                              "adopted": nearest if turbo else None})
    sheet = _sheet_for(w, h)
    k = 1.0 / sheet["scale"]
    origin = [round(w / 2 - sheet["pw"] * k * 0.4, 3), round(h / 2 - sheet["ph"] * k * 0.55, 3)]
    summary = (f"{note} 绘制 {fmt_num(w)}×{fmt_num(h)} 矩形板" + (f"、{len(holes)} 个 Ø{fmt_num(d or 0)} 孔" if holes else "") + "，含尺寸、中心线和标题栏。") \
        if zh else (f"{note} Drew a {fmt_num(w)}×{fmt_num(h)} plate" + (f" with {len(holes)} Ø{fmt_num(d or 0)} holes" if holes else "")
                    + ", dimensions, centre lines and a title block.")
    return {"summary": summary, "sheet": {"size": sheet["size"], "orientation": sheet["orientation"], "scale": sheet["scale"], "origin": origin},
            "title_block": title, "entities": [{k2: v for k2, v in e.items() if v is not None} for e in entities],
            "conflicts": conflicts, "assumptions": assumptions if turbo else [], "questions": questions}


def fmt_num(v: float) -> str:
    return drafting.fmt(v)


register_schema_responder(SCHEMA_NAME, simulated_drafting_responder)

__all__ = ["DraftingOutcome", "DraftingReply", "SCHEMA_NAME", "build_state", "run_drafting", "simulated_drafting_responder",
           "user_message"]
