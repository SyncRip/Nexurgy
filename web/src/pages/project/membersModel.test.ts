import { describe, expect, it } from 'vitest';
import { isLastOwner, memberDuties, permissionCell } from './membersModel';

const task = (id: string, status: string, owner_id: string | null, reviewer_id: string | null) => ({ id, key: `T-${id}`, title: id, status, owner_id, reviewer_id }) as never;
const req = (id: string, kind: string, status: string, assignee_id: string | null) => ({ id, kind, status, title: id, assignee_id }) as never;

describe('member duties', () => {
  it('counts open tasks owned / accepted and pending reviews by name', () => {
    const d = memberDuties(
      'u',
      [task('1', 'in_progress', 'u', 'x'), task('2', 'todo', 'x', 'u'), task('3', 'done', 'u', 'u'), task('4', 'cancelled', 'u', null), task('5', 'in_review', 'u', 'u')],
      [req('a', 'engineering', 'pending', 'u'), req('b', 'task_acceptance', 'pending', 'u'), req('c', 'milestone_review', 'approved', 'u'), req('d', 'engineering', 'pending', null)],
    );
    expect([d.owner, d.reviewer, d.review]).toEqual([2, 2, 1]);
    expect(d.items.map((i) => `${i.kind}:${i.id}`)).toEqual(['owner:1', 'reviewer:2', 'owner:5', 'reviewer:5', 'review:a']);
  });
});

describe('permission matrix', () => {
  it('mirrors the server roles and the template-manager grant', () => {
    expect(permissionCell('owner', 'manage')).toBe('yes');
    expect(permissionCell('designer', 'manage')).toBe('no');
    expect(permissionCell('process', 'process')).toBe('yes');
    expect(permissionCell('viewer', 'workflow')).toBe('no');
    expect(permissionCell('viewer', 'view')).toBe('yes');
    expect(permissionCell('owner', 'template')).toBe('yes');
    expect(permissionCell('quoter', 'template')).toBe('grantable');
    expect(permissionCell('viewer', 'template')).toBe('no');
  });

  it('protects the only owner', () => {
    expect(isLastOwner({ role: 'owner' }, [{ role: 'owner' }, { role: 'designer' }])).toBe(true);
    expect(isLastOwner({ role: 'owner' }, [{ role: 'owner' }, { role: 'owner' }])).toBe(false);
    expect(isLastOwner({ role: 'designer' }, [{ role: 'owner' }])).toBe(false);
  });
});
