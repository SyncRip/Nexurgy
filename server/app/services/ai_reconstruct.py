"""Drawing → STEP reconstruction (D3): an authorized DXF / DWG of the project → checked revision.

One core for every entry (W6 parity): the workflow node ``drawing_reconstruct`` (and so the AI
assistant operation ``drawing_reconstruct``, which runs it in a small graph) and the collaboration
tool ``drawing_reconstruct``. Flow (.trellis/tasks/archive/2026-10/10-08-cad-ai/design.md §0.5, §5, §6):

1. source: the file version (or a drawing's DXF) of the project; a DWG goes through the LibreDWG
   adapter into a derived DXF (stored blob + read report = provenance), a DXF is used as is;
2. evidence: ``extract_evidence`` (frames, dimensions, texts, circles; stable entity-handle ids);
3. sheet: one frame → bound; else the ``sheet`` parameter (frame id / drawing number), a drawing
   number in the notes, or the user's answer to the sheet question (``needs_clarification`` with
   every frame listed); no model call before the sheet is known;
4. units: every signal is collected (header $INSUNITS, $MEASUREMENT, notes on the sheet, the title
   block, the user's notes and answers); signals that disagree, or a header other than mm / inch that
   nothing corroborates (the ezdxf default metres), ask the user naming the sources; nothing → a units
   question (N01) - never assumed mm. The sheet, once bound, is frozen for the operation;
5. the modeling agent in reconstruct mode (services/ai_modeling.run_modeling(drawing=...)): the
   literal ``evidence_prompt`` of the frame, the server's per-view scale classification, the
   rendered sheet (when the model has vision), grounding against the evidence index, the
   critical-field gate plus the conflict gate (N03), build, independent check of the exported
   STEP, the same repair budget, ``unsupported`` for out-of-scope parts (N06);
6. success → revision (source ``ai``, ``source_file_version_id`` = the input file version,
   requirements, checks: conformance + ``source`` provenance + ``evidence`` frame / derived DXF sha).

Decision 5 (dimension text): the displayed number is the statement. Per view the server computes the
displayed ÷ measured ratio of every length dimension; the dominant ratio of the view is the one most
dimensions share (within 0.5 %). Dimensions matching it are consistent,
others conflicting. When a view has no unique dominant ratio (a tie), a dimension counts as
consistent only when its displayed number equals measurement × DIMLFAC (or × the title scale). A
requirement citing a conflicting dimension is not built: the user is asked which value is right,
naming both values; it is accepted once the requirement cites the user's answer to the question about
THAT dimension and uses the value the answer states (an unrelated answer never settles it).

Simulated mode (no usable model config, simulation on): the rule-based simulated model cannot read
a drawing, so nothing is faked: the run fails honestly with ``simulation_unsupported`` (labelled
simulated) after the deterministic source / evidence steps.
"""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from ..ai.modeling_agent import DrawingInput
from ..ai.providers.base import ImagePart, ModelConfig, ModelProvider
from ..errors import AppError, not_found
from ..models import Drawing, File, FileVersion, Part, User
from ..storage import read_blob
from . import ai_modeling
from . import drawing_input as din

SERVICE_VERSION = "reconstruct-service/1"
SIMULATION_UNSUPPORTED = "simulation_unsupported"
#: A view's own dominant ratio needs this many supporting dimensions (else the sheet ratio is used).
MIN_VIEW_SUPPORT = 2
RATIO_TOL = 0.005  # relative tolerance of the per-view displayed ÷ measured ratio (0.5 %)
#: Seconds for extracting and rendering one drawing (shared deadline).
EVIDENCE_BUDGET_S = 90.0
RENDER_PX = 2000
_CJK = re.compile(r"[㐀-鿿]")
_CJK_SPACE = re.compile(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿])")
# Unit declarations in English or Chinese, e.g. "units: mm" or "unit = inch".
_UNIT_DECL = re.compile(r"(?:单位|\bunits?\b)\s*(?:为|是|in)?\s*[:：=]?\s*(mm|毫米|millimet(?:er|re)s?|inch(?:es)?|英寸|in)(?![a-z])",
                        re.IGNORECASE)
_MM_WORD = re.compile(r"(?<![a-z])mm(?![a-z])|毫米|millimet(?:er|re)s?", re.IGNORECASE)
_INCH_WORD = re.compile(r"\binch(?:es)?\b|英寸|(?<![a-z])in\.?(?![a-z])", re.IGNORECASE)
UNITS_MARK = "$INSUNITS"


@dataclass
class ReconstructResult(ai_modeling.ModelingResult):
    """``ModelingResult`` plus the bound sheet and the source provenance (set once the input was read)."""

    frame_id: str | None = None
    source: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None


@dataclass
class DrawingSource:
    fmt: din.DrawingFormat
    name: str
    data: bytes
    dxf: bytes
    file_version_id: uuid.UUID | None = None
    drawing_id: uuid.UUID | None = None
    dwg: dict[str, Any] | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


def _step(kind: str, ok: bool = True, **detail: Any) -> dict[str, Any]:
    return {"kind": kind, "ok": ok, "at": datetime.now(UTC).isoformat(), "detail": detail}


def is_simulation_fallback(provider: ModelProvider, cfg: ModelConfig | None) -> bool:
    """The labelled simulated model (services.ai.provider_for returns no config only for it)."""
    return cfg is None and bool(provider.simulated)


# --- source -----------------------------------------------------------------------------------------


def load_source(db: Session, *, project_id: uuid.UUID, file_version_id: uuid.UUID | str | None = None,
                drawing_id: uuid.UUID | str | None = None) -> DrawingSource:
    """Read the drawing (authorized: it must belong to ``project_id``) and derive the DXF to analyse."""
    fv: FileVersion | None = None
    if file_version_id:
        fv = db.get(FileVersion, uuid.UUID(str(file_version_id)))
        f = db.get(File, fv.file_id) if fv else None
        if fv is None or f is None or f.project_id != project_id:
            raise not_found("file version")
    elif drawing_id:
        d = db.get(Drawing, uuid.UUID(str(drawing_id)))
        part = db.get(Part, d.part_id) if d else None
        if d is None or part is None or part.project_id != project_id:
            raise not_found("drawing")
        if d.file_version_id:
            return load_source(db, project_id=project_id, file_version_id=d.file_version_id)
        if not d.dxf_sha:
            raise AppError(422, "drawing_has_no_dxf", "this drawing has no DXF file to reconstruct from", {"drawing_id": str(d.id)})
        data, _ = read_blob(db, d.dxf_sha)  # a generated drawing package: its first (overview) page
        src = DrawingSource(fmt=din.drawing_format("drawing.dxf", data), name=f"drawing-{str(d.id)[:8]}-page1.dxf", data=data,
                            dxf=data, drawing_id=d.id)
        src.provenance = din.provenance(fmt="dxf", name=src.name, data=data, dxf=data, dwg=None, drawing_id=d.id)
        return src
    else:
        raise AppError(422, "drawing_required", "choose the DXF or DWG drawing file to reconstruct")
    assert fv is not None
    data, _ = read_blob(db, fv.blob_sha256)
    fmt = din.drawing_format(fv.original_name, data)
    dxf, dwg = (din.dwg_to_dxf(db, data) if fmt == "dwg" else (data, None))
    src = DrawingSource(fmt=fmt, name=fv.original_name, data=data, dxf=dxf, file_version_id=fv.id, dwg=dwg)
    src.provenance = din.provenance(fmt=fmt, name=fv.original_name, data=data, dxf=dxf, dwg=dwg, file_version_id=fv.id)
    return src


