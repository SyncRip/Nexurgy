// Pure helpers for the CAD AI surfaces (no React, unit-tested in status.test.ts):
// geometry-check / annotation / DWG status mapping, requirement facts and basis,
// drawing package pages and formats, error-code families and the AI-call ledger.
//
// Three verdicts are kept apart everywhere (docs/development/cad-ai/acceptance §1, §6):
// the independent geometry check of the STEP, the drawing annotation / DWG checks,
// and business acceptance (never implied by either). Unknown values stay unknown.

import type {
  AiCallRecord,
  CheckItem,
  CheckSummary,
  ClarificationGap,
  CoverageEntry,
  Drawing,
  DrawingBrief,
  DrawingPackageFile,
  DrawingPage,
  NotesCoverage,
  Requirement,
  RequirementContour,
  RequirementEvidence,
} from '../../api/types';

// ─── geometry check ────────────────────────────────────────────────────────

/**
 * passed / failed / not_evaluated, `skipped_turbo` (W7: a Turbo revision whose check was skipped — never a verdict),
 * or `none` when the revision has no requirement contract (manual, import, older).
 */
export type GeomCheck = 'passed' | 'failed' | 'not_evaluated' | 'skipped_turbo' | 'none';

export function geomCheckState(status: string | null | undefined): GeomCheck {
  if (status === null || status === undefined || status === '') return 'none';
  if (status === 'passed' || status === 'failed' || status === 'skipped_turbo') return status;
  return 'not_evaluated';
}

export type StatusTone = 'ok' | 'err' | 'warn' | 'pending';

export function geomCheckTone(s: GeomCheck): StatusTone {
  return s === 'passed' ? 'ok' : s === 'failed' ? 'err' : s === 'skipped_turbo' ? 'warn' : 'pending';
}

/** Item counts of a check summary, with every status present (missing = 0). */
export function summaryCounts(c: Pick<CheckSummary, 'items'> | null | undefined): { passed: number; failed: number; other: number; total: number } {
  const items = c?.items ?? {};
  let passed = 0;
  let failed = 0;
  let other = 0;
  for (const [k, v] of Object.entries(items)) {
    const n = typeof v === 'number' && Number.isFinite(v) ? v : 0;
    if (k === 'passed') passed += n;
    else if (k === 'failed') failed += n;
    else other += n;
  }
  return { passed, failed, other, total: passed + failed + other };
}

/** Check items grouped by requirement id (items without one under ""). */
export function itemsByRequirement(items: readonly CheckItem[] | null | undefined): Map<string, CheckItem[]> {
  const out = new Map<string, CheckItem[]>();
  for (const i of items ?? []) {
    const k = i.requirement_id ?? '';
    out.set(k, [...(out.get(k) ?? []), i]);
  }
  return out;
}

// ─── requirements ──────────────────────────────────────────────────────────

/** How a requirement's values are known: all stated in a source, some derived, or some assumed (asked first when critical). */
export type Basis = 'stated' | 'derived' | 'assumed';

export function requirementBasis(r: Pick<Requirement, 'derived' | 'assumed'>): Basis {
  if (r.assumed?.length) return 'assumed';
  if (r.derived?.length) return 'derived';
  return 'stated';
}

const round = (v: number, d = 3) => {
  const f = 10 ** d;
  const x = Math.round(v * f) / f;
  return Object.is(x, -0) ? 0 : x;
};

/** Short number text (≤ 3 decimals), "?" when unknown. */
export function n(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(round(v)) : '?';
}

export function vecText(v: readonly (number | null)[] | null | undefined): string {
  return v && v.length ? `(${v.map((x) => n(x)).join(', ')})` : '?';
}

/** Language-neutral contour text: "▭ 40 × 20 @ (0, 0) R2", "Ø20 @ (10, 5)", "⬠ 6 pts". */
export function contourText(c: RequirementContour | null | undefined): string {
  if (!c) return '?';
  switch (c.kind) {
    case 'rect':
      return `▭ ${n(c.size?.[0])} × ${n(c.size?.[1])} @ ${vecText(c.center)}${c.corner_radius ? ` R${n(c.corner_radius)}` : ''}`;
    case 'circle':
      return `Ø${n(c.diameter)} @ ${vecText(c.center)}`;
    case 'polygon':
      return `⬠ ${c.points?.length ?? 0} pts`;
    default:
      return '?';
  }
}

