import { operationChoices } from './capabilities';
// Assembly drawing packages (W5) API: server/app/api/assembly_drawings.py. A package (GA + detail sheets, DXF / SVG /
// DWG per page) is bound to one exact assembly revision and immutable; a layout overlay is stored as its next version.
// Execution policy is explicit per request (standard by default, never silently Turbo).

import { request } from './client';
import type { Assumption, CheckStatus, ExecutionPolicy, ProcurementKind, RepresentationLevel, Verification } from './engineering';

type UUID = string;
type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export type DrawingFormat = 'dxf' | 'svg' | 'dwg';
export type ViewName = 'front' | 'top' | 'right' | 'iso';
export type Completeness = 'complete' | 'partial';
export type PageStatus = 'complete' | 'partial' | 'failed';
export type Axis = 'x' | 'y' | 'z';

export const DRAWING_FORMATS: DrawingFormat[] = ['dxf', 'svg', 'dwg'];
export const VIEW_NAMES: ViewName[] = ['front', 'top', 'right', 'iso'];

export interface PointRef {
  path: string[];
  datum?: string | null;
  point?: number[] | null;
}

export interface KeyDimension {
  id: string;
  a: PointRef;
  b: PointRef;
  axis: Axis;
  view?: 'front' | 'top' | 'right' | 'section' | null;
  label?: string;
}

/** server: app/cad/assembly_drawing.py AssemblyDrawingConfig (formats and the DWG timeout are set by the service). */
export interface AssemblyDrawingConfig {
  views?: ViewName[];
  projection?: 'third' | 'first';
  scale?: 'auto' | number;
  hidden_lines?: boolean;
  section?: 'auto' | 'none' | { plane: 'XZ' | 'YZ'; offset: number };
  not_sectioned?: string[];
  always_sectioned?: string[];
  bom_view?: 'flattened' | 'hierarchical';
  balloon_per_instance?: boolean;
  pitch_dimensions?: boolean;
  key_dimensions?: KeyDimension[];
  notes?: string[];
  details?: boolean;
  detail_procurements?: ProcurementKind[];
  title?: { author?: string; date?: string; project?: string; drawing_number?: string | null };
}

export interface PageFile {
  format: DrawingFormat;
  path: string;
  sha256: string;
  bytes: number;
  content_type: string;
  version: string | null;
}

export interface PageError {
  format?: string;
  code: string;
  message?: string;
}

export interface DrawingDimension {
  id: string;
  view: string;
  kind: 'overall' | 'pitch' | 'key';
  axis: Axis | null;
  value: number;
  text: string;
  measured: string;
  handle: string;
  text_at: number[] | null;
}

export interface Balloon {
  id: string;
  item: string;
  bom: 'flattened' | 'hierarchical';
  level: number;
  def_key: string;
  def_kind: string;
  quantity: number;
  view: string;
  occurrence: string[];
  anchor: number[];
  center: number[];
  radius: number;
  handle: string;
}

export interface BomRow {
  item: string;
  level: number;
  def_key: string;
  def_kind: string;
  name: string;
  number: string | null;
  material: string | null;
  procurement: ProcurementKind;
  representation: RepresentationLevel;
  quantity: number;
  total_quantity: number;
  shown_quantity: number;
}

export interface SectionInfo {
  letter: string;
  plane: 'XZ' | 'YZ';
  offset: number;
  chosen: 'auto' | 'config';
  view: string;
  cut: { occurrence: string[]; item: string | null; def_key?: string }[];
  not_sectioned: { occurrence: string[]; rule: string; item?: string | null }[];
}

export interface DrawingPage {
  index: number;
  key: string;
  document: string;
  role: 'assembly' | 'bom' | 'detail';
  title: string;
  sheet: number;
  sheets: number;
  def_key: string | null;
  item: string | null;
  scale: number | null;
  status: PageStatus;
  files: PageFile[];
  errors: PageError[];
  hatch_count: number;
  dwg: { status: string; code?: string; verified?: boolean; version?: string; bytes?: number } | null;
  // full package only
  views?: { name: string; kind: string; label: string; scale: number; bbox: number[] }[];
  dimensions?: DrawingDimension[];
  balloons?: Balloon[];
  bom_rows?: BomRow[];
  notes?: string[];
  callouts?: string[];
  section?: SectionInfo | null;
  overlay?: Record<string, unknown>[];
}

export interface StaleReason {
  code: 'newer_assembly_revision' | 'component_updates' | string;
  latest_revision_id?: string;
  latest_rev_no?: number;
  count?: number;
  instances?: string[];
}

export interface PackageDocument {
  key: string;
  role: 'assembly' | 'detail';
  sheets: number;
  def_key?: string;
  item?: string;
  error?: { code: string; message: string };
}

export interface PackageError {
  page: string | null;
  document: string;
  format?: string;
  code: string;
  message?: string;
}

export interface CheckRecord {
  check: string;
  status: CheckStatus | string;
  failed?: number;
  pages?: Record<string, string>;
  report_path?: string;
  report_sha256?: string;
}

export interface PackageVerification extends Verification {
  drawing_unverified?: boolean;
  upstream?: { assembly_revision_id: UUID; rev_no: number; execution_policy: ExecutionPolicy; check_status: CheckStatus; unverified: boolean };
}