# --- sheet --------------------------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def frame_name(frame: Any) -> str:
    """Last line of the title name, CJK letter spacing removed (for example, collapsing spaces between the characters of a Chinese cover name)."""
    raw = frame.title.name.value if frame.title.name else ""
    line = [ln for ln in raw.splitlines() if ln.strip()][-1:] or [""]
    return _CJK_SPACE.sub("", " ".join(line[0].split()))


def frame_number(frame: Any) -> str:
    return " ".join((frame.title.number.value if frame.title.number else "").split())


def frame_label(frame: Any) -> str:
    bits = [frame.id, frame_number(frame), frame_name(frame)]
    return " ".join(b for b in bits if b)


def _token_in(token: str, text: str) -> bool:
    """``token`` occurs in ``text`` (both normalised) not glued to other ASCII letters / digits / . - _ /."""
    if not token:
        return False
    for m in re.finditer(re.escape(token), text):
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        nxt = text[m.end() + 1] if m.end() + 1 < len(text) else ""
        if before and ((before.isascii() and before.isalnum()) or before in "._-/"):
            continue
        if after and ((after.isascii() and after.isalnum()) or after in "_-/" or (after == "." and nxt.isdigit())):
            continue
        return True
    return False


def match_frame(frames: list[Any], text: str, *, by_id: bool = True, by_name: bool = True) -> Any | None:
    """The one frame ``text`` names by frame id (S29), drawing number (D07.07-01) or title name, else None."""
    t = _norm(text)
    if not t:
        return None
    exact = [f for f in frames if t in {_norm(f.id), _norm(frame_number(f)), _norm(frame_name(f))} - {""}]
    if len(exact) == 1:
        return exact[0]
    if by_id:
        hits = [f for f in frames if _token_in(_norm(f.id), t)]
        if len(hits) == 1:
            return hits[0]
    hits = [f for f in frames if len(_norm(frame_number(f))) >= 3 and _token_in(_norm(frame_number(f)), t)]
    if len(hits) == 1:
        return hits[0]
    if by_name:
        hits = [f for f in frames if len(_norm(frame_name(f))) >= 2 and _norm(frame_name(f)) in t]
        names = {_norm(frame_name(f)) for f in hits}
        hits = [f for f in hits if not any(_norm(frame_name(f)) != n and _norm(frame_name(f)) in n for n in names)]
        if len(hits) == 1:
            return hits[0]
    return None


#: Marks of the server's sheet question (zh / en): only an answer to THAT question binds a sheet.
SHEET_QUESTION_MARKS = ("请选择要重建的那一张", "which one should be reconstructed")


def is_sheet_question(question: str | None) -> bool:
    q = " ".join(str(question or "").split())
    return any(m in q for m in SHEET_QUESTION_MARKS)


def resolve_sheet(evidence: Any, *, sheet: str | None, notes: str, answers: list[dict[str, str]],
                  frozen: str | None = None) -> tuple[str | None, str, str | None]:
    """``(frame_id, how, problem)``: how = frozen | only_frame | parameter | answer | notes | ask; problem names an
    unmatched ``sheet`` parameter.

    Once bound, the sheet is frozen for the operation: ``frozen`` (the frame recorded in the node outputs of an
    earlier attempt) wins, and only an answer to the sheet question itself can bind one (the first that names a
    frame) — a later answer to another question that mentions another frame or drawing number never switches it."""
    frames = list(evidence.frames)
    if frozen and any(f.id == frozen for f in frames):
        return frozen, "frozen", None
    if len(frames) == 1:
        return frames[0].id, "only_frame", None
    problem = None
    if sheet and sheet.strip():
        f = match_frame(frames, sheet)
        if f is not None:
            return f.id, "parameter", None
        problem = sheet.strip()
    for a in answers:
        if not is_sheet_question(a.get("question")):
            continue
        f = match_frame(frames, str(a.get("answer") or ""))
        if f is not None:
            return f.id, "answer", None
    if notes and problem is None:
        f = match_frame(frames, notes, by_id=False, by_name=False)  # only an explicit drawing number in the notes binds
        if f is not None:
            return f.id, "notes", None
    return None, "ask", problem


def sheet_question(evidence: Any, lang: str, problem: str | None = None) -> str:
    items = "；".join(frame_label(f) for f in evidence.frames) if lang == "zh" else "; ".join(frame_label(f) for f in evidence.frames)
    if lang == "zh":
        head = f"未找到图纸“{problem}”。" if problem else ""
        return f"{head}这个文件里有 {len(evidence.frames)} 个图框，请选择要重建的那一张（回答图框编号或图号）：{items}"
    head = f"No sheet matches {problem!r}. " if problem else ""
    return f"{head}The file holds {len(evidence.frames)} sheets; which one should be reconstructed (answer the sheet id or drawing number)? {items}"


# --- units --------------------------------------------------------------------------------------------


def _stated_unit(text: str, *, loose: bool) -> str | None:
    found = set()
    for m in _UNIT_DECL.finditer(text or ""):
        found.add("mm" if _MM_WORD.fullmatch(m.group(1)) else "inch")
    if loose and not found:
        if _MM_WORD.search(text or ""):
            found.add("mm")
        if _INCH_WORD.search(text or ""):
            found.add("inch")
    return found.pop() if len(found) == 1 else None


