import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SpecValue } from '../../api/orchestration';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ChainValueView } from './ChainValue';

afterEach(() => vi.unstubAllGlobals());

function specCard(derived: SpecValue['derived'], lang = 'en'): string {
  vi.stubGlobal('localStorage', { getItem: () => lang });
  const value: SpecValue = {
    type: 'engineering_spec', spec_id: 'spec', spec_version_id: 'version', code: 'SP-5', title: 'Lift platform',
    version_no: 2, status: 'ready', execution_policy: 'standard', simulated: false,
    counts: { definitions: 12, instances: 17, facts: 5 }, path: '/specs/spec?version=version',
    open_questions: [], assumptions: [], derived,
  };
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ChainValueView, { value }))));
}

describe('derived engineering spec card', () => {
  it('describes authorized design as unconfirmed AI choices in English', () => {
    const html = specCard({ rule: 'authorized_engineering_design' });
    expect(html).toContain('Engineering design added under explicit human authorization');
    expect(html).toContain('AI choices are not yet confirmed by a person');
    expect(html).not.toContain('put under the root assembly');
  });

  it('describes authorized design accurately in Chinese', () => {
    const html = specCard({ rule: 'authorized_engineering_design' }, 'zh');
    expect(html).toContain('依据用户明确授权补充工程设计');
    expect(html).toContain('AI 选择尚未经人工确认');
    expect(html).not.toContain('挂到根装配下');
  });

  it('keeps root-placement and alias-resolution explanations for their own rules', () => {
    expect(specCard({ rule: 'hang_unplaced_under_root', definitions: ['D-04'] }))
      .toContain('definitions that sat nowhere (D-04) were put under the root assembly');
    const aliases = specCard({ rule: 'turbo_alias_resolution', merged: [{ alias: 'D-02', into: 'D-01' }], hung: [] });
    expect(aliases).toContain('Derived by the Turbo structure decision');
    expect(aliases).not.toContain('explicit human authorization');
  });
});
