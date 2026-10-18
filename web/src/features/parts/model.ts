// Part cards (R1.2 prototype `#parts` / workbench "recent parts"): each card
// shows the part's latest run — its state, a flow progress bar built from the
// run's node_counts and the workflow name. Thumbnails use the current model
// revision, with its drawing's iso view as a fallback.
// Runs are fetched once per project (listRuns) and grouped here; nothing is
// invented: a part without a run in the fetched window says so.

import { PART_STATUSES } from '../../api/types';
import type { Drawing, NodeStatus, Part, PartStatus, RunSummary } from '../../api/types';

/** State of a part's latest run, as the card badge and the list filters read it. */
export type PartRunState =
  /** No run of this part exists. */
  | 'none'
  /** The fetched window was full and holds no run of this part: older runs may exist. */
  | 'unknown'
  | 'queued'
  | 'running'
  | 'waiting'
  | 'failed'
  | 'cancelled'
  | 'done';

export const PARTS_FILTERS = ['all', 'attention', 'running', 'done'] as const;
export type PartsFilter = (typeof PARTS_FILTERS)[number];

/** Progress-bar segment of one node. */
export type SegmentTone = 'ok' | 'skip' | 'run' | 'wait' | 'err' | 'stop' | 'pend';

export interface FlowProgress {
  /** Succeeded nodes (failed / skipped ones are never "done", S6). */
  done: number;
  total: number;
  /** One entry per node, finished first (the list endpoint has counts, not the graph order). */
  segments: SegmentTone[];
}

const SEGMENT_ORDER: readonly [NodeStatus, SegmentTone][] = [
  ['succeeded', 'ok'],
  ['skipped', 'skip'],
  ['running', 'run'],
  ['waiting_human', 'wait'],
  ['failed', 'err'],
  ['cancelled', 'stop'],
  ['ready', 'pend'],
  ['pending', 'pend'],
];

const newestFirst = (a: RunSummary, b: RunSummary) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id);

/** Newest run per key (part id, task id …); runs without a key are left out. */
export function latestRunBy(runs: readonly RunSummary[], key: (r: RunSummary) => string | null | undefined): Map<string, RunSummary> {
  const out = new Map<string, RunSummary>();
  for (const r of runs) {
    const k = key(r);
    if (!k) continue;
    const cur = out.get(k);
    if (!cur || newestFirst(r, cur) < 0) out.set(k, r);
  }
  return out;
}

export const latestRunByPart = (runs: readonly RunSummary[]) => latestRunBy(runs, (r) => r.part_id);
export const latestRunByTask = (runs: readonly RunSummary[]) => latestRunBy(runs, (r) => r.task_id);

export function runStateOf(run: Pick<RunSummary, 'status'> | undefined, windowFull = false): PartRunState {
  if (!run) return windowFull ? 'unknown' : 'none';
  switch (run.status) {
    case 'queued':
      return 'queued';
    case 'running':
      return 'running';
    case 'waiting':
      return 'waiting';
    case 'failed':
    case 'partial': // S6: a required step failed; the products it made are shown with their own states
      return 'failed';
    case 'cancelled':
      return 'cancelled';
    case 'succeeded':
      return 'done';
    default:
      return 'running';
  }
}

export function flowProgress(counts: Partial<Record<NodeStatus, number>> | null | undefined): FlowProgress {
  const segments: SegmentTone[] = [];
  for (const [status, tone] of SEGMENT_ORDER) {
    const n = Math.max(0, Math.floor(counts?.[status] ?? 0));
    for (let i = 0; i < n; i++) segments.push(tone);
  }
  // Statuses this client does not know yet still count as unfinished nodes.
  const known = new Set(SEGMENT_ORDER.map(([s]) => s as string));
  for (const [status, n] of Object.entries(counts ?? {})) {
    if (known.has(status)) continue;
    for (let i = 0; i < Math.max(0, Math.floor(n ?? 0)); i++) segments.push('pend');
  }
  // S6: only succeeded nodes are done. A node is skipped only because an input failed, so it never counts as done.
  const done = segments.filter((s) => s === 'ok').length;
  return { done, total: segments.length, segments };
}

/** Which list filter a run state belongs to besides "all" ('unknown' only shows under "all"). */
export function matchesFilter(state: PartRunState, filter: PartsFilter): boolean {
  switch (filter) {
    case 'all':
      return true;
    case 'attention':
      return state === 'none' || state === 'waiting' || state === 'failed' || state === 'cancelled';
    case 'running':
      return state === 'queued' || state === 'running';
    case 'done':
      return state === 'done';
  }
}

export function filterCounts(states: readonly PartRunState[]): Record<PartsFilter, number> {
  const out = { all: 0, attention: 0, running: 0, done: 0 } as Record<PartsFilter, number>;
  for (const s of states) for (const f of PARTS_FILTERS) if (matchesFilter(s, f)) out[f]++;
  return out;
}

export interface PartFlowSummary {
  run: RunSummary | null;
  state: PartRunState;
  progress: FlowProgress | null;
}

/**
 * Summaries of every part from one run list. `windowFull`: the list hit its
 * limit, so a part without a run in it may still have older runs.
 */
export function partFlowSummaries(parts: readonly Pick<Part, 'id'>[], runs: readonly RunSummary[], windowFull: boolean): Map<string, PartFlowSummary> {
  const latest = latestRunByPart(runs);
  return new Map(parts.map((p) => [p.id, flowSummary(latest.get(p.id), windowFull)]));
}

