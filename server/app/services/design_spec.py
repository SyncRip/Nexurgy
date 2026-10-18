"""Compile authorized AI design data without relabeling it as measured source evidence.

The plan is validated before storage and resolved again at every engineering boundary. Ordinary
source extraction never consumes this authority. Geometry still passes the normal independent STEP checks.
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import product
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import policy as pol
from ..ai.design_contracts import DesignPlanContent
from ..ai.spec_agent import SpecReply
from ..cad.requirements import DesignDefinitionContext, gate, requirements_sha256
from ..errors import AppError
from ..models import AiDesignPlanVersion, EngineeringSpec, EngineeringSpecVersion, User, WorkflowDesignPlanVersion
from ..security import ProjectAccess
from .assistant_tool_inputs import ResolvedDesignPlan

COMPILER_VERSION = "design-spec-1"


def _fail(message: str, *, code: str = "design_plan_invalid", **details: Any) -> None:
    raise AppError(422, code, message, details or None)


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(v) for v in value)
    return True


def semantic_anchor(content: dict[str, Any], path: list[str | int]) -> dict[str, str]:
    """Stable owner of a bounded editable field; array position alone is not an identity."""
    try:
        head = path[0]
        if head == "part_requirements":
            return {"definition_key": str(path[1]), "requirement_id": content[head][path[1]][path[2]]["id"]}
        if head in {"quantities", "assembly_envelope"}:
            return {"definition_key": str(path[1])}
        item = content[head][path[1]]
        if head == "definitions":
            return {"definition_key": item["key"]}
        if head == "instances":
            return {"instance_key": item["key"], "definition_key": item["definition"]}
        key = {"relations": "relation_key", "facts": "fact_id", "interfaces": "interface_key"}.get(str(head))
        return {key: item["id" if head == "facts" else "key"]} if key else {}
    except (IndexError, KeyError, TypeError):
        return {}


def _path_value(content: dict[str, Any], path: list[str | int], quantities: Counter[str]) -> Any:
    try:
        if len(path) == 2 and path[0] == "quantities":
            return quantities[str(path[1])]
        if len(path) == 4 and path[0] == "assembly_envelope" and path[2] == "size" and path[3] in (0, 1, 2):
            low, high = _design_bounds(content, str(path[1]))
            return high[path[3]] - low[path[3]]
        value: Any = content
        for key in path:
            if isinstance(key, bool) or isinstance(key, int) and key < 0:
                raise KeyError(key)
            value = value[key]
        return value
    except (KeyError, IndexError, TypeError):
        _fail("design constraint field is missing", code="design_constraint_invalid", field_path=path)


def _design_bounds(content: dict[str, Any], key: str) -> tuple[list[float], list[float]]:
    """Static assembly bounds composed from independently checked local part envelopes."""
    from ..cad.assembly_model import placement

    requirements = content["part_requirements"].get(key)
    if requirements is not None:
        envelope = next(r for r in requirements if r["kind"] == "envelope")
        low = envelope["min"]
        return low, [low[n] + envelope["size"][n] for n in range(3)]
    points: list[list[float]] = []
    for instance in content["instances"]:
        if instance["parent"] != key:
            continue
        low, high = _design_bounds(content, instance["definition"])
        pose = instance["placement"]
        matrix = placement(tuple(pose["translate"]), tuple(pose["rotate"]))
        for point in product(*zip(low, high)):
            points.append([sum(matrix[row][col] * point[col] for col in range(3)) + matrix[row][3] for row in range(3)])
    if not points:
        _fail("assembly dimension constraint names an unknown definition", code="design_constraint_invalid", definition=key)
    return [min(p[n] for p in points) for n in range(3)], [max(p[n] for p in points) for n in range(3)]


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=0, abs_tol=1e-8)
    return a == b


_DIMENSION = re.compile(
    r"(?P<label>length|width|height|thickness|diameter|radius|depth|长度|总长|长|宽度|宽|高度|高|厚度|厚|孔径|直径|半径|深度|深)"
    r"\s*(?:is|为|是|=|:|：)?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mm|毫米|cm|厘米|m(?![a-z])|米)?", re.I)
_AXIS = {"length": 0, "长度": 0, "总长": 0, "长": 0, "width": 1, "宽度": 1, "宽": 1,
         "height": 2, "高度": 2, "高": 2, "thickness": 2, "厚度": 2, "厚": 2}
_MATERIALS = {"aluminum": "aluminium", "aluminium": "aluminium", "铝合金": "aluminium", "铝": "aluminium",
              "steel": "steel", "钢": "steel", "stainless steel": "stainless_steel", "不锈钢": "stainless_steel",
              "carbon steel": "carbon_steel", "碳钢": "carbon_steel",
              "plastic": "plastic", "塑料": "plastic", "木材": "wood", "wood": "wood"}


def _owners(text: str, content: dict[str, Any]) -> set[str]:
    lowered = text.casefold()
    names = [(d["key"], n.casefold()) for d in content["definitions"] for n in [d.get("name", ""), *d.get("aliases", [])] if n]
    hits = [(key, name) for key, name in names if name in lowered]
    # A short alias inside a longer component name must not steal that component's constraint.
    return {key for key, name in hits if not any(name != other and name in other for _, other in hits)}


def _dimension_owner(sentence: str, match: re.Match[str], content: dict[str, Any]) -> str | None:
    """A dimension belongs to its nearest named component, never every name in a sentence."""
    if match["label"] == "总长" or re.search(r"(?:overall|total|整机|整车|整体)\s*$", sentence[:match.start()], re.I):
        return content["root"]
    mentions = [(m.start(), m.end(), d["key"]) for d in content["definitions"]
                for name in [d.get("name", ""), *d.get("aliases", [])] if name
                for m in re.finditer(re.escape(name), sentence, re.I)]
    mentions = [m for m in mentions if not any(n[0] <= m[0] and n[1] >= m[1]
                and (n[0], n[1]) != (m[0], m[1]) for n in mentions)]
    before = [m for m in mentions if m[1] <= match.start()]
    if before:
        nearest = max(m[1] for m in before)
        owners = {m[2] for m in before if m[1] == nearest}
    else:
        owners = {m[2] for m in mentions if m[0] >= match.end()}
    if owners:
        return next(iter(owners)) if len(owners) == 1 else None
    children = [i["definition"] for i in content["instances"] if i["parent"] == content["root"]]
    parts = {d["key"] for d in content["definitions"] if d["kind"] != "assembly"}
    return children[0] if len(children) == 1 and children[0] in parts else content["root"]


def _dimension_path(content: dict[str, Any], path: list[str | int], label: str) -> bool:
    label = label.casefold()
    if len(path) == 4 and path[0] == "assembly_envelope":
        return label in _AXIS and path[2:] == ["size", _AXIS[label]]
    if len(path) < 4 or path[0] != "part_requirements":
        return False
    try:
        requirement = content[path[0]][path[1]][path[2]]
    except (KeyError, IndexError, TypeError):
        return False
    if label in _AXIS:
        return requirement["kind"] == "envelope" and path[3:] == ["size", _AXIS[label]]
    field = "depth" if label in {"depth", "深度", "深"} else "diameter" if label in {"diameter", "直径", "孔径"} else "radius"
    return (path[3:] == [field] and field in requirement
            or requirement["kind"] == "band" and field == "diameter" and path[3:] == ["outer", "diameter"])


def _dimension_key(content: dict[str, Any], owner: str | None, label: str) -> tuple | None:
    """Only a unique typed field can be superseded by a later bound free-text reply."""
    if owner is None:
        return None
    label = label.casefold()
    requirements = content["part_requirements"].get(owner)
    if requirements is None:
        return ("dimension", owner, "size", _AXIS[label]) if label in _AXIS else None
    candidates = []
    for index, requirement in enumerate(requirements):
        tails = [["size", _AXIS[label]]] if label in _AXIS else [["depth"], ["diameter"], ["radius"], ["outer", "diameter"]]
        for tail in tails:
            if _dimension_path(content, ["part_requirements", owner, index, *tail], label):
                candidates.append(("dimension", owner, requirement["id"], *tail))
    return candidates[0] if len(candidates) == 1 else None


def _material_mentions(sentence: str):
    from ..domain.materials import MATERIALS

    tokens = dict(_MATERIALS)
    for material in MATERIALS.values():
        tokens.update({alias: 'grade:' + alias for alias in (material.id, *material.aliases)})
    # An explicitly named unpriced alloy is still a source constraint, not permission to substitute a priced one.
    for match in re.finditer(r'(?<![a-z0-9.])(?:AL|AA)?[1-8]\d{3}(?:-?T\d+)?(?![a-z0-9.])', sentence, re.I):
        if (match[0][:2].upper() in {'AL', 'AA'} or re.match(r'\s*(铝|alumin(?:i)?um)', sentence[match.end():], re.I)
                or re.search(r'(铝合金|铝|alumin(?:i)?um)\s*$', sentence[:match.start()], re.I)):
            tokens.setdefault(match[0], 'grade:' + match[0])
    mentions = []
    for token, material in tokens.items():
        match = re.search((r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])") if token.isascii() else re.escape(token), sentence, re.I)
        if match:
            # Numeric aliases are not material declarations when they are a dimension value.
            if re.fullmatch(r'\d+(\.\d+)?', token) and (
                    re.match(r'\s*(mm|cm|毫米|厘米|m\b|米|kg|公斤|个|件|套|parts?\b|units?\b|[x×*]\s*\d)', sentence[match.end():], re.I)
                    or re.search(r'\d\s*[x×*]\s*$', sentence[:match.start()], re.I)
                    or any(m.start() <= match.start() < m.end() for m in _DIMENSION.finditer(sentence))):
                continue
            mentions.append((match, material))
    for match, material in mentions:
        # A grade/longer family owns its whole spelling (e.g. POM's Chinese alias (literally "steel-like") is not a steel request).
        if any(other.start() <= match.start() and other.end() >= match.end()
               and other.end() - other.start() > match.end() - match.start() for other, _ in mentions):
            continue
        prefix = re.split(r"[,，]", sentence[:match.start()])[-1]
        excluded = bool(re.search(r"(?:not\s+(?:use\s+)?|without\s+|不要(?:用|使用)?|不(?:用|使用|要)|不得(?:用|使用)?|不是)\s*$", prefix, re.I))
        yield match, material, excluded


def _material_matches(actual: str, material: str) -> bool:
    from ..domain.materials import find_material

    known = find_material(actual)
    if material.startswith('grade:'):
        requested = material.removeprefix('grade:')
        expected = find_material(requested)
        if known is not None and expected is not None:
            same_grade = known.id == expected.id
        else:
            # Normalize only spelling prefixes, not grade/temper differences; no unknown value is replaced.
            def normalize(value: str) -> str:
                return re.sub(r'^(AL|AA)(?=\d)', '', re.sub(r'[\s\-_/]', '', value.upper()))

            same_grade = normalize(actual) == normalize(requested)
        requested_temper = re.search(r'T\d+|(?<=POM-)[CH]$', requested, re.I)
        actual_temper = re.search(r'T\d+|(?<=POM-)[CH]$', actual, re.I)
        return same_grade and (requested_temper is None or actual_temper is not None
            and requested_temper[0].upper() == actual_temper[0].upper())
    if known is not None:
        return known.family == material or material == 'steel' and known.family in {'carbon_steel', 'stainless_steel'}
    if material == 'aluminium' and re.fullmatch(r'(AL|AA)[1-8]\d{3}(-?T\d+)?', actual, re.I):
        return True
    return any(alias in actual.casefold() for alias, category in _MATERIALS.items() if category == material)


def _quantity_match(sentence: str, name: str):
    return re.search(r"(?<![\d.])(\d+)\s*(?:个|件|根|套|只)?\s*" + re.escape(name) + r"s?", sentence, re.I)


def _source_revisions(texts: list[str], content: dict[str, Any]) -> tuple[list[tuple[int, str]], dict[tuple, int]]:
    # Original text and original constraints share one revision; only bound replies can replace them.
    sentences = [(max(-1, i - 2), sentence) for i, text in enumerate(texts)
                 for sentence in re.split(r"[。\n;；!?！？]|\.(?!\d)", text)]
    latest: dict[tuple, int] = {}
    part_keys = {d["key"] for d in content["definitions"] if d["kind"] != "assembly"}
    for revision, sentence in sentences:
        if revision < 0:
            continue
        for match in _DIMENSION.finditer(sentence):
            key = _dimension_key(content, _dimension_owner(sentence, match, content), match["label"])
            if key is not None:
                latest[key] = revision
        owners = _owners(sentence, content)
        material_owners = owners if owners else part_keys
        if len(material_owners) == 1 and material_owners <= part_keys and any(not excluded for _, _, excluded in _material_mentions(sentence)):
            latest[("material", next(iter(material_owners)))] = revision
        for definition in content["definitions"]:
            if any(name and _quantity_match(sentence, name) for name in [definition.get("name", ""), *definition.get("aliases", [])]):
                latest[("quantity", definition["key"])] = revision
    return sentences, latest


def effective_parameter_overrides(prior_plan: dict[str, Any], source_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep typed answers unless a provably later bound reply revises their exact semantic field.

    Question UUIDs identify the actual persisted answer event. Missing lineage, same-response
    ambiguity and changed array owners all retain the typed choice for normal validation to resolve.
    Raw replies and immutable choice receipts remain untouched.
    """
    responses = source_snapshot.get("user_responses") or []
    texts = [str(source_snapshot.get("text") or ""), str(source_snapshot.get("constraints") or "")]
    texts.extend(str(row.get("text") or "") for row in responses)
    _, latest = _source_revisions(texts, prior_plan)
    effective = []
    for override in source_snapshot.get("parameter_overrides") or []:
        path = override.get("field_path") or []
        anchor = semantic_anchor(prior_plan, path)
        events = [i for i, response in enumerate(responses) for answer in response.get("answers") or []
                  if override.get("question_id") and answer.get("question_id") == override["question_id"]]
        key = None
        if anchor and override.get("semantic_anchor") == anchor:
            owner = anchor.get("definition_key")
            if path[0] == "part_requirements":
                key = ("dimension", owner, anchor["requirement_id"], *path[3:])
            elif path[0] == "assembly_envelope":
                key = ("dimension", owner, *path[2:])
            elif len(path) == 3 and path[0] == "definitions" and path[2] == "material":
                key = ("material", owner)
            elif len(path) == 2 and path[0] == "quantities":
                key = ("quantity", owner)
        if not events or key is None or latest.get(key, -1) <= max(events):
            effective.append(override)
    return effective


