// Pure helpers of the assembly drawings tab (W5): enum guards (unknown values never read as success), the generate
// dialog's config, the layout overlay edit list, page / file naming.

import type { AssemblyDrawingConfig, Axis, DrawingFormat, DrawingPage, KeyDimension, OverlayEdit, ViewName } from '../../api/assemblyDrawings';
import { DRAWING_FORMATS } from '../../api/assemblyDrawings';
import type { CheckStatus } from '../../api/engineering';

const CHECKS: CheckStatus[] = ['not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown'];

/** An unknown check status shows as "unknown", never as passed. */
export function safeCheck(status: string | null | undefined): CheckStatus {
  return CHECKS.includes(status as CheckStatus) ? (status as CheckStatus) : 'unknown';
}

/** Completeness: anything but an explicit "complete" is partial. */
export function safeCompleteness(value: string | null | undefined): 'complete' | 'partial' {
  return value === 'complete' ? 'complete' : 'partial';
}

/** Required formats a page does not have (a failed DWG stays missing; nothing stands in for it). */
export function missingFormats(page: Pick<DrawingPage, 'files'>): DrawingFormat[] {
  const have = new Set(page.files.map((f) => f.format));
  return DRAWING_FORMATS.filter((f) => !have.has(f));
}

export function pageFileName(stem: string, page: Pick<DrawingPage, 'key'>, format: DrawingFormat): string {
  return `${stem}_${page.key}.${format}`.replace(/[^A-Za-z0-9._-]+/g, '-');
}

// --------------------------------------------------------------------------- generate dialog

export type SectionMode = 'auto' | 'none' | 'plane';
export type DatumChoice = 'origin' | 'bbox.min' | 'bbox.max' | 'bbox.center';
export const DATUMS: DatumChoice[] = ['origin', 'bbox.min', 'bbox.center', 'bbox.max'];

export interface KeyDimRow {
  label: string;
  axis: Axis;
  a: string; // occurrence path joined with "/"
  aDatum: DatumChoice;
  b: string;
  bDatum: DatumChoice;
}

export interface GenerateForm {
  views: ViewName[];
  section: SectionMode;
  plane: 'XZ' | 'YZ';
  offset: string;
  bomView: 'flattened' | 'hierarchical';
  keyDims: KeyDimRow[];
  notes: string;
}

export const DEFAULT_FORM: GenerateForm = {
  views: ['front', 'top', 'right'],
  section: 'auto',
  plane: 'XZ',
  offset: '0',
  bomView: 'flattened',
  keyDims: [],
  notes: '',
};

export const parseNotes = (text: string): string[] =>
  text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);

/** Why the form cannot be sent (message key suffix), or null. */
export function formProblem(form: GenerateForm): 'views' | 'offset' | 'keyDim' | null {
  if (!form.views.length) return 'views';
  if (form.section === 'plane' && !Number.isFinite(Number(form.offset.trim() === '' ? NaN : form.offset))) return 'offset';
  if (form.keyDims.some((k) => !k.a || !k.b || (k.a === k.b && k.aDatum === k.bDatum))) return 'keyDim';
  return null;
}

/** The AssemblyDrawingConfig of the dialog (only what the user chose; the server fills the rest). */
export function buildConfig(form: GenerateForm): AssemblyDrawingConfig {
  const cfg: AssemblyDrawingConfig = { views: [...form.views], bom_view: form.bomView };
  cfg.section = form.section === 'plane' ? { plane: form.plane, offset: Number(form.offset) } : form.section;
  const notes = parseNotes(form.notes);
  if (notes.length) cfg.notes = notes;
  if (form.keyDims.length) {
    cfg.key_dimensions = form.keyDims.map(
      (k, i): KeyDimension => ({
        id: `K${i + 1}`,
        label: k.label.trim(),
        axis: k.axis,
        a: { path: k.a.split('/'), datum: k.aDatum },
        b: { path: k.b.split('/'), datum: k.bDatum },
      }),
    );
  }
  return cfg;
}

// --------------------------------------------------------------------------- layout overlay

export interface MoveRow {
  kind: 'balloon' | 'dimension';
  ref: string; // balloon item | dimension id
  occurrence?: string[];
  /** What the row points at (shown under its name). */
  label?: string;
  dx: string;
  dy: string;
}

const num = (s: string): number => (s.trim() === '' ? 0 : Number(s));

/** Rows with a non-numeric offset (shown as errors). */
export function badRows(rows: MoveRow[]): number[] {
  return rows.flatMap((r, i) => (Number.isFinite(num(r.dx)) && Number.isFinite(num(r.dy)) ? [] : [i]));
}

/** The overlay edits of the editor: moves that actually move, then the notes. Layout only, never a value. */
export function overlayEdits(page: string, rows: MoveRow[], notes: string[]): OverlayEdit[] {
  const edits: OverlayEdit[] = [];
  for (const r of rows) {
    const dx = num(r.dx);
    const dy = num(r.dy);
    if (!Number.isFinite(dx) || !Number.isFinite(dy) || (dx === 0 && dy === 0)) continue;
    if (r.kind === 'balloon') edits.push({ op: 'move_balloon', page, item: r.ref, ...(r.occurrence ? { occurrence: r.occurrence } : {}), by: [dx, dy] });
    else edits.push({ op: 'move_dimension_text', page, dimension: r.ref, by: [dx, dy] });
  }
  for (const n of notes.map((s) => s.trim()).filter(Boolean)) edits.push({ op: 'add_note', page, text: n });
  return edits;
}

/** Pages a layout overlay can edit (it rewrites the DXF and SVG of the page). */
export function editablePages(pages: DrawingPage[]): DrawingPage[] {
  return pages.filter((p) => p.files.some((f) => f.format === 'dxf') && p.files.some((f) => f.format === 'svg'));
}