export interface AssemblyDrawingBrief {
  id: UUID;
  project_id: UUID;
  assembly_id: UUID;
  assembly_revision_id: UUID;
  rev_no: number;
  rev_label: string;
  number: number;
  version: number;
  label: string;
  parent_id: UUID | null;
  superseded_by: UUID | null;
  execution_policy: ExecutionPolicy;
  completeness: Completeness;
  check_status: CheckStatus;
  verification: PackageVerification;
  checks: Record<string, CheckRecord>;
  stale: boolean;
  stale_reasons: StaleReason[];
  page_count: number;
  pages: DrawingPage[];
  documents: PackageDocument[];
  errors: PackageError[];
  warnings: { code: string; message: string }[];
  manifest_sha256: string;
  bom_sha256: string | null;
  overlay: { base_manifest_sha256: string; base_version: number; sha256: string; edits: Record<string, unknown>[]; pages: string[] } | null;
  asset_version_id: UUID | null;
  node_run_id: UUID | null;
  /** S3: the partial package this one continued (its complete sheets were kept). */
  resumed_from_id?: UUID | null;
  /** S3: what the generation did per document (generated | reused | failed) and per sheet (… | dwg_converted). */
  progress?: { documents?: Record<string, string>; pages?: Record<string, string>; resumed_from?: UUID | null } | null;
  /** S3 failure info of a partial package (recovery retry_sheet): null when complete. */
  failure?: PackageFailure | null;
  /** Generation supplement B-03: drawn from a partial assembly revision (its coverage); null otherwise. */
  coverage?: { status: 'complete' | 'partial' | 'none'; missing: unknown[] } | null;
  created_by: UUID;
  created_at: string;
}

export interface PackageFailure {
  stage: string;
  code: string;
  message: string;
  retryable: boolean;
  recovery: string;
  /** Why it cannot be continued: already_continued | overlay | other_generator. */
  not_continuable: string | null;
  continued_by: UUID | null;
  failed: { document: string | null; page: string | null; format: string | null; code: string | null; message: string | null }[];
}

export interface AssemblyDrawingPackage extends AssemblyDrawingBrief {
  config: AssemblyDrawingConfig;
  assumptions: Assumption[];
  manifest: Record<string, unknown> & { pages: { key: string; files: PageFile[]; missing: string[] }[] };
  generation: Record<string, unknown>;
}

/** One layout / annotation edit (sheet mm, origin bottom-left, y up). */
export type OverlayEdit =
  | { op: 'move_balloon'; page: string; item: string; occurrence?: string[]; by: [number, number] }
  | { op: 'move_dimension_text'; page: string; dimension: string; by: [number, number] }
  | { op: 'add_note'; page: string; text: string; at?: [number, number] };

export const listRevisionDrawings = (rid: UUID, o: Opt = {}) =>
  request<AssemblyDrawingBrief[]>(`/assembly-revisions/${enc(rid)}/drawings`, { signal: o.signal });
export const listAssemblyDrawings = (assemblyId: UUID, q: { revision_id?: UUID } = {}, o: Opt = {}) =>
  request<AssemblyDrawingBrief[]>(`/assemblies/${enc(assemblyId)}/drawings`, { query: q, signal: o.signal });
export const getAssemblyDrawing = (id: UUID, o: Opt = {}) => request<AssemblyDrawingPackage>(`/assembly-drawings/${enc(id)}`, { signal: o.signal });
export const generateAssemblyDrawing = (rid: UUID, body: { config: AssemblyDrawingConfig; execution_policy?: ExecutionPolicy }) =>
  request<AssemblyDrawingPackage>(`/assembly-revisions/${enc(rid)}/drawings`, { method: 'POST', body: operationChoices(body) });
/** S3: continue a partial package — a new package keeping the complete sheets, redoing only the failed ones. */
export const continueAssemblyDrawing = (id: UUID) => request<AssemblyDrawingPackage>(`/assembly-drawings/${enc(id)}/continue`, { method: 'POST' });
export const applyLayoutOverlay = (id: UUID, body: { base_manifest_sha256: string; edits: OverlayEdit[] }) =>
  request<AssemblyDrawingPackage>(`/assembly-drawings/${enc(id)}/overlays`, { method: 'POST', body });

function dispositionName(res: Response): string | null {
  const header = res.headers.get('Content-Disposition');
  const m = header ? /filename\*=UTF-8''([^;]+)/i.exec(header) : null;
  return m ? decodeURIComponent(m[1]) : null;
}

/** Authenticated download of one page file (404 page_format_missing when that format failed). */
export async function fetchPageFile(id: UUID, pageKey: string, format: DrawingFormat, o: Opt = {}): Promise<{ blob: Blob; filename: string | null; sha256: string | null }> {
  const res = await request<Response>(`/assembly-drawings/${enc(id)}/pages/${enc(pageKey)}/${format}`, { as: 'response', signal: o.signal });
  return { blob: await res.blob(), filename: dispositionName(res), sha256: res.headers.get('X-Content-SHA256') };
}

/** Authenticated download of the whole package (page files, check reports, manifest.json). */
export async function fetchPackageZip(id: UUID): Promise<{ blob: Blob; filename: string | null; manifestSha256: string | null }> {
  const res = await request<Response>(`/assembly-drawings/${enc(id)}/package.zip`, { as: 'response' });
  return { blob: await res.blob(), filename: dispositionName(res), manifestSha256: res.headers.get('X-Manifest-SHA256') };
}
