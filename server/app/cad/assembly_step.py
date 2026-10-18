"""Structured assembly STEP read / write (XCAF), W4.

Reading keeps the product structure: shared part definitions (one XCAF label per unique leaf), assembly
definitions (one label per unique (sub-)assembly, reused by every instance), the component tree, the
component locations (rigid 4x4, child -> parent) and the names. Raw GBK / UTF-8 product names (non-conforming
SolidWorks exports) are converted to ISO 10303-21 ``\\X2\\`` escapes before OCCT reads the file, the same
decoding :mod:`app.cad.step_source` uses for its audit.

An assembly is NEVER passed through the kernel's single-part import (``kernel.import_step_solid`` fuses
several solids into one). Each leaf definition is checked on its own: exactly one valid solid is a part
definition; a multi-solid, solid-less or invalid leaf is reported with its path (``problems``), never
dropped, fused or flattened silently.

Writing produces an assembly STEP with the same shared definitions, hierarchy, names and instance
transforms, plus single-part STEP files for the unique definitions. Output strings are ASCII with
``\\X2\\`` escapes; the header timestamp is fixed so equal content gives equal bytes.

Pure: no database, no storage (backend directory rule).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Trsf
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.Message import Message, Message_Gravity
from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Reader, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_Controller, STEPControl_Reader, STEPControl_StepModelType
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString, TCollection_HAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
from OCP.XSControl import XSControl_WorkSession

from . import assembly_model as am
from .step_source import _codec, decode_p21_string, detect_text_encoding

try:  # OCP >= 7.8 moved NCollection sequences to OCP.collections
    from OCP.collections import Sequence_TDF_Label as _LabelSeq
except ImportError:  # pragma: no cover - older OCP
    from OCP.TDF import TDF_LabelSequence as _LabelSeq  # type: ignore[no-redef]

STEP_TIMESTAMP = "2000-01-01T00:00:00"
ORIGINATING_SYSTEM = "PartRoute"
MAX_DEPTH = 64  # defensive bound while walking a (possibly cyclic) reference graph
#: Leaf instances closer than this (mm, bbox gap) belong to the same spatial cluster (pose notes only).
CLUSTER_GAP_MM = 1.0
#: A cluster further than this from the main cluster is noted as an isolated pose (kept as imported).
ISOLATED_MIN_MM = 50.0

_STRING_RE = re.compile(r"'((?:[^'\r\n]|'')*)'")
_WRAP_RE = re.compile(r"\r?\n[ \t]+")
_NAUO_RE = re.compile(r"(NEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\(\s*)'\d+'")
_GENERIC_INSTANCE_NAME = re.compile(r"^(NAUO\d*|\d+|\s*)$", re.I)


class StepAssemblyError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class StepLeaf:
    key: str
    name: str
    shape: Any  # TopoDS_Shape in the definition's own frame
    solids: int
    valid_solids: int
    volume_mm3: float
    bbox: tuple[tuple[float, float, float], tuple[float, float, float]] | None
    free_faces: int = 0
    problem: str | None = None  # multibody | no_solid | invalid

    def brief(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "solids": self.solids, "valid_solids": self.valid_solids,
                "volume_mm3": round(self.volume_mm3, 4), "problem": self.problem,
                "bbox": None if self.bbox is None else {"min": list(self.bbox[0]), "max": list(self.bbox[1])}}


@dataclass
class StepComponent:
    ref: str  # child definition key (leaf or assembly)
    name: str  # display name (source name when meaningful, else "<definition> <n>")
    source_name: str  # component / NAUO name as read
    transform: am.Matrix4
    rigid: bool = True


@dataclass
class StepAssemblyDef:
    key: str
    name: str
    components: list[StepComponent] = field(default_factory=list)


@dataclass
class StepOccurrence:
    path: tuple[int, ...]  # component indexes root -> leaf
    names: tuple[str, ...]
    leaf: str
    world: am.Matrix4


@dataclass
class StepStructure:
    """The product structure of one STEP file (definition keys are stable for the file: L1.., A1..)."""

    root: str
    leaves: dict[str, StepLeaf]
    assemblies: dict[str, StepAssemblyDef]
    header: dict[str, Any] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)
    synthetic_root: bool = False

    def occurrences(self) -> list[StepOccurrence]:
        out: list[StepOccurrence] = []

        def walk(key: str, acc: am.Matrix4, path: tuple[int, ...], names: tuple[str, ...], depth: int) -> None:
            if depth > MAX_DEPTH:
                raise StepAssemblyError("step_assembly_cycle", "the STEP product structure is cyclic or too deep")
            asm = self.assemblies.get(key)
            if asm is None:
                out.append(StepOccurrence(path=path, names=names, leaf=key, world=acc))
                return
            for i, c in enumerate(asm.components):
                walk(c.ref, am.compose(acc, c.transform), path + (i,), names + (c.name,), depth + 1)

        walk(self.root, am.IDENTITY, (), (), 0)
        return out

    def size(self) -> tuple[int, int]:
        """(leaf instances under the root, nesting depth), computed once per definition bottom-up (memoized
        multiplication), never by expanding the occurrence tree: a tiny file of 5 levels x 20 components
        describes 3.2 million occurrences. Budgets are checked on these numbers before any expansion."""
        leaves_under: dict[str, int] = {}
        depth_of: dict[str, int] = {}

        def visit(key: str, stack: tuple[str, ...]) -> None:
            if key in leaves_under:
                return
            if key in stack or len(stack) > MAX_DEPTH:
                raise StepAssemblyError("step_assembly_cycle", "the STEP product structure is cyclic or too deep")
            asm = self.assemblies.get(key)
            if asm is None:
                leaves_under[key], depth_of[key] = 1, 0
                return
            n = d = 0
            for c in asm.components:
                visit(c.ref, stack + (key,))
                n += leaves_under[c.ref]
                d = max(d, depth_of[c.ref])
            leaves_under[key], depth_of[key] = n, d + 1

        visit(self.root, ())
        return leaves_under[self.root], depth_of[self.root]

    def depth(self) -> int:
        return self.size()[1]

    def counts(self) -> dict[str, int]:
        leaf_instances, depth = self.size()
        return {"part_definitions": len(self.leaves), "assembly_definitions": len(self.assemblies), "leaf_instances": leaf_instances,
                "depth": depth, "components": sum(len(a.components) for a in self.assemblies.values())}

    def problems(self) -> list[dict[str, Any]]:
        """Leaves that are not exactly one valid solid, with every instance path that uses them."""
        bad = {k: leaf for k, leaf in self.leaves.items() if leaf.problem}
        if not bad:
            return []
        paths: dict[str, list[str]] = {k: [] for k in bad}
        for o in self.occurrences():
            if o.leaf in bad and len(paths[o.leaf]) < 20:
                paths[o.leaf].append(" / ".join(o.names))
        return [{**bad[k].brief(), "paths": paths[k]} for k in bad]


# --------------------------------------------------------------------------- #
# Text normalisation
# --------------------------------------------------------------------------- #


def _escape(text: str) -> str:
    """ISO 10303-21 escapes: runs of BMP characters as one ``\\X2\\…\\X0\\``, others as ``\\X4\\``."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append("\\X2\\" + "".join(run) + "\\X0\\")
            run.clear()

    for ch in text:
        o = ord(ch)
        if o < 128:
            flush()
            out.append(ch)
        elif o <= 0xFFFF:
            run.append("%04X" % o)
        else:
            flush()
            out.append("\\X4\\%08X\\X0\\" % o)
    flush()
    return "".join(out)


