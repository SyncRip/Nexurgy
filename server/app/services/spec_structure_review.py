"""Pure, bounded final structure review over merged source readings.

The model proposes scope decisions; this module validates their graph effects. Raw
skeleton rows never change. Common hardware at multiple levels is only a candidate,
never an automatic deletion. The caller validates evidence against the pages actually
sent and persists the returned audit record before finalizing the specification.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from typing import Any

from .spec_identity import CATALOG_ONLY_FIELDS, _core, catalog_scope_problem, identity_key, merge_guard, size_of

MAX_INSTANCES = 2000
MAX_EXPANDED = 100_000
MAX_DECISIONS = 100
_STRUCTURE_COUNT_FIELDS = {'quantity', 'title_block_quantity', 'drawing_quantity', 'total_quantity',
                           'instance_quantity', 'component_quantity', 'member_quantity',
                           'instance_count', 'component_count', 'member_count'}
_POSE_FIELD = re.compile(r'placement|position|transform|translate|rotation|coordinate|origin|mate|location|mounting|姿态|位置|定位|坐标', re.I)
_CATALOG_FIELDS = CATALOG_ONLY_FIELDS


def _defs(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d['key']: d for d in content.get('definitions', [])}


def _edges(content: dict[str, Any]) -> Counter[tuple[str, str]]:
    return Counter((i.get('parent') or content.get('root'), i['definition'])
                   for i in content.get('instances', []))


def _expanded(edges: Counter[tuple[str, str]], root: str | None) -> Counter[str]:
    children: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (p, c), n in edges.items():
        if n > 0:
            children[p].append((c, n))
    memo: dict[str, Counter[str]] = {}
    def visit(k: str, stack: set[str]) -> Counter[str]:
        if k in stack:
            raise ValueError('structure review cannot accept a containment cycle')
        if k in memo:
            return memo[k]
        out: Counter[str] = Counter({k: 1})
        for child, quantity in children.get(k, []):
            for descendant, count in visit(child, stack | {k}).items():
                out[descendant] += quantity * count
                if sum(out.values()) > MAX_EXPANDED:
                    raise ValueError('expanded structure exceeds review limit')
        memo[k] = out
        return out
    # Detect cycles even in branches not currently reachable from the root.
    for k in list(children):
        visit(k, set())
    return visit(root, set()) if root else Counter()


def review_context(content: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Complete compact graph plus candidates, without clipping or guessing scope."""
    defs, edges = _defs(content), _edges(content)
    totals = _expanded(edges, content.get('root'))
    candidates: list[dict[str, Any]] = []
    for (parent, child), quantity in sorted(edges.items()):
        via = []
        for (p, sub), n in sorted(edges.items()):
            if p != parent or sub == child or defs.get(sub, {}).get('kind') != 'assembly':
                continue
            below = _expanded(edges, sub).get(child, 0)
            if below:
                via.append({'assembly': sub, 'quantity': n, 'per_assembly': below, 'total': n * below})
        if via:
            candidates.append({'kind': 'scope_overlap', 'parent': parent, 'child': child,
                               'direct': quantity, 'nested': sum(v['total'] for v in via), 'via': via})
    for key, d in defs.items():
        if key != content.get('root') and not totals[key]:
            candidates.append({'kind': 'unplaced', 'child': key, 'parent': None,
                               'has_children': any(p == key for p, _ in edges)})
    split_pairs: set[tuple[str, str, str]] = set()
    for split in (record or {}).get('standard_identity_splits') or []:
        child, target = split.get('old_definition'), split.get('target')
        for parent in split.get('parents') or []:
            triple = (parent, child, target)
            if (triple not in split_pairs and child != target and (parent, child) in edges
                    and (parent, target) in edges):
                split_pairs.add(triple)
                candidates.append({'kind': 'member_identity', 'parent': parent, 'child': child, 'target': target,
                                   'original_quantity': edges[(parent, child)], 'read_quantity': edges[(parent, target)],
                                   'warning': 'A local BOM declares a different standard identity; review its membership without changing other parents.'})
    quantities = [copy.deepcopy(f) for f in content.get('facts', [])
                  if (str(f.get('subject', '')).partition('.')[2] in _STRUCTURE_COUNT_FIELDS
                      or str(f.get('subject', '')).partition('.')[2].startswith('qty['))]
    for f in quantities:
        subject = str(f.get('subject', ''))
        child, _, field = subject.partition('.')
        value = f.get('value')
        if (child in defs and isinstance(value, int) and not isinstance(value, bool) and value > 0
                and field in ('title_block_quantity', 'drawing_quantity') and value != totals[child]):
            for (parent, k), n in sorted(edges.items()):
                if k == child:
                    candidates.append({'kind': 'quantity_scope', 'parent': parent, 'child': child,
                                       'local': n, 'expanded': totals[child], 'source_quantity': value,
                                       'fact_id': f.get('id'), 'warning': 'Title quantity may have another scope; do not copy mechanically.'})
    return {
        'root': content.get('root'),
        'definitions': [{k: copy.deepcopy(d.get(k)) for k in ('key', 'kind', 'name', 'number', 'size', 'aliases')}
                        for d in defs.values()],
        'memberships': [{'parent': p, 'child': c, 'quantity': n} for (p, c), n in sorted(edges.items())],
        'expanded_totals': {k: totals[k] for k in defs}, 'candidates': candidates,
        'skeleton_claims': copy.deepcopy(content.get('skeleton', [])),
        'quantity_facts': quantities,
        'quantity_conflicts': [copy.deepcopy(c) for c in content.get('conflicts', []) if '.qty[' in c.get('subject', '')],
        'restated': copy.deepcopy((record or {}).get('restated', [])),
        'standard_identity_splits': copy.deepcopy((record or {}).get('standard_identity_splits', [])),
        'source_memberships': copy.deepcopy((record or {}).get('source_memberships', [])),
        'rules': ['Review every candidate explicitly; keep legitimate shared hardware.',
                  'Use assembly views and own-parent BOMs to reconcile local versus rolled-up counts.',
                  'No scalar/title count alone authorizes a parent change; no unsupported catalogue merge.'],
    }


