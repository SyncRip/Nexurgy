"""Explicit source-bound missing assembly edges with atomic count-scope reconciliation."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import source_reader as sr
from .drawing_identity import title_match
from .spec_identity import identity_key


def proof_refs(decision: dict[str, Any]) -> list[dict[str, Any]]:
    proof = decision.get('assembly_attachment_evidence') or {}
    refs = [proof[k] for k in ('parent_installation', 'child_number', 'child_name') if k in proof]
    refs += [e for row in proof.get('child_memberships', []) for e in row.get('evidence', [])]
    distinct = decision.get('distinct_installation_evidence')
    if distinct:
        refs.append(distinct['parent_installation'])
    return refs


def row_cited(row: dict[str, Any], refs: list[dict[str, Any]]) -> bool:
    """Bind all number/designation/count cells, or their exact complete original grouped row."""
    refs = [e for e in refs if e.get('source') == row.get('source') and e.get('chunk') == row.get('chunk')]
    if any(e.get('quote') and sr.locate_quote(e['quote'], row['row']) is not None for e in refs):
        return True
    return bool(row.get('evidence')) and all(any(e.get('entity') == cell.get('entity') for e in refs)
                                            for cell in row['evidence'])


def bind_proposed_scope_rows(content: dict[str, Any], decisions: list[dict[str, Any]], context: dict[str, Any],
                             chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate explicitly cited original BOM rows for an existing flat edge omitted by extraction.

    This adds proof only; neither a name match nor a table scan installs/removes a membership.
    """
    from .spec_structure_review import _edges
    defs = {d['key']: d for d in content.get('definitions', [])}
    edges = _edges(content)
    out = []
    for d in decisions:
        parent, key = d.get('parent'), d.get('child')
        if not isinstance(parent, str) or not isinstance(key, str):
            continue
        pair = (parent, key)
        if d.get('action') != 'rolled_up' or pair not in edges or pair[1] not in defs:
            continue
        child = defs[pair[1]]
        for chunk in chunks:
            sheet = _sheet(chunk, context)
            refs = [e for e in d.get('evidence', []) if e.get('source') == chunk['source'] and e.get('chunk') == chunk['chunk']]
            if not sheet or pair[0] not in sheet['assembly_definitions'] or not refs:
                continue
            for line in chunk['text'].splitlines():
                if not line.startswith(f"drawing table row {sheet['frame']} "):
                    continue
                cells = [(json.loads('"' + value + '"'), entity) for value, entity in
                         re.findall(r'"((?:\\.|[^"\\])*)" \[([^\]]+)\]', line)]
                for j in range(len(cells) - 2):
                    (number, number_id), (name, name_id), (count, count_id) = cells[j:j + 3]
                    if not count.strip().isdigit() or int(count.strip()) != edges[pair]:
                        continue
                    row_identity = identity_key({'number': number, 'name': name})
                    if not row_identity or row_identity != identity_key(child):
                        continue
                    if not row_identity.startswith('std:') and title_match(child, number=number, name=name) != 'canonical':
                        continue
                    row = {'source': chunk['source'], 'chunk': chunk['chunk'], 'frame': sheet['frame'],
                        'parent': pair[0], 'child': pair[1], 'quantity': edges[pair], 'row': line,
                        'child_identity': row_identity, 'source_sha256': sheet['source_sha256'],
                        'file_version_id': sheet['file_version_id'],
                        'evidence': [{'source': chunk['source'], 'chunk': chunk['chunk'], 'entity': entity}
                                     for entity in (number_id, name_id, count_id)],
                        'basis': 'Explicit review citation of exact original BOM identity/count for an existing membership.'}
                    if row_cited(row, refs) and row not in out:
                        out.append(row)
    return out


