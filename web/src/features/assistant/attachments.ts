// Generation supplement A (design .trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.5): attachment wording in the
// composer and what a spec step read. Pure helpers (unit-tested in attachments.test.ts).
//
// mentionsAttachment mirrors the server's services/attachments.mentions_attachment so the composer asks the resolve
// endpoint only for text that clearly points at an attached / uploaded file; the server always decides.

import type { AiAutoReference, AiRef } from '../../api/assistant';
import type { SpecSourceBrief } from '../../api/orchestration';
import type { Tone } from '../../components/ui/Badges';

const W = (word: string) => `(?<![A-Za-z0-9_])${word}(?![A-Za-z0-9_])`;

const ATTACH = new RegExp(
  [
    '刚刚?上传|新上传|已上传|上传的|附件|附上的|随附',
    '(?:这|那|该|此)\\s*(?:份|个|篇|张)?\\s*(?:文件|文档|资料|附件|说明书|说明文档|清单|图纸|markdown|md(?![A-Za-z0-9_]))',
    '\\.(?:md|markdown)(?![A-Za-z0-9_])',
    `${W('md')}\\s*(?:文件|文档)`,
    W('attach(?:ed|ment|ments)'),
    W('uploaded'),
    W('(?:this|that|the)\\s+(?:file|document|doc|markdown)'),
  ].join('|'),
  'i',
);
const MARKDOWN = new RegExp(W('markdown'), 'gi');
const MARKDOWN_FORMAT = /^\s*(?:格式|format|语法|syntax|表格|table)/i;
const MARKDOWN_AS = new RegExp(`(?:用|以|输出成?|写成|转成|转为|转换为|改成|${W('(?:in|as|to)')})\\s*$`, 'i');

/** The text clearly points at an attached / uploaded file ("Use the Markdown just uploaded", "attachment", ".md", "attached"); a
 * Markdown *format* request ("Output in Markdown format") does not. */
export function mentionsAttachment(text: string): boolean {
  const t = text ?? '';
  if (ATTACH.test(t)) return true;
  for (const m of t.matchAll(MARKDOWN)) {
    const at = m.index ?? 0;
    if (!MARKDOWN_FORMAT.test(t.slice(at + m[0].length, at + m[0].length + 8)) && !MARKDOWN_AS.test(t.slice(Math.max(0, at - 8), at))) return true;
  }
  return false;
}

export function hasFileReference(refs: Pick<AiRef, 'kind'>[]): boolean {
  return refs.some((r) => r.kind === 'file_version');
}

/** The first and the last non-empty line (where the instruction is): a pasted description mentioning "attachment" in its body
 * is material, not an instruction to bind a file (server: attachments.instruction_lines). */
export function instructionLines(text: string): string {
  const lines = (text ?? '').split('\n').filter((l) => l.trim());
  return [...new Set([...lines.slice(0, 1), ...lines.slice(-1)])].join('\n');
}

/** Ask the server which file the text means: attachment wording in the instruction and no file referenced yet. */
/** The composer asks the server only for a request that may build from files (the server applies the full rule:
 * assembly wording, no question, no other object as target, no spec version referenced). */
export function shouldResolveAttachment(text: string, refs: Pick<AiRef, 'kind'>[], operation = 'auto', target: Pick<AiRef, 'kind'> | null = null): boolean {
  if (operation !== 'auto' && operation !== 'assembly_create') return false;
  if (target && target.kind !== 'file_version') return false;
  if (refs.some((r) => r.kind === 'engineering_spec_version')) return false;
  return !!text.trim() && mentionsAttachment(instructionLines(text)) && !hasFileReference(refs);
}

/** "gen02.md v1" of a bound file / a candidate reference. */
export function fileLabel(r: Pick<AiAutoReference, 'name' | 'version'> | Pick<AiRef, 'title' | 'version_label'>): string {
  if ('name' in r) return `${r.name} v${r.version}`;
  return `${r.title}${r.version_label ? ` ${r.version_label}` : ''}`;
}

export type SourceState = 'read' | 'partial' | 'unread' | 'failed';

/** Read / partially read / referenced (not read) / read failed — an unknown state reads as not read, never as read. */
export function sourceState(s: Pick<SpecSourceBrief, 'read_status'>): SourceState {
  return s.read_status === 'read' || s.read_status === 'partial' || s.read_status === 'failed' ? s.read_status : 'unread';
}

export function sourceTone(state: SourceState): Tone {
  return state === 'read' ? 'ok' : state === 'partial' ? 'warn' : state === 'failed' ? 'err' : 'stale';
}

/** Sources a person gave (answers to spec questions are shown on the spec page, not here). */
export function givenSources(sources: SpecSourceBrief[] | undefined): SpecSourceBrief[] {
  return (sources ?? []).filter((s) => !s.answer_of);
}

/** Generation reliability K (design §2.4): a drawing source's coverage as the spec value shows it — chunk progress and
 * where every entity went, kept apart (all chunks sent does not mean every entity was read). */
export function coverageSummary(c: NonNullable<SpecSourceBrief['coverage']>): {
  frames: number; chunks: number; sent: number; total: number; inFrames: number; read: number; excluded: number;
  unexplained: number; quarantined: number; reasons: { reason: string; n: number }[];
} {
  const e = c.entities;
  const reasons = Object.entries(e.outside.excluded ?? {}).map(([reason, n]) => ({ reason, n })).filter((r) => r.n > 0);
  return {
    frames: c.frames, chunks: c.chunks ?? 0, sent: c.chunks_sent ?? 0, total: e.total, inFrames: e.in_frames, read: e.outside.read,
    excluded: reasons.reduce((a, r) => a + r.n, 0), unexplained: e.outside.unexplained, quarantined: e.quarantined, reasons,
  };
}
