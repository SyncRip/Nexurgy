// Agent detail page (UI06, design §3.2 / C07), laid out like the R1.2 prototype
// #agent.<id>: a header with back / copy / enable-disable / "save as v{n+1}",
// the configuration cards on the left (basic, model & pricing, instructions,
// knowledge, tool whitelist, behaviour guardrails, execution modes, budget) and
// usage / versions on the right. Saves carry expected_version; a 409
// keeps the form and offers a reload. Unknown costs are "not set", never 0.
// Running instances keep their own snapshot: editing never changes them.
//
// Only persisted configuration is edited here. Retired runtimes remain readable as historical profiles.

import { ArrowLeft, Bot, Check, CheckCircle2, CircleDashed, CircleX, Copy, ExternalLink, Info, Lock, PauseCircle, PlayCircle, RefreshCw, Save, Stethoscope, TriangleAlert } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  AGENT_PURPOSES,
  AGENT_RUNTIMES,
  MAX_TOOL_CALLS_RANGE,
  isSelectableRuntime,
  type SelectableAgentRuntime,
  type AgentRuntime,
  createAgentProfile,
  DEFAULT_LIMITS,
  EXECUTION_MODES,
  LIMIT_RANGES,
  patchAgentProfile,
  REVIEW_ONLY_TOOLS,
  purposeAllows,
  type AgentLimits,
  type AgentProfile,
  type AgentProfileIn,
  type AgentPurpose,
  type BoundedLimit,
  type ExecutionMode,
} from '../../api/agents';
import { isApiError } from '../../api/client';
import type { ModelConfig, Project } from '../../api/types';
import { paths } from '../../app/nav';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { validationFields } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import type { Members } from '../tasks/common';
import { issueLabel, knowledgeLabel, modelBad, modelLabel } from './labels';
import { ModelReadiness, PurposeIcon } from './common';
import { agentIssues, modelState, type AgentUse } from './model';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { RuntimeChip, RuntimeIcon } from '../agentExec/common';
import { ToolContract } from './ToolContract';

const BOUNDED: BoundedLimit[] = ['max_steps', 'max_duration_s', 'max_attempts', 'max_reworks'];
const UNIT: Record<BoundedLimit | 'max_cost_fen', 'steps' | 'seconds' | 'times' | 'fen'> = {
  max_steps: 'steps',
  max_duration_s: 'seconds',
  max_attempts: 'times',
  max_reworks: 'times',
  max_cost_fen: 'fen',
};
const PURPOSE_TONE: Record<string, string> = { execute: 'measure', review: 'warn', planner: 'stale', general: 'pending' };
/** Rules the server already enforces for every agent (collaboration self_review, agent_tools whitelist, role snapshots, execution records). */
const FIXED_RULES = ['selfReview', 'whitelist', 'snapshot', 'records'] as const;

interface FormState {
  name: string;
  description: string;
  role_prompt: string;
  purpose: AgentPurpose;
  model_config_id: string;
  execution_modes: ExecutionMode[];
  allowed_tools: string[];
  scope: 'project' | 'none';
  /** Text so partial input survives; parsed on save. */
  limits: Record<BoundedLimit | 'max_cost_fen', string>;
  pricingOn: boolean;
  in_fen_per_1k: string;
  out_fen_per_1k: string;
  runtime: AgentRuntime;
  /** tool_agent options (runtime_options). */
  parallel: boolean;
  /** tool_agent tool-call limit. */
  maxTools: string;
}

function fromProfile(p: AgentProfile | null, name?: string): FormState {
  const lim: AgentLimits = { ...DEFAULT_LIMITS, ...(p?.limits ?? {}) };
  return {
    name: name ?? p?.name ?? '',
    description: p?.description ?? '',
    role_prompt: p?.role_prompt ?? '',
    purpose: p?.purpose ?? 'execute',
    model_config_id: p?.model_config?.id ?? '',
    execution_modes: p?.execution_modes ?? ['full_auto', 'semi_auto'],
    allowed_tools: p?.allowed_tools ?? [],
    scope: p?.knowledge_scope?.scope === 'none' ? 'none' : 'project',
    limits: {
      max_steps: String(lim.max_steps),
      max_duration_s: String(lim.max_duration_s),
      max_attempts: String(lim.max_attempts),
      max_reworks: String(lim.max_reworks),
      max_cost_fen: lim.max_cost_fen === null || lim.max_cost_fen === undefined ? '' : String(lim.max_cost_fen),
    },
    pricingOn: !!p?.pricing,
    in_fen_per_1k: p?.pricing ? String(p.pricing.in_fen_per_1k) : '',
    out_fen_per_1k: p?.pricing ? String(p.pricing.out_fen_per_1k) : '',
    runtime: p?.runtime ?? 'native',
    parallel: p?.runtime_options?.parallel_tool_calls === true,
    maxTools: p?.runtime_options?.max_tool_calls == null ? '' : String(p.runtime_options.max_tool_calls),
  };
}

