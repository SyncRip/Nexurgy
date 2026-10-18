"""Versioned parametric part templates (W3; W0 contract §4: ``TEMPLATES``, ``instantiate(template_id, params)``).

A template turns typed parameters (unit, legal range, default) into an ordinary op list (:class:`ModelSpec`) plus
metadata: representation level (exact / nominal / simplified), procurement (standard / buy / make), material and its
source, the standard the nominal sizes come from, a spec label for BOMs and the named ports (``port.<name>`` datums:
axes and seating planes) that mates use. The op list is replayed by the kernel and checked like any hand-made one;
nothing is hidden in the template that the ops do not show.

Frames: round parts have their axis on +Z with the main seating face on Z = 0 (bolt: bearing face of the head on
Z = 0, shank toward −Z; nut / washer / gasket / ring / pin / sleeve: bottom face on Z = 0). Plates and boxes sit on
Z = 0 centred on the origin; the bent bracket has the outer virtual sharp of its first bend at the origin, base leg
along +X on Z = 0, width centred on Y = 0.

Representation honesty: threads are never modelled (bolt shank and nut bore are plain at the nominal diameter,
labelled ``simplified``); O-rings and gaskets are nominal static shapes (a compressed O-ring is an area-preserving
stadium section, labelled, no compression is simulated). Nominal size tables are the public ISO dimensions of the
head / nut / washer (``standard`` names the table); verify against the standard before procurement.

Springs (generation supplement): ``split_lock_washer`` (GB/T 93 split ring with its gap and the axial offset of its
ends in the free state, or the flattened installed state — never a closed flat washer) and ``compression_spring``
(outer / mean / inner diameter kept apart, active and total coils, pitch, free and installed length, handedness, end
form; contradictory values are refused naming the parameters) are ``coil`` ops (:mod:`app.cad.coil`), labelled
``nominal`` with what the static shape omits; ``meta["derived"]`` lists every adopted value.

Bumping a template's ``version`` is required whenever its generated ops change for the same parameters.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .assembly_model import Datum
from .ops import ModelSpec
from .sketch import bent_plate_layout, default_x

Representation = Literal["exact", "nominal", "simplified"]
Procurement = Literal["standard", "buy", "make"]


class TemplateError(ValueError):
    """Unknown template or invalid parameters. ``code``: ``template_unknown`` / ``template_param_invalid``;
    ``problems``: ``[{"param", "message"}]``."""

    def __init__(self, code: str, message: str, problems: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = problems or []


@dataclass(frozen=True)
class Param:
    name: str
    type: Literal["float", "int", "choice", "text"]
    default: Any
    unit: str | None = None  # "mm", "deg", "%", None
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    label_en: str = ""
    label_zh: str = ""
    #: choice -> words that state it in a request (a model may set the choice when one of them is written)
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "default": self.default, "unit": self.unit,
                               "label": {"en": self.label_en, "zh": self.label_zh}}
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.choices:
            out["choices"] = list(self.choices)
        return out


@dataclass(frozen=True)
class Template:
    id: str
    version: int
    title_en: str
    title_zh: str
    category: Literal["fastener", "pin", "seal", "sleeve", "plate", "bracket", "box", "spring"]
    representation: Representation
    procurement: Procurement
    standard: str | None
    params: tuple[Param, ...]
    build: Callable[[dict[str, Any]], list[dict[str, Any]]]
    ports: Callable[[dict[str, Any]], list[Datum]]
    label: Callable[[dict[str, Any]], str]
    check: Callable[[dict[str, Any]], list[tuple[str, str]]] = lambda p: []
    notes: Callable[[dict[str, Any]], list[str]] = lambda p: []
    port_names: tuple[str, ...] = field(default_factory=tuple)
    #: Adopted values computed from the parameters (springs: every diameter, coil count, pitch and length), as meta["derived"].
    derived: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def catalog_entry(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "title": {"en": self.title_en, "zh": self.title_zh},
                "category": self.category, "representation": self.representation, "procurement": self.procurement,
                "standard": self.standard, "params": [p.schema() for p in self.params], "ports": list(self.port_names)}


# --------------------------------------------------------------------------- #
# Nominal size tables (public ISO dimensions, mm)
# --------------------------------------------------------------------------- #

METRIC_SIZES = ("M3", "M4", "M5", "M6", "M8", "M10", "M12", "M14", "M16", "M20", "M24")
#: d, s (across flats), k (hex bolt head height, ISO 4017 / 4014)
BOLT = {"M3": (3, 5.5, 2.0), "M4": (4, 7, 2.8), "M5": (5, 8, 3.5), "M6": (6, 10, 4.0), "M8": (8, 13, 5.3), "M10": (10, 16, 6.4),
        "M12": (12, 18, 7.5), "M14": (14, 21, 8.8), "M16": (16, 24, 10.0), "M20": (20, 30, 12.5), "M24": (24, 36, 15.0)}
#: m (hex nut height, ISO 4032 style 1)
NUT_M = {"M3": 2.4, "M4": 3.2, "M5": 4.7, "M6": 5.2, "M8": 6.8, "M10": 8.4, "M12": 10.8, "M14": 12.8, "M16": 14.8, "M20": 18.0,
         "M24": 21.5}
#: d1 (inner), d2 (outer), h (ISO 7089 normal series)
WASHER = {"M3": (3.2, 7, 0.5), "M4": (4.3, 9, 0.8), "M5": (5.3, 10, 1.0), "M6": (6.4, 12, 1.6), "M8": (8.4, 16, 1.6),
          "M10": (10.5, 20, 2.0), "M12": (13.0, 24, 2.5), "M14": (15.0, 28, 2.5), "M16": (17.0, 30, 3.0), "M20": (21.0, 37, 3.0),
          "M24": (25.0, 44, 4.0)}

_MATERIAL = Param("material", "text", "", label_en="Material (optional, as stated)", label_zh="材料（可选，按给定填写）")


# --------------------------------------------------------------------------- #
# Small builders
# --------------------------------------------------------------------------- #


def _plane(name: str, origin: tuple[float, float, float], normal: tuple[float, float, float],
           x: tuple[float, float, float] | None = None) -> Datum:
    return Datum(name=name, kind="plane", origin=origin, direction=normal, x_dir=x or default_x(normal))


def _axis(name: str, origin: tuple[float, float, float], d: tuple[float, float, float]) -> Datum:
    return Datum(name=name, kind="axis", origin=origin, direction=d, x_dir=default_x(d))


def _hex_points(s: float) -> list[list[float]]:
    r = s / math.sqrt(3.0)
    return [[round(r * math.cos(math.radians(60 * k)), 9), round(r * math.sin(math.radians(60 * k)), 9)] for k in range(6)]


def _loop(points: list[tuple[float, float]]) -> dict[str, Any]:
    return {"kind": "sketch", "start": list(points[0]), "segments": [{"kind": "line", "to": list(p)} for p in points[1:]]}


def _g(x: float) -> str:
    return f"{x:g}"


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def _bolt_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    d, s, k = BOLT[p["size"]]
    L = p["length"]
    return [
        {"id": "F-01", "type": "extrude", "label": f"hex head s{_g(s)} k{_g(k)}", "mode": "new",
         "profile": {"kind": "polygon", "points": _hex_points(s)}, "plane": "XY", "offset": 0.0, "height": k},
        {"id": "F-02", "type": "cylinder", "label": f"shank Ø{_g(d)} (thread not modelled)", "mode": "add", "radius": d / 2.0,
         "height": L, "at": [0.0, 0.0, -L], "axis": "z"},
    ]


def _bolt_ports(p: dict[str, Any]) -> list[Datum]:
    d, s, k = BOLT[p["size"]]
    L = p["length"]
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.seat", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.head_top", (0.0, 0.0, k), (0.0, 0.0, 1.0)),
            _plane("port.tip", (0.0, 0.0, -L), (0.0, 0.0, -1.0))]


def _bolt_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    d = BOLT[p["size"]][0]
    return [("length", f"length must be at least 1.5 × d = {_g(1.5 * d)} mm for {p['size']}")] if p["length"] < 1.5 * d else []


def _nut_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    d, s, _ = BOLT[p["size"]]
    m = NUT_M[p["size"]]
    return [
        {"id": "F-01", "type": "extrude", "label": f"hex nut s{_g(s)} m{_g(m)}", "mode": "new",
         "profile": {"kind": "polygon", "points": _hex_points(s)}, "plane": "XY", "offset": 0.0, "height": m},
        {"id": "F-02", "type": "hole", "label": f"bore Ø{_g(d)} (thread not modelled)", "at": [0.0, 0.0, m], "direction": [0.0, 0.0, -1.0],
         "diameter": float(d), "depth": None},
    ]


def _nut_ports(p: dict[str, Any]) -> list[Datum]:
    m = NUT_M[p["size"]]
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.seat", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.top", (0.0, 0.0, m), (0.0, 0.0, 1.0))]


def _washer_dims(p: dict[str, Any]) -> tuple[float, float, float]:
    if p["size"] == "custom":
        return p["inner_diameter"], p["outer_diameter"], p["thickness"]
    return WASHER[p["size"]]


def _washer_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    d1, d2, h = _washer_dims(p)
    return [{"id": "F-01", "type": "extrude", "label": f"washer Ø{_g(d2)}/Ø{_g(d1)} × {_g(h)}", "mode": "new",
             "profile": {"kind": "ring", "outer_radius": d2 / 2.0, "inner_radius": d1 / 2.0}, "plane": "XY", "height": h}]


def _washer_ports(p: dict[str, Any]) -> list[Datum]:
    _, _, h = _washer_dims(p)
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.top", (0.0, 0.0, h), (0.0, 0.0, 1.0))]


def _washer_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    if p["size"] != "custom":
        return []
    if p["inner_diameter"] >= p["outer_diameter"]:
        return [("inner_diameter", "inner diameter must be smaller than the outer diameter")]
    return []


def _pin_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    d, L, c = p["diameter"], p["length"], p["chamfer"]
    r = d / 2.0
    if c <= 0:
        return [{"id": "F-01", "type": "cylinder", "label": f"pin Ø{_g(d)} × {_g(L)}", "mode": "new", "radius": r, "height": L}]
    pts = [(0.0, 0.0), (0.0, r - c), (c, r), (L - c, r), (L, r - c), (L, 0.0)]
    return [{"id": "F-01", "type": "revolve", "label": f"pin Ø{_g(d)} × {_g(L)}, ends C{_g(c)}", "mode": "new", "profile": _loop(pts),
             "at": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0]}]


def _pin_ports(p: dict[str, Any]) -> list[Datum]:
    L = p["length"]
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.end_bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.end_top", (0.0, 0.0, L), (0.0, 0.0, 1.0))]


def _pin_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    if p["chamfer"] >= p["diameter"] / 2.0:
        out.append(("chamfer", "chamfer must be smaller than the pin radius"))
    if 2 * p["chamfer"] >= p["length"]:
        out.append(("chamfer", "two chamfers must fit in the pin length"))
    return out


def _oring_section(p: dict[str, Any]) -> tuple[float, float]:
    """(axial height, radial width) of the static section."""
    d2 = p["cross_section"]
    if p["state"] == "free":
        return d2, d2
    h = d2 * (1.0 - p["squeeze_pct"] / 100.0)
    w = (math.pi * d2 * d2 / 4.0 - math.pi * h * h / 4.0) / h + h  # area-preserving stadium (radial width)
    return h, w


def _oring_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    d1, d2 = p["inner_diameter"], p["cross_section"]
    rc = d1 / 2.0 + d2 / 2.0  # section centre radius (free state)
    h, w = _oring_section(p)
    if p["state"] == "free":
        prof = {"kind": "sketch", "start": [d2 / 2.0, rc], "segments": [{"kind": "arc", "to": [d2 / 2.0, rc], "center": [0.0, rc]}]}
        label = f"O-ring {_g(d1)} × {_g(d2)} (free state)"
    else:
        a = h / 2.0
        b = (w - h) / 2.0  # half length of the straight sides (radial)
        prof = {"kind": "sketch", "start": [-a, rc - b], "segments": [
            {"kind": "line", "to": [-a, rc + b]},
            {"kind": "arc", "to": [a, rc + b], "center": [0.0, rc + b], "ccw": False},
            {"kind": "line", "to": [a, rc - b]},
            {"kind": "arc", "to": [-a, rc - b], "center": [0.0, rc - b], "ccw": False}]}
        label = f"O-ring {_g(d1)} × {_g(d2)} (nominal compressed, squeeze {_g(p['squeeze_pct'])}%)"
    return [{"id": "F-01", "type": "revolve", "label": label, "mode": "new", "profile": prof, "at": [0.0, 0.0, 0.0],
             "axis": [0.0, 0.0, 1.0]}]


def _oring_ports(p: dict[str, Any]) -> list[Datum]:
    h, _ = _oring_section(p)
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.mid_plane", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            _plane("port.bottom", (0.0, 0.0, -h / 2.0), (0.0, 0.0, -1.0)), _plane("port.top", (0.0, 0.0, h / 2.0), (0.0, 0.0, 1.0))]


def _oring_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    h, w = _oring_section(p)
    if p["state"] != "free" and p["inner_diameter"] / 2.0 + p["cross_section"] / 2.0 - w / 2.0 <= 0:
        return [("squeeze_pct", "the compressed section would reach the axis; lower the squeeze")]
    return []


def _ring_gasket_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    od, idd, t = p["outer_diameter"], p["inner_diameter"], p["thickness"]
    ops: list[dict[str, Any]] = [{"id": "F-01", "type": "extrude", "label": f"ring gasket Ø{_g(od)}/Ø{_g(idd)} × {_g(t)}", "mode": "new",
                                  "profile": {"kind": "ring", "outer_radius": od / 2.0, "inner_radius": idd / 2.0}, "plane": "XY",
                                  "height": t}]
    n = int(p["hole_count"])
    if n > 0:
        hole: dict[str, Any] = {"id": "F-02", "type": "hole", "label": f"{n} × Ø{_g(p['hole_diameter'])} on PCD {_g(p['pcd'])}",
                                "at": [0.0, 0.0, t] if n > 1 else [p["pcd"] / 2.0, 0.0, t], "direction": [0.0, 0.0, -1.0],
                                "diameter": p["hole_diameter"], "depth": None}
        if n > 1:
            hole["pattern"] = {"kind": "circular", "count": n, "radius": p["pcd"] / 2.0}
        ops.append(hole)
    return ops


def _ring_gasket_ports(p: dict[str, Any]) -> list[Datum]:
    t = p["thickness"]
    out = [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
           _plane("port.top", (0.0, 0.0, t), (0.0, 0.0, 1.0))]
    n = int(p["hole_count"])
    for k in range(n):
        a = 2 * math.pi * k / n
        out.append(_axis(f"port.hole[{k}]", (round(p["pcd"] / 2.0 * math.cos(a), 9), round(p["pcd"] / 2.0 * math.sin(a), 9), t),
                         (0.0, 0.0, -1.0)))
    return out


def _ring_gasket_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    if p["inner_diameter"] >= p["outer_diameter"]:
        out.append(("inner_diameter", "inner diameter must be smaller than the outer diameter"))
    if int(p["hole_count"]) > 0:
        r_in, r_out, rh = p["inner_diameter"] / 2.0, p["outer_diameter"] / 2.0, p["hole_diameter"] / 2.0
        if not (r_in + rh < p["pcd"] / 2.0 < r_out - rh):
            out.append(("pcd", "the bolt holes must lie between the inner and outer diameter"))
    return out


def _rect_gasket_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    L, W, t, cr = p["length"], p["width"], p["thickness"], p["corner_radius"]
    ops: list[dict[str, Any]] = [{"id": "F-01", "type": "extrude", "label": f"gasket {_g(L)} × {_g(W)} × {_g(t)}", "mode": "new",
                                  "profile": {"kind": "rect", "width": L, "length": W, "corner_radius": cr}, "plane": "XY", "height": t}]
    nid = 2
    if p["inner_length"] > 0 and p["inner_width"] > 0:
        ops.append({"id": f"F-{nid:02d}", "type": "cut", "label": f"window {_g(p['inner_length'])} × {_g(p['inner_width'])}",
                    "profile": {"kind": "rect", "width": p["inner_length"], "length": p["inner_width"],
                                "corner_radius": p["inner_corner_radius"]}, "plane": "XY", "offset": t, "depth": None})
        nid += 1
    if p["hole_diameter"] > 0:
        e = p["hole_inset"]
        ops.append({"id": f"F-{nid:02d}", "type": "hole", "label": f"4 × Ø{_g(p['hole_diameter'])} corner holes",
                    "at": [-(L / 2.0 - e), -(W / 2.0 - e), t], "direction": [0.0, 0.0, -1.0], "diameter": p["hole_diameter"],
                    "depth": None, "pattern": {"kind": "linear", "count": [2, 2], "pitch": [L - 2 * e, W - 2 * e]}})
    return ops


def _rect_gasket_ports(p: dict[str, Any]) -> list[Datum]:
    L, W, t = p["length"], p["width"], p["thickness"]
    out = [_plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)), _plane("port.top", (0.0, 0.0, t), (0.0, 0.0, 1.0))]
    if p["hole_diameter"] > 0:
        e = p["hole_inset"]
        for k, (sx, sy) in enumerate(((-1, -1), (1, -1), (-1, 1), (1, 1))):
            out.append(_axis(f"port.hole[{k}]", (sx * (L / 2.0 - e), sy * (W / 2.0 - e), t), (0.0, 0.0, -1.0)))
    return out


def _rect_gasket_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    L, W = p["length"], p["width"]
    if p["corner_radius"] * 2 >= min(L, W):
        out.append(("corner_radius", "corner radius must be less than half the shorter side"))
    if p["inner_length"] > 0 or p["inner_width"] > 0:
        if not (0 < p["inner_length"] < L and 0 < p["inner_width"] < W):
            out.append(("inner_length", "the window must be smaller than the gasket (give both inner length and width)"))
        elif p["inner_corner_radius"] * 2 >= min(p["inner_length"], p["inner_width"]):
            out.append(("inner_corner_radius", "window corner radius must be less than half its shorter side"))
    if p["hole_diameter"] > 0:
        e, r = p["hole_inset"], p["hole_diameter"] / 2.0
        if e <= r or 2 * e >= min(L, W):
            out.append(("hole_inset", "corner holes need an inset larger than their radius and inside the gasket"))
        elif p["inner_length"] > 0 and (L / 2.0 - e - r < p["inner_length"] / 2.0 and W / 2.0 - e - r < p["inner_width"] / 2.0):
            out.append(("hole_inset", "the corner holes overlap the window"))
    return out


def _sleeve_layers(p: dict[str, Any]) -> list[tuple[float, float]]:
    out = []
    for i in (1, 2, 3):
        od, ln = p[f"od{i}"], p[f"length{i}"]
        if ln > 0:
            out.append((od, ln))
    return out


def _sleeve_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    ri = p["bore"] / 2.0
    layers = _sleeve_layers(p)
    pts: list[tuple[float, float]] = [(0.0, ri)]
    z = 0.0
    for od, ln in layers:
        pts.append((z, od / 2.0))
        z += ln
        pts.append((z, od / 2.0))
    pts.append((z, ri))
    clean: list[tuple[float, float]] = []
    for q in pts:
        if not clean or abs(q[0] - clean[-1][0]) > 1e-9 or abs(q[1] - clean[-1][1]) > 1e-9:
            clean.append(q)
    label = "sleeve Ø" + "/Ø".join(_g(od) for od, _ in layers) + f" bore Ø{_g(p['bore'])} L{_g(z)}"
    return [{"id": "F-01", "type": "revolve", "label": label, "mode": "new", "profile": _loop(clean), "at": [0.0, 0.0, 0.0],
             "axis": [0.0, 0.0, 1.0]}]


def _sleeve_ports(p: dict[str, Any]) -> list[Datum]:
    layers = _sleeve_layers(p)
    total = sum(ln for _, ln in layers)
    out = [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
           _plane("port.top", (0.0, 0.0, total), (0.0, 0.0, 1.0))]
    z = 0.0
    for i, (od, ln) in enumerate(layers[:-1]):
        z += ln
        nxt = layers[i + 1][0]
        if abs(nxt - od) > 1e-9:
            out.append(_plane(f"port.shoulder[{i}]", (0.0, 0.0, z), (0.0, 0.0, 1.0 if nxt < od else -1.0)))
    return out


def _sleeve_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    layers = _sleeve_layers(p)
    if not layers:
        return [("length1", "at least one layer needs a length")]
    return [(f"od{i + 1}", "every layer's outer diameter must exceed the bore") for i, (od, _) in enumerate(layers) if od <= p["bore"]]


def _plate_holes(p: dict[str, Any]) -> list[tuple[float, float]]:
    nx, ny = int(p["holes_x"]), int(p["holes_y"])
    if p["hole_diameter"] <= 0 or nx * ny == 0:
        return []
    return [(-(nx - 1) * p["pitch_x"] / 2.0 + i * p["pitch_x"], -(ny - 1) * p["pitch_y"] / 2.0 + j * p["pitch_y"])
            for j in range(ny) for i in range(nx)]


def _plate_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    L, W, t = p["length"], p["width"], p["thickness"]
    ops: list[dict[str, Any]] = [{"id": "F-01", "type": "extrude", "label": f"plate {_g(L)} × {_g(W)} × {_g(t)}", "mode": "new",
                                  "profile": {"kind": "rect", "width": L, "length": W, "corner_radius": p["corner_radius"]},
                                  "plane": "XY", "height": t}]
    holes = _plate_holes(p)
    if holes:
        nx, ny = int(p["holes_x"]), int(p["holes_y"])
        ops.append({"id": "F-02", "type": "hole", "label": f"{nx * ny} × Ø{_g(p['hole_diameter'])}", "at": [holes[0][0], holes[0][1], t],
                    "direction": [0.0, 0.0, -1.0], "diameter": p["hole_diameter"], "depth": None,
                    **({"pattern": {"kind": "linear", "count": [nx, ny], "pitch": [p["pitch_x"], p["pitch_y"]]}} if nx * ny > 1 else {})})
    return ops


def _plate_ports(p: dict[str, Any]) -> list[Datum]:
    t = p["thickness"]
    out = [_plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)), _plane("port.top", (0.0, 0.0, t), (0.0, 0.0, 1.0))]
    out += [_axis(f"port.hole[{k}]", (x, y, t), (0.0, 0.0, -1.0)) for k, (x, y) in enumerate(_plate_holes(p))]
    return out


def _plate_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    L, W = p["length"], p["width"]
    if p["corner_radius"] * 2 >= min(L, W):
        out.append(("corner_radius", "corner radius must be less than half the shorter side"))
    r = p["hole_diameter"] / 2.0
    for x, y in _plate_holes(p):
        if abs(x) + r >= L / 2.0 or abs(y) + r >= W / 2.0:
            out.append(("pitch_x", "the hole grid does not fit inside the plate"))
            break
    return out


def _bracket_layout(p: dict[str, Any]) -> Any:
    return bent_plate_layout(p["thickness"], [p["leg1"], p["leg2"]], [p["bend_angle"]], p["inner_radius"], "outside")


def _bracket_frame(p: dict[str, Any]) -> dict[str, Any]:
    return {"origin": [p["leg1"], -p["width"] / 2.0, 0.0], "normal": [0.0, 1.0, 0.0], "x_dir": [-1.0, 0.0, 0.0]}


def _to_world(p: dict[str, Any], u: float, v: float, w: float) -> tuple[float, float, float]:
    """Bracket outline frame → world: origin (leg1, −width/2, 0), x = −X, y = +Z, z (width) = +Y."""
    return (p["leg1"] - u, -p["width"] / 2.0 + w, v)


def _vec_world(u: float, v: float) -> tuple[float, float, float]:
    return (-u + 0.0, 0.0, v + 0.0)


def _bracket_holes(p: dict[str, Any]) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]:
    """(leg, entry on the inner face, outward normal of the inner face, in-plane x across the width) per hole; holes
    sit at the middle of each leg's flat, evenly spread across the width, drilled through from the inner face."""
    if p["hole_diameter"] <= 0:
        return []
    lay = _bracket_layout(p)
    t, w = p["thickness"], p["width"]
    out = []
    for leg, count in (("leg1", int(p["holes_leg1"])), ("leg2", int(p["holes_leg2"]))):
        if count <= 0:
            continue
        a, b, phi = lay.legs[0 if leg == "leg1" else 1]
        lu, lv = -math.sin(phi), math.cos(phi)  # thickness side (inner face for a positive bend)
        inner = ((a[0] + b[0]) / 2.0 + t * lu, (a[1] + b[1]) / 2.0 + t * lv)
        for k in range(count):
            out.append((leg, _to_world(p, inner[0], inner[1], w * (k + 0.5) / count), _vec_world(lu, lv), (0.0, 1.0, 0.0)))
    return out


