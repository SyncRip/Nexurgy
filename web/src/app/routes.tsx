// Routes rendered inside the authenticated shell (top bar + rail).
//
// Extension point: later tasks add pages here, e.g.
//   { path: '/workflows/:workflowId', element: <WorkflowCanvasPage /> },
//   { path: '/runs/:runId', element: <RunPage /> },
// using lazy() so their code is split out. Project-level tabs are swapped in
// pages/project/registry.tsx and part workspace tabs in pages/part/registry.tsx.

import { lazy, type ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

const Projects = lazy(() => import('../pages/Projects'));
// R1.2 areas (.trellis/tasks/archive/2026-10/10-09-web-refactor-r12/prd.md).
const WorkbenchPage = lazy(() => import('../pages/workbench/WorkbenchPage'));
const PartsPage = lazy(() => import('../pages/areas/PartsPage'));
const FlowsPage = lazy(() => import('../pages/areas/FlowsPage'));
const ResourcesPage = lazy(() => import('../pages/areas/ResourcesPage'));
const ProjectPage = lazy(() => import('../pages/project/ProjectPage'));
const LegacyProjectRedirect = lazy(() => import('../pages/project/LegacyProjectRedirect'));
const PartWorkspace = lazy(() => import('../pages/part/PartWorkspace'));
const Settings = lazy(() => import('../pages/Settings'));
const TaskDetailPage = lazy(() => import('../pages/tasks/TaskDetailPage'));
const AssetDetailPage = lazy(() => import('../pages/assets/AssetDetailPage'));
const ApprovalCenterPage = lazy(() => import('../pages/approvals/ApprovalCenterPage'));
const ApprovalDetailPage = lazy(() => import('../pages/approvals/ApprovalDetailPage'));
const WorkflowCanvasPage = lazy(() => import('../pages/workflow/WorkflowCanvasPage'));
const RunPage = lazy(() => import('../pages/workflow/RunPage'));
const TemplateLibraryPage = lazy(() => import('../pages/templates/TemplateLibraryPage'));
const PublicTemplatePage = lazy(() => import('../pages/templates/PublicTemplatePage'));
// Agent collaboration layer (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §5.1).
const CollaborationNewPage = lazy(() => import('../pages/collaboration/CollaborationNewPage'));
const CollaborationDraftPage = lazy(() => import('../pages/collaboration/CollaborationDraftPage'));
const CollaborationRunPage = lazy(() => import('../pages/collaboration/CollaborationRunPage'));
// Unified AI workspace (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §7.1).
const AssistantPage = lazy(() => import('../pages/assistant/AssistantPage'));
// General mechanical engineering (docs/development/mechanical-engineering/w0-contract.zh-CN.md §7).
const AssemblyWorkspacePage = lazy(() => import('../pages/assembly/AssemblyWorkspacePage'));
const DraftingPage = lazy(() => import('../pages/drafting/DraftingPage'));
const SpecPage = lazy(() => import('../pages/spec/SpecPage'));
// Agent execution layer P1 (docs/archive/agent/README.zh-CN.md §5.5).
const AgentExecutionPage = lazy(() => import('../pages/agentExec/AgentExecutionPage'));
const NotFound = lazy(() => import('../pages/NotFound'));

export interface ShellRoute {
  path: string;
  element: ReactNode;
}

// Primitive gallery: the DEV constant is false in production builds, so the
// route and its lazy chunk are dropped.
const DEV_ROUTES: ShellRoute[] = [];
if (import.meta.env.DEV) {
  const UiPlayground = lazy(() => import('../pages/dev/UiPlayground'));
  DEV_ROUTES.push({ path: '/dev/ui', element: <UiPlayground /> });
}

export const SHELL_ROUTES: ShellRoute[] = [
  ...DEV_ROUTES,
  { path: '/home', element: <WorkbenchPage /> },
  { path: '/parts', element: <PartsPage /> },
  { path: '/flows/:tab?', element: <FlowsPage /> },
  { path: '/resources/:tab?', element: <ResourcesPage /> },
  { path: '/project/:tab?', element: <ProjectPage /> },
  { path: '/projects', element: <Projects /> },
  // Old project URLs → their area (knowledge → asset center knowledge tab).
  { path: '/projects/:projectId', element: <LegacyProjectRedirect /> },
  { path: '/projects/:projectId/collaboration/new', element: <CollaborationNewPage /> },
  { path: '/projects/:projectId/:section', element: <LegacyProjectRedirect /> },
  { path: '/collaboration/drafts/:workflowId', element: <CollaborationDraftPage /> },
  { path: '/collaboration/runs/:runId', element: <CollaborationRunPage /> },
  { path: '/parts/:partId', element: <PartWorkspace /> },
  { path: '/parts/:partId/:tab', element: <PartWorkspace /> },
  // Template detail: overview / config (draft canvas) / versions / usage. One route with an
  // optional segment so switching tabs keeps the page (and an unsaved canvas) mounted.
  { path: '/workflows/:workflowId/:tab?', element: <WorkflowCanvasPage /> },
  { path: '/template-library', element: <TemplateLibraryPage /> },
  { path: '/template-library/:publicTemplateId', element: <PublicTemplatePage /> },
  { path: '/runs/:runId', element: <RunPage /> },
  { path: '/tasks/:taskId', element: <TaskDetailPage /> },
  { path: '/assets/:assetId', element: <AssetDetailPage /> },
  { path: '/approvals', element: <ApprovalCenterPage /> },
  { path: '/approval-requests/:requestId', element: <ApprovalDetailPage /> },
  { path: '/assistant', element: <AssistantPage /> },
  { path: '/assistant/:conversationId', element: <AssistantPage /> },
  { path: '/assemblies/:assemblyId/:tab?', element: <AssemblyWorkspacePage /> },
  { path: '/drafting/:documentId', element: <DraftingPage /> },
  { path: '/specs/:specId', element: <SpecPage /> },
  { path: '/agent-executions/:executionId', element: <AgentExecutionPage /> },
  { path: '/settings', element: <Settings /> },
  { path: '/app', element: <Navigate to="/home" replace /> },
  { path: '*', element: <NotFound /> },
];