/** Summary of one latest run (undefined: none in the window). */
export function flowSummary(run: RunSummary | undefined, windowFull: boolean): PartFlowSummary {
  return { run: run ?? null, state: runStateOf(run, windowFull), progress: run ? flowProgress(run.node_counts) : null };
}

/** Workflow name shown on a card: the run's title, else its template's name. */
export function runFlowName(run: Pick<RunSummary, 'title' | 'template_version'>): string {
  return run.title?.trim() || run.template_version?.name || '';
}

/** Append a run selection to a part workflow URL. */
export function partRunPath(partPath: string, runId: string): string {
  return `${partPath}?run=${encodeURIComponent(runId)}`;
}

// ─── status breakdown (workbench donut) ─────────────────────────────────────

/** One slice of the status donut: a real part status, its count and share. */
export interface StatusSlice {
  status: PartStatus;
  count: number;
  /** Fraction of the total, 0‥1 (0 when there are no parts). */
  fraction: number;
}

export interface StatusBreakdown {
  total: number;
  /** Every status in PART_STATUSES order; empty ones are kept so the legend is stable. */
  slices: StatusSlice[];
}

/**
 * Counts parts by their real `status` field (never invented — a status with no
 * part stays at zero). Slices follow PART_STATUSES order so the donut and legend
 * never reshuffle as counts change.
 */
export function partStatusBreakdown(parts: readonly Pick<Part, 'status'>[]): StatusBreakdown {
  const counts = Object.fromEntries(PART_STATUSES.map((s) => [s, 0])) as Record<PartStatus, number>;
  for (const p of parts) if (p.status in counts) counts[p.status]++;
  const total = parts.length;
  const slices = PART_STATUSES.map((status) => ({ status, count: counts[status], fraction: total ? counts[status] / total : 0 }));
  return { total, slices };
}

// ─── thumbnail ─────────────────────────────────────────────────────────────

export interface DrawingPreview {
  sha: string;
  drawingId: string;
  revNo: number | null;
  /** Sheet-mm crop [x, y, w, h] around the iso view (else the first view); null shows the whole sheet. */
  viewBox: [number, number, number, number] | null;
}

function validBox(b: unknown): b is [number, number, number, number] {
  return Array.isArray(b) && b.length === 4 && b.every((v) => typeof v === 'number' && Number.isFinite(v)) && b[2] > b[0] && b[3] > b[1];
}

const PATH_TAG = /<path\b([^>]*?)\/?>/gi;
const CLASS_ATTR = /(?:^|\s)class\s*=\s*"([^"]*)"/i;
const D_ATTR = /(?:^|\s)d\s*=\s*"([^"]*)"/i;
/** Path data only: commands, numbers, separators — nothing else is ever copied into the page. */
const SAFE_D = /^[MmLlHhVvCcSsQqTtAaZz0-9.,\s+\-eE]+$/;
const NUM = /[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?/gi;

/**
 * The visible outline paths ("vis" class) of the drawing's views that start
 * inside the crop — the iso view. They are redrawn with a hairline that stays
 * one pixel wide at thumbnail size (the sheet's 0.5 mm lines would vanish).
 * Only validated path data is kept; the drawing's markup is never inserted.
 */
export function viewPaths(svgText: string, crop: readonly [number, number, number, number], max = 4000): string[] {
  const [cx, cy, cw, ch] = crop;
  const out: string[] = [];
  for (const m of svgText.matchAll(PATH_TAG)) {
    const attrs = m[1];
    const cls = CLASS_ATTR.exec(attrs)?.[1] ?? '';
    if (!cls.split(/\s+/).includes('vis')) continue;
    const d = D_ATTR.exec(attrs)?.[1]?.trim();
    if (!d || d.length > 20000 || !SAFE_D.test(d)) continue;
    const nums = d.match(NUM);
    if (!nums || nums.length < 2) continue;
    const x = Number(nums[0]);
    const y = Number(nums[1]);
    if (!(x >= cx && x <= cx + cw && y >= cy && y <= cy + ch)) continue;
    out.push(d);
    if (out.length >= max) break;
  }
  return out;
}

/** Crop around one placed view, with a margin, as an SVG viewBox. */
export function viewCrop(views: Drawing['views'] | null | undefined): DrawingPreview['viewBox'] {
  const list = (views ?? []).filter((v) => validBox(v.bbox));
  const v = list.find((x) => x.name === 'iso') ?? list[0];
  if (!v) return null;
  const [x0, y0, x1, y1] = v.bbox;
  const w = x1 - x0;
  const h = y1 - y0;
  const m = Math.max(w, h) * 0.06;
  const r = (n: number) => Math.round(n * 1000) / 1000;
  return [r(x0 - m), r(y0 - m), r(w + 2 * m), r(h + 2 * m)];
}

/**
 * The drawing to preview for a part: the newest drawing with an SVG of the
 * part's latest model revision. A drawing of an older revision is not shown —
 * the card would picture geometry the part no longer has.
 */
export function pickPreviewDrawing(drawings: readonly Drawing[], latestRevisionId: string | null | undefined): DrawingPreview | null {
  if (!latestRevisionId) return null;
  const d = drawings
    .filter((x) => x.svg_sha && x.model_revision_id === latestRevisionId)
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '') || b.id.localeCompare(a.id))[0];
  if (!d?.svg_sha) return null;
  return { sha: d.svg_sha, drawingId: d.id, revNo: d.rev_no, viewBox: viewCrop(d.views) };
}
