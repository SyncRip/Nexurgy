// v0.4 note: retained only because three/geometry.ts, three/viewer.ts and three/hero.ts
// (the landing hero and the upcoming viewer task) import it. Not used by v0.4 pages.

// Engineering model core for the gear_bracket template (PR-2041).
//
// Every supported feature has an instance id that survives parameter edits
// (positions are re-derived from `layout`), and every 3D edit is a structured,
// validated engineering operation. A working copy is "base revision + ordered
// operations". Edits made in the UI and requests pasted in the structured-request
// entry (the contract the Agent service will use) go through the same
// validateOp / applyOp. Pure functions only: no React, no store.

import { clampParam, layout, MATERIALS, PARAM_META, validateParams } from './bracket';
import { dnum } from './format';
import type { Annotation, BracketParams, FeatureEdits, FeatureRef, MaterialId, ParamKey, Role } from './types';

export type { FeatureEdits } from './types';

export type FeatureKind = 'cbore_hole' | 'through_hole' | 'bore' | 'tapped_hole' | 'pocket';

export interface FeatureInstance {
  /** H-01 / B-01 / T-01 / P-01 / H-05 … stable across parameter edits. */
  id: string;
  kind: FeatureKind;
  /** Category link kept for the existing category-level highlighting and planning refs. */
  ref: FeatureRef;
  label: string;
  face: 'base-top' | 'wall-front';
  /** base-top: (x, z); wall-front: (x, y). Part coordinates, mm. */
  pos: [number, number];
  /** Hole diameter (tapped holes: tap drill Ø3.3; pocket: 0). */
  dia: number;
  cboreDia?: number;
  cboreDepth?: number;
  /** Pocket only: length × width. */
  size?: [number, number];
  origin: 'template' | 'op';
}

export interface DesignState {
  params: BracketParams;
  material: MaterialId;
  features: FeatureEdits;
}

export type EngineeringOp =
  | { kind: 'set_param'; key: ParamKey; value: number }
  | { kind: 'set_material'; material: MaterialId }
  | { kind: 'set_hole_dia'; featureId: string; dia: number }
  | { kind: 'add_through_hole'; x: number; z: number; dia: number }
  | { kind: 'delete_feature'; featureId: string };

export type OpSource = 'human' | 'request';

export interface OpRecord {
  opId: string;
  op: EngineeringOp;
  baseRevId: string;
  /** request = structured-request entry (same contract as the Agent service). */
  source: OpSource;
  by: Role;
  at: string;
  result: 'applied' | 'rejected';
  reason?: string;
  createdFeatureId?: string;
}

export const OP_KINDS: EngineeringOp['kind'][] = ['set_param', 'set_material', 'set_hole_dia', 'add_through_hole', 'delete_feature'];

export const OP_LABEL: Record<EngineeringOp['kind'], string> = {
  set_param: '修改参数',
  set_material: '修改材料',
  set_hole_dia: '修改孔径',
  add_through_hole: '新增通孔',
  delete_feature: '删除特征',
};

export const FEATURE_KIND_LABEL: Record<FeatureKind, string> = {
  cbore_hole: '安装沉孔',
  through_hole: '通孔',
  bore: '轴承孔',
  tapped_hole: '端盖螺孔',
  pocket: '腔',
};

/** Validation rules. Every rejection reason names the rule it came from. */
export const ENG_RULES = {
  E01: { id: 'E-01', text: '安装沉孔孔径 3 mm ≤ Ø ≤ 沉孔径 − 2 mm' },
  E02: { id: 'E-02', text: '新增通孔孔径 3–12 mm，只能放在底板上表面可用区域' },
  E03: { id: 'E-03', text: '最小壁厚 3 mm：新增孔到底板外缘、立板前面、腔轮廓（圆角按矩形近似）、筋占位与其他孔' },
  E04: { id: 'E-04', text: '轴承孔为装配接口，已冻结' },
  E05: { id: 'E-05', text: '螺纹规格变更不在本期支持范围' },
  E06: { id: 'E-06', text: '只允许删除操作新增的特征；模板特征不支持删除' },
  E07: { id: 'E-07', text: '冻结参数不可修改；数值须在模板范围内并符合步长，单位 mm' },
  E08: { id: 'E-08', text: '修改后的参数须通过模板校验' },
} as const;

