"""Text geometry: font choice and glyph outlines for the ``text`` op (generation reliability M, design §2.7).

Fonts are never chosen by OCCT's font manager: asked for a font it does not index (most ``.ttc`` CJK collections)
it silently falls back to Arial, which has no CJK glyphs. Here the font files are indexed with fontTools (a
build123d dependency) and a face is usable only when its character map covers every non-space character of the
text. Order: the requested font, then the installed CJK fonts of :data:`FALLBACK_FAMILIES` (WenQuanYi Micro Hei,
Noto / Source Han Sans CJK, PingFang, Heiti, Hiragino, Songti, Arial Unicode), then DejaVu Sans (Latin only). A
font other than the requested one is a substitution, reported by the caller as a deviation (requested, used,
impact). No usable face at all is :class:`FontUnavailable` (``unsupported_font``): deterministic, not worth a
retry until the text or the installed fonts change.

Glyphs are OCCT's exact outlines (``StdPrs_BRepFont`` on the chosen file and face index): planar faces in the XY
plane, the text block centred on the origin, ``height`` = the font size (em height: CJK glyphs span about 0.9 of
it, Latin capitals about 0.7).

Font directories: ``PARTROUTE_FONT_DIRS`` (os.pathsep separated) or the platform's system and user font folders.
Pure: no database.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")

#: Fallback order after the requested font: (family, other names of the same family). CJK first (design §2.7).
FALLBACK_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("WenQuanYi Micro Hei", ("文泉驿微米黑", "文泉驛微米黑", "WenQuanYi Micro Hei Mono")),
    ("Noto Sans CJK SC", ("Noto Sans SC", "Source Han Sans SC", "Source Han Sans CN", "思源黑体")),
    ("WenQuanYi Zen Hei", ("文泉驿正黑", "文泉驛正黑")),
    ("PingFang SC", ("苹方-简", "蘋方-簡")),
    ("Heiti SC", ("STHeiti", "黑体-简", "黑體-簡", "华文黑体")),
    ("Hiragino Sans GB", ("冬青黑体简体中文",)),
    ("Songti SC", ("STSong", "宋体-简", "华文宋体")),
    ("Arial Unicode MS", ()),
    ("DejaVu Sans", ()),
)

SUBSTITUTION_IMPACT = ("the letter shapes follow the substitute font; the text, character height, depth (raised / engraved), "
                       "position and rotation are as requested")


class FontUnavailable(Exception):
    """No installed font covers the text (``unsupported_font``)."""

    code = "unsupported_font"

    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.details = details


@dataclass(frozen=True)
class FontFace:
    path: str
    index: int
    family: str  # the English family name when the font has one
    names: frozenset[str]  # lower-cased family / full / typographic names, every language
    style: str  # subfamily, lower case ("regular", "medium", "bold", …)


@dataclass(frozen=True)
class FontChoice:
    face: FontFace
    requested: str | None
    substituted: bool
    reason: str | None = None  # not_installed | missing_glyphs (of the requested font)
    missing: tuple[str, ...] = ()  # characters of the text the requested font lacks

    def brief(self) -> dict[str, Any]:
        return {"requested": self.requested, "used": self.face.family, "file": os.path.basename(self.face.path),
                "face_index": self.face.index, "substituted": self.substituted, "reason": self.reason,
                **({"missing": list(self.missing)} if self.missing else {})}


@dataclass
class _Index:
    dirs: tuple[str, ...]
    faces: list[FontFace] = field(default_factory=list)
    cmaps: dict[tuple[str, int], frozenset[int]] = field(default_factory=dict)


_LOCK = threading.Lock()
_INDEX: _Index | None = None


def font_dirs() -> tuple[str, ...]:
    env = os.environ.get("PARTROUTE_FONT_DIRS")
    if env is not None:
        return tuple(d for d in env.split(os.pathsep) if d)
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return ("/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library/Fonts"))
    if sys.platform.startswith("win"):  # pragma: no cover - not a deployment target
        return (os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),)
    return ("/usr/share/fonts", "/usr/local/share/fonts", os.path.join(home, ".local/share/fonts"), os.path.join(home, ".fonts"))


def _scan(dirs: tuple[str, ...]) -> list[FontFace]:
    from fontTools.ttLib import TTCollection, TTFont

    out: list[FontFace] = []
    files: list[str] = []
    for d in dirs:
        for root, _subdirs, names in os.walk(d):
            files += [os.path.join(root, n) for n in names if n.lower().endswith(FONT_EXTENSIONS)]
    for path in sorted(set(files)):
        try:
            fonts = TTCollection(path, lazy=True).fonts if path.lower().endswith((".ttc", ".otc")) else [TTFont(path, lazy=True)]
        except Exception:  # unreadable or not a font: skipped
            continue
        for i, font in enumerate(fonts):
            try:
                table = font["name"]
            except Exception:
                continue
            names: set[str] = set()
            family = style = ""
            for rec in table.names:
                if rec.nameID not in (1, 2, 4, 16, 17):
                    continue
                try:
                    text = rec.toUnicode().strip()
                except Exception:
                    continue
                if not text:
                    continue
                english = rec.platformID == 3 and rec.langID == 0x409 or rec.platformID == 1 and rec.langID == 0
                if rec.nameID in (1, 4, 16):
                    names.add(text.lower())
                if rec.nameID == 16 and english or rec.nameID == 1 and english and not family:
                    family = text
                if rec.nameID in (2, 17) and english and (rec.nameID == 17 or not style):
                    style = text.lower()
            if names:
                out.append(FontFace(path=path, index=i, family=family or sorted(names)[0], names=frozenset(names), style=style))
    return out


def _index() -> _Index:
    global _INDEX
    dirs = font_dirs()
    with _LOCK:
        if _INDEX is None or _INDEX.dirs != dirs:
            _INDEX = _Index(dirs=dirs, faces=_scan(dirs))
        return _INDEX


def _cmap(idx: _Index, face: FontFace) -> frozenset[int]:
    key = (face.path, face.index)
    with _LOCK:
        hit = idx.cmaps.get(key)
    if hit is not None:
        return hit
    from fontTools.ttLib import TTCollection, TTFont

    try:
        font = TTCollection(face.path, lazy=True).fonts[face.index] if face.path.lower().endswith((".ttc", ".otc")) else TTFont(face.path, lazy=True)
        cmap = frozenset((font.getBestCmap() or {}).keys())
    except Exception:
        cmap = frozenset()
    with _LOCK:
        idx.cmaps[key] = cmap
    return cmap


_STYLE_RANK = {"regular": 0, "book": 0, "normal": 0, "roman": 0, "medium": 1, "w3": 1, "w4": 1, "light": 2}


def find_faces(name: str) -> list[FontFace]:
    """Installed faces whose family / full / typographic name equals ``name`` (case-insensitive), regular first."""
    want = name.strip().lower()
    idx = _index()
    hits = [f for f in idx.faces if want in f.names]
    return sorted(hits, key=lambda f: (_STYLE_RANK.get(f.style, 5), f.path, f.index))


def _needed(text: str) -> set[int]:
    return {ord(c) for c in text if not c.isspace()}


def _covering(faces: list[FontFace], needed: set[int]) -> tuple[FontFace | None, set[int]]:
    """The first face covering ``needed``, else (None, the characters the best face lacks)."""
    idx = _index()
    best: set[int] | None = None
    for f in faces:
        lack = needed - _cmap(idx, f)
        if not lack:
            return f, set()
        if best is None or len(lack) < len(best):
            best = lack
    return None, best or set()


def resolve_font(text: str, requested: str | None = None) -> FontChoice:
    """The face that draws ``text``: the requested font when installed and covering every character, else the first
    covering font of :data:`FALLBACK_FAMILIES` (a substitution when a font was requested). Raises
    :class:`FontUnavailable`."""
    needed = _needed(text)
    requested = (requested or "").strip() or None
    reason: str | None = None
    missing: set[int] = set()
    if requested is not None:
        faces = find_faces(requested)
        if faces:
            face, missing = _covering(faces, needed)
            if face is not None:
                return FontChoice(face=face, requested=requested, substituted=False)
            reason = "missing_glyphs"
        else:
            reason = "not_installed"
    tried: list[str] = []
    uncovered: set[int] | None = None
    for family, aliases in FALLBACK_FAMILIES:
        faces = []
        for n in (family, *aliases):
            faces += [f for f in find_faces(n) if f not in faces]
        if not faces:
            continue
        tried.append(family)
        face, lack = _covering(faces, needed)
        if face is not None:
            return FontChoice(face=face, requested=requested, substituted=requested is not None, reason=reason,
                              missing=tuple(sorted(chr(c) for c in missing)))
        uncovered = lack if uncovered is None else uncovered & lack
    lack_chars = sorted(chr(c) for c in (uncovered if uncovered is not None else needed))
    what = f"the requested font {requested!r} is {'not installed' if reason == 'not_installed' else 'missing glyphs'} and " if requested else ""
    raise FontUnavailable(
        f"no usable font: {what}no installed font covers {''.join(lack_chars[:20])!r}"
        f"{' (fonts tried: ' + ', '.join(tried) + ')' if tried else ' (no fallback font is installed)'}",
        {"requested": requested, "reason": reason, "missing": lack_chars[:50], "fonts_tried": tried, "font_dirs": list(_index().dirs)})


def text_faces(text: str, choice: FontChoice, height: float) -> list[Any]:
    """The glyph faces (build123d ``Face``) of one line of ``text`` in the XY plane, em height ``height`` mm, the
    text block's bounding box centred on the origin."""
    import build123d as bd
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Graphic3d import Graphic3d_HorizontalTextAlignment, Graphic3d_VerticalTextAlignment
    from OCP.gp import gp_Ax3
    from OCP.NCollection import NCollection_String
    from OCP.StdPrs import StdPrs_BRepFont, StdPrs_BRepTextBuilder

    font = StdPrs_BRepFont()
    if not font.Init(NCollection_String(choice.face.path), float(height), int(choice.face.index)):
        raise RuntimeError(f"the font file {os.path.basename(choice.face.path)} cannot be loaded")
    shape = StdPrs_BRepTextBuilder().Perform(font, NCollection_String(text), gp_Ax3(),
                                             Graphic3d_HorizontalTextAlignment.Graphic3d_HTA_LEFT,
                                             Graphic3d_VerticalTextAlignment.Graphic3d_VTA_BOTTOM)
    if shape is None or shape.IsNull():
        raise RuntimeError("the font gave no outline for the text")
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise RuntimeError("the font gave no outline for the text")
    lo, hi = box.CornerMin(), box.CornerMax()
    shift = bd.Vector(-(lo.X() + hi.X()) / 2.0, -(lo.Y() + hi.Y()) / 2.0, 0.0)
    faces = list(bd.Compound(shape).moved(bd.Location(shift)).faces())
    if not faces:
        raise RuntimeError("the font gave no outline for the text")
    return faces


