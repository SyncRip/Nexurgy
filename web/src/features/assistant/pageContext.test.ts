import { describe, expect, it } from 'vitest';
import type { AiRef } from '../../api/assistant';
import { exactVersion, shouldFollowResult } from './exactVersion';
import { declareContext, EMPTY_CONTEXT, inputOnly, lockPageTarget, pageReferences, parsePending, pendingFromRef, queuePending, refIdentity, registerPage, removePending, restoreContext, sendFingerprint, type PageContext, type PendingReference } from './pageContext';

const page: PageContext = {
  projectId: 'p1', label: 'R1', object: { type: 'part_model', id: 'part1', version: 'r1' },
  target: { kind: 'part_revision', project_id: 'p1', object_id: 'r1' },
};
const pick: PendingReference = { label: 'Face 3', target: true, automatic: true, input: { kind: 'cad_selection', object_id: 'r1', project_id: 'p1', selection: { entity: 'face', id: 3, build_key: 'build1', mesh_sha: 'mesh1', resolved_op_id: 'F-2' } } };

describe('local exact page context', () => {
  it('keeps a removed chip removed across polling and translated labels', () => {
    const current = declareContext(EMPTY_CONTEXT, page);
    const removed = removePending(current, refIdentity(page.target!));
    expect(declareContext(removed, { ...page, label: 'Version 1' }).references).toEqual([]);
    const selection = queuePending(current, pick);
    const hidden = removePending(selection, refIdentity(pick.input));
    expect(queuePending(hidden, { ...pick, label: '面 3' })).toBe(hidden);
    expect(queuePending(hidden, { ...pick, automatic: false }).references[0].target).toBe(true);
  });
  it('keeps the selected feature ahead of its whole object during same-scope refreshes', () => {
    const selected = queuePending(declareContext(EMPTY_CONTEXT, page), pick);
    const refreshed = declareContext(selected, { ...page, label: 'R1 refreshed' });
    expect(refreshed.references.map((r) => [r.input.kind, r.target])).toEqual([['cad_selection', true], ['part_revision', false]]);
    expect(declareContext(refreshed, { ...page, target: null, unavailable: 'Unsaved preview' }).references).toEqual([]);
  });
  it('discards previous selections and suppression when version, object or project changes', () => {
    const before = removePending(queuePending(declareContext(EMPTY_CONTEXT, page), pick), refIdentity(page.target!));
    for (const next of [
      { ...page, object: { ...page.object!, version: 'r2' }, target: { ...page.target!, object_id: 'r2' } },
      { ...page, object: { ...page.object!, id: 'part2' }, target: null },
      { ...page, projectId: 'p2', target: { ...page.target!, project_id: 'p2' } },
    ]) {
      const context = declareContext(before, next);
      expect(context.references).toEqual(pageReferences(next));
      expect(context.suppressed).toEqual([]);
    }
  });
  it('does not infer a target on a page that explicitly has none', () => {
    const refs = pageReferences({ ...page, target: null, references: [{ input: { kind: 'file_version', project_id: 'p1', object_id: 'file1' }, label: 'Drawing' }] });
    expect(refs.some((r) => r.target)).toBe(false);
  });
  it('projects normalized nodes and quote fields back to strict reference inputs', () => {
    const ref = { kind: 'workflow', object_id: 'w1', project_id: 'p1', version: 4, node: { node_id: 'model', type: 'model_3d', label: 'Modeling' }, field: { tier_qty: 10, key: 'material', value: 300, currency: 'CNY' } } as AiRef;
    expect(inputOnly(ref as never)).toEqual({ kind: 'workflow', object_id: 'w1', project_id: 'p1', version: 4, node: { node_id: 'model' }, field: { tier_qty: 10, key: 'material' } });
    expect(pendingFromRef(ref).input.version).toBe(4);
    expect(refIdentity(inputOnly(ref as never))).toBe(refIdentity({ ...ref, node: { ...ref.node!, label: '建模' } } as never));
  });
  it('preserves exact assembly paths and drawing entity identities', () => {
    const refs: PendingReference[] = [
      { label: 'Instance', input: { kind: 'assembly_revision', object_id: 'a-v2', project_id: 'p1', instance: { path: ['outer', 'bolt'] } } },
      { label: 'Entities', input: { kind: 'drawing_document_version', object_id: 'd-v1', project_id: 'p1', entities: { ids: ['L-2', 'C-1'] } } },
    ];
    expect(parsePending(JSON.parse(JSON.stringify(refs))).map((r) => r.input)).toEqual(refs.map((r) => r.input));
    expect(refIdentity(refs[1].input)).toBe(refIdentity({ ...refs[1].input, entities: { ids: ['C-1', 'L-2', 'L-2'] } }));
    expect(parsePending([{ ...pick, input: { ...pick.input, selection: { ...pick.input.selection, mesh_sha: null } } }])).toEqual([]);
    expect(parsePending([{ ...refs[0], input: { ...refs[0].input, instance: { path: [] } } }])).toEqual([]);
  });
  it('keeps semantic CAD identity independent of display reasons and pick location', () => {
    const selection = pick.input.selection!;
    const changedDisplay = { ...pick.input, selection: { ...selection, point: [1, 2, 3] as [number, number, number], candidates: [{ op_id: 'F-2', kind: 'hole', label: '孔', reason: '半径匹配' }] } };
    expect(refIdentity(changedDisplay)).toBe(refIdentity(pick.input));
    expect(refIdentity({ ...pick.input, selection: { ...selection, resolved_op_id: 'F-3' } })).not.toBe(refIdentity(pick.input));
  });
  it('matches server limits without discarding valid large drawing selections', () => {
    const drawing = { label: 'Drawing', input: { kind: 'drawing_document_version', project_id: 'p1', object_id: 'd1', entities: { ids: Array.from({ length: 20_000 }, (_, index) => `L-${index}`) } } };
    expect(parsePending([drawing])[0].input.entities?.ids).toHaveLength(20_000);
    expect(parsePending([{ ...drawing, input: { ...drawing.input, entities: { ids: [...drawing.input.entities.ids, 'extra'] } } }])).toEqual([]);
    const workflow = { label: 'Workflow', input: { kind: 'workflow', project_id: 'p1', object_id: 'w1', version: 0 } };
    expect(parsePending([workflow])).toEqual([]);
    const assembly = { label: 'Assembly', input: { kind: 'assembly_revision', project_id: 'p1', object_id: 'a1', instance: { path: Array(11).fill('instance') } } };
    expect(parsePending([assembly])).toEqual([]);
  });
  it('locks the panel target within an exact version and resets on a different scope', () => {
    const first = { ...page, target: { ...page.target!, node: { node_id: 'first' } } };
    const second = { ...page, label: 'Second', target: { ...page.target!, node: { node_id: 'second' } } };
    expect(lockPageTarget(first, second, true)?.target).toEqual(first.target);
    expect(lockPageTarget(first, second, false)?.target).toEqual(second.target);
    const newVersion = { ...second, object: { ...page.object!, version: 'r2' } };
    expect(lockPageTarget(first, newVersion, true)).toBe(newVersion);
  });
  it('protects mobile handover and ignores another registrar cleanup', () => {
    const a = Symbol('source'), b = Symbol('pane');
    const current = { owner: a, page, handedOver: false };
    expect(registerPage(current, null, b)).toBe(current);
    const handed = { ...current, handedOver: true };
    expect(registerPage(handed, null, a)).toBe(handed);
    const pane = registerPage(handed, { ...page, label: 'Pane' }, b);
    expect(pane.handedOver).toBe(false);
    expect(registerPage(pane, null, a)).toBe(pane);
    expect(registerPage(pane, null, b).page).toBeNull();
  });
  it('does not restore another object draft over the visible page', () => {
    const current = declareContext(EMPTY_CONTEXT, page);
    const old = { scope: 'old-project', references: [{ ...pick, input: { ...pick.input, project_id: 'old' } }], suppressed: [] };
    expect(restoreContext(current, old, page)).toEqual(current);
    const removed = removePending(current, refIdentity(page.target!));
    expect(restoreContext(current, removed, page).references).toEqual([]);
  });
  it('identifies retries by project, text, target, exact references and revise choice', () => {
    const original = sendFingerprint('p1', 'Edit this', [pick]);
    expect(sendFingerprint('p1', ' Edit this ', [{ ...pick, label: 'Translated' }])).toBe(original);
    for (const changed of [sendFingerprint('p2', 'Edit this', [pick]), sendFingerprint('p1', 'Edit this', [{ ...pick, target: false }]), sendFingerprint('p1', 'Edit this', []), sendFingerprint('p1', 'Edit this', [pick], false)]) expect(changed).not.toBe(original);
  });
});

describe('saved version viewing', () => {
  it('never substitutes the latest version for a missing historical link', () => {
    const versions = [{ id: 'r2' }, { id: 'r1' }];
    expect(exactVersion(versions, null)?.id).toBe('r2');
    expect(exactVersion(versions, 'r1')?.id).toBe('r1');
    expect(exactVersion(versions, 'gone')).toBeNull();
    expect(exactVersion(undefined, 'r1')).toBeNull();
  });
  it('follows a fresh result only on a clean head or empty page', () => {
    expect(shouldFollowResult('r1', 'r2', false)).toBe(false);
    expect(shouldFollowResult('r2', 'r2', true)).toBe(false);
    expect(shouldFollowResult('r2', 'r2', false)).toBe(true);
    expect(shouldFollowResult(null, null, false)).toBe(true);
  });
});
