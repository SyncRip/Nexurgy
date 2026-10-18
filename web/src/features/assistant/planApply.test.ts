import { describe, expect, it } from 'vitest';
import type { AiDraftingModifyResult } from '../../api/assistant';
import { planApplyBody, planConfirmationKey } from './planApply';

const head = '26c6ca3f-768b-492e-86e5-3b3128abef80';
const hash = 'a'.repeat(64);
const drawing = (removals: string[] = []): AiDraftingModifyResult => ({
  kind: 'drafting_modify', document_id: '4dcd0d4a-bb77-4408-8c52-79b1689b3ce3', base_version_id: head,
  plan_hash: hash, summary: 'Move selected line', changes: [], warnings: [], questions: [], removals, application: null,
  plan: { schema_version: 1, document_id: '4dcd0d4a-bb77-4408-8c52-79b1689b3ce3', base_version_id: head, base_version_no: 2,
    state_sha256: 'b'.repeat(64), selection: { ids: ['E-1'] }, commands: [{ op: 'transform', ids: ['E-1'], dx: 10 }],
    affected_entity_ids: ['E-1'], removed_entity_ids: removals, summary: 'Move selected line', changes: [], warnings: [], questions: [] },
});

describe('human plan application snapshot', () => {
  it('binds a drawing plan to the displayed hash and exact base', () => {
    expect(planApplyBody(drawing())).toEqual({ plan_hash: hash, expected_head: head, confirm_removals: false });
  });
  it('requires explicit removal consent, including removed block contents', () => {
    const plan = drawing(['block:DETAIL:E-1']);
    expect(planApplyBody(plan)).toBeNull();
    expect(planApplyBody(plan, true)).toEqual({ plan_hash: hash, expected_head: head, confirm_removals: true });
    expect(planConfirmationKey(plan)).not.toBe(planConfirmationKey({ ...plan, plan_hash: 'c'.repeat(64) }));
    expect(planConfirmationKey(plan)).not.toBe(planConfirmationKey({ ...plan, base_version_id: '11bb22cc-3344-4555-8666-778899aabbcc' }));
  });
  it('cannot degrade an invalid or missing new drawing binding into legacy apply', () => {
    for (const plan_hash of ['', 'A'.repeat(64), 'a'.repeat(63)]) expect(planApplyBody({ ...drawing(), plan_hash })).toBeNull();
    expect(planApplyBody({ ...drawing(), base_version_id: '' })).toBeNull();
    const unresolved = drawing();
    unresolved.plan.questions = ['Which unit?'];
    expect(planApplyBody(unresolved)).toBeNull();
    expect(planApplyBody({ ...drawing(), base_version_id: '11bb22cc-3344-4555-8666-778899aabbcc' })).toBeNull();
  });
  it('keeps workflow numeric-version guards and historical plans without a hash', () => {
    expect(planApplyBody({ kind: 'workflow_edit', base_version: 7, plan_hash: hash })).toEqual({ plan_hash: hash, expected_version: 7 });
    expect(planApplyBody({ kind: 'workflow_edit', base_version: 7 })).toEqual({ expected_version: 7 });
    expect(planApplyBody({ kind: 'workflow_edit', base_version: 0, plan_hash: hash })).toBeNull();
    expect(planApplyBody({ kind: 'workflow_create', plan_hash: hash })).toEqual({ plan_hash: hash });
    expect(planApplyBody({ kind: 'workflow_create' })).toEqual({});
  });
  it('binds assembly application to its base and never supplies removal consent by default', () => {
    const plan = { kind: 'assembly_modify' as const, base_revision_id: head, plan_hash: hash, removals: [{ op: 'remove_instance', instance: 'A-1' }] };
    expect(planApplyBody(plan)).toBeNull();
    expect(planApplyBody(plan, true)).toEqual({ plan_hash: hash, expected_head: head, confirm_removals: true });
  });
});
