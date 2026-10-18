// Navigation metadata shared by the rail, the mobile bar and the area pages.
//
// The rail has four areas — Workbench, Parts, Workflows and Resources — plus
// Settings. The old project sections (/projects/:id/:section) stay valid
// links: each maps to an area tab (SECTION_TARGET) and redirects there.

import { Bot,Boxes,ClipboardCheck,FolderOpen,Gauge,LayoutDashboard,Library,ListChecks,PlayCircle,Users,Waypoints,Workflow } from 'lucide-react';
import type { ReactNode } from 'react';
import { ASSET_BUSINESS_TYPE } from '../features/resources/types';
import type { MessageKey } from '../i18n/en';

// Old project sections, kept as link vocabulary: every page that builds a
// project link still says "the runs of project X", and paths.project() turns
// that into the area URL. collaboration / agents: agent collaboration layer
// (.trellis/tasks/archive/2026-10/10-08-agent-collaboration/design.md §5.1).
export const PROJECT_SECTION_IDS = ['overview', 'tasks', 'assets', 'approvals', 'collaboration', 'agents', 'workflows', 'members', 'parts', 'files', 'runs'] as const;
export type ProjectSectionId = (typeof PROJECT_SECTION_IDS)[number];

export interface SectionMeta {
  id: ProjectSectionId;
  labelKey: MessageKey;
  icon: ReactNode;
}

export const ALL_SECTIONS: SectionMeta[] = [
  { id: 'overview', labelKey: 'nav.overview', icon: <Gauge /> },
  { id: 'tasks', labelKey: 'nav.tasks', icon: <ListChecks /> },
  { id: 'assets', labelKey: 'nav.assets', icon: <Library /> },
  { id: 'approvals', labelKey: 'nav.approvals', icon: <ClipboardCheck /> },
  { id: 'collaboration', labelKey: 'nav.collaboration', icon: <Waypoints /> },
  { id: 'agents', labelKey: 'nav.agents', icon: <Bot /> },
  { id: 'workflows', labelKey: 'nav.workflowTemplates', icon: <Workflow /> },
  { id: 'members', labelKey: 'nav.members', icon: <Users /> },
  { id: 'parts', labelKey: 'nav.parts', icon: <Boxes /> },
  { id: 'files', labelKey: 'nav.files', icon: <FolderOpen /> },
  { id: 'runs', labelKey: 'tpl.nav.monitor', icon: <PlayCircle /> },
];

export function isProjectSection(v: string | undefined): v is ProjectSectionId {
  return !!v && (PROJECT_SECTION_IDS as readonly string[]).includes(v);
}

// ─── R1.2 areas ────────────────────────────────────────────────────────────

/**
 * Workflow URL vocabulary retains historical links. The visible tab bar has
 * runs, templates and engineering team; frozen capabilities retain their separate route guards.
 */
export const FLOW_TABS = ['runs', 'templates', 'team', 'collaboration', 'agents', 'executions', 'library'] as const;
export type FlowTab = (typeof FLOW_TABS)[number];
export const FLOW_MAIN_TABS = ['runs', 'templates', 'team'] as const;
export type FlowMainTab = (typeof FLOW_MAIN_TABS)[number];
/** Sections of the templates tab, in display order. */
export const FLOW_TEMPLATE_SECTIONS = ['templates'] as const satisfies readonly FlowTab[];

/** The tab-bar entry a URL tab belongs to. */
export function flowMainTab(tab: FlowTab): FlowMainTab {
  return tab === 'runs' || tab === 'team' ? tab : 'templates';
}
/**
 * Resources has one unified files view. Historical knowledge/process URLs and
 * old specs/drafting/machines links remain part of the redirect vocabulary.
 */
export const RESOURCE_TABS = ['files', 'knowledge', 'process'] as const;
export type ResourceTab = (typeof RESOURCE_TABS)[number];
/** The unified resources view. */
export const RESOURCE_MAIN_TABS = ['files'] as const satisfies readonly ResourceTab[];
export type ResourceMainTab = Exclude<ResourceTab, 'process'>;
/** Project (manager view): tasks first, then stages, members, project approvals. */
export const PROJECT_TABS = ['tasks', 'overview', 'members', 'approvals'] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

