// S5 components rendered to static markup (no DOM needed): the policy / source note with the consequence of an
// explicit switch, and the batch-confirm list (grouped by owner, state chips, per-value buttons only where a person
// still has to decide).

import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { UpstreamAssumption, UpstreamAssumptions } from '../../api/turbo';
import { I18nProvider } from '../../i18n/I18nProvider';
import { PolicySourceNote } from './PolicySource';
import { UpstreamAssumptionList } from './UpstreamConfirm';

const html = (el: ReactElement) => renderToStaticMarkup(createElement(I18nProvider, null, el));
const text = (markup: string) => markup.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ');

const specSource = { type: 'engineering_spec_version', id: 'v1', label: 'SP-3 v2', execution_policy: 'turbo' as const };

describe('PolicySourceNote', () => {
  it('shows an inherited Turbo policy with its source and nothing to confirm', () => {
    const out = text(html(createElement(PolicySourceNote, { resolved: { policy: 'turbo', source: 'inherited' }, sourceVersion: specSource,
      upstream: { unconfirmed: 2, corrected: 0, confirmed: 0, pending: 2, blocked: false } })));
    expect(out).toContain('Policy source');
    expect(out).toContain('Turbo');
    expect(out).toContain('Inherited from SP-3 v2');
    expect(out).not.toContain('Confirm these assumptions');
  });

  it('explains that standard over a Turbo spec needs the N upstream values confirmed, with the entry', () => {
    const markup = html(createElement(PolicySourceNote, { resolved: { policy: 'standard', source: 'explicit' }, sourceVersion: specSource,
      upstream: { unconfirmed: 3, corrected: 0, confirmed: 1, pending: 3, blocked: false }, onConfirm: () => {}, onFollow: () => {} }));
    const out = text(markup);
    expect(out).toContain('Chosen for this request');
    expect(out).toContain('SP-3 v2 ran in Turbo');
    expect(out).toContain('Standard will require confirming the 3 upstream assumption(s) first.');
    expect(out).toContain('Confirm these assumptions');
    expect(out).toContain('Follow the source');
    expect(markup).toContain('data-source="explicit"');
  });
});

const a = (over: Partial<UpstreamAssumption>): UpstreamAssumption => ({
  key: 'engineering_spec_version:v1#A-01',
  id: 'A-01',
  subject: 'D-02.length',
  field: 'length',
  adopted: 20,
  unit: 'mm',
  author: 'ai_turbo',
  state: 'unconfirmed',
  owner: { type: 'engineering_spec_version', id: 'v1', label: 'SP-3 v2' },
  basis: 'typical sleeve length',
  alternatives: [22],
  ...over,
});

describe('UpstreamAssumptionList (batch confirm)', () => {
  const listing: Pick<UpstreamAssumptions, 'items' | 'counts'> = {
    items: [
      a({}),
      a({ key: 'assembly_revision:r1#A-02', id: 'A-02', subject: 'M-01', field: 'a.datum', adopted: 'F-01.axis', unit: null, author: 'rule',
        owner: { type: 'assembly_revision', id: 'r1', label: 'GEN-02 A1' }, state: 'confirmed', by: { id: 'u1', name: 'Dana Designer' } }),
      a({ key: 'model_revision:m1#A-03', id: 'A-03', owner: { type: 'model_revision', id: 'm1', label: 'U-SIM R1' }, state: 'corrected', value: 22 }),
    ],
    counts: { unconfirmed: 1, confirmed: 1, corrected: 1 },
  };

  it('lists every value under its owner with its state, who confirmed and what was corrected', () => {
    const out = text(html(createElement(UpstreamAssumptionList, { listing, onConfirmOne: () => {} })));
    expect(out).toContain('1 to confirm · 1 confirmed · 1 corrected');
    expect(out.indexOf('Spec SP-3 v2')).toBeLessThan(out.indexOf('Assembly GEN-02 A1'));
    expect(out.indexOf('Assembly GEN-02 A1')).toBeLessThan(out.indexOf('Part U-SIM R1'));
    expect(out).toContain('20 mm');
    expect(out).toContain('AI · Turbo');
    expect(out).toContain('by Dana Designer');
    expect(out).toContain('corrected to 22 mm: needs a new version');
    // one button: only the value still waiting for a person
    expect(out.match(/ Confirm /g)?.length).toBe(1);
  });

  it('is read-only without a confirm handler and says when nothing waits', () => {
    expect(text(html(createElement(UpstreamAssumptionList, { listing })))).not.toMatch(/ Confirm /);
    expect(text(html(createElement(UpstreamAssumptionList, { listing: { items: [], counts: { unconfirmed: 0, confirmed: 0, corrected: 0 } } }))))
      .toContain('Nothing upstream waits for a person.');
  });
});
