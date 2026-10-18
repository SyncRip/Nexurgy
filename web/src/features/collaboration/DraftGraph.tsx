// UI01 right column: the planned task graph as ordered node cards (number,
// title, "not started", objective, executor, reviewer), the selected node's
// editor (title / objective / acceptance criteria — patched for that node only,
// A54), the planner's missing information, validation errors located on their
// node (A03) and "confirm and start" with its disabled reason always visible.

import { AlertTriangle, ArrowRight, Check, CircleDashed, ExternalLink, HelpCircle, Play, Plus, Save, Sparkles, Trash2, Undo2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { AgentProfile } from '../../api/agents';
import { isApiError } from '../../api/client';
import { businessTaskParams, type BusinessTaskParams, type CriterionParam, type DraftOut, type MissingInformation, type ValidationError } from '../../api/collaboration';
import type { AssetType, RuleSpec } from '../../api/pm';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { Field } from '../../components/ui/Form';
import { EmptyState, ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetTypeToggles, RuleSpecEditor, normalizeSpec } from '../tasks/RuleSpecEditor';
import type { Members } from '../tasks/common';
import { depths } from '../workflow/model';
import { ActorRefLabel, CL_ERRORS, SimulatedChip, validationErrorText } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

export interface OrderedNode {
  id: string;
  seq: number;
  params: BusinessTaskParams;
}

export function orderedNodes(draft: DraftOut | null): OrderedNode[] {
  if (!draft) return [];
  const g = draft.graph;
  const d = depths(
    g.nodes.map((n) => n.id),
    g.edges,
  );
  return [...g.nodes]
    .sort((a, b) => (d.get(a.id) ?? 0) - (d.get(b.id) ?? 0) || a.id.localeCompare(b.id))
    .map((n, i) => ({ id: n.id, seq: i + 1, params: businessTaskParams(n.params) }));
}

function errorsByNode(errors: ValidationError[]): Map<string, ValidationError[]> {
  const m = new Map<string, ValidationError[]>();
  for (const e of errors) {
    if (!e.node_id) continue;
    const xs = m.get(e.node_id) ?? [];
    xs.push(e);
    m.set(e.node_id, xs);
  }
  return m;
}

// ─── node card ─────────────────────────────────────────────────────────────

function DraftNodeCard({ node, selected, errors, members, agents, onSelect }: { node: OrderedNode; selected: boolean; errors: ValidationError[]; members: Members; agents: AgentProfile[] | undefined; onSelect: () => void }) {
  const { t } = useI18n();
  return (
    <button type="button" className={`cl-node ${selected ? 'selected' : ''} ${errors.length ? 'has-errors' : ''}`} aria-pressed={selected} onClick={onSelect} data-node-id={node.id}>
      <span className="cl-node-head">
        <span className="cl-node-seq">{node.seq}</span>
        <span className="cl-node-title ellip" title={node.params.title}>
          {node.params.title || node.id}
        </span>
        <Chip tone="pending" icon={<CircleDashed aria-hidden />} className="cl-chip-sm">
          {t('collab.draft.nodeStatus')}
        </Chip>
      </span>
      <span className="tk-label">{t('collab.draft.objective')}</span>
      <span className="cl-node-objective" title={node.params.objective}>
        {node.params.objective || <span className="faint">{t('common.none')}</span>}
      </span>
      <dl className="cl-node-roles">
        <dt>{t('collab.draft.executor')}</dt>
        <dd>
          <ActorRefLabel actor={node.params.executor} members={members} agents={agents} />
        </dd>
        <dt>{t('collab.draft.reviewer')}</dt>
        <dd>
          <ActorRefLabel actor={node.params.reviewer} members={members} agents={agents} />
        </dd>
      </dl>
      {errors.length > 0 && (
        <ul className="cl-node-errs">
          {errors.map((e, i) => (
            <li key={`${e.code}-${i}`} title={e.message}>
              {validationErrorText(t, e)}
            </li>
          ))}
        </ul>
      )}
    </button>
  );
}

// ─── node editor ───────────────────────────────────────────────────────────

interface CriterionDraft extends CriterionParam {
  key: number;
  spec: RuleSpec;
}

function toDrafts(criteria: CriterionParam[]): CriterionDraft[] {
  return criteria.map((c, i) => ({ ...c, key: i + 1, spec: normalizeSpec(c.rule_spec, c.allowed_types as AssetType[]) }));
}

function NodeEditor({ node, busy, error, onSave, onReload }: { node: OrderedNode; busy: boolean; error: unknown; onSave: (patch: Partial<BusinessTaskParams>) => Promise<void>; onReload: () => void }) {
  const { t } = useI18n();
  const [title, setTitle] = useState(node.params.title);
  const [objective, setObjective] = useState(node.params.objective);
  const [criteria, setCriteria] = useState<CriterionDraft[]>(() => toDrafts(node.params.criteria));
  const [seq, setSeq] = useState(node.params.criteria.length + 1);
  const [touched, setTouched] = useState(false);

  // A new node (or a reloaded draft) replaces the editor content; unsaved edits of another node are kept by the key above.
  useEffect(() => {
    setTitle(node.params.title);
    setObjective(node.params.objective);
    setCriteria(toDrafts(node.params.criteria));
    setSeq(node.params.criteria.length + 1);
    setTouched(false);
  }, [node]);

  // Compare normalised criteria (description, flags and the effective rule set) so rule-only edits count.
  const projection = (xs: { description: string; required: boolean; manual_required: boolean; spec: RuleSpec }[]) =>
    JSON.stringify(xs.map((c) => [c.description, c.required, c.manual_required, c.spec]));
  const dirty =
    title !== node.params.title ||
    objective !== node.params.objective ||
    projection(criteria) !== projection(toDrafts(node.params.criteria));
  const titleErr = !title.trim() ? t('validation.required', { field: t('collab.draft.nodeTitle') }) : null;
  const critErrs = criteria.map((c) => (!c.description.trim() ? t('validation.required', { field: t('tk.crit.description') }) : null));
  const noRequired = criteria.length > 0 && !criteria.some((c) => c.required);
  const invalid = !!titleErr || critErrs.some(Boolean) || !criteria.length || noRequired;

  const update = (key: number, patch: Partial<CriterionDraft>) => setCriteria((xs) => xs.map((c) => (c.key === key ? { ...c, ...patch } : c)));
  const save = async () => {
    setTouched(true);
    if (invalid) return;
    await onSave({
      title: title.trim(),
      objective: objective.trim(),
      criteria: criteria.map(({ key: _k, spec, description, required, manual_required }) => ({
        description: description.trim(),
        required,
        manual_required,
        allowed_types: spec.asset_types,
        rule_spec: spec,
      })),
    });
  };
  const conflict = isApiError(error) && error.code === 'version_conflict';

  return (
    <form
      noValidate
      className="cl-node-editor"
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy) void save();
      }}
    >
      <p className="faint small">{t('collab.draft.criteriaHint')}</p>
      <div className="form-grid">
        <Field label={t('collab.draft.nodeTitle')} error={touched ? titleErr : null}>
          <input className="input" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label={t('collab.draft.nodeId')}>
          <input className="input mono" value={node.id} readOnly />
        </Field>
      </div>
      <Field label={t('collab.draft.objective')} optional>
        <LimitedTextarea className="input" rows={3} limit="objective" value={objective} onChange={(e) => setObjective(e.target.value)} />
      </Field>
      <div className="row">
        <span className="tk-label grow">{t('collab.draft.criteria')}</span>
        <button
          type="button"
          className="btn sm"
          disabled={criteria.length >= 50}
          onClick={() => {
            setCriteria((xs) => [...xs, { key: seq, description: '', required: true, manual_required: false, allowed_types: [], rule_spec: {}, spec: normalizeSpec(null) }]);
            setSeq((s) => s + 1);
          }}
        >
          <Plus /> {t('collab.draft.addCriterion')}
        </button>
      </div>
      {criteria.length ? (
        <ol className="cl-crit-list">
          {criteria.map((c, i) => (
            <li key={c.key} className="cl-crit">
              <div className="cl-crit-head">
                <span className="mono faint">#{i + 1}</span>
                <span className="grow" />
                <label className="check">
                  <input type="checkbox" checked={c.required} onChange={(e) => update(c.key, { required: e.target.checked })} />
                  {t('tk.crit.requiredLabel')}
                </label>
                <label className="check">
                  <input type="checkbox" checked={c.manual_required} onChange={(e) => update(c.key, { manual_required: e.target.checked })} />
                  {t('tk.crit.manualLabel')}
                </label>
                <button type="button" className="btn ghost icon sm" aria-label={t('collab.draft.removeCriterion', { n: i + 1 })} onClick={() => setCriteria((xs) => xs.filter((x) => x.key !== c.key))}>
                  <Trash2 />
                </button>
              </div>
              <Field label={t('tk.crit.description')} error={touched ? critErrs[i] : null}>
                <LimitedTextarea className="input" rows={2} limit="description" value={c.description} onChange={(e) => update(c.key, { description: e.target.value })} />
              </Field>
              <Field label={t('tk.rule.assetTypes')}>
                <AssetTypeToggles value={c.spec.asset_types} onChange={(v) => update(c.key, { spec: { ...c.spec, asset_types: v }, allowed_types: v })} label={t('tk.rule.assetTypes')} />
              </Field>
              <details>
                <summary className="small muted">{t('collab.draft.rules')}</summary>
                <RuleSpecEditor value={c.spec} onChange={(spec) => update(c.key, { spec, allowed_types: spec.asset_types })} slots={[]} />
              </details>
            </li>
          ))}
        </ol>
      ) : (
        <p className="small err-text">{t('collab.draft.needsCriterion')}</p>
      )}
      {touched && noRequired && <span className="field-error">{t('collab.draft.needsRequired')}</span>}
      {error ? (
        <div className="stack-sm">
          <ErrorNotice error={error} overrides={CL_ERRORS} />
          {conflict && (
            <div>
              <button type="button" className="btn sm" onClick={onReload}>
                <Undo2 /> {t('tk.reloadLatest')}
              </button>
            </div>
          )}
        </div>
      ) : null}
      <div className="row">
        <button type="submit" className="btn primary" disabled={busy || !dirty}>
          <Save /> {busy ? t('common.saving') : t('collab.draft.saveNode')}
        </button>
        {dirty && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => {
              setTitle(node.params.title);
              setObjective(node.params.objective);
              setCriteria(toDrafts(node.params.criteria));
              setTouched(false);
            }}
          >
            {t('collab.draft.discard')}
          </button>
        )}
        {dirty && <span className="faint small">{t('collab.draft.unsavedHint')}</span>}
      </div>
    </form>
  );
}

