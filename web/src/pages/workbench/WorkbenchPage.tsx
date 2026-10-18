import { ArrowRight,Boxes,FolderOpen,Maximize2,MessagesSquare,PieChart,ScanLine,Sparkles,Workflow } from 'lucide-react';
import { useMemo,useState } from 'react';
import { Link,useNavigate } from 'react-router-dom';
import { listAiConversations } from '../../api/assistant';
import { listParts } from '../../api/endpoints';
import type { Part,RunSummary } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { projectCapabilities,type ProjectCapabilities } from '../../auth/permissions';
import { useCurrentProject } from '../../components/shell/CurrentProject';
import { useCrumbs } from '../../components/shell/ShellContext';
import { EmptyState,ErrorNotice,SkeletonCards,SkeletonRows,Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useAssistant } from '../../features/assistant/AssistantProvider';
import { Composer } from '../../features/assistant/Composer';
import { flowSummary,latestRunByPart } from '../../features/parts/model';
import { PartCard } from '../../features/parts/PartCard';
import { useProjectRuns } from '../../features/parts/useProjectRuns';
import { StartRunDialog } from '../../features/templates/StartRunDialog';
import { TodoCard } from '../../features/todos/Todos';
import { NewPartButton,UploadStartButton,useUploadStart } from '../../features/workbench/StartActions';
import { StatusDonut } from '../../features/workbench/StatusDonut';
import { workbenchRuns } from '../../features/workbench/runs';
import '../../features/workbench/workbench.css';
import { RunStatusChip } from '../../features/workflow/icons';
import { Reveal } from '../../hooks/Reveal';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
const PARTS_SHOWN = 6;
function greetingKey(now: Date): 'wb.greet.morning' | 'wb.greet.afternoon' | 'wb.greet.evening' { const h = now.getHours(); return h < 12 ? 'wb.greet.morning' : h < 18 ? 'wb.greet.afternoon' : 'wb.greet.evening'; }
function CommandBox({ projectId, caps }: { projectId: string | null; caps: ProjectCapabilities }) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const assistant = useAssistant();
  const [startRun, setStartRun] = useState(false);
  const upload = useUploadStart(projectId);
  const recent = useResource((signal) => listAiConversations({ project_id: projectId!, archived: 'false', limit: 1 }, { signal }), [projectId], { enabled: !!projectId });
  const last = recent.data?.[0];

  const chips = () => (
    <div className="wb-chips" role="group" aria-label={t('home.cmd.quick')}>
      {caps.design && (
        <button type="button" className="wb-chip" onClick={() => upload.pick(true)} disabled={!!upload.busy} title={upload.busy ? t('home.upload.busy', { name: upload.busy }) : undefined}>
          {upload.busy ? <Spinner /> : <ScanLine aria-hidden />} {t('home.cmd.drawing')}
        </button>
      )}
      {caps.workflow && (
        <button type="button" className="wb-chip" onClick={() => setStartRun(true)}>
          <Workflow aria-hidden /> {t('home.cmd.template')}
        </button>
      )}
      {last && (
        <button type="button" className="wb-chip" title={last.title || undefined} onClick={() => assistant.open({ conversationId: last.id, projectId: projectId ?? undefined, full: true })}>
          <MessagesSquare aria-hidden /> {t('home.cmd.continue')}
        </button>
      )}
    </div>
  );

  return (
    <section className="wb-cmd" aria-label={t('home.cmd.label')}>
      <div className="wb-cmd-head">
        <h2 className="wb-cmd-title">{t('wb.command.title')}</h2>
        <button
          type="button"
          className="btn sm wb-cmd-expand"
          disabled={!projectId}
          title={t(projectId ? 'as.head.expand' : 'as.start.needProject')}
          onClick={() => assistant.open({ newConversation: true, projectId: projectId ?? undefined, full: true })}
        >
          <Maximize2 aria-hidden /> {t('wb.command.fullscreen')}
        </button>
      </div>
      {projectId ? (
        <Composer
          variant="line"
          detail={null}
          projectId={projectId}
          placeholder={t('features.homePlaceholder')}
          leading={<Sparkles className="wb-cmd-spark" aria-hidden />}
          barStart={chips}
          sendLabel={t('home.cmd.start')}
          hint={t('features.homeHint')}
          // Sending opens the full workspace; its "Back to workbench" returns here.
          onSent={(id) => assistant.open({ conversationId: id, projectId, full: true })}
        />
      ) : (
        <p className="as-ctx-warn">{t('as.start.needProject')}</p>
      )}
      {upload.input}
      {upload.busy && (
        <span className="sr-only" role="status">
          {t('home.upload.busy', { name: upload.busy })}
        </span>
      )}
      {startRun && projectId && (
        <StartRunDialog
          projectId={projectId}
          onClose={() => setStartRun(false)}
          onStarted={(run) => {
            setStartRun(false);
            toast(t('wf.runStarted'));
            navigate(paths.run(run.id));
          }}
        />
      )}
    </section>
  );
}

