import { createElement as h, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RunOutputRef, WorkflowRun } from '../../api/types';
import { I18nProvider } from '../../i18n/I18nProvider';
import { RunIoPanels } from './RunContext';

const outputs: RunOutputRef[] = ['A', 'B'].map((suffix) => ({
  node_id: 'assembly', output_key: 'parts', attempt: 1, asset_version_id: `version-${suffix}`,
  version: { id: `version-${suffix}`, asset_id: `asset-${suffix}`, asset_type: 'model', asset_name: `Part ${suffix}`, label: 'R1' },
}));

function runWithOutputs(savedOutputs: RunOutputRef[]): WorkflowRun {
  return {
    id: 'run', project_id: 'project', part_id: null, workflow_id: 'workflow', workflow_version: 1,
    origin: 'workflow', title: 'Two-part assembly', status: 'succeeded', cancel_requested: false,
    node_counts: { succeeded: 1 }, created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z',
    started_at: null, finished_at: null, nodes: [], error: null, outputs: savedOutputs,
    graph: { nodes: [{ id: 'assembly', type: 'assembly_build', position: { x: 0, y: 0 }, params: {} }], edges: [], groups: [] },
  };
}

function listRows(node: ReactNode): ReactElement[] {
  if (Array.isArray(node)) return node.flatMap(listRows);
  if (!isValidElement<{ children?: ReactNode }>(node)) return [];
  return node.type === 'li' ? [node] : listRows(node.props.children);
}

function renderOutputs(savedOutputs: RunOutputRef[]) {
  let rows: ReactElement[] = [];
  function Capture() {
    // Inspect the actual component elements: static HTML alone drops React keys.
    const tree = RunIoPanels({ run: runWithOutputs(savedOutputs) });
    rows = listRows(tree);
    return tree;
  }
  vi.stubGlobal('localStorage', { getItem: () => 'en' });
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(Capture))));
  return { html, keys: rows.map((row) => row.key) };
}

afterEach(() => vi.unstubAllGlobals());

describe('saved run output identities', () => {
  it('keeps two R1 part revisions from one node distinct and links to each exact version', () => {
    const rendered = renderOutputs(outputs);
    expect(rendered.keys).toHaveLength(2);
    expect(rendered.keys.every((key) => key !== null)).toBe(true);
    expect(new Set(rendered.keys).size).toBe(2);
    expect(rendered.html.match(/<li>/g)).toHaveLength(2);
    expect(rendered.html.match(/>R1<\/span>/g)).toHaveLength(2);
    for (const suffix of ['A', 'B']) {
      expect(rendered.html).toContain(`Part ${suffix}`);
      expect(rendered.html).toContain(`/assets/asset-${suffix}?version=version-${suffix}`);
    }
    expect(renderOutputs([...outputs].reverse()).keys).toEqual([...rendered.keys].reverse());
  });

  it('retains stored identity when attempt metadata or version availability changes', () => {
    const initial = renderOutputs(outputs);
    const refreshed = renderOutputs(outputs.map((output) => ({ ...output, attempt: 2, version: null })));
    expect(refreshed.keys).toEqual(initial.keys);
    expect(refreshed.html.match(/<li>/g)).toHaveLength(2);
    expect(refreshed.html).not.toContain('/assets/');
  });
});