/** Fact keys the requirement panel translates (cad.req.fact.<key>). */
export type FactKey =
  | 'size'
  | 'min'
  | 'diameter'
  | 'count'
  | 'through'
  | 'depth'
  | 'counterbore'
  | 'countersink'
  | 'thread'
  | 'direction'
  | 'contour'
  | 'entry_z'
  | 'z_range'
  | 'outer'
  | 'voids'
  | 'category'
  | 'text';

export interface Fact {
  key: FactKey;
  value: string;
  /** Through / blind are words, not numbers: the panel translates value "yes" / "no". */
  bool?: boolean;
}

/** Key values of one requirement, in reading order (numbers in mm). */
export function requirementFacts(r: Requirement): Fact[] {
  switch (r.kind) {
    case 'envelope': {
      const out: Fact[] = [{ key: 'size', value: (r.size ?? []).map((x) => n(x)).join(' × ') }];
      if (r.min) out.push({ key: 'min', value: vecText(r.min) });
      return out;
    }
    case 'hole': {
      const out: Fact[] = [
        { key: 'diameter', value: `Ø${n(r.diameter)}` },
        { key: 'count', value: String(r.entries?.length ?? 0) },
        { key: 'through', value: r.through ? 'yes' : 'no', bool: r.through },
      ];
      if (!r.through) out.push({ key: 'depth', value: n(r.depth) });
      if (r.counterbore) out.push({ key: 'counterbore', value: `Ø${n(r.counterbore.diameter)} ↧${n(r.counterbore.depth)}` });
      if (r.countersink) out.push({ key: 'countersink', value: `Ø${n(r.countersink.diameter)} ${n(r.countersink.angle ?? 90)}°` });
      if (r.thread) out.push({ key: 'thread', value: r.thread });
      if (r.direction && !(r.direction[0] === 0 && r.direction[1] === 0 && r.direction[2] === -1)) out.push({ key: 'direction', value: vecText(r.direction) });
      return out;
    }
    case 'pocket': {
      const out: Fact[] = [
        { key: 'contour', value: contourText(r.contour) },
        { key: 'entry_z', value: n(r.entry_z) },
        { key: 'through', value: r.through ? 'yes' : 'no', bool: r.through },
      ];
      if (!r.through) out.push({ key: 'depth', value: n(r.depth) });
      return out;
    }
    case 'band': {
      const out: Fact[] = [
        { key: 'z_range', value: `${n(r.z0)} – ${n(r.z1)}` },
        { key: 'outer', value: contourText(r.outer) },
      ];
      if (r.voids?.length) out.push({ key: 'voids', value: r.voids.map(contourText).join('; ') });
      return out;
    }
    case 'note':
      return [
        { key: 'category', value: r.category },
        { key: 'text', value: r.text },
      ];
    default:
      return [];
  }
}

/** Evidence grouped for display: literal quotes vs drawing / geometry ids. */
export function evidenceParts(ev: readonly RequirementEvidence[] | null | undefined): { quotes: RequirementEvidence[]; ids: RequirementEvidence[] } {
  const quotes: RequirementEvidence[] = [];
  const ids: RequirementEvidence[] = [];
  for (const e of ev ?? []) (e.source === 'request' || e.source === 'clarification' ? quotes : ids).push(e);
  return { quotes, ids };
}

// ─── drawing package ───────────────────────────────────────────────────────

const FORMAT_ORDER: Record<string, number> = { svg: 0, dxf: 1, dwg: 2 };

/** Pages of a drawing: the package pages, else one legacy overview page from the page-1 aliases. */
export function drawingPages(d: Pick<Drawing, 'pages' | 'svg_sha' | 'dxf_sha'>): DrawingPage[] {
  if (d.pages && d.pages.length) return [...d.pages].sort((a, b) => a.index - b.index);
  const files: DrawingPackageFile[] = [];
  if (d.svg_sha) files.push({ format: 'svg', sha256: d.svg_sha, bytes: 0, path: 'pages/p1_overview.svg' });
  if (d.dxf_sha) files.push({ format: 'dxf', sha256: d.dxf_sha, bytes: 0, path: 'pages/p1_overview.dxf' });
  return files.length ? [{ index: 1, role: 'overview', title: '', files }] : [];
}

/** True for drawings saved before the package manifest (page-1 SVG + DXF only, no checks). */
export function isLegacyDrawing(d: Pick<Drawing, 'pages' | 'manifest'>): boolean {
  return !d.manifest && !(d.pages && d.pages.length);
}

