import { describe, expect, it } from 'vitest';
import type { Project } from '../../api/types';
import type { KeyValueStorage } from '../project/currentProject';
import {
  SCOPE_STORAGE_KEY,
  fallbackScope,
  isFiltered,
  parseScopeValue,
  readMaterialQuery,
  readSavedScope,
  readUrlScope,
  resolveScope,
  sameScope,
  scopeUrl,
  withScope,
  withScopeProject,
  writeSavedScope,
  type ScopeInputs,
} from './scope';
import type { ViewScope } from './types';

const P1 = '11111111-1111-4111-8111-111111111111';
const P2 = '22222222-2222-4222-8222-222222222222';
const P3 = '33333333-3333-4333-8333-333333333333';
const GONE = '99999999-9999-4999-8999-999999999999';

const q = (s: string) => new URLSearchParams(s);
const project = (projectId: string): ViewScope => ({ kind: 'project', projectId });
const listed = [
  { id: P1, status: 'archived' as const },
  { id: P2, status: 'active' as const },
  { id: P3, status: 'active' as const },
];

function memoryStorage(init: Record<string, string> = {}): KeyValueStorage & { data: Record<string, string> } {
  const data = { ...init };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v;
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}

const inputs = (over: Partial<ScopeInputs>): ScopeInputs => ({
  url: { scope: null, invalid: false },
  saved: null,
  current: null,
  projects: listed,
  unavailable: new Set(),
  ...over,
});

describe('scope in the URL', () => {
  it('reads ?project=<uuid> and ?scope=all, normalizing the retired shared scope', () => {
    expect(readUrlScope(q(`project=${P1}`))).toEqual({ scope: project(P1), invalid: false });
    expect(readUrlScope(q('scope=all'))).toEqual({ scope: { kind: 'all' }, invalid: false });
    expect(readUrlScope(q('scope=shared&q=x'))).toEqual({ scope: { kind: 'all' }, invalid: false, normalize: true });
    expect(readUrlScope(q('q=x'))).toEqual({ scope: null, invalid: false });
  });

  it('reports unusable values instead of guessing', () => {
    expect(readUrlScope(q('project=abc'))).toEqual({ scope: null, invalid: true });
    expect(readUrlScope(q('scope=global'))).toEqual({ scope: null, invalid: true });
    // A project link wins over a stray scope key.
    expect(readUrlScope(q(`project=${P2}&scope=all`)).scope).toEqual(project(P2));
  });

  it('keeps only the scope keys when the scope changes', () => {
    expect(scopeUrl('files', { kind: 'all' })).toBe('/resources/files?scope=all');
    expect(scopeUrl('knowledge', project(P1))).toBe(`/resources/knowledge?project=${P1}`);
    expect(scopeUrl('files', project(P1), { item: 'asset:a1', version: '' })).toBe(`/resources/files?project=${P1}&item=asset%3Aa1`);
  });

  it('names the scope of a URL that had none and keeps its other keys', () => {
    expect(withScope(q('type=spec&q=bolt'), project(P2)).toString()).toBe(`project=${P2}&type=spec&q=bolt`);
    expect(withScope(q(`project=${P1}&type=spec`), { kind: 'all' }).toString()).toBe('scope=all&type=spec');
  });

  it('normalizes old shared links without dropping their filters or exact version', () => {
    const before = q('scope=shared&q=bolt&type=spec&item=asset%3Aa1&version=v2');
    const resolved = resolveScope(inputs({ url: readUrlScope(before) }));
    expect(resolved).toEqual({ scope: { kind: 'all' }, fallback: null, fromUrl: false });
    const after = withScope(before, resolved.scope!);
    expect(after.toString()).toBe('scope=all&q=bolt&type=spec&item=asset%3Aa1&version=v2');
    expect(resolveScope(inputs({ url: readUrlScope(after) })).fromUrl).toBe(true);
  });

  it('compares and parses scope values', () => {
    expect(parseScopeValue('all')).toEqual({ kind: 'all' });
    expect(parseScopeValue(P3)).toEqual(project(P3));
    expect(parseScopeValue('nope')).toBeNull();
    expect(sameScope(project(P1), project(P1))).toBe(true);
    expect(sameScope(project(P1), { kind: 'all' })).toBe(false);
    expect(sameScope(null, { kind: 'all' })).toBe(false);
  });
});

describe('remembered scope', () => {
  it('round-trips per user and ignores other users and broken data', () => {
    const s = memoryStorage();
    writeSavedScope(s, 'u1', { kind: 'all' });
    expect(readSavedScope(s, 'u1')).toEqual({ kind: 'all' });
    expect(readSavedScope(s, 'u2')).toBeNull();
    writeSavedScope(s, 'u1', project(P2));
    expect(JSON.parse(s.data[SCOPE_STORAGE_KEY])).toEqual({ user: 'u1', scope: P2 });
    expect(readSavedScope(memoryStorage({ [SCOPE_STORAGE_KEY]: '{oops' }), 'u1')).toBeNull();
    expect(readSavedScope(memoryStorage({ [SCOPE_STORAGE_KEY]: JSON.stringify({ user: 'u1', scope: 'bogus' }) }), 'u1')).toBeNull();
    expect(readSavedScope(null, 'u1')).toBeNull();
  });

  it('migrates a remembered shared scope to all projects for the same user', () => {
    const s = memoryStorage({ [SCOPE_STORAGE_KEY]: JSON.stringify({ user: 'u1', scope: 'shared' }) });
    expect(readSavedScope(s, 'u2')).toBeNull();
    const saved = readSavedScope(s, 'u1');
    expect(saved).toEqual({ kind: 'all' });
    writeSavedScope(s, 'u1', saved!);
    expect(JSON.parse(s.data[SCOPE_STORAGE_KEY])).toEqual({ user: 'u1', scope: 'all' });
  });

  it('survives storage that throws', () => {
    const broken: KeyValueStorage = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('quota');
      },
      removeItem: () => {},
    };
    expect(readSavedScope(broken, 'u1')).toBeNull();
    expect(() => writeSavedScope(broken, 'u1', { kind: 'all' })).not.toThrow();
  });
});

