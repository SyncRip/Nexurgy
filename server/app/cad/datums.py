"""Semantic datums of a part definition (W0 contract §4; W3 geometry library).

Mates (W4), DatumRef workplanes (:class:`app.cad.sketch.DatumRef`) and template ports name these instead of face / edge
indices, which change between builds. Every datum is computed from the op parameters alone (pure Python, no kernel),
in the definition frame (the world frame of the ops), so the names are stable across rebuilds of the same op list:

- ``origin``: the part frame (always present);
- ``<op>.<side>``: box faces ``top`` / ``bottom`` / ``front`` (−Y) / ``back`` (+Y) / ``left`` (−X) / ``right`` (+X);
  cylinder / revolve / coil end planes ``top`` / ``bottom``; extrude / sweep / bent plate ``start`` / ``end``; cut ``entry`` /
  ``floor``; bent plate leg faces ``leg[i].a`` (reference face) / ``leg[i].b`` (opposite face);
- ``<op>.wall[i]``: the planar side wall of straight profile segment i of an extrude / cut; inner profile loops use
  ``<op>.hole[j].wall[i]``. Indices follow the resolved profile loop, including its closing segment; curved segments
  keep their index but have no plane datum;
- ``<op>.axis`` and ``<op>.axis[i]``: cylinder, revolve, coil, circular extrude / cut, hole instance i (``axis`` = instance
  0), bent plate ``bend[i].axis``, pattern instance i of an axis-carrying target;
- template ports ``port.<name>`` (:func:`template_datums`).

Planes carry their outward normal (out of the material for a face) as ``direction`` and an in-plane ``x_dir``; axes
carry the axis direction (a hole's points into the material, the drilling direction). Body ``rotate`` and
``transform`` ops move the datums of their target exactly like the kernel moves its tool. A suppressed op has no
datums; a reference to a datum that no longer exists is a broken reference (never re-bound to a neighbour).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from .assembly_model import IDENTITY, Datum, Matrix4, apply, apply_dir, compose, rotation, translation
from .ops import (
    BentPlateOp,
    BoxOp,
    CircularLayout,
    CoilOp,
    CutOp,
    CylinderOp,
    ExtrudeOp,
    HoleOp,
    LinearLayout,
    ModelSpec,
    PatternOp,
    RevolveOp,
    SweepOp,
    TransformOp,
    hole_positions,
    hole_world_frame,
)
from .sketch import (
    NAMED_PLANES,
    DatumRef,
    Frame,
    Workplane,
    bent_plate_layout,
    cross3,
    default_x,
    loop_area,
    profile_extent,
    profile_loops,
    resolve_path,
    unit3,
)

V3 = tuple[float, float, float]


class DatumError(ValueError):
    """A DatumRef that names a datum the earlier ops do not produce."""

    def __init__(self, ref: str, available: Sequence[str]):
        self.ref = ref
        self.available = list(available)
        op = ref.split(".", 1)[0]
        near = [n for n in available if n.startswith(op + ".")]
        hint = f"; {op} has: {', '.join(near[:12])}" if near else f"; {op} has no datums (suppressed, deleted or not a feature)"
        super().__init__(f"datum {ref!r} does not exist{hint}")


def _r(v: Iterable[float]) -> V3:
    return tuple(round(float(c), 9) + 0.0 for c in v)  # type: ignore[return-value]


def _plane(name: str, origin: Sequence[float], normal: Sequence[float], x: Sequence[float] | None = None) -> Datum:
    n = unit3(normal)
    xx = default_x(n) if x is None else Frame.make(origin, n, x).x
    return Datum(name=name, kind="plane", origin=_r(origin), direction=_r(n), x_dir=_r(xx))


def _axis(name: str, origin: Sequence[float], direction: Sequence[float]) -> Datum:
    d = unit3(direction)
    return Datum(name=name, kind="axis", origin=_r(origin), direction=_r(d), x_dir=_r(default_x(d)))


def _moved(d: Datum, m: Matrix4) -> Datum:
    if m == IDENTITY:
        return d
    return Datum(name=d.name, kind=d.kind, origin=_r(apply(m, d.origin)), direction=_r(apply_dir(m, d.direction)),
                 x_dir=_r(apply_dir(m, d.x_dir)))


def _renamed(d: Datum, name: str) -> Datum:
    return Datum(name=name, kind=d.kind, origin=d.origin, direction=d.direction, x_dir=d.x_dir)


def rotation_about(anchor: Sequence[float], rotate: Sequence[float]) -> Matrix4:
    """Rotation about world-parallel X, then Y, then Z axes through ``anchor`` (kernel ``_rotation_trsf``)."""
    a = tuple(float(c) for c in anchor)
    return compose(translation(*a), compose(rotation(*rotate), translation(-a[0], -a[1], -a[2])))


def body_anchor(op: Any, frame: Frame | None = None) -> V3:
    """The point a body's ``rotate`` and transforms turn about (kernel ``_body_tool``)."""
    if isinstance(op, (BoxOp, CylinderOp, RevolveOp, CoilOp)):
        return tuple(float(c) for c in op.at)  # type: ignore[return-value]
    assert frame is not None
    return frame.origin


