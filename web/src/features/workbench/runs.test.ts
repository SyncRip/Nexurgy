import { describe, expect, it } from 'vitest';
import { workbenchRuns } from './runs';

describe('Workbench run priority', () => {
  it('keeps pending and active tasks on the first screen when newer runs complete', () => {
    const rows = [
      { id: 'done', status: 'succeeded' as const, created_at: '2026-10-17T12:00:00Z' },
      { id: 'running', status: 'running' as const, created_at: '2026-10-17T10:00:00Z' },
      { id: 'waiting', status: 'waiting' as const, created_at: '2026-10-17T09:00:00Z' },
      { id: 'failed', status: 'partial' as const, created_at: '2026-10-17T11:00:00Z' },
    ];
    expect(workbenchRuns(rows).map((r) => r.id)).toEqual(['failed', 'waiting', 'running', 'done']);
    expect(rows[0].id).toBe('done');
  });
  it('handles empty lists and sorts equal timestamps stably by ID', () => {
    expect(workbenchRuns([])).toEqual([]);
    expect(workbenchRuns([{ id: 'a', status: 'queued', created_at: 'now' }, { id: 'b', status: 'queued', created_at: 'now' }]).map((r) => r.id)).toEqual(['b', 'a']);
  });
});