def _bracket_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{
        "id": "F-01", "type": "bent_plate", "label": f"bracket t{_g(p['thickness'])} {_g(p['leg1'])}/{_g(p['leg2'])} ri{_g(p['inner_radius'])}",
        "mode": "new", "thickness": p["thickness"], "width": p["width"], "legs": [p["leg1"], p["leg2"]], "bends": [p["bend_angle"]],
        "inner_radius": p["inner_radius"], "leg_length": "outside", "plane": _bracket_frame(p)}]
    nid = 2
    for leg, entry, n, x in _bracket_holes(p):
        ops.append({"id": f"F-{nid:02d}", "type": "hole", "label": f"Ø{_g(p['hole_diameter'])} in {leg}",
                    "plane": {"origin": [round(c, 9) for c in entry], "normal": [round(c, 12) + 0.0 for c in n], "x_dir": list(x)},
                    "at": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, -1.0], "diameter": p["hole_diameter"], "depth": None})
        nid += 1
    return ops


def _bracket_ports(p: dict[str, Any]) -> list[Datum]:
    """``port.base`` / ``port.wall``: the outer (reference) faces of the two legs at the middle of their flats."""
    lay = _bracket_layout(p)
    out = []
    for name, (a, b, phi) in zip(("port.base", "port.wall"), lay.legs):
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        along = _vec_world(math.cos(phi), math.sin(phi))
        out.append(_plane(name, _to_world(p, mid[0], mid[1], p["width"] / 2.0), _vec_world(math.sin(phi), -math.cos(phi)), along))
    out += [_axis(f"port.hole[{k}]", e, (-n[0] + 0.0, -n[1] + 0.0, -n[2] + 0.0)) for k, (_, e, n, _) in enumerate(_bracket_holes(p))]
    return out


