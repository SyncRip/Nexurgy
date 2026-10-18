"""Real reading of engineering sources (W2, docs/development/mechanical-engineering/w0-contract.zh-CN.md §5, §7).

A source is direct text, a UTF-8 text / Markdown file version, a DXF / DWG file version (through the
drawing evidence index of ``cad/drawing_evidence.py``) or an image / PDF attachment. Every source is cut
into chunks with stable locators:

- text: paragraphs (blank-line separated; Markdown headings tracked) packed into chunks of at most
  :data:`CHUNK_CHARS` characters; a paragraph longer than that is cut at sentence ends. Every chunk after
  the first starts up to :data:`CHUNK_OVERLAP_CHARS` characters before the end of the previous one (S4), so a
  statement or quote that crosses a chunk boundary is whole in one chunk; ``meta.overlap`` is the number of
  leading characters repeated, and what was read is counted once (:func:`covered_chars`). Locator
  ``chars a–b · ¶p–q · <heading>``; the chunk text is the exact substring ``text[a:b]`` (overlap included);
- drawing: one or more chunks per frame (sheet) of the literal evidence index (title block, views,
  dimensions, texts, circles with their DXF entity handles). Locator ``S01 · <number> · <name> · lines a–b``;
  entities outside every frame are grouped by position (generation reliability K): groups with text / dimensions / a
  table are extra chunks (``U01 · outside frames · <range>``), geometry-only groups are excluded with a reason, and
  the source's ``coverage`` block says where every entity went — never silently dropped;
- attachment (PNG / JPEG / WEBP / PDF): one chunk sent as an inline attachment. It is located evidence
  only (regions / pages, no scale); the server cannot verify what the model read from it.

Nothing here decides what a source means and nothing is truncated: :func:`over_budget` lists every part
beyond :data:`MAX_CHUNKS` so the caller can refuse with an explicit error. Read status (read / partial /
unread / failed) is computed by the service from what it really sent (:func:`read_status`).
"""

from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from ..ai import limits
from ..models import Blob, File, FileVersion
from ..storage import read_blob

#: Target size of one chunk (characters) and the most chunks one spec version reads (W0 §7); defined in ai/limits.py.
CHUNK_CHARS = limits.SPEC_CHUNK_CHARS
MAX_CHUNKS = limits.SPEC_MAX_CHUNKS
#: Characters a text chunk repeats from the end of the previous chunk of the same source (S4); a chunk's text,
#: overlap included, stays within CHUNK_CHARS.
CHUNK_OVERLAP_CHARS = limits.SPEC_CHUNK_OVERLAP_CHARS
#: Text-like file names / content types read as UTF-8.
TEXT_EXTS = (".txt", ".md", ".markdown", ".text", ".csv", ".tsv", ".json", ".yaml", ".yml", ".ini", ".cfg")
ATTACHMENT_TYPES = ("image/png", "image/jpeg", "image/webp", "application/pdf")
#: Largest attachment sent inline (providers/base.py MAX_INLINE_BYTES).
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
#: Wall-clock budget for one drawing's evidence extraction.
DRAWING_BUDGET_S = 60.0

SourceKind = Literal["text", "file_version", "drawing"]
ChunkKind = Literal["text", "drawing", "attachment"]

_PARA_SPLIT = re.compile(r"\n[ \t\r]*\n+")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])|(?<=\.)\s|\n")
#: Where an overlap may begin: after whitespace or a sentence / clause mark (never in the middle of a word).
_OVERLAP_START = re.compile(r"[\s。！？；!?;，,、]")


@dataclass
class SourceChunk:
    source_id: str
    source_chunk: int
    kind: ChunkKind
    locator: str
    text: str
    start: int | None = None
    end: int | None = None
    data: bytes | None = None
    mime_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Source:
    """One source of a spec version, read and chunked. ``error`` set: nothing could be read."""

    id: str
    kind: SourceKind
    label: str
    name: str = ""
    file_version_id: str | None = None
    file_id: str | None = None
    version: int | None = None
    sha256: str = ""
    bytes: int = 0
    chars: int = 0  # text sources: the length of the whole text (read coverage is counted against it)
    content_type: str = ""
    answer_of: str | None = None
    chunks: list[SourceChunk] = field(default_factory=list)
    error: dict[str, Any] | None = None
    # Content that exists but is not part of any chunk (e.g. entities outside every drawing frame).
    not_chunked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # text: paragraph start offsets (¶ numbering); drawing: every citable evidence id.
    para_starts: list[int] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)
    # Drawing sources (generation reliability K, design §2.4): where every entity went — frames, outside groups read /
    # excluded / unexplained, quarantined. The service adds chunks / chunks_sent from the chunk rows.
    coverage: dict[str, Any] | None = None
    # Transient original drawing context. Kept outside the manifest; spec chunk
    # readers render selected frames lazily when the model supports vision.
    drawing_dxf: bytes | None = None
    drawing_evidence: Any | None = None

    def manifest(self) -> dict[str, Any]:
        """The manifest entry without read state (the service adds read_status / ranges from the chunks)."""
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "name": self.name,
            "file_version_id": self.file_version_id, "file_id": self.file_id, "version": self.version,
            "sha256": self.sha256, "bytes": self.bytes, "content_type": self.content_type,
            "answer_of": self.answer_of, "chunks": len(self.chunks), "not_chunked": list(self.not_chunked),
            "notes": list(self.notes), **({"chars": self.chars} if self.chars else {}),
            **({"coverage": self.coverage} if self.coverage is not None else {}),
        }


