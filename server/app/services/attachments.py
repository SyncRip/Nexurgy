"""Attachments and input pre-checks of the generation chain (generation supplement package A, problem Q4;
docs/agent/generation-workflow-supplement.zh-CN.md §3, .trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.2, §2.5).

"The file was uploaded" must become "this run really read its text", and an input problem must show before any CAD
step:

- Attachment binding (A-01, design §2.5): a request whose instruction (its first / last line: a pasted description
  that mentions "attachment" in its body is material, not an instruction) clearly points at an attachment ("just uploaded", "attachment",
  "this document", "markdown", ".md", "attached", "uploaded" …) while neither its target nor its references hold a file
  version gets the file it means. Candidates, in order, the first source that finds any decides: a file of the project
  named in the text (full name, or a stem of at least 4 characters); a file this conversation referenced before and
  that is no longer in the composer; a document the person uploaded to the project in the last 24 hours that the spec
  step can read. Latest version of each file only; the project's file library is never read. One candidate is bound
  (``auto_references``), several are 422 ``attachment_ambiguous`` with the candidates as references, none is 422
  ``attachment_required``. Without such wording nothing happens (:func:`mentions_attachment` is deliberately narrow).
- Sources of the spec step (A-02, A-04): every entry (the assistant's assembly_create, the workflow spec node, the agent
  tool) checks its text / files / spec version with :func:`check_spec_and_sources` — an existing spec version together
  with new files or change wording is 422 ``spec_and_sources`` (never silently one of them) — and the spec step reads a
  text line that is also in an attached text file once, from the file (:func:`dedupe_text`, recorded on the version).
- Pre-check of a spec version before any CAD step (A-03, :func:`precheck`): ``source_unread`` (a requested file is
  missing from the spec's sources or was not read), ``spec_no_parts``, ``extraction_missed_parts`` (an explainable
  parts-list rule on the text really read, :func:`parts_list_evidence`; never a character count),
  ``spec_structure_incomplete`` (standard). Turbo hangs definitions that hang nowhere under the root in a derived spec
  version (:func:`hang_unplaced`), one ``rule`` decision per definition; ``spec_empty`` stays the assembly step's last
  line of defence.
"""

from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import AiConversation, AiMessage, EngineeringSpecChunk, EngineeringSpecVersion, File, FileVersion, User
from ..storage import read_blob

#: How far back "the document I just uploaded" looks.
RECENT_UPLOAD = timedelta(hours=24)
#: A file stem named in the text counts from this many characters (design §2.5; shorter stems match too much).
MIN_STEM_CHARS = 4
#: Candidates returned with attachment_ambiguous / by the resolve endpoint.
MAX_CANDIDATES = 10
#: Project files whose names are compared with the text (names only, newest first).
MAX_NAMED_FILES = 2000
#: Extensions the spec step reads (services/source_reader.py: text / Markdown, DXF / DWG, PDF / images).
READABLE_EXTS = (".txt", ".md", ".markdown", ".text", ".csv", ".tsv", ".json", ".yaml", ".yml", ".dxf", ".dwg", ".pdf", ".png",
                 ".jpg", ".jpeg", ".webp")
MARKDOWN_EXTS = (".md", ".markdown")
#: Why a candidate was found (the operation card shows it with the bound file).
REASONS = ("named_in_text", "referenced_before", "recent_upload")


def _ascii_word(word: str) -> str:
    return rf"(?<![A-Za-z0-9_]){word}(?![A-Za-z0-9_])"


# Clear wording about an attached / uploaded file. Narrow on purpose: the Chinese equivalents of "explain" or "this" alone or a
# Markdown *format* request ("output in Markdown format") never count.
_ATTACH = re.compile(
    r"刚刚?上传|新上传|已上传|上传的|附件|附上的|随附|"
    r"(?:这|那|该|此)\s*(?:份|个|篇|张)?\s*(?:文件|文档|资料|附件|说明书|说明文档|清单|图纸|markdown|md(?![A-Za-z0-9_]))|"
    r"\.(?:md|markdown)" + r"(?![A-Za-z0-9_])|" + _ascii_word(r"md") + r"\s*(?:文件|文档)|"
    + _ascii_word(r"attach(?:ed|ment|ments)") + "|" + _ascii_word("uploaded") + "|"
    + _ascii_word(r"(?:this|that|the)\s+(?:file|document|doc|markdown)"),
    re.IGNORECASE)