def _bracket_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    try:
        lay = _bracket_layout(p)
    except ValueError as exc:
        return [("leg1", str(exc))]
    out = []
    if p["hole_diameter"] > 0:
        for leg, count in (("leg1", int(p["holes_leg1"])), ("leg2", int(p["holes_leg2"]))):
            if count > 0:
                flat = lay.flats[0 if leg == "leg1" else 1]
                if p["hole_diameter"] >= flat or p["width"] / count <= p["hole_diameter"]:
                    out.append((f"holes_{leg}", f"{count} × Ø{_g(p['hole_diameter'])} do not fit on the flat of {leg}"))
    return out


def _box_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    L, W, H, wall, bottom = p["length"], p["width"], p["height"], p["wall"], p["bottom"]
    ops: list[dict[str, Any]] = [{"id": "F-01", "type": "box", "label": f"box {_g(L)} × {_g(W)} × {_g(H)}", "mode": "new", "size": [L, W, H],
                                  "at": [0.0, 0.0, 0.0]}]
    if abs(bottom - wall) < 1e-9:
        ops.append({"id": "F-02", "type": "shell", "label": f"walls {_g(wall)}, open top", "thickness": wall, "open_faces": [[0.0, 0.0, 1.0]]})
    else:
        ops.append({"id": "F-02", "type": "cut", "label": f"cavity, walls {_g(wall)}, bottom {_g(bottom)}",
                    "profile": {"kind": "rect", "width": L - 2 * wall, "length": W - 2 * wall}, "plane": "XY", "offset": H,
                    "depth": H - bottom})
    n = int(p["partitions"])
    if n > 0:
        inner = L - 2 * wall
        pt = p["partition_thickness"]
        x0 = -inner / 2.0 + inner / (n + 1)
        ops.append({"id": "F-03", "type": "box", "label": f"partition t{_g(pt)}", "mode": "add", "size": [pt, W - 2 * wall, H - bottom],
                    "at": [round(x0, 9), 0.0, bottom]})
        if n > 1:
            ops.append({"id": "F-04", "type": "pattern", "label": f"{n} partitions", "target": "F-03",
                        "layout": {"kind": "linear", "direction": [1.0, 0.0, 0.0], "spacing": round(inner / (n + 1), 9), "count": n}})
    return ops


