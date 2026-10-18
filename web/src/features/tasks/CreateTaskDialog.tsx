import { useFeature } from '../../api/capabilities';
// New task: title, objective, owner, reviewer, milestone, part, priority,
// required-for-stage flag, dates and the first acceptance criteria. Criteria
// are created right after the task (POST /tasks/{id}/criteria); full rules are
// edited on the task page. Owners can let the AI draft the form from a short
// description (POST /projects/{id}/tasks/draft); nothing is stored until the
// person reviews the form and creates the task. `initialDraft` pre-fills the
// form the same way (the AI assistant's "Create a task from this draft").

import { ExternalLink, FlaskConical, Plus, Sparkles, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { listParts, listWorkflows } from '../../api/endpoints';
import {
  addCriterion,
  createTask,
  DEFAULT_RULE_SPEC,
  draftTask,
  listMilestones,
  TASK_PRIORITIES,
  type AssetType,
  type RuleSpec,
  type TaskDetail,
  type TaskDraft,
  type TaskPriority,
} from '../../api/pm';
import type { Project } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { fromDateInput, TkError, type Members } from './common';
import { AssetTypeToggles } from './RuleSpecEditor';
import { LimitedInput, LimitedTextarea } from '../../components/ui/LimitedText';

interface DraftCriterion {
  key: number;
  description: string;
  required: boolean;
  types: AssetType[];
  /** Rule fields proposed by the AI draft (kept when the criterion is created). */
  rule?: Partial<RuleSpec>;
}

/** Form criteria from an AI draft (keys start at `firstKey`). */
function criteriaFromDraft(d: TaskDraft, firstKey: number): DraftCriterion[] {
  return (d.criteria ?? []).map((c, i) => ({
    key: firstKey + i,
    description: c.description ?? '',
    required: c.required !== false,
    types: [...new Set([...(c.allowed_types ?? []), ...((c.rule_spec?.asset_types as AssetType[] | undefined) ?? [])])],
    rule: c.rule_spec ?? {},
  }));
}

/** "Draft with AI": a short description → filled form. Owners only (manage action). */
function AiDraftBox({ projectId, onDraft }: { projectId: string; onDraft: (d: TaskDraft) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      onDraft(await draftTask(projectId, text.trim()));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  if (!open)
    return (
      <div>
        <button type="button" className="btn sm" onClick={() => setOpen(true)}>
          <Sparkles /> {t('tk.draft.open')}
        </button>
      </div>
    );
  return (
    <fieldset className="tk-fieldset stack-sm">
      <legend className="tk-label">{t('tk.draft.title')}</legend>
      <p className="faint small">{t('tk.draft.hint')}</p>
      <LimitedTextarea
        className="input"
        rows={3}
        limit="chat"
        value={text}
        placeholder={t('tk.draft.placeholder')}
        aria-label={t('tk.draft.title')}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void run();
          }
        }}
        autoFocus
      />
      <div className="row">
        <button type="button" className="btn sm primary" onClick={() => void run()} disabled={busy || !text.trim()}>
          <Sparkles /> {busy ? t('tk.draft.working') : t('tk.draft.run')}
        </button>
        <button type="button" className="btn sm ghost" onClick={() => setOpen(false)} disabled={busy}>
          {t('common.close')}
        </button>
      </div>
      <ErrorNotice error={error} overrides={{ ai_unavailable: 'tk.draft.unavailable', ai_bad_response: 'tk.draft.badResponse', forbidden: 'tk.draft.forbidden' }} />
    </fieldset>
  );
}

