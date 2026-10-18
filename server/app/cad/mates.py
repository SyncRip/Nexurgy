"""Semantic datums and the static mate solver (W4).

Datums (:class:`app.cad.assembly_model.Datum`) are named references on a part definition that stay stable
across rebuilds of the same op list (W0 contract §4): ``origin``; ``<op>.axis`` / ``<op>.axis[i]`` (cylinder,
extrude, hole and hole-pattern instance i); ``<op>.<side>`` (box faces top / bottom / front / back / left /
right; cylinder / extrude end faces top / bottom); template ports ``port.<name>`` (W3). An imported STEP base
(``F-00``) gets its bounding-box faces and the axes of its detected bores (heuristic).

:func:`fallback_datums` derives them from today's op list. W3 delivers ``cad/datums.py``
(``datums_for(spec, built)``); the service resolves datums through one function and switches to W3's when it
exists, keeping this extractor as the fallback.

Mates (``fixed``, ``coaxial``, ``planar``, ``parallel``, ``perpendicular``, ``offset``, ``angle``,
``pattern_match``) are equations between world datums of two instances. :func:`solve` places the free
instances: a constructive pass aligns each newly constrained instance to an already placed one, then a
Levenberg-Marquardt least-squares pass satisfies all active mates. It reports per mate the residual and a
status (solved / redundant / conflict / broken / invalid), per instance the remaining degrees of freedom with
the free motions (translate along / rotate about), and, under Turbo, the dropped lower-priority conflicting
mates and the DOF held at the current pose as assumptions. A datum that is missing on the (new) definition
makes the mate ``broken``; it is never re-bound to a nearby feature.

Generation reliability round 3 (B, mate-based placement): an assembly's own frame takes part in a solve as a fixed
instance named ``@<assembly key>`` whose datums are its frame axes / planes and interfaces
(:func:`app.cad.datums.frame_datums` / :func:`~app.cad.datums.interface_datums`); :func:`grounded` says which
instances the applied mates POSITION in it (the solver's report: no free translation left; a free turn about an axis is
held), :func:`compatible` which datum kinds a mate kind joins. A planar / offset mate with a frame datum on one side lays
the frame's XY on the other side, +Z along its normal (:func:`same_way`).

Pure: no database (backend directory rule).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from . import assembly_model as am
from .assembly_model import Datum

Vec = tuple[float, float, float]

POS_TOL = 1e-5  # mm
ANG_TOL = 1e-6  # rad
RANK_TOL = 1e-7
MAX_ITER = 200


class SolveStopped(Exception):
    """A caller's cooperative solve budget ended; no numerical trial is a verified pose."""


_SOLVE_BUDGET: ContextVar[tuple[float | None, Callable[[], bool] | None]] = ContextVar("mate_solve_budget", default=(None, None))
_SOLVE_CACHE: ContextVar[dict[str, Any] | None] = ContextVar("mate_solve_cache", default=None)


@contextmanager
def solve_budget(*, deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
                 cache: dict[str, Any] | None = None) -> Iterator[None]:
    """Bound all nested solves on this thread. Deadlines are monotonic; nested scopes cannot extend a budget."""
    old_deadline, old_cancelled = _SOLVE_BUDGET.get()
    ends = [d for d in (deadline, old_deadline) if d is not None]
    last_check, stopped = float("-inf"), False

    def check() -> bool:
        nonlocal last_check, stopped
        now = time.monotonic()
        if not stopped and now - last_check >= 0.25:
            last_check = now
            stopped = bool(old_cancelled and old_cancelled()) or bool(cancelled and cancelled())
        return stopped

    token = _SOLVE_BUDGET.set((min(ends) if ends else None, check))
    cache_token = _SOLVE_CACHE.set(cache if cache is not None else _SOLVE_CACHE.get())
    try:
        yield
    finally:
        _SOLVE_BUDGET.reset(token)
        _SOLVE_CACHE.reset(cache_token)


def _check_budget() -> None:
    deadline, cancelled = _SOLVE_BUDGET.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise SolveStopped("solver_runtime_budget")
    if cancelled is not None and cancelled():
        raise SolveStopped("solver_cancelled")


