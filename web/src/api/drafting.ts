import { operationChoices } from './capabilities';
// Standalone 2D drawing documents (W6; server/app/api/drawing_documents.py, server/app/cad/drafting.py).
// A document belongs to a project only; every save is a new immutable version based on the head version
// (409 version_conflict when someone saved first). Execution, engineering check and acceptance stay separate.

import { request, buildUrl, currentToken, parseErrorBody } from './client';
import type { AcceptanceStatus, Assumption, CheckStatus, ExecutionPolicy, Verification } from './engineering';
import { filenameFromDisposition, saveBlob } from './blobs';

type UUID = string;
type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export type Pt = [number, number];
export type EntityOrigin = 'source' | 'ai' | 'person' | 'generated';
export type DimType = 'linear' | 'aligned' | 'angular' | 'diameter' | 'radius';

interface EntityBase {
  id: string;
  layer: string;
  color?: number | null;
  linetype?: string | null;
  origin: EntityOrigin;
  modified?: boolean;
  source_ref?: string | null;
}

export interface LineEntity extends EntityBase {
  type: 'line';
  p1: Pt;
  p2: Pt;
}
export interface PolylineEntity extends EntityBase {
  type: 'polyline';
  points: Pt[];
  bulges?: number[] | null;
  closed: boolean;
}
export interface CircleEntity extends EntityBase {
  type: 'circle';
  center: Pt;
  radius: number;
}
export interface ArcEntity extends EntityBase {
  type: 'arc';
  center: Pt;
  radius: number;
  start_angle: number;
  end_angle: number;
}
export interface TextEntity extends EntityBase {
  type: 'text';
  insert: Pt;
  text: string;
  height: number;
  rotation: number;
  align: 'left' | 'center' | 'right';
}
export interface MTextEntity extends EntityBase {
  type: 'mtext';
  insert: Pt;
  text: string;
  height: number;
  width?: number | null;
  rotation: number;
}
export interface LeaderEntity extends EntityBase {
  type: 'leader';
  points: Pt[];
  text: string;
  height: number;
}
export interface DimensionEntity extends EntityBase {
  type: 'dimension';
  dimtype: DimType;
  p1?: Pt | null;
  p2?: Pt | null;
  center?: Pt | null;
  radius?: number | null;
  angle: number;
  location?: Pt | null;
  text_override?: string | null;
  dimlfac?: number;
  refs?: string[];
}
export interface HatchLoop {
  points?: Pt[] | null;
  bulges?: number[] | null;
  center?: Pt | null;
  radius?: number | null;
}
export interface HatchEntity extends EntityBase {
  type: 'hatch';
  loops: HatchLoop[];
  pattern: string;
  scale: number;
  angle: number;
  solid: boolean;
  boundary_refs?: string[];
}
export interface InsertEntity extends EntityBase {
  type: 'insert';
  block: string;
  insert: Pt;
  rotation: number;
  xscale: number;
  yscale: number;
}

export type DraftEntity =
  | LineEntity
  | PolylineEntity
  | CircleEntity
  | ArcEntity
  | TextEntity
  | MTextEntity
  | LeaderEntity
  | DimensionEntity
  | HatchEntity
  | InsertEntity;
export type EntityType = DraftEntity['type'];

export interface DraftLayer {
  name: string;
  color: number;
  linetype: string;
  lineweight: number;
  visible: boolean;
  locked: boolean;
  description?: string;
}

export interface DraftBlock {
  name: string;
  base: Pt;
  entities: DraftEntity[];
  description?: string;
  origin?: EntityOrigin;
}

export interface Reading {
  text: string;
  value?: number | null;
  source?: string | null;
  locator?: string | null;
}

export const TITLE_FIELDS = ['title', 'drawing_number', 'material', 'scale', 'sheet', 'revision', 'drawn_by', 'checked_by', 'date', 'company', 'project', 'units', 'weight', 'notes'] as const;
export type TitleField = (typeof TITLE_FIELDS)[number];
export const BOM_COLUMNS = ['item', 'number', 'name', 'qty', 'material', 'note'] as const;

