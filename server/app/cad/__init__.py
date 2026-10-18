"""Pure geometry library (no DB): modeling ops, kernel, importers, drawings, diffs,
requirements, the independent requirement checker, the STEP source audit and the native DWG adapter.

``ops``, ``results``, ``requirements`` and ``diff`` are light (pydantic only).
The kernel, drawing, STEP import, checker (``conformance``) and STEP audit
(``step_source``) load build123d / OCP (several seconds on first import), so
they are exposed lazily: ``from app.cad import build`` imports the kernel on
first access, while ``from app.cad.ops import ModelSpec`` stays cheap.
The DWG adapter (``dwg``, LibreDWG run as external processes, see
``DWG_NOTES.md``) is lazy as well.
"""

from __future__ import annotations

import importlib
from typing import Any

from .ops import (
    EdgeSelector,
    ModelSpec,
    Operation,
    StepBase,
    build_key,
    hole_frame,
    hole_positions,
    model_spec_json_schema,
    next_op_id,
)
from .requirements import (
    CRITICAL_FIELDS,
    BandReq,
    CriticalGap,
    EnvelopeReq,
    Evidence,
    HoleReq,
    NoteReq,
    PocketReq,
    Requirement,
    RequirementList,
    Requirements,
    default_question,
    gate,
    ground,
    requirements_json_schema,
    requirements_sha256,
)
from .results import (
    BuildArtifacts,
    BuildResult,
    DetectedHole,
    DrawingConfig,
    DrawingOutput,
    DwgBlob,
    DwgQuarantinedEntity,
    DwgReadbackCheck,
    DwgReadResult,
    DwgRuntime,
    DwgStep,
    DwgTool,
    DwgWriteResult,
    FeatureRecord,
    HoleRecord,
    Issue,
    MeshInspection,
    Metrics,
    TitleBlock,
    ViewInfo,
)

_LAZY = {
    "KERNEL_VERSION": "kernel",
    "build": "kernel",
    "select_edges": "kernel",
    "detect_holes": "kernel",
    "hole_callout_text": "kernel",
    "KernelError": "kernel",
    "inspect_step": "importers",
    "inspect_mesh": "importers",
    "MeshParseError": "importers",
    "make_drawing": "drawing",
    "DrawingError": "drawing",
    "diff_specs": "diff",
    "diff_metrics": "diff",
    "OpChange": "diff",
    "FieldChange": "diff",
    "dwg_runtime": "dwg",
    "detect_dwg_version": "dwg",
    "read_dwg": "dwg",
    "write_dwg": "dwg",
    "DwgError": "dwg",
    "check_requirements": "conformance",
    "CheckReport": "conformance",
    "CheckItem": "conformance",
    "Tolerance": "conformance",
    "audit_step": "step_source",
    "StepAudit": "step_source",
}

__all__ = [
    "CRITICAL_FIELDS",
    "BandReq",
    "CriticalGap",
    "EnvelopeReq",
    "Evidence",
    "HoleReq",
    "NoteReq",
    "PocketReq",
    "Requirement",
    "RequirementList",
    "Requirements",
    "default_question",
    "gate",
    "ground",
    "requirements_json_schema",
    "requirements_sha256",
    "BuildArtifacts",
    "BuildResult",
    "DetectedHole",
    "DrawingConfig",
    "DrawingOutput",
    "DwgBlob",
    "DwgQuarantinedEntity",
    "DwgReadbackCheck",
    "DwgReadResult",
    "DwgRuntime",
    "DwgStep",
    "DwgTool",
    "DwgWriteResult",
    "EdgeSelector",
    "FeatureRecord",
    "HoleRecord",
    "Issue",
    "MeshInspection",
    "Metrics",
    "ModelSpec",
    "Operation",
    "StepBase",
    "TitleBlock",
    "ViewInfo",
    "build_key",
    "hole_frame",
    "hole_positions",
    "model_spec_json_schema",
    "next_op_id",
    *_LAZY,
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'app.cad' has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