describe('resolving the scope', () => {
  it('uses the URL scope, even a project that is not listed (an archived project the server may still open)', () => {
    expect(resolveScope(inputs({ url: { scope: { kind: 'all' }, invalid: false } }))).toEqual({ scope: { kind: 'all' }, fallback: null, fromUrl: true });
    expect(resolveScope(inputs({ url: { scope: project(GONE), invalid: false } }))).toEqual({ scope: project(GONE), fallback: null, fromUrl: true });
  });

  it('falls back with a reason when the server refused the URL project', () => {
    const r = resolveScope(inputs({ url: { scope: project(GONE), invalid: false }, current: GONE, unavailable: new Set([GONE]) }));
    // The refused id is the current project too (the link made it current): the first active project is used.
    expect(r).toEqual({ scope: project(P2), fallback: 'url_project', fromUrl: false });
    expect(resolveScope(inputs({ url: { scope: project(GONE), invalid: false }, current: P3, unavailable: new Set([GONE]) })).scope).toEqual(project(P3));
  });

  it('falls back with a reason for an unusable URL value', () => {
    expect(resolveScope(inputs({ url: { scope: null, invalid: true }, current: P3 }))).toEqual({ scope: project(P3), fallback: 'url_invalid', fromUrl: false });
  });

  it('uses the remembered scope when the URL names none', () => {
    expect(resolveScope(inputs({ saved: { kind: 'all' }, current: P2 }))).toEqual({ scope: { kind: 'all' }, fallback: null, fromUrl: false });
    expect(resolveScope(inputs({ saved: project(P3), current: P2 }))).toEqual({ scope: project(P3), fallback: null, fromUrl: false });
    // Still waiting for the list before trusting a remembered project.
    expect(resolveScope(inputs({ saved: project(P3), projects: undefined })).scope).toBeNull();
  });

  it('drops a remembered project that is gone, with a reason', () => {
    expect(resolveScope(inputs({ saved: project(GONE), current: P3 }))).toEqual({ scope: project(P3), fallback: 'saved_project', fromUrl: false });
    expect(resolveScope(inputs({ saved: project(P3), current: P2, unavailable: new Set([P3]) }))).toEqual({ scope: project(P2), fallback: 'saved_project', fromUrl: false });
  });

  it('else the current project, else the first active one, else all projects', () => {
    expect(resolveScope(inputs({ current: P3 })).scope).toEqual(project(P3));
    expect(resolveScope(inputs({})).scope).toEqual(project(P2));
    expect(resolveScope(inputs({ projects: [] })).scope).toEqual({ kind: 'all' });
    expect(resolveScope(inputs({ projects: undefined })).scope).toBeNull();
    // A stored current project is not tried before the list confirms it, nor when it is not listed.
    expect(resolveScope(inputs({ current: P3, projects: undefined })).scope).toBeNull();
    expect(resolveScope(inputs({ current: GONE })).scope).toEqual(project(P2));
    expect(fallbackScope([{ id: P1, status: 'archived' }], null, new Set())).toEqual(project(P1));
    expect(fallbackScope(listed, null, new Set([P1, P2, P3]))).toEqual({ kind: 'all' });
  });
});

describe('materials URL state', () => {
  it('reads the filter and ignores values outside the closed sets', () => {
    const { filter, invalid } = readMaterialQuery(q(`q=%20bolt%20&type=drawing&subtype=drawing_document&source=upload&part_id=pp&status=deprecated&archived=1`), project(P1));
    expect(filter).toEqual({ q: 'bolt', type: 'drawing', subtype: 'drawing_document', creation: 'upload', partId: 'pp', status: 'deprecated', archived: true });
    expect(invalid).toEqual([]);
    const bad = readMaterialQuery(q('type=asset&source=ai&status=done&archived=yes'), project(P1));
    expect(bad.filter).toEqual({ q: '', type: '', subtype: '', creation: '', partId: '', status: '', archived: false });
    expect(bad.invalid.sort()).toEqual(['source', 'status', 'type']);
    expect(readMaterialQuery(q('source=unknown'), project(P1)).filter.creation).toBe('unknown');
  });

  it('only narrows a chosen type by subtype and only filters by part in a project scope', () => {
    const r = readMaterialQuery(q('subtype=drawing_document&part_id=pp'), { kind: 'all' });
    expect(r.filter.subtype).toBe('');
    expect(r.filter.partId).toBe('');
    expect(r.invalid.sort()).toEqual(['part_id', 'subtype']);
    expect(isFiltered(r.filter)).toBe(false);
    expect(isFiltered(readMaterialQuery(q('archived=1'), { kind: 'all' }).filter)).toBe(true);
  });

  it('adds the scope project to the project list when it is not listed', () => {
    const a = { id: P1, code: 'A', name: 'A', status: 'active' } as Project;
    const b = { id: P2, code: 'B', name: 'B', status: 'archived' } as Project;
    expect(withScopeProject([a], b)).toEqual([a, b]);
    expect(withScopeProject([a, b], b)).toEqual([a, b]);
    expect(withScopeProject(undefined, null)).toBeNull();
    expect(withScopeProject(undefined, b)).toEqual([b]);
  });
});
