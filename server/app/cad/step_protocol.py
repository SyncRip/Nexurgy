"""STEP application protocol of written files (generation reliability M, design §2.7).

Every STEP writer (part revisions through :func:`app.cad.kernel.export_step_bytes`, assemblies and their part files
through :mod:`app.cad.assembly_step`) writes AP203 (``CONFIG_CONTROL_DESIGN``) unless AP214 is asked for. The protocol
of a file is read from its header ``FILE_SCHEMA`` (:mod:`app.cad.step_source`), never from a file name or extension.
A file in another protocol (an AP214 part STEP stored before this change and reused) is re-exported: OCCT reads it
(XCAF: product names, structure and placements kept) and writes it again in the asked protocol; the header is never
edited. The re-export is checked: its FILE_SCHEMA names the protocol and it reads back with the same solid count.

Pure: no database, no storage.
"""

from __future__ import annotations

import io
from typing import Any

from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from . import assembly_step as S
from .step_source import _header

DEFAULT_PROTOCOL = S.DEFAULT_PROTOCOL
STEP_PROTOCOLS = S.STEP_PROTOCOLS
StepProtocolError = S.StepAssemblyError


def protocol_of(data: bytes) -> str | None:
    """``AP203`` / ``AP214`` / ``AP242`` / ``AP203e2`` read from the header's FILE_SCHEMA, None when unknown."""
    head = data[:16384].decode("latin-1", errors="replace")
    return _header(head).get("application_protocol")


def solid_count(data: bytes) -> int:
    """Solids OCCT reads from STEP bytes (every instance of an assembly counts); 0 when unreadable."""
    S._quiet()
    reader = STEPControl_Reader()
    try:
        if reader.ReadStream("check.step", io.BytesIO(S.normalize_p21(data)[0])) != IFSelect_ReturnStatus.IFSelect_RetDone:
            return 0
    except Exception:  # pragma: no cover - reader raises rarely
        return 0
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        return 0
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        n += 1
        exp.Next()
    return n


def read_back(data: bytes) -> dict[str, Any]:
    """The independent read-back facts of a written file: ``{protocol, file_schema, solids}``."""
    head = _header(data[:16384].decode("latin-1", errors="replace"))
    return {"protocol": head.get("application_protocol"), "file_schema": head.get("step_schema"), "solids": solid_count(data)}


def reexport(data: bytes, protocol: str = DEFAULT_PROTOCOL) -> bytes:
    """STEP bytes read by OCCT and written again in ``protocol`` (names, structure and placements kept). Raises
    :class:`StepProtocolError`: ``step_unreadable`` when OCCT cannot read the source, ``step_protocol_unmet`` when the
    written file is not in the protocol or does not read back with the source's solid count."""
    norm, _ = S.normalize_p21(data)
    S._quiet()
    doc, _tool = S._new_doc()
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    try:
        status = reader.ReadStream("source.step", io.BytesIO(norm))
    except Exception as exc:  # pragma: no cover - reader raises rarely
        raise StepProtocolError("step_unreadable", f"the STEP reader failed: {exc}") from exc
    if status != IFSelect_ReturnStatus.IFSelect_RetDone or not reader.Transfer(doc):
        raise StepProtocolError("step_unreadable", "OCCT cannot read the source STEP for a re-export")
    name = _header(norm[:16384].decode("latin-1", errors="replace")).get("file_name") or "model"
    out = S.write_doc(doc, name=name, protocol=protocol)
    want, got = solid_count(data), solid_count(out)
    if want != got or got == 0:
        raise StepProtocolError("step_protocol_unmet", f"the {protocol} re-export reads back with {got} solid(s), the source "
                                f"with {want}", {"required": protocol, "solids": {"source": want, "reexport": got}})
    return out


__all__ = ["DEFAULT_PROTOCOL", "STEP_PROTOCOLS", "StepProtocolError", "protocol_of", "read_back", "reexport", "solid_count"]
