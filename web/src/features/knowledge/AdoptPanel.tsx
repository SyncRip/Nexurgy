// "Reference or adopt" an archived item (UI05; handoff §4.1 Adopting experience): the
// person names the target project, the target task (and through it the
// workflow node) and the usage — reference records a relationship only,
// adopting as task input proposes the exact version for an input slot. The
// target may be any accessible project where the person has the workflow role
// (the server needs view on the source and workflow on the target); it
// defaults to the collection's own project. The adoption mode follows the
// target task's instance: semi_auto needs the node's human reviewer to confirm
// before the input changes; full_auto adoption is the executing agent's call
// within its scope. Either way the target task is accepted anew — nothing is
// inherited from the archived run or its project.

import '../resources/experience.css';
import { Bot, GitBranch, Link2, Send, TriangleAlert, UserCheck } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { collaborationOf, useKnowledgeItem, type KnowledgeCollectionDetail, type KnowledgeItem, type KnowledgeUsageKind } from '../../api/knowledge';
import { getTask, listTasks } from '../../api/pm';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import { canInProject, type ProjectCapabilities } from '../../auth/permissions';
import { useOptionalCurrentProject } from '../../components/shell/CurrentProject';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import { ExecutionModeChip } from '../assets/collabMeta';
import { VersionToken } from '../assets/meta';
import { useMembers } from '../tasks/common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

const USE_ERRORS: ErrorOverrides = {
  item_not_reusable: 'kb.err.item_not_reusable',
  mode_forbids: 'kb.err.mode_forbids',
  actor_not_allowed: 'kb.err.actor_not_allowed',
  not_a_member: 'kb.err.not_a_member',
  task_closed: 'kb.err.task_closed',
  slot_required: 'kb.err.slot_required',
  version_conflict: 'error.version_conflict',
};

const SLOT_RE = /^[A-Za-z][A-Za-z0-9_-]{0,59}$/;

/** Projects the item can be used in: workflow role there (so not archived). The source project comes first. */
function targetProjects(listed: readonly Project[] | undefined, source: Project): Project[] {
  const all = listed ?? [source];
  const ok = all.filter((p) => canInProject(p, 'workflow'));
  return [...ok.filter((p) => p.id === source.id), ...ok.filter((p) => p.id !== source.id)];
}

const projectLabel = (p: Pick<Project, 'name' | 'code'>) => `${p.name} · ${p.code}`;