export const MIN_WALL = 3;
export const CBORE_HOLE_MIN = 3;
export const THROUGH_HOLE_DIA: readonly [number, number] = [3, 12];
export const TAP_DRILL_DIA = 3.3;

const EPS = 1e-9;
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export function emptyEdits(): FeatureEdits {
  return { dia: {}, added: [], deleted: [] };
}

export function isEmptyEdits(e: FeatureEdits | undefined): boolean {
  return !e || (!Object.keys(e.dia).length && !e.added.length && !e.deleted.length);
}

export function cloneEdits(e: FeatureEdits): FeatureEdits {
  return { dia: { ...e.dia }, added: e.added.map((a) => ({ ...a })), deleted: [...e.deleted] };
}

/** Template instances at the positions given by `layout(p)`. */
export function templateFeatures(p: BracketParams): FeatureInstance[] {
  const L = layout(p);
  const cb = (id: string, where: string, x: number, z: number): FeatureInstance => ({
    id,
    kind: 'cbore_hole',
    ref: 'cbores',
    label: `安装沉孔 ${id}（${where}）`,
    face: 'base-top',
    pos: [x, z],
    dia: p.mountHoleDia,
    cboreDia: p.cboreDia,
    cboreDepth: p.cboreDepth,
    origin: 'template',
  });
  const out: FeatureInstance[] = [
    cb('H-01', '前右', L.holeX, L.holeZFront),
    cb('H-02', '前左', -L.holeX, L.holeZFront),
    cb('H-03', '后右', L.holeX, L.holeZRear),
    cb('H-04', '后左', -L.holeX, L.holeZRear),
  ];
  const bores: [string, string, number][] = [
    ['B-01', '左', -p.centerDist / 2],
    ['B-02', '右', p.centerDist / 2],
  ];
  for (const [id, where, x] of bores)
    out.push({ id, kind: 'bore', ref: 'bores', label: `轴承孔 ${id}（${where}）`, face: 'wall-front', pos: [x, p.boreHeight], dia: p.boreDia, origin: 'template' });
  let n = 1;
  for (const [bid, , bx] of bores) {
    for (let k = 0; k < 4; k++) {
      const a = Math.PI / 4 + (k * Math.PI) / 2;
      const id = `T-${String(n++).padStart(2, '0')}`;
      out.push({
        id,
        kind: 'tapped_hole',
        ref: 'taps',
        label: `端盖螺孔 ${id}（M4-6H，${bid} 周围）`,
        face: 'wall-front',
        pos: [bx + L.tapRadius * Math.cos(a), p.boreHeight + L.tapRadius * Math.sin(a)],
        dia: TAP_DRILL_DIA,
        origin: 'template',
      });
    }
  }
  out.push({ id: 'P-01', kind: 'pocket', ref: 'pocket', label: '底板腔 P-01', face: 'base-top', pos: [0, L.pocketCz], dia: 0, size: [p.pocketLen, p.pocketWidth], origin: 'template' });
  return out;
}

/** Template instances (re-placed by layout) + per-instance overrides + added holes − deleted. */
export function resolveFeatures(d: Pick<DesignState, 'params' | 'features'>): FeatureInstance[] {
  const f = d.features;
  const tpl = templateFeatures(d.params).map((x) => (f.dia[x.id] !== undefined ? { ...x, dia: f.dia[x.id] } : x));
  const del = new Set(f.deleted);
  const added = f.added
    .filter((a) => !del.has(a.id))
    .map<FeatureInstance>((a) => ({ id: a.id, kind: 'through_hole', ref: 'holes', label: `通孔 ${a.id}`, face: 'base-top', pos: [a.x, a.z], dia: a.dia, origin: 'op' }));
  const holes = tpl.filter((x) => x.kind === 'cbore_hole');
  return [...holes, ...added, ...tpl.filter((x) => x.kind !== 'cbore_hole')];
}

