import { useFeature } from '../../api/capabilities';
// /workflows/:workflowId/:tab? — template detail (design §5.1, §8.1):
//   overview (Overview) · config (Configuration: the draft canvas) · versions (Published versions) · usage (Usage records)
// The canvas is mounted on first visit and kept while switching tabs, so unsaved
// edits survive. Publishing, deprecating, sharing and upgrading need the template
// permission (owner or template manager); the server re-checks every action.

import '@xyflow/react/dist/style.css';
import '../../features/workflow/workflow.css';
import '../../features/templates/templates.css';
import { ReactFlowProvider } from '@xyflow/react';
import { ArrowLeft, FileText, History, Network, Play, Rocket, Tags } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { getProject, getWorkflow, listTemplateVersions, listWorkflowNodeTypes } from '../../api/endpoints';
import type { TemplateVersion, UUID, Workflow } from '../../api/types';
import { paths, type TemplateTab } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { ArchivedChip } from '../../components/ui/Badges';
import { EmptyState, ErrorState, Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { LibrarySourceChip, PublishStateChips } from '../../features/templates/common';
import { ShareDialog } from '../../features/templates/LibraryDialogs';
import { StartRunDialog } from '../../features/templates/StartRunDialog';
import { DeprecateDialog, PublishDialog, UpgradeDialog } from '../../features/templates/TemplateDialogs';
import { TemplateOverviewTab, TemplateUsageTab, TemplateVersionsTab, type TemplateTabProps } from '../../features/templates/TemplateTabs';
import { WorkflowEditor } from '../../features/workflow/editor/WorkflowEditor';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

const TABS: { id: TemplateTab; key: 'tpl.tab.overview' | 'tpl.tab.config' | 'tpl.tab.versions' | 'tpl.tab.usage'; icon: ReactNode }[] = [
  { id: 'overview', key: 'tpl.tab.overview', icon: <FileText /> },
  { id: 'config', key: 'tpl.tab.config', icon: <Network /> },
  { id: 'versions', key: 'tpl.tab.versions', icon: <Tags /> },
  { id: 'usage', key: 'tpl.tab.usage', icon: <History /> },
];

function isTab(v: string | undefined): v is TemplateTab {
  return v === 'overview' || v === 'config' || v === 'versions' || v === 'usage';
}

function PageSkeleton() {
  const { t } = useI18n();
  return (
    <div className="page" role="status" aria-label={t('common.loading')}>
      <Skeleton w={260} h={26} />
      <Skeleton w="60%" h={14} />
      <div className="wf-canvas-skel" />
    </div>
  );
}

export default function WorkflowCanvasPage() {
  const libraryEnabled = useFeature('template_library');
  const collaborationEnabled = useFeature('collaboration');
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { workflowId = '', tab: tabParam } = useParams();
  const tab: TemplateTab = isTab(tabParam) ? tabParam : 'overview';
  const [search, setSearch] = useSearchParams();
  const wfRes = useResource((signal) => getWorkflow(workflowId, { signal }), [workflowId]);
  const defs = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const versions = useResource((signal) => listTemplateVersions(workflowId, { signal }), [workflowId]);
  const wf = wfRes.data;
  const projectId = wf?.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });

  // The canvas: mounted on first visit to "config", then kept (hidden) while on other tabs.
  const [editorOn, setEditorOn] = useState(false);
  const [editorEpoch, setEditorEpoch] = useState(0);
  const [editorInitial, setEditorInitial] = useState<Workflow | null>(null);
  const [canvasDirty, setCanvasDirty] = useState(false);
  useEffect(() => {
    if (tab === 'config' && wf && !editorOn) {
      setEditorInitial(wf);
      setEditorOn(true);
    }
  }, [tab, wf, editorOn]);

  const [runFor, setRunFor] = useState<{ versionId?: UUID } | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [sharing, setSharing] = useState<{ versionId?: UUID } | null>(null);
  const [deprecating, setDeprecating] = useState<TemplateVersion | null>(null);
  const [upgrading, setUpgrading] = useState(false);

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    ...(project.data ? [{ label: t('nav.workflowTemplates'), to: paths.project(project.data.id, 'workflows') }] : []),
    ...(wf ? [{ label: wf.name }] : []),
  ]);

  const error = wfRes.error || defs.error || project.error;
  if (error && (!wf || !defs.data || !project.data)) {
    if (isApiError(error) && error.status === 404) {
      return (
        <div className="page">
          <EmptyState title={t('wf.notFound.title')} body={t('wf.notFound.body')} />
        </div>
      );
    }
    return (
      <div className="page">
        <ErrorState
          error={error}
          onRetry={() => {
            wfRes.reload();
            defs.reload();
            project.reload();
          }}
        />
      </div>
    );
  }
  if (!wf || !defs.data || !project.data) return <PageSkeleton />;

  const caps = projectCapabilities(project.data);
  const archived = !!wf.archived_at;
  const frozenGraph = !collaborationEnabled && wf.graph.nodes.some((node) => node.type === 'business_task');
  const canManage = (wf.can_manage ?? caps.workflow) && caps.workflow && !archived && !frozenGraph;
  const canRun = caps.workflow && !archived && !frozenGraph;
  const setWf = (w: Workflow) => wfRes.setData((prev) => ({ ...(prev ?? w), ...w }));
  const refresh = () => {
    wfRes.reload();
    versions.reload();
  };

  const tabProps: TemplateTabProps = {
    workflow: wf,
    project: project.data,
    versions,
    canManage,
    canRun,
    onWorkflow: setWf,
    onStartRun: (versionId) => setRunFor({ versionId }),
    onPublish: () => setPublishing(true),
    onShare: (versionId) => setSharing({ versionId }),
    onDeprecate: setDeprecating,
    onUpgrade: () => setUpgrading(true),
  };
  const versionCount = versions.data?.length ?? wf.published_count;

  return (
    <div className={`tpl-page ${tab === 'config' ? 'is-config' : ''}`}>
      <header className="tpl-head">
        <div className="tpl-title-row">
          <Link to={paths.project(wf.project_id, 'workflows')} className="btn ghost icon sm" aria-label={t('wf.backToList')} title={t('wf.backToList')}>
            <ArrowLeft />
          </Link>
          <div className="tpl-titles">
            <span className="eyebrow-sm">{t('tpl.eyebrow')}</span>
            <h1 className="ellip">{wf.name}</h1>
          </div>
          <span className="tpl-chips">
            <PublishStateChips latest={wf.latest_published ?? versions.data?.[0] ?? null} draftDirty={wf.draft_dirty || canvasDirty} publishedCount={wf.published_count} />
            {wf.library && <LibrarySourceChip name={wf.library.name} adopted={wf.library.adopted_version_no} upgrade={wf.library.upgrade_available} />}
            {archived && <ArchivedChip />}
          </span>
          <span className="grow" />
          <div className="tpl-head-actions">
            {canManage && tab !== 'config' && (
              <button type="button" className="btn sm" onClick={() => setPublishing(true)}>
                <Rocket /> {t('tpl.publish.action')}
              </button>
            )}
            {canRun && tab !== 'config' && (
              <button type="button" className="btn sm primary" onClick={() => setRunFor({})}>
                <Play /> {t('tpl.action.startRun')}
              </button>
            )}
          </div>
        </div>
        <nav className="tabs tpl-tabs" aria-label={t('tpl.tabsLabel')}>
          {TABS.map((x) => (
            <Link key={x.id} to={paths.workflow(wf.id, x.id)} className={`tab ${tab === x.id ? 'active' : ''}`} aria-current={tab === x.id ? 'page' : undefined}>
              {x.icon}
              <span>{t(x.key)}</span>
              {x.id === 'versions' && versionCount ? <span className="tpl-tab-count mono">{versionCount}</span> : null}
              {x.id === 'config' && canvasDirty ? <span className="dot tpl-dirty-dot" aria-label={t('wf.unsaved')} /> : null}
            </Link>
          ))}
        </nav>
      </header>

      {tab === 'overview' && (
        <div className="tpl-body">
          <TemplateOverviewTab {...tabProps} />
        </div>
      )}
      {tab === 'versions' && (
        <div className="tpl-body">
          <TemplateVersionsTab {...tabProps} />
        </div>
      )}
      {tab === 'usage' && (
        <div className="tpl-body">
          <TemplateUsageTab
            {...tabProps}
            versionFilter={search.get('version') ?? ''}
            onVersionFilter={(v) => {
              const next = new URLSearchParams(search);
              if (v) next.set('version', v);
              else next.delete('version');
              setSearch(next, { replace: true });
            }}
          />
        </div>
      )}
      {editorOn && editorInitial && (
        <div className="tpl-editor-slot" hidden={tab !== 'config'}>
          <ReactFlowProvider>
            {/* Keyed by id (+ epoch after an upgrade replaced the draft) for a clean editor state. */}
            <WorkflowEditor
              key={`${wf.id}:${editorEpoch}`}
              initial={editorInitial}
              project={project.data}
              defs={defs.data}
              external={wf}
              onWorkflowChange={setWf}
              onDirtyChange={setCanvasDirty}
              onRequestRun={canRun ? () => setRunFor({}) : undefined}
              onRequestPublish={canManage ? () => setPublishing(true) : undefined}
            />
          </ReactFlowProvider>
        </div>
      )}

      {canRun && runFor && (
        <StartRunDialog
          projectId={wf.project_id}
          workflow={wf}
          versionId={runFor.versionId}
          allowDraft={canManage}
          onClose={() => setRunFor(null)}
          onStarted={(run) => {
            setRunFor(null);
            toast(t('wf.runStarted'));
            navigate(paths.run(run.id));
          }}
        />
      )}
      {canManage && publishing && (
        <PublishDialog
          workflow={wf}
          unsavedCanvas={canvasDirty}
          onClose={() => setPublishing(false)}
          onReload={() => {
            setPublishing(false);
            refresh();
          }}
          onPublished={(v) => {
            setPublishing(false);
            toast(t('tpl.publish.done', { n: v.version_no }));
            refresh();
          }}
        />
      )}
      {canManage && libraryEnabled && sharing && (
        <ShareDialog
          workflow={wf}
          versions={versions.data ?? []}
          defaultVersionId={sharing.versionId}
          onClose={() => setSharing(null)}
          onShared={(pt) => {
            setSharing(null);
            toast(t('tpl.share.done', { name: pt.name }), { action: { label: t('common.open'), onClick: () => navigate(paths.publicTemplate(pt.id)) } });
          }}
        />
      )}
      {canManage && deprecating && (
        <DeprecateDialog
          version={deprecating}
          onClose={() => setDeprecating(null)}
          onDone={(v) => {
            setDeprecating(null);
            toast(t('tpl.deprecate.done', { n: v.version_no }));
            refresh();
          }}
        />
      )}
      {canManage && libraryEnabled && upgrading && (
        <UpgradeDialog
          workflow={wf}
          unsavedCanvas={canvasDirty}
          onClose={() => setUpgrading(false)}
          onUpgraded={(w) => {
            setUpgrading(false);
            setWf(w);
            setEditorInitial(w);
            setEditorEpoch((n) => n + 1);
            toast(t('tpl.upgrade.done'));
            refresh();
          }}
        />
      )}
    </div>
  );
}