export function isFlowTab(v: string | undefined): v is FlowTab {
  return !!v && (FLOW_TABS as readonly string[]).includes(v);
}
export function isResourceTab(v: string | undefined): v is ResourceTab {
  return !!v && (RESOURCE_TABS as readonly string[]).includes(v);
}
export function isProjectTab(v: string | undefined): v is ProjectTab {
  return !!v && (PROJECT_TABS as readonly string[]).includes(v);
}

export interface TabMeta<T extends string> {
  id: T;
  labelKey: MessageKey;
  icon: ReactNode;
}

/** The visible workflow tabs. */
export const FLOW_TAB_META: TabMeta<FlowMainTab>[] = [
  { id: 'runs', labelKey: 'area.flows.runs', icon: <PlayCircle /> },
  { id: 'templates', labelKey: 'area.flows.templates', icon: <Workflow /> },
  { id: 'team', labelKey: 'teamSettings.title', icon: <Users /> },
];
/** Section switch inside the templates tab. */
export const FLOW_TEMPLATE_SECTION_META: TabMeta<(typeof FLOW_TEMPLATE_SECTIONS)[number]>[] = [
  { id: 'templates', labelKey: 'fl.tpl.project', icon: <Workflow /> },
];
export const RESOURCE_TAB_META: TabMeta<ResourceMainTab>[] = [
  { id: 'files', labelKey: 'rp.view.files', icon: <FolderOpen /> },
];
export const PROJECT_TAB_META: TabMeta<ProjectTab>[] = [
  { id: 'tasks', labelKey: 'pa.tab.tasks', icon: <ListChecks /> },
  { id: 'overview', labelKey: 'area.project.stages', icon: <Gauge /> },
  { id: 'members', labelKey: 'nav.members', icon: <Users /> },
  { id: 'approvals', labelKey: 'area.project.approvals', icon: <ClipboardCheck /> },
];

/** Rail / bottom bar entries (settings is available to every signed-in user). */
export interface AreaMeta {
  id: 'home' | 'parts' | 'flows' | 'approvals' | 'resources' | 'project';
  labelKey: MessageKey;
  /** Shorter label for the phone bottom bar. */
  shortKey?: MessageKey;
  icon: ReactNode;
  to: string;
}

export const AREAS: AreaMeta[] = [
  { id: 'home', labelKey: 'area.home', icon: <LayoutDashboard />, to: '/home' },
  { id: 'parts', labelKey: 'nav.parts', icon: <Boxes />, to: '/parts' },
  { id: 'flows', labelKey: 'area.flows', shortKey: 'area.flows.short', icon: <Workflow />, to: '/flows' },
  { id: 'resources', labelKey: 'area.resources', icon: <Library />, to: '/resources' },
];

/**
 * Which rail area a pathname belongs to, so detail pages (a run, a template,
 * a part) keep their area highlighted.
 */
export function areaOf(pathname: string): AreaMeta['id'] | null {
  const seg = pathname.split('/').filter(Boolean);
  switch (seg[0]) {
    case 'home':
      return 'home';
    case 'parts':
      return 'parts';
    case 'flows':
    case 'runs':
    case 'workflows':
    case 'template-library':
    case 'collaboration':
    case 'agent-executions':
      return 'flows';
    case 'approvals':
    case 'approval-requests':
      return 'approvals';
    case 'resources':
    case 'assets':
    case 'drafting':
      return 'resources';
    case 'project':
    case 'tasks':
      return 'project';
    case 'projects':
      return seg[2] === 'collaboration' ? 'flows' : 'project';
    default:
      return null;
  }
}

/** Old project section → area URL (without query). */
export const SECTION_TARGET: Record<ProjectSectionId, string> = {
  overview: '/project/overview',
  tasks: '/project/tasks',
  members: '/settings',
  approvals: '/project/approvals',
  // The asset center's own ?tab= picks the resources tab (assetCenterTarget).
  assets: '/resources/files',
  files: '/resources/files',
  parts: '/parts',
  runs: '/flows/runs',
  workflows: '/flows/templates',
  collaboration: '/flows/collaboration',
  agents: '/flows/agents',
};

/** The query parameter that makes a project current when a link is opened. */
export const PROJECT_PARAM = 'project';

/** Scope of the resources page other than one project (`?scope=all`; a project scope is `?project=<id>`). */
export const RESOURCE_SCOPE_PARAM = 'scope';