/** Short size text of an instance, e.g. "Ø6.6 ⌴Ø11 ↧6.5", "Ø8 through hole", "70 × 34". */
export function featureSpec(f: FeatureInstance): string {
  switch (f.kind) {
    case 'cbore_hole':
      return `Ø${dnum(f.dia)} ⌴Ø${dnum(f.cboreDia ?? 0)} ↧${dnum(f.cboreDepth ?? 0)}`;
    case 'through_hole':
      return `Ø${dnum(f.dia)} 通孔`;
    case 'bore':
      return `Ø${dnum(f.dia)}`;
    case 'tapped_hole':
      return 'M4-6H ↧10';
    case 'pocket':
      return f.size ? `${dnum(f.size[0])} × ${dnum(f.size[1])}` : '—';
  }
}

export function featurePos(f: Pick<FeatureInstance, 'face' | 'pos'>): string {
  return f.face === 'base-top' ? `x ${dnum(f.pos[0])} · z ${dnum(f.pos[1])}` : `x ${dnum(f.pos[0])} · y ${dnum(f.pos[1])}`;
}

const holeNo = (id: string) => {
  const m = id.match(/^H-(\d+)$/);
  return m ? Number(m[1]) : 0;
};

/** Next free H-xx id: one above every H id the design or `reserved` has ever used (ids are never reused). */
export function nextHoleId(d: Pick<DesignState, 'features'>, reserved: string[] = []): string {
  const used = ['H-04', ...d.features.added.map((a) => a.id), ...d.features.deleted, ...reserved];
  const n = Math.max(...used.map(holeNo)) + 1;
  return `H-${String(n).padStart(2, '0')}`;
}

/** Minimum-wall checks for operation-created holes (template holes are covered by validateParams). */
export function clearanceErrors(d: Pick<DesignState, 'params' | 'features'>): string[] {
  const p = d.params;
  const L = layout(p);
  const tag = `（${ENG_RULES.E03.id}）`;
  const msg = (id: string, what: string, gap: number) =>
    gap <= EPS ? `${id} 与${what}干涉${tag}` : `${id} 与${what}壁厚 ${dnum(gap)} mm < ${MIN_WALL} mm${tag}`;
  const holes = resolveFeatures(d).filter((f) => f.kind === 'cbore_hole' || f.kind === 'through_hole');
  const footprint = (f: FeatureInstance) => (f.kind === 'cbore_hole' ? (f.cboreDia ?? f.dia) : f.dia) / 2;
  const rects = [
    { what: '腔 P-01 ', x0: -p.pocketLen / 2, x1: p.pocketLen / 2, z0: L.pocketCz - p.pocketWidth / 2, z1: L.pocketCz + p.pocketWidth / 2 },
    { what: '右侧筋', x0: p.length / 2 - p.ribThk, x1: p.length / 2, z0: L.wallZ1, z1: L.wallZ1 + L.ribDepth },
    { what: '左侧筋', x0: -p.length / 2, x1: -p.length / 2 + p.ribThk, z0: L.wallZ1, z1: L.wallZ1 + L.ribDepth },
  ];
  const errs: string[] = [];
  holes.forEach((h, i) => {
    if (h.origin !== 'op') return;
    const [x, z] = h.pos;
    const r = h.dia / 2;
    const edge = Math.min(p.length / 2 - Math.abs(x), p.baseDepth / 2 - z) - r;
    if (edge < MIN_WALL - EPS) errs.push(edge <= EPS ? `${h.id} 超出底板${tag}` : `${h.id} 距底板外缘 ${dnum(edge)} mm < ${MIN_WALL} mm${tag}`);
    const wall = z - r - L.wallZ1;
    if (wall < MIN_WALL - EPS) errs.push(msg(h.id, '立板前面', wall));
    for (const R of rects) {
      const gap = Math.hypot(Math.max(R.x0 - x, 0, x - R.x1), Math.max(R.z0 - z, 0, z - R.z1)) - r;
      if (gap < MIN_WALL - EPS) errs.push(msg(h.id, R.what, gap));
    }
    holes.forEach((o, j) => {
      if (o === h || (o.origin === 'op' && j < i)) return; // each pair of added holes once
      const gap = Math.hypot(o.pos[0] - x, o.pos[1] - z) - r - footprint(o);
      if (gap < MIN_WALL - EPS) errs.push(msg(h.id, ` ${o.id} `, gap));
    });
  });
  return errs;
}

