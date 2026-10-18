// v0.4 note: retained only because three/geometry.ts, three/viewer.ts and three/hero.ts
// (the landing hero and the upcoming viewer task) import it. Not used by v0.4 pages.

// Parametric template `gear_bracket@0.3` (PR-2041). The same deterministic
// function feeds the 3D viewer, the 2D drawing generator, planning and costing.
// It is a browser-side analytic model (geometry level L1); the kernel-backed
// B-rep version (L2) is produced by the CAD worker, which this prototype does
// not include.

import type { FeatureInstance } from './engineering';
import type { BracketParams, MaterialId, ParamKey } from './types';

export interface ParamMeta {
  key: ParamKey;
  label: string;
  group: '外形' | '轴承孔' | '腔' | '安装孔' | '筋';
  min: number;
  max: number;
  step: number;
  /** Frozen = functional interface agreed with the customer; editing needs an envelope change. */
  frozen?: boolean;
  hint?: string;
}

export const PARAM_META: ParamMeta[] = [
  { key: 'length', label: '总长 L', group: '外形', min: 120, max: 180, step: 1 },
  { key: 'baseDepth', label: '底板宽 D', group: '外形', min: 60, max: 90, step: 1 },
  { key: 'baseThk', label: '底板厚 T', group: '外形', min: 10, max: 16, step: 0.5 },
  { key: 'wallHeight', label: '立板总高 H', group: '外形', min: 80, max: 110, step: 1 },
  { key: 'wallThk', label: '立板厚 t', group: '外形', min: 12, max: 18, step: 0.5 },
  { key: 'boreDia', label: '轴承孔径', group: '轴承孔', min: 32, max: 32, step: 1, frozen: true, hint: '6201 轴承外圈 Ø32，装配接口' },
  { key: 'centerDist', label: '中心距', group: '轴承孔', min: 39, max: 39, step: 0.5, frozen: true, hint: 'm1.5 · z20/z32 齿轮副，a = 39' },
  { key: 'boreHeight', label: '孔中心高', group: '轴承孔', min: 52, max: 52, step: 0.5, frozen: true, hint: '与电机轴线对齐' },
  { key: 'pocketLen', label: '腔长', group: '腔', min: 50, max: 80, step: 1 },
  { key: 'pocketWidth', label: '腔宽', group: '腔', min: 24, max: 40, step: 1 },
  { key: 'pocketDepth', label: '腔深', group: '腔', min: 3, max: 8, step: 0.5 },
  { key: 'pocketR', label: '腔内角 R', group: '腔', min: 1.5, max: 6, step: 0.5, hint: '清角刀径 ≈ 1.6R，决定可用设备' },
  { key: 'mountHoleDia', label: '安装孔 Ø', group: '安装孔', min: 6.6, max: 6.6, step: 0.1, frozen: true, hint: 'M6 通孔，与机架孔位配合' },
  { key: 'cboreDia', label: '沉孔 Ø', group: '安装孔', min: 11, max: 11, step: 0.5, frozen: true },
  { key: 'cboreDepth', label: '沉孔深', group: '安装孔', min: 6, max: 7, step: 0.5 },
  { key: 'ribThk', label: '筋厚', group: '筋', min: 6, max: 11, step: 0.5 },
];

export const PARAM_GROUPS: ParamMeta['group'][] = ['外形', '轴承孔', '腔', '安装孔', '筋'];

export const DEFAULT_PARAMS: BracketParams = {
  length: 140,
  baseDepth: 70,
  baseThk: 12,
  wallHeight: 90,
  wallThk: 14,
  boreDia: 32,
  centerDist: 39,
  boreHeight: 52,
  pocketLen: 70,
  pocketWidth: 34,
  pocketDepth: 6,
  pocketR: 4,
  mountHoleDia: 6.6,
  cboreDia: 11,
  cboreDepth: 6.5,
  ribThk: 10,
};

export interface MaterialSpec {
  id: MaterialId;
  label: string;
  density: number; // g/cm3
  machinability: number; // relative removal-rate factor vs 6061
}

export const MATERIALS: Record<MaterialId, MaterialSpec> = {
  'AL6061-T651': { id: 'AL6061-T651', label: '6061-T651 铝板', density: 2.7, machinability: 1 },
  'AL7075-T651': { id: 'AL7075-T651', label: '7075-T651 铝板', density: 2.81, machinability: 0.8 },
  S45C: { id: 'S45C', label: 'S45C 碳钢', density: 7.85, machinability: 0.3 },
};

export function clampParam(key: ParamKey, v: number): number {
  const m = PARAM_META.find((p) => p.key === key)!;
  const snapped = Math.round((v - m.min) / m.step) * m.step + m.min;
  return +Math.min(m.max, Math.max(m.min, snapped)).toFixed(3);
}

/** Checks rules that the parameter sliders alone cannot guarantee. */
export function validateParams(p: BracketParams): string[] {
  const errs: string[] = [];
  const freeDepth = p.baseDepth - p.wallThk;
  if (p.pocketWidth + 10 > freeDepth) errs.push(`腔宽 ${p.pocketWidth} 超出底板可用宽度 ${freeDepth - 10}`);
  if (p.pocketDepth > p.baseThk - 3) errs.push(`腔深 ${p.pocketDepth} 使腔底壁厚 < 3 mm`);
  if (p.pocketR * 2 > Math.min(p.pocketLen, p.pocketWidth)) errs.push('腔内角 R 大于腔宽的一半');
  if (p.cboreDepth >= p.baseThk - 2) errs.push('沉孔深度使孔底壁厚 < 2 mm');
  const holeX = p.length / 2 - 18;
  if (p.pocketLen / 2 + 4 > holeX - p.cboreDia / 2) errs.push('腔与安装沉孔间壁厚 < 4 mm');
  if (p.boreHeight + p.boreDia / 2 + 6 > p.wallHeight) errs.push('立板高度不足以包住轴承孔');
  if (p.length / 2 - p.ribThk - 1 < holeX + p.cboreDia / 2) errs.push('筋与后排安装沉孔干涉');
  return errs;
}

