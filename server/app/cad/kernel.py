"""Cloud CAD kernel: replay a :class:`~app.cad.ops.ModelSpec` into an exact
B-rep solid (build123d / Open CASCADE) and export it.

``build()`` replays the optional imported base (implicit op ``F-00``) and the
ops in order, from scratch, and exports STEP, GLB and ``mesh_json`` from the
same final in-memory solid. Units are mm and degrees, world +Z up.

Placement conventions (complementing the docstrings in ``ops.py``):

- ``box``: ``at`` is the centre of the bottom face; ``size`` is along X/Y/Z.
- ``cylinder``: ``at`` is the centre of the base circle; the body extends
  ``height`` along +``axis``.
- ``extrude``: the profile lies on ``plane`` (build123d planes: ``XY`` u=X v=Y
  normal +Z; ``XZ`` u=X v=Z normal -Y; ``YZ`` u=Y v=Z normal +X), shifted by
  ``offset`` along the normal; ``at`` is the profile origin in (u, v) and the
  profile is rotated by ``rotate_profile`` degrees about it (counter-clockwise
  seen from the normal). The body extends ``height`` along the normal.
  ``rect``: ``width`` along u, ``length`` along v, centred. ``slot``: overall
  ``length`` along u. ``circle`` / ``ring``: centred. ``polygon``: points are
  profile-local (u, v) offsets from ``at`` (not re-centred).
- Body ``rotate = [rx, ry, rz]`` rotates the body about world-parallel axes
  through the op's own anchor (box/cylinder ``at``; extrude profile origin in
  world space), applied X, then Y, then Z.
- ``transform`` re-places the *tool solid* of its target body op before that
  op's boolean, i.e. exactly as if the target's ``rotate``/``at`` had been
  edited: first rotate about the target's current anchor (X, Y, Z order), then
  translate. Several transforms on one target compose in list order, each
  rotating about the anchor as already moved by the previous ones. Because
  the change is applied at the target, every op after the target sees the
  moved body (even ops listed before the transform). Only body ops can be
  targets.
- ``hole``: one cut per op. ``depth`` (blind) is measured from the entry point
  along ``direction`` and gives a flat-bottomed bore; ``depth = None`` cuts
  through all material of the current solid ahead of the entry point. The
  tool starts 0.01 mm before the entry point to avoid coplanar faces.
  Counterbore ``depth`` is also measured from the entry point. Countersink:
  a cone from ``diameter`` at the entry point narrowing at the included
  ``angle`` to the hole diameter. Pattern placement: see
  :func:`app.cad.ops.hole_positions`. Every instance must remove material:
  if any instance misses the part (or a through hole points away from it) the
  op fails with ``hole_missed`` and ``errors[].details`` lists the missed
  instance indices / positions, so ``HoleRecord.count`` is always the number
  of holes actually cut.
- ``fillet`` / ``chamfer``: edges chosen by :func:`select_edges`.

W3 geometry library (frames and loops: :mod:`app.cad.sketch`; datums: :mod:`app.cad.datums`):

- ``plane`` of extrude / cut / sweep / bent plate is a named plane, a ``Workplane`` (origin, normal, x_dir) or a
  ``DatumRef`` to a datum of an earlier op; extrude / cut then apply ``offset`` / ``at`` / ``rotate_profile`` in
  that frame exactly as for a named plane (named planes keep their original build123d code path). A ``sketch``
  profile is built from its line / arc loops (arcs through three points), holes cut from the outline.
- ``hole`` with a local ``plane``: ``at`` / ``direction`` are plane coordinates (:func:`app.cad.ops.hole_entry`,
  :func:`app.cad.ops.hole_world_frame`); the cut itself is the same as for a world hole.
- ``cut``: the profile on its frame, the tool starts 0.01 mm outside the entry face and goes against the plane
  normal ``depth`` mm (flat floor) or through all material ahead (``depth`` null). It must remove material
  (``cut_missed``).
- ``revolve``: profile frame u = axis, v = ``ref_dir`` (radial), origin ``at``; revolved ``angle`` degrees
  counter-clockwise about +axis. Anchor ``at``.
- ``sweep``: the section frame at the path start has u = path-plane normal × tangent (left of travel), v = the
  path-plane normal; the path wire is lines and three-point arcs. Anchor = path start.
- ``bent_plate``: :func:`app.cad.sketch.bent_plate_layout` drawn on the placed frame, extruded ``width`` along the
  normal. Anchor = the frame origin.
- ``shell``: the one solid hollowed inward (OCCT thick solid, intersection joins); ``open_faces`` pick the planar
  faces whose outward normal matches, at the extreme level along that normal (``no_faces_selected``).
- ``coil``: the law of :mod:`app.cad.coil` built by :mod:`app.cad.coil_occ` (exact B-spline centre line on the
  cylinder of the mean radius about ``axis`` through ``at``, the section swept in the Frenet frame, ground ends cut by
  the slab 0 ≤ z ≤ length). Anchor ``at``.
- ``pattern``: copies of the target's tool (body as placed, hole pieces, cut prism) moved by the instance
  transforms (:func:`app.cad.datums.pattern_matrices`) and applied with the target's boolean; through holes /
  cuts are sized for the farthest instance. Every copy must change the solid (``pattern_instance_missed``,
  ``details.missed_instances``); patterned holes add one ``HoleRecord`` for the pattern op.
- ``text`` (generation reliability M): the glyph faces of :func:`app.cad.text_geometry.text_faces` (font chosen by
  :func:`~app.cad.text_geometry.resolve_font`), centred on ``at`` of the placed frame (``plane`` / ``offset`` /
  ``rotation`` as for extrude), extruded ``depth`` along the normal and fused (raised), or from 0.01 mm outside the
  plane ``|depth|`` against it and cut (engraved). Every glyph must sit on / in material: raised letters must add
  their whole prism without a new separate solid, engraved ones remove their whole prism (``text_missed``). A
  substituted font is a ``font_substituted`` warning (``details``: requested, used, file, reason, impact); no usable
  font is ``unsupported_font`` (deterministic: the same text and fonts fail the same way).

Every op leaves an :class:`OpTrace` (solid before / after, tool solids, frames) on ``result.trace`` for the
independent feature checks (:mod:`app.cad.feature_checks`); it is never serialised.

Failure handling: the first failing op stops the replay (later ops are
reported as ``skipped``); ``errors`` carries ``{op_id, code, message}``.
Codes: ``base_missing``, ``base_hash_mismatch``, ``base_import_failed``,
``no_body``, ``invalid_profile``, ``invalid_mode``, ``boolean_failed``,
``empty_result``, ``invalid_solid``, ``invalid_hole``, ``hole_missed``,
``hole_failed``, ``no_edges_selected``, ``fillet_failed``, ``chamfer_failed``,
``invalid_transform_target``, ``export_failed``, ``internal_error``;
W3: ``datum_missing``, ``invalid_workplane``, ``cut_missed``, ``cut_failed``, ``revolve_failed``,
``sweep_failed``, ``bent_plate_failed``, ``shell_failed``, ``no_faces_selected``, ``invalid_pattern_target``,
``pattern_failed``, ``pattern_instance_missed``; ``coil_failed``; ``unsupported_font``, ``text_failed``, ``text_missed``.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

import build123d as bd
from OCP.BRepAdaptor import BRepAdaptor_Surface
import OCP
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_State
from OCP.TopoDS import TopoDS

from . import datums as dt
from .bounds import tight_bounds
from .bounds import volume as precise_volume
from .coil_occ import CoilBuildError, coil_solid
from .meshing import bbox_dict, seam_edge_set, shape_mesh_json
from .ops import (
    BODY_TYPES,
    PATTERN_TARGET_TYPES,
    BentPlateOp,
    BoxOp,
    ChamferOp,
    CoilOp,
    CutOp,
    CylinderOp,
    EdgeSelector,
    ExtrudeOp,
    FilletOp,
    HoleOp,
    ModelSpec,
    Operation,
    PatternOp,
    RevolveOp,
    ShellOp,
    SweepOp,
    TextOp,
    TransformOp,
    Vec3,
    build_key,
    hole_entry,
    hole_frame,
    hole_positions,
    hole_world_frame,
)
from .sketch import Frame, Seg, bent_plate_layout, profile_loops, resolve_loop
from .results import (
    BBox,
    BuildArtifacts,
    BuildResult,
    DetectedHole,
    FeatureRecord,
    HoleRecord,
    Issue,
    Metrics,
)


def _short_version(v: str, parts: int = 3) -> str:
    return ".".join(str(v).split(".")[:parts])


KERNEL_VERSION: str = f"build123d-{_short_version(bd.__version__)}/ocp-{_short_version(getattr(OCP, '__version__', '0'))}"

# Fixed STEP header timestamp so identical geometry gives identical STEP bytes.
STEP_TIMESTAMP = "2000-01-01T00:00:00"
HOLE_BACKOFF = 0.01  # mm the hole tool starts before the entry point
THROUGH_MARGIN = 1.0  # mm a through-hole tool extends beyond the part
MIN_VOLUME = 1e-9
MISS_VOLUME = 1e-6  # mm³ a hole instance must remove to count as a hit


class KernelError(Exception):
    """A build failure attributed to one op."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class OpTrace:
    """What one successful op did, for the independent feature checks: the solid before and after, the tool
    solids it applied (before the boolean) and op-specific placement info (frames, instance transforms)."""

    op: Any
    before: Any
    after: Any
    tools: list[Any] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)