V3 = tuple[float, float, float]


@dataclass(frozen=True)
class TextRegion:
    """The slab a built ``text`` op occupies: its text block (``half_w`` × ``half_h`` about the frame origin, in the
    frame's x / y) between ``s0`` and ``s1`` along the frame normal. Drawings leave faces inside it out of the feature
    analysis (letters are not holes, pockets or steps to dimension)."""

    op_id: str
    text: str
    origin: V3
    x: V3
    y: V3
    z: V3
    half_w: float
    half_h: float
    s0: float
    s1: float
    detail: dict[str, Any] = field(default_factory=dict, compare=False)

    def contains(self, p: Any) -> bool:
        px, py, pz = (float(p[0]), float(p[1]), float(p[2])) if isinstance(p, (tuple, list)) else (float(p.X), float(p.Y), float(p.Z))
        d = (px - self.origin[0], py - self.origin[1], pz - self.origin[2])
        m = 0.01 + 1e-4 * max(self.half_w, self.half_h)
        u = d[0] * self.x[0] + d[1] * self.x[1] + d[2] * self.x[2]
        v = d[0] * self.y[0] + d[1] * self.y[1] + d[2] * self.y[2]
        s = d[0] * self.z[0] + d[1] * self.z[1] + d[2] * self.z[2]
        return abs(u) <= self.half_w + m and abs(v) <= self.half_h + m and self.s0 - m <= s <= self.s1 + m


