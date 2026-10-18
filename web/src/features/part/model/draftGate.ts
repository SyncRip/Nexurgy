// Unsaved editor drafts vs. a link that asks for a revision (?rev=, e.g. "View"
// on an assistant result). The browser keeps one draft per part; opening
// another revision and editing it would overwrite that draft. So a draft of a
// different revision (or of a blank design) is offered first — restore or
// discard — and the linked revision opens only after a discard.

import type { ModelRevision } from '../../../api/types';
import { opsEqual } from '../../editor/ops';
import type { StoredDraft } from '../../editor/store';

export type DraftHold = { kind: 'blank' } | { kind: 'rev'; id: string };

/**
 * What to open first when the link asks for `askedId` while `draft` is kept:
 * the draft's own revision (or the blank editor) so its restore / discard
 * offer shows. Null = nothing to hold back (no draft, the same revision, a
 * draft without edits, or one whose revision is gone and cannot be offered).
 */
export function draftBeforeLink(
  draft: Pick<StoredDraft, 'revisionId' | 'ops'> | null,
  askedId: string,
  revisions: readonly Pick<ModelRevision, 'id' | 'ops'>[],
): DraftHold | null {
  if (!draft || draft.revisionId === askedId) return null;
  if (draft.revisionId === null) return opsEqual(draft.ops, []) ? null : { kind: 'blank' };
  const rev = revisions.find((r) => r.id === draft.revisionId);
  if (!rev || opsEqual(draft.ops, rev.ops ?? [])) return null;
  return { kind: 'rev', id: rev.id };
}