# --- text -----------------------------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def paragraphs(text: str) -> list[tuple[int, int]]:
    """(start, end) of every non-empty paragraph (blank-line separated), in order."""
    out: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA_SPLIT.finditer(text):
        if text[pos:m.start()].strip():
            out.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        out.append((pos, len(text)))
    return out


def _headings(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(2).strip()) for m in _HEADING.finditer(text)]


def _split_long(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Cut [start, end) into pieces of at most ``limit`` characters, at sentence ends when possible."""
    pieces: list[tuple[int, int]] = []
    s = start
    while end - s > limit:
        window = text[s:s + limit]
        cuts = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() > limit // 2]
        cut = s + (cuts[-1] if cuts else limit)
        pieces.append((s, cut))
        s = cut
    if end > s:
        pieces.append((s, end))
    return pieces


def _pack(text: str, limit: int, later_limit: int) -> list[tuple[int, int]]:
    """Whole paragraphs packed into non-overlapping chunks: the first up to ``limit`` chars, the others up to
    ``later_limit`` (room is left for their overlap). A paragraph longer than ``later_limit`` is cut at sentence ends."""
    units: list[tuple[int, int]] = []
    for s, e in paragraphs(text):
        units.extend(_split_long(text, s, e, later_limit) if e - s > later_limit else [(s, e)])
    chunks: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for s, e in units:
        if cur is None:
            cur = (s, e)
        elif e - cur[0] <= (later_limit if chunks else limit):
            cur = (cur[0], e)
        else:
            chunks.append(cur)
            cur = (s, e)
    if cur is not None:
        chunks.append(cur)
    return chunks


def chunk_text(text: str, *, limit: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[tuple[int, int]]:
    """Chunk (start, end) offsets: whole paragraphs packed up to ``limit`` chars, long paragraphs cut; every chunk
    after the first starts at most ``overlap`` chars before the previous chunk's end (at a word / clause boundary when
    there is one), never before the previous chunk's start, and stays within ``limit`` with its overlap.
    Deterministic for the same text, so locators are stable."""
    overlap = max(0, min(overlap, limit // 4))
    base = _pack(text, limit, limit - overlap)
    out: list[tuple[int, int]] = []
    for i, (s, e) in enumerate(base):
        if i == 0 or overlap == 0:
            out.append((s, e))
            continue
        start = max(base[i - 1][0], s - overlap)
        m = _OVERLAP_START.search(text, start, s)
        if m is not None and start > base[i - 1][0]:
            start = m.end()
        out.append((min(start, s), e))
    return out


def chunk_overlap(chunks: list[tuple[int, int]], i: int) -> int:
    """Leading characters chunk ``i`` (0-based) repeats from the previous chunk of the same text."""
    if i == 0:
        return 0
    return max(0, chunks[i - 1][1] - chunks[i][0])


def covered_chars(ranges: list[tuple[int, int]]) -> int:
    """Characters covered by the (start, end) ranges, each counted once (overlapping chunks are not double counted)."""
    total, reach = 0, None
    for s, e in sorted(r for r in ranges if r[1] > r[0]):
        if reach is None or s >= reach:
            total += e - s
            reach = e
        elif e > reach:
            total += e - reach
            reach = e
    return total


def text_source(source_id: str, text: str, *, label: str, kind: SourceKind = "text", name: str = "",
                answer_of: str | None = None, limit: int = CHUNK_CHARS) -> Source:
    """A source over text that is fully in hand (direct text, an answer, a decoded text file)."""
    src = Source(id=source_id, kind=kind, label=label, name=name, sha256=sha256_text(text), bytes=len(text.encode("utf-8")),
                 chars=len(text), content_type="text/plain; charset=utf-8", answer_of=answer_of)
    paras = paragraphs(text)
    src.para_starts = [s for s, _ in paras]
    heads = _headings(text)
    head_pos = [p for p, _ in heads]
    spans = chunk_text(text, limit=limit)
    for i, (s, e) in enumerate(spans, start=1):
        p1 = max(1, bisect.bisect_right(src.para_starts, s))
        p2 = max(p1, bisect.bisect_right(src.para_starts, max(s, e - 1)))
        h = bisect.bisect_right(head_pos, s) - 1
        heading = heads[h][1] if h >= 0 else ""
        loc = f"chars {s}–{e} · ¶{p1}" + (f"–{p2}" if p2 != p1 else "") + (f" · {heading[:60]}" if heading else "")
        ov = chunk_overlap(spans, i - 1)
        src.chunks.append(SourceChunk(source_id=source_id, source_chunk=i, kind="text", locator=loc, text=text[s:e], start=s, end=e,
                                      meta={"overlap": ov} if ov else {}))
    if not src.chunks:
        src.error = {"code": "source_empty", "message": "the text is empty"}
    return src


# --- quote location (evidence the server can check) --------------------------------------------------------


#: Engineering-symbol variants compared as one character (S4). Looked up before NFKC (º and ˚ would become "o" /
#: a combining ring) and again after it (full-width ＊ → *, ϕ → φ, Φ casefolds to φ).
_SYMBOLS = {
    "Ø": "ø", "ø": "ø", "⌀": "ø", "∅": "ø", "φ": "ø", "Φ": "ø", "ϕ": "ø",  # diameter
    "×": "x", "✕": "x", "✖": "x", "*": "x",  # multiplication / "by"
    "°": "°", "º": "°", "˚": "°",  # degree
    "−": "-", "‐": "-", "‑": "-", "–": "-",  # minus sign, hyphens, en dash
}
#: Whitespace next to these is dropped, so "12 mm" = "12mm", "Ø 12" = "Ø12", "12 ± 0.1" = "12±0.1", "M8 x 1.25" = "M8x1.25".
_DIGITS = frozenset("0123456789")
_GLUE = _DIGITS | frozenset("ø±°+")
_DEG_WORDS = ("degrees", "degree", "deg")


def _symbol_units(text: str) -> list[tuple[str, int, int]]:
    """(normalised char, original start, original end) per char: symbol variants, NFKC (full width) and casefold."""
    out: list[tuple[str, int, int]] = []
    for i, ch in enumerate(text):
        mapped = _SYMBOLS.get(ch)
        if mapped is not None:
            out.append((mapped, i, i + 1))
            continue
        for c in unicodedata.normalize("NFKC", ch).casefold():
            out.append((_SYMBOLS.get(c, c), i, i + 1))
    return out


def _last_solid(units: list[tuple[str, int, int]]) -> str | None:
    for c, _, _ in reversed(units):
        if not c.isspace():
            return c
    return None


def _normalise(text: str) -> tuple[str, list[int], list[int]]:
    """Quote-matching form of ``text`` and, per normalised char, the original (start, end) it came from.

    NFKC (full-width digits / letters / punctuation), casefold, engineering symbols (Ø / φ / ⌀ / Φ; × / x / X / *;
    ± / +- / +/-; ° / º / deg after a number; minus / hyphen / en dash), whitespace runs collapsed to one space and
    dropped next to a digit or one of those symbols (units: "12 mm" = "12mm") but never between two digits. Every rule
    looks at neighbouring chars only, so a quote normalises to a substring of its text's normal form."""
    units = _symbol_units(text)
    merged: list[tuple[str, int, int]] = []
    i, n = 0, len(units)
    while i < n:
        c, s, e = units[i]
        if c == "+" and i + 1 < n and units[i + 1][0] == "-":
            merged.append(("±", s, units[i + 1][2]))
            i += 2
            continue
        if c == "+" and i + 2 < n and units[i + 1][0] == "/" and units[i + 2][0] == "-":
            merged.append(("±", s, units[i + 2][2]))
            i += 3
            continue
        if c == "d" and _last_solid(merged) in _DIGITS:
            word = next((w for w in _DEG_WORDS if "".join(u[0] for u in units[i:i + len(w)]) == w
                         and not (i + len(w) < n and units[i + len(w)][0].isalpha())), None)
            if word is not None:
                merged.append(("°", s, units[i + len(word) - 1][2]))
                i += len(word)
                continue
        merged.append((c, s, e))
        i += 1
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    k, m = 0, len(merged)
    while k < m:
        c, s, e = merged[k]
        if c.isspace():
            j = k
            while j < m and merged[j][0].isspace():
                j += 1
            nxt = merged[j][0] if j < m else ""
            # kept between two digits ("1 2" never becomes "12"), dropped next to one (units, symbols)
            if out and nxt and ((out[-1] in _DIGITS and nxt in _DIGITS) or (out[-1] not in _GLUE and nxt not in _GLUE)):
                out.append(" ")
                starts.append(s)
                ends.append(merged[j - 1][2])
            k = j
            continue
        out.append(c)
        starts.append(s)
        ends.append(e)
        k += 1
    return "".join(out), starts, ends


