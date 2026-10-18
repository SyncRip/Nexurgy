"""Authorized design choices become an immutable derived spec, never extracted human evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import math
import re
from typing import Any
import uuid

from ..errors import AppError
from ..models import AiMessage, AiOperation, EngineeringSpecVersion
from .. import policy as pol


def explicit_authorization(text: str) -> bool:
    """Conservative permission from original human text only; a tool flag alone grants nothing."""
    permit = r"自行设计|自主设计|由你.{0,12}(?:设计|选定)|(?:缺少|缺失|未知).{0,18}(?:合理选定|自行确定)|design\s+(?:it\s+)?yourself|choose\s+(?:the\s+)?missing\s+(?:design\s+)?(?:parameters|dimensions)|autonomously\s+design"
    deny = r"不要|禁止|不得|不允许|不能|do\s+not|don't|never"
    revoke = r"(?:不要|禁止|不得|不允许|不能).{0,12}(?:猜测|假设|补充|自行|自主|选定)|(?:缺尺寸|缺少参数).{0,12}(?:先问|确认)|(?:do not|don't|never).{0,16}(?:guess|assume|invent|choose)"
    result = False
    for clause in re.split(r"[。；;\n.!?！？]", text.lower()):
        if re.search(revoke, clause, re.I):
            result = False
            continue
        if re.search(permit, clause, re.I):
            result = not bool(re.search(deny, clause, re.I))
    return result


def operation_authorization(db, op: AiOperation) -> dict[str, Any] | None:
    req = op.request or {}
    if not (req.get("tool_input") or {}).get("design_from_requirements"):
        return None
    source = req.get("human_source") or {}
    segments = [{k: source.get(k) for k in ("operation_id", "message_id", "text")}, *(source.get("amendments") or [])]
    pinned = []
    for segment in segments:
        try:
            root = db.get(AiOperation, uuid.UUID(str(segment.get("operation_id"))))
            message = db.get(AiMessage, uuid.UUID(str(segment.get("message_id"))))
        except (ValueError, TypeError):
            raise AppError(422, "design_authorization_missing", "Design requires an exact original human request")
        if (root is None or message is None or root.user_id != op.user_id or root.project_id != op.project_id
                or root.conversation_id != op.conversation_id or message.conversation_id != op.conversation_id
                or root.message_id != message.id or message.role != "user"
                or str((root.request or {}).get("text") or "") != str(segment.get("text") or "")):
            raise AppError(422, "design_authorization_missing", "Design source is not the original human request")
        pinned.append({"operation_id": str(root.id), "message_id": str(message.id), "text": message.text or str(segment.get("text") or "")})
    text = "\n\n".join(s["text"] for s in pinned)
    if not explicit_authorization(text):
        raise AppError(422, "design_authorization_missing", "The human request does not explicitly delegate engineering design")
    return {"authorized": True, "operation_id": str(op.id), "sources": pinned, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


def compose_design(original: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Validate new structure and retain extracted data byte-for-byte. All added values are AI choices."""
    from ..ai.engineering_design_agent import DesignReply
    from .engineering_spec import structure_problems

    try:
        json.dumps(proposal, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AppError(422, "design_invalid", "Design values must be finite JSON values") from exc
    reply = DesignReply.model_validate(proposal)
    design = reply.spec.model_dump(mode="json")
    problems: list[str] = []

    def stated(field: str, value: Any) -> bool:
        # Empty extraction placeholders are missing inputs, not facts that prevent authorized design.
        return value not in (None, "", [], {}) and not (field == "procurement" and value == "unknown")

    for field in ("skeleton", "unbound"):
        if design[field] and design[field] != (original.get(field) or []):
            problems.append(f"{field}: only original source extraction records are allowed")
    if design["conflicts"]:
        problems.append("Do not invent source conflict readings; describe new design inconsistencies as questions")
    if original.get("root") and design["root"] != original["root"]:
        problems.append("Keep the extracted root key")
    defs = {d["key"]: d for d in design["definitions"]}
    if not design["root"] or not design["instances"] or not any(d["kind"] != "assembly" for d in defs.values()):
        problems.append("A design needs a root assembly and actual part instances")
    used = {i["definition"] for i in design["instances"]}
    if any(k != design["root"] and k not in used for k in defs):
        problems.append("Every non-root definition must have an instance")
    for instance in design["instances"]:
        pl = instance.get("placement")
        if not pl or not all(math.isfinite(n) for n in [*pl["translate"], *pl["rotate"]]):
            problems.append(f"{instance['key']}: an explicit finite local placement is required")
    for a in design["assumptions"]:
        if not a["basis"].strip() or a["adopted"] is None:
            problems.append(f"{a['id']}: design assumptions require an adopted value and a concrete basis")
    for d in design["definitions"]:
        if d.get("template"):
            from ..cad import templates

            try:
                templates.instantiate(d["template"]["id"], d["template"]["params"])
            except (ValueError, AppError) as exc:
                problems.append(f"{d['key']}: invalid template parameters: {exc}")
    old_facts = {f["id"] for f in original.get("facts") or []}
    old_subjects = {f["subject"] for f in original.get("facts") or [] if f.get("status") in ("asserted", "derived")}
    objects = set(defs) | {i["key"] for i in design["instances"]} | {r["key"] for r in design["relations"]}
    for a in design["assumptions"]:
        if a["subject"] in old_subjects:
            problems.append(f"{a['id']}: do not replace an extracted fact with an assumption")
        if a["subject"].split(".", 1)[0] not in objects or any(k not in objects for k in a["affected"]):
            problems.append(f"{a['id']}: assumption must refer to known design objects")
        if a["sources"]:
            problems.append(f"{a['id']}: do not invent source citations for design assumptions")
    for n in design["manufacturing_notes"]:
        if n["evidence"]:
            problems.append(f"{n['id']}: AI design notes cannot cite invented source evidence")
    for i in design["interfaces"]:
        if i["sources"]:
            problems.append(f"{i['key']}: AI design interfaces cannot cite invented source evidence")
    for f in design["facts"]:
        if f["id"] in old_facts or f["subject"] in old_subjects:
            problems.append(f"{f['id']}: do not overwrite or restate an extracted fact")
        if f["evidence"]:
            problems.append(f"{f['id']}: AI design choices cannot cite invented source evidence")
    for collection in ("definitions", "instances", "relations"):
        supplied = {x["key"]: x for x in design[collection]}
        for old in original.get(collection) or []:
            if old["key"] not in supplied:
                problems.append(f"Keep original {collection} key {old['key']}")
            elif any(supplied[old["key"]].get(k) != v for k, v in old.items() if stated(k, v) and k in supplied[old["key"]]):
                problems.append(f"Do not change extracted {collection} {old['key']}")
    if problems:
        raise AppError(422, "design_invalid", "Design is incomplete or contradicts its source", {"problems": problems[:40]})
    content = deepcopy(design)
    for collection in ("definitions", "instances", "relations"):
        old = {x["key"]: x for x in original.get(collection) or []}
        content[collection] = [{**item, **deepcopy({k: v for k, v in old[item["key"]].items() if stated(k, v)})}
                               if item["key"] in old else item for item in content[collection]]
    now = datetime.now(UTC).isoformat()
    assumptions = []
    occupied = {a["id"] for a in original.get("assumptions") or []}
    for a in content["assumptions"]:
        if a["id"] in occupied:
            raise AppError(422, "design_invalid", "A design reused an existing assumption id")
        occupied.add(a["id"])
        assumptions.append(pol.Assumption(**a, author="ai_design", at=now).model_dump(mode="json"))

    def decision(subject: str, value: Any, basis: str, unit: str | None = None) -> str:
        i = 1000
        while f"A-{i}" in occupied:
            i += 1
        aid = f"A-{i}"
        occupied.add(aid)
        assumptions.append(pol.Assumption(id=aid, subject=subject, field=subject.split(".", 1)[-1], adopted=value, unit=unit,
                                         basis=basis, affected=[subject.split(".", 1)[0]], author="ai_design", at=now,
                                         trigger="missing").model_dump(mode="json"))
        return aid

    for f in content["facts"]:
        f["status"], f["evidence"] = "assumed", []
        matching = next((a for a in assumptions if a["subject"] == f["subject"] and a["adopted"] == f["value"]), None)
        f["derivation"] = matching["id"] if matching else decision(f["subject"], f["value"], f.get("derivation") or reply.rationale, f.get("unit"))
    original_defs = {d["key"]: d for d in original.get("definitions") or []}
    for d in content["definitions"]:
        previous = original_defs.get(d["key"], {})
        choices = {k: v for k, v in d.items() if k != "key" and stated(k, v) and previous.get(k) != v}
        if choices:
            decision(d["key"] + ".design", choices, reply.rationale)
    original_instances = {i["key"]: i for i in original.get("instances") or []}
    for i in content["instances"]:
        previous = original_instances.get(i["key"], {})
        if i["placement"] != previous.get("placement"):
            decision(i["key"] + ".placement", i["placement"], "AI-selected assembly placement in the explicitly documented local datum frames")
        if previous and i["parent"] != previous.get("parent"):
            decision(i["key"] + ".parent", i["parent"], "AI-selected parent for the previously unplaced instance")
    original_relations = {r["key"]: r for r in original.get("relations") or []}
    for r in content["relations"]:
        previous = original_relations.get(r["key"], {})
        if any(stated(k, v) and previous.get(k) != v for k, v in r.items() if k != "key"):
            decision(r["key"] + ".relation", r, "AI-selected assembly relationship between the documented datum frames")
    for n in content["manufacturing_notes"]:
        decision((n["subject"] or content["root"]) + ".note_" + n["id"], n["text"], reply.rationale)
    interface_keys = {i["key"] for i in original.get("interfaces") or []}
    for i in content["interfaces"]:
        if i["key"] in interface_keys:
            raise AppError(422, "design_invalid", "Do not restate original interfaces")
        i["status"], i["how"] = "derived", "AI engineering design choice; unconfirmed"
        decision(i["owner"] + ".interface_" + i["key"], i, "AI-selected local assembly datum")
    content["interfaces"] = deepcopy(original.get("interfaces") or []) + content["interfaces"]
    content["facts"] = deepcopy(original.get("facts") or []) + content["facts"]
    content["assumptions"] = deepcopy(original.get("assumptions") or []) + assumptions
    for coll in ("questions", "conflicts", "manufacturing_notes"):
        ids = {x["id"] for x in original.get(coll) or []}
        if any(x["id"] in ids for x in content[coll]):
            raise AppError(422, "design_invalid", f"Do not reuse original {coll} ids")
        content[coll] = deepcopy(original.get(coll) or []) + content[coll]
    content["sources"] = deepcopy(original.get("sources") or [])
    for field in ("skeleton", "unbound"):
        content[field] = deepcopy(original.get(field) or [])
    deliverables = {d["kind"]: d for d in content["deliverables"]}
    for d in original.get("deliverables") or []:
        if d.get("required", True) or d["kind"] not in deliverables:
            deliverables[d["kind"]] = deepcopy(d)
    content["deliverables"] = list(deliverables.values())
    # Original facts/interfaces are omitted from the model's additions by contract. Validate references only
    # after restoring them, including the exact known object fields the server retained above.
    if problems := structure_problems(content):
        raise AppError(422, "design_invalid", "Design is incomplete or contradicts its source", {"problems": problems[:40]})
    return content


def synthesize(db, parent: EngineeringSpecVersion, *, op: AiOperation, model_ref: Any, node_run_id: uuid.UUID) -> EngineeringSpecVersion:
    from ..ai import engineering_design_agent, retry
    from ..ai.providers.base import ProviderError
    from . import ai as ai_svc, engineering_spec as specs, orchestration
    from ..security import project_access
    from ..models import User

    authorization = operation_authorization(db, op)
    if authorization is None:
        return parent
    try:
        provider, cfg = ai_svc.provider_for(db, "planning", model_ref)
    except ProviderError as exc:
        raise AppError(422, exc.code, exc.message) from exc
    from .assistant_context import _budget

    _, _, _, available, _ = _budget(cfg)
    db.commit()  # input/source version durable before provider I/O
    outcome = engineering_design_agent.run_design(provider, authorization["text"], parent.content or {}, available_tokens=available)
    ids = specs._record_calls(db, outcome.calls, ai_operation_id=op.id)
    orchestration.attach_spec_calls(db, parent, node_run_id=node_run_id, ids=ids)
    if outcome.error:
        retryable = bool(outcome.error.get("retryable"))
        raise AppError(422, outcome.error["code"], outcome.error["message"],
                       {**(outcome.error.get("details") or {}), "call_ids": ids, "retryable": retryable,
                        **({"recovery": "retry_design"} if retryable else {})})
    active_limits = retry.current_limits()
    if active_limits and (stop := active_limits.stop_reason(calls=False)):
        raise AppError(409, stop.code, stop.message, {"call_ids": ids})
    project_access(db, db.get(User, op.user_id), op.project_id, "design")
    db.refresh(op)
    if op.status == "cancelled":
        raise AppError(409, "cancelled", "The design request was cancelled", {"call_ids": ids})
    report = {**authorization, **outcome.report, "prompt_version": engineering_design_agent.PROMPT_VERSION,
              "model": {"provider": provider.provider_name, "model_id": provider.model_id}, "ai_call_ids": ids,
              "assumption_ids": [a["id"] for a in outcome.content["assumptions"] if a.get("author") == "ai_design"],
              "status": "unconfirmed", "simulated": provider.simulated}
    return specs.derive_rule_version(db, parent, content=outcome.content, created_by=op.user_id,
                                     note={"rule": "authorized_engineering_design", "authorization_sha256": authorization["sha256"]},
                                     ai_call_ids=ids, simulated=provider.simulated, planning_extra={"design": report})


def report_files(report: dict[str, Any]) -> dict[str, bytes]:
    lines = ["# AI engineering design basis — unconfirmed", report.get("rationale", ""), "", "## Motion", report.get("motion", ""),
             "", "## Calculations (analytical estimates, not independent validation)"]
    for c in report.get("calculations") or []:
        lines += [f"### {c['name']}", f"Formula: {c['formula']}", f"Substitution: {c['substitution']}", f"Result: {c['result']}", f"Limits: {c['limitations']}"]
    lines += ["", "## Not yet verified", *["- " + x for x in report.get("unverified") or []], "", "## Original human requirements", report.get("text", "")]
    return {"report/design_basis.json": json.dumps(report, ensure_ascii=False, indent=2).encode(),
            "report/design_basis.md": "\n".join(lines).encode()}
