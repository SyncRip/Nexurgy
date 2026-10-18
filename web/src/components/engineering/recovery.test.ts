import { describe, expect, it } from 'vitest';
import { en } from '../../i18n/en';
import { recoveryEn } from '../../i18n/messages/recovery.en';
import type { TFunction } from '../../i18n/I18nProvider';
import { blockedReason, CONTINUE_CODES, continueState, recoveryText } from './recovery';

const t = ((key: string, vars?: Record<string, string | number>) => {
  let s = (en as Record<string, string>)[key] ?? key;
  for (const [k, v] of Object.entries(vars ?? {})) s = s.replace(`{${k}}`, String(v));
  return s;
}) as TFunction;

describe('S3 failure recovery', () => {
  it('offers Continue only for a retryable failure whose recovery continues from the checkpoint', () => {
    for (const code of CONTINUE_CODES) {
      expect(continueState(t, { recovery: code, retryable: true })).toEqual({ show: true, enabled: true, reason: null });
      const off = continueState(t, { recovery: code, retryable: false });
      expect(off.show && !off.enabled && off.reason).toBe(recoveryEn['recovery.disabled.notRetryable']);
    }
    // another recovery: the action is shown disabled with what to do instead
    const q = continueState(t, { recovery: 'answer_questions', retryable: false });
    expect(q).toEqual({ show: true, enabled: false, reason: recoveryEn['recovery.code.answer_questions'] });
    expect(continueState(t, { recovery: 're_extract', retryable: true }).enabled).toBe(false);
    // no recovery info (e.g. a step that records none): nothing is shown
    expect(continueState(t, { code: 'capability_unavailable' }).show).toBe(false);
    expect(continueState(t, null).show).toBe(false);
    // a reason the page knows wins (already continued)
    expect(continueState(t, { recovery: 'retry_sheet', retryable: true }, 'done')).toEqual({ show: true, enabled: false, reason: 'done' });
  });

  it('shows the recovery text of a code and keeps the sentence of older failures', () => {
    expect(recoveryText(t, { recovery: 'continue_extract' })).toBe(recoveryEn['recovery.code.continue_extract']);
    expect(recoveryText(t, { recovery: 'Re-extract; check the key.' })).toBe('Re-extract; check the key.');
    expect(recoveryText(t, { recovery: 'mystery', recovery_message: 'Do this.' })).toBe('Do this.');
    expect(blockedReason(t, 'already_continued')).toBe(recoveryEn['recovery.disabled.already_continued']);
    expect(blockedReason(t, null)).toBeNull();
  });

  it('continues an interrupted design while keeping its completed extraction', () => {
    expect(continueState(t, { stage: 'engineering_design', recovery: 'retry_design', retryable: true }).enabled).toBe(true);
    expect(continueState(t, { recovery: 'retry_design', retryable: false }).enabled).toBe(false);
    expect(recoveryText(t, { recovery: 'retry_design' })).toContain('completed extraction');
  });
});