def placement(op: Any, transforms: Sequence[TransformOp], anchor: Sequence[float]) -> Matrix4:
    """World placement of a body tool: its own ``rotate`` about the anchor, then each transform (rotate about the
    current anchor, then translate), in list order."""
    m = IDENTITY
    a = tuple(float(c) for c in anchor)
    if any(abs(x) > 1e-12 for x in op.rotate):
        m = compose(rotation_about(a, op.rotate), m)
    for t in transforms:
        if any(abs(x) > 1e-12 for x in t.rotate):
            m = compose(rotation_about(a, t.rotate), m)
        if any(abs(x) > 1e-12 for x in t.translate):
            m = compose(translation(*t.translate), m)
            a = (a[0] + t.translate[0], a[1] + t.translate[1], a[2] + t.translate[2])
    return m


def pattern_matrices(layout: Any) -> list[Matrix4]:
    """Rigid transforms of pattern instances 1 … n−1 (instance 0 is the target itself), in instance order: linear
    (i, j) row by row (j-major, i within a row), circular k = 1 … count − 1."""
    out: list[Matrix4] = []
    if isinstance(layout, LinearLayout):
        d1 = unit3(layout.direction)
        d2 = unit3(layout.direction2) if layout.direction2 is not None else (0.0, 0.0, 0.0)
        for j in range(layout.count2):
            for i in range(layout.count):
                if i == 0 and j == 0:
                    continue
                v = tuple(i * layout.spacing * d1[k] + j * layout.spacing2 * d2[k] for k in range(3))
                out.append(translation(*v))
        return out
    assert isinstance(layout, CircularLayout)
    a = unit3(layout.axis)
    o = tuple(float(c) for c in layout.axis_origin)
    for k in range(1, layout.count):
        out.append(axis_rotation(o, a, layout.step * k))
    return out


def axis_rotation(origin: Sequence[float], axis: Sequence[float], deg: float) -> Matrix4:
    """Rotation by ``deg`` (right-hand) about the line through ``origin`` along ``axis``."""
    x, y, z = unit3(axis)
    t = math.radians(deg)
    c, s, C = math.cos(t), math.sin(t), 1 - math.cos(t)
    r = ((c + x * x * C, x * y * C - z * s, x * z * C + y * s),
         (y * x * C + z * s, c + y * y * C, y * z * C - x * s),
         (z * x * C - y * s, z * y * C + x * s, c + z * z * C))
    o = tuple(float(v) for v in origin)
    tx = tuple(o[i] - sum(r[i][k] * o[k] for k in range(3)) for i in range(3))
    return ((r[0][0], r[0][1], r[0][2], tx[0]), (r[1][0], r[1][1], r[1][2], tx[1]), (r[2][0], r[2][1], r[2][2], tx[2]),
            (0.0, 0.0, 0.0, 1.0))


# --------------------------------------------------------------------------- #
# Plane frames of ops
# --------------------------------------------------------------------------- #


def base_frame(plane: Any, known: dict[str, Datum] | None = None) -> Frame:
    """The frame of a plane spec: a named plane, a :class:`Workplane`, or a :class:`DatumRef` resolved against
    ``known`` datums (raises :class:`DatumError` when it is not there)."""
    if isinstance(plane, str):
        return NAMED_PLANES[plane]
    if isinstance(plane, Workplane):
        return plane.frame()
    if isinstance(plane, DatumRef):
        if plane.ref == "origin":
            d = Datum(name="origin", kind="frame", origin=(0.0, 0.0, 0.0))
        else:
            if known is None or plane.ref not in known:
                raise DatumError(plane.ref, sorted(known or {}))
            d = known[plane.ref]
        f = Frame.make(d.origin, d.direction, d.x_dir)
        if plane.flip:
            f = f.flipped()
        return f.moved(w=plane.offset) if abs(plane.offset) > 0 else f
    raise TypeError(f"unknown plane {plane!r}")


