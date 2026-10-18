import { describe, expect, it } from 'vitest';
import { safeReturnTo } from './returnTo';

const P = '11111111-1111-4111-8111-111111111111';
const R = '22222222-2222-4222-8222-222222222222';

describe('return_to validation (D07)', () => {
  it('accepts a section of the same project with its query', () => {
    expect(safeReturnTo(`/projects/${P}/assets?tab=resources&kind=formal&offset=50`, P)).toEqual({ kind: 'project', path: `/projects/${P}/assets?tab=resources&kind=formal&offset=50` });
  });

  it('rejects other projects, external and malformed targets', () => {
    expect(safeReturnTo('/projects/other/assets', P)).toBeNull();
    expect(safeReturnTo('https://evil.example/projects/x', P)).toBeNull();
    expect(safeReturnTo(`//evil.example/projects/${P}/assets`, P)).toBeNull();
    expect(safeReturnTo(`/\\evil.example`, P)).toBeNull();
    expect(safeReturnTo(`javascript:alert(1)`, P)).toBeNull();
    expect(safeReturnTo(`/projects/${P}/assets\n`, P)).toBeNull();
    expect(safeReturnTo('', P)).toBeNull();
    expect(safeReturnTo(`/projects/${P}/assets`, '')).toBeNull();
  });

  it('returns run pages for a later project check only', () => {
    expect(safeReturnTo(`/runs/${R}?node=n1`, P)).toEqual({ kind: 'run', path: `/runs/${R}?node=n1`, runId: R });
    expect(safeReturnTo(`/collaboration/runs/${R}`, P)).toEqual({ kind: 'run', path: `/collaboration/runs/${R}`, runId: R });
    expect(safeReturnTo('/runs/not-a-uuid', P)).toBeNull();
    expect(safeReturnTo('/tasks/abc', P)).toBeNull();
  });

  it('accepts R1.2 area pages that name the same project', () => {
    expect(safeReturnTo(`/resources/assets?project=${P}&tab=knowledge&collection_id=c`, P)).toEqual({ kind: 'project', path: `/resources/assets?project=${P}&tab=knowledge&collection_id=c` });
    expect(safeReturnTo(`/flows/runs?task=t&project=${P}`, P)?.kind).toBe('project');
    expect(safeReturnTo(`/parts?project=${P}`, P)?.kind).toBe('project');
    expect(safeReturnTo(`/resources/assets?project=other`, P)).toBeNull();
    expect(safeReturnTo('/resources/assets?tab=knowledge', P)).toBeNull();
    expect(safeReturnTo(`/parts/x?project=${P}`, P)).toBeNull();
    expect(safeReturnTo(`/resources/assets/deep/er?project=${P}`, P)).toBeNull();
    // The resources page's cross-project scopes list this project's items without naming it.
    expect(safeReturnTo('/resources/files?scope=all&item=asset:a', P)?.kind).toBe('project');
    expect(safeReturnTo('/resources/knowledge?scope=shared', P)?.kind).toBe('project');
    expect(safeReturnTo('/resources/files?scope=bogus', P)).toBeNull();
    expect(safeReturnTo('/parts?scope=all', P)).toBeNull();
  });
});
