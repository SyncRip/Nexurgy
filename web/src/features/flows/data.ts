import { useFeature } from '../../api/capabilities';
// Lists the /flows page shares between its tab bar (counts) and its tabs, so
// the counts and the board come from the same request and stay in sync while
// the board polls. Tabs rendered outside /flows (old project registry) load
// their own data when there is no provider.

import { createContext, useContext } from 'react';
import { listAgentProfiles, type AgentProfile } from '../../api/agents';
import { listCollaborationDrafts } from '../../api/collaboration';
import { listRuns, listWorkflows } from '../../api/endpoints';
import type { RunSummary, UUID, Workflow } from '../../api/types';
import { useResource, type Resource } from '../../hooks/useResource';

/** The board shows the latest runs; older ones stay in the list view. */
export const BOARD_LIMIT = 100;

export interface FlowsData {
  projectId: UUID;
  /** Latest BOARD_LIMIT + 1 runs (one extra to know there are more). */
  runs: Resource<RunSummary[]>;
  /** Collaboration drafts (kind=collaboration workflows). */
  drafts: Resource<Workflow[]>;
  /** Project templates (not archived). */
  templates: Resource<Workflow[]>;
  /** All agent profiles of the project, enabled or not. */
  agents: Resource<AgentProfile[]>;
}

const FlowsDataContext = createContext<FlowsData | null>(null);
export const FlowsDataProvider = FlowsDataContext.Provider;

export function useFlowsDataLoader(projectId: UUID | null | undefined): FlowsData | null {
  const collaboration = useFeature('collaboration');
  const on = { enabled: !!projectId };
  const runs = useResource((signal) => listRuns(projectId!, { limit: BOARD_LIMIT + 1 }, { signal }), [projectId], on);
  const drafts = useResource((signal) => listCollaborationDrafts(projectId!, { signal }), [projectId, collaboration], { enabled: !!projectId && collaboration });
  const templates = useResource((signal) => listWorkflows(projectId!, {}, { signal }), [projectId], on);
  const agents = useResource((signal) => listAgentProfiles(projectId!, {}, { signal }), [projectId, collaboration], { enabled: !!projectId && collaboration });
  return projectId ? { projectId, runs, drafts: collaboration ? drafts : { ...drafts, data: [], loading: false, initial: false }, templates, agents: collaboration ? agents : { ...agents, data: [], loading: false, initial: false } } : null;
}

/** The page's shared lists for this project, or null outside /flows (or for another project). */
export function useFlowsData(projectId: UUID): FlowsData | null {
  const data = useContext(FlowsDataContext);
  return data && data.projectId === projectId ? data : null;
}
