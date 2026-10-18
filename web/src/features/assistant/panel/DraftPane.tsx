// Draft pane (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §3): a task draft the main agent stored, edited in
// place — title, objective, acceptance criteria (description + required) — and saved against the version it was
// loaded at (changed meanwhile → the latest is loaded on request, never merged silently). "Let the main agent
// continue" saves, then says so in the chat (an `auto` request). A draft that became a task, was discarded, or sits in
// an archived conversation is read only. The draft lives in the conversation (no page of its own).

import { AlertTriangle, ClipboardPen, ListChecks, Plus, Save, Send, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { isOrchestratorTurn, type AiTaskDraft } from '../../../api/assistant';
import { isApiError } from '../../../api/client';
import { useFeature } from '../../../api/capabilities';
import type { UUID } from '../../../api/types';
import { Field } from '../../../components/ui/Form';
import { useI18n } from '../../../i18n/I18nProvider';
import { useAssistantSession } from '../AssistantProvider';
import { AsError, SimChip } from '../common';
import { DraftStatusChip } from '../SessionOverview';
import { PaneFrame, PaneLoading } from './PaneFrame';
import { useWorkPanel } from './WorkPanelContext';

type Criterion = AiTaskDraft['criteria'][number] & { key: number };

interface DraftForm {
  title: string;
  objective: string;
  criteria: Criterion[];
}

function formOf(d: AiTaskDraft): DraftForm {
  return { title: d.title, objective: d.objective ?? '', criteria: (d.criteria ?? []).map((c, i) => ({ ...c, key: i })) };
}

export function DraftPane({ draftId }: { draftId: UUID }) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const { store } = useAssistantSession();
  const detail = store.detail;
  const draft = detail?.work?.drafts.find((d) => d.id === draftId) ?? null;
  const icon = <ClipboardPen />;
  const kind = t('wp.kind.draft');

  if (!draft) {
    if (!detail) return <PaneLoading icon={icon} kind={kind} />;
    return (
      <PaneFrame icon={icon} kind={kind} title={kind}>
        <div className="wp-pad">
          <p className="muted">{t('wp.draft.missing')}</p>
        </div>
      </PaneFrame>
    );
  }

  const readOnly = !tasksEnabled || draft.status !== 'open' || !!detail?.conversation.archived;
  return (
    <PaneFrame
      icon={icon}
      kind={kind}
      title={draft.title || t('wp.draft.untitled')}
      chips={
        <>
          <DraftStatusChip status={draft.status} />
          {draft.simulated && <SimChip />}
          <span className="mono faint">v{draft.version}</span>
        </>
      }
    >
      <div className="wp-pad wp-draft">{readOnly ? <DraftView draft={draft} /> : <DraftEditor key={draft.id} draft={draft} />}</div>
    </PaneFrame>
  );
}