_MARKDOWN = re.compile(_ascii_word("markdown"), re.IGNORECASE)
_MARKDOWN_FORMAT = re.compile(r"^\s*(?:格式|format|语法|syntax|表格|table)", re.IGNORECASE)
_MARKDOWN_AS = re.compile(r"(?:用|以|输出成?|写成|转成|转为|转换为|改成|" + _ascii_word(r"(?:in|as|to)") + r")\s*$", re.IGNORECASE)


def mentions_attachment(text: str) -> bool:
    """The text clearly points at an attached / uploaded file (design §2.5)."""
    t = text or ""
    if _ATTACH.search(t):
        return True
    for m in _MARKDOWN.finditer(t):
        if not _MARKDOWN_FORMAT.match(t[m.end():m.end() + 8]) and not _MARKDOWN_AS.search(t[max(0, m.start() - 8):m.start()]):
            return True
    return False


def instruction_lines(text: str) -> str:
    """The first and the last non-empty line of a request — where its instruction is. A pasted description that mentions
    "attachment" somewhere in its body is material, not an instruction to bind a file (A-03: sufficient text never needs an
    attachment)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(dict.fromkeys(lines[:1] + lines[-1:]))


def _wants_markdown(text: str) -> bool:
    return bool(re.search(r"markdown|\.md(?![A-Za-z0-9_])|" + _ascii_word("md"), text or "", re.IGNORECASE))


def has_file(refs: list[dict | None]) -> bool:
    return any(isinstance(r, dict) and r.get("kind") == "file_version" for r in refs)


def _ext(name: str) -> str:
    lower = (name or "").lower()
    return lower[lower.rfind("."):] if "." in lower else ""


def _readable(name: str, *, markdown_only: bool) -> bool:
    ext = _ext(name)
    return ext in MARKDOWN_EXTS if markdown_only else ext in READABLE_EXTS


# =====================================================================================================================
# Attachment binding (A-01)
# =====================================================================================================================


@dataclass
class Candidate:
    fv: FileVersion
    f: File
    reason: str


@dataclass
class Binding:
    """``needed``: the wording asks for an attachment and the request holds no file version. ``status``: not_needed |
    unique | ambiguous | required; ``reason``: the candidate source that decided (:data:`REASONS`)."""

    needed: bool = False
    status: str = "not_needed"
    reason: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


def _latest_versions(db: Session, file_ids: list[uuid.UUID]) -> dict[uuid.UUID, FileVersion]:
    if not file_ids:
        return {}
    last = (select(FileVersion.file_id, func.max(FileVersion.version).label("v")).where(FileVersion.file_id.in_(file_ids))
            .group_by(FileVersion.file_id).subquery())
    rows = db.scalars(select(FileVersion).join(last, (FileVersion.file_id == last.c.file_id) & (FileVersion.version == last.c.v))).all()
    return {fv.file_id: fv for fv in rows}


def _named(text: str, name: str) -> bool:
    """The text names the file: its full name, or its stem (≥ MIN_STEM_CHARS) as a whole word for Latin names. A
    Chinese stem counts only quoted (a Chinese parts-list name enclosed in corner, curly, title or straight quotation marks): unquoted it is usually an ordinary
    phrase ("generate from the parts list in the attachment") and must not win over the file just uploaded (review fix)."""
    low, n = text.casefold(), (name or "").strip().casefold()
    if not n:
        return False
    if n in low:
        return True
    stem = n[: n.rfind(".")] if "." in n else n
    if len(stem) < MIN_STEM_CHARS:
        return False
    if stem.isascii():
        return re.search(_ascii_word(re.escape(stem)), low) is not None
    return any(f"{a}{stem}{b}" in low for a, b in ("「」", "“”", "《》", '""', "''", "『』"))


def _project_files(db: Session, project_id: uuid.UUID, file_ids: list[uuid.UUID] | None = None) -> list[File]:
    q = select(File).where(File.project_id == project_id, File.archived_at.is_(None))
    if file_ids is not None:
        q = q.where(File.id.in_(file_ids))
    return list(db.scalars(q.order_by(File.created_at.desc()).limit(MAX_NAMED_FILES)).all())


def _by_name(db: Session, project_id: uuid.UUID, text: str) -> list[Candidate]:
    files = _project_files(db, project_id)
    latest = _latest_versions(db, [f.id for f in files])
    out = []
    for f in files:
        fv = latest.get(f.id)
        if fv is not None and (_named(text, f.name) or _named(text, fv.original_name)):
            out.append(Candidate(fv, f, "named_in_text"))
    return out


def _referenced_before(db: Session, conv: AiConversation, exclude_files: set[uuid.UUID], markdown_only: bool) -> list[Candidate]:
    fv_ids: list[uuid.UUID] = []
    for target, references in db.execute(select(AiMessage.target, AiMessage.references).where(
            AiMessage.conversation_id == conv.id, AiMessage.role == "user").order_by(AiMessage.seq.desc())).all():
        for r in ([target] if target else []) + list(references or []):
            if isinstance(r, dict) and r.get("kind") == "file_version":
                try:
                    fv_ids.append(uuid.UUID(str(r.get("object_id"))))
                except (TypeError, ValueError):
                    continue
    if not fv_ids:
        return []
    file_ids = list(dict.fromkeys(db.scalars(select(FileVersion.file_id).where(FileVersion.id.in_(fv_ids))).all()))
    file_ids = [i for i in file_ids if i not in exclude_files]
    files = {f.id: f for f in _project_files(db, conv.project_id, file_ids)}
    latest = _latest_versions(db, list(files))
    order = {fid: n for n, fid in enumerate(file_ids)}
    return [Candidate(latest[fid], files[fid], "referenced_before") for fid in sorted(files, key=lambda i: order.get(i, 0))
            if fid in latest and _readable(latest[fid].original_name, markdown_only=markdown_only)]


def _recent_uploads(db: Session, user: User, project_id: uuid.UUID, markdown_only: bool) -> list[Candidate]:
    since = datetime.now(UTC) - RECENT_UPLOAD
    rows = db.execute(select(FileVersion.file_id, func.max(FileVersion.created_at).label("at"))
                      .join(File, File.id == FileVersion.file_id)
                      .where(File.project_id == project_id, File.archived_at.is_(None), FileVersion.created_by == user.id,
                             FileVersion.created_at >= since)
                      .group_by(FileVersion.file_id).order_by(func.max(FileVersion.created_at).desc()).limit(50)).all()
    file_ids = [r[0] for r in rows]
    files = {f.id: f for f in _project_files(db, project_id, file_ids)}
    latest = _latest_versions(db, list(files))
    return [Candidate(latest[fid], files[fid], "recent_upload") for fid in file_ids
            if fid in files and fid in latest and _readable(latest[fid].original_name, markdown_only=markdown_only)]


def find_binding(db: Session, user: User, conv: AiConversation, text: str, refs: list[dict | None], *,
                 composer_refs: list[dict] | None = None, search: bool = True) -> Binding:
    """Design §2.5. ``refs``: the request's target and references; ``composer_refs``: what the composer holds now (files
    there are not "referenced before"); ``search`` False (``auto_references=false``: the person set the references):
    nothing is looked for or added — the wording without any file is still ``required``. Only the instruction lines
    count (:func:`instruction_lines`)."""
    text = instruction_lines(text)
    if not mentions_attachment(text) or has_file(refs):
        return Binding()
    if not search:
        return Binding(needed=True, status="required")
    markdown_only = _wants_markdown(text)
    in_composer: set[uuid.UUID] = set()
    for r in composer_refs or []:
        if isinstance(r, dict) and r.get("kind") == "file_version":
            fv = db.get(FileVersion, _uuid(r.get("object_id")))
            if fv is not None:
                in_composer.add(fv.file_id)
    for found in (lambda: _by_name(db, conv.project_id, text),
                  lambda: _referenced_before(db, conv, in_composer, markdown_only),
                  lambda: _recent_uploads(db, user, conv.project_id, markdown_only)):
        cands = found()
        if cands:
            return Binding(needed=True, status="unique" if len(cands) == 1 else "ambiguous", reason=cands[0].reason,
                           candidates=cands[:MAX_CANDIDATES])
    return Binding(needed=True, status="required")


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def candidate_ref(db: Session, user: User, project_id: uuid.UUID, c: Candidate) -> dict[str, Any]:
    """A candidate as a normalised reference the composer can hold (``binding_reason`` says why it was found)."""
    from . import assistant_refs as refs

    ref = refs.normalize_ref(db, user, project_id, refs.AiRefInput(kind="file_version", object_id=c.fv.id))
    return {**ref, "binding_reason": c.reason}


def auto_reference(ref: dict[str, Any], c: Candidate) -> dict[str, Any]:
    """The request snapshot / message record of a bound file (design §2.5)."""
    return {"key": ref["key"], "file_version_id": str(c.fv.id), "name": c.f.name, "version": c.fv.version, "reason": c.reason}


def binding_error(b: Binding, candidates: list[dict[str, Any]]) -> AppError:
    if b.status == "ambiguous":
        return AppError(422, "attachment_ambiguous",
                        "the request points at an attached file and several files fit; choose the file to use (add it as a reference)",
                        {"recovery": "choose_file", "reason": b.reason, "candidates": candidates})
    return AppError(422, "attachment_required",
                    "the request points at an attached file but references none, and no file could be bound; add the file as a "
                    "reference (or say what to use instead)", {"recovery": "attach_file"})


def binding_out(db: Session, user: User, project_id: uuid.UUID, b: Binding) -> dict[str, Any]:
    """POST /ai/conversations/{id}/attachments/resolve: what a send would do with this text now (nothing is written)."""
    cands = [candidate_ref(db, user, project_id, c) for c in b.candidates]
    return {"needed": b.needed, "status": b.status, "reason": b.reason, "candidates": cands,
            "recovery": {"ambiguous": "choose_file", "required": "attach_file"}.get(b.status)}


# =====================================================================================================================
# Sources of the spec step (A-02, A-04)
# =====================================================================================================================

#: Change wording next to a referenced spec version ("change the bolt to M10", "adjust the thickness to 12", "add a washer", "remove the cover"):
#: a change verb, not a value or the Chinese word for "supplement" on its own ("generate an M8 bolt assembly from this spec and supplement it with drawings" asks for no change).
_REVISE = re.compile(r"(?:改|换|调整|更改|修改|替换)(?:成|为)|改用|换用|把.{1,20}(?:改|换|调整|替换|删|去掉)|增加|添加|加上|删除|去掉|删掉|移除|"
                     + _ascii_word(r"(?:change\s+\S+.{0,30}\bto|modify|revise|add|remove|delete|replace)"), re.IGNORECASE)


def request_sources(target: dict | None, references: list[dict]) -> tuple[list[dict], dict | None]:
    """(file version references, the one spec version reference) of a request; several spec versions: 422."""
    files: list[dict] = []
    spec: dict | None = None
    for r in ([target] if target else []) + list(references or []):
        if not isinstance(r, dict) or not r.get("object_id"):
            continue
        if r.get("kind") == "file_version":
            files.append(r)
        elif r.get("kind") == "engineering_spec_version":
            if spec is not None and str(spec["object_id"]) != str(r["object_id"]):
                raise AppError(422, "several_spec_versions", "reference one engineering spec version (or none) for a new assembly")
            spec = r
    return files, spec


def _spec_source_files(db: Session, spec_version_id: Any) -> set[str]:
    v = db.get(EngineeringSpecVersion, _uuid(spec_version_id)) if _uuid(spec_version_id) else None
    return {str(s.get("file_version_id")) for s in (v.sources if v is not None else []) or [] if s.get("file_version_id")}


def check_spec_and_sources(db: Session, *, spec_version_id: Any, file_version_ids: list[str], text: str, instruction: bool,
                           spec_key: str | None = None, file_keys: dict[str, str] | None = None) -> list[str]:
    """A-04: an existing spec version and new material together are ambiguous — build from that exact version, or extract
    a new spec from the new material — so they are refused with 422 ``spec_and_sources`` (recovery
    ``choose_spec_or_revise``) instead of silently using one of them. New material: a file version that is not already a
    source of that spec version; text — any text for a workflow / tool input (``instruction`` False), change wording or
    stated values for an assistant request (``instruction`` True: the request text is the instruction). Returns the
    file versions that are sources of the spec version already (read by it; nothing new)."""
    if not spec_version_id:
        return []
    known_list, new_files, changes = spec_material_mix(db, spec_version_id=spec_version_id, file_version_ids=file_version_ids, text=text,
                                                       instruction=instruction)
    if new_files or changes:
        details: dict[str, Any] = {"recovery": "choose_spec_or_revise", "spec_version_id": str(spec_version_id),
                                   "file_version_ids": new_files, "text_changes": changes}
        if spec_key:
            details["spec_key"] = spec_key
        if file_keys:
            details["file_keys"] = [file_keys[f] for f in new_files if f in file_keys]
        raise AppError(422, "spec_and_sources",
                       "an existing engineering spec version is referenced together with new files or change requests: build from "
                       "that exact version (revise false, or remove the new material), make a new version of that spec with the "
                       "new material (revise true), or extract a new spec from the new material (remove the spec version "
                       "reference)", details)
    return known_list


def spec_material_mix(db: Session, *, spec_version_id: Any, file_version_ids: list[str], text: str,
                      instruction: bool) -> tuple[list[str], list[str], bool]:
    """(file versions that version already read, new file versions, whether the text is a change) of a spec version
    referenced with material — what :func:`check_spec_and_sources` refuses unless the person chose (generation
    supplement I: ``revise`` true = a new version of that spec with the new material, false = the exact version, the new
    material dropped visibly)."""
    known = _spec_source_files(db, spec_version_id)
    new_files = [f for f in dict.fromkeys(str(x) for x in file_version_ids) if f not in known]
    t = (text or "").strip()
    changes = bool(t) and (not instruction or bool(_REVISE.search(t)))
    return [str(x) for x in file_version_ids if str(x) in known], new_files, changes


def file_problems(db: Session, project_id: uuid.UUID, file_version_ids: list[str]) -> list[dict[str, Any]]:
    """Requested file versions the spec step cannot read at all (missing / archived): ``source_unread`` entries. A file
    of another project is the caller's ``bad_input`` (as every file input), never listed here."""
    out = []
    for fid in file_version_ids:
        fv = db.get(FileVersion, _uuid(fid)) if _uuid(fid) else None
        f = db.get(File, fv.file_id) if fv is not None else None
        if fv is None or f is None:
            out.append({"file_version_id": str(fid), "kind": "file_version", "read_status": "failed",
                        "error": {"code": "file_version_missing", "message": "the file version no longer exists"}})
        elif f.project_id != project_id:
            continue
        elif f.archived_at is not None:
            out.append({"file_version_id": str(fv.id), "kind": "file_version", "name": f.name, "version": fv.version, "read_status": "failed",
                        "error": {"code": "file_archived", "message": "the file is archived"}})
    return out


