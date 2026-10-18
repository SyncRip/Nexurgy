import { featureEnabled,useCapabilities } from '../../api/capabilities';
import { flowTabFeature } from '../../app/featureAccess';
// /flows/:tab — AI workflows (R1.2 prototype #flows.*): tabs with counts —
// workflows (the run board / list), templates, agents, and the agent executions
// record (Agent prototype #executions, execution layer P1). Collaboration and the
// public template library keep their URLs (/flows/collaboration, /flows/library)
// and open as sections of the templates tab.

import { Boxes,ChevronDown,Play,Plus } from 'lucide-react';
import { lazy,Suspense,useMemo,useState } from 'react';
import { Link,Navigate,useNavigate,useParams } from 'react-router-dom';
import { FLOW_TAB_META,FLOW_TEMPLATE_SECTION_META,flowMainTab,isFlowTab,paths,type FlowTab } from '../../app/nav';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { Menu } from '../../components/ui/Menu';
import { useToast } from '../../components/ui/Toast';
import { buildBoard,countLabel,LANES } from '../../features/flows/board';
import { BOARD_LIMIT,FlowsDataProvider,useFlowsDataLoader } from '../../features/flows/data';
import '../../features/flows/flows.css';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../project/tabProps';
import { AreaFrame,type AreaContext } from './AreaFrame';

const RunsTab = lazy(() => import('../../features/workflow/RunsTab').then((m) => ({ default: m.RunsTab })));
const WorkflowsTab = lazy(() => import('../../features/workflow/WorkflowsTab').then((m) => ({ default: m.WorkflowsTab })));
const ProjectCollaborationTab = lazy(() => import('../../features/collaboration/ProjectCollaborationTab'));
const ProjectAgentsTab = lazy(() => import('../../features/agents/ProjectAgentsTab'));
const TeamSettings = lazy(() => import('../../features/agent-team/TeamSettings'));
const ExecutionList = lazy(() => import('../../features/agentExec/ExecutionList'));
const TemplateLibraryView = lazy(() => import('../templates/TemplateLibraryPage').then((m) => ({ default: m.TemplateLibraryView })));
const StartRunDialog = lazy(() => import('../../features/templates/StartRunDialog').then((m) => ({ default: m.StartRunDialog })));

const TABS: Record<Exclude<FlowTab, 'library'>, React.ComponentType<ProjectTabProps>> = {
  runs: RunsTab,
  templates: WorkflowsTab,
  collaboration: ProjectCollaborationTab,
  agents: ProjectAgentsTab,
  executions: ExecutionList,
  team: TeamSettings,
};

/** "New workflow": run a published template, start a collaboration demand, or go to a part. */
function NewFlowMenu({ ctx }: { ctx: AreaContext }) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  if (!ctx.caps.workflow) return null;
  return (
    <>
      <Menu
        label={t('fl.new.menu')}
        triggerClassName="btn primary"
        trigger={
          <>
            <Plus aria-hidden /> {t('fl.new')} <ChevronDown aria-hidden />
          </>
        }
        items={[
          { id: 'template', label: t('fl.new.fromTemplate'), icon: <Play />, onSelect: () => setStarting(true) },
          { id: 'part', label: t('fl.new.forPart'), icon: <Boxes />, onSelect: () => navigate(paths.parts()) },
        ]}
      />
      {starting && (
        <Suspense fallback={null}>
          <StartRunDialog
          projectId={ctx.project.id}
          onClose={() => setStarting(false)}
          onStarted={(run) => {
            setStarting(false);
            toast(t('wf.runStarted'));
              navigate(paths.run(run.id));
            }}
          />
        </Suspense>
      )}
    </>
  );
}

export default function FlowsPage() {
  const capabilities = useCapabilities();
  const visibleTab = (id: string) => { const feature = flowTabFeature(id); return !feature || featureEnabled(feature, capabilities); };
  const { t } = useI18n();
  const { tab } = useParams();
  const { projectId } = useCurrentProject();
  const data = useFlowsDataLoader(projectId);

  const runs = data?.runs.data;
  const drafts = data?.drafts.data;
  const boardCount = useMemo(() => {
    if (!runs || !drafts) return undefined;
    if (runs.length > BOARD_LIMIT) return countLabel(runs.length, BOARD_LIMIT);
    const board = buildBoard(runs, drafts);
    return LANES.reduce((n, lane) => n + board[lane].length, 0);
  }, [runs, drafts]);

  if (!isFlowTab(tab)) return <Navigate to={paths.flows('runs')} replace />;
  const main = flowMainTab(tab);
  const meta = FLOW_TAB_META.find((m) => m.id === main)!;
  const section = main === 'templates' ? FLOW_TEMPLATE_SECTION_META.find((m) => m.id === tab) : undefined;
  const crumbs = [{ label: t(meta.labelKey), to: section && tab !== 'templates' ? paths.flows(main) : undefined }, ...(section && tab !== 'templates' ? [{ label: t(section.labelKey) }] : [])];

  return (
    <FlowsDataProvider value={data}>
      <AreaFrame
        title={t('area.flows')}
        subtitle={t(tab === 'team' ? 'teamSettings.subtitle' : 'fl.subtitle')}
        crumbs={crumbs}
        actions={tab === 'team' ? undefined : (ctx) => <NewFlowMenu ctx={ctx} />}
        tabs={{
          label: t('area.flows.tabs'),
          items: FLOW_TAB_META.filter((item) => visibleTab(item.id)),
          to: (id) => paths.flows(id),
          activeId: main,
          counts: { runs: boardCount, templates: data?.templates.data?.length },
        }}
      >
        {(ctx) => {
          const body = tab === 'library' ? <TemplateLibraryView /> : (() => {
            const Tab = TABS[tab];
            return <Tab project={ctx.project} caps={ctx.caps} reloadProject={ctx.reloadProject} />;
          })();
          if (main !== 'templates' || FLOW_TEMPLATE_SECTION_META.length < 2) return body;
          return (
            <div className="stack">
              <nav className="fl-sections" aria-label={t('fl.tpl.sections')}>
                <div className="seg">
                  {FLOW_TEMPLATE_SECTION_META.filter((item) => visibleTab(item.id)).map((s) => (
                    <Link key={s.id} to={paths.flows(s.id)} className="seg-btn" aria-current={s.id === tab ? 'page' : undefined}>
                      {s.icon}
                      {t(s.labelKey)}
                    </Link>
                  ))}
                </div>
              </nav>
              {body}
            </div>
          );
        }}
      </AreaFrame>
    </FlowsDataProvider>
  );
}