BOX_SIDES = {
    "top": ((0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    "bottom": ((0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
    "front": ((0.0, -0.5, 0.5), (0.0, -1.0, 0.0)),
    "back": ((0.0, 0.5, 0.5), (0.0, 1.0, 0.0)),
    "left": ((-0.5, 0.0, 0.5), (-1.0, 0.0, 0.0)),
    "right": ((0.5, 0.0, 0.5), (1.0, 0.0, 0.0)),
}  # (centre as a fraction of size from the bottom-centre anchor, outward normal)
MATE_KINDS = ("fixed", "coaxial", "planar", "parallel", "perpendicular", "offset", "angle", "pattern_match")


# --------------------------------------------------------------------------- #
# Small vector helpers
# --------------------------------------------------------------------------- #


def _v(x: Any) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _unit(x: Any) -> np.ndarray:
    a = _v(x)
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-15 else a


def _perp(d: np.ndarray) -> np.ndarray:
    ref = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    return _unit(ref - d * float(ref @ d))


def _tuple(a: np.ndarray) -> Vec:
    return (round(float(a[0]), 9) + 0.0, round(float(a[1]), 9) + 0.0, round(float(a[2]), 9) + 0.0)


def rodrigues(w: np.ndarray) -> np.ndarray:
    th = float(np.linalg.norm(w))
    if th < 1e-15:
        return np.eye(3)
    k = w / th
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * kx + (1 - math.cos(th)) * (kx @ kx)


def _rot_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Smallest rotation taking unit vector a onto unit vector b."""
    a, b = _unit(a), _unit(b)
    c = float(np.clip(a @ b, -1.0, 1.0))
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:
        return rodrigues(_perp(a) * math.pi)
    axis = np.cross(a, b)
    return rodrigues(_unit(axis) * math.acos(c))


def _mat4(r: np.ndarray, t: np.ndarray) -> am.Matrix4:
    m = np.eye(4)
    m[:3, :3] = r
    m[:3, 3] = t
    return am.from_list(m.tolist())


def _np4(m: am.Matrix4) -> np.ndarray:
    return np.array(m, dtype=float)


# --------------------------------------------------------------------------- #
# Fallback datum extraction (today's op list)
# --------------------------------------------------------------------------- #


def _rot_about(rotate: Any, anchor: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) of the kernel's rotation about world X, then Y, then Z through ``anchor``."""
    r = _np4(am.rotation(*rotate))[:3, :3]
    return r, anchor - r @ anchor


def _apply(rt: tuple[np.ndarray, np.ndarray], p: np.ndarray) -> np.ndarray:
    return rt[0] @ p + rt[1]


def fallback_datums(spec: Any, *, bbox: dict[str, Any] | None = None, detected_holes: list[dict[str, Any]] | None = None) -> list[Datum]:
    """Datums of a :class:`app.cad.ops.ModelSpec` (or its dict) from the op list alone.

    Body datums follow the kernel replay exactly: the op's own ``rotate`` about its anchor, then each
    ``transform`` op (rotate about the current anchor, then translate). Faces of a subtracting body point out
    of the remaining material (flipped). ``bbox`` / ``detected_holes`` describe an imported base (F-00)."""
    from .ops import BODY_TYPES, ModelSpec

    if not isinstance(spec, ModelSpec):
        spec = ModelSpec.model_validate(spec)
    out: list[Datum] = [Datum(name="origin", kind="frame", origin=(0.0, 0.0, 0.0))]
    if spec.base is not None:
        if bbox:
            lo, hi = _v(bbox["min"]), _v(bbox["max"])
            mid = (lo + hi) / 2
            for side, (pt, n) in {
                "top": ((mid[0], mid[1], hi[2]), (0, 0, 1)), "bottom": ((mid[0], mid[1], lo[2]), (0, 0, -1)),
                "front": ((mid[0], lo[1], mid[2]), (0, -1, 0)), "back": ((mid[0], hi[1], mid[2]), (0, 1, 0)),
                "left": ((lo[0], mid[1], mid[2]), (-1, 0, 0)), "right": ((hi[0], mid[1], mid[2]), (1, 0, 0)),
            }.items():
                out.append(Datum(name=f"F-00.{side}", kind="plane", origin=_tuple(_v(pt)), direction=_tuple(_v(n))))
        k = 0
        for h in detected_holes or []:
            axis = _unit(h.get("axis") or (0, 0, 1))
            for pos in h.get("positions") or []:
                out.append(Datum(name=f"F-00.axis[{k}]", kind="axis", origin=_tuple(_v(pos)), direction=_tuple(axis),
                                 x_dir=_tuple(_perp(axis))))
                k += 1
    transforms: dict[str, list[Any]] = {}
    for op in spec.ops:
        if op.type == "transform" and not op.suppressed:
            transforms.setdefault(op.target, []).append(op)
    for op in spec.ops:
        if op.suppressed:
            continue
        if op.type in BODY_TYPES and op.type in ("box", "cylinder", "extrude"):
            out += _body_datums(op, transforms.get(op.id, []))
        elif op.type == "hole":
            out += _hole_datums(op)
        # other op types (W3 features) only get datums from W3's cad/datums.py
    return out


def _body_datums(op: Any, transforms: list[Any]) -> list[Datum]:
    flip = -1.0 if op.mode == "subtract" else 1.0
    raw: list[tuple[str, str, np.ndarray, np.ndarray]] = []  # (name, kind, point, direction)
    if op.type == "box":
        size, at = _v(op.size), _v(op.at)
        anchor = at.copy()
        for side, (frac, n) in BOX_SIDES.items():
            p = at + _v(frac) * size
            raw.append((f"{op.id}.{side}", "plane", p, _v(n) * flip))
    elif op.type == "cylinder":
        d = _v({"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[op.axis])
        at = _v(op.at)
        anchor = at.copy()
        raw.append((f"{op.id}.axis", "axis", at, d))
        raw.append((f"{op.id}.bottom", "plane", at, -d * flip))
        raw.append((f"{op.id}.top", "plane", at + d * op.height, d * flip))
    else:  # extrude
        base = {"XY": (_v((1, 0, 0)), _v((0, 1, 0)), _v((0, 0, 1))), "XZ": (_v((1, 0, 0)), _v((0, 0, 1)), _v((0, -1, 0))),
                "YZ": (_v((0, 1, 0)), _v((0, 0, 1)), _v((1, 0, 0)))}[op.plane]
        x_dir, y_dir, z_dir = base
        u, v = op.at
        origin = z_dir * op.offset + x_dir * u + y_dir * v
        anchor = origin.copy()
        raw.append((f"{op.id}.axis", "axis", origin, z_dir))
        raw.append((f"{op.id}.bottom", "plane", origin, -z_dir * flip))
        raw.append((f"{op.id}.top", "plane", origin + z_dir * op.height, z_dir * flip))
    steps: list[tuple[np.ndarray, np.ndarray]] = []
    if any(abs(a) > 1e-12 for a in op.rotate):
        steps.append(_rot_about(op.rotate, anchor))
    for t in transforms:
        if any(abs(a) > 1e-12 for a in t.rotate):
            steps.append(_rot_about(t.rotate, anchor))
        if any(abs(c) > 1e-12 for c in t.translate):
            steps.append((np.eye(3), _v(t.translate)))
            anchor = anchor + _v(t.translate)
    out: list[Datum] = []
    for name, kind, p, d in raw:
        for r, tr in steps:
            p = r @ p + tr
            d = r @ d
        d = _unit(d)
        out.append(Datum(name=name, kind=kind, origin=_tuple(p), direction=_tuple(d), x_dir=_tuple(_perp(d))))  # type: ignore[arg-type]
    return out


def _hole_datums(op: Any) -> list[Datum]:
    from .ops import hole_frame, hole_positions

    d = _v(hole_frame(op.direction)[2])
    positions = hole_positions(op)
    out: list[Datum] = []
    if op.pattern is None:
        out.append(Datum(name=f"{op.id}.axis", kind="axis", origin=_tuple(_v(positions[0])), direction=_tuple(d), x_dir=_tuple(_perp(d))))
    for i, p in enumerate(positions):
        out.append(Datum(name=f"{op.id}.axis[{i}]", kind="axis", origin=_tuple(_v(p)), direction=_tuple(d), x_dir=_tuple(_perp(d))))
    return out


def transformed(d: Datum, m: am.Matrix4, name: str | None = None) -> Datum:
    """A datum expressed in the parent frame of rigid transform ``m``."""
    return Datum(name=name or d.name, kind=d.kind, origin=am.apply(m, d.origin), direction=am.apply_dir(m, d.direction),
                 x_dir=am.apply_dir(m, d.x_dir))


def datum_dict(d: Datum) -> dict[str, Any]:
    return {"name": d.name, "kind": d.kind, "origin": list(d.origin), "direction": list(d.direction), "x_dir": list(d.x_dir)}


# --------------------------------------------------------------------------- #
# Solver
# --------------------------------------------------------------------------- #


@dataclass
class SolveInstance:
    iid: str
    transform: am.Matrix4
    fixed: bool = False
    datums: dict[str, Datum] | None = None  # None = the definition's datums are unknown
    suppressed: bool = False
    unplaced: bool = False  # no explicit pose was given (spec instance without placement)
    bbox: tuple[Vec, Vec] | None = None  # definition-frame bbox (auto layout)
    name: str = ""


@dataclass
class SolveMate:
    mate_id: str
    kind: str
    a: dict[str, Any]
    b: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    position: int = 0
    suppressed: bool = False


@dataclass
class SolveResult:
    transforms: dict[str, am.Matrix4]
    mates: dict[str, dict[str, Any]]
    instances: dict[str, dict[str, Any]]
    assumptions: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    converged: bool
    max_residual: dict[str, float]

    def to_json(self) -> dict[str, Any]:
        return {"mates": self.mates, "instances": self.instances, "converged": self.converged, "max_residual": self.max_residual,
                "notes": self.notes}


class _Bad(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class _Eq:
    """A bound mate: datum lookups resolved, ready to evaluate."""

    mate: SolveMate
    ia: str
    ib: str | None
    da: list[Datum]
    db: list[Datum]


def _pick(ref: dict[str, Any] | None, insts: dict[str, SolveInstance], kind: str) -> tuple[str, list[Datum]]:
    if not ref or not ref.get("instance"):
        raise _Bad("invalid", "the mate side has no instance")
    iid = str(ref["instance"])
    inst = insts.get(iid)
    if inst is None:
        raise _Bad("invalid", f"instance {iid} is not in this revision")
    if inst.suppressed:
        raise _Bad("suppressed", f"instance {iid} is suppressed")
    name = str(ref.get("datum") or "origin")
    if inst.datums is None:
        raise _Bad("broken", f"the datums of instance {iid}'s definition are unknown")
    if kind == "pattern_match":
        prefix = name.split(".axis")[0]
        found = sorted(((int(n.split("[")[1].rstrip("]")), d) for n, d in inst.datums.items()
                        if n.startswith(prefix + ".axis[") and n.endswith("]") and "/" not in n[len(prefix):]), key=lambda x: x[0])
        if not found:
            raise _Bad("broken", f"datum pattern {prefix}.axis[*] not found on instance {iid}")
        return iid, [d for _, d in found]
    d = inst.datums.get(name)
    if d is None:
        raise _Bad("broken", f"datum {name} not found on instance {iid} (never re-bound to another feature)")
    return iid, [d]


_NEEDS = {
    "coaxial": ({"axis"}, {"axis"}),
    "planar": ({"plane", "frame"}, {"plane", "frame"}),
    "parallel": ({"axis", "plane", "frame"}, {"axis", "plane", "frame"}),
    "perpendicular": ({"axis", "plane", "frame"}, {"axis", "plane", "frame"}),
    "offset": ({"plane", "axis", "point", "frame"}, {"plane", "axis", "point", "frame"}),
    "angle": ({"axis", "plane", "frame"}, {"axis", "plane", "frame"}),
    "pattern_match": ({"axis"}, {"axis"}),
    "fixed": ({"frame", "plane", "axis", "point"}, {"frame", "plane", "axis", "point"}),
}


def _bind(m: SolveMate, insts: dict[str, SolveInstance]) -> _Eq:
    if m.kind not in MATE_KINDS:
        raise _Bad("invalid", f"unknown mate kind {m.kind}")
    ia, da = _pick(m.a, insts, m.kind)
    if m.kind == "fixed" and not m.b:
        return _Eq(m, ia, None, da, [])
    if not m.b:
        raise _Bad("invalid", f"{m.kind} needs two datums")
    ib, db = _pick(m.b, insts, m.kind)
    if ia == ib:
        raise _Bad("invalid", "both sides of the mate are on the same instance")
    ka, kb = _NEEDS[m.kind]
    if da[0].kind not in ka or db[0].kind not in kb:
        raise _Bad("invalid", f"{m.kind} cannot use a {da[0].kind} datum with a {db[0].kind} datum")
    if m.kind == "pattern_match" and len(da) != len(db):
        raise _Bad("invalid", f"hole patterns differ in count ({len(da)} vs {len(db)})")
    if m.kind in ("offset",) and "distance" not in m.params:
        raise _Bad("invalid", "offset needs params.distance (mm)")
    if m.kind == "angle" and "angle" not in m.params:
        raise _Bad("invalid", "angle needs params.angle (degrees)")
    return _Eq(m, ia, ib, da, db)


def _world(d: Datum, m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return m[:3, :3] @ _v(d.origin) + m[:3, 3], m[:3, :3] @ _v(d.direction)


def same_way(kind_a: str, kind_b: str, flip: Any) -> bool:
    """Whether a ``planar`` / ``offset`` mate between two planes or frames points their normals the same way. Two planes
    face each other (opposed normals: face against face); a FRAME datum (a part's ``origin``, an assembly frame) lays
    its XY plane on the other side with its +Z along that side's normal — no turn-over (generation reliability round 3,
    review: a spec relation ``planar <part>.origin ↔ @<assembly>.if.<plane>`` turned the part over). ``params.flip``
    reverses either."""
    return (kind_a == "frame" or kind_b == "frame") != bool(flip)


def _angle_direction(mate: SolveMate) -> float | None:
    """At exactly 0/180 degrees the directed-vector form has the correct first-order rank."""
    if mate.kind != "angle":
        return None
    theta = math.radians(float(mate.params["angle"]))
    return (1.0 if math.cos(theta) >= 0 else -1.0) if abs(math.sin(theta)) < 1e-12 else None


def _residual(eq: _Eq, poses: dict[str, np.ndarray], fixed_rel: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """(position residuals [mm], direction residuals [unitless])."""
    m = eq.mate
    ma = poses[eq.ia]
    if eq.ib is None:
        return np.zeros(0), np.zeros(0)
    mb = poses[eq.ib]
    pa, na = _world(eq.da[0], ma)
    pb, nb = _world(eq.db[0], mb)
    k = m.kind
    if k == "fixed":  # rigid lock of b relative to a at the stored relative pose
        rel = np.linalg.inv(ma) @ mb
        want = fixed_rel[m.mate_id]
        return (rel[:3, 3] - want[:3, 3]), (rel[:3, :3] - want[:3, :3]).reshape(-1)
    if k == "coaxial":
        dirs = np.cross(na, nb)
        pos = np.cross(pb - pa, na)
        if "aligned" in m.params:
            s = 1.0 if m.params.get("aligned") else -1.0
            dirs = np.concatenate([dirs, [float(na @ nb) - s]])
        return pos, dirs
    if k == "pattern_match":
        n = len(eq.da)
        shift = int(m.params.get("shift", 0))
        pos_l, dir_l = [], []
        for i in range(n):
            pa_i, na_i = _world(eq.da[i], ma)
            pb_i, nb_i = _world(eq.db[(i + shift) % n], mb)
            pos_l.append(np.cross(pb_i - pa_i, na_i))
            dir_l.append(np.cross(na_i, nb_i))
        return np.concatenate(pos_l), np.concatenate(dir_l)
    if k in ("planar", "offset") and eq.da[0].kind in ("plane", "frame") and eq.db[0].kind in ("plane", "frame"):
        dirs = (na - nb) if same_way(eq.da[0].kind, eq.db[0].kind, m.params.get("flip")) else (na + nb)
        dist = float(m.params.get("distance", m.params.get("offset", 0.0)))
        return np.array([float((pb - pa) @ na) - dist]), dirs
    if k == "offset":
        dist = float(m.params["distance"])
        if eq.da[0].kind == "plane":
            return np.array([float((pb - pa) @ na) - dist]), np.zeros(0)
        if eq.db[0].kind == "plane":
            return np.array([float((pa - pb) @ nb) - dist]), np.zeros(0)
        if eq.da[0].kind == "axis" and eq.db[0].kind == "axis":
            rej = (pb - pa) - float((pb - pa) @ na) * na
            return np.array([float(np.linalg.norm(rej)) - dist]), np.cross(na, nb)
        return np.array([float(np.linalg.norm(pb - pa)) - dist]), np.zeros(0)
    mixed = (eq.da[0].kind == "axis") != (eq.db[0].kind == "axis")
    if k == "parallel":
        return np.zeros(0), (np.array([float(na @ nb)]) if mixed else np.cross(na, nb))
    if k == "perpendicular":
        return np.zeros(0), (np.cross(na, nb) if mixed else np.array([float(na @ nb)]))
    if k == "angle":
        sign = _angle_direction(m)
        if sign is not None:
            return np.zeros(0), na - sign * nb
        return np.zeros(0), np.array([float(na @ nb) - math.cos(math.radians(float(m.params["angle"])))])
    raise _Bad("invalid", f"{k} is not supported between {eq.da[0].kind} and {eq.db[0].kind}")


def _regularize_tangent_offsets(eqs: list[_Eq]) -> list[_Eq]:
    """Add an equivalent axis equation when two rigid parallel-axis distance circles touch.

    At external/internal tangency the two circle equations have dependent first derivatives, although their
    transverse intersection is a single point. Their unique axis has a regular coaxial residual. The target axes
    must belong to the SAME instance: a separation seen only at the current pose of different supports is not an
    invariant. A free support stays in the derived equation, so its motion still couples to the moving instance.
    These are numerical equations only; original mates retain all conflict priority and persistence semantics.
    """
    groups: dict[tuple[str, str, str], list[tuple[_Eq, Datum, Datum]]] = {}
    for eq in eqs:
        if eq.mate.kind != "offset" or eq.ib is None or eq.da[0].kind != "axis" or eq.db[0].kind != "axis":
            continue
        distance = float(eq.mate.params["distance"])
        if not math.isfinite(distance) or distance < 0:
            continue  # signed/invalid offsets never become positive circle radii
        for own, datum, other, target in ((eq.ia, eq.da[0], eq.ib, eq.db[0]), (eq.ib, eq.db[0], eq.ia, eq.da[0])):
            groups.setdefault((own, datum.name, other), []).append((eq, datum, target))
    derived: list[_Eq] = []
    seen: set[tuple[Any, ...]] = set()
    roundoff = 64 * np.finfo(float).eps
    for (own, name, other), group in groups.items():
        for i, (first, source, one) in enumerate(group):
            for second, _, two in group[i + 1:]:
                _check_budget()
                axis, opposite = _unit(one.direction), _unit(two.direction)
                if np.linalg.norm(axis) < .5 or np.linalg.norm(opposite) < .5 or np.linalg.norm(np.cross(axis, opposite)) > roundoff:
                    continue
                delta = _v(two.origin) - _v(one.origin)
                axial = float(delta @ axis)
                projection_error = 8 * np.finfo(float).eps * (float(np.sum(np.abs(delta))) + abs(axial))
                delta = delta - axial * axis
                separation = float(np.linalg.norm(delta))
                r1, r2 = float(first.mate.params["distance"]), float(second.mate.params["distance"])
                epsilon = roundoff * separation
                external = abs(separation - (r1 + r2)) <= epsilon
                internal = abs(separation - abs(r1 - r2)) <= epsilon
                if projection_error > epsilon or separation <= epsilon or not (external or internal):
                    continue  # coincident, near-tangent and two-intersection circles remain unchanged
                along = (r1 if external or r1 >= r2 else -r1) / separation
                origin = _v(one.origin) + along * delta
                signature = (own, name, other, *origin, *axis)
                if signature in seen:
                    continue
                seen.add(signature)
                target = Datum("internal tangent axis", "axis", tuple(float(x) for x in origin), tuple(float(x) for x in axis))
                mate = SolveMate(f"internal tangent {first.mate.mate_id} {second.mate.mate_id}", "coaxial",
                                 {"instance": own, "datum": name}, {"instance": other, "datum": target.name},
                                 priority=max(first.mate.priority, second.mate.priority),
                                 position=max(first.mate.position, second.mate.position))
                derived.append(_Eq(mate, own, other, [source], [target]))
    return eqs + derived


def _pose(base: np.ndarray, x: np.ndarray) -> np.ndarray:
    """base moved by x = (rotation vector about its own origin, translation)."""
    c = base[:3, 3]
    r = rodrigues(x[:3])
    out = np.eye(4)
    out[:3, :3] = r @ base[:3, :3]
    out[:3, 3] = c + x[3:]
    return out


def _construct(eqs: list[_Eq], poses: dict[str, np.ndarray], placed: set[str], free: set[str]) -> None:
    """Greedy initial placement: align each instance to the first mate that ties it to a placed one."""
    def signed(eq: _Eq) -> bool:  # mates that fix the orientation sign, not only the direction line
        k = eq.mate.kind
        return (k in ("planar", "offset") and eq.da[0].kind in ("plane", "frame")
                or (k == "coaxial" and "aligned" in eq.mate.params) or _angle_direction(eq.mate) is not None)

    ordered = sorted(eqs, key=lambda e: (not signed(e), e.mate.priority, e.mate.position))

    def directed_rotation(dst: str) -> np.ndarray | None:
        """Fit the directed normals together; one-plane seeding can leave another exactly antiparallel.

        At that 180-degree pose the vector residual has zero first-order angular gradient. Only use a consistent
        rigid rotation from at least two independent directions; incompatible constraints still go through the
        ordinary priority/residual handling. This is an initializer, never a relaxed acceptance test.
        """
        local, world = [], []
        for e in ordered:
            if not signed(e):
                continue
            if e.ia == dst and e.ib in placed:
                own, other, ref = e.da[0], e.db[0], e.ib
            elif e.ib == dst and e.ia in placed:
                own, other, ref = e.db[0], e.da[0], e.ia
            else:
                continue
            if e.mate.kind not in ("coaxial", "angle") and not (own.kind in ("plane", "frame") and other.kind in ("plane", "frame")):
                continue  # plane-to-point/axis distance has no orientation residual
            _, target = _world(other, poses[ref])
            same = (_angle_direction(e.mate) == 1.0 if e.mate.kind == "angle" else
                    bool(e.mate.params.get("aligned")) if e.mate.kind == "coaxial" else
                    same_way(own.kind, other.kind, e.mate.params.get("flip")))
            local.append(_v(own.direction))
            world.append(target if same else -target)
        if len(local) < 2 or np.linalg.matrix_rank(np.array(local), tol=1e-6) < 2:
            return None
        a, b = np.array(local), np.array(world)
        u, _, vt = np.linalg.svd(b.T @ a)
        correction = np.diag([1.0, 1.0, np.linalg.det(u @ vt)])
        rotation = u @ correction @ vt
        return rotation if np.max(np.abs(a @ rotation.T - b)) < 1e-6 else None

    for _ in range(len(eqs) + 1):
        _check_budget()
        progress = False
        for eq in ordered:
            if eq.ib is None:
                continue
            if eq.ia in placed and eq.ib in free and eq.ib not in placed:
                src, dst, d_src, d_dst = eq.ia, eq.ib, eq.da[0], eq.db[0]
            elif eq.ib in placed and eq.ia in free and eq.ia not in placed:
                src, dst, d_src, d_dst = eq.ib, eq.ia, eq.db[0], eq.da[0]
            else:
                continue
            k = eq.mate.kind
            angle_sign = _angle_direction(eq.mate)
            if k not in ("coaxial", "planar", "offset", "pattern_match", "parallel") and angle_sign is None:
                continue
            if k in ("parallel", "offset") and (d_src.kind == "axis") != (d_dst.kind == "axis"):
                continue
            ps, ns = _world(d_src, poses[src])
            pd, nd = _world(d_dst, poses[dst])
            target = ns
            same = same_way(d_src.kind, d_dst.kind, eq.mate.params.get("flip"))
            if angle_sign is not None:
                target = angle_sign * ns
            elif k in ("planar", "offset") and d_src.kind in ("plane", "frame") and d_dst.kind in ("plane", "frame"):
                target = ns if same else -ns
            elif k == "coaxial" and eq.mate.params.get("aligned") is False:
                target = -ns
            elif k in ("coaxial", "parallel", "pattern_match") and float(nd @ ns) < 0 and "aligned" not in eq.mate.params:
                target = -ns  # keep the nearer orientation
            rot = _rot_between(nd, target)
            fit = directed_rotation(dst)
            if fit is not None:
                rot = fit @ poses[dst][:3, :3].T
            m = poses[dst].copy()
            c = m[:3, 3].copy()
            m[:3, :3] = rot @ m[:3, :3]
            pd = rot @ (pd - c) + c
            if k in ("coaxial", "pattern_match"):
                delta = (pd - ps) - float((pd - ps) @ ns) * ns
                m[:3, 3] = c - delta
            elif k in ("planar", "offset") and d_src.kind in ("plane", "frame"):
                dist = float(eq.mate.params.get("distance", eq.mate.params.get("offset", 0.0)))
                if src == eq.ib and same:
                    dist = -dist  # the distance runs along a's normal; with the normals the same way it reverses from b
                m[:3, 3] = c - (float((pd - ps) @ ns) - dist) * ns
            poses[dst] = m
            placed.add(dst)
            progress = True
            # This support can make an earlier directed contact eligible. Rescan before an unsigned axis mate
            # picks the opposite branch for its child and prevents that contact from initializing it later.
            break
        if not progress:
            break


def _stack(eqs: list[_Eq], poses: dict[str, np.ndarray], fixed_rel: dict[str, np.ndarray]) -> np.ndarray:
    parts = []
    for eq in eqs:
        pos, dirs = _residual(eq, poses, fixed_rel)
        parts.append(pos)
        parts.append(dirs * 100.0)  # 0.01 rad ~ 1 mm: comparable scales for the least squares
    return np.concatenate(parts) if parts else np.zeros(0)


class _ResidualSystem:
    """Same joint residuals, with finite differences only on an instance's dependent rows.

    A free support still couples all of its children in the joint Jacobian. Its
    derivatives must remain in those rows; unrelated instances have exact zero
    derivatives and do not require reevaluating the whole assembly.
    """

    def __init__(self, eqs: list[_Eq], poses: dict[str, np.ndarray], free: list[str],
                 fixed_rel: dict[str, np.ndarray]):
        self.eqs, self.poses, self.free, self.fixed_rel = eqs, poses, free, fixed_rel
        self.base = {iid: poses[iid].copy() for iid in free}
        self.rows: dict[str, list[tuple[_Eq, slice]]] = {iid: [] for iid in free}
        offset = 0
        for eq in eqs:
            _check_budget()
            pos, dirs = _residual(eq, poses, fixed_rel)
            span = slice(offset, offset + pos.size + dirs.size)
            offset = span.stop
            for iid in {eq.ia, eq.ib} & self.rows.keys():
                self.rows[iid].append((eq, span))
        self.size = offset

    def at(self, x: np.ndarray) -> dict[str, np.ndarray]:
        cur = dict(self.poses)
        for k, iid in enumerate(self.free):
            cur[iid] = _pose(self.base[iid], x[6 * k:6 * k + 6])
        return cur

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return _stack(self.eqs, self.at(x), self.fixed_rel)

    def jacobian(self, x: np.ndarray, h: float) -> np.ndarray:
        j = np.zeros((self.size, x.size))
        current = self.at(x)
        for k, iid in enumerate(self.free):
            rows = self.rows[iid]
            for axis in range(6):
                _check_budget()
                delta = np.zeros(6)
                delta[axis] = h
                segment = x[6 * k:6 * k + 6]
                plus, minus = dict(current), dict(current)
                plus[iid] = _pose(self.base[iid], segment + delta)
                minus[iid] = _pose(self.base[iid], segment - delta)
                for eq, span in rows:
                    p1, d1 = _residual(eq, plus, self.fixed_rel)
                    p0, d0 = _residual(eq, minus, self.fixed_rel)
                    # Match _stack's scaling before subtraction exactly.
                    j[span, 6 * k + axis] = (np.concatenate((p1, d1 * 100.0))
                                             - np.concatenate((p0, d0 * 100.0))) / (2 * h)
        return j


def _lm(eqs: list[_Eq], poses: dict[str, np.ndarray], free: list[str], fixed_rel: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    _check_budget()
    blocks = _free_blocks(eqs, free)
    if len(blocks) > 1:
        result = dict(poses)
        for block in blocks:
            mine = [eq for eq in eqs if eq.ia in block or eq.ib in block]
            result = _lm(mine, result, block, fixed_rel)
        return result
    if not free or not eqs:
        return poses
    x = np.zeros(6 * len(free))
    f = _ResidualSystem(eqs, poses, free, fixed_rel)
    r = f(x)
    lam = 1e-3
    for _ in range(MAX_ITER):
        _check_budget()
        if r.size == 0 or float(np.max(np.abs(r))) < 1e-10:
            break
        j = _jac(f, x, r)
        g = j.T @ r
        h = j.T @ j
        # Marquardt scaling with a floor: directions the residuals barely see stay strongly damped
        # (minimal-norm steps for under-determined systems, no wild rotations along near-null directions)
        dg = np.diag(h)
        damp = np.diag(np.maximum(dg, 1e-2 * float(dg.max()) + 1e-12))
        improved = False
        for _ in range(20):
            _check_budget()
            step = np.linalg.solve(h + lam * damp, -g)
            r2 = f(x + step)
            if float(r2 @ r2) < float(r @ r):
                x, r = x + step, r2
                lam = max(lam / 3, 1e-12)
                improved = True
                break
            lam *= 4
        if not improved or float(np.max(np.abs(step))) < 1e-13:
            break
    return f.at(x)


def _jac(f: Any, x: np.ndarray, r: np.ndarray, h: float = 1e-7) -> np.ndarray:
    if isinstance(f, _ResidualSystem):
        return f.jacobian(x, h)
    j = np.zeros((r.size, x.size))
    for i in range(x.size):
        _check_budget()
        dx = np.zeros_like(x)
        dx[i] = h
        j[:, i] = (f(x + dx) - f(x - dx)) / (2 * h)
    return j


def _mate_residual(eq: _Eq, poses: dict[str, np.ndarray], fixed_rel: dict[str, np.ndarray]) -> dict[str, float]:
    pos, dirs = _residual(eq, poses, fixed_rel)
    p = float(np.max(np.abs(pos))) if pos.size else 0.0
    # direction residuals are chord lengths of unit vectors: angle ~ asin
    a = float(np.max(np.abs(dirs))) if dirs.size else 0.0
    return {"position_mm": round(p, 9) + 0.0, "angle_rad": round(math.asin(min(1.0, a)) if a <= 1 else a, 12) + 0.0}


def _ok(res: dict[str, float]) -> bool:
    return res["position_mm"] <= POS_TOL and res["angle_rad"] <= ANG_TOL


def _free_motions(eqs: list[_Eq], poses: dict[str, np.ndarray], iid: str, fixed_rel: dict[str, np.ndarray]) -> tuple[int, list[dict[str, Any]]]:
    """Local DOF of one instance with every other instance held: 6 - rank(J_i)."""
    _check_budget()
    mine = [eq for eq in eqs if iid in (eq.ia, eq.ib)]
    if not mine:
        return 6, [{"motion": "translate", "axis": ax} for ax in ([1, 0, 0], [0, 1, 0], [0, 0, 1])] + \
               [{"motion": "rotate", "axis": ax} for ax in ([1, 0, 0], [0, 1, 0], [0, 0, 1])]
    base = poses[iid].copy()

    def f(xv: np.ndarray) -> np.ndarray:
        cur = dict(poses)
        cur[iid] = _pose(base, xv)
        return _stack(mine, cur, fixed_rel)

    x0 = np.zeros(6)
    r0 = f(x0)
    if r0.size == 0:
        return 6, []
    j = _jac(f, x0, r0, 1e-6)
    _, s, vt = np.linalg.svd(j)
    smax = float(s[0]) if s.size else 0.0
    rank = int(np.sum(s > max(RANK_TOL, RANK_TOL * smax)))
    null = vt[rank:]
    motions: list[dict[str, Any]] = []
    for v in null:
        w, t = v[:3], v[3:]
        if np.linalg.norm(w) > 1e-6:
            motions.append({"motion": "rotate", "axis": [round(float(c), 6) + 0.0 for c in _unit(w)]})
        else:
            motions.append({"motion": "translate", "axis": [round(float(c), 6) + 0.0 for c in _unit(t)]})
    return 6 - rank, motions


def _located(eqs: list[_Eq], poses: dict[str, np.ndarray], free: list[str], fixed_rel: dict[str, np.ndarray]) -> set[str]:
    """The free instances whose POSITION the applied mates fix relative to the fixed ones: among all the motions of the
    free instances together that keep every mate satisfied (the null space of the joint Jacobian), none moves the
    instance without turning it. A free turn about an axis (a coaxial part's spin, its origin off the axis or not) leaves
    it located; a free slide — along a coaxial axis, in a contact plane, a group placed only relative to each other —
    does not (generation reliability round 3, review: "grounded" meant connected, not positioned)."""
    _check_budget()
    blocks = _free_blocks(eqs, free)
    if len(blocks) > 1:
        return set().union(*(_located([eq for eq in eqs if eq.ia in block or eq.ib in block], poses, block, fixed_rel)
                             for block in blocks))
    if not free or not eqs:
        return set()
    f = _ResidualSystem(eqs, poses, free, fixed_rel)
    x0 = np.zeros(6 * len(free))
    r0 = f(x0)
    if r0.size == 0:
        return set()
    j = _jac(f, x0, r0, 1e-6)
    _, s, vt = np.linalg.svd(j)
    smax = float(s[0]) if s.size else 0.0
    null = vt[int(np.sum(s > max(RANK_TOL, RANK_TOL * smax))):]

    def rank(m: np.ndarray) -> int:
        sv = np.linalg.svd(m, compute_uv=False) if m.size else np.zeros(0)
        return int(np.sum(sv > 1e-6))

    out: set[str] = set()
    for k, iid in enumerate(free):
        block = null[:, 6 * k:6 * k + 6]
        if rank(block) == rank(block[:, :3]):  # every allowed motion of it turns it: no pure translation is left free
            out.add(iid)
    return out


def _free_blocks(eqs: list[_Eq], free: list[str]) -> list[list[str]]:
    """A fixed support is a constant, not a coupling between otherwise independent free-instance variables."""
    wanted = set(free)
    groups = relation_groups([(eq.ia, eq.ib) for eq in eqs if eq.ia in wanted and eq.ib in wanted])
    grouped = {key for group in groups for key in group}
    return [sorted(group) for group in groups] + [[key] for key in free if key not in grouped]


def compatible(kind: str, a_kind: str, b_kind: str) -> str | None:
    """Why a ``kind`` mate cannot join a datum of kind ``a_kind`` with one of ``b_kind``; None when it can (the same
    table :func:`solve` binds with)."""
    if kind not in MATE_KINDS:
        return f"unknown mate kind {kind}"
    ka, kb = _NEEDS[kind]
    if a_kind not in ka or b_kind not in kb:
        return f"{kind} cannot use a {a_kind} datum with a {b_kind} datum"
    return None


def grounded(result: SolveResult) -> set[str]:
    """Instances POSITIONED in the frame of the solve, from the solver's own report (``instances[…].grounded``): the
    fixed ones (``fixed`` flag or a one-ended ``fixed`` mate) and those whose translation the applied mates fix relative
    to them (:func:`_located`; a free turn about an axis is held and recorded, a free slide is not positioned). Connected
    is not positioned: a part only ``parallel`` to a fixed one, a group placed only relative to each other
    (``mates_not_grounded``) are not grounded."""
    return {i for i, info in result.instances.items() if info.get("grounded")}


def relation_groups(edges: list[tuple[str, str]] | list[tuple[str, str | None]]) -> list[list[str]]:
    """The connected groups of instances joined by two-ended mates / relations (each group in first-seen order, the
    groups in the order their first instance appears). Pure; one-ended entries (``b`` None) join nothing."""
    adj: dict[str, list[str]] = {}
    for a, b in edges:
        if not a or not b or a == b:
            continue
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen: set[str] = set()
    out: list[list[str]] = []
    for start in adj:
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            x = stack.pop(0)
            group.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        out.append(group)
    return out


def _cache_signature(instances: list[SolveInstance], mates: list[SolveMate], keys: dict[str, str], turbo: bool) -> str | None:
    """Exact equation inputs, independent of stored UUIDs and unused datum aliases; display boxes are recomputed."""
    insts = {i.iid: i for i in instances}
    all_ids = [i.iid for i in instances] + [m.mate_id for m in mates]
    if len({keys.get(k, k) for k in all_ids}) != len(all_ids):
        return None
    rows = []
    for mate in mates:
        try:
            eq = _bind(mate, insts)
        except _Bad:
            return None  # broken/unavailable geometry is not a reusable verification

        def datum(d: Datum) -> dict[str, Any]:
            return {k: v for k, v in asdict(d).items() if k != "name"}

        rows.append({"id": keys.get(mate.mate_id, mate.mate_id), "kind": mate.kind,
                     "a": [keys.get(eq.ia, eq.ia), (mate.a or {}).get("datum"), [datum(d) for d in eq.da]],
                     "b": [keys.get(eq.ib, eq.ib), (mate.b or {}).get("datum"), [datum(d) for d in eq.db]],
                     "params": mate.params, "priority": mate.priority, "position": mate.position, "suppressed": mate.suppressed})
    data = {"instances": [{"id": keys.get(i.iid, i.iid), "transform": i.transform, "fixed": i.fixed,
                            "suppressed": i.suppressed, "unplaced": i.unplaced} for i in instances], "mates": rows, "turbo": turbo}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cached_result(saved: SolveResult, old_keys: dict[str, str], keys: dict[str, str],
                   instances: list[SolveInstance], turbo: bool) -> SolveResult:
    inverse = {canonical: actual for actual, canonical in keys.items()}
    remap = {old: inverse.get(canonical, canonical) for old, canonical in old_keys.items()}
    result = copy.deepcopy(saved)
    result.transforms = {remap.get(k, k): t for k, t in result.transforms.items()}
    result.instances = {remap.get(k, k): value for k, value in result.instances.items()}
    result.mates = {remap.get(k, k): value for k, value in result.mates.items()}
    # Only schema fields that carry object IDs are mapped. IDs may themselves be words such as "fixed" or "free".
    for mate in result.mates.values():
        if mate.get("grounds"):
            mate["grounds"] = remap.get(mate["grounds"], mate["grounds"])
    for note in result.notes:
        if "instances" in note:
            note["instances"] = [remap.get(k, k) for k in note["instances"]]
    for assumption in result.assumptions:
        if "subject" in assumption:
            assumption["subject"] = remap.get(assumption["subject"], assumption["subject"])
        if "affected" in assumption:
            assumption["affected"] = [remap.get(k, k) for k in assumption["affected"]]
        if assumption.get("field") == "mate":  # conflict alternatives are mate IDs; motion/basis text is not
            assumption["alternatives"] = [remap.get(k, k) if isinstance(k, str) else k
                                           for k in assumption.get("alternatives") or []]
    # Display placement is not an equation. Current part/assembly boxes can change its spacing independently.
    reset = {k for k, info in result.instances.items() if info.get("status") == "auto_placed"}
    if reset:
        result.assumptions = [a for a in result.assumptions if not (a.get("subject") in reset and a.get("author") == "rule"
                                                                 and a.get("field") == "placement")]
        poses = {k: _np4(t) for k, t in result.transforms.items()}
        for instance in instances:
            if instance.iid in reset:
                poses[instance.iid] = _np4(instance.transform)
                result.instances[instance.iid] = {"status": "unplaced", "dof": 6, "free": [], "grounded": False}
        if turbo:
            _auto_layout(instances, poses, result.instances, result.assumptions)
        result.transforms = {k: _mat4(t[:3, :3], t[:3, 3]) for k, t in poses.items()}
    return result


def solve(instances: list[SolveInstance], mates: list[SolveMate], *, turbo: bool = False,
          cache_keys: dict[str, str] | None = None) -> SolveResult:
    """Solve within the caller's budget; an interrupted trial never counts as installed."""
    cache = _SOLVE_CACHE.get()
    keys = {k: (cache_keys or {}).get(k, k) for k in [i.iid for i in instances] + [m.mate_id for m in mates]}
    signature = _cache_signature(instances, mates, keys, turbo) if cache is not None else None
    if signature is not None and signature in cache:
        saved, old_keys = cache[signature]
        result = _cached_result(saved, old_keys, keys, instances, turbo)
        result.notes.append({"code": "verified_solver_reused", "signature": signature,
                             "message": "reused a verified solve of the same bound datums, constraints and initial states"})
        return result
    try:
        _check_budget()
        result = _solve(instances, mates, turbo=turbo)
        if signature is not None and result.converged:
            if len(cache) >= 256:
                cache.pop(next(iter(cache)))
            cache[signature] = (copy.deepcopy(result), keys)
        return result
    except SolveStopped as exc:
        # Use only explicit fixed states. Even a numerically plausible intermediate pose has not passed rank/residual checks.
        token = _SOLVE_BUDGET.set((None, None))
        try:
            fixed = [mate for mate in mates if mate.kind == "fixed" and not mate.b]
            result = _solve(instances, fixed, turbo=turbo)
        finally:
            _SOLVE_BUDGET.reset(token)
        for mate in mates:
            result.mates.setdefault(mate.mate_id, {"status": "pending", "message": str(exc), "code": str(exc)})
        result.converged = False
        result.notes.append({"code": str(exc), "message": "mate solving stopped; unverified instances remain unpositioned",
                             "instances": sorted(i for i, state in result.instances.items() if not state.get("grounded"))})
        return result


def _solve(instances: list[SolveInstance], mates: list[SolveMate], *, turbo: bool = False) -> SolveResult:
    """Place the free instances so the active mates hold (see module doc). Transforms of fixed instances and
    of instances without mates are returned unchanged."""
    insts = {i.iid: i for i in instances}
    poses = {i.iid: _np4(i.transform) for i in instances}
    mate_out: dict[str, dict[str, Any]] = {}
    assumptions: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    grounded = {i.iid for i in instances if i.fixed and not i.suppressed}
    eqs: list[_Eq] = []
    ordered = sorted(mates, key=lambda m: (m.priority, m.position))
    for m in ordered:
        if m.suppressed:
            mate_out[m.mate_id] = {"status": "suppressed"}
            continue
        try:
            eq = _bind(m, insts)
        except _Bad as exc:
            mate_out[m.mate_id] = {"status": exc.status, "message": exc.message}
            continue
        if eq.ib is None:  # fixed / ground on one instance
            grounded.add(eq.ia)
            mate_out[m.mate_id] = {"status": "solved", "residual": {"position_mm": 0.0, "angle_rad": 0.0}, "grounds": eq.ia}
            continue
        eqs.append(eq)
    fixed_rel = {eq.mate.mate_id: np.linalg.inv(poses[eq.ia]) @ poses[eq.ib] for eq in eqs if eq.mate.kind == "fixed"}
    involved = {eq.ia for eq in eqs} | {eq.ib for eq in eqs if eq.ib}
    free = sorted(involved - grounded)
    placed = set(grounded)
    # generation reliability P (floating groups): every mated group without a fixed member keeps its rigid-body freedom —
    # its first instance is the reference the others are laid out from, and the group is reported on its own
    for group in relation_groups([(eq.ia, eq.ib) for eq in eqs if eq.ib]):
        if not set(group) & grounded:
            placed.add(sorted(group)[0])
            notes.append({"code": "mates_not_grounded", "message": "no instance of this mated group is fixed: the group keeps its "
                          "rigid-body freedom (positioned only relative to each other)", "instances": sorted(group)})
    active = list(eqs)
    dropped: list[str] = []
    converged = True
    for _ in range(len(eqs) + 1):
        _check_budget()
        trial = dict(poses)
        effective = _regularize_tangent_offsets(active)
        _construct(effective, trial, set(placed), set(free))
        trial = _lm(effective, trial, free, fixed_rel)
        bad = [eq for eq in active if not _ok(_mate_residual(eq, trial, fixed_rel))]
        if not bad:
            poses = trial
            break
        # drop the least important conflicting mate (largest priority number, then latest) and solve again
        victim = max(bad, key=lambda e: (e.mate.priority, e.mate.position))
        res = _mate_residual(victim, trial, fixed_rel)
        active = [eq for eq in active if eq is not victim]
        dropped.append(victim.mate.mate_id)
        mate_out[victim.mate.mate_id] = {"status": "dropped" if turbo else "conflict", "residual": res,
                                         "message": "conflicts with higher-priority mates; not applied to the pose"}
        if turbo:
            assumptions.append({"subject": victim.mate.mate_id, "field": "mate", "adopted": "dropped",
                                "basis": f"conflicting {victim.mate.kind} mate with the lowest priority "
                                         f"(priority {victim.mate.priority}); residual {res['position_mm']:.4g} mm / {res['angle_rad']:.4g} rad",
                                "alternatives": [eq.mate.mate_id for eq in bad if eq is not victim], "author": "ai_turbo",
                                "trigger": "conflict", "affected": [victim.mate.mate_id, *[i for i in (victim.ia, victim.ib) if i]]})
    else:  # pragma: no cover - bounded by the number of mates
        converged = False
    for eq in eqs:  # residual of a conflicting / dropped mate at the pose actually adopted
        st = mate_out.get(eq.mate.mate_id)
        if st is not None and st.get("status") in ("conflict", "dropped"):
            st["residual"] = _mate_residual(eq, poses, fixed_rel)
    worst = {"position_mm": 0.0, "angle_rad": 0.0}
    for eq in active:
        _check_budget()
        res = _mate_residual(eq, poses, fixed_rel)
        worst = {k: max(worst[k], res[k]) for k in worst}
        mate_out[eq.mate.mate_id] = {"status": "solved", "residual": res}
    inst_out: dict[str, dict[str, Any]] = {}
    located = _located(effective, poses, free, fixed_rel)
    for inst in instances:
        iid = inst.iid
        if inst.suppressed:
            inst_out[iid] = {"status": "suppressed", "dof": None, "free": [], "grounded": False}
            continue
        if iid in grounded:
            inst_out[iid] = {"status": "fixed", "dof": 0, "free": [], "grounded": True}
            continue
        if iid not in involved:
            if inst.unplaced:
                inst_out[iid] = {"status": "unplaced", "dof": 6, "free": [], "message": "no placement and no mates", "grounded": False}
            else:
                inst_out[iid] = {"status": "floating", "dof": 6, "free": [], "message": "explicit pose, not fixed and not mated",
                                 "grounded": False}
            continue
        dof, motions = _free_motions(effective, poses, iid, fixed_rel)
        # grounded: positioned in the frame of the solve (fixed, or its translation fixed by the applied mates)
        inst_out[iid] = {"status": "under_constrained" if dof > 0 else "constrained", "dof": dof, "free": motions,
                         "grounded": iid in located}
        if any(mate_out.get(eq.mate.mate_id, {}).get("status") in ("conflict", "dropped") for eq in eqs if iid in (eq.ia, eq.ib)):
            inst_out[iid]["over_constrained"] = True
    # redundancy: a solved mate whose equations add no rank to its instances
    for eq in active:
        targets = [i for i in (eq.ia, eq.ib) if i and i not in grounded]
        if not targets:
            continue
        rest = _regularize_tangent_offsets([e for e in active if e is not eq])
        if all(inst_out[t]["dof"] == _free_motions(rest, poses, t, fixed_rel)[0] for t in targets):
            mate_out[eq.mate.mate_id]["status"] = "redundant"
            mate_out[eq.mate.mate_id]["message"] = "consistent but adds no constraint (over-defined)"
    # Turbo never leaves unplaced parts piled at the origin: a display area beside the placed parts, one rule decision each
    if turbo:
        _auto_layout(instances, poses, inst_out, assumptions)
        for iid, info in inst_out.items():
            if info["status"] == "under_constrained":
                assumptions.append({"subject": iid, "field": "dof", "adopted": "held at the solved pose",
                                    "basis": f"{info['dof']} remaining degree(s) of freedom fixed at the current pose",
                                    "alternatives": info["free"], "author": "ai_turbo", "trigger": "missing", "affected": [iid]})
    elif any(info["status"] == "unplaced" for info in inst_out.values()):
        notes.append({"code": "unplaced_instances", "message": "instances without a placement or mates stay at the origin; place or mate them",
                      "instances": sorted(i for i, info in inst_out.items() if info["status"] == "unplaced")})
    transforms = {iid: _mat4(m[:3, :3], m[:3, 3]) for iid, m in poses.items()}
    return SolveResult(transforms=transforms, mates=mate_out, instances=inst_out, assumptions=assumptions, notes=notes,
                       converged=converged, max_residual=worst)


#: Display area of instances with neither a placement nor a mate (Turbo): the least gap between items, and between
#: the area and the placed content (mm).
LAYOUT_MIN_GAP = 5.0
LAYOUT_AREA_GAP = 10.0


def _auto_layout(instances: list[SolveInstance], poses: dict[str, np.ndarray], inst_out: dict[str, dict[str, Any]],
                 assumptions: list[dict[str, Any]]) -> None:
    """Turbo: instances with neither a placement nor a mate are shown in a display area beside the placed content
    (generation supplement B-02, design §3 B.2): never piled at the origin, never one row that looks like an assembly.
    Rows of ⌈√n⌉ items on the +Y side of the placed parts' bounding box, bottoms on its lowest Z, each item spaced from
    its neighbours by half its own largest size (at least :data:`LAYOUT_MIN_GAP`). One ``rule`` decision per instance
    (trigger ``missing``): a layout preview, not an installed position (the build counts it as arranged)."""
    pending = [i for i in instances if inst_out.get(i.iid, {}).get("status") == "unplaced"]
    if not pending:
        return
    placed = [i for i in instances if i.bbox and inst_out.get(i.iid, {}).get("status") not in ("unplaced", "suppressed")]
    area_lo, area_hi = np.zeros(3), np.zeros(3)
    if placed:
        pts = []
        for i in placed:
            lo, hi = _v(i.bbox[0]), _v(i.bbox[1])  # type: ignore[index]
            corners = np.array([[a, b, c] for a in (lo[0], hi[0]) for b in (lo[1], hi[1]) for c in (lo[2], hi[2])])
            pts.append(corners @ poses[i.iid][:3, :3].T + poses[i.iid][:3, 3])
        allp = np.vstack(pts)
        area_lo, area_hi = allp.min(axis=0), allp.max(axis=0)
    boxes = [(_v(i.bbox[0]), _v(i.bbox[1])) if i.bbox else (np.zeros(3), np.zeros(3)) for i in pending]
    gaps = [max(LAYOUT_MIN_GAP, 0.5 * float((hi - lo).max())) for lo, hi in boxes]
    per_row = max(1, math.ceil(math.sqrt(len(pending))))
    beside = "the placed parts" if placed else "the assembly origin (nothing is placed yet)"
    y = float(area_hi[1]) + max(LAYOUT_AREA_GAP, max(gaps))
    for r0 in range(0, len(pending), per_row):
        row = list(range(r0, min(r0 + per_row, len(pending))))
        x = float(area_lo[0])
        depth = 0.0
        for col, k in enumerate(row):
            i, (lo, hi), gap = pending[k], boxes[k], gaps[k]
            t = np.array([x - lo[0], y - lo[1], float(area_lo[2]) - lo[2]])
            m = np.eye(4)
            m[:3, 3] = t
            poses[i.iid] = m
            x += float(hi[0] - lo[0]) + gap
            depth = max(depth, float(hi[1] - lo[1]))
            inst_out[i.iid] = {"status": "auto_placed", "dof": 6, "free": [],
                               "message": "display arrangement by rule (Turbo): a layout preview, not an installed position"}
            assumptions.append({
                "subject": i.iid, "field": "placement", "trigger": "missing", "author": "rule", "unit": "mm",
                "adopted": {"translate": [round(float(c), 6) + 0.0 for c in t], "rotate": [0.0, 0.0, 0.0]},
                "basis": (f"no placement and no relation in the spec: shown in a display area on the +Y side of {beside}, "
                          f"row {r0 // per_row + 1} column {col + 1}, {gap:g} mm from its neighbour (half its own largest size); "
                          "a layout preview, not an installed position"),
                "alternatives": [], "affected": [i.iid]})
        y += depth + max(gaps[k] for k in row)
