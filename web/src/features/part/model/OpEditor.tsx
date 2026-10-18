// Parameter panel for one op, with per-field validation messages and the
// Move / Rotate panel for body ops.

import { Crosshair, Eye, EyeOff, Move3d, Rotate3d, Trash2, X } from 'lucide-react';
import { useState } from 'react';
import type { Datum } from '../../../api/geometry';
import type { BodyMode, BoxOp, ChamferOp, CylinderOp, EdgeGroup, ExtrudeOp, FilletOp, HoleOp, ModelOp, TransformOp, Vec2, Vec3 } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { BentPlateFields, CutFields, HoleGeometryFields, PatternFields, PlaneField, ProfileEditor, RevolveFields, ShellFields, SweepFields } from '../../editor/GeometryForms';
import { isNamedPlane } from '../../editor/geometry';
import { axisName, BODY_MODES, EDGE_GROUPS, holeFrame, isBodyOp, rotateBody, round, translateBody, type BodyOp, type FieldIssue } from '../../editor/ops';
import { CheckField, NumField, SelectField, VecField } from './fields';
import { OP_ICON } from './opText';

export interface ServerIssue {
  code?: string;
  message: string;
}

export interface OpEditorProps {
  op: ModelOp;
  ops: ModelOp[];
  editable: boolean;
  hasBase: boolean;
  issues: FieldIssue[];
  serverIssues: ServerIssue[];
  update: (updater: (op: ModelOp) => ModelOp, coalesce?: string) => void;
  remove: () => void;
  pickFace: () => void;
  pickEdge: () => void;
  picking: 'face' | 'edge' | null;
  /** Datums of the draft (preview) or the revision, for datum planes (W3). */
  datums?: Datum[];
}

const DIRECTIONS: [string, Vec3][] = [
  ['−Z', [0, 0, -1]],
  ['+Z', [0, 0, 1]],
  ['−X', [-1, 0, 0]],
  ['+X', [1, 0, 0]],
  ['−Y', [0, -1, 0]],
  ['+Y', [0, 1, 0]],
];

