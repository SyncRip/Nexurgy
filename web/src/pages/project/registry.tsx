// Current project tabs. Legacy project URLs redirect through app/nav.tsx.

import { lazy, type ComponentType } from 'react';
import type { ProjectTab } from '../../app/nav';
import { MembersTab } from './MembersTab';
import type { ProjectTabProps } from './tabProps';

const ProjectOverviewTab = lazy(() => import('../../features/tasks/ProjectOverviewTab'));
const ProjectTasksTab = lazy(() => import('../../features/tasks/ProjectTasksTab'));
const ProjectApprovalsTab = lazy(() => import('../../features/approvals/ProjectApprovalsTab'));

export const PROJECT_SECTION_COMPONENTS: Record<ProjectTab, ComponentType<ProjectTabProps>> = {
  overview: ProjectOverviewTab,
  tasks: ProjectTasksTab,
  approvals: ProjectApprovalsTab,
  members: MembersTab,
};
