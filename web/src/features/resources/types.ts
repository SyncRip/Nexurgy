// Shared contract of the resources & knowledge area (docs/resources-knowledge-handoff.zh-CN.md):
// one in-page view scope (a project / all accessible projects),
// two main views (materials / experience), and one catalogue display model that
// joins the existing per-project sources (asset catalogue, files, drawing
// documents, engineering specs) by real object identity. This is a front-end
// adapter over existing endpoints, not a server DTO.

import type { AssetAvailability, AssetType, EngineeringState, PublishState } from '../../api/assets';
import type { ISODateTime, Project, UUID } from '../../api/types';

/** The single "View scope" of the page; every stored material belongs to a project. */
export type ViewScope = { kind: 'project'; projectId: UUID } | { kind: 'all' };

/** The nine business types of the type filter; file format is a separate fact. */
export const BUSINESS_TYPES = ['model', 'drawing', 'spec', 'process_plan', 'quote', 'document', 'process_module', 'intake', 'delivery'] as const;
export type BusinessType = (typeof BUSINESS_TYPES)[number];

/**
 * Business type of every asset type (`engineering_spec` is in the server enum but never registered) — the one mapping
 * for the catalogue rows (catalog.ts) and old `asset_type` links (app/nav.tsx). Kept here, a module without runtime
 * imports, so the router can read it without importing the catalogue.
 */
export const ASSET_BUSINESS_TYPE: Readonly<Record<AssetType | 'engineering_spec', BusinessType>> = {
  part_model: 'model',
  assembly_model: 'model',
  drawing: 'drawing',
  drawing_document: 'drawing',
  assembly_drawing: 'drawing',
  engineering_spec: 'spec',
  process_plan: 'process_plan',
  quote: 'quote',
  document: 'document',
  process_module: 'process_module',
  intake: 'intake',
  delivery_manifest: 'delivery',
};

/** How the shown (latest) version came to be, from its records — never from a file name or extension. */
export const CREATION_METHODS = ['upload', 'manual', 'task'] as const;
export type CreationMethod = (typeof CREATION_METHODS)[number] | 'unknown';

/**
 * What the browser can do with the item:
 * - preview: shown in the detail panel (PDF / image / text / generated SVG)
 * - page: opened in its professional page (model, spec, 2D editor, plan, quote, intake, module, delivery)
 * - convert: an uploaded CAD / DWG original that has not been imported or converted
 * - download: original download only (e.g. an uploaded Excel, ZIP)
 * - failed: the build / conversion failed
 * - none: nothing to show yet (still building, or no version)
 */
export type PreviewState = 'preview' | 'page' | 'convert' | 'download' | 'failed' | 'none';

/** Status filter under "More"; only states the records support. */
export const CATALOG_STATUSES = ['deprecated', 'upstream_newer', 'published', 'failed'] as const;
export type CatalogStatus = (typeof CATALOG_STATUSES)[number];

/** Real identity of a catalogue row. Files and drawing documents fold into their asset when it exists. */
export type CatalogRef =
  | { kind: 'asset'; assetId: UUID; assetType: string; fileId: UUID | null; drawingDocumentId: UUID | null }
  | { kind: 'spec'; specId: UUID }
  /** An uploaded file without a document asset (older data). */
  | { kind: 'file'; fileId: UUID }
  /** A standalone drawing document without a saved version (so without an asset yet). */
  | { kind: 'drawing_document'; documentId: UUID };

export interface CatalogProject {
  id: UUID;
  code: string;
  name: string;
}

/**
 * Why a row is hidden from the daily list: `self` — the object the row is (its file, drawing document or asset) is
 * archived, which "Restore" undoes; `part` — only its part is archived (restore the part to show it again).
 */
export type ArchivedBy = 'self' | 'part';

/** A task's current formal output of an asset: the exact version that task accepted (task context, not a global state). */
export interface TaskFormalVersion {
  versionId: UUID;
  label: string;
  taskNumber: number | null;
}

export interface CatalogVersionBrief {
  /** Asset version id, or the spec version id for specs; null when the source has no version id. */
  id: UUID | null;
  /** R3 / D2 / P1 / Q1 / v2 / V1 / DL-1 … */
  label: string;
  createdAt: ISODateTime | null;
  publishState: PublishState | null;
  availability: AssetAvailability | null;
  labels: string[];
  engineering: EngineeringState | null;
}

export interface CatalogItem {
  /** `${ref.kind}:${id}` — unique across projects; also the `item` URL value. */
  key: string;
  ref: CatalogRef;
  project: CatalogProject;
  type: BusinessType;
  /** Raw kind: part_model / assembly_model / drawing / drawing_document / assembly_drawing / engineering_spec / file … */
  subtype: string;
  name: string;
  /** Short context under the name: part number, spec code, drawing number, original file name. */
  context: string[];
  /** The object's own description, edited with its name (drawing documents); null when it has none. */
  description: string | null;
  partId: UUID | null;
  partNumber: string | null;
  creation: CreationMethod;
  latest: CatalogVersionBrief | null;
  versionCount: number;
  preview: PreviewState;
  /** A dependency of the latest version has a newer version in the same catalogue. */
  upstreamNewer: boolean;
  /** Current formal outputs of tasks (from workflow resources), shown beside the latest version — never merged into it. */
  formal: TaskFormalVersion[];
  /** Versions of this asset a run / agent wrote (workflow resource registration): their creation method is task output. */
  taskWritten: UUID[];
  /** Hidden from the daily list, and why; null = shown. */
  archived: ArchivedBy | null;
  updatedAt: ISODateTime | null;
  /** Professional page of the object (in-app path), when it has one. */
  openPath: string | null;
  /** Lower-case search text: name, part number, codes, drawing numbers, file names. */
  search: string;
}

export interface ProjectLoadState {
  project: CatalogProject;
  status: 'loading' | 'ok' | 'error';
  error?: unknown;
  /** Sources that returned fewer rows than exist (e.g. specs beyond the page limit). */
  truncated?: string[];
}

export interface CatalogState {
  items: CatalogItem[];
  /** One entry per project in the scope (one for a project scope, every accessible project for `all`). */
  projects: ProjectLoadState[];
  /** Nothing loaded yet for this scope. */
  initial: boolean;
  /** A load is in flight (initial or reload). */
  loading: boolean;
  reload: () => void;
  retryProject: (projectId: UUID) => void;
}

export interface CatalogFilter {
  q: string;
  type: BusinessType | '';
  /** Narrows a business type to one raw kind (e.g. drawing → drawing_document); '' = all. */
  subtype: string;
  creation: CreationMethod | '';
  partId: string;
  status: CatalogStatus | '';
  archived: boolean;
}

/** Full project rows by id for the scope (caps, roles, part lists come from these). */
export type ProjectsById = ReadonlyMap<UUID, Project>;