def placed_frame(plane: Any, offset: float, at: Sequence[float], rotate_profile: float, known: dict[str, Datum] | None = None) -> Frame:
    """Frame of a profile: the plane shifted by ``offset`` along its normal, moved to ``at`` (u, v) and turned by
    ``rotate_profile`` about the normal (extrude / cut / bent plate)."""
    f = base_frame(plane, known)
    return f.moved(float(at[0]), float(at[1]), float(offset)).rotated(float(rotate_profile))


# --------------------------------------------------------------------------- #
# Datums per op
# --------------------------------------------------------------------------- #


def _box_datums(op: BoxOp) -> list[Datum]:
    (ax, ay, az), (sx, sy, sz) = op.at, op.size
    zc = az + sz / 2.0
    return [
        _plane(f"{op.id}.top", (ax, ay, az + sz), (0, 0, 1), (1, 0, 0)),
        _plane(f"{op.id}.bottom", (ax, ay, az), (0, 0, -1), (1, 0, 0)),
        _plane(f"{op.id}.front", (ax, ay - sy / 2.0, zc), (0, -1, 0), (1, 0, 0)),
        _plane(f"{op.id}.back", (ax, ay + sy / 2.0, zc), (0, 1, 0), (-1, 0, 0)),
        _plane(f"{op.id}.left", (ax - sx / 2.0, ay, zc), (-1, 0, 0), (0, -1, 0)),
        _plane(f"{op.id}.right", (ax + sx / 2.0, ay, zc), (1, 0, 0), (0, 1, 0)),
    ]


def _cylinder_datums(op: CylinderOp) -> list[Datum]:
    d = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[op.axis]
    at = tuple(float(c) for c in op.at)
    top = tuple(at[i] + op.height * d[i] for i in range(3))
    return [_axis(f"{op.id}.axis", at, d), _plane(f"{op.id}.top", top, d), _plane(f"{op.id}.bottom", at, tuple(-c for c in d))]


def _extrude_frame(op: ExtrudeOp, known: dict[str, Datum]) -> Frame:
    return placed_frame(op.plane, op.offset, op.at, op.rotate_profile, known)


def _profile_wall_datums(op: ExtrudeOp | CutOp, f: Frame) -> list[Datum]:
    """Supporting planes of actual straight profile walls, independent of kernel face ordering.

    Loop winding determines the outside of the tool; an inner loop and a subtracting tool each reverse that normal.
    Cut tools run against the profile normal, whereas extrusions run along it. The plane's point follows that span;
    through cuts use the entry edge because their final length depends on the material being cut.
    """
    outer, holes = profile_loops(op.profile)
    if isinstance(op, CutOp):
        w, material = -(op.depth or 0.0) / 2.0, -1.0
    else:
        w, material = op.height / 2.0, -1.0 if op.mode == "subtract" else 1.0
    out: list[Datum] = []
    for prefix, segs, inner in [(op.id, outer, False)] + [
        (f"{op.id}.hole[{j}]", segs, True) for j, segs in enumerate(holes)
    ]:
        area = loop_area(segs)
        if abs(area) < 1e-15:
            continue
        sign = (1.0 if area > 0 else -1.0) * material * (-1.0 if inner else 1.0)
        for i, seg in enumerate(segs):
            if seg.kind != "line":
                continue  # a curved wall has no single supporting plane
            du, dv = seg.p1[0] - seg.p0[0], seg.p1[1] - seg.p0[1]
            u, v = (seg.p0[0] + seg.p1[0]) / 2.0, (seg.p0[1] + seg.p1[1]) / 2.0
            out.append(_plane(f"{prefix}.wall[{i}]", f.point(u, v, w),
                              f.vector(sign * dv, -sign * du), f.vector(du, dv)))
    return out


