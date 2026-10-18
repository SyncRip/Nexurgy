"""Independent annotation check of a drawing package (design §4).

``check_package(pages, *, step_bytes=None, requirements=None)`` re-reads the
files of every page (it never asks the generator) and reports:

- per DXF: parseable, mm units, every layer / entity linetype and text style
  resolvable, layer DIM holds DIMENSION entities only (no loose copies);
- per DIMENSION: native value (stored measurement × DIMLFAC) equals the
  displayed number — a literal text override that states another number
  fails, and so does a rendered block text (what CAD shows before a regen)
  that differs from the native value;
- the page's dimension records (what the generator claims) match the DXF
  handles, native values and texts, with no unrecorded DIMENSION;
- SVG ↔ DXF parity: one SVG dimension group per DIMENSION (same handle, text
  and definition points), the same number of hatches;
- section pages: a HATCH (ANSI31, layer HATCH) inside every section view, the
  same in the SVG, the "SECTION X-X" label and the cutting-line letters;
- required dimensions from the requirement list (design §1 dicts), matched
  through the page records' ``ref`` (never by value alone when a record
  exists): envelope sizes (record axis must be the axis), hole diameters (a Ø
  record on the axis of every entry, or a hole callout on a view — never the
  notes block), blind / counterbore depths and pocket depths (native DIMENSION
  from the required entry to the required floor; a callout is not enough), hole
  positions from the datum (missing → failed, a warning only past the view's
  position-dimension cap), pocket outline size (width / length or Ø) and
  position, band heights (the height or both levels from the datum) and
  diameters (outer and every round void, each on its own axis);
- feature patterns: identical features (equal bores) may be dimensioned once
  with their count — the Ø text starts with ``N× `` (``4× Ø11``) and the
  record's ``pattern`` lists the axis point of each of the N members; such a
  dimension states the diameter on every member's axis (a count that differs
  from the members listed fails the record). Every feature a requirement or
  the STEP survey names needs its own Ø or a pattern Ø listing it;
- with ``step_bytes``: the overall dimensions equal the STEP bounding box, and
  the solid's own features need dimensions whether or not requirements exist
  (``conformance.survey_features``): a native Ø on every bore (its own or its
  pattern's) and every round boss / outline, size and position of every inner
  outline (pocket, through opening, boss footprint), datum positions of every
  hole.

The report is bound to every file's sha256 (and the STEP / requirement
hashes). ``status`` is ``passed`` only when no item failed. This module only
needs ezdxf (OCP is imported lazily for the STEP check).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Literal

import ezdxf
from ezdxf import bbox as dxfbbox
from ezdxf import recover
from ezdxf.entities import DimStyleOverride
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "partroute.annotation_check/1"
VALIDATOR_VERSION = "partroute-annotation-check/2"
NATIVE_TOL = 0.005  # mm: displayed values carry 2 decimals
REQ_TOL = 0.01  # mm: requirement value vs stated dimension
POS_TOL = 0.01  # mm (model): a dimension record's reference (axis point, coordinate) vs a feature
POINT_TOL = 0.02  # mm (paper): SVG vs DXF definition points
#: Position dimensions a view carries per axis (the generator stops there; a missing position past it is a warning).
POSITION_DIM_CAP = 8
SVG_NS = "{http://www.w3.org/2000/svg}"

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
_PATTERN_COUNT = re.compile(r"^(\d+)× ")
_MTEXT_FMT = re.compile(r"\\[A-Za-z][^;\\]*;|\\P|[{}]")
_UNICODE_ESC = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


class AnnotationItem(BaseModel):
    id: str
    check: str
    page: int | None = None
    status: Literal["passed", "failed", "warning"]
    expected: Any = None
    measured: Any = None
    message: str = ""


class PageDigest(BaseModel):
    index: int
    role: str
    files: list[dict[str, Any]]
    dimensions: int = 0
    hatches: int = 0


class AnnotationReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default=SCHEMA, alias="schema")
    validator_version: str = VALIDATOR_VERSION
    status: Literal["passed", "failed"]
    step_sha256: str | None = None
    requirements_sha256: str | None = None
    pages: list[PageDigest] = Field(default_factory=list)
    items: list[AnnotationItem] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def failures(self) -> list[AnnotationItem]:
        return [i for i in self.items if i.status == "failed"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


def _norm_text(s: str) -> str:
    s = _UNICODE_ESC.sub(lambda m: chr(int(m.group(1), 16)), s or "")
    s = _MTEXT_FMT.sub("", s)
    for code, ch in (("%%c", "Ø"), ("%%C", "Ø"), ("%%d", "°"), ("%%D", "°"), ("%%p", "±"), ("%%P", "±"), ("⌀", "Ø"), ("φ", "Ø")):
        s = s.replace(code, ch)
    return re.sub(r"\s+", " ", s).strip()


def _last_number(s: str) -> float | None:
    nums = _NUM.findall(s or "")
    return float(nums[-1]) if nums else None


def _fmt(v: float, dec: int) -> str:
    s = f"{v:.{dec}f}".rstrip("0").rstrip(".") if dec > 0 else f"{v:.0f}"
    return "0" if s in ("-0", "") else s


def pattern_prefix(n: int) -> str:
    """Text prefix of a dimension or callout that states ``n`` identical features at once (``4× Ø11``); empty for
    one. A dimension carrying it lists the axis point of each member in its record's ``pattern``."""
    return f"{n}× " if n > 1 else ""


def pattern_count(text: str) -> int:
    """Members a dimension text states: the count of its ``N× `` prefix, else 1."""
    m = _PATTERN_COUNT.match(text or "")
    return int(m.group(1)) if m else 1


def _is_diameter(text: str) -> bool:
    return _PATTERN_COUNT.sub("", text or "", count=1).startswith("Ø")