/** The requested page, else the first one; null when the drawing has no page. */
export function selectPage(pages: readonly DrawingPage[], index: number | null | undefined): DrawingPage | null {
  if (!pages.length) return null;
  return pages.find((p) => p.index === index) ?? pages[0];
}

/** The page title when it says more than the role (the server titles page 1 "OVERVIEW"); null otherwise. */
export function extraPageTitle(page: Pick<DrawingPage, 'title' | 'role'>, roleLabel: string): string | null {
  const title = (page.title ?? '').trim();
  if (!title) return null;
  const same = (a: string) => a.trim().toLowerCase() === title.toLowerCase();
  return same(page.role) || same(roleLabel) ? null : title;
}

/** Every file of a page, SVG → DXF → DWG (unknown formats last, kept). */
export function pageFiles(page: Pick<DrawingPage, 'files'> | null | undefined): DrawingPackageFile[] {
  return [...(page?.files ?? [])].sort((a, b) => (FORMAT_ORDER[a.format] ?? 9) - (FORMAT_ORDER[b.format] ?? 9));
}

export function pageFile(page: Pick<DrawingPage, 'files'> | null | undefined, format: string): DrawingPackageFile | null {
  return page?.files?.find((f) => f.format === format) ?? null;
}

/** DWG / DXF version code → release name (AC1015 = R2000 …); the code itself when unknown. */
const ACAD_RELEASES: Record<string, string> = {
  AC1009: 'R12',
  AC1012: 'R13',
  AC1014: 'R14',
  AC1015: 'R2000',
  AC1018: 'R2004',
  AC1021: 'R2007',
  AC1024: 'R2010',
  AC1027: 'R2013',
  AC1032: 'R2018',
};

export function acadRelease(code: string | null | undefined): string | null {
  if (!code) return null;
  return ACAD_RELEASES[code] ?? code;
}

/** "SVG", "DXF R2000", "DWG R2000". */
export function formatLabel(f: Pick<DrawingPackageFile, 'format' | 'version'>): string {
  const rel = acadRelease(f.version);
  return `${String(f.format).toUpperCase()}${rel ? ` ${rel}` : ''}`;
}

const SAFE = /[^A-Za-z0-9._-]+/g;

/** Download name of one page file: "<part>_R<rev>_p<page>_<role>.<ext>". */
export function pageFileName(partNumber: string, revNo: number | null | undefined, page: Pick<DrawingPage, 'index' | 'role'>, f: Pick<DrawingPackageFile, 'format'>): string {
  const base = [partNumber || 'drawing', revNo ? `R${revNo}` : '', `p${page.index}`, page.role || 'page'].filter(Boolean).join('_');
  return `${base.replace(SAFE, '-')}.${String(f.format).toLowerCase()}`;
}

/** Download name of the whole package (the server's Content-Disposition is not exposed cross-origin). */
export function packageFileName(partNumber: string, revNo: number | null | undefined, drawingId: string): string {
  const base = [partNumber || 'drawing', revNo ? `R${revNo}` : '', drawingId.slice(0, 8)].filter(Boolean).join('_');
  return `${base.replace(SAFE, '-')}_package.zip`;
}

/** skipped_turbo (W7): the annotation check of a Turbo package was skipped. */
export type AnnotationState = 'passed' | 'failed' | 'skipped_turbo' | 'unknown' | 'legacy';

export function annotationState(d: Pick<Drawing, 'checks' | 'pages' | 'manifest'> | null | undefined, brief?: Pick<DrawingBrief, 'annotation_status'> | null): AnnotationState {
  const s = d?.checks?.annotation?.status ?? brief?.annotation_status ?? null;
  if (s === 'passed' || s === 'failed' || s === 'skipped_turbo') return s;
  if (d && isLegacyDrawing(d)) return 'legacy';
  return 'unknown';
}

/** unverified (W7): a Turbo package wrote real DWG files without the semantic readback. */
export type DwgState = 'passed' | 'unverified' | 'unavailable' | 'failed' | 'unknown' | 'legacy';

export interface DwgInfo {
  state: DwgState;
  /** Release of the generated DWG files (R2000), when passed. */
  release: string | null;
  /** Why it is unavailable / failed (server text). */
  reason: string | null;
  code: string | null;
  page: number | null;
}

