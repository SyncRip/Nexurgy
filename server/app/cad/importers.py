"""Inspect uploaded model files.

- STEP: exact B-rep. :func:`inspect_step` is a kernel build with the file as
  base (``F-00``) and no ops, so the import path gets metrics, previews,
  STEP re-export and heuristic ``detected_holes`` without any AI. Feature
  trees of the original CAD system are not recovered.
- STL / OBJ / GLB: triangle meshes, reference only. :func:`inspect_mesh`
  parses them with numpy (no extra dependencies) and reports a bounding box,
  triangle count, an approximate volume when the mesh is closed and a preview.
  Meshes can never become a model base or be exported to STEP.

Units: STL / OBJ carry no units and are assumed to be mm. GLB is metres with
+Y up by the glTF spec; it is converted to mm with +Z up for the reported
numbers (the GLB preview bytes are passed through unchanged).

``inspect_mesh`` does not import build123d, so it is cheap to call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from typing import TYPE_CHECKING, Literal

import numpy as np

from .results import BBox, Issue, MeshInspection

if TYPE_CHECKING:  # pragma: no cover
    from .results import BuildResult

MeshExt = Literal["stl", "obj", "glb"]

_GL_COMPONENTS = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_GL_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class MeshParseError(ValueError):
    """Raised when a mesh file cannot be parsed at all."""


def inspect_step(data: bytes, *, tolerance: float = 0.05, angular_tolerance: float = 0.2) -> "BuildResult":
    """Import STEP bytes as a base with no ops (see :func:`app.cad.kernel.build`).

    The returned ``BuildResult`` has ``features == [F-00]``, metrics, all
    artifacts (STEP re-export, GLB, mesh_json) and ``detected_holes``.
    """
    from .kernel import build
    from .ops import ModelSpec, StepBase

    spec = ModelSpec(base=StepBase(blob_sha256=hashlib.sha256(data).hexdigest()), ops=[])
    return build(spec, data, tolerance=tolerance, angular_tolerance=angular_tolerance)


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


def parse_stl(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(positions (N,3), triangles (M,3))`` from ASCII or binary STL."""
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            rec = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            arr = np.frombuffer(data, dtype=rec, count=n, offset=84)
            pos = arr["v"].reshape(-1, 3).astype(float)
            return pos, np.arange(len(pos), dtype=np.int64).reshape(-1, 3)
    text = data.decode("latin-1")
    if not text.lstrip().lower().startswith("solid"):
        raise MeshParseError("not a valid STL file (neither binary nor ASCII)")
    num = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    verts = re.findall(rf"vertex\s+({num})\s+({num})\s+({num})", text)
    if len(verts) % 3:
        raise MeshParseError("ASCII STL has an incomplete facet")
    pos = np.array(verts, dtype=float).reshape(-1, 3)
    return pos, np.arange(len(pos), dtype=np.int64).reshape(-1, 3)


