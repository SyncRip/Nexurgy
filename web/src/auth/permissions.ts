// Role → action visibility (mirror of ACTION_ROLES in server/app/security.py,
// design.md §5). The server is the authority: hiding a control here is only a
// courtesy, and every mutation still handles 403 gracefully.

import type { ApprovalStage, Project, Role } from '../api/types';

export type ProjectAction = 'view' | 'manage' | 'design' | 'process' | 'quote' | 'workflow';

export const ACTION_ROLES: Record<ProjectAction, readonly Role[]> = {
  view: ['owner', 'designer', 'process', 'quoter', 'viewer'],
  manage: ['owner'],
  design: ['owner', 'designer'],
  process: ['owner', 'process'],
  quote: ['owner', 'quoter'],
  workflow: ['owner', 'designer', 'process', 'quoter'],
};

/** Approval stage → action whose roles may confirm it. */
export const STAGE_ACTION: Record<ApprovalStage, ProjectAction> = { design: 'design', process: 'process', quote: 'quote' };

/** Pure role check (ignores project status). */
export function roleCan(role: Role | null | undefined, action: ProjectAction): boolean {
  return !!role && ACTION_ROLES[action].includes(role);
}

/**
 * Role check that also follows the server's archived-project rule: in an
 * archived project only "view" and "manage" (restore) are allowed.
 */
export function canInProject(project: Pick<Project, 'my_role' | 'status'> | null | undefined, action: ProjectAction): boolean {
  if (!project) return false;
  if (!roleCan(project.my_role, action)) return false;
  if (project.status === 'archived' && action !== 'view' && action !== 'manage') return false;
  return true;
}

export interface ProjectCapabilities {
  view: boolean;
  manage: boolean;
  design: boolean;
  process: boolean;
  quote: boolean;
  workflow: boolean;
  /** Archive / restore the project itself (owner, regardless of status). */
  archive: boolean;
}

export function projectCapabilities(project: Pick<Project, 'my_role' | 'status'> | null | undefined): ProjectCapabilities {
  return {
    view: canInProject(project, 'view'),
    // The server lets owners manage members / details even while archived.
    manage: canInProject(project, 'manage'),
    design: canInProject(project, 'design'),
    process: canInProject(project, 'process'),
    quote: canInProject(project, 'quote'),
    workflow: canInProject(project, 'workflow'),
    archive: roleCan(project?.my_role, 'manage'),
  };
}

/** Projects a part can be copied into: the user must be able to design there. */
export function copyTargets<P extends Pick<Project, 'my_role' | 'status'>>(projects: P[]): P[] {
  return projects.filter((p) => canInProject(p, 'design'));
}