def _validate_constraints(content: dict[str, Any], source: dict[str, Any], quantities: Counter[str]) -> None:
    texts = [str(source.get("text") or ""), str(source.get("constraints") or "")]
    texts.extend(str(row.get("text") or "") for row in source.get("user_responses") or [] if isinstance(row, dict))
    constraints = content["hard_constraints"]
    overrides = source.get("parameter_overrides") or []
    for override in overrides:
        path, expected = override.get("field_path") or [], override.get("value")
        actual_anchor = semantic_anchor(content, path)
        if not path or not _equal(_path_value(content, path, quantities), expected):
            _fail("user constraint override was changed or omitted", code="design_constraint_mismatch", field_path=path)
        if override.get("semantic_anchor") and override["semantic_anchor"] != actual_anchor:
            _fail("user constraint override changed its semantic owner", code="design_constraint_mismatch", field_path=path)
    for constraint in constraints:
        path, quote = constraint["field_path"], constraint["source_quote"]
        if not any(quote in text for text in texts) or not _equal(_path_value(content, path, quantities), constraint["value"]):
            _fail("design constraint must match its literal source and engineering field", code="design_constraint_mismatch", id=constraint["id"])
        owners = _owners(quote, content)
        anchor = semantic_anchor(content, path)
        if owners and anchor.get("definition_key") not in owners:
            _fail("design constraint belongs to another component", code="design_constraint_mismatch", id=constraint["id"])
        matches = list(_DIMENSION.finditer(quote))
        if matches:
            if not any(_dimension_path(content, path, m["label"]) and _equal(
                    float(m["value"]) * {"cm": 10, "厘米": 10, "m": 1000, "米": 1000}.get((m["unit"] or "").lower(), 1),
                    constraint["value"]) for m in matches):
                _fail("design constraint maps to the wrong dimension or axis", code="design_constraint_mismatch", id=constraint["id"])
        elif path[0] == "definitions" and path[-1] == "material":
            if str(constraint["value"]).casefold() not in quote.casefold() and not any(
                    not excluded and _material_matches(str(constraint["value"]), material)
                    for _, material, excluded in _material_mentions(quote)):
                _fail("material constraint differs from its source", code="design_constraint_mismatch", id=constraint["id"])
        elif len(path) == 3 and path[0] == "definitions" and path[-1] == "number" and anchor.get("definition_key") == content["root"]:
            value = constraint["value"]
            label = r"(?:编号|零件号|图号|(?<!\w)(?i:part\s+(?:number|no\.?)|p/n))\s*(?:(?i:is)|为|是)?\s*[:：=]?\s*[\"'“‘]?"
            if not isinstance(value, str) or not value.strip() or not re.search(label + re.escape(value) + r"(?![\w.-])", quote):
                _fail("part number constraint differs from its explicit source", code="design_constraint_mismatch", id=constraint["id"])
        elif path[0] == "quantities":
            if not re.search(rf"(?<!\d){re.escape(str(constraint['value']))}(?!\d)", quote):
                _fail("quantity constraint differs from its source", code="design_constraint_mismatch", id=constraint["id"])
        else:
            _fail("this source constraint needs a supported typed field", code="design_constraint_unsupported", id=constraint["id"])
    # Independently enumerate explicit source dimensions; model-written coverage is not proof of completeness.
    sentences, latest = _source_revisions(texts, content)
    for source_revision, sentence in sentences:
        owners = _owners(sentence, content)
        for match in _DIMENSION.finditer(sentence):
            owner = _dimension_owner(sentence, match, content)
            if latest.get(_dimension_key(content, owner, match["label"]), -1) > source_revision:
                continue
            value = float(match["value"]) * {"cm": 10, "厘米": 10, "m": 1000, "米": 1000}.get((match["unit"] or "").lower(), 1)
            covered = [c for c in constraints if match.group(0) in c["source_quote"] and _equal(c["value"], value)
                       and _dimension_path(content, c["field_path"], match["label"])
                       and owner is not None and semantic_anchor(content, c["field_path"]).get("definition_key") == owner]
            replaced = any(_dimension_path(content, o["field_path"], match["label"])
                and owner is not None and semantic_anchor(content, o["field_path"]).get("definition_key") == owner for o in overrides)
            if not covered and not replaced:
                _fail("explicit user dimension constraint is missing", code="design_constraint_missing", source_quote=match.group(0))
        for material_match, material, excluded in _material_mentions(sentence):
            targets = [d for d in content["definitions"] if d["kind"] != "assembly" and (not owners or d["key"] in owners)]
            if owners and not targets:
                _fail("explicit user material constraint names an unsupported assembly scope", code="design_constraint_unsupported",
                      definitions=sorted(owners))
            for definition in targets:
                path = ["definitions", content["definitions"].index(definition), "material"]
                if (any(o["field_path"] == path for o in overrides)
                        or not excluded and latest.get(("material", definition["key"]), -1) > source_revision):
                    continue
                actual = str(definition.get("material") or "").casefold()
                matches = _material_matches(actual, material)
                if matches == excluded:
                    _fail("explicit user material constraint was changed", code="design_constraint_mismatch", definition=definition["key"])
                if not excluded and not any(c["field_path"] == path and material_match.group(0) in c["source_quote"] for c in constraints):
                    _fail("explicit user material constraint is missing", code="design_constraint_missing", definition=definition["key"])
        # Excluding a named component is a source rule even if no model constraint reports it.
        for definition in content["definitions"]:
            names = [definition.get("name", ""), *definition.get("aliases", [])]
            if any(n and re.search(r"(?:不要|不含|排除|without\s+|exclude\s+)" + re.escape(n), sentence, re.I) for n in names):
                _fail("design includes a component explicitly excluded by the user", code="design_constraint_mismatch", definition=definition["key"])
            for name in names:
                if not name:
                    continue
                match = _quantity_match(sentence, name)
                if match:
                    path = ["quantities", definition["key"]]
                    if (any(o["field_path"] == path for o in overrides)
                            or latest.get(("quantity", definition["key"]), -1) > source_revision):
                        continue
                    if int(match[1]) != quantities[definition["key"]]:
                        _fail("explicit user quantity constraint was changed", code="design_constraint_mismatch", definition=definition["key"])
                    if not any(c["field_path"] == path and match.group(0) in c["source_quote"] for c in constraints):
                        _fail("explicit user quantity constraint is missing", code="design_constraint_missing", definition=definition["key"])


