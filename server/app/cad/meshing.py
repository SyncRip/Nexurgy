"""Tessellation and the ``mesh_json`` preview format.

``mesh_json`` (format ``partroute-mesh/1``) is what the editor uses for display
and picking. Coordinates are mm, world +Z up, rounded to 1e-4 mm::

    {
      "format": "partroute-mesh/1", "units": "mm", "up": "+z",
      "bbox": {"min": [x,y,z], "max": [...], "size": [...]},
      "faces": [{"id": 0, "surface": "plane", "normal": [0,0,1],
                 "center": [...], "area": 12.5,
                 "positions": [x0,y0,z0, x1,...], "indices": [0,1,2, ...]}, ...],
      "edges": [{"id": 0, "kind": "line", "points": [[x,y,z], ...]}, ...]
    }

Face and edge ids are the 0-based indices of ``shape.faces()`` /
``shape.edges()``; they are stable for one build only (selectors in ops use
geometry, not ids). ``indices`` are local to each face's ``positions``.
Cylinder and cone faces also carry ``radius`` and ``axis`` (origin +
direction) to help hole picking; seam edges carry ``"seam": true``. Triangles are counter-clockwise seen from
outside the solid.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

# OCP / build123d are imported inside the functions that need them so that
# the pure helpers (used by mesh-file inspection) stay cheap to import.

MESH_FORMAT = "partroute-mesh/1"
ROUND = 4  # decimals, i.e. 1e-4 mm


def _round_list(values: np.ndarray | list[float], ndigits: int = ROUND) -> list[float]:
    arr = np.round(np.asarray(values, dtype=float), ndigits) + 0.0  # + 0.0 drops -0.0
    return arr.reshape(-1).tolist()


def _vec(v: Any, ndigits: int = 6) -> list[float]:
    return [round(float(v.X), ndigits) + 0.0, round(float(v.Y), ndigits) + 0.0, round(float(v.Z), ndigits) + 0.0]


def bbox_dict(lo: list[float] | np.ndarray, hi: list[float] | np.ndarray, ndigits: int = 6) -> dict[str, list[float]]:
    lo_l = [round(float(x), ndigits) + 0.0 for x in lo]
    hi_l = [round(float(x), ndigits) + 0.0 for x in hi]
    return {"min": lo_l, "max": hi_l, "size": [round(b - a, ndigits) + 0.0 for a, b in zip(lo_l, hi_l)]}


def seam_edge_set(shape: Any) -> set[Any]:
    """Edges that only border one face (cylinder / cone seams)."""
    from build123d import Edge
    from OCP.collections import IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as _EdgeFaceMap
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp

    emap = _EdgeFaceMap()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, emap)
    seams: set[Any] = set()
    for i in range(1, emap.Extent() + 1):
        faces = emap.FindFromIndex(i)  # iterating this OCP list is slow; use First/Last
        n = faces.Size()
        if n == 1 or (n == 2 and faces.First().IsSame(faces.Last())):
            seams.add(Edge(emap.FindKey(i)))
    return seams


def mesh_shape(shape: Any, tolerance: float, angular_tolerance: float) -> None:
    """Triangulate ``shape`` in place with an absolute linear deflection.

    Running this before ``export_gltf`` with the same tolerances makes the GLB
    reuse exactly this triangulation (build123d only re-meshes when the stored
    deflection is coarser than requested).
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    BRepMesh_IncrementalMesh(shape.wrapped, tolerance, False, angular_tolerance, True)


def face_triangles(face: Any) -> tuple[np.ndarray, np.ndarray]:
    """Positions (N,3) and outward-oriented triangles (M,3) of a meshed face."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_Orientation
    from OCP.TopLoc import TopLoc_Location

    loc = TopLoc_Location()
    poly = BRep_Tool.Triangulation_s(face.wrapped, loc)
    if poly is None:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    trsf = loc.Transformation()
    identity = loc.IsIdentity()
    n = poly.NbNodes()
    pts = np.empty((n, 3))
    for i in range(1, n + 1):
        p = poly.Node(i)
        if not identity:
            p = p.Transformed(trsf)
        pts[i - 1] = (p.X(), p.Y(), p.Z())
    m = poly.NbTriangles()
    tris = np.empty((m, 3), dtype=np.int64)
    for i in range(1, m + 1):
        a, b, c = poly.Triangle(i).Get()
        tris[i - 1] = (a - 1, b - 1, c - 1)
    if face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        tris = tris[:, [0, 2, 1]]
    return pts, tris


def edge_polyline(edge: Any, tolerance: float, angular_tolerance: float) -> list[list[float]]:
    """Polyline approximation of an edge (empty for degenerate edges)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_TangentialDeflection

    if BRep_Tool.Degenerated_s(edge.wrapped):
        return []
    if edge.geom_type.name == "LINE":
        pts = [edge.start_point(), edge.end_point()]
        return [_round_list([p.X, p.Y, p.Z]) for p in pts]
    curve = BRepAdaptor_Curve(edge.wrapped)
    disc = GCPnts_TangentialDeflection(curve, angular_tolerance, tolerance, 2)
    out = []
    for i in range(1, disc.NbPoints() + 1):
        p = disc.Value(i)
        out.append(_round_list([p.X(), p.Y(), p.Z()]))
    return out