export interface DraftSheet {
  id: string;
  name: string;
  size: 'A4' | 'A3' | 'A2' | 'A1' | 'A0' | 'custom';
  orientation: 'landscape' | 'portrait';
  width: number;
  height: number;
  scale: number;
  origin: Pt;
  draw_frame: boolean;
  title_block: Partial<Record<TitleField, string>>;
  title_readings: Partial<Record<TitleField, Reading>>;
  bom_columns: string[];
  bom: Record<string, string>[];
}

export type CalibrationStatus = 'unknown' | 'calibrated' | 'assumed';
export interface Calibration {
  status: CalibrationStatus;
  p1_px?: Pt | null;
  p2_px?: Pt | null;
  distance_mm?: number | null;
  mm_per_px?: number | null;
  assumption_id?: string | null;
  by?: string | null;
  at?: string | null;
}
export interface Underlay {
  id: string;
  sha256: string;
  name: string;
  content_type: string;
  width_px: number;
  height_px: number;
  file_version_id?: string | null;
  origin: Pt;
  rotation: number;
  opacity: number;
  visible: boolean;
  calibration: Calibration;
}

export interface OverrideRecord {
  text: string | null;
  author: 'source' | 'person' | 'ai';
  by?: string | null;
  at?: string | null;
}
export interface AnnotationProvenance {
  original?: Reading | null;
  override?: OverrideRecord | null;
  assumption?: string | null;
  measured?: number | null;
  unit: 'mm' | 'deg';
  displayed: string;
  consistent?: boolean | null;
  flags: string[];
}

export interface ConflictValue {
  value?: number | string | null;
  text: string;
  kind: 'displayed' | 'measured' | 'stated' | 'source';
  source?: string | null;
  locator?: string | null;
}
export interface Conflict {
  id: string;
  subject: string;
  kind: 'reading_mismatch' | 'duplicate_reading' | 'source_conflict';
  values: ConflictValue[];
  note: string;
  status: 'open' | 'resolved';
  resolution?: Record<string, unknown> | null;
}

export interface UnsupportedRow {
  type: string;
  count: number;
  preserved: boolean;
  handling: string;
  where?: string;
  handles?: string[];
}

export interface DraftSource {
  id: string;
  kind: 'text' | 'file_version' | 'image';
  name?: string;
  file_version_id?: string | null;
  sha256?: string;
  format?: 'dxf' | 'dwg';
  text?: string;
  width_px?: number;
  height_px?: number;
  [k: string]: unknown;
}

export interface CheckItem {
  rule: string;
  status: 'passed' | 'failed' | 'warning';
  message: string;
  ids?: string[];
}
export interface CheckReport {
  check: string;
  status: CheckStatus | 'passed' | 'failed' | 'unknown';
  items?: CheckItem[];
  mismatches?: string[];
  reason?: string;
  [k: string]: unknown;
}

export interface VersionFile {
  format: 'dxf' | 'dwg';
  sha256: string;
  bytes: number;
  version: string;
  path: string;
}

export type DraftingOrigin = 'empty' | 'text' | 'file' | 'image' | 'edit' | 'restore' | 'calibrate';
export type ScaleStatus = 'unknown' | 'calibrated' | 'assumed' | 'native';
export type OutputStatus = 'succeeded' | 'partial';

export interface VersionBrief {
  id: UUID;
  document_id: UUID;
  version_no: number;
  label: string;
  parent_id: UUID | null;
  restored_from_id: UUID | null;
  origin: DraftingOrigin;
  execution_policy: ExecutionPolicy;
  check_status: CheckStatus;
  acceptance_status: AcceptanceStatus;
  output_status: OutputStatus;
  scale_status: ScaleStatus;
  verification: Verification;
  labels: string[];
  /** unverified: the DWG file was written, its read-back check is closed by Turbo (generation reliability N). */
  dwg_status: 'passed' | 'failed' | 'unavailable' | 'unverified' | null;
  files: VersionFile[];
  entity_count: number;
  entity_types: Record<string, number>;
  conflicts_open: number;
  unsupported_count: number;
  commands: string[];
  note: string;
  manifest_sha256: string;
  content_hash: string;
  simulated: boolean;
  created_by: UUID;
  created_at: string;
}