def validate_plan_content(content: DesignPlanContent | dict[str, Any], source_snapshot: dict[str, Any]) -> DesignPlanContent:
    """Validate a complete static design graph and its exact per-part geometric contract."""
    raw = content.model_dump(mode="json") if isinstance(content, DesignPlanContent) else content
    if not _finite(raw):
        _fail("design numbers must be finite")
    try:
        plan = DesignPlanContent.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        _fail("design plan schema is invalid", problems=str(exc)[:4000])
    data = plan.model_dump(mode="json")
    for collection, key in (("definitions", "key"), ("instances", "key"), ("relations", "key"), ("facts", "id"),
                            ("interfaces", "key"), ("decisions", "id"), ("hard_constraints", "id"), ("questions", "id")):
        occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for index, item in enumerate(data[collection]):
            occurrences[item[key]].append({"index": index, **({"owner": item["owner"]} if "owner" in item else {})})
        duplicates = [{key: value, "occurrences": entries} for value, entries in occurrences.items() if len(entries) > 1]
        if duplicates:
            _fail("design identifiers must be globally unique within their collection, including across owners",
                  collection=collection, scope="global", duplicates=duplicates)
    definitions = {d.key: d for d in plan.definitions}
    instances = {i.key: i for i in plan.instances}
    if any(not re.fullmatch(r"D-\d{2,}", key) for key in definitions) or any(not re.fullmatch(r"I-\d{2,}", key) for key in instances):
        _fail("design definitions and instances need canonical stable IDs")
    if plan.root not in definitions:
        _fail("design root must name an existing definition")
    part_root = definitions[plan.root].kind != "assembly"
    if part_root:
        if len(definitions) != 1 or plan.instances or plan.relations or plan.locked_placements:
            _fail("a single-part design must contain only its root part without assembly structure")
        if any(item.kind in {"assembly_step", "assembly_dwg"} for item in plan.deliverables):
            _fail("a single-part design cannot request an assembly export")
        if len(definitions[plan.root].material or "") > 40:
            _fail("a single-part material designation must fit the part metadata limit",
                  code="design_part_metadata_invalid", definition=plan.root, max_length=40)
        if len(definitions[plan.root].number or "") > 80:
            _fail("a single-part number must fit the part metadata limit",
                  code="design_part_metadata_invalid", definition=plan.root, max_length=80)
    children: dict[str, list[Any]] = defaultdict(list)
    for instance in plan.instances:
        if instance.definition not in definitions or instance.parent not in definitions or definitions[instance.parent].kind != "assembly":
            _fail("design instance references a missing definition or non-assembly parent", instance=instance.key)
        if instance.placement is None:
            _fail("design instances require an explicit local pose", instance=instance.key)
        children[instance.parent].append(instance)
    reachable: set[str] = set()
    paths: set[str] = set()
    quantities: Counter[str] = Counter()
    if part_root:
        quantities[plan.root] = 1

    def visit(key: str, ancestors: tuple[str, ...], path: str) -> None:
        if key in ancestors or len(ancestors) > 12:
            _fail("design graph has a cycle or exceeds its depth bound")
        reachable.add(key)
        if definitions[key].kind == "assembly" and not children[key]:
            _fail("design assembly cannot be empty", definition=key)
        for child in children[key]:
            full = f"{path}/{child.key}" if path else child.key
            paths.add(full)
            quantities[child.definition] += 1
            if len(paths) > 512:
                _fail("expanded design exceeds 512 occurrences")
            visit(child.definition, (*ancestors, key), full)

    visit(plan.root, (), "")
    if reachable != set(definitions):
        _fail("design has unreachable definitions", definitions=sorted(set(definitions) - reachable))
    if any(p not in paths for p in plan.locked_placements):
        _fail("locked design placements must name exact occurrence paths")
    parts = {d.key for d in plan.definitions if d.kind != "assembly"}
    if set(plan.part_requirements) != parts:
        _fail("design requirements must cover exactly every part definition")
    for key, requirements in plan.part_requirements.items():
        if not 1 <= len(requirements) <= 128 or sum(r.kind == "envelope" for r in requirements) != 1:
            _fail("each design part requires one envelope and a bounded requirement set", definition=key)
        if gate(requirements) or any(r.assumed for r in requirements):
            _fail("design requirements must resolve critical fields without assumed geometry", definition=key)
        if any(r.kind == "envelope" and r.min is None for r in requirements):
            _fail("design part envelope must pin its local origin with min XYZ", definition=key)
        if definitions[key].template is not None:
            _fail("design parts must pass the independent modeling path; templates are not accepted", definition=key)
        for requirement in requirements:
            if any(e.source != "design_plan" or e.ref != f"{key}/{requirement.id}" or e.value is not None for e in requirement.evidence) or not requirement.evidence:
                _fail("design requirements cite their own server-bound definition and requirement", definition=key, requirement=requirement.id)
    graphs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for relation in plan.relations:
        fields = {"fixed": {}, "parallel": {}, "perpendicular": {}, "coaxial": {"aligned": (bool,)},
            "planar": {"flip": (bool,), "distance": (int, float), "offset": (int, float)},
            "offset": {"distance": (int, float), "flip": (bool,)}, "angle": {"angle": (int, float)},
            "pattern_match": {"shift": (int,)}}[relation.kind]
        required = {"offset": {"distance"}, "angle": {"angle"}}.get(relation.kind, set())
        if (required - relation.params.keys() or any(k not in fields or type(v) not in fields[k] for k, v in relation.params.items())
                or {"distance", "offset"} <= relation.params.keys()):
            _fail("design relation parameters must use the supported solver fields and types", relation=relation.key)
        endpoints = [relation.a.instance, relation.b.instance]
        owners = [e[1:] if e.startswith("@") else instances[e].parent if e in instances else None for e in endpoints]
        if None in owners or owners[0] != owners[1] or owners[0] not in children or endpoints[0] == endpoints[1]:
            _fail("design relations must join concrete children of the same assembly or its own frame", relation=relation.key)
        graphs[owners[0]][endpoints[0]].add(endpoints[1])
        graphs[owners[0]][endpoints[1]].add(endpoints[0])
    for owner, items in children.items():
        if len(items) < 2:
            continue
        seen, pending = set(), [items[0].key]
        while pending:
            key = pending.pop()
            if key not in seen:
                seen.add(key)
                pending.extend(graphs[owner][key] - seen)
        if {i.key for i in items} - seen:
            _fail("multi-component design assembly needs connected required relations", code="design_relations_missing", definition=owner)
    fact_ids = {f.id for f in plan.facts}
    for item in [*plan.definitions, *plan.instances, *plan.relations]:
        if set(item.fact_ids) - fact_ids:
            _fail("design references missing facts")
    for interface in plan.interfaces:
        if interface.owner not in definitions or interface.at is None or interface.kind in {"axis", "plane"} and not interface.dir:
            _fail("design interfaces need concrete owner and local geometry", interface=interface.key)
        if interface.dir and sum(v * v for v in interface.dir) < 1e-12:
            _fail("design interface direction cannot be zero", interface=interface.key)
    _validate_constraints(data, source_snapshot, quantities)
    from .design_geometry import validate_design_geometry
    from .design_static import validate_static_plan

    validate_design_geometry(plan)
    validate_static_plan(data)
    return plan