def _escape_strings(text: str) -> str:
    """Rewrite every non-ASCII Part 21 string literal with ``\\X2\\`` / ``\\X4\\`` escapes."""

    def repl(m: re.Match[str]) -> str:
        body = m.group(1)
        return m.group(0) if body.isascii() else "'" + _escape(body) + "'"

    return _STRING_RE.sub(repl, text)


def normalize_p21(data: bytes) -> tuple[bytes, str]:
    """Raw STEP bytes -> conforming ASCII bytes (non-ASCII names escaped) and the detected source encoding."""
    encoding = detect_text_encoding(data)
    if encoding == "ascii":
        return data, encoding
    text = data.decode(_codec(encoding), errors="replace")
    return _escape_strings(text).encode("ascii", errors="replace"), encoding


# --------------------------------------------------------------------------- #
# OCCT helpers
# --------------------------------------------------------------------------- #


def _quiet() -> None:
    messenger = Message.DefaultMessenger_s()
    for printer in messenger.Printers():
        printer.SetTraceLevel(Message_Gravity.Message_Fail)


def _new_doc() -> tuple[Any, Any]:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    app.InitDocument(doc)
    return doc, XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())


def _name(label: Any) -> str | None:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return decode_p21_string(attr.Get().ToExtString()).strip() or None
    return None