const RUNTIME_TAG: Record<SelectableAgentRuntime, { key: 'ax.rt.recommended' | 'ax.rt.fallback'; tone: string }> = {
  tool_agent: { key: 'ax.rt.recommended', tone: 'ok' },
  native: { key: 'ax.rt.fallback', tone: 'pending' },
};

/** One capability line (prototype .cap): icon + text, never colour alone. */
function CapRow({ state, title, note, action }: { state: 'ok' | 'err' | 'pending'; title: string; note: string; action?: ReactNode }) {
  return (
    <div className={`ax-cap ${state}`}>
      {state === 'ok' ? <CheckCircle2 aria-hidden /> : state === 'err' ? <CircleX aria-hidden /> : <CircleDashed aria-hidden />}
      <span>
        {title}
        <small>{note}</small>
      </span>
      <span>{action}</span>
    </div>
  );
}

/** Integer in [min, max] or null when the text is not a whole number in range. */
function intIn(text: string, min: number, max: number): number | null {
  if (!/^\s*-?\d+\s*$/.test(text)) return null;
  const n = Number.parseInt(text, 10);
  return Number.isFinite(n) && n >= min && n <= max ? n : null;
}

/** Key / value rows of the side cards (prototype .list). */
function Kv({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="ag-kv">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A card of the detail page (prototype .card with .card-h). */
function Sec({ title, aside, children, id }: { title: string; aside?: ReactNode; children: ReactNode; id: string }) {
  return (
    <section className="panel ag-sec" aria-labelledby={id}>
      <div className="ag-sec-h">
        <h3 id={id}>{title}</h3>
        {aside && <span className="ag-sec-aside">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

export function AgentProfileEditor({
  project,
  profile,
  prefill,
  configs,
  configsError,
  members,
  canManage,
  usage,
  kbTotal,
  onSaved,
  onBack,
  onCopy,
  onReload,
}: {
  project: Pick<Project, 'id' | 'code'>;
  /** null = create a new profile. */
  profile: AgentProfile | null;
  /** New profile only: start from this configuration (Copy) under the given name. */
  prefill?: { from: AgentProfile; name: string } | null;
  configs: ModelConfig[] | undefined;
  configsError: unknown;
  members: Members;
  canManage: boolean;
  /** Workflows / templates whose graph uses this agent; undefined when the list was not loaded. */
  usage: AgentUse[] | undefined;
  /** Active knowledge collections of the project (null = unknown). */
  kbTotal: number | null | undefined;
  onSaved: (p: AgentProfile) => void;
  /** Back to the wall. */
  onBack: () => void;
  /** Open a new form prefilled from this profile. */
  onCopy?: () => void;
  /** Re-fetch the profile after a version conflict. */
  onReload: () => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const initial = useMemo(() => fromProfile(profile ?? prefill?.from ?? null, profile ? undefined : prefill?.name), [profile, prefill]);
  const [form, setForm] = useState<FormState>(initial);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [serverFields, setServerFields] = useState<Record<string, string>>({});
  const [toggling, setToggling] = useState<'disable' | 'enable' | null>(null);
  const [toggleBusy, setToggleBusy] = useState(false);
  const [toggleError, setToggleError] = useState<unknown>(null);
  const retiredRuntime = !isSelectableRuntime(form.runtime);
  const readOnly = !canManage || retiredRuntime;

  // A different profile (or a fresh one) resets the form; unsaved edits of the previous one are dropped on purpose.
  const key = profile?.id ?? `new:${prefill?.from.id ?? ''}`;
  const version = profile?.version;
  useEffect(() => {
    setForm(initial);
    setTouched(false);
    setError(null);
    setServerFields({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, version]);

  const dirty = !profile || JSON.stringify(form) !== JSON.stringify(initial);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));
  const setLimit = (k: BoundedLimit | 'max_cost_fen', v: string) => setForm((f) => ({ ...f, limits: { ...f.limits, [k]: v } }));

  // engineering_review only for review / general profiles (design §3.2).
  const reviewTools = purposeAllows(form.purpose, 'review');
  const setPurpose = (p: AgentPurpose) =>
    setForm((f) => ({ ...f, purpose: p, allowed_tools: purposeAllows(p, 'review') ? f.allowed_tools : f.allowed_tools.filter((x) => !REVIEW_ONLY_TOOLS.has(x)) }));

  // ─── validation ──────────────────────────────────────────────────────────
  const errors = useMemo(() => {
    const e: Partial<Record<string, string>> = {};
    if (!form.name.trim()) e.name = t('validation.required', { field: t('agents.field.name') });
    else if (form.name.trim().length > 200) e.name = t('validation.maxLength', { field: t('agents.field.name'), max: 200 });
    if (!form.execution_modes.length) e.execution_modes = t('agents.validation.modes');
    for (const k of BOUNDED) {
      const r = LIMIT_RANGES[k];
      if (intIn(form.limits[k], r.min, r.max) === null) e[k] = t('agents.validation.range', { min: r.min, max: r.max });
    }
    if (form.limits.max_cost_fen.trim() && intIn(form.limits.max_cost_fen, 0, Number.MAX_SAFE_INTEGER) === null) e.max_cost_fen = t('agents.validation.nonNegative');
    if (form.runtime !== 'native' && form.maxTools.trim() && intIn(form.maxTools, MAX_TOOL_CALLS_RANGE.min, MAX_TOOL_CALLS_RANGE.max) === null) e.max_tool_calls = t('ax.ed.maxToolsInvalid');
    if (form.pricingOn) {
      if (intIn(form.in_fen_per_1k, 0, Number.MAX_SAFE_INTEGER) === null) e.in_fen_per_1k = t('agents.validation.nonNegative');
      if (intIn(form.out_fen_per_1k, 0, Number.MAX_SAFE_INTEGER) === null) e.out_fen_per_1k = t('agents.validation.nonNegative');
    }
    return e;
  }, [form, t]);
  const fieldError = (k: string): string | null => (touched && errors[k]) || serverFields[k] || null;
  const shownErrors = Object.entries({ ...(touched ? errors : {}), ...serverFields }).filter((kv): kv is [string, string] => !!kv[1]);

  // Issues of the configuration as it would run (the saved profile, or the form's model choice).
  const selectedConfig = configs?.find((c) => c.id === form.model_config_id);
  const formModel = selectedConfig
    ? { id: selectedConfig.id, label: selectedConfig.label, provider: selectedConfig.provider, model_id: selectedConfig.model_id, ready: selectedConfig.ready && selectedConfig.enabled }
    : form.model_config_id && profile?.model_config?.id === form.model_config_id
      ? profile.model_config
      : null;
  const issues = agentIssues(
    {
      model_config: formModel,
      model_config_id: form.model_config_id || null,
      purpose: form.purpose,
      allowed_tools: form.allowed_tools,
      execution_modes: form.execution_modes,
      limits: profile?.limits ?? DEFAULT_LIMITS,
      knowledge_scope: { scope: form.scope },
    },
    configs,
  ).filter((x) => !(x === 'model_missing' && !configs));
  const model = modelState({ model_config: formModel, model_config_id: form.model_config_id || null }, configs);
  // The configuration whose capabilities the runtime needs: the chosen one, else the project default.
  const capConfig = form.model_config_id ? selectedConfig : configs?.find((c) => c.is_default && c.enabled);

  const body = (): AgentProfileIn => {
    if (!isSelectableRuntime(form.runtime)) throw new Error(t('ax.rt.retired'));
    return {
      name: form.name.trim(),
      purpose: form.purpose,
      description: form.description.trim(),
      role_prompt: form.role_prompt.trim(),
      model_config_id: form.model_config_id || null,
      allowed_tools: form.allowed_tools,
      execution_modes: form.execution_modes,
      knowledge_scope: { scope: form.scope },
      limits: {
        max_steps: Number.parseInt(form.limits.max_steps, 10),
        max_duration_s: Number.parseInt(form.limits.max_duration_s, 10),
        max_attempts: Number.parseInt(form.limits.max_attempts, 10),
        max_reworks: Number.parseInt(form.limits.max_reworks, 10),
        max_cost_fen: form.limits.max_cost_fen.trim() ? Number.parseInt(form.limits.max_cost_fen, 10) : null,
      },
      pricing: form.pricingOn ? { in_fen_per_1k: Number.parseInt(form.in_fen_per_1k, 10), out_fen_per_1k: Number.parseInt(form.out_fen_per_1k, 10) } : null,
      runtime: form.runtime,
      // Closed per-runtime structure (plan §4.7); native has no options.
      runtime_options: form.runtime === 'tool_agent' ? { parallel_tool_calls: form.parallel, max_tool_calls: maxToolCalls() } : {},
    };
  };
  const maxToolCalls = () => (form.maxTools.trim() ? Number.parseInt(form.maxTools, 10) : null);

  const save = async () => {
    if (readOnly) return;
    setTouched(true);
    setServerFields({});
    if (Object.keys(errors).length) return;
    setBusy(true);
    setError(null);
    try {
      const saved = profile ? await patchAgentProfile(profile.id, { expected_version: profile.version, ...body() }) : await createAgentProfile(project.id, body());
      toast(t(profile ? 'agents.saved' : 'agents.created', { name: saved.name }));
      onSaved(saved);
    } catch (e) {
      setError(e);
      if (isApiError(e)) {
        const fields: Record<string, string> = {};
        if (e.code === 'model_config_unusable') fields.model_config_id = t('agents.err.model_config_unusable');
        if (e.code === 'name_taken' || e.code === 'agent_name_taken') fields.name = t('agents.err.name_taken');
        for (const f of validationFields(e)) fields[f] = t('agents.validation.serverField');
        setServerFields(fields);
      }
    } finally {
      setBusy(false);
    }
  };

  const toggle = async () => {
    if (!profile || readOnly) return;
    setToggleBusy(true);
    setToggleError(null);
    try {
      const saved = await patchAgentProfile(profile.id, { expected_version: profile.version, enabled: toggling === 'enable' });
      toast(t(toggling === 'enable' ? 'agents.enabledToast' : 'agents.disabledToast', { name: saved.name }));
      setToggling(null);
      onSaved(saved);
    } catch (e) {
      setToggleError(e);
    } finally {
      setToggleBusy(false);
    }
  };

  const conflict = isApiError(error) && error.code === 'version_conflict';
  const title = profile ? profile.name : prefill ? form.name || t('agents.newTitle') : t('agents.newTitle');

  return (
    <div className="ag-page">
      <header className="ag-page-h">
        <div className="ag-page-titles">
          <span className={`ag-wall-ico big tone-${PURPOSE_TONE[form.purpose] ?? 'pending'}`} aria-hidden>
            <PurposeIcon purpose={form.purpose} />
          </span>
          <div className="ag-page-name">
            <h2>{title}</h2>
            <p className="muted small">
              {t('fl.ag.meta', { code: project.code })}
              {profile && (
                <>
                  {' · '}
                  <span className="mono" title={t('agents.versionHint')}>
                    v{profile.version}
                  </span>
                  {' · '}
                  {profile.enabled ? t('fl.ag.enabledState') : t('agents.disabledState')}
                </>
              )}
              {profile && dirty && !readOnly && <span className="ag-dirty"> · {t('fl.ag.unsaved')}</span>}
            </p>
          </div>
        </div>
        <div className="ag-page-actions">
          <button type="button" className="btn ghost" onClick={onBack}>
            <ArrowLeft /> {t('fl.ag.back')}
          </button>
          {profile && !readOnly && onCopy && (
            <button type="button" className="btn" onClick={onCopy} disabled={busy}>
              <Copy /> {t('fl.ag.copy')}
            </button>
          )}
          {profile && !readOnly && (
            <button type="button" className="btn" onClick={() => setToggling(profile.enabled ? 'disable' : 'enable')} disabled={busy}>
              {profile.enabled ? <PauseCircle /> : <PlayCircle />} {profile.enabled ? t('fl.ag.disable') : t('fl.ag.enable')}
            </button>
          )}
          {!readOnly && (
            <button type="submit" form="ag-form" className="btn primary" disabled={busy || !dirty}>
              <Save /> {busy ? t('common.saving') : profile ? t('fl.ag.saveAs', { n: profile.version + 1 }) : t('agents.create')}
            </button>
          )}
        </div>
      </header>

      {shownErrors.length > 0 && (
        <div className="callout err slim" role="alert">
          <TriangleAlert aria-hidden />
          <span>
            <b>{t('fl.ag.fixBeforeSave', { n: shownErrors.length })}</b> {shownErrors.map(([, m]) => m).join('；')}
          </span>
        </div>
      )}
      {issues.length > 0 && (
        <div className="callout warn slim" role="note">
          <TriangleAlert aria-hidden />
          <span>
            <b>{t('fl.ag.issuesLead', { n: issues.length })}</b> 
            {issues.map((x) => issueLabel(t, x)).join('；')}
          </span>
        </div>
      )}
      {readOnly && (
        <div className="callout pending slim" role="note">
          <Info aria-hidden />
          <span>{t(retiredRuntime ? 'ax.rt.retired' : 'agents.readOnly')}</span>
        </div>
      )}

      <div className="ag-grid">
        <form
          id="ag-form"
          noValidate
          className="ag-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!busy && !readOnly && dirty) void save();
          }}
        >
          <fieldset disabled={readOnly || busy} className="ag-form">
            <Sec id="ag-sec-basic" title={t('fl.ag.sec.basic')}>
              <div className="form-grid">
                <Field label={t('agents.field.name')} error={fieldError('name')}>
                  <input className="input" value={form.name} maxLength={200} onChange={(e) => set('name', e.target.value)} data-autofocus />
                </Field>
                <Field label={t('agents.field.description')} hint={t('agents.field.descriptionHint')} error={fieldError('description')} optional>
                  <input className="input" value={form.description} maxLength={500} onChange={(e) => set('description', e.target.value)} />
                </Field>
              </div>
              <div className="ag-section">
                <h4>{t('agents.field.purpose')}</h4>
                <div className="ag-cards" role="radiogroup" aria-label={t('agents.field.purpose')}>
                  {AGENT_PURPOSES.map((p) => (
                    <button key={p} type="button" role="radio" aria-checked={form.purpose === p} className="ag-card" onClick={() => setPurpose(p)}>
                      <span className="ag-card-mark round">{form.purpose === p && <Check aria-hidden />}</span>
                      <span className="ag-card-title">
                        <PurposeIcon purpose={p} />
                        {t(`agents.purpose.${p}`)}
                      </span>
                      <span className="ag-card-desc">{t(`agents.purposeHint.${p}`)}</span>
                    </button>
                  ))}
                </div>
                <p className="ag-section-hint">{t('agents.field.purposeHint')}</p>
                {fieldError('purpose') && <span className="field-error">{fieldError('purpose')}</span>}
              </div>
            </Sec>

            <Sec id="ag-sec-runtime" title={t('ax.ed.sec.runtime')} aside={<span className="faint small">{t('ax.ed.runtimeAside')}</span>}>
              <div className="ag-cards" role="radiogroup" aria-label={t('ax.ed.sec.runtime')}>
                {AGENT_RUNTIMES.filter(isSelectableRuntime).map((rt) => {
                  const tag = RUNTIME_TAG[rt];
                  return (
                    <button
                      key={rt}
                      type="button"
                      role="radio"
                      aria-checked={form.runtime === rt}
                      className="ag-card ax-rt-card"
                      onClick={() => set('runtime', rt)}
                    >
                      <span className="ag-card-mark round">{form.runtime === rt && <Check aria-hidden />}</span>
                      <span className="ag-card-title">
                        <RuntimeIcon runtime={rt} />
                        {t(`ax.rt.${rt}`)}
                        <span className={`chip ${tag.tone} cl-chip-sm`}>{t(tag.key)}</span>
                      </span>
                      <span className="ag-card-desc">{t(`ax.rt.desc.${rt}`)}</span>
                    </button>
                  );
                })}
              </div>
              {retiredRuntime && <div className="callout" role="status"><RuntimeChip runtime={form.runtime} /><span>{t('ax.rt.retired')}</span></div>}
              {fieldError('runtime') && <span className="field-error">{fieldError('runtime')}</span>}
              {form.runtime === 'tool_agent' && (
                <div className="form-grid">
                  <div className="ag-section">
                    <label className="check">
                      <input type="checkbox" checked={form.parallel} onChange={(e) => set('parallel', e.target.checked)} />
                      {t('ax.ed.parallel')}
                    </label>
                    <p className="ag-section-hint">{t('ax.ed.parallelHint')}</p>
                  </div>
                  <Field label={t('ax.ed.maxTools')} hint={t('ax.ed.maxToolsHint')} error={fieldError('max_tool_calls') ?? fieldError('runtime_options')} optional>
                    <input className="input" inputMode="numeric" value={form.maxTools} placeholder="—" onChange={(e) => set('maxTools', e.target.value)} />
                  </Field>
                </div>
              )}
              {form.runtime === 'native' && profile?.runtime && profile.runtime !== 'native' && (
                <div className="callout warn slim" role="note">
                  <TriangleAlert aria-hidden />
                  <span>{t('ax.ed.nativeWarn')}</span>
                </div>
              )}
              <p className="ag-section-hint">{t('ax.ed.simNote')}</p>
            </Sec>

            <Sec
              id="ag-sec-model"
              title={t('fl.ag.sec.model')}
              aside={
                <Link to={paths.settings()} className="small">
                  {t('fl.ag.modelSettings')} →
                </Link>
              }
            >
              <Field label={t('agents.field.modelConfig')} hint={configsError ? undefined : t('agents.field.modelConfigHint')} error={fieldError('model_config_id')}>
                <select className="select" value={form.model_config_id} onChange={(e) => set('model_config_id', e.target.value)}>
                  <option value="">{t('agents.field.modelConfigDefault')}</option>
                  {(configs ?? []).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label} · {c.model_id} · {c.ready ? t('agents.modelReady') : c.enabled ? t('agents.modelNotReady') : t('agents.modelDisabled')}
                    </option>
                  ))}
                  {profile?.model_config && !configs?.some((c) => c.id === profile.model_config?.id) && (
                    <option value={profile.model_config.id}>
                      {profile.model_config.label} · {profile.model_config.model_id}
                    </option>
                  )}
                </select>
              </Field>
              <div className="ag-model-state small">
                {configsError ? <ErrorNotice error={configsError} /> : null}
                <span className={`chip mono ${modelBad(model) ? 'err' : 'pending'}`}>{modelLabel(t, model)}</span>
                {selectedConfig ? (
                  <ModelReadiness config={{ id: selectedConfig.id, label: selectedConfig.label, provider: selectedConfig.provider, model_id: selectedConfig.model_id, ready: selectedConfig.ready }} enabled={selectedConfig.enabled} />
                ) : form.model_config_id && profile?.model_config ? (
                  <ModelReadiness config={profile.model_config} />
                ) : null}
              </div>
              <div className="ag-section">
                <h4>{form.runtime === 'native' ? t('ax.ed.cap.nativeTitle') : t('ax.ed.cap.title')}</h4>
                {form.runtime === 'native' ? (
                  <CapRow state="pending" title={t('ax.ed.cap.tools')} note={t('ax.ed.cap.nativeOk')} />
                ) : capConfig ? (
                  capConfig.capabilities?.tools ? (
                    <CapRow state="ok" title={t('ax.ed.cap.tools')} note={`${capConfig.label} · ${t('ax.ed.cap.toolsOk')}`} />
                  ) : (
                    <CapRow
                      state="err"
                      title={t('ax.ed.cap.tools')}
                      note={`${capConfig.label} · ${t('ax.ed.cap.toolsMissing')}`}
                      action={
                        <Link to={`${paths.settings()}?diagnose=${encodeURIComponent(capConfig.id)}`} className="small nowrap">
                          <Stethoscope className="inline-ico" aria-hidden /> {t('ax.ed.cap.diagnose')}
                        </Link>
                      }
                    />
                  )
                ) : (
                  <CapRow state="pending" title={t('ax.ed.cap.tools')} note={t('ax.ed.cap.defaultModel')} />
                )}
                {profile && profile.model_available !== undefined && !dirty && (
                  <span className={`chip ${profile.model_available ? 'ok' : 'err'}`}>
                    {profile.model_available ? <CheckCircle2 aria-hidden /> : <CircleX aria-hidden />}
                    {t(profile.model_available ? 'ax.ed.cap.available' : 'ax.ed.cap.unavailable')}
                  </span>
                )}
              </div>
              <div className="ag-section">
                <h4>{t('agents.field.pricing')}</h4>
                <label className="check">
                  <input type="checkbox" checked={form.pricingOn} onChange={(e) => set('pricingOn', e.target.checked)} />
                  {t('agents.field.pricingOn')}
                </label>
                {form.pricingOn ? (
                  <div className="form-grid">
                    <Field label={t('agents.pricing.in')} error={fieldError('in_fen_per_1k')}>
                      <span className="ag-unit-input">
                        <input className="input" inputMode="numeric" value={form.in_fen_per_1k} onChange={(e) => set('in_fen_per_1k', e.target.value)} aria-label={t('agents.pricing.in')} />
                        <span className="ag-unit">{t('agents.limitUnit.fenPer1k')}</span>
                      </span>
                    </Field>
                    <Field label={t('agents.pricing.out')} error={fieldError('out_fen_per_1k')}>
                      <span className="ag-unit-input">
                        <input className="input" inputMode="numeric" value={form.out_fen_per_1k} onChange={(e) => set('out_fen_per_1k', e.target.value)} aria-label={t('agents.pricing.out')} />
                        <span className="ag-unit">{t('agents.limitUnit.fenPer1k')}</span>
                      </span>
                    </Field>
                  </div>
                ) : (
                  <p className="faint small">{t('agents.pricingUnset')}</p>
                )}
                <p className="ag-section-hint">{t('agents.field.pricingHint')}</p>
              </div>
            </Sec>

            <Sec id="ag-sec-prompt" title={t('fl.ag.sec.prompt')}>
              <Field label={t('agents.field.rolePrompt')} hint={t('agents.field.rolePromptHint')} error={fieldError('role_prompt')} optional>
                <LimitedTextarea className="input mono ag-prompt" rows={7} limit="role_prompt" value={form.role_prompt} onChange={(e) => set('role_prompt', e.target.value)} />
              </Field>
            </Sec>

            <Sec
              id="ag-sec-kb"
              title={t('fl.ag.sec.knowledge')}
              aside={!readOnly && (
                <Link to={paths.knowledge(project.id)} className="small">
                  {t('fl.ag.manageKb')} →
                </Link>
              )}
            >
              <Field label={t('agents.field.knowledgeScope')} hint={t('agents.field.knowledgeScopeHint')} error={fieldError('knowledge_scope') ?? fieldError('scope')}>
                <select className="select" value={form.scope} onChange={(e) => set('scope', e.target.value === 'none' ? 'none' : 'project')}>
                  <option value="project">{t('agents.scope.project')}</option>
                  <option value="none">{t('agents.scope.none')}</option>
                </select>
              </Field>
              <div className="row">
                <span className="chip pending">{knowledgeLabel(t, form.scope, kbTotal)}</span>
              </div>
            </Sec>

            <Sec id="ag-sec-tools" title={t('agents.contract.title')} aside={<span className="faint small">{t('fl.ag.toolsAside')}</span>}>
              <ToolContract projectId={project.id} allowed={form.allowed_tools} reviewTools={reviewTools} onChange={(tools) => set('allowed_tools', tools)} />
              <p className="ag-section-hint">{t('agents.field.toolsHint')}</p>
              {fieldError('allowed_tools') && <span className="field-error">{fieldError('allowed_tools')}</span>}
            </Sec>

            <Sec id="ag-sec-guards" title={t('fl.ag.sec.guards')}>
              <div className="ag-fixed">
                <span className="ag-fixed-h">
                  <Lock aria-hidden /> {t('fl.ag.fixedRules')}
                </span>
                <ul>
                  {FIXED_RULES.map((r) => (
                    <li key={r}>{t(`fl.ag.rule.${r}`)}</li>
                  ))}
                </ul>
              </div>
            </Sec>

            <Sec id="ag-sec-modes" title={t('fl.ag.sec.modes')}>
              <div className="ag-cards" role="group" aria-label={t('agents.field.modes')}>
                {EXECUTION_MODES.map((m) => {
                  const on = form.execution_modes.includes(m);
                  return (
                    <button key={m} type="button" aria-pressed={on} className="ag-card" onClick={() => set('execution_modes', on ? form.execution_modes.filter((x) => x !== m) : [...form.execution_modes, m])}>
                      <span className="ag-card-mark">{on && <Check aria-hidden />}</span>
                      <span className="ag-card-title">{t(`agents.mode.${m}`)}</span>
                      <span className="ag-card-desc">{t(`agents.modeHint.${m}`)}</span>
                    </button>
                  );
                })}
              </div>
              <p className="ag-section-hint">{t('agents.field.modesHint')}</p>
              {fieldError('execution_modes') && <span className="field-error">{fieldError('execution_modes')}</span>}
              <div className="callout info slim" role="note">
                <Bot aria-hidden />
                <span>{t('agents.fullAutoNote')}</span>
              </div>
            </Sec>

            <Sec id="ag-sec-budget" title={t('fl.ag.sec.budget')} aside={<span className="faint small">{t('fl.ag.budgetAside')}</span>}>
              <div className="ag-limits">
                {BOUNDED.map((k) => (
                  <Field key={k} label={t(`agents.limit.${k}`)} hint={t('agents.limitRange', { min: LIMIT_RANGES[k].min, max: LIMIT_RANGES[k].max })} error={fieldError(k)}>
                    <span className="ag-unit-input">
                      <input className="input" inputMode="numeric" value={form.limits[k]} onChange={(e) => setLimit(k, e.target.value)} aria-label={t(`agents.limit.${k}`)} />
                      <span className="ag-unit">{t(`agents.limitUnit.${UNIT[k]}`)}</span>
                    </span>
                  </Field>
                ))}
                <Field label={t('agents.limit.max_cost_fen')} hint={t('agents.limit.maxCostHint')} error={fieldError('max_cost_fen')}>
                  <span className="ag-unit-input">
                    <input className="input" inputMode="numeric" value={form.limits.max_cost_fen} placeholder={t('agents.costUnset')} onChange={(e) => setLimit('max_cost_fen', e.target.value)} aria-label={t('agents.limit.max_cost_fen')} />
                    <span className="ag-unit">{t('agents.limitUnit.fen')}</span>
                  </span>
                </Field>
              </div>
              <p className="ag-section-hint">{t('agents.field.limitsHint')}</p>
            </Sec>

          </fieldset>

          {error ? (
            <div className="stack-sm">
              <ErrorNotice error={error} overrides={{ model_config_unusable: 'agents.err.model_config_unusable', name_taken: 'agents.err.name_taken', agent_name_taken: 'agents.err.name_taken' }} />
              {conflict && (
                <div>
                  <button type="button" className="btn sm" onClick={onReload}>
                    <RefreshCw /> {t('tk.reloadLatest')}
                  </button>
                </div>
              )}
            </div>
          ) : null}
          {!readOnly && profile && <p className="faint small">{t('agents.saveHint', { n: profile.version })}</p>}
        </form>

        <aside className="ag-side">
          <Sec id="ag-sec-usage" title={t('fl.ag.sec.usage')}>
            {profile ? (
              <>
                {usage && (usage.length ? (
                  <ul className="ag-uses">
                    {usage.map((u) => (
                      <li key={u.id}>
                        <span className="ellip">
                          <span className="faint small">{u.kind === 'template' ? t('fl.ag.useTemplate') : t('fl.ag.useCollab')} · </span>
                          {u.name}
                        </span>
                        <Link to={u.kind === 'template' ? paths.workflow(u.id) : paths.collaborationDraft(u.id)} className="small">
                          {t('common.open')} <ExternalLink aria-hidden className="ag-ext" />
                        </Link>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="faint small">{t('fl.ag.noUse')}</p>
                ))}
                <Kv
                  rows={[
                    [t('agents.history.runsActive'), <span className="mono">{fmt.number(profile.usage?.runs_active)}</span>],
                    [t('agents.history.executionsTotal'), <span className="mono">{fmt.number(profile.usage?.executions_total)}</span>],
                    [t('agents.history.cost'), <span className="faint">{t('agents.costUnset')}</span>],
                  ]}
                />
              </>
            ) : (
              <p className="faint small">{t('fl.ag.noUseYet')}</p>
            )}
          </Sec>

          <Sec id="ag-sec-versions" title={t('fl.ag.sec.versions')}>
            {profile ? (
              <Kv
                rows={[
                  [t('agents.history.version'), <span className="mono">v{profile.version} · {t('fl.ag.current')}</span>],
                  [t('agents.history.updated'), fmt.dateTime(profile.updated_at)],
                  [t('agents.history.created'), fmt.dateTime(profile.created_at)],
                  [t('agents.history.createdBy'), members.name(profile.created_by)],
                  [t('agents.history.disabledAt'), profile.disabled_at ? fmt.dateTime(profile.disabled_at) : <span className="faint">—</span>],
                ]}
              />
            ) : null}
            <p className="faint small">{t('fl.ag.snapshotNote')}</p>
          </Sec>
        </aside>
      </div>

      {toggling && profile && (
        <ConfirmDialog
          title={t(toggling === 'disable' ? 'agents.disableTitle' : 'agents.enableTitle', { name: profile.name })}
          body={t(toggling === 'disable' ? 'agents.disableBody' : 'agents.enableBody')}
          confirmLabel={t(toggling === 'disable' ? 'agents.disable' : 'agents.enable')}
          danger={toggling === 'disable'}
          busy={toggleBusy}
          error={
            toggleError ? (
              <div className="stack-sm">
                <ErrorNotice error={toggleError} />
                {isApiError(toggleError) && toggleError.code === 'version_conflict' && (
                  <div>
                    <button
                      type="button"
                      className="btn sm"
                      onClick={() => {
                        setToggling(null);
                        onReload();
                      }}
                    >
                      <RefreshCw /> {t('tk.reloadLatest')}
                    </button>
                  </div>
                )}
              </div>
            ) : null
          }
          onConfirm={() => void toggle()}
          onClose={() => {
            setToggling(null);
            setToggleError(null);
          }}
        />
      )}
    </div>
  );
}