def _box_ports(p: dict[str, Any]) -> list[Datum]:
    return [_plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)), _plane("port.rim", (0.0, 0.0, p["height"]), (0.0, 0.0, 1.0)),
            _plane("port.inner_floor", (0.0, 0.0, p["bottom"]), (0.0, 0.0, 1.0))]


def _box_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    if 2 * p["wall"] >= min(p["length"], p["width"]):
        out.append(("wall", "walls must leave an inner cavity"))
    if p["bottom"] >= p["height"]:
        out.append(("bottom", "the bottom must be thinner than the height"))
    n = int(p["partitions"])
    if n > 0 and (p["length"] - 2 * p["wall"]) / (n + 1) <= p["partition_thickness"]:
        out.append(("partitions", "the partitions do not fit in the cavity"))
    return out


# --------------------------------------------------------------------------- #
# Springs (generation supplement, package C): both are ``coil`` ops (app/cad/coil.py)
# --------------------------------------------------------------------------- #

#: GB/T 93-1987 standard split lock washers (normal series): d (inner, min) and s = b (square section), nominal mm.
#: Free height H ranges 2s … 2.5s; the free width of the split is not specified by the table.
LOCK_WASHER = {"M3": (3.1, 0.8), "M4": (4.1, 1.1), "M5": (5.1, 1.3), "M6": (6.1, 1.6), "M8": (8.1, 2.1), "M10": (10.2, 2.6),
               "M12": (12.2, 3.1), "M14": (14.2, 3.6), "M16": (16.2, 4.1), "M20": (20.2, 5.0), "M24": (24.5, 6.0)}


def _lock_washer(p: dict[str, Any]) -> dict[str, Any]:
    """Adopted dimensions of a split lock washer: inner / outer diameter, section b (radial) × s (axial), free height H,
    lift H − s, gap and where each came from."""
    if p["size"] == "custom":
        d1, d2, s = p["inner_diameter"], p["outer_diameter"], p["thickness"]
        b = (d2 - d1) / 2.0
    else:
        d1, s = LOCK_WASHER[p["size"]]
        b = s
        d2 = d1 + 2 * b
    H = p["free_height"] if p["free_height"] > 0 else 2.0 * s
    gap = p["gap"] if p["gap"] > 0 else 0.5 * s
    dm = d1 + b
    return {"inner_diameter": d1, "outer_diameter": d2, "section_width": b, "thickness": s, "mean_diameter": dm,
            "free_height": H, "lift": H - s, "gap": gap, "gap_angle_deg": math.degrees(gap / (dm / 2.0)),
            "free_height_source": "parameter" if p["free_height"] > 0 else "2 × thickness (GB/T 93 minimum free height)",
            "gap_source": "parameter" if p["gap"] > 0 else "0.5 × thickness (GB/T 93 gives no free gap width)",
            "state": "free" if H - s > 1e-9 else "flat"}


def _lock_washer_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    if p["size"] == "custom" and p["inner_diameter"] >= p["outer_diameter"]:
        return [("inner_diameter", "inner diameter must be smaller than the outer diameter")]
    w = _lock_washer(p)
    out = []
    if w["free_height"] < w["thickness"] - 1e-9:
        out.append(("free_height", f"free height {_g(w['free_height'])} is less than the thickness {_g(w['thickness'])} (H = s is "
                                   "the flattened, installed state)"))
    if w["gap"] >= math.pi * w["mean_diameter"] / 2.0:
        out.append(("gap", f"gap {_g(w['gap'])} must be less than half the mean circumference ({_g(math.pi * w['mean_diameter'] / 2.0)})"))
    return out


def _lock_washer_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    w = _lock_washer(p)
    g = math.radians(w["gap_angle_deg"])
    if w["state"] == "flat":  # installed: a flat split ring from +X round to the gap just before it, the gap kept
        ro, ri = w["outer_diameter"] / 2.0, w["inner_diameter"] / 2.0

        def pt(r: float, a: float) -> list[float]:
            return [round(r * math.cos(a), 9) + 0.0, round(r * math.sin(a), 9) + 0.0]

        prof = {"kind": "sketch", "start": pt(ro, 0.0), "segments": [
            {"kind": "arc", "to": pt(ro, -g), "center": [0.0, 0.0], "ccw": True},
            {"kind": "line", "to": pt(ri, -g)},
            {"kind": "arc", "to": pt(ri, 0.0), "center": [0.0, 0.0], "ccw": False}]}
        return [{"id": "F-01", "type": "extrude", "label": f"split lock washer (flattened) gap {_g(w['gap'])}", "mode": "new",
                 "profile": prof, "plane": "XY", "height": w["thickness"]}]
    # free state: the wire starts on +X (start angle 0) and runs round to the gap just before it
    return [{"id": "F-01", "type": "coil", "label": f"split lock washer {_g(w['section_width'])}×{_g(w['thickness'])} lift {_g(w['lift'])}",
             "mode": "new", "section": {"kind": "rect", "width": w["section_width"], "length": w["thickness"]},
             "mean_diameter": w["mean_diameter"], "turns": round(1.0 - w["gap"] / (math.pi * w["mean_diameter"]), 12),
             "length": w["free_height"], "handedness": p["handedness"], "start_angle": 0.0, "end_form": "open"}]