def _norm_line(line: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", line).split()).casefold()


_CONTENT = re.compile(r"[0-9A-Za-z一-鿿]")


def dedupe_text(db: Session, text: str, file_version_ids: list[str]) -> tuple[str, dict[str, Any] | None]:
    """Item 5: a pasted line that is also a line of an attached text file is read once, from the file (the attachment
    wins); lines only in the text (the instruction, extra statements) stay. Returns (text to read, record or None)."""
    from . import source_reader as sr

    if not (text or "").strip() or not file_version_ids:
        return text, None
    lines: set[str] = set()
    used: list[dict[str, Any]] = []
    for fid in file_version_ids:
        fv = db.get(FileVersion, _uuid(fid)) if _uuid(fid) else None
        body = sr.file_version_text(db, fv) if fv is not None else None
        if not body:
            continue
        mine = {n for n in (_norm_line(x) for x in body.splitlines()) if n and _CONTENT.search(n)}
        if mine:
            lines |= mine
            f = db.get(File, fv.file_id)
            used.append({"file_version_id": str(fv.id), "name": f.name if f else fv.original_name, "version": fv.version, "_lines": mine})
    if not lines:
        return text, None
    kept: list[str] = []
    removed, removed_chars = 0, 0
    hit: set[str] = set()
    for line in text.splitlines():
        n = _norm_line(line)
        if n and _CONTENT.search(n) and n in lines:
            removed += 1
            removed_chars += len(line)
            hit |= {u["file_version_id"] for u in used if n in u["_lines"]}
            continue
        kept.append(line)
    if not removed:
        return text, None
    out = "\n".join(kept).strip()
    record = {"rule": "pasted_lines_in_attachment", "lines_removed": removed, "chars_removed": removed_chars, "text_chars_kept": len(out),
              "text_dropped": not out, "files": [{k: v for k, v in u.items() if k != "_lines"} for u in used if u["file_version_id"] in hit]}
    return out, record


# =====================================================================================================================
# Pre-check before CAD (A-03)
# =====================================================================================================================

#: Column names of a parts list: the item column and the quantity column (a header row needs both, as whole cells).
_ITEM_COL = re.compile(r"^(?:序号|名称|零件名称|零件|件名|物料|物料名称|代号|图号|编号|零件号|规格|型号|名称及规格|"
                       r"part|parts|part name|part no\.?|part number|name|item|item no\.?|description|p/n)$", re.IGNORECASE)
_QTY_COL = re.compile(r"^(?:数量|件数|个数|用量|数目|qty|quantity|pcs|count)$", re.IGNORECASE)
_QTY_WORD = re.compile(r"数量|件数|用量|" + _ascii_word(r"(?:qty|quantity)"), re.IGNORECASE)
_LIST_TITLE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\d+[.、)]\s*)?(?:零件)?(?:明细表|明细栏|零件清单|零件表|零件明细|物料清单|材料清单|BOM|"
                         r"bill of materials|parts? list)\s*[:：]?\s*$", re.IGNORECASE)
