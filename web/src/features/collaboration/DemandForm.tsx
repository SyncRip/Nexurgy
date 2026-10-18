// UI01 left column: execution mode (two cards), the demand (30000-character
// counter), fixed asset versions (exact versions through AssetVersionPicker),
// expected deliverables, part, executor / final reviewer / pre-reviewer /
// planner, and the plan / save actions. The mode decides which actors are
// required (design §0.4, §3.4); client-side issues only mirror the server rules.
// The execution policy (W7: standard / Turbo) is chosen explicitly; standard is
// preselected and a Turbo plan needs no final reviewer (no acceptance).

import { Bot, Plus, Sparkles, Trash2, UserRound } from 'lucide-react';
import { useState } from 'react';
import { actorRef, type AgentProfile, type ExecutionMode } from '../../api/agents';
import { DEMAND_MAX, draftActor, type DraftOut, type DraftPatch, type PlanIn } from '../../api/collaboration';
import type { ExecutionPolicy } from '../../api/engineering';
import type { AssetVersion } from '../../api/pm';
import type { Part, UUID } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import type { Resource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { AssetVersionPicker } from '../tasks/AssetVersionPicker';
import { policyOf } from '../turbo/model';
import { PolicySelector } from '../turbo/Turbo';
import { SLOT_RE } from '../tasks/RuleSpecEditor';
import { assetTypeLabel, type Members } from '../tasks/common';
import { ActorPicker, type ActorValue } from './ActorPicker';
import { LimitedTextarea } from '../../components/ui/LimitedText';

export interface FixedInputRow {
  slot_key: string;
  asset_version_id: UUID;
  label: string;
  asset_name?: string;
  asset_type?: string;
  content_hash?: string;
}

export interface DemandFormValue {
  mode: ExecutionMode;
  /** W7: explicit per plan; standard unless the person picks Turbo (never carried over from another draft). */
  policy: ExecutionPolicy;
  demand: string;
  /** New page only: appended to the demand as a labelled section (C01 has no separate field). */
  deliverables: string;
  fixedInputs: FixedInputRow[];
  partId: string;
  executor: ActorValue;
  reviewer: ActorValue;
  preReviewerId: string;
  plannerId: string;
}

export function initialFormValue(draft: DraftOut | null): DemandFormValue {
  const d = draft?.draft;
  const ex = d ? draftActor(d, 'executor') : null;
  const rv = d ? draftActor(d, 'reviewer') : null;
  const mode = d?.execution_mode ?? 'semi_auto';
  return {
    mode,
    policy: policyOf(d),
    demand: d?.demand ?? '',
    deliverables: '',
    fixedInputs: (d?.fixed_inputs ?? []).map((f) => ({ slot_key: f.slot_key, asset_version_id: f.asset_version_id, label: f.label ?? '', content_hash: f.content_hash })),
    partId: d?.part_id ?? '',
    executor: ex ? { kind: ex.kind, id: (ex.kind === 'human' ? ex.user_id : ex.agent_profile_id) ?? '' } : { kind: 'agent', id: '' },
    reviewer: rv ? { kind: rv.kind, id: (rv.kind === 'human' ? rv.user_id : rv.agent_profile_id) ?? '' } : { kind: mode === 'full_auto' ? 'agent' : 'human', id: '' },
    preReviewerId: d?.pre_reviewer_agent_id ?? '',
    plannerId: d?.planner_agent_id ?? '',
  };
}

/** Apply the mode's actor rules: full_auto = two different agents; semi_auto = a person reviews. */
export function withMode(v: DemandFormValue, mode: ExecutionMode): DemandFormValue {
  if (mode === 'full_auto') {
    return {
      ...v,
      mode,
      executor: v.executor.kind === 'agent' ? v.executor : { kind: 'agent', id: '' },
      reviewer: v.reviewer.kind === 'agent' ? v.reviewer : { kind: 'agent', id: '' },
      preReviewerId: '',
    };
  }
  return { ...v, mode, reviewer: v.reviewer.kind === 'human' ? v.reviewer : { kind: 'human', id: '' } };
}

export function composeDemand(v: DemandFormValue, t: TFunction): string {
  const d = v.demand.trim();
  const dl = v.deliverables.trim();
  return dl ? `${d}\n\n${t('collab.form.deliverablesSection')}\n${dl}` : d;
}

export type FormIssues = Partial<Record<'demand' | 'executor' | 'reviewer' | 'preReviewer' | 'fixedInputs', string>>;

export function formIssues(v: DemandFormValue, t: TFunction): FormIssues {
  const e: FormIssues = {};
  const demand = composeDemand(v, t);
  if (!demand) e.demand = t('validation.required', { field: t('collab.form.demand') });
  else if (demand.length > DEMAND_MAX) e.demand = t('validation.maxLength', { field: t('collab.form.demand'), max: DEMAND_MAX });
  if (!v.executor.id) e.executor = t('validation.required', { field: t('collab.form.executor') });
  const turbo = v.policy === 'turbo';
  if (!v.reviewer.id && !turbo) e.reviewer = t('validation.required', { field: t('collab.form.reviewer') });
  if (turbo) {
    // No acceptance in Turbo: a reviewer is optional and only kept for a later standard re-plan.
    if (v.mode === 'full_auto' && v.executor.kind !== 'agent') e.executor = t('collab.form.needsAgent');
  } else if (v.mode === 'full_auto') {
    if (v.executor.kind !== 'agent') e.executor = t('collab.form.needsAgent');
    if (v.reviewer.kind !== 'agent') e.reviewer = t('collab.form.needsAgent');
    else if (v.reviewer.id && v.reviewer.id === v.executor.id) e.reviewer = t('collab.form.sameAgent');
  } else {
    if (v.reviewer.kind !== 'human') e.reviewer = t('collab.form.needsHuman');
    else if (v.executor.kind === 'human' && v.executor.id && v.executor.id === v.reviewer.id) e.reviewer = t('collab.form.samePerson');
  }
  const seen = new Set<string>();
  for (const f of v.fixedInputs) {
    if (!SLOT_RE.test(f.slot_key)) e.fixedInputs = t('collab.form.slotInvalid');
    else if (seen.has(f.slot_key)) e.fixedInputs = t('collab.form.slotDuplicate', { slot: f.slot_key });
    seen.add(f.slot_key);
  }
  return e;
}

/** C01 body (without the idempotency key, which the page derives from this content). */
export function planBodyOf(v: DemandFormValue, t: TFunction, workflowId: UUID | null): Omit<PlanIn, 'idempotency_key'> {
  return {
    demand: composeDemand(v, t),
    execution_mode: v.mode,
    execution_policy: v.policy,
    planning_source: 'ai',
    planner_agent_id: v.plannerId || null,
    executor: actorRef(v.executor.kind, v.executor.id),
    reviewer: v.policy === 'turbo' && !v.reviewer.id ? null : actorRef(v.reviewer.kind, v.reviewer.id),
    pre_reviewer_agent_id: v.mode === 'semi_auto' && v.preReviewerId ? v.preReviewerId : null,
    part_id: v.partId || null,
    fixed_inputs: v.fixedInputs.map((f) => ({ slot_key: f.slot_key, asset_version_id: f.asset_version_id })),
    knowledge_item_ids: [],
    graph: null,
    // A ready full-auto draft starts in the same request (design C01); semi-auto always confirms first.
    auto_start: v.mode === 'full_auto',
    workflow_id: workflowId,
  };
}

/** C02 PATCH body of the form fields (nodes are patched separately, one at a time). */
export function patchBodyOf(v: DemandFormValue, t: TFunction, expectedVersion: number): DraftPatch {
  return {
    expected_version: expectedVersion,
    demand: composeDemand(v, t),
    execution_mode: v.mode,
    executor: actorRef(v.executor.kind, v.executor.id),
    ...(v.reviewer.id ? { reviewer: actorRef(v.reviewer.kind, v.reviewer.id) } : {}),
    pre_reviewer_agent_id: v.mode === 'semi_auto' && v.preReviewerId ? v.preReviewerId : null,
    part_id: v.partId || null,
    fixed_inputs: v.fixedInputs.map((f) => ({ slot_key: f.slot_key, asset_version_id: f.asset_version_id })),
  };
}

// ─── fixed input dialog ────────────────────────────────────────────────────

function FixedInputDialog({ projectId, taken, onClose, onAdd }: { projectId: UUID; taken: string[]; onClose: () => void; onAdd: (row: FixedInputRow) => void }) {
  const { t } = useI18n();
  const [slot, setSlot] = useState('');
  const [version, setVersion] = useState<AssetVersion | null>(null);
  const [touched, setTouched] = useState(false);
  const slotErr = !slot.trim() ? t('validation.required', { field: t('collab.form.slotKey') }) : !SLOT_RE.test(slot.trim()) ? t('collab.form.slotInvalid') : taken.includes(slot.trim()) ? t('collab.form.slotDuplicate', { slot: slot.trim() }) : null;
  const submit = () => {
    setTouched(true);
    if (slotErr || !version) return;
    onAdd({ slot_key: slot.trim(), asset_version_id: version.id, label: version.label, asset_name: version.asset_name, asset_type: version.asset_type, content_hash: version.content_hash });
  };
  return (
    <Dialog
      title={t('collab.form.addFixedInput')}
      subtitle={t('collab.form.fixedInputsHint')}
      onClose={onClose}
      size="lg"
      onSubmit={submit}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!version}>
            <Plus /> {t('common.add')}
          </button>
        </>
      }
    >
      <div className="stack">
        <Field label={t('collab.form.slotKey')} hint={t('collab.form.slotKeyHint')} error={touched ? slotErr : null}>
          <input className="input mono" value={slot} maxLength={80} onChange={(e) => setSlot(e.target.value)} placeholder="reference_document" />
        </Field>
        <AssetVersionPicker projectId={projectId} value={version} onChange={setVersion} />
      </div>
    </Dialog>
  );
}

