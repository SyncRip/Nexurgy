// Right-hand inspector of the canvas: node parameters, group settings,
// multi-selection actions, or the workflow overview (description, issues, keys).

import { Group, Info, Keyboard, Sparkles, Trash, TriangleAlert, Ungroup } from 'lucide-react';
import { useId } from 'react';
import type { GraphError, WorkflowGroup, WorkflowNodeType } from '../../../api/types';
import type { MessageKey } from '../../../i18n/en';
import { useI18n } from '../../../i18n/I18nProvider';
import { NodeTypeIcon } from '../icons';
import { categoryLabel, graphErrorText, groupDisplayLabel, nodeDisplayName, nodeTypeDoc, nodeTypeLabel, paramLabel, portLabel, portTypeLabel } from '../labels';
import { GROUP_COLORS, effectiveParam, groupColor, paramSpecs } from '../model';
import { ParamWidget, type FieldEnv } from './ParamFields';

function paramHint(t: ReturnType<typeof useI18n>['t'], nodeType: string, name: string, connected: ReadonlySet<string>): string | null {
  if (nodeType === 'model_3d' && name === 'text') return connected.has('text') ? t('wf.hint.model3dTextConnected') : t('wf.hint.model3dText');
  if (nodeType === 'text_input' && name === 'text') return t('wf.hint.textInput');
  if (nodeType === 'human_review' && name === 'stage') return t('wf.hint.stage');
  if (nodeType === 'drawing_intake' && name === 'material') return t('wf.hint.intakeMaterial');
  if (nodeType === 'drawing_reconstruct' && name === 'sheet') return t('wf.hint.reconstructSheet');
  if (nodeType === 'drawing_reconstruct' && name === 'notes') return t('wf.hint.reconstructNotes');
  if (nodeType === 'drawing_reconstruct' && name === 'drawing_id') return t('cad.wf.drawingPicker.hint');
  if (nodeType === 'drawing' && name === 'section') return t('cad.dw.section.hint');
  if (nodeType === 'process_plan' && name === 'allowance_mm') return t('wf.hint.allowance');
  return null;
}

