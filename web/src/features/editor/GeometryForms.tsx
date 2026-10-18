// Parameter forms of the W3 geometry-library ops: plane picker (standard / custom workplane / datum of an earlier
// op), profile editor (incl. line + arc sketches with inner loops), revolve, sweep, bent plate, cut, shell, pattern
// and the hole's local-workplane / clean-mouth options.

import { Plus, X } from 'lucide-react';
import type { BentPlateOp, CutOp, Datum, PatternOp, PlaneSpec, RevolveOp, Segment, ShellOp, SketchLoop, SketchProfile, SweepOp, Workplane } from '../../api/geometry';
import type { HoleOp, ModelOp, Profile, Vec2, Vec3 } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { CheckField, NumField, SelectField, VecField } from '../part/model/fields';
import { isDatumRef, isNamedPlane, NAMED_FRAMES, planeToWorkplane, sketchSummary } from './geometry';
import { defaultProfile, PROFILE_KINDS, round } from './ops';
import './geometry.css';

export type SetFn = <T extends ModelOp>(fn: (o: T) => T, key?: string) => void;
export type ErrFn = (field: string) => string | undefined;

// ─── plane ──────────────────────────────────────────────────────────────────

type PlaneKind = 'named' | 'custom' | 'datum';

export function PlaneField({ value, onChange, datums, opId, ops, err, allowNamed = true }: {
  value: PlaneSpec | undefined;
  onChange: (p: PlaneSpec, key?: string) => void;
  datums: Datum[];
  opId: string;
  ops: ModelOp[];
  err: ErrFn;
  allowNamed?: boolean;
}) {
  const { t } = useI18n();
  const plane = value ?? 'XY';
  const kind: PlaneKind = isNamedPlane(plane) ? 'named' : isDatumRef(plane) ? 'datum' : 'custom';
  const idx = ops.findIndex((o) => o.id === opId);
  const earlier = new Set(ops.slice(0, idx < 0 ? ops.length : idx).map((o) => o.id));
  const usable = datums.filter((d) => d.kind !== 'frame' || d.name === 'origin').filter((d) => d.name === 'origin' || earlier.has(d.name.split('.')[0]));
  const kinds: PlaneKind[] = allowNamed ? ['named', 'custom', 'datum'] : ['custom', 'datum'];
  return (
    <div className="ws-geo-plane">
      <SelectField<PlaneKind>
        label={t('geo.plane.label')}
        value={kind}
        options={kinds.map((k) => ({ value: k, label: t(`geo.plane.kind.${k}`) }))}
        onChange={(k) => {
          if (k === kind) return;
          if (k === 'named') onChange('XY');
          else if (k === 'custom') onChange(isNamedPlane(plane) ? planeToWorkplane(plane) : { origin: [0, 0, 0], normal: [0, 0, 1], x_dir: [1, 0, 0] });
          else onChange({ ref: usable.find((d) => d.kind === 'plane')?.name ?? 'origin', offset: 0, flip: false });
        }}
        hint={t('geo.plane.hint')}
      />
      {isNamedPlane(plane) && (
        <SelectField<'XY' | 'XZ' | 'YZ'> label={t('ws.f.plane')} value={plane} options={(['XY', 'XZ', 'YZ'] as const).map((x) => ({ value: x, label: t(`ws.plane.${x}`) }))} onChange={(x) => onChange(x)} />
      )}
      {kind === 'custom' && !isNamedPlane(plane) && !isDatumRef(plane) && <WorkplaneFields wp={plane} onChange={(w, key) => onChange(w, key)} err={err} />}
      {isDatumRef(plane) && (
        <>
          {usable.length ? (
            <SelectField<string>
              label={t('geo.plane.datum')}
              value={plane.ref}
              options={[...(usable.some((d) => d.name === plane.ref) ? [] : [{ value: plane.ref, label: plane.ref, disabled: true }]),
                ...usable.map((d) => ({ value: d.name, label: `${d.name} · ${d.kind}` }))]}
              onChange={(ref) => onChange({ ...plane, ref })}
              error={err('plane')}
            />
          ) : (
            <p className="ws-f-hint">{t('geo.plane.noDatums')}</p>
          )}
          <div className="ws-row2">
            <NumField label={t('geo.plane.datumOffset')} unit="mm" value={plane.offset ?? 0} onChange={(v) => onChange({ ...plane, offset: v }, 'doff')} error={err('plane.offset')} />
            <CheckField label={t('geo.plane.flip')} checked={!!plane.flip} onChange={(v) => onChange({ ...plane, flip: v })} />
          </div>
        </>
      )}
    </div>
  );
}

