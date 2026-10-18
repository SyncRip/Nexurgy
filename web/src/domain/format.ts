// v0.4 note: retained only because three/geometry.ts, three/viewer.ts and three/hero.ts
// (the landing hero and the upcoming viewer task) import it. Not used by v0.4 pages.

import type { MaterialId, ModelRevision, TolSpec } from './types';

export const yuan = (fen: number) =>
  `¥${(fen / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const num = (v: number, d = 1) =>
  (Math.round(v * 10 ** d) / 10 ** d).toLocaleString('zh-CN', { maximumFractionDigits: d });

/** Drawing-style number: drop trailing zeros, keep up to 2 decimals. */
export const dnum = (v: number) => String(+v.toFixed(2));

export function tolText(t: TolSpec): string {
  switch (t.kind) {
    case 'none':
      return '';
    case 'sym':
      return ` ±${dnum(t.value)}`;
    case 'dev':
      return ` ${t.upper >= 0 ? '+' : ''}${dnum(t.upper)}/${t.lower >= 0 ? '+' : ''}${dnum(t.lower)}`;
    case 'fit':
      return ` ${t.code}`;
  }
}

export const shortTime = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

export const ROLE_LABEL = {
  design: '设计工程师',
  process: '工艺工程师',
  quote: '报价负责人',
  viewer: '只读查看',
  system: '系统',
} as const;

/** Local calendar date YYYY-MM-DD (toISOString would give the UTC date). */
export function localDate(d = new Date()) {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Local calendar date `days` after a YYYY-MM-DD date. */
export function addDays(iso: string, days: number) {
  const [y, m, d] = iso.split('-').map(Number);
  return localDate(new Date(y, m - 1, d + days));
}

/**
 * Number in a dimension text, shared by the 2D sheet and planning so both judge
 * overrides the same way: "2×Ø32 H7" → 32, "70 +0.1" → 70, "R4" → 4. null if none.
 */
export function parseDimNumber(text: string): number | null {
  const t = text
    .trim()
    .replace(/^\d+\s*[×xX*]\s*/, '')
    .replace(/^[Ø⌀∅RrMm]\s*/, '');
  const m = t.match(/\d+(?:\.\d+)?|\.\d+/);
  if (!m) return null;
  const v = Number(m[0]);
  return Number.isFinite(v) ? v : null;
}

/** Normalised alloy of a material text or MaterialId: "AL6061-T651" / "6061-T6" → "6061", "S45C" → "S45C". */
export function alloyOf(text: string): string {
  const s = text.toUpperCase().replace(/\s/g, '');
  return s.match(/\d{4}/)?.[0] ?? s.match(/S45C/)?.[0] ?? s;
}

/** Title-block text for a model material: 6061-T651 / 7075-T651 / S45C. */
export const materialDrawingText = (id: MaterialId) => id.replace(/^AL/, '');

/** Geometry level with the seed caveat, e.g. "L2 (seed record, not rechecked)". */
export function levelLabel(m: Pick<ModelRevision, 'geometryLevel' | 'source' | 'kernelNote'>): string {
  return m.source === 'seed' || m.kernelNote?.startsWith('种子') ? `${m.geometryLevel}（种子记录，未复核）` : m.geometryLevel;
}