export interface DraftContent {
  sheets: DraftSheet[];
  layers: DraftLayer[];
  entities: DraftEntity[];
  blocks: DraftBlock[];
  underlays: Underlay[];
  next_entity_no?: number;
  provenance: Record<string, AnnotationProvenance>;
  conflicts: Conflict[];
  assumptions: Assumption[];
  open_questions: string[];
}

export interface VersionFull extends VersionBrief, DraftContent {
  sources: DraftSource[];
  calibration: Record<string, unknown>;
  unsupported: UnsupportedRow[];
  checks: Record<string, CheckReport>;
  dwg: Record<string, unknown>;
  manifest: Record<string, unknown>;
  ai: { simulated: boolean; model_id: string; summary: string; questions: string[]; repairs: number } | null;
  commands_detail: Record<string, unknown>[];
}

export interface DrawingDocument<V = VersionBrief> {
  id: UUID;
  project_id: UUID;
  number: string;
  title: string;
  description: string;
  status: 'draft' | 'released';
  head_version_id: UUID | null;
  head_version_no: number;
  created_by: UUID;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  head: V | null;
}

export type CreateMode = 'empty' | 'text' | 'file' | 'image';
export interface DocumentCreate {
  title: string;
  number?: string;
  description?: string;
  mode: CreateMode;
  /** Absent: the project default (S5); the server resolves and freezes it. */
  execution_policy?: ExecutionPolicy;
  text?: string;
  file_version_id?: UUID;
  sheet?: Partial<Pick<DraftSheet, 'size' | 'orientation' | 'scale'>>;
}

/** One edit command (server: cad/drafting.py Command). */
export type DraftCommand =
  | { op: 'add_entities'; entities: Partial<DraftEntity>[] }
  | { op: 'delete_entities'; ids: string[] }
  | { op: 'transform'; ids: string[]; dx?: number; dy?: number; rotate?: number; center?: Pt | null; copy?: boolean }
  | { op: 'update_entity'; id: string; set: Record<string, unknown> }
  | { op: 'set_entity_layer'; ids: string[]; layer: string }
  | { op: 'upsert_layer'; layer: DraftLayer }
  | { op: 'set_layer_visibility'; name: string; visible: boolean }
  | { op: 'edit_text'; id: string; text: string; height?: number }
  | { op: 'edit_dimension'; id: string; text_override: string | null }
  | { op: 'define_block'; block: DraftBlock }
  | { op: 'insert_block'; block: string; insert: Pt; rotation?: number; xscale?: number; yscale?: number; layer?: string }
  | { op: 'set_title_block'; sheet_id?: string; fields: Record<string, string> }
  | { op: 'set_bom'; sheet_id?: string; rows: Record<string, string>[]; columns?: string[] }
  | { op: 'set_sheet'; sheet_id?: string; set: Record<string, unknown> }
  | { op: 'set_underlay'; id: string; set: Record<string, unknown> }
  | { op: 'calibrate'; underlay_id: string; p1_px: Pt; p2_px: Pt; distance_mm: number }
  | { op: 'resolve_conflict'; conflict_id: string; value?: number | string | null; note?: string }
  /** A person's decision on an AI / rule assumption, recorded in the new version (W0 §2.2). */
  | { op: 'confirm_assumption'; assumption_id: string; note?: string };

// ─── endpoints ─────────────────────────────────────────────────────────────