/**
 * Old `asset_type` filter → the materials type of that asset type (ASSET_BUSINESS_TYPE, the catalogue's own mapping),
 * plus the raw kind as `subtype` when the type groups several asset kinds; null for an unknown asset type.
 */
function legacyAssetType(assetType: string): { type: string; subtype?: string } | null {
  if (!Object.prototype.hasOwnProperty.call(ASSET_BUSINESS_TYPE, assetType)) return null;
  const type = ASSET_BUSINESS_TYPE[assetType as keyof typeof ASSET_BUSINESS_TYPE];
  const kinds = Object.values(ASSET_BUSINESS_TYPE).filter((t) => t === type).length;
  return kinds > 1 ? { type, subtype: assetType } : { type };
}

/** Keys the materials view understands (see features/resources/scope.ts); legacy keys are mapped or dropped. */
const MATERIAL_URL_KEYS = new Set([PROJECT_PARAM, RESOURCE_SCOPE_PARAM, 'q', 'type', 'subtype', 'source', 'part_id', 'status', 'archived', 'item', 'version']);
/** Keys of the old files tab / asset catalogue that the materials view replaces. */
const LEGACY_FILES_KEYS = ['view', 'asset_type', 'label', 'limit', 'offset'];

/**
 * Old files / asset catalogue query → the materials query: scope, search, part and archived survive, `asset_type`
 * becomes `type` (+ `subtype`), `view=assets`, labels and paging are dropped. `force` sets the type of an old tab.
 */
function materialsQuery(query: URLSearchParams, force?: { type: string; subtype?: string }): URLSearchParams {
  const out = new URLSearchParams();
  query.forEach((v, k) => {
    if (k === 'asset_type') {
      const m = legacyAssetType(v);
      if (m && !force && !query.has('type')) {
        out.set('type', m.type);
        if (m.subtype) out.set('subtype', m.subtype);
      }
    } else if (MATERIAL_URL_KEYS.has(k) && !(force && (k === 'type' || k === 'subtype'))) out.append(k, v);
  });
  if (force) {
    out.set('type', force.type);
    if (force.subtype) out.set('subtype', force.subtype);
  }
  return out;
}

/**
 * Old asset center URL (`?tab=all|resources|knowledge` plus that tab's filters) → the resources view and the query
 * without `tab`: knowledge → experience, resources → usage records, anything else → materials (filters mapped).
 */
export function assetCenterTarget(search = ''): { path: string; query: URLSearchParams } {
  const query = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  const tab = query.get('tab');
  query.delete('tab');
  if (tab === 'knowledge') return { path: '/resources/knowledge', query };
  if (tab === 'resources') return { path: '/resources/process', query };
  return { path: '/resources/files', query: materialsQuery(query) };
}

/**
 * Where an old or partial /resources URL goes (null: already a current view URL). The query is kept: specs →
 * materials of type spec, drafting → standalone 2D drawings, machines → usage records, assets → assetCenterTarget,
 * the files view with old catalogue keys → mapped, anything else → materials.
 */
export function resourcesRedirect(tab: string | undefined, search = ''): string | null {
  const query = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  const to = (path: string, q: URLSearchParams) => {
    const s = q.toString();
    return `${path}${s ? `?${s}` : ''}`;
  };
  switch (tab) {
    case 'knowledge':
    case 'process':
      return null;
    case 'files':
      return LEGACY_FILES_KEYS.some((k) => query.has(k)) ? to('/resources/files', materialsQuery(query)) : null;
    case 'assets': {
      const { path, query: q } = assetCenterTarget(search);
      return to(path, q);
    }
    case 'specs':
      return to('/resources/files', materialsQuery(query, { type: 'spec' }));
    case 'drafting':
      return to('/resources/files', materialsQuery(query, { type: 'drawing', subtype: 'drawing_document' }));
    case 'machines':
      return to('/resources/process', query);
    default:
      return resourcesRedirect('files', search) ?? to('/resources/files', query);
  }
}

/**
 * URL of an old project section in the new areas, keeping the old query
 * (monitor filters, asset center tab…) and naming the project.
 */
export function sectionUrl(section: ProjectSectionId, projectId?: string | null, search = ''): string {
  let target = SECTION_TARGET[section];
  let q = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  if (section === 'assets') ({ path: target, query: q } = assetCenterTarget(q.toString()));
  if (section === 'members') q.set('section', 'project');
  q.delete(PROJECT_PARAM);
  const out = new URLSearchParams();
  if (projectId) out.set(PROJECT_PARAM, projectId);
  q.forEach((v, k) => out.append(k, v));
  const s = out.toString();
  return `${target}${s ? `?${s}` : ''}`;
}

