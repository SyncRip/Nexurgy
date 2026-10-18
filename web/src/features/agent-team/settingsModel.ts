import { ApiError, isApiError } from '../../api/client';
import type { TeamConfig, TeamSettingsOut } from '../../api/engineeringTeam';
import { ENGINEERING_ROLES, type EngineeringRole } from './roles';

// Collection cardinality is part of the engineering-team API contract. Text limits come from capabilities.
export const MAX_ROLE_SKILLS = 12;
export const MAX_CUSTOM_SKILLS = 32;

export function teamRoleFromQuery(value: string | null): EngineeringRole {
  return ENGINEERING_ROLES.includes(value as EngineeringRole) ? value as EngineeringRole : 'manager';
}

export function removeTeamSkill(config: TeamConfig, skillId: string): TeamConfig {
  if (!config.skills.some((skill) => skill.id === skillId)) return config;
  const agents = { ...config.agents };
  for (const role of ENGINEERING_ROLES) agents[role] = { ...agents[role], skill_ids: agents[role].skill_ids.filter((id) => id !== skillId) };
  return { agents, skills: config.skills.filter((skill) => skill.id !== skillId) };
}

export function resetTeamRole(config: TeamConfig, defaults: TeamConfig, role: EngineeringRole): TeamConfig {
  return { ...config, agents: { ...config.agents, [role]: { ...defaults.agents[role], skill_ids: [...defaults.agents[role].skill_ids] } } };
}

export function toggleTeamSkill(config: TeamConfig, role: EngineeringRole, id: string): TeamConfig {
  const before = config.agents[role];
  const selected = before.skill_ids.includes(id);
  if (!selected && before.skill_ids.length >= MAX_ROLE_SKILLS) return config;
  const skill_ids = selected ? before.skill_ids.filter((value) => value !== id) : [...before.skill_ids, id];
  return { ...config, agents: { ...config.agents, [role]: { ...before, skill_ids } } };
}

export function teamConfigCharacters(config: TeamConfig): number {
  const texts = ENGINEERING_ROLES.flatMap((role) => {
    const agent = config.agents[role];
    return [agent.display_name, agent.profile, agent.tone, agent.personality];
  }).concat(config.skills.flatMap((skill) => [skill.name, skill.description, skill.instructions]));
  return texts.reduce((sum, value) => sum + Array.from(value).length, 0);
}

export function teamDirty(saved: TeamConfig | null | undefined, draft: TeamConfig | null): boolean {
  return !!saved && !!draft && JSON.stringify(saved) !== JSON.stringify(draft);
}

export interface TeamEditorState {
  projectId: string;
  canManage: boolean;
  editing: boolean;
  saved: TeamSettingsOut | null;
  draft: TeamConfig | null;
  loading: boolean;
  busy: boolean;
  error: unknown;
  restored: boolean;
}

export interface TeamDraftRecord { saved: TeamSettingsOut; draft: TeamConfig }

export type TeamEditorAction = { projectId: string } & (
  | { type: 'loaded'; value: TeamSettingsOut; preserved?: TeamDraftRecord }
  | { type: 'saved'; value: TeamSettingsOut }
  | { type: 'edit'; config: TeamConfig }
  | { type: 'permission'; canManage: boolean }
  | { type: 'beginEdit' | 'loading' | 'saving' | 'discard' }
  | { type: 'failed'; error: unknown }
);

export const initialTeamEditor = (projectId: string, canManage = false): TeamEditorState => ({ projectId, canManage, editing: false, saved: null, draft: null, loading: true, busy: false, error: null, restored: false });

/** Memory only: drafts survive SPA navigation, never cross identities/projects, and are not authorization. */
export function createTeamDraftStore() {
  const drafts = new Map<string, TeamDraftRecord>();
  const key = (userId: string, projectId: string) => JSON.stringify([userId, projectId]);
  return {
    read: (userId: string, projectId: string) => drafts.get(key(userId, projectId)),
    remember(userId: string, state: TeamEditorState) {
      if (!state.saved || !state.draft) return;
      const id = key(userId, state.projectId);
      if (teamDirty(state.saved.config, state.draft)) drafts.set(id, { saved: state.saved, draft: state.draft });
      else drafts.delete(id);
    },
  };
}

/** Only explicit successful loads/saves replace a draft; failures and stale project responses never do. */
export function teamEditorReducer(state: TeamEditorState, action: TeamEditorAction): TeamEditorState {
  if (action.projectId !== state.projectId) return state;
  switch (action.type) {
    case 'loaded': {
      if (action.value.project_id !== state.projectId) return state;
      const previous = action.preserved;
      if (previous?.saved.project_id === state.projectId && teamDirty(previous.saved.config, previous.draft)) {
        const conflict = previous.saved.version !== action.value.version;
        return { ...state, saved: { ...action.value, version: previous.saved.version, updated_at: previous.saved.updated_at, config: previous.saved.config }, draft: previous.draft,
          loading: false, busy: false, editing: false, restored: true, error: conflict ? new ApiError(409, 'version_conflict', 'The saved team changed while the draft was open.') : null };
      }
      return { ...state, saved: action.value, draft: action.value.config, loading: false, busy: false, editing: false, error: null, restored: false };
    }
    case 'saved':
      if (action.value.project_id !== state.projectId) return state;
      return { ...state, saved: action.value, draft: action.value.config, loading: false, busy: false, editing: false, error: null, restored: false };
    case 'permission':
      if (state.canManage === action.canManage) return state;
      return { ...state, canManage: action.canManage, editing: action.canManage && state.editing };
    case 'beginEdit':
      if (!state.canManage || state.editing || !state.saved || !state.draft || state.busy || state.loading) return state;
      return { ...state, editing: true };
    case 'edit':
      if (!state.canManage || !state.editing || state.busy || state.loading) return state;
      return { ...state, draft: action.config };
    case 'loading':
      return { ...state, loading: true, error: null };
    case 'saving':
      if (!state.canManage || !state.editing || state.busy || state.loading || !teamDirty(state.saved?.config, state.draft)
        || (isApiError(state.error) && state.error.code === 'version_conflict')) return state;
      return { ...state, busy: true, error: null };
    case 'failed':
      return { ...state, loading: false, busy: false, error: action.error };
    case 'discard':
      if (!state.editing || state.busy || state.loading) return state;
      return { ...state, draft: state.saved?.config ?? null, editing: false, error: null, restored: false };
  }
}