export function CreateTaskDialog({
  project,
  members,
  isProjectOwner,
  onClose,
  onCreated,
  initialDraft,
}: {
  project: Project;
  members: Members;
  isProjectOwner: boolean;
  onClose: () => void;
  /** failed = criteria that could not be created (the task exists). */
  onCreated: (task: TaskDetail, failed: number) => void;
  /** Pre-fill title / objective / criteria from an AI draft (the person still reviews and creates). */
  initialDraft?: TaskDraft | null;
}) {
  const { t } = useI18n();
  const { user } = useAuth();
  const me = user?.id ?? '';
  const stagesEnabled = useFeature('stages');
  const milestones = useResource((signal) => listMilestones(project.id, { signal }), [project.id, stagesEnabled], { enabled: stagesEnabled });
  const parts = useResource((signal) => listParts(project.id, {}, { signal }), [project.id]);
  const [draft, setDraft] = useState<Pick<TaskDraft, 'simulated' | 'suggested_workflow_id'> | null>(() =>
    initialDraft ? { simulated: !!initialDraft.simulated, suggested_workflow_id: initialDraft.suggested_workflow_id ?? null } : null,
  );
  const workflows = useResource((signal) => listWorkflows(project.id, {}, { signal }), [project.id, draft?.suggested_workflow_id], { enabled: !!draft?.suggested_workflow_id });
  const [title, setTitle] = useState(initialDraft?.title ?? '');
  const [objective, setObjective] = useState(initialDraft?.objective ?? '');
  const [ownerId, setOwnerId] = useState(me);
  const [reviewerId, setReviewerId] = useState('');
  const [milestoneId, setMilestoneId] = useState('');
  const [partId, setPartId] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('normal');
  const [required, setRequired] = useState(true);
  const [start, setStart] = useState('');
  const [due, setDue] = useState('');
  const [criteria, setCriteria] = useState<DraftCriterion[]>(() => (initialDraft ? criteriaFromDraft(initialDraft, 1) : []));
  const [seq, setSeq] = useState(() => 1 + (initialDraft?.criteria?.length ?? 0));
  const [focusKey, setFocusKey] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const assignable = (members.list ?? []).filter((m) => m.role !== 'viewer' && (isProjectOwner || m.id === me));
  const titleErr = !title.trim() ? t('validation.required', { field: t('tk.field.title') }) : null;
  const dateErr = start && due && due < start ? t('tk.create.dueBeforeStart') : null;
  const critErr = criteria.some((c) => !c.description.trim());

  const submit = async () => {
    setTouched(true);
    if (titleErr || dateErr || critErr) return;
    setBusy(true);
    setError(null);
    let task: TaskDetail;
    try {
      task = await createTask(project.id, {
        title: title.trim(),
        objective: objective.trim(),
        owner_id: ownerId || null,
        reviewer_id: reviewerId || null,
        milestone_id: stagesEnabled ? milestoneId || null : null,
        part_id: partId || null,
        priority,
        required,
        start_date: fromDateInput(start),
        due_date: fromDateInput(due),
      });
    } catch (e) {
      setError(e);
      setBusy(false);
      return;
    }
    let failed = 0;
    for (const c of criteria) {
      try {
        await addCriterion(task.id, {
          description: c.description.trim(),
          required: c.required,
          manual_required: false,
          allowed_types: c.types,
          rule_spec: { ...DEFAULT_RULE_SPEC, ...(c.rule ?? {}), asset_types: c.types },
        });
      } catch {
        failed += 1;
      }
    }
    onCreated(task, failed);
  };

  const update = (key: number, patch: Partial<DraftCriterion>) => setCriteria((xs) => xs.map((c) => (c.key === key ? { ...c, ...patch } : c)));

  /** Fill the form from an AI draft; the person still reviews and creates. */
  const applyDraft = (d: TaskDraft) => {
    setTitle(d.title ?? '');
    setObjective(d.objective ?? '');
    const list = criteriaFromDraft(d, seq);
    setCriteria(list);
    setSeq((n) => n + list.length);
    setDraft({ simulated: !!d.simulated, suggested_workflow_id: d.suggested_workflow_id ?? null });
    setTouched(false);
  };
  const suggested = draft?.suggested_workflow_id ? (workflows.data ?? []).find((w) => w.id === draft.suggested_workflow_id) : undefined;

  return (
    <Dialog
      title={t('tk.create.title')}
      subtitle={t('tk.create.subtitle')}
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
            {busy ? t('common.creating') : t('tk.create.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        {isProjectOwner && <AiDraftBox projectId={project.id} onDraft={applyDraft} />}
        {draft && (
          <div className="callout info slim" role="status">
            <Sparkles aria-hidden />
            <div className="stack-sm grow">
              <span className="row">
                <span>{t('tk.draft.filled')}</span>
                {draft.simulated && (
                  <Chip tone="stale" icon={<FlaskConical aria-hidden />} title={t('tk.draft.simulatedHint')}>
                    {t('tk.draft.simulated')}
                  </Chip>
                )}
              </span>
              {draft.suggested_workflow_id && (
                <span>
                  {t('tk.draft.suggestedWorkflow')}{' '}
                  <Link to={`/workflows/${draft.suggested_workflow_id}`} target="_blank" rel="noreferrer">
                    {suggested?.name ?? t('tk.draft.openWorkflow')} <ExternalLink aria-hidden className="inline-ico" />
                  </Link>
                </span>
              )}
            </div>
          </div>
        )}
        <div className="form-grid">
          <Field label={t('tk.field.title')} error={touched ? titleErr : null} className="span-2">
            <input className="input" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} data-autofocus />
          </Field>
          <Field label={t('tk.field.objective')} optional hint={t('tk.create.objectiveHint')} className="span-2">
            <LimitedTextarea className="input" rows={3} limit="objective" value={objective} onChange={(e) => setObjective(e.target.value)} />
          </Field>
          <Field label={t('tk.field.owner')} hint={isProjectOwner ? undefined : t('tk.create.selfOnly')}>
            <select className="select" value={ownerId} onChange={(e) => setOwnerId(e.target.value)} disabled={!members.list}>
              {assignable.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                  {m.id === me ? ` (${t('common.you')})` : ''}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('tk.field.reviewer')} optional hint={t('tk.create.reviewerHint')}>
            <select className="select" value={reviewerId} onChange={(e) => setReviewerId(e.target.value)} disabled={!members.list}>
              <option value="">{t('tk.create.reviewerLater')}</option>
              {assignable.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                  {m.id === me ? ` (${t('common.you')})` : ''}
                </option>
              ))}
            </select>
          </Field>
          {stagesEnabled && <Field label={t('tk.field.milestone')} optional>
            <select className="select" value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} disabled={!milestones.data}>
              <option value="">{!milestones.data ? t('common.loading') : milestones.data.length ? t('tk.none') : t('tk.create.noMilestones')}</option>
              {(milestones.data ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.seq}. {m.name}
                </option>
              ))}
            </select>
          </Field>}
          <Field label={t('tk.field.part')} optional hint={t('tk.create.partHint')}>
            <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={!parts.data}>
              <option value="">{parts.data ? t('tk.none') : t('common.loading')}</option>
              {(parts.data ?? [])
                .filter((p) => !p.archived_at)
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
            <label className="check tk-wrap">
              <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
              <span>
                {t('tk.required.label')} <span className="faint small">· {t('tk.required.hint')}</span>
              </span>
            </label>
          </div>
        </div>

        <fieldset className="tk-fieldset stack-sm">
          <legend className="tk-label">{t('tk.create.criteria')}</legend>
          <p className="faint small">{t('tk.create.criteriaHint')}</p>
          {criteria.map((c, i) => (
            <div key={c.key} className="tk-crit-draft">
              <div className="row">
                <span className="mono faint">#{i + 1}</span>
                <LimitedInput
                  className="input grow"
                  value={c.description}
                  limit="description"
                  placeholder={t('tk.crit.descriptionPlaceholder')}
                  aria-label={t('tk.crit.descriptionN', { n: i + 1 })}
                  aria-invalid={touched && !c.description.trim() ? true : undefined}
                  ref={(el) => {
                    if (el && focusKey === c.key) {
                      el.focus();
                      setFocusKey(null);
                    }
                  }}
                  onChange={(e) => update(c.key, { description: e.target.value })}
                />
                <label className="check">
                  <input type="checkbox" checked={c.required} onChange={(e) => update(c.key, { required: e.target.checked })} />
                  {t('tk.crit.required')}
                </label>
                <button type="button" className="btn ghost icon sm" onClick={() => setCriteria((xs) => xs.filter((x) => x.key !== c.key))} aria-label={t('tk.crit.removeN', { n: i + 1 })}>
                  <Trash2 />
                </button>
              </div>
              <AssetTypeToggles value={c.types} onChange={(types) => update(c.key, { types })} label={t('tk.rule.assetTypes')} />
            </div>
          ))}
          {touched && critErr && <span className="field-error">{t('tk.create.criteriaEmpty')}</span>}
          <div>
            <button
              type="button"
              className="btn sm"
              onClick={() => {
                setCriteria((xs) => [...xs, { key: seq, description: '', required: true, types: [] }]);
                setFocusKey(seq);
                setSeq((n) => n + 1);
              }}
            >
              <Plus /> {t('tk.crit.add')}
            </button>
          </div>
        </fieldset>
        <TkError error={error ?? members.error ?? milestones.error ?? parts.error} />
      </div>
    </Dialog>
  );
}
