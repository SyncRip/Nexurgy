// Parameter widgets generated from a node's params_schema, with dedicated
// pickers for ids (file versions, revisions, model configs).

import { Plus, RefreshCw, X } from 'lucide-react';
import { useEffect, useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { getDrawing, listDrawings, listParts, listRevisions } from '../../../api/endpoints';
import type { DrawingSection, ModelConfig, UUID } from '../../../api/types';
import { SectionPicker } from '../../cad/SectionPicker';
import { annotationState, dwgInfo } from '../../cad/status';
import { paths } from '../../../app/nav';
import { KindBadge } from '../../../components/ui/Badges';
import { ErrorText } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { KNOWN_MATERIALS } from '../../../lib/catalog';
import { enumLabel } from '../labels';
import { rememberRevision, useRevision, type Lookups } from '../lookups';
import type { ParamSchema, ParamSpec } from '../model';

export interface FieldEnv {
  projectId: UUID;
  nodeType: string;
  readOnly: boolean;
  lookups: Lookups;
  connectedInputs: ReadonlySet<string>;
}

interface WidgetProps<T = unknown> {
  id: string;
  spec: ParamSpec;
  value: T;
  onChange: (v: unknown) => void;
  env: FieldEnv;
  describedBy?: string;
}

function numeric(s: ParamSchema | undefined): boolean {
  return s?.type === 'number' || s?.type === 'integer';
}

// ─── basic widgets ─────────────────────────────────────────────────────────

function TextAreaWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const max = spec.schema.maxLength;
  const v = typeof value === 'string' ? value : '';
  return (
    <div className="wf-textarea">
      <textarea
        id={id}
        className="input"
        rows={spec.name === 'text' ? 6 : 3}
        maxLength={max}
        value={v}
        readOnly={env.readOnly}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.value)}
      />
      {max ? <span className="wf-count mono faint">{v.length}/{max}</span> : null}
    </div>
  );
}

function TextWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const listId = useId();
  const isMaterial = spec.name === 'material';
  return (
    <>
      <input
        id={id}
        className="input"
        value={typeof value === 'string' ? value : ''}
        maxLength={spec.schema.maxLength}
        readOnly={env.readOnly}
        list={isMaterial ? listId : undefined}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.value === '' && spec.nullable ? null : e.target.value)}
      />
      {isMaterial && (
        <datalist id={listId}>
          {KNOWN_MATERIALS.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      )}
    </>
  );
}

function bounds(s: ParamSchema): { min?: number; max?: number; int: boolean } {
  return { min: s.minimum, max: s.maximum, int: s.type === 'integer' };
}

/** Number input that keeps the typed text while it is not a valid number yet. */
function NumberWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const { t } = useI18n();
  const b = bounds(spec.schema);
  const [text, setText] = useState(value === null || value === undefined ? '' : String(value));
  useEffect(() => {
    setText(value === null || value === undefined ? '' : String(value));
  }, [value]);
  const n = Number(text);
  const invalid = text.trim() === '' || Number.isNaN(n) || (b.int && !Number.isInteger(n)) || (b.min !== undefined && n < b.min) || (b.max !== undefined && n > b.max);
  return (
    <>
      <input
        id={id}
        className={`input mono ${invalid ? 'is-invalid' : ''}`}
        type="number"
        inputMode={b.int ? 'numeric' : 'decimal'}
        step={b.int ? 1 : 'any'}
        min={b.min}
        max={b.max}
        value={text}
        readOnly={env.readOnly}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        onChange={(e) => {
          setText(e.target.value);
          const v = Number(e.target.value);
          const ok = e.target.value.trim() !== '' && !Number.isNaN(v) && (!b.int || Number.isInteger(v)) && (b.min === undefined || v >= b.min) && (b.max === undefined || v <= b.max);
          if (ok) onChange(v);
        }}
      />
      {invalid && (
        <span className="field-error">
          {b.min !== undefined && b.max !== undefined ? t('wf.param.range', { min: b.min, max: b.max }) : t('wf.param.invalidNumber')}
        </span>
      )}
    </>
  );
}

function EnumWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const { t } = useI18n();
  const options = (spec.schema.enum ?? []).map(String);
  return (
    <select id={id} className="select" value={String(value ?? '')} disabled={env.readOnly} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o} value={o}>
          {enumLabel(t, spec.name, o)}
        </option>
      ))}
    </select>
  );
}

function MultiToggleWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const { t } = useI18n();
  const options = (spec.schema.items?.enum ?? []).map(String);
  const current = Array.isArray(value) ? value.map(String) : [];
  const toggle = (o: string) => {
    const next = current.includes(o) ? current.filter((x) => x !== o) : options.filter((x) => x === o || current.includes(x));
    onChange(next);
  };
  return (
    <div className="wf-toggles" role="group" id={id} aria-describedby={describedBy}>
      {options.map((o) => (
        <button key={o} type="button" className="wf-toggle" aria-pressed={current.includes(o)} disabled={env.readOnly} onClick={() => toggle(o)}>
          {enumLabel(t, spec.name, o)}
        </button>
      ))}
      {current.length === 0 && <span className="field-error">{t('wf.param.pickOne')}</span>}
    </div>
  );
}