export interface Layout {
  holeX: number;
  holeZFront: number;
  holeZRear: number;
  pocketCz: number;
  wallZ0: number;
  wallZ1: number;
  ribDepth: number;
  ribHeight: number;
  ribX: number;
  tapRadius: number;
}

/** Derived placement shared by 3D and 2D so both always agree. */
export function layout(p: BracketParams): Layout {
  const wallZ0 = -p.baseDepth / 2;
  const wallZ1 = wallZ0 + p.wallThk;
  const freeZ0 = wallZ1;
  const freeZ1 = p.baseDepth / 2;
  return {
    holeX: p.length / 2 - 18,
    holeZFront: freeZ1 - 12,
    holeZRear: freeZ0 + 10,
    pocketCz: (freeZ0 + freeZ1) / 2 + 2,
    wallZ0,
    wallZ1,
    ribDepth: (p.baseDepth - p.wallThk) * 0.5,
    ribHeight: (p.wallHeight - p.baseThk) * 0.45,
    ribX: p.length / 2 - p.ribThk / 2,
    tapRadius: p.boreDia / 2 + 6,
  };
}

export function roundedRectArea(a: number, b: number, r: number) {
  return a * b - (4 - Math.PI) * r * r;
}

export interface Metrics {
  bbox: [number, number, number];
  volume: number; // mm3
  mass: number; // g
  stock: [number, number, number];
  stockVolume: number; // mm3
  stockMass: number; // kg
  envelopeVolume: number; // L-shape + ribs, no holes/pocket (mm3)
  pocketVolume: number; // mm3
  surfaceDm2: number;
}

export const STOCK_ALLOWANCE = 3; // mm per side

/**
 * Volume, mass, stock and surface. With `features` (resolved instances, see
 * domain/engineering.ts) hole volumes are summed per instance; without them the
 * template's default holes are used, so existing callers get the same numbers.
 */
export function metrics(p: BracketParams, material: MaterialId, features?: FeatureInstance[]): Metrics {
  const L = layout(p);
  const base = p.length * p.baseDepth * p.baseThk;
  const wall = p.length * (p.wallHeight - p.baseThk) * p.wallThk;
  const ribs = 2 * 0.5 * L.ribDepth * L.ribHeight * p.ribThk;
  const envelopeVolume = base + wall + ribs;
  const pocketVolume = roundedRectArea(p.pocketLen, p.pocketWidth, p.pocketR) * p.pocketDepth;
  let volume: number;
  if (features) {
    const circle = (d: number) => Math.PI * (d / 2) ** 2;
    let holes = 0;
    for (const f of features) {
      if (f.kind === 'cbore_hole') holes += circle(f.dia) * p.baseThk + (circle(f.cboreDia ?? f.dia) - circle(f.dia)) * (f.cboreDepth ?? 0);
      else if (f.kind === 'through_hole') holes += circle(f.dia) * p.baseThk;
      else if (f.kind === 'bore') holes += circle(f.dia) * p.wallThk;
      else if (f.kind === 'tapped_hole') holes += circle(f.dia) * 10;
    }
    volume = envelopeVolume - pocketVolume - holes;
  } else {
    const hole = Math.PI * (p.mountHoleDia / 2) ** 2 * p.baseThk;
    const cbore = Math.PI * ((p.cboreDia / 2) ** 2 - (p.mountHoleDia / 2) ** 2) * p.cboreDepth;
    const bores = 2 * Math.PI * (p.boreDia / 2) ** 2 * p.wallThk;
    const taps = 8 * Math.PI * 1.65 ** 2 * 10;
    volume = envelopeVolume - 4 * (hole + cbore) - pocketVolume - bores - taps;
  }
  const rho = MATERIALS[material].density;
  const a = STOCK_ALLOWANCE * 2;
  const stock: [number, number, number] = [p.length + a, p.wallHeight + a, p.baseDepth + a];
  const stockVolume = stock[0] * stock[1] * stock[2];
  const H = p.wallHeight - p.baseThk;
  const pocketPerim = 2 * (p.pocketLen + p.pocketWidth) - (8 - 2 * Math.PI) * p.pocketR;
  const surface =
    2 * p.length * p.baseDepth +
    2 * p.baseDepth * p.baseThk +
    p.length * p.baseThk +
    2 * p.length * H +
    2 * p.wallThk * H +
    p.length * p.wallThk +
    4 * 0.5 * L.ribDepth * L.ribHeight +
    2 * Math.PI * p.boreDia * p.wallThk +
    pocketPerim * p.pocketDepth;
  return {
    bbox: [p.length, p.wallHeight, p.baseDepth],
    volume,
    mass: (volume / 1000) * rho,
    stock,
    stockVolume,
    stockMass: (stockVolume / 1e6) * rho,
    envelopeVolume,
    pocketVolume,
    surfaceDm2: surface / 1e4,
  };
}

export function paramDiff(a: BracketParams, b: BracketParams) {
  return PARAM_META.filter((m) => a[m.key] !== b[m.key]).map((m) => ({
    key: m.key,
    label: m.label,
    from: a[m.key],
    to: b[m.key],
  }));
}