export function WorkplaneFields({ wp, onChange, err, prefix = 'plane' }: { wp: Workplane; onChange: (w: Workplane, key?: string) => void; err: ErrFn; prefix?: string }) {
  const { t } = useI18n();
  return (
    <>
      <VecField label={t('geo.plane.origin')} unit="mm" value={wp.origin ?? [0, 0, 0]} onChange={(v) => onChange({ ...wp, origin: v as Vec3 }, `${prefix}o`)} error={err(`${prefix}.origin`)} />
      <VecField label={t('geo.plane.normal')} step={0.1} value={wp.normal ?? [0, 0, 1]} onChange={(v) => onChange({ ...wp, normal: v as Vec3 }, `${prefix}n`)} error={err(`${prefix}.normal`)} />
      <VecField label={t('geo.plane.xdir')} step={0.1} value={wp.x_dir ?? [1, 0, 0]} onChange={(v) => onChange({ ...wp, x_dir: v as Vec3 }, `${prefix}x`)} error={err(`${prefix}.x_dir`)} />
    </>
  );
}

// ─── sketches and profiles ─────────────────────────────────────────────────

export function LoopEditor({ loop, onChange, err, field, closed = true }: { loop: SketchLoop; onChange: (l: SketchLoop, key?: string) => void; err: ErrFn; field: string; closed?: boolean }) {
  const { t } = useI18n();
  const setSeg = (i: number, s: Segment, key?: string) => onChange({ ...loop, segments: loop.segments.map((q, j) => (j === i ? s : q)) }, key);
  const last = loop.segments.length ? loop.segments[loop.segments.length - 1].to : loop.start;
  return (
    <div className="ws-geo-loop">
      <VecField label={t('geo.sketch.start')} unit="mm" axes={['U', 'V']} value={loop.start} onChange={(v) => onChange({ ...loop, start: v as Vec2 }, `${field}s`)} error={err(`${field}.start`)} />
      {loop.segments.map((s, i) => (
        <div className="ws-geo-seg" key={i}>
          <div className="ws-row2">
            <SelectField<'line' | 'arc'>
              label={t('geo.sketch.segment', { n: i + 1 })}
              value={s.kind}
              options={[{ value: 'line', label: t('geo.sketch.line') }, { value: 'arc', label: t('geo.sketch.arc') }]}
              onChange={(k) => {
                if (k === s.kind) return;
                const from = i === 0 ? loop.start : loop.segments[i - 1].to;
                setSeg(i, k === 'line' ? { kind: 'line', to: s.to } : { kind: 'arc', to: s.to, center: [round((from[0] + s.to[0]) / 2), round((from[1] + s.to[1]) / 2)], ccw: true });
              }}
              error={err(`${field}.segments.${i}`)}
            />
            <button type="button" className="btn sm icon ghost" disabled={loop.segments.length <= 1} onClick={() => onChange({ ...loop, segments: loop.segments.filter((_, j) => j !== i) })} aria-label={t('geo.sketch.remove')} title={t('geo.sketch.remove')}>
              <X />
            </button>
          </div>
          <VecField label={t('geo.sketch.to')} unit="mm" axes={['U', 'V']} value={s.to} onChange={(v) => setSeg(i, { ...s, to: v as Vec2 }, `${field}t${i}`)} />
          {s.kind === 'arc' && (
            <>
              <VecField label={t('geo.sketch.center')} unit="mm" axes={['U', 'V']} value={s.center} onChange={(v) => setSeg(i, { ...s, center: v as Vec2 }, `${field}c${i}`)} />
              <CheckField label={t('geo.sketch.ccw')} checked={s.ccw !== false} onChange={(v) => setSeg(i, { ...s, ccw: v })} />
            </>
          )}
        </div>
      ))}
      <div className="row">
        <button type="button" className="btn sm" disabled={loop.segments.length >= 256} onClick={() => onChange({ ...loop, segments: [...loop.segments, { kind: 'line', to: [round(last[0] + 10), last[1]] }] })}>
          <Plus /> {t('geo.sketch.addLine')}
        </button>
        <button
          type="button"
          className="btn sm"
          disabled={loop.segments.length >= 256}
          onClick={() => onChange({ ...loop, segments: [...loop.segments, { kind: 'arc', to: [round(last[0] + 5), round(last[1] + 5)], center: [last[0], round(last[1] + 5)], ccw: true }] })}
        >
          <Plus /> {t('geo.sketch.addArc')}
        </button>
      </div>
      <p className="ws-f-hint">{closed ? t('geo.sketch.hint') : t('geo.sweep.pathHint')}</p>
    </div>
  );
}

