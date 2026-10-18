"""2D drawing files as inputs (DXF / native DWG): format detection and DWG → derived DXF with provenance.

Shared by the strict PROFILE import (services/revisions.py) and the AI reconstruction
(services/ai_reconstruct.py). Rules (design §0.3, §5, §6; app/cad/DWG_NOTES.md):

- the format comes from the bytes, checked against the file name: a ``.dwg`` must really be
  a DWG (``require_dwg``: a DXF renamed to .dwg is refused, ``dwg_not_dwg``), and a ``.dxf``
  holding DWG bytes is refused too (``drawing_format_mismatch``);
- a DWG is converted by the LibreDWG adapter (``read_dwg``, separate process, fail closed) into
  a labelled, derived DXF. The original is never changed; the derived DXF is stored as its own
  blob and the read report (source sha / version, converter version + fingerprint, steps,
  losses, warnings) is returned as provenance for run steps and ``revision.checks["source"]``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from sqlalchemy.orm import Session

from ..errors import AppError
from ..storage import save_blob

DrawingFormat = Literal["dxf", "dwg"]
PROVENANCE_SCHEMA = "partroute.drawing_source/1"
DXF_CONTENT_TYPE = "image/vnd.dxf"
_DXF_HEAD = re.compile(rb"\s*(?:0\s*\r?\n\s*SECTION|999\s*\r?\n)")


def drawing_ext(name: str | None) -> DrawingFormat | None:
    """``dxf`` / ``dwg`` from a file name, else None."""
    lower = (name or "").lower().strip()
    if lower.endswith(".dxf"):
        return "dxf"
    if lower.endswith(".dwg"):
        return "dwg"
    return None


def looks_like_dxf(data: bytes) -> bool:
    head = bytes(data[:512]).lstrip(b"\xef\xbb\xbf")
    return head.startswith(b"AutoCAD Binary DXF") or bool(_DXF_HEAD.match(head))


def sniff(name: str | None, data: bytes) -> DrawingFormat | None:
    """The drawing format of a file: its extension, else its magic bytes (None: not a CAD drawing)."""
    from ..cad.dwg import detect_dwg_version

    ext = drawing_ext(name)
    if ext is not None:
        return ext
    if detect_dwg_version(data):
        return "dwg"
    if looks_like_dxf(data):
        return "dxf"
    return None


def drawing_format(name: str | None, data: bytes) -> DrawingFormat:
    """The verified format of drawing bytes, or a 422: the content must match the name."""
    from ..cad.dwg import DwgError, detect_dwg_version, require_dwg

    ext = drawing_ext(name)
    version = detect_dwg_version(data)
    if ext == "dwg":
        try:
            require_dwg(data)
        except DwgError as exc:
            raise AppError(422, exc.code, f"{name}: {exc.message}; a DXF renamed to .dwg is not a DWG file",
                           {"name": name, **compact(exc.details)})
        return "dwg"
    if ext == "dxf":
        if version is not None:
            raise AppError(422, "drawing_format_mismatch", f"{name} holds DWG data ({version}), not DXF; upload it as .dwg",
                           {"name": name, "dwg_version": version})
        if not looks_like_dxf(data):
            raise AppError(422, "dxf_unreadable", f"{name} is not a DXF file", {"name": name})
        return "dxf"
    if version is not None:
        return "dwg"
    if looks_like_dxf(data):
        return "dxf"
    raise AppError(422, "unsupported_drawing_file", "only DXF and DWG drawings can be read here", {"name": name})


def dwg_to_dxf(db: Session, data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Convert DWG bytes with the LibreDWG adapter; store the derived DXF as a blob.

    Returns ``(derived_dxf, provenance)``. Converter problems are AppErrors with the adapter's code:
    503 when the runtime is missing or timed out (an infrastructure state, retry later), else 422."""
    from ..cad.dwg import DwgError, dwg_runtime, read_dwg
    from ..config import get_settings

    settings = get_settings()
    try:
        res = read_dwg(data, runtime=dwg_runtime(settings.libredwg_bin_dir), timeout_s=settings.dwg_timeout_s)
    except DwgError as exc:
        status = 503 if exc.code in ("dwg_runtime_unavailable", "dwg_timeout") else 422
        raise AppError(status, exc.code, exc.message, {"dwg": compact(exc.details)})
    derived = bytes(res.dxf)
    sha = save_blob(db, derived, DXF_CONTENT_TYPE)
    rt = res.runtime
    prov = {
        "source": res.source.model_dump(mode="json"),
        "version_status": res.version_status,
        "converter": {"name": "GNU LibreDWG", "version": rt.libredwg_version, "fingerprint": rt.fingerprint,
                      "independent_reader": rt.independent_reader, "ezdxf": rt.ezdxf_version},
        "raw_dxf": res.raw_dxf.model_dump(mode="json"),
        "derived_dxf": {**res.derived_dxf.model_dump(mode="json"), "blob_sha256": sha},
        "load_mode": res.load_mode,
        "steps": [compact({k: v for k, v in s.model_dump(mode="json").items() if k not in ("argv",)}) for s in res.steps],
        "losses": [compact(i.model_dump(mode="json")) for i in res.losses],
        "warnings": [compact(i.model_dump(mode="json")) for i in res.warnings],
        "quarantined": len(res.quarantined),
        "seconds": res.seconds,
    }
    return derived, prov


def provenance(*, fmt: DrawingFormat, name: str, data: bytes, dxf: bytes, dwg: dict[str, Any] | None,
               file_version_id: Any = None, drawing_id: Any = None) -> dict[str, Any]:
    """The source record kept in run steps and ``revision.checks["source"]``."""
    import hashlib

    return {
        "schema": PROVENANCE_SCHEMA,
        "format": fmt,
        "name": name,
        "file_version_id": str(file_version_id) if file_version_id else None,
        "drawing_id": str(drawing_id) if drawing_id else None,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_bytes": len(data),
        "dxf_sha256": hashlib.sha256(dxf).hexdigest(),
        "dxf_bytes": len(dxf),
        "dxf_role": "derived" if fmt == "dwg" else "original",
        "dwg": dwg,
    }


def compact(value: Any, *, max_list: int = 20, max_str: int = 400, depth: int = 0) -> Any:
    """JSON-safe copy with long lists / strings cut (marked), for provenance records."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {str(k): compact(v, max_list=max_list, max_str=max_str, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [compact(v, max_list=max_list, max_str=max_str, depth=depth + 1) for v in list(value)[:max_list]]
        if len(value) > max_list:
            items.append(f"… {len(value) - max_list} more")
        return items
    if isinstance(value, str) and len(value) > max_str:
        return value[: max_str - 1] + "…"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value