export function NodeInspector({
  nodeId,
  def,
  params,
  label,
  errors,
  env,
  onParam,
  onLabel,
  onDelete,
}: {
  nodeId: string;
  def: WorkflowNodeType | undefined;
  params: Record<string, unknown>;
  label: string | null;
  errors: GraphError[];
  env: FieldEnv;
  onParam: (name: string, value: unknown) => void;
  onLabel: (label: string) => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const labelId = useId();
  const baseId = useId();
  const type = env.nodeType;
  return (
    <div className="wf-insp-section stack">
      <div className="wf-insp-head">
        <span className={`wf-insp-ico cat-${def?.category ?? 'unknown'}`}>
          <NodeTypeIcon type={type} />
        </span>
        <div className="wf-insp-titles">
          <h2 className="ellip">{nodeDisplayName(t, { type, label })}</h2>
          <span className="faint small">
            {nodeTypeLabel(t, type)} · {def ? categoryLabel(t, def.category) : type} · <span className="mono">{nodeId}</span>
          </span>
        </div>
      </div>
      {def?.uses_ai && (
        <div className="callout info slim">
          <Sparkles aria-hidden />
          <span>{t('wf.ai.inspectorNote', { n: def.max_attempts })}</span>
        </div>
      )}
      {def && <p className="muted small">{nodeTypeDoc(t, type, def.doc)}</p>}
      {errors.length > 0 && (
        <ul className="wf-node-errors" aria-label={t('wf.issues.title')}>
          {errors.map((e, i) => (
            <li key={i} title={e.message}>
              <TriangleAlert aria-hidden /> {graphErrorText(t, e, type)}
            </li>
          ))}
        </ul>
      )}
      <div className="field">
        <label htmlFor={labelId}>
          {t('wf.node.labelField')} <span className="opt">· {t('common.optional')}</span>
        </label>
        <input id={labelId} className="input" value={label ?? ''} maxLength={120} placeholder={nodeTypeLabel(t, type)} readOnly={env.readOnly} onChange={(e) => onLabel(e.target.value)} />
      </div>
      {def && (
        <div className="wf-insp-ports">
          {def.inputs.length > 0 && (
            <div>
              <span className="wf-insp-label">{t('wf.ports.inputs')}</span>
              <ul>
                {def.inputs.map((p) => (
                  <li key={p.name}>
                    <span className={`wf-port-dot pt-${p.type}`} aria-hidden />
                    {portLabel(t, p.name)} <span className="faint">· {portTypeLabel(t, p.type)}</span>
                    {!p.required && <span className="faint"> · {t('common.optional')}</span>}
                    {env.connectedInputs.has(p.name) && <span className="chip ok wf-mini-chip">{t('wf.ports.connected')}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <span className="wf-insp-label">{t('wf.ports.outputs')}</span>
            <ul>
              {def.outputs.map((p) => (
                <li key={p.name}>
                  <span className={`wf-port-dot pt-${p.type}`} aria-hidden />
                  {portLabel(t, p.name)} <span className="faint">· {portTypeLabel(t, p.type)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {def && paramSpecs(def).length > 0 && (
        <div className="stack-sm">
          <span className="wf-insp-label">{t('wf.params.title')}</span>
          {paramSpecs(def).map((spec) => {
            const fid = `${baseId}-${spec.name}`;
            const hint = paramHint(t, type, spec.name, env.connectedInputs);
            const hintId = hint ? `${fid}-hint` : undefined;
            const paramErrors = errors.filter((e) => e.param === spec.name || e.param?.startsWith(`${spec.name}.`));
            return (
              <div key={spec.name} className={`field ${paramErrors.length ? 'has-error' : ''}`}>
                <label htmlFor={fid}>{paramLabel(t, spec.name, spec.schema.title)}</label>
                <ParamWidget id={fid} spec={spec} value={effectiveParam(spec, params)} onChange={(v) => onParam(spec.name, v)} env={env} describedBy={hintId} />
                {hint && (
                  <span className="field-hint" id={hintId}>
                    {hint}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
      {!env.readOnly && (
        <button type="button" className="btn danger sm wf-insp-delete" onClick={onDelete}>
          <Trash /> {t('wf.node.delete')}
        </button>
      )}
    </div>
  );
}

export function GroupInspector({
  group,
  readOnly,
  memberNames,
  selectedCount,
  onChange,
  onAddSelected,
  onRemoveMember,
  onUngroup,
}: {
  group: WorkflowGroup;
  readOnly: boolean;
  memberNames: { id: string; name: string }[];
  selectedCount: number;
  onChange: (patch: Partial<WorkflowGroup>) => void;
  onAddSelected: () => void;
  onRemoveMember: (id: string) => void;
  onUngroup: () => void;
}) {
  const { t } = useI18n();
  const labelId = useId();
  const current = groupColor(group.color);
  return (
    <div className="wf-insp-section stack">
      <div className="wf-insp-head">
        <span className={`wf-insp-ico gc-${current}`}>
          <Group aria-hidden />
        </span>
        <div className="wf-insp-titles">
          <h2 className="ellip">{group.label ? groupDisplayLabel(t, group.label) : t('wf.group.untitled')}</h2>
          <span className="faint small">{t('wf.group.kind')}</span>
        </div>
      </div>
      <div className="field">
        <label htmlFor={labelId}>{t('wf.group.label')}</label>
        <input id={labelId} className="input" value={group.label} maxLength={80} readOnly={readOnly} onChange={(e) => onChange({ label: e.target.value })} />
      </div>
      <div className="field">
        <span className="wf-insp-label">{t('wf.group.color')}</span>
        <div className="wf-swatches" role="radiogroup" aria-label={t('wf.group.color')}>
          {GROUP_COLORS.map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={current === c}
              aria-label={t(`wf.color.${c}`)}
              title={t(`wf.color.${c}`)}
              className={`wf-swatch gc-${c}`}
              disabled={readOnly}
              onClick={() => onChange({ color: c })}
            />
          ))}
        </div>
      </div>
      <div className="stack-sm">
        <span className="wf-insp-label">{t('wf.group.members', { n: memberNames.length })}</span>
        <ul className="wf-member-list">
          {memberNames.map((m) => (
            <li key={m.id}>
              <span className="ellip">{m.name}</span>
              <span className="mono faint small">{m.id}</span>
              {!readOnly && (
                <button type="button" className="btn ghost sm" onClick={() => onRemoveMember(m.id)}>
                  {t('common.remove')}
                </button>
              )}
            </li>
          ))}
        </ul>
        {!readOnly && selectedCount > 0 && (
          <button type="button" className="btn sm" onClick={onAddSelected}>
            <Group /> {t('wf.group.addSelected', { n: selectedCount })}
          </button>
        )}
      </div>
      {!readOnly && (
        <button type="button" className="btn danger sm" onClick={onUngroup}>
          <Ungroup /> {t('wf.group.ungroup')}
        </button>
      )}
    </div>
  );
}

export function SelectionInspector({ count, readOnly, onGroup, onDelete }: { count: number; readOnly: boolean; onGroup: () => void; onDelete: () => void }) {
  const { t } = useI18n();
  return (
    <div className="wf-insp-section stack">
      <h2>{t('wf.selection.count', { n: count })}</h2>
      <p className="muted small">{t('wf.selection.body')}</p>
      {!readOnly && (
        <div className="row">
          <button type="button" className="btn sm" onClick={onGroup}>
            <Group /> {t('wf.group.create')}
          </button>
          <button type="button" className="btn danger sm" onClick={onDelete}>
            <Trash /> {t('wf.selection.delete')}
          </button>
        </div>
      )}
    </div>
  );
}

export function ShortcutHelp() {
  const { t } = useI18n();
  const rows: [MessageKey, MessageKey][] = [
    ['wf.keys.add', 'wf.keys.addKeys'],
    ['wf.keys.connect', 'wf.keys.connectKeys'],
    ['wf.keys.select', 'wf.keys.selectKeys'],
    ['wf.keys.delete', 'wf.keys.deleteKeys'],
    ['wf.keys.group', 'wf.keys.groupKeys'],
    ['wf.keys.save', 'wf.keys.saveKeys'],
    ['wf.keys.pan', 'wf.keys.panKeys'],
  ];
  return (
    <details className="wf-keys">
      <summary>
        <Keyboard aria-hidden /> {t('wf.keys.title')}
      </summary>
      <dl>
        {rows.map(([a, b]) => (
          <div key={a}>
            <dt>{t(a)}</dt>
            <dd className="mono small">{t(b)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function ReadOnlyNote({ reason }: { reason: 'archived' | 'role' }) {
  const { t } = useI18n();
  return (
    <div className="callout warn slim">
      <Info aria-hidden />
      <span>{reason === 'archived' ? t('wf.readonly.archived') : t('wf.readonly.role')}</span>
    </div>
  );
}