def _set_name(label: Any, name: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name, True))


def _entry(label: Any) -> str:
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def matrix_of(trsf: gp_Trsf) -> am.Matrix4:
    rows = [[trsf.Value(r, c) for c in range(1, 5)] for r in range(1, 4)]
    return am.from_list([[round(v, 12) + 0.0 for v in row] for row in rows] + [[0.0, 0.0, 0.0, 1.0]])


def trsf_of(m: am.Matrix4) -> gp_Trsf:
    t = gp_Trsf()
    t.SetValues(m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3], m[2][0], m[2][1], m[2][2], m[2][3])
    return t


def location_of(m: am.Matrix4) -> TopLoc_Location:
    return TopLoc_Location(trsf_of(m))


def shape_bbox(shape: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    from .bounds import tight_bounds

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return None
    lo, hi = tight_bounds(shape)  # the optimal box, tightened on trimmed free-form faces (ground coil ends)
    return ((round(lo[0], 4) + 0.0, round(lo[1], 4) + 0.0, round(lo[2], 4) + 0.0),
            (round(hi[0], 4) + 0.0, round(hi[1], 4) + 0.0, round(hi[2], 4) + 0.0))


def _solid_facts(shape: Any) -> tuple[int, int, float, int]:
    solids = valid = 0
    volume = 0.0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        solid = exp.Current()
        exp.Next()
        solids += 1
        valid += int(BRepCheck_Analyzer(solid).IsValid())
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        volume += float(props.Mass())
    free_faces = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SHELL)
    while exp.More():
        free_faces += 1
        exp.Next()
    return solids, valid, volume, free_faces


