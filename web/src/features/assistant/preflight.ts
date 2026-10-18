// Generation reliability K (design .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.3): what a send would do, as
// the composer shows it before sending — "Will execute: <operation> · target <object> · n references" — and whether the send is held back
// (missing is not empty) with the entry that fixes it. Pure helpers (unit-tested in preflight.test.ts).

import type { AiPreflight, AiPreflightMissing, AiPreflightTarget, AiSendBody } from '../../api/assistant';

/** The preflight body of a send (the same body without its client_request_id). */
export function preflightBody(body: AiSendBody): Omit<AiSendBody, 'client_request_id'> {
  const { client_request_id: _, ...rest } = body;
  return rest;
}

/** A stable key of what a preflight answered for: a new answer is needed when any of it changes. */
export function preflightKey(body: Omit<AiSendBody, 'client_request_id'>): string {
  return JSON.stringify([body.text, body.operation, body.target_key ?? null, body.reference_keys ?? [], body.task_id ?? null,
    body.execution_policy ?? null, !!body.as_material, body.auto_references ?? true, body.revise ?? null]);
}

/** "Base plate R3" — the object's label and version; "" when there is none. */
export function targetLabel(t: Pick<AiPreflightTarget, 'label' | 'version'> | null | undefined): string {
  if (!t) return '';
  return [t.label, t.version].filter((x) => x && String(x).trim()).join(' ').trim();
}

/** A preflight holds the send back while something is missing (the same checks a send would fail with). Attachment and
 * spec-and-sources problems are left to the send: it refuses them with 422 before anything is written, and the
 * attachment notice offers their choices (which file, revise or keep the spec) from that answer. */
export function blocksSend(p: Pick<AiPreflight, 'missing'> | null | undefined): boolean {
  return ownMissing(p).length > 0;
}

/** The entry of a missing item the composer can act on directly: add a suggested object, pick a file, attach one. */
export type MissingAction =
  | { kind: 'add'; ref: { kind: string; object_id: string }; label: string }
  | { kind: 'files' }
  | { kind: 'choose'; candidates: NonNullable<AiPreflightMissing['entry']['candidates']> }
  | { kind: 'none' };

export function missingAction(m: AiPreflightMissing): MissingAction {
  const e = m.entry ?? { action: '' };
  if (typeof e.object_id === 'string' && typeof e.kind === 'string') return { kind: 'add', ref: { kind: e.kind, object_id: e.object_id }, label: String(e.label ?? '') };
  if (e.action === 'choose_file' && Array.isArray(e.candidates) && e.candidates.length) return { kind: 'choose', candidates: e.candidates };
  if (e.action === 'attach_file') return { kind: 'files' };
  return { kind: 'none' };
}

/** Codes the preflight shows itself; attachment / spec-and-sources problems are explained by the attachment notice. */
export const NOTICE_CODES = ['attachment_required', 'attachment_ambiguous', 'spec_and_sources'] as const;

export function ownMissing(p: Pick<AiPreflight, 'missing'> | null | undefined): AiPreflightMissing[] {
  return (p?.missing ?? []).filter((m) => !(NOTICE_CODES as readonly string[]).includes(m.code));
}
