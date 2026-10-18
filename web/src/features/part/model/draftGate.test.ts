import { describe, expect, it } from 'vitest';
import type { ModelOp } from '../../../api/types';
import { draftBeforeLink } from './draftGate';

const hole = { id: 'op1', type: 'hole' } as unknown as ModelOp;
const pocket = { id: 'op2', type: 'pocket' } as unknown as ModelOp;
const revs = [
  { id: 'r3', ops: [hole, pocket] },
  { id: 'r2', ops: [hole] },
];

describe('a link to a revision vs. an unsaved draft', () => {
  it('opens the draft of another revision first', () => {
    expect(draftBeforeLink({ revisionId: 'r2', ops: [hole, pocket] }, 'r3', revs)).toEqual({ kind: 'rev', id: 'r2' });
  });
  it('opens the blank editor first for a draft of a blank design', () => {
    expect(draftBeforeLink({ revisionId: null, ops: [hole] }, 'r3', revs)).toEqual({ kind: 'blank' });
  });
  it('holds nothing back when the draft is of the asked revision, has no edits, or cannot be offered', () => {
    expect(draftBeforeLink(null, 'r3', revs)).toBeNull();
    expect(draftBeforeLink({ revisionId: 'r3', ops: [hole] }, 'r3', revs)).toBeNull();
    expect(draftBeforeLink({ revisionId: 'r2', ops: [hole] }, 'r3', revs)).toBeNull();
    expect(draftBeforeLink({ revisionId: null, ops: [] }, 'r3', revs)).toBeNull();
    expect(draftBeforeLink({ revisionId: 'gone', ops: [hole] }, 'r3', revs)).toBeNull();
  });
});