def _leaf_problem(solids: int, valid: int, volume: float) -> str | None:
    if solids == 0:
        return "no_solid"
    if solids > 1:
        return "multibody"
    if valid != 1 or volume <= 1e-9:
        return "invalid"
    return None


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def read_assembly(data: bytes, *, file_name: str | None = None, max_definitions: int | None = None) -> StepStructure:
    """Read the product structure of STEP bytes (see module doc). Raises :class:`StepAssemblyError`
    (``step_unreadable``, ``step_assembly_cycle``) for files OCCT cannot read, and ``assembly_too_large``
    as soon as more than ``max_definitions`` unique definitions are met (before their solids are analysed)."""
    if not data.lstrip()[:16].upper().startswith(b"ISO-10303-21"):
        raise StepAssemblyError("step_unreadable", "not a STEP file (missing the ISO-10303-21 header)")
    norm, encoding = normalize_p21(data)
    _quiet()
    doc, tool = _new_doc()
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    try:
        status = reader.ReadStream(file_name or "assembly.step", io.BytesIO(norm))
    except Exception as exc:  # pragma: no cover - reader raises rarely
        raise StepAssemblyError("step_unreadable", f"the STEP reader failed: {exc}") from exc
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepAssemblyError("step_unreadable", f"the STEP reader could not parse the file (status {getattr(status, 'name', status)})")
    if not reader.Transfer(doc):
        raise StepAssemblyError("step_unreadable", "the STEP data could not be transferred to shapes")

    leaves: dict[str, StepLeaf] = {}
    assemblies: dict[str, StepAssemblyDef] = {}
    keys: dict[str, str] = {}  # XCAF label entry -> definition key
    notes: list[dict[str, Any]] = []

    def visit(label: Any, depth: int) -> str:
        entry = _entry(label)
        if entry in keys:
            return keys[entry]
        if depth > MAX_DEPTH:
            raise StepAssemblyError("step_assembly_cycle", "the STEP product structure is cyclic or deeper than supported")
        if max_definitions is not None and len(leaves) + len(assemblies) >= max_definitions:
            raise StepAssemblyError("assembly_too_large", f"the STEP file has more than {max_definitions} unique definitions",
                                    {"counts": {"part_definitions": len(leaves), "assembly_definitions": len(assemblies),
                                                "definitions_at_least": max_definitions + 1}})
        name = _name(label) or f"unnamed {entry}"
        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            key = f"A{len(assemblies) + 1}"
            keys[entry] = key
            asm = StepAssemblyDef(key=key, name=name)
            assemblies[key] = asm
            comps = _LabelSeq()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
            per_def: dict[str, int] = {}
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                ref = TDF_Label()
                XCAFDoc_ShapeTool.GetReferredShape_s(comp, ref)
                child = visit(ref, depth + 1)
                m = matrix_of(XCAFDoc_ShapeTool.GetLocation_s(comp).Transformation())
                source_name = _name(comp) or ""
                child_name = (assemblies[child].name if child in assemblies else leaves[child].name)
                per_def[child] = per_def.get(child, 0) + 1
                display = source_name if source_name and not _GENERIC_INSTANCE_NAME.match(source_name) else f"{child_name} <{per_def[child]}>"
                asm.components.append(StepComponent(ref=child, name=display, source_name=source_name, transform=m, rigid=am.is_rigid(m, 1e-6)))
            return key
        key = f"L{len(leaves) + 1}"
        keys[entry] = key
        shape = XCAFDoc_ShapeTool.GetShape_s(label)
        solids, valid, volume, free_faces = _solid_facts(shape)
        leaves[key] = StepLeaf(key=key, name=name, shape=shape, solids=solids, valid_solids=valid, volume_mm3=volume,
                               bbox=shape_bbox(shape), free_faces=free_faces, problem=_leaf_problem(solids, valid, volume))
        return key

    free = _LabelSeq()
    tool.GetFreeShapes(free)
    roots = [visit(free.Value(i), 0) for i in range(1, free.Length() + 1)]
    if not roots:
        raise StepAssemblyError("step_unreadable", "the STEP file has no product / shape")
    synthetic = False
    if len(roots) == 1:
        root = roots[0]
    else:
        # several top-level products: keep them all under one explicit synthetic root (noted, never merged)
        root = f"A{len(assemblies) + 1}"
        stem = (file_name or "assembly").rsplit(".", 1)[0]
        assemblies[root] = StepAssemblyDef(key=root, name=stem, components=[
            StepComponent(ref=r, name=(assemblies[r].name if r in assemblies else leaves[r].name), source_name="", transform=am.IDENTITY)
            for r in roots])
        synthetic = True
        notes.append({"code": "synthetic_root", "subject": root,
                      "message": f"the file has {len(roots)} top-level products; they were placed under one root '{stem}' "
                                 "with identity transforms"})
    header = {"text_encoding": encoding}
    structure = StepStructure(root=root, leaves=leaves, assemblies=assemblies, header=header, notes=notes, synthetic_root=synthetic)
    for asm in assemblies.values():
        for i, c in enumerate(asm.components):
            if not c.rigid:
                det = am.determinant3(c.transform)
                notes.append({"code": "non_rigid_transform", "subject": f"{asm.name} / {c.name}",
                              "message": f"component {i + 1} of '{asm.name}' has a non-rigid transform (det {det:.4g}); "
                                         "a mirrored or scaled instance needs its own definition",
                              "details": {"assembly": asm.key, "index": i, "determinant": det}})
    return structure


