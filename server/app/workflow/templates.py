"""Built-in workflow templates (design.md §7)."""

from __future__ import annotations

import copy

from .graph import validate_graph


def _n(id_: str, type_: str, x: float, y: float, params: dict | None = None, label: str | None = None) -> dict:
    node = {"id": id_, "type": type_, "position": {"x": x, "y": y}, "params": params or {}}
    if label:
        node["label"] = label
    return node


def _e(src: str, sp: str, dst: str, dp: str) -> dict:
    return {"id": f"{src}.{sp}->{dst}.{dp}", "source": src, "source_port": sp, "target": dst, "target_port": dp}


TEMPLATES: dict[str, dict] = {
    "natural_language_manufacturing": {
        "name": {"en": "One request to manufacturing estimates", "zh": "一句话到建模、图纸、工艺与估算报价"},
        "description": {
            "en": "AI plans a concept, builds parts and assemblies, creates layered drawings and exports machining plans and draft quotations. "
                  "Prices use synthetic example rates; purchased parts and assembly labour need separate prices.",
            "zh": "输入一句需求，AI 规划方案并生成零件与装配、分层图纸、加工工艺及报价草稿。费率为示例估算，外购件与装配人工费用待补充。",
        },
        "graph": {
            "nodes": [
                _n("request", "text_input", 0, 120, {"text": ""}),
                _n("concept", "concept_design", 280, 120),
                _n("assembly", "assembly_from_spec", 560, 120),
                _n("drawings", "assembly_drawing", 840, 0, {"subassemblies": True}),
                _n("process", "assembly_process_plan", 840, 260),
                _n("quote", "assembly_quote", 1120, 260),
                _n("export", "manufacturing_export", 1400, 120),
            ],
            "edges": [
                _e("request", "text", "concept", "text"),
                _e("concept", "spec", "assembly", "spec"),
                _e("assembly", "assembly", "drawings", "assembly"),
                _e("assembly", "assembly", "export", "assembly"),
                _e("drawings", "drawings", "export", "drawings"),
                _e("assembly", "assembly", "process", "assembly"),
                _e("process", "plans", "quote", "plans"),
                _e("process", "plans", "export", "plans"),
                _e("quote", "quotes", "export", "quotes"),
            ],
            "groups": [
                {"id": "design", "label": "Design", "node_ids": ["request", "concept", "assembly", "drawings"]},
                {"id": "manufacturing", "label": "Manufacturing", "node_ids": ["process", "quote"]},
            ],
        },
    },
    "design_to_quote": {
        "name": {"en": "Design to quote", "zh": "设计到报价"},
        "description": {
            "en": "Describe a part, let AI model it in the cloud kernel, confirm the design, generate drawings, plan the process and quote.",
            "zh": "用文字描述零件，AI 在云端内核建模；确认设计后生成图纸、规划工艺并报价。",
        },
        "graph": {
            "nodes": [
                _n("request", "text_input", 0, 120, {"text": ""}),
                _n("model", "model_3d", 280, 120),
                _n("design_review", "human_review", 560, 120, {"stage": "design", "instructions": "Check geometry, dimensions and holes."}),
                _n("drawing", "drawing", 840, 0),
                _n("plan", "process_plan", 840, 200),
                _n("process_review", "human_review", 1120, 200, {"stage": "process"}),
                _n("quote", "quote", 1400, 200),
                _n("quote_review", "human_review", 1680, 200, {"stage": "quote"}),
                _n("export", "export", 1960, 100),
            ],
            "edges": [
                _e("request", "text", "model", "text"),
                _e("model", "model", "design_review", "subject"),
                _e("design_review", "subject", "drawing", "model"),
                _e("design_review", "subject", "plan", "model"),
                _e("plan", "plan", "process_review", "subject"),
                _e("process_review", "subject", "quote", "plan"),
                _e("quote", "quote", "quote_review", "subject"),
                _e("drawing", "drawing", "export", "items"),
                _e("quote_review", "subject", "export", "items"),
            ],
            "groups": [
                {"id": "g_design", "label": "Design", "node_ids": ["request", "model", "design_review", "drawing"]},
                {"id": "g_mfg", "label": "Manufacturing", "node_ids": ["plan", "process_review", "quote", "quote_review"]},
            ],
        },
    },
    "drawing_to_quote": {
        "name": {"en": "Customer drawing to quote", "zh": "客户图纸到报价"},
        "description": {
            "en": "Organise a customer drawing into manufacturing data, check it by hand, then plan and quote. Volume and blank data are estimates.",
            "zh": "把客户图纸整理成制造信息并人工核对，再规划工艺并报价；体积与毛坯为估算。",
        },
        "graph": {
            "nodes": [
                _n("drawing_file", "file_input", 0, 120),
                _n("intake", "drawing_intake", 280, 120),
                _n("intake_review", "human_review", 560, 120, {"stage": "design", "instructions": "Verify material, size, holes and tolerances against the drawing."}),
                _n("plan", "process_plan", 840, 120),
                _n("process_review", "human_review", 1120, 120, {"stage": "process"}),
                _n("quote", "quote", 1400, 120),
                _n("quote_review", "human_review", 1680, 120, {"stage": "quote"}),
            ],
            "edges": [
                _e("drawing_file", "file", "intake", "file"),
                _e("intake", "spec", "intake_review", "subject"),
                _e("intake_review", "subject", "plan", "spec"),
                _e("plan", "plan", "process_review", "subject"),
                _e("process_review", "subject", "quote", "plan"),
                _e("quote", "quote", "quote_review", "subject"),
            ],
            "groups": [],
        },
    },
    # AI work panel (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §2.5): the base of a workflow_create for a DXF / DWG.
    "drawing_model_to_quote": {
        "name": {"en": "Customer drawing to model and quote", "zh": "客户图纸建模到报价"},
        "description": {
            "en": "Reconstruct a 3D model from a customer DXF / DWG drawing, review the design, then plan the process and quote.",
            "zh": "从客户 DXF / DWG 图纸重建三维模型，设计审核后规划工艺并报价。",
        },
        "graph": {
            "nodes": [
                _n("drawing_file", "file_input", 0, 120),
                _n("reconstruct", "drawing_reconstruct", 280, 120),
                _n("design_review", "human_review", 560, 120, {"stage": "design", "instructions": "Compare the model with the drawing: size, holes and tolerances."}),
                _n("plan", "process_plan", 840, 120),
                _n("process_review", "human_review", 1120, 120, {"stage": "process"}),
                _n("quote", "quote", 1400, 120),
                _n("quote_review", "human_review", 1680, 120, {"stage": "quote"}),
            ],
            "edges": [
                _e("drawing_file", "file", "reconstruct", "file"),
                _e("reconstruct", "model", "design_review", "subject"),
                _e("design_review", "subject", "plan", "model"),
                _e("plan", "plan", "process_review", "subject"),
                _e("process_review", "subject", "quote", "plan"),
                _e("quote", "quote", "quote_review", "subject"),
            ],
            "groups": [],
        },
    },
    "design_change": {
        "name": {"en": "Design change and recalculation", "zh": "设计变更与重算"},
        "description": {
            "en": "Apply a change request to an existing revision with AI, review it, and recalculate drawings, process and quote.",
            "zh": "用 AI 把变更要求应用到已有修订，审核后重新生成图纸、工艺与报价。",
        },
        "graph": {
            "nodes": [
                _n("base", "model_input", 0, 60),
                _n("change", "text_input", 0, 220, {"text": ""}),
                _n("model", "model_3d", 280, 120),
                _n("design_review", "human_review", 560, 120, {"stage": "design", "instructions": "Compare with the previous revision."}),
                _n("drawing", "drawing", 840, 0),
                _n("plan", "process_plan", 840, 200),
                # A formal quote needs a confirmed process plan (status.py), so the
                # change template must include the process review as well.
                _n("process_review", "human_review", 1120, 200, {"stage": "process"}),
                _n("quote", "quote", 1400, 200),
                _n("quote_review", "human_review", 1680, 200, {"stage": "quote"}),
            ],
            "edges": [
                _e("base", "model", "model", "model"),
                _e("change", "text", "model", "text"),
                _e("model", "model", "design_review", "subject"),
                _e("design_review", "subject", "drawing", "model"),
                _e("design_review", "subject", "plan", "model"),
                _e("plan", "plan", "process_review", "subject"),
                _e("process_review", "subject", "quote", "plan"),
                _e("quote", "quote", "quote_review", "subject"),
            ],
            "groups": [],
        },
    },
    # W8: the general mechanical engineering chain (project-level; no part).
    "spec_to_delivery": {
        "name": {"en": "Engineering description to delivery package", "zh": "工程描述到交付包"},
        "description": {
            "en": "Read the description (and connected files) into an engineering spec, build every part and the static assembly, "
                  "draw the assembly, each sub-assembly and each part, and deliver STEP, BOM and drawings of every level with a "
                  "hashed manifest.",
            "zh": "把工程描述（及连接的文件）整理成工程规格，逐个建零件并组成静态装配，出总装图、各子装配图和零件图，"
                  "分层交付带哈希清单的 STEP、BOM 与图纸。",
        },
        "graph": {
            "nodes": [
                _n("request", "text_input", 0, 120, {"text": ""}),
                _n("spec", "engineering_spec", 280, 120),
                _n("assembly", "assembly_from_spec", 560, 120),
                # generation supplement E: every level drawn and delivered (sub-assembly packages, every part's detail sheet)
                _n("drawings", "assembly_drawing", 840, 0, {"subassemblies": True}),
                _n("delivery", "delivery_package", 1120, 120),
            ],
            "edges": [
                _e("request", "text", "spec", "text"),
                _e("spec", "spec", "assembly", "spec"),
                _e("assembly", "assembly", "drawings", "assembly"),
                _e("assembly", "assembly", "delivery", "assembly"),
                _e("drawings", "drawings", "delivery", "drawings"),
            ],
            "groups": [],
        },
    },
}

# Templates must always be structurally valid; empty inputs are filled by the user.
for _key, _tpl in TEMPLATES.items():
    _, _errs = validate_graph(_tpl["graph"])
    _structural = [e for e in _errs if e["code"] not in ("missing_param",)]
    assert not _structural, (_key, _structural)


def template_list() -> list[dict]:
    return [{"key": k, "name": t["name"], "description": t["description"], "graph": copy.deepcopy(t["graph"])} for k, t in TEMPLATES.items()]


def template_graph(key: str) -> dict:
    return copy.deepcopy(TEMPLATES[key]["graph"])
