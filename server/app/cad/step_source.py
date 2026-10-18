"""STEP source audit (design §3 of .trellis/tasks/archive/2026-10/10-08-cad-ai/design.md).

:func:`audit_step` reads the raw uploaded bytes BEFORE any repair or fusion
(N07) and decides whether they may become a model base: exactly one valid,
positive-volume solid in one part. Everything else gets a refusal code:

``step_unreadable``  not ISO 10303-21, or OCCT cannot read / transfer it;
``step_assembly``    more than one part definition or instance (XCAF);
``step_no_solid``    surfaces / wireframe only;
``step_multibody``   one part with several solids;
``step_invalid``     the single solid fails ``BRepCheck`` or has no positive volume.

Facts come from the STEP text (header, declared length unit, product names;
non-conforming raw GBK / UTF-8 bytes are decoded defensively) and from OCCT
(``STEPCAFControl_Reader`` + ``XCAFDoc_ShapeTool`` for products and
instances, plain shape exploration for solids / shells / free faces). OCCT
converts coordinates to mm, so volumes and boxes are in mm whatever the
declared unit. Pure: no DB, no kernel import.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Literal

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
from pydantic import BaseModel, Field

from .results import BBox, Issue

try:  # OCP >= 7.8 moved NCollection sequences to OCP.collections
    from OCP.collections import Sequence_TDF_Label as _LabelSeq
except ImportError:  # pragma: no cover - older OCP
    from OCP.TDF import TDF_LabelSequence as _LabelSeq  # type: ignore[no-redef]

AuditCode = Literal["accepted", "step_assembly", "step_multibody", "step_no_solid", "step_invalid", "step_unreadable"]

AUDIT_SCHEMA = "partroute.step_audit/1"
MAX_SOLID_DETAILS = 200
MAX_PRODUCT_NAMES = 100

_SI_PREFIX_MM = {
    "": ("m", 1000.0),
    "KILO": ("km", 1e6),
    "HECTO": ("hm", 1e5),
    "DECA": ("dam", 1e4),
    "DECI": ("dm", 100.0),
    "CENTI": ("cm", 10.0),
    "MILLI": ("mm", 1.0),
    "MICRO": ("um", 1e-3),
    "NANO": ("nm", 1e-6),
}
_NAMED_UNITS_MM = {"inch": 25.4, "in": 25.4, "foot": 304.8, "ft": 304.8, "mil": 0.0254, "yard": 914.4, "mile": 1609344.0}
_AP_NAMES = {
    "CONFIG_CONTROL_DESIGN": "AP203",
    "AUTOMOTIVE_DESIGN": "AP214",
    "AP203_CONFIGURATION_CONTROLLED_3D_DESIGN_OF_MECHANICAL_PARTS_AND_ASSEMBLIES_MIM_LF": "AP203e2",
    "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF": "AP242",
}


class StepSolid(BaseModel):
    index: int
    valid: bool
    volume_mm3: float
    faces: int
    bbox: BBox | None = None


class StepAudit(BaseModel):
    """Raw audit of uploaded STEP bytes; ``code == "accepted"`` is the only pass."""

    audit_schema: str = AUDIT_SCHEMA
    sha256: str
    size_bytes: int
    code: AuditCode
    message: str
    # header (text)
    step_schema: str | None = None
    application_protocol: str | None = None
    file_name: str | None = None
    originating_system: str | None = None
    preprocessor: str | None = None
    timestamp: str | None = None
    text_encoding: str | None = None
    length_unit: str | None = None
    length_unit_mm: float | None = None
    length_units: list[str] = Field(default_factory=list)
    product_names: list[str] = Field(default_factory=list)
    # structure (XCAF)
    products: int | None = None
    assemblies: int | None = None
    parts: int | None = None
    instances: int | None = None
    # shapes
    solids: int = 0
    shells: int = 0
    free_shells: int = 0
    free_faces: int = 0
    valid_solids: int = 0
    solid_details: list[StepSolid] = Field(default_factory=list)
    volume_mm3: float | None = None
    bbox: BBox | None = None
    warnings: list[Issue] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.code == "accepted"


# --------------------------------------------------------------------------- #
# Text: encoding, Part 21 strings, header, units, product names
# --------------------------------------------------------------------------- #


def detect_text_encoding(data: bytes) -> str:
    """``ascii`` (conforming), else the codec that decodes the raw non-ASCII
    bytes: ``utf-8``, ``gbk`` (as GB18030) or ``latin-1``."""
    if data.isascii():
        return "ascii"
    for name, codec in (("utf-8", "utf-8"), ("gbk", "gb18030")):
        try:
            data.decode(codec)
            return name
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _codec(encoding: str) -> str:
    return {"ascii": "ascii", "utf-8": "utf-8", "gbk": "gb18030"}.get(encoding, "latin-1")


_ESCAPE_RE = re.compile(r"\\X2\\((?:[0-9A-Fa-f]{4})*)\\X0\\|\\X4\\((?:[0-9A-Fa-f]{8})*)\\X0\\|\\X\\([0-9A-Fa-f]{2})|\\S\\(.)|\\P[A-Z]?\\|\\\\")


def decode_p21_string(raw: str) -> str:
    """Decode ISO 10303-21 string escapes (``\\X2\\…\\X0\\``, ``\\X4\\``, ``\\X\\hh``,
    ``\\S\\c``, ``''``). Never raises: malformed escapes are kept as text."""

    def repl(m: re.Match[str]) -> str:
        try:
            if m.group(1) is not None:
                units = bytes.fromhex(m.group(1))
                return units.decode("utf-16-be", errors="replace")
            if m.group(2) is not None:
                return bytes.fromhex(m.group(2)).decode("utf-32-be", errors="replace")
            if m.group(3) is not None:
                return chr(int(m.group(3), 16))
            if m.group(4) is not None:
                return chr(ord(m.group(4)) + 128)
            if m.group(0) == "\\\\":
                return "\\"
            return ""  # \P?\ code page switch
        except Exception:  # pragma: no cover - defensive
            return m.group(0)

    try:
        return _ESCAPE_RE.sub(repl, raw.replace("''", "'"))
    except Exception:  # pragma: no cover - defensive
        return raw


def _p21_params(text: str) -> list[Any]:
    """Parse a Part 21 parameter list ``(a, 'b', (c), $)`` into nested lists.
    Strings stay raw (escapes undecoded); other tokens are stripped text."""
    pos = 0
    n = len(text)

    def parse_list() -> list[Any]:
        nonlocal pos
        out: list[Any] = []
        pos += 1  # skip "("
        token = ""
        while pos < n:
            ch = text[pos]
            if ch == "'":
                end = pos + 1
                while end < n:
                    if text[end] == "'":
                        if end + 1 < n and text[end + 1] == "'":
                            end += 2
                            continue
                        break
                    end += 1
                out.append(("str", text[pos + 1 : end]))
                pos = end + 1
                token = ""
            elif ch == "(":
                out.append(parse_list())
                token = ""
            elif ch == ")":
                if token.strip():
                    out.append(token.strip())
                pos += 1
                return out
            elif ch == ",":
                if token.strip():
                    out.append(token.strip())
                token = ""
                pos += 1
            else:
                token += ch
                pos += 1
        return out

    start = text.find("(")
    if start < 0:
        return []
    pos = start
    return parse_list()


def _str_param(params: list[Any], i: int) -> str | None:
    if i < len(params) and isinstance(params[i], tuple) and params[i][0] == "str":
        s = decode_p21_string(params[i][1]).strip()
        return s or None
    return None


def _header(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = re.search(r"HEADER\s*;(.*?)ENDSEC\s*;", text, re.S | re.I)
    if not m:
        return out
    head = m.group(1)
    for name in ("FILE_NAME", "FILE_SCHEMA"):
        rec = re.search(rf"{name}\s*(\(.*?\))\s*;", head, re.S | re.I)
        if not rec:
            continue
        params = _p21_params(rec.group(1))
        if name == "FILE_NAME":
            out["file_name"] = _str_param(params, 0)
            out["timestamp"] = _str_param(params, 1)
            out["preprocessor"] = _str_param(params, 4)
            out["originating_system"] = _str_param(params, 5)
        elif params and isinstance(params[0], list) and params[0]:
            schema = _str_param(params[0], 0)
            out["step_schema"] = schema
            if schema:
                word = schema.split()[0].split("{")[0].strip().upper()
                out["application_protocol"] = _AP_NAMES.get(word, "AP242" if word.startswith("AP242") else None)
    return out


_RECORD_RE = re.compile(r"#(\d+)\s*=\s*((?:[^;']|'(?:[^']|'')*')*);")
_SI_LENGTH_RE = re.compile(r"SI_UNIT\s*\(\s*(?:\.(\w+)\.|\$)\s*,\s*\.METRE\.\s*\)", re.I)
_CONV_RE = re.compile(r"CONVERSION_BASED_UNIT\s*\(\s*'((?:[^']|'')*)'\s*,\s*#(\d+)", re.I)
_CONTEXT_RE = re.compile(r"GLOBAL_UNIT_ASSIGNED_CONTEXT\s*\(\s*\(([^)]*)\)", re.I)
_MEASURE_RE = re.compile(r"LENGTH_MEASURE_WITH_UNIT\s*\(\s*(?:LENGTH_MEASURE\s*\()?\s*([-+0-9.Ee]+)\s*\)?\s*,\s*#(\d+)", re.I)


def length_units(text: str) -> list[tuple[str, float | None]]:
    """Declared length units ``[(name, mm_per_unit)]`` (distinct, file order)."""
    records: dict[str, str] = {}
    unit_records: list[str] = []
    for m in _RECORD_RE.finditer(text):
        body = m.group(2)
        if "UNIT" in body.upper():
            records[m.group(1)] = body
            if re.search(r"\bLENGTH_UNIT\b", body, re.I):
                unit_records.append(body)

    def factor(ref: str, depth: int = 0) -> tuple[str, float | None] | None:
        body = records.get(ref)
        if body is None or depth > 4:
            return None
        return unit_of(body, depth + 1)

    def unit_of(body: str, depth: int = 0) -> tuple[str, float | None] | None:
        si = _SI_LENGTH_RE.search(body)
        if si:
            return _SI_PREFIX_MM.get((si.group(1) or "").upper(), (f"{(si.group(1) or '').lower()}m", None))
        conv = _CONV_RE.search(body)
        if conv:
            name = decode_p21_string(conv.group(1)).strip().lower() or "unnamed"
            mm = _NAMED_UNITS_MM.get(name)
            measure = records.get(conv.group(2))
            if measure is not None:
                mm_m = _MEASURE_RE.search(measure)
                if mm_m:
                    base = factor(mm_m.group(2), depth)
                    try:
                        value = float(mm_m.group(1))
                    except ValueError:
                        value = None
                    if base and base[1] is not None and value is not None:
                        mm = value * base[1]
            return (name, mm)
        return None

    # the units of the representation contexts; every LENGTH_UNIT record when no context lists one
    context_refs = [r for body in records.values() for ctx in _CONTEXT_RE.findall(body) for r in re.findall(r"#(\d+)", ctx)]
    chosen = [records[r] for r in context_refs if r in records and re.search(r"\bLENGTH_UNIT\b", records[r], re.I)]
    out: list[tuple[str, float | None]] = []
    for body in chosen or unit_records:
        u = unit_of(body)
        if u is not None and u not in out:
            out.append(u)
    return out


_PRODUCT_RE = re.compile(r"(?<![A-Z_])PRODUCT\s*\(\s*'((?:[^']|'')*)'", re.I)


def product_names(text: str) -> list[str]:
    names: list[str] = []
    for m in _PRODUCT_RE.finditer(text):
        name = decode_p21_string(m.group(1)).strip()
        if name and name not in names:
            names.append(name)
    return names


# --------------------------------------------------------------------------- #
# OCCT: structure and shapes
# --------------------------------------------------------------------------- #


def _count(shape: Any, kind: Any, avoid: Any | None = None) -> int:
    n = 0
    exp = TopExp_Explorer(shape, kind) if avoid is None else TopExp_Explorer(shape, kind, avoid)
    while exp.More():
        n += 1
        exp.Next()
    return n


def _bbox(shape: Any) -> BBox | None:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return None
    lo, hi = box.CornerMin(), box.CornerMax()
    mn = [round(lo.X(), 4) + 0.0, round(lo.Y(), 4) + 0.0, round(lo.Z(), 4) + 0.0]
    mx = [round(hi.X(), 4) + 0.0, round(hi.Y(), 4) + 0.0, round(hi.Z(), 4) + 0.0]
    return BBox(min=mn, max=mx, size=[round(b - a, 4) + 0.0 for a, b in zip(mn, mx)])


def _leaf_instances(label: Any, depth: int = 0) -> int:
    if depth > 64:  # pragma: no cover - defensive against cyclic references
        return 0
    comps = _LabelSeq()
    XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
    total = 0
    for i in range(1, comps.Length() + 1):
        ref = TDF_Label()
        XCAFDoc_ShapeTool.GetReferredShape_s(comps.Value(i), ref)
        total += _leaf_instances(ref, depth + 1) if XCAFDoc_ShapeTool.IsAssembly_s(ref) else 1
    return total


def _read_xcaf(data: bytes) -> tuple[Any | None, dict[str, int], str | None]:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    app.InitDocument(doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    try:
        status = reader.ReadStream("audit.step", io.BytesIO(data))
    except Exception as exc:  # pragma: no cover - reader raises rarely
        return None, {}, f"the STEP reader failed: {exc}"
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        return None, {}, f"the STEP reader could not parse the file (status {getattr(status, 'name', status)})"
    try:
        if not reader.Transfer(doc):
            return None, {}, "the STEP data could not be transferred to shapes"
    except Exception as exc:  # pragma: no cover - defensive
        return None, {}, f"the STEP data could not be transferred: {exc}"
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels = _LabelSeq()
    tool.GetShapes(labels)
    free = _LabelSeq()
    tool.GetFreeShapes(free)
    assemblies = sum(1 for i in range(1, labels.Length() + 1) if XCAFDoc_ShapeTool.IsAssembly_s(labels.Value(i)))
    instances = 0
    for i in range(1, free.Length() + 1):
        lab = free.Value(i)
        instances += _leaf_instances(lab) if XCAFDoc_ShapeTool.IsAssembly_s(lab) else 1
    counts = {
        "products": labels.Length(),
        "assemblies": assemblies,
        "parts": labels.Length() - assemblies,
        "instances": instances,
    }
    shape = tool.GetOneShape()
    return (None if shape is None or shape.IsNull() else shape), counts, None


def _fmt_size(b: BBox | None) -> str:
    return "?" if b is None else " × ".join(f"{s:g}" for s in b.size) + " mm"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def decide(audit: StepAudit) -> StepAudit:
    """Set ``code`` / ``message`` from the counts of a readable file (the policy, in order:
    assembly, no solid, several solids, invalid solid, accepted)."""
    parts, instances = audit.parts or 0, audit.instances or 0
    if ((audit.assemblies or 0) > 0 and instances > 1) or parts > 1:
        audit.code = "step_assembly"
        audit.message = (
            f"the STEP file is an assembly: {parts} part definition(s), {instances} instance(s)"
            + (f" in {audit.assemblies} (sub-)assemblies" if audit.assemblies else "")
            + f", {audit.solids} solids. Assemblies are not supported in this release: export one part definition as a "
            "single-solid STEP and import that"
        )
    elif audit.solids == 0:
        audit.code = "step_no_solid"
        audit.message = (
            f"the STEP file contains no solid ({audit.shells} shell(s), {audit.free_faces} free face(s)): surface or "
            "wireframe models cannot be a model base"
        )
    elif audit.solids > 1:
        audit.code = "step_multibody"
        audit.message = (
            f"the STEP part has {audit.solids} solids; exactly one solid is required (combine the bodies in the CAD "
            "system or export a single body)"
        )
    elif audit.valid_solids != 1:
        audit.code = "step_invalid"
        audit.message = "the solid is not valid (BRepCheck failed); repair it in the CAD system and export again"
    elif (audit.volume_mm3 or 0.0) <= 1e-9:
        audit.code = "step_invalid"
        audit.message = f"the solid has no positive volume ({audit.volume_mm3} mm³; inverted or open shell)"
    else:
        audit.code = "accepted"
        unit = f", declared unit {audit.length_unit}" if audit.length_unit else ", no declared unit"
        audit.message = f"one valid solid, {_fmt_size(audit.bbox)}, volume {audit.volume_mm3:g} mm³{unit}"
    return audit


def audit_step(data: bytes) -> StepAudit:
    """Audit raw STEP bytes (never raises for bad input; see module doc)."""
    sha = hashlib.sha256(data).hexdigest()
    audit = StepAudit(sha256=sha, size_bytes=len(data), code="step_unreadable", message="")
    if not data.lstrip()[:16].upper().startswith(b"ISO-10303-21"):
        audit.message = "not a STEP file (missing the ISO-10303-21 header)"
        return audit

    encoding = detect_text_encoding(data)
    audit.text_encoding = encoding
    text = data.decode(_codec(encoding), errors="replace")
    try:
        for k, v in _header(text).items():
            setattr(audit, k, v)
        units = length_units(text)
        audit.product_names = product_names(text)[:MAX_PRODUCT_NAMES]
    except Exception as exc:  # pragma: no cover - text parsing must never block the audit
        units = []
        audit.warnings.append(Issue(code="header_unparsed", message=f"STEP header / units could not be parsed: {exc}"))
    audit.length_units = [u[0] for u in units]
    if units:
        audit.length_unit, audit.length_unit_mm = units[0]
    if not units:
        audit.warnings.append(
            Issue(code="length_unit_missing", message="no length unit is declared in the STEP data; OCCT reads the values as mm")
        )
    elif len(units) > 1:
        audit.warnings.append(
            Issue(code="mixed_length_units", message=f"several length units are declared: {', '.join(audit.length_units)}")
        )
    if encoding not in ("ascii",):
        audit.warnings.append(
            Issue(
                code="raw_non_ascii_text",
                message=f"names are stored as raw {encoding.upper()} bytes instead of ISO 10303-21 escapes; decoded as {encoding}",
            )
        )

    shape, counts, error = _read_xcaf(data)
    if error is not None:
        audit.message = error
        return audit
    for k, v in counts.items():
        setattr(audit, k, v)
    if shape is not None:
        audit.shells = _count(shape, TopAbs_SHELL)
        audit.free_shells = _count(shape, TopAbs_SHELL, TopAbs_SOLID)
        audit.free_faces = _count(shape, TopAbs_FACE, TopAbs_SHELL)
        boxes: list[BBox] = []
        total = 0.0
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            solid = exp.Current()
            exp.Next()
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(solid, props)
            vol = float(props.Mass())
            valid = bool(BRepCheck_Analyzer(solid).IsValid())
            audit.solids += 1
            audit.valid_solids += int(valid)
            total += vol
            box = _bbox(solid)
            if box is not None:
                boxes.append(box)
            if len(audit.solid_details) < MAX_SOLID_DETAILS:
                audit.solid_details.append(
                    StepSolid(
                        index=audit.solids - 1, valid=valid, volume_mm3=round(vol, 4) + 0.0, faces=_count(solid, TopAbs_FACE), bbox=box
                    )
                )
        audit.volume_mm3 = round(total, 4) + 0.0 if audit.solids else None
        if boxes and audit.free_shells == 0 and audit.free_faces == 0:  # union of the solid boxes (cheaper than a second pass)
            mn = [min(b.min[i] for b in boxes) for i in range(3)]
            mx = [max(b.max[i] for b in boxes) for i in range(3)]
            audit.bbox = BBox(min=mn, max=mx, size=[round(b - a, 4) + 0.0 for a, b in zip(mn, mx)])
        else:
            audit.bbox = _bbox(shape)

    decide(audit)
    return audit