def pose_notes(structure: StepStructure) -> list[dict[str, Any]]:
    """Spatial clusters of leaf instances (bbox gap <= CLUSTER_GAP_MM). Clusters far from the main (largest
    volume) cluster are noted as isolated poses; they are kept exactly as imported."""
    occ = structure.occurrences()
    boxes: list[tuple[np.ndarray, np.ndarray]] = []
    vols: list[float] = []
    for o in occ:
        leaf = structure.leaves[o.leaf]
        if leaf.bbox is None:
            boxes.append((np.zeros(3), np.zeros(3)))
            vols.append(0.0)
            continue
        lo, hi = np.array(leaf.bbox[0]), np.array(leaf.bbox[1])
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        r = np.array([row[:3] for row in o.world[:3]])
        t = np.array([o.world[0][3], o.world[1][3], o.world[2][3]])
        w = corners @ r.T + t
        boxes.append((w.min(axis=0), w.max(axis=0)))
        vols.append(leaf.volume_mm3)
    n = len(boxes)
    if n < 2:
        return []
    lo = np.array([b[0] for b in boxes])
    hi = np.array([b[1] for b in boxes])
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n - 1):
        gap = np.maximum(0.0, np.maximum(lo[i + 1:] - hi[i], lo[i] - hi[i + 1:]))
        dist = np.sqrt((gap ** 2).sum(axis=1))
        for j in np.nonzero(dist <= CLUSTER_GAP_MM)[0]:
            a, b = find(i), find(i + 1 + int(j))
            if a != b:
                parent[a] = b
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    if len(clusters) < 2:
        return []
    main = max(clusters.values(), key=lambda idx: sum(vols[i] for i in idx))
    mlo, mhi = lo[main].min(axis=0), hi[main].max(axis=0)
    diag = float(np.linalg.norm(mhi - mlo))
    out: list[dict[str, Any]] = []
    for members in clusters.values():
        if members is main:
            continue
        clo, chi = lo[members].min(axis=0), hi[members].max(axis=0)
        gap = np.maximum(0.0, np.maximum(mlo - chi, clo - mhi))
        dist = float(np.linalg.norm(gap))
        if dist < max(ISOLATED_MIN_MM, 0.1 * diag):
            continue
        names = sorted({" / ".join(occ[i].names) for i in members})
        out.append({"code": "isolated_pose", "subject": names[0],
                    "message": f"{len(members)} instance(s) sit {dist:.1f} mm away from the main body; the source pose was "
                               "kept as imported (no automatic correction)",
                    "details": {"distance_mm": round(dist, 3), "instances": names[:20], "instance_count": len(members)}})
    return out


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


@dataclass
class WritePart:
    key: str
    name: str
    shape: Any


@dataclass
class WriteComponent:
    ref: str
    name: str
    transform: am.Matrix4


@dataclass
class WriteAssembly:
    key: str
    name: str
    components: list[WriteComponent]


#: Application protocols the writers produce (generation reliability M, design §2.7): AP203 by default, AP214 on
#: request. Values of OCCT's ``write.step.schema`` static (``"AP214"`` alone is not a valid value: it is refused
#: silently and the previous schema stays).
STEP_PROTOCOLS = ("AP203", "AP214")
DEFAULT_PROTOCOL = "AP203"
_SCHEMA_STATIC = {"AP203": "AP203", "AP214": "AP214IS"}
_SCHEMA_WORD = {"AP203": "CONFIG_CONTROL_DESIGN", "AP214": "AUTOMOTIVE_DESIGN"}
_FILE_SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([A-Za-z0-9_]+)")
#: Why a part of several solids is not written in AP203 (review of generation reliability M). OCCT 8's AP203 writer
#: (CONFIG_CONTROL_DESIGN) writes such a shape as one product whose SHAPE_REPRESENTATION points, through
#: SHAPE_REPRESENTATION_RELATIONSHIPs, at one ADVANCED_BREP_SHAPE_REPRESENTATION per solid; OCCT readers (this module,
#: FreeCAD) read that back as a sub-assembly of unnamed "SOLID" parts, so the multibody part is no longer one part.
#: No writer setting keeps the solids in one representation (write.step.assembly Off / On, write.step.nonmanifold —
#: which also flattens assemblies — and the ManifoldSolidBrep mode were tried); AP214 does.
AP203_MULTI_SOLID = ("OCCT's AP203 writer splits a part of several solids into one representation per solid, read back as a "
                     "sub-assembly of unnamed solids; such a part is written in AP214")


