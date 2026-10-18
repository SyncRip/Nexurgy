// Business actions on a result (design §7.3), each through the existing task
// services and their checks (U10): use as task input = adopt a baseline slot
// (shows the impact notices it raised); add as a deliverable = deliverable
// draft linked to criteria; create a task from an AI task draft. None of these
// changes acceptance or a project stage. Loaded lazily from ResultCard.

import { CheckCircle2, ExternalLink, Info } from 'lucide-react';
import { useEffect, useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { AiResultItem } from '../../api/assistant';
import { getProject } from '../../api/endpoints';
import { addDeliverable, adoptBaseline, getTask, isMissingVersion, listTasks, type BaselineAdopted, type TaskDraft } from '../../api/pm';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice, Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { CreateTaskDialog } from '../tasks/CreateTaskDialog';
import { dynT, TkError, useMembers } from '../tasks/common';
import { ImpactNoticeList } from '../tasks/ImpactNotices';
import { SLOT_PRESETS, SLOT_RE } from '../tasks/RuleSpecEditor';
import { LimitedTextarea } from '../../components/ui/LimitedText';

const NEW_SLOT = '__new';
const BLOCKED_FOR_INPUT = new Set(['in_review', 'cancelled']);

function ResultLine({ item }: { item: AiResultItem }) {
  const { t } = useI18n();
  return (
    <p className="as-dlg-result">
      <span className="faint">{t('as.dlg.result')}</span> <b>{item.ref.title}</b> <span className="mono strong">{item.label}</span>
      {item.base?.version_label ? <span className="faint"> · {t('as.dlg.base', { label: item.base.version_label })}</span> : null}
    </p>
  );
}

/** Use a result as a task input: one baseline slot → this exact asset version. */
export function UseAsInputDialog({ projectId, item, defaultTaskId, onClose }: { projectId: UUID; item: AiResultItem; defaultTaskId: UUID | null; onClose: () => void }) {
  const { t } = useI18n();
  const tasks = useResource((signal) => listTasks(projectId, {}, { signal }), [projectId]);
  const [taskId, setTaskId] = useState<string>(defaultTaskId ?? '');
  const task = useResource((signal) => getTask(taskId, { signal }), [taskId], { enabled: !!taskId });
  const [slotChoice, setSlotChoice] = useState<string>('');
  const [newSlot, setNewSlot] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<BaselineAdopted | null>(null);
  const [touched, setTouched] = useState(false);
  const taskSel = useId();
  const slotList = useId();

  // Default slot: the baseline slot holding the same asset type, else the preset slot for the type.
  const items = useMemo(() => (task.data && task.data.id === taskId ? task.data.baseline?.items ?? [] : []), [task.data, taskId]);
  useEffect(() => {
    if (!task.data || task.data.id !== taskId) return;
    const same = items.find((it) => !isMissingVersion(it.version) && it.version.asset_type === item.asset_type);
    if (same) {
      setSlotChoice(same.slot);
    } else {
      setSlotChoice(NEW_SLOT);
      const used = new Set(items.map((i) => i.slot));
      setNewSlot(SLOT_PRESETS.find((p) => p.type === item.asset_type && !used.has(p.slot))?.slot ?? '');
    }
  }, [task.data, taskId, items, item.asset_type]);

  const slot = slotChoice === NEW_SLOT ? newSlot.trim() : slotChoice;
  const current = items.find((i) => i.slot === slot);
  const badSlot = slotChoice === NEW_SLOT && (!SLOT_RE.test(slot) || items.some((i) => i.slot === slot));
  const taskRow = tasks.data?.find((x) => x.id === taskId);
  const statusBlocked = !!taskRow && BLOCKED_FOR_INPUT.has(taskRow.status);

  const submit = async () => {
    setTouched(true);
    if (!taskId || !slot || badSlot || !item.asset_version_id || !task.data || statusBlocked) return;
    setBusy(true);
    setError(null);
    try {
      setDone(await adoptBaseline(taskId, { items: { [slot]: item.asset_version_id }, reason: reason.trim(), expected_version: task.data.version }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <Dialog
        title={t('as.dlg.inputTitle')}
        onClose={onClose}
        footer={
          <>
            <Link className="btn" to={paths.task(done.task.id)} onClick={onClose}>
              <ExternalLink /> {t('as.dlg.openTask')}
            </Link>
            <button type="button" className="btn primary" onClick={onClose} data-autofocus>
              {t('common.close')}
            </button>
          </>
        }
      >
        <div className="stack-sm">
          <div className="callout ok slim" role="status">
            <CheckCircle2 aria-hidden />
            <span>{done.created ? t('as.dlg.inputDone', { seq: done.baseline.seq, slot: dynT(t, `tk.slot.${slot}`, slot) }) : t('as.dlg.inputSame')}</span>
          </div>
          {done.impacts.length > 0 ? (
            <>
              <p className="small strong">{t('as.dlg.impacts', { n: done.impacts.length })}</p>
              <ImpactNoticeList notices={done.impacts} canResolve={false} onChanged={() => undefined} />
            </>
          ) : (
            <p className="faint small">{t('as.dlg.noImpacts')}</p>
          )}
          <p className="faint small">{t('as.dlg.inputNote')}</p>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog
      title={t('as.dlg.inputTitle')}
      subtitle={t('as.dlg.inputSub')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !taskId || !task.data || statusBlocked}>
            {busy ? t('common.working') : t('as.dlg.inputSubmit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <ResultLine item={item} />
        <div className="field">
          <label htmlFor={taskSel}>{t('as.dlg.task')}</label>
          <select id={taskSel} className="select" value={taskId} onChange={(e) => setTaskId(e.target.value)} disabled={!tasks.data}>
            <option value="">{tasks.data ? t('as.dlg.pickTask') : t('common.loading')}</option>
            {(tasks.data ?? []).map((x) => (
              <option key={x.id} value={x.id} disabled={BLOCKED_FOR_INPUT.has(x.status)}>
                {x.key} · {x.title} · {dynT(t, `tk.status.${x.status}`, x.status)}
              </option>
            ))}
          </select>
          {statusBlocked && <span className="field-error">{t('as.dlg.taskLocked')}</span>}
          {tasks.data && !tasks.data.length && <span className="field-hint">{t('as.dlg.noTasks')}</span>}
        </div>
        {taskId && !task.data && !task.error && <Spinner label={t('common.loading')} />}
        {task.data && task.data.id === taskId && (
          <fieldset className="as-fieldset">
            <legend>{t('as.dlg.slot')}</legend>
            {items.map((it) => (
              <label key={it.slot} className="check as-wrap">
                <input type="radio" name="as-slot" checked={slotChoice === it.slot} onChange={() => setSlotChoice(it.slot)} />
                <span>
                  <b>{dynT(t, `tk.slot.${it.slot}`, it.slot)}</b>{' '}
                  <span className="faint">
                    {t('as.dlg.slotNow', { label: isMissingVersion(it.version) ? t('as.dlg.missing') : `${it.version.asset_name} ${it.version.label}` })}
                  </span>
                </span>
              </label>
            ))}
            <label className="check as-wrap">
              <input type="radio" name="as-slot" checked={slotChoice === NEW_SLOT} onChange={() => setSlotChoice(NEW_SLOT)} />
              <span>{t('as.dlg.newSlot')}</span>
            </label>
            {slotChoice === NEW_SLOT && (
              <>
                <input
                  className="input mono"
                  list={slotList}
                  value={newSlot}
                  maxLength={80}
                  placeholder="target_model"
                  aria-label={t('as.dlg.slotName')}
                  aria-invalid={touched && badSlot ? true : undefined}
                  onChange={(e) => setNewSlot(e.target.value.trim())}
                />
                <datalist id={slotList}>
                  {SLOT_PRESETS.filter((p) => !items.some((i) => i.slot === p.slot)).map((p) => (
                    <option key={p.slot} value={p.slot}>
                      {dynT(t, `tk.slot.${p.slot}`, p.slot)}
                    </option>
                  ))}
                </datalist>
                {touched && badSlot && <span className="field-error">{t('as.dlg.slotInvalid')}</span>}
              </>
            )}
            {current && !isMissingVersion(current.version) && (
              <p className="faint small">{t('as.dlg.replaces', { from: current.version.label, to: item.label })}</p>
            )}
          </fieldset>
        )}
        <Field label={t('as.dlg.reason')} optional>
          <LimitedTextarea className="input" rows={2} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <p className="faint small">
          <Info className="inline-ico" aria-hidden /> {t('as.dlg.inputNote')}
        </p>
        {tasks.error ? <ErrorNotice error={tasks.error} onRetry={tasks.reload} /> : null}
        {task.error ? <ErrorNotice error={task.error} onRetry={task.reload} /> : null}
        <TkError error={error} onReload={task.reload} />
      </div>
    </Dialog>
  );
}

/** Add a result to a task's deliverable drafts (in-progress tasks), linked to criteria. */
export function AddDeliverableDialog({ projectId, item, defaultTaskId, onClose }: { projectId: UUID; item: AiResultItem; defaultTaskId: UUID | null; onClose: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const tasks = useResource((signal) => listTasks(projectId, { status: 'in_progress' }, { signal }), [projectId]);
  const [taskId, setTaskId] = useState<string>('');
  const task = useResource((signal) => getTask(taskId, { signal }), [taskId], { enabled: !!taskId });
  const [criteria, setCriteria] = useState<Set<string>>(new Set());
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const taskSel = useId();

  useEffect(() => {
    if (!tasks.data || taskId) return;
    if (defaultTaskId && tasks.data.some((x) => x.id === defaultTaskId)) setTaskId(defaultTaskId);
  }, [tasks.data, defaultTaskId, taskId]);
  useEffect(() => setCriteria(new Set()), [taskId]);

  const detail = task.data && task.data.id === taskId ? task.data : null;
  const already = detail?.deliverables.some((d) => d.asset_version_id === item.asset_version_id);

  const submit = async () => {
    if (!taskId || !item.asset_version_id) return;
    setBusy(true);
    setError(null);
    try {
      await addDeliverable(taskId, { asset_version_id: item.asset_version_id, criterion_ids: [...criteria], note: note.trim() });
      toast(t('as.dlg.delivDone', { label: item.label }));
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('as.dlg.delivTitle')}
      subtitle={t('as.dlg.delivSub')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !detail}>
            {busy ? t('common.working') : t('as.dlg.delivSubmit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <ResultLine item={item} />
        <div className="field">
          <label htmlFor={taskSel}>{t('as.dlg.task')}</label>
          <select id={taskSel} className="select" value={taskId} onChange={(e) => setTaskId(e.target.value)} disabled={!tasks.data}>
            <option value="">{tasks.data ? t('as.dlg.pickTask') : t('common.loading')}</option>
            {(tasks.data ?? []).map((x) => (
              <option key={x.id} value={x.id}>
                {x.key} · {x.title}
              </option>
            ))}
          </select>
          <span className="field-hint">{tasks.data && !tasks.data.length ? t('as.dlg.noActiveTasks') : t('as.dlg.onlyActive')}</span>
        </div>
        {taskId && !detail && !task.error && <Spinner label={t('common.loading')} />}
        {detail && (
          <fieldset className="as-fieldset">
            <legend>{t('as.dlg.criteria')}</legend>
            {detail.criteria.length ? (
              detail.criteria.map((c) => (
                <label key={c.id} className="check as-wrap">
                  <input
                    type="checkbox"
                    checked={criteria.has(c.id)}
                    onChange={(e) =>
                      setCriteria((s) => {
                        const n = new Set(s);
                        if (e.target.checked) n.add(c.id);
                        else n.delete(c.id);
                        return n;
                      })
                    }
                  />
                  <span>
                    <span className="mono faint">#{c.seq}</span> {c.description}
                  </span>
                </label>
              ))
            ) : (
              <p className="faint small">{t('as.dlg.noCriteria')}</p>
            )}
          </fieldset>
        )}
        {already && <p className="faint small">{t('as.dlg.delivAlready')}</p>}
        {detail && !detail.permissions.participate && <p className="faint small">{t('as.dlg.notParticipant')}</p>}
        <Field label={t('as.dlg.note')} optional>
          <LimitedTextarea className="input" rows={2} limit="description" value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <p className="faint small">
          <Info className="inline-ico" aria-hidden /> {t('as.dlg.delivNote')}
        </p>
        {tasks.error ? <ErrorNotice error={tasks.error} onRetry={tasks.reload} /> : null}
        {task.error ? <ErrorNotice error={task.error} onRetry={task.reload} /> : null}
        <TkError error={error} onReload={task.reload} />
      </div>
    </Dialog>
  );
}

/** "Create a task from this draft": the regular task dialog, pre-filled; nothing is stored until the person confirms. */
export function DraftTaskDialog({ projectId, draft, onClose }: { projectId: UUID; draft: TaskDraft; onClose: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const project = useResource((signal) => getProject(projectId, { signal }), [projectId]);
  const members = useMembers(projectId);
  if (!project.data) {
    return (
      <Dialog title={t('as.result.createTask')} onClose={onClose} size="sm">
        {project.error ? <ErrorNotice error={project.error} onRetry={project.reload} /> : <Spinner label={t('common.loading')} />}
      </Dialog>
    );
  }
  return (
    <CreateTaskDialog
      project={project.data}
      members={members}
      isProjectOwner={projectCapabilities(project.data).manage}
      initialDraft={draft}
      onClose={onClose}
      onCreated={(task, failed) => {
        onClose();
        toast(failed ? t('as.dlg.taskCreatedPartial', { key: task.key, n: failed }) : t('as.dlg.taskCreated', { key: task.key }), { tone: failed ? 'err' : 'ok' });
      }}
    />
  );
}