def parse_obj(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(positions, triangles)`` from OBJ ``v`` / ``f`` records.

    Polygons are fan-triangulated; negative (relative) indices are supported;
    texture / normal indices are ignored.
    """
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("v "):
            parts = line.split()
            if len(parts) < 4:
                raise MeshParseError(f"bad vertex line: {line[:60]}")
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            idx = []
            for tok in line.split()[1:]:
                i = int(tok.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
    pos = np.array(verts, dtype=float).reshape(-1, 3)
    tri = np.array(tris, dtype=np.int64).reshape(-1, 3)
    if len(tri) and (tri.min() < 0 or tri.max() >= len(pos)):
        raise MeshParseError("OBJ face references a missing vertex")
    return pos, tri


def _quat_matrix(q: list[float]) -> np.ndarray:
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=float).reshape(4, 4).T  # column-major
    m = np.eye(4)
    s = np.diag(node.get("scale", [1.0, 1.0, 1.0]))
    r = _quat_matrix(node.get("rotation", [0.0, 0.0, 0.0, 1.0]))
    m[:3, :3] = r @ s
    m[:3, 3] = node.get("translation", [0.0, 0.0, 0.0])
    return m


def parse_glb(data: bytes) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Return world-space ``(positions, triangles, notes)`` of a binary glTF.

    Node transforms are applied (glTF frame: metres, +Y up). Triangle lists,
    strips and fans are supported; Draco / meshopt-compressed or externally
    referenced buffers are not (``notes`` explains what was skipped).
    """
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshParseError("not a binary glTF (GLB) file")
    version, total = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise MeshParseError(f"unsupported glTF version {version}")
    off, doc, binchunk = 12, None, b""
    while off + 8 <= min(total, len(data)):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8 : off + 8 + clen]
        if ctype == 0x4E4F534A:  # JSON
            doc = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:  # BIN
            binchunk = chunk
        off += 8 + clen
    if doc is None:
        raise MeshParseError("GLB has no JSON chunk")
    notes: list[str] = []
    if any(e in doc.get("extensionsUsed", []) for e in ("KHR_draco_mesh_compression", "EXT_meshopt_compression")):
        notes.append("compressed mesh data (Draco / meshopt) is not decoded")

    buffers: list[bytes | None] = []
    for i, b in enumerate(doc.get("buffers", [])):
        uri = b.get("uri")
        if uri is None:
            buffers.append(binchunk if i == 0 else None)
        elif uri.startswith("data:") and ";base64," in uri:
            buffers.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            buffers.append(None)
            notes.append(f"external buffer {uri!r} is not available")

    def accessor(idx: int) -> np.ndarray | None:
        acc = doc["accessors"][idx]
        if "bufferView" not in acc or acc.get("sparse"):
            return None
        view = doc["bufferViews"][acc["bufferView"]]
        buf = buffers[view["buffer"]] if view["buffer"] < len(buffers) else None
        if buf is None:
            return None
        dtype = np.dtype(_GL_COMPONENTS[acc["componentType"]]).newbyteorder("<")
        width = _GL_WIDTH[acc["type"]]
        count = acc["count"]
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = view.get("byteStride") or dtype.itemsize * width
        if stride == dtype.itemsize * width:
            arr = np.frombuffer(buf, dtype=dtype, count=count * width, offset=start).reshape(count, width)
        else:
            raw = np.frombuffer(buf, dtype=np.uint8, count=stride * (count - 1) + dtype.itemsize * width, offset=start)
            rows = [raw[k * stride : k * stride + dtype.itemsize * width].view(dtype) for k in range(count)]
            arr = np.vstack(rows).reshape(count, width)
        return arr.astype(np.float64 if acc["componentType"] == 5126 else np.int64)

    all_pos: list[np.ndarray] = []
    all_tri: list[np.ndarray] = []
    offset = 0

    def emit(mesh_idx: int, world: np.ndarray) -> None:
        nonlocal offset
        for prim in doc["meshes"][mesh_idx].get("primitives", []):
            mode = prim.get("mode", 4)
            if mode not in (4, 5, 6) or "POSITION" not in prim.get("attributes", {}):
                continue
            if prim.get("extensions"):
                notes.append("a primitive uses an extension and was skipped")
                continue
            pos = accessor(prim["attributes"]["POSITION"])
            if pos is None:
                notes.append("a primitive has unreadable positions and was skipped")
                continue
            ind = accessor(prim["indices"]).reshape(-1) if "indices" in prim else np.arange(len(pos), dtype=np.int64)
            if mode == 4:
                tri = ind[: len(ind) // 3 * 3].reshape(-1, 3)
            elif mode == 5:
                tri = np.array(
                    [(ind[k], ind[k + 1], ind[k + 2]) if k % 2 == 0 else (ind[k + 1], ind[k], ind[k + 2]) for k in range(len(ind) - 2)],
                    dtype=np.int64,
                ).reshape(-1, 3)
            else:
                tri = np.array([(ind[0], ind[k], ind[k + 1]) for k in range(1, len(ind) - 1)], dtype=np.int64).reshape(-1, 3)
            homo = np.hstack([pos[:, :3], np.ones((len(pos), 1))])
            all_pos.append((homo @ world.T)[:, :3])
            all_tri.append(tri + offset)
            offset += len(pos)

    def walk(node_idx: int, parent: np.ndarray) -> None:
        node = doc["nodes"][node_idx]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            emit(node["mesh"], world)
        for child in node.get("children", []):
            walk(child, world)

    scenes = doc.get("scenes", [])
    if scenes:
        roots = scenes[doc.get("scene", 0)].get("nodes", [])
        for r in roots:
            walk(r, np.eye(4))
    else:
        for m in range(len(doc.get("meshes", []))):
            emit(m, np.eye(4))
    if not all_pos:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), notes
    return np.vstack(all_pos), np.vstack(all_tri), notes


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def weld(positions: np.ndarray, triangles: np.ndarray, decimals: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident vertices (STL repeats them per facet)."""
    if len(positions) == 0:
        return positions, triangles
    key = np.round(positions, decimals) + 0.0
    uniq, inverse = np.unique(key, axis=0, return_inverse=True)
    return uniq, inverse.reshape(-1)[triangles]


def is_watertight(triangles: np.ndarray) -> bool:
    """Every edge shared by exactly two triangles with opposite directions."""
    if len(triangles) == 0:
        return False
    t = triangles[(triangles[:, 0] != triangles[:, 1]) & (triangles[:, 1] != triangles[:, 2]) & (triangles[:, 0] != triangles[:, 2])]
    directed = np.vstack([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    _, dcount = np.unique(directed, axis=0, return_counts=True)
    if dcount.max() > 1:
        return False  # duplicated / inconsistently oriented
    undirected = np.sort(directed, axis=1)
    _, ucount = np.unique(undirected, axis=0, return_counts=True)
    return bool(np.all(ucount == 2))


def signed_volume(positions: np.ndarray, triangles: np.ndarray) -> float:
    """Volume enclosed by a closed, consistently oriented mesh (tetrahedra)."""
    if len(triangles) == 0:
        return 0.0
    a, b, c = positions[triangles[:, 0]], positions[triangles[:, 1]], positions[triangles[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def inspect_mesh(data: bytes, ext: MeshExt) -> MeshInspection:
    """Measure a mesh reference file. Raises :class:`MeshParseError` on
    unreadable input. The result is always ``reference_only``."""
    from .meshing import bbox_dict, triangle_area, triangle_mesh_json

    warnings: list[Issue] = []
    ext_l = ext.lower()
    if ext_l == "stl":
        pos, tri = parse_stl(data)
        units = "mm (assumed; STL has no units)"
    elif ext_l == "obj":
        pos, tri = parse_obj(data)
        units = "mm (assumed; OBJ has no units)"
    elif ext_l == "glb":
        pos, tri, notes = parse_glb(data)
        # glTF: metres, +Y up  ->  mm, +Z up
        pos = np.column_stack([pos[:, 0], -pos[:, 2], pos[:, 1]]) * 1000.0 if len(pos) else pos
        units = "m (glTF) converted to mm, +Y up converted to +Z up"
        warnings.extend(Issue(code="glb_partial", message=n) for n in notes)
    else:
        raise MeshParseError(f"unsupported mesh format {ext!r}")

    welded_pos, welded_tri = weld(pos, tri)
    watertight = is_watertight(welded_tri)
    volume = None
    if watertight:
        volume = round(abs(signed_volume(welded_pos, welded_tri)), 4)
    else:
        warnings.append(Issue(code="not_watertight", message="mesh is open or non-manifold; volume not computed"))
    bbox = None
    if len(welded_pos):
        bbox = BBox(**bbox_dict(welded_pos.min(axis=0), welded_pos.max(axis=0), 4))
    else:
        warnings.append(Issue(code="empty_mesh", message="no triangles found"))

    if ext_l == "glb":
        preview, preview_format = data, "glb"
    else:
        preview, preview_format = triangle_mesh_json(welded_pos, welded_tri), "mesh_json"
    warnings.append(Issue(code="reference_only", message="mesh files are reference only: no STEP export, cannot be a model base"))
    return MeshInspection(
        format=ext_l,  # type: ignore[arg-type]
        units_assumed=units,
        triangles=int(len(tri)),
        vertices=int(len(welded_pos)),
        bbox=bbox,
        volume_mm3=volume,
        area_mm2=round(triangle_area(welded_pos, welded_tri), 4) if len(welded_tri) else None,
        watertight=watertight,
        warnings=warnings,
        preview_format=preview_format,  # type: ignore[arg-type]
        preview=preview,
    )