/** Array of numbers as chips (e.g. quote quantities). */
function NumberChipsWidget({ id, spec, value, onChange, env, describedBy }: WidgetProps) {
  const { t, fmt } = useI18n();
  const items = Array.isArray(value) ? value.filter((v): v is number => typeof v === 'number') : [];
  const [draft, setDraft] = useState('');
  const int = spec.schema.items?.type === 'integer';
  const min = spec.schema.items?.minimum ?? (spec.name === 'quantities' ? 1 : undefined);
  const maxItems = spec.schema.maxItems ?? 20;
  const minItems = spec.schema.minItems ?? 0;
  const n = Number(draft);
  const draftOk = draft.trim() !== '' && !Number.isNaN(n) && (!int || Number.isInteger(n)) && (min === undefined || n >= min) && !items.includes(n);
  const add = () => {
    if (!draftOk || items.length >= maxItems) return;
    onChange([...items, n].sort((a, b) => a - b));
    setDraft('');
  };
  return (
    <div className="wf-chips" id={id} aria-describedby={describedBy}>
      {items.map((v) => (
        <span key={v} className="wf-chip mono">
          {fmt.number(v)}
          {!env.readOnly && items.length > minItems && (
            <button type="button" aria-label={t('wf.param.removeValue', { value: v })} onClick={() => onChange(items.filter((x) => x !== v))}>
              <X />
            </button>
          )}
        </span>
      ))}
      {!env.readOnly && items.length < maxItems && (
        <span className="wf-chip-add">
          <input
            className="input sm mono"
            type="number"
            inputMode="numeric"
            min={min}
            step={int ? 1 : 'any'}
            value={draft}
            placeholder={t('wf.param.addValue')}
            aria-label={t('wf.param.addValue')}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                add();
              }
            }}
          />
          <button type="button" className="btn sm icon" onClick={add} disabled={!draftOk} aria-label={t('common.add')}>
            <Plus />
          </button>
        </span>
      )}
    </div>
  );
}

function ScaleWidget({ id, value, onChange, env, describedBy }: WidgetProps) {
  const { t } = useI18n();
  const auto = value === 'auto' || value === undefined || value === null;
  return (
    <div className="wf-scale" id={id} aria-describedby={describedBy}>
      <div className="seg" role="group">
        <button type="button" className="seg-btn" aria-pressed={auto} disabled={env.readOnly} onClick={() => onChange('auto')}>
          {t('wf.enum.scale.auto')}
        </button>
        <button type="button" className="seg-btn" aria-pressed={!auto} disabled={env.readOnly} onClick={() => onChange(auto ? 1 : value)}>
          {t('wf.enum.scale.fixed')}
        </button>
      </div>
      {!auto && (
        <input
          className="input mono sm"
          type="number"
          step="any"
          min={0.01}
          value={String(value)}
          readOnly={env.readOnly}
          aria-label={t('wf.param.scale')}
          onChange={(e) => {
            const v = Number(e.target.value);
            if (e.target.value && !Number.isNaN(v) && v > 0) onChange(v);
          }}
        />
      )}
    </div>
  );
}

/** DrawingParams.section: automatic / no section / explicit plane XZ | YZ + offset (instead of a raw text field). */
function SectionWidget({ id, value, onChange, env, describedBy }: WidgetProps) {
  const v: DrawingSection =
    value === 'none' ? 'none' : value && typeof value === 'object' && ((value as { plane?: unknown }).plane === 'XZ' || (value as { plane?: unknown }).plane === 'YZ') ? (value as DrawingSection) : 'auto';
  return <SectionPicker id={id} value={v} onChange={onChange} disabled={env.readOnly} size="md" describedBy={describedBy} />;
}

// ─── id pickers ────────────────────────────────────────────────────────────

