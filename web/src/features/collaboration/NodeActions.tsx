// Node task actions (C04): only the actions the server lists in
// node_task.actions are offered. Human executor: start, add deliverable, check,
// submit, withdraw; task owner: reopen, cancel; semi-auto: adopt a baseline.
// There is never a human "approve" here: final acceptance happens in the
// approval request (semi-auto, UI03) or by the independent agent (full-auto).

import { Ban, ClipboardCheck, Layers, Play, Plus, RotateCcw, Send, Undo2 } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { isApiError } from '../../api/client';
import { nodeTaskAction, type NodeAction, type NodeTaskActionIn, type NodeTaskActionOut, type NodeTaskOut } from '../../api/collaboration';
import { getTask, getTaskChecks, type AssetVersion, type CheckReport } from '../../api/pm';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetVersionPicker } from '../tasks/AssetVersionPicker';
import { CheckReportView } from '../tasks/ChecksView';
import { TK_ERRORS } from '../tasks/common';
import { SLOT_RE } from '../tasks/RuleSpecEditor';
import { CL_ERRORS } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

const ACTION_ORDER: NodeAction[] = ['start', 'add_deliverable', 'check', 'submit', 'withdraw', 'adopt_baseline', 'reopen', 'cancel'];

function ActionIcon({ action }: { action: NodeAction }) {
  switch (action) {
    case 'start':
      return <Play />;
    case 'add_deliverable':
      return <Plus />;
    case 'check':
      return <ClipboardCheck />;
    case 'submit':
      return <Send />;
    case 'withdraw':
      return <Undo2 />;
    case 'reopen':
      return <RotateCcw />;
    case 'cancel':
      return <Ban />;
    default:
      return <Layers />;
  }
}

/** Error notice with the collaboration and task vocabularies plus a reload on version conflicts. */
export function CollabError({ error, onReload }: { error: unknown; onReload?: () => void }) {
  const { t } = useI18n();
  if (!error) return null;
  const conflict = isApiError(error) && error.code === 'version_conflict';
  return (
    <div className="stack-sm">
      <ErrorNotice error={error} overrides={{ ...TK_ERRORS, ...CL_ERRORS }} />
      {conflict && onReload && (
        <div>
          <button type="button" className="btn sm" onClick={onReload}>
            <RotateCcw /> {t('tk.reloadLatest')}
          </button>
        </div>
      )}
    </div>
  );
}

