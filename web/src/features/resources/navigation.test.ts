import { describe, expect, it } from 'vitest';
import { catalogNeighbors } from './navigation';

describe('Sequential material preview', () => {
  const rows = [{ key: 'project-b:file-3' }, { key: 'project-a:file-1' }, { key: 'project-a:file-2' }];
  it('finds adjacent items across projects in filtered order without sorting again by name', () => {
    expect(catalogNeighbors(rows, rows[1].key)).toEqual({ index: 1, total: 3, previous: rows[0].key, next: rows[2].key });
  });
  it('does not wrap at either end or produce out-of-range items', () => {
    expect(catalogNeighbors(rows, rows[0].key).previous).toBeNull();
    expect(catalogNeighbors(rows, rows[2].key).next).toBeNull();
  });
  it('does not navigate to unrelated material when an external link is outside the filtered results', () => {
    expect(catalogNeighbors(rows, 'filtered-out')).toEqual({ index: -1, total: 3, previous: null, next: null });
    expect(catalogNeighbors([], null)).toEqual({ index: -1, total: 0, previous: null, next: null });
  });
});