function RecentParts({ projectId, canDesign, parts, runs, runsUnsure, error, onRetry }: { projectId: string; canDesign: boolean; parts: Part[] | undefined; runs: RunSummary[] | undefined; runsUnsure: boolean; error: unknown; onRetry: () => void }) {
  const { t } = useI18n();
  const latest = useMemo(() => latestRunByPart(runs ?? []), [runs]);
  const rows = (parts ?? [])
    .slice()
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, PARTS_SHOWN);
  if (parts && !parts.length && !error) return (
    <section className="panel wb-start" aria-labelledby="wb-start-title">
      <div className="wb-start-intro">
        <span className="wb-start-mark"><Boxes aria-hidden /></span>
        <div><h2 id="wb-start-title">{t('wb.start.title')}</h2><p>{t(canDesign ? 'wb.start.body' : 'wb.start.readOnly')}</p></div>
      </div>
      <nav className="wb-start-links" aria-label={t('home.cmd.quick')}>
        <Link to={paths.resources('files', projectId)}><FolderOpen aria-hidden /><span><strong>{t('wb.start.resources')}</strong><small>{t('wb.start.resourcesBody')}</small></span><ArrowRight aria-hidden /></Link>
        <Link to={paths.parts(projectId)}><Boxes aria-hidden /><span><strong>{t('wb.openParts')}</strong><small>{t('wb.start.partsBody')}</small></span><ArrowRight aria-hidden /></Link>
      </nav>
    </section>
  );
  return (
    <section className="panel wb-card" aria-labelledby="wb-parts">
      <header className="wb-card-head">
        <h2 id="wb-parts">
          <Boxes aria-hidden /> {t('wb.parts.title')}
        </h2>
        <Link to={paths.parts()} className="wb-more-link">
          {t('home.parts.all')} <ArrowRight aria-hidden />
        </Link>
      </header>
      {!!error && <ErrorNotice error={error} onRetry={onRetry} />}
      {!parts ? (
        !error &&
        <SkeletonCards count={3} />
      ) : !rows.length ? (
        <EmptyState compact icon={<Boxes className="big-ico" aria-hidden />} title={t('wb.parts.empty')} body={t('home.parts.emptyBody')} action={<Link className="btn" to={paths.parts()}>{t('wb.openParts')} <ArrowRight aria-hidden /></Link>} />
      ) : (
        <ul className="pc-grid">
          {rows.map((p) => (
            <li key={p.id}>
              <PartCard part={p} summary={flowSummary(latest.get(p.id), runsUnsure)} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function WorkbenchPage() {
  const { t, fmt } = useI18n();
  const user = useUser();
  const navigate = useNavigate();
  const toast = useToast();
  const { projectId, project } = useCurrentProject();
  useCrumbs([{ label: t('area.home') }]);
  const caps = projectCapabilities(project);
  const partsRes = useResource((signal) => listParts(projectId!, {}, { signal }), [projectId], { enabled: !!projectId });
  const runs = useProjectRuns(projectId);
  const parts = partsRes.data?.every((p) => p.project_id === projectId) ? partsRes.data : undefined;
  const visibleRuns = useMemo(() => workbenchRuns(runs.runs ?? []).slice(0, 6), [runs.runs]);
  return <div className="page wb-page">
    <header className="wb-hello page-halo">
      <div className="wb-hello-text">
        {/* Fixed page name so it sits at the same visual level as Parts/Workflows/Materials;
            the greeting + project context move to the subtitle. */}
        <h1>{t('area.home')}</h1>
        <p>
          {t(greetingKey(new Date()), { name: user.name || user.email })}
          {' · '}
          {project ? t('wb.projectContext', { name: project.name }) : t('area.noProject.title')}
        </p>
      </div>
      {project && caps.design && <div className="wb-hello-acts"><UploadStartButton projectId={project.id} /><NewPartButton project={project} folders={[...new Set((parts ?? []).map((p) => p.folder).filter(Boolean))].sort()} onSaved={(p) => { toast(t('parts.createdToast', { number: p.number })); navigate(paths.part(p.id)); }} /></div>}
    </header>
    <CommandBox projectId={projectId} caps={caps} />
    <div className="wb-home">
      <aside className="wb-side">
        <TodoCard description={t('wb.todo.scope')} />
        {parts && parts.length > 0 && (
          <Reveal>
            <section className="panel wb-card wb-chart" aria-labelledby="wb-chart-title">
              <header className="wb-card-head">
                <h2 id="wb-chart-title"><PieChart aria-hidden />{t('wb.chart.title')}</h2>
              </header>
              <p className="wb-chart-sub">{t('wb.chart.sub')}</p>
              <StatusDonut parts={parts} />
            </section>
          </Reveal>
        )}
      </aside>
      <div className="stack wb-main">
        {projectId && <Reveal><RecentParts projectId={projectId} canDesign={caps.design} parts={parts} runs={runs.runs} runsUnsure={runs.windowFull || !runs.runs} error={partsRes.error} onRetry={partsRes.reload} /></Reveal>}
        <Reveal delay={80}>
        <section className="panel wb-card">
          <header className="wb-card-head">
            <h2><Workflow aria-hidden />{t('wb.runs.title')}</h2>
            <Link className="wb-more-link" to={paths.flows('runs')}>{t('wb.runs.all')} <ArrowRight className="inline-ico" /></Link>
          </header>
          {!!runs.error && <ErrorNotice error={runs.error} onRetry={runs.reload} />}
          {!projectId ? (
            <EmptyState compact title={t('area.noProject.title')} action={<Link className="btn" to={paths.projects()}>{t('pswitch.all')}</Link>} />
          ) : !runs.runs ? (
            !runs.error && <SkeletonRows rows={3} />
          ) : !visibleRuns.length ? (
            <div className="wb-quiet"><p>{t('wb.runs.empty')}</p><span>{t('wb.runs.emptyBody')}</span></div>
          ) : (
            <ul className="wb-todo">
              {visibleRuns.map((run) => (
                <li key={run.id}>
                  <Link className="wb-todo-row" to={paths.run(run.id)}>
                    <span className="wb-todo-main"><strong>{run.title || run.id.slice(0, 8)}</strong><span className="small faint">{fmt.relative(run.created_at)}</span></span>
                    <RunStatusChip status={run.status} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
        </Reveal>
      </div>
    </div>
  </div>;
}