def _solid_count(shape: Any) -> int:
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        n += 1
        exp.Next()
    return n


def _multi_solid_parts(doc: Any) -> list[str]:
    """Names of the part (non-assembly) shapes of an XCAF document that hold more than one solid."""
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels = _LabelSeq()
    tool.GetShapes(labels)
    out = []
    for i in range(1, labels.Length() + 1):
        lab = labels.Value(i)
        if not XCAFDoc_ShapeTool.IsAssembly_s(lab) and _solid_count(XCAFDoc_ShapeTool.GetShape_s(lab)) > 1:
            out.append(_name(lab) or _entry(lab))
    return out


def writable_protocol(protocol: str, shapes: list[Any]) -> str:
    """The protocol a writer uses for ``shapes``: ``protocol``, except AP214 when AP203 is asked for and a shape has
    several solids (:data:`AP203_MULTI_SOLID`). The file's FILE_SCHEMA says AP214; a delivery that requires AP203
    re-exports it, which :func:`_write_doc` refuses with this reason (``protocol_unmet``, the AP214 file kept as a
    labelled alternative)."""
    if protocol == "AP203" and any(s is not None and _solid_count(s) > 1 for s in shapes):
        return "AP214"
    return protocol


def _write_doc(doc: Any, *, name: str, protocol: str = DEFAULT_PROTOCOL) -> bytes:
    """STEP bytes of an XCAF document in ``protocol`` (AP203 / AP214). The schema static is set after the controllers
    are initialised and before the writer exists, and the header is made after the transfer: a header made before it
    keeps OCCT's AP214 FILE_SCHEMA while the data follows the static (a mixed file). The static is restored after the
    write (process-wide: build123d's own writer would otherwise inherit it). The written FILE_SCHEMA is checked against
    the protocol (``step_protocol_unmet`` otherwise); it is never edited. A part of several solids is never written in
    AP203 (``step_protocol_unmet`` with :data:`AP203_MULTI_SOLID`; :func:`write_part` / :func:`write_assembly` write
    AP214 instead, see :func:`writable_protocol`)."""
    if protocol not in _SCHEMA_STATIC:
        raise StepAssemblyError("step_protocol_unsupported", f"STEP protocol {protocol!r} is not supported (use {', '.join(STEP_PROTOCOLS)})")
    if protocol == "AP203":
        multi = _multi_solid_parts(doc)
        if multi:
            raise StepAssemblyError("step_protocol_unmet", f"{AP203_MULTI_SOLID} ({', '.join(multi[:5])})",
                                    {"required": protocol, "multi_solid_parts": multi[:20]})
    _quiet()
    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    previous = Interface_Static.CVal_s("write.step.schema")
    Interface_Static.SetCVal_s("write.step.schema", _SCHEMA_STATIC[protocol])
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    try:  # the schema static is process-wide: restored after the write so no other writer inherits it
        session = XSControl_WorkSession()
        writer = STEPCAFControl_Writer(session, False)
        writer.SetNameMode(True)
        writer.SetColorMode(False)
        writer.SetLayerMode(False)
        if not writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
            raise StepAssemblyError("step_export_failed", "the assembly could not be transferred to STEP")
        header = APIHeaderSection_MakeHeader(writer.Writer().Model())
        if not header.IsDone():  # pragma: no cover - no header after a transfer
            header = APIHeaderSection_MakeHeader(0)
            header.Apply(writer.Writer().Model())
        header.SetName(TCollection_HAsciiString(_escape(name)[:200] or "assembly"))
        header.SetTimeStamp(TCollection_HAsciiString(STEP_TIMESTAMP))
        header.SetOriginatingSystem(TCollection_HAsciiString(ORIGINATING_SYSTEM))
        buf = io.BytesIO()
        status = writer.WriteStream(buf)
    finally:
        if previous:
            Interface_Static.SetCVal_s("write.step.schema", previous)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepAssemblyError("step_export_failed", f"the STEP writer failed (status {getattr(status, 'name', status)})")
    text = buf.getvalue().decode("utf-8", errors="replace")
    # OCCT numbers NAUO ids from a process-wide counter and wraps records by length: join the continuation
    # lines and renumber the ids in file order so equal content gives equal bytes; raw UTF-8 names become
    # conforming escapes.
    text = _WRAP_RE.sub("", text)
    counter = iter(range(1, 1 << 30))
    text = _NAUO_RE.sub(lambda m: f"{m.group(1)}'{next(counter)}'", text)
    m = _FILE_SCHEMA_RE.search(text[:4000])
    if m is None or m.group(1).upper() != _SCHEMA_WORD[protocol]:
        raise StepAssemblyError("step_protocol_unmet", f"the STEP writer did not produce {protocol} (FILE_SCHEMA "
                                f"{m.group(1) if m else 'missing'})", {"required": protocol, "file_schema": m.group(1) if m else None})
    return _escape_strings(text).encode("ascii", errors="replace")


