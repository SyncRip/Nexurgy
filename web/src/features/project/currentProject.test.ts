import { describe, expect, it } from 'vitest';
import { currentProjectKey, readCurrentProject, resolveCurrentProject, writeCurrentProject, type KeyValueStorage } from './currentProject';

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const C = '33333333-3333-4333-8333-333333333333';

function memory(): KeyValueStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v), removeItem: (k) => void data.delete(k) };
}

const throwing: KeyValueStorage = {
  getItem: () => {
    throw new Error('blocked');
  },
  setItem: () => {
    throw new Error('quota');
  },
  removeItem: () => {
    throw new Error('blocked');
  },
};

describe('current project storage', () => {
  it('remembers the choice per user', () => {
    const s = memory();
    writeCurrentProject(s, 'u1', A);
    writeCurrentProject(s, 'u2', B);
    expect(readCurrentProject(s, 'u1')).toBe(A);
    expect(readCurrentProject(s, 'u2')).toBe(B);
    writeCurrentProject(s, 'u1', null);
    expect(readCurrentProject(s, 'u1')).toBeNull();
  });

  it('ignores malformed values and broken or missing storage', () => {
    const s = memory();
    s.setItem(currentProjectKey('u1'), 'javascript:alert(1)');
    expect(readCurrentProject(s, 'u1')).toBeNull();
    writeCurrentProject(s, 'u1', 'not-a-uuid');
    expect(s.data.has(currentProjectKey('u1'))).toBe(false);
    expect(readCurrentProject(throwing, 'u1')).toBeNull();
    expect(() => writeCurrentProject(throwing, 'u1', A)).not.toThrow();
    expect(readCurrentProject(null, 'u1')).toBeNull();
    expect(() => writeCurrentProject(null, 'u1', A)).not.toThrow();
  });
});

describe('resolving the current project', () => {
  const list = [
    { id: A, status: 'archived' as const },
    { id: B, status: 'active' as const },
  ];
  it('prefers the project a link names, then the stored one if still visible', () => {
    expect(resolveCurrentProject(list, C, B)).toBe(C);
    expect(resolveCurrentProject(list, null, A)).toBe(A);
    expect(resolveCurrentProject(list, 'bad', A)).toBe(A);
  });
  it('falls back to the first active project', () => {
    expect(resolveCurrentProject(list, null, C)).toBe(B);
    expect(resolveCurrentProject(list, null, null)).toBe(B);
    expect(resolveCurrentProject([{ id: A, status: 'archived' }], null, null)).toBe(A);
    expect(resolveCurrentProject([], null, C)).toBeNull();
  });
  it('uses the stored choice while the list loads', () => {
    expect(resolveCurrentProject(undefined, null, C)).toBe(C);
    expect(resolveCurrentProject(undefined, null, null)).toBeNull();
  });
});