def has_design(version: EngineeringSpecVersion) -> bool:
    return bool((version.content or {}).get("design") or (version.planning or {}).get("design_ref")
                or any(s.get("kind") == "design_plan" for s in version.sources or []))


def _authority_row(db: Session, version_id: uuid.UUID) -> AiDesignPlanVersion | WorkflowDesignPlanVersion | None:
    """Only the actual database table identifies authority; client JSON never chooses its resolver."""
    assistant = db.get(AiDesignPlanVersion, version_id)
    workflow = db.get(WorkflowDesignPlanVersion, version_id)
    if assistant is not None and workflow is not None:
        raise AppError(409, "design_scope_mismatch", "the design reference has ambiguous persisted authority")
    return assistant if assistant is not None else workflow


def _resolved(db: Session, user: User, ref: Any, *, project_id: uuid.UUID, tool_kind: str,
              node_run_id: uuid.UUID | None = None) -> ResolvedDesignPlan:
    from ..ai.design_contracts import DesignRef
    from . import design_plans

    try:
        reference = ref if isinstance(ref, DesignRef) else DesignRef.model_validate(ref)
    except (ValidationError, TypeError, ValueError) as exc:
        raise AppError(409, "design_reference_invalid", "the stored design reference is invalid") from exc
    row = _authority_row(db, reference.version_id)
    if row is None or row.project_id != project_id or row.user_id != user.id:
        raise AppError(404, "design_plan_not_found", "design plan not found")
    if isinstance(row, WorkflowDesignPlanVersion):
        from . import workflow_design

        resolved = workflow_design.resolve_authorized_design(db, user, reference, project_id=project_id,
            node_run_id=node_run_id, tool_kind=tool_kind)
    else:
        resolved = design_plans.resolve_authorized_design(db, user, reference, conversation_id=row.conversation_id,
            root_operation_id=row.root_operation_id, tool_kind=tool_kind)
    validate_plan_content(resolved.content, resolved.source_snapshot)
    return resolved