function ActionDialog({
  title,
  body,
  reasonLabel,
  reasonRequired,
  confirmLabel,
  danger,
  size = 'sm',
  canConfirm = true,
  onConfirm,
  onClose,
  onReload,
  children,
}: {
  title: ReactNode;
  body?: ReactNode;
  reasonLabel?: string;
  reasonRequired?: boolean;
  confirmLabel: ReactNode;
  danger?: boolean;
  size?: 'sm' | 'md' | 'lg';
  canConfirm?: boolean;
  /** Resolve to close; throw to keep the dialog open with the error. */
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
  onReload: () => void;
  children?: ReactNode;
}) {
  const { t } = useI18n();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const missing = !!reasonRequired && !reason.trim();
  const submit = async () => {
    setTouched(true);
    if (missing || !canConfirm) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(reason.trim());
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={title}
      onClose={onClose}
      busy={busy}
      size={size}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className={`btn ${danger ? 'danger-solid' : 'primary'}`} disabled={busy || !canConfirm}>
            {busy ? t('common.working') : confirmLabel}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        {body && <p className="muted">{body}</p>}
        {children}
        {reasonLabel !== undefined && (
          <Field label={reasonLabel} optional={!reasonRequired} error={touched && missing ? t('validation.required', { field: reasonLabel }) : null}>
            <LimitedTextarea className="input" rows={3} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        <CollabError
          error={error}
          onReload={() => {
            onClose();
            onReload();
          }}
        />
      </div>
    </Dialog>
  );
}

function DeliverableFields({ node, version, setVersion, criteria, setCriteria }: { node: NodeTaskOut; version: AssetVersion | null; setVersion: (v: AssetVersion | null) => void; criteria: Set<string>; setCriteria: (s: Set<string>) => void }) {
  const { t } = useI18n();
  const taskId = node.task?.id ?? node.task_id;
  const task = useResource((signal) => getTask(taskId!, { signal }), [taskId], { enabled: !!taskId });
  const projectId = node.task?.project_id;
  return (
    <div className="stack">
      {projectId ? <AssetVersionPicker projectId={projectId} value={version} onChange={setVersion} /> : <p className="faint small">{t('collab.node.noTask')}</p>}
      <fieldset className="tk-fieldset stack-sm">
        <legend className="tk-label">{t('tk.dl.criteria')}</legend>
        {task.error && !task.data ? (
          <ErrorNotice error={task.error} onRetry={task.reload} />
        ) : !task.data ? (
          <SkeletonRows rows={2} cols={2} />
        ) : task.data.criteria.length ? (
          <div className="tk-check-col">
            {task.data.criteria.map((c) => (
              <label key={c.id} className="check tk-wrap">
                <input
                  type="checkbox"
                  checked={criteria.has(c.id)}
                  onChange={(e) => {
                    const n = new Set(criteria);
                    if (e.target.checked) n.add(c.id);
                    else n.delete(c.id);
                    setCriteria(n);
                  }}
                />
                <span className="mono faint">#{c.seq}</span> {c.description}
              </label>
            ))}
          </div>
        ) : (
          <p className="faint small">{t('tk.dl.noCriteria')}</p>
        )}
      </fieldset>
    </div>
  );
}

export function NodeActions({ runId, node, onNode, onReload }: { runId: string; node: NodeTaskOut; onNode: (n: NodeTaskActionOut) => void; onReload: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [open, setOpen] = useState<NodeAction | null>(null);
  const [checkBusy, setCheckBusy] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [report, setReport] = useState<CheckReport | null>(null);
  // add_deliverable / adopt_baseline dialog state
  const [version, setVersion] = useState<AssetVersion | null>(null);
  const [criteria, setCriteria] = useState<Set<string>>(new Set());
  const [note, setNote] = useState('');
  const [slot, setSlot] = useState('');

  const actions = ACTION_ORDER.filter((a) => node.actions.includes(a));
  if (!actions.length) return null;

  const run = async (body: NodeTaskActionIn, toastKey: Parameters<typeof t>[0]) => {
    const out = await nodeTaskAction(runId, node.node_id, body);
    setOpen(null);
    onNode(out);
    toast(t(toastKey, { title: node.title }));
    return out;
  };

  const doCheck = async () => {
    setCheckBusy(true);
    setCheckError(null);
    try {
      const out = await nodeTaskAction(runId, node.node_id, { action: 'check', expected_version: node.version });
      onNode(out);
      const taskId = node.task?.id ?? node.task_id;
      setReport(out.checks ?? (taskId ? await getTaskChecks(taskId) : null));
    } catch (e) {
      setCheckError(e);
    } finally {
      setCheckBusy(false);
    }
  };

  const openDialog = (a: NodeAction) => {
    setVersion(null);
    setCriteria(new Set());
    setNote('');
    setSlot('');
    setOpen(a);
  };

  const slotOptions = Array.from(new Set([...(node.input_slots ?? []).map((s) => s.slot_key), ...(node.bound_inputs ?? []).map((b) => b.slot_key)]));
  const slotBad = !!slot.trim() && !SLOT_RE.test(slot.trim());

  return (
    <div className="stack-sm">
      <div className="cl-actions">
        {actions.map((a) =>
          a === 'check' ? (
            <button key={a} type="button" className="btn sm" onClick={() => void doCheck()} disabled={checkBusy}>
              <ClipboardCheck /> {checkBusy ? t('common.working') : t('collab.action.check')}
            </button>
          ) : (
            <button key={a} type="button" className={`btn sm ${a === 'start' || a === 'submit' ? 'primary' : a === 'cancel' ? 'danger' : ''}`} onClick={() => openDialog(a)}>
              <ActionIcon action={a} /> {t(`collab.action.${a}`)}
            </button>
          ),
        )}
      </div>
      <CollabError error={checkError} onReload={onReload} />
      {report && (
        <div className="stack-sm">
          <CheckReportView report={report} title={t('collab.action.checkResult')} />
          <button type="button" className="btn ghost sm" onClick={() => setReport(null)}>
            {t('common.close')}
          </button>
        </div>
      )}

      {open === 'start' && (
        <ActionDialog
          title={t('collab.action.startTitle', { title: node.title })}
          body={t('collab.action.startBody')}
          confirmLabel={t('collab.action.start')}
          onConfirm={async () => void (await run({ action: 'start', expected_version: node.version }, 'collab.action.started'))}
          onClose={() => setOpen(null)}
          onReload={onReload}
        />
      )}
      {open === 'add_deliverable' && (
        <ActionDialog
          title={t('collab.action.add_deliverable')}
          body={t('collab.action.addBody')}
          confirmLabel={t('collab.action.add_deliverable')}
          size="lg"
          canConfirm={!!version}
          onConfirm={async () =>
            void (await run({ action: 'add_deliverable', expected_version: node.version, asset_version_id: version!.id, criterion_ids: [...criteria], note: note.trim() }, 'collab.action.added'))
          }
          onClose={() => setOpen(null)}
          onReload={onReload}
        >
          <DeliverableFields node={node} version={version} setVersion={setVersion} criteria={criteria} setCriteria={setCriteria} />
          <Field label={t('tk.dl.note')} optional>
            <LimitedTextarea className="input" rows={2} limit="description" value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </ActionDialog>
      )}
      {open === 'submit' && (
        <ActionDialog
          title={t('collab.action.submitTitle', { title: node.title })}
          body={t('collab.action.submitBody')}
          confirmLabel={t('collab.action.submit')}
          onConfirm={async () => void (await run({ action: 'submit', expected_version: node.version, note: note.trim() }, 'collab.action.submitted'))}
          onClose={() => setOpen(null)}
          onReload={onReload}
        >
          <Field label={t('tk.sub.note')} optional>
            <LimitedTextarea className="input" rows={3} limit="long_note" value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </ActionDialog>
      )}
      {open === 'withdraw' && (
        <ActionDialog
          title={t('collab.action.withdrawTitle')}
          body={t('collab.action.withdrawBody')}
          reasonLabel={t('tk.reason')}
          reasonRequired
          confirmLabel={t('collab.action.withdraw')}
          onConfirm={async (reason) => void (await run({ action: 'withdraw', expected_version: node.version, reason }, 'collab.action.withdrawn'))}
          onClose={() => setOpen(null)}
          onReload={onReload}
        />
      )}
      {open === 'reopen' && (
        <ActionDialog
          title={t('collab.action.reopenTitle', { title: node.title })}
          body={t('collab.action.reopenBody')}
          reasonLabel={t('tk.reason')}
          reasonRequired
          confirmLabel={t('collab.action.reopen')}
          danger
          onConfirm={async (reason) => void (await run({ action: 'reopen', expected_version: node.version, reason }, 'collab.action.reopened'))}
          onClose={() => setOpen(null)}
          onReload={onReload}
        />
      )}
      {open === 'cancel' && (
        <ActionDialog
          title={t('collab.action.cancelTitle', { title: node.title })}
          body={t('collab.action.cancelBody')}
          reasonLabel={t('tk.reason')}
          reasonRequired
          confirmLabel={t('collab.action.cancel')}
          danger
          onConfirm={async (reason) => void (await run({ action: 'cancel', expected_version: node.version, reason }, 'collab.action.cancelled'))}
          onClose={() => setOpen(null)}
          onReload={onReload}
        />
      )}
      {open === 'adopt_baseline' && (
        <ActionDialog
          title={t('collab.action.adopt_baseline')}
          body={t('collab.action.adoptBody')}
          reasonLabel={t('tk.reason')}
          reasonRequired
          confirmLabel={t('collab.action.adopt_baseline')}
          size="lg"
          canConfirm={!!version && !!slot.trim() && !slotBad}
          onConfirm={async (reason) =>
            void (await run({ action: 'adopt_baseline', expected_version: node.version, items: { [slot.trim()]: version!.id }, reason }, 'collab.action.adopted'))
          }
          onClose={() => setOpen(null)}
          onReload={onReload}
        >
          <Field label={t('collab.form.slotKey')} error={slotBad ? t('collab.form.slotInvalid') : null}>
            <input className="input mono" list="cl-slot-options" value={slot} maxLength={80} onChange={(e) => setSlot(e.target.value)} />
          </Field>
          <datalist id="cl-slot-options">
            {slotOptions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
          {node.task?.project_id && <AssetVersionPicker projectId={node.task.project_id} value={version} onChange={setVersion} />}
        </ActionDialog>
      )}
    </div>
  );
}