def _frame_plane(f: Frame) -> Any:
    return bd.Plane(origin=f.origin, x_dir=f.x, z_dir=f.z)


def _matrix_trsf(m: Any) -> gp_Trsf:
    trsf = gp_Trsf()
    trsf.SetValues(m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3], m[2][0], m[2][1], m[2][2], m[2][3])
    return trsf


# --------------------------------------------------------------------------- #
# Small geometry helpers
# --------------------------------------------------------------------------- #


def _rotation_trsf(rotate: Vec3, anchor: Iterable[float]) -> gp_Trsf:
    """Rotation about world X, then Y, then Z axes through ``anchor``."""
    ax, ay, az = (float(c) for c in anchor)
    trsf = gp_Trsf()
    for axis_dir, angle in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), rotate):
        if abs(angle) > 1e-12:
            step = gp_Trsf()
            step.SetRotation(gp_Ax1(gp_Pnt(ax, ay, az), gp_Dir(*axis_dir)), math.radians(angle))
            trsf = step.Multiplied(trsf)  # previous first, then this step
    return trsf


def _translation_trsf(offset: Iterable[float]) -> gp_Trsf:
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(*(float(c) for c in offset)))
    return trsf


def _moved(shape: Any, trsf: gp_Trsf) -> Any:
    return shape.moved(bd.Location(trsf))


def _has_rotation(rotate: Iterable[float]) -> bool:
    return any(abs(a) > 1e-12 for a in rotate)


def _solids_of(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, bd.Solid):
        return [result]
    if isinstance(result, (list, tuple)):  # ShapeList
        out: list[Any] = []
        for item in result:
            out.extend(_solids_of(item))
        return out
    return list(result.solids())


def _as_part(result: Any) -> Any | None:
    """Normalise a boolean result to one Solid or a Compound of solids."""
    solids = [s for s in _solids_of(result) if s.volume > MIN_VOLUME]
    if not solids:
        return None
    if len(solids) == 1:
        return solids[0]
    return bd.Compound(solids)


def _clean(shape: Any, warnings: list[Issue], op_id: str | None) -> Any:
    try:
        cleaned = shape.clean()
        return cleaned if cleaned is not None else shape
    except Exception as exc:  # pragma: no cover - defensive
        warnings.append(Issue(op_id=op_id, code="clean_failed", message=f"face merge skipped: {exc}"))
        return shape


def _is_valid(shape: Any) -> bool:
    return bool(BRepCheck_Analyzer(shape.wrapped).IsValid())


def _fmt(x: float) -> str:
    return f"{x:g}"


def _fmt_vec(v: Iterable[float]) -> str:
    return "(" + ", ".join(_fmt(c) for c in v) + ")"


def _r6(x: float) -> float:
    return round(float(x), 6) + 0.0


# --------------------------------------------------------------------------- #
# Edge selection
# --------------------------------------------------------------------------- #


def _level_tol(shape: Any) -> float:
    return max(1e-4, 1e-7 * shape.bounding_box().diagonal)


def select_edges(shape: Any, selector: EdgeSelector) -> list[Any]:
    """Evaluate an :class:`EdgeSelector` on the current solid.

    - ``all``: every edge.
    - ``vertical``: straight edges parallel to Z.
    - ``top`` / ``bottom``: edges lying entirely in one horizontal plane, at
      the highest / lowest such level.
    - ``top_outer`` / ``bottom_outer``: the ``top`` / ``bottom`` edges that
      belong to the outer wire of an up- / down-facing planar face at that
      level (hole and pocket rims are excluded).
    - ``near``: keep the ``max_count`` (default 1) edges of the group closest
      to the point (ties broken by edge centre). Without ``near``,
      ``max_count`` is ignored.

    Seam edges (the parametric seam of a cylinder etc.) are never selected.
    """
    seams = seam_edge_set(shape)
    edges = [e for e in shape.edges() if e not in seams]
    group = selector.group
    if group == "all":
        sel = edges
    elif group == "vertical":
        sel = []
        for e in edges:
            if e.geom_type != bd.GeomType.LINE:
                continue
            d = e.end_point() - e.start_point()
            if d.length > 0 and abs(d.normalized().Z) > 1 - 1e-7:
                sel.append(e)
    else:
        top = group.startswith("top")
        tol = _level_tol(shape)
        flat = []
        for e in edges:
            bb = e.bounding_box()
            if bb.max.Z - bb.min.Z <= tol:
                flat.append((e, (bb.max.Z + bb.min.Z) / 2.0))
        if not flat:
            return []
        level = max(z for _, z in flat) if top else min(z for _, z in flat)
        sel = [e for e, z in flat if abs(z - level) <= tol]
        if group.endswith("_outer"):
            outer: set[Any] = set()
            for f in shape.faces():
                if f.geom_type != bd.GeomType.PLANE:
                    continue
                n = f.normal_at(f.center())
                if abs(abs(n.Z) - 1.0) > 1e-7 or (n.Z > 0) != top:
                    continue
                if abs(f.center().Z - level) > tol:
                    continue
                outer.update(f.outer_wire().edges())
            sel = [e for e in sel if e in outer]
    if selector.near is not None and sel:
        p = bd.Vector(*selector.near)

        def key(e: Any) -> tuple[float, float, float, float]:
            c = e.center()
            return (round(e.distance_to(p), 6), round(c.X, 6), round(c.Y, 6), round(c.Z, 6))

        sel = sorted(sel, key=key)[: selector.max_count or 1]
    return sel


# --------------------------------------------------------------------------- #
# Hole detection on imported solids (heuristic)
# --------------------------------------------------------------------------- #


class _Inside:
    """Reusable point-in-solid test (classifier set up once per shape)."""

    def __init__(self, shape: Any):
        self._cls = BRepClass3d_SolidClassifier(shape.wrapped)

    def __call__(self, p: Any) -> bool:
        self._cls.Perform(gp_Pnt(float(p.X), float(p.Y), float(p.Z)), 1e-6)
        return self._cls.State() == TopAbs_State.TopAbs_IN


