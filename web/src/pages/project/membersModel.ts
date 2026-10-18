// Members tab (prototype #project.members): what each member is responsible
// for right now, and the role / permission matrix. Pure functions, unit-tested.
// The matrix mirrors ACTION_ROLES (auth/permissions.ts ↔ server security.py);
// the server stays the authority.

import type { ApprovalRequest } from '../../api/approvals';
import type { Task } from '../../api/pm';
import type { Role } from '../../api/types';
import { ACTION_ROLES, type ProjectAction } from '../../auth/permissions';

export interface Duty {
  kind: 'owner' | 'reviewer' | 'review';
  /** Task id for task duties, request id for reviews. */
  id: string;
  label: string;
}

export interface Duties {
  owner: number;
  reviewer: number;
  review: number;
  items: Duty[];
}

const OPEN_TASK = new Set(['todo', 'in_progress', 'in_review']);

/**
 * Open work of one member: tasks they own, open tasks they accept, and
 * pending approvals assigned to them by name (task acceptances are already
 * counted as "accepts").
 */
export function memberDuties(
  userId: string,
  tasks: readonly Pick<Task, 'id' | 'key' | 'title' | 'status' | 'owner_id' | 'reviewer_id'>[],
  requests: readonly Pick<ApprovalRequest, 'id' | 'kind' | 'status' | 'title' | 'assignee_id'>[],
): Duties {
  const items: Duty[] = [];
  for (const t of tasks) {
    if (!OPEN_TASK.has(t.status)) continue;
    if (t.owner_id === userId) items.push({ kind: 'owner', id: t.id, label: `${t.key} · ${t.title}` });
    if (t.reviewer_id === userId) items.push({ kind: 'reviewer', id: t.id, label: `${t.key} · ${t.title}` });
  }
  for (const r of requests) {
    if (r.status === 'pending' && r.kind !== 'task_acceptance' && r.assignee_id === userId) items.push({ kind: 'review', id: r.id, label: r.title });
  }
  const n = (k: Duty['kind']) => items.filter((d) => d.kind === k).length;
  return { owner: n('owner'), reviewer: n('reviewer'), review: n('review'), items };
}

/** Actions of the permission matrix: the role actions plus template maintenance. */
export type MatrixAction = ProjectAction | 'template';
export const MATRIX_ACTIONS: MatrixAction[] = ['view', 'manage', 'design', 'process', 'quote', 'workflow', 'template'];

/**
 * yes = the role can; grantable = only with the template-manager capability
 * (any working role, never a viewer); no = never.
 */
export function permissionCell(role: Role, action: MatrixAction): 'yes' | 'grantable' | 'no' {
  if (action === 'template') return role === 'owner' ? 'yes' : role === 'viewer' ? 'no' : 'grantable';
  return ACTION_ROLES[action].includes(role) ? 'yes' : 'no';
}

/** The only owner cannot be removed or lose the owner role (the server refuses it too). */
export function isLastOwner(member: { role: Role }, members: readonly { role: Role }[]): boolean {
  return member.role === 'owner' && members.filter((m) => m.role === 'owner').length <= 1;
}