def _compiled_content(resolved: ResolvedDesignPlan) -> dict[str, Any]:
    plan = resolved.content.model_dump(mode="json")
    fields = ("summary", "root", "definitions", "instances", "relations", "facts", "interfaces", "deliverables")
    content = SpecReply.model_validate({k: plan[k] for k in fields}).model_dump(mode="json")
    ref = resolved.ref.model_dump(mode="json")
    # These are planned values. No fake sent chunks, source quotes, human answers or Turbo confirmations.
    for fact in content["facts"]:
        fact.update(status="derived", evidence=[], origin="ai_design", design_ref=ref,
                    derivation="Compiled from the authorized AI design plan")
    for interface in content["interfaces"]:
        interface.update(status="derived", sources=[], how="Compiled from the authorized AI design plan", origin="ai_design")
    content["design"] = {"origin": "ai_design", "ref": ref, "source_sha256": resolved.source_sha256,
        "compiler": COMPILER_VERSION, "required_relations": plan["relations"],
        "locked_placements": plan["locked_placements"], "static_poses": True,
        "requirements_sha256": {key: requirements_sha256(reqs) for key, reqs in resolved.content.part_requirements.items()}}
    return content


def compile_design_spec(db: Session, access: ProjectAccess, resolved: ResolvedDesignPlan, *, policy: str,
                        frozen_planning: dict[str, Any] | None = None,
                        node_run_id: uuid.UUID | None = None) -> tuple[EngineeringSpec, EngineeringSpecVersion]:
    """Idempotently compile one exact authorized plan/policy; never run prose extraction."""
    from . import design_plans, engineering_spec, spec_identity
    from .audit import audit

    access.require("design")
    policy = pol.parse_policy(policy)
    live = _resolved(db, access.user, resolved.ref, project_id=access.project.id, tool_kind="spec_extract", node_run_id=node_run_id)
    if (live.authorization.get("kind") == "workflow_design_request"
            and live.authorization.get("execution_policy") != policy):
        raise AppError(409, "design_scope_mismatch", "the compiler must preserve the design workflow's frozen policy")
    if (design_plans.digest(live.content.model_dump(mode="json")) != design_plans.digest(resolved.content.model_dump(mode="json"))
            or live.source_sha256 != resolved.source_sha256 or live.source_snapshot != resolved.source_snapshot
            or live.authorization != resolved.authorization):
        raise AppError(409, "design_hash_mismatch", "the supplied design carrier differs from the authorized version")
    key = f"design-spec:{live.ref.version_id}:{policy}"
    db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    prior = db.scalar(select(EngineeringSpecVersion).where(EngineeringSpecVersion.project_id == access.project.id,
        EngineeringSpecVersion.execution_policy == policy,
        EngineeringSpecVersion.planning["design_ref"]["version_id"].astext == str(live.ref.version_id)))
    if prior is not None:
        resolve_spec_design(db, access.user, prior, tool_kind="spec_extract", node_run_id=node_run_id)
        return db.get(EngineeringSpec, prior.spec_id), prior
    db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"engineering_spec.number:{access.project.id}"})
    number = (db.scalar(select(func.max(EngineeringSpec.number)).where(EngineeringSpec.project_id == access.project.id)) or 0) + 1
    spec = EngineeringSpec(project_id=access.project.id, number=number, title=live.content.title, created_by=access.user.id)
    db.add(spec)
    db.flush()
    row = _authority_row(db, live.ref.version_id)
    planning = {**(frozen_planning or {}), "compiler": COMPILER_VERSION, "design_ref": live.ref.model_dump(mode="json"),
                "source_sha256": live.source_sha256, "ai_call_ids": [], "chunks_total": 0, "chunks_sent": 0}
    if isinstance(row, WorkflowDesignPlanVersion):
        planning["ai_call_ids"] = list(row.provenance.get("call_ids") or [])
    sources = [{"id": "DESIGN", "kind": "design_plan", "label": live.content.title, "sha256": live.ref.sha256,
                "design_ref": live.ref.model_dump(mode="json"), "origin": "ai_design", "read_status": "compiled"}]
    version = engineering_spec._new_version(db, spec, origin="rule", policy=policy, created_by=access.user.id,
                                            sources=sources, frozen_planning=planning)
    version.content = spec_identity.freeze(_compiled_content(live), version=version, reason="authorized_design")
    version.status, version.finished_at = "ready", datetime.now(UTC)
    version.simulated = bool((row.provenance or {}).get("simulated"))
    version.validation = {"compiler": COMPILER_VERSION, "problems": [], "design_ref": planning["design_ref"]}
    version.checks = [{"check": "spec.clarification_gate", **pol.run_check("spec.clarification_gate", policy,
        lambda: {"status": "passed", "questions": 0, "origin": "ai_design"})}]
    audit(db, action="engineering_spec.design_compile", subject_type="engineering_spec", subject_id=spec.id,
        user_id=access.user.id, project_id=access.project.id,
        data={"version_id": str(version.id), "design_ref": planning["design_ref"], "execution_policy": policy})
    db.flush()
    return spec, version