def _canonical_sha(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _token_in(texts: list[str], token: str) -> bool:
    pat = re.compile(re.escape(token) + r"(?![\d.])")
    return any(pat.search(t) for t in texts)


class _Items:
    def __init__(self) -> None:
        self.items: list[AnnotationItem] = []

    def add(self, id: str, check: str, ok: bool | None, message: str, *, page: int | None = None, expected: Any = None,
            measured: Any = None, warn: bool = False) -> None:
        status = "passed" if ok else ("warning" if warn else "failed")
        self.items.append(AnnotationItem(id=id, check=check, page=page, status=status, expected=expected, measured=measured,
                                         message=message))


# --------------------------------------------------------------------------- #
# DXF / SVG reading
# --------------------------------------------------------------------------- #


def _read_dims(doc: Any) -> list[dict[str, Any]]:
    out = []
    for dim in doc.modelspace().query("DIMENSION"):
        try:
            meas = dim.get_measurement()
            meas = float(meas.magnitude) if hasattr(meas, "magnitude") else float(meas)
        except Exception:  # noqa: BLE001 - unsupported kinds
            meas = None
        ovr = DimStyleOverride(dim)
        lfac = float(ovr.get("dimlfac", 1.0) or 1.0)
        dec = int(ovr.get("dimdec", 4) if ovr.get("dimdec", 4) is not None else 4)
        native = meas * lfac if meas is not None else None
        raw = dim.dxf.get("text", "") or ""
        literal = raw not in ("", "<>") and "<>" not in raw
        if native is None:
            display = _norm_text(raw)
        elif raw in ("", "<>"):
            display = _fmt(native, dec)
        else:
            display = _norm_text(raw.replace("<>", _fmt(native, dec)))
        shown = None
        geom = dim.dxf.get("geometry")
        block = doc.blocks.get(geom) if geom else None
        if block is not None:
            texts = [_norm_text(e.text if e.dxftype() == "MTEXT" else e.dxf.text) for e in block if e.dxftype() in ("MTEXT", "TEXT")]
            shown = " ".join(t for t in texts if t) or None
        p2 = dim.dxf.get("defpoint2")
        p3 = dim.dxf.get("defpoint3")
        tm = dim.dxf.get("text_midpoint")
        out.append({
            "handle": dim.dxf.handle, "layer": dim.dxf.get("layer"), "dimtype": dim.dimtype, "measurement": meas, "dimlfac": lfac,
            "native": native, "raw": raw, "literal": literal, "display": display, "shown": shown, "has_block": block is not None,
            "p1": (float(p2[0]), float(p2[1])) if p2 is not None else None,
            "p2": (float(p3[0]), float(p3[1])) if p3 is not None else None,
            "text_at": (float(tm[0]), float(tm[1])) if tm is not None else None,
        })
    return out


def _svg_points(d: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", d or "")]
    return list(zip(nums[0::2], nums[1::2]))


def _read_svg(svg: bytes) -> tuple[float, dict[str, dict[str, Any]], list[tuple[float, float, float, float]], list[str]]:
    root = ET.fromstring(svg)
    vb = (root.get("viewBox") or "0 0 420 297").split()
    height = float(vb[3])
    dims: dict[str, dict[str, Any]] = {}
    for g in root.iter(f"{SVG_NS}g"):
        if g.get("class") != "dimension":
            continue
        text_el = next(g.iter(f"{SVG_NS}text"), None)
        p1 = [float(c) for c in (g.get("data-p1") or "").split()]
        p2 = [float(c) for c in (g.get("data-p2") or "").split()]
        dims[g.get("data-handle") or g.get("id") or ""] = {
            "id": g.get("id"), "text": _norm_text(text_el.text if text_el is not None else ""),
            "p1": tuple(p1) if len(p1) == 2 else None, "p2": tuple(p2) if len(p2) == 2 else None,
        }
    hatches = []
    for path in root.iter(f"{SVG_NS}path"):
        if path.get("class") == "hatch":
            pts = _svg_points(path.get("d") or "")
            if pts:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                hatches.append((min(xs), min(ys), max(xs), max(ys)))
    texts = [_norm_text("".join(t.itertext())) for t in root.iter(f"{SVG_NS}text") if t.get("class") == "callout"]
    return height, dims, hatches, texts


def _inside(box: tuple[float, float, float, float], outer: tuple[float, float, float, float], tol: float = 0.5) -> bool:
    return box[0] >= outer[0] - tol and box[1] >= outer[1] - tol and box[2] <= outer[2] + tol and box[3] <= outer[3] + tol


# --------------------------------------------------------------------------- #
# per page
# --------------------------------------------------------------------------- #


def _check_page(page: Any, it: _Items, pool: list[dict[str, Any]], callouts: list[str]) -> PageDigest:
    index = int(_get(page, "index", 0))
    role = str(_get(page, "role", ""))
    svg: bytes = _get(page, "svg", b"") or b""
    dxf: bytes = _get(page, "dxf", b"") or b""
    files = []
    for fmt, data in (("svg", svg), ("dxf", dxf)):
        files.append({"format": fmt, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    digest = PageDigest(index=index, role=role, files=files)
    P = f"P{index}"
    try:
        doc, auditor = recover.read(io.BytesIO(dxf))
    except Exception as exc:  # noqa: BLE001
        it.add(f"{P}:dxf:parse", "dxf_parse", False, f"DXF cannot be read: {str(exc)[:200]}", page=index)
        return digest
    it.add(f"{P}:dxf:parse", "dxf_parse", True, f"DXF {doc.dxfversion} read ({len(auditor.errors)} audit errors)", page=index)
    msp = doc.modelspace()
    it.add(f"{P}:dxf:units", "dxf_units", doc.units == ezdxf.units.MM, f"$INSUNITS = {doc.units} (4 = mm)", page=index,
           expected=4, measured=doc.units)

    # ---- tables: linetypes and text styles resolvable
    ltypes = {lt.dxf.name.casefold() for lt in doc.linetypes}
    missing_lt = sorted({layer.dxf.linetype for layer in doc.layers if layer.dxf.linetype.casefold() not in ltypes}
                        | {e.dxf.linetype for e in msp if e.dxf.hasattr("linetype") and e.dxf.linetype.casefold() not in ltypes | {"bylayer", "byblock"}})
    it.add(f"{P}:dxf:linetypes", "linetypes_resolvable", not missing_lt,
           "every linetype is defined" if not missing_lt else f"undefined linetype(s): {', '.join(missing_lt)}", page=index,
           measured=missing_lt or None)
    styles = {s.dxf.name.casefold(): s for s in doc.styles}
    used = {e.dxf.style for e in msp.query("TEXT MTEXT")}
    for ds in doc.dimstyles:
        if ds.dxf.hasattr("dimtxsty"):
            used.add(ds.dxf.dimtxsty)
    missing_st = sorted(s for s in used if s.casefold() not in styles)
    non_ttf = sorted(s for s in used if s.casefold() in styles
                     and not str(styles[s.casefold()].dxf.font).casefold().endswith((".ttf", ".ttc", ".otf")))
    it.add(f"{P}:dxf:styles", "text_styles", not missing_st,
           "every text style is defined" if not missing_st else f"undefined text style(s): {', '.join(missing_st)}", page=index)
    it.add(f"{P}:dxf:ttf", "text_style_ttf", not non_ttf,
           "text styles use TrueType fonts" if not non_ttf else f"non-TrueType style(s) (Ø ↧ ⌴ / CJK may be missing): {', '.join(non_ttf)}",
           page=index, warn=True)

    # ---- layer DIM holds DIMENSION entities only
    loose = sorted({e.dxftype() for e in msp if e.dxf.get("layer", "").upper() == "DIM" and e.dxftype() != "DIMENSION"})
    it.add(f"{P}:dxf:dim_layer", "dimension_only", not loose,
           "layer DIM holds DIMENSION entities only" if not loose else f"loose {', '.join(loose)} on layer DIM (dimension drawn twice?)",
           page=index)

    # ---- native values
    dims = _read_dims(doc)
    digest.dimensions = len(dims)
    texts = [(_norm_text(e.text if e.dxftype() == "MTEXT" else e.dxf.text), e) for e in msp.query("TEXT MTEXT")]
    for d in dims:
        h = d["handle"]
        native = d["native"]
        if native is None:
            it.add(f"{P}:dim:{h}:native", "dimension_native", False, "DIMENSION has no measurable native value", page=index)
            continue
        if d["literal"]:
            num = _last_number(d["display"])
            ok = num is not None and abs(num - native) <= NATIVE_TOL
            it.add(f"{P}:dim:{h}:override", "dimension_native", ok,
                   f"text override '{d['display']}' " + ("equals" if ok else "differs from") + f" the native value {native:.4g} "
                   f"(measurement {d['measurement']:.4g} × DIMLFAC {d['dimlfac']:.4g})", page=index, expected=native, measured=num)
        else:
            it.add(f"{P}:dim:{h}:native", "dimension_native", True,
                   f"native value {native:.4g} = measurement {d['measurement']:.4g} × DIMLFAC {d['dimlfac']:.4g}, shown via '<>'",
                   page=index, expected=native, measured=native)
        shown_num = _last_number(d["shown"] or "")
        ok = d["has_block"] and shown_num is not None and abs(shown_num - native) <= NATIVE_TOL
        it.add(f"{P}:dim:{h}:rendered", "dimension_rendered", ok,
               f"rendered text '{d['shown']}' " + ("matches" if ok else "does not match") + f" the native value {native:.4g}"
               + ("" if d["has_block"] else " (no dimension block)"), page=index, expected=native, measured=shown_num)
        # a loose TEXT copy of the dimension text at the dimension (old double drawing)
        anchor = d["text_at"] or ((d["p1"][0] + d["p2"][0]) / 2.0, (d["p1"][1] + d["p2"][1]) / 2.0) if d["p1"] and d["p2"] else None
        if anchor is not None:
            dup = [t for t, e in texts if t == d["display"] and abs(e.dxf.insert[0] - anchor[0]) < 10 and abs(e.dxf.insert[1] - anchor[1]) < 10]
            if dup:
                it.add(f"{P}:dim:{h}:duplicate", "dimension_only", False, f"loose TEXT '{d['display']}' duplicates the DIMENSION",
                       page=index)
        pool.append({"page": index, "handle": h, "native": native, "text": d["shown"] or d["display"], "record": None})

    # ---- records (generator claims) vs DXF
    by_handle = {d["handle"]: d for d in dims}
    records = [_dump(r) for r in (_get(page, "dimensions", None) or [])]
    rec_handles = set()
    for r in records:
        h = r.get("handle", "")
        rec_handles.add(h)
        d = by_handle.get(h)
        if d is None:
            it.add(f"{P}:rec:{r.get('id')}", "records_match", False, f"record {r.get('id')} ({r.get('measured')}) has no DIMENSION {h}",
                   page=index)
            continue
        native = d["native"] if d["native"] is not None else float("nan")
        same_val = abs(native - float(r.get("value", 0.0))) <= NATIVE_TOL
        same_txt = _norm_text(r.get("text", "")) == d["display"] == (d["shown"] or d["display"])
        stated, members = pattern_count(d["display"]), max(1, len(r.get("pattern") or []))
        it.add(f"{P}:rec:{r.get('id')}", "records_match", same_val and same_txt and stated == members,
               f"{r.get('kind')} {r.get('measured')}: record {r.get('value')} '{r.get('text')}', DXF native {native:.4g} "
               f"displayed '{d['display']}' rendered '{d['shown']}'"
               + ("" if stated == members else f"; the text states {stated} feature(s), the record lists {members}"),
               page=index, expected=r.get("value"), measured=native)
        for p in pool:
            if p["page"] == index and p["handle"] == h:
                p["record"] = r
    if records:
        extra = sorted(h for h in by_handle if h not in rec_handles)
        it.add(f"{P}:rec:complete", "records_match", not extra,
               "every DIMENSION is recorded" if not extra else f"unrecorded DIMENSION(s): {', '.join(extra)}", page=index)

    # ---- SVG parity
    try:
        sheet_h, sdims, shatches, stexts = _read_svg(svg)
    except Exception as exc:  # noqa: BLE001
        it.add(f"{P}:svg:parse", "svg_parse", False, f"SVG cannot be parsed: {str(exc)[:200]}", page=index)
        sheet_h, sdims, shatches, stexts = 297.0, {}, [], []
    else:
        it.add(f"{P}:svg:parse", "svg_parse", True, "SVG parsed", page=index)
    it.add(f"{P}:svg:dim_count", "svg_dxf_parity", len(sdims) == len(dims),
           f"SVG dimensions {len(sdims)}, DXF DIMENSIONs {len(dims)}", page=index, expected=len(dims), measured=len(sdims))
    for d in dims:
        s = sdims.get(d["handle"])
        if s is None:
            it.add(f"{P}:svg:{d['handle']}", "svg_dxf_parity", False, f"DIMENSION {d['handle']} has no SVG dimension", page=index)
            continue
        problems = []
        if s["text"] != (d["shown"] or d["display"]):
            problems.append(f"text SVG '{s['text']}' vs DXF '{d['shown'] or d['display']}'")
        for key in ("p1", "p2"):
            if s[key] is None or d[key] is None or abs(s[key][0] - d[key][0]) > POINT_TOL or abs((sheet_h - s[key][1]) - d[key][1]) > POINT_TOL:
                problems.append(f"{key} SVG {s[key]} vs DXF {d[key]}")
        it.add(f"{P}:svg:{d['handle']}", "svg_dxf_parity", not problems,
               "SVG dimension matches the DIMENSION" if not problems else "; ".join(problems), page=index)
    # hole callouts on the views only (layer CALLOUT / SVG class "callout"), never the notes block
    callouts.extend(t for t, e in texts if str(e.dxf.get("layer", "")).upper() == "CALLOUT")
    callouts.extend(stexts)

    # ---- hatches
    hatches = list(msp.query("HATCH"))
    digest.hatches = len(hatches)
    it.add(f"{P}:svg:hatch_count", "svg_dxf_parity", len(hatches) == len(shatches),
           f"SVG hatches {len(shatches)}, DXF HATCH {len(hatches)}", page=index, expected=len(hatches), measured=len(shatches))
    if role == "section":
        hboxes = []
        for h in hatches:
            ext = dxfbbox.extents([h])
            if ext.has_data:
                hboxes.append(((ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y), h))
        views = [_dump(v) for v in (_get(page, "views", None) or [])]
        sviews = [v for v in views if v.get("kind") == "section"]
        if not sviews:
            it.add(f"{P}:section:view", "section_hatch", False, "section page has no section view", page=index)
        for v in sviews:
            x0, y0, x1, y1 = v["bbox"]
            box_up = (x0, sheet_h - y1, x1, sheet_h - y0)
            inside = [h for b, h in hboxes if _inside(b, box_up)]
            good = [h for h in inside if h.dxf.layer.upper() == "HATCH" and str(h.dxf.pattern_name).upper() == "ANSI31"]
            it.add(f"{P}:section:{v['name']}:hatch", "section_hatch", bool(good),
                   f"{len(good)} ANSI31 HATCH on layer HATCH inside {v.get('label') or v['name']}" if good
                   else f"no ANSI31 HATCH inside {v.get('label') or v['name']} ({len(inside)} hatch entities there)", page=index)
            s_in = [b for b in shatches if _inside(b, (x0, y0, x1, y1))]
            it.add(f"{P}:section:{v['name']}:svg_hatch", "section_hatch", bool(s_in),
                   f"{len(s_in)} hatched SVG region(s) inside {v.get('label') or v['name']}", page=index)
            label = str(v.get("label") or "")
            m = re.fullmatch(r"SECTION ([A-Z])-\1", label)
            letter = m.group(1) if m else None
            dxf_texts = [t for t, _ in texts]
            ok_label = bool(letter) and label in dxf_texts and sum(1 for t in dxf_texts if t == letter) >= 2
            it.add(f"{P}:section:{v['name']}:labels", "section_labels", ok_label,
                   f"'{label}' and two cutting-line letters present" if ok_label else f"missing '{label}' or the cutting-line letters",
                   page=index)
    return digest


# --------------------------------------------------------------------------- #
# requirements and STEP
# --------------------------------------------------------------------------- #


def _find(pool: list[dict[str, Any]], value: float, *, kinds: tuple[str, ...] | None = None, diameter: bool | None = None,
          axis: str | None = None) -> list[dict[str, Any]]:
    """Dimensions stating ``value``. ``kinds`` / ``axis`` filter by the page record when there is one (a
    record without an axis never matches a required axis); pages without records match by value only."""
    out = []
    for p in pool:
        if abs(p["native"] - value) > REQ_TOL:
            continue
        if diameter is not None and _is_diameter(p["text"]) != diameter:
            continue
        rec = p["record"]
        if kinds is not None and rec is not None and rec.get("kind") not in kinds:
            continue
        if axis is not None and rec is not None and rec.get("axis") != axis:
            continue
        out.append(p)
    return out


def _where(hits: list[dict[str, Any]]) -> str:
    h = hits[0]
    kind = h["record"]["kind"] if h["record"] else "dimension"
    return f"{kind} '{h['text']}' (sheet {h['page']}, {h['handle']})"


def _f(v: Any) -> str:
    return _fmt(float(v), 3)


V3 = tuple[float, float, float]
AXES = "xyz"
LOOK_VIEW = {0: "right", 1: "front", 2: "top"}  # the view looking down a model axis


def _unit(v: Any) -> V3 | None:
    try:
        x, y, z = (float(c) for c in v)
    except (TypeError, ValueError):
        return None
    n = (x * x + y * y + z * z) ** 0.5
    return (x / n, y / n, z / n) if n > 1e-12 else None


def _dominant(d: V3) -> int | None:
    """Index of the model axis ``d`` runs along, None when oblique."""
    i = max(range(3), key=lambda k: abs(d[k]))
    return i if abs(d[i]) >= 0.9999 else None


def _ref_axis(ref: dict[str, Any]) -> tuple[V3, V3 | None] | None:
    try:
        p = (float(ref["x"]), float(ref["y"]), float(ref["z"]))
    except (KeyError, TypeError, ValueError):
        return None
    d = _unit((ref["dx"], ref["dy"], ref["dz"])) if all(k in ref for k in ("dx", "dy", "dz")) else None
    return p, d


def _on_axis(ref: dict[str, Any], point: V3, direction: V3) -> bool:
    """Whether a diameter record's axis (``ref.x/y/z`` + ``ref.dx/dy/dz``) is the line through ``point`` along
    ``direction``."""
    ax = _ref_axis(ref)
    if ax is None:
        return False
    q, d = ax
    if d is not None and abs(sum(a * b for a, b in zip(d, direction))) < 0.9999:
        return False
    w = tuple(point[i] - q[i] for i in range(3))
    t = sum(w[i] * direction[i] for i in range(3))
    off = tuple(w[i] - t * direction[i] for i in range(3))
    return (off[0] ** 2 + off[1] ** 2 + off[2] ** 2) ** 0.5 <= POS_TOL


def _member_refs(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The axes a diameter record states: its own (``ref``) and, for a pattern, each member's (``pattern`` points along
    the record's direction)."""
    ref = rec.get("ref") or {}
    direction = {k: ref[k] for k in ("dx", "dy", "dz") if k in ref}
    return [ref] + [{"x": m[0], "y": m[1], "z": m[2], **direction} for m in rec.get("pattern") or [] if len(m) == 3]


def _diameters(pool: list[dict[str, Any]], value: float, axes: list[tuple[V3, V3]]) -> list[dict[str, Any]]:
    """Ø dimensions of ``value`` whose record ties them to one of ``axes`` (point, direction), as the feature's own Ø
    or a pattern Ø listing it; dimensions without a page record (foreign DXF) match by value."""
    out = []
    for p in pool:
        if abs(p["native"] - value) > REQ_TOL or not _is_diameter(p["text"]):
            continue
        rec = p["record"]
        if rec is None or (rec.get("kind") == "diameter"
                           and any(_on_axis(ref, q, d) for ref in _member_refs(rec) for q, d in axes)):
            out.append(p)
    return out


def _diameters_each(pool: list[dict[str, Any]], value: float, axes: list[tuple[V3, V3]]) -> tuple[list[dict[str, Any]], list[V3]]:
    """The Ø ``value`` stated on every one of ``axes`` (the entries of a requirement, the bores of a group): the
    dimensions stating it (none unless every axis is covered) and the axis points no dimension states it on."""
    hits: list[dict[str, Any]] = []
    missing: list[V3] = []
    for ax in axes:
        found = _diameters(pool, value, [ax])
        if not found:
            missing.append(ax[0])
        hits += [p for p in found if all(p is not h for h in hits)]
    return ([] if missing else hits), missing


def _points(points: list[V3]) -> str:
    return ", ".join(f"({_f(q[0])}, {_f(q[1])}, {_f(q[2])})" for q in points[:4]) + (" …" if len(points) > 4 else "")


class _Datum:
    """Datum coordinates the position dimensions start from: their own ``ref.datum``, else the STEP bbox min."""

    def __init__(self, pool: list[dict[str, Any]], bbox_min: list[float] | None):
        self.by_axis: dict[str, float] = {}
        if bbox_min is not None:
            self.by_axis = {a: float(v) for a, v in zip(AXES, bbox_min)}
        for p in pool:
            rec = p["record"] or {}
            if rec.get("kind") == "position" and rec.get("axis") in AXES and "datum" in (rec.get("ref") or {}):
                self.by_axis.setdefault(rec["axis"], float(rec["ref"]["datum"]))

    def on(self, axis: str, coord: float) -> bool:
        return axis in self.by_axis and abs(self.by_axis[axis] - coord) <= POS_TOL


def _positions(pool: list[dict[str, Any]], axis: str, coords: list[float]) -> list[dict[str, Any]]:
    return [p for p in pool if (p["record"] or {}).get("kind") == "position" and p["record"].get("axis") == axis
            and any(abs(float((p["record"].get("ref") or {}).get("coord", 1e300)) - c) <= POS_TOL for c in coords)]


def _need_positions(it: _Items, pool: list[dict[str, Any]], datum: _Datum, iid: str, what: str, axis: str, view: str,
                    targets: list[list[float]]) -> None:
    """Every target (alternative coordinates of one feature along ``axis``, e.g. edge or centre) needs a position
    dimension from the datum, or lies on the datum. Past the position-dimension cap of the view a missing one is a
    visible warning."""
    missing = []
    for alts in targets:
        if any(datum.on(axis, c) for c in alts) or _positions(pool, axis, alts):
            continue
        missing.append(alts[0])
    n = len(targets)
    if not missing:
        it.add(iid, "required_position", True, f"{what}: {axis.upper()} position{'s' if n > 1 else ''} from the datum "
               f"dimensioned ({n})", expected=[a[0] for a in targets])
        return
    shown = sum(1 for p in pool if (p["record"] or {}).get("kind") == "position" and p["record"].get("axis") == axis
                and p["record"].get("view") == view)
    capped = shown >= POSITION_DIM_CAP
    msg = (f"{what}: {axis.upper()} position {', '.join(_f(c) for c in missing[:8])} not dimensioned from the datum"
           + (f" (the {view} view already carries the cap of {POSITION_DIM_CAP} {axis.upper()} position dimensions)" if capped else ""))
    it.add(iid, "required_position", False, msg, expected=missing, warn=capped)


def _check_requirements(reqs: list[dict[str, Any]], pool: list[dict[str, Any]], callouts: list[str], it: _Items,
                        datum: _Datum, part_box: tuple[list[float], list[float]] | None) -> None:
    # Z levels the drawing defines: datum and levels / heights from it, plus
    # floors of Z depths whose entry face is itself a defined level.
    levels: list[float] = []
    for p in pool:
        rec = p["record"] or {}
        ref = rec.get("ref") or {}
        if rec.get("kind") in ("level", "height") and "z" in ref:
            levels.append(float(ref["z"]))
            if "datum_z" in ref:
                levels.append(float(ref["datum_z"]))
    depths = [(p["record"] or {}).get("ref") or {} for p in pool
              if (p["record"] or {}).get("kind") == "depth" and (p["record"] or {}).get("axis") == "z"]
    grown = True
    while grown:
        grown = False
        for ref in depths:
            if "entry" in ref and "floor" in ref and any(abs(z - float(ref["entry"])) <= REQ_TOL for z in levels) \
                    and not any(abs(z - float(ref["floor"])) <= REQ_TOL for z in levels):
                levels.append(float(ref["floor"]))
                grown = True

    has_records = any(p["record"] is not None for p in pool)

    def loose(token: str) -> str | None:
        """A view callout states a diameter only on pages without dimension records: with records every Ø is a
        native DIMENSION tied to its feature (a callout text cannot be tied to one)."""
        return None if has_records else token

    def done(rid: str, what: str, value: float, hits: list[dict[str, Any]], *, callout: str | None = None,
             native_only: bool = False, missing: str = "") -> None:
        if hits:
            it.add(f"req:{rid}:{what}", "required_dimension", True, f"{what} {_f(value)} stated by {_where(hits)}", expected=value,
                   measured=hits[0]["native"])
            return
        if callout and _token_in(callouts, callout):
            if native_only:
                it.add(f"req:{rid}:{what}", "required_dimension", False,
                       f"{what} {_f(value)} only appears in a callout ('{callout}'), no native DIMENSION states it", expected=value)
            else:
                it.add(f"req:{rid}:{what}", "required_dimension", True, f"{what} {_f(value)} stated by the view callout '{callout}'",
                       expected=value)
            return
        it.add(f"req:{rid}:{what}", "required_dimension", False, f"no dimension states {what} {_f(value)}{missing}", expected=value)

    def depth_hits(value: float, axis: str | None, entry: float | None, floor: float | None, *,
                   on: tuple[V3, V3] | None = None, inside: tuple[float, float, float, float] | None = None) -> list[dict[str, Any]]:
        """Depth DIMENSIONs of ``value`` from ``entry`` to ``floor`` (levels along ``axis``), measured on the hole's
        axis (``on``) or on a floor inside the pocket outline (``inside``) when the record says where."""
        hits = _find(pool, value, kinds=("depth",), axis=axis)
        if entry is None or floor is None:
            return hits
        out = []
        for p in hits:
            ref = (p["record"] or {}).get("ref") or {}
            if p["record"] is not None and (abs(float(ref.get("entry", 1e300)) - entry) > REQ_TOL
                                            or abs(float(ref.get("floor", 1e300)) - floor) > REQ_TOL):
                continue
            if on is not None and "x" in ref and not _on_axis(ref, *on):
                continue
            if inside is not None and "x" in ref and not (inside[0] - POS_TOL <= float(ref["x"]) <= inside[2] + POS_TOL
                                                          and inside[1] - POS_TOL <= float(ref["y"]) <= inside[3] + POS_TOL):
                continue
            out.append(p)
        return out

    for r in reqs:
        rid = str(r.get("id") or "?")
        kind = r.get("kind")
        if kind == "envelope":
            for axis, v in zip("xyz", r.get("size") or []):
                if v is not None:
                    done(rid, f"envelope {axis.upper()}", float(v), _find(pool, float(v), kinds=("overall", "height"), diameter=False, axis=axis))
        elif kind == "hole":
            d = _unit(r.get("direction") or (0.0, 0.0, -1.0)) or (0.0, 0.0, -1.0)
            dom = _dominant(d)
            entries = [tuple(float(c) for c in e) for e in (r.get("entries") or []) if len(e) == 3]
            axes = [(e, d) for e in entries]

            def on_entries(value: float, axes: list[tuple[V3, V3]] = axes) -> tuple[list[dict[str, Any]], str]:
                """The Ø ``value`` on the axis of every entry (its own Ø or a pattern Ø listing it)."""
                if not axes:
                    return _find(pool, value, kinds=("diameter",), diameter=True), ""
                hits, miss = _diameters_each(pool, value, axes)
                return hits, " on the hole's axis" + (f" at {_points(miss)}" if len(axes) > 1 else "")

            dia = r.get("diameter")
            if dia is not None:
                hits, where = on_entries(float(dia))
                done(rid, "diameter", float(dia), hits, callout=loose(f"Ø{_f(dia)}"), missing=where)
            for label, depth in (("depth", None if r.get("through") else r.get("depth")),
                                 ("counterbore depth", (r.get("counterbore") or {}).get("depth"))):
                if depth is None:
                    continue
                if dom is not None and len(entries) >= 1:
                    a = AXES[dom]
                    hits = [p for e in entries for p in depth_hits(float(depth), a, e[dom], e[dom] + d[dom] * float(depth), on=(e, d))]
                else:
                    hits = depth_hits(float(depth), None, None, None)
                done(rid, label, float(depth), hits, callout=f"↧{_f(depth)}", native_only=True)
            cb = r.get("counterbore")
            if cb:
                hits, where = on_entries(float(cb["diameter"]))
                done(rid, "counterbore diameter", float(cb["diameter"]), hits, callout=loose(f"Ø{_f(cb['diameter'])}"),
                     missing=where)
            cs = r.get("countersink")
            if cs:
                hits, where = on_entries(float(cs["diameter"])) if axes else ([], " on the hole's axis")
                done(rid, "countersink diameter", float(cs["diameter"]), hits, callout=loose(f"⌵Ø{_f(cs['diameter'])}"),
                     missing=where)
            if entries:
                if dom is None:
                    it.add(f"req:{rid}:position", "required_position", False,
                           f"hole {rid} is oblique: its position is not dimensioned (listed in the notes only)", warn=True)
                else:
                    for k in range(3):
                        if k != dom:
                            _need_positions(it, pool, datum, f"req:{rid}:position:{AXES[k]}", f"hole {rid}", AXES[k], LOOK_VIEW[dom],
                                            [[c] for c in sorted({round(e[k], 6) for e in entries})])
            if r.get("thread"):
                it.add(f"req:{rid}:thread", "required_annotation", False, f"thread {r['thread']} is not annotated (see coverage)",
                       warn=True)
        elif kind == "pocket":
            entry_z = r.get("entry_z")
            if not r.get("through") and r.get("depth") is not None:
                depth = float(r["depth"])
                floor = float(entry_z) - depth if entry_z is not None else None
                done(rid, "depth", depth, depth_hits(depth, "z", float(entry_z) if entry_z is not None else None, floor,
                                                     inside=_contour_box(r.get("contour") or {})), native_only=True)
            _pocket_outline(rid, r.get("contour") or {}, pool, callouts, it, datum, part_box)
        elif kind == "band":
            z0, z1 = r.get("z0"), r.get("z1")
            if z0 is not None and z1 is not None:
                h = float(z1) - float(z0)
                direct = _find(pool, h, kinds=("level", "height", "depth", "overall"), diameter=False)
                by_levels = any(abs(z - float(z0)) <= REQ_TOL for z in levels) and any(abs(z - float(z1)) <= REQ_TOL for z in levels)
                ok = bool(direct) or by_levels
                how = (_where(direct) if direct else "levels from the datum") if ok else ""
                it.add(f"req:{rid}:height", "required_dimension", ok,
                       f"band Z {_f(z0)}…{_f(z1)} (height {_f(h)}) stated by {how}" if ok else f"band Z {_f(z0)}…{_f(z1)} is not dimensioned",
                       expected=[z0, z1])
            for what, c in [("outer diameter", r.get("outer") or {})] + [(f"void {i + 1} diameter", v) for i, v in enumerate(r.get("voids") or [])]:
                if c.get("kind") == "circle" and c.get("diameter") is not None:
                    cx, cy = (float(v) for v in c.get("center") or (0.0, 0.0))
                    zc = float(z0) if z0 is not None else 0.0
                    hits = _diameters(pool, float(c["diameter"]), [((cx, cy, zc), (0.0, 0.0, 1.0))])
                    done(rid, what, float(c["diameter"]), hits, callout=loose(f"Ø{_f(c['diameter'])}") if what != "outer diameter" else None,
                         missing=" on its axis")


def _contour_box(contour: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        if contour.get("kind") == "circle":
            cx, cy = (float(v) for v in contour["center"])
            h = float(contour["diameter"]) / 2
            return (cx - h, cy - h, cx + h, cy + h)
        if contour.get("kind") == "rect":
            cx, cy = (float(v) for v in contour["center"])
            w, ln = (float(v) for v in contour["size"])
            return (cx - w / 2, cy - ln / 2, cx + w / 2, cy + ln / 2)
        if contour.get("kind") == "polygon":
            xs = [float(p[0]) for p in contour["points"]]
            ys = [float(p[1]) for p in contour["points"]]
            return (min(xs), min(ys), max(xs), max(ys))
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _pocket_outline(rid: str, contour: dict[str, Any], pool: list[dict[str, Any]], callouts: list[str], it: _Items, datum: _Datum,
                    part_box: tuple[list[float], list[float]] | None) -> None:
    """A pocket outline needs its size (width / length, or Ø) and its position from the datum, in the view looking
    down the pocket (top)."""
    kind = contour.get("kind")
    if kind == "circle":
        cx, cy = (float(v) for v in contour.get("center") or (0.0, 0.0))
        dia = float(contour.get("diameter") or 0.0)
        box = (cx - dia / 2, cy - dia / 2, cx + dia / 2, cy + dia / 2)
    elif kind == "rect":
        cx, cy = (float(v) for v in contour.get("center") or (0.0, 0.0))
        w, ln = (float(v) for v in contour.get("size") or (0.0, 0.0))
        box = (cx - w / 2, cy - ln / 2, cx + w / 2, cy + ln / 2)
    elif kind == "polygon" and contour.get("points"):
        xs = [float(p[0]) for p in contour["points"]]
        ys = [float(p[1]) for p in contour["points"]]
        box = (min(xs), min(ys), max(xs), max(ys))
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    else:
        return
    if part_box is not None:
        lo, hi = part_box
        if box[0] < lo[0] + POS_TOL or box[1] < lo[1] + POS_TOL or box[2] > hi[0] - POS_TOL or box[3] > hi[1] - POS_TOL:
            it.add(f"req:{rid}:outline", "required_dimension", False,
                   f"pocket {rid} outline reaches the part boundary (an open slot or step): its size and position are not "
                   "required as an inner outline; check the section levels", warn=True)
            return
    if kind == "circle":
        hits = _diameters(pool, dia, [((cx, cy, 0.0), (0.0, 0.0, 1.0))])
        ok = bool(hits) or (not any(p["record"] for p in pool) and _token_in(callouts, f"Ø{_f(dia)}"))
        it.add(f"req:{rid}:outline", "required_dimension", ok,
               f"pocket outline Ø{_f(dia)} " + (f"stated by {_where(hits)}" if hits else "stated by a view callout" if ok else "not dimensioned"),
               expected=dia)
        targets = {"x": [[cx]], "y": [[cy]]}
    else:
        missing = []
        for axis, lo_v, hi_v in (("x", box[0], box[2]), ("y", box[1], box[3])):
            size = hi_v - lo_v
            hits = [p for p in _find(pool, size, kinds=("size",), axis=axis)
                    if p["record"] is None or abs(float((p["record"].get("ref") or {}).get("lo", lo_v)) - lo_v) <= POS_TOL]
            if not hits:
                missing.append(f"{axis.upper()} {_f(size)}")
        it.add(f"req:{rid}:outline", "required_dimension", not missing,
               "pocket outline width and length dimensioned" if not missing else f"pocket {rid} outline size {', '.join(missing)} not dimensioned",
               expected=[box[2] - box[0], box[3] - box[1]])
        targets = {"x": [[box[0], cx]], "y": [[box[1], cy]]}
    for axis, alts in targets.items():
        _need_positions(it, pool, datum, f"req:{rid}:position:{axis}", f"pocket {rid}", axis, "top", alts)


def _check_geometry(survey: Any, pool: list[dict[str, Any]], it: _Items, datum: _Datum) -> None:
    """What the solid itself needs on the drawing, with or without a requirement list (e.g. an imported STEP):
    a native Ø DIMENSION tied to every hole group and round boss, size and datum position of every inner outline
    (pocket, through opening, boss footprint), datum positions of every hole."""
    groups: dict[tuple, list[Any]] = {}
    for h in survey.holes:
        groups.setdefault((round(h.diameter, 3), tuple(round(c, 4) for c in h.direction)), []).append(h)
    for (dia, direction), hs in sorted(groups.items()):
        d = _unit(direction) or (0.0, 0.0, 1.0)
        dom = _dominant(d)
        where = ", ".join(f"({_f(h.point[0])}, {_f(h.point[1])}, {_f(h.point[2])})" for h in hs[:3]) + (" …" if len(hs) > 3 else "")
        iid = f"geom:hole:Ø{_f(dia)}:{'xyz'[dom] if dom is not None else 'oblique'}:{len(hs)}"
        if dom is None:
            it.add(iid, "required_dimension", False, f"{len(hs)} oblique Ø{_f(dia)} hole(s): not dimensioned on the views", warn=True)
            continue
        axes = [(tuple(float(c) for c in h.point), d) for h in hs]
        hits, miss = _diameters_each(pool, dia, axes)  # type: ignore[arg-type]
        it.add(iid, "required_dimension", not miss,
               f"Ø{_f(dia)} hole group ({len(hs)} bore(s), axis {'XYZ'[dom]}) "
               + (f"stated by {_where(hits)}" if not miss else f"has no native Ø dimension (bores at {where})"
                  if len(miss) == len(hs) else f"has no native Ø dimension on {len(miss)} of its bores ({_points(miss)})"),
               expected=dia)
        for k in range(3):
            if k != dom:
                _need_positions(it, pool, datum, f"{iid}:position:{AXES[k]}", f"Ø{_f(dia)} hole group", AXES[k], LOOK_VIEW[dom],
                                [[c] for c in sorted({round(h.point[k], 6) for h in hs})])
    for b in survey.bosses:
        d = _unit(b.direction) or (0.0, 0.0, 1.0)
        dom = _dominant(d)
        iid = f"geom:boss:Ø{_f(b.diameter)}:({_f(b.point[0])}, {_f(b.point[1])}, {_f(b.point[2])})"
        if dom is None:
            it.add(iid, "required_dimension", False, f"oblique round outline Ø{_f(b.diameter)}: not dimensioned", warn=True)
            continue
        hits = _diameters(pool, b.diameter, [(tuple(float(c) for c in b.point), d)])  # type: ignore[list-item]
        it.add(iid, "required_dimension", bool(hits), f"round outline / boss Ø{_f(b.diameter)} (axis {'XYZ'[dom]}) "
               + (f"stated by {_where(hits)}" if hits else "has no native Ø dimension"), expected=b.diameter)
    for o in survey.outlines:
        x0, y0, x1, y1 = o.bbox
        iid = f"geom:outline:({_f(x0)}, {_f(y0)})-({_f(x1)}, {_f(y1)})"
        missing = []
        for axis, lo_v, hi_v in (("x", x0, x1), ("y", y0, y1)):
            hits = [p for p in _find(pool, hi_v - lo_v, kinds=("size",), axis=axis)
                    if p["record"] is None or abs(float((p["record"].get("ref") or {}).get("lo", lo_v)) - lo_v) <= POS_TOL]
            if not hits:
                missing.append(f"{axis.upper()} {_f(hi_v - lo_v)}")
        it.add(f"{iid}:size", "required_dimension", not missing,
               f"inner outline {_f(x1 - x0)} × {_f(y1 - y0)} at Z {_f(o.z)} (pocket / opening / boss footprint) "
               + ("sized" if not missing else f"has no size dimension {', '.join(missing)}"), expected=[x1 - x0, y1 - y0])
        for axis, alts in (("x", [x0, (x0 + x1) / 2]), ("y", [y0, (y0 + y1) / 2])):
            _need_positions(it, pool, datum, f"{iid}:position:{axis}", f"inner outline at ({_f(x0)}, {_f(y0)})", axis, "top", [alts])


def _step_bbox(step_bytes: bytes) -> tuple[float, float, float]:
    from OCP.IFSelect import IFSelect_ReturnStatus

    from .bounds import tight_bounds
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    status = reader.ReadStream("check.step", io.BytesIO(step_bytes))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError(f"STEP read status {status}")
    reader.TransferRoots()
    shape = reader.OneShape()
    lo, hi = tight_bounds(shape)  # the optimal box, tightened on trimmed free-form faces (ground coil ends)
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


# --------------------------------------------------------------------------- #
# public
# --------------------------------------------------------------------------- #


def check_package(pages: list[Any], *, step_bytes: bytes | None = None, requirements: list[Any] | None = None,
                  features: list[Any] | None = None) -> AnnotationReport:
    """Check every page of a drawing package (``DrawingPage`` objects or dicts
    with ``index, role, svg, dxf, views, dimensions``). ``features``: the kernel's feature records of the revision the
    STEP came from; the letters of its text ops are left out of the STEP survey (generation reliability M)."""
    it = _Items()
    pool: list[dict[str, Any]] = []
    callouts: list[str] = []
    digests: list[PageDigest] = []
    if not pages:
        it.add("package:pages", "package", False, "the package has no pages")
    for page in pages:
        digests.append(_check_page(page, it, pool, callouts))

    survey = None
    step_sha = None
    if step_bytes is not None:
        step_sha = hashlib.sha256(step_bytes).hexdigest()
        try:
            from .conformance import survey_features
            from .text_geometry import text_regions

            survey = survey_features(step_bytes, text_regions(features))
        except Exception as exc:  # noqa: BLE001
            it.add("step:survey", "required_dimension", False, f"the STEP features could not be surveyed: {str(exc)[:200]}")
        else:
            if survey.error:
                it.add("step:survey", "required_dimension", False, f"the STEP features could not be surveyed: {survey.error}")
                survey = None
    box = (survey.bbox_min, survey.bbox_max) if survey is not None and survey.bbox_min and survey.bbox_max else None
    datum = _Datum(pool, box[0] if box else None)

    reqs = [_dump(r) for r in (requirements or [])]
    reqs = [r for r in reqs if isinstance(r, dict)]
    if reqs:
        _check_requirements(reqs, pool, callouts, it, datum, box)
    if survey is not None:
        _check_geometry(survey, pool, it, datum)

    if step_bytes is not None:
        try:
            size = _step_bbox(step_bytes)
        except Exception as exc:  # noqa: BLE001
            it.add("step:read", "step_bbox", False, f"STEP cannot be read: {str(exc)[:200]}")
        else:
            overall = [p for p in pool if p["record"] and p["record"].get("kind") == "overall" and p["record"].get("axis") in ("x", "y", "z")]
            for axis, v in zip("xyz", size):
                mine = [p for p in overall if p["record"]["axis"] == axis]
                if mine:
                    bad = [p for p in mine if abs(p["native"] - v) > REQ_TOL]
                    it.add(f"step:overall:{axis}", "step_bbox", not bad,
                           f"overall {axis.upper()} {_f(mine[0]['native'])} vs STEP bounding box {_f(v)}", expected=round(v, 6),
                           measured=mine[0]["native"])
                else:
                    hits = _find(pool, v)
                    it.add(f"step:overall:{axis}", "step_bbox", bool(hits),
                           f"STEP size {axis.upper()} {_f(v)} " + ("stated" if hits else "not stated by any dimension"),
                           expected=round(v, 6), warn=True)

    failed = any(i.status == "failed" for i in it.items)
    return AnnotationReport(
        status="failed" if failed else "passed",
        step_sha256=step_sha,
        requirements_sha256=_canonical_sha(reqs) if requirements is not None else None,
        pages=digests,
        items=it.items,
    )
