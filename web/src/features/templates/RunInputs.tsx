// Run-time inputs and parameter overrides of a template version: one block per
// input node (asset version picker filtered by asset type, or text), then the
// overridable parameters of the other nodes. Review stages are fixed by the
// template and shown locked. Used by StartRunDialog and RerunDialog.

import { ChevronDown,Lock,RotateCcw,ShieldCheck,TriangleAlert } from 'lucide-react';
import { useId,useState,type ReactNode } from 'react';
import type { AssetType,AssetVersion } from '../../api/pm';
import type { TemplateInputRules,TemplateInputSpec,UUID,WorkflowGraph } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetVersionPicker } from '../tasks/AssetVersionPicker';
import { assetTypeLabel,VersionRef } from '../tasks/common';
import { ParamWidget } from '../workflow/editor/ParamFields';
import { nodeDisplayName,nodeTypeLabel,paramLabel,stageLabel } from '../workflow/labels';
import { useRevision,type Lookups } from '../workflow/lookups';
import { effectiveParam,paramSpecs,type DefMap } from '../workflow/model';
import { INPUT_PARAM,inputAssetType,qualityProblems,qualityText } from './common';

export interface InputValue {
  /** Chosen asset version (model / file inputs). null = keep the default. */
  version?: AssetVersion | null;
  /** Text inputs. */
  text?: string;
}
export type InputValues = Record<string, InputValue>;
export type ParamOverrides = Record<string, Record<string, unknown>>;

/** Value a node parameter defaults to when nothing is chosen for this run. */
function defaultOf(spec: TemplateInputSpec, defaults: Record<string, Record<string, unknown>> | undefined): unknown {
  const param = INPUT_PARAM[spec.node_type];
  const d = defaults?.[spec.node_id] ?? spec.default_params ?? {};
  return param ? d[param] : undefined;
}

/** Is every required input satisfied? Returns the node ids that are still missing. */
export function missingInputs(inputs: TemplateInputSpec[], values: InputValues, defaults?: Record<string, Record<string, unknown>>): string[] {
  const out: string[] = [];
  for (const s of inputs) {
    if (!s.required) continue;
    const v = values[s.node_id];
    if (s.node_type === 'text_input') {
      const text = v?.text ?? (typeof defaultOf(s, defaults) === 'string' ? (defaultOf(s, defaults) as string) : '');
      if (!text.trim()) out.push(s.node_id);
    } else if (!v?.version && !defaultOf(s, defaults)) {
      out.push(s.node_id);
    }
  }
  return out;
}

/** Per-node params for the run request: chosen versions, edited texts, then overrides. */
export function buildRunParams(inputs: TemplateInputSpec[], values: InputValues, overrides: ParamOverrides, defaults?: Record<string, Record<string, unknown>>): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const [nodeId, p] of Object.entries(overrides)) {
    const ordinary = Object.fromEntries(Object.entries(p).filter(([key]) => key !== 'model_config_id' && key !== 'execution_policy'));
    if (Object.keys(ordinary).length) out[nodeId] = ordinary;
  }
  for (const s of inputs) {
    const v = values[s.node_id];
    const param = INPUT_PARAM[s.node_type];
    if (!v || !param) continue;
    if (s.node_type === 'text_input') {
      const d = defaultOf(s, defaults);
      if (v.text !== undefined && v.text !== (typeof d === 'string' ? d : '')) out[s.node_id] = { ...(out[s.node_id] ?? {}), text: v.text };
    } else if (v.version) {
      out[s.node_id] = { ...(out[s.node_id] ?? {}), [param]: v.version.target.id };
    }
  }
  return out;
}

/** What a model / file input's default value points to (revision or file version). */
function DefaultValueLabel({ nodeType, value, lookups }: { nodeType: string; value: unknown; lookups: Lookups }) {
  const { t } = useI18n();
  const rev = useRevision(nodeType === 'model_input' ? value : undefined);
  if (typeof value !== 'string' || !value) return <span className="faint">{t('tpl.input.noDefault')}</span>;
  if (nodeType === 'model_input') return <span className="mono">{rev ? `R${rev.rev_no}` : value.slice(0, 8)}</span>;
  const fv = lookups.fileVersion(value);
  return <span>{fv ? `${fv.file.name} · v${fv.version.version}` : <span className="mono">{value.slice(0, 8)}</span>}</span>;
}