def _next(items: list[dict[str, Any]], field: str, prefix: str) -> str:
    used = {str(x.get(field)) for x in items}
    n = max((int(m.group(1)) for k in used if (m := re.fullmatch(prefix + r'-(\d+)', k))), default=0) + 1
    return f'{prefix}-{n:03d}'


def _source(e: dict[str, Any]) -> str:
    locator = e.get('locator') or f"chunk {e.get('chunk')}"
    tail = f" · entity {e['entity']}" if e.get('entity') else f" · region {e['region']}" if e.get('region') else ''
    return f"{e.get('source')} · {locator}{tail}"


def _readings(content: dict[str, Any], subject: str, new: int, evidence: list[dict[str, Any]], basis: str) -> list[dict[str, Any]]:
    """ConflictReply-compatible readings; full originals remain in the audit."""
    conflict = next((f for f in content.get('conflicts', []) if f.get('subject') == subject), None)
    values = copy.deepcopy((conflict or {}).get('values', []))
    fact = next((f for f in content.get('facts', []) if f.get('subject') == subject), None)
    fields = ('source', 'chunk', 'quote', 'entity', 'region', 'detail')
    if not values and fact:
        for e in fact.get('evidence') or []:
            values.append({**{k: copy.deepcopy(e[k]) for k in fields if k in e},
                           'value': fact.get('value'), 'unit': 'pcs',
                           'status': fact['status'] if fact.get('status') in ('asserted', 'derived') else 'unknown',
                           'derivation': fact.get('derivation')})
    values.extend({**{k: copy.deepcopy(e[k]) for k in fields if k in e}, 'value': new, 'unit': 'pcs',
                   'status': 'derived', 'derivation': basis} for e in evidence)
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        unique.setdefault(json.dumps(value, ensure_ascii=False, sort_keys=True), value)
    return list(unique.values())