def text_regions(features: Any) -> list[TextRegion]:
    """Regions of the ``ok`` text features of a build record (``FeatureRecord`` objects or dicts)."""
    out: list[TextRegion] = []
    for f in features or []:
        get = f.get if isinstance(f, dict) else (lambda k, f=f: getattr(f, k, None))
        det = get("detail") or {}
        if get("type") != "text" or get("status") != "ok" or not isinstance(det, dict):
            continue
        fr, block, depth = det.get("frame") or {}, det.get("block") or [], det.get("depth")
        try:
            o, x, z = (tuple(float(c) for c in fr[k]) for k in ("origin", "x", "z"))
            y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
            w, h, dep = float(block[0]), float(block[1]), float(depth)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        s0, s1 = (0.0, dep) if dep > 0 else (dep, 0.0)
        out.append(TextRegion(op_id=str(get("op_id")), text=str(det.get("text") or ""), origin=o, x=x, y=y, z=z,  # type: ignore[arg-type]
                              half_w=w / 2.0, half_h=h / 2.0, s0=s0, s1=s1, detail=det))
    return out


__all__ = ["FALLBACK_FAMILIES", "FontChoice", "FontFace", "FontUnavailable", "SUBSTITUTION_IMPACT", "TextRegion", "find_faces",
           "font_dirs", "resolve_font", "text_faces", "text_regions"]