export function SketchEditor({ profile, onChange, err, field = 'profile' }: { profile: SketchProfile; onChange: (p: SketchProfile, key?: string) => void; err: ErrFn; field?: string }) {
  const { t } = useI18n();
  const s = sketchSummary(profile);
  const holes = profile.holes ?? [];
  return (
    <div className="ws-geo-sketch">
      <span className="ws-f-label">
        {t('geo.sketch.title')} <span className="faint small">{t('geo.sketch.summary', { segments: s.segments, arcs: s.arcs, holes: s.holes })}</span>
      </span>
      <LoopEditor loop={profile} field={field} err={err} onChange={(l, key) => onChange({ ...profile, start: l.start, segments: l.segments }, key)} />
      <span className="ws-f-label">{t('geo.sketch.holes')}</span>
      {holes.map((h, i) => (
        <div className="ws-geo-hole" key={i}>
          <div className="row">
            <strong className="small">{t('geo.sketch.hole', { n: i + 1 })}</strong>
            <span className="grow" />
            <button type="button" className="btn sm icon ghost" onClick={() => onChange({ ...profile, holes: holes.filter((_, j) => j !== i) })} aria-label={t('geo.sketch.removeHole')} title={t('geo.sketch.removeHole')}>
              <X />
            </button>
          </div>
          <LoopEditor loop={h} field={`${field}.holes.${i}`} err={err} onChange={(l, key) => onChange({ ...profile, holes: holes.map((q, j) => (j === i ? l : q)) }, key)} />
        </div>
      ))}
      <button type="button" className="btn sm" disabled={holes.length >= 64} onClick={() => onChange({ ...profile, holes: [...holes, { start: [1, 0], segments: [{ kind: 'arc', to: [1, 0], center: [0, 0], ccw: true }] }] })}>
        <Plus /> {t('geo.sketch.addHole')}
      </button>
    </div>
  );
}

/** Profile editor shared by extrude, cut and sweep sections (every kind, incl. sketches). */
export function ProfileEditor({ profile, onChange, err, kinds = PROFILE_KINDS }: { profile: Profile; onChange: (p: Profile, key?: string) => void; err: ErrFn; kinds?: readonly Profile['kind'][] }) {
  const { t } = useI18n();
  const p = profile;
  const setP = (fn: (q: Profile) => Profile, key?: string) => onChange(fn(p), key ? `profile.${key}` : undefined);
  return (
    <>
      <SelectField<Profile['kind']> label={t('ws.f.profile')} value={p.kind} options={kinds.map((k) => ({ value: k, label: t(`ws.profile.${k}`) }))} onChange={(k) => setP((prev) => (prev.kind === k ? prev : defaultProfile(k, prev)))} />
      {p.kind === 'rect' && (
        <>
          <div className="ws-row2">
            <NumField label={t('ws.f.widthU')} unit="mm" value={p.width} onChange={(v) => setP((q) => ({ ...q, width: v }) as Profile, 'width')} error={err('profile.width')} />
            <NumField label={t('ws.f.lengthV')} unit="mm" value={p.length} onChange={(v) => setP((q) => ({ ...q, length: v }) as Profile, 'length')} error={err('profile.length')} />
          </div>
          <NumField label={t('ws.f.cornerRadius')} unit="mm" value={p.corner_radius ?? 0} onChange={(v) => setP((q) => ({ ...q, corner_radius: v }) as Profile, 'cr')} error={err('profile.corner_radius')} />
        </>
      )}
      {p.kind === 'circle' && <NumField label={t('ws.f.diameter')} unit="mm" value={round(p.radius * 2, 6)} onChange={(v) => setP((q) => ({ ...q, radius: v / 2 }) as Profile, 'radius')} error={err('profile.radius')} />}
      {p.kind === 'slot' && (
        <div className="ws-row2">
          <NumField label={t('ws.f.slotLength')} unit="mm" value={p.length} onChange={(v) => setP((q) => ({ ...q, length: v }) as Profile, 'length')} error={err('profile.length')} />
          <NumField label={t('ws.f.width')} unit="mm" value={p.width} onChange={(v) => setP((q) => ({ ...q, width: v }) as Profile, 'width')} error={err('profile.width')} />
        </div>
      )}
      {p.kind === 'ring' && (
        <div className="ws-row2">
          <NumField label={t('ws.f.outerD')} unit="mm" value={round(p.outer_radius * 2, 6)} onChange={(v) => setP((q) => ({ ...q, outer_radius: v / 2 }) as Profile, 'or')} error={err('profile.outer_radius')} />
          <NumField label={t('ws.f.innerD')} unit="mm" value={round(p.inner_radius * 2, 6)} onChange={(v) => setP((q) => ({ ...q, inner_radius: v / 2 }) as Profile, 'ir')} error={err('profile.inner_radius')} />
        </div>
      )}
      {p.kind === 'polygon' && <PolygonPoints points={p.points} onChange={(pts, key) => onChange({ kind: 'polygon', points: pts }, key)} error={err('profile.points')} />}
      {p.kind === 'sketch' && <SketchEditor profile={p} onChange={(q, key) => onChange(q, key)} err={err} />}
    </>
  );
}

