import { createElement as h, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AssemblyCoverage } from '../../api/orchestration';
import { I18nProvider } from '../../i18n/I18nProvider';
import { CoverageMarks, CoverageView } from './Coverage';
import { coverageOf } from './coverageModel';

const coverage: AssemblyCoverage = {
  status: 'none',
  target: { basis: 'spec', definitions: 6, instances: 16, subassemblies: 0, subassembly_instances: 0, recognized_definitions: 6 },
  built: { definitions: 0 },
  placed: { definitions: 0, instances: 0 },
  arranged: { definitions: 0, instances: 0 },
  missing: [{ stage: 'modeling', key: 'D-01', name: 'Housing', kind: 'part', code: 'model_failed', reason: 'No valid geometry', quantity: 1, instances: ['I-01'], recovery: 'retry_definition' }],
  missing_counts: { modeling: 1 },
};

const render = (element: ReactElement, language = 'en') => {
  vi.stubGlobal('localStorage', { getItem: () => language });
  return renderToStaticMarkup(h(I18nProvider, null, element));
};
afterEach(() => vi.unstubAllGlobals());

describe('coverage status presentation', () => {
  it('does not claim usable parts when the build produced zero valid definitions', () => {
    const en = render(h(CoverageMarks, { coverage }));
    expect(en).toContain('No parts or assembly generated');
    expect(en).not.toContain('Single parts usable');
    const zh = render(h(CoverageMarks, { coverage }), 'zh');
    expect(zh).toContain('未生成零件及总装');
    expect(zh).not.toContain('单件可用');
  });

  it('preserves the parts-available mark when valid definitions exist without an assembly', () => {
    const built = { ...coverage, built: { definitions: 2 } };
    expect(render(h(CoverageMarks, { coverage: built }))).toContain('Single parts usable, no assembly');
    expect(render(h(CoverageMarks, { coverage: built }), 'zh')).toContain('单件可用，总装未形成');
  });

  it.each([undefined, null, {}, { definitions: null }, { definitions: '2' }, { definitions: -1 }, { definitions: 1.5 }, { definitions: NaN }, { definitions: Infinity }])(
    'keeps missing or invalid legacy counts unknown (%j)',
    (built) => {
      const legacy = coverageOf({ coverage: { ...coverage, built } });
      const en = render(h(CoverageMarks, { coverage: legacy }));
      expect(en).toContain('No assembly; part status unknown');
      expect(en).not.toContain('Single parts usable');
      expect(en).not.toContain('No parts or assembly generated');
      const zh = render(h(CoverageMarks, { coverage: legacy }), 'zh');
      expect(zh).toContain('总装未形成，零件状态未知');
      expect(zh).not.toContain('单件可用');
      expect(zh).not.toContain('未生成零件及总装');
      expect(legacy).not.toBeNull();
      if (legacy) {
        const view = render(h(CoverageView, { coverage: legacy }));
        expect(view).toContain('valid geometry Unknown; installed 0; failed 1');
        expect(view).toContain('No valid geometry');
        expect(render(h(CoverageView, { coverage: legacy }), 'zh')).toContain('有效几何 未知；已装入 0；失败 1');
      }
    },
  );

  it('keeps partial and complete assembly marks separate from AI decisions and verification', () => {
    const partial = render(h(CoverageMarks, { coverage: { ...coverage, status: 'partial', built: { definitions: 2 } }, aiDecisions: 3, unverified: true }));
    expect(partial).toContain('Partial assembly');
    expect(partial).toContain('Includes AI decisions (3)');
    expect(partial).toContain('Not verified');
    expect(partial).not.toContain('no assembly');
    const complete = render(h(CoverageMarks, { coverage: { ...coverage, status: 'complete', built: { definitions: 6 } } }));
    expect(complete).toBe('');
    expect(render(h(CoverageMarks, { coverage: null }))).toBe('');
  });

  it('retains target counts, missing part identity and its actual failure reason', () => {
    const html = render(h(CoverageView, { coverage }));
    expect(html).toContain('Target: 6 part definitions, 16 instances');
    expect(html).toContain('valid geometry 0; installed 0; failed 1');
    expect(html).toContain('Housing');
    expect(html).toContain('D-01');
    expect(html).toContain('model_failed');
    expect(html).toContain('No valid geometry');
    expect(html).toContain('What to do:');
  });
});