_IMPERIAL_MM = (25.4, 304.8, 1609344.0, 25.4e-6, 0.0254, 914.4)
_UNIT_KEYS = {1.0: "mm", 25.4: "inch", 10.0: "cm", 1000.0: "m", 304.8: "ft"}
#: Header units trusted without a second signal. Anything else (metres: the ezdxf template default 6, centimetres,
#: feet, …) is believed only when a sheet note, the paper size of the frame or the user corroborates it.
TRUSTED_HEADER_UNITS = ("mm", "inch")


def _unit_key(mm_per_unit: float, name: str | None) -> str:
    return _UNIT_KEYS.get(float(mm_per_unit), name or f"{mm_per_unit:g} mm")


def _family(unit: str) -> str:
    return "imperial" if unit in ("inch", "ft") or unit in ("miles", "microinches", "mils", "yards") else "metric"


def _units_of(unit: str, source: str, **extra: Any) -> dict[str, Any]:
    mm = {"mm": 1.0, "inch": 25.4, "cm": 10.0, "m": 1000.0, "ft": 304.8}.get(unit)
    return {"unit": unit, "mm_per_unit": mm, "source": source, **extra}


def unit_signals(evidence: Any, frame_id: str | None, *, notes: str, answers: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Every statement of the length unit: the file header ($INSUNITS), $MEASUREMENT (metric / imperial only), notes on
    the bound sheet, the title block, the user's notes and answers; and the sheet border's paper size as a
    corroboration of the header (``paper``)."""
    u = evidence.units
    out: list[dict[str, Any]] = []
    if u.mm_per_unit:
        out.append({"source": "header", "unit": _unit_key(u.mm_per_unit, u.insunits_name), "insunits": u.insunits})
    if u.measurement in (0, 1):
        out.append({"source": "measurement", "family": "imperial" if u.measurement == 0 else "metric", "measurement": u.measurement})
    if frame_id is not None:
        frame = evidence.frame(frame_id)
        tb = set(frame.title_block_text_ids)
        for t in evidence.texts:
            if t.frame_id != frame_id or not t.text:
                continue
            in_tb = t.id in tb
            unit = _stated_unit(t.text, loose=in_tb and len(t.text.strip()) <= 16)
            if unit:
                out.append({"source": "title_block" if in_tb else "drawing_text", "unit": unit, "text_id": t.id,
                            "text": t.text.strip()[:80]})
        if frame.kind == "paper_border" and frame.paper and u.mm_per_unit:
            out.append({"source": "paper", "unit": _unit_key(u.mm_per_unit, u.insunits_name), "paper": frame.paper,
                        "paper_factor": frame.paper_factor})
    for i, a in enumerate(answers):
        q = str(a.get("question") or "")
        unit = _stated_unit(str(a.get("answer") or ""), loose=UNITS_MARK in q)
        if unit:
            out.append({"source": "clarification", "unit": unit, "answer": i, "units_question": UNITS_MARK in q})
    unit = _stated_unit(notes, loose=False)
    if unit:
        out.append({"source": "request", "unit": unit})
    return out


def units_resolution(evidence: Any, frame_id: str | None, *, notes: str, answers: list[dict[str, str]]) -> dict[str, Any]:
    """``{"units": dict | None, "status": ok | conflict | untrusted_header | unknown, "signals", "conflict"}``.

    - The user's answer to the units question settles it (that question named every source).
    - Signals that disagree (two units, or a unit against $MEASUREMENT's metric / imperial) → ``conflict``: ask,
      naming the sources; the header never silently overrides a sheet note, the title block or the user.
    - The header alone is trusted only for mm / inch; another unit (metres: the ezdxf default) needs a corroborating
      note, the paper size of the frame or the user → otherwise ``untrusted_header``: ask.
    - Nothing states a unit → ``unknown``: ask (never assumed mm)."""
    signals = unit_signals(evidence, frame_id, notes=notes, answers=answers)
    u = evidence.units
    settled = [s for s in signals if s["source"] == "clarification" and s.get("units_question")]
    if settled:
        a = answers[settled[-1]["answer"]]
        return {"status": "ok", "signals": signals, "conflict": None,
                "units": _units_of(settled[-1]["unit"], "clarification", quote=str(a.get("answer") or "")[:200], insunits=u.insunits)}
    stated = [s for s in signals if "unit" in s and s["source"] != "paper"]
    units = {s["unit"] for s in stated}
    families = {s["family"] for s in signals if s["source"] == "measurement"}
    if len(units) > 1 or (families and any(_family(x) not in families for x in units)):
        return {"status": "conflict", "signals": signals, "conflict": [s for s in signals if s["source"] != "paper"], "units": None}
    header = next((s for s in signals if s["source"] == "header"), None)
    if header is not None:
        others = [s for s in signals if s is not header and s.get("unit") == header["unit"]]
        if header["unit"] in TRUSTED_HEADER_UNITS or others:
            out = _units_of(header["unit"], "header", insunits=u.insunits)
            out["mm_per_unit"] = u.mm_per_unit
            out["unit_name"] = u.insunits_name
            if others:
                out["corroborated_by"] = [o["source"] for o in others]
            return {"status": "ok", "signals": signals, "conflict": None, "units": out}
        return {"status": "untrusted_header", "signals": signals, "conflict": None, "units": None}
    for src in ("drawing_text", "title_block", "clarification", "request"):
        s = next((x for x in stated if x["source"] == src), None)
        if s is None:
            continue
        extra: dict[str, Any] = {"insunits": u.insunits}
        if src in ("drawing_text", "title_block"):
            extra["text_id"] = s["text_id"]
        elif src == "clarification":
            extra["quote"] = str(answers[s["answer"]].get("answer") or "")[:200]
        return {"status": "ok", "signals": signals, "conflict": None, "units": _units_of(s["unit"], src, **extra)}
    return {"status": "unknown", "signals": signals, "conflict": None, "units": None}


def resolve_units(evidence: Any, frame_id: str | None, *, notes: str, answers: list[dict[str, str]]) -> dict[str, Any] | None:
    """How lengths on the sheet are to be read, or None when the unit is unknown, conflicting or only an untrusted
    header states it (never assumed mm). See :func:`units_resolution`."""
    return units_resolution(evidence, frame_id, notes=notes, answers=answers)["units"]


def _signal_label(s: dict[str, Any], lang: str) -> str:
    src = s["source"]
    zh = lang == "zh"
    if src == "header":
        return f"{'文件头' if zh else 'file header'} {UNITS_MARK}={s.get('insunits')} ({s['unit']})"
    if src == "measurement":
        return f"$MEASUREMENT={s.get('measurement')} ({'公制' if zh and s['family'] == 'metric' else '英制' if zh else s['family']})"
    if src in ("drawing_text", "title_block"):
        where = ("图上文字" if src == "drawing_text" else "标题栏") if zh else ("sheet note" if src == "drawing_text" else "title block")
        return f"{where} {s.get('text_id')} “{s.get('text')}” ({s['unit']})"
    if src == "clarification":
        return f"{'你的回答' if zh else 'your answer'} ({s['unit']})"
    return f"{'你的说明' if zh else 'your notes'} ({s['unit']})"


def units_question(evidence: Any, lang: str, resolution: dict[str, Any] | None = None) -> str:
    n = evidence.units.insunits
    status = (resolution or {}).get("status")
    if status == "conflict":
        labels = [_signal_label(s, lang) for s in resolution["conflict"]]
        if not any(s["source"] == "header" for s in resolution["conflict"]):  # the answer is read as a units answer
            labels.append(f"{UNITS_MARK}={n if n is not None else '-'}")
        names = ("；" if lang == "zh" else "; ").join(labels)
        if lang == "zh":
            return f"图纸的长度单位说法不一致：{names}。图中尺寸数字的单位到底是什么（mm 或 inch）？"
        return f"The drawing's length unit is stated inconsistently: {names}. In which unit are the dimension numbers written (mm or inch)?"
    if status == "untrusted_header":
        name = evidence.units.insunits_name or "?"
        if lang == "zh":
            return (f"文件头 {UNITS_MARK}={n}（{name}）是唯一的单位依据，图上没有单位说明，图框也不是该单位下的标准图幅"
                    "（很多 CAD 文件带着默认的 meters）。图中尺寸数字的单位是什么（mm 或 inch）？")
        return (f"The only unit statement is the file header {UNITS_MARK}={n} ({name}); no note states a unit and the sheet border is "
                "no standard paper size in that unit (many CAD files carry the default meters). In which unit are the dimension "
                "numbers written (mm or inch)?")
    if lang == "zh":
        return (f"图纸文件没有声明长度单位（{UNITS_MARK}={n if n is not None else '缺失'}），图上也没有单位说明。"
                "图中尺寸数字的单位是什么（mm 或 inch）？")
    return (f"The drawing file declares no length unit ({UNITS_MARK}={n if n is not None else 'missing'}) and no note states one. "
            "In which unit are the dimension numbers written (mm or inch)?")


def _index_scale_note(evidence: Any, units: dict[str, Any]) -> list[str]:
    """When the decided unit overrides the header, the index's coordinates (converted with the header) are off by a
    factor: say so, so the model reads positions from the displayed dimensions only."""
    hdr, mm = evidence.units.mm_per_unit, units.get("mm_per_unit")
    if not hdr or not mm or abs(hdr - mm) <= 1e-9 * max(hdr, mm):
        return []
    return [f"note: the evidence index converts coordinates with the file header ({hdr:g} mm per drawing unit), which the "
            f"units above override: index coordinates are {hdr / mm:g}× the real size; take sizes from the displayed dimensions"]


def units_line(units: dict[str, Any]) -> str:
    src = units["source"]
    if src == "header":
        where = f"file header $INSUNITS={units.get('insunits')} ({units.get('unit_name') or units.get('unit')})"
        if units.get("corroborated_by"):
            where += ", corroborated by " + ", ".join(units["corroborated_by"])
    elif src == "drawing_text":
        where = f"note on the sheet, text {units.get('text_id')}"
    elif src == "title_block":
        where = f"title block, text {units.get('text_id')}"
    elif src == "clarification":
        where = "the user's answer"
    else:
        where = "the user's request"
    unit = "millimetres" if units.get("mm_per_unit") == 1.0 else f"{units.get('unit')} (1 drawing unit = {units.get('mm_per_unit'):g} mm)"
    conv = "" if units.get("mm_per_unit") == 1.0 else "; convert every length to mm, list converted fields in derived"
    return f"units: {unit}, from {where}{conv}"


# --- per-view scale classification (N03) -------------------------------------------------------------


def _fmt(v: float | None) -> str:
    if v is None:
        return "?"
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _numbers(d: Any) -> list[float]:
    """Positive numbers of the displayed text (tolerance parts excluded by the extractor)."""
    return [float(n) for n in d.numbers if float(n) > 0]


def _drawing_measurement(d: Any) -> float | None:
    """Graphical length for view ratios; legacy evidence has only the native definition-point measurement."""
    return (getattr(d, "drawing_measurement", None) if getattr(d, "drawing_transform", None) is not None
            else d.measurement)


def _matches(d: Any, ratio: float) -> bool:
    m = float(_drawing_measurement(d))
    return any(abs(n / m - ratio) <= RATIO_TOL * ratio for n in _numbers(d))


def _best_ratio(d: Any, target: float | None) -> float | None:
    rs = [n / float(_drawing_measurement(d)) for n in _numbers(d)]
    if not rs:
        return None
    return min(rs, key=lambda r: abs(r - target)) if target else rs[-1]


def classify_dimensions(evidence: Any, frame_id: str) -> dict[str, Any]:
    """Per-view displayed ÷ measured classification of the frame's dimensions (see the module docstring).

    A view's dominant ratio needs the support of at least two dimensions. A view with a single classifiable dimension
    cannot vouch for itself (that dimension would always agree with its own ratio): it is judged against the sheet's
    dominant ratio (the ratio of the best-supported view), so a lone overridden thickness in a side view is still a
    conflict."""
    dims = [d for d in evidence.dimensions if d.frame_id == frame_id]
    groups: dict[str, list[Any]] = {}
    for d in dims:
        groups.setdefault(d.view_id or f"{frame_id}.-", []).append(d)
    out: dict[str, dict[str, Any]] = {}
    stats: list[dict[str, Any]] = []
    for view, ds in groups.items():
        eligible = [d for d in ds if d.measurement_unit == "length" and d.dimlfac_applies and _drawing_measurement(d)
                    and float(_drawing_measurement(d)) > 1e-9 and _numbers(d)]
        for d in ds:
            if d not in eligible:
                why = ("angular" if d.measurement_unit != "length" else "no graphical measurement" if not _drawing_measurement(d)
                       else "no displayed number")
                out[d.id] = {"id": d.id, "view": view, "status": "not_classified", "reason": why, "displayed": d.displayed}
        cands = sorted({round(n / float(_drawing_measurement(d)), 9) for d in eligible for n in _numbers(d)})
        support = {r: frozenset(d.id for d in eligible if _matches(d, r)) for r in cands}
        best = max((len(sp) for sp in support.values()), default=0)
        tops = [r for r in cands if len(support[r]) == best]
        dominant: float | None = None
        tie: list[float] = []
        if tops and len({support[r] for r in tops}) == 1:
            dominant = tops[len(tops) // 2]
        elif tops:
            tie = tops
        stats.append({"view": view, "ds": ds, "eligible": eligible, "dominant": dominant, "tie": tie, "best": best})
    supported = [st for st in stats if st["dominant"] is not None and st["best"] >= MIN_VIEW_SUPPORT]
    sheet = max(supported, key=lambda st: st["best"], default=None)
    sheet_ratio = sheet["dominant"] if sheet is not None else None
    views: list[dict[str, Any]] = []
    for st in stats:
        view, ds, eligible = st["view"], st["ds"], st["eligible"]
        dominant, tie, best, source = st["dominant"], st["tie"], st["best"], "view"
        if dominant is not None and best < MIN_VIEW_SUPPORT:
            dominant, tie, source = sheet_ratio, [], "sheet" if sheet_ratio is not None else "none"
        conflicts = []
        for d in eligible:
            if dominant is not None:
                ok = _matches(d, dominant)
            else:
                ok = d.consistent is True or d.consistent_with_sheet_scale is True
            ratio = _best_ratio(d, dominant)
            info = {"id": d.id, "view": view, "status": "consistent" if ok else "conflict", "displayed": d.displayed,
                    "measurement": d.measurement, "dimlfac": d.dimlfac, "value": d.value,
                    "drawing_measurement": _drawing_measurement(d),
                    "ratio": round(ratio, 4) if ratio is not None else None,
                    "dominant_ratio": round(dominant, 4) if dominant is not None else None,
                    "dominant_source": source if dominant is not None else None,
                    "expected": round(float(_drawing_measurement(d)) * dominant, 4) if dominant is not None else None}
            out[d.id] = info
            if not ok:
                conflicts.append(d.id)
        views.append({"view": view, "dimensions": len(ds), "classified": len(eligible),
                      "dominant_ratio": round(dominant, 4) if dominant is not None else None,
                      "dominant_source": source if dominant is not None else None,
                      "support": best if dominant is not None else None,
                      "tie": [round(r, 4) for r in tie] if tie else None, "conflicts": conflicts})
    return {"method": "per-view dominant displayed/measured ratio (single-dimension views: the sheet's ratio)",
            "tolerance": {"relative": RATIO_TOL}, "views": views, "dims": out,
            "conflicts": [i for v in views for i in v["conflicts"]]}


def classification_text(cls: dict[str, Any]) -> str:
    lines = ["VIEW SCALE CLASSIFICATION (server: per view, displayed number ÷ measured length; a dimension matches its view's "
             "dominant ratio within 0.5 %; CONFLICT = it does not)"]
    for v in cls["views"]:
        name = v["view"].split(".")[-1]
        if v["dominant_ratio"] is not None and v.get("dominant_source") == "sheet":
            lines.append(f" {name}: too few dimensions for its own ratio; judged against the sheet ratio {_fmt(v['dominant_ratio'])}")
        elif v["dominant_ratio"] is not None:
            lines.append(f" {name}: dominant ratio {_fmt(v['dominant_ratio'])} ({v['support']} of {v['classified']} dimensions)")
        elif v["tie"]:
            lines.append(f" {name}: no dominant ratio (tie: {', '.join(_fmt(r) for r in v['tie'])}); a dimension is consistent only "
                         "when its displayed number equals measurement × DIMLFAC or × the title scale")
        else:
            lines.append(f" {name}: no length dimension to classify")
        for info in [i for i in cls["dims"].values() if i["view"] == v["view"]]:
            if info["status"] == "not_classified":
                lines.append(f"  {info['id']} {info['displayed']!r} not classified ({info['reason']})")
                continue
            extra = ""
            if info["status"] == "conflict":
                extra = (f" (measured {_fmt(info.get('drawing_measurement', info['measurement']))} × view ratio {_fmt(info['dominant_ratio'])} = {_fmt(info['expected'])})"
                         if info["dominant_ratio"] is not None else
                         f" (measured {_fmt(info['measurement'])} × DIMLFAC {_fmt(info['dimlfac'])} = {_fmt(info['value'])})")
            lines.append(f"  {info['id']} {info['displayed']!r} ratio {_fmt(info['ratio'])} "
                         f"{'CONFLICT' if info['status'] == 'conflict' else 'consistent'}{extra}")
    if cls["conflicts"]:
        lines.append("CONFLICT dimensions: " + ", ".join(cls["conflicts"]) + ". Cite one only together with the user's answer to "
                     "the question about THAT dimension (source clarification, its exact words) and use the value the answer "
                     "states; otherwise the server asks the user.")
    return "\n".join(lines)


def conflict_question_id(dim_id: str) -> str:
    """Stable id of the conflict question about one dimension (kept with the gap in the node outputs)."""
    return f"conflict:{dim_id}"


def _conflict_heads(info: dict[str, Any]) -> tuple[str, str]:
    """The opening of the conflict question about one dimension (zh, en): identifies an answer to THAT question."""
    view = str(info["view"]).split(".")[-1]
    disp = info["displayed"]
    return f"视图 {view} 中尺寸 {info['id']} 显示“{disp}”", f"Dimension {info['id']} in view {view} displays \"{disp}\""


def conflict_question(info: dict[str, Any], req_ids: list[str], lang: str) -> str:
    zh_head, en_head = _conflict_heads(info)
    rids = ", ".join(req_ids)
    if info.get("dominant_ratio") is not None:
        other = _fmt(info["expected"])
        measured = _fmt(info.get("drawing_measurement", info["measurement"]))
        if lang == "zh":
            return (f"{zh_head}，但它的图形测量 {measured} × 该视图主导比例 "
                    f"{_fmt(info['dominant_ratio'])} = {other}（测量 × DIMLFAC = {_fmt(info['value'])}），两者不一致；{rids} 用到了它。"
                    f"应以显示的“{info['displayed']}”为准，还是以 {other} 为准？")
        return (f"{en_head}, but its measured length {measured} × the "
                f"view's dominant ratio {_fmt(info['dominant_ratio'])} = {other} (measurement × DIMLFAC = {_fmt(info['value'])}); "
                f"{rids} use it. Is the displayed \"{info['displayed']}\" or {other} correct?")
    other = _fmt(info["value"])
    if lang == "zh":
        return (f"{zh_head}，但它的图形测量 {_fmt(info['measurement'])} × DIMLFAC "
                f"{_fmt(info['dimlfac'])} = {other}，与显示值不一致，且该视图各尺寸的显示/测量比例不统一；{rids} 用到了它。"
                f"应以显示的“{info['displayed']}”为准，还是以 {other} 为准？")
    return (f"{en_head}, but its measured length {_fmt(info['measurement'])} × DIMLFAC "
            f"{_fmt(info['dimlfac'])} = {other}, and the dimensions of this view share no common scale; {rids} use it. "
            f"Is the displayed \"{info['displayed']}\" or {other} correct?")


#: An answer saying the displayed value governs ("use the displayed value", "as annotated", "the displayed value is correct").
_DISPLAYED_GOVERNS = re.compile(
    r"以\s*(?:显示|标注)(?:的)?\s*(?:值|尺寸|数值|数字)?\s*为准|按\s*(?:显示|标注)(?:的)?\s*(?:值|尺寸|数值)?|"
    r"(?:显示|标注)(?:的)?\s*(?:值|尺寸|数值)?\s*(?:是)?\s*(?:对|正确)|"
    r"display(?:ed)?\s+(?:value\s+|text\s+|number\s+)?(?:governs|wins|is\s+(?:right|correct))|(?:use|keep)\s+the\s+display(?:ed)?",
    re.IGNORECASE)


def conflict_answers(answers: list[dict[str, str]], info: dict[str, Any]) -> list[str]:
    """The user's answers to the conflict question about THIS dimension (matched by the question's opening)."""
    heads = tuple(" ".join(h.split()) for h in _conflict_heads(info))
    return [str(a.get("answer") or "") for a in answers
            if isinstance(a, dict) and " ".join(str(a.get("question") or "").split()).startswith(heads) and str(a.get("answer") or "").strip()]


def _settled_by(req: Any, info: dict[str, Any], answered: list[str]) -> bool:
    """The requirement quotes an answer to this dimension's question, and that answer states the value it uses (a
    number of the requirement) or says the displayed value governs (and the requirement uses the displayed number)."""
    from ..cad.requirements import extract_numbers, number_in, quote_in, requirement_numbers

    quotes = [ev.ref for ev in req.evidence if ev.source == "clarification"]
    used = [v for v in requirement_numbers(req) if abs(v) > 1e-12]
    for text in answered:
        if not any(quote_in(q, [text]) for q in quotes):
            continue
        if any(number_in(v, [n for n in extract_numbers(text) if abs(n) > 1e-12]) for v in used):
            return True
        displayed = [n for n in extract_numbers(str(info.get("displayed") or "")) if abs(n) > 1e-12]
        if _DISPLAYED_GOVERNS.search(text) and any(number_in(v, displayed) for v in used):
            return True
    return False


def make_conflict_gate(cls: dict[str, Any], lang: str, answers: list[dict[str, str]] | None = None):
    """Server gate (N03): a requirement citing a conflicting dimension is accepted only with the user's answer to the
    question about THAT dimension — quoted as clarification evidence, and stating the value the requirement uses (or
    that the displayed value governs). No answer yet → a gap with the question (``question_id`` =
    :func:`conflict_question_id`, kept with the gap in the node outputs); an answer the reply did not apply → a
    ``repair`` gap (the modeling agent turns it into a validation problem, it never asks the same question again).
    ``answers`` are the ``{"question", "answer"}`` pairs of the run; a bare answer text never settles a conflict."""
    conflicts = {i: info for i, info in cls["dims"].items() if info["status"] == "conflict"}
    pairs = [a for a in answers or [] if isinstance(a, dict)]

    def gate(reqs: list[Any], answer_texts: list[str]) -> list[dict[str, Any]]:
        if not conflicts:
            return []
        by_dim: dict[str, list[Any]] = {}
        for r in reqs:
            for ev in r.evidence:
                if ev.source == "drawing_dimension" and ev.ref in conflicts and r not in by_dim.setdefault(ev.ref, []):
                    by_dim[ev.ref].append(r)
        gaps: list[dict[str, Any]] = []
        for dim_id, reqs_of in by_dim.items():
            info = conflicts[dim_id]
            answered = conflict_answers(pairs, info)
            base = {"field": "evidence", "evidence_id": dim_id, "view": info["view"], "displayed": info["displayed"],
                    "measurement": info["measurement"], "dimlfac": info["dimlfac"], "native_value": info["value"],
                    "drawing_measurement": info.get("drawing_measurement", info["measurement"]),
                    "view_ratio": info.get("dominant_ratio"), "expected_by_view": info.get("expected"),
                    "question_id": conflict_question_id(dim_id)}
            if not answered:
                q = conflict_question(info, [r.id for r in reqs_of], lang)
                gaps += [{"requirement_id": r.id, "kind": r.kind, "label": r.label, "reason": "conflict", **base, "question": q}
                         for r in reqs_of]
                continue
            for r in reqs_of:
                if not _settled_by(r, info, answered):
                    gaps.append({"requirement_id": r.id, "kind": r.kind, "label": r.label, "reason": "answer_not_applied", **base,
                                 "repair": True,
                                 "problem": f"{r.id} cites the conflicting dimension {dim_id}; the user answered the question "
                                            "about it: cite that answer (source clarification, its exact words) and use the "
                                            "value it states"})
        return gaps

    return gate


# --- the run -------------------------------------------------------------------------------------------


def frozen_sheet(db: Session, node_run_id: uuid.UUID | None) -> str | None:
    """The frame an earlier attempt of this ``drawing_reconstruct`` node bound (its outputs ``reconstruct.frame_id``,
    written while it waited for an answer), or None. Freezes the sheet for the operation."""
    if node_run_id is None:
        return None
    from ..models import NodeRun

    nr = db.get(NodeRun, node_run_id)
    if nr is None or nr.node_type != "drawing_reconstruct":
        return None
    rec = (nr.outputs or {}).get("reconstruct") if isinstance(nr.outputs, dict) else None
    fid = rec.get("frame_id") if isinstance(rec, dict) else None
    return str(fid) if fid else None


def _lang(notes: str, evidence: Any, frame_id: str | None) -> Literal["zh", "en"]:
    if _CJK.search(notes or ""):
        return "zh"
    if notes and notes.strip():
        return "en"
    texts = [t.text for t in evidence.texts if frame_id is None or t.frame_id == frame_id]
    return "zh" if any(_CJK.search(t or "") for t in texts) else "en"


def drawing_values(evidence: Any, frame_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Citable ids of the frame: dimension id → displayed text, text id → text; and their kinds."""
    values: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for d in evidence.dimensions:
        if d.frame_id == frame_id and d.displayed.strip():
            values[d.id], kinds[d.id] = d.displayed, "dimension"
    for t in evidence.texts:
        if t.frame_id == frame_id and t.text.strip():
            values[t.id], kinds[t.id] = t.text, "text"
    return values, kinds