function DraftView({ draft }: { draft: AiTaskDraft }) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const wp = useWorkPanel();
  const { store } = useAssistantSession();
  const why = store.detail?.conversation.archived && draft.status === 'open' ? 'archived' : draft.status === 'used' ? 'used' : 'discarded';
  return (
    <>
      <p className="faint small">{t(!tasksEnabled && draft.status === 'open' ? 'features.historyOnly' : `wp.draft.readOnly.${why}`)}</p>
      {draft.task_id && (
        <div>
          <button type="button" className="btn sm" onClick={() => wp.open({ kind: 'task', id: draft.task_id! })}>
            <ListChecks /> {t('wp.draft.openTask')}
          </button>
        </div>
      )}
      <section className="wp-sec">
        <h3>{t('wp.draft.objective')}</h3>
        <p className={draft.objective ? '' : 'faint'}>{draft.objective || t('wp.task.noObjective')}</p>
      </section>
      <section className="wp-sec">
        <h3>{t('wp.draft.criteria')}</h3>
        {draft.criteria.length ? (
          <ul className="wp-crit">
            {draft.criteria.map((c, i) => (
              <li key={i}>
                <span className="mono faint">#{i + 1}</span>
                <span className="wp-crit-main">
                  {c.description}
                  {!c.required && <span className="faint small"> · {t('common.optional')}</span>}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="faint small">{t('wp.draft.noCriteria')}</p>
        )}
      </section>
    </>
  );
}

function DraftEditor({ draft }: { draft: AiTaskDraft }) {
  const { t } = useI18n();
  const { store, bumpList, requestIds } = useAssistantSession();
  const [form, setForm] = useState<DraftForm>(() => formOf(draft));
  /** The version the form was loaded from: a save is checked against it. */
  const [base, setBase] = useState(draft.version);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<null | 'save' | 'continue' | 'discard'>(null);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const [seq, setSeq] = useState(() => draft.criteria.length);

  const load = (d: AiTaskDraft) => {
    setForm(formOf(d));
    setSeq(d.criteria.length);
    setBase(d.version);
    setDirty(false);
    setTouched(false);
    setError(null);
  };
  // Changed elsewhere (the main agent, another tab): follow it while nothing is edited here; otherwise ask (below).
  useEffect(() => {
    if (draft.version !== base && !dirty) load(draft);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.version]);
  const stale = draft.version !== base;

  const edit = (patch: Partial<DraftForm>) => {
    setForm((f) => ({ ...f, ...patch }));
    setDirty(true);
  };
  const editCrit = (key: number, patch: Partial<Criterion>) => edit({ criteria: form.criteria.map((c) => (c.key === key ? { ...c, ...patch } : c)) });

  const titleErr = !form.title.trim();
  const critErr = form.criteria.some((c) => !c.description.trim());

  /** Saves the form; the saved draft, or null when it was refused (shown). */
  const save = async (): Promise<AiTaskDraft | null> => {
    setTouched(true);
    if (titleErr || critErr) return null;
    try {
      const saved = await store.saveDraft(draft.id, {
        expected_version: base,
        title: form.title.trim(),
        objective: form.objective.trim(),
        criteria: form.criteria.map((c) => ({ description: c.description.trim(), required: c.required, allowed_types: c.allowed_types ?? [], rule_spec: c.rule_spec ?? {} })),
      });
      load(saved);
      return saved;
    } catch (e) {
      // Changed meanwhile / no longer open: fetch the latest (the notice below offers to load it).
      if (isApiError(e) && (e.code === 'version_conflict' || e.code === 'draft_closed')) store.reload();
      if (!(isApiError(e) && e.code === 'version_conflict')) setError(e);
      return null;
    }
  };

  const run = async (kind: 'save' | 'continue' | 'discard', fn: () => Promise<unknown>) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const conversationId = store.detail?.conversation.id;
  // One active turn per conversation: while the main agent works, a new request would only be refused (agent_busy).
  const agentBusy = !!store.detail?.operations.some((o) => isOrchestratorTurn(o) && (o.status === 'pending' || o.status === 'running'));
  const continueAgent = () =>
    run('continue', async () => {
      const saved = dirty ? await save() : draft;
      if (!saved || !conversationId) return;
      // One id per draft version, kept until the server recorded the request (a resend after a lost response is the same).
      const scope = `draft-continue.${saved.id}.${saved.version}`;
      await store.send(conversationId, { client_request_id: requestIds.take(scope), text: t('wp.draft.continueText', { title: saved.title }), operation: 'auto' });
      requestIds.forget(scope);
      bumpList();
    });

  const discard = () => {
    if (!window.confirm(t('wp.draft.discardConfirm'))) return;
    void run('discard', () => store.discardDraft(draft.id));
  };

  return (
    <form
      className="wp-draft"
      onSubmit={(e) => {
        e.preventDefault();
        void run('save', save);
      }}
    >
      {stale && dirty && busy === null && (
        <div className="callout warn slim" role="alert">
          <AlertTriangle aria-hidden />
          <div className="grow stack-sm">
            <span>{t('wp.draft.stale')}</span>
            <div>
              <button type="button" className="btn sm" onClick={() => load(draft)}>
                {t('wp.draft.reload')}
              </button>
            </div>
          </div>
        </div>
      )}
      <Field label={t('wp.draft.title')} error={touched && titleErr ? t('wp.draft.titleRequired') : undefined}>
        <input className="input" value={form.title} maxLength={300} onChange={(e) => edit({ title: e.target.value })} />
      </Field>
      <Field label={t('wp.draft.objective')} optional>
        <textarea className="input" rows={4} value={form.objective} onChange={(e) => edit({ objective: e.target.value })} />
      </Field>
      <fieldset className="wp-draft-crit stack-sm">
        <legend className="wp-draft-legend">{t('wp.draft.criteria')}</legend>
        {form.criteria.length === 0 && <p className="faint small">{t('wp.draft.noCriteria')}</p>}
        {form.criteria.map((c, i) => (
          <div key={c.key} className="wp-draft-row">
            <span className="mono faint">#{i + 1}</span>
            <input
              className="input sm"
              value={c.description}
              placeholder={t('wp.draft.criterionPh')}
              aria-label={t('wp.draft.criterionN', { n: i + 1 })}
              aria-invalid={touched && !c.description.trim() ? true : undefined}
              onChange={(e) => editCrit(c.key, { description: e.target.value })}
            />
            <label className="check">
              <input type="checkbox" checked={c.required} onChange={(e) => editCrit(c.key, { required: e.target.checked })} />
              {t('wp.draft.required')}
            </label>
            <button type="button" className="btn ghost icon sm" onClick={() => edit({ criteria: form.criteria.filter((x) => x.key !== c.key) })} aria-label={t('wp.draft.removeN', { n: i + 1 })}>
              <Trash2 />
            </button>
          </div>
        ))}
        {touched && critErr && <span className="field-error">{t('wp.draft.criterionEmpty')}</span>}
        <div>
          <button
            type="button"
            className="btn sm"
            onClick={() => {
              edit({ criteria: [...form.criteria, { key: seq, description: '', required: true, allowed_types: [], rule_spec: {} }] });
              setSeq((n) => n + 1);
            }}
          >
            <Plus /> {t('wp.draft.add')}
          </button>
        </div>
      </fieldset>
      <div className="wp-draft-acts">
        <button type="submit" className="btn sm" disabled={busy !== null || !dirty}>
          <Save /> {busy === 'save' ? t('wp.draft.saving') : t('wp.draft.save')}
        </button>
        <button type="button" className="btn sm primary" onClick={() => void continueAgent()} disabled={busy !== null || agentBusy} title={agentBusy ? t('as.orch.working') : t('wp.draft.continueHint')}>
          <Send /> {busy === 'continue' ? t('as.sending') : t('wp.draft.continue')}
        </button>
        <span className="grow" />
        <button type="button" className="btn sm ghost" onClick={discard} disabled={busy !== null}>
          <Trash2 /> {t('wp.draft.discard')}
        </button>
      </div>
      <p className="faint small">{t('wp.draft.note')}</p>
      <AsError error={error} />
    </form>
  );
}
