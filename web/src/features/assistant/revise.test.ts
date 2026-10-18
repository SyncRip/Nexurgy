import { describe, expect, it } from 'vitest';
import type { AiOperation, AiRef } from '../../api/assistant';
import { canReplaceSource, currentRequestFiles, replaceCandidates, specRevision } from './revise';

const ref = (id: string, kind = 'file_version'): AiRef => ({ key: `${kind}:${id}`, kind, object_id: id, title: `${id}.md`, version_label: 'v1' }) as unknown as AiRef;
const op = (over: Partial<AiOperation> = {}): AiOperation =>
  ({
    kind: 'assembly_create',
    status: 'failed',
    error: { code: 'source_unread', message: '', recovery: 'replace_source', details: { failed_node: 'spec' } },
    request: { target: null, references: [ref('good'), ref('bad'), ref('sv', 'engineering_spec_version')] },
    ...over,
  }) as unknown as AiOperation;

describe('continue with other files (IN-08)', () => {
  it('is offered only for a spec step a file can fix', () => {
    expect(canReplaceSource(op())).toBe(true);
    expect(canReplaceSource(op({ error: { code: 'spec_no_parts', message: '', recovery: 'fix_input', details: {} } }))).toBe(true);
    expect(canReplaceSource(op({ error: { code: 'assembly_partial', message: '', recovery: 'retry_definition', details: { failed_node: 'assembly' } } }))).toBe(false);
    expect(canReplaceSource(op({ status: 'running' }))).toBe(false);
    expect(canReplaceSource(op({ kind: 'ask' }))).toBe(false);
  });

  it('offers the composer files the request does not read yet, after its latest input revision', () => {
    const composer = [ref('good'), ref('bad'), ref('fixed'), ref('fixed'), ref('p1', 'part')];
    expect(currentRequestFiles(op())).toEqual(['good', 'bad']);
    expect(replaceCandidates(op(), composer).map((r) => r.object_id)).toEqual(['fixed']);
    const continued = op({
      request: { ...op().request, input_revisions: [{ n: 1, file_version_ids: ['good', 'fixed'], added: [], dropped: ['bad'] }] },
    } as unknown as Partial<AiOperation>);
    expect(replaceCandidates(continued, composer).map((r) => r.object_id)).toEqual(['bad']);
  });
});

describe('spec revision line (A-04)', () => {
  it('reads what a new version of the same spec added, dropped and whether it has a note', () => {
    const sources = [{ id: 'S-3', kind: 'file_version', file_version_id: 'fx', name: 'fixed.md', label: 'fixed.md v1', version: 1, read_status: 'read', ranges: [], error: null }];
    const base = { of_version_id: 'v1', of_version_no: 1, note_chars: 0, answers_kept: 0 };
    expect(specRevision({ sources, revision: { ...base, reason: 'replace_source', added_files: [{ source: 'S-3', file_version_id: 'fx', label: '' }], dropped_files: [{ file_version_id: 'b', label: 'bad.md v1' }], note_source: null } } as never))
      .toEqual({ kind: 'replaced', of: 1, added: ['fixed.md v1'], dropped: ['bad.md v1'], note: false });
    expect(specRevision({ sources, revision: { ...base, reason: 'revise', added_files: [], dropped_files: [], note_source: 'S-4' } } as never)?.note).toBe(true);
    expect(specRevision({ sources, revision: null } as never)).toBeNull();
  });
});