/** Template validation plus the minimum-wall checks of added holes. Empty = valid design. */
export function designErrors(d: Pick<DesignState, 'params' | 'features'>): string[] {
  return [...validateParams(d.params).map((e) => `${e}（${ENG_RULES.E08.id}）`), ...clearanceErrors(d)];
}

function findTarget(d: DesignState, id: unknown): { f?: FeatureInstance; err?: string } {
  if (typeof id !== 'string' || !id.trim()) return { err: '未指定目标特征' };
  const hits = resolveFeatures(d).filter((f) => f.id === id);
  if (hits.length === 1) return { f: hits[0] };
  if (hits.length > 1) return { err: `目标 ${id} 不唯一` };
  if (d.features.deleted.includes(id)) return { err: `目标特征 ${id} 已删除` };
  return { err: `目标特征 ${id} 不存在` };
}

/** Reasons the operation cannot be applied to `d`; an empty array means it passes. */
export function validateOp(d: DesignState, op: EngineeringOp): string[] {
  switch (op.kind) {
    case 'set_param': {
      const m = PARAM_META.find((x) => x.key === op.key);
      if (!m) return [`未知参数 ${String(op.key)}`];
      if (!fin(op.value)) return [`${m.label} 数值无效`];
      if (m.frozen) return [`${m.label} 为冻结参数（${m.hint ?? '装配接口'}），修改需先变更设计包络（${ENG_RULES.E07.id}）`];
      if (op.value < m.min - EPS || op.value > m.max + EPS) return [`${m.label} ${dnum(op.value)} 超出模板范围 ${dnum(m.min)}–${dnum(m.max)} mm（${ENG_RULES.E07.id}）`];
      if (Math.abs(clampParam(op.key, op.value) - op.value) > 1e-6) return [`${m.label} ${dnum(op.value)} 不在步长 ${dnum(m.step)} mm 的刻度上（${ENG_RULES.E07.id}）`];
      if (d.params[op.key] === op.value) return [`${m.label} 已是 ${dnum(op.value)}，无变化`];
      return designErrors(applyOp(d, op).state);
    }
    case 'set_material':
      if (!Object.hasOwn(MATERIALS, op.material)) return [`未知材料 ${String(op.material)}`];
      if (d.material === op.material) return [`材料已是 ${MATERIALS[op.material].label}，无变化`];
      return [];
    case 'set_hole_dia': {
      const { f, err } = findTarget(d, op.featureId);
      if (!f) return [err!];
      if (f.kind === 'bore') return [`${f.id} 轴承孔为装配接口，已冻结（${ENG_RULES.E04.id}）`];
      if (f.kind === 'tapped_hole') return [`${f.id} 螺纹规格变更不在本期支持范围（${ENG_RULES.E05.id}）`];
      if (f.kind === 'pocket') return [`${f.id} 是腔，不接受孔径操作`];
      if (!fin(op.dia)) return [`${f.id} 孔径数值无效`];
      if (f.kind === 'cbore_hole') {
        const max = (f.cboreDia ?? 0) - 2;
        if (op.dia < CBORE_HOLE_MIN - EPS || op.dia > max + EPS) return [`${f.id} 孔径 Ø${dnum(op.dia)} 超出允许范围 Ø${dnum(CBORE_HOLE_MIN)}–Ø${dnum(max)}（${ENG_RULES.E01.id}）`];
      } else if (op.dia < THROUGH_HOLE_DIA[0] - EPS || op.dia > THROUGH_HOLE_DIA[1] + EPS) {
        return [`${f.id} 孔径 Ø${dnum(op.dia)} 超出允许范围 Ø${THROUGH_HOLE_DIA[0]}–Ø${THROUGH_HOLE_DIA[1]}（${ENG_RULES.E02.id}）`];
      }
      if (Math.abs(f.dia - op.dia) < EPS) return [`${f.id} 孔径已是 Ø${dnum(op.dia)}，无变化`];
      return designErrors(applyOp(d, op).state);
    }
    case 'add_through_hole': {
      if (!fin(op.x) || !fin(op.z)) return ['通孔位置数值无效'];
      if (!fin(op.dia)) return ['通孔孔径数值无效'];
      if (op.dia < THROUGH_HOLE_DIA[0] - EPS || op.dia > THROUGH_HOLE_DIA[1] + EPS)
        return [`通孔孔径 Ø${dnum(op.dia)} 超出允许范围 Ø${THROUGH_HOLE_DIA[0]}–Ø${THROUGH_HOLE_DIA[1]}（${ENG_RULES.E02.id}）`];
      return designErrors(applyOp(d, op).state);
    }
    case 'delete_feature': {
      const { f, err } = findTarget(d, op.featureId);
      if (!f) return [err!];
      if (f.origin !== 'op') return [`${f.id} 为模板特征，不支持删除（${ENG_RULES.E06.id}）`];
      return [];
    }
    default:
      return [`不支持的操作 ${(op as { kind?: unknown }).kind as string}`];
  }
}

