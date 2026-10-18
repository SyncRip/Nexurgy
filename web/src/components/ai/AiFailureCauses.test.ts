import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { AiFailureCauses, groupFailureCauses, readFailureCauses } from './AiFailureCauses';

const render = (details: unknown) => renderToStaticMarkup(createElement(I18nProvider, null, createElement(AiFailureCauses, { details })));

describe('partial assembly failure causes', () => {
  it('shows the observed service and placement errors while keeping a partial result honest', () => {
    const html = render({ causes: [
      { stage: 'modeling', definition: 'D-03', name: 'A05', code: 'timeout', failure_class: 'no_response',
        message: 'No response within 240 s', call_ids: ['model-call'] },
      { stage: 'modeling', definition: 'D-04', name: 'A06', code: 'network', failure_class: 'server_error',
        message: 'HTTP 503: No available accounts', call_ids: ['service-call'] },
      { stage: 'placement', code: 'network', failure_class: 'connection',
        message: 'Stream interrupted before any content arrived', instances: ['I-01'], call_ids: ['placement-call'] },
    ] });
    expect(html).toContain('Recorded failure causes');
    expect(html).toContain('A05');
    expect(html).toContain('A06');
    expect(html).toContain('No available accounts');
    expect(html).toContain('placement-call');
    expect(html).toContain('Check service availability before continuing');
    expect(html).not.toContain('quota');
    expect(html).not.toContain('Change the drawing');
  });

  it('keeps geometry failures separate and does not suggest a service outage', () => {
    const html = render({ causes: [{ stage: 'modeling', code: 'build_failed', failure_class: 'geometry_error', message: 'Invalid solid' }] });
    expect(html).toContain('geometry error');
    expect(html).toContain('Invalid solid');
    expect(html).not.toContain('Check service availability');
  });

  it('does not guess causes for older or malformed failures and renders untrusted messages as text', () => {
    expect(render({ code: 'assembly_partial', message: 'some parts missing' })).toBe('');
    expect(readFailureCauses(null)).toEqual([]);
    expect(readFailureCauses({ causes: 'timeout' })).toEqual([]);
    expect(readFailureCauses({ causes: [null, [], { code: 12 }] })).toEqual([]);
    const html = render({ causes: [{ code: 'network', message: '<script>alert(1)</script>', call_ids: ['one', { key: 'hidden' }] }] });
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('hidden');
    expect(html).toContain('&lt;script&gt;');
  });

  it('groups identical observed failures while retaining the affected definitions and call references', () => {
    const base = { stage: 'modeling', code: 'network', failure_class: 'server_error', message: 'HTTP 503' };
    const causes = readFailureCauses({ causes: [
      { ...base, definition: 'D-04', name: 'A06', call_ids: ['first'] },
      { ...base, definition: 'D-05', name: 'A07', call_ids: ['second'] },
    ] });
    expect(groupFailureCauses(causes)).toMatchObject([{ subjects: ['A06', 'A07'], callIds: ['first', 'second'] }]);
    expect(causes[0].callIds).toEqual(['first']);
  });
});