/** drawing_reconstruct.drawing_id: a drawing of a part in this project (used when no file / drawing input is connected). */
function DrawingPicker({ id, value, onChange, env, describedBy }: WidgetProps) {
  const { t, fmt } = useI18n();
  const current = useResource((signal) => getDrawing(value as string, { signal }), [value], { enabled: typeof value === 'string' && !!value });
  const [partId, setPartId] = useState<string>('');
  useEffect(() => {
    if (current.data && !partId) setPartId(current.data.part_id);
  }, [current.data, partId]);
  const parts = useResource((signal) => listParts(env.projectId, {}, { signal }), [env.projectId]);
  const drawings = useResource((signal) => listDrawings(partId, { signal }), [partId], { enabled: !!partId });
  return (
    <div className="stack-sm">
      <select className="select" value={partId} disabled={env.readOnly || !parts.data} aria-label={t('cad.wf.drawingPicker.part')} onChange={(e) => setPartId(e.target.value)}>
        <option value="">{parts.data ? t('wf.picker.choosePart') : t('common.loading')}</option>
        {(parts.data ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.number} · {p.name}
          </option>
        ))}
      </select>
      <select id={id} className="select" value={typeof value === 'string' ? value : ''} disabled={env.readOnly || !partId || !drawings.data} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{!partId ? t('wf.picker.partFirst') : drawings.data ? (drawings.data.length ? t('cad.wf.drawingPicker.choose') : t('cad.wf.drawingPicker.none')) : t('common.loading')}</option>
        {typeof value === 'string' && value && drawings.data && !drawings.data.some((d) => d.id === value) && <option value={value}>{t('cad.wf.drawingPicker.unknown')}</option>}
        {(drawings.data ?? []).map((d) => {
          const ann = annotationState(d);
          const dwg = dwgInfo(d);
          return (
            <option key={d.id} value={d.id}>
              {d.rev_no ? `R${d.rev_no}` : '—'} · {t('cad.dw.pagesN', { n: d.page_count ?? (d.pages?.length || 1) })} · {t(`cad.ann.${ann}`)} · {dwg.state === 'passed' ? t('cad.dwg.passed', { release: dwg.release ?? 'R2000' }) : t(`cad.dwg.${dwg.state}`)} · {fmt.date(d.created_at)}
            </option>
          );
        })}
      </select>
      {typeof value === 'string' && value && !env.readOnly && (
        <button type="button" className="btn sm ghost" onClick={() => onChange(null)}>
          <X aria-hidden /> {t('common.remove')}
        </button>
      )}
      {parts.error ? <ErrorText error={parts.error} /> : null}
      {drawings.error ? <ErrorText error={drawings.error} /> : null}
    </div>
  );
}

function FileVersionPicker({ id, value, onChange, env, describedBy }: WidgetProps) {
  const { t, fmt } = useI18n();
  const { files, filesError, reloadFiles } = env.lookups;
  const current = env.lookups.fileVersion(value);
  const live = (files ?? []).filter((f) => !f.archived_at && f.versions.length);
  return (
    <div className="stack-sm">
      <div className="row nowrap-row">
        <select id={id} className="select grow" value={typeof value === 'string' ? value : ''} disabled={env.readOnly || !files} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value || null)}>
          <option value="">{files ? t('wf.picker.chooseFile') : t('common.loading')}</option>
          {typeof value === 'string' && value && !current && files && <option value={value}>{t('wf.picker.unknownFile')}</option>}
          {live.map((f) => (
            <optgroup key={f.id} label={`${f.name} · ${t(`fileKind.${f.kind}`)}`}>
              {f.versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version} · {v.original_name} · {fmt.date(v.created_at)}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <button type="button" className="btn icon sm" onClick={reloadFiles} aria-label={t('wf.picker.reload')} title={t('wf.picker.reload')}>
          <RefreshCw />
        </button>
      </div>
      {filesError ? <ErrorText error={filesError} /> : null}
      {current && (
        <div className="row small">
          <KindBadge kind={current.file.kind} />
          <span className="faint">{fmt.bytes(current.version.size)}</span>
        </div>
      )}
      {files && !live.length && (
        <span className="field-hint">
          {t('wf.picker.noFiles')}{' '}
          <Link to={paths.project(env.projectId, 'files')} className="linkish">
            {t('wf.picker.openFiles')}
          </Link>
        </span>
      )}
    </div>
  );
}

function RevisionPicker({ id, value, onChange, env, describedBy }: WidgetProps) {
  const { t, fmt } = useI18n();
  const selected = useRevision(value);
  const [partId, setPartId] = useState<string>('');
  useEffect(() => {
    if (selected && !partId) setPartId(selected.part_id);
  }, [selected, partId]);
  const parts = useResource((signal) => listParts(env.projectId, {}, { signal }), [env.projectId]);
  const revs = useResource((signal) => listRevisions(partId, { signal }), [partId], { enabled: !!partId });
  useEffect(() => {
    revs.data?.forEach(rememberRevision);
  }, [revs.data]);
  const partLabel = useMemo(() => new Map((parts.data ?? []).map((p) => [p.id, `${p.number} · ${p.name}`])), [parts.data]);
  return (
    <div className="stack-sm">
      <select className="select" value={partId} disabled={env.readOnly || !parts.data} aria-label={t('wf.picker.part')} onChange={(e) => setPartId(e.target.value)}>
        <option value="">{parts.data ? t('wf.picker.choosePart') : t('common.loading')}</option>
        {(parts.data ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.number} · {p.name}
          </option>
        ))}
      </select>
      <select id={id} className="select" value={typeof value === 'string' ? value : ''} disabled={env.readOnly || !partId || !revs.data} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{!partId ? t('wf.picker.partFirst') : revs.data ? (revs.data.length ? t('wf.picker.chooseRevision') : t('wf.picker.noRevisions')) : t('common.loading')}</option>
        {typeof value === 'string' && value && revs.data && !revs.data.some((r) => r.id === value) && <option value={value}>{selected ? t('wf.revisionLabel', { n: selected.rev_no }) : t('wf.picker.unknownRevision')}</option>}
        {(revs.data ?? []).map((r) => (
          <option key={r.id} value={r.id} disabled={r.status !== 'ready'}>
            {t('wf.revisionLabel', { n: r.rev_no })} · {t(`revSource.${r.source}`)}
            {r.mesh_only ? ` · ${t('rev.meshOnly')}` : ''}
            {r.status !== 'ready' ? ` · ${t('rev.failed')}` : ''} · {fmt.date(r.created_at)}
          </option>
        ))}
      </select>
      {parts.error ? <ErrorText error={parts.error} /> : null}
      {revs.error ? <ErrorText error={revs.error} /> : null}
      {selected && <span className="field-hint">{t('wf.picker.revisionPartHint', { part: partLabel.get(selected.part_id) ?? '' })}</span>}
    </div>
  );
}

