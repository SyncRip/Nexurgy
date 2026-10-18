"""Rule-based responders for the explicitly labelled "simulated" mode.

Used when no model key is configured, so the pipeline (prompt → validation →
gate → kernel → check → revision) can be demonstrated end to end. Results carry
``simulated=True``; they never count as a real AI run.

The modeling responder follows the same contract as a real model, including the
requirement list (app/cad/requirements.py) with quotes copied from the request or
the user's answers, so the server gate and the independent check run on it.
Creating, it understands an overall size ("120x80x10") plus optional
"N × Ød holes" — and builds them only when the critical hole facts are stated
(in the request or an answer): through ("through") or blind with a depth
("blind, 8 deep"), and the positions ("at the centre" for one
hole, "on a Ø60 circle", or "15 mm from the edges" for
four corner holes), including their Chinese equivalents. Anything missing becomes a question, never a default.
Modifying, it understands an overall size change, a hole diameter change
("diameter 10", "Ø10") and a hole move ("move the hole right 2 mm"),
including Chinese equivalents; requirements cover every changed op (the holes
that follow a moved top face too) and cite the kept values with
``source_geometry`` (the server reads them from the current op). Derived
positions are written out with the arithmetic from cited numbers, as the
grounding rules require of a real model. The hole is the op named in the
request, else the op of the TARGET selection (the "Target selection" section of
the <context> block: resolved / selected op, else its only candidate), else the
rest of <context>, else a selection referenced in <data>, else the first hole op.
Everything else turns into a clarification question. Under Turbo (:data:`app.ai.turbo.POLICY_TAG`) it adopts the size,
through / blind and position instead and writes each as a decision line (trigger, value with unit, basis, affects;
generation supplement D-02); what it cannot choose stays ``unsupported``, never invented.

Springs (generation supplement): a request naming a compression spring or a split
lock washer, in English or Chinese, is answered with the part template (compression_spring / split_lock_washer)
and the values the request states (wire / outer / mean / inner diameter, active / total coils, pitch, free length,
handedness, end form; a GB/T 93 size or inner / outer diameter and thickness), plus an envelope and a coil requirement
that cite the template. A missing value is asked for; values the template refuses as contradictory are reported back.

Drawing → STEP reconstruction has no simulated responder on purpose: rule-based
text matching cannot read a drawing, and faking geometry from one would be a false
result. services/ai_reconstruct.py runs the deterministic steps (DWG read, evidence
index) and then fails with ``simulation_unsupported`` (labelled simulated) instead
of calling this module; the AI workspace reports the operation as unavailable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .prompts import extract_answers, extract_block, extract_request_text
from .providers.base import Capabilities, ModelConfig
from .providers.fake import FakeProvider, FakeRequest
from .turbo import is_turbo_text

_NUM = r"(\d+(?:\.\d+)?)"
_DIMS = re.compile(_NUM + r"\s*(?:mm)?\s*[x×X*]\s*" + _NUM + r"\s*(?:mm)?\s*[x×X*]\s*" + _NUM)
_DIA = re.compile(r"[Øø⌀φΦ]\s*" + _NUM)
_COUNT_BEFORE_DIA = re.compile(r"(\d+)\s*(?:个|pcs|-|x|×|X)?\s*(?=[Øø⌀φΦ])")
_COUNT_HOLES = re.compile(r"(\d+)\s*(?:个)?\s*(?:through[- ]?)?(?:holes?|孔)")
_SIM_NOTE = "simulated reply: no AI model was called"
# Turbo (W7) without a stated size: the simulated responder adopts this block and lists it as assumed.
_TURBO_SIZE = (100.0, 60.0, 10.0)
_CJK = re.compile(r"[\u3400-\u9fff]")
_OP_ID = re.compile(r"\bF-\d{2,}\b")
# Chinese forms mean "change hole diameter to 10 mm" or "set diameter to Ø10"; English forms include "diameter 10", "dia. 10" and "Ø10".
_HOLE_DIA = re.compile(
    r"(?:孔径|直径|diameter|dia\.?)(?:\s*(?:改成|改为|改到|变成|变为|设为|设成|调整为|调到|to|=|:|：|of|is|the|hole))*\s*[Øø⌀φΦ]?\s*" + _NUM,
    re.IGNORECASE,
)
# Chinese move phrases mean "move the hole right 2 mm", "move left 3", "shift right 2 mm" or "translate backward 1.5".
_MOVE_ZH = re.compile(r"(?:向|往|朝)?\s*(左|右|前|后|上|下)\s*(?:移动|平移|移)\s*(?:了)?\s*" + _NUM)
# "move the hole right 2 mm", "shift it left by 3", "move hole F-02 back 1.5mm"
_MOVE_EN = re.compile(
    r"\b(?:move|shift|offset)\b[^\n\d]*?\b(left|right|back|backward|backwards|forward|forwards|front|up|down)\b\s*(?:by\s*)?" + _NUM,
    re.IGNORECASE,
)
_MOVE_EN_NUM_FIRST = re.compile(
    r"\b(?:move|shift|offset)\b[^\n\d]*?" + _NUM + r"\s*(?:mm)?\s*(?:to\s+the\s+)?(left|right|back|backward|backwards|forward|forwards|front|up|down)\b",
    re.IGNORECASE,
)
# In-plane directions as seen from the top (+Z): right = +X, back / up = +Y.
_MOVE_AXIS: dict[str, tuple[int, float]] = {
    "右": (0, 1.0), "左": (0, -1.0), "后": (1, 1.0), "前": (1, -1.0), "上": (1, 1.0), "下": (1, -1.0),
    "right": (0, 1.0), "left": (0, -1.0), "back": (1, 1.0), "backward": (1, 1.0), "backwards": (1, 1.0), "up": (1, 1.0),
    "forward": (1, -1.0), "forwards": (1, -1.0), "front": (1, -1.0), "down": (1, -1.0),
}


_THROUGH = re.compile(r"\bthrough\b|\bthru\b|通孔|贯穿|打穿", re.IGNORECASE)
_BLIND = re.compile(r"\bblind\b|盲孔", re.IGNORECASE)
_DEPTH = re.compile(r"(?:深度?|\bdepth(?:\s+of)?)\s*[:：=]?\s*" + _NUM + r"|" + _NUM + r"\s*(?:mm\s*)?(?:\bdeep\b|深)", re.IGNORECASE)
_CENTRE = re.compile(r"\b(?:at|in)\s+the\s+(?:centre|center|middle)\b|\bcent(?:re|er)ed\b|居中|中心|正中|中间", re.IGNORECASE)
_CIRCLE = re.compile(
    r"[Øø⌀φΦ]\s*" + _NUM + r"\s*(?:mm\s*)?(?:bolt[- ]?)?circle|\bPCD\s*[Øø⌀φΦ]?\s*" + _NUM
    + r"|\bbolt[- ]circle(?:\s+diameter)?\s*(?:of\s*)?[Øø⌀φΦ]?\s*" + _NUM
    + r"|分布圆\s*(?:直径)?\s*[Øø⌀φΦ]?\s*" + _NUM + r"|[Øø⌀φΦ]\s*" + _NUM + r"\s*(?:mm\s*)?(?:的)?(?:分布)?圆(?:周)?上",
    re.IGNORECASE,
)
_EDGES = re.compile(_NUM + r"\s*(?:mm\s*)?from\s+the\s+edges?|距边\s*" + _NUM + r"|边距\s*" + _NUM, re.IGNORECASE)

Source = tuple[str, str]  # (evidence source, text)


def _find(rx: re.Pattern[str], sources: list[Source]) -> tuple[re.Match[str], str] | None:
    """First match of ``rx`` in the request, then in the answers: (match, evidence source)."""
    for source, text in sources:
        m = rx.search(text)
        if m:
            return m, source
    return None


def _num(m: re.Match[str]) -> float:
    return float(next(g for g in m.groups() if g is not None))


def _ev(found: tuple[re.Match[str], str]) -> dict[str, Any]:
    return {"source": found[1], "ref": found[0].group(0).strip()}


def simulated_modeling_responder(req: FakeRequest) -> dict[str, Any]:
    first = req.messages[0].content if req.messages else ""
    text = extract_request_text(first)
    dims = _DIMS.search(text)
    turbo = is_turbo_text(first)  # W7: adopt values (listed in assumed / assumptions) instead of asking
    if "<current_model>" in first:
        return _modify(first, dims, turbo=turbo)
    zh = bool(_CJK.search(text))
    sources: list[Source] = [("request", text)] + [("clarification", a) for a in extract_answers(first)]
    if _SPRING.search(text) or _LOCK_WASHER.search(text):
        return _spring_reply(sources, zh, turbo, washer=not _SPRING.search(text))
    found_dims = _find(_DIMS, sources)
    adopted: list[str] = []
    if not found_dims and not turbo:
        return _ask(["零件的总体尺寸（长 × 宽 × 高，mm）是多少？（模拟模式）" if zh else
                     "What is the overall size, length × width × height (mm)? (simulated mode)"])
    if found_dims:
        x, y, z = (float(v) for v in found_dims[0].groups())
        envelope: dict[str, Any] = {"evidence": [_ev(found_dims)]}
    else:
        x, y, z = _TURBO_SIZE
        envelope = {"evidence": [], "assumed": ["size"]}
        adopted.append(f"R1.size = {x:g} × {y:g} × {z:g} mm; trigger: missing; basis: the request states no overall size, so the "
                       f"simulated rules use their default {x:g} × {y:g} × {z:g} mm plate (simulated); affects: R1, F-01")
    ops: list[dict[str, Any]] = [{"id": "F-01", "type": "box", "label": "body", "mode": "new", "size": [x, y, z], "at": [0, 0, 0]}]
    changes = [f"F-01: block {x:g}×{y:g}×{z:g}"]
    reqs: list[dict[str, Any]] = [{"id": "R1", "kind": "envelope", "label": f"block {x:g} × {y:g} × {z:g}", "size": [x, y, z], **envelope}]
    circle = _find(_CIRCLE, sources)
    plain = [(src, t.replace(circle[0].group(0), " ") if circle and src == circle[1] else t) for src, t in sources]
    found_dia = _find(_DIA, plain)
    if found_dia:
        d = float(found_dia[0].group(1))
        m = _COUNT_BEFORE_DIA.search(text) or _COUNT_HOLES.search(text)
        n = max(1, int(m.group(1))) if m else 1
        hole: dict[str, Any] = {"id": "F-02", "type": "hole", "label": "holes", "at": [0, 0, z], "direction": [0, 0, -1],
                                "diameter": d, "depth": None}
        evidence = [_ev(found_dia)]
        questions: list[str] = []
        assumed: list[str] = []
        through, blind, depth = _find(_THROUGH, sources), _find(_BLIND, sources), _find(_DEPTH, sources)
        if through and not blind:
            evidence.append(_ev(through))
            kind_text = "through"
        elif depth is not None and _num(depth[0]) < z:
            hole["depth"] = _num(depth[0])
            evidence += [_ev(depth)] + ([_ev(blind)] if blind else [])
            kind_text = f"blind, {hole['depth']:g} deep"
        elif turbo:
            kind_text = "through"
            assumed.append("through")
            adopted.append(f"R2.through = through; trigger: missing; basis: the request says neither through nor blind; a through hole "
                           f"needs no depth in the {z:g} mm plate (simulated); affects: R2, F-02")
        else:
            kind_text = ""
            questions.append("孔是通孔还是盲孔？盲孔请给出深度（mm）。（模拟模式）" if zh else
                             "Are the holes through, or blind (how deep, mm)? (simulated mode)")
        derivation = ""
        edges = _find(_EDGES, sources)
        centre = _find(_CENTRE, sources)
        top = f"entry on the top face z = {z:g}"
        if n > 1 and circle and _num(circle[0]) / 2 > d:
            r = _num(circle[0]) / 2
            hole["pattern"] = {"kind": "circular", "count": n, "radius": r}
            evidence.append(_ev(circle))
            derivation = (f"{n} holes equally spaced on the Ø{2 * r:g} circle (R = {2 * r:g}/2 = {r:g}) centred on the part, "
                          f"the first on +X: {_points(hole)}; {top}")
            where = f"on R{r:g}"
        elif n == 4 and edges and 2 * _num(edges[0]) < min(x, y):
            e = _num(edges[0])
            hole["at"] = [-(x / 2 - e), -(y / 2 - e), z]
            hole["pattern"] = {"kind": "linear", "count": [2, 2], "pitch": [x - 2 * e, y - 2 * e]}
            evidence.append(_ev(edges))
            derivation = (f"corner holes: x = ±({x:g}/2 − {e:g}) = ±{x / 2 - e:g}, y = ±({y:g}/2 − {e:g}) = ±{y / 2 - e:g}; "
                          f"{top}")
            where = f"{e:g} from the edges"
        elif n == 1 and centre:
            evidence.append(_ev(centre))
            derivation = f"centre of the top face: (0, 0, {z:g})"
            where = "at the centre"
        elif turbo and (n == 1 or min(x, y) * 0.3 > d):
            if n > 1:
                r = round(min(x, y) * 0.3, 3)
                hole["pattern"] = {"kind": "circular", "count": n, "radius": r}
                where = f"on R{r:g} (assumed)"
                why = f"{n} holes equally spaced on R{r:g} = 0.3 × the smaller side {min(x, y):g} mm, centred on the part"
            else:
                where = "at the centre (assumed)"
                why = "one hole at the centre of the top face keeps equal edge distances on every side"
            assumed.append("entries")
            adopted.append(f"R2.entries = {_points(hole)}; trigger: missing; basis: the request gives no hole position; {why} (simulated); "
                           "affects: R2, F-02")
        else:
            where = ""
            questions.append(("孔的位置在哪里？（例如“居中”）（模拟模式）" if zh else "Where is the hole? (e.g. 'at the centre') (simulated mode)")
                             if n == 1 else
                             (f"{n} 个孔如何布置？（例如“均布在 Ø60 圆上”或“距边 15”）（模拟模式）" if zh else
                              f"Where are the {n} holes? (e.g. 'on a Ø60 circle' or '15 mm from the edges') (simulated mode)"))
        if questions:
            return _turbo_refusal(questions) if turbo else _ask(questions)
        ops.append(hole)
        changes.append(f"F-02: {n} × Ø{d:g} {kind_text} hole(s) {where}")
        hole_req = _hole_req("R2", hole, f"{n} × Ø{d:g} {kind_text}", evidence,
                             derived=["entries"] if "entries" not in assumed else None, derivation=derivation)
        if assumed:
            hole_req["assumed"] = assumed
        reqs.append(hole_req)
    return {"status": "ops", "questions": [], "assumptions": [_SIM_NOTE, *adopted], "requirements": reqs, "ops": ops, "changes": changes}


_SPRING = re.compile(r"压缩弹簧|螺旋弹簧|\bcompression spring|\bcoil spring", re.IGNORECASE)
_LOCK_WASHER = re.compile(r"弹簧垫圈|开口垫圈|\b(?:split )?lock washer|\bspring washer", re.IGNORECASE)
_SPRING_VALUES: dict[str, re.Pattern[str]] = {
    "wire_diameter": re.compile(r"(?:线径|丝径|\bwire(?: diameter)?)\s*[dD]?\s*[:：=]?\s*[Øø⌀φΦ]?\s*" + _NUM, re.IGNORECASE),
    "outer_diameter": re.compile(r"(?:外径|\bOD\b|\bouter diameter)\s*[:：=]?\s*[Øø⌀φΦ]?\s*" + _NUM, re.IGNORECASE),
    "mean_diameter": re.compile(r"(?:中径|\bmean diameter)\s*[:：=]?\s*[Øø⌀φΦ]?\s*" + _NUM, re.IGNORECASE),
    "inner_diameter": re.compile(r"(?:内径|\bID\b|\binner diameter)\s*[:：=]?\s*[Øø⌀φΦ]?\s*" + _NUM, re.IGNORECASE),
    "active_coils": re.compile(r"(?:有效圈数?|\bactive coils?)\s*[:：=]?\s*" + _NUM, re.IGNORECASE),
    "total_coils": re.compile(r"(?:总圈数?|\btotal coils?)\s*[:：=]?\s*" + _NUM, re.IGNORECASE),
    "pitch": re.compile(r"(?:节距|\bpitch)\s*[:：=]?\s*" + _NUM, re.IGNORECASE),
    "free_length": re.compile(r"(?:自由(?:高度|长度)|\bfree (?:length|height))\s*[:：=]?\s*" + _NUM, re.IGNORECASE),
    "thickness": re.compile(r"(?:厚度?|\bthickness)\s*[:：=]?\s*" + _NUM, re.IGNORECASE),
}
_LEFT = re.compile(r"左旋|\bleft[- ]hand", re.IGNORECASE)
_ENDS = (("closed_ground", re.compile(r"并紧磨平|\bclosed and ground|\bground ends?", re.IGNORECASE)),
         ("closed", re.compile(r"并紧|\bclosed ends?", re.IGNORECASE)),
         ("open", re.compile(r"开口端|不并紧|\b(?:open|plain) ends?", re.IGNORECASE)))
_METRIC = re.compile(r"\bM(\d+)\b")


def _spring_reply(sources: list[Source], zh: bool, turbo: bool, *, washer: bool) -> dict[str, Any]:
    """A spring part through its template, with an envelope and a coil requirement citing the template."""
    from app.cad.templates import LOCK_WASHER, TemplateError, instantiate

    found = {k: _find(rx, sources) for k, rx in _SPRING_VALUES.items()}
    params: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []

    def take(name: str, key: str | None = None) -> None:
        f = found[key or name]
        if f:
            params[name] = _num(f[0])
            evidence.append(_ev(f))

    tid = "split_lock_washer" if washer else "compression_spring"
    if washer:
        size = _find(_METRIC, sources)
        if size and f"M{size[0].group(1)}" in LOCK_WASHER:
            params["size"] = f"M{size[0].group(1)}"
            evidence.append(_ev(size))
        elif found["inner_diameter"] and found["outer_diameter"] and found["thickness"]:
            params["size"] = "custom"
            for name in ("inner_diameter", "outer_diameter", "thickness"):
                take(name)
        else:
            q = ("弹簧垫圈的规格（如 M8）或内径、外径和厚度是多少（mm）？（模拟模式）" if zh else
                 "Which lock washer size (e.g. M8), or inner / outer diameter and thickness (mm)? (simulated mode)")
            return _turbo_refusal([q]) if turbo else _ask([q])
        take("free_height", "free_length")
    else:
        for name in ("wire_diameter", "outer_diameter", "mean_diameter", "inner_diameter", "active_coils", "total_coils", "pitch",
                     "free_length"):
            take(name)
        missing = [q for q, ok in (
            ("线径 d" if zh else "wire diameter d", "wire_diameter" in params),
            ("外径、中径或内径" if zh else "outer, mean or inner diameter", any(k in params for k in ("outer_diameter", "mean_diameter", "inner_diameter"))),
            ("有效圈数或总圈数" if zh else "active or total coils", any(k in params for k in ("active_coils", "total_coils"))),
            ("节距或自由高度" if zh else "pitch or free length", any(k in params for k in ("pitch", "free_length"))),
        ) if not ok]
        if missing:
            q = ("压缩弹簧还缺：" + "、".join(missing) + "（mm）。（模拟模式）") if zh else (
                "The compression spring still needs: " + ", ".join(missing) + " (mm). (simulated mode)")
            return _turbo_refusal([q]) if turbo else _ask([q])
        for value, rx in _ENDS:
            f = _find(rx, sources)
            if f:
                params["end_form"] = value
                evidence.append(_ev(f))
                break
    left = _find(_LEFT, sources)
    if left:
        params["handedness"] = "left"
        evidence.append(_ev(left))
    try:
        spec, meta = instantiate(tid, params)
    except TemplateError as e:
        q = ("参数互相矛盾：" if zh else "the values contradict each other: ") + e.message + "（模拟模式）"
        return _turbo_refusal([q]) if turbo else _ask([q])
    coil = spec.ops[0]
    if coil.type != "coil":  # a flattened washer is an extrude: this responder only builds the free state
        q = "模拟模式只生成自由态弹簧垫圈，请给出自由高度。" if zh else "simulated mode builds the free state only; give the free height."
        return _ask([q])
    law = coil.law()
    d = meta["derived"]
    od = d["outer_diameter"]
    s_rad, s_ax = coil.section_size()
    t_ev = [{"source": "template", "ref": tid}]
    if washer:
        env_derivation = (f"outer Ø{od:g} = {d['inner_diameter']:g} + 2 × {d['section_width']:g}; height = free height "
                          f"{law.length:g}")
        section: dict[str, Any] = {"kind": "rect", "width": s_rad, "height": s_ax}
        label = f"lock washer {meta['spec_label']}"
    else:
        env_derivation = f"outer Ø = D + d = {coil.mean_diameter:g} + {s_rad:g} = {od:g}; height = length {law.length:g}"
        section = {"kind": "circle", "diameter": s_rad}
        label = meta["spec_label"]
    reqs = [
        {"id": "R1", "kind": "envelope", "label": f"envelope Ø{od:g} × {law.length:g}", "size": [od, od, law.length],
         "evidence": t_ev + evidence, "derived": ["size"], "derivation": env_derivation},
        {"id": "R2", "kind": "coil", "label": label, "axis_origin": [0, 0, 0], "axis": [0, 0, 1], "mean_diameter": coil.mean_diameter,
         "section": section, "turns": coil.turns, "length": coil.length, "end_form": coil.end_form, "handedness": coil.handedness,
         "evidence": t_ev + evidence},
    ]
    notes = [n for n in meta["notes"] if n.startswith(("static", "closed end", "ground ends", "section a sharp"))]
    if notes:
        reqs.append({"id": "R3", "kind": "note", "category": "general", "label": "simplified representation",
                     "text": ("简化表示：" if zh else "simplified: ") + "; ".join(notes), "evidence": t_ev})
    return {"status": "ops", "template": {"id": tid, "params": [{"name": k, "value": v} for k, v in params.items()]},
            "questions": [], "assumptions": [_SIM_NOTE, *meta["notes"]], "requirements": reqs, "ops": [],
            "changes": [f"{meta['op_ids'][0]}: {meta['spec_label']} (template {tid})"]}


def _hole_req(rid: str, op: dict[str, Any], label: str, evidence: list[dict[str, Any]], *, derived: list[str] | None = None,
              derivation: str = "") -> dict[str, Any]:
    """Hole requirement describing exactly what ``op`` states (entries of every instance)."""
    from app.cad.ops import HoleOp, hole_positions

    h = HoleOp.model_validate(op)
    out: dict[str, Any] = {"id": rid, "kind": "hole", "label": label, "evidence": evidence, "diameter": h.diameter,
                           "direction": list(h.direction), "entries": [list(e) for e in hole_positions(h)],
                           "through": h.depth is None, "depth": h.depth}
    if h.counterbore is not None:
        out["counterbore"] = h.counterbore.model_dump()
    if h.countersink is not None:
        out["countersink"] = h.countersink.model_dump()
    if derived and derivation:
        out["derived"], out["derivation"] = derived, derivation
    return out


def _quote(text: str, candidate: str) -> str:
    """``candidate`` when it is literally in ``text`` (after whitespace collapse), else the whole request."""
    c = " ".join(candidate.split())
    return c if c and c in " ".join(text.split()) else text.strip()


def _geometry_value(op: dict[str, Any]) -> str:
    """How the current model states a hole op (cited as source_geometry)."""
    from app.cad.ops import HoleOp, hole_positions

    h = HoleOp.model_validate(op)
    pts = "; ".join("(" + ", ".join(f"{c:g}" for c in e) + ")" for e in hole_positions(h))
    kind = "through" if h.depth is None else f"blind, depth {h.depth:g}"
    return f"Ø{h.diameter:g} {kind} at {pts}"


def _positions(op: dict[str, Any]) -> list[Any]:
    from app.cad.ops import HoleOp, hole_positions

    return hole_positions(HoleOp.model_validate(op))


def _points(op: dict[str, Any]) -> str:
    """Every instance position of a hole op, written out (3 decimals) for a derivation."""
    from app.cad.ops import HoleOp, hole_positions

    def f(v: float) -> str:
        t = f"{round(v, 3):g}"
        return "0" if t == "-0" else t

    return "; ".join("(" + ", ".join(f(c) for c in p) + ")" for p in hole_positions(HoleOp.model_validate(op)))


def _modify(user_message: str, dims: re.Match[str] | None, *, turbo: bool = False) -> dict[str, Any]:
    ask = _turbo_refusal if turbo else _ask
    line = next((ln for ln in user_message.splitlines() if ln.startswith("ops: ")), "ops: []")
    ops: list[dict[str, Any]] = json.loads(line[5:])
    original = {op["id"]: json.loads(json.dumps(op)) for op in ops if isinstance(op.get("id"), str)}
    text = extract_request_text(user_message)
    zh = bool(_CJK.search(text))
    changes: list[str] = []
    assumptions: list[str] = [_SIM_NOTE]

    reqs: list[dict[str, Any]] = []
    followed: list[str] = []  # holes whose entry followed the moved top face (changed ops: checked too)
    dims_ev: dict[str, Any] | None = None
    box = next((op for op in ops if op.get("type") == "box"), None)
    if dims and box is not None:
        old_top = box["at"][2] + box["size"][2]
        new = [float(v) for v in dims.groups()]
        box["size"] = new
        for op in ops:
            if op.get("type") == "hole" and abs(op["at"][2] - old_top) < 1e-6:
                op["at"][2] = box["at"][2] + new[2]
                followed.append(op["id"])
        changes.append(f"{box['id']}: size → {new[0]:g}×{new[1]:g}×{new[2]:g}")
        changes += [f"{i}: entry z {old_top:g} → {box['at'][2] + new[2]:g} (follows the top face)" for i in followed]
        dims_ev = {"source": "request", "ref": _quote(text, dims.group(0))}
        reqs.append({"id": "R1", "kind": "envelope", "label": f"block {new[0]:g} × {new[1]:g} × {new[2]:g}",
                     "evidence": [dims_ev], "size": new})

    targets: dict[str, tuple[list[dict[str, Any]], str]] = {}  # hole id → (request quotes, derivation)
    plain = _OP_ID.sub(" ", text)  # op ids carry digits; never read them as sizes or distances
    dia = _HOLE_DIA.search(plain) or (_DIA.search(plain) if not dims else None)
    move = _parse_move(plain)
    if dia or move:
        hole, why = _target_hole(ops, text, extract_block(user_message, "context"), extract_block(user_message, "data"))
        if hole is None:
            return ask(["当前模型没有孔特征可以修改。（模拟模式）" if zh else
                         "The current model has no hole op to change. (simulated mode)"])
        if why:  # Turbo writes it as a decision line (generation supplement D-02)
            assumptions.append(f"{hole['id']}.target = {hole['id']}; trigger: ambiguous; basis: {why} (simulated); affects: {hole['id']}"
                               if turbo else why)
        evidence: list[dict[str, Any]] = []
        derivation = ""
        if dia:
            d = float(dia.group(1))
            for extra in ("counterbore", "countersink"):
                spec = hole.get(extra)
                if isinstance(spec, dict) and float(spec.get("diameter") or 0) <= d:
                    return ask([f"{hole['id']} 的{'沉头孔' if extra == 'counterbore' else '埋头孔'}直径 Ø{spec['diameter']:g} 不大于新孔径 Ø{d:g}，"
                                 f"{'沉头孔' if extra == 'counterbore' else '埋头孔'}改成多大？（模拟模式）" if zh else
                                 f"The {extra} of {hole['id']} (Ø{spec['diameter']:g}) is not larger than the new Ø{d:g}; "
                                 f"what {extra} diameter should it have (mm)? (simulated mode)"])
            old_d = float(hole.get("diameter") or 0)
            hole["diameter"] = d
            changes.append(f"{hole['id']}: Ø{old_d:g} → Ø{d:g}")
            evidence.append({"source": "request", "ref": _quote(text, dia.group(0))})
        if move:
            axis, delta, word, _ = move
            at = list(hole.get("at") or [0, 0, 0])
            old_c = at[axis]
            at[axis] = round(old_c + delta, 6)
            hole["at"] = at
            name = "x" if axis == 0 else "y"
            changes.append(f"{hole['id']}: entry {name} {old_c:g} → {at[axis]:g} ({word} {abs(delta):g} mm)")
            direction = ("移动方向按俯视（+Z 向下看）理解：右 = +X，后 / 上 = +Y" if zh else
                         "Directions are read in the top view (looking down -Z): right = +X, back / up = +Y")
            assumptions.append(f"{hole['id']}.move_axis = {'+' if delta > 0 else '-'}{name.upper()}; trigger: ambiguous; basis: {direction} "
                               f"(simulated); affects: {hole['id']}" if turbo else direction)
            evidence.append({"source": "request", "ref": _quote(text, move[3])})
            olds = sorted({round(p[axis], 3) for p in _positions(original[hole["id"]])})
            steps = ", ".join(f"{o:g} {'+' if delta > 0 else '−'} {abs(delta):g} = {round(o + delta, 3):g}" for o in olds)
            derivation = f"{hole['id']} moved {word} {abs(delta):g} mm: {name} {steps} → {_points(hole)}"
        targets[hole["id"]] = (evidence, derivation)
    for hid in followed:
        targets.setdefault(hid, ([], ""))
    for hid, (evidence, derivation) in targets.items():
        op = next(o for o in ops if o.get("id") == hid)
        ev = list(evidence) + ([dims_ev] if hid in followed and dims_ev else [])
        ev.append({"source": "source_geometry", "ref": hid, "value": _geometry_value(original[hid])})
        reqs.append(_hole_req(f"R{len(reqs) + 1}", op, f"{hid} Ø{float(op['diameter']):g}", ev,
                              derived=["entries"] if derivation else None, derivation=derivation))
    if not changes:
        return ask(["模拟模式只能理解：整体尺寸（如 120x80x12）、孔径修改（如“孔径改成 10 mm”）、孔移动（如“孔向右移动 2 mm”）。请换一种说法。"
                     if zh else
                     "Simulated mode only understands overall size changes such as '120x80x12', hole diameter changes "
                     "('diameter 10', 'Ø10') and hole moves ('move the hole right 2 mm')."])
    return {"status": "ops", "questions": [], "assumptions": assumptions, "requirements": reqs, "ops": ops, "changes": changes}


def _parse_move(text: str) -> tuple[int, float, str, str] | None:
    """(axis, signed distance, direction word, matched text)."""
    m = _MOVE_ZH.search(text)
    if m:
        axis, sign = _MOVE_AXIS[m.group(1)]
        return axis, sign * float(m.group(2)), m.group(1), m.group(0)
    m = _MOVE_EN.search(text)
    if m:
        word = m.group(1).lower()
        axis, sign = _MOVE_AXIS[word]
        return axis, sign * float(m.group(2)), word, m.group(0)
    m = _MOVE_EN_NUM_FIRST.search(text)
    if m:
        word = m.group(2).lower()
        axis, sign = _MOVE_AXIS[word]
        return axis, sign * float(m.group(1)), word, m.group(0)
    return None


def _target_section(context: str) -> str:
    """The "Target selection" section of the <context> block (up to the next blank line), or ""."""
    start = context.find("Target selection")
    if start == -1:
        return ""
    end = context.find("\n\n", start)
    return context[start:] if end == -1 else context[start:end]


def _selected_hole(holes: dict[str, dict[str, Any]], text: str) -> dict[str, Any] | None:
    for key in ("resolved_op_id", "selected_op_id"):
        m = re.search(key + r":\s*(F-\d{2,})", text)
        if m and m.group(1) in holes:
            return holes[m.group(1)]
    candidates = [c for c in re.findall(r"candidate:\s*(F-\d{2,})", text) if c in holes]
    if len(set(candidates)) == 1:
        return holes[candidates[0]]
    return None


def _target_hole(ops: list[dict[str, Any]], text: str, context: str, data: str = "") -> tuple[dict[str, Any] | None, str]:
    """The hole op to change and, when it was a fallback choice, why. The target selection
    (<context>) always wins over a selection that is only referenced (<data>)."""
    holes = {op["id"]: op for op in ops if op.get("type") == "hole" and isinstance(op.get("id"), str)}
    if not holes:
        return None, ""
    for oid in _OP_ID.findall(text):
        if oid in holes:
            return holes[oid], ""
    for section in (_target_section(context), context):
        hole = _selected_hole(holes, section) if section else None
        if hole is not None:
            return hole, ""
    hole = _selected_hole(holes, data) if data else None
    if hole is not None:
        return hole, f"no target selection; changed {hole['id']} from a referenced selection"
    first = next(iter(holes.values()))
    return first, f"hole not specified; changed the first hole op {first['id']}"


def _ask(questions: list[str]) -> dict[str, Any]:
    return {"status": "needs_clarification", "questions": questions, "assumptions": [_SIM_NOTE], "requirements": [], "ops": [],
            "changes": []}


def _turbo_refusal(questions: list[str]) -> dict[str, Any]:
    """Turbo: the rule-based responder cannot choose a value for what it would have asked; it says so (``unsupported``,
    nothing built) instead of asking a person or inventing geometry it cannot read."""
    return {"status": "unsupported", "reason": "simulated mode cannot adopt a value for: " + " / ".join(questions)[:400],
            "questions": [], "assumptions": [_SIM_NOTE], "requirements": [], "ops": [], "changes": []}


def simulated_intake_responder(req: FakeRequest) -> dict[str, Any]:
    """Extracts nothing: every field is reported missing so a person fills it in."""
    return {
        "units_on_drawing": "unknown",
        "missing": ["material", "overall_size_mm.x", "overall_size_mm.y", "overall_size_mm.z", "quantity"],
        "other_requirements": [],
        "holes": [],
        "tolerances": [],
        "surface_finish": [],
    }


Responder = Callable[[FakeRequest], dict[str, Any]]

# purpose -> deterministic responder. The collaboration planner / executor /
# reviewer modules register theirs with register_responder() at import time.
_RESPONDERS: dict[str, Responder] = {
    "modeling": simulated_modeling_responder,
    "intake": simulated_intake_responder,
}


#: schema name -> responder: answers that structured output whatever the purpose (several agents share the
#: "assistant" purpose and its configuration, e.g. ask and workflow_edit).
_SCHEMA_RESPONDERS: dict[str, Responder] = {}


def register_responder(purpose: str, responder: Responder) -> None:
    _RESPONDERS[purpose] = responder


def register_schema_responder(schema_name: str, responder: Responder) -> None:
    _SCHEMA_RESPONDERS[schema_name] = responder


def simulated_provider(purpose: str, config: ModelConfig | None = None) -> FakeProvider:
    """A FakeProvider answering every call with the matching rule-based responder (by schema name first,
    then by purpose)."""
    if purpose == "assistant" and purpose not in _RESPONDERS:
        from . import assistant_agent  # noqa: F401  (registers its responder on import)
    responder = _RESPONDERS.get(purpose)
    if responder is None:
        raise ValueError(f"no simulated responder registered for {purpose!r}")
    by_purpose = responder

    def respond(req: FakeRequest) -> dict[str, Any]:
        return _SCHEMA_RESPONDERS.get(req.schema_name, by_purpose)(req)

    caps = Capabilities(text=True, vision=True, tools=False, structured=True)
    return FakeProvider(respond, capabilities=caps, model_id="simulated", config=config)