def _extrude_datums(op: ExtrudeOp, known: dict[str, Datum]) -> list[Datum]:
    f = _extrude_frame(op, known)
    out = [_plane(f"{op.id}.start", f.origin, tuple(-c for c in f.z), f.x),
           _plane(f"{op.id}.end", f.point(0, 0, op.height), f.z, f.x)]
    if op.profile.kind in ("circle", "ring"):
        out.append(_axis(f"{op.id}.axis", f.origin, f.z))
    out += _profile_wall_datums(op, f)
    return out


def _revolve_datums(op: RevolveOp) -> list[Datum]:
    f = op.frame()
    u_max = profile_extent(op.profile, (1.0, 0.0))
    u_min = -profile_extent(op.profile, (-1.0, 0.0))
    a = f.x
    return [_axis(f"{op.id}.axis", f.origin, a),
            _plane(f"{op.id}.top", f.point(u_max, 0.0), a, f.y),
            _plane(f"{op.id}.bottom", f.point(u_min, 0.0), tuple(-c for c in a), f.y)]


def _coil_datums(op: CoilOp) -> list[Datum]:
    """Axis, seating plane (``bottom``, z = 0 of the coil frame) and ``top`` (z = overall length) of a coil."""
    f = op.frame()
    length = op.law().length
    return [_axis(f"{op.id}.axis", f.origin, f.z),
            _plane(f"{op.id}.bottom", f.origin, tuple(-c for c in f.z), f.x),
            _plane(f"{op.id}.top", f.point(0.0, 0.0, length), f.z, f.x)]


def sweep_frames(op: SweepOp, known: dict[str, Datum] | None = None) -> tuple[Frame, Frame, list[Any]]:
    """(path plane frame, section frame at the path start, resolved path segments)."""
    pf = base_frame(op.plane, known)
    segs = resolve_path(op.path.start, op.path.segments)
    t0 = segs[0].tangent(0.0)
    T = unit3(pf.vector(t0[0], t0[1]))
    left = cross3(pf.z, T)
    section = Frame(pf.point(*segs[0].p0), left, pf.z, T)
    return pf, section, segs


def _sweep_datums(op: SweepOp, known: dict[str, Datum]) -> list[Datum]:
    pf, section, segs = sweep_frames(op, known)
    t1 = segs[-1].tangent(1.0)
    end_t = unit3(pf.vector(t1[0], t1[1]))
    return [_plane(f"{op.id}.start", section.origin, tuple(-c for c in section.z), section.x),
            _plane(f"{op.id}.end", pf.point(*segs[-1].p1), end_t, cross3(pf.z, end_t))]


def bent_plate_frame(op: BentPlateOp, known: dict[str, Datum] | None = None) -> Frame:
    return placed_frame(op.plane, op.offset, op.at, 0.0, known)


def _bent_plate_datums(op: BentPlateOp, known: dict[str, Datum]) -> list[Datum]:
    f = bent_plate_frame(op, known)
    lay = bent_plate_layout(op.thickness, op.legs, op.bends, op.inner_radius, op.leg_length)
    w = op.width / 2.0
    out = [_plane(f"{op.id}.start", f.origin, tuple(-c for c in f.z), f.x),
           _plane(f"{op.id}.end", f.point(0, 0, op.width), f.z, f.x)]
    for i, (p, q, phi) in enumerate(lay.legs):
        mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
        lu, lv = -math.sin(phi), math.cos(phi)  # left of travel = thickness side
        along = f.vector(math.cos(phi), math.sin(phi))
        out.append(_plane(f"{op.id}.leg[{i}].a", f.point(mid[0], mid[1], w), f.vector(-lu, -lv), along))
        out.append(_plane(f"{op.id}.leg[{i}].b", f.point(mid[0] + op.thickness * lu, mid[1] + op.thickness * lv, w), f.vector(lu, lv), along))
    for i, c in enumerate(lay.bend_centers):
        out.append(_axis(f"{op.id}.bend[{i}].axis", f.point(c[0], c[1], 0.0), f.z))
    return out


def _hole_datums(op: HoleOp) -> list[Datum]:
    _, _, d = hole_world_frame(op)
    pos = hole_positions(op)
    out = [_axis(f"{op.id}.axis", pos[0], d)]
    if len(pos) > 1:
        out += [_axis(f"{op.id}.axis[{i}]", p, d) for i, p in enumerate(pos)]
    return out


def cut_frame(op: CutOp, known: dict[str, Datum] | None = None) -> Frame:
    return placed_frame(op.plane, op.offset, op.at, op.rotate_profile, known)


