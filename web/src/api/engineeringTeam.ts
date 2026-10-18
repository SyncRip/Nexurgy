import type { EngineeringRole } from '../features/agent-team/roles';
import { request } from './client';

export interface TeamAgentConfig {
  display_name: string;
  profile: string;
  tone: string;
  personality: string;
  skill_ids: string[];
}

export interface TeamSkill {
  id: string;
  name: string;
  description: string;
  instructions: string;
}

export interface BuiltinTeamSkill {
  id: string;
  name_zh: string;
  name_en: string;
  description_zh: string;
  description_en: string;
  instructions: string;
}

export interface TeamConfig {
  agents: Record<EngineeringRole, TeamAgentConfig>;
  skills: TeamSkill[];
}

export interface TeamSettingsOut {
  project_id: string;
  version: number;
  updated_at: string | null;
  config: TeamConfig;
  defaults: TeamConfig;
  builtin_skills: BuiltinTeamSkill[];
}

type Options = { signal?: AbortSignal };
export const getEngineeringTeam = (projectId: string, options: Options = {}) =>
  request<TeamSettingsOut>(`/projects/${encodeURIComponent(projectId)}/engineering-team`, { signal: options.signal });
export const updateEngineeringTeam = (projectId: string, body: { expected_version: number; config: TeamConfig }, options: Options = {}) =>
  request<TeamSettingsOut>(`/projects/${encodeURIComponent(projectId)}/engineering-team`, { method: 'PUT', body, signal: options.signal });