/**
 * Applies an operation that passed validateOp. `createdId` replays a recorded
 * add_through_hole with its original id (redo, rebuilding a working copy).
 */
export function applyOp(d: DesignState, op: EngineeringOp, createdId?: string): { state: DesignState; createdFeatureId?: string } {
  switch (op.kind) {
    case 'set_param':
      return { state: { ...d, params: { ...d.params, [op.key]: op.value } } };
    case 'set_material':
      return { state: { ...d, material: op.material } };
    case 'set_hole_dia': {
      const added = d.features.added.find((a) => a.id === op.featureId);
      if (added) return { state: { ...d, features: { ...d.features, added: d.features.added.map((a) => (a.id === op.featureId ? { ...a, dia: op.dia } : a)) } } };
      const dia = { ...d.features.dia };
      if (op.dia === d.params.mountHoleDia) delete dia[op.featureId];
      else dia[op.featureId] = op.dia;
      return { state: { ...d, features: { ...d.features, dia } } };
    }
    case 'add_through_hole': {
      const id = createdId ?? nextHoleId(d);
      return { state: { ...d, features: { ...d.features, added: [...d.features.added, { id, x: op.x, z: op.z, dia: op.dia }] } }, createdFeatureId: id };
    }
    case 'delete_feature': {
      const dia = { ...d.features.dia };
      delete dia[op.featureId];
      return { state: { ...d, features: { ...d.features, dia, deleted: [...d.features.deleted, op.featureId] } } };
    }
  }
}