/** DWG output of a package: generated (and verified) / unavailable with the reason / failed with code and page. */
export function dwgInfo(d: Pick<Drawing, 'checks' | 'dwg_status' | 'pages' | 'manifest'> | null | undefined, brief?: Pick<DrawingBrief, 'dwg_status'> | null): DwgInfo {
  const c = d?.checks?.dwg ?? null;
  const status = c?.status ?? d?.dwg_status ?? brief?.dwg_status ?? null;
  const dwgFile = d?.pages?.flatMap((p) => p.files ?? []).find((f) => f.format === 'dwg');
  const base: DwgInfo = { state: 'unknown', release: null, reason: null, code: c?.code ?? null, page: typeof c?.page === 'number' ? c.page : null };
  if (status === 'passed') return { ...base, state: 'passed', release: acadRelease(dwgFile?.version ?? c?.target ?? 'AC1015') };
  if (status === 'unverified') return { ...base, state: 'unverified', release: acadRelease(dwgFile?.version ?? c?.target ?? 'AC1015') };
  if (status === 'unavailable') return { ...base, state: 'unavailable', reason: c?.reason ?? null };
  if (status === 'failed') return { ...base, state: 'failed', reason: c?.message ?? c?.reason ?? null };
  if (d && isLegacyDrawing(d)) return { ...base, state: 'legacy' };
  return base;
}

export function stateTone(s: AnnotationState | DwgState): StatusTone {
  if (s === 'passed') return 'ok';
  if (s === 'failed') return 'err';
  if (s === 'unavailable' || s === 'skipped_turbo' || s === 'unverified') return 'warn';
  return 'pending';
}

const EMPTY_COVERAGE: NotesCoverage = { covered: [], uncovered: [], unknown: [] };

/** Coverage lists of a drawing (each list always present). */
export function coverageOf(d: Pick<Drawing, 'coverage' | 'manifest'> | null | undefined): NotesCoverage {
  const c = d?.coverage ?? d?.manifest?.coverage ?? null;
  if (!c) return EMPTY_COVERAGE;
  const list = (v: unknown): CoverageEntry[] => (Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as CoverageEntry[]) : []);
  return { covered: list(c.covered), uncovered: list(c.uncovered), unknown: list(c.unknown) };
}

/** Drawing names a reconstruction / PROFILE import accepts: "dxf" / "dwg" from the file name, else null. */
export function cadDrawingFormat(name: string | null | undefined, kind?: string | null): 'dxf' | 'dwg' | null {
  const lower = (name ?? '').toLowerCase().trim();
  if (lower.endsWith('.dwg')) return 'dwg';
  if (lower.endsWith('.dxf') || kind === 'drawing_dxf') return 'dxf';
  return null;
}

// ─── errors ────────────────────────────────────────────────────────────────

export type CadErrorFamily = 'requirements' | 'unsupported' | 'simulation' | 'annotation' | 'step' | 'profile' | 'drawing_input' | 'dwg' | 'package' | 'other';

/** Which detail block a CAD error shows (its message key is error.<code>). */
export function cadErrorFamily(code: string | null | undefined): CadErrorFamily {
  const c = code ?? '';
  if (c === 'requirements_not_met') return 'requirements';
  if (c === 'unsupported') return 'unsupported';
  if (c === 'simulation_unsupported') return 'simulation';
  if (c === 'drawing_check_failed') return 'annotation';
  if (c.startsWith('step_')) return 'step';
  if (c.startsWith('profile_') || c === 'drawing_needs_reconstruct') return 'profile';
  if (c.startsWith('dwg_')) return 'dwg';
  if (c === 'drawing_format_mismatch' || c === 'dxf_unreadable' || c === 'unsupported_drawing_file' || c === 'drawing_empty' || c === 'not_a_drawing' || c === 'drawing_has_no_dxf') return 'drawing_input';
  if (c.startsWith('package_')) return 'package';
  return 'other';
}

