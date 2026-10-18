"""Repeated image declarations are review candidates, never automatic component aliases."""
from __future__ import annotations

import copy
import math
from collections.abc import Callable
from typing import Any

EvidenceLocator = Callable[[dict[str, Any]], tuple[dict[str, Any], str | None]]


def _correspondence(decision: dict[str, Any]) -> list[dict[str, Any]]:
    return (decision.get("visual_identity_evidence") or {}).get("dimension_correspondence", [])


def dimension_problem(content: dict[str, Any], decision: dict[str, Any]) -> str | None:
    """Validate explicit aliases again at application; a validated identity triple cannot authorize new mappings."""
    from .spec_merge import norm_unit
    mappings = _correspondence(decision)
    if not isinstance(mappings, list) or len(mappings) > 8:
        return "Dimension correspondence must be a bounded explicit list"
    refs = list(decision.get("evidence", []))
    for mapping in mappings:
        if isinstance(mapping, dict):
            for role in ("child_evidence", "target_evidence"):
                if mapping.get(role) not in refs:
                    refs.append(mapping.get(role))
    if len(refs) > 20:
        return "Dimension correspondence exceeds retained review evidence capacity"
    seen: dict[str, set[str]] = {"child": set(), "target": set()}
    for mapping in mappings:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("basis"), str) or not mapping["basis"].strip():
            return "Each dimension correspondence requires its physical-axis rationale"
        pair = []
        for role in ("child", "target"):
            facts = [f for f in content.get("facts", []) if f.get("id") == mapping.get(role + "_fact")]
            if len(facts) != 1:
                return "Dimension correspondence must identify unique existing fact IDs"
            fact = facts[0]
            subject = str(fact.get("subject", ""))
            if (subject.partition(".")[0] != decision.get(role) or not subject.partition(".")[2]
                    or fact.get("kind") != "dimension" or fact.get("status") not in ("asserted", "derived")):
                return "Dimension correspondence requires asserted/derived dimensions on the selected child and target"
            if subject in seen[role] or sum(f.get("subject") == subject for f in content.get("facts", [])) != 1:
                return "Dimension correspondence must be one-to-one with unambiguous fact subjects"
            seen[role].add(subject)
            value = fact.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                return "Dimension correspondence requires finite numeric original values"
            cited = mapping.get(role + "_evidence")
            if (not isinstance(cited, dict) or not any(cited.get(k) for k in ("entity", "quote", "region"))
                    or not any(all(e.get(k) == cited.get(k) for k in ("source", "chunk", "entity", "quote", "region"))
                               for e in fact.get("evidence", []))):
                return "Dimension correspondence must cite the exact original evidence of each named fact"
            pair.append(fact)
        a, b = pair
        if a["value"] != b["value"] or not norm_unit(a.get("unit")) or norm_unit(a.get("unit")) != norm_unit(b.get("unit")):
            return "Dimension correspondence cannot change values or convert incompatible/unknown units"
    return None