def detect_holes(shape: Any) -> list[DetectedHole]:
    """Find hole-like bores on a solid (heuristic, for imported bases).

    A bore is a group of coaxial, equal-radius cylindrical faces whose normals
    point at the axis (material outside) and that together cover the full
    360°. An end is "closed" when material sits just beyond it (probe ring at
    0.9 r, 0.25 r past the end); open at both ends = through, closed at one =
    blind (axis points from the open end into the material), closed at both
    = unknown (``through=None``). Identical bores are grouped with a count.
    """
    inside = _Inside(shape)
    groups: dict[tuple, list[tuple[Any, float, Any, Any]]] = defaultdict(list)
    for face in shape.faces():
        if face.geom_type != bd.GeomType.CYLINDER:
            continue
        surf = BRepAdaptor_Surface(face.wrapped).Cylinder()
        ax = surf.Axis()
        r = float(surf.Radius())
        d = bd.Vector(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        o = bd.Vector(ax.Location().X(), ax.Location().Y(), ax.Location().Z())
        # canonical direction: dominant component positive
        comps = [d.X, d.Y, d.Z]
        dom = max(range(3), key=lambda i: abs(comps[i]))
        if comps[dom] < 0:
            d = -d
        # canonical origin: point on the axis closest to the world origin
        o = o - d * o.dot(d)
        pnt = face.position_at(0.5, 0.5)
        normal = face.normal_at(pnt)
        radial = pnt - (o + d * (pnt - o).dot(d))
        if radial.length < 1e-9 or normal.dot(radial) >= 0:
            continue  # outer cylinder (boss) — not a hole
        key = (
            tuple(round(c, 4) + 0.0 for c in (d.X, d.Y, d.Z)),
            tuple(round(c, 3) + 0.0 for c in (o.X, o.Y, o.Z)),
            round(r, 4),
        )
        groups[key].append((face, r, d, o))

    bores: list[dict[str, Any]] = []
    for (_, _, _), items in groups.items():
        r = items[0][1]
        d = items[0][2]
        o = items[0][3]
        ts = [(bd.Vector(v.X, v.Y, v.Z) - o).dot(d) for face, *_ in items for v in face.vertices()]
        if not ts:
            continue
        t0, t1 = min(ts), max(ts)
        length = t1 - t0
        if length <= 1e-6:
            continue
        coverage = sum(f.area for f, *_ in items) / (2 * math.pi * r * length)
        if coverage < 0.98:
            continue  # partial cylinder (fillet, slot end, …)
        u, v, _ = hole_frame((d.X, d.Y, d.Z))
        uu, vv = bd.Vector(*u), bd.Vector(*v)

        def closed(t_end: float, sign: float) -> bool:
            c = o + d * (t_end + sign * 0.25 * r)
            probes = [c] + [
                c + (uu * math.cos(a) + vv * math.sin(a)) * (0.9 * r)
                for a in (k * math.pi / 4 for k in range(8))
            ]
            return any(inside(p) for p in probes)

        lo_closed, hi_closed = closed(t0, -1.0), closed(t1, 1.0)
        if not lo_closed and not hi_closed:
            through: bool | None = True
            entry, axis = o + d * t1, -d
        elif lo_closed and not hi_closed:
            through, entry, axis = False, o + d * t1, -d
        elif hi_closed and not lo_closed:
            through, entry, axis = False, o + d * t0, d
        else:
            through, entry, axis = None, o + d * t1, -d
        bores.append(
            {
                "diameter": round(2 * r, 4),
                "length": round(length, 4),
                "through": through,
                "axis": [round(axis.X, 6) + 0.0, round(axis.Y, 6) + 0.0, round(axis.Z, 6) + 0.0],
                "entry": [round(entry.X, 4) + 0.0, round(entry.Y, 4) + 0.0, round(entry.Z, 4) + 0.0],
            }
        )

    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for b in bores:
        grouped[(b["diameter"], b["length"], b["through"], tuple(b["axis"]))].append(b)
    out = []
    for (dia, length, through, axis), items in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]), kv[0][3])):
        out.append(
            DetectedHole(
                diameter=dia,
                length=length,
                through=through,
                axis=list(axis),
                count=len(items),
                positions=sorted(i["entry"] for i in items),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# STEP import / export
# --------------------------------------------------------------------------- #


def import_step_solid(data: bytes) -> tuple[Any, int]:
    """Read STEP bytes into one solid (several solids are fused).

    Returns ``(shape, solid_count)``; raises :class:`KernelError`
    (``base_import_failed``) when no solid can be read.
    """
    if not data.lstrip()[:16].upper().startswith(b"ISO-10303-21"):
        raise KernelError("base_import_failed", "not a STEP file (missing ISO-10303-21 header)")
    reader = STEPControl_Reader()
    try:
        status = reader.ReadStream("base.step", io.BytesIO(data))
    except Exception as exc:
        raise KernelError("base_import_failed", f"cannot read STEP: {exc}") from exc
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise KernelError("base_import_failed", f"cannot read STEP (status {status.name})")
    reader.TransferRoots()
    raw = reader.OneShape()
    if raw is None or raw.IsNull():
        raise KernelError("base_import_failed", "STEP file contains no shapes")
    shape = bd.Compound.cast(raw)
    solids = _solids_of(shape)
    if not solids:
        raise KernelError("base_import_failed", "STEP file contains no solid bodies (surfaces/meshes cannot be a base)")
    if len(solids) == 1:
        return solids[0], 1
    fused = _as_part(solids[0].fuse(*solids[1:]))
    if fused is None:
        raise KernelError("base_import_failed", "fusing the STEP solids gave an empty result")
    return fused.clean(), len(solids)


def export_step_bytes(shape: Any, protocol: str = "AP203") -> bytes:
    """STEP bytes of one shape in ``protocol`` (AP203 by default, AP214 on request; generation reliability M): the
    shared XCAF writer of :mod:`app.cad.assembly_step` (fixed timestamp, product named after the shape type as
    build123d did). build123d's own ``export_step`` makes its header before the transfer and so always writes the
    AP214 FILE_SCHEMA."""
    from .assembly_step import write_part

    return write_part(shape.wrapped, type(shape).__name__.upper(), protocol=protocol)


def export_glb_bytes(shape: Any, tolerance: float, angular_tolerance: float) -> bytes:
    copy = shape.moved(bd.Location())  # export_gltf mutates the location of its argument
    with tempfile.TemporaryDirectory(prefix="partroute-glb-") as tmp:
        path = os.path.join(tmp, "model.glb")
        if not bd.export_gltf(copy, path, binary=True, linear_deflection=tolerance, angular_deflection=angular_tolerance):
            raise RuntimeError("glTF writer failed")
        with open(path, "rb") as fh:
            return fh.read()


def compute_metrics(shape: Any) -> Metrics:
    """Exact (B-rep) metrics of a solid. The box is the optimal one, tightened on trimmed free-form faces
    (:func:`app.cad.bounds.tight_bounds`: ground coil ends)."""
    lo, hi = tight_bounds(shape)
    com = shape.center(bd.CenterOf.MASS)
    box = bbox_dict(list(lo), list(hi))
    return Metrics(
        valid=_is_valid(shape),
        solids=len(shape.solids()),
        faces=len(shape.faces()),
        edges=len(shape.edges()),
        volume_mm3=_r6(precise_volume(shape)),
        area_mm2=_r6(shape.area),
        bbox=BBox(**box),
        center_of_mass=[_r6(com.X), _r6(com.Y), _r6(com.Z)],
    )


def export_artifacts(shape: Any, tolerance: float = 0.05, angular_tolerance: float = 0.2) -> BuildArtifacts:
    """STEP, GLB and mesh_json of one shape (mesh_json first, so the GLB
    reuses its triangulation)."""
    mesh = shape_mesh_json(shape, tolerance, angular_tolerance)
    glb = export_glb_bytes(shape, tolerance, angular_tolerance)
    step = export_step_bytes(shape)
    return BuildArtifacts(step=step, glb=glb, mesh_json=mesh)


# --------------------------------------------------------------------------- #
# Op summaries
# --------------------------------------------------------------------------- #


def _profile_text(p: Any) -> str:
    if p.kind == "rect":
        return f"rect {_fmt(p.width)}×{_fmt(p.length)}" + (f" r{_fmt(p.corner_radius)}" if p.corner_radius else "")
    if p.kind == "circle":
        return f"circle r{_fmt(p.radius)}"
    if p.kind == "slot":
        return f"slot {_fmt(p.length)}×{_fmt(p.width)}"
    if p.kind == "ring":
        return f"ring r{_fmt(p.outer_radius)}/{_fmt(p.inner_radius)}"
    if p.kind == "sketch":
        arcs = sum(1 for s in p.segments if s.kind == "arc")
        return f"sketch {len(p.segments)} seg ({arcs} arc)" + (f" − {len(p.holes)} loop(s)" if p.holes else "")
    return f"polygon {len(p.points)} pts"


def _plane_text(plane: Any) -> str:
    if isinstance(plane, str):
        return plane
    if hasattr(plane, "ref"):
        return f"datum {plane.ref}" + (" (flipped)" if plane.flip else "") + (f" {'+' if plane.offset >= 0 else ''}{_fmt(plane.offset)}" if plane.offset else "")
    return f"plane {_fmt_vec(plane.origin)} n{_fmt_vec(plane.normal)}"


def _op_summary(op: Operation) -> str:
    if isinstance(op, BoxOp):
        s = f"box {_fmt(op.size[0])}×{_fmt(op.size[1])}×{_fmt(op.size[2])} at {_fmt_vec(op.at)}"
    elif isinstance(op, CylinderOp):
        s = f"cylinder r{_fmt(op.radius)} h{_fmt(op.height)} along {op.axis} at {_fmt_vec(op.at)}"
    elif isinstance(op, ExtrudeOp):
        s = (f"extrude {_profile_text(op.profile)} on {_plane_text(op.plane)}{'+' if op.offset >= 0 else ''}{_fmt(op.offset)} "
             f"h{_fmt(op.height)}")
    elif isinstance(op, RevolveOp):
        s = f"revolve {_profile_text(op.profile)} about {_fmt_vec(op.axis)} at {_fmt_vec(op.at)}" + (
            f" {_fmt(op.angle)}°" if op.angle < 360 else "")
    elif isinstance(op, SweepOp):
        s = f"sweep {_profile_text(op.profile)} along {len(op.path.segments)}-segment path on {_plane_text(op.plane)}"
    elif isinstance(op, CoilOp):
        law = op.law()
        sec = f"Ø{_fmt(2 * op.section.radius)}" if op.section.kind == "circle" else f"{_fmt(op.section.width)}×{_fmt(op.section.length)}"
        s = (f"coil {sec} Dm{_fmt(op.mean_diameter)} n{_fmt(op.turns)} p{law.pitch:.4g} L{law.length:.4g} "
             f"{op.handedness} {op.end_form}")
    elif isinstance(op, BentPlateOp):
        s = (f"bent plate t{_fmt(op.thickness)} w{_fmt(op.width)} legs {'/'.join(_fmt(x) for x in op.legs)} "
             f"bends {'/'.join(_fmt(x) for x in op.bends)}° ri{_fmt(op.inner_radius)}")
    elif isinstance(op, CutOp):
        return (f"cut {_profile_text(op.profile)} from {_plane_text(op.plane)}"
                + (f"{'+' if op.offset >= 0 else ''}{_fmt(op.offset)}" if op.offset else "")
                + (" THRU" if op.depth is None else f" ↧{_fmt(op.depth)}"))
    elif isinstance(op, ShellOp):
        return f"shell t{_fmt(op.thickness)}" + (f", open {', '.join(_fmt_vec(d) for d in op.open_faces)}" if op.open_faces else ", closed")
    elif isinstance(op, TextOp):
        shown = op.text if len(op.text) <= 24 else op.text[:23] + "…"
        return (f"text \"{shown}\" h{_fmt(op.height)} {'raised' if op.depth > 0 else 'engraved'} {_fmt(abs(op.depth))} on "
                f"{_plane_text(op.plane)}" + (f" font {op.font}" if op.font else ""))
    elif isinstance(op, PatternOp):
        lay = op.layout
        if lay.kind == "linear":
            n = lay.count * lay.count2
            return f"pattern {op.target} ×{n} linear {_fmt(lay.spacing)} along {_fmt_vec(lay.direction)}"
        return f"pattern {op.target} ×{lay.count} circular {_fmt(lay.step)}° about {_fmt_vec(lay.axis)}"
    elif isinstance(op, HoleOp):
        return hole_callout_text(op.diameter, op.depth, len(hole_positions(op)), _cb(op), _cs(op))
    elif isinstance(op, FilletOp):
        return f"fillet r{_fmt(op.radius)} on {op.edges.group} edges" + (" (near point)" if op.edges.near else "")
    elif isinstance(op, ChamferOp):
        return f"chamfer {_fmt(op.distance)} on {op.edges.group} edges" + (" (near point)" if op.edges.near else "")
    elif isinstance(op, TransformOp):
        return f"move {op.target} by {_fmt_vec(op.translate)} rotate {_fmt_vec(op.rotate)}"
    else:  # pragma: no cover
        return op.type
    if _has_rotation(op.rotate):
        s += f" rot {_fmt_vec(op.rotate)}"
    return f"{s}, {op.mode}"


def _cb(op: HoleOp) -> dict[str, float] | None:
    return {"diameter": op.counterbore.diameter, "depth": op.counterbore.depth} if op.counterbore else None


def _cs(op: HoleOp) -> dict[str, float] | None:
    return {"diameter": op.countersink.diameter, "angle": op.countersink.angle} if op.countersink else None


def hole_callout_text(
    diameter: float,
    depth: float | None,
    count: int = 1,
    counterbore: dict[str, float] | None = None,
    countersink: dict[str, float] | None = None,
) -> str:
    """Drawing-style hole callout, e.g. ``4× Ø8 THRU`` or ``Ø6 ↧10 ⌴Ø11 ↧4``."""
    s = (f"{count}× " if count > 1 else "") + f"Ø{_fmt(diameter)} " + ("THRU" if depth is None else f"↧{_fmt(depth)}")
    if counterbore:
        s += f" ⌴Ø{_fmt(counterbore['diameter'])} ↧{_fmt(counterbore['depth'])}"
    if countersink:
        s += f" ⌵Ø{_fmt(countersink['diameter'])}×{_fmt(countersink.get('angle', 90.0))}°"
    return s


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


class _Replay:
    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.part: Any | None = None
        self.errors: list[Issue] = []
        self.warnings: list[Issue] = []
        self.features: list[FeatureRecord] = []
        self.holes: list[HoleRecord] = []
        self.detected: list[DetectedHole] = []
        self.ops_by_id = {op.id: op for op in spec.ops}
        self.transforms: dict[str, list[TransformOp]] = defaultdict(list)
        for op in spec.ops:
            if isinstance(op, TransformOp) and not op.suppressed:
                target = self.ops_by_id.get(op.target)
                if target is not None and target.type in BODY_TYPES:
                    self.transforms[op.target].append(op)
        self.trace: list[OpTrace] = []
        self._tools: list[Any] = []
        self._info: dict[str, Any] = {}
        self._current: str | None = None

    # -- base ------------------------------------------------------------- #

    def load_base(self, data: bytes) -> None:
        shape, n = import_step_solid(data)
        if not _is_valid(shape):
            fixed = shape.fix()
            if _is_valid(fixed):
                self.warnings.append(Issue(op_id="F-00", code="base_fixed", message="imported solid was repaired by ShapeFix"))
                shape = fixed
            else:
                self.warnings.append(Issue(op_id="F-00", code="base_invalid", message="imported solid fails the validity check"))
        self.part = shape
        try:
            self.detected = detect_holes(shape)
        except Exception as exc:  # pragma: no cover - heuristic must never break a build
            self.warnings.append(Issue(op_id="F-00", code="hole_detection_failed", message=str(exc)))
        self.features.append(
            FeatureRecord(
                op_id="F-00",
                type="base_step",
                status="ok",
                summary=f"imported STEP base ({n} solid{'s' if n != 1 else ''}{', fused' if n > 1 else ''})",
                detail={"solids": n, "detected_holes": sum(h.count for h in self.detected)},
            )
        )

    # -- body ops --------------------------------------------------------- #

    # -- W3: frames, datums and sketch faces -------------------------------- #

    def _known_datums(self) -> dict[str, Any]:
        return dt.op_datums(self.spec, upto=self._current)

    def _frame(self, plane: Any, offset: float = 0.0, at: Iterable[float] = (0.0, 0.0), rotate_profile: float = 0.0) -> Frame:
        known = self._known_datums() if hasattr(plane, "ref") else None
        try:
            return dt.placed_frame(plane, offset, tuple(at), rotate_profile, known)
        except dt.DatumError as exc:
            raise KernelError("datum_missing", str(exc), details={"ref": exc.ref, "available": exc.available[:40]}) from exc
        except ValueError as exc:
            raise KernelError("invalid_workplane", f"the plane cannot be resolved: {exc}") from exc

    @staticmethod
    def _wire(segs: list[Seg], f: Frame) -> Any:
        edges = []
        for s in segs:
            if s.kind == "line":
                edges.append(bd.Edge.make_line(f.point(*s.p0), f.point(*s.p1)))
            elif abs(s.sweep) >= 2 * math.pi - 1e-9:
                assert s.center is not None
                edges.append(bd.Edge.make_circle(s.radius, bd.Plane(origin=f.point(*s.center), x_dir=f.x, z_dir=f.z)))
            else:
                edges.append(bd.Edge.make_three_point_arc(f.point(*s.p0), f.point(*s.point(0.5)), f.point(*s.p1)))
        return bd.Wire(edges)

    def _loops_face(self, outer: list[Seg], holes: list[list[Seg]], f: Frame, what: str) -> Any:
        try:
            face = bd.Face(self._wire(outer, f))
            for h in holes:
                cut = face.cut(bd.Face(self._wire(h, f)))
                faces = list(cut.faces()) if not isinstance(cut, bd.Face) else [cut]
                if len(faces) != 1:
                    raise KernelError("invalid_profile", f"{what}: a hole loop splits the outline into {len(faces)} regions")
                face = faces[0]
        except KernelError:
            raise
        except Exception as exc:
            raise KernelError("invalid_profile", f"{what} does not form one valid region: {exc}") from exc
        if not face.is_valid or face.area <= MIN_VOLUME:
            raise KernelError("invalid_profile", f"{what} does not form one valid region (self-intersecting?)")
        return face

    def _sketch_face(self, p: Any, f: Frame) -> Any:
        outer, holes = profile_loops(p)
        return self._loops_face(outer, holes, f, f"{p.kind} profile")

    def _named_or_sketch_face(self, profile: Any, f: Frame) -> Any:
        """A profile face on an arbitrary frame (basic kinds via build123d sketches like the named-plane path)."""
        if profile.kind == "sketch":
            return self._sketch_face(profile, f)
        return self._profile_face_kind(profile, _frame_plane(f))

    def _profile_face(self, op: ExtrudeOp, plane: Any) -> Any:
        if op.profile.kind == "sketch":
            f = Frame(tuple(plane.origin), tuple(plane.x_dir), tuple(plane.y_dir), tuple(plane.z_dir))  # type: ignore[arg-type]
            return self._sketch_face(op.profile, f)
        return self._profile_face_kind(op.profile, plane)

    def _profile_face_kind(self, p: Any, plane: Any) -> Any:
        if p.kind == "rect":
            sk = bd.RectangleRounded(p.width, p.length, p.corner_radius) if p.corner_radius > 0 else bd.Rectangle(p.width, p.length)
        elif p.kind == "circle":
            sk = bd.Circle(p.radius)
        elif p.kind == "slot":
            sk = bd.SlotOverall(p.length, p.width)
        elif p.kind == "ring":
            sk = bd.Circle(p.outer_radius) - bd.Circle(p.inner_radius)
        else:
            sk = bd.Polygon(*[(float(u), float(v)) for u, v in p.points])
        faces = list(sk.moved(plane.location).faces())
        if len(faces) != 1 or not faces[0].is_valid or faces[0].area <= MIN_VOLUME:
            raise KernelError("invalid_profile", f"{p.kind} profile does not form one valid region (self-intersecting?)")
        return faces[0]

    def _w3_body_tool(self, op: Any) -> tuple[Any, bd.Vector]:
        """Unplaced tool and anchor of an extrude on a Workplane / DatumRef and of revolve / sweep / bent plate."""
        if isinstance(op, ExtrudeOp):
            f = self._frame(op.plane, op.offset, op.at, op.rotate_profile)
            face = self._named_or_sketch_face(op.profile, f)
            self._info["frame"] = f
            return bd.Solid.extrude(face, bd.Vector(*f.z) * op.height), bd.Vector(*f.origin)
        if isinstance(op, RevolveOp):
            f = op.frame()
            face = self._sketch_face(op.profile, f)
            try:
                tool = bd.Solid.revolve(face, op.angle, bd.Axis(f.origin, f.x))
            except Exception as exc:
                raise KernelError("revolve_failed", f"revolve failed: {exc}") from exc
            self._info["frame"] = f
            return tool, bd.Vector(*op.at)
        if isinstance(op, SweepOp):
            try:
                pf, section, segs = dt.sweep_frames(op, self._known_datums() if hasattr(op.plane, "ref") else None)
            except dt.DatumError as exc:
                raise KernelError("datum_missing", str(exc), details={"ref": exc.ref, "available": exc.available[:40]}) from exc
            face = self._named_or_sketch_face(op.profile, section)
            try:
                tool = bd.Solid.sweep(face, self._wire(segs, pf), transition=bd.Transition.RIGHT)
            except Exception as exc:
                raise KernelError("sweep_failed", f"sweep failed: {exc}") from exc
            if tool is None or tool.volume <= MIN_VOLUME or not _is_valid(tool):
                raise KernelError("sweep_failed", "the sweep does not give a valid solid (does the section fold on a tight arc?)")
            self._info.update(frame=pf, section=section)
            return tool, bd.Vector(*section.origin)
        if isinstance(op, CoilOp):
            tool = self._coil_tool(op)
            self._info["frame"] = op.frame()
            return tool, bd.Vector(*op.at)
        if isinstance(op, BentPlateOp):
            f = self._frame(op.plane, op.offset, op.at, 0.0)
            lay = bent_plate_layout(op.thickness, op.legs, op.bends, op.inner_radius, op.leg_length)
            outer = resolve_loop(lay.start, lay.segments, what="bent plate outline")
            face = self._loops_face(outer, [], f, "bent plate outline")
            self._info["frame"] = f
            return bd.Solid.extrude(face, bd.Vector(*f.z) * op.width), bd.Vector(*f.origin)
        raise KernelError("internal_error", f"no tool for {op.type}")  # pragma: no cover

    def _coil_tool(self, op: CoilOp) -> Any:
        """The coil solid in world coordinates (before ``rotate`` / transforms): :func:`app.cad.coil_occ.coil_solid`."""
        try:
            tool = bd.Solid(TopoDS.Solid(coil_solid(op.law(), op.frame(), op.section.kind)))
        except CoilBuildError as exc:
            raise KernelError("coil_failed", str(exc)) from exc
        except Exception as exc:
            raise KernelError("coil_failed", f"coil sweep failed: {exc}") from exc
        if tool.volume <= MIN_VOLUME or not _is_valid(tool):
            raise KernelError("coil_failed", "the coil does not give a valid solid (section too large for the coil?)")
        return tool

    def _body_tool(self, op: Any) -> tuple[Any, bd.Vector]:
        if isinstance(op, BoxOp):
            sx, sy, sz = op.size
            ax, ay, az = op.at
            tool = bd.Solid.make_box(sx, sy, sz, bd.Plane(origin=(ax - sx / 2, ay - sy / 2, az)))
            anchor = bd.Vector(*op.at)
        elif isinstance(op, CylinderOp):
            z_dir = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[op.axis]
            tool = bd.Solid.make_cylinder(op.radius, op.height, bd.Plane(origin=op.at, z_dir=z_dir))
            anchor = bd.Vector(*op.at)
        elif not isinstance(op, ExtrudeOp) or not isinstance(op.plane, str):
            tool, anchor = self._w3_body_tool(op)
        else:
            base = {"XY": bd.Plane.XY, "XZ": bd.Plane.XZ, "YZ": bd.Plane.YZ}[op.plane]
            u, v = op.at
            origin = base.origin + base.z_dir * op.offset + base.x_dir * u + base.y_dir * v
            a = math.radians(op.rotate_profile)
            x_dir = base.x_dir * math.cos(a) + base.y_dir * math.sin(a)
            plane = bd.Plane(origin=origin, x_dir=x_dir, z_dir=base.z_dir)
            face = self._profile_face(op, plane)
            tool = bd.Solid.extrude(face, plane.z_dir * op.height)
            anchor = origin
        if _has_rotation(op.rotate):
            tool = _moved(tool, _rotation_trsf(op.rotate, anchor))
        for t in self.transforms.get(op.id, []):
            if _has_rotation(t.rotate):
                tool = _moved(tool, _rotation_trsf(t.rotate, anchor))
            if any(abs(c) > 1e-12 for c in t.translate):
                tool = _moved(tool, _translation_trsf(t.translate))
                anchor = anchor + bd.Vector(*t.translate)
        return tool, anchor

    def body(self, op: Any) -> str:
        tool, _ = self._body_tool(op)
        self._tools = [tool]
        if op.mode == "new":
            if self.part is not None:
                raise KernelError("invalid_mode", "mode 'new' is only allowed for the first body without a base")
            result = tool
        else:
            if self.part is None:
                if op.mode == "add":
                    result = tool
                else:
                    raise KernelError("no_body", f"nothing to {op.mode}: there is no body yet")
            else:
                try:
                    if op.mode == "add":
                        raw = self.part.fuse(tool)
                    elif op.mode == "subtract":
                        raw = self.part.cut(tool)
                    else:
                        raw = self.part.intersect(tool)
                except Exception as exc:
                    raise KernelError("boolean_failed", f"{op.mode} failed: {exc}") from exc
                result = _as_part(raw)
                if result is None:
                    raise KernelError("empty_result", f"{op.mode} leaves no material")
                result = _clean(result, self.warnings, op.id)
        self._commit(op.id, result)
        return _op_summary(op)

    # -- holes ------------------------------------------------------------ #

    def hole(self, op: HoleOp) -> str:
        if self.part is None:
            raise KernelError("no_body", "a hole needs an existing body")
        if op.countersink and not (0.0 < op.countersink.angle < 180.0):
            raise KernelError("invalid_hole", "countersink angle must be between 0 and 180 degrees")
        _, _, d_t = hole_world_frame(op)
        d = bd.Vector(*d_t)
        bb = self.part.bounding_box()
        corners = [bd.Vector(x, y, z) for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y) for z in (bb.min.Z, bb.max.Z)]
        positions = hole_positions(op)
        inside = _Inside(self.part)  # fast hit test: an axis point inside material
        tools: list[Any] = []
        missed: list[int] = []
        for index, pos in enumerate(positions):
            p = bd.Vector(*pos)
            if op.depth is None:
                length = max((c - p).dot(d) for c in corners) + THROUGH_MARGIN
                if length <= THROUGH_MARGIN:
                    missed.append(index)  # this instance points away from the part
                    continue
            else:
                length = op.depth
            pieces = self._hole_pieces(op, p, d, length, self._mouth_backoff(op, p, d))
            probes = [p + d * (length * (k + 0.5) / 8.0) for k in range(8)]
            if any(inside(q) for q in probes) or self._removes_material(pieces):
                tools.extend(pieces)
            else:
                missed.append(index)
        if missed:
            raise self._missed_error(op, positions, missed)
        before = self.part.volume
        try:
            raw = self.part.cut(*tools)
        except Exception as exc:
            raise KernelError("hole_failed", f"hole cut failed: {exc}") from exc
        result = _as_part(raw)
        if result is None:
            raise KernelError("empty_result", "the hole removes all material")
        if result.volume >= before - 1e-7:  # pragma: no cover - instances were checked above
            raise KernelError("hole_missed", "the hole removes no material; check 'at' and 'direction'")
        result = _clean(result, self.warnings, op.id)
        self._commit(op.id, result)
        self._tools = tools
        self.holes.append(
            HoleRecord(
                op_id=op.id,
                diameter=op.diameter,
                depth=op.depth,
                through=op.depth is None,
                axis=[_r6(c) for c in d_t],
                entry=[float(c) for c in hole_entry(op)],
                count=len(positions),
                counterbore=_cb(op),
                countersink=_cs(op),
                positions=[list(p) for p in positions],
            )
        )
        return _op_summary(op)

    @staticmethod
    def _hole_pieces(op: HoleOp, p: Any, d: Any, length: float, backoff: float = HOLE_BACKOFF) -> list[Any]:
        """Tool pieces of one hole instance at entry ``p`` along ``d`` (bore, counterbore / countersink); the tool
        starts ``backoff`` before the entry point."""
        r = op.diameter / 2.0
        start = p - d * backoff
        start_plane = bd.Plane(origin=start, z_dir=d)
        pieces = [bd.Solid.make_cylinder(r, length + backoff, start_plane)]
        if op.counterbore:
            pieces.append(bd.Solid.make_cylinder(op.counterbore.diameter / 2.0, op.counterbore.depth + backoff, start_plane))
        if op.countersink:
            big = op.countersink.diameter / 2.0
            h = (big - r) / math.tan(math.radians(op.countersink.angle / 2.0))
            pieces.append(bd.Solid.make_cone(big, r, h, bd.Plane(origin=p, z_dir=d)))
            pieces.append(bd.Solid.make_cylinder(big, backoff, start_plane))
        return pieces

    def _mouth_backoff(self, op: HoleOp, p: Any, d: Any) -> float:
        """How far before the entry point the tool starts. Legacy holes: 0.01 mm. A ``plane`` hole or ``flush_entry``:
        also the height the inclined entry face rises over the widest radius (r · tan θ, θ = angle between the axis
        and the face normal), so no lip of material is left in the mouth."""
        if op.plane is None and not op.flush_entry:
            return HOLE_BACKOFF
        if op.plane is not None:
            n = bd.Vector(*op.plane.frame().z)
        else:
            face = min(self.part.faces(), key=lambda fc: fc.distance_to(p))
            n = face.normal_at(p)
        c = abs(n.normalized().dot(d))
        if c < 0.05:
            raise KernelError("invalid_hole", "the hole axis is almost parallel to its entry face (more than 87° off the face "
                                              "normal); check 'at' and 'direction'")
        r_max = max(op.diameter, op.counterbore.diameter if op.counterbore else 0.0,
                    op.countersink.diameter if op.countersink else 0.0) / 2.0
        return HOLE_BACKOFF + r_max * math.sqrt(max(0.0, 1.0 - c * c)) / c

    # -- W3: cut, shell, pattern ------------------------------------------------ #

    def _through_length(self, entry: Any, d: Any, parts: Any = None) -> float:
        """Tool length from ``entry`` along ``d`` past every corner of the current solid's box (0 margin = misses)."""
        bb = (parts or self.part).bounding_box()
        corners = [bd.Vector(x, y, z) for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y) for z in (bb.min.Z, bb.max.Z)]
        return max((c - entry).dot(d) for c in corners) + THROUGH_MARGIN

    def _cut_tool(self, op: CutOp, f: Frame, length: float) -> Any:
        start = f.moved(w=HOLE_BACKOFF)
        face = self._named_or_sketch_face(op.profile, start)
        return bd.Solid.extrude(face, bd.Vector(*f.z) * -(length + HOLE_BACKOFF))

    def cut(self, op: CutOp) -> str:
        if self.part is None:
            raise KernelError("no_body", "a cut needs an existing body")
        f = self._frame(op.plane, op.offset, op.at, op.rotate_profile)
        inward = bd.Vector(*f.z) * -1.0
        if op.depth is None:
            length = self._through_length(bd.Vector(*f.origin), inward)
            if length <= THROUGH_MARGIN:
                raise KernelError("cut_missed", f"the through cut from {_fmt_vec(f.origin)} points away from the part (the "
                                                "cut goes against the plane normal; flip the plane or check its offset)")
        else:
            length = op.depth
        tool = self._cut_tool(op, f, length)
        if not self._removes_material([tool]):
            raise KernelError("cut_missed", f"the cut at {_fmt_vec(f.origin)} removes no material; it enters at the plane and "
                                            "goes against its normal — check plane, offset and at")
        try:
            raw = self.part.cut(tool)
        except Exception as exc:
            raise KernelError("cut_failed", f"cut failed: {exc}") from exc
        result = _as_part(raw)
        if result is None:
            raise KernelError("empty_result", "the cut removes all material")
        result = _clean(result, self.warnings, op.id)
        self._commit(op.id, result)
        self._tools = [tool]
        self._info.update(frame=f, length=length)
        return _op_summary(op)

    def shell(self, op: ShellOp) -> str:
        if self.part is None:
            raise KernelError("no_body", "a shell needs an existing body")
        solids = list(self.part.solids())
        if len(solids) != 1:
            raise KernelError("shell_failed", f"a shell needs one solid; the part has {len(solids)}")
        solid = solids[0]
        tol = _level_tol(solid)
        openings: list[Any] = []
        for i, raw_d in enumerate(op.open_faces):
            n = math.sqrt(sum(c * c for c in raw_d))
            du = bd.Vector(*(c / n for c in raw_d))
            cands = [fc for fc in solid.faces() if fc.geom_type == bd.GeomType.PLANE
                     and fc.normal_at(fc.center()).dot(du) >= 1 - 1e-6]
            if not cands:
                raise KernelError("no_faces_selected", f"open_faces[{i}] {_fmt_vec(raw_d)}: no planar face of the solid faces "
                                                       "that way")
            level = max(fc.center().dot(du) for fc in cands)
            openings += [fc for fc in cands if abs(fc.center().dot(du) - level) <= tol]
        before = solid.volume
        try:
            raw = solid.offset_3d(openings, -op.thickness, kind=bd.Kind.INTERSECTION)
        except Exception as exc:
            raise KernelError("shell_failed", f"shell of {_fmt(op.thickness)} mm failed; try a thinner wall, fewer open faces or "
                                              f"move fillets after the shell ({exc})") from exc
        result = _as_part(raw)
        if result is None or result.volume >= before - 1e-7 or not _is_valid(result):
            raise KernelError("shell_failed", f"shell of {_fmt(op.thickness)} mm gave no valid hollow solid (wall thicker than "
                                              "the part?)")
        self._commit(op.id, result)
        self._info.update(open_faces=openings)
        return _op_summary(op)

    def text(self, op: TextOp) -> tuple[str, dict[str, Any]]:
        """Raised / engraved lettering (see the module doc); returns the summary and the feature detail (font used,
        glyph faces, text block size, glyph area)."""
        from . import text_geometry as tg

        if self.part is None:
            raise KernelError("no_body", "text needs an existing body: raised letters stand on a face, engraved ones cut into it")
        try:
            choice = tg.resolve_font(op.text, op.font)
        except tg.FontUnavailable as exc:
            raise KernelError("unsupported_font", exc.message, details=exc.details) from exc
        f = self._frame(op.plane, op.offset, op.at, op.rotation)
        try:
            glyphs = tg.text_faces(op.text, choice, op.height)
        except Exception as exc:
            raise KernelError("text_failed", f"the text outlines could not be made with {choice.face.family}: {exc}") from exc
        placed_at = f.moved(w=HOLE_BACKOFF) if op.depth < 0 else f
        loc = _frame_plane(placed_at).location
        faces = [g.moved(loc) for g in glyphs]
        area = sum(g.area for g in glyphs)
        depth = abs(op.depth)
        length = depth + (HOLE_BACKOFF if op.depth < 0 else 0.0)
        direction = bd.Vector(*f.z) * (length if op.depth > 0 else -length)
        try:
            tools = [bd.Solid.extrude(face, direction) for face in faces]
        except Exception as exc:
            raise KernelError("text_failed", f"the text could not be extruded: {exc}") from exc
        before_v, before_n = self.part.volume, len(self.part.solids())
        try:
            raw = self.part.fuse(*tools) if op.depth > 0 else self.part.cut(*tools)
        except Exception as exc:
            raise KernelError("text_failed", f"the text boolean failed: {exc}") from exc
        result = _as_part(raw)
        if result is None:
            raise KernelError("empty_result", "the engraved text removes all material")
        expected = area * depth
        changed = result.volume - before_v if op.depth > 0 else before_v - result.volume
        where = f"{_plane_text(op.plane)} at {_fmt_vec(f.origin)}"
        if op.depth > 0 and len(result.solids()) > before_n:
            raise KernelError("text_missed", f"raised text on {where}: some letters do not touch the part (they would float as "
                                             "separate solids); put the plane on the face the letters stand on")
        if changed < 0.99 * expected - 1e-6:
            raise KernelError("text_missed", f"{'raised' if op.depth > 0 else 'engraved'} text on {where} changes {changed:.4g} mm³ "
                                             f"of the {expected:.4g} mm³ its letters need: "
                              + ("the letters overlap existing material (raised letters start on the face and grow out of it)"
                                 if op.depth > 0 else "some letters lie outside the material (the cut starts on the face and goes "
                                                      "into it)"), details={"expected_mm3": round(expected, 6), "changed_mm3": round(changed, 6)})
        result = _clean(result, self.warnings, op.id)
        self._commit(op.id, result)
        self._tools = tools
        self._info.update(frame=f, font=choice)
        font = choice.brief()
        if choice.substituted:
            reason = ("is not installed" if choice.reason == "not_installed"
                      else f"lacks {''.join(choice.missing[:12])}" if choice.missing else "cannot draw the text")
            self.warnings.append(Issue(op_id=op.id, code="font_substituted",
                                       message=f"font {choice.requested} {reason}; the text was built in {choice.face.family}",
                                       details={**font, "text": op.text, "impact": tg.SUBSTITUTION_IMPACT}))
        bb = bd.Compound(glyphs).bounding_box(optimal=False)
        detail = {"text": op.text, "height": op.height, "depth": op.depth, "font": font, "glyph_faces": len(glyphs),
                  "block": [_r6(bb.max.X - bb.min.X), _r6(bb.max.Y - bb.min.Y)], "glyph_area_mm2": _r6(area),
                  # the placed frame (block centre, reading direction, normal): drawings leave the letters out of the
                  # feature analysis (app.cad.text_geometry.text_regions)
                  "frame": {"origin": [_r6(c) for c in f.origin], "x": [_r6(c) for c in f.x], "z": [_r6(c) for c in f.z]}}
        return _op_summary(op), detail

    def pattern(self, op: PatternOp) -> str | None:
        target = self.ops_by_id.get(op.target)
        if target is None or target.type not in PATTERN_TARGET_TYPES:
            raise KernelError("invalid_pattern_target", f"pattern target {op.target} must be an earlier body, hole or cut op")
        if target.suppressed:
            return None
        if self.part is None:
            raise KernelError("no_body", "a pattern needs an existing body")
        mats = dt.pattern_matrices(op.layout)
        trsfs = [_matrix_trsf(m) for m in mats]
        if target.type in BODY_TYPES:
            if target.mode == "intersect":
                raise KernelError("invalid_pattern_target", f"{target.id} intersects; an intersecting body cannot be patterned")
            mode = "subtract" if target.mode == "subtract" else "add"
            tool, _ = self._body_tool(target)
            copies = [[_moved(tool, t)] for t in trsfs]
            base_tools = [tool]
        elif isinstance(target, HoleOp):
            mode = "subtract"
            _, _, d_t = hole_world_frame(target)
            positions = hole_positions(target)
            lengths = []
            for m in mats:
                dm = bd.Vector(*dt.apply_dir(m, d_t))
                for pos in positions:
                    lengths.append(self._through_length(bd.Vector(*dt.apply(m, pos)), dm) if target.depth is None else target.depth)
            length = max(lengths)
            d = bd.Vector(*d_t)
            base_tools = [pc for pos in positions
                          for pc in self._hole_pieces(target, bd.Vector(*pos), d, length,
                                                      self._mouth_backoff(target, bd.Vector(*pos), d))]
            copies = [[_moved(pc, t) for pc in base_tools] for t in trsfs]
        else:
            assert isinstance(target, CutOp)
            mode = "subtract"
            f = self._frame(target.plane, target.offset, target.at, target.rotate_profile)
            inward = tuple(-c for c in f.z)
            if target.depth is None:
                length = max(self._through_length(bd.Vector(*dt.apply(m, f.origin)), bd.Vector(*dt.apply_dir(m, inward)))
                             for m in mats)
            else:
                length = target.depth
            tool = self._cut_tool(target, f, length)
            base_tools = [tool]
            copies = [[_moved(tool, t)] for t in trsfs]
        missed = []
        for k, pieces in enumerate(copies):
            if mode == "subtract":
                hit = self._removes_material(pieces)
            else:
                hit = any(self._adds_material(pc) for pc in pieces)
            if not hit:
                missed.append(k + 1)
        if missed:
            raise KernelError("pattern_instance_missed",
                              f"{len(missed)} of {len(copies)} pattern copies change nothing (instances "
                              f"{', '.join(str(i) for i in missed[:12])}{', …' if len(missed) > 12 else ''}; instance 0 is "
                              f"{op.target} itself): check direction, spacing / axis and count",
                              details={"missed_instances": missed, "instances": len(copies) + 1})
        flat = [pc for pieces in copies for pc in pieces]
        try:
            raw = self.part.cut(*flat) if mode == "subtract" else self.part.fuse(*flat)
        except Exception as exc:
            raise KernelError("pattern_failed", f"pattern boolean failed: {exc}") from exc
        result = _as_part(raw)
        if result is None:
            raise KernelError("empty_result", "the pattern leaves no material")
        result = _clean(result, self.warnings, op.id)
        self._commit(op.id, result)
        self._tools = flat
        self._info.update(matrices=mats, mode=mode, base_tools=base_tools, target=target)
        if isinstance(target, HoleOp):  # one record per drilling direction (a radial pattern turns the axis)
            _, _, d_t = hole_world_frame(target)
            groups: dict[tuple[float, ...], list[list[float]]] = {}
            for m in mats:
                axis = tuple(_r6(c) for c in dt.apply_dir(m, d_t))
                groups.setdefault(axis, []).extend([_r6(c) for c in dt.apply(m, p)] for p in hole_positions(target))
            for axis, pts in groups.items():
                self.holes.append(HoleRecord(
                    op_id=op.id, diameter=target.diameter, depth=target.depth, through=target.depth is None, axis=list(axis),
                    entry=pts[0], count=len(pts), counterbore=_cb(target), countersink=_cs(target), positions=pts))
        return _op_summary(op)

    def _adds_material(self, piece: Any) -> bool:
        """A body copy adds material: part of it lies outside the current solid."""
        common = _as_part(self.part.intersect(piece))
        inside_v = common.volume if common is not None else 0.0
        return piece.volume - inside_v > MISS_VOLUME

    def _removes_material(self, pieces: list[Any]) -> bool:
        """Exact check: any tool piece overlaps the current solid with volume."""
        pb_all = self.part.bounding_box()
        for piece in pieces:
            pb = piece.bounding_box()
            if (pb.min.X > pb_all.max.X or pb.max.X < pb_all.min.X or pb.min.Y > pb_all.max.Y
                    or pb.max.Y < pb_all.min.Y or pb.min.Z > pb_all.max.Z or pb.max.Z < pb_all.min.Z):
                continue
            common = _as_part(self.part.intersect(piece))
            if common is not None and common.volume > MISS_VOLUME:
                return True
        return False

    @staticmethod
    def _missed_error(op: HoleOp, positions: list[Vec3], missed: list[int]) -> KernelError:
        n = len(positions)
        listed = ", ".join(f"#{i} {_fmt_vec(positions[i])}" for i in missed[:8]) + (", …" if len(missed) > 8 else "")
        if n == 1:
            msg = f"the hole at {_fmt_vec(positions[0])} removes no material; check 'at' (entry point on the surface) and 'direction'"
        else:
            msg = f"{len(missed)} of {n} hole instances remove no material: {listed}"
            if op.pattern is not None and op.pattern.kind == "linear":
                msg += ("; linear pattern: 'at' is the FIRST instance (index 0) and instances step by +pitch along "
                        "the in-plane axes — it is not the grid centre")
            else:
                msg += "; circular pattern: 'at' is the pattern centre, instances lie on 'radius' around it"
        return KernelError(
            "hole_missed",
            msg,
            details={"missed_instances": missed, "missed_positions": [list(positions[i]) for i in missed], "instances": n},
        )

    # -- edge ops --------------------------------------------------------- #

    def edge_op(self, op: FilletOp | ChamferOp) -> tuple[str, dict[str, Any]]:
        if self.part is None:
            raise KernelError("no_body", f"a {op.type} needs an existing body")
        edges = select_edges(self.part, op.edges)
        if op.edges.near is None and op.edges.max_count is not None:
            self.warnings.append(Issue(op_id=op.id, code="max_count_ignored", message="max_count only applies together with near"))
        if not edges:
            raise KernelError("no_edges_selected", f"edge selector {op.edges.group!r} matched no edges")
        code = f"{op.type}_failed"
        try:
            if isinstance(op, FilletOp):
                raw = self.part.fillet(op.radius, edges)
            else:
                raw = self.part.chamfer(op.distance, None, edges)
        except Exception as exc:
            size = op.radius if isinstance(op, FilletOp) else op.distance
            raise KernelError(code, f"{op.type} of {_fmt(size)} mm on {len(edges)} edge(s) failed; try a smaller value ({exc})") from exc
        result = _as_part(raw)
        if result is None or not _is_valid(result):
            raise KernelError(code, f"{op.type} produced an invalid solid; try a smaller value")
        self._commit(op.id, result)
        return _op_summary(op), {"edge_count": len(edges)}

    # -- common ----------------------------------------------------------- #

    def _commit(self, op_id: str, result: Any) -> None:
        if result.volume <= MIN_VOLUME:
            raise KernelError("empty_result", "operation leaves no material")
        if not _is_valid(result):
            fixed = result.fix()
            if not _is_valid(fixed):
                raise KernelError("invalid_solid", "operation produced an invalid solid")
            self.warnings.append(Issue(op_id=op_id, code="solid_fixed", message="result was repaired by ShapeFix"))
            result = fixed
        n = len(result.solids())
        if n > 1:
            self.warnings.append(Issue(op_id=op_id, code="multiple_solids", message=f"the part now has {n} separate solids"))
        self.part = result

    def run_op(self, op: Operation) -> None:
        before = self.part
        self._tools, self._info, self._current = [], {}, op.id
        n_features = len(self.features)
        self._run_op(op)
        if len(self.features) > n_features and self.features[-1].status == "ok":
            self.trace.append(OpTrace(op=op, before=before, after=self.part, tools=list(self._tools), info=dict(self._info)))

    def _run_op(self, op: Operation) -> None:
        if isinstance(op, (BoxOp, CylinderOp, ExtrudeOp, RevolveOp, SweepOp, BentPlateOp, CoilOp)):
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=self.body(op)))
        elif isinstance(op, CutOp):
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=self.cut(op)))
        elif isinstance(op, ShellOp):
            summary = self.shell(op)
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=summary,
                                               detail={"open_faces": len(self._info.get("open_faces", []))}))
        elif isinstance(op, TextOp):
            summary, detail = self.text(op)
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=summary, detail=detail))
        elif isinstance(op, PatternOp):
            pat = self.pattern(op)
            if pat is None:
                self.warnings.append(Issue(op_id=op.id, code="target_suppressed", message=f"target {op.target} is suppressed; no effect"))
                self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="suppressed", summary=f"target {op.target} is suppressed"))
                return
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=pat,
                                               detail={"instances": len(self._info["matrices"]) + 1}))
        elif isinstance(op, HoleOp):
            summary = self.hole(op)
            n = self.holes[-1].count
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=summary, detail={"count": n}))
        elif isinstance(op, (FilletOp, ChamferOp)):
            summary, detail = self.edge_op(op)
            self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=summary, detail=detail))
        elif isinstance(op, TransformOp):
            target = self.ops_by_id.get(op.target)
            if target is None or target.type not in BODY_TYPES:
                raise KernelError("invalid_transform_target", f"transform target {op.target} must be a body op (box, cylinder, "
                                                              "extrude, revolve, sweep, bent plate or coil)")
            if target.suppressed:
                self.warnings.append(Issue(op_id=op.id, code="target_suppressed", message=f"target {op.target} is suppressed; no effect"))
                self.features.append(FeatureRecord(op_id=op.id, type=op.type, status="suppressed", summary=f"target {op.target} is suppressed"))
                return
            self.features.append(
                FeatureRecord(op_id=op.id, type=op.type, status="ok", summary=_op_summary(op) + f" (applied at {op.target})")
            )
        else:  # pragma: no cover
            raise KernelError("internal_error", f"unknown op type {op.type}")