def _surface_info(face: Any) -> dict[str, Any]:
    kind = face.geom_type.name.lower()
    info: dict[str, Any] = {"surface": kind}
    if kind == "plane":
        info["normal"] = _vec(face.normal_at(face.center()))
    elif kind in ("cylinder", "cone"):
        surf = face.geom_adaptor()
        if not hasattr(surf, "Radius" if kind == "cylinder" else "RefRadius"):
            # A trimmed surface (e.g. from a sweep) hides the elementary surface: read it through the face adaptor.
            from OCP.BRepAdaptor import BRepAdaptor_Surface

            adaptor = BRepAdaptor_Surface(face.wrapped)
            surf = adaptor.Cylinder() if kind == "cylinder" else adaptor.Cone()
        ax = surf.Axis() if hasattr(surf, "Axis") else surf.Position().Axis()
        loc = ax.Location()
        d = ax.Direction()
        info["axis"] = {
            "origin": [round(loc.X(), 6) + 0.0, round(loc.Y(), 6) + 0.0, round(loc.Z(), 6) + 0.0],
            "direction": [round(d.X(), 6) + 0.0, round(d.Y(), 6) + 0.0, round(d.Z(), 6) + 0.0],
        }
        if kind == "cylinder":
            info["radius"] = round(float(surf.Radius()), 6)
        else:
            info["ref_radius"] = round(float(surf.RefRadius()), 6)
            info["semi_angle_deg"] = round(float(np.degrees(surf.SemiAngle())), 6)
    return info


def shape_mesh_json(shape: Any, tolerance: float, angular_tolerance: float) -> bytes:
    """Serialise a (meshed or not yet meshed) solid to ``mesh_json`` bytes."""
    from build123d import CenterOf
    from OCP.BRep import BRep_Tool

    mesh_shape(shape, tolerance, angular_tolerance)
    faces_out = []
    for idx, face in enumerate(shape.faces()):
        pts, tris = face_triangles(face)
        entry: dict[str, Any] = {"id": idx}
        entry.update(_surface_info(face))
        entry["center"] = _vec(face.center(CenterOf.MASS), ROUND)
        entry["area"] = round(float(face.area), 4)
        entry["positions"] = _round_list(pts)
        entry["indices"] = tris.reshape(-1).tolist()
        faces_out.append(entry)
    edges_out = []
    seams = seam_edge_set(shape)
    for idx, edge in enumerate(shape.edges()):
        degenerate = BRep_Tool.Degenerated_s(edge.wrapped)
        item: dict[str, Any] = {
            "id": idx,
            "kind": "degenerate" if degenerate else edge.geom_type.name.lower(),
            "points": edge_polyline(edge, tolerance, angular_tolerance),
        }
        if edge in seams:
            item["seam"] = True  # parametric seam, usually hidden by viewers
        edges_out.append(item)
    from .bounds import tight_bounds

    lo, hi = tight_bounds(shape)
    doc = {
        "format": MESH_FORMAT,
        "units": "mm",
        "up": "+z",
        "bbox": bbox_dict(list(lo), list(hi), ROUND),
        "faces": faces_out,
        "edges": edges_out,
    }
    return json.dumps(doc, separators=(",", ":")).encode()


def triangle_mesh_json(positions: np.ndarray, triangles: np.ndarray) -> bytes:
    """``mesh_json`` for a plain triangle mesh (reference STL/OBJ previews).

    The whole mesh is one face with ``surface: "mesh"``; there are no edges.
    """
    positions = np.asarray(positions, dtype=float).reshape(-1, 3)
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if len(positions):
        lo, hi = positions.min(axis=0), positions.max(axis=0)
        center = (lo + hi) / 2.0
    else:
        lo = hi = center = np.zeros(3)
    area = triangle_area(positions, triangles)
    doc = {
        "format": MESH_FORMAT,
        "units": "mm",
        "up": "+z",
        "bbox": bbox_dict(lo, hi, ROUND),
        "faces": [
            {
                "id": 0,
                "surface": "mesh",
                "center": _round_list(center),
                "area": round(area, 4),
                "positions": _round_list(positions),
                "indices": triangles.reshape(-1).tolist(),
            }
        ],
        "edges": [],
    }
    return json.dumps(doc, separators=(",", ":")).encode()


def triangle_area(positions: np.ndarray, triangles: np.ndarray) -> float:
    if len(triangles) == 0:
        return 0.0
    a, b, c = positions[triangles[:, 0]], positions[triangles[:, 1]], positions[triangles[:, 2]]
    return float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() / 2.0)