/** The given in-app URL (path + query) naming the project, e.g. for a return_to link. */
export function withProjectParam(url: string, projectId: string): string {
  const i = url.indexOf('?');
  const path = i < 0 ? url : url.slice(0, i);
  const q = new URLSearchParams(i < 0 ? '' : url.slice(i + 1));
  q.delete(PROJECT_PARAM);
  const out = new URLSearchParams({ [PROJECT_PARAM]: projectId });
  q.forEach((v, k) => out.append(k, v));
  return `${path}?${out.toString()}`;
}

/**
 * Where the old /projects/:id[/:section] URLs go. Unknown sections land on the
 * project's task list.
 */
export function legacyProjectUrl(projectId: string, section: string | undefined, search = ''): string {
  if (section === 'knowledge') {
    const q = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
    const keep = new URLSearchParams({ tab: 'knowledge' });
    for (const key of ['collection_id', 'collection_version_id', 'q']) {
      const v = q.get(key);
      if (v) keep.set(key, v);
    }
    return sectionUrl('assets', projectId, keep.toString());
  }
  return sectionUrl(isProjectSection(section) ? section : 'tasks', projectId, isProjectSection(section) ? search : '');
}

/**
 * Where switching the current project leaves the user: area pages stay on
 * their tab; pages of one object (a part, a run, a task…) belong to the old
 * project, so the user goes to that area's list instead. Query strings are
 * dropped (filters are per project).
 */
export function projectSwitchTarget(pathname: string): string {
  const seg = pathname.split('/').filter(Boolean);
  switch (seg[0]) {
    case 'flows':
      return isFlowTab(seg[1]) ? `/flows/${seg[1]}` : '/flows';
    case 'resources':
      return isResourceTab(seg[1]) ? `/resources/${seg[1]}` : '/resources';
    case 'project':
      return isProjectTab(seg[1]) ? `/project/${seg[1]}` : '/project';
    case 'parts':
      return '/parts';
    case 'runs':
    case 'workflows':
    case 'collaboration':
      return '/flows';
    case 'agent-executions':
      return '/flows/executions';
    case 'template-library':
      return '/flows/library';
    case 'tasks':
    case 'projects':
      return '/project';
    case 'assets':
      return '/resources';
    case 'approval-requests':
      return '/approvals';
    case 'home':
    case 'approvals':
    case 'settings':
    case 'assistant':
      return `/${seg.join('/')}`;
    default:
      return '/home';
  }
}

/** Execution monitor filters (query string of /projects/:id/runs). */
export interface MonitorFilters {
  task?: string;
  /** Only runs without a task (independent exploration). */
  unlinked?: boolean;
  workflow?: string;
  version?: string;
  /** Comma-separated run statuses, or the shortcuts "active" / "failed". */
  status?: string;
  by?: string;
}

export type TemplateTab = 'overview' | 'config' | 'versions' | 'usage';

/** Former asset center tabs (design §5.3), still the vocabulary of paths.assetsTab. */
export const ASSET_TABS = ['all', 'resources', 'knowledge'] as const;
export type AssetTab = (typeof ASSET_TABS)[number];

type QueryValues = Record<string, string | number | boolean | null | undefined>;

/** Query of the given values: empty / false / null are left out, true is `1`. */
function toQuery(query: QueryValues = {}): URLSearchParams {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '' || v === false) continue;
    q.set(k, v === true ? '1' : String(v));
  }
  return q;
}

function withQuery(base: string, query: QueryValues = {}): string {
  const s = toQuery(query).toString();
  return s ? `${base}?${s}` : base;
}