def _cut_datums(op: CutOp, known: dict[str, Datum]) -> list[Datum]:
    f = cut_frame(op, known)
    out = [_plane(f"{op.id}.entry", f.origin, f.z, f.x)]
    if op.depth is not None:
        out.append(_plane(f"{op.id}.floor", f.point(0, 0, -op.depth), f.z, f.x))
    if op.profile.kind in ("circle", "ring"):
        out.append(_axis(f"{op.id}.axis", f.origin, tuple(-c for c in f.z)))
    out += _profile_wall_datums(op, f)
    return out


def _local_datums(op: Any, known: dict[str, Datum]) -> list[Datum]:
    if isinstance(op, BoxOp):
        return _box_datums(op)
    if isinstance(op, CylinderOp):
        return _cylinder_datums(op)
    if isinstance(op, ExtrudeOp):
        return _extrude_datums(op, known)
    if isinstance(op, RevolveOp):
        return _revolve_datums(op)
    if isinstance(op, SweepOp):
        return _sweep_datums(op, known)
    if isinstance(op, BentPlateOp):
        return _bent_plate_datums(op, known)
    if isinstance(op, CoilOp):
        return _coil_datums(op)
    if isinstance(op, HoleOp):
        return _hole_datums(op)
    if isinstance(op, CutOp):
        return _cut_datums(op, known)
    return []


def body_frame(op: Any, known: dict[str, Datum] | None = None) -> Frame | None:
    """The profile / path frame whose origin is the body anchor (extrude, sweep, bent plate)."""
    if isinstance(op, ExtrudeOp):
        return _extrude_frame(op, known or {})
    if isinstance(op, SweepOp):
        return sweep_frames(op, known)[1]
    if isinstance(op, BentPlateOp):
        return bent_plate_frame(op, known)
    return None


def _transforms(spec: ModelSpec) -> dict[str, list[TransformOp]]:
    out: dict[str, list[TransformOp]] = defaultdict(list)
    by_id = {op.id: op for op in spec.ops}
    for op in spec.ops:
        if isinstance(op, TransformOp) and not op.suppressed:
            t = by_id.get(op.target)
            if t is not None and t.type in ("box", "cylinder", "extrude", "revolve", "sweep", "bent_plate", "coil"):
                out[op.target].append(op)
    return out


def op_datums(spec: ModelSpec, *, upto: str | None = None) -> dict[str, Datum]:
    """Datums of the active ops (in list order), stopping before op ``upto``; ``origin`` included. Ops whose own
    DatumRef cannot be resolved contribute nothing (the build reports them)."""
    known: dict[str, Datum] = {"origin": Datum(name="origin", kind="frame", origin=(0.0, 0.0, 0.0))}
    tf = _transforms(spec)
    by_id = {op.id: op for op in spec.ops}
    for op in spec.ops:
        if op.id == upto:
            break
        if op.suppressed:
            continue
        try:
            if isinstance(op, PatternOp):
                ds = _pattern_datums(op, by_id.get(op.target), known)
            else:
                ds = _local_datums(op, known)
                if op.type in ("box", "cylinder", "extrude", "revolve", "sweep", "bent_plate", "coil"):
                    m = placement(op, tf.get(op.id, []), body_anchor(op, body_frame(op, known)))
                    ds = [_moved(d, m) for d in ds]
        except (DatumError, ValueError):
            continue
        for d in ds:
            known[d.name] = d
    return known


def _pattern_datums(op: PatternOp, target: Any, known: dict[str, Datum]) -> list[Datum]:
    if target is None or target.suppressed:
        return []
    base = known.get(f"{target.id}.axis")
    if base is None:
        return []
    mats = [IDENTITY] + pattern_matrices(op.layout)
    return [_renamed(_moved(base, m), f"{op.id}.axis[{k}]") for k, m in enumerate(mats)]


def template_datums(template: dict[str, Any] | None) -> list[Datum]:
    """``port.<name>`` datums of a template instance (``meta["ports"]`` of :func:`app.cad.templates.instantiate`,
    also stored as ``checks["template"]`` of a revision). None once a template op was edited or removed: the ports
    were computed from the template parameters and no longer describe the geometry (a stale port is missing, so a
    mate on it reports broken instead of binding to old coordinates)."""
    out: list[Datum] = []
    if (template or {}).get("modified_ops") or (template or {}).get("removed_ops"):
        return out
    for p in (template or {}).get("ports") or []:
        if isinstance(p, dict) and str(p.get("name", "")).startswith("port."):
            out.append(Datum(name=p["name"], kind=p.get("kind", "plane"), origin=_r(p["origin"]),
                             direction=_r(p.get("direction", (0, 0, 1))), x_dir=_r(p.get("x_dir", (1, 0, 0)))))
    return out