_CELL_SPLIT = re.compile(r"\s*[|\t,;]\s*")
_CELL_UNIT = re.compile(r"\s*[（(][^)）]{0,12}[)）]\s*$|[:：*]+$")


def _cell(c: str) -> str:
    return _CELL_UNIT.sub("", c.strip().strip("*").strip())


def _header_row(line: str) -> bool:
    """A table header row: ``|`` / tab / comma / semicolon separated cells, one of them an item column name and one a
    quantity column name (whole cells, a unit in brackets allowed: "Quantity (pieces)")."""
    if not re.search(r"[|\t,;]", line):
        return False
    cells = [_cell(c) for c in _CELL_SPLIT.split(line.strip().strip("|")) if c.strip()]
    return len(cells) >= 2 and any(_QTY_COL.match(c) for c in cells) and any(_ITEM_COL.match(c) for c in cells)


def parts_list_evidence(texts: list[tuple[str, str, str]]) -> dict[str, Any] | None:
    """Whether the read text looks like it lists parts (A-03 extraction missed): rule ``parts_table_header`` — a table
    header row (``|`` / tab / comma separated) naming an item column and a quantity column; rule ``parts_list_title`` —
    a heading line "BOM / parts list" or their Chinese equivalents with a quantity column word in the same source. ``texts``:
    (source id, locator, text) of what was read. Returns the rule, where and the line, or None."""
    titled: dict[str, tuple[str, str]] = {}
    has_qty: set[str] = set()
    for sid, loc, text in texts:
        for line in (text or "").splitlines():
            if _header_row(line):
                return {"rule": "parts_table_header", "source": sid, "locator": loc, "line": line.strip()[:160]}
            if _LIST_TITLE.match(line):
                titled.setdefault(sid, (loc, line.strip()[:160]))
            if _QTY_WORD.search(line):
                has_qty.add(sid)
    for sid, (loc, line) in titled.items():
        if sid in has_qty:
            return {"rule": "parts_list_title", "source": sid, "locator": loc, "line": line}
    return None