function InputBlock({
  projectId,
  spec,
  graph,
  value,
  defaults,
  rules,
  exploratory,
  lookups,
  missing,
  onChange,
}: {
  projectId: UUID;
  spec: TemplateInputSpec;
  graph: WorkflowGraph;
  value: InputValue | undefined;
  defaults?: Record<string, Record<string, unknown>>;
  rules: TemplateInputRules;
  exploratory: boolean;
  lookups: Lookups;
  missing: boolean;
  onChange: (v: InputValue) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  const node = graph.nodes.find((n) => n.id === spec.node_id);
  const name = node ? nodeDisplayName(t, node) : spec.node_id;
  const assetType = inputAssetType(spec);
  const def = defaultOf(spec, defaults);
  const [picking, setPicking] = useState(false);
  const stage = spec.require_approved_stage ?? rules.require_approved_stage;
  const problems = value?.version ? qualityProblems(value.version, rules, spec.require_approved_stage, exploratory) : [];

  let body: ReactNode;
  if (spec.node_type === 'text_input') {
    const text = value?.text ?? (typeof def === 'string' ? def : '');
    body = (
      <LimitedTextarea
        id={id}
        className="input"
        rows={3}
        limit="chat"
        value={text}
        aria-invalid={missing || undefined}
        onChange={(e) => onChange({ ...value, text: e.target.value })}
      />
    );
  } else {
    body = (
      <div className="stack-sm">
        <div className="tpl-input-current">
          {value?.version ? (
            <VersionRef v={value.version} showEngineering />
          ) : (
            <span className="small">
              <span className="faint">{t('tpl.input.default')}: </span>
              <DefaultValueLabel nodeType={spec.node_type} value={def} lookups={lookups} />
            </span>
          )}
          <span className="grow" />
          {value?.version && (
            <button type="button" className="btn ghost sm" onClick={() => onChange({ ...value, version: null })}>
              <RotateCcw /> {def ? t('tpl.input.useDefault') : t('tpl.input.clear')}
            </button>
          )}
          <button type="button" className="btn sm" aria-expanded={picking} aria-controls={`${id}-picker`} onClick={() => setPicking((x) => !x)}>
            <ChevronDown className={picking ? 'tpl-rot' : undefined} /> {picking ? t('tpl.input.hidePicker') : t('tpl.input.choose')}
          </button>
        </div>
        {picking && (
          <div id={`${id}-picker`}>
            <AssetVersionPicker
              projectId={projectId}
              types={assetType ? [assetType as AssetType] : undefined}
              value={value?.version ?? null}
              onChange={(v) => {
                onChange({ ...value, version: v });
                if (v) setPicking(false);
              }}
            />
          </div>
        )}
        {problems.length > 0 && (
          <ul className="tpl-issues warn" aria-label={t('tpl.input.qualityTitle')}>
            {problems.map((p) => (
              <li key={p}>
                <TriangleAlert aria-hidden />
                <span>{qualityText(t, p, p, stage)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    <fieldset className={`tpl-input ${missing ? 'is-missing' : ''}`}>
      <legend className="tpl-input-head">
        <label htmlFor={spec.node_type === 'text_input' ? id : undefined} className="strong">
          {name}
        </label>
        <span className="faint small">
          {assetType ? assetTypeLabel(t, assetType) : nodeTypeLabel(t, spec.node_type)} · <span className="mono">{spec.node_id}</span>
        </span>
        {spec.required ? <span className="tpl-req small">{t('tpl.input.required')}</span> : <span className="faint small">{t('common.optional')}</span>}
        {stage && spec.node_type !== 'text_input' && (
          <Chip tone="measure" icon={<ShieldCheck aria-hidden />} title={t('tpl.input.needsApprovalHint')}>
            {t('tpl.input.needsApproval', { stage: stageLabel(t, stage) })}
          </Chip>
        )}
      </legend>
      {body}
      {missing && <span className="field-error">{t(spec.node_type === 'text_input' ? 'tpl.input.missingText' : 'tpl.input.missing')}</span>}
    </fieldset>
  );
}

export function RunInputsForm({
  projectId,
  inputs,
  graph,
  values,
  onChange,
  defaults,
  rules,
  exploratory,
  lookups,
  showMissing,
}: {
  projectId: UUID;
  inputs: TemplateInputSpec[];
  graph: WorkflowGraph;
  values: InputValues;
  onChange: (nodeId: string, v: InputValue) => void;
  /** Override default params per input node (rerun: the original run's frozen params). */
  defaults?: Record<string, Record<string, unknown>>;
  rules: TemplateInputRules;
  exploratory: boolean;
  lookups: Lookups;
  showMissing: boolean;
}) {
  const { t } = useI18n();
  if (!inputs.length) return <p className="faint small">{t('tpl.input.none')}</p>;
  const missing = new Set(showMissing ? missingInputs(inputs, values, defaults) : []);
  return (
    <div className="stack-sm">
      {inputs.map((s) => (
        <InputBlock
          key={s.node_id}
          projectId={projectId}
          spec={s}
          graph={graph}
          value={values[s.node_id]}
          defaults={defaults}
          rules={rules}
          exploratory={exploratory}
          lookups={lookups}
          missing={missing.has(s.node_id)}
          onChange={(v) => onChange(s.node_id, v)}
        />
      ))}
    </div>
  );
}

/** Review gates of a graph (human_review stages), always fixed by the template. */
export function GateList({ graph }: { graph: WorkflowGraph }) {
  const { t } = useI18n();
  const gates = graph.nodes.filter((n) => n.type === 'human_review');
  if (!gates.length) return null;
  return (
    <p className="tpl-gates small">
      <Lock aria-hidden className="inline-ico" /> {t('tpl.params.gates')}{' '}
      {gates.map((n, i) => (
        <span key={n.id}>
          {i > 0 ? ', ' : ''}
          <span className="strong">{nodeDisplayName(t, n)}</span> ({stageLabel(t, String(n.params?.stage ?? 'design'))})
        </span>
      ))}
    </p>
  );
}

/** Overridable parameters of non-input nodes, collapsed by default. */
export function ParamOverridesForm({
  projectId,
  graph,
  overridable,
  defs,
  lookups,
  overrides,
  onChange,
}: {
  projectId: UUID;
  graph: WorkflowGraph;
  overridable: Record<string, string[]>;
  defs: DefMap;
  lookups: Lookups;
  overrides: ParamOverrides;
  onChange: (next: ParamOverrides) => void;
}) {
  const { t } = useI18n();
  // Input nodes are filled above; a review's stage is never in `overridable` (locked by the template).
  const nodes = graph.nodes.filter((n) => (overridable[n.id] ?? []).some((p) => p !== 'model_config_id' && !(n.type === 'human_review' && p === 'stage')) && !INPUT_PARAM[n.type]);
  const changed = Object.values(overrides).reduce((a, o) => a + Object.keys(o).length, 0);
  if (!nodes.length) return <GateList graph={graph} />;
  return (
    <details className="tpl-params">
      <summary>
        <span className="strong">{t('tpl.params.title')}</span>
        <span className="faint small"> · {changed ? t('tpl.params.changed', { n: changed }) : t('tpl.params.none')}</span>
      </summary>
      <div className="stack-sm">
        <p className="faint small">{t('tpl.params.hint')}</p>
        <GateList graph={graph} />
        {nodes.map((n) => {
          const def = defs.get(n.type);
          const specs = def ? paramSpecs(def).filter((s) => overridable[n.id].includes(s.name) && s.name !== 'model_config_id' && !(n.type === 'human_review' && s.name === 'stage')) : [];
          if (!specs.length) return null;
          const own = overrides[n.id] ?? {};
          return (
            <fieldset key={n.id} className="tpl-param-node">
              <legend className="tpl-input-head">
                <span className="strong">{nodeDisplayName(t, n)}</span>
                <span className="faint small mono">{n.id}</span>
                {Object.keys(own).length > 0 && (
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() => {
                      const next = { ...overrides };
                      delete next[n.id];
                      onChange(next);
                    }}
                  >
                    <RotateCcw /> {t('tpl.params.reset')}
                  </button>
                )}
              </legend>
              {specs.map((spec) => {
                const fieldId = `tpl-p-${n.id}-${spec.name}`;
                const current = Object.prototype.hasOwnProperty.call(own, spec.name) ? own[spec.name] : effectiveParam(spec, n.params ?? {});
                return (
                  <div className="field" key={spec.name}>
                    <label htmlFor={fieldId}>
                      {paramLabel(t, spec.name, spec.schema.title)}
                      {Object.prototype.hasOwnProperty.call(own, spec.name) && <span className="tpl-req small"> · {t('tpl.params.overridden')}</span>}
                    </label>
                    <ParamWidget
                      id={fieldId}
                      spec={spec}
                      value={current}
                      env={{ projectId, nodeType: n.type, readOnly: false, lookups, connectedInputs: new Set(graph.edges.filter((e) => e.target === n.id).map((e) => e.target_port)) }}
                      onChange={(v) => onChange({ ...overrides, [n.id]: { ...own, [spec.name]: v } })}
                    />
                  </div>
                );
              })}
            </fieldset>
          );
        })}
      </div>
    </details>
  );
}