/** Chinese one-line summary with the old → new value, e.g. "H-01 hole diameter Ø6.6 → Ø8". */
export function describeOp(op: EngineeringOp, d: DesignState, createdId?: string): string {
  switch (op.kind) {
    case 'set_param': {
      const m = PARAM_META.find((x) => x.key === op.key);
      const from = d.params[op.key];
      return `${m?.label ?? op.key} ${from !== undefined ? dnum(from) : '?'} → ${fin(op.value) ? dnum(op.value) : '?'} mm`;
    }
    case 'set_material':
      return `材料 ${MATERIALS[d.material]?.label ?? d.material} → ${MATERIALS[op.material]?.label ?? op.material}`;
    case 'set_hole_dia': {
      const f = resolveFeatures(d).find((x) => x.id === op.featureId);
      return `${op.featureId} 孔径 ${f ? `Ø${dnum(f.dia)}` : '?'} → Ø${fin(op.dia) ? dnum(op.dia) : '?'}`;
    }
    case 'add_through_hole':
      return `新增通孔 ${createdId ?? nextHoleId(d)} Ø${dnum(op.dia)}（x ${dnum(op.x)}, z ${dnum(op.z)}）`;
    case 'delete_feature': {
      const f = resolveFeatures(d).find((x) => x.id === op.featureId);
      return `删除 ${f ? f.label : op.featureId}`;
    }
  }
}

/** Summary without the old value (for log entries whose before-state is not at hand). */
export function opBrief(op: EngineeringOp): string {
  switch (op.kind) {
    case 'set_param':
      return `${PARAM_META.find((m) => m.key === op.key)?.label ?? op.key} → ${dnum(op.value)} mm`;
    case 'set_material':
      return `材料 → ${MATERIALS[op.material]?.label ?? op.material}`;
    case 'set_hole_dia':
      return `${op.featureId} 孔径 → Ø${dnum(op.dia)}`;
    case 'add_through_hole':
      return `新增通孔 Ø${dnum(op.dia)}（x ${dnum(op.x)}, z ${dnum(op.z)}）`;
    case 'delete_feature':
      return `删除 ${op.featureId}`;
  }
}

const GROUP_TARGETS: Record<string, FeatureKind[]> = { 轴承孔: ['bore', 'tapped_hole'], 腔: ['pocket'], 安装孔: ['cbore_hole'] };

/** Feature instances an operation touches (preview highlight). */
export function opTargets(op: EngineeringOp, d: DesignState, createdId?: string): string[] {
  switch (op.kind) {
    case 'set_hole_dia':
    case 'delete_feature':
      return [op.featureId];
    case 'add_through_hole':
      return [createdId ?? nextHoleId(d)];
    case 'set_param': {
      const kinds = GROUP_TARGETS[PARAM_META.find((m) => m.key === op.key)?.group ?? ''] ?? [];
      return resolveFeatures(d)
        .filter((f) => kinds.includes(f.kind))
        .map((f) => f.id);
    }
    case 'set_material':
      return [];
  }
}

export interface DiffRow {
  kind: 'param' | 'material' | 'feature';
  change: 'added' | 'removed' | 'modified';
  /** ParamKey, 'material' or a feature id. */
  key: string;
  /** For modified features: which attribute changed. */
  attr?: 'dia' | 'pos' | 'size';
  label: string;
  from: string;
  to: string;
  featureIds: string[];
}

/** Object-level diff of `next` against `base`: parameters, material, features added / removed / modified. */
export function diffDesign(base: DesignState, next: DesignState): DiffRow[] {
  const rows: DiffRow[] = [];
  for (const m of PARAM_META)
    if (base.params[m.key] !== next.params[m.key])
      rows.push({ kind: 'param', change: 'modified', key: m.key, label: m.label, from: dnum(base.params[m.key]), to: dnum(next.params[m.key]), featureIds: [] });
  if (base.material !== next.material)
    rows.push({ kind: 'material', change: 'modified', key: 'material', label: '材料', from: MATERIALS[base.material].label, to: MATERIALS[next.material].label, featureIds: [] });
  const a = resolveFeatures(base);
  const b = resolveFeatures(next);
  const am = new Map(a.map((f) => [f.id, f]));
  const bm = new Map(b.map((f) => [f.id, f]));
  const feat = (f: FeatureInstance, change: DiffRow['change'], attr: DiffRow['attr'], from: string, to: string): DiffRow => ({
    kind: 'feature',
    change,
    key: f.id,
    attr,
    label: f.label,
    from,
    to,
    featureIds: [f.id],
  });
  for (const f of b) {
    const o = am.get(f.id);
    if (!o) {
      rows.push(feat(f, 'added', undefined, '', `${featureSpec(f)} · ${featurePos(f)}`));
      continue;
    }
    if (Math.abs(o.dia - f.dia) > EPS) rows.push(feat(f, 'modified', 'dia', `Ø${dnum(o.dia)}`, `Ø${dnum(f.dia)}`));
    if (o.size && f.size && (Math.abs(o.size[0] - f.size[0]) > EPS || Math.abs(o.size[1] - f.size[1]) > EPS)) rows.push(feat(f, 'modified', 'size', featureSpec(o), featureSpec(f)));
    if (Math.abs(o.pos[0] - f.pos[0]) > EPS || Math.abs(o.pos[1] - f.pos[1]) > EPS) rows.push(feat(f, 'modified', 'pos', featurePos(o), featurePos(f)));
  }
  for (const o of a) if (!bm.has(o.id)) rows.push(feat(o, 'removed', undefined, `${featureSpec(o)} · ${featurePos(o)}`, ''));
  return rows;
}

