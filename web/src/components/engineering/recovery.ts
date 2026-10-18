// S3 failure info (agent execution layer design §3.1): every multi-step AI task fails as
// {stage, code, message, retryable, recovery}. `recovery` is a fixed code; the page offers "Continue" for the codes that
// continue from the checkpoint and shows what to do otherwise. Pure helpers, unit-tested in recovery.test.ts.

import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import type { TFunction } from '../../i18n/I18nProvider';

export const RECOVERY_CODES = [
  'continue_extract',
  'retry_design',
  're_extract',
  'split_sources',
  'retry_definition',
  'answer_questions',
  'configure_model',
  'fix_spec',
  'retry_sheet',
  // A Turbo protocol error (a step asked inside a Turbo run): continuing would ask again.
  'run_standard',
  // Generation supplement A (spec pre-check): a referenced source was not read / the material has no part to model.
  'replace_source',
  'fix_input',
] as const;
export type RecoveryCode = (typeof RECOVERY_CODES)[number];

/** Codes whose action is "Continue": what is done is kept, only the failed / missing part is redone. */
export const CONTINUE_CODES: readonly RecoveryCode[] = ['continue_extract', 'retry_design', 'retry_definition', 'retry_sheet'];

export interface FailureInfo {
  stage?: string | null;
  code?: string | null;
  message?: string | null;
  retryable?: boolean | null;
  /** A recovery code; failures recorded before S3 may carry a sentence here instead. */
  recovery?: string | null;
  recovery_message?: string | null;
}

export function isRecoveryCode(v: unknown): v is RecoveryCode {
  return typeof v === 'string' && (RECOVERY_CODES as readonly string[]).includes(v);
}

export interface ContinueState {
  /** The failure has recovery info: the action is shown (enabled or with the reason it is not). */
  show: boolean;
  enabled: boolean;
  /** Why it is disabled (translated), null when enabled. */
  reason: string | null;
}

/** The Continue action of a failure. ``also``: codes that continue on this page only (the AI assembly card re-runs the
 * spec step for ``re_extract``; a spec page has no run to continue). */
export function continueState(t: TFunction, f: FailureInfo | null | undefined, blocked?: string | null,
                              also: readonly RecoveryCode[] = []): ContinueState {
  if (!f || !f.recovery) return { show: false, enabled: false, reason: null };
  if (blocked) return { show: true, enabled: false, reason: blocked };
  const code = isRecoveryCode(f.recovery) ? f.recovery : null;
  const continues = !!code && (CONTINUE_CODES.includes(code) || also.includes(code));
  if (continues && f.retryable) return { show: true, enabled: true, reason: null };
  if (continues) return { show: true, enabled: false, reason: t('recovery.disabled.notRetryable') };
  return { show: true, enabled: false, reason: recoveryText(t, f) || t('recovery.disabled.notRetryable') };
}

/** What to do next, in the UI language: the code's text, else the server's sentence (older failures). */
export function recoveryText(t: TFunction, f: FailureInfo | null | undefined): string {
  if (!f) return '';
  const key = `recovery.code.${f.recovery ?? ''}`;
  if (isRecoveryCode(f.recovery) && hasKey(key)) return t(key as MessageKey);
  return f.recovery_message || (f.recovery && !isRecoveryCode(f.recovery) ? f.recovery : '') || '';
}

/** A reason code of the server (e.g. drawing packages: already_continued / overlay / other_generator), translated. */
export function blockedReason(t: TFunction, code: string | null | undefined): string | null {
  if (!code) return null;
  const key = `recovery.disabled.${code}`;
  return hasKey(key) ? t(key as MessageKey) : code;
}