// ─── column ────────────────────────────────────────────────────────────────

export function DraftGraph({
  draft,
  members,
  agents,
  selected,
  onSelect,
  nodeBusy,
  nodeError,
  onSaveNode,
  onReload,
  startBlock,
  startBusy,
  startError,
  onStart,
  onResolveMissing,
  missingBusy,
}: {
  draft: DraftOut | null;
  members: Members;
  agents: AgentProfile[] | undefined;
  selected: string | null;
  onSelect: (id: string | null) => void;
  nodeBusy: boolean;
  nodeError: unknown;
  onSaveNode: (nodeId: string, patch: Partial<BusinessTaskParams>) => Promise<void>;
  onReload: () => void;
  /** Page-level reason the draft cannot start now (permission, unsaved form edits); null = none. */
  startBlock: string | null;
  startBusy: boolean;
  startError: unknown;
  onStart: () => void;
  /** Mark one missing fact as supplied (PATCH missing_information); omitted = not offered. */
  onResolveMissing?: (index: number) => void;
  missingBusy?: boolean;
}) {
  const { t } = useI18n();
  const nodes = useMemo(() => orderedNodes(draft), [draft]);
  const errors = draft?.validation_errors ?? [];
  const missing: MissingInformation[] = draft?.missing_information ?? [];
  const byNode = useMemo(() => errorsByNode(errors), [errors]);
  const current = nodes.find((n) => n.id === selected) ?? null;
  const planning = draft?.planning ?? draft?.draft.planning;
  const started = draft?.run ?? null;

  let status: { tone: 'ok' | 'warn' | 'err' | 'pending'; text: string };
  if (!draft || !nodes.length) status = { tone: 'pending', text: t('collab.draft.status.empty') };
  else if (started) status = { tone: 'ok', text: t('collab.draft.status.started') };
  else if (missing.length) status = { tone: 'warn', text: t('collab.draft.status.clarify') };
  else if (errors.length || !draft.ready_to_start) status = { tone: 'err', text: t('collab.draft.status.invalid') };
  else status = { tone: 'ok', text: t('collab.draft.status.ready') };

  const cannot: string | null = !draft
    ? t('collab.draft.cannot.noGraph')
    : started
      ? t('collab.draft.cannot.started')
      : !nodes.length
        ? t('collab.draft.cannot.noGraph')
        : missing.length
          ? t('collab.draft.cannot.missing', { n: missing.length })
          : errors.length
            ? t('collab.draft.cannot.errors', { n: errors.length })
            : !draft.ready_to_start
              ? t('collab.draft.cannot.notReady')
              : startBlock;

  return (
    <div className="cl-draft-graph">
      <section className="panel" aria-labelledby="cl-draft-h">
        <div className="panel-h">
          <h2 id="cl-draft-h">{t('collab.draft.title')}</h2>
          <Chip tone={status.tone}>{status.text}</Chip>
          {planning?.simulated && <SimulatedChip />}
          <span className="grow" />
          {draft && (
            <span className="faint small">
              {t('collab.draft.version', { n: draft.draft.version })}
              {planning?.provider ? ` · ${t('collab.draft.plannedBy', { provider: planning.provider, model: planning.model_id ?? '' })}` : ''}
            </span>
          )}
        </div>
        <div className="panel-b stack">
          {!draft || !nodes.length ? (
            <EmptyState compact icon={<Sparkles className="big-ico" aria-hidden />} title={t('collab.draft.empty')} body={t('collab.draft.emptyBody')} />
          ) : (
            <>
              {(draft.draft.summary || planning?.summary) && <p className="muted small">{draft.draft.summary || planning?.summary}</p>}
              <ol className="cl-node-strip" aria-label={t('collab.draft.nodesLabel')}>
                {nodes.map((n, i) => (
                  <li key={n.id}>
                    <DraftNodeCard node={n} selected={n.id === selected} errors={byNode.get(n.id) ?? []} members={members} agents={agents} onSelect={() => onSelect(n.id === selected ? null : n.id)} />
                    {i < nodes.length - 1 && (
                      <span className="cl-arrow" aria-hidden>
                        <ArrowRight />
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      </section>

      {current && draft && (
        <section className="panel" aria-labelledby="cl-node-h">
          <div className="panel-h">
            <h2 id="cl-node-h">{t('collab.draft.criteriaOf', { title: current.params.title || current.id })}</h2>
          </div>
          <div className="panel-b">
            <NodeEditor key={`${current.id}:${draft.draft.version}`} node={current} busy={nodeBusy} error={nodeError} onSave={(patch) => onSaveNode(current.id, patch)} onReload={onReload} />
          </div>
        </section>
      )}

      {missing.length > 0 && (
        <div className="callout warn" role="alert">
          <HelpCircle aria-hidden />
          <div className="grow stack-sm">
            <b>{t('collab.draft.missing')}</b>
            <span className="small">{t('collab.draft.missingBody')}</span>
            <ul className="cl-missing">
              {missing.map((m, i) => (
                <li key={`${m.field}-${i}`}>
                  <span className="mono small">{m.field}</span> · {m.question}
                  {m.node_id && (
                    <>
                      {' '}
                      <button type="button" className="linkish small" onClick={() => onSelect(m.node_id)}>
                        {nodes.find((n) => n.id === m.node_id)?.params.title || m.node_id}
                      </button>
                    </>
                  )}
                  {onResolveMissing && (
                    <>
                      {' '}
                      <button type="button" className="btn ghost sm" disabled={missingBusy} title={t('collab.draft.resolveHint')} onClick={() => onResolveMissing(i)}>
                        <Check /> {t('collab.draft.resolve')}
                      </button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {errors.length > 0 && (
        <div className="callout err" role="alert">
          <AlertTriangle aria-hidden />
          <div className="grow stack-sm">
            <b>{t('collab.draft.errors')}</b>
            <ul className="cl-missing">
              {errors.map((e, i) => (
                <li key={`${e.code}-${i}`} title={e.message}>
                  {validationErrorText(t, e)} <code className="error-code">{e.code}</code>
                  {e.node_id && (
                    <>
                      {' '}
                      <button type="button" className="linkish small" onClick={() => onSelect(e.node_id ?? null)}>
                        {nodes.find((n) => n.id === e.node_id)?.params.title || e.node_id}
                      </button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <section className="panel">
        <div className="panel-b stack-sm">
          {startError ? <ErrorNotice error={startError} overrides={CL_ERRORS} /> : null}
          <div className="cl-start">
            {started ? (
              <Link className="btn primary lg" to={paths.collaborationRun(started.id)}>
                <ExternalLink /> {t('collab.draft.openRun')}
              </Link>
            ) : (
              <button type="button" className="btn primary lg" disabled={!!cannot || startBusy} onClick={onStart} aria-describedby="cl-start-why">
                <Play /> {startBusy ? t('collab.draft.starting') : t('collab.draft.start')}
              </button>
            )}
            <span id="cl-start-why" className={`small ${cannot ? 'warn-text' : 'faint'}`}>
              {cannot ?? t('collab.draft.startHint')}
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
