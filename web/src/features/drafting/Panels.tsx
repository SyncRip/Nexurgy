// Side panels of the drafting workspace: properties, layers, provenance (sources, readings, conflicts,
// assumptions, unsupported objects, calibration), sheet (title block / BOM), history and checks.

import { Eye, EyeOff, Plus, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  BOM_COLUMNS,
  TITLE_FIELDS,
  type DimensionEntity,
  type DraftCommand,
  type DraftContent,
  type DraftEntity,
  type DraftLayer,
  type DraftSheet,
  type Pt,
  type VersionBrief,
  type VersionFull,
} from '../../api/drafting';
import { CheckStatusChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import { useI18n } from '../../i18n/I18nProvider';
import { OriginTag, ScaleChip, VersionChips } from './chips';
import { blockFromSelection } from './commands';
import type { Tool } from './DraftingCanvas';
import { fmtNum, measure, selectionCenter } from './geometry';
import { scaleText } from './sheet';

export interface PanelCtx {
  content: DraftContent;
  version: VersionFull;
  canEdit: boolean;
  selection: string[];
  setSelection: (ids: string[]) => void;
  push: (cmds: DraftCommand[]) => void;
  activeLayer: string;
  setActiveLayer: (name: string) => void;
  activeBlock: string | null;
  setActiveBlock: (name: string | null) => void;
  setTool: (t: Tool) => void;
}

function Section({ title, children, extra }: { title: ReactNode; children: ReactNode; extra?: ReactNode }) {
  return (
    <section className="dr-sec">
      <header className="dr-sec-h">
        <h3>{title}</h3>
        {extra}
      </header>
      {children}
    </section>
  );
}

function NumberField({ label, value, onCommit, disabled, step = 'any' }: { label: string; value: number; onCommit: (v: number) => void; disabled?: boolean; step?: string }) {
  const [draft, setDraft] = useState(fmtNum(value, 6));
  useEffect(() => setDraft(fmtNum(value, 6)), [value]);
  const commit = () => {
    const v = parseFloat(draft);
    if (Number.isFinite(v) && Math.abs(v - value) > 1e-12) onCommit(v);
    else setDraft(fmtNum(value, 6));
  };
  return (
    <label className="dr-num">
      <span>{label}</span>
      <input className="input sm" inputMode="decimal" type="number" step={step} value={draft} disabled={disabled} onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === 'Enter' && commit()} />
    </label>
  );
}

function PointFields({ label, p, onCommit, disabled }: { label: string; p: Pt; onCommit: (p: Pt) => void; disabled?: boolean }) {
  return (
    <div className="dr-pt">
      <span className="dr-pt-l">{label}</span>
      <NumberField label="x" value={p[0]} disabled={disabled} onCommit={(x) => onCommit([x, p[1]])} />
      <NumberField label="y" value={p[1]} disabled={disabled} onCommit={(y) => onCommit([p[0], y])} />
    </div>
  );
}

function TextField({ label, value, onCommit, disabled, multiline, placeholder }: { label: string; value: string; onCommit: (v: string) => void; disabled?: boolean; multiline?: boolean; placeholder?: string }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => draft !== value && onCommit(draft);
  return (
    <label className="field dr-field">
      <span>{label}</span>
      {multiline ? (
        <textarea className="input" rows={3} value={draft} disabled={disabled} placeholder={placeholder} onChange={(e) => setDraft(e.target.value)} onBlur={commit} />
      ) : (
        <input className="input sm" value={draft} disabled={disabled} placeholder={placeholder} onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === 'Enter' && commit()} />
      )}
    </label>
  );
}

