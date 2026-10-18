"""Pure-data assembly geometry shared by static assemblies (W4) and assembly drawings (W5).

No database or storage access (backend directory rule: CAD modules never touch business data). The assembly
service resolves an exact AssemblyRevision into an :class:`AssemblyGeometry` plus BOM lines; the drawing
module only consumes these objects.

Conventions (W0 contract §4):

- Units mm, right-handed XYZ, world +Z up.
- A transform is a rigid 4x4 row-major matrix (tuple of 4 rows) mapping child-local coordinates into the
  parent frame: ``p_parent = M · [p_local, 1]``. World transform of a leaf = parent chain composed root-first.
- ``rotation(rx, ry, rz)`` rotates about the frame's X, then Y, then Z axes (``R = Rz · Ry · Rx``), the same
  order as body ``rotate`` in :mod:`app.cad.ops`.
- A mirror (determinant -1) is not a rigid transform: a handed copy must be a new / derived definition.
- Instance ids are stable strings owned by the assembly service; a path is the tuple of instance ids from
  the root assembly down to the leaf, so the same definition placed twice has two different paths.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

Matrix4 = tuple[tuple[float, float, float, float], tuple[float, float, float, float],
                tuple[float, float, float, float], tuple[float, float, float, float]]

IDENTITY: Matrix4 = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))

Procurement = Literal["make", "buy", "standard", "unknown"]
Representation = Literal["exact", "nominal", "simplified"]


def _mat(rows: Any) -> Matrix4:
    return tuple(tuple(float(v) for v in r) for r in rows)  # type: ignore[return-value]


def rotation(rx: float = 0.0, ry: float = 0.0, rz: float = 0.0) -> Matrix4:
    """Rotation in degrees about X, then Y, then Z."""
    ax, ay, az = (math.radians(a) for a in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    rxm = ((1, 0, 0, 0), (0, cx, -sx, 0), (0, sx, cx, 0), (0, 0, 0, 1))
    rym = ((cy, 0, sy, 0), (0, 1, 0, 0), (-sy, 0, cy, 0), (0, 0, 0, 1))
    rzm = ((cz, -sz, 0, 0), (sz, cz, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))
    return compose(_mat(rzm), compose(_mat(rym), _mat(rxm)))


def translation(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Matrix4:
    return _mat(((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z), (0, 0, 0, 1)))


def placement(translate: tuple[float, float, float] = (0.0, 0.0, 0.0),
              rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Matrix4:
    """Rotate about the local origin first, then translate (GEN-02: "rotate 180° about Z, then move")."""
    return compose(translation(*translate), rotation(*rotate))


def compose(a: Matrix4, b: Matrix4) -> Matrix4:
    """a · b (apply b first, then a)."""
    return _mat([[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)])


def invert(m: Matrix4) -> Matrix4:
    """Inverse of a rigid transform."""
    r = [[m[j][i] for j in range(3)] for i in range(3)]  # transpose of rotation
    t = [m[0][3], m[1][3], m[2][3]]
    nt = [-sum(r[i][k] * t[k] for k in range(3)) for i in range(3)]
    return _mat([[r[0][0], r[0][1], r[0][2], nt[0]], [r[1][0], r[1][1], r[1][2], nt[1]],
                 [r[2][0], r[2][1], r[2][2], nt[2]], [0, 0, 0, 1]])


def apply(m: Matrix4, p: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))  # type: ignore[return-value]


def apply_dir(m: Matrix4, d: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(m[i][0] * d[0] + m[i][1] * d[1] + m[i][2] * d[2] for i in range(3))  # type: ignore[return-value]


def determinant3(m: Matrix4) -> float:
    a = m
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def is_rigid(m: Matrix4, tol: float = 1e-6) -> bool:
    """Orthonormal rotation with determinant +1 and a proper last row."""
    if any(abs(m[3][j] - (1.0 if j == 3 else 0.0)) > tol for j in range(4)):
        return False
    for i in range(3):
        for j in range(3):
            dot = sum(m[k][i] * m[k][j] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > tol:
                return False
    return abs(determinant3(m) - 1.0) <= tol


def to_list(m: Matrix4) -> list[list[float]]:
    """JSON form (persisted in JSONB and returned by the API)."""
    return [list(r) for r in m]


def from_list(rows: list[list[float]]) -> Matrix4:
    m = _mat(rows)
    if len(m) != 4 or any(len(r) != 4 for r in m):
        raise ValueError("transform must be 4x4")
    return m


@dataclass(frozen=True)
class PartDefinition:
    """One unique leaf definition (an exact part revision). ``shape`` is a build123d / OCP shape in the
    definition's own frame; ``key`` is stable for the revision (e.g. the model revision id)."""

    key: str
    name: str
    number: str | None
    material: str | None
    procurement: Procurement = "unknown"
    representation: Representation = "exact"
    shape: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Occurrence:
    """One placed, non-suppressed leaf of the expanded tree."""

    path: tuple[str, ...]  # instance ids root → leaf
    names: tuple[str, ...]  # display names along the path
    part_key: str
    world: Matrix4


@dataclass
class AssemblyGeometry:
    """An exact assembly revision resolved for geometry consumers."""

    key: str  # assembly revision id
    name: str
    number: str | None
    revision_label: str  # e.g. "A3"
    parts: dict[str, PartDefinition]
    occurrences: list[Occurrence]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BomLine:
    """One BOM row.

    Hierarchical lines (``level`` ≥ 1) list each assembly's direct children with the per-parent quantity;
    flattened lines (``level`` = 0) aggregate leaf part definitions across the whole tree with
    ``total_quantity``. ``item`` is the balloon / BOM table number; quantities always come from the instance tree.
    """

    item: str
    level: int
    def_key: str
    def_kind: Literal["part", "assembly"]
    name: str
    number: str | None
    material: str | None
    procurement: Procurement
    representation: Representation
    quantity: int
    total_quantity: int
    instance_paths: tuple[tuple[str, ...], ...] = ()


DatumKind = Literal["frame", "axis", "plane", "point"]


@dataclass(frozen=True)
class Datum:
    """A semantic reference on a part definition, used by mates instead of unstable face / edge indices.

    Names are stable across rebuilds of the same op list: ``origin`` (the part frame, always present),
    ``<op id>.axis`` / ``<op id>.axis[i]`` (cylinder, revolve, hole instance i), ``<op id>.<side>``
    (box faces: top, bottom, front, back, left, right), and template ports (``port.<name>``, W3).
    Coordinates are in the definition's own frame. ``direction`` is the axis direction / plane normal;
    ``x_dir`` completes a frame. A mate that names a datum the new revision no longer has is reported as
    broken (never re-bound to "the closest" one).
    """

    name: str
    kind: DatumKind
    origin: tuple[float, float, float]
    direction: tuple[float, float, float] = (0.0, 0.0, 1.0)
    x_dir: tuple[float, float, float] = (1.0, 0.0, 0.0)