def evidence_record(evidence: Any, frame: Any, src: DrawingSource, units: dict[str, Any], cls: dict[str, Any],
                    image: bool) -> dict[str, Any]:
    """``revision.checks["evidence"]``: what the reconstruction read, bound to the exact DXF bytes."""
    from ..ai import prompts

    return {
        "schema": evidence.schema_version, "extractor_version": evidence.extractor_version, "service_version": SERVICE_VERSION,
        "prompt_version": prompts.RECONSTRUCT_PROMPT_VERSION, "dxf_sha256": evidence.source_sha256, "frame_id": frame.id,
        "frame": {"number": frame_number(frame), "name": frame_name(frame),
                  "scale": frame.title.scale.value if frame.title.scale else None, "paper": frame.paper,
                  "orientation": frame.orientation},
        "units": units, "image_attached": image, "dxf_role": src.provenance.get("dxf_role"),
        "scale_classification": {"views": cls["views"], "conflicts": cls["conflicts"]},
    }


def run_reconstruct(
    db: Session,
    *,
    provider: ModelProvider,
    cfg: ModelConfig | None,
    part: Part,
    user: User,
    file_version_id: uuid.UUID | str | None = None,
    drawing_id: uuid.UUID | str | None = None,
    sheet: str | None = None,
    notes: str = "",
    answers: list[dict[str, str]] | None = None,
    context: str | None = None,
    private: dict[str, Any] | None = None,
    node_run_id: uuid.UUID | None = None,
    policy: str | None = None,
) -> ReconstructResult:
    """Reconstruct the part on one sheet of the drawing (see the module docstring). Runs inside the caller's
    transaction and never raises for drawing / model problems: they come back as ``status`` + ``error``
    (``kind`` source | provider | outcome | service); the caller records ``result.calls``.

    ``policy`` = turbo (W7): an ambiguous sheet or an unknown / conflicting unit is adopted (first frame; the header
    unit, else the first stated one, else mm) and recorded as an assumption instead of a question; the dimension
    conflict gate and the checks are skipped inside ``run_modeling``. The drawing must still be readable."""
    from ..cad.drawing_evidence import DrawingEvidenceError, evidence_prompt, extract_evidence, render_frame_png
    from .. import policy as exec_policy

    turbo = exec_policy.is_turbo(policy)
    adopted: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    res = ReconstructResult(status="failed", label=ai_modeling.label_of(provider, cfg), steps=steps)
    answers = [a for a in answers or [] if isinstance(a, dict) and str(a.get("answer") or "").strip()]

    def fail(kind: str, code: str, message: str, details: dict[str, Any] | None = None) -> ReconstructResult:
        res.status = "failed"
        res.error = {"kind": kind, "code": code, "message": message, "retryable": False, "details": details or {}}
        return res

    try:
        src = load_source(db, project_id=part.project_id, file_version_id=file_version_id, drawing_id=drawing_id)
    except AppError as exc:
        steps.append(_step("drawing_source", ok=False, code=exc.code, message=exc.message))
        return fail("source", exc.code, exc.message, dict(exc.details or {}))
    res.source = src.provenance
    steps.append(_step("drawing_source", **{k: v for k, v in src.provenance.items() if k != "dwg"},
                       dwg=None if src.dwg is None else {k: src.dwg.get(k) for k in ("source", "version_status", "converter",
                                                                                     "derived_dxf", "load_mode", "quarantined",
                                                                                     "seconds", "losses", "steps")}))
    # One wall-clock budget for reading the drawing (extraction + the vision render), so a hostile file cannot hold
    # the node; the render is optional (the literal index alone still works), the extraction is not.
    deadline = time.monotonic() + EVIDENCE_BUDGET_S
    try:
        evidence = extract_evidence(src.dxf, deadline=deadline)
    except DrawingEvidenceError as exc:
        steps.append(_step("evidence", ok=False, code=exc.code, message=exc.message))
        return fail("source", exc.code, exc.message)
    steps.append(_step("evidence", extractor_version=evidence.extractor_version, dxf_sha256=evidence.source_sha256,
                       load_mode=evidence.load_mode, units=evidence.units.model_dump(mode="json"), frames=len(evidence.frames),
                       dimensions=len(evidence.dimensions), texts=len(evidence.texts), quarantined=len(evidence.quarantined),
                       warnings=[w.code for w in evidence.warnings][:20]))
    if is_simulation_fallback(provider, cfg):
        steps.append(_step("model", ok=False, simulated=True, reason=SIMULATION_UNSUPPORTED))
        return fail("outcome", SIMULATION_UNSUPPORTED,
                    "drawing reconstruction needs a configured AI model: the simulated mode cannot read a drawing, so nothing "
                    "was built (configure a model for modeling)", {"simulated": True})
    if not evidence.frames:
        return fail("source", "drawing_empty", "the drawing has no geometry to reconstruct")

    frozen = frozen_sheet(db, node_run_id)
    frame_id, how, problem = resolve_sheet(evidence, sheet=sheet, notes=notes, answers=answers, frozen=frozen)
    if frame_id is None and turbo:
        first = evidence.frames[0]
        frame_id, how = first.id, "turbo_assumed"
        adopted.append(exec_policy.Assumption(
            id=f"A-S{len(adopted) + 1}", subject="drawing.sheet", field="sheet", adopted=frame_label(first),
            basis=("the requested sheet was not found; " if problem else "the drawing has several sheets; ")
            + "Turbo reconstructs the first frame", alternatives=[frame_label(f) for f in evidence.frames[1:10]],
            trigger="missing" if problem else "ambiguous", affected=["drawing.sheet"], author="ai_turbo").model_dump(mode="json"))
    lang = _lang(notes, evidence, frame_id)
    ures = units_resolution(evidence, frame_id, notes=notes, answers=answers)
    units = ures["units"]
    if units is None and turbo:
        signals = [s for s in ures["signals"] if s.get("unit") and s["source"] != "paper"]
        pick = next((s for s in signals if s["source"] == "header"), signals[0] if signals else None)
        unit = pick["unit"] if pick is not None and _units_of(pick["unit"], "x")["mm_per_unit"] else "mm"
        units = _units_of(unit, "turbo_assumed", insunits=evidence.units.insunits)
        adopted.append(exec_policy.Assumption(
            id=f"A-S{len(adopted) + 1}", subject="drawing.units", field="units", adopted=unit, unit=unit,
            basis={"conflict": "the unit signals disagree", "untrusted_header": "only an untrusted file header states a unit"}
            .get(ures["status"], "nothing states the length unit") + f"; Turbo reads lengths as {unit}"
            + (f" ({pick['source']})" if pick is not None else " (default)"),
            alternatives=sorted({s["unit"] for s in signals if s["unit"] != unit}), author="ai_turbo",
            trigger={"conflict": "conflict", "untrusted_header": "ambiguous"}.get(ures["status"], "missing"),
            affected=["drawing.units"]).model_dump(mode="json"))
    questions: list[str] = []
    gaps: list[dict[str, Any]] = []
    if frame_id is None:
        q = sheet_question(evidence, lang, problem)
        questions.append(q)
        gaps.append({"requirement_id": None, "kind": "drawing", "label": "sheet", "field": "sheet", "question_id": "sheet",
                     "reason": "not_found" if problem else "ambiguous", "frames": len(evidence.frames), "question": q})
        steps.append(_step("sheet", ok=True, status="needs_clarification", frames=len(evidence.frames), sheet_parameter=sheet or None))
    else:
        frame = evidence.frame(frame_id)
        res.frame_id = frame_id  # recorded in the node outputs (reconstruct.frame_id): frozen for later attempts
        steps.append(_step("sheet", frame_id=frame_id, how=how, number=frame_number(frame), name=frame_name(frame)))
    sheet_states_units = frame_id is None and any(_stated_unit(t.text, loose=False) for t in evidence.texts)
    if units is None and not sheet_states_units:
        q = units_question(evidence, lang, ures)
        questions.append(q)
        reason = {"conflict": "conflict", "untrusted_header": "untrusted_header"}.get(ures["status"], "missing")
        gap: dict[str, Any] = {"requirement_id": None, "kind": "drawing", "label": "units", "field": "units", "question_id": "units",
                               "reason": reason, "question": q}
        if ures["conflict"]:
            gap["sources"] = ures["conflict"]
        gaps.append(gap)
        steps.append(_step("units", ok=True, status="needs_clarification", reason=reason, insunits=evidence.units.insunits,
                           signals=ures["signals"]))
    elif units is not None:
        steps.append(_step("units", **units, signals=ures["signals"]))
    if questions:
        res.status, res.questions, res.gaps = "needs_clarification", questions, gaps
        return res
    assert frame_id is not None and units is not None
    frame = evidence.frame(frame_id)
    res.frame_id = frame_id

    cls = classify_dimensions(evidence, frame_id)
    steps.append(_step("scale_classification", views=cls["views"], conflicts=cls["conflicts"]))
    image: list[ImagePart] = []
    if provider.capabilities.vision:
        try:
            png = render_frame_png(src.dxf, frame, px=RENDER_PX, deadline=deadline)
        except DrawingEvidenceError as exc:
            steps.append(_step("render", ok=False, frame_id=frame_id, attached=False, code=exc.code, message=exc.message))
        else:
            image = [ImagePart(mime_type="image/png", data=png, name=f"{frame_id}.png")]
            steps.append(_step("render", frame_id=frame_id, px=RENDER_PX, bytes=len(png), attached=True))
    else:
        steps.append(_step("render", frame_id=frame_id, attached=False, reason="the model config does not declare vision"))
    values, kinds = drawing_values(evidence, frame_id)
    label = frame_label(frame)
    block = "\n".join([
        f"sheet: {label} (frame {frame_id} of {len(evidence.frames)} in {src.name}; {src.fmt.upper()}"
        + (" converted to a derived DXF" if src.fmt == "dwg" else "") + ")",
        units_line(units),
        *_index_scale_note(evidence, units),
        "image: " + ("attached (PNG render of this sheet)" if image else "not attached (use the evidence index)"),
        evidence_prompt(evidence, frame_id),
        classification_text(cls),
    ])
    res.evidence = evidence_record(evidence, frame, src, units, cls, bool(image))
    conflicting = set(cls["conflicts"])
    drawing = DrawingInput(block=block, sheet_label=label, values=values, kinds=kinds, images=image,
                           gate=make_conflict_gate(cls, lang, answers), lang=lang,
                           detail={"frame_id": frame_id, "conflicts": len(cls["conflicts"]), "source_format": src.fmt},
                           derivation_sources=tuple(v for k, v in values.items() if k not in conflicting))
    extra_checks: dict[str, Any] = {"source": src.provenance, "evidence": res.evidence}
    if adopted:
        extra_checks["drawing_assumptions"] = adopted
    m = ai_modeling.run_modeling(
        db, provider=provider, cfg=cfg, part=part, user=user, base=None, text=notes or "", answers=answers, context=context,
        private=private, node_run_id=node_run_id, drawing=drawing, source_file_version_id=src.file_version_id,
        extra_checks=extra_checks, note=f"Reconstructed from {src.name}, sheet {label}", policy=policy,
    )
    res.status, res.label, res.calls, res.outcome = m.status, m.label, m.calls, m.outcome
    res.revision, res.questions, res.gaps, res.summary, res.error = m.revision, m.questions, m.gaps, m.summary, m.error
    res.assumptions = list(m.assumptions) if m.assumptions else adopted
    res.steps = steps + list(m.steps)
    return res