def _read_texts(db: Session, v: EngineeringSpecVersion) -> list[tuple[str, str, str]]:
    """(source id, locator, text) of every text / drawing chunk of the version (what the spec step had to read)."""
    out = []
    for c in db.scalars(select(EngineeringSpecChunk).where(EngineeringSpecChunk.version_id == v.id, EngineeringSpecChunk.kind != "attachment")
                        .order_by(EngineeringSpecChunk.seq)).all():
        if not c.text_sha256:
            continue
        try:
            out.append((c.source_id, c.locator, read_blob(db, c.text_sha256)[0].decode("utf-8", errors="replace")))
        except (FileNotFoundError, OSError):
            continue
    return out


def source_entry(s: dict[str, Any]) -> dict[str, Any]:
    """A spec source as the port value / failure details show it (kind, exact file version, name, read state, ranges,
    error)."""
    out = {"id": s.get("id"), "kind": s.get("kind"), "file_version_id": s.get("file_version_id"), "name": s.get("name") or "",
           "label": s.get("label") or "", "version": s.get("version"), "read_status": s.get("read_status") or "unread",
           "ranges": list(s.get("ranges") or [])[:40], "error": s.get("error")}
    if s.get("answer_of"):
        out["answer_of"] = s["answer_of"]
    if s.get("unread"):
        out["unread"] = len(s["unread"])
    if s.get("chars_read") is not None:
        out["chars_read"] = s.get("chars_read")
    if isinstance(s.get("coverage"), dict):
        # generation reliability K (design §2.4): where every drawing entity went, next to the chunk progress (the groups
        # one by one stay on the spec version)
        out["coverage"] = {k: v for k, v in s["coverage"].items() if k != "outside_groups"}
    return out


