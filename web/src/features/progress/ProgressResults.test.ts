import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ProgressDrawing, ProgressUnit } from '../../api/progress';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ProgressResults, progressParts } from './ProgressResults';
import { run, stage, unit } from './test-fixtures';

const revision = (id: string) => ({ id, part_id: `part-${id}`, label: `R${id}`, status: 'ready', size: null, operations: [] });
const part = (n: number, ready: boolean) => unit(`definition:D-${n}`, ready ? 'built' : 'pending', {
  title: `Part ${n}`, quantity: n === 4 ? 4 : 1, revision: ready ? revision(`${n}`) : null,
});
function render(units: ProgressUnit[], language = 'en') {
  vi.stubGlobal('localStorage', { getItem: () => language });
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
    h(ProgressResults, { progress: run('running', [stage('assembly', 'running', units)]) }))));
}
afterEach(() => vi.unstubAllGlobals());

describe('visible execution results', () => {
  it('progresses from one to four saved parts without losing queued units or exact revision links', () => {
    const first = render([1, 2, 3, 4].map((n) => part(n, n === 1)));
    expect(first.match(/class="prog-result-card/g)).toHaveLength(4);
    expect(first).toContain('/parts/part-1/model?rev=1');
    expect(first).not.toContain('/parts/part-2/model');
    expect(first).toContain('Not done yet');
    const complete = render([1, 2, 3, 4].map((n) => part(n, true)));
    for (const n of [1, 2, 3, 4]) expect(complete).toContain(`/parts/part-${n}/model?rev=${n}`);
    expect(complete).toContain('×4');
    expect(complete).toContain('Result previews');
    expect(complete).not.toContain('<details');
  });

  it('shows failed and missing results honestly while preserving saved model access', () => {
    const html = render([part(1, true), { ...part(2, false), status: 'failed' }, { ...part(3, false), status: 'built' }]);
    expect(html).toContain('Failed');
    expect(html).toContain('No saved model');
    expect(html).toContain('/parts/part-1/model?rev=1');
    expect(html).not.toContain('accepted');
    expect(render([part(1, true)], 'zh')).toContain('生成预览');
  });

  it('keeps keys stable while parts arrive and replaces run/revision identity without duplicate previews', () => {
    const p = run('running', [stage('assembly', 'running', [part(1, false), part(2, false)])]);
    const before = progressParts(p).map((item) => item.key);
    p.stages[0].units = [part(1, true), part(2, true)];
    expect(progressParts(p).map((item) => item.key)).toEqual(before);
    p.stages.push(stage('repeated', 'succeeded', [part(1, true)]));
    expect(progressParts(p)).toHaveLength(2);
    p.run_id = 'new-run';
    expect(progressParts(p)[0].key).not.toBe(before[0]);
  });

  it.each([
    ['pending', 'Not started'], ['running', 'previews appear after the package is saved'], ['failed', 'Generation failed'],
  ])('labels an unsaved drawing stage %s without inventing preview images', (status, expected) => {
    vi.stubGlobal('localStorage', { getItem: () => 'en' });
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
      h(ProgressResults, { progress: run(status, [stage('drawing', status)]) }))));
    expect(html).toContain(expected);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('SVG page saved');
  });

  it('keeps the bounded-results notice visible outside collapsed diagnostics', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'en' });
    const progress = run('succeeded', [stage('assembly', 'succeeded', [part(1, true)])]);
    progress.truncated = true;
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ProgressResults, { progress }))));
    expect(html).toContain('Only part of a very long record is shown.');
    expect(html).not.toContain('<details');
  });

  it('renders a reused drawing once while preserving both source nodes in the saved-output check', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'en' });
    const progress = run('succeeded', [stage('first', 'succeeded', [], { node_type: 'drawing' }), stage('second', 'succeeded', [], { node_type: 'drawing' })]);
    progress.drawings = ['first', 'second'].map((node_id) => ({ kind: 'drawing', node_id, id: 'shared-drawing', model_revision_id: 'r1', manifest_sha256: null }));
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ProgressResults, { progress }))));
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(1);
    expect(html).not.toContain('No saved preview pages');
  });

  it('excludes only the represented successful revision and retains failed, pending and other outputs', () => {
    const ready = part(1, true);
    const progress = run('running', [stage('model', 'running', [ready,
      { ...ready, key: 'model:prior', revision: { ...revision('1'), id: 'prior-r1' } },
      { ...ready, key: 'model:other-part', revision: { ...revision('1'), part_id: 'other-part' } },
      { ...ready, key: 'model:failed', status: 'failed' },
      { ...ready, key: 'model:pending', status: 'pending' },
      { ...ready, key: 'model:error', error: { code: 'save_failed' } },
      { ...part(2, false), status: 'built' },
    ])]);
    const represented = [{ runId: 'r', partId: 'part-1', revisionId: '1' }];
    expect(progressParts(progress, represented).map((entry) => entry.unit.key)).toEqual([
      'model:prior', 'model:other-part', 'model:failed', 'model:pending', 'model:error', 'definition:D-2',
    ]);
    expect(progressParts(progress, [{ ...represented[0], runId: 'other-run' }])).toHaveLength(7);
  });

  it('keeps truncated notice and drawing associations even when the known model is represented elsewhere', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'en' });
    const progress = run('succeeded', [stage('model', 'succeeded', [part(1, true)]),
      stage('first', 'succeeded', [], { node_type: 'drawing' }), stage('second', 'succeeded', [], { node_type: 'drawing' })]);
    progress.truncated = true;
    progress.drawings = ['first', 'second'].map((node_id) => ({ kind: 'drawing', node_id, id: 'shared-drawing', model_revision_id: '1', manifest_sha256: null }));
    const representedParts = [{ runId: 'r', partId: 'part-1', revisionId: '1' }];
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ProgressResults, { progress, representedParts }))));
    expect(html).not.toContain('prog-result-card');
    expect(html).toContain('Only part of a very long record is shown.');
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(1);
    expect(html).not.toContain('No saved preview pages');
    const onlyTruncation = { ...progress, stages: [progress.stages[0]], drawings: [] };
    const notice = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
      h(ProgressResults, { progress: onlyTruncation, representedParts }))));
    expect(notice).toContain('Only part of a very long record is shown.');
    expect(notice).not.toContain('prog-result-card');
  });

  it('suppresses only the represented drawing gallery while retaining original associations and unresolved stages', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'en' });
    const drawing: Extract<ProgressDrawing, { kind: 'drawing' }> = { kind: 'drawing', node_id: 'first',
      id: 'drawing-1', model_revision_id: 'r1', manifest_sha256: 'manifest-1' };
    const progress = run('partial', [stage('first', 'succeeded', [], { node_type: 'drawing' }),
      stage('reused', 'succeeded', [], { node_type: 'drawing' }), stage('drawing', 'failed'),
      stage('pending', 'pending', [], { node_type: 'drawing' }), stage('model', 'running', [{ ...part(1, true), status: 'failed' }, part(2, false)])]);
    progress.drawings = [drawing, { ...drawing, node_id: 'reused' }];
    progress.truncated = true;
    const representedDrawings = [{ runId: 'r', drawing }];
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
      h(ProgressResults, { progress, representedDrawings }))));
    expect(html).not.toContain('Loading saved drawing pages');
    expect(html).not.toContain('No saved preview pages');
    expect(html).toContain('Generation failed');
    expect(html).toContain('Not started');
    expect(html).toContain('Only part of a very long record is shown.');
    expect(html.match(/class="prog-result-card/g)).toHaveLength(2);
    const otherRun = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
      h(ProgressResults, { progress, representedDrawings: [{ runId: 'other-run', drawing }] }))));
    expect(otherRun.match(/Loading saved drawing pages/g)).toHaveLength(1);
  });

  it('never suppresses other drawing identities, source revisions, manifests or assembly pages', () => {
    const drawing: Extract<ProgressDrawing, { kind: 'drawing' }> = { kind: 'drawing', node_id: 'drawing',
      id: 'drawing-1', model_revision_id: 'r1', manifest_sha256: 'manifest-1' };
    const progress = { ...run('succeeded', []), drawings: [drawing, { ...drawing, id: 'drawing-2' },
      { ...drawing, model_revision_id: 'r2' }, { ...drawing, manifest_sha256: 'another-manifest' },
      { kind: 'assembly_drawing' as const, id: 'drawing-1', node_id: 'assembly', assembly_revision_id: 'a1',
        manifest_sha256: 'manifest-1', completeness: 'partial' }] };
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
      h(ProgressResults, { progress, representedDrawings: [{ runId: 'r', drawing }] }))));
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(4);
  });
});
