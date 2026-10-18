import { useFeature } from '../../api/capabilities';
// Task detail at /tasks/:taskId: header with status and transitions, then
// sections — goal & acceptance, baseline, execution, deliverables,
// submissions, activity (design §8.2). The selected section is kept in ?tab=.
// "Work on this with AI" opens the assistant on the task's latest conversation
// (or a new one bound to the task); objective / criteria / selected text are
// added to it only through their explicit "Add to AI" entries.

import { Activity,Eye,Layers,ListChecks,Package,PauseCircle,PlayCircle,Send,Sparkles } from 'lucide-react';
import { useRef,type KeyboardEvent } from 'react';
import { Link,useParams,useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { getProjectFull,getTask } from '../../api/pm';
import { paths } from '../../app/nav';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { EmptyState,ErrorState,Skeleton,SkeletonRows } from '../../components/ui/States';
import { useAssistant,useAssistantPageContext } from '../../features/assistant/AssistantProvider';
import { AutoSelectionCapture } from '../../features/assistant/SelectionContext';
import { taskRef } from '../../features/assistant/refs';
import { FeatureNotice } from '../../features/capabilities/FeatureGate';
import { BlockedChip,isOverdue,LifecycleChip,PriorityChip,TaskStatusChip,useMembers } from '../../features/tasks/common';
import { ActivitySection } from '../../features/tasks/detail/ActivitySection';
import { BaselineSection } from '../../features/tasks/detail/BaselineSection';
import { DeliverablesSection } from '../../features/tasks/detail/DeliverablesSection';
import { ExecutionSection } from '../../features/tasks/detail/ExecutionSection';
import { GoalSection } from '../../features/tasks/detail/GoalSection';
import { SubmissionsSection } from '../../features/tasks/detail/SubmissionsSection';
import { TaskActions } from '../../features/tasks/detail/TaskActions';
import type { TaskSectionProps } from '../../features/tasks/detail/shared';
import '../../features/workflow/workflow.css';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { dueDayForDisplay } from '../../lib/dueDate';

const SECTIONS = ['goal', 'baseline', 'execution', 'deliverables', 'submissions', 'activity'] as const;
type SectionId = (typeof SECTIONS)[number];

function SectionIcon({ id }: { id: SectionId }) {
  switch (id) {
    case 'goal':
      return <ListChecks aria-hidden />;
    case 'baseline':
      return <Layers aria-hidden />;
    case 'execution':
      return <PlayCircle aria-hidden />;
    case 'deliverables':
      return <Package aria-hidden />;
    case 'submissions':
      return <Send aria-hidden />;
    default:
      return <Activity aria-hidden />;
  }
}

export default function TaskDetailPage() {
  const tasksEnabled = useFeature('tasks');
  const { t, fmt } = useI18n();
  const { taskId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const res = useResource((signal) => getTask(taskId, { signal }), [taskId]);
  // Never show the previous task's data while another one loads.
  const task = res.data && res.data.id === taskId ? (tasksEnabled ? res.data : { ...res.data, permissions: { edit: false, participate: false } }) : undefined;
  const projectId = task?.project_id;
  const project = useResource((signal) => getProjectFull(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const members = useMembers(projectId);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const assistant = useAssistant();

  // Offer the task to the assistant (never added automatically).
  useAssistantPageContext(
    task
      ? {
          projectId: task.project_id,
          label: t('ar.task.pageLabel', { key: task.key, title: task.title }),
          taskId: task.id,
          ...(task.part_id ? { partId: task.part_id } : {}),
          suggestions: [{ ...taskRef(task.project_id, task.id), label: t('ar.task.suggestion', { key: task.key, title: task.title }) }],
        }
      : null,
  );

  const raw = params.get('tab');
  const section: SectionId = (SECTIONS as readonly string[]).includes(raw ?? '') ? (raw as SectionId) : 'goal';
  const setSection = (s: SectionId) => {
    const next = new URLSearchParams(params);
    if (s === 'goal') next.delete('tab');
    else next.set('tab', s);
    setParams(next, { replace: true });
  };

  useShellProject(project.data);
  useCrumbs([
    { label: t('nav.project'), to: paths.projectTab() },
    ...(projectId ? [{ label: t('nav.tasks'), to: paths.project(projectId, 'tasks') }] : []),
    ...(task ? [{ label: task.key, mono: true }] : []),
  ]);

  if (res.error && !task) {
    if (isApiError(res.error) && res.error.status === 404) {
      return (
        <div className="page">
          <EmptyState title={t('tk.detail.notFound')} body={t('tk.detail.notFoundBody')} action={<Link to={paths.projects()} className="btn">{t('nav.projects')}</Link>} />
        </div>
      );
    }
    return (
      <div className="page">
        <ErrorState error={res.error} onRetry={res.reload} />
      </div>
    );
  }
  if (!task) {
    return (
      <div className="page stack">
        <div className="stack-sm">
          <Skeleton w="20%" h={12} />
          <Skeleton w="50%" h={24} />
        </div>
        <SkeletonRows rows={6} cols={3} />
      </div>
    );
  }

  const p = project.data;
  const archived = !!p && (p.status === 'archived' || !!p.archived_at);
  const projectActive = tasksEnabled && !!p && !archived && p.lifecycle === 'active';
  const projectPlannable = tasksEnabled && !!p && !archived && ['draft', 'active', 'on_hold'].includes(p.lifecycle);
  const sectionProps: TaskSectionProps = { task, setTask: (x) => res.setData(x), reloadTask: res.reload, project: p, members, projectActive, projectPlannable };
  const pendingReview = task.latest_submission?.status === 'pending' ? task.latest_submission : null;
  const counts: Partial<Record<SectionId, number>> = {
    goal: task.criteria.length,
    execution: task.active_run_count,
    deliverables: task.deliverables.length,
  };

  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let j = -1;
    if (e.key === 'ArrowRight') j = (i + 1) % SECTIONS.length;
    else if (e.key === 'ArrowLeft') j = (i - 1 + SECTIONS.length) % SECTIONS.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = SECTIONS.length - 1;
    if (j < 0) return;
    e.preventDefault();
    setSection(SECTIONS[j]);
    tabRefs.current[SECTIONS[j]]?.focus();
  };

  return (
    <div className="page tk-detail">
      <FeatureNotice feature="tasks" />
      <header className="tk-detail-head">
        <div className="tk-detail-titles">
          <div className="row">
            <span className="mono faint">{task.key}</span>
            <TaskStatusChip status={task.status} />
            <PriorityChip priority={task.priority} hideNormal />
            {task.blocked_reason && <BlockedChip reason={task.blocked_reason} />}
            {task.due_date && (
              <span className={`small ${isOverdue(task.due_date, task.status) ? 'err-text' : 'faint'}`}>
                {t('tk.detail.due', { date: fmt.date(dueDayForDisplay(task.due_date)) })}
                {isOverdue(task.due_date, task.status) && ` · ${t('tk.overdue')}`}
              </span>
            )}
          </div>
          <h1 className="tk-detail-title">{task.title}</h1>
          <span className="faint small">
            {t('tk.detail.version', { v: task.version })} · {t('tk.detail.updated', { time: fmt.relative(task.updated_at) })}
          </span>
        </div>
        <div className="tk-detail-actions row">
          {tasksEnabled && <button
            type="button"
            className="btn sm"
            onClick={() => assistant.open({ projectId: task.project_id, taskId: task.id, focus: true })}
            title={t('ar.task.useAiTitle', { key: task.key })}
          >
            <Sparkles aria-hidden /> {t('ar.task.useAi')}
          </button>}
          {tasksEnabled && <TaskActions key={task.id} task={task} setTask={(x) => res.setData(x)} reloadTask={res.reload} disabled={!projectPlannable} />}
        </div>
      </header>

      {p && !projectActive && (
        <div className="callout warn slim">
          <PauseCircle aria-hidden />
          <span className="row">
            <LifecycleChip lifecycle={p.lifecycle} />
            {archived ? t('tk.detail.projectArchived') : t('tk.detail.projectInactive')}
          </span>
        </div>
      )}
      {pendingReview && (
        <div className="callout info slim">
          <Eye aria-hidden />
          <span>
            {t('tk.detail.inReview', { n: pendingReview.seq })}{' '}
            {pendingReview.request_id && <Link to={paths.approvalRequest(pendingReview.request_id)}>{t('tk.sub.openRequest')}</Link>}
          </span>
        </div>
      )}
      {task.status === 'done' && task.open_impacts.length > 0 && (
        <div className="callout warn slim">
          <Layers aria-hidden />
          <span>{t('tk.detail.doneWithImpacts', { n: task.open_impacts.length })}</span>
        </div>
      )}

      <div className="tabs tk-tabs" role="tablist" aria-label={t('tk.detail.sections')}>
        {SECTIONS.map((s, i) => (
          <button
            key={s}
            ref={(el) => {
              tabRefs.current[s] = el;
            }}
            type="button"
            role="tab"
            id={`tk-tab-${s}`}
            aria-selected={section === s}
            aria-controls={`tk-panel-${s}`}
            tabIndex={section === s ? 0 : -1}
            className={`tab ${section === s ? 'active' : ''}`}
            onClick={() => setSection(s)}
            onKeyDown={(e) => onTabKey(e, i)}
          >
            <SectionIcon id={s} />
            <span>{t(`tk.section.${s}`)}</span>
            {counts[s] ? <span className="tk-count mono">{counts[s]}</span> : null}
          </button>
        ))}
      </div>

      <div key={task.id} className="section-body" role="tabpanel" id={`tk-panel-${section}`} aria-labelledby={`tk-tab-${section}`}>
        {project.error && !p ? <ErrorState error={project.error} onRetry={project.reload} /> : null}
        {section === 'goal' && <GoalSection {...sectionProps} />}
        {section === 'baseline' && <BaselineSection {...sectionProps} />}
        {section === 'execution' && <ExecutionSection {...sectionProps} />}
        {section === 'deliverables' && <DeliverablesSection {...sectionProps} />}
        {section === 'submissions' && <SubmissionsSection {...sectionProps} />}
        {section === 'activity' && <ActivitySection {...sectionProps} />}
      </div>
      <AutoSelectionCapture />
    </div>
  );
}