def build(
    spec: ModelSpec,
    base_step: bytes | None = None,
    *,
    tolerance: float = 0.05,
    angular_tolerance: float = 0.2,
    export: bool = True,
) -> BuildResult:
    """Replay ``spec`` (and the base STEP bytes when ``spec.base`` is set).

    ``tolerance`` (absolute mm) and ``angular_tolerance`` (radians) control the
    GLB / mesh_json tessellation only; they do not change ``build_key``.
    With ``export=False`` artifacts stay empty (drawing / validation use).
    The resulting solid is kept in memory on ``result.shape``.
    """
    key = build_key(spec, KERNEL_VERSION)
    rp = _Replay(spec)
    failed = False

    if spec.base is not None:
        if base_step is None:
            rp.errors.append(Issue(op_id="F-00", code="base_missing", message="spec has a STEP base but no base bytes were given"))
            failed = True
        elif hashlib.sha256(base_step).hexdigest() != spec.base.blob_sha256:
            rp.errors.append(Issue(op_id="F-00", code="base_hash_mismatch", message="base bytes do not match base.blob_sha256"))
            failed = True
        else:
            try:
                rp.load_base(base_step)
            except KernelError as exc:
                rp.errors.append(Issue(op_id="F-00", code=exc.code, message=exc.message))
                failed = True
            except Exception as exc:
                rp.errors.append(Issue(op_id="F-00", code="base_import_failed", message=str(exc)))
                failed = True
        if failed:
            rp.features.append(FeatureRecord(op_id="F-00", type="base_step", status="error", summary=rp.errors[-1].message))
    elif base_step is not None:
        rp.warnings.append(Issue(op_id=None, code="base_ignored", message="base bytes given but spec has no base; ignored"))

    for op in spec.ops:
        if failed:
            rp.features.append(FeatureRecord(op_id=op.id, type=op.type, status="skipped", summary="not built (an earlier op failed)"))
            continue
        if op.suppressed:
            rp.features.append(FeatureRecord(op_id=op.id, type=op.type, status="suppressed", summary=_op_summary(op)))
            continue
        try:
            rp.run_op(op)
        except KernelError as exc:
            rp.errors.append(Issue(op_id=op.id, code=exc.code, message=exc.message, details=exc.details))
            failed = True
        except Exception as exc:  # OCCT failures surface as many exception types
            rp.errors.append(Issue(op_id=op.id, code="internal_error", message=f"{type(exc).__name__}: {exc}"))
            failed = True
        if failed:
            rp.features.append(FeatureRecord(op_id=op.id, type=op.type, status="error", summary=rp.errors[-1].message))

    shape = rp.part
    if not failed and shape is None:
        rp.errors.append(Issue(op_id=None, code="empty_result", message="no geometry: there is no base and no active body op"))
        failed = True

    metrics = None
    artifacts = BuildArtifacts()
    if shape is not None:
        metrics = compute_metrics(shape)
        if not failed and not metrics.valid:
            last = next((op.id for op in reversed(spec.ops) if not op.suppressed), "F-00")
            rp.errors.append(Issue(op_id=last, code="invalid_solid", message="final solid fails the validity check"))
            failed = True
        if export:
            try:
                artifacts = export_artifacts(shape, tolerance, angular_tolerance)
            except Exception as exc:
                rp.errors.append(Issue(op_id=None, code="export_failed", message=f"{type(exc).__name__}: {exc}"))
                failed = True

    result = BuildResult(
        ok=not failed,
        build_key=key,
        kernel_version=KERNEL_VERSION,
        errors=rp.errors,
        warnings=rp.warnings,
        metrics=metrics,
        features=rp.features,
        holes=rp.holes,
        detected_holes=rp.detected,
        artifacts=artifacts,
    )
    result._shape = shape
    result._trace = rp.trace
    return result
