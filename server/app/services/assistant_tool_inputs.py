"""Server-only child inputs; never a model-facing free-form request overlay."""
from dataclasses import dataclass, field
import uuid

from ..ai.design_contracts import DesignPlanContent, DesignRef


@dataclass(frozen=True)
class ResolvedDesignPlan:
    ref: DesignRef
    content: DesignPlanContent
    source_snapshot: dict
    source_sha256: str
    authorization: dict


def design_part_id(root_operation_id: uuid.UUID, ref: DesignRef) -> uuid.UUID:
    """Replay identity of a new planned part; callers must separately resolve exact design authority."""
    return uuid.uuid5(root_operation_id, f"design-part:{ref.version_id}")


def source_text(request: dict) -> str:
    """Use the server-pinned human source, never a tool's shortened engineering description.

    Historical and direct operations predate the snapshot and keep their original request text. Long human
    requests still use the existing exact material file references; the snapshot does not replace that contract.
    """
    source = request.get("human_source")
    if isinstance(source, dict) and isinstance(source.get("text"), str):
        return "\n\n".join(item["text"] for item in [source, *(source.get("amendments") or [])]
                           if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"])
    return str(request.get("text") or "")


def extraction_text(request: dict) -> str:
    """A long paste is read once from its exact material file, not again from the displayed excerpt."""
    from .assistant_materials import is_excerpt

    source = request.get("human_source")
    if isinstance(source, dict) and "amendments" in source:
        # Each human message has its own material boundary. A material from the original request must not
        # suppress a short amendment, and a new long amendment is read only through its exact file version.
        return "\n\n".join(item["text"] for item in [source, *source["amendments"]]
                           if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"]
                           and not item.get("material_keys") and not is_excerpt(item["text"]))
    text = source_text(request)
    materials = [item for item in request.get("materials") or [] if isinstance(item, dict)]
    return "" if materials and (any(item.get("new") for item in materials) or is_excerpt(text)) else text


@dataclass(frozen=True)
class ChildInput:
    title: str = ""
    requirements: str = ""
    sheet: str = ""
    notes: str = ""
    reuse_earlier_builds: bool | None = None
    references: tuple[dict, ...] = field(default_factory=tuple)
    design_ref: DesignRef | None = None
    source_operation_id: str | None = None
    design_from_requirements: bool = False

    def payload(self) -> dict:
        out = {"title": self.title, "requirements": self.requirements, "sheet": self.sheet, "notes": self.notes,
               "source_operation_id": self.source_operation_id, "design_from_requirements": self.design_from_requirements}
        if self.design_ref is not None:
            out['design_ref'] = self.design_ref.model_dump(mode='json')
        if self.reuse_earlier_builds is not None:
            out["reuse_earlier_builds"] = self.reuse_earlier_builds
        return out