export const listDrawingDocuments = (projectId: UUID, includeArchived = false, o: Opt = {}) =>
  request<DrawingDocument[]>(`/projects/${enc(projectId)}/drawing-documents`, {
    query: { include_archived: includeArchived || undefined },
    signal: o.signal,
  });

export const createDrawingDocument = (projectId: UUID, body: DocumentCreate, o: Opt = {}) =>
  request<DrawingDocument<VersionFull>>(`/projects/${enc(projectId)}/drawing-documents`, { method: 'POST', body: operationChoices(body), signal: o.signal });

export const getDrawingDocument = (documentId: UUID, o: Opt = {}) =>
  request<DrawingDocument<VersionFull>>(`/drawing-documents/${enc(documentId)}`, { signal: o.signal });

export const patchDrawingDocument = (
  documentId: UUID,
  body: { title?: string; description?: string; status?: 'draft' | 'released'; archived?: boolean },
  o: Opt = {},
) => request<DrawingDocument>(`/drawing-documents/${enc(documentId)}`, { method: 'PATCH', body, signal: o.signal });

export const listDrawingVersions = (documentId: UUID, o: Opt = {}) =>
  request<VersionBrief[]>(`/drawing-documents/${enc(documentId)}/versions`, { signal: o.signal });

export const getDrawingVersion = (documentId: UUID, ref: string | number, o: Opt = {}) =>
  request<VersionFull>(`/drawing-documents/${enc(documentId)}/versions/${enc(String(ref))}`, { signal: o.signal });

export const saveDrawingVersion = (
  documentId: UUID,
  body: { base_version_id: UUID; commands: DraftCommand[]; execution_policy?: ExecutionPolicy; note?: string },
  o: Opt = {},
) => request<DrawingDocument<VersionFull>>(`/drawing-documents/${enc(documentId)}/versions`, { method: 'POST', body: operationChoices(body), signal: o.signal });

export const restoreDrawingVersion = (
  documentId: UUID,
  body: { base_version_id: UUID; version_id: UUID; execution_policy?: ExecutionPolicy; note?: string },
  o: Opt = {},
) => request<DrawingDocument<VersionFull>>(`/drawing-documents/${enc(documentId)}/restore`, { method: 'POST', body: operationChoices(body), signal: o.signal });

export const calibrateUnderlay = (
  documentId: UUID,
  body: { base_version_id: UUID; underlay_id: string; p1_px: Pt; p2_px: Pt; distance_mm: number; note?: string },
  o: Opt = {},
) => request<DrawingDocument<VersionFull>>(`/drawing-documents/${enc(documentId)}/calibrate`, { method: 'POST', body, signal: o.signal });

async function fetchAuthed(path: string, signal?: AbortSignal): Promise<{ blob: Blob; filename: string | null }> {
  const token = currentToken();
  const res = await fetch(buildUrl(path), { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal });
  if (!res.ok) throw parseErrorBody(res.status, await res.text().catch(() => ''), res.statusText);
  return { blob: await res.blob(), filename: filenameFromDisposition(res.headers.get('Content-Disposition')) };
}

/** Download one file of a version (dxf / dwg / manifest) or the whole package. */
export async function downloadDrawingFile(versionId: UUID, what: 'dxf' | 'dwg' | 'manifest' | 'package', fallbackName: string, o: Opt = {}): Promise<void> {
  const path = what === 'package' ? `/drawing-document-versions/${enc(versionId)}/package.zip` : `/drawing-document-versions/${enc(versionId)}/files/${what}`;
  const { blob, filename } = await fetchAuthed(path, o.signal);
  saveBlob(blob, filename ?? fallbackName);
}

/** Object URL of an underlay image (the caller revokes it). */
export async function underlayUrl(versionId: UUID, underlayId: string, o: Opt = {}): Promise<string> {
  const { blob } = await fetchAuthed(`/drawing-document-versions/${enc(versionId)}/underlays/${enc(underlayId)}`, o.signal);
  return URL.createObjectURL(blob);
}