function LayerSelect({ layers, value, onChange, disabled }: { layers: DraftLayer[]; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  const { t } = useI18n();
  return (
    <label className="field dr-field">
      <span>{t('dr.props.layer')}</span>
      <select className="input sm" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {layers.map((l) => (
          <option key={l.name} value={l.name}>
            {l.name}
            {l.visible ? '' : ` (${t('dr.layers.hidden')})`}
          </option>
        ))}
      </select>
    </label>
  );
}

// ─── properties ────────────────────────────────────────────────────────────

function EntityEditor({ e, ctx }: { e: DraftEntity; ctx: PanelCtx }) {
  const { t } = useI18n();
  const ro = !ctx.canEdit;
  const set = (fields: Record<string, unknown>) => ctx.push([{ op: 'update_entity', id: e.id, set: fields }]);
  const prov = ctx.content.provenance[e.id];
  const body: ReactNode[] = [];
  switch (e.type) {
    case 'line':
      body.push(<PointFields key="p1" label="P1" p={e.p1} disabled={ro} onCommit={(p) => set({ p1: p })} />, <PointFields key="p2" label="P2" p={e.p2} disabled={ro} onCommit={(p) => set({ p2: p })} />);
      break;
    case 'circle':
      body.push(<PointFields key="c" label={t('dr.props.center')} p={e.center} disabled={ro} onCommit={(p) => set({ center: p })} />, <NumberField key="r" label={t('dr.props.radius')} value={e.radius} disabled={ro} onCommit={(v) => set({ radius: v })} />, <div key="d" className="faint small">Ø {fmtNum(e.radius * 2, 4)}</div>);
      break;
    case 'arc':
      body.push(
        <PointFields key="c" label={t('dr.props.center')} p={e.center} disabled={ro} onCommit={(p) => set({ center: p })} />,
        <NumberField key="r" label={t('dr.props.radius')} value={e.radius} disabled={ro} onCommit={(v) => set({ radius: v })} />,
        <NumberField key="s" label={t('dr.props.start')} value={e.start_angle} disabled={ro} onCommit={(v) => set({ start_angle: v })} />,
        <NumberField key="e" label={t('dr.props.end')} value={e.end_angle} disabled={ro} onCommit={(v) => set({ end_angle: v })} />,
      );
      break;
    case 'polyline':
      body.push(
        <div key="n" className="faint small">
          {t('dr.props.vertices', { n: e.points.length })}
        </div>,
        <label key="cl" className="check">
          <input type="checkbox" checked={e.closed} disabled={ro} onChange={(ev) => set({ closed: ev.target.checked })} /> {t('dr.props.closed')}
        </label>,
      );
      break;
    case 'text':
    case 'mtext':
    case 'leader':
      body.push(<TextField key="tx" label={t('dr.props.text')} value={e.text} multiline={e.type !== 'text'} disabled={ro} onCommit={(v) => ctx.push([{ op: 'edit_text', id: e.id, text: v }])} />, <NumberField key="h" label={t('dr.props.height')} value={e.height} disabled={ro} onCommit={(v) => set({ height: v })} />);
      break;
    case 'dimension': {
      const d = e as DimensionEntity;
      const m = measure(d);
      body.push(
        <div key="m" className="dr-kv">
          <span>{t('dr.props.dimtype')}</span>
          <b>{t(`dr.dim.${d.dimtype}`)}</b>
          <span>{t('dr.prov.measured')}</span>
          <b className="mono">
            {fmtNum(m.value, 4)}
            {m.unit === 'deg' ? '°' : ' mm'}
          </b>
          <span>{t('dr.prov.displayed')}</span>
          <b className="mono">{prov?.displayed ?? '—'}</b>
        </div>,
        <TextField
          key="ov"
          label={t('dr.props.override')}
          placeholder={t('dr.props.overrideHint')}
          value={d.text_override ?? ''}
          disabled={ro}
          onCommit={(v) => ctx.push([{ op: 'edit_dimension', id: d.id, text_override: v.trim() ? v : null }])}
        />,
      );
      if (prov?.consistent === false) body.push(<Chip key="mm" tone="warn">{t('dr.prov.mismatch')}</Chip>);
      if (d.center && d.radius != null) body.push(<NumberField key="a" label={t('dr.props.angle')} value={d.angle} disabled={ro} onCommit={(v) => set({ angle: v })} />);
      break;
    }
    case 'hatch':
      body.push(
        <label key="p" className="field dr-field">
          <span>{t('dr.props.pattern')}</span>
          <select className="input sm" value={e.solid ? 'SOLID' : e.pattern} disabled={ro} onChange={(ev) => set(ev.target.value === 'SOLID' ? { solid: true } : { solid: false, pattern: ev.target.value })}>
            {['ANSI31', 'ANSI32', 'ANSI37', 'NET', 'SOLID'].map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>,
        <NumberField key="s" label={t('dr.props.patternScale')} value={e.scale} disabled={ro} onCommit={(v) => v > 0 && set({ scale: v })} />,
      );
      break;
    case 'insert':
      body.push(
        <div key="b" className="dr-kv">
          <span>{t('dr.props.block')}</span>
          <b>{e.block}</b>
        </div>,
        <PointFields key="i" label={t('dr.props.insert')} p={e.insert} disabled={ro} onCommit={(p) => set({ insert: p })} />,
        <NumberField key="r" label={t('dr.props.rotation')} value={e.rotation} disabled={ro} onCommit={(v) => set({ rotation: v })} />,
      );
      break;
  }
  return (
    <div className="dr-props">
      <div className="dr-props-head">
        <b>{t(`dr.type.${e.type}`)}</b>
        <span className="mono faint">{e.id}</span>
        <OriginTag origin={e.origin} modified={e.modified} />
      </div>
      {e.source_ref && <div className="faint small mono">{t('dr.props.sourceRef', { ref: e.source_ref })}</div>}
      <LayerSelect layers={ctx.content.layers} value={e.layer} disabled={ro} onChange={(v) => ctx.push([{ op: 'set_entity_layer', ids: [e.id], layer: v }])} />
      {body}
    </div>
  );
}

export function PropertiesPanel({ ctx }: { ctx: PanelCtx }) {
  const { t } = useI18n();
  const [blockName, setBlockName] = useState('');
  const selected = ctx.content.entities.filter((e) => ctx.selection.includes(e.id));
  return (
    <div className="dr-panel-body">
      {selected.length === 1 && <EntityEditor e={selected[0]} ctx={ctx} />}
      {selected.length > 1 && (
        <Section title={t('dr.props.multi', { n: selected.length })}>
          <LayerSelect layers={ctx.content.layers} value={selected[0].layer} disabled={!ctx.canEdit} onChange={(v) => ctx.push([{ op: 'set_entity_layer', ids: ctx.selection, layer: v }])} />
        </Section>
      )}
      {selected.length > 0 && ctx.canEdit && (
        <Section title={t('dr.props.actions')}>
          <div className="dr-row">
            <button type="button" className="btn sm danger" onClick={() => ctx.push([{ op: 'delete_entities', ids: ctx.selection }])}>
              <Trash2 aria-hidden /> {t('dr.action.delete')}
            </button>
          </div>
          <div className="dr-row">
            <input className="input sm" placeholder={t('dr.block.namePlaceholder')} value={blockName} onChange={(e) => setBlockName(e.target.value)} aria-label={t('dr.block.name')} />
            <button
              type="button"
              className="btn sm"
              disabled={!blockName.trim()}
              onClick={() => {
                ctx.push([blockFromSelection(blockName.trim(), selectionCenter(selected), selected)]);
                ctx.setActiveBlock(blockName.trim());
                setBlockName('');
              }}
            >
              {t('dr.block.make')}
            </button>
          </div>
        </Section>
      )}
      {selected.length === 0 && (
        <Section title={t('dr.props.drawing')}>
          <p className="faint small">{t('dr.props.empty')}</p>
          <label className="field dr-field">
            <span>{t('dr.props.activeLayer')}</span>
            <select className="input sm" value={ctx.activeLayer} onChange={(e) => ctx.setActiveLayer(e.target.value)}>
              {ctx.content.layers.map((l) => (
                <option key={l.name}>{l.name}</option>
              ))}
            </select>
          </label>
        </Section>
      )}
      <Section title={t('dr.block.title')}>
        {ctx.content.blocks.length === 0 ? (
          <p className="faint small">{t('dr.block.none')}</p>
        ) : (
          <div className="dr-row wrap">
            {ctx.content.blocks.map((b) => (
              <button
                key={b.name}
                type="button"
                className={`btn sm${ctx.activeBlock === b.name ? ' active' : ''}`}
                disabled={!ctx.canEdit}
                onClick={() => {
                  ctx.setActiveBlock(b.name);
                  ctx.setTool('insert');
                }}
              >
                {b.name} <span className="faint">({b.entities.length})</span>
              </button>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

// ─── layers ────────────────────────────────────────────────────────────────

const COLORS = [7, 1, 2, 3, 4, 5, 6, 8];
const LINETYPES = ['CONTINUOUS', 'CENTER', 'HIDDEN', 'DASHED', 'PHANTOM', 'DOT'];
const SWATCH: Record<number, string> = { 1: '#ff4d4d', 2: '#f2c94c', 3: '#3ecf8e', 4: '#42d3e0', 5: '#4f8bff', 6: '#d36bff', 7: 'var(--dw-ink)', 8: '#8b95a3', 9: '#c0c6ce' };

export function LayersPanel({ ctx }: { ctx: PanelCtx }) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [color, setColor] = useState(7);
  const [linetype, setLinetype] = useState('CONTINUOUS');
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of ctx.content.entities) m.set(e.layer, (m.get(e.layer) ?? 0) + 1);
    return m;
  }, [ctx.content.entities]);
  return (
    <div className="dr-panel-body">
      <ul className="dr-layers">
        {ctx.content.layers.map((l) => (
          <li key={l.name} className={l.name === ctx.activeLayer ? 'is-active' : ''}>
            <button type="button" className="btn sm icon ghost" disabled={!ctx.canEdit} aria-pressed={l.visible} aria-label={l.visible ? t('dr.layers.hide', { name: l.name }) : t('dr.layers.show', { name: l.name })} onClick={() => ctx.push([{ op: 'set_layer_visibility', name: l.name, visible: !l.visible }])}>
              {l.visible ? <Eye aria-hidden /> : <EyeOff aria-hidden />}
            </button>
            <span className="dr-swatch" style={{ background: SWATCH[l.color] ?? 'var(--dw-ink)' }} aria-hidden />
            <button type="button" className="linkish dr-layer-name" onClick={() => ctx.setActiveLayer(l.name)} title={t('dr.layers.makeActive')}>
              {l.name}
            </button>
            <span className="faint small mono">{l.linetype.toLowerCase()}</span>
            <span className="faint small">{counts.get(l.name) ?? 0}</span>
          </li>
        ))}
      </ul>
      {ctx.canEdit && (
        <Section title={t('dr.layers.add')}>
          <div className="dr-row wrap">
            <input className="input sm" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('dr.layers.name')} aria-label={t('dr.layers.name')} />
            <select className="input sm" value={color} onChange={(e) => setColor(Number(e.target.value))} aria-label={t('dr.layers.color')}>
              {COLORS.map((c) => (
                <option key={c} value={c}>
                  {t('dr.layers.colorN', { n: c })}
                </option>
              ))}
            </select>
            <select className="input sm" value={linetype} onChange={(e) => setLinetype(e.target.value)} aria-label={t('dr.layers.linetype')}>
              {LINETYPES.map((lt) => (
                <option key={lt}>{lt}</option>
              ))}
            </select>
            <button
              type="button"
              className="btn sm"
              disabled={!name.trim()}
              onClick={() => {
                ctx.push([{ op: 'upsert_layer', layer: { name: name.trim(), color, linetype, lineweight: -3, visible: true, locked: false } }]);
                ctx.setActiveLayer(name.trim());
                setName('');
              }}
            >
              <Plus aria-hidden /> {t('dr.layers.addBtn')}
            </button>
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── provenance ────────────────────────────────────────────────────────────

export function ProvenancePanel({ ctx }: { ctx: PanelCtx }) {
  const { t } = useI18n();
  const v = ctx.version;
  const c = ctx.content;
  const dims = c.entities.filter((e) => e.type === 'dimension');
  return (
    <div className="dr-panel-body">
      <Section title={t('dr.prov.sources')}>
        {v.sources.length === 0 ? (
          <p className="faint small">{t('dr.prov.noSources')}</p>
        ) : (
          <ul className="dr-list">
            {v.sources.map((s) => (
              <li key={s.id}>
                <b className="mono">{s.id}</b> {t(`dr.source.${s.kind}`)} {s.name ? <span className="mono">{String(s.name)}</span> : null}
                {s.format && <Chip tone="measure">{String(s.format).toUpperCase()}</Chip>}
                {s.sha256 && <span className="faint small mono"> sha256 {String(s.sha256).slice(0, 12)}…</span>}
                {s.kind === 'text' && s.text && <blockquote className="dr-quote">{String(s.text)}</blockquote>}
              </li>
            ))}
          </ul>
        )}
        {v.ai && (
          <p className="faint small">
            {v.ai.simulated ? t('dr.prov.aiSimulated') : t('dr.prov.aiModel', { model: v.ai.model_id })} {v.ai.summary}
          </p>
        )}
      </Section>

      <Section title={t('dr.prov.scale')} extra={<ScaleChip status={v.scale_status} />}>
        {c.underlays.length === 0 ? (
          <p className="faint small">{t(`dr.scale.${v.scale_status}.hint`)}</p>
        ) : (
          c.underlays.map((u) => (
            <div key={u.id} className="dr-underlay">
              <div>
                <b className="mono">{u.id}</b> {u.name} · {u.width_px}×{u.height_px}px
              </div>
              <div className="faint small">
                {u.calibration.status === 'calibrated' && t('dr.cal.done', { mmpp: fmtNum(u.calibration.mm_per_px ?? 0, 6), d: fmtNum(u.calibration.distance_mm ?? 0) })}
                {u.calibration.status === 'assumed' && t('dr.cal.assumed', { mmpp: fmtNum(u.calibration.mm_per_px ?? 0, 6), id: u.calibration.assumption_id ?? '' })}
                {u.calibration.status === 'unknown' && t('dr.cal.unknown')}
              </div>
              {ctx.canEdit && (
                <div className="dr-row wrap">
                  <button type="button" className="btn sm" onClick={() => ctx.setTool('calibrate')}>
                    {t('dr.cal.start')}
                  </button>
                  <button type="button" className="btn sm ghost" onClick={() => ctx.push([{ op: 'set_underlay', id: u.id, set: { visible: !u.visible } }])}>
                    {u.visible ? t('dr.cal.hide') : t('dr.cal.show')}
                  </button>
                  <label className="dr-num">
                    <span>{t('dr.cal.opacity')}</span>
                    <input type="range" min={0.1} max={1} step={0.1} value={u.opacity} onChange={(e) => ctx.push([{ op: 'set_underlay', id: u.id, set: { opacity: Number(e.target.value) } }])} />
                  </label>
                </div>
              )}
            </div>
          ))
        )}
      </Section>

      <Section title={t('dr.prov.readings', { n: dims.length })}>
        {dims.length === 0 ? (
          <p className="faint small">{t('dr.prov.noDims')}</p>
        ) : (
          <div className="dr-table-wrap">
            <table className="dr-table">
              <thead>
                <tr>
                  <th>{t('dr.prov.entity')}</th>
                  <th>{t('dr.prov.displayed')}</th>
                  <th>{t('dr.prov.original')}</th>
                  <th>{t('dr.prov.measured')}</th>
                  <th>{t('dr.prov.override')}</th>
                  <th>{t('dr.prov.assumption')}</th>
                </tr>
              </thead>
              <tbody>
                {dims.map((d) => {
                  const p = c.provenance[d.id];
                  return (
                    <tr key={d.id} className={p?.consistent === false ? 'is-warn' : ''} onClick={() => ctx.setSelection([d.id])}>
                      <td className="mono">{d.id}</td>
                      <td className="mono">{p?.displayed ?? '—'}</td>
                      <td>{p?.original ? <span title={`${p.original.source ?? ''} ${p.original.locator ?? ''}`}>“{p.original.text}” <span className="faint small">{p.original.source}</span></span> : <span className="faint">—</span>}</td>
                      <td className="mono">{p?.measured != null ? `${fmtNum(p.measured, 4)}${p.unit === 'deg' ? '°' : ''}` : '—'}</td>
                      <td>{p?.override ? <span>{p.override.text ?? t('dr.prov.cleared')} <span className="faint small">({t(`dr.author.${p.override.author}`)})</span></span> : <span className="faint">—</span>}</td>
                      <td className="mono">{p?.assumption ?? '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title={t('dr.prov.conflicts', { n: c.conflicts.length })}>
        {c.conflicts.length === 0 ? (
          <p className="faint small">{t('dr.prov.noConflicts')}</p>
        ) : (
          <ul className="dr-list">
            {c.conflicts.map((cf) => (
              <li key={cf.id} className="dr-conflict">
                <div>
                  <b className="mono">{cf.id}</b> {cf.subject} <Chip tone={cf.status === 'open' ? 'warn' : 'ok'}>{t(`dr.conflict.${cf.status}`)}</Chip> <span className="faint small">{t(`dr.conflictKind.${cf.kind}`)}</span>
                </div>
                <ul className="dr-values">
                  {cf.values.map((val, i) => (
                    <li key={i}>
                      <b className="mono">{val.text || String(val.value)}</b> <span className="faint small">{t(`dr.valueKind.${val.kind}`)} · {val.source ?? ''} {val.locator ?? ''}</span>
                      {ctx.canEdit && cf.status === 'open' && (
                        <button type="button" className="btn sm ghost" onClick={() => ctx.push([{ op: 'resolve_conflict', conflict_id: cf.id, value: val.value ?? val.text, note: '' }])}>
                          {t('dr.conflict.choose')}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
                {cf.note && <div className="faint small">{cf.note}</div>}
                {cf.resolution && (
                  <div className="small">
                    {t('dr.conflict.resolution', { by: String(cf.resolution.by ?? ''), value: String(cf.resolution.value ?? '') })}
                    {cf.resolution.by === 'ai_turbo' && <span className="faint"> · {t('dr.conflict.unconfirmed')}</span>}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={t('dr.prov.assumptions', { n: c.assumptions.length })}>
        {c.assumptions.length === 0 ? (
          <p className="faint small">{t('dr.prov.noAssumptions')}</p>
        ) : (
          <ul className="dr-list">
            {c.assumptions.map((a) => (
              <li key={a.id}>
                <b className="mono">{a.id}</b> {a.subject}: <b>{JSON.stringify(a.adopted)}</b> {a.unit ?? ''} <Chip tone="warn">{t(`dr.author.${a.author}`)}</Chip>
                {a.confirmation ? (
                  <Chip tone={a.confirmation.decision === 'confirmed' ? 'ok' : 'warn'}>{t(`dr.assumption.${a.confirmation.decision}`)}</Chip>
                ) : (
                  a.author !== 'person' &&
                  ctx.canEdit && (
                    <button type="button" className="btn sm ghost" onClick={() => ctx.push([{ op: 'confirm_assumption', assumption_id: a.id }])}>
                      {t('dr.assumption.confirm')}
                    </button>
                  )
                )}
                <div className="faint small">{a.basis}</div>
                {a.alternatives.length > 0 && <div className="faint small">{t('dr.prov.alternatives', { v: a.alternatives.map((x) => JSON.stringify(x)).join(' / ') })}</div>}
              </li>
            ))}
          </ul>
        )}
        {c.open_questions.length > 0 && (
          <>
            <h4 className="dr-sub">{t('dr.prov.questions')}</h4>
            <ul className="dr-list">
              {c.open_questions.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
          </>
        )}
      </Section>

      <Section title={t('dr.prov.unsupported', { n: v.unsupported.reduce((s, r) => s + r.count, 0) })}>
        {v.unsupported.length === 0 ? (
          <p className="faint small">{t('dr.prov.noUnsupported')}</p>
        ) : (
          <table className="dr-table">
            <thead>
              <tr>
                <th>{t('dr.prov.type')}</th>
                <th>{t('dr.prov.count')}</th>
                <th>{t('dr.prov.kept')}</th>
                <th>{t('dr.prov.handling')}</th>
              </tr>
            </thead>
            <tbody>
              {v.unsupported.map((r, i) => (
                <tr key={i} className={r.preserved ? '' : 'is-warn'}>
                  <td className="mono">{r.type}</td>
                  <td>{r.count}</td>
                  <td>{r.preserved ? <Chip tone="ok">{t('dr.prov.preserved')}</Chip> : <Chip tone="err">{t('dr.prov.notPreserved')}</Chip>}</td>
                  <td className="small">{r.handling}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}

// ─── sheet: size, title block, BOM ─────────────────────────────────────────

export function SheetPanel({ ctx }: { ctx: PanelCtx }) {
  const { t } = useI18n();
  const sheet = ctx.content.sheets[0] as DraftSheet | undefined;
  const [fields, setFields] = useState<Record<string, string>>({});
  const [bom, setBom] = useState<Record<string, string>[]>([]);
  useEffect(() => {
    setFields({ ...(sheet?.title_block ?? {}) } as Record<string, string>);
    setBom((sheet?.bom ?? []).map((r) => ({ ...r })));
  }, [sheet]);
  if (!sheet) return <p className="faint small">{t('dr.sheet.none')}</p>;
  const ro = !ctx.canEdit;
  const tbDirty = JSON.stringify(fields) !== JSON.stringify(sheet.title_block);
  const bomDirty = JSON.stringify(bom) !== JSON.stringify(sheet.bom);
  return (
    <div className="dr-panel-body">
      <Section title={t('dr.sheet.paper')}>
        <div className="dr-row wrap">
          <label className="field dr-field">
            <span>{t('dr.sheet.size')}</span>
            <select className="input sm" value={sheet.size} disabled={ro} onChange={(e) => ctx.push([{ op: 'set_sheet', set: { size: e.target.value } }])}>
              {['A4', 'A3', 'A2', 'A1', 'A0'].map((s) => (
                <option key={s}>{s}</option>
              ))}
              {sheet.size === 'custom' && <option value="custom">{t('dr.sheet.custom')}</option>}
            </select>
          </label>
          <label className="field dr-field">
            <span>{t('dr.sheet.orientation')}</span>
            <select className="input sm" value={sheet.orientation} disabled={ro || sheet.size === 'custom'} onChange={(e) => ctx.push([{ op: 'set_sheet', set: { orientation: e.target.value } }])}>
              <option value="landscape">{t('dr.sheet.landscape')}</option>
              <option value="portrait">{t('dr.sheet.portrait')}</option>
            </select>
          </label>
          <label className="field dr-field">
            <span>{t('dr.sheet.scale')}</span>
            <select className="input sm" value={String(sheet.scale)} disabled={ro} onChange={(e) => ctx.push([{ op: 'set_sheet', set: { scale: Number(e.target.value) } }])}>
              {[...new Set([sheet.scale, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02])].map((s) => (
                <option key={s} value={String(s)}>
                  {scaleText(s)}
                </option>
              ))}
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={sheet.draw_frame} disabled={ro} onChange={(e) => ctx.push([{ op: 'set_sheet', set: { draw_frame: e.target.checked } }])} /> {t('dr.sheet.frame')}
          </label>
        </div>
        {!sheet.draw_frame && <p className="faint small">{t('dr.sheet.frameOffHint')}</p>}
      </Section>
      <Section
        title={t('dr.sheet.titleBlock')}
        extra={
          ctx.canEdit && tbDirty ? (
            <button type="button" className="btn sm primary" onClick={() => ctx.push([{ op: 'set_title_block', fields: Object.fromEntries(TITLE_FIELDS.map((k) => [k, fields[k] ?? ''])) }])}>
              {t('dr.sheet.apply')}
            </button>
          ) : null
        }
      >
        <div className="dr-grid2">
          {TITLE_FIELDS.filter((k) => k !== 'scale' && k !== 'units').map((k) => (
            <label key={k} className="field dr-field">
              <span>{t(`dr.tb.${k}`)}</span>
              <input className="input sm" value={fields[k] ?? ''} disabled={ro} onChange={(e) => setFields((f) => ({ ...f, [k]: e.target.value }))} />
              {sheet.title_readings[k] && <span className="faint small">{t('dr.sheet.reading', { text: sheet.title_readings[k]!.text, src: sheet.title_readings[k]!.locator ?? '' })}</span>}
            </label>
          ))}
        </div>
      </Section>
      <Section
        title={t('dr.sheet.bom', { n: sheet.bom.length })}
        extra={
          ctx.canEdit && bomDirty ? (
            <button type="button" className="btn sm primary" onClick={() => ctx.push([{ op: 'set_bom', rows: bom.map((r) => Object.fromEntries(Object.entries(r).filter(([, v]) => v !== ''))) }])}>
              {t('dr.sheet.apply')}
            </button>
          ) : null
        }
      >
        <div className="dr-table-wrap">
          <table className="dr-table dr-bom">
            <thead>
              <tr>
                {BOM_COLUMNS.map((c) => (
                  <th key={c}>{t(`dr.bom.${c}`)}</th>
                ))}
                <th />
              </tr>
            </thead>
            <tbody>
              {bom.map((row, ri) => (
                <tr key={ri}>
                  {BOM_COLUMNS.map((c) => (
                    <td key={c}>
                      <input className="input sm" value={row[c] ?? ''} disabled={ro} aria-label={`${t(`dr.bom.${c}`)} ${ri + 1}`} onChange={(e) => setBom((b) => b.map((r, i) => (i === ri ? { ...r, [c]: e.target.value } : r)))} />
                    </td>
                  ))}
                  <td>
                    {!ro && (
                      <button type="button" className="btn sm icon ghost" aria-label={t('dr.bom.remove')} onClick={() => setBom((b) => b.filter((_, i) => i !== ri))}>
                        <Trash2 aria-hidden />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!ro && (
          <button type="button" className="btn sm" onClick={() => setBom((b) => [...b, { item: String(b.length + 1) }])}>
            <Plus aria-hidden /> {t('dr.bom.add')}
          </button>
        )}
      </Section>
    </div>
  );
}

// ─── history ───────────────────────────────────────────────────────────────

export function HistoryPanel({
  versions,
  headId,
  viewingId,
  canEdit,
  onView,
  onRestore,
  onDownload,
}: {
  versions: VersionBrief[] | undefined;
  headId: string;
  viewingId: string;
  canEdit: boolean;
  onView: (v: VersionBrief) => void;
  onRestore: (v: VersionBrief) => void;
  onDownload: (v: VersionBrief, what: 'dxf' | 'dwg' | 'package') => void;
}) {
  const { t, fmt } = useI18n();
  if (!versions) return <p className="faint small">{t('dr.history.loading')}</p>;
  return (
    <div className="dr-panel-body">
      <ol className="dr-history">
        {versions.map((v) => (
          <li key={v.id} className={`${v.id === viewingId ? 'is-viewing' : ''}`}>
            <div className="dr-history-h">
              <b className="mono">{v.label}</b>
              <span className="faint small">{t(`dr.originKind.${v.origin}`)}</span>
              {v.id === headId && <Chip tone="accent">{t('dr.history.head')}</Chip>}
              <span className="faint small">{fmt.dateTime(v.created_at)}</span>
            </div>
            <VersionChips v={v} compact />
            <div className="faint small">
              {v.commands.length ? v.commands.join(', ') : ''} {v.note}
            </div>
            <div className="dr-row wrap">
              {v.id !== viewingId && (
                <button type="button" className="btn sm ghost" onClick={() => onView(v)}>
                  {t('dr.history.view')}
                </button>
              )}
              {canEdit && v.id !== headId && (
                <button type="button" className="btn sm" onClick={() => onRestore(v)}>
                  {t('dr.history.restore')}
                </button>
              )}
              <button type="button" className="btn sm ghost" onClick={() => onDownload(v, 'dxf')}>
                DXF
              </button>
              <button type="button" className="btn sm ghost" disabled={!v.files.some((f) => f.format === 'dwg')} onClick={() => onDownload(v, 'dwg')}>
                DWG
              </button>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

// ─── checks ────────────────────────────────────────────────────────────────

export function ChecksPanel({ version, onSelect }: { version: VersionFull; onSelect: (ids: string[]) => void }) {
  const { t } = useI18n();
  const ruleLabel = (rule: string) => {
    const key = `dr.rule.${rule}`;
    return hasKey(key) ? t(key) : rule;
  };
  const rules = version.checks['drafting.rules'];
  const sem = version.checks['drawing.dwg_semantic'];
  const enc = ((version.manifest as { encoding?: { what: string; written_as: string; reason: string; id?: string }[] }).encoding ?? []);
  const dwg = version.dwg as { status?: string; code?: string; message?: string; reason?: string; readback?: { reader: string; status: string }[] };
  return (
    <div className="dr-panel-body">
      <Section title={t('dr.checks.verification')}>
        <VersionChips v={version} />
        <p className="faint small">{version.execution_policy === 'turbo' ? t('dr.checks.turboHint') : t('dr.checks.standardHint')}</p>
      </Section>
      <Section title={t('dr.checks.rules')} extra={rules ? <CheckStatusChip status={rules.status as never} /> : null}>
        {rules?.items ? (
          <ul className="dr-list">
            {rules.items.map((i) => (
              <li key={i.rule} className={`dr-check dr-check-${i.status}`}>
                <b>{ruleLabel(i.rule)}</b> <span className="small">{i.message}</span>
                {i.ids && i.ids.length > 0 && (
                  <button type="button" className="linkish small" onClick={() => onSelect(i.ids!)}>
                    {t('dr.checks.selectIds', { n: i.ids.length })}
                  </button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="faint small">{t('dr.checks.skipped')}</p>
        )}
      </Section>
      <Section title={t('dr.checks.dwg')} extra={sem ? <CheckStatusChip status={sem.status as never} /> : null}>
        <p className="small">
          {t('dr.checks.dwgStatus', { s: dwg.status ?? '—' })} {dwg.code ? <span className="mono">{dwg.code}</span> : null} {dwg.message ?? dwg.reason ?? ''}
        </p>
        {dwg.readback && <p className="faint small">{dwg.readback.map((r) => `${r.reader}: ${r.status}`).join(' · ')}</p>}
        {sem?.mismatches && sem.mismatches.length > 0 && (
          <ul className="dr-list small">
            {sem.mismatches.map((m, i) => (
              <li key={i}>{m}</li>
            ))}
          </ul>
        )}
        {typeof sem?.independent_reader === 'string' && <p className="faint small">{sem.independent_reader}</p>}
      </Section>
      {enc.length > 0 && (
        <Section title={t('dr.checks.encoding')}>
          <ul className="dr-list small">
            {enc.map((e, i) => (
              <li key={i}>
                {e.id ? <span className="mono">{e.id} </span> : null}
                {e.what} → {e.written_as} <span className="faint">({e.reason})</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