export interface HoleCallouts {
  /** The group of counterbored holes kept on A15 ("n×…"): the largest group with one diameter. */
  primary: { count: number; dia: number; featureIds: string[] } | null;
  /** Per-instance callouts: A15-H-xx for counterbored holes outside that group, A20-H-xx for added through holes. */
  derived: Annotation[];
  /** Feature id → id of the annotation that carries its size on the drawing. */
  byFeature: Record<string, string>;
}

/**
 * Hole callouts of a design, shared by the drawing generator and planning so both
 * use the same annotation ids. Ties between equal-sized groups prefer the
 * template diameter (`p.mountHoleDia`), then the group listed first.
 */
export function featureAnnotations(features: FeatureInstance[], p?: Pick<BracketParams, 'mountHoleDia'>): HoleCallouts {
  const cb = features.filter((f) => f.kind === 'cbore_hole');
  const groups = new Map<number, FeatureInstance[]>();
  for (const f of cb) groups.set(f.dia, [...(groups.get(f.dia) ?? []), f]);
  let best: FeatureInstance[] | null = null;
  for (const g of groups.values()) {
    if (!best || g.length > best.length || (g.length === best.length && p && g[0].dia === p.mountHoleDia && best[0].dia !== p.mountHoleDia)) best = g;
  }
  const byFeature: Record<string, string> = {};
  const derived: Annotation[] = [];
  for (const f of cb) {
    if (best?.includes(f)) {
      byFeature[f.id] = 'A15';
      continue;
    }
    const id = `A15-${f.id}`;
    byFeature[f.id] = id;
    derived.push({ id, kind: 'dim-diameter', label: `安装沉孔 ${f.id}`, featureRef: 'cbores', featureId: f.id, tol: { kind: 'none' }, text: featureSpec(f), place: 'top-feature' });
  }
  for (const f of features.filter((x) => x.kind === 'through_hole')) {
    const id = `A20-${f.id}`;
    byFeature[f.id] = id;
    derived.push({ id, kind: 'dim-diameter', label: `通孔 ${f.id}`, featureRef: 'holes', featureId: f.id, tol: { kind: 'none' }, text: featureSpec(f), place: 'top-feature' });
  }
  return { primary: best ? { count: best.length, dia: best[0].dia, featureIds: best.map((f) => f.id) } : null, derived, byFeature };
}

/** "H-02–H-04" for consecutive ids, otherwise a comma list. */
export function idRange(ids: string[]): string {
  if (ids.length <= 1) return ids.join('');
  const m = ids.map((id) => id.match(/^([A-Z])-(\d+)$/));
  const seq = m.every((x, i) => x && x[1] === m[0]![1] && (i === 0 || Number(x[2]) === Number(m[i - 1]![2]) + 1));
  return seq && ids.length > 2 ? `${ids[0]}–${ids[ids.length - 1]}` : ids.join('、');
}