def _lock_washer_ports(p: dict[str, Any]) -> list[Datum]:
    w = _lock_washer(p)
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.top", (0.0, 0.0, w["free_height"]), (0.0, 0.0, 1.0))]


def _lock_washer_label(p: dict[str, Any]) -> str:
    w = _lock_washer(p)
    head = f"Split lock washer GB/T 93 {p['size']}" if p["size"] != "custom" else (
        f"Split lock washer Ø{_g(w['outer_diameter'])}/Ø{_g(w['inner_diameter'])}×{_g(w['thickness'])}")
    return head + (" (flattened)" if w["state"] == "flat" else f" H{_g(w['free_height'])}")


def _lock_washer_notes(p: dict[str, Any]) -> list[str]:
    w = _lock_washer(p)
    out = [f"inner Ø{_g(w['inner_diameter'])}, outer Ø{_g(w['outer_diameter'])}, section {_g(w['section_width'])} (radial) × "
           f"{_g(w['thickness'])} (axial), gap {_g(w['gap'])} ({w['gap_source']})"]
    if w["state"] == "free":
        out.append(f"static free state: the split ends are offset axially by H − s = {_g(w['lift'])} (free height {_g(w['free_height'])}: "
                   f"{w['free_height_source']}); flattening under load is not simulated")
    else:
        out.append("installed (flattened) state: a flat split ring, the gap kept; no axial offset")
    out.append("section a sharp rectangle normal to the wire; the slanted cut, section twist and edge rounding of real washers are "
               "omitted")
    return out


# Free length H0 = t·n + k·d (n active coils; open ends: n = total coils, k = 1), k within a band per end form.
# GB/T 23935-2009 (Design and calculation of cylindrical helical springs), free height of compression springs: closed and ground ends H0 ≈ n·t + 1.5d with
# n1 = n + 2 (n·t + 2d with n1 = n + 2.5); closed, not ground H0 ≈ n·t + 3d with n1 = n + 2 (n·t + 3.5d with n1 = n + 2.5).
# (Shigley, table 10-1, with one closed coil per end: squared and ground n·t + 2d, squared n·t + 3d.) How much of an end
# coil is closed and ground differs between makers and handbooks, so for the template's n1 = n + 2 a stated free length
# anywhere from the GB n1 = n + 2 value to the n1 = n + 2.5 one (with the max(0.1, 1 %) tolerance) is accepted, and the
# GB n1 = n + 2 value is adopted when one of t / H0 is derived from the other. The modelled coil grinds about half an end
# coil (≈ n·t + 2d) and takes its active pitch from the length, so a stated length is kept exactly.
SPRING_FREE_LENGTH_K = {"open": (1.0, 1.0), "closed": (3.0, 3.5), "closed_ground": (1.5, 2.0)}


def _spring(p: dict[str, Any]) -> dict[str, Any]:
    """Adopted values of a cylindrical helical compression spring and a list of the parameters that do not agree
    (``problems``: (param, message)). 0 means "not given" for the derivable parameters."""
    from .coil import CLOSED_TURNS, coil_law

    d = p["wire_diameter"]
    form = p["end_form"]
    ne = 2 * CLOSED_TURNS if form != "open" else 0.0
    k, k_hi = SPRING_FREE_LENGTH_K[form]  # H0 = t·n + k·d adopted; t·n + k·d … t·n + k_hi·d accepted
    problems: list[tuple[str, str]] = []
    defaults: list[str] = []
    # diameters: OD = D + d = ID + 2d
    cand = [(name, v, v + off) for name, v, off in (("mean_diameter", p["mean_diameter"], 0.0), ("outer_diameter", p["outer_diameter"], -d),
                                                   ("inner_diameter", p["inner_diameter"], d)) if v > 0]
    if not cand:
        D = 16.0
        defaults.append("mean diameter 16 (no coil diameter given)")
    else:
        D = cand[0][2]
        bad = [c for c in cand if abs(c[2] - D) > 0.01]
        if bad:
            names = " / ".join(c[0] for c in cand)
            readings = ", ".join(f"{c[0]} {_g(c[1])} → D {_g(round(c[2], 4))}" for c in cand)
            problems.append((bad[0][0], f"{names} contradict each other with wire_diameter {_g(d)} (OD = D + d = ID + 2d): {readings}"))
    if D - d <= 0:
        problems.append(("wire_diameter", f"wire_diameter {_g(d)} leaves no inner diameter on a mean diameter of {_g(D)}"))
    # coils
    n, n1 = p["active_coils"], p["total_coils"]
    if n <= 0 and n1 <= 0:
        n = 6.0
        defaults.append("6 active coils (no coil count given)")
    if n > 0 and n1 > 0 and abs(n1 - (n + ne)) > 1e-6:
        problems.append(("total_coils", f"total_coils {_g(n1)} ≠ active_coils {_g(n)} + {_g(ne)} end turns ({form} ends): give one of "
                                        "them or make them agree"))
    if n <= 0:
        n = n1 - ne
    n1 = n + ne
    if n <= 0 or (form != "open" and n < 0.5):
        problems.append(("total_coils", f"total_coils {_g(n1)} leaves {_g(n)} active coils with {form} ends (at least "
                                        f"{'0.5' if form != 'open' else 'more than 0'})"))
    # pitch / free length: H0 = t·n + k·d … t·n + k_hi·d (SPRING_FREE_LENGTH_K; open ends: n = total coils)
    t, H0 = p["pitch"], p["free_length"]
    if t <= 0 and H0 <= 0:
        t = round(0.3 * D, 3)
        defaults.append(f"pitch 0.3 × D = {_g(t)} (neither pitch nor free length given)")
    nominal = (t * n + k * d) if t > 0 else None
    if t > 0 and H0 > 0 and nominal is not None:
        top, tol = t * n + k_hi * d, max(0.1, 0.01 * H0)
        if not nominal - tol <= H0 <= top + tol:
            rule = (f"H0 = t·n + {_g(k)}·d = {_g(round(nominal, 4))}" if k_hi == k else
                    f"GB/T 23935: H0 ≈ t·n + {_g(k)}·d … t·n + {_g(k_hi)}·d = {_g(round(nominal, 4))} … {_g(round(top, 4))}")
            problems.append(("free_length", f"free_length {_g(H0)} contradicts pitch {_g(t)}, active_coils {_g(n)} and wire_diameter "
                                            f"{_g(d)} with {form} ends ({rule})"))
    if H0 <= 0 and nominal is not None:
        H0 = nominal
    if t <= 0:
        t = (H0 - k * d) / n if n > 0 else 0.0
    H1 = p["installed_length"]
    if H1 > 0 and H1 > H0 + 1e-9:
        problems.append(("installed_length", f"installed_length {_g(H1)} exceeds the free length {_g(round(H0, 4))}"))
    H = H1 if H1 > 0 else H0
    out = {"wire_diameter": d, "mean_diameter": D, "outer_diameter": D + d, "inner_diameter": D - d, "active_coils": n,
           "total_coils": n1, "pitch": t, "free_length": H0, "installed_length": H1 if H1 > 0 else None,
           "state": "installed" if H1 > 0 else "free", "modelled_length": H, "end_form": form, "handedness": p["handedness"],
           "spring_index": D / d if d > 0 else None, "free_length_rule": (
               f"H0 = t·n + {_g(k)}·d" if k_hi == k else
               f"H0 = t·n + {_g(k)}·d (GB/T 23935, n1 = n + 2); t·n + {_g(k)}·d … t·n + {_g(k_hi)}·d accepted"),
           "defaults": defaults, "problems": problems}
    if not problems:
        try:
            law = coil_law(mean_diameter=D, s_rad=d, s_ax=d, turns=n1, length=H, end_form=form, handedness=p["handedness"])
            out["modelled_pitch"] = law.pitch
        except ValueError as exc:
            param = "installed_length" if H1 > 0 else ("free_length" if p["free_length"] > 0 else "pitch")
            why = (" (at the installed length)" if H1 > 0 else
                   f" (free length derived from pitch {_g(t)} as t·n + {_g(k)}·d)" if param == "pitch" else "")
            problems.append((param, str(exc) + why))
    return out