export const paths = {
  /** Public landing page. */
  home: () => '/',
  /** Personal workbench (sign-in lands here). */
  workbench: () => '/home',
  login: () => '/login',
  projects: () => '/projects',
  /** A project section in its area, making the project current (default: the project's task list). */
  project: (projectId: string, section: ProjectSectionId = 'tasks') => sectionUrl(section, projectId),
  parts: (projectId?: string) => withQuery('/parts', { [PROJECT_PARAM]: projectId }),
  flows: (tab: FlowTab = 'runs', projectId?: string) => withQuery(`/flows/${tab}`, { [PROJECT_PARAM]: projectId }),
  resources: (tab: ResourceTab = 'files', projectId?: string) => withQuery(`/resources/${tab}`, { [PROJECT_PARAM]: projectId }),
  projectTab: (tab: ProjectTab = 'tasks', projectId?: string) => withQuery(`/project/${tab}`, { [PROJECT_PARAM]: projectId }),
  /** Project execution monitor, optionally pre-filtered. */
  runs: (projectId: string, f: MonitorFilters = {}) => {
    const q = new URLSearchParams();
    if (f.task) q.set('task', f.task);
    if (f.unlinked) q.set('unlinked', '1');
    if (f.workflow) q.set('workflow', f.workflow);
    if (f.version) q.set('version', f.version);
    if (f.status) q.set('status', f.status);
    if (f.by) q.set('by', f.by);
    return sectionUrl('runs', projectId, q.toString());
  },
  run: (runId: string) => `/runs/${runId}`,
  workflow: (workflowId: string, tab?: TemplateTab) => (tab && tab !== 'overview' ? `/workflows/${workflowId}/${tab}` : `/workflows/${workflowId}`),
  templateLibrary: () => '/flows/library',
  publicTemplate: (publicTemplateId: string) => `/template-library/${publicTemplateId}`,
  task: (taskId: string) => `/tasks/${taskId}`,
  asset: (assetId: string, versionId?: string) => (versionId ? `/assets/${assetId}?version=${versionId}` : `/assets/${assetId}`),
  /** Asset version detail with the exact version and the in-app page to return to (same project only). */
  assetVersion: (assetId: string, versionId: string, returnTo?: string) =>
    withQuery(`/assets/${assetId}`, { version: versionId, return_to: returnTo }),
  /** Materials of a project (or the remembered scope), optionally filtered by type / raw kind or opening one item. */
  materials: (projectId?: string | null, f: { type?: string; subtype?: string; item?: string; version?: string } = {}) =>
    withQuery('/resources/files', { [PROJECT_PARAM]: projectId, type: f.type, subtype: f.subtype, item: f.item, version: f.version }),
  /**
   * A former asset center tab with its filters: all → materials (asset_type → type, q, part_id, archived kept),
   * resources → usage records (run_id, node_id, kind, …, limit, offset), knowledge → experience (collection_id, …).
   */
  assetsTab: (projectId: string, tab: AssetTab = 'all', query: QueryValues = {}) =>
    tab === 'all'
      ? `/resources/files?${materialsQuery(toQuery({ [PROJECT_PARAM]: projectId, ...query })).toString()}`
      : withQuery(`/resources/${tab === 'resources' ? 'process' : 'knowledge'}`, { [PROJECT_PARAM]: projectId, ...query }),
  knowledge: (projectId: string, collectionId?: string, versionId?: string) =>
    withQuery('/resources/knowledge', { [PROJECT_PARAM]: projectId, collection_id: collectionId, collection_version_id: versionId }),
  // Agent collaboration layer (design §5.1).
  collaboration: (projectId: string) => sectionUrl('collaboration', projectId),
  collaborationNew: (projectId: string) => `/projects/${projectId}/collaboration/new`,
  collaborationDraft: (workflowId: string) => `/collaboration/drafts/${workflowId}`,
  collaborationRun: (runId: string, nodeId?: string) => withQuery(`/collaboration/runs/${runId}`, { node: nodeId }),
  agents: (projectId: string) => sectionUrl('agents', projectId),
  /** One agent profile's page on the Agents tab. */
  agentProfile: (projectId: string, agentId: string) => withQuery('/flows/agents', { [PROJECT_PARAM]: projectId, agent: agentId }),
  // Agent execution layer P1: one execution's live / replayed record.
  agentExecution: (executionId: string) => `/agent-executions/${executionId}`,
  // Unified AI workspace (full page; the dock has no route).
  assistant: (conversationId?: string) => (conversationId ? `/assistant/${conversationId}` : '/assistant'),
  approvals: () => '/approvals',
  approvalRequest: (requestId: string) => `/approval-requests/${requestId}`,
  part: (partId: string, tab?: string) => (tab ? `/parts/${partId}/${tab}` : `/parts/${partId}`),
  settings: (section?: 'personal' | 'project' | 'models', projectId?: string) => withQuery('/settings', { section, [PROJECT_PARAM]: projectId }),
};
