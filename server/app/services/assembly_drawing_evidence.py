"""Original drawing sheets shared by assembly part modeling and placement.

The cache is owned by the orchestration thread. Returned bundles contain only plain
values and can be passed to modeling workers; no ORM objects cross that boundary.
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..ai.modeling_agent import DrawingInput
from ..ai.providers.base import ImagePart
from ..cad.drawing_evidence import DrawingEvidenceError, evidence_prompt, extract_evidence, render_frame_png, render_size
from ..errors import AppError
from ..models import EngineeringSpecVersion
from . import ai_reconstruct as reconstruct
from .drawing_identity import title_match

EVIDENCE_VERSION = "assembly-drawing-evidence/9"
MAX_SHEET_CHARS = 1_000_000


@dataclass
class DrawingBundle:
    drawing: DrawingInput
    source_file_version_id: uuid.UUID | None
    checks: dict[str, Any]


class DrawingEvidenceCache:
    """Read pinned DWG/DXF sources once; select actual detail sheets, never BOM mentions.

    A definition without a detail sheet (e.g. a standard fastener) keeps the text
    modeling path. Multiple equally matching sheets fail explicitly.
    """

    def __init__(self, db: Session, spec_version: EngineeringSpecVersion, *, vision: bool = False) -> None:
        self.db, self.version, self.vision = db, spec_version, vision
        self._sources: list[tuple[str, Any, Any]] | None = None
        self._bundles: dict[tuple[Any, ...], DrawingBundle] = {}
        self._renders: dict[tuple[str, str], bytes] = {}

    def _load(self) -> list[tuple[str, Any, Any]]:
        if self._sources is None:
            loaded: list[tuple[str, Any, Any]] = []
            seen: set[str] = set()
            for entry in self.version.sources or []:
                fid = str(entry.get("file_version_id") or "")
                name = str(entry.get("name") or "").casefold()
                if not fid or fid in seen or not name.endswith((".dwg", ".dxf")):
                    continue
                seen.add(fid)
                src = reconstruct.load_source(self.db, project_id=self.version.project_id, file_version_id=fid)
                if entry.get("sha256") and hashlib.sha256(src.data).hexdigest() != entry["sha256"]:
                    raise AppError(422, "sha_mismatch", "the drawing content differs from the pinned spec source",
                                   {"file_version_id": fid, "source": entry.get("id")})
                evidence = extract_evidence(src.dxf, deadline=time.monotonic() + reconstruct.EVIDENCE_BUDGET_S)
                loaded.append((str(entry.get("id") or fid), src, evidence))
            self._sources = loaded
        return self._sources

    def for_definition(self, definition: dict[str, Any], content: dict[str, Any]) -> DrawingBundle | None:
        frames = [(sid, src, ev, f) for sid, src, ev in self._load() for f in ev.frames]
        def match(row: Any) -> str | None:
            return title_match(definition, number=reconstruct.frame_number(row[3]), name=reconstruct.frame_name(row[3]))

        matches = [row for row in frames if match(row) == "canonical"]
        inherited = None
        if not matches:
            inherited = self._visual_parent_sheet(definition, content, frames)
            if inherited:
                matches = [inherited[0]]
        if not matches:
            # An obsolete number must not select another component's title. An
            # exact-name alternative stays a source candidate, never a renumbering.
            matches = [row for row in frames if match(row) == "alternate"]
        if len(matches) > 1:
            raise AppError(422, "drawing_sheet_ambiguous", "several original drawing sheets match this definition",
                           {"definition": definition.get("key"), "sheets": [f"{s}:{f.id}" for s, _, _, f in matches]})
        if not matches:
            return None
        sid, src, evidence, frame = matches[0]
        visual = inherited[1] if inherited else None
        key = (str(src.file_version_id), frame.id, definition.get("key"), definition.get("number"), definition.get("name"),
               tuple(tuple(e["region"]) for e in visual) if visual else ())
        if key in self._bundles:
            return self._bundles[key]
        resolution = reconstruct.units_resolution(evidence, frame.id, notes="", answers=[])
        units = resolution["units"]
        if units is None:
            raise AppError(422, "drawing_units_ambiguous", "the selected drawing sheet has no unambiguous length unit",
                           {"definition": definition.get("key"), "frame": frame.id, "signals": resolution["signals"]})
        literal = evidence_prompt(evidence, frame.id, max_chars=MAX_SHEET_CHARS)
        if "\nTRUNCATED" in literal:
            raise AppError(422, "drawing_context_over_budget", "the selected sheet exceeds the evidence limit",
                           {"frame": frame.id, "limit_chars": MAX_SHEET_CHARS})
        images: list[ImagePart] = []
        render_error: dict[str, str] | None = None
        if self.vision:
            try:
                render_key = (str(src.file_version_id), frame.id)
                if render_key not in self._renders:
                    self._renders[render_key] = render_frame_png(src.dxf, frame, px=reconstruct.RENDER_PX,
                                                               deadline=time.monotonic() + reconstruct.EVIDENCE_BUDGET_S)
                png = self._renders[render_key]
                images.append(ImagePart(mime_type="image/png", data=png, name=f"{sid}-{frame.id}.png"))
            except DrawingEvidenceError as exc:
                render_error = {"code": exc.code, "message": str(exc)[:300]}
        if visual:
            if not images:
                raise AppError(422, "visual_drawing_unavailable", "this component needs its original attached parent-sheet image",
                               {"definition": definition.get("key"), "frame": frame.id, "render_error": render_error})
            width, height = render_size(frame, reconstruct.RENDER_PX)
            actual_hash = hashlib.sha256(images[0].data).hexdigest()
            if any(e.get("image_sha256") != actual_hash or (e.get("image_width"), e.get("image_height")) != (width, height) for e in visual):
                raise AppError(422, "visual_drawing_changed", "the component's recorded source image no longer matches this render",
                               {"definition": definition.get("key"), "frame": frame.id})
        cls = reconstruct.classify_dimensions(evidence, frame.id)
        values, kinds = reconstruct.drawing_values(evidence, frame.id)
        label = reconstruct.frame_label(frame)
        block = "\n".join([
            f"Original source {sid}: {src.name}; frame {frame.id}; sheet {label}.", reconstruct.units_line(units),
            *( [f"This is the immediate parent assembly's source sheet. Model ONLY the separately visible child "
                 f"{definition.get('name')!r}, not the whole assembly or adjacent hardware. Its validated visual regions "
                 f"[x,y,w,h] in original image pixels are {[e['region'] for e in visual]}. "
                 f"Separation rationale: {'; '.join(str(e.get('detail') or '') for e in visual)}. "
                 "Use those regions to identify the child; use displayed dimensions and sections, never pixel lengths, for dimensions."]
               if visual else []),
            *reconstruct._index_scale_note(evidence, units),
            "Use the section and orthographic views together to read shape, interfaces and the local datum frame. "
            "Drawing coordinates are paper coordinates, not assembly translations. Opening groups are distinct only "
            "when the original views establish distinct physical openings and their faces. Different field names or "
            "diameters in the extracted spec do not establish extra holes. A parent assembly's interface diameter may "
            "describe a mating part or another reading of an existing opening. Compare its source frame with this "
            "detail sheet and record any unresolved difference; never put an extra group on an otherwise blank face "
            "to accommodate it. A separate bottom plate in the BOM must not be added to a wall part. The original "
            "profiles, displayed dimensions and feature topology are primary; the extracted spec is a secondary "
            "summary and may have split one feature into several fields or assigned a neighboring part's dimension.",
            "image: " + ("attached original sheet render" if images else "not attached; use literal evidence"),
            literal, reconstruct.classification_text(cls),
        ])
        record = reconstruct.evidence_record(evidence, frame, src, units, cls, bool(images))
        record.update(service_version=EVIDENCE_VERSION, source_id=sid,
                      image_sha256=hashlib.sha256(images[0].data).hexdigest() if images else None,
                      render_error=render_error)
        record["title_binding"] = {"definition": definition.get("key"), "number": definition.get("number"),
                                   "name": definition.get("name"), "match": "visual_parent" if visual else match(matches[0])}
        if visual:
            record["inherited_for_visual_component"] = {"definition": definition.get("key"), "evidence": visual}
        drawing = DrawingInput(block=block, sheet_label=label, values=values, kinds=kinds, images=images, assembly_component=True,
                               gate=reconstruct.make_conflict_gate(cls, "zh", []),
                               detail={"frame_id": frame.id, "source_id": sid, "source_format": src.fmt,
                                       "evidence_version": EVIDENCE_VERSION},
                               derivation_sources=tuple(v for k, v in values.items() if k not in cls["conflicts"]))
        bundle = DrawingBundle(drawing=drawing, source_file_version_id=src.file_version_id,
                               checks={"source": src.provenance, "evidence": record})
        self._bundles[key] = bundle
        return bundle

    def _visual_parent_sheet(self, definition: dict[str, Any], content: dict[str, Any], frames: list[Any]) -> Any:
        """Only server-located visual_presence evidence authorizes inheritance of a parent's exact title sheet."""
        key = definition.get("key")
        parents = {i.get("parent") for i in content.get("instances", []) if i.get("definition") == key}
        parent_defs = [d for d in content.get("definitions", []) if d.get("key") in parents and d.get("kind") == "assembly"]
        facts = [f for f in content.get("facts", []) if f.get("subject") == f"{key}.visual_presence" and f.get("status") == "derived"]
        if not facts:
            return None
        candidates: dict[tuple[str, str], tuple[Any, list[Any]]] = {}
        for fact in facts:
            for ev in fact.get("evidence", []):
                if not (ev.get("located") and not ev.get("verified") and ev.get("region") and ev.get("frame_id")
                        and ev.get("image_sha256") and ev.get("file_version_id") and ev.get("source_sha256")):
                    continue
                for row in frames:
                    sid, src, evidence, frame = row
                    if (sid != ev.get("source") or str(src.file_version_id) != str(ev["file_version_id"])
                            or frame.id != ev["frame_id"] or hashlib.sha256(src.data).hexdigest() != ev["source_sha256"]):
                        continue
                    region = ev["region"]
                    width, height = render_size(frame, reconstruct.RENDER_PX)
                    if (len(region) != 4 or any(isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) for v in region)
                            or region[0] < 0 or region[1] < 0 or region[2] <= 0 or region[3] <= 0
                            or region[0] + region[2] > width or region[1] + region[3] > height):
                        continue
                    if not any(title_match(p, number=reconstruct.frame_number(frame), name=reconstruct.frame_name(frame))
                               == "canonical" for p in parent_defs):
                        continue
                    candidate = candidates.setdefault((sid, frame.id), (row, []))
                    if ev not in candidate[1]:
                        candidate[1].append(ev)
        if len(candidates) != 1:
            raise AppError(422, "visual_drawing_ambiguous", "visual component evidence does not identify one pinned immediate-parent sheet",
                           {"definition": key, "matched_sheets": list(candidates)})
        return next(iter(candidates.values()))


def source_fingerprint(version: EngineeringSpecVersion) -> dict[str, Any]:
    """Changing the source or reader invalidates old text-only cached part designs."""
    return {"version": EVIDENCE_VERSION, "sources": [{"id": s.get("file_version_id"), "sha256": s.get("sha256")}
            for s in version.sources or [] if str(s.get("name") or "").casefold().endswith((".dwg", ".dxf"))]}