def resolve_spec_design(db: Session, user: User, version: EngineeringSpecVersion, *, tool_kind: str = "modeling",
                        node_run_id: uuid.UUID | None = None) -> ResolvedDesignPlan | None:
    """A persisted spec JSON cannot grant itself design authority or alter the compiled graph."""
    from .design_plans import digest

    if not has_design(version):
        return None
    live = _resolved(db, user, (version.planning or {}).get("design_ref"), project_id=version.project_id,
                     tool_kind=tool_kind, node_run_id=node_run_id)
    expected = _compiled_content(live)
    actual = {k: v for k, v in (version.content or {}).items() if k not in {"identity", "baseline", "target_history"}}
    if (digest(actual) != digest(expected) or version.planning.get("compiler") != COMPILER_VERSION
            or version.planning.get("source_sha256") != live.source_sha256
            or live.authorization.get("kind") == "workflow_design_request"
            and version.execution_policy != live.authorization.get("execution_policy")):
        raise AppError(409, "design_spec_mismatch", "the compiled design specification no longer matches its exact plan")
    return live


def definition_contexts(db: Session, user: User, version: EngineeringSpecVersion, *,
                        node_run_id: uuid.UUID | None = None) -> dict[str, DesignDefinitionContext]:
    live = resolve_spec_design(db, user, version, node_run_id=node_run_id)
    if live is None:
        return {}
    return {key: DesignDefinitionContext(plan_ref=live.ref.model_dump(mode="json"), spec_version_id=str(version.id),
        definition_key=key, requirements=tuple(r.model_dump(mode="json") for r in reqs),
        interfaces=tuple(i.model_dump(mode="json") for i in live.content.interfaces if i.owner == key))
        for key, reqs in live.content.part_requirements.items()}
