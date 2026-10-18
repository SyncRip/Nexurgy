// Sheet border, title block and BOM table in model space — the same layout the server writes into the DXF
// (server/app/cad/drafting.py _TITLE_ROWS / BOM_WIDTHS), so the preview matches the downloaded file.

import type { DraftSheet, Pt } from '../../api/drafting';
import { fmtNum } from './geometry';

export const FRAME_MARGIN = 10;
export const TITLE_W = 180;
export const TITLE_ROW_H = 8;
export const TITLE_ROWS: [number, string, string][][] = [
  [
    [60, 'company', '单位 COMPANY'],
    [60, 'project', '项目 PROJECT'],
    [60, 'units', '单位制 UNITS'],
  ],
  [
    [45, 'drawn_by', '设计 DRAWN'],
    [45, 'checked_by', '审核 CHECKED'],
    [45, 'date', '日期 DATE'],
    [45, 'revision', '版次 REV'],
  ],
  [
    [60, 'material', '材料 MATERIAL'],
    [40, 'scale', '比例 SCALE'],
    [40, 'sheet', '张次 SHEET'],
    [40, 'weight', '重量 WEIGHT'],
  ],
  [
    [110, 'title', '名称 TITLE'],
    [70, 'drawing_number', '图号 DWG NO.'],
  ],
];
export const TITLE_H = TITLE_ROW_H * TITLE_ROWS.length;
export const BOM_ROW_H = 7;
export const BOM_WIDTHS: Record<string, number> = { item: 12, number: 38, name: 50, qty: 12, material: 34, note: 34 };
export const BOM_LABELS: Record<string, string> = { item: '序号', number: '代号', name: '名称', qty: '数量', material: '材料', note: '备注' };

export function scaleText(scale: number): string {
  return scale >= 1 ? `${fmtNum(scale, 3)}:1` : `1:${fmtNum(1 / scale, 3)}`;
}

export interface FrameText {
  at: Pt;
  text: string;
  height: number;
  role: 'label' | 'value';
  field?: string;
}

export interface FrameGeometry {
  border: Pt[];
  inner: Pt[];
  lines: [Pt, Pt][];
  texts: FrameText[];
}

/** Border, title block grid / texts and BOM table of a sheet, in model millimetres. */
export function frameGeometry(sheet: DraftSheet): FrameGeometry {
  const k = 1 / sheet.scale;
  const [ox, oy] = sheet.origin;
  const P = (x: number, y: number): Pt => [ox + x * k, oy + y * k];
  const w = sheet.width;
  const h = sheet.height;
  const m = FRAME_MARGIN;
  const lines: [Pt, Pt][] = [];
  const texts: FrameText[] = [];
  const x0 = w - m - TITLE_W;
  const y0 = m;
  const fields: Record<string, string> = { scale: scaleText(sheet.scale), units: 'mm', ...(sheet.title_block as Record<string, string>) };
  lines.push([P(x0, y0 + TITLE_H), P(x0 + TITLE_W, y0 + TITLE_H)], [P(x0, y0), P(x0, y0 + TITLE_H)]);
  TITLE_ROWS.forEach((row, ri) => {
    const yb = y0 + ri * TITLE_ROW_H;
    if (ri) lines.push([P(x0, yb), P(x0 + TITLE_W, yb)]);
    let xc = x0;
    row.forEach(([cw, key, label], ci) => {
      if (ci) lines.push([P(xc, yb), P(xc, yb + TITLE_ROW_H)]);
      texts.push({ at: P(xc + 1, yb + TITLE_ROW_H - 2.6), text: label, height: 1.8 * k, role: 'label' });
      const value = fields[key];
      if (value) texts.push({ at: P(xc + 1, yb + 1.2), text: value, height: (key === 'title' || key === 'drawing_number' ? 3 : 2.5) * k, role: 'value', field: key });
      xc += cw;
    });
  });
  if (sheet.bom.length) {
    const cols = sheet.bom_columns.filter((c) => c in BOM_WIDTHS);
    const widths = cols.map((c) => BOM_WIDTHS[c]);
    const total = widths.reduce((a, b) => a + b, 0);
    const xb = x0 + TITLE_W - total;
    const yb = y0 + TITLE_H;
    const rows = sheet.bom.length + 1;
    for (let r = 0; r <= rows; r++) lines.push([P(xb, yb + r * BOM_ROW_H), P(xb + total, yb + r * BOM_ROW_H)]);
    let xc = xb;
    for (const cw of [0, ...widths]) {
      xc += cw;
      lines.push([P(xc, yb), P(xc, yb + rows * BOM_ROW_H)]);
    }
    xc = xb;
    cols.forEach((c, i) => {
      texts.push({ at: P(xc + 1, yb + 2), text: BOM_LABELS[c] ?? c, height: 2.5 * k, role: 'label' });
      sheet.bom.forEach((row, ri) => {
        if (row[c]) texts.push({ at: P(xc + 1, yb + (ri + 1) * BOM_ROW_H + 2), text: row[c], height: 2.5 * k, role: 'value', field: `bom.${ri + 1}.${c}` });
      });
      xc += widths[i];
    });
  }
  return {
    border: [P(0, 0), P(w, 0), P(w, h), P(0, h)],
    inner: [P(m, m), P(w - m, m), P(w - m, h - m), P(m, h - m)],
    lines,
    texts,
  };
}
