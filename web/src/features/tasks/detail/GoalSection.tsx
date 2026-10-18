import { useFeature } from '../../../api/capabilities';
// Goal & acceptance: objective, people, plan fields, dependencies and the
// acceptance criteria with their rules. Objective and criteria are frozen
// while the task is in review or done (withdraw / reopen first). The objective
// and each criterion can be added to the AI assistant (and their text quoted).

import { GitBranch,Hand,Lock,Milestone,Pencil,Plus,Trash2,Users } from 'lucide-react';
import { useMemo,useState } from 'react';
import { Link } from 'react-router-dom';
import { listParts } from '../../../api/endpoints';
import {
deleteCriterion,
listMilestones,
listTasks,
patchTask,
setTaskCollaborators,
setTaskDependencies,
TASK_PRIORITIES,
type Criterion,
type TaskDetail,
type TaskPatch,
type TaskPriority,
} from '../../../api/pm';
import { paths } from '../../../app/nav';
import { useAuth } from '../../../auth/AuthProvider';
import { Chip } from '../../../components/ui/Badges';
import { ConfirmDialog,Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { EmptyState,ErrorNotice,SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';

import { LimitedTextarea } from '../../../components/ui/LimitedText';
import { dayForDisplay } from '../../../lib/dueDate';
import { aiSourceProps } from '../../assistant/SelectionContext';
import { BlockedChip,Count,fromDateInput,isOverdue,PersonName,PriorityChip,TaskStatusChip,TkError,toDateInput,type Members } from '../common';
import { CriterionDialog,ruleSummary } from '../RuleSpecEditor';
import { LOCKED_STATUSES,type TaskSectionProps } from './shared';

function EditTaskDialog({ task, members, isProjectOwner, onClose, onSaved, onReload }: { task: TaskDetail; members: Members; isProjectOwner: boolean; onClose: () => void; onSaved: (t: TaskDetail) => void; onReload: () => void }) {
  const { t } = useI18n();
  const { user } = useAuth();
  const me = user?.id ?? '';
  const stagesEnabled = useFeature('stages');
  const milestones = useResource((signal) => listMilestones(task.project_id, { signal }), [task.project_id, stagesEnabled], { enabled: stagesEnabled });
  const parts = useResource((signal) => listParts(task.project_id, {}, { signal }), [task.project_id]);
  const locked = LOCKED_STATUSES.has(task.status);
  const [title, setTitle] = useState(task.title);
  const [objective, setObjective] = useState(task.objective);
  const [ownerId, setOwnerId] = useState(task.owner_id ?? '');
  const [reviewerId, setReviewerId] = useState(task.reviewer_id ?? '');
  const [milestoneId, setMilestoneId] = useState(task.milestone_id ?? '');
  const [partId, setPartId] = useState(task.part_id ?? '');
  const [priority, setPriority] = useState<TaskPriority>(task.priority);
  const [required, setRequired] = useState(task.required !== false);
  const [start, setStart] = useState(toDateInput(task.start_date));
  const [due, setDue] = useState(toDateInput(task.due_date));
  const [blocked, setBlocked] = useState(!!task.blocked_reason);
  const [blockedReason, setBlockedReason] = useState(task.blocked_reason ?? '');
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const people = (members.list ?? []).filter((m) => m.role !== 'viewer');
  const canPick = (id: string) => isProjectOwner || id === me || id === '';
  const titleErr = !title.trim() ? t('validation.required', { field: t('tk.field.title') }) : null;
  const blockedErr = blocked && !blockedReason.trim() ? t('validation.required', { field: t('tk.field.blockedReason') }) : null;
  const dateErr = start && due && due < start ? t('tk.create.dueBeforeStart') : null;

  const submit = async () => {
    setTouched(true);
    if (titleErr || blockedErr || dateErr) return;
    const body: TaskPatch = { expected_version: task.version };
    if (title.trim() !== task.title) body.title = title.trim();
    if (!locked && objective.trim() !== task.objective) body.objective = objective.trim();
    if ((ownerId || null) !== task.owner_id) body.owner_id = ownerId || null;
    if (task.status !== 'in_review' && (reviewerId || null) !== task.reviewer_id) body.reviewer_id = reviewerId || null;
    if (stagesEnabled && (milestoneId || null) !== task.milestone_id) body.milestone_id = milestoneId || null;
    if ((partId || null) !== task.part_id) body.part_id = partId || null;
    if (priority !== task.priority) body.priority = priority;
    if (task.required !== undefined && required !== task.required) body.required = required;
    if (start !== toDateInput(task.start_date)) body.start_date = fromDateInput(start);
    if (due !== toDateInput(task.due_date)) body.due_date = fromDateInput(due);
    const br = blocked ? blockedReason.trim() : null;
    if (br !== (task.blocked_reason ?? null)) body.blocked_reason = br;
    if (Object.keys(body).length === 1) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSaved(await patchTask(task.id, body));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const personOptions = (current: string) =>
    people
      .filter((m) => canPick(m.id) || m.id === current)
      .map((m) => (
        <option key={m.id} value={m.id} disabled={!canPick(m.id)}>
          {m.name}
          {m.id === me ? ` (${t('common.you')})` : ''}
        </option>
      ));

  return (
    <Dialog
      title={t('tk.edit.title', { key: task.key })}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="stack">
        {!isProjectOwner && <p className="faint small">{t('tk.edit.assignHint')}</p>}
        <div className="form-grid">
          <Field label={t('tk.field.title')} error={touched ? titleErr : null} className="span-2">
            <input className="input" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label={t('tk.field.objective')} optional hint={locked ? t('tk.edit.objectiveLocked') : undefined} className="span-2">
            <LimitedTextarea className="input" rows={4} limit="objective" value={objective} disabled={locked} onChange={(e) => setObjective(e.target.value)} />
          </Field>
          <Field label={t('tk.field.owner')}>
            <select className="select" value={ownerId} onChange={(e) => setOwnerId(e.target.value)} disabled={!members.list}>
              <option value="" disabled={!canPick('')}>
                {t('tk.unassigned')}
              </option>
              {personOptions(task.owner_id ?? '')}
            </select>
          </Field>
          <Field label={t('tk.field.reviewer')} hint={task.status === 'in_review' ? t('tk.edit.reviewerLocked') : t('tk.edit.reviewerHint')}>
            <select className="select" value={reviewerId} onChange={(e) => setReviewerId(e.target.value)} disabled={!members.list || task.status === 'in_review'}>
              <option value="">{t('tk.unassigned')}</option>
              {personOptions(task.reviewer_id ?? '')}
            </select>
          </Field>
          {stagesEnabled && <Field label={t('tk.field.milestone')} optional>
            <select className="select" value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} disabled={!milestones.data}>
              <option value="">{t('tk.none')}</option>
              {(milestones.data ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.seq}. {m.name}
                </option>
              ))}
            </select>
          </Field>}
          <Field label={t('tk.field.part')} optional>
            <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={!parts.data}>
              <option value="">{t('tk.none')}</option>
              {(parts.data ?? [])
                .filter((p) => !p.archived_at || p.id === task.part_id)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.number} · {p.name}
                  </option>
                ))}
            </select>
          </Field>
          <Field label={t('tk.field.priority')}>
            <select className="select" value={priority} onChange={(e) => setPriority(e.target.value as TaskPriority)}>
              {TASK_PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {t(`tk.priority.${p}`)}
                </option>
              ))}
            </select>
          </Field>
          <div className="form-grid tk-dates">
            <Field label={t('tk.field.start')} optional>
              <input className="input" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </Field>
            <Field label={t('tk.field.due')} optional error={dateErr}>
              <input className="input" type="date" value={due} onChange={(e) => setDue(e.target.value)} />
            </Field>
          </div>
          <div className="span-2 stack-sm">
            {task.required !== undefined && (
              <label className="check tk-wrap">
                <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
                <span>
                  {t('tk.required.label')} <span className="faint small">· {t('tk.required.hint')}</span>
                </span>
              </label>
            )}
            <label className="check">
              <input type="checkbox" checked={blocked} onChange={(e) => setBlocked(e.target.checked)} />
              {t('tk.edit.blocked')}
            </label>
            {blocked && (
              <Field label={t('tk.field.blockedReason')} error={touched ? blockedErr : null}>
                <input className="input" value={blockedReason} maxLength={2000} onChange={(e) => setBlockedReason(e.target.value)} />
              </Field>
            )}
          </div>
        </div>
        <TkError error={error} onReload={onReload} />
      </div>
    </Dialog>
  );
}