// ── Structured request (the contract shared with the Agent service)

/** Prefix of operation ids the store assigns to edits made in the UI (UI-1, UI-2 …); requests may not use it. */
export const UI_OP_ID = /^UI-/i;

export interface EngRequest {
  opId: string;
  baseRevId: string;
  op: EngineeringOp;
}

/**
 * Parses a pasted request. Shape errors (not JSON, unknown operation, wrong field
 * types, unit other than mm, missing opId/baseRevId, an opId with the reserved
 * UI- prefix) are reported here; whether
 * the operation is allowed is decided by validateOp in the store.
 */
export function parseEngRequest(text: string): { ok: true; req: EngRequest } | { ok: false; errors: string[] } {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    return { ok: false, errors: [`不是有效的 JSON：${(e as Error).message}`] };
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { ok: false, errors: ['请求必须是一个 JSON 对象'] };
  const r = raw as Record<string, unknown>;
  const o = (r.op && typeof r.op === 'object' && !Array.isArray(r.op) ? r.op : r) as Record<string, unknown>;
  const errors: string[] = [];
  for (const u of [r.unit, r.units, o.unit, o.units]) if (u !== undefined && u !== 'mm') errors.push(`单位必须为 mm（收到 ${JSON.stringify(u)}）`);
  const opId = r.opId;
  const baseRevId = r.baseRevId;
  if (typeof opId !== 'string' || !opId.trim() || opId.length > 64) errors.push('缺少 opId（1–64 个字符，用于防止重复提交）');
  else if (UI_OP_ID.test(opId.trim())) errors.push(`opId ${opId.trim()} 以保留前缀 “UI-” 开头：该前缀用于页面上的编辑操作，请换一个 opId`);
  if (typeof baseRevId !== 'string' || !baseRevId.trim()) errors.push('缺少 baseRevId（请求必须声明基于哪个版本）');
  const kind = o.kind;
  const num = (k: string) => {
    if (!fin(o[k])) errors.push(`${String(kind)}.${k} 必须是数字（mm）`);
    return o[k] as number;
  };
  const str = (k: string) => {
    if (typeof o[k] !== 'string' || !(o[k] as string).trim()) errors.push(`${String(kind)}.${k} 必须是非空字符串`);
    return o[k] as string;
  };
  let op: EngineeringOp | null = null;
  switch (kind) {
    case 'set_param': {
      const key = str('key');
      const value = num('value');
      if (typeof key === 'string' && !PARAM_META.some((m) => m.key === key)) errors.push(`未知参数 ${key}（可用：${PARAM_META.map((m) => m.key).join('、')}）`);
      op = { kind, key: key as ParamKey, value };
      break;
    }
    case 'set_material': {
      const material = str('material');
      if (typeof material === 'string' && !Object.hasOwn(MATERIALS, material)) errors.push(`未知材料 ${material}（可用：${Object.keys(MATERIALS).join('、')}）`);
      op = { kind, material: material as MaterialId };
      break;
    }
    case 'set_hole_dia':
      op = { kind, featureId: str('featureId'), dia: num('dia') };
      break;
    case 'add_through_hole':
      op = { kind, x: num('x'), z: num('z'), dia: num('dia') };
      break;
    case 'delete_feature':
      op = { kind, featureId: str('featureId') };
      break;
    default:
      errors.push(`不支持的操作 ${kind === undefined ? '（缺少 kind）' : JSON.stringify(kind)}；本期支持：${OP_KINDS.join('、')}`);
  }
  if (errors.length || !op) return { ok: false, errors };
  return { ok: true, req: { opId: (opId as string).trim(), baseRevId: (baseRevId as string).trim(), op } };
}

export function exampleRequest(baseRevId: string): string {
  return JSON.stringify({ opId: 'REQ-001', baseRevId, unit: 'mm', op: { kind: 'set_hole_dia', featureId: 'H-01', dia: 8 } }, null, 2);
}