def _sheet(evidence: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    return next((s for s in context.get('attached_sheets', []) if s['source'] == evidence.get('source')
                 and evidence.get('chunk') in s['chunks']), None)


def proof_problem(content: dict[str, Any], decision: dict[str, Any], context: dict[str, Any]) -> str | None:
    """Refs have already passed the production sent/image/bounds locator; prove their distinct source roles."""
    defs = {d['key']: d for d in content.get('definitions', [])}
    parent, child = decision.get('parent'), decision.get('child')
    proof = decision.get('assembly_attachment_evidence')
    if decision.get('action') != 'attach_assembly' or not proof or parent not in defs or child not in defs:
        return 'Assembly attachment requires existing endpoints and typed original-source roles'
    refs = list(decision.get('evidence', []))
    for ref in proof_refs(decision):
        if ref not in refs:
            refs.append(ref)
    if len(refs) > 20:
        return 'Assembly attachment exceeds retained evidence capacity; no proof may be truncated'
    if (proof.get('installed_groups_are_distinct') is not True or proof.get('repeated_projections_excluded') is not True
            or not str(proof.get('count_basis', '')).strip()):
        return 'Assembly attachment must explicitly count distinct physical groups and exclude repeated projections'
    installation = proof.get('parent_installation') or {}
    parent_sheet = _sheet(installation, context)
    if not installation.get('region') or not parent_sheet or parent not in parent_sheet['assembly_definitions']:
        return 'Assembly installation must locate a region on the actual reachable parent original sheet'
    number, name = proof.get('child_number') or {}, proof.get('child_name') or {}
    child_sheet = _sheet(number, context)
    if (not child_sheet or child not in child_sheet['assembly_definitions'] or _sheet(name, context) != child_sheet
            or any(parent_sheet.get(k) != child_sheet.get(k) for k in ('source', 'source_sha256', 'file_version_id'))):
        return 'Assembly title and installation must bind the exact original source/file version and their own sheets'
    title = next((t for t in context.get('attachment_titles', []) if t['source'] == child_sheet['source']
                  and t['frame'] == child_sheet['frame'] and title_match(defs[child], number=t['number'], name=t['name']) == 'canonical'), None)
    if (not title or not title.get('number') or not title.get('name') or
            any(ref.get('entity') != title.get('title_entities', {}).get(role) or ref.get('region')
                for role, ref in (('number', number), ('name', name)))):
        return 'Assembly attachment must cite both exact current number/name title entities'
    direct = {i['definition'] for i in content.get('instances', []) if i.get('parent') == child}
    members = proof.get('child_memberships') or []
    if not direct or len(members) != len(direct) or {m.get('child') for m in members} != direct:
        return 'Assembly attachment needs original own-parent bindings for every direct child member'
    for member in members:
        rows = [r for r in context.get('source_memberships', []) if r.get('parent') == child
                and r.get('child') == member['child'] and r.get('source') == child_sheet['source']
                and r.get('child_identity') == identity_key(defs[member['child']])
                and r.get('frame') == child_sheet['frame'] and r.get('source_sha256') == child_sheet.get('source_sha256')
                and r.get('file_version_id') == child_sheet.get('file_version_id')]
        if not any(row_cited(r, member.get('evidence', [])) for r in rows):
            return 'Assembly member proof must cite its independently bound original own-parent BOM row'
    return None


def scope_problem(content: dict[str, Any], decisions: list[dict[str, Any]], context: dict[str, Any],
                  attachments: list[dict[str, Any]], new_overlaps: list[dict[str, Any]]) -> str | None:
    """A newly attached group cannot double flat claims without an explicit original-scope decision."""
    by_pair = {(d.get('parent'), d['child']): d for d in decisions}
    for overlap in new_overlaps:
        parent, child = overlap['parent'], overlap['child']
        d = by_pair.get((parent, child), {})
        if d.get('action') == 'keep':
            proof = d.get('distinct_installation_evidence') or {}
            evidence = proof.get('parent_installation') or {}
            sheet = _sheet(evidence, context)
            if (proof.get('distinct_physical_installation') is not True or not evidence.get('region')
                    or not sheet or parent not in sheet['assembly_definitions']):
                return 'New attachment overlap keep requires explicit distinct installation proof on the actual parent image'
        elif d.get('action') == 'rolled_up':
            ancestor_rows = [r for r in context.get('source_memberships', []) + context.get('proposed_scope_bindings', [])
                             if r.get('parent') == parent and r.get('child') == child]
            if not any(row_cited(r, d.get('evidence', [])) for r in ancestor_rows):
                return 'New attachment rolled-up scope must cite the original ancestor own-parent count row'
            via = set(d.get('covered_by', []))
            for attach in attachments:
                if attach['child'] not in via:
                    continue
                member_rows = [r for r in context.get('source_memberships', [])
                               if r.get('parent') == attach['child'] and r.get('child') == child]
                if not any(row_cited(r, d.get('evidence', [])) for r in member_rows):
                    return 'New attachment rolled-up scope must cite the attached child own-parent member count row'
        else:
            return 'Every newly exposed attachment overlap needs an explicit supported keep or rolled_up decision'
    return None


def stage_problem(content: dict[str, Any], decisions: list[dict[str, Any]], validated: set[tuple[str, str]],
                  context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    from .spec_structure_review import MAX_INSTANCES, _edges, _expanded, review_context
    c = copy.deepcopy(content)
    defs = {d['key']: d for d in c.get('definitions', [])}
    edges = _edges(c)
    totals = _expanded(edges, c.get('root'))
    attachments = [d for d in decisions if d.get('action') == 'attach_assembly']
    seen = set()
    for d in attachments:
        parent, child, quantity = d.get('parent'), d.get('child'), d.get('quantity')
        if not isinstance(parent, str) or not isinstance(child, str):
            return c, attachments, 'Attachment endpoints must be existing assembly keys'
        if (parent not in defs or child not in defs or parent == child or child == c.get('root')
                or defs[parent].get('kind') != 'assembly' or defs[child].get('kind') != 'assembly'
                or not totals[parent] or any(k == child and n for (_, k), n in edges.items()) or child in seen
                or (parent, child) not in validated):
            return c, attachments, 'Attachment requires an independently source-validated unparented assembly and already reachable assembly parent'
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_INSTANCES:
            return c, attachments, 'Attachment local quantity must be a positive bounded integer'
        seen.add(child)
        if not d.get('basis') or not d.get('evidence'):
            return c, attachments, 'Attachment requires an explicit basis and original source evidence'
        if sum(edges.values()) + quantity > MAX_INSTANCES:
            return c, attachments, 'Attachment exceeds local instance review limit'
        edges[(parent, child)] = quantity
        for _ in range(quantity):
            keys = {i['key'] for i in c['instances']}
            from .spec_structure_review import _next
            key = _next([{'key': k} for k in keys], 'key', 'I')
            c['instances'].append({'key': key, 'parent': parent, 'definition': child,
                                   'name': defs[child].get('name', ''), 'placement': None, 'fact_ids': []})
    if sum(edges.values()) > MAX_INSTANCES:
        return c, attachments, 'Attachment exceeds local instance review limit'
    try:
        _expanded(edges, c.get('root'))
        old_via = {(x['parent'], x['child']): {v['assembly']: v['total'] for v in x['via']}
                   for x in review_context(content)['candidates'] if x['kind'] == 'scope_overlap'}
        new_overlaps = [x for x in review_context(c)['candidates'] if x['kind'] == 'scope_overlap'
                        and {v['assembly']: v['total'] for v in x['via']} != old_via.get((x['parent'], x['child']), {})]
    except ValueError as exc:
        return c, attachments, str(exc)
    problem = scope_problem(content, decisions, context, attachments, new_overlaps)
    if problem:
        return c, attachments, problem
    # The existing scope applicator protects poses; attachment also refuses every original
    # instance reference (including unpositioned notes/assumptions) before atomic deletion.
    for d in decisions:
        if d.get('action') not in ('rolled_up', 'quantity', 'replace_member'):
            continue
        group = [i for i in content['instances'] if (i.get('parent') or content.get('root'), i['definition']) == (d.get('parent'), d['child'])]
        new = d.get('quantity') if d['action'] == 'quantity' else 0
        if not isinstance(new, int) or isinstance(new, bool):
            return c, attachments, 'Invalid local quantity in attachment scope batch'
        gone = {i['key'] for i in group[new:]}
        if (any(str(n.get('subject', '')).partition('.')[0] in gone for n in content.get('manufacturing_notes', []))
                or any(gone.intersection(item.get(field, [])) for coll, field in (('assumptions', 'affected'), ('questions', 'affects'))
                       for item in content.get(coll, []))):
            return c, attachments, 'Attachment scope cannot delete an instance referenced by original notes, assumptions or questions'
    return c, attachments, None