def datums_for(spec: ModelSpec, built: Any = None, *, template: dict[str, Any] | None = None) -> list[Datum]:
    """Every datum of a part definition (W0 §4): ``origin``, the op datums and, when the revision came from a part
    template, its ``port.*`` datums. ``built`` (a ``BuildResult`` of ``spec``, optional) adds the axes of holes
    detected on an imported base (``F-00.axis[i]``, heuristic, as the build reports them)."""
    known = op_datums(spec)
    out = list(known.values())
    if built is not None and spec.base is not None:
        k = 0
        for h in getattr(built, "detected_holes", None) or []:
            for p in h.positions:
                out.append(_axis(f"F-00.axis[{k}]", p, h.axis))
                k += 1
    out += template_datums(template)
    return out


def datum_dict(d: Datum) -> dict[str, Any]:
    """JSON form of a datum (API, revision checks)."""
    return {"name": d.name, "kind": d.kind, "origin": list(d.origin), "direction": list(d.direction), "x_dir": list(d.x_dir)}


# --------------------------------------------------------------------------- #
# Assembly frames and interfaces (generation reliability round 3, B: mate-based placement)
# --------------------------------------------------------------------------- #

#: The datums of an assembly's own frame: its origin, the axes X / Y / Z through it and the planes XY / YZ / ZX
#: through it (normals +Z / +X / +Y). A sub-assembly instance and an assembly frame (``@<assembly key>`` in a mate)
#: expose them, so a frame can be attached before anything inside it has a position.
FRAME_DATUMS: tuple[str, ...] = ("origin", "X", "Y", "Z", "XY", "YZ", "ZX")


def frame_datums() -> list[Datum]:
    """:data:`FRAME_DATUMS` in the assembly's own frame."""
    o = (0.0, 0.0, 0.0)
    return [Datum(name="origin", kind="frame", origin=o),
            _axis("X", o, (1, 0, 0)), _axis("Y", o, (0, 1, 0)), _axis("Z", o, (0, 0, 1)),
            _plane("XY", o, (0, 0, 1), (1, 0, 0)), _plane("YZ", o, (1, 0, 0), (0, 1, 0)), _plane("ZX", o, (0, 1, 0), (0, 0, 1))]


def interface_datums(interfaces: Iterable[dict[str, Any]]) -> list[Datum]:
    """Datums of an assembly's interfaces (spec content ``interfaces``: ``{key, owner, kind: axis | plane | point |
    pattern, at, dir, points, …}``, coordinates in the owner's frame): ``if.<key>`` for an axis (``at`` on it, ``dir``
    along it), a plane (``at`` on it, ``dir`` its normal) or a point; a pattern of axes along ``dir`` through each of
    ``points`` is ``if.<key>.axis[i]`` (a ``pattern_match`` names it ``if.<key>``). An interface without usable numbers
    gives no datum (it is never guessed)."""
    out: list[Datum] = []
    for f in interfaces:
        if not isinstance(f, dict) or not f.get("key"):
            continue
        name, kind = f"if.{f['key']}", f.get("kind")
        try:
            at = _vec(f.get("at")) if f.get("at") is not None else None
            d = _vec(f.get("dir")) if f.get("dir") is not None else None
            if kind == "axis" and at and d and any(d):
                out.append(_axis(name, at, d))
            elif kind == "plane" and at and d and any(d):
                out.append(_plane(name, at, d))
            elif kind == "point" and at:
                out.append(Datum(name=name, kind="point", origin=_r(at)))
            elif kind == "pattern" and d and any(d):
                pts = [_vec(p) for p in f.get("points") or []]
                out += [_axis(f"{name}.axis[{i}]", p, d) for i, p in enumerate(pts) if p]
        except (TypeError, ValueError):
            continue
    return out


def _vec(v: Any) -> V3 | None:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return None
    out = tuple(float(c) for c in v)
    return out if all(math.isfinite(c) for c in out) else None  # type: ignore[return-value]