export function AdoptPanel({ project, caps, collection, item, onDone }: { project: Project; caps: ProjectCapabilities; collection: KnowledgeCollectionDetail; item: KnowledgeItem; onDone: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const shell = useOptionalCurrentProject();
  const targets = useMemo(() => targetProjects(shell?.projects, project), [shell?.projects, project]);
  const targetsLoading = !!shell && !shell.projects && shell.projectsLoading;
  const [targetId, setTargetId] = useState(caps.workflow ? project.id : '');
  const target = targets.find((p) => p.id === targetId) ?? null;
  const crossProject = !!target && target.id !== project.id;
  const members = useMembers(target?.id);
  const [taskId, setTaskId] = useState('');
  const [usage, setUsage] = useState<KnowledgeUsageKind>('reference');
  const [slot, setSlot] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);

  // The task list is tagged with its project, so a previous target's tasks never show under a new one.
  const tasks = useResource(
    (signal) => listTasks(targetId, { status: 'todo,in_progress,in_review' }, { signal }).then((list) => ({ projectId: targetId, list })),
    [targetId],
    { enabled: !!target },
  );
  const taskList = tasks.data?.projectId === targetId ? tasks.data.list : undefined;
  const task = useResource((signal) => getTask(taskId, { signal }), [taskId], { enabled: !!taskId });
  const detail = task.data && task.data.id === taskId ? task.data : undefined;
  const collab = detail ? collaborationOf(detail) : null;
  const mode = collab?.execution_mode ?? null;
  const reviewerName = detail?.reviewer_id ? members.name(detail.reviewer_id) : null;

  const readOnly = collection.status === 'deprecated' || (!targetsLoading && !targets.length);
  // Adopting needs a reusable item and a target the person may adopt into (a full-auto node adopts through its agent).
  const baselineBlocked = !item.reusable ? t('kb.adopt.notReusable') : mode === 'full_auto' ? t('kb.adopt.noteFullTitle') : null;
  const slotTrim = slot.trim();
  const errProject = !target ? t('rx.adopt.errProject') : null;
  const errTask = !taskId ? t('kb.adopt.errTask') : null;
  const errReason = !reason.trim() ? t('kb.adopt.errReason') : null;
  const errSlot = usage !== 'baseline' ? null : !slotTrim ? t('kb.adopt.errSlot') : !SLOT_RE.test(slotTrim) ? t('kb.adopt.errSlotFormat') : null;

  const chooseTarget = (id: string) => {
    if (id === targetId) return;
    setTargetId(id);
    setTaskId('');
    setError(null);
    setTouched(false);
  };

  const submit = async () => {
    setTouched(true);
    if (!target || errTask || errReason || errSlot || (usage === 'baseline' && baselineBlocked)) return;
    setBusy(true);
    setError(null);
    try {
      const u = await useKnowledgeItem(item.id, { usage, target: { task_id: taskId }, slot_key: usage === 'baseline' ? slotTrim : undefined, reason: reason.trim() });
      const vars = { label: item.version.label, project: projectLabel(target) };
      if (usage === 'reference') toast(t('rx.adopt.referencedToast', vars));
      else if (u.confirmation === 'pending') toast(t('rx.adopt.proposedToast', { ...vars, who: reviewerName ?? t('kb.adopt.reviewer') }));
      else toast(t('rx.adopt.adoptedToast', vars));
      setReason('');
      setSlot('');
      setTouched(false);
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const off = readOnly || busy;
  return (
    <section className="panel kb-adopt" aria-labelledby="kb-adopt-h">
      <div className="panel-h">
        <h2 id="kb-adopt-h">{t('rx.adopt.title')}</h2>
        <span className="faint small ellip">
          {item.version.asset_name} <VersionToken label={item.version.label} />
        </span>
      </div>
      <div className="panel-b stack">
        {readOnly && <p className="faint small">{collection.status === 'deprecated' ? t('kb.adopt.deprecated') : t('rx.adopt.noTargets')}</p>}
        <div className="kb-adopt-grid">
          <div className="stack-sm">
            <dl className="kb-adopt-facts">
              <div>
                <dt>{t('rx.adopt.source')}</dt>
                <dd className="ellip small" title={projectLabel(project)}>
                  {projectLabel(project)}
                </dd>
              </div>
            </dl>
            <Field label={t('rx.adopt.targetProject')} hint={t('rx.adopt.targetProjectHint')} error={touched && errProject ? errProject : null}>
              <select className="select" value={target ? targetId : ''} onChange={(e) => chooseTarget(e.target.value)} disabled={off}>
                <option value="">{targetsLoading ? t('common.loading') : t('rx.adopt.targetProjectPlaceholder')}</option>
                {targets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {projectLabel(p)}
                    {p.id === project.id ? ` · ${t('rx.adopt.sourceMark')}` : ''}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('kb.adopt.target')} error={touched && !errProject && errTask ? errTask : null}>
              <select className="select" value={taskId} onChange={(e) => setTaskId(e.target.value)} disabled={off || !target}>
                <option value="">{target && !taskList && !tasks.error ? t('common.loading') : t('kb.adopt.targetPlaceholder')}</option>
                {(taskList ?? []).map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.key} · {x.title}
                  </option>
                ))}
              </select>
            </Field>
            {taskList && !taskList.length && <p className="faint small">{t('rx.adopt.noTasks')}</p>}
            {!!tasks.error && <ErrorNotice error={tasks.error} onRetry={tasks.reload} />}
            {!!task.error && <ErrorNotice error={task.error} onRetry={task.reload} />}
            <dl className="kb-adopt-facts">
              <div>
                <dt>{t('kb.adopt.mode')}</dt>
                <dd className="row">
                  {taskId && !detail && !task.error ? (
                    <span className="faint small">{t('common.loading')}</span>
                  ) : (
                    <>
                      <ExecutionModeChip mode={mode} />
                      <span className="small muted">{!taskId ? t('kb.adopt.modeChooseTask') : mode === 'semi_auto' ? t('kb.adopt.modeSemi') : mode === 'full_auto' ? t('kb.adopt.modeFull') : t('kb.adopt.modePlain')}</span>
                    </>
                  )}
                </dd>
              </div>
              {collab && (
                <div>
                  <dt>{t('rx.adopt.node')}</dt>
                  <dd>
                    <Link to={paths.collaborationRun(collab.run_id, collab.node_id)} className="as-use-link small">
                      <GitBranch aria-hidden className="inline-ico" /> <span className="mono">{collab.node_id}</span>
                    </Link>
                  </dd>
                </div>
              )}
            </dl>
            <fieldset className="rx-usage" disabled={off}>
              <legend>{t('rx.adopt.usage')}</legend>
              <label className="check rx-usage-opt">
                <input type="radio" name={`rx-usage-${item.id}`} checked={usage === 'reference'} onChange={() => setUsage('reference')} />
                <span>
                  <b>{t('rx.adopt.usageReference')}</b>
                  <span className="faint small">{t('rx.adopt.usageReferenceHint')}</span>
                </span>
              </label>
              <label className="check rx-usage-opt">
                <input type="radio" name={`rx-usage-${item.id}`} checked={usage === 'baseline'} onChange={() => setUsage('baseline')} disabled={!item.reusable} />
                <span>
                  <b>{t('rx.adopt.usageBaseline')}</b>
                  <span className="faint small">{item.reusable ? t('rx.adopt.usageBaselineHint') : t('kb.adopt.notReusable')}</span>
                </span>
              </label>
            </fieldset>
            {usage === 'baseline' && (
              <Field label={t('kb.adopt.slot')} hint={t('kb.adopt.slotHint')} error={touched && errSlot ? errSlot : null}>
                <input className="input mono" value={slot} maxLength={60} placeholder="reference_doc" onChange={(e) => setSlot(e.target.value)} disabled={off} />
              </Field>
            )}
            <Field label={t('kb.adopt.reason')} error={touched && errReason ? errReason : null}>
              <LimitedTextarea className="input" rows={2} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} disabled={off} placeholder={t('kb.adopt.reasonPlaceholder')} />
            </Field>
            {usage === 'baseline' && mode === 'semi_auto' && (
              <dl className="kb-adopt-facts">
                <div>
                  <dt>{t('kb.adopt.confirmer')}</dt>
                  <dd>
                    <UserCheck aria-hidden className="inline-ico" /> {reviewerName ?? t('kb.adopt.noReviewer')}
                  </dd>
                </div>
              </dl>
            )}
          </div>
          <div className="stack-sm">
            {mode === 'full_auto' ? (
              <div className="callout info" role="note">
                <Bot aria-hidden />
                <div className="stack-sm">
                  <b>{t('kb.adopt.noteFullTitle')}</b>
                  <span className="small">{t('kb.adopt.noteFullBody')}</span>
                </div>
              </div>
            ) : (
              <div className="callout warn" role="note">
                <TriangleAlert aria-hidden />
                <div className="stack-sm">
                  <b>{t('kb.adopt.noteSemiTitle')}</b>
                  <span className="small">{t('kb.adopt.noteSemiBody')}</span>
                </div>
              </div>
            )}
            <p className="small muted">{crossProject && target ? t('rx.adopt.noInheritCross', { source: projectLabel(project), target: projectLabel(target) }) : t('rx.adopt.noInherit')}</p>
            <ErrorNotice error={error} overrides={USE_ERRORS} />
          </div>
        </div>
        <div className="row kb-adopt-actions">
          <button
            type="button"
            className={`btn ${usage === 'baseline' ? 'primary' : ''}`}
            disabled={off || (usage === 'baseline' && !!baselineBlocked)}
            title={usage === 'baseline' && baselineBlocked ? baselineBlocked : undefined}
            onClick={() => void submit()}
          >
            {usage === 'baseline' ? <Send /> : <Link2 />} {busy ? t('common.working') : usage === 'baseline' ? t('kb.adopt.propose') : t('rx.adopt.submitReference')}
          </button>
          {target && <span className="faint small">{t('rx.adopt.into', { project: projectLabel(target) })}</span>}
        </div>
      </div>
    </section>
  );
}