def _spring_check(p: dict[str, Any]) -> list[tuple[str, str]]:
    return list(_spring(p)["problems"])


def _spring_ops(p: dict[str, Any]) -> list[dict[str, Any]]:
    s = _spring(p)
    return [{"id": "F-01", "type": "coil",
             "label": f"compression spring d{_g(s['wire_diameter'])} D{_g(s['mean_diameter'])} n{_g(s['active_coils'])}/{_g(s['total_coils'])} "
                      f"H{_g(round(s['modelled_length'], 4))}",
             "mode": "new", "section": {"kind": "circle", "radius": s["wire_diameter"] / 2.0}, "mean_diameter": s["mean_diameter"],
             "turns": s["total_coils"], "length": round(s["modelled_length"], 9), "handedness": s["handedness"], "start_angle": 0.0,
             "end_form": s["end_form"]}]


def _spring_ports(p: dict[str, Any]) -> list[Datum]:
    s = _spring(p)
    return [_axis("port.axis", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), _plane("port.bottom", (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            _plane("port.top", (0.0, 0.0, round(s["modelled_length"], 9)), (0.0, 0.0, 1.0))]


def _spring_label(p: dict[str, Any]) -> str:
    s = _spring(p)
    return (f"Compression spring d{_g(s['wire_diameter'])}×D{_g(s['mean_diameter'])}×H0 {_g(round(s['free_length'], 3))} "
            f"n{_g(s['active_coils'])}/n1 {_g(s['total_coils'])} {s['handedness']} {s['end_form']}")


def _spring_notes(p: dict[str, Any]) -> list[str]:
    s = _spring(p)
    out = [f"OD {_g(round(s['outer_diameter'], 4))} / D {_g(round(s['mean_diameter'], 4))} / ID {_g(round(s['inner_diameter'], 4))}, "
           f"wire Ø{_g(s['wire_diameter'])}, {_g(s['active_coils'])} active / {_g(s['total_coils'])} total coils, pitch "
           f"{_g(round(s['pitch'], 4))}, free length {_g(round(s['free_length'], 4))}, {s['handedness']}-hand, {s['end_form']}"]
    out.append(f"free length rule {s['free_length_rule']}")
    if s["state"] == "installed":
        out.append(f"installed state: the active coils uniformly compressed to {_g(s['modelled_length'])}; end turns unchanged")
    else:
        out.append("free state")
    out.append(f"modelled active pitch {_g(round(s.get('modelled_pitch', 0.0), 4))} so the modelled length is exact")
    if s["end_form"] != "open":
        out.append("closed end turns keep a 1 % gap between neighbouring turns (no wire contact) with a quarter-turn pitch "
                   "transition; real coiling transitions differ")
    if s["end_form"] == "closed_ground":
        out.append("ground ends are flat cuts at 0 and the length (about half of each end turn ground; real springs about 3/4 turn)")
    out.append("static nominal shape: spring rate, load, stress and buckling are not calculated")
    if s["defaults"]:
        out.append("template defaults adopted: " + "; ".join(s["defaults"]))
    return out


def _spring_derived(p: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in _spring(p).items() if k != "problems"}


_LEN = dict(unit="mm", min=0.1, max=5000.0)
_OPT = dict(unit="mm", min=0.0, max=5000.0)
_HAND_WORDS = (("right", ("右旋", "right-hand", "right hand")), ("left", ("左旋", "left-hand", "left hand")))

TEMPLATES: dict[str, Template] = {t.id: t for t in (
    Template("hex_bolt", 1, "Hex bolt", "六角头螺栓", "fastener", "simplified", "standard", "ISO 4017 / ISO 4014 head sizes (nominal)",
             (Param("size", "choice", "M8", choices=METRIC_SIZES, label_en="Size", label_zh="规格"),
              Param("length", "float", 30.0, unit="mm", min=4.0, max=400.0, label_en="Length under head", label_zh="公称长度（头下）"),
              _MATERIAL),
             _bolt_ops, _bolt_ports, lambda p: f"Hex bolt {p['size']}×{_g(p['length'])}", _bolt_check,
             lambda p: ["thread not modelled: shank is a plain cylinder at the nominal diameter"],
             ("port.axis", "port.seat", "port.head_top", "port.tip")),
    Template("hex_nut", 1, "Hex nut", "六角螺母", "fastener", "simplified", "standard", "ISO 4032 nut sizes (nominal)",
             (Param("size", "choice", "M8", choices=METRIC_SIZES, label_en="Size", label_zh="规格"), _MATERIAL),
             _nut_ops, _nut_ports, lambda p: f"Hex nut {p['size']}", notes=lambda p: ["thread not modelled: bore at the nominal diameter"],
             port_names=("port.axis", "port.seat", "port.top")),
    Template("flat_washer", 1, "Flat washer", "平垫圈", "fastener", "nominal", "standard", "ISO 7089 normal series (nominal)",
             (Param("size", "choice", "M8", choices=(*METRIC_SIZES, "custom"), label_en="Size", label_zh="规格"),
              Param("inner_diameter", "float", 8.4, label_en="Inner diameter (custom)", label_zh="内径（自定义）", **_LEN),
              Param("outer_diameter", "float", 16.0, label_en="Outer diameter (custom)", label_zh="外径（自定义）", **_LEN),
              Param("thickness", "float", 1.6, label_en="Thickness (custom)", label_zh="厚度（自定义）", **_LEN), _MATERIAL),
             _washer_ops, _washer_ports,
             lambda p: f"Flat washer {p['size']}" if p["size"] != "custom" else
             f"Flat washer Ø{_g(p['outer_diameter'])}/Ø{_g(p['inner_diameter'])}×{_g(p['thickness'])}", _washer_check,
             port_names=("port.axis", "port.bottom", "port.top")),
    Template("dowel_pin", 1, "Dowel / parallel pin", "圆柱销", "pin", "nominal", "standard", "ISO 2338 / ISO 8734 form (ends simplified)",
             (Param("diameter", "float", 6.0, label_en="Diameter", label_zh="直径", **_LEN),
              Param("length", "float", 20.0, label_en="Length", label_zh="长度", **_LEN),
              Param("chamfer", "float", 0.0, unit="mm", min=0.0, max=50.0, label_en="End chamfer", label_zh="端部倒角"), _MATERIAL),
             _pin_ops, _pin_ports, lambda p: f"Pin Ø{_g(p['diameter'])}×{_g(p['length'])}", _pin_check,
             lambda p: ["end form simplified to a chamfer"] if p["chamfer"] > 0 else [],
             ("port.axis", "port.end_bottom", "port.end_top")),
    Template("o_ring", 1, "O-ring", "O 形圈", "seal", "nominal", "buy", None,
             (Param("inner_diameter", "float", 20.0, label_en="Inner diameter d1", label_zh="内径 d1", **_LEN),
              Param("cross_section", "float", 2.5, label_en="Cross-section d2", label_zh="截面直径 d2", **_LEN),
              Param("state", "choice", "free", choices=("free", "compressed_nominal"), label_en="State", label_zh="状态"),
              Param("squeeze_pct", "float", 20.0, unit="%", min=0.0, max=40.0, label_en="Squeeze (compressed)", label_zh="压缩率（压缩态）"),
              _MATERIAL),
             _oring_ops, _oring_ports,
             lambda p: f"O-ring {_g(p['inner_diameter'])}×{_g(p['cross_section'])}" + (" (compressed, nominal)" if p["state"] != "free" else ""),
             _oring_check,
             lambda p: ["static nominal shape; compression and elastic behaviour are not simulated"]
             + (["compressed section: area-preserving stadium at the stated squeeze"] if p["state"] != "free" else []),
             ("port.axis", "port.mid_plane", "port.bottom", "port.top")),
    Template("ring_gasket", 1, "Ring gasket", "环形垫片", "seal", "nominal", "make", None,
             (Param("outer_diameter", "float", 60.0, label_en="Outer diameter", label_zh="外径", **_LEN),
              Param("inner_diameter", "float", 30.0, label_en="Inner diameter", label_zh="内径", **_LEN),
              Param("thickness", "float", 2.0, label_en="Thickness", label_zh="厚度", **_LEN),
              Param("hole_count", "int", 0, unit=None, min=0, max=64, label_en="Bolt holes", label_zh="螺栓孔数"),
              Param("hole_diameter", "float", 6.6, label_en="Bolt hole diameter", label_zh="螺栓孔径", **_LEN),
              Param("pcd", "float", 45.0, label_en="Bolt circle diameter", label_zh="分布圆直径", **_LEN), _MATERIAL),
             _ring_gasket_ops, _ring_gasket_ports,
             lambda p: f"Ring gasket Ø{_g(p['outer_diameter'])}/Ø{_g(p['inner_diameter'])}×{_g(p['thickness'])}", _ring_gasket_check,
             lambda p: ["static nominal (uncompressed) shape"], ("port.axis", "port.bottom", "port.top", "port.hole[i]")),
    Template("rect_gasket", 1, "Rectangular gasket", "矩形垫片", "seal", "nominal", "make", None,
             (Param("length", "float", 120.0, label_en="Length (X)", label_zh="长（X）", **_LEN),
              Param("width", "float", 80.0, label_en="Width (Y)", label_zh="宽（Y）", **_LEN),
              Param("thickness", "float", 2.0, label_en="Thickness", label_zh="厚度", **_LEN),
              Param("corner_radius", "float", 0.0, unit="mm", min=0.0, max=2500.0, label_en="Corner radius", label_zh="圆角半径"),
              Param("inner_length", "float", 0.0, unit="mm", min=0.0, max=5000.0, label_en="Window length (0 = none)", label_zh="窗口长（0 为无）"),
              Param("inner_width", "float", 0.0, unit="mm", min=0.0, max=5000.0, label_en="Window width", label_zh="窗口宽"),
              Param("inner_corner_radius", "float", 0.0, unit="mm", min=0.0, max=2500.0, label_en="Window corner radius", label_zh="窗口圆角"),
              Param("hole_diameter", "float", 0.0, unit="mm", min=0.0, max=500.0, label_en="Corner hole diameter (0 = none)", label_zh="角孔直径（0 为无）"),
              Param("hole_inset", "float", 8.0, label_en="Corner hole inset", label_zh="角孔边距", **_LEN), _MATERIAL),
             _rect_gasket_ops, _rect_gasket_ports, lambda p: f"Gasket {_g(p['length'])}×{_g(p['width'])}×{_g(p['thickness'])}",
             _rect_gasket_check, lambda p: ["static nominal (uncompressed) shape"], ("port.bottom", "port.top", "port.hole[i]")),
    Template("layered_sleeve", 1, "Multi-layer sleeve / collar", "多层套环 / 轴套", "sleeve", "nominal", "make", None,
             (Param("bore", "float", 12.2, label_en="Bore diameter", label_zh="内孔直径", **_LEN),
              Param("od1", "float", 24.0, label_en="Layer 1 outer diameter", label_zh="第 1 层外径", **_LEN),
              Param("length1", "float", 20.0, unit="mm", min=0.0, max=5000.0, label_en="Layer 1 length", label_zh="第 1 层长度"),
              Param("od2", "float", 30.0, label_en="Layer 2 outer diameter", label_zh="第 2 层外径", **_LEN),
              Param("length2", "float", 0.0, unit="mm", min=0.0, max=5000.0, label_en="Layer 2 length (0 = none)", label_zh="第 2 层长度（0 为无）"),
              Param("od3", "float", 36.0, label_en="Layer 3 outer diameter", label_zh="第 3 层外径", **_LEN),
              Param("length3", "float", 0.0, unit="mm", min=0.0, max=5000.0, label_en="Layer 3 length (0 = none)", label_zh="第 3 层长度（0 为无）"),
              _MATERIAL),
             _sleeve_ops, _sleeve_ports,
             lambda p: "Sleeve " + "/".join(f"Ø{_g(od)}×{_g(ln)}" for od, ln in _sleeve_layers(p)) + f" bore Ø{_g(p['bore'])}",
             _sleeve_check, port_names=("port.axis", "port.bottom", "port.top", "port.shoulder[i]")),
    Template("plate", 1, "Plate", "平板", "plate", "exact", "make", None,
             (Param("length", "float", 100.0, label_en="Length (X)", label_zh="长（X）", **_LEN),
              Param("width", "float", 60.0, label_en="Width (Y)", label_zh="宽（Y）", **_LEN),
              Param("thickness", "float", 10.0, label_en="Thickness", label_zh="厚度", **_LEN),
              Param("corner_radius", "float", 0.0, unit="mm", min=0.0, max=2500.0, label_en="Corner radius", label_zh="圆角半径"),
              Param("hole_diameter", "float", 0.0, unit="mm", min=0.0, max=500.0, label_en="Hole diameter (0 = none)", label_zh="孔径（0 为无）"),
              Param("holes_x", "int", 2, unit=None, min=0, max=50, label_en="Holes along X", label_zh="X 向孔数"),
              Param("holes_y", "int", 2, unit=None, min=0, max=50, label_en="Holes along Y", label_zh="Y 向孔数"),
              Param("pitch_x", "float", 70.0, label_en="Hole pitch X", label_zh="X 向孔距", **_LEN),
              Param("pitch_y", "float", 40.0, label_en="Hole pitch Y", label_zh="Y 向孔距", **_LEN), _MATERIAL),
             _plate_ops, _plate_ports, lambda p: f"Plate {_g(p['length'])}×{_g(p['width'])}×{_g(p['thickness'])}", _plate_check,
             port_names=("port.bottom", "port.top", "port.hole[i]")),
    Template("bent_bracket", 1, "Bent bracket (L / angle)", "折弯支架（L 形 / 角度）", "bracket", "exact", "make", None,
             (Param("thickness", "float", 4.0, label_en="Thickness", label_zh="板厚", **_LEN),
              Param("width", "float", 40.0, label_en="Width", label_zh="宽度", **_LEN),
              Param("leg1", "float", 60.0, label_en="Base leg (outside)", label_zh="底边（外形）", **_LEN),
              Param("leg2", "float", 44.0, label_en="Second leg (outside)", label_zh="立边（外形）", **_LEN),
              Param("bend_angle", "float", 90.0, unit="deg", min=1.0, max=175.0, label_en="Bend angle", label_zh="折弯角"),
              Param("inner_radius", "float", 2.0, unit="mm", min=0.0, max=500.0, label_en="Inner bend radius", label_zh="内弯半径"),
              Param("hole_diameter", "float", 0.0, unit="mm", min=0.0, max=500.0, label_en="Hole diameter (0 = none)", label_zh="孔径（0 为无）"),
              Param("holes_leg1", "int", 2, unit=None, min=0, max=10, label_en="Holes in base leg", label_zh="底边孔数"),
              Param("holes_leg2", "int", 0, unit=None, min=0, max=10, label_en="Holes in second leg", label_zh="立边孔数"), _MATERIAL),
             _bracket_ops, _bracket_ports,
             lambda p: f"Bracket t{_g(p['thickness'])} {_g(p['leg1'])}×{_g(p['leg2'])}×{_g(p['width'])} {_g(p['bend_angle'])}°",
             _bracket_check, lambda p: ["formed shape only; no unfolding / bend allowance"],
             ("port.base", "port.wall", "port.hole[i]")),
    Template("split_lock_washer", 1, "Split lock washer (spring washer)", "开口式弹簧垫圈", "fastener", "nominal", "standard",
             "GB/T 93-1987 normal series (nominal d min, s = b; free height 2s … 2.5s)",
             (Param("size", "choice", "M8", choices=(*LOCK_WASHER, "custom"), label_en="Size", label_zh="规格"),
              Param("inner_diameter", "float", 8.1, label_en="Inner diameter (custom)", label_zh="内径（自定义）", **_LEN),
              Param("outer_diameter", "float", 12.3, label_en="Outer diameter (custom)", label_zh="外径（自定义）", **_LEN),
              Param("thickness", "float", 2.1, label_en="Section thickness s (custom)", label_zh="截面厚度 s（自定义）", **_LEN),
              Param("free_height", "float", 0.0, label_en="Free height H (0 = 2 × s; = s: flattened)", label_zh="自由高度 H（0 为 2s；等于 s 为压平）",
                    **_OPT),
              Param("gap", "float", 0.0, label_en="Split gap (0 = 0.5 × s)", label_zh="开口宽度（0 为 0.5s）", **_OPT),
              Param("handedness", "choice", "right", choices=("right", "left"), label_en="Handedness", label_zh="旋向",
                    aliases=_HAND_WORDS), _MATERIAL),
             _lock_washer_ops, _lock_washer_ports, _lock_washer_label, _lock_washer_check, _lock_washer_notes,
             ("port.axis", "port.bottom", "port.top"),
             derived=lambda p: {k: v for k, v in _lock_washer(p).items()}),
    Template("compression_spring", 2, "Cylindrical helical compression spring", "圆柱螺旋压缩弹簧", "spring", "nominal", "buy",
             "GB/T 23935 geometry (H0 ≈ t·n + d open; t·n + 3d … 3.5d closed; t·n + 1.5d … 2d closed and ground, "
             "the GB value adopted when deriving); no load calculation",
             (Param("wire_diameter", "float", 2.0, unit="mm", min=0.1, max=100.0, label_en="Wire diameter d", label_zh="线径 d"),
              Param("mean_diameter", "float", 0.0, label_en="Mean diameter D (0 = from OD / ID)", label_zh="中径 D（0 为由外径 / 内径推出）", **_OPT),
              Param("outer_diameter", "float", 0.0, label_en="Outer diameter (0 = derived)", label_zh="外径（0 为推出）", **_OPT),
              Param("inner_diameter", "float", 0.0, label_en="Inner diameter (0 = derived)", label_zh="内径（0 为推出）", **_OPT),
              Param("active_coils", "float", 0.0, unit=None, min=0.0, max=98.0, label_en="Active coils n (0 = total − end turns)",
                    label_zh="有效圈数 n（0 为总圈数减支承圈）"),
              Param("total_coils", "float", 0.0, unit=None, min=0.0, max=100.0, label_en="Total coils n1 (0 = n + end turns)",
                    label_zh="总圈数 n1（0 为 n 加支承圈）"),
              Param("pitch", "float", 0.0, label_en="Pitch t (0 = from free length)", label_zh="节距 t（0 为由自由高度推出）", **_OPT),
              Param("free_length", "float", 0.0, label_en="Free length H0 (0 = from pitch)", label_zh="自由高度 H0（0 为由节距推出）", **_OPT),
              Param("installed_length", "float", 0.0, label_en="Installed length H1 (0 = free state)", label_zh="安装高度 H1（0 为自由态）",
                    **_OPT),
              Param("handedness", "choice", "right", choices=("right", "left"), label_en="Handedness", label_zh="旋向",
                    aliases=_HAND_WORDS),
              Param("end_form", "choice", "closed_ground", choices=("closed_ground", "closed", "open"), label_en="End form",
                    label_zh="端部形式（并紧磨平 / 并紧 / 开口）",
                    aliases=(("closed_ground", ("并紧磨平", "磨平", "closed and ground", "squared and ground", "ground ends")),
                             ("closed", ("并紧", "squared", "closed ends")),
                             ("open", ("开口", "不并紧", "plain ends", "open ends")))), _MATERIAL),
             _spring_ops, _spring_ports, _spring_label, _spring_check, _spring_notes, ("port.axis", "port.bottom", "port.top"),
             derived=_spring_derived),
    Template("open_box", 1, "Thin-wall open box", "薄壁开口箱体", "box", "exact", "make", None,
             (Param("length", "float", 120.0, label_en="Length (X)", label_zh="长（X）", **_LEN),
              Param("width", "float", 80.0, label_en="Width (Y)", label_zh="宽（Y）", **_LEN),
              Param("height", "float", 50.0, label_en="Height", label_zh="高", **_LEN),
              Param("wall", "float", 3.0, label_en="Wall thickness", label_zh="壁厚", **_LEN),
              Param("bottom", "float", 3.0, label_en="Bottom thickness", label_zh="底厚", **_LEN),
              Param("partitions", "int", 0, unit=None, min=0, max=20, label_en="Partitions across X", label_zh="X 向隔板数"),
              Param("partition_thickness", "float", 3.0, label_en="Partition thickness", label_zh="隔板厚", **_LEN), _MATERIAL),
             _box_ops, _box_ports, lambda p: f"Open box {_g(p['length'])}×{_g(p['width'])}×{_g(p['height'])} t{_g(p['wall'])}",
             _box_check, port_names=("port.bottom", "port.rim", "port.inner_floor")),
)}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def catalog() -> list[dict[str, Any]]:
    """Every template with its typed parameters (for the editor dialog, the API and the modeling prompt)."""
    return [t.catalog_entry() for t in TEMPLATES.values()]


def resolve_params(template: Template, params: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults filled, types and ranges checked; unknown names are an error (never ignored)."""
    params = dict(params or {})
    problems: list[dict[str, str]] = []
    known = {p.name: p for p in template.params}
    for name in params:
        if name not in known:
            problems.append({"param": name, "message": f"unknown parameter (known: {', '.join(known)})"})
    out: dict[str, Any] = {}
    for p in template.params:
        v = params.get(p.name, p.default)
        if p.type == "choice":
            if v not in p.choices:
                problems.append({"param": p.name, "message": f"must be one of {', '.join(p.choices)}"})
                v = p.default
        elif p.type == "text":
            v = "" if v is None else str(v).strip()[:120]
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                problems.append({"param": p.name, "message": "must be a number"})
                v = p.default
            elif p.type == "int" and float(v) != int(v):
                problems.append({"param": p.name, "message": "must be a whole number"})
                v = int(v)
            v = int(v) if p.type == "int" else float(v)
            if (p.min is not None and v < p.min) or (p.max is not None and v > p.max):
                rng = f"{_g(p.min) if p.min is not None else '−∞'} … {_g(p.max) if p.max is not None else '∞'}"
                problems.append({"param": p.name, "message": f"must be within {rng}{' ' + p.unit if p.unit else ''}"})
        out[p.name] = v
    if not problems:
        problems += [{"param": n, "message": m} for n, m in template.check(out)]
    if problems:
        raise TemplateError("template_param_invalid", f"{template.id}: " + "; ".join(f"{q['param']}: {q['message']}" for q in problems),
                            problems)
    return out


def instantiate(template_id: str, params: dict[str, Any] | None = None) -> tuple[ModelSpec, dict[str, Any]]:
    """The op list and metadata of one template instance.

    ``meta``: ``template_id``, ``template_version``, ``title``, ``category``, resolved ``params``, ``representation``,
    ``procurement``, ``material`` / ``material_source`` (``parameter`` or ``unspecified`` — never invented),
    ``standard``, ``spec_label``, ``notes`` (what is simplified), ``ports`` (``port.<name>`` datums as dicts) and
    ``op_ids``. Raises :class:`TemplateError`."""
    t = TEMPLATES.get(template_id)
    if t is None:
        raise TemplateError("template_unknown", f"unknown template {template_id!r} (known: {', '.join(TEMPLATES)})")
    p = resolve_params(t, params)
    try:
        spec = ModelSpec.model_validate({"ops": t.build(p)})
    except ValueError as exc:  # a parameter combination the op validation refuses
        raise TemplateError("template_param_invalid", f"{template_id}: {exc}") from exc
    ports = [{"name": d.name, "kind": d.kind, "origin": [round(c, 9) + 0.0 for c in d.origin],
              "direction": [round(c, 12) + 0.0 for c in d.direction], "x_dir": [round(c, 12) + 0.0 for c in d.x_dir]}
             for d in t.ports(p)]
    material = p.get("material") or None
    meta = {
        "schema": "partroute.part_template/1",
        "template_id": t.id,
        "template_version": t.version,
        "title": {"en": t.title_en, "zh": t.title_zh},
        "category": t.category,
        "params": p,
        "representation": t.representation,
        "procurement": t.procurement,
        "material": material,
        "material_source": "parameter" if material else "unspecified",
        "standard": t.standard,
        "spec_label": t.label(p),
        "notes": t.notes(p),
        "ports": ports,
        "op_ids": [op.id for op in spec.ops],
    }
    if t.derived is not None:
        meta["derived"] = {k: (round(v, 9) + 0.0 if isinstance(v, float) else v) for k, v in t.derived(p).items()}
    return spec, meta


def renumbered(spec: ModelSpec, meta: dict[str, Any], first: int) -> tuple[ModelSpec, dict[str, Any]]:
    """The instance with op ids starting at ``F-<first>`` (appending to a part with history); refs follow."""
    from .ops import remap_refs

    mapping = {op.id: f"F-{first + i:02d}" for i, op in enumerate(spec.ops)}
    data = spec.model_dump(mode="json")
    for op in data["ops"]:
        op["id"] = mapping[op["id"]]
        remap_refs(op, mapping)
    return ModelSpec.model_validate(data), {**meta, "op_ids": [mapping[i] for i in meta["op_ids"]]}