def _apply_review(content: dict[str, Any], decisions: list[dict[str, Any]], *, policy: str,
                 at: str = '', sentences: list[dict[str, str]] | None = None,
                 validated_identity: set[tuple[str, str]] | None = None,
                 validated_catalog_exclusions: set[tuple[str, str]] | None = None,
                 validated_member_replacements: set[tuple[str, str, str]] | None = None,
                 validated_visual_duplicates: set[tuple[str, str, str]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and apply one atomic quantity/scope batch. Failed proposals stay pending.

    Existing instance keys survive where possible; removed endpoints and their
    relations are archived. Standard never changes the tree. Caller must validate
    quote/entity/region provenance before this pure structural guard. A validated
    identity pair additionally requires independently bound catalogue, current
    detail-title and parent-BOM evidence; never derive this set from model prose.
    """
    c = copy.deepcopy(content)
    for field in ('facts', 'questions', 'conflicts', 'assumptions', 'relations', 'manufacturing_notes'):
        c.setdefault(field, [])
    defs, before = _defs(c), _edges(c)
    totals = _expanded(before, c.get('root'))
    context = review_context(c)
    overlaps = {(x['parent'], x['child']): x for x in context['candidates'] if x['kind'] == 'scope_overlap'}
    record: dict[str, Any] = {'rule': 'final_structure_review', 'applied': [], 'kept': [], 'unresolved': [],
                              'rejected': [], 'merged': [], 'removed_instances': [], 'removed_relations': [],
                              'before': context['memberships'], 'decisions': copy.deepcopy(decisions)}
    if len(decisions) > MAX_DECISIONS:
        raise ValueError('too many structure review decisions')
    staged = Counter(before)
    proposed: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    visual_duplicates: list[dict[str, Any]] = []
    catalog_exclusions: list[dict[str, Any]] = []
    claimed: set[tuple[str | None, str]] = set()
    duplicated: set[tuple[str | None, str]] = set()

    def question(d: dict[str, Any], why: str) -> None:
        child, parent = d.get('child'), d.get('parent')
        affected = list(dict.fromkeys(k for k in (parent, child) if k in defs))
        if not affected and c.get('root') in defs:
            affected = [c['root']]
        if not affected:
            return  # a malformed reply has no valid object on which to hang a question
        subject = f'{affected[0]}.structure_scope[{child}]'
        if not any(q.get('subject') == subject for q in c['questions']):
            c['questions'].append({'id': _next(c['questions'], 'id', 'Q'), 'subject': subject,
                                  'question': why, 'affects': affected,
                                  'candidates': [], 'blocking': True, 'origin': 'server'})

    def reject(d: dict[str, Any], why: str) -> None:
        record['rejected'].append({'decision': copy.deepcopy(d), 'reason': why})
        question(d, why)

    for d in decisions:
        action, parent, child = d.get('action'), d.get('parent'), d.get('child')
        pair = (parent, child)
        if child not in defs or parent is not None and parent not in defs:
            reject(d, 'Unknown structure endpoint'); continue
        if pair in claimed:
            duplicated.add(pair)
            reject(d, 'Several decisions modify the same membership or candidate'); continue
        claimed.add(pair)
        evidence = d.get('evidence') or []
        if not d.get('basis') or action != 'unresolved' and not evidence:
            reject(d, 'A structure decision requires its basis and located source evidence'); continue
        if any(not isinstance(e, dict) or not e.get('source') or not isinstance(e.get('chunk'), int)
               or not (e.get('entity') or e.get('quote') or e.get('region')) for e in evidence):
            reject(d, 'Structure evidence must identify an actual chunk and quote, entity or image region'); continue
        if action == 'keep':
            record['kept'].append(copy.deepcopy(d)); continue
        if action == 'unresolved':
            record['unresolved'].append(copy.deepcopy(d)); question(d, d['basis']); continue
        if action in ('quantity', 'rolled_up', 'replace_member'):
            if pair not in before or defs[parent].get('kind') != 'assembly':
                reject(d, 'Only an existing parent membership may be reviewed'); continue
            if action == 'replace_member':
                target = d.get('target')
                a, b = defs[child], defs.get(target, {})
                if (target == child or (parent, target) not in before
                        or before[(parent, target)] != before[pair]
                        or (parent, child, target) not in (validated_member_replacements or set())):
                    reject(d, 'Member replacement requires two equal existing memberships and independently validated own-parent BOM evidence'); continue
                if (a.get('kind') != 'standard_part' or b.get('kind') != 'standard_part'
                        or not (identity_key(a) or '').startswith('std:') or not (identity_key(b) or '').startswith('std:')
                        or identity_key(a) == identity_key(b)
                        or not size_of(a.get('size'), a.get('name'))
                        or size_of(a.get('size'), a.get('name')) != size_of(b.get('size'), b.get('name'))):
                    reject(d, 'Member replacement may only separate distinct complete standard identities of the same nominal size'); continue
                staged[pair] = 0
            elif action == 'rolled_up':
                via = d.get('covered_by') or []
                allowed = {x['assembly'] for x in overlaps.get(pair, {}).get('via', [])}
                if not via or len(via) != len(set(via)) or not set(via) <= allowed:
                    reject(d, 'Rolled-up scope requires existing descendant paths through named direct subassemblies'); continue
                staged[pair] = 0
            else:
                quantity = d.get('quantity')
                if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_INSTANCES:
                    reject(d, 'Local quantity must be a positive bounded integer'); continue
                staged[pair] = quantity
            proposed.append(d)
        elif action == 'exclude_catalog':
            target = d.get('target')
            if parent is not None or (child, target) not in (validated_catalog_exclusions or set()):
                reject(d, 'Catalog exclusion requires independently validated catalog/detail/parent-BOM source roles'); continue
            why = catalog_scope_problem(c, child, target)
            if why:
                reject(d, why); continue
            catalog_exclusions.append(d)
        elif action == 'duplicate_visual':
            from .spec_visual_identity import merge_problem
            if (parent, child, d.get('target')) not in (validated_visual_duplicates or set()):
                reject(d, 'Visual duplicate requires independently validated original declarations and same-component image evidence'); continue
            why = merge_problem(c, d)
            if why:
                reject(d, why); continue
            visual_duplicates.append(d)
        elif action == 'same_as':
            target = d.get('target')
            if (totals[child] or any(p == child or k == child for p, k in before)
                    or target not in defs or not totals[target] or target == child
                    or defs[child].get('kind') == 'assembly' or defs[target].get('kind') == 'assembly'
                    or defs[child].get('kind') != defs[target].get('kind')):
                reject(d, 'Only a wholly unplaced leaf catalogue item may merge into a reachable leaf'); continue
            why = merge_guard(defs[child], defs[target], sentences)
            if (child, target) in (validated_identity or set()):
                a, b = defs[child], defs[target]
                fields = {str(f.get('subject', '')).partition('.')[2] for f in c['facts']
                          if str(f.get('subject', '')).partition('.')[0] == child}
                if a.get('kind') != b.get('kind'):
                    why = 'Catalogue supersession cannot change the component type'
                elif (a.get('size') or b.get('size')) and a.get('size') != b.get('size'):
                    why = 'Catalogue supersession cannot change an explicit size'
                elif a.get('kind') == 'standard_part' and size_of(a.get('size'), a.get('name')) != size_of(b.get('size'), b.get('name')):
                    why = 'Catalogue supersession cannot change a standard-part size'
                elif fields - _CATALOG_FIELDS:
                    why = 'A definition with its own geometric or engineering detail is not a catalogue-only alias'
                else:
                    # The service proved the changed catalogue number/series from
                    # three actual source locations; retain the name-kind guard.
                    ac, bc = _core(a.get('name')), _core(b.get('name'))
                    why = ('Catalogue/detail names do not identify the same component type'
                           if not ac or not bc or not set(ac).intersection(bc)
                           else merge_guard({**a, 'number': None, 'name': ac}, {**b, 'number': None, 'name': bc}))
            if why:
                reject(d, why); continue
            aliases.append(d)
        else:
            reject(d, 'Unknown structure action')

    if duplicated:
        for d in proposed + aliases + catalog_exclusions + visual_duplicates:
            if (d.get('parent'), d['child']) in duplicated:
                reject(d, 'Conflicting duplicate decisions: no proposal for this membership is applied')
        proposed = [d for d in proposed if (d.get('parent'), d['child']) not in duplicated]
        aliases = [d for d in aliases if (d.get('parent'), d['child']) not in duplicated]
        catalog_exclusions = [d for d in catalog_exclusions if (d.get('parent'), d['child']) not in duplicated]
        visual_duplicates = [d for d in visual_duplicates if (d.get('parent'), d['child']) not in duplicated]
        for pair in duplicated:
            staged[pair] = before[pair]

    removed_definitions = {d['child'] for d in visual_duplicates + aliases}
    chained = [d for d in visual_duplicates if d['target'] in removed_definitions]
    if chained:
        for d in visual_duplicates:
            reject(d, 'Chained or cyclic visual identity decisions require a single surviving target')
        visual_duplicates = []

    # Validate all quantity changes together, so a local correction can establish
    # the exact descendant total needed by an ancestor's rolled-up interpretation.
    try:
        if sum(staged.values()) > MAX_INSTANCES:
            raise ValueError('local instance count exceeds review limit')
        _expanded(staged, c.get('root'))
        for d in proposed:
            parent, child = d['parent'], d['child']
            new = staged[(parent, child)]
            group = [i for i in c['instances'] if (i.get('parent') or c.get('root'), i['definition']) == (parent, child)]
            removed = group[new:]
            gone = {i['key'] for i in removed}
            resolving = {f'{parent}.qty[{child}]', f'{parent}.qty[{child}].unit'}
            def anchored(item: dict[str, Any], field: str) -> bool:
                subject = str(item.get('subject', ''))
                if subject in resolving:
                    return False
                own, _, name = subject.partition('.')
                return bool(gone.intersection(item.get(field, []))) and (own in gone or bool(_POSE_FIELD.search(name)))
            if (any(i.get('placement') is not None for i in removed)
                    or any(r.get(end, {}).get('instance') in gone for r in c['relations'] for end in ('a', 'b'))
                    or any(str(f.get('subject', '')).partition('.')[0] in gone for f in c['facts'])
                    or any(anchored(a, 'affected') for a in c['assumptions'])
                    or any(anchored(q, 'affects') for q in c['questions'])):
                raise ValueError(f'{parent}/{child}: scope review cannot remove source-positioned or referenced instances')
            if d['action'] == 'quantity' and len(_readings(c, f'{parent}.qty[{child}]', new, d['evidence'], d['basis'])) > 20:
                raise ValueError('quantity conflict exceeds 20 retained readings; no source reading may be truncated')
            if d['action'] == 'replace_member' and staged[(parent, d['target'])] != before[(parent, d['target'])]:
                raise ValueError('The replacement target membership must remain unchanged throughout this batch')
            if d['action'] != 'rolled_up':
                continue
            total = sum(staged[(parent, sub)] * _expanded(staged, sub).get(child, 0) for sub in d['covered_by'])
            if total != before[(parent, child)]:
                raise ValueError(f'rolled-up total {parent}/{child}: descendants {total} do not match source total {before[(parent, child)]}')
    except ValueError as exc:
        for d in proposed:
            reject(d, str(exc))
        proposed = []
        staged = Counter(before)

    for candidate in context['candidates']:
        pair = (candidate.get('parent'), candidate['child'])
        if pair not in claimed:
            record['unresolved'].append({'candidate': candidate, 'reason': 'Review omitted this candidate'})
            question(candidate, 'The final structure review did not resolve this source scope or unplaced component.')

    if policy != 'turbo':
        for d in proposed + aliases + catalog_exclusions + visual_duplicates:
            question(d, f"Review source scope before changing {d.get('parent') or ''}/{d['child']}: {d['basis']}")
        record['pending'] = copy.deepcopy(proposed + aliases + catalog_exclusions + visual_duplicates)
        record['after'] = context['memberships']
        return c, record

    used_i = {i['key'] for i in c.get('instances', [])}
    for d in proposed:
        parent, child = d['parent'], d['child']
        old, new = before[(parent, child)], staged[(parent, child)]
        if old == new:
            record['kept'].append(copy.deepcopy(d)); continue
        aid = _next(c['assumptions'], 'id', 'A')
        subject = f'{parent}.qty[{child}]'
        evidence = copy.deepcopy(d['evidence'])
        old_fact = next((f for f in c['facts'] if f.get('subject') == subject), None)
        prior_fact = copy.deepcopy(old_fact)
        prior_conflicts = copy.deepcopy([f for f in c['conflicts'] if f.get('subject') == subject])
        prior_assumptions = copy.deepcopy([a for a in c['assumptions'] if a.get('subject') == subject])
        prior_questions = copy.deepcopy([q for q in c['questions'] if q.get('subject') in (subject, subject + '.unit')])
        replacing = d['action'] == 'replace_member'
        assumption = {'id': aid, 'subject': subject, 'field': 'member_identity' if replacing else 'scope' if d['action'] == 'rolled_up' else 'quantity',
                      'adopted': {'replacement': d['target'], 'local_quantity': old} if replacing else
                      {'local_quantity': new, 'covered_by': d.get('covered_by', [])} if d['action'] == 'rolled_up' else new,
                      'unit': 'pcs', 'basis': d['basis'], 'alternatives': [old], 'affected': [parent, child],
                      'sources': [_source(e) for e in evidence], 'trigger': 'conflict'}
        c['assumptions'].append(assumption)
        if d['action'] in ('rolled_up', 'replace_member'):
            if replacing:
                assumption['affected'].append(d['target'])
            total_subject = f"{parent}.{'superseded_qty' if replacing else 'rolled_up_qty'}[{child}]"
            if old_fact:
                old_fact['subject'] = total_subject
            for conflict in c['conflicts']:
                if conflict.get('subject') == subject:
                    conflict.update(subject=total_subject, resolution={'assumption': aid})
            for prior in c['assumptions']:
                if prior.get('subject') == subject and prior['id'] != aid:
                    prior['subject'] = total_subject
            scope_subject = f'{parent}.membership_scope[{child}]'
            value = ({'scope': 'replaced_by', 'definition': d['target'], 'quantity': old} if replacing else
                     {'scope': 'rolled_up', 'total': old, 'covered_by': d['covered_by']})
            c['facts'].append({'id': _next(c['facts'], 'id', 'F'), 'subject': scope_subject, 'kind': 'structure',
                               'value': value, 'unit': None,
                               'status': 'assumed', 'evidence': evidence, 'derivation': aid})
        else:
            values = _readings(c, subject, new, evidence, d['basis'])
            if not old_fact or not old_fact.get('evidence'):
                # Old memberships can originate in skeleton readings with no fact.
                # Keep that raw claim in the audit rather than inventing a source id.
                record.setdefault('prior_unlocated_counts', []).append({'parent': parent, 'child': child, 'quantity': old})
            conflict = next((f for f in c['conflicts'] if f.get('subject') == subject), None)
            if conflict:
                conflict['values'] = values
                conflict['resolution'] = {'assumption': aid}
            elif len({json.dumps(v.get('value'), sort_keys=True) for v in values}) > 1:
                c['conflicts'].append({'id': _next(c['conflicts'], 'id', 'C'), 'subject': subject,
                                      'values': values, 'resolution': {'assumption': aid}})
            else:
                assumption['trigger'] = 'ambiguous'
            if old_fact is None:
                old_fact = {'id': _next(c['facts'], 'id', 'F'), 'subject': subject, 'kind': 'quantity'}
                c['facts'].append(old_fact)
            old_fact.update(value=new, unit='pcs', status='assumed', evidence=evidence, derivation=aid)
        c['questions'] = [q for q in c['questions'] if q.get('subject') not in (subject, subject + '.unit')]
        for fact in c['facts']:
            if str(fact.get('subject', '')).partition('.')[0] == parent and fact['id'] not in defs[parent].setdefault('fact_ids', []):
                defs[parent]['fact_ids'].append(fact['id'])
        group = [i for i in c['instances'] if (i.get('parent') or c.get('root'), i['definition']) == (parent, child)]
        removed = group[new:] if new < old else []
        gone = {i['key'] for i in removed}
        for collection, field in (('assumptions', 'affected'), ('questions', 'affects')):
            for item in c[collection]:
                if gone.intersection(item.get(field, [])):
                    record.setdefault('reference_updates', []).append({'collection': collection, 'before': copy.deepcopy(item)})
                    item[field] = list(dict.fromkeys([k for k in item[field] if k not in gone] + [parent, child]))
        record['removed_instances'].extend(copy.deepcopy(removed))
        c['instances'] = [i for i in c['instances'] if i['key'] not in gone]
        removed_relations = [r for r in c['relations'] if any(r.get(end, {}).get('instance') in gone for end in ('a', 'b'))]
        record['removed_relations'].extend(copy.deepcopy(removed_relations))
        c['relations'] = [r for r in c['relations'] if r not in removed_relations]
        for index in range(old, new):
            n = max((int(m.group(1)) for k in used_i if (m := re.fullmatch(r'I-(\d+)', k))), default=0) + 1
            key = f'I-{n:03d}'; used_i.add(key)
            c['instances'].append({'key': key, 'definition': child, 'parent': parent,
                                   'name': f"{defs[child].get('name') or child} #{index + 1}", 'placement': None, 'fact_ids': []})
        record['applied'].append({'action': d['action'], 'parent': parent, 'child': child, 'old': old, 'new': new,
                                  **({'target': d['target'], 'target_quantity': old} if replacing else {}),
                                  'prior_fact': prior_fact, 'prior_conflicts': prior_conflicts,
                                  'prior_assumptions': prior_assumptions, 'prior_questions': prior_questions,
                                  'assumption': aid, 'evidence': evidence, 'basis': d['basis']})

    # The established helper handles fact conflicts/re-keying, notes and references.
    # It performs no I/O; guard and narrow orphan eligibility were checked above.
    for d in aliases:
        from .spec_aliases import _merge
        child, target = d['child'], d['target']
        aid = _next(c['assumptions'], 'id', 'A')
        used_a = {a['id'] for a in c['assumptions']} | {aid}
        used_c = {x['id'] for x in c['conflicts']}
        existing_assumptions = {a['id'] for a in c['assumptions']}
        alias_name = defs[child].get('name')
        archive = {'definition': copy.deepcopy(defs[child]),
                   'facts': copy.deepcopy([f for f in c['facts'] if str(f.get('subject', '')).partition('.')[0] in (child, target)]),
                   'conflicts': copy.deepcopy([f for f in c['conflicts'] if str(f.get('subject', '')).partition('.')[0] in (child, target)])}
        merged = _merge(c, defs, child, target, aid, used_a, used_c, at)
        for assumption in c['assumptions']:
            if assumption['id'] not in existing_assumptions:
                # _merge also serves finalized specs. This input is still a
                # SpecReply; the normal finalizer supplies author/time later.
                record.setdefault('alias_assumptions', []).append(copy.deepcopy(assumption))
                assumption.pop('author', None)
                assumption.pop('at', None)
                assumption['affected'] = list(dict.fromkeys(target if k == child else k
                                                            for k in assumption.get('affected', [])))
        c['assumptions'].append({'id': aid, 'subject': target, 'field': 'identity', 'adopted': {'alias': child, 'target': target},
                                 'basis': d['basis'], 'unit': None, 'alternatives': [f'{child} remains a separate component'],
                                 'affected': [target], 'sources': [_source(e) for e in d['evidence']], 'trigger': 'ambiguous'})
        record['merged'].append({'alias': child, 'alias_name': alias_name, 'into': target, 'into_name': defs[target].get('name'),
                                 'decision': aid, 'evidence': copy.deepcopy(d['evidence']), 'prior': archive,
                                 'validated_catalogue_supersession': (child, target) in (validated_identity or set()), **merged})
    for d in visual_duplicates:
        from . import spec_visual_identity
        why = spec_visual_identity.merge_problem(c, d)
        if why:
            reject(d, why); continue
        aid = _next(c['assumptions'], 'id', 'A')
        if 'visual_identity_prior' not in record:
            record['visual_identity_prior'] = copy.deepcopy(content)
        applied_visual = spec_visual_identity.apply(c, d, aid, at)
        record.setdefault('visual_duplicates', []).append(applied_visual)
        record['merged'].append({key: copy.deepcopy(applied_visual[key]) for key in
                                ('action', 'alias', 'alias_name', 'into', 'into_name', 'decision', 'sources', 'evidence', 'prior_ref')})
    for d in catalog_exclusions:
        child, target = d['child'], d['target']
        # Quantity and alias decisions in this batch may change reachability.
        why = catalog_scope_problem(c, child, target)
        if why:
            reject(d, why); continue
        aid = _next(c['assumptions'], 'id', 'A')
        c['assumptions'].append({'id': aid, 'subject': child, 'field': 'catalog_scope',
                                 'adopted': {'scope': 'outside_root', 'selected_variant': target},
                                 'basis': d['basis'], 'unit': None, 'alternatives': ['unresolved product membership'],
                                 'affected': [child, target], 'sources': [_source(e) for e in d['evidence']],
                                 'trigger': 'ambiguous'})
        record.setdefault('catalog_exclusions', []).append({
            'key': child, 'target': target, 'decision': aid, 'validated_catalog_scope': True,
            'definition': copy.deepcopy(defs[child]), 'selected_definition': copy.deepcopy(defs[target]),
            'basis': d['basis'], 'evidence': copy.deepcopy(d['evidence']),
        })
    record['after'] = [{'parent': p, 'child': k, 'quantity': n} for (p, k), n in sorted(_edges(c).items())]
    return c, record


def apply_review(content: dict[str, Any], decisions: list[dict[str, Any]], *, policy: str,
                 at: str = '', sentences: list[dict[str, str]] | None = None,
                 validated_identity: set[tuple[str, str]] | None = None,
                 validated_catalog_exclusions: set[tuple[str, str]] | None = None,
                 validated_member_replacements: set[tuple[str, str, str]] | None = None,
                 validated_visual_duplicates: set[tuple[str, str, str]] | None = None,
                 validated_assembly_attachments: set[tuple[str, str]] | None = None,
                 attachment_context: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage missing assembly edges before the established quantity/scope validator; commit the whole batch atomically."""
    options: dict[str, Any] = dict(at=at, sentences=sentences, validated_identity=validated_identity,
        validated_catalog_exclusions=validated_catalog_exclusions, validated_member_replacements=validated_member_replacements,
        validated_visual_duplicates=validated_visual_duplicates)
    attachments = [d for d in decisions if d.get('action') == 'attach_assembly']
    if not attachments:
        return _apply_review(content, decisions, policy=policy, **options)
    from . import spec_assembly_attachment as attachment
    context = attachment_context or {}
    problem = next((why for d in attachments if (why := attachment.proof_problem(content, d, context))), None)
    if len(decisions) > MAX_DECISIONS or len({(d.get('parent'), d['child']) for d in decisions}) != len(decisions):
        problem = 'Attachment batch contains duplicate membership decisions or exceeds the review limit'
    staged = copy.deepcopy(content)
    if not problem:
        staged, attachments, problem = attachment.stage_problem(content, decisions, validated_assembly_attachments or set(), context)
    proposed, audit = copy.deepcopy(content), {}
    if not problem:
        proposed, audit = _apply_review(staged, [d for d in decisions if d.get('action') != 'attach_assembly'], policy='turbo', **options)
        if audit['rejected']:
            problem = '; '.join(r['reason'] for r in audit['rejected'])
    if problem or policy != 'turbo':
        pending = [{**d, 'action': 'unresolved', 'basis': problem or 'Confirm the source-supported assembly attachment and its atomic scope decisions.'}
                   for d in decisions]
        unchanged, record = _apply_review(content, pending, policy='standard', **options)
        record.update(decisions=copy.deepcopy(decisions), attachments=[], pending=[] if problem else copy.deepcopy(decisions))
        if problem:
            record['rejected'] = [{'decision': copy.deepcopy(d), 'reason': problem} for d in decisions]
        return unchanged, record
    audit['assembly_attachment_prior'] = copy.deepcopy(content)
    if 'visual_identity_prior' in audit:
        audit.pop('visual_identity_prior')
        for item in audit.get('visual_duplicates', []) + audit.get('merged', []):
            if item.get('prior_ref') == 'visual_identity_prior':
                item['prior_ref'] = 'assembly_attachment_prior'
    audit['before'] = review_context(content)['memberships']
    audit['decisions'] = copy.deepcopy(decisions)
    audit['attachments'] = []
    defs = _defs(proposed)
    for d in attachments:
        parent, child, quantity = d['parent'], d['child'], d['quantity']
        aid = _next(proposed['assumptions'], 'id', 'A')
        adopted = {'parent': parent, 'child': child, 'quantity': quantity, 'unconfirmed': True}
        proposed['assumptions'].append({'id': aid, 'subject': parent, 'field': 'assembly_attachment',
            'adopted': adopted, 'basis': d['basis'], 'unit': 'pcs', 'trigger': 'missing',
            'alternatives': ['The detailed assembly remains unplaced pending confirmation of original installation scope.'],
            'affected': [parent, child], 'sources': [_source(e) for e in d['evidence']]})
        fid = _next(proposed['facts'], 'id', 'F')
        proposed['facts'].append({'id': fid, 'subject': f'{parent}.qty[{child}]', 'kind': 'quantity',
            'value': quantity, 'unit': 'pcs', 'status': 'assumed', 'evidence': copy.deepcopy(d['evidence']), 'derivation': aid})
        defs[parent].setdefault('fact_ids', []).append(fid)
        audit['attachments'].append({**adopted, 'assumption': aid, 'basis': d['basis'], 'evidence': copy.deepcopy(d['evidence']),
            'proof': copy.deepcopy(d['assembly_attachment_evidence']), 'prior_ref': 'assembly_attachment_prior'})
    return proposed, audit