def write_doc(doc: Any, *, name: str, protocol: str = DEFAULT_PROTOCOL) -> bytes:
    """Public :func:`_write_doc` (the protocol module re-exports read documents through it)."""
    return _write_doc(doc, name=name, protocol=protocol)


def write_assembly(parts: list[WritePart], assemblies: list[WriteAssembly], root: str, *, protocol: str = DEFAULT_PROTOCOL) -> bytes:
    """Assembly STEP with shared definitions: each part / assembly definition is written once and every
    component references it with its own transform. ``assemblies`` may be in any order. ``protocol``: AP203
    (default) or AP214 (AP214 when a part has several solids, :func:`writable_protocol`)."""
    doc, tool = _new_doc()
    labels: dict[str, Any] = {}
    for p in parts:
        lab = tool.AddShape(p.shape, False)
        _set_name(lab, p.name)
        labels[p.key] = lab
    by_key = {a.key: a for a in assemblies}
    state: dict[str, int] = {}

    def make(key: str, depth: int) -> Any:
        if key in labels:
            return labels[key]
        if depth > MAX_DEPTH or state.get(key) == 1:
            raise StepAssemblyError("assembly_cycle", f"assembly {key} references itself")
        asm = by_key.get(key)
        if asm is None:
            raise StepAssemblyError("step_export_failed", f"definition {key} is missing")
        state[key] = 1
        children = [make(c.ref, depth + 1) for c in asm.components]  # children before the parent
        lab = tool.NewShape()
        _set_name(lab, asm.name)
        for c, child in zip(asm.components, children):
            comp = tool.AddComponent(lab, child, location_of(c.transform))
            _set_name(comp, c.name)
        labels[key] = lab
        state[key] = 2
        return lab

    make(root, 0)
    tool.UpdateAssemblies()
    return _write_doc(doc, name=by_key[root].name if root in by_key else root, protocol=writable_protocol(protocol, [p.shape for p in parts]))


def write_part(shape: Any, name: str, *, protocol: str = DEFAULT_PROTOCOL) -> bytes:
    """Single-part STEP of one definition (its own frame) with its product name; AP203 unless ``protocol`` (AP214 for
    a shape of several solids, :func:`writable_protocol`)."""
    doc, tool = _new_doc()
    lab = tool.AddShape(shape, False)
    _set_name(lab, name)
    return _write_doc(doc, name=name, protocol=writable_protocol(protocol, [shape]))


def read_shape(step_bytes: bytes) -> Any:
    """One shape from (single-part) STEP bytes, in its own frame. No fusing: a multi-solid file stays a
    compound so callers can see it."""
    _quiet()
    reader = STEPControl_Reader()
    status = reader.ReadStream("part.step", io.BytesIO(step_bytes))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepAssemblyError("step_unreadable", f"cannot read part STEP (status {getattr(status, 'name', status)})")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        raise StepAssemblyError("step_unreadable", "the part STEP has no shape")
    return shape


def located(shape: Any, m: am.Matrix4) -> Any:
    """``shape`` moved by the rigid transform ``m`` (new TopoDS handle, geometry shared)."""
    return shape.Moved(location_of(m))