def unread_sources(v: EngineeringSpecVersion, requested: list[str]) -> list[dict[str, Any]]:
    """A-02: sources of the version that were not read (failed / unread) and requested file versions that are not among
    its sources at all. Partial reads (e.g. drawing entities outside every frame) are shown, not refused."""
    sources = [s for s in v.sources or [] if isinstance(s, dict)]
    out = [source_entry(s) for s in sources if (s.get("read_status") or "unread") in ("failed", "unread")]
    have = {str(s.get("file_version_id")) for s in sources if s.get("file_version_id")}
    for fid in dict.fromkeys(str(x) for x in requested):
        if fid not in have:
            out.append({"file_version_id": fid, "kind": "file_version", "read_status": "unread",
                        "error": {"code": "not_in_spec_sources", "message": "the requested file is not among the spec's sources"}})
    return out


def failure(code: str, message: str, *, recovery: str, retryable: bool = False, **details: Any) -> dict[str, Any]:
    """A spec step failure in the S3 shape (stage, retryable, recovery) with its details."""
    return {"code": code, "message": message, "stage": "spec_precheck", "retryable": retryable, "recovery": recovery,
            "details": {"recovery": recovery, **details}}


@dataclass
class Precheck:
    """The pre-check of a usable spec version: a failure for the node, or a derived version to use instead (Turbo
    structure repair), or neither (go on with the version as it is)."""

    failure: dict[str, Any] | None = None
    derived: EngineeringSpecVersion | None = None
    repaired: list[str] = field(default_factory=list)
    # Generation supplement H (services/spec_aliases.py): which rule derived the version, the ai_calls rows of the Turbo
    # alias / structure calls (bound to the node run by the caller) and a brief for the step record.
    rule: str = "hang_unplaced_under_root"
    call_ids: list[str] = field(default_factory=list)
    aliases: dict[str, Any] | None = None