def normalise_quote(text: str) -> str:
    """The comparison form used by :func:`locate_quote` (two texts that are equal here state the same thing)."""
    return _normalise(text)[0]


def locate_quote(chunk_text_: str, quote: str) -> tuple[int, int] | None:
    """(start, end) of ``quote`` inside the chunk text, else None. Insensitive to whitespace, character width, case and
    equivalent engineering notations (see :func:`_normalise`); the offsets are into the original chunk text."""
    q = _normalise(quote.strip())[0]
    if len(q) < 2:
        return None
    norm, starts, ends = _normalise(chunk_text_)
    pos = norm.find(q)
    if pos < 0:
        return None
    return starts[pos], ends[pos + len(q) - 1]


def paragraph_of(src: Source, offset: int) -> int:
    return max(1, bisect.bisect_right(src.para_starts, offset))


# --- file versions ----------------------------------------------------------------------------------------


def _ext(name: str) -> str:
    lower = (name or "").lower()
    return lower[lower.rfind("."):] if "." in lower else ""


def _decode_utf8(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    # Binary content that happens to decode (NUL bytes) is not a text document.
    return None if "\x00" in text else text


def file_version_text(db: Session, fv: FileVersion) -> str | None:
    """UTF-8 content of a text / Markdown file version, else None (binary, drawing, image, not UTF-8, missing)."""
    blob = db.get(Blob, fv.blob_sha256)
    ctype = ((blob.content_type if blob else "") or "").split(";")[0].strip().lower()
    if _ext(fv.original_name) not in TEXT_EXTS and not ctype.startswith("text/"):
        return None
    try:
        data, _ = read_blob(db, fv.blob_sha256)
    except (FileNotFoundError, OSError):
        return None
    return _decode_utf8(data)


def file_version_source(db: Session, source_id: str, fv: FileVersion, f: File) -> Source:
    """Read one file version of the project (the caller checked the project). Never raises for content
    problems: they come back as ``error`` (failed) or chunk-less sources with a reason."""
    from ..ai.providers.base import sniff_mime
    from .drawing_input import sniff

    blob = db.get(Blob, fv.blob_sha256)
    base = dict(name=fv.original_name, file_version_id=str(fv.id), file_id=str(f.id), version=fv.version,
                sha256=fv.blob_sha256, bytes=blob.size if blob else 0, content_type=blob.content_type if blob else "")
    label = f"{f.name} v{fv.version}"
    try:
        data, _ = read_blob(db, fv.blob_sha256)
    except (FileNotFoundError, OSError) as exc:
        return Source(id=source_id, kind="file_version", label=label, error={"code": "blob_missing", "message": str(exc)[:300]}, **base)
    ext = _ext(fv.original_name)
    if ext in (".dxf", ".dwg") or (ext not in TEXT_EXTS and sniff(fv.original_name, data) is not None):
        return _drawing_source(db, source_id, label, data, base)
    ctype = (base["content_type"] or "").split(";")[0].strip().lower()
    mime = sniff_mime(data)
    if mime in ATTACHMENT_TYPES or ctype in ATTACHMENT_TYPES:
        return _attachment_source(source_id, label, data, mime or ctype, base)
    if ext in TEXT_EXTS or ctype.startswith("text/"):
        text = _decode_utf8(data)
        if text is None:
            return Source(id=source_id, kind="file_version", label=label,
                          error={"code": "not_utf8", "message": "the file is not UTF-8 text; save it as UTF-8 and upload a new version"},
                          **base)
        src = text_source(source_id, text, label=label, kind="file_version", name=fv.original_name)
        src.file_version_id, src.file_id, src.version = base["file_version_id"], base["file_id"], base["version"]
        src.sha256, src.bytes, src.content_type = base["sha256"], base["bytes"], base["content_type"]
        return src
    if ext in (".step", ".stp"):
        return Source(id=source_id, kind="file_version", label=label,
                      error={"code": "unsupported_format", "message": "STEP files are read by the STEP import, not as a text source"},
                      **base)
    return Source(id=source_id, kind="file_version", label=label,
                  error={"code": "unsupported_format", "message": f"cannot read {ext or 'this file type'} as an engineering source "
                                                                  "(text / Markdown, DXF / DWG, PNG / JPEG / WEBP / PDF are supported)"},
                  **base)


def _attachment_source(source_id: str, label: str, data: bytes, mime: str, base: dict[str, Any]) -> Source:
    src = Source(id=source_id, kind="file_version", label=label, **base)
    if len(data) > MAX_ATTACHMENT_BYTES:
        src.error = {"code": "attachment_too_large", "message": f"{len(data)} bytes; at most {MAX_ATTACHMENT_BYTES} bytes are sent inline"}
        return src
    meta: dict[str, Any] = {"mime_type": mime, "scale": None}
    if mime.startswith("image/"):
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                meta["width"], meta["height"] = int(im.width), int(im.height)
        except Exception:  # noqa: BLE001 - a size we cannot read only weakens region checks
            pass
    size = f"{meta['width']}×{meta['height']} px" if "width" in meta else mime
    caption = (f"Attachment {base['name']} ({mime}, {len(data)} bytes, {size}). Located evidence only: there is no scale, so "
               "cite a region [x, y, w, h] in pixels (images) or a page (PDF); measured sizes are not facts.")
    src.chunks.append(SourceChunk(source_id=source_id, source_chunk=1, kind="attachment", locator=f"attachment · {size} · no scale",
                                  text=caption, data=data, mime_type=mime, meta=meta))
    src.notes.append("located evidence only (no scale)")
    return src


def _drawing_source(db: Session, source_id: str, label: str, data: bytes, base: dict[str, Any]) -> Source:
    import time

    from ..cad.drawing_evidence import DrawingEvidenceError, evidence_prompt, extract_evidence, outside_group_lines, outside_header
    from ..errors import AppError
    from . import drawing_input as din

    src = Source(id=source_id, kind="drawing", label=label, **base)
    try:
        fmt = din.drawing_format(base["name"], data)
        dxf = din.dwg_to_dxf(db, data)[0] if fmt == "dwg" else data
        evidence = extract_evidence(dxf, deadline=time.monotonic() + DRAWING_BUDGET_S)
    except AppError as exc:
        src.error = {"code": exc.code, "message": exc.message}
        return src
    except DrawingEvidenceError as exc:
        src.error = {"code": getattr(exc, "code", "dxf_unreadable"), "message": str(exc)[:300]}
        return src
    src.notes.append(f"{fmt.upper()} · units {evidence.units.insunits_name or 'unknown'} · {len(evidence.frames)} frame(s)")
    src.drawing_dxf, src.drawing_evidence = dxf, evidence
    if not evidence.frames:
        src.error = {"code": "drawing_empty", "message": "the drawing has no frame or geometry to read"}
        return src
    src.entities = set(evidence.index()) | {f.id for f in evidence.frames}
    for f in evidence.frames:
        src.entities.update(v.id for v in f.views)
        for name in ("number", "name", "material", "scale", "quantity"):
            tf = getattr(f.title, name)
            if tf is not None and tf.text_id:
                src.entities.add(tf.text_id)
    # Generation reliability K (design §2.4): entities outside every frame are grouped; groups with text / dimensions /
    # a table are read as extra chunks (below), geometry-only groups are excluded with their reason, entities without a
    # position stay unexplained (the source is then partial). Quarantined entities are listed apart — except block
    # references past the expansion limits: their content was skipped, not read, so they leave the source partial too.
    cov = evidence.entity_coverage()
    outside = cov["outside"]
    if outside["unexplained"]:
        src.not_chunked.append(f"{outside['unexplained']} model-space entities outside every frame not explained (no position)")
    for reason, k in outside["excluded"].items():
        src.notes.append(f"{k} entities outside every frame excluded: {reason}")
    skipped = int(cov["quarantined_reasons"].get("block_expansion_limit") or 0)
    if skipped:
        src.not_chunked.append(f"{skipped} block reference(s) not expanded (block_expansion_limit): their content was not read")
    apart = {r: k for r, k in cov["quarantined_reasons"].items() if r != "block_expansion_limit"}
    if apart:
        src.notes.append(f"{sum(apart.values())} quarantined entities listed apart (invalid coordinates or unreadable parts): "
                         + ", ".join(f"{r} {k}" for r, k in apart.items()))
    src.coverage = {"frames": len(evidence.frames), "entities": cov,
                    "outside_groups": [{"id": c.id, "range": _range(c.bbox), "entities": c.entity_count, "status": c.status,
                                        "reason": c.reason, "texts": len(c.text_ids), "dimensions": len(c.dimension_ids),
                                        "table": c.table} for c in evidence.unframed.clusters[:MAX_LISTED_GROUPS]],
                    "outside_groups_total": len(evidence.unframed.clusters)}
    n = 0
    for f in evidence.frames:
        lines = evidence_prompt(evidence, f.id, max_chars=10_000_000).splitlines()
        cut = next((i for i, ln in enumerate(lines) if ln.startswith("views (")), min(len(lines), 4))
        head, body = lines[:cut], lines[cut:]
        # A BOM consists of separate TEXT entities. Keeping only its number
        # cell in the structure digest loses the name, variant and quantity.
        body = [*_drawing_table_rows(evidence, f), *body]
        head_text = "\n".join(head)
        title = " · ".join(x for x in ((f.title.number.value if f.title.number else ""),
                                       (" ".join((f.title.name.value if f.title.name else "").split()))) if x)
        budget = max(1000, CHUNK_CHARS - len(head_text) - 1)
        groups: list[tuple[int, int]] = []
        start, size = 0, 0
        for i, ln in enumerate(body):
            if size and size + len(ln) + 1 > budget:
                groups.append((start, i))
                start, size = i, 0
            size += len(ln) + 1
        groups.append((start, len(body)))
        for a, b in groups:
            n += 1
            text = head_text + "\n" + "\n".join(body[a:b])
            loc = f"{f.id}" + (f" · {title[:80]}" if title else "") + f" · lines {cut + a + 1}–{cut + b}"
            src.chunks.append(SourceChunk(source_id=source_id, source_chunk=n, kind="drawing", locator=loc, text=text,
                                          start=cut + a + 1, end=cut + b, meta={"frame": f.id}))
    # Outside-frame groups with text / dimensions / a table: packed in order into chunks of at most CHUNK_CHARS
    # (locator "U01 · outside frames · <range>", "U01–U03 · …" when several groups share one chunk).
    header = outside_header(evidence)
    head_len = sum(len(x) + 1 for x in header)
    budget = max(1000, CHUNK_CHARS - head_len)
    # (groups, body lines, part "lines a–b" of one group cut because it alone is longer than a chunk)
    packs: list[tuple[list[Any], list[str], str]] = []
    for c in (c for c in evidence.unframed.clusters if c.status == "read"):
        body = outside_group_lines(evidence, c)
        size = sum(len(x) + 1 for x in body)
        last = packs[-1] if packs else None
        if last is not None and not last[2] and sum(len(x) + 1 for x in last[1]) + size <= budget:
            last[0].append(c)
            last[1].extend(body)
            continue
        if size <= budget:
            packs.append(([c], list(body), ""))
            continue
        start, used = 0, 0
        for i, ln in enumerate(body):
            if used and used + len(ln) + 1 > budget:
                packs.append(([c], body[start:i], f" · lines {start + 1}–{i}"))
                start, used = i, 0
            used += len(ln) + 1
        packs.append(([c], body[start:], f" · lines {start + 1}–{len(body)}"))
    for pack, body, part in packs:
        n += 1
        box = None
        for c in pack:
            if c.bbox is not None:
                box = c.bbox if box is None else (min(box[0], c.bbox[0]), min(box[1], c.bbox[1]), max(box[2], c.bbox[2]), max(box[3], c.bbox[3]))
        ids = pack[0].id if len(pack) == 1 else f"{pack[0].id}–{pack[-1].id}"
        src.chunks.append(SourceChunk(source_id=source_id, source_chunk=n, kind="drawing",
                                      locator=f"{ids} · outside frames · {_range(box)}{part}", text="\n".join([*header, *body]),
                                      meta={"outside": [c.id for c in pack]}))
    return src


def _drawing_table_rows(evidence: Any, frame: Any) -> list[str]:
    """Literal neighboring text cells, grouped around drawing/standard numbers.

    No BOM meaning or quantity is inferred here. Coordinates and handles let
    readers distinguish a catalog's sheet count from a BOM's part count.
    """
    import json

    from .spec_identity import STANDARD_IN_TEXT

    tb = set(frame.title_block_text_ids)
    all_texts = [t for t in evidence.texts if t.frame_id == frame.id and t.text.strip()]
    texts = [t for t in all_texts if t.id not in tb and "\n" not in t.text and len(t.text) <= 160]
    anchors = [t for t in texts if _DRAWING_NO.search(t.text) or STANDARD_IN_TEXT.search(t.text)]
    if not anchors:
        return []
    rows: dict[str, list[Any]] = {t.id: [] for t in anchors}
    for cell in texts:
        anchor = min(anchors, key=lambda t: (abs(t.insert[1] - cell.insert[1]), abs(t.insert[0] - cell.insert[0])))
        if abs(anchor.insert[1] - cell.insert[1]) <= max(anchor.height, cell.height) * 1.1:
            rows[anchor.id].append(cell)
    headings = [t for t in all_texts if re.search(r"图\s*纸\s*明\s*细\s*表|图\s*样\s*目\s*录|drawing\s+(?:list|index)", t.text, re.I)]
    headers = [t for t in texts if re.fullmatch(r"序\s*号|代\s*号|图\s*号|名\s*称|图\s*纸\s*名\s*称|数\s*量|张\s*数|幅\s*面", t.text.strip())]
    result = [f"drawing table heading {frame.id}: {json.dumps(t.text, ensure_ascii=False)} [{t.id}]" for t in headings]
    if headers:
        result.append(f"drawing table heading {frame.id} column labels: " + " | ".join(
            f"{json.dumps(t.text, ensure_ascii=False)} [{t.id}]" for t in sorted(headers, key=lambda t: (-round(t.insert[1] / 4), t.insert[0]))))
    for anchor in sorted(anchors, key=lambda t: (-t.insert[1], t.insert[0])):
        cells = sorted(rows[anchor.id], key=lambda t: t.insert[0])
        if len(cells) < 2:
            continue
        y = evidence.to_local(frame.id, anchor.insert)[1]
        values = " | ".join(f"{json.dumps(t.text, ensure_ascii=False)} [{t.id}]" for t in cells)
        result.append(f"drawing table row {frame.id} y={y:.2f} (literal cells, left to right): {values}")
    return result


#: Outside-frame groups listed one by one in a source's coverage (the counts always cover all of them).
MAX_LISTED_GROUPS = 200


def _range(b: Any) -> str:
    """"(x0,y0)–(x1,y1)" of a sheet-layout box in drawing units (a range on the sheet, never a product coordinate)."""
    if not b:
        return "?"

    def f(v: float) -> str:
        s = f"{float(v):.2f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    return f"({f(b[0])},{f(b[1])})–({f(b[2])},{f(b[3])})"


# --- structure digest (generation reliability A, design §6.1) ---------------------------------------------
#
# Before the chunks are read, the skeleton call reads a compact structural digest of ALL sources at once: headings,
# table rows, sentences / lines naming a drawing or standard number, Chinese counts of pieces / items / sets, containment and assembly statements,
# and stated coordinates / datums. Every unit is a verbatim line (a table row, a heading) or sentence of a source, in
# source order, so whatever the skeleton quotes is found again in the chunks the server sends. When the digest is longer
# than the skeleton calls take, units are cut lowest value first (other table rows, then datum lines, structure
# statements, …) and the digest says what was left out.

#: Digest characters one skeleton call reads, and the most skeleton calls of one reading.
SKELETON_CALL_CHARS = 24_000
SKELETON_MAX_CALLS = 2
#: A unit longer than this is cut (a sentence that runs on); the quote the skeleton takes from it stays verbatim.
DIGEST_UNIT_CHARS = 300
#: Value tiers of a digest unit, highest value first (the cut drops the last tier first).
DIGEST_TIERS = ("heading", "numbered_row", "numbered_line", "structure", "datum", "table_row")

_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RULE_ROW = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
_UNIT_SPLIT = re.compile(r"(?<=[。；;！？!?])")
#: A drawing number: a label ("Drawing number ..." in Chinese) or letters, optional digits and dotted / dashed digit groups (D07.04-01, SH-01).
_DRAWING_NO = re.compile(r"(?i:图号|代号|编号|零件号|件号|物料号|drawing\s*no|part\s*no|dwg\s*no)\s*[:：.]?\s*[A-Za-z0-9]|"
                         r"(?<![A-Za-z0-9])[A-Z]{1,5}\d{0,4}(?:[.\-_]\d{1,5}){1,5}(?![A-Za-z0-9])")
#: Counts (Chinese counts of pieces / items / sets, "2 pcs") and structure statements (contains, consists of, general assembly / subassembly / component, in Chinese) — including a
#: component declared in prose ("Part base plate: ...", "Standard part ...", "Part: ..."), which is the structure of a document without numbers.
_STRUCTURE = re.compile(r"(?:\d+|[一二两三四五六七八九十]+)\s*(?:件|个|套|只|根|块|片|组|台|副|对|颗|枚|条)|"
                        r"包含|包括|组成|装有|数量|总装|子装配|部件|组件|装配体|零件|标准件|外购件|\bpart\s*[:：]|"
                        r"\b\d+\s*(?:pcs|pieces|sets|off)\b|\bcontains?\b|"
                        r"\bconsists\s+of\b|\bincludes?\b|\bqty\b|\b(?:sub-?assembly|root assembly|top assembly)\b", re.IGNORECASE)
_SIGNED = r"[-+±−]?\s*\d+(?:\.\d+)?"
#: A coordinate or datum statement — public so every reader of stated assembly geometry uses this one pattern (the
#: structure digest here; the placement context's global coordinate sentences): a point "(x, y, z)" whose numbers may
#: carry a sign or ± ("(±96, 0, -71.5)"), an axis value "Z=-71.5" / "X=±48", a symmetric coordinate "±112" ("X and Y take
#: ±112 …"; not a tolerance "75±0.2", where ± follows a number), or a datum word (Chinese terms for origin, datum, axis, coordinates, mating surface, or datum,
#: origin, axis).
COORDINATE_STATEMENT = re.compile(
    rf"\(\s*{_SIGNED}\s*[,，]\s*{_SIGNED}\s*[,，]\s*{_SIGNED}\s*\)|（\s*{_SIGNED}\s*[,，]\s*{_SIGNED}\s*[,，]\s*{_SIGNED}\s*）|"
    r"(?<![A-Za-z])[XYZ]\s*[=＝]\s*[-+±−]?\s*\d|(?<![\d.])(?<![\d.]\s)±\s*\d|"
    r"原点|基准|轴线|坐标|结合面|\bdatum\b|\borigin\b|\baxis\b", re.IGNORECASE)


@dataclass
class DigestUnit:
    source: str
    order: int
    tier: int
    text: str
    table: int | None = None  # table id within the source (header rows follow their table)
    header: bool = False


@dataclass
class Digest:
    """The skeleton calls' input: ``parts`` (one text per call, at most :data:`SKELETON_MAX_CALLS`), what each source
    gave (``by_source``: units / characters kept and cut) and what the cut left out (``cut``: units per tier)."""

    parts: list[str]
    by_source: dict[str, dict[str, int]]
    cut: dict[str, int]
    chars: int

    @property
    def note(self) -> str:
        if not self.cut:
            return ""
        return ("The digest was cut to fit; left out, lowest value first: "
                + ", ".join(f"{n} {t.replace('_', ' ')} unit(s)" for t, n in self.cut.items()) + ".")

    def record(self) -> dict[str, Any]:
        return {"chars": self.chars, "calls": len(self.parts), "part_chars": [len(p) for p in self.parts], "by_source": self.by_source,
                "cut": dict(self.cut)}


def _tier(text: str, *, row: bool, heading: bool) -> int | None:
    from .spec_identity import STANDARD_IN_TEXT

    if heading:
        return 0
    numbered = bool(_DRAWING_NO.search(text) or STANDARD_IN_TEXT.search(text))
    if numbered:
        return 1 if row else 2
    if _STRUCTURE.search(text):
        return 3
    if COORDINATE_STATEMENT.search(text):
        return 4
    return 5 if row else None


def digest_units(source_id: str, texts: list[tuple[str, str]]) -> list[DigestUnit]:
    """The digest units of one source, in order. ``texts``: (kind, text) of its chunks, overlap already removed; a text
    chunk is read line by line (table rows and headings whole, other lines sentence by sentence), a drawing chunk's
    evidence index line by line (its ``title:`` line is the heading). A line repeated (a drawing chunk's head) is one unit."""
    out: list[DigestUnit] = []
    seen: set[str] = set()
    table: int | None = None
    tables = 0
    header_pending = False
    for kind, text in texts:
        for line in text.splitlines():
            s = line.strip()
            if not s:
                table = None
                continue
            drawing_row = kind == "drawing" and s.startswith("drawing table row ")
            row = bool(_ROW.match(s)) or drawing_row
            if row and _RULE_ROW.match(s):
                header_pending = False
                continue
            if row and table is None:
                tables += 1
                table, header_pending = tables, not drawing_row
            elif not row:
                table = None
            heading = ((kind == "text" and bool(_MD_HEAD.match(s)))
                       or (kind == "drawing" and s.startswith(("title: ", "drawing table heading "))))
            pieces = [s] if row or heading or kind == "drawing" else [p.strip() for p in _UNIT_SPLIT.split(s) if p.strip()]
            for p in pieces:
                if p in seen:
                    continue
                is_header = row and header_pending
                tier = _tier(p, row=row, heading=heading)
                if tier is None and not is_header:
                    continue
                seen.add(p)
                out.append(DigestUnit(source_id, len(out), 5 if tier is None else tier, clip_unit(p), table if row else None, is_header))
            if row:
                header_pending = False
    # a header row stays with its table: as valuable as the table's best row, never kept alone
    best: dict[int, int] = {}
    for u in out:
        if u.table is not None and not u.header:
            best[u.table] = min(best.get(u.table, 9), u.tier)
    return [u if not u.header else DigestUnit(u.source, u.order, best.get(u.table or 0, 5), u.text, u.table, True)
            for u in out if not u.header or (u.table in best)]


def clip_unit(s: str) -> str:
    return s if len(s) <= DIGEST_UNIT_CHARS else s[:DIGEST_UNIT_CHARS - 1] + "…"


def structure_digest(sources: list[tuple[str, str, list[tuple[str, str]]]], *, call_chars: int = SKELETON_CALL_CHARS,
                     max_calls: int = SKELETON_MAX_CALLS) -> Digest:
    """The structure digest of every source (``sources``: (id, label, [(chunk kind, text)])), cut lowest value first to fit
    ``max_calls`` calls of ``call_chars`` and split into one text per call (at a source or heading boundary when one is
    near the middle). Empty ``parts`` when no source has a structural unit (attachments only)."""
    units: dict[str, list[DigestUnit]] = {sid: digest_units(sid, texts) for sid, _, texts in sources}
    labels = {sid: label for sid, label, _ in sources}

    def head(sid: str) -> str:
        return f'<source id="{sid}" label="{labels[sid][:120]}">'

    def size(us: list[DigestUnit]) -> int:
        return sum(len(u.text) + 1 for u in us)

    overhead = sum(len(head(sid)) + len("</source>") + 2 for sid in units if units[sid])
    kept = [u for sid in units for u in units[sid]]
    cut: dict[str, int] = {}
    budget = call_chars * max_calls - overhead - 200 * max_calls
    if size(kept) > budget:
        # lowest value first, later units of a tier first; a table's header goes when its last row goes
        drop: set[int] = set()
        total = size(kept)
        for u in sorted(kept, key=lambda u: (-u.tier, -u.order)):
            if total <= budget:
                break
            if u.header:
                continue
            drop.add(id(u))
            total -= len(u.text) + 1
            cut[DIGEST_TIERS[u.tier]] = cut.get(DIGEST_TIERS[u.tier], 0) + 1
        kept = [u for u in kept if id(u) not in drop]
        rows_left = {(u.source, u.table) for u in kept if u.table is not None and not u.header}
        kept = [u for u in kept if not u.header or (u.source, u.table) in rows_left]
    kept_ids = {id(u) for u in kept}
    by_source = {sid: {"units": sum(1 for u in us if id(u) in kept_ids), "chars": size([u for u in us if id(u) in kept_ids]),
                       "cut": sum(1 for u in us if id(u) not in kept_ids)} for sid, us in units.items()}
    if not kept:
        return Digest([], by_source, cut, 0)

    def render(us: list[DigestUnit]) -> str:
        out: list[str] = []
        for sid in dict.fromkeys(u.source for u in us):
            out += [head(sid), *(u.text for u in us if u.source == sid), "</source>"]
        return "\n".join(out)

    whole = render(kept)
    if len(whole) <= call_chars or max_calls < 2:
        return Digest([whole], by_source, cut, len(whole))
    # two calls: the boundary (a new source or a heading) nearest the middle with both halves within a call, else the
    # unit nearest the middle
    def fits(i: int) -> bool:
        return max(len(render(kept[:i])), len(render(kept[i:]))) <= call_chars

    cands = [i for i, u in enumerate(kept) if i and (u.tier == 0 or u.source != kept[i - 1].source) and fits(i)]
    best_i = min(cands or range(1, len(kept)), key=lambda i: abs(size(kept[:i]) - size(kept[i:])))
    parts = [render(kept[:best_i]), render(kept[best_i:])]
    return Digest(parts, by_source, cut, sum(len(p) for p in parts))


# --- budget and read status -----------------------------------------------------------------------------


def over_budget(sources: list[Source], *, max_chunks: int = MAX_CHUNKS) -> list[dict[str, Any]]:
    """Parts beyond the per-version chunk budget, per source (empty: everything fits)."""
    out: list[dict[str, Any]] = []
    n = 0
    for src in sources:
        beyond = []
        for c in src.chunks:
            n += 1
            if n > max_chunks:
                beyond.append(c.locator)
        if beyond:
            out.append({"source": src.id, "label": src.label, "chunks": len(beyond), "locators": beyond})
    return out


def read_status(src_error: dict[str, Any] | None, statuses: list[str], not_chunked: list[str],
                coverage: dict[str, Any] | None = None) -> str:
    """read: every chunk sent and nothing left out; partial: some sent; unread: none sent; failed: unreadable. A drawing
    with a ``coverage`` block (generation reliability K, design §2.4) is read only when no outside-frame entity is left
    unexplained as well (excluded groups have their reason; quarantined entities are listed apart, block references
    left unexpanded are in ``not_chunked``)."""
    if src_error is not None:
        return "failed"
    if not statuses:
        return "unread"
    sent = sum(1 for s in statuses if s == "sent")
    unexplained = int((((coverage or {}).get("entities") or {}).get("outside") or {}).get("unexplained") or 0)
    if sent == len(statuses) and not not_chunked and not unexplained:
        return "read"
    if sent:
        return "partial"
    return "failed" if any(s == "failed" for s in statuses) else "unread"


def new_source_id(n: int) -> str:
    return f"S-{n}"


def uuid_or_none(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None
