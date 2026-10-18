import { describe, expect, it } from 'vitest';
import { sortProjects } from './sortProjects';

const rows = [
  { id: 'b', name: '轴座 10', code: 'P-10', updated_at: '2026-10-17T10:00:00+08:00' },
  { id: 'a', name: '轴座 2', code: 'P-2', updated_at: '2026-10-17T03:00:00Z' },
  { id: 'c', name: '轴座 1', code: 'P-1', updated_at: '2026-10-16T23:00:00Z' },
];
describe('Project list sorting', () => {
  it('sorts by actual timestamps and preserves the original input order', () => {
    expect(sortProjects(rows, 'updated', 'zh-CN').map(p => p.id)).toEqual(['a', 'b', 'c']);
    expect(rows.map(p => p.id)).toEqual(['b', 'a', 'c']);
  });
  it('sorts numbers in names and codes in natural order', () => {
    for (const sort of ['name', 'code'] as const) {
      expect(sortProjects(rows, sort, 'zh-CN').map(p => p.id)).toEqual(['c', 'a', 'b']);
    }
  });
  it('breaks equal timestamp and code ties stably by ID and keeps empty lists empty', () => {
    const same = rows.map(p => ({ ...p, code: 'P', updated_at: rows[0].updated_at }));
    expect(sortProjects(same, 'updated', 'en-US').map(p => p.id)).toEqual(['a', 'b', 'c']);
    expect(sortProjects([], 'updated', 'en-US')).toEqual([]);
  });
});