def precheck(db: Session, v: EngineeringSpecVersion, *, requested_files: list[str], policy: str, user_id: uuid.UUID,
             title: str = "", model_ref: Any = None) -> Precheck:
    """A-03 before any CAD step (design §2.2 spec rows), in this order: unread sources, no part definition (extraction
    missed when the text read looks like a parts list), no part definition under the root (standard: refused; Turbo:
    a derived version hangs them under the root, :func:`hang_unplaced`). Turbo with some definitions under the root and
    others not (supplement H): the run's planning model (``model_ref``; default the version's frozen one) resolves the
    unplaced ones as aliases / members / non-parts in a derived version (``spec_aliases.precheck_aliases``)."""
    from .. import policy as pol
    from . import engineering_spec as spec_svc
    from .spec_coverage import targets

    sid = str(v.id)
    bad = unread_sources(v, requested_files)
    if bad:
        return Precheck(failure=failure("source_unread", "some sources of this request were not read: " + ", ".join(
            str(b.get("name") or b.get("label") or b.get("file_version_id") or b.get("id")) for b in bad) + "; replace or fix them and run again",
            recovery="replace_source", sources=bad, spec_version_id=sid))
    content = v.content or {}
    t = targets(content)
    if t["recognized_definitions"] == 0:
        ev = parts_list_evidence(_read_texts(db, v))
        if ev is not None:
            return Precheck(failure=failure("extraction_missed_parts", "the sources look like they list parts (" + ev["rule"] + ": " + ev["line"]
                                            + ") but the spec recognised none; extract again or fix the material", recovery="re_extract",
                                            retryable=True, evidence=ev, spec_version_id=sid))
        return Precheck(failure=failure("spec_no_parts", "the sources define no part that could be modeled (only "
                                        + (f"the root assembly {t['root']}" if content.get("root") else "no definition") + ")",
                                        recovery="fix_input", spec_version_id=sid, root=content.get("root"),
                                        definitions=len(content.get("definitions") or [])))
    if t["definitions"] > 0:
        if pol.is_turbo(policy) and t["unplaced"]:
            from .spec_aliases import precheck_aliases

            return precheck_aliases(db, v, user_id=user_id, model_ref=model_ref)
        return Precheck()
    if not pol.is_turbo(policy):
        return Precheck(failure=failure("spec_structure_incomplete", "the spec has part definitions but none hangs under the root assembly; "
                                        "state where they go (containment) in the spec, or run in Turbo", recovery="fix_spec",
                                        spec_version_id=sid, root=content.get("root"), unplaced=t["unplaced"]))
    repaired, hung = hang_unplaced(content, title=title or str(content.get("title") or ""))
    if targets(repaired)["definitions"] == 0:
        return Precheck(failure=failure("spec_structure_incomplete", "the definitions could not be hung under the root by rule",
                                        recovery="fix_spec", spec_version_id=sid, root=content.get("root"), unplaced=t["unplaced"]))
    derived = spec_svc.derive_rule_version(db, v, content=repaired, created_by=user_id,
                                           note={"rule": "hang_unplaced_under_root", "definitions": hung})
    return Precheck(derived=derived, repaired=hung)


def _next_key(used: set[str], prefix: str, width: int = 2, start: int = 1) -> str:
    n = start
    while f"{prefix}-{n:0{width}d}" in used:
        n += 1
    key = f"{prefix}-{n:0{width}d}"
    used.add(key)
    return key


_QTY_FIELDS = ("quantity", "qty", "count", "数量")