function CollaboratorsDialog({ task, members, onClose, onSaved }: { task: TaskDetail; members: Members; onClose: () => void; onSaved: (t: TaskDetail) => void }) {
  const { t } = useI18n();
  const [ids, setIds] = useState<Set<string>>(() => new Set(task.collaborators.map((c) => c.id)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await setTaskCollaborators(task.id, [...ids]));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('tk.collab.title')}
      subtitle={t('tk.collab.hint')}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <fieldset className="tk-check-col">
        <legend className="sr-only">{t('tk.collab.title')}</legend>
        {(members.list ?? [])
          .filter((m) => m.id !== task.owner_id)
          .map((m) => (
            <label key={m.id} className="check">
              <input
                type="checkbox"
                checked={ids.has(m.id)}
                onChange={(e) =>
                  setIds((s) => {
                    const n = new Set(s);
                    if (e.target.checked) n.add(m.id);
                    else n.delete(m.id);
                    return n;
                  })
                }
              />
              {m.name} <span className="faint small">· {t(`role.${m.role}`)}</span>
            </label>
          ))}
      </fieldset>
      <TkError error={error ?? members.error} />
    </Dialog>
  );
}

function DependenciesDialog({ task, onClose, onSaved }: { task: TaskDetail; onClose: () => void; onSaved: (t: TaskDetail) => void }) {
  const { t } = useI18n();
  const all = useResource((signal) => listTasks(task.project_id, {}, { signal }), [task.project_id]);
  const [ids, setIds] = useState<Set<string>>(() => new Set(task.dependencies.predecessors.map((p) => p.id)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const successors = useMemo(() => new Set(task.dependencies.successors.map((s) => s.id)), [task.dependencies.successors]);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await setTaskDependencies(task.id, [...ids]));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  const others = (all.data ?? []).filter((x) => x.id !== task.id);
  return (
    <Dialog
      title={t('tk.dep.title')}
      subtitle={t('tk.dep.hint')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !all.data}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      {all.error && !all.data ? (
        <ErrorNotice error={all.error} onRetry={all.reload} />
      ) : !all.data ? (
        <SkeletonRows rows={4} cols={2} />
      ) : !others.length ? (
        <p className="faint">{t('tk.dep.noOthers')}</p>
      ) : (
        <fieldset className="tk-check-col">
          <legend className="sr-only">{t('tk.dep.title')}</legend>
          {others.map((x) => (
            <label key={x.id} className="check tk-wrap">
              <input
                type="checkbox"
                checked={ids.has(x.id)}
                onChange={(e) =>
                  setIds((s) => {
                    const n = new Set(s);
                    if (e.target.checked) n.add(x.id);
                    else n.delete(x.id);
                    return n;
                  })
                }
              />
              <span className="mono faint">{x.key}</span> {x.title} <TaskStatusChip status={x.status} />
              {successors.has(x.id) && <span className="faint small">· {t('tk.dep.isSuccessor')}</span>}
            </label>
          ))}
        </fieldset>
      )}
      <TkError error={error} />
    </Dialog>
  );
}

function CriteriaList({ task, canEdit, onChanged }: { task: TaskDetail; canEdit: boolean; onChanged: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [editing, setEditing] = useState<Criterion | 'new' | null>(null);
  const [deleting, setDeleting] = useState<Criterion | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const slots = (task.baseline?.items ?? []).map((i) => i.slot);
  const locked = LOCKED_STATUSES.has(task.status);
  const editable = canEdit && !locked && task.status !== 'cancelled';

  return (
    <section className="panel tk-panel" aria-label={t('tk.crit.title')}>
      <div className="panel-h">
        <h3>{t('tk.crit.title')}</h3>
        <Count n={task.criteria.length} />
        <span className="grow" />
        {editable && (
          <button type="button" className="btn sm" onClick={() => setEditing('new')}>
            <Plus /> {t('tk.crit.add')}
          </button>
        )}
      </div>
      <div className="panel-b stack-sm">
        {locked && (
          <div className="callout slim pending">
            <Lock aria-hidden />
            <span>{task.status === 'in_review' ? t('tk.crit.lockedReview') : t('tk.crit.lockedDone')}</span>
          </div>
        )}
        {!task.criteria.length ? (
          <EmptyState compact title={t('tk.crit.empty')} body={t('tk.crit.emptyBody')} />
        ) : (
          <ol className="tk-crit-list">
            {task.criteria.map((c) => (
              <li key={c.id} {...aiSourceProps('criterion', c.id, task.project_id, task.id)}>
                <span className="mono faint tk-crit-seq">#{c.seq}</span>
                <div className="grow tk-xs">
                  <div className="row">
                    <span className="strong">{c.description}</span>
                    {c.required ? <Chip tone="accent">{t('tk.crit.required')}</Chip> : <Chip tone="pending">{t('tk.crit.optional')}</Chip>}
                    {c.manual_required && (
                      <Chip tone="measure" icon={<Hand aria-hidden />} title={t('tk.crit.manualHint')}>
                        {t('tk.crit.manual')}
                      </Chip>
                    )}
                  </div>
                  <span className="tk-rule-summary small">
                    {ruleSummary(t, c).map((s, i) => (
                      <span key={i}>{s}</span>
                    ))}
                  </span>
                </div>
                <span className="row-actions">
                  <></>
                  {editable && (
                    <>
                      <button type="button" className="btn ghost icon sm" onClick={() => setEditing(c)} aria-label={t('tk.crit.editN', { n: c.seq })}>
                        <Pencil />
                      </button>
                      <button type="button" className="btn ghost icon sm" onClick={() => setDeleting(c)} aria-label={t('tk.crit.removeN', { n: c.seq })}>
                        <Trash2 />
                      </button>
                    </>
                  )}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
      {editing && (
        <CriterionDialog
          taskId={task.id}
          criterion={editing === 'new' ? null : editing}
          slots={slots}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            toast(t('tk.crit.saved'));
            onChanged();
          }}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={t('tk.crit.removeTitle', { n: deleting.seq })}
          body={t('tk.crit.removeBody', { text: deleting.description })}
          confirmLabel={t('common.remove')}
          danger
          busy={busy}
          error={<TkError error={error} />}
          onConfirm={async () => {
            setBusy(true);
            setError(null);
            try {
              await deleteCriterion(deleting.id);
              setDeleting(null);
              toast(t('tk.crit.removed'));
              onChanged();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
          onClose={() => {
            setDeleting(null);
            setError(null);
          }}
        />
      )}
    </section>
  );
}

export function GoalSection({ task, setTask, reloadTask, project, members, projectPlannable }: TaskSectionProps) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const [dialog, setDialog] = useState<'edit' | 'collab' | 'deps' | null>(null);
  const canEdit = task.permissions.edit && projectPlannable;
  const isProjectOwner = project?.my_role === 'owner';
  const saved = (out: TaskDetail) => {
    setDialog(null);
    setTask(out);
    toast(t('tk.edit.saved'));
  };
  const waitingOn = task.dependencies.predecessors.filter((p) => p.status !== 'done' && p.status !== 'cancelled');

  return (
    <div className="stack">
      <section className="panel tk-panel" aria-label={t('tk.field.objective')}>
        <div className="panel-h">
          <h3>{t('tk.field.objective')}</h3>
          {LOCKED_STATUSES.has(task.status) && <Lock className="tk-lock" aria-label={t('tk.edit.objectiveLocked')} />}
          <span className="grow" />
          <></>
          {canEdit && (
            <button type="button" className="btn sm" onClick={() => setDialog('edit')} aria-label={t('tk.edit.title', { key: task.key })}>
              <Pencil /> {t('tk.edit.button')}
            </button>
          )}
        </div>
        <div className="panel-b stack">
          {task.objective ? (
            <p className="tk-objective" {...aiSourceProps('task', task.id, task.project_id, task.id)}>
              {task.objective}
            </p>
          ) : (
            <p className="faint">{t('tk.noObjective')}</p>
          )}
          {task.blocked_reason && (
            <div className="callout err slim">
              <Hand aria-hidden />
              <span>
                <BlockedChip reason={task.blocked_reason} /> {task.blocked_reason}
              </span>
            </div>
          )}
          <dl className="tk-meta">
            <div>
              <dt>{t('tk.field.owner')}</dt>
              <dd>
                <PersonName id={task.owner_id} members={members} />
              </dd>
            </div>
            <div>
              <dt>{t('tk.field.reviewer')}</dt>
              <dd>{task.reviewer_id ? <PersonName id={task.reviewer_id} members={members} /> : <span className="err-text small">{t('tk.noReviewer')}</span>}</dd>
            </div>
            <div>
              <dt>{t('tk.field.priority')}</dt>
              <dd>
                <PriorityChip priority={task.priority} />
              </dd>
            </div>
            {task.required !== undefined && (
              <div>
                <dt>{t('tk.required.field')}</dt>
                <dd>
                  {task.required ? (
                    <Chip tone="accent" icon={<Milestone aria-hidden />} title={t('tk.required.hint')}>
                      {t('tk.required.yes')}
                    </Chip>
                  ) : (
                    <Chip tone="pending" title={t('tk.required.hint')}>
                      {t('tk.required.no')}
                    </Chip>
                  )}
                </dd>
              </div>
            )}
            <div>
              <dt>{t('tk.field.start')}</dt>
              <dd>{fmt.date(dayForDisplay(task.start_date))}</dd>
            </div>
            <div>
              <dt>{t('tk.field.due')}</dt>
              <dd className={isOverdue(task.due_date, task.status) ? 'err-text' : ''}>
                {fmt.date(dayForDisplay(task.due_date))}
                {isOverdue(task.due_date, task.status) && ` · ${t('tk.overdue')}`}
              </dd>
            </div>
            <div>
              <dt>{t('tk.field.part')}</dt>
              <dd>{task.part_id ? <Link to={paths.part(task.part_id)}>{t('tk.openPart')}</Link> : <span className="faint">{t('tk.none')}</span>}</dd>
            </div>
          </dl>
        </div>
      </section>

      <div className="tk-grid-2">
        <section className="panel tk-panel" aria-label={t('tk.collab.title')}>
          <div className="panel-h">
            <Users className="tk-panel-ico" aria-hidden />
            <h3>{t('tk.collab.title')}</h3>
            <Count n={task.collaborators.length} />
            <span className="grow" />
            {canEdit && (
              <button type="button" className="btn sm" onClick={() => setDialog('collab')} aria-label={t('tk.collab.edit')}>
                <Pencil /> {t('tk.edit.button')}
              </button>
            )}
          </div>
          <div className="panel-b">
            {task.collaborators.length ? (
              <ul className="tk-inline-list">
                {task.collaborators.map((c) => (
                  <li key={c.id}>
                    <PersonName id={c.id} members={members} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint small">{t('tk.collab.none')}</p>
            )}
            <p className="faint small">{t('tk.collab.note')}</p>
          </div>
        </section>
        <section className="panel tk-panel" aria-label={t('tk.dep.title')}>
          <div className="panel-h">
            <GitBranch className="tk-panel-ico" aria-hidden />
            <h3>{t('tk.dep.title')}</h3>
            <span className="grow" />
            {canEdit && (
              <button type="button" className="btn sm" onClick={() => setDialog('deps')} aria-label={t('tk.dep.edit')}>
                <Pencil /> {t('tk.edit.button')}
              </button>
            )}
          </div>
          <div className="panel-b stack-sm">
            {waitingOn.length > 0 && <p className="small warn-text">{t('tk.dep.waiting', { n: waitingOn.length })}</p>}
            <div className="tk-label">{t('tk.dep.predecessors')}</div>
            {task.dependencies.predecessors.length ? (
              <ul className="tk-rows">
                {task.dependencies.predecessors.map((p) => (
                  <li key={p.id}>
                    <Link to={paths.task(p.id)} className="tk-row-link">
                      <span className="mono faint">{p.key}</span>
                      <span className="grow ellip">{p.title}</span>
                      <TaskStatusChip status={p.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint small">{t('tk.dep.none')}</p>
            )}
            <div className="tk-label">{t('tk.dep.successors')}</div>
            {task.dependencies.successors.length ? (
              <ul className="tk-rows">
                {task.dependencies.successors.map((p) => (
                  <li key={p.id}>
                    <Link to={paths.task(p.id)} className="tk-row-link">
                      <span className="mono faint">{p.key}</span>
                      <span className="grow ellip">{p.title}</span>
                      <TaskStatusChip status={p.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint small">{t('tk.dep.none')}</p>
            )}
          </div>
        </section>
      </div>

      <CriteriaList task={task} canEdit={canEdit} onChanged={reloadTask} />

      {dialog === 'edit' && (
        <EditTaskDialog
          task={task}
          members={members}
          isProjectOwner={isProjectOwner}
          onClose={() => setDialog(null)}
          onSaved={saved}
          onReload={() => {
            setDialog(null);
            reloadTask();
          }}
        />
      )}
      {dialog === 'collab' && <CollaboratorsDialog task={task} members={members} onClose={() => setDialog(null)} onSaved={saved} />}
      {dialog === 'deps' && <DependenciesDialog task={task} onClose={() => setDialog(null)} onSaved={saved} />}
    </div>
  );
}