export function OpEditor(props: OpEditorProps) {
  const { op, editable, issues, serverIssues, update, remove } = props;
  const { t } = useI18n();
  const err = (field: string) => {
    const i = issues.find((x) => x.field === field || (field && x.field.startsWith(`${field}.`)));
    return i ? t(i.key, i.vars) : undefined;
  };
  const general = issues.filter((x) => !x.field);
  const set = <T extends ModelOp>(fn: (o: T) => T, key?: string) => update((o) => fn(o as T), key);
  const datums = props.datums ?? [];

  return (
    <div className="ws-op-editor">
      <div className="ws-op-head">
        <span className={`ws-op-ico t-${op.type}`}>{OP_ICON[op.type]}</span>
        <div className="grow">
          <div className="ws-op-title">
            <span className="mono strong">{op.id}</span> {t(`ws.op.${op.type}`)}
          </div>
          <input
            className="input sm ws-op-label"
            value={op.label ?? ''}
            placeholder={t('ws.op.labelPlaceholder')}
            disabled={!editable}
            maxLength={120}
            aria-label={t('ws.op.label')}
            onChange={(e) => update((o) => ({ ...o, label: e.target.value || null }), 'label')}
          />
        </div>
        <button type="button" className="btn sm icon ghost" disabled={!editable} onClick={() => update((o) => ({ ...o, suppressed: !o.suppressed }))} title={op.suppressed ? t('ws.op.unsuppress') : t('ws.op.suppress')} aria-label={op.suppressed ? t('ws.op.unsuppress') : t('ws.op.suppress')}>
          {op.suppressed ? <EyeOff /> : <Eye />}
        </button>
        <button type="button" className="btn sm icon ghost danger" disabled={!editable} onClick={remove} title={`${t('ws.op.delete')} (Del)`} aria-label={t('ws.op.delete')}>
          <Trash2 />
        </button>
      </div>

      {(serverIssues.length > 0 || general.length > 0) && (
        <div className="callout err slim" role="alert">
          <div className="grow">
            {general.map((g, i) => (
              <div key={`g${i}`}>{t(g.key, g.vars)}</div>
            ))}
            {serverIssues.map((s, i) => (
              <div key={`s${i}`}>
                {s.code && <code className="error-code">{s.code}</code>} {s.message}
              </div>
            ))}
          </div>
        </div>
      )}
      {op.suppressed && <div className="callout pending slim">{t('ws.op.suppressedNote')}</div>}

      <fieldset className="ws-fields" disabled={!editable}>
        {isBodyOp(op) && <ModeField op={op} hasBase={props.hasBase} ops={props.ops} set={set} err={err} />}
        {op.type === 'box' && (
          <>
            <VecField label={t('ws.f.size')} unit="mm" value={op.size} onChange={(v) => set<BoxOp>((o) => ({ ...o, size: v as Vec3 }), 'size')} error={err('size')} />
            <VecField label={t('ws.f.atBottom')} unit="mm" value={op.at ?? [0, 0, 0]} onChange={(v) => set<BoxOp>((o) => ({ ...o, at: v as Vec3 }), 'at')} error={err('at')} />
          </>
        )}
        {op.type === 'cylinder' && <CylinderFields op={op} set={set} err={err} />}
        {op.type === 'extrude' && <ExtrudeFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'revolve' && <RevolveFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'sweep' && <SweepFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'bent_plate' && <BentPlateFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'coil' && (
          <div className="callout slim">
            <div className="faint small">{t('geo.coil.readonly')}</div>
            <div className="mono small">
              {op.section.kind === 'circle' ? `Ø${op.section.radius * 2}` : `${op.section.width} × ${op.section.length}`} · Dm {op.mean_diameter} · n {op.turns} ·{' '}
              {op.length !== null && op.length !== undefined ? `L ${op.length}` : `p ${op.pitch}`} · {op.handedness ?? 'right'} · {t(`geo.coil.end.${op.end_form ?? 'open'}`)}
            </div>
          </div>
        )}
        {isBodyOp(op) && (
          <VecField label={t('ws.f.rotate')} unit="°" step={15} value={op.rotate ?? [0, 0, 0]} onChange={(v) => set<BodyOp>((o) => ({ ...o, rotate: v as Vec3 }), 'rotate')} error={err('rotate')} hint={t('ws.f.rotateHint')} />
        )}
        {op.type === 'hole' && <HoleFields op={op} set={set} err={err} pickFace={props.pickFace} picking={props.picking === 'face'} />}
        {(op.type === 'fillet' || op.type === 'chamfer') && <EdgeFields op={op} set={set} err={err} pickEdge={props.pickEdge} picking={props.picking === 'edge'} />}
        {op.type === 'transform' && <TransformFields op={op} ops={props.ops} set={set} err={err} />}
        {op.type === 'cut' && <CutFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'shell' && <ShellFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
        {op.type === 'pattern' && <PatternFields op={op} ops={props.ops} datums={datums} set={set} err={err} />}
      </fieldset>

      {isBodyOp(op) && editable && <MovePanel op={op} apply={(fn) => set<BodyOp>(fn)} />}
    </div>
  );
}

type SetFn = <T extends ModelOp>(fn: (o: T) => T, key?: string) => void;
type ErrFn = (field: string) => string | undefined;

function ModeField({ op, hasBase, ops, set, err }: { op: BodyOp; hasBase: boolean; ops: ModelOp[]; set: SetFn; err: ErrFn }) {
  const { t } = useI18n();
  const idx = ops.findIndex((o) => o.id === op.id);
  const isFirst = !hasBase && !ops.slice(0, idx).some((o) => isBodyOp(o) && !o.suppressed);
  const mode = op.mode ?? 'add';
  const options = BODY_MODES.filter((m) => (isFirst ? m !== 'add' : m !== 'new')).map((m) => ({ value: m, label: t(`ws.mode.${m}`) }));
  return (
    <SelectField<BodyMode>
      label={t('ws.f.mode')}
      value={isFirst && mode === 'add' ? 'new' : !isFirst && mode === 'new' ? 'add' : mode}
      options={options}
      onChange={(m) => set<BodyOp>((o) => ({ ...o, mode: m }))}
      error={err('mode')}
      hint={isFirst ? t('ws.mode.firstHint') : undefined}
    />
  );
}

function CylinderFields({ op, set, err }: { op: CylinderOp; set: SetFn; err: ErrFn }) {
  const { t } = useI18n();
  return (
    <>
      <div className="ws-row2">
        <NumField label={t('ws.f.diameter')} unit="mm" value={round(op.radius * 2, 6)} onChange={(v) => set<CylinderOp>((o) => ({ ...o, radius: v / 2 }), 'radius')} error={err('radius')} />
        <NumField label={t('ws.f.height')} unit="mm" value={op.height} onChange={(v) => set<CylinderOp>((o) => ({ ...o, height: v }), 'height')} error={err('height')} />
      </div>
      <SelectField<'x' | 'y' | 'z'> label={t('ws.f.axis')} value={op.axis ?? 'z'} options={(['x', 'y', 'z'] as const).map((a) => ({ value: a, label: `+${a.toUpperCase()}` }))} onChange={(a) => set<CylinderOp>((o) => ({ ...o, axis: a }))} />
      <VecField label={t('ws.f.atBase')} unit="mm" value={op.at ?? [0, 0, 0]} onChange={(v) => set<CylinderOp>((o) => ({ ...o, at: v as Vec3 }), 'at')} error={err('at')} />
    </>
  );
}

function ExtrudeFields({ op, ops, datums, set, err }: { op: ExtrudeOp; ops: ModelOp[]; datums: Datum[]; set: SetFn; err: ErrFn }) {
  const { t } = useI18n();
  const plane = op.plane ?? 'XY';
  const uv = !isNamedPlane(plane) ? ['U', 'V'] : plane === 'XY' ? ['U·X', 'V·Y'] : plane === 'XZ' ? ['U·X', 'V·Z'] : ['U·Y', 'V·Z'];
  return (
    <>
      <ProfileEditor profile={op.profile} err={err} onChange={(p, key) => set<ExtrudeOp>((o) => ({ ...o, profile: p }), key)} />
      <PlaneField value={plane} datums={datums} opId={op.id} ops={ops} err={err} onChange={(p, key) => set<ExtrudeOp>((o) => ({ ...o, plane: p }), key)} />
      <NumField label={t('ws.f.offset')} unit="mm" value={op.offset ?? 0} onChange={(v) => set<ExtrudeOp>((o) => ({ ...o, offset: v }), 'offset')} error={err('offset')} />
      <VecField label={t('ws.f.profileAt')} unit="mm" axes={uv} value={op.at ?? [0, 0]} onChange={(v) => set<ExtrudeOp>((o) => ({ ...o, at: v as Vec2 }), 'at')} error={err('at')} />
      <div className="ws-row2">
        <NumField label={t('ws.f.height')} unit="mm" value={op.height} onChange={(v) => set<ExtrudeOp>((o) => ({ ...o, height: v }), 'height')} error={err('height')} />
        <NumField label={t('ws.f.rotateProfile')} unit="°" step={15} value={op.rotate_profile ?? 0} onChange={(v) => set<ExtrudeOp>((o) => ({ ...o, rotate_profile: v }), 'rp')} error={err('rotate_profile')} />
      </div>
    </>
  );
}

function HoleFields({ op, set, err, pickFace, picking }: { op: HoleOp; set: SetFn; err: ErrFn; pickFace: () => void; picking: boolean }) {
  const { t } = useI18n();
  const dir = op.direction ?? [0, 0, -1];
  const head = op.counterbore ? 'cb' : op.countersink ? 'cs' : 'none';
  const pattern = op.pattern?.kind ?? 'none';
  const [u, v] = holeFrame(dir);
  const dirKey = DIRECTIONS.find(([, d]) => d.every((c, i) => Math.abs(c - dir[i]) < 1e-9))?.[0] ?? 'custom';
  return (
    <>
      <div className="ws-pick-row">
        <button type="button" className={`btn sm ${picking ? 'active' : ''}`} onClick={pickFace}>
          <Crosshair /> {picking ? t('ws.hole.picking') : t('ws.hole.pick')}
        </button>
        <span className="faint small">{t('ws.hole.pickHint')}</span>
      </div>
      <VecField label={t(op.pattern?.kind === 'linear' ? 'ws.hole.atFirst' : op.pattern?.kind === 'circular' ? 'ws.hole.atCenter' : 'ws.hole.at')} unit="mm" value={op.at} onChange={(nv) => set<HoleOp>((o) => ({ ...o, at: nv as Vec3 }), 'at')} error={err('at')} />
      <div className="ws-row2">
        <SelectField<string>
          label={t('ws.hole.direction')}
          value={dirKey}
          options={[...DIRECTIONS.map(([k]) => ({ value: k, label: k })), ...(dirKey === 'custom' ? [{ value: 'custom', label: t('ws.hole.customDir') }] : [])]}
          onChange={(k) => {
            const d = DIRECTIONS.find(([key]) => key === k)?.[1];
            if (d) set<HoleOp>((o) => ({ ...o, direction: d }));
          }}
        />
        <NumField label={t('ws.f.diameter')} unit="mm" value={op.diameter} onChange={(x) => set<HoleOp>((o) => ({ ...o, diameter: x }), 'diameter')} error={err('diameter')} />
      </div>
      {dirKey === 'custom' && <VecField label={t('ws.hole.directionVec')} value={dir} step={0.1} onChange={(nv) => set<HoleOp>((o) => ({ ...o, direction: nv as Vec3 }), 'direction')} error={err('direction')} />}
      <div className="ws-row2">
        <CheckField
          label={t('ws.hole.through')}
          checked={op.depth === null || op.depth === undefined}
          onChange={(thru) => set<HoleOp>((o) => ({ ...o, depth: thru ? null : round(Math.max(o.diameter * 2, 1)) }))}
        />
        {op.depth !== null && op.depth !== undefined && <NumField label={t('ws.hole.depth')} unit="mm" value={op.depth} onChange={(x) => set<HoleOp>((o) => ({ ...o, depth: x }), 'depth')} error={err('depth')} />}
      </div>
      <SelectField<'none' | 'cb' | 'cs'>
        label={t('ws.hole.head')}
        value={head}
        options={[
          { value: 'none', label: t('ws.hole.head.none') },
          { value: 'cb', label: t('ws.hole.head.cb') },
          { value: 'cs', label: t('ws.hole.head.cs') },
        ]}
        onChange={(h) =>
          set<HoleOp>((o) => ({
            ...o,
            counterbore: h === 'cb' ? { diameter: round(o.diameter * 1.8), depth: round(Math.max(o.diameter * 0.6, 1)) } : null,
            countersink: h === 'cs' ? { diameter: round(o.diameter * 2), angle: 90 } : null,
          }))
        }
        error={err('counterbore') ?? err('countersink')}
      />
      {op.counterbore && (
        <div className="ws-row2">
          <NumField label={t('ws.hole.cbD')} unit="mm" value={op.counterbore.diameter} onChange={(x) => set<HoleOp>((o) => ({ ...o, counterbore: { ...o.counterbore!, diameter: x } }), 'cbd')} error={err('counterbore.diameter')} />
          <NumField label={t('ws.hole.cbDepth')} unit="mm" value={op.counterbore.depth} onChange={(x) => set<HoleOp>((o) => ({ ...o, counterbore: { ...o.counterbore!, depth: x } }), 'cbh')} error={err('counterbore.depth')} />
        </div>
      )}
      {op.countersink && (
        <div className="ws-row2">
          <NumField label={t('ws.hole.csD')} unit="mm" value={op.countersink.diameter} onChange={(x) => set<HoleOp>((o) => ({ ...o, countersink: { ...o.countersink!, diameter: x } }), 'csd')} error={err('countersink.diameter')} />
          <NumField label={t('ws.hole.csAngle')} unit="°" value={op.countersink.angle ?? 90} onChange={(x) => set<HoleOp>((o) => ({ ...o, countersink: { ...o.countersink!, angle: x } }), 'csa')} error={err('countersink.angle')} />
        </div>
      )}
      <SelectField<'none' | 'linear' | 'circular'>
        label={t('ws.hole.pattern')}
        value={pattern}
        options={[
          { value: 'none', label: t('ws.hole.pattern.none') },
          { value: 'linear', label: t('ws.hole.pattern.linear') },
          { value: 'circular', label: t('ws.hole.pattern.circular') },
        ]}
        onChange={(k) =>
          set<HoleOp>((o) => ({
            ...o,
            pattern: k === 'linear' ? { kind: 'linear', count: [2, 1], pitch: [round(o.diameter * 3), round(o.diameter * 3)] } : k === 'circular' ? { kind: 'circular', count: 4, radius: round(o.diameter * 3) } : null,
          }))
        }
      />
      {op.pattern?.kind === 'linear' && (
        <>
          <VecField label={t('ws.hole.count')} axes={['U', 'V']} value={op.pattern.count} step={1} onChange={(c) => set<HoleOp>((o) => ({ ...o, pattern: { kind: 'linear', count: [Math.round(c[0]), Math.round(c[1])] as [number, number], pitch: (o.pattern as { pitch: Vec2 }).pitch } }), 'pc')} error={err('pattern.count')} />
          <VecField label={t('ws.hole.pitch')} unit="mm" axes={['U', 'V']} value={op.pattern.pitch} onChange={(pv) => set<HoleOp>((o) => ({ ...o, pattern: { kind: 'linear', count: (o.pattern as { count: [number, number] }).count, pitch: pv as Vec2 } }), 'pp')} error={err('pattern.pitch')} />
          <p className="ws-f-hint">{t('ws.hole.linearHint', { u: axisName(u), v: axisName(v) })}</p>
        </>
      )}
      {op.pattern?.kind === 'circular' && (
        <>
          <div className="ws-row2">
            <NumField label={t('ws.hole.count')} integer value={op.pattern.count} onChange={(c) => set<HoleOp>((o) => ({ ...o, pattern: { kind: 'circular', count: c, radius: (o.pattern as { radius: number }).radius } }), 'pc')} error={err('pattern.count')} />
            <NumField label={t('ws.hole.pcd')} unit="mm" value={round(op.pattern.radius * 2, 6)} onChange={(d) => set<HoleOp>((o) => ({ ...o, pattern: { kind: 'circular', count: (o.pattern as { count: number }).count, radius: d / 2 } }), 'pr')} error={err('pattern.radius')} />
          </div>
          <p className="ws-f-hint">{t('ws.hole.circularHint', { u: axisName(u) })}</p>
        </>
      )}
      <HoleGeometryFields op={op} set={set} err={err} />
    </>
  );
}

function EdgeFields({ op, set, err, pickEdge, picking }: { op: FilletOp | ChamferOp; set: SetFn; err: ErrFn; pickEdge: () => void; picking: boolean }) {
  const { t } = useI18n();
  const near = op.edges.near;
  return (
    <>
      {op.type === 'fillet' ? (
        <NumField label={t('ws.f.radius')} unit="mm" value={op.radius} step={0.5} onChange={(v) => set<FilletOp>((o) => ({ ...o, radius: v }), 'radius')} error={err('radius')} />
      ) : (
        <NumField label={t('ws.f.distance')} unit="mm" value={op.distance} step={0.5} onChange={(v) => set<ChamferOp>((o) => ({ ...o, distance: v }), 'distance')} error={err('distance')} />
      )}
      <SelectField<EdgeGroup>
        label={t('ws.edges.group')}
        value={op.edges.group ?? 'all'}
        options={EDGE_GROUPS.map((g) => ({ value: g, label: t(`ws.edges.${g}`) }))}
        onChange={(g) => set<FilletOp | ChamferOp>((o) => ({ ...o, edges: { ...o.edges, group: g } }))}
        hint={t(`ws.edges.hint.${op.edges.group ?? 'all'}`)}
      />
      <div className="ws-pick-row">
        <button type="button" className={`btn sm ${picking ? 'active' : ''}`} onClick={pickEdge}>
          <Crosshair /> {picking ? t('ws.edges.picking') : t('ws.edges.pickNear')}
        </button>
        {near && (
          <button type="button" className="btn sm ghost" onClick={() => set<FilletOp | ChamferOp>((o) => ({ ...o, edges: { ...o.edges, near: null, max_count: null } }))}>
            <X /> {t('ws.edges.clearNear')}
          </button>
        )}
      </div>
      {near ? (
        <>
          <VecField label={t('ws.edges.near')} unit="mm" value={near} onChange={(v) => set<FilletOp | ChamferOp>((o) => ({ ...o, edges: { ...o.edges, near: v as Vec3 } }), 'near')} error={err('edges.near')} />
          <NumField label={t('ws.edges.maxCount')} integer value={op.edges.max_count ?? 1} onChange={(v) => set<FilletOp | ChamferOp>((o) => ({ ...o, edges: { ...o.edges, max_count: v } }), 'mc')} error={err('edges.max_count')} />
        </>
      ) : (
        <p className="ws-f-hint">{t('ws.edges.allInGroup')}</p>
      )}
      <p className="ws-f-hint">{t('ws.edges.previewNote')}</p>
    </>
  );
}

function TransformFields({ op, ops, set, err }: { op: TransformOp; ops: ModelOp[]; set: SetFn; err: ErrFn }) {
  const { t } = useI18n();
  const idx = ops.findIndex((o) => o.id === op.id);
  const targets = ops.slice(0, idx).filter(isBodyOp);
  return (
    <>
      <SelectField<string>
        label={t('ws.f.target')}
        value={op.target}
        options={[{ value: '', label: '—', disabled: true }, ...targets.map((b) => ({ value: b.id, label: `${b.id} · ${t(`ws.op.${b.type}`)}` }))]}
        onChange={(v) => set<TransformOp>((o) => ({ ...o, target: v }))}
        error={err('target')}
      />
      <VecField label={t('ws.f.translate')} unit="mm" value={op.translate ?? [0, 0, 0]} onChange={(v) => set<TransformOp>((o) => ({ ...o, translate: v as Vec3 }), 'tr')} error={err('translate')} />
      <VecField label={t('ws.f.rotate')} unit="°" step={15} value={op.rotate ?? [0, 0, 0]} onChange={(v) => set<TransformOp>((o) => ({ ...o, rotate: v as Vec3 }), 'rot')} error={err('rotate')} />
    </>
  );
}

function MovePanel({ op, apply }: { op: BodyOp; apply: (fn: (o: BodyOp) => BodyOp) => void }) {
  const { t } = useI18n();
  const [d, setD] = useState<number[]>([0, 0, 0]);
  const [r, setR] = useState<number[]>([0, 0, 90]);
  return (
    <div className="ws-move">
      <div className="ws-move-h">
        <Move3d aria-hidden /> {t('ws.move.title')}
      </div>
      <div className="ws-move-row">
        <VecField label={t('ws.move.by')} unit="mm" value={d} onChange={setD} />
        <button type="button" className="btn sm" disabled={d.every((x) => !x)} onClick={() => apply((o) => translateBody(o, d as Vec3))}>
          {t('ws.move.apply')}
        </button>
      </div>
      <div className="ws-move-row">
        <VecField label={t('ws.move.rotateBy')} unit="°" step={15} value={r} onChange={setR} />
        <button type="button" className="btn sm" disabled={r.every((x) => !x)} onClick={() => apply((o) => rotateBody(o, r as Vec3))}>
          <Rotate3d /> {t('ws.move.apply')}
        </button>
      </div>
      <p className="ws-f-hint">{t(op.type === 'extrude' ? 'ws.move.hintExtrude' : 'ws.move.hint')}</p>
    </div>
  );
}