/** Codes with their own localized message (error.<code> in i18n/messages/cad.*.ts). */
export const CAD_ERROR_CODES = [
  'requirements_not_met',
  'unsupported',
  'simulation_unsupported',
  'drawing_check_failed',
  'drawing_failed',
  'invalid_drawing_config',
  'source_missing',
  'drawing_needs_reconstruct',
  'not_a_drawing',
  'profile_missing_parameter',
  'profile_units',
  'profile_unsupported_entity',
  'profile_open_contour',
  'profile_no_outer',
  'profile_touching',
  'profile_multiview',
  'profile_unreadable',
  'profile_check_failed',
  'step_assembly',
  'step_multibody',
  'step_no_solid',
  'step_invalid',
  'step_unreadable',
  'dwg_not_dwg',
  'dwg_runtime_unavailable',
  'dwg_timeout',
  'dwg_version_unsupported',
  'dwg_conversion_failed',
  'dwg_readback_mismatch',
  'drawing_format_mismatch',
  'dxf_unreadable',
  'unsupported_drawing_file',
  'drawing_empty',
  'drawing_has_no_dxf',
  'drawing_required',
  'package_manifest_mismatch',
  'package_file_missing',
  'package_file_mismatch',
  'mesh_reference',
] as const;

export type CadErrorCode = (typeof CAD_ERROR_CODES)[number];

export function isCadErrorCode(code: string | null | undefined): code is CadErrorCode {
  return !!code && (CAD_ERROR_CODES as readonly string[]).includes(code);
}

// ─── clarification gaps ────────────────────────────────────────────────────

/** What a gap is about: the drawing sheet, its units, a conflicting dimension, or a critical requirement field. */
export type GapKind = 'sheet' | 'units' | 'conflict' | 'field';

export function gapKind(g: Pick<ClarificationGap, 'kind' | 'field' | 'reason'>): GapKind {
  if (g.reason === 'conflict') return 'conflict';
  if (g.kind === 'drawing' && g.field === 'sheet') return 'sheet';
  if (g.kind === 'drawing' && g.field === 'units') return 'units';
  return 'field';
}

// ─── AI-call ledger ────────────────────────────────────────────────────────

export interface LedgerTotals {
  calls: number;
  failed: number;
  /** Sums over calls that reported usage; null when no call did. */
  tokensIn: number | null;
  tokensOut: number | null;
  /** Some call did not report its usage (the totals are then lower bounds). */
  incomplete: boolean;
  latencyMs: number | null;
  simulated: boolean;
}

/** Provider-reported usage of one call; a simulated call counted nothing (its numbers are not provider counts). */
export function callTokens(c: Pick<AiCallRecord, 'tokens_in' | 'tokens_out' | 'simulated'>): { tokensIn: number | null; tokensOut: number | null } {
  if (c.simulated) return { tokensIn: null, tokensOut: null };
  return { tokensIn: typeof c.tokens_in === 'number' ? c.tokens_in : null, tokensOut: typeof c.tokens_out === 'number' ? c.tokens_out : null };
}

export function ledgerTotals(calls: readonly Pick<AiCallRecord, 'status' | 'tokens_in' | 'tokens_out' | 'latency_ms' | 'simulated'>[]): LedgerTotals {
  let tin: number | null = null;
  let tout: number | null = null;
  let lat: number | null = null;
  let incomplete = false;
  let failed = 0;
  for (const c of calls) {
    if (c.status !== 'ok' && c.status !== 'succeeded') failed += 1;
    const t = callTokens(c);
    if (t.tokensIn !== null) tin = (tin ?? 0) + t.tokensIn;
    else incomplete = true;
    if (t.tokensOut !== null) tout = (tout ?? 0) + t.tokensOut;
    else incomplete = true;
    if (typeof c.latency_ms === 'number') lat = (lat ?? 0) + c.latency_ms;
  }
  return { calls: calls.length, failed, tokensIn: tin, tokensOut: tout, incomplete, latencyMs: lat, simulated: calls.some((c) => c.simulated) };
}

// ─── form values ───────────────────────────────────────────────────────────

/** auto / none / plane from a stored section value (anything unknown counts as auto, the server default). */
export function sectionMode(v: unknown): 'auto' | 'none' | 'plane' {
  if (v === 'none') return 'none';
  if (v && typeof v === 'object' && ((v as { plane?: unknown }).plane === 'XZ' || (v as { plane?: unknown }).plane === 'YZ')) return 'plane';
  return 'auto';
}

/** A typed number ("12.5", "-3", "4,5"); null for empty or not-a-number text. 0 is a valid value. */
export function parseOffset(text: string): number | null {
  const t = text.trim().replace(',', '.');
  if (!t || t === '-' || t === '.' || t === '-.') return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

/** PROFILE import field check: null = ok, else the problem (required / positive). */
export function profileFieldProblem(v: number | null | undefined, opts: { positive?: boolean } = {}): 'required' | 'positive' | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'required';
  if (opts.positive && !(v > 0)) return 'positive';
  return null;
}
