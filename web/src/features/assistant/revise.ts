// Generation supplement I (A-04 / IN-07 / IN-08): pure helpers for the spec_and_sources choice ("Generate a new specification version" /
// "Generate from the original specification"), the request card's lines about that choice and the inputs a request continued with, and the
// "Replace the file and continue" picker (which composer files can replace the ones the spec step could not read).

import type { AiOperation, AiRef, AiSpecChoice } from '../../api/assistant';
import type { SpecValue } from '../../api/orchestration';

/** Recoveries of a spec step failure another file can fix (server: services/spec_inputs.REPLACEABLE). */
export const REPLACEABLE_RECOVERIES = ['replace_source', 'fix_input'] as const;

/** Whether a failed assembly request can continue with other files: its spec step failed with a recovery a file fixes. */
export function canReplaceSource(op: Pick<AiOperation, 'kind' | 'status' | 'error'>): boolean {
  if (op.kind !== 'assembly_create' || op.status !== 'failed' || !op.error) return false;
  const d = (op.error.details ?? {}) as { failed_node?: string; recovery?: string };
  if (d.failed_node && d.failed_node !== 'spec') return false;
  const recovery = op.error.recovery ?? d.recovery;
  return !!recovery && (REPLACEABLE_RECOVERIES as readonly string[]).includes(recovery);
}

/** The file versions the request's spec step reads now: the latest input revision, else the request's own files. */
export function currentRequestFiles(op: Pick<AiOperation, 'request'>): string[] {
  const revs = op.request.input_revisions ?? [];
  if (revs.length) return [...revs[revs.length - 1].file_version_ids];
  return [op.request.target, ...(op.request.references ?? [])].filter((r): r is AiRef => !!r && r.kind === 'file_version').map((r) => r.object_id);
}

/** Composer files that could replace the unread ones: file versions not already read by the request. */
export function replaceCandidates(op: Pick<AiOperation, 'request'>, composerRefs: readonly AiRef[]): AiRef[] {
  const used = new Set(currentRequestFiles(op));
  const seen = new Set<string>();
  return composerRefs.filter((r) => {
    if (r.kind !== 'file_version' || used.has(r.object_id) || seen.has(r.object_id)) return false;
    seen.add(r.object_id);
    return true;
  });
}

/** Labels of the files a spec choice adds (revise) or leaves out (keep). */
export function choiceFiles(choice: AiSpecChoice): string[] {
  return choice.files.map((f) => f.label || f.file_version_id);
}

/** Display labels of the request's files by file version id (references, then the files added by input revisions). */
export function requestFileLabels(op: Pick<AiOperation, 'request'>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of [op.request.target, ...(op.request.references ?? [])]) {
    if (r && r.kind === 'file_version') out[r.object_id] = r.version_label ? `${r.title} ${r.version_label}` : r.title;
  }
  for (const rev of op.request.input_revisions ?? []) for (const a of rev.added) out[a.file_version_id] = `${a.name} v${a.version}`;
  return out;
}

/** The revision a spec value records (A-04): from which version, what was added / dropped, whether a note was read. */
export function specRevision(v: Pick<SpecValue, 'revision' | 'sources'>): {
  kind: 'revised' | 'replaced';
  of: number;
  added: string[];
  dropped: string[];
  note: boolean;
} | null {
  const r = v.revision;
  if (!r) return null;
  const labels: Record<string, string> = {};
  for (const s of v.sources ?? []) if (s.file_version_id) labels[s.file_version_id] = s.label || s.name || s.file_version_id;
  return {
    kind: r.reason === 'replace_source' ? 'replaced' : 'revised',
    of: r.of_version_no,
    added: r.added_files.map((a) => a.label || labels[a.file_version_id] || a.file_version_id),
    dropped: r.dropped_files.map((d) => d.label || d.file_version_id),
    note: !!r.note_source,
  };
}
