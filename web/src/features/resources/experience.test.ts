import { describe, expect, it } from 'vitest';
import type { KnowledgeCollection } from '../../api/knowledge';
import { mergeCollections, scopeLoad, sumTotals } from './experience';
import type { PerProjectEntry } from './perProject';

const col = (id: string, project: string, created: string | null) => ({ id, project_id: project, created_at: created }) as KnowledgeCollection;

describe('mergeCollections', () => {
  it('orders newest first across projects (created_at desc, id desc), undated last', () => {
    const merged = mergeCollections([
      [col('a2', 'A', '2026-10-10T00:00:00Z'), col('a1', 'A', '2026-10-01T00:00:00Z')],
      [col('b9', 'B', '2026-10-12T00:00:00Z'), col('b0', 'B', null), col('b5', 'B', '2026-10-10T00:00:00Z')],
    ]);
    expect(merged.map((c) => c.id)).toEqual(['b9', 'b5', 'a2', 'a1', 'b0']);
  });
});

describe('scope load and counts', () => {
  const entries = new Map<string, PerProjectEntry<number>>([
    ['A', { status: 'ok', data: 3 }],
    ['B', { status: 'error', error: new Error('x') }],
    ['C', { status: 'loading', data: 2 }],
  ]);

  it('reports loaded, pending and failed projects in scope order', () => {
    expect(scopeLoad(['A', 'B', 'C', 'D'], entries)).toEqual({ projects: 4, loaded: 1, pending: 2, failed: ['B'] });
  });

  it('sums the projects that answered and marks a failed project as partial, never as zero', () => {
    // C is reloading with a previous answer: it still counts.
    expect(sumTotals(['A', 'B', 'C'], entries)).toEqual({ count: 5, partial: true });
    expect(sumTotals(['A', 'C'], entries)).toEqual({ count: 5, partial: false });
  });

  it('is unknown while a project has never answered; an empty scope counts zero', () => {
    expect(sumTotals(['A', 'D'], entries)).toEqual({ count: null, partial: false });
    expect(sumTotals([], entries)).toEqual({ count: 0, partial: false });
    expect(sumTotals(['B'], entries)).toEqual({ count: null, partial: true });
  });
});