function PolygonPoints({ points, onChange, error }: { points: Vec2[]; onChange: (p: Vec2[], key?: string) => void; error?: string }) {
  const { t } = useI18n();
  return (
    <div className={`ws-f ${error ? 'has-error' : ''}`}>
      <span className="ws-f-label">{t('ws.f.points')}</span>
      <div className="ws-poly">
        {points.map((pt, i) => (
          <div className="ws-poly-row" key={i}>
            <span className="faint mono small">{i + 1}</span>
            <VecField label="" axes={['U', 'V']} value={pt} onChange={(v) => onChange(points.map((q, j) => (j === i ? (v as Vec2) : q)), `pt${i}`)} />
            <button type="button" className="btn sm icon ghost" disabled={points.length <= 3} onClick={() => onChange(points.filter((_, j) => j !== i))} aria-label={t('ws.f.removePoint')}>
              <X />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="btn sm"
          disabled={points.length >= 256}
          onClick={() => {
            const a = points[points.length - 1];
            const b = points[0];
            onChange([...points, [round((a[0] + b[0]) / 2), round((a[1] + b[1]) / 2)]]);
          }}
        >
          <Plus /> {t('ws.f.addPoint')}
        </button>
      </div>
      {error ? <span className="ws-f-err">{error}</span> : <span className="ws-f-hint">{t('ws.f.pointsHint')}</span>}
    </div>
  );
}

// ─── op forms ──────────────────────────────────────────────────────────────

interface FormProps<T> {
  op: T;
  ops: ModelOp[];
  datums: Datum[];
  set: SetFn;
  err: ErrFn;
}

export function RevolveFields({ op, set, err }: FormProps<RevolveOp>) {
  const { t } = useI18n();
  return (
    <>
      <SketchEditor profile={op.profile} err={err} onChange={(p, key) => set<RevolveOp>((o) => ({ ...o, profile: p }), key)} />
      <p className="ws-f-hint">{t('geo.revolve.hint')}</p>
      <VecField label={t('geo.revolve.at')} unit="mm" value={op.at ?? [0, 0, 0]} onChange={(v) => set<RevolveOp>((o) => ({ ...o, at: v as Vec3 }), 'at')} error={err('at')} />
      <VecField label={t('geo.revolve.axis')} step={0.1} value={op.axis ?? [0, 0, 1]} onChange={(v) => set<RevolveOp>((o) => ({ ...o, axis: v as Vec3 }), 'axis')} error={err('axis')} />
      <NumField label={t('geo.revolve.angle')} unit="°" step={15} value={op.angle ?? 360} onChange={(v) => set<RevolveOp>((o) => ({ ...o, angle: v }), 'angle')} error={err('angle')} />
    </>
  );
}

export function SweepFields({ op, ops, datums, set, err }: FormProps<SweepOp>) {
  const { t } = useI18n();
  return (
    <>
      <span className="ws-f-label">{t('geo.sweep.section')}</span>
      <ProfileEditor profile={op.profile} err={err} onChange={(p, key) => set<SweepOp>((o) => ({ ...o, profile: p }), key)} />
      <PlaneField value={op.plane} datums={datums} opId={op.id} ops={ops} err={err} onChange={(p, key) => set<SweepOp>((o) => ({ ...o, plane: p }), key)} />
      <span className="ws-f-label">{t('geo.sweep.path')}</span>
      <LoopEditor loop={op.path} field="path" closed={false} err={err} onChange={(l, key) => set<SweepOp>((o) => ({ ...o, path: l }), key)} />
    </>
  );
}

export function BentPlateFields({ op, ops, datums, set, err }: FormProps<BentPlateOp>) {
  const { t } = useI18n();
  return (
    <>
      <div className="ws-row2">
        <NumField label={t('geo.bend.thickness')} unit="mm" value={op.thickness} onChange={(v) => set<BentPlateOp>((o) => ({ ...o, thickness: v }), 'th')} error={err('thickness')} />
        <NumField label={t('geo.bend.width')} unit="mm" value={op.width} onChange={(v) => set<BentPlateOp>((o) => ({ ...o, width: v }), 'w')} error={err('width')} />
      </div>
      {op.legs.map((leg, i) => (
        <div className="ws-row2" key={i}>
          <NumField label={t('geo.bend.leg', { n: i + 1 })} unit="mm" value={leg} onChange={(v) => set<BentPlateOp>((o) => ({ ...o, legs: o.legs.map((x, j) => (j === i ? v : x)) }), `leg${i}`)} error={err(`legs.${i}`)} />
          {i < op.bends.length && (
            <NumField label={t('geo.bend.bend', { n: i + 1 })} unit="°" step={15} value={op.bends[i]} onChange={(v) => set<BentPlateOp>((o) => ({ ...o, bends: o.bends.map((x, j) => (j === i ? v : x)) }), `bend${i}`)} error={err(`bends.${i}`) ?? err('bends')} />
          )}
        </div>
      ))}
      <div className="row">
        <button type="button" className="btn sm" disabled={op.legs.length >= 16} onClick={() => set<BentPlateOp>((o) => ({ ...o, legs: [...o.legs, 20], bends: [...o.bends, 90] }))}>
          <Plus /> {t('geo.bend.addLeg')}
        </button>
        <button type="button" className="btn sm ghost" disabled={op.legs.length <= 2} onClick={() => set<BentPlateOp>((o) => ({ ...o, legs: o.legs.slice(0, -1), bends: o.bends.slice(0, -1) }))}>
          <X /> {t('geo.bend.removeLeg')}
        </button>
      </div>
      <div className="ws-row2">
        <NumField label={t('geo.bend.innerRadius')} unit="mm" value={op.inner_radius ?? 0} onChange={(v) => set<BentPlateOp>((o) => ({ ...o, inner_radius: v }), 'ri')} error={err('inner_radius')} />
        <SelectField<'outside' | 'flat'>
          label={t('geo.bend.legLength')}
          value={op.leg_length ?? 'outside'}
          options={(['outside', 'flat'] as const).map((x) => ({ value: x, label: t(`geo.bend.legLength.${x}`) }))}
          onChange={(x) => set<BentPlateOp>((o) => ({ ...o, leg_length: x }))}
        />
      </div>
      <PlaneField value={op.plane ?? 'XZ'} datums={datums} opId={op.id} ops={ops} err={err} onChange={(p, key) => set<BentPlateOp>((o) => ({ ...o, plane: p }), key)} />
      <p className="ws-f-hint">{t('geo.bend.hint')}</p>
    </>
  );
}

export function CutFields({ op, ops, datums, set, err }: FormProps<CutOp>) {
  const { t } = useI18n();
  const through = op.depth === null || op.depth === undefined;
  const plane = op.plane ?? 'XY';
  const uv = isNamedPlane(plane) ? (plane === 'XY' ? ['U·X', 'V·Y'] : plane === 'XZ' ? ['U·X', 'V·Z'] : ['U·Y', 'V·Z']) : ['U', 'V'];
  return (
    <>
      <ProfileEditor profile={op.profile} err={err} onChange={(p, key) => set<CutOp>((o) => ({ ...o, profile: p }), key)} />
      <PlaneField value={op.plane} datums={datums} opId={op.id} ops={ops} err={err} onChange={(p, key) => set<CutOp>((o) => ({ ...o, plane: p }), key)} />
      <div className="ws-row2">
        <NumField label={t('geo.cut.offset')} unit="mm" value={op.offset ?? 0} onChange={(v) => set<CutOp>((o) => ({ ...o, offset: v }), 'off')} error={err('offset')} />
        <NumField label={t('ws.f.rotateProfile')} unit="°" step={15} value={op.rotate_profile ?? 0} onChange={(v) => set<CutOp>((o) => ({ ...o, rotate_profile: v }), 'rp')} />
      </div>
      <VecField label={t('ws.f.profileAt')} unit="mm" axes={uv} value={op.at ?? [0, 0]} onChange={(v) => set<CutOp>((o) => ({ ...o, at: v as Vec2 }), 'at')} error={err('at')} />
      <div className="ws-row2">
        <CheckField label={t('geo.cut.through')} checked={through} onChange={(v) => set<CutOp>((o) => ({ ...o, depth: v ? null : 5 }))} />
        {!through && <NumField label={t('geo.cut.depth')} unit="mm" value={op.depth ?? 0} onChange={(v) => set<CutOp>((o) => ({ ...o, depth: v }), 'depth')} error={err('depth')} />}
      </div>
      <p className="ws-f-hint">{t('geo.cut.hint')}</p>
    </>
  );
}

const FACE_DIRS: [string, Vec3][] = [
  ['+Z', [0, 0, 1]],
  ['−Z', [0, 0, -1]],
  ['+X', [1, 0, 0]],
  ['−X', [-1, 0, 0]],
  ['+Y', [0, 1, 0]],
  ['−Y', [0, -1, 0]],
];

export function ShellFields({ op, set, err }: FormProps<ShellOp>) {
  const { t } = useI18n();
  const open = op.open_faces ?? [];
  const has = (d: Vec3) => open.some((o) => o.every((c, i) => Math.abs(c - d[i]) < 1e-9));
  return (
    <>
      <NumField label={t('geo.shell.thickness')} unit="mm" value={op.thickness} onChange={(v) => set<ShellOp>((o) => ({ ...o, thickness: v }), 'th')} error={err('thickness')} />
      <span className="ws-f-label">{t('geo.shell.open')}</span>
      <div className="row wrap">
        {FACE_DIRS.map(([name, d]) => (
          <CheckField
            key={name}
            label={name}
            checked={has(d)}
            onChange={(v) => set<ShellOp>((o) => ({ ...o, open_faces: v ? [...(o.open_faces ?? []), d] : (o.open_faces ?? []).filter((x) => !x.every((c, i) => Math.abs(c - d[i]) < 1e-9)) }))}
          />
        ))}
      </div>
      <p className="ws-f-hint">{t('geo.shell.hint')}</p>
    </>
  );
}

export function PatternFields({ op, ops, set, err }: FormProps<PatternOp>) {
  const { t } = useI18n();
  const idx = ops.findIndex((o) => o.id === op.id);
  const targets = ops.slice(0, idx).filter((o) => ['box', 'cylinder', 'extrude', 'revolve', 'sweep', 'bent_plate', 'hole', 'cut'].includes(o.type));
  const l = op.layout;
  return (
    <>
      <SelectField<string>
        label={t('geo.pattern.target')}
        value={op.target}
        options={[{ value: '', label: '—', disabled: true }, ...targets.map((o) => ({ value: o.id, label: `${o.id} · ${t(`ws.op.${o.type}`)}` }))]}
        onChange={(v) => set<PatternOp>((o) => ({ ...o, target: v }))}
        error={err('target')}
      />
      <SelectField<'linear' | 'circular'>
        label={t('geo.pattern.kind')}
        value={l.kind}
        options={[{ value: 'linear', label: t('geo.pattern.linear') }, { value: 'circular', label: t('geo.pattern.circular') }]}
        onChange={(k) =>
          set<PatternOp>((o) => ({ ...o, layout: k === 'linear' ? { kind: 'linear', direction: [1, 0, 0], spacing: 20, count: o.layout.count } : { kind: 'circular', axis_origin: [0, 0, 0], axis: [0, 0, 1], count: o.layout.count, step_angle: null } }))
        }
      />
      {l.kind === 'linear' ? (
        <>
          <VecField label={t('geo.pattern.direction')} step={0.1} value={l.direction} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, direction: v as Vec3 } }), 'dir')} error={err('layout.direction')} />
          <div className="ws-row2">
            <NumField label={t('geo.pattern.spacing')} unit="mm" value={l.spacing} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, spacing: v } }), 'sp')} error={err('layout.spacing')} />
            <NumField label={t('geo.pattern.count')} integer value={l.count} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, count: v } }), 'n')} error={err('layout.count')} />
          </div>
          <CheckField
            label={t('geo.pattern.second')}
            checked={(l.count2 ?? 1) > 1}
            onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: v ? { ...l, direction2: [0, 1, 0], spacing2: 20, count2: 2 } : { ...l, direction2: null, spacing2: 0, count2: 1 } }))}
          />
          {(l.count2 ?? 1) > 1 && (
            <>
              <VecField label={t('geo.pattern.direction2')} step={0.1} value={l.direction2 ?? [0, 1, 0]} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, direction2: v as Vec3 } }), 'dir2')} />
              <div className="ws-row2">
                <NumField label={t('geo.pattern.spacing2')} unit="mm" value={l.spacing2 ?? 0} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, spacing2: v } }), 'sp2')} />
                <NumField label={t('geo.pattern.count2')} integer value={l.count2 ?? 1} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, count2: v } }), 'n2')} />
              </div>
            </>
          )}
        </>
      ) : (
        <>
          <VecField label={t('geo.pattern.axisOrigin')} unit="mm" value={l.axis_origin ?? [0, 0, 0]} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, axis_origin: v as Vec3 } }), 'ao')} />
          <VecField label={t('geo.pattern.axis')} step={0.1} value={l.axis ?? [0, 0, 1]} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, axis: v as Vec3 } }), 'ax')} error={err('layout.axis')} />
          <div className="ws-row2">
            <NumField label={t('geo.pattern.count')} integer value={l.count} onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, count: v } }), 'n')} error={err('layout.count')} />
            <NumField
              label={t('geo.pattern.step')}
              unit="°"
              value={l.step_angle ?? null}
              onChange={(v) => set<PatternOp>((o) => ({ ...o, layout: { ...l, step_angle: v } }), 'st')}
              onClear={() => set<PatternOp>((o) => ({ ...o, layout: { ...l, step_angle: null } }))}
            />
          </div>
        </>
      )}
      <p className="ws-f-hint">{t('geo.pattern.hint')}</p>
    </>
  );
}