def _stage_dimensions(content: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply explicit aliases simultaneously, preserving the target reading and complete originals for audit."""
    facts = {f["id"]: f for f in content.get("facts", [])}
    audit = []
    subjects = {}
    for mapping in _correspondence(decision):
        child, target = (facts[mapping[k + "_fact"]] for k in ("child", "target"))
        audit.append({**copy.deepcopy(mapping), "child_original": copy.deepcopy(child), "target_original": copy.deepcopy(target),
                      "mapped_subject": target["subject"], "unconfirmed": True})
        subjects[child["subject"]] = decision["child"] + "." + target["subject"].partition(".")[2]
    for item in audit:
        child, target = (facts[item[k + "_fact"]] for k in ("child", "target"))
        child.update(subject=subjects[item["child_original"]["subject"]], value=target["value"], unit=target.get("unit"))
    for coll in ("assumptions", "questions", "conflicts", "manufacturing_notes"):
        for item in content.get(coll, []):
            if item.get("subject") in subjects:
                item["subject"] = subjects[item["subject"]]
    return audit


def family(evidence: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(evidence.get(k) for k in ("source", "file_version_id", "source_sha256", "frame_id", "image_sha256"))


def declarations(content: dict[str, Any], sheets: list[dict[str, Any]], titled: set[str],
                 locate: EvidenceLocator) -> list[dict[str, Any]]:
    out = []
    for definition in content.get("definitions", []):
        key = definition["key"]
        if definition.get("kind") != "part" or definition.get("number") or key in titled:
            continue
        uses = [i for i in content.get("instances", []) if i["definition"] == key]
        parents = {i.get("parent") or content.get("root") for i in uses}
        if len(parents) != 1 or any(i.get("parent") == key for i in content.get("instances", [])):
            continue
        parent = next(iter(parents))
        facts = [f for f in content.get("facts", []) if f.get("subject") == key + ".visual_presence"]
        for fact in facts:
            for original in fact.get("evidence", []):
                evidence, problem = locate(original)
                if (problem or not evidence.get("located") or not evidence.get("image_sha256")
                        or not evidence.get("region") or not evidence.get("frame_id")):
                    continue
                sheet = next((s for s in sheets if s["source"] == evidence["source"]
                              and s["frame"] == evidence["frame_id"] and s["image_sha256"] == evidence["image_sha256"]
                              and parent in s["assembly_definitions"] and evidence["chunk"] in s["chunks"]), None)
                if sheet is None:
                    continue
                out.append({"child": key, "parent": parent, "quantity": len(uses), "fact_id": fact["id"],
                            "evidence": evidence, "name": definition.get("name"),
                            "facts": copy.deepcopy([f for f in content.get("facts", [])
                                if str(f.get("subject", "")).partition(".")[0] == key
                                or f.get("subject") == f"{parent}.qty[{key}]"])})
    return out


def candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        targets = sorted({other["child"] for other in rows if row["parent"] == other["parent"]
                          and row["child"] != other["child"] and family(row["evidence"]) == family(other["evidence"])
                          and row["evidence"]["chunk"] != other["evidence"]["chunk"]})
        if targets:
            item = grouped.setdefault((row["parent"], row["child"]), {
                "kind": "visual_identity", "parent": row["parent"], "child": row["child"], "targets": [],
                "warning": "Repeated original frame only: inspect whether declarations identify one physical component or distinct parts; do not merge by overlap/name."})
            item["targets"] = sorted(set(item["targets"]) | set(targets))
    return list(grouped.values())


def proof_problem(decision: dict[str, Any], rows: list[dict[str, Any]], locate: EvidenceLocator) -> str | None:
    proof = decision.get("visual_identity_evidence")
    if not proof or proof.get("same_physical_component") is not True:
        return "Visual identity requires an explicit same-physical-component assertion and both original declarations"
    selected = []
    for role, key in (("child_declaration", decision["child"]), ("target_declaration", decision.get("target"))):
        cited = proof[role]
        row = next((r for r in rows if r["child"] == key and r["parent"] == decision.get("parent")
                    and all(r["evidence"].get(k) == cited.get(k) for k in ("source", "chunk", "region"))), None)
        if row is None:
            return "Visual identity must cite each exact original declaration on its actual parent's attached sheet"
        selected.append(row)
    a, b = selected
    if a["child"] == b["child"] or a["evidence"]["chunk"] == b["evidence"]["chunk"] or family(a["evidence"]) != family(b["evidence"]):
        return "Visual duplicates must be separate fragment declarations on the exact same original source/frame/image"
    same, problem = locate(proof["same_component"])
    if problem or not same.get("located") or not same.get("region") or family(same) != family(a["evidence"]):
        return "Same-component evidence must locate a valid region on that exact attached original image"
    content = {"facts": list({f["id"]: f for row in selected for f in row["facts"]}.values())}
    problem = dimension_problem(content, decision)
    if problem:
        return problem
    for mapping in _correspondence(decision):
        for role, row in (("child", a), ("target", b)):
            cited = mapping[role + "_evidence"]
            stored, problem = locate(cited)
            if problem or not (stored.get("located") or stored.get("verified")):
                return "Dimension correspondence evidence must locate each original fact"
            # Entity/quote locators need not carry image metadata. Bind their original chunk to
            # the attached declaration's sheet through the same server-owned image locator.
            image, problem = locate({**cited, "entity": None, "quote": None,
                                     "region": cited.get("region") or row["evidence"]["region"]})
            if problem or not image.get("located") or family(image) != family(row["evidence"]):
                return "Dimension correspondence must cite the same attached original body sheet"
    return None


def merge_problem(content: dict[str, Any], decision: dict[str, Any]) -> str | None:
    """No count selection, geometric override, implicit evidence clipping or anchored instance deletion."""
    parent, child, target = (decision.get(k) for k in ("parent", "child", "target"))
    if not all(isinstance(k, str) for k in (parent, child, target)):
        return "Visual duplicate endpoints must be existing definition keys"
    assert isinstance(parent, str) and isinstance(child, str) and isinstance(target, str)
    definitions = {d["key"]: d for d in content.get("definitions", [])}
    if child == target or any(k not in definitions for k in (parent, child, target)):
        return "Visual duplicate endpoints must be three distinct existing definitions"
    if definitions[parent].get("kind") != "assembly" or any(definitions[k].get("kind") != "part" or definitions[k].get("number") for k in (child, target)):
        return "Only numberless visual leaf parts under one actual assembly may be deduplicated"
    if any(definitions[child].get(field) is not None and definitions[target].get(field) is not None
           and definitions[child][field] != definitions[target][field] for field in ("size", "material", "template")):
        return "Conflicting explicit component size/material/template identities must be reconciled before visual consolidation"
    uses = {k: [i for i in content.get("instances", []) if i["definition"] == k] for k in (child, target)}
    if (not uses[child] or not uses[target] or len(uses[child]) != len(uses[target])
            or any((i.get("parent") or content.get("root")) != parent for group in uses.values() for i in group)
            or any(i.get("parent") in (child, target) for i in content.get("instances", []))):
        return "Visual duplicates require equal existing local quantities under the same sole parent; reconcile conflicting counts explicitly"
    gone = {i["key"] for i in uses[child]}
    if (any(i.get("placement") is not None for i in uses[child])
            or any(r.get(end, {}).get("instance") in gone for r in content.get("relations", []) for end in ("a", "b"))
            or any(str(f.get("subject", "")).partition(".")[0] in gone for f in content.get("facts", []))
            or any(str(n.get("subject", "")).partition(".")[0] in gone for n in content.get("manufacturing_notes", []))
            or any(gone.intersection(x.get(field, [])) for coll, field in (("assumptions", "affected"), ("questions", "affects"))
                   for x in content.get(coll, []))):
        return "Visual deduplication cannot remove a source-positioned or referenced instance"
    from .spec_aliases import _rekey
    problem = dimension_problem(content, decision)
    if problem:
        return problem
    if any(not x.get("resolution") and (str(x.get("subject", "")).startswith((child + ".", target + "."))
               or x.get("subject") in (f"{parent}.qty[{child}]", f"{parent}.qty[{target}]")) for x in content.get("conflicts", [])):
        return "Unresolved visual declaration conflicts must remain explicit before identity consolidation"
    content = copy.deepcopy(content)
    _stage_dimensions(content, decision)
    mapped_subjects = [f["subject"] for f in content.get("facts", [])]
    if len(mapped_subjects) != len(set(mapped_subjects)):
        return "Dimension correspondence leaves ambiguous duplicate subjects"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in content.get("facts", []):
        subject = str(fact.get("subject", ""))
        if subject.startswith((child + ".", target + ".")) or subject in (f"{parent}.qty[{child}]", f"{parent}.qty[{target}]"):
            grouped.setdefault(_rekey(subject, child, target), []).append(fact)
    for facts in grouped.values():
        values = {(repr(f.get("value")), f.get("unit")) for f in facts if f.get("value") is not None and f.get("status") != "unknown"}
        if len(values) > 1:
            return "Visual declarations have conflicting fact/count readings; preserve and explicitly resolve their alternatives before merging"
        evidence = [e for f in facts for e in f.get("evidence", [])]
        if len(evidence) > 20:
            return "Visual merge exceeds retained evidence capacity; no original reading may be truncated"
    return None


def apply(content: dict[str, Any], decision: dict[str, Any], assumption_id: str, at: str) -> dict[str, Any]:
    """Called only after proof/graph guards. Archive originals before the guarded legacy fact rekey helper."""
    from .spec_aliases import _merge, _rekey
    problem = dimension_problem(content, decision)
    if problem:
        raise ValueError(problem)
    dimension_audit = _stage_dimensions(content, decision)
    for mapping in dimension_audit:
        mapping["assumption"] = assumption_id
    parent, child, target = (decision[k] for k in ("parent", "child", "target"))
    definitions = {d["key"]: d for d in content["definitions"]}
    alias_name, into_name = definitions[child].get("name"), definitions[target].get("name")
    by_subject = {f["subject"]: f["id"] for f in content["facts"]}
    renamed = {f["id"]: by_subject[_rekey(f["subject"], child, target)] for f in content["facts"]
               if _rekey(f["subject"], child, target) != f["subject"] and _rekey(f["subject"], child, target) in by_subject}
    original_evidence: dict[str, list[dict[str, Any]]] = {}
    for f in content["facts"]:
        if f["subject"].startswith((child + ".", target + ".")) or f["subject"] in (f"{parent}.qty[{child}]", f"{parent}.qty[{target}]"):
            dest = original_evidence.setdefault(_rekey(f["subject"], child, target), [])
            dest.extend(copy.deepcopy(f.get("evidence", [])))
    if definitions[target].get("size") is None and definitions[child].get("size") is not None:
        definitions[target]["size"] = copy.deepcopy(definitions[child]["size"])
    removed = [i for i in content["instances"] if i["definition"] == child]
    content["instances"] = [i for i in content["instances"] if i["definition"] != child]
    merged = _merge(content, definitions, child, target, assumption_id,
                    {a["id"] for a in content["assumptions"]} | {assumption_id}, {c["id"] for c in content["conflicts"]}, at)
    for f in content["facts"]:
        if f["subject"] in original_evidence:
            f["evidence"] = original_evidence[f["subject"]]
    for collection in ("definitions", "instances", "relations", "interfaces", "manufacturing_notes", "assumptions", "questions", "conflicts"):
        for item in content.get(collection, []):
            if item.get("subject") in renamed:
                item["subject"] = renamed[item["subject"]]
            for field in ("fact_ids", "affected", "affects"):
                if field in item:
                    item[field] = list(dict.fromkeys(renamed.get(key, key) for key in item[field]))
    # Definition-level scalar subjects as well as quantity brackets retain the complete old reading in prior.
    for item in content.get("manufacturing_notes", []):
        if item.get("subject"):
            item["subject"] = _rekey(item["subject"], child, target)
    content["assumptions"].append({"id": assumption_id, "subject": target, "field": "identity", "unit": None,
        "trigger": "ambiguous", "adopted": {"duplicate_visual": child, "target": target, "parent": parent,
            "quantity": sum(i["definition"] == target and (i.get("parent") or content.get("root")) == parent for i in content["instances"]),
            "dimension_correspondence": dimension_audit},
        "basis": decision["basis"], "alternatives": [f"{child} is a separate physical component"],
        "affected": [parent, target], "sources": [f"{e['source']} · chunk {e['chunk']} · region {e.get('region')}"
            for e in decision["evidence"]]})
    return {"action": "duplicate_visual", "alias": child, "alias_name": alias_name, "into": target,
            "into_name": into_name, "parent": parent, "decision": assumption_id,
            "assumption": assumption_id, "basis": decision["basis"], "evidence": copy.deepcopy(decision["evidence"]),
            "proof": copy.deepcopy(decision["visual_identity_evidence"]), "removed_instances": copy.deepcopy(removed),
            "sources": list(content["assumptions"][-1]["sources"]), "fact_id_map": renamed,
            "dimension_correspondence": copy.deepcopy(dimension_audit),
            "prior_ref": "visual_identity_prior", **merged}
