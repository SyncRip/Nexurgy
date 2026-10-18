// S6 rendered: the chips people actually see (zh), via server-side rendering (no DOM needed).
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Verification } from '../../api/engineering';
import { I18nProvider } from '../../i18n/I18nProvider';
import { PhaseChip } from '../collaboration/common';
import { RunStatusChip } from '../workflow/icons';
import { VerificationChips } from './Turbo';

const g = globalThis as { localStorage?: unknown };
let saved: unknown;
beforeAll(() => {
  saved = g.localStorage;
  g.localStorage = { getItem: () => 'zh', setItem: () => undefined, removeItem: () => undefined };
});
afterAll(() => {
  g.localStorage = saved;
});

const html = (el: ReactElement) => renderToStaticMarkup(createElement(I18nProvider, null, el));
const text = (el: ReactElement) => html(el).replace(/<[^>]+>/g, ' ');

const verification = (v: Partial<Verification>): Verification => ({
  execution_policy: 'turbo',
  check_status: 'skipped_turbo',
  acceptance_status: 'not_requested',
  unverified: true,
  assumptions: [],
  pending_checks: [],
  unconfirmed_assumptions: [],
  ...v,
});

describe('VerificationChips (artifact level)', () => {
  it('check_status=failed + unverified=true reads Check failed, never Unverified', () => {
    const s = text(createElement(VerificationChips, { verification: verification({ check_status: 'failed' }) }));
    expect(s).toContain('检查失败');
    expect(s).not.toContain('未经校验');
    expect(s).toContain('Turbo');
  });
});

describe('run and node level', () => {
  it('a failed or partial run never shows the word Completed', () => {
    for (const status of ['failed', 'partial'] as const) {
      const s = text(createElement(RunStatusChip, { status, large: true }));
      expect(s).toContain('失败');
      expect(s).not.toContain('完成');
    }
    expect(html(createElement(RunStatusChip, { status: 'partial' }))).toContain('rs-partial');
  });

  it('a Turbo node whose check failed reads Check failed', () => {
    const s = text(createElement(PhaseChip, { phase: 'check_failed' }));
    expect(s).toContain('检查失败');
    expect(s).not.toContain('完成');
  });
});