function configStatus(c: ModelConfig): 'ready' | 'disabled' | 'key_missing' | 'issues' {
  if (!c.enabled) return 'disabled';
  if (!c.key_present) return 'key_missing';
  return c.ready ? 'ready' : 'issues';
}

function ModelConfigPicker({ id, value, onChange, env, describedBy }: WidgetProps) {
  const { t } = useI18n();
  const configs = env.lookups.modelConfigs;
  const chosen = env.lookups.modelConfig(value);
  const def = (configs ?? []).find((c) => c.is_default && c.enabled);
  const effective = chosen ?? def;
  const needsVision = env.nodeType === 'drawing_intake';
  return (
    <div className="stack-sm">
      <select id={id} className="select" value={typeof value === 'string' ? value : ''} disabled={env.readOnly || !configs} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{def ? t('wf.picker.defaultModelNamed', { label: def.label }) : t('wf.param.defaultModel')}</option>
        {(configs ?? []).map((c) => (
          <option key={c.id} value={c.id} disabled={!c.enabled}>
            {c.label} — {c.model_id} · {t(`wf.model.${configStatus(c)}`)}
          </option>
        ))}
      </select>
      {env.lookups.modelConfigsError ? <ErrorText error={env.lookups.modelConfigsError} /> : null}
      {effective && (
        <div className="wf-model-state">
          <span className={`chip ${configStatus(effective) === 'ready' ? 'ok' : 'warn'}`}>{t(`wf.model.${configStatus(effective)}`)}</span>
          <span className="faint small mono ellip">
            {t(`provider.${effective.provider}`)} · {effective.model_id}
          </span>
        </div>
      )}
      {effective && configStatus(effective) !== 'ready' && <span className="field-hint">{effective.issues.join(' · ') || t('wf.model.notReadyHint')}</span>}
      {effective && needsVision && effective.capabilities.vision === false && <span className="field-hint">{t('wf.model.noVision')}</span>}
      {configs && !configs.length && <span className="field-hint">{t('wf.model.none')}</span>}
      <span className="field-hint">{needsVision ? t('wf.model.intakeHint') : t('wf.model.hint')}</span>
    </div>
  );
}

// ─── dispatcher ────────────────────────────────────────────────────────────

export function ParamWidget(props: WidgetProps) {
  const { spec } = props;
  const s = spec.schema;
  switch (spec.name) {
    case 'text':
    case 'hints':
    case 'instructions':
      return <TextAreaWidget {...props} />;
    case 'file_version_id':
      return <FileVersionPicker {...props} />;
    case 'revision_id':
      return <RevisionPicker {...props} />;
    case 'model_config_id':
      return <ModelConfigPicker {...props} />;
    case 'scale':
      return <ScaleWidget {...props} />;
    case 'section':
      return <SectionWidget {...props} />;
    case 'drawing_id':
      return <DrawingPicker {...props} />;
    default:
      break;
  }
  if (s.enum) return <EnumWidget {...props} />;
  if (s.type === 'array' && s.items?.enum) return <MultiToggleWidget {...props} />;
  if (s.type === 'array' && numeric(s.items)) return <NumberChipsWidget {...props} />;
  if (numeric(s)) return <NumberWidget {...props} />;
  if (s.type === 'string' && (s.maxLength ?? 0) > 200) return <TextAreaWidget {...props} />;
  return <TextWidget {...props} />;
}