/** Local workplane and clean-mouth options of a hole. */
export function HoleGeometryFields({ op, set, err }: { op: HoleOp; set: SetFn; err: ErrFn }) {
  const { t } = useI18n();
  const local = !!op.plane;
  return (
    <>
      <CheckField
        label={t('geo.hole.localPlane')}
        checked={local}
        onChange={(v) => set<HoleOp>((o) => (v ? { ...o, plane: { origin: o.at, normal: NAMED_FRAMES.XY.z, x_dir: NAMED_FRAMES.XY.x }, at: [0, 0, 0], direction: [0, 0, -1] } : { ...o, plane: null, at: o.plane?.origin ?? o.at, direction: [0, 0, -1] }))}
      />
      {local && op.plane && (
        <>
          <WorkplaneFields wp={op.plane} err={err} onChange={(w, key) => set<HoleOp>((o) => ({ ...o, plane: w }), key)} />
          <p className="ws-f-hint">{t('geo.hole.localPlaneHint')}</p>
        </>
      )}
      <CheckField label={t('geo.hole.flush')} checked={local || !!op.flush_entry} disabled={local} onChange={(v) => set<HoleOp>((o) => ({ ...o, flush_entry: v || null }))} />
      <p className="ws-f-hint">{t('geo.hole.flushHint')}</p>
    </>
  );
}