def _stated_quantity(content: dict[str, Any], key: str, root: str) -> tuple[int, str] | None:
    """A quantity the spec states for the definition (a fact ``<key>.quantity`` / ``<root>.qty[<key>]``), else None."""
    subjects = {f"{key}.{f}" for f in _QTY_FIELDS} | {f"{root}.qty[{key}]"}
    for fct in content.get("facts") or []:
        if not isinstance(fct, dict) or fct.get("subject") not in subjects or fct.get("status") not in ("asserted", "derived"):
            continue
        val = fct.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val).is_integer() and 1 <= int(val) <= 1000:
            return int(val), str(fct.get("id") or fct.get("subject"))
    return None


def hang_unplaced(content: dict[str, Any], *, title: str = "") -> tuple[dict[str, Any], list[str]]:
    """Turbo structure repair (design §3 A item 3): every definition that hangs nowhere and is no child of another
    definition gets instances under the root — the quantity the spec states for it, else 1; placement left null (the
    assembly step decides and records it). A spec without a root definition gets one. One ``rule`` decision
    (``policy.Assumption`` shape, trigger ``missing``) per definition hung, and one for a created root. Pure: returns
    (new content, keys hung)."""
    from .assemblies import spec_tree

    c = copy.deepcopy(content)
    c.setdefault("definitions", [])
    c.setdefault("instances", [])
    c.setdefault("assumptions", [])
    defs = {d["key"]: d for d in c["definitions"] if isinstance(d, dict) and d.get("key")}
    used_a = {str(a.get("id")) for a in c["assumptions"] if isinstance(a, dict)}
    used_i = {str(i.get("key")) for i in c["instances"] if isinstance(i, dict)}
    at = datetime.now(UTC).isoformat()
    root = c.get("root")
    if not root or root not in defs:
        root = "D-00" if "D-00" not in defs else _next_key(set(defs), "D")
        name = (title or "总装").strip()[:120]
        c["definitions"].insert(0, {"key": root, "kind": "assembly", "name": name, "number": None, "material": None, "procurement": "unknown",
                                    "representation": "exact", "template": None, "fact_ids": [], "notes": ""})
        defs[root] = c["definitions"][0]
        c["root"] = root
        c["assumptions"].append({"id": _next_key(used_a, "A"), "subject": root, "field": "root", "adopted": f"新建根装配 {root}「{name}」",
                                 "unit": None, "basis": "资料识别出零件定义，但没有说明总装（根装配）；按规则新建一个根装配来容纳这些定义，名称取规格标题",
                                 "alternatives": [], "affected": [root], "sources": [], "author": "rule", "at": at, "trigger": "missing"})
    from .spec_identity import placeholder_reason

    tree = spec_tree(c)
    attached = {i.get("definition") for i in c["instances"] if isinstance(i, dict) and i.get("parent") in defs}
    # generation reliability K: a title / "—" / placeholder is no part (the exclusion audit), never hung as one
    tops = [k for k in tree["unplaced"] if k != root and k not in attached and placeholder_reason(defs[k], c) is None]
    hung: list[str] = []
    for k in tops:
        d = defs[k]
        stated = _stated_quantity(c, k, root)
        n = stated[0] if stated else 1
        keys = [_next_key(used_i, "I") for _ in range(n)]
        for ik in keys:
            c["instances"].append({"key": ik, "definition": k, "parent": root, "name": d.get("name") or k, "placement": None, "fact_ids": []})
        kind_zh = {"assembly": "子装配", "standard_part": "标准件"}.get(str(d.get("kind")), "零件")
        qty = f"数量取规格写明的 {n}（{stated[1]}）" if stated else "资料没有写数量，取 1"
        evidence = [f"{e.get('source')} · {e.get('locator')}" for fct in c.get("facts") or [] if isinstance(fct, dict)
                    and str(fct.get("subject") or "").startswith(f"{k}.") for e in fct.get("evidence") or [] if isinstance(e, dict)][:5]
        c["assumptions"].append({
            "id": _next_key(used_a, "A"), "subject": k, "field": "parent",
            "adopted": f"挂到根装配 {root} 下，{n} 个实例（{'、'.join(keys)}）",
            "unit": None,
            "basis": f"规格识别出{kind_zh}定义 {k}「{d.get('name') or k}」，但资料没有说明它装在哪个装配里，根装配 {root} 下也没有任何零件实例；"
                     f"按规则把它挂到根装配下，{qty}，位置留空，由装配步骤按记录的判断摆放",
            "alternatives": [], "affected": [k, *keys, root], "sources": evidence, "author": "rule", "at": at, "trigger": "missing"})
        hung.append(k)
    return c, hung
