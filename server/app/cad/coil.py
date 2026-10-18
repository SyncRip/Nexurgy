"""Helical coil law of the ``coil`` op (springs, coils, split lock washers) — pure Python, no geometry kernel.

Shared by the op validation (:mod:`app.cad.ops`), the kernel, datums, the independent feature check
(:mod:`app.cad.feature_checks`) and the requirement checker (:mod:`app.cad.conformance_w3`), so all of them mean the
same coil while the kernel builds it with OCCT and the checks measure it independently.

Local frame: origin on the coil axis in its seating plane, z along the axis, x the reference direction of the start
angle. The centre line is ``P(t) = (R cos θ(t), R sin θ(t), z(t))`` for the turn parameter ``t ∈ [0, n]`` (``n`` =
total turns): ``θ(t) = θ0 + h·2π·t`` (``h`` = +1 right-hand: the coil climbs counter-clockwise seen from +z; −1
left-hand) and ``z(t) = z0 + ∫ pitch dt``. The section (a circle, or a rectangle ``s_rad`` across the radius by
``s_ax`` along the axis) lies in the plane normal to the centre line, centred on it; its first axis is the principal
normal pointing away from the axis (radially outward on a constant-pitch stretch), its second axis ≈ the coil axis.
The sweep carries it in the Frenet frame, so on a constant-pitch stretch the wire is a rigid screw motion of the
section and its volume is ``area × centre-line length`` exactly.

End forms (the usual compression-spring length formulas, e.g. DIN EN 13906-1 / Shigley table 10-1, written with the
wire's axial size ``s``; ``cos α`` is the cosine of the helix angle, ≈ 1):

- ``open`` (plain ends): constant pitch ``p``; overall length ``L = p·n + s·cos α`` (≈ ``p·n + d``).
- ``closed`` (squared ends): one closed turn at each end, pitched to leave a 1 % gap between neighbouring turns
  measured across the wire (``p_c = 1.01·s / cos α_c``: no wire contact, so the solid stays valid), the pitch ramping
  linearly to the active pitch over a quarter turn centred where the closed turn ends;
  ``L = 2·p_c + p·(n − 2) + s·cos α_c`` (≈ ``p·n_a + 3d``).
- ``closed_ground`` (squared and ground): the closed coil with its centre line running from z = 0 to z = L, cut flat
  by the planes z = 0 and z = L (about half a turn of each end coil is ground); ``L = 2·p_c + p·(n − 2)``
  (≈ ``p·n_a + 2d``).

The seating plane is z = 0 and the top is z = L for every form. Given ``pitch`` the length follows; given ``length``
the active pitch follows (so a stated free length is kept exactly). Neighbouring turns never touch: with more than one
turn the active pitch must leave the same 1 % gap (``p ≥ p_c``). Units mm and degrees.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

V3 = tuple[float, float, float]
EndForm = Literal["open", "closed", "closed_ground"]
END_FORMS: tuple[str, ...] = ("open", "closed", "closed_ground")

CLOSED_TURNS = 1.0  # closed turns at each end of the closed forms
RAMP = 0.25  # turns over which the pitch changes from a closed turn to the active pitch
CLEARANCE = 0.01  # gap between neighbouring turns, as a fraction of the wire's axial size (measured across the wire)
MIN_TURNS = 0.05
MAX_TURNS = 100.0
MIN_ACTIVE = 0.5  # closed forms: active turns at least


@lru_cache(maxsize=None)
def _gauss_legendre(n: int) -> tuple[tuple[float, float], ...]:
    """(node, weight) pairs of the n-point Gauss–Legendre rule on [-1, 1]."""
    out = []
    for i in range(1, n + 1):
        x = math.cos(math.pi * (i - 0.25) / (n + 0.5))
        for _ in range(100):
            p0, p1 = 1.0, x
            for k in range(2, n + 1):
                p0, p1 = p1, ((2 * k - 1) * x * p1 - (k - 1) * p0) / k
            dp = n * (x * p1 - p0) / (x * x - 1.0)
            dx = p1 / dp
            x -= dx
            if abs(dx) < 1e-15:
                break
        out.append((x, 2.0 / ((1.0 - x * x) * dp * dp)))
    return tuple(out)


def integrate(f: Callable[[float], float], a: float, b: float, n: int = 16) -> float:
    if b <= a:
        return 0.0
    h, c = (b - a) / 2.0, (a + b) / 2.0
    return h * sum(w * f(c + h * x) for x, w in _gauss_legendre(n))


def cos_helix(radius: float, pitch: float) -> float:
    """cos α of a helix of this radius and pitch (α = helix angle against the plane normal to the axis)."""
    c = 2.0 * math.pi * radius
    return c / math.hypot(c, pitch)


def min_pitch(radius: float, s_ax: float) -> float:
    """Smallest pitch that leaves the 1 % gap between neighbouring turns, measured across the wire:
    ``p · cos α = 1.01 · s`` with ``tan α = p / (2πR)``."""
    k = (1.0 + CLEARANCE) * s_ax
    q = k / (2.0 * math.pi * radius)
    if q >= 0.9:
        raise ValueError(f"the section's axial size {s_ax:g} is too large for a coil of mean diameter {2 * radius:g} "
                         "(neighbouring turns cannot clear each other)")
    return k / math.sqrt(1.0 - q * q)


def _f(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


def _sub(a: Sequence[float], b: Sequence[float]) -> V3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> V3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a: Sequence[float]) -> V3:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


@dataclass(frozen=True)
class CoilLaw:
    """One coil's centre line and section placement (see the module docstring). ``knots`` are ``(t, pitch)``
    breakpoints; the pitch is linear between them."""

    radius: float
    s_rad: float
    s_ax: float
    turns: float
    pitch: float  # active pitch
    end_pitch: float  # pitch of the closed end turns (= pitch for open ends)
    end_form: str
    hand: int  # +1 right-hand, -1 left-hand
    start: float  # start angle, radians
    z0: float  # centre-line height at t = 0
    length: float  # overall length along the axis (seating plane z = 0 to the top)
    knots: tuple[tuple[float, float], ...]

    # -- centre line --------------------------------------------------------- #

    @property
    def ground(self) -> bool:
        return self.end_form == "closed_ground"

    @property
    def active_turns(self) -> float:
        return self.turns - (2 * CLOSED_TURNS if self.end_form != "open" else 0.0)

    def breaks(self) -> list[float]:
        out: list[float] = []
        for t, _ in self.knots:
            if not out or t > out[-1] + 1e-12:
                out.append(t)
        return out

    def pitch_at(self, t: float) -> float:
        ks = self.knots
        if t <= ks[0][0]:
            return ks[0][1]
        for (t0, p0), (t1, p1) in zip(ks, ks[1:]):
            if t <= t1:
                return p1 if t1 - t0 < 1e-15 else p0 + (p1 - p0) * (t - t0) / (t1 - t0)
        return ks[-1][1]

    def dpitch(self, t: float) -> float:
        ks = self.knots
        for (t0, p0), (t1, p1) in zip(ks, ks[1:]):
            if t0 <= t < t1 and t1 - t0 > 1e-15:
                return (p1 - p0) / (t1 - t0)
        return 0.0

    def z(self, t: float) -> float:
        ks = self.knots
        if t <= 0.0:
            return self.z0 + ks[0][1] * t
        acc = self.z0
        for (t0, p0), (t1, p1) in zip(ks, ks[1:]):
            if t <= t0:
                break
            te = min(t, t1)
            pe = p0 if t1 - t0 < 1e-15 else p0 + (p1 - p0) * (te - t0) / (t1 - t0)
            acc += 0.5 * (p0 + pe) * (te - t0)
        if t > self.turns:
            acc += ks[-1][1] * (t - self.turns)
        return acc

    def theta(self, t: float) -> float:
        return self.start + self.hand * 2.0 * math.pi * t

    def point(self, t: float) -> V3:
        th = self.theta(t)
        return (self.radius * math.cos(th), self.radius * math.sin(th), self.z(t))

    def d1(self, t: float) -> V3:
        th, w = self.theta(t), self.hand * 2.0 * math.pi
        return (-self.radius * w * math.sin(th), self.radius * w * math.cos(th), self.pitch_at(t))

    def d2(self, t: float) -> V3:
        th, w = self.theta(t), 2.0 * math.pi
        return (-self.radius * w * w * math.cos(th), -self.radius * w * w * math.sin(th), self.dpitch(t))

    def speed(self, t: float) -> float:
        return math.hypot(2.0 * math.pi * self.radius, self.pitch_at(t))

    def frame(self, t: float) -> tuple[V3, V3, V3]:
        """(T, U, V) at t: the unit tangent, the section's first axis U (minus the principal normal: radially
        outward on a constant-pitch stretch) and its second axis V = T × U (≈ along the coil axis)."""
        T = _unit(self.d1(t))
        a = self.d2(t)
        k = _dot(a, T)
        N = _unit((a[0] - k * T[0], a[1] - k * T[1], a[2] - k * T[2]))
        U = (-N[0], -N[1], -N[2])
        return T, U, _cross(T, U)

    def section_point(self, t: float, u: float, v: float) -> V3:
        P = self.point(t)
        _, U, V = self.frame(t)
        return (P[0] + u * U[0] + v * V[0], P[1] + u * U[1] + v * V[1], P[2] + u * U[2] + v * V[2])

    def constant_spans(self) -> list[tuple[float, float]]:
        """Stretches of constant pitch (where the section frame is exactly radial / screw-like)."""
        out = []
        for (t0, p0), (t1, p1) in zip(self.knots, self.knots[1:]):
            if t1 - t0 > 1e-12 and abs(p1 - p0) < 1e-12:
                out.append((t0, t1))
        return out

    def _pieces(self, extra: Sequence[float] = (), step: float = 0.125) -> list[tuple[float, float]]:
        cuts = sorted({*self.breaks(), *(x for x in extra if 0.0 < x < self.turns)})
        out = []
        for a, b in zip(cuts, cuts[1:]):
            n = max(1, math.ceil((b - a) / step - 1e-9))
            out += [(a + (b - a) * i / n, a + (b - a) * (i + 1) / n) for i in range(n)]
        return out

    def arc_length(self) -> float:
        return sum(integrate(self.speed, a, b) for a, b in self._pieces())

    # -- section, extents and volume ----------------------------------------- #

    def kept_band(self, t: float) -> tuple[float, float]:
        """Range of the section's v coordinate kept at t: everything, or (ground ends) what lies between the planes
        z = 0 and z = L."""
        if not self.ground:
            return (-math.inf, math.inf)
        vz = self.frame(t)[2][2]
        zc = self.z(t)
        if abs(vz) < 1e-12:
            return (-math.inf, math.inf) if 0.0 <= zc <= self.length else (0.0, 0.0)
        a, b = -zc / vz, (self.length - zc) / vz
        return (min(a, b), max(a, b))

    def _end_crossings(self) -> list[float]:
        """t where a grinding plane leaves the section (the kept area stops changing): bottom and top."""
        if not self.ground:
            return []
        half = self.s_ax / 2.0

        def bottom(t: float) -> float:
            return self.z(t) - half * abs(self.frame(t)[2][2])

        def top(t: float) -> float:
            return self.z(t) + half * abs(self.frame(t)[2][2]) - self.length

        out = []
        for g, lo, hi in ((bottom, 0.0, min(1.0, self.turns)), (top, max(0.0, self.turns - 1.0), self.turns)):
            a, b = lo, hi
            ga, gb = g(a), g(b)
            if ga * gb > 0:
                continue
            for _ in range(80):
                m = 0.5 * (a + b)
                gm = g(m)
                if (gm > 0) == (ga > 0):
                    a, ga = m, gm
                else:
                    b = m
            out.append(0.5 * (a + b))
        return out

    def section_area(self, kind: str, lo: float = -math.inf, hi: float = math.inf) -> float:
        """Area of the section between v = lo and v = hi (whole section by default)."""
        if kind == "circle":
            r = self.s_rad / 2.0
            a, b = max(lo, -r), min(hi, r)
            if b <= a:
                return 0.0

            def below(y: float) -> float:
                y = max(-r, min(r, y))
                return y * math.sqrt(max(0.0, r * r - y * y)) + r * r * math.asin(y / r) + r * r * math.pi / 2.0

            return below(b) - below(a)
        a, b = max(lo, -self.s_ax / 2.0), min(hi, self.s_ax / 2.0)
        return self.s_rad * max(0.0, b - a)

    def volume(self, kind: str) -> float:
        """Expected solid volume: the kept section area integrated along the centre line (= area × length unless the
        ends are ground)."""
        if not self.ground:
            return self.section_area(kind) * self.arc_length()
        ends = self._end_crossings()

        def f(t: float) -> float:
            return self.section_area(kind, *self.kept_band(t)) * self.speed(t)

        return sum(integrate(f, a, b, 24) for a, b in self._pieces(ends, step=0.0625))

    def stations(self, n: int = 6) -> list[float]:
        """Up to n turn parameters for probes: inside constant-pitch stretches, clear of the ramps and of ground
        ends, evenly spread."""
        ends = self._end_crossings()
        lo = ends[0] + 0.05 if self.ground and ends else 0.02
        hi = ends[-1] - 0.05 if self.ground and len(ends) > 1 else self.turns - 0.02
        spans = [(max(a + 0.02, lo), min(b - 0.02, hi)) for a, b in self.constant_spans()]
        spans = [(a, b) for a, b in spans if b > a]
        total = sum(b - a for a, b in spans)
        if total <= 0:
            return []
        out = []
        for i in range(n):
            s = total * (i + 0.5) / n
            for a, b in spans:
                if s <= b - a:
                    out.append(a + s)
                    break
                s -= b - a
        return out

    # -- point location (requirement checker occupancy) ---------------------- #

    def locate(self, p: Sequence[float]) -> list[tuple[float, float, float]]:
        """``(t, u, v)`` of the section planes through the local point ``p`` near the coil: one per turn whose centre
        line passes the point's angle within reach (Newton on (p − P(t)) · P'(t) = 0)."""
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        phi = math.atan2(y, x) if math.hypot(x, y) > 1e-12 else self.start
        base = ((self.hand * (phi - self.start)) / (2.0 * math.pi)) % 1.0
        reach = max(self.s_rad, self.s_ax) + max(self.pitch, self.end_pitch)
        out = []
        for k in range(-1, int(math.ceil(self.turns)) + 2):
            t = base + k
            if t < -0.5 or t > self.turns + 0.5 or abs(self.z(min(max(t, 0.0), self.turns)) - z) > reach:
                continue
            for _ in range(30):
                P, a, b = self.point(t), self.d1(t), self.d2(t)
                d = _sub((x, y, z), P)
                g = _dot(d, a)
                gp = -_dot(a, a) + _dot(d, b)
                if abs(gp) < 1e-15:
                    break
                step = max(-0.25, min(0.25, g / gp))
                t -= step
                if abs(step) < 1e-13:
                    break
            _, U, V = self.frame(t)
            d = _sub((x, y, z), self.point(t))
            out.append((t, _dot(d, U), _dot(d, V)))
        return out

    def contains(self, p: Sequence[float], inside: Callable[[float, float], bool]) -> bool:
        """Whether the local point lies in the coil (``inside(u, v)``: the section region)."""
        if self.ground and not (0.0 <= p[2] <= self.length):
            return False
        return any(0.0 <= t <= self.turns and inside(u, v) for t, u, v in self.locate(p))


def coil_law(*, mean_diameter: float, s_rad: float, s_ax: float, turns: float, pitch: float | None = None,
             length: float | None = None, end_form: str = "open", handedness: str = "right", start_angle: float = 0.0) -> CoilLaw:
    """The coil of these parameters, or ``ValueError`` naming the parameters that do not fit together."""
    R = mean_diameter / 2.0
    if not (s_rad > 0 and s_ax > 0):
        raise ValueError("the section needs a positive size")
    if s_rad >= mean_diameter - 1e-6:
        raise ValueError(f"mean_diameter {_f(mean_diameter)} must exceed the section's radial size {_f(s_rad)} (the inner "
                         f"diameter {_f(mean_diameter - s_rad)} would not be positive)")
    if not (MIN_TURNS <= turns <= MAX_TURNS):
        raise ValueError(f"turns must be between {MIN_TURNS:g} and {MAX_TURNS:g} (got {_f(turns)})")
    if (pitch is None) == (length is None):
        raise ValueError("give exactly one of pitch (active pitch, centre-line rise per turn) or length (overall length "
                         "along the axis)")
    if end_form not in END_FORMS:
        raise ValueError(f"end_form must be one of {', '.join(END_FORMS)}")
    if handedness not in ("right", "left"):
        raise ValueError("handedness must be right or left")
    pc = min_pitch(R, s_ax)
    closed = end_form != "open"
    if closed and turns < 2 * CLOSED_TURNS + MIN_ACTIVE - 1e-9:
        raise ValueError(f"end_form {end_form} takes one closed turn at each end: turns must be at least "
                         f"{2 * CLOSED_TURNS + MIN_ACTIVE:g} (got {_f(turns)})")
    na = turns - 2 * CLOSED_TURNS if closed else turns
    ca = cos_helix(R, pc)
    if pitch is not None:
        if pitch <= 0:
            raise ValueError("pitch must be > 0")
        p = float(pitch)
        if not closed:
            L = p * turns + s_ax * cos_helix(R, p)
        elif end_form == "closed":
            L = 2 * pc + p * na + s_ax * ca
        else:
            L = 2 * pc + p * na
    else:
        L = float(length)  # type: ignore[arg-type]
        if not closed:
            if L <= s_ax * cos_helix(R, 0.0) + 1e-9:
                raise ValueError(f"length {_f(L)} must exceed the section's axial size {_f(s_ax)}")
            lo, hi = 0.0, L / turns + s_ax
            for _ in range(200):
                m = 0.5 * (lo + hi)
                if m * turns + s_ax * cos_helix(R, m) < L:
                    lo = m
                else:
                    hi = m
            p = 0.5 * (lo + hi)
        else:
            fixed = 2 * pc + (s_ax * ca if end_form == "closed" else 0.0)
            p = (L - fixed) / na
            if p <= 0:
                raise ValueError(f"length {_f(L)} is too short for {_f(turns)} turns with {end_form} ends (the two closed "
                                 f"turns alone take {_f(fixed)} mm)")
    if turns > 1.0 + 1e-9 and p < pc - 1e-9:
        if pitch is not None:
            raise ValueError(f"pitch {_f(p)} leaves no gap between neighbouring turns: with a section of axial size "
                             f"{_f(s_ax)} it must be at least {_f(pc)} (s / cos α + 1 %); coils that touch at the ends need "
                             "end_form closed or closed_ground")
        raise ValueError(f"length {_f(L)} over {_f(turns)} turns ({end_form} ends) gives an active pitch of {_f(p)}, less "
                         f"than the minimum {_f(pc)} (s / cos α + 1 %): neighbouring turns would intersect")
    if not closed:
        knots: tuple[tuple[float, float], ...] = ((0.0, p), (float(turns), p))
        z0 = s_ax / 2.0 * cos_helix(R, p)
        end_pitch = p
    else:
        a, b, w = CLOSED_TURNS, turns - CLOSED_TURNS, RAMP / 2.0
        knots = ((0.0, pc), (a - w, pc), (a + w, p), (b - w, p), (b + w, pc), (float(turns), pc))
        z0 = s_ax / 2.0 * ca if end_form == "closed" else 0.0
        end_pitch = pc
    return CoilLaw(radius=R, s_rad=float(s_rad), s_ax=float(s_ax), turns=float(turns), pitch=p, end_pitch=end_pitch,
                   end_form=end_form, hand=1 if handedness == "right" else -1, start=math.radians(start_angle), z0=z0,
                   length=L, knots=knots)