// ─── form ──────────────────────────────────────────────────────────────────

export function DemandForm({
  projectId,
  value,
  onChange,
  members,
  agents,
  parts,
  issues,
  showDeliverables,
  busy,
  disabled,
  canSave,
  onPlan,
  onSave,
  isReplan,
  policyNote,
}: {
  projectId: UUID;
  value: DemandFormValue;
  onChange: (v: DemandFormValue) => void;
  members: Members;
  agents: Resource<AgentProfile[]>;
  parts: Part[] | undefined;
  /** Shown after the first attempt (null = untouched). */
  issues: FormIssues | null;
  showDeliverables: boolean;
  busy: 'plan' | 'save' | null;
  disabled?: boolean;
  /** Draft page: "save draft" patches the stored draft. */
  canSave: boolean;
  onPlan: () => void;
  onSave?: () => void;
  isReplan: boolean;
  /** Draft page: why changing the policy re-plans (shown under the selector). */
  policyNote?: string | null;
}) {
  const { t, fmt } = useI18n();
  const [adding, setAdding] = useState(false);
  const set = <K extends keyof DemandFormValue>(k: K, v: DemandFormValue[K]) => onChange({ ...value, [k]: v });
  const composedLength = composeDemand(value, t).length;
  const over = composedLength > DEMAND_MAX;
  const full = value.mode === 'full_auto';
  const turbo = value.policy === 'turbo';
  const liveParts = (parts ?? []).filter((p) => !p.archived_at);

  return (
    <form
      noValidate
      className="panel"
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy && !disabled) onPlan();
      }}
    >
      <div className="panel-h">
        <h2>{t('collab.form.section')}</h2>
      </div>
      <fieldset disabled={disabled || !!busy} className="panel-b cl-form" style={{ border: 0, margin: 0, minWidth: 0 }}>
        <div className="cl-mode-cards" role="radiogroup" aria-label={t('collab.form.mode')}>
          {(['full_auto', 'semi_auto'] as const).map((m) => (
            <button key={m} type="button" role="radio" aria-checked={value.mode === m} className="cl-mode-card" onClick={() => onChange(withMode(value, m))}>
              <span className="cl-mode-radio" aria-hidden />
              <span className="cl-mode-title">
                {m === 'full_auto' ? <Bot aria-hidden /> : <UserRound aria-hidden />}
                {t(`collab.mode.${m}`)}
              </span>
              <span className="cl-mode-desc">{t(`collab.modeHint.${m}`)}</span>
            </button>
          ))}
        </div>

        <PolicySelector value={value.policy} onChange={(p) => set('policy', p)} disabled={disabled || !!busy} note={policyNote} />

        <Field label={t('collab.form.demand')} hint={t('collab.form.demandHint')} error={issues?.demand ?? null}>
          <textarea className="input" rows={6} value={value.demand} placeholder={t('collab.form.demandPlaceholder')} onChange={(e) => set('demand', e.target.value)} maxLength={DEMAND_MAX} />
        </Field>
        <span className={`cl-counter ${over ? 'over' : ''}`} aria-live="polite">
          {t('collab.form.counter', { n: fmt.number(composedLength), max: fmt.number(DEMAND_MAX) })}
        </span>

        <div className="stack-sm">
          <div className="row">
            <span className="tk-label grow">{t('collab.form.fixedInputs')}</span>
            <button type="button" className="btn sm" onClick={() => setAdding(true)}>
              <Plus /> {t('collab.form.addFixedInput')}
            </button>
          </div>
          {value.fixedInputs.length ? (
            <ul className="cl-inputs">
              {value.fixedInputs.map((f) => (
                <li key={f.slot_key}>
                  <span className="cl-slot">{f.slot_key}</span>
                  <span className="grow ellip">
                    {f.asset_name ? `${f.asset_name} ` : ''}
                    <span className="mono strong">{f.label || f.asset_version_id.slice(0, 8)}</span>
                    {f.asset_type && <span className="faint small"> · {assetTypeLabel(t, f.asset_type)}</span>}
                  </span>
                  <button type="button" className="btn ghost icon sm" aria-label={t('collab.form.removeInput', { slot: f.slot_key })} onClick={() => set('fixedInputs', value.fixedInputs.filter((x) => x.slot_key !== f.slot_key))}>
                    <Trash2 />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('collab.form.noFixedInputs')}</p>
          )}
          {issues?.fixedInputs && <span className="field-error">{issues.fixedInputs}</span>}
        </div>

        {showDeliverables && (
          <Field label={t('collab.form.deliverables')} hint={t('collab.form.deliverablesHint')} optional>
            <LimitedTextarea className="input" rows={2} value={value.deliverables} placeholder={t('collab.form.deliverablesPlaceholder')} onChange={(e) => set('deliverables', e.target.value)} limit="description" />
          </Field>
        )}

        <Field label={t('collab.form.part')} hint={t('collab.form.partHint')} optional>
          <select className="select" value={value.partId} onChange={(e) => set('partId', e.target.value)}>
            <option value="">{t('collab.form.partNone')}</option>
            {liveParts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.number} · {p.name}
              </option>
            ))}
            {value.partId && !liveParts.some((p) => p.id === value.partId) && <option value={value.partId}>{value.partId.slice(0, 8)}</option>}
          </select>
        </Field>

        <div className="form-grid">
          <ActorPicker
            projectId={projectId}
            label={full ? t('collab.form.executorAgent') : t('collab.form.executor')}
            hint={t('collab.form.executorHint')}
            role="execute"
            mode={value.mode}
            kinds={full ? ['agent'] : ['human', 'agent']}
            value={value.executor}
            onChange={(v) => set('executor', v)}
            members={members}
            agents={agents}
            error={issues?.executor ?? null}
          />
          <ActorPicker
            projectId={projectId}
            label={full ? t('collab.form.reviewerAgent') : t('collab.form.reviewerHuman')}
            hint={turbo ? t('tb.policy.turboReviewer') : full ? t('collab.form.reviewerAgentHint') : t('collab.form.reviewerHumanHint')}
            optional={turbo}
            role="review"
            mode={value.mode}
            kinds={full ? ['agent'] : ['human']}
            value={value.reviewer}
            onChange={(v) => set('reviewer', v)}
            members={members}
            agents={agents}
            excludeAgentId={full ? value.executor.id || undefined : undefined}
            excludeUserId={!full && value.executor.kind === 'human' ? value.executor.id || undefined : undefined}
            error={issues?.reviewer ?? null}
          />
          {!full && !turbo && (
            <ActorPicker
              projectId={projectId}
              label={t('collab.form.preReviewer')}
              hint={t('collab.form.preReviewerHint')}
              role="pre_review"
              mode={value.mode}
              kinds={['agent']}
              value={{ kind: 'agent', id: value.preReviewerId }}
              onChange={(v) => set('preReviewerId', v.id)}
              members={members}
              agents={agents}
              error={issues?.preReviewer ?? null}
              optional
            />
          )}
          <ActorPicker
            projectId={projectId}
            label={t('collab.form.planner')}
            hint={t('collab.form.plannerHint')}
            role="planner"
            mode={value.mode}
            kinds={['agent']}
            value={{ kind: 'agent', id: value.plannerId }}
            onChange={(v) => set('plannerId', v.id)}
            members={members}
            agents={agents}
            optional
          />
        </div>

        <div className="cl-form-foot">
          <button type="submit" className="btn primary lg" disabled={!!busy || disabled}>
            <Sparkles /> {busy === 'plan' ? t('collab.form.planning') : isReplan ? t('collab.form.replan') : t('collab.form.plan')}
          </button>
          {canSave && onSave && (
            <button type="button" className="btn lg" disabled={!!busy || disabled} onClick={onSave}>
              {busy === 'save' ? t('common.saving') : t('collab.form.save')}
            </button>
          )}
          <span className="faint small">{full ? t('collab.form.autoStartHint') : t('collab.form.planHint')}</span>
        </div>
      </fieldset>
      {adding && (
        <FixedInputDialog
          projectId={projectId}
          taken={value.fixedInputs.map((f) => f.slot_key)}
          onClose={() => setAdding(false)}
          onAdd={(row) => {
            set('fixedInputs', [...value.fixedInputs, row]);
            setAdding(false);
          }}
        />
      )}
    </form>
  );
}