def source_brief(res: ReconstructResult) -> dict[str, Any] | None:
    """Compact, public source summary for node outputs, tool results and assistant results."""
    s = res.source
    if not s:
        return None
    out = {k: s.get(k) for k in ("format", "name", "file_version_id", "drawing_id", "source_sha256", "dxf_sha256", "dxf_role")}
    dwg = s.get("dwg") or {}
    if dwg:
        out["dwg"] = {"version": (dwg.get("source") or {}).get("version"), "version_status": dwg.get("version_status"),
                      "converter": dwg.get("converter"), "losses": len(dwg.get("losses") or []),
                      "derived_dxf_blob": (dwg.get("derived_dxf") or {}).get("blob_sha256")}
    out["frame_id"] = res.frame_id
    if res.evidence:
        out["frame"] = res.evidence.get("frame")
        out["conflicts"] = len((res.evidence.get("scale_classification") or {}).get("conflicts") or [])
    return out


__all__ = ["ReconstructResult", "SIMULATION_UNSUPPORTED", "classify_dimensions", "conflict_question_id", "frozen_sheet",
           "is_simulation_fallback", "load_source", "make_conflict_gate", "match_frame", "resolve_sheet", "resolve_units",
           "run_reconstruct", "source_brief", "unit_signals", "units_resolution"]
