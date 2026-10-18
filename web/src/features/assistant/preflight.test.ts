import { describe, expect, it } from 'vitest';
import type { AiPreflight, AiPreflightMissing } from '../../api/assistant';
import { blocksSend, missingAction, ownMissing, preflightBody, preflightKey, targetLabel } from './preflight';

const miss = (code: string, entry: AiPreflightMissing['entry'] = { action: 'change_request' }): AiPreflightMissing => ({ code, message: code, entry, expected: [] });

describe('composer preflight (generation reliability K, design §2.3)', () => {
  it('asks with the send body without its request id and re-asks only when the request changes', () => {
    const body = { client_request_id: 'r-1', text: '出图', operation: 'auto' as const, target_key: 'part:1', reference_keys: [] };
    expect(preflightBody(body)).toEqual({ text: '出图', operation: 'auto', target_key: 'part:1', reference_keys: [] });
    expect(preflightKey(preflightBody(body))).toBe(preflightKey(preflightBody({ ...body, client_request_id: 'r-2' })));
    expect(preflightKey(preflightBody(body))).not.toBe(preflightKey(preflightBody({ ...body, target_key: null })));
    expect(targetLabel({ label: '底板', version: 'R3' })).toBe('底板 R3');
    expect(targetLabel({ label: '', version: null })).toBe('');
  });

  it('holds the send back for target problems, leaves attachment choices to the send, and offers the fixing entry', () => {
    const p = (missing: AiPreflightMissing[]): Pick<AiPreflight, 'missing'> => ({ missing });
    expect(blocksSend(p([]))).toBe(false);
    expect(blocksSend(null)).toBe(false); // not answered yet: the server checks again on send
    expect(blocksSend(p([miss('part_required')]))).toBe(true);
    expect(blocksSend(p([miss('spec_and_sources'), miss('attachment_ambiguous')]))).toBe(false);
    expect(ownMissing(p([miss('attachment_required'), miss('target_required')])).map((m) => m.code)).toEqual(['target_required']);
    expect(missingAction(miss('target_required', { action: 'choose_target', kind: 'part', object_id: 'p-1', label: 'K-1 底板' }))).toEqual({
      kind: 'add', ref: { kind: 'part', object_id: 'p-1' }, label: 'K-1 底板' });
    expect(missingAction(miss('attachment_required', { action: 'attach_file' }))).toEqual({ kind: 'files' });
    expect(missingAction(miss('part_required', { action: 'add_reference', kind: 'part' }))).toEqual({ kind: 'none' });
  });
});
