// Catalogue adapter of the resources & knowledge area (design §1): one pure function joins one project's existing
// sources — asset catalogue, files, standalone drawing documents, engineering specs (and its parts, for part facts) —
// into CatalogItem rows by real object identity; plus the list filter, the `item` URL lookup and small counts.
//
// Identity, never names or extensions:
// - a `document` asset IS its File (Asset.file_id, server services/assets.register_file_version). Name, part and
//   archived state come from the File row: renaming / relinking / archiving a file never touches its asset.
// - a `drawing_document` asset IS its DrawingDocument (version target summary.document_id, register_drawing_document).
//   Title, number, description and archived state come from the document.
// - a file without a document asset, or a drawing document without a saved version, stays its own row.
//
// The per asset type rules (creation method, recorded source field, content, professional pages, publish role) and the
// content / preview rule live here once: the list summarises the latest version with them, the detail
// (detailModel.ts) shows any version with them. Anything the records cannot prove is `unknown`.

import type { Asset, AssetAvailability, AssetVersion } from '../../api/assets';
import type { DrawingDocument } from '../../api/drafting';
import { specPath, type EngineeringSpec, type SpecVersionBrief, type SpecVersionStatus } from '../../api/spec';
import type { FileVersion, Part, ProjectFile, UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { assemblyRevisionPath, assetWorkspacePath } from '../../app/versionLinks';
import { previewKind } from '../approvals/TargetSnapshot';
import type {
  ArchivedBy,
  BusinessType,
  CatalogFilter,
  CatalogItem,
  CatalogProject,
  CatalogRef,
  CatalogStatus,
  CatalogVersionBrief,
  CreationMethod,
  PreviewState,
  TaskFormalVersion,
} from './types';
import { ASSET_BUSINESS_TYPE, BUSINESS_TYPES } from './types';

export const EMPTY_FILTER: CatalogFilter = { q: '', type: '', subtype: '', creation: '', partId: '', status: '', archived: false };

/** Specs are paged on the server (≤ 200 per call); more than this marks the project's catalogue as truncated. */
export const SPEC_PAGE_LIMIT = 200;

/** What the project's workflow resources record about asset versions (sources.ts loadTaskOutputs). */
export interface TaskOutputs {
  /** Per asset id, the versions a run / agent wrote (e.g. a document an agent wrote in a collaboration task, which has no run id). */
  written: ReadonlyMap<UUID, ReadonlySet<UUID>>;
  /** Current formal outputs of tasks, per asset id. */
  formal: ReadonlyMap<UUID, readonly TaskFormalVersion[]>;
  /** More workflow resources exist than were read. */
  truncated: boolean;
}

/** One project's raw sources, as the list endpoints return them (archived rows included). */
export interface ProjectSources {
  assets: Asset[];
  files: ProjectFile[];
  drawingDocuments: DrawingDocument[];
  specs: { items: EngineeringSpec[]; total: number };
  /** The project's parts, archived included: part number / name of a file's own part and archived parts. Optional. */
  parts?: Part[];
  /** Optional. */
  taskOutputs?: TaskOutputs;
}

export interface ProjectCatalog {
  items: CatalogItem[];
  /** Sources that returned fewer rows than exist ('specs', 'task_outputs'). */
  truncated: string[];
}

// ─── record readers ────────────────────────────────────────────────────────

type Summary = Record<string, unknown>;

/** Own-property lookup (a record value such as "constructor" must not hit Object.prototype). */
function pick<T>(table: Readonly<Record<string, T>>, key: string | null | undefined): T | undefined {
  return key != null && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

const str = (s: Summary, k: string): string | null => (typeof s[k] === 'string' && s[k] ? (s[k] as string) : null);
const summaryOf = (v: AssetVersion): Summary => v.target.summary ?? {};

const draftingPath = (documentId: UUID) => `/drafting/${documentId}`;

// ─── content and preview: one rule for the list and the detail ─────────────

/** How a version's content is shown: its stored file, a generated SVG, a server summary, a module, a spec, or only its page. */
export type ContentMode = 'file' | 'svg' | 'summary' | 'module' | 'spec' | 'page';

/** What the browser can do with a stored original. */
export type FileState = Extract<PreviewState, 'preview' | 'convert' | 'download'>;

/** Uploaded CAD / mesh originals (File.kind, or the extension — a DWG has no kind of its own). */
const CAD_KINDS = new Set(['model_step', 'model_mesh', 'drawing_dxf']);
const CAD_EXT = /\.(step|stp|iges|igs|stl|obj|glb|3mf|dxf|dwg)$/i;
/** Edges that turned an uploaded original into an engineering object (STEP → model, DWG / DXF → 2D drawing). */
const CONVERSION_RELATIONS = new Set(['imported_from', 'redrawn_from']);

/** The version has been imported / redrawn into a system object (its result is downstream). */
export const isConverted = (v: Pick<AssetVersion, 'dependents'> | null | undefined): boolean => !!v && (v.dependents ?? []).some((e) => CONVERSION_RELATIONS.has(e.relation));

/**
 * What the browser can do with a stored original: preview exactly what BlobPreview renders (previewKind, by the stored
 * file name), "needs conversion" for an unconverted CAD / mesh original, download only for everything else (a converted
 * original too — the system object it became is downstream).
 */
export function fileState(name: string | null, kind: string | null, converted: boolean): FileState {
  if (previewKind(name)) return 'preview';
  if ((kind && CAD_KINDS.has(kind)) || (name && CAD_EXT.test(name))) return converted ? 'download' : 'convert';
  return 'download';
}

/** What one version stores that the browser could show (an asset, file, spec or drawing version, normalised). */
export interface StoredContent {
  availability: AssetAvailability | null;
  /** The stored original (documents, uncatalogued files): what BlobPreview is given. */
  file: { sha: string; name: string; kind: string | null } | null;
  /** The generated SVG of a part drawing. */
  svg: { sha: string; name: string } | null;
  /** The original was imported / redrawn into a system object. */
  converted: boolean;
}

/** An asset version's stored content (document summary: original_name, kind, blob_sha256; part drawing: svg_sha). */
export function assetStored(v: AssetVersion): StoredContent {
  const s = summaryOf(v);
  const sha = str(s, 'blob_sha256');
  const svg = str(s, 'svg_sha');
  return {
    availability: v.availability,
    file: sha ? { sha, name: str(s, 'original_name') ?? `${v.asset_name}-${v.label}`, kind: str(s, 'kind') } : null,
    svg: svg ? { sha: svg, name: `${v.asset_name}-${v.label}.svg` } : null,
    converted: isConverted(v),
  };
}

/** An uploaded file version without a document asset. */
export const fileStored = (v: Pick<FileVersion, 'blob_sha256' | 'original_name'>, kind: string | null): StoredContent => ({
  availability: null,
  file: { sha: v.blob_sha256, name: v.original_name, kind },
  svg: null,
  converted: false,
});

/** Spec version status → the asset availability vocabulary. */
const SPEC_AVAILABILITY: Record<SpecVersionStatus, AssetAvailability> = { extracting: 'building', ready: 'ready', needs_answers: 'ready', failed: 'failed' };

/** A spec version: nothing stored to preview; its status says whether it is still extracting or failed. */
export const specStored = (v: Pick<SpecVersionBrief, 'status'>): StoredContent => ({ availability: SPEC_AVAILABILITY[v.status] ?? null, file: null, svg: null, converted: false });

export type VersionContent =
  | { kind: 'blob'; sha: string; name: string; state: FileState; converted: boolean }
  | { kind: 'summary' }
  | { kind: 'module' }
  | { kind: 'spec' }
  | { kind: 'page' }
  | { kind: 'failed' }
  | { kind: 'none' };

/** How a version is shown (detail) — and so its preview state (list): no version / building → none, failed, then the mode. */
export function versionContent(mode: ContentMode, s: StoredContent | null): VersionContent {
  if (!s || s.availability === 'building') return { kind: 'none' };
  if (s.availability === 'failed') return { kind: 'failed' };
  switch (mode) {
    case 'file':
      return s.file ? { kind: 'blob', sha: s.file.sha, name: s.file.name, state: fileState(s.file.name, s.file.kind, s.converted), converted: s.converted } : { kind: 'none' };
    case 'svg':
      return s.svg ? { kind: 'blob', sha: s.svg.sha, name: s.svg.name, state: fileState(s.svg.name, null, false), converted: false } : { kind: 'summary' };
    default:
      return { kind: mode };
  }
}

/** The catalogue's preview state of a content. */
export function contentPreview(c: VersionContent): PreviewState {
  switch (c.kind) {
    case 'blob':
      return c.state;
    case 'failed':
    case 'none':
      return c.kind;
    default:
      return 'page';
  }
}

// ─── per asset type rules (one table for the list and the detail) ──────────

/** ModelRevision / AssemblyRevision.source (server models REVISION_SOURCES, ASSEMBLY_REVISION_SOURCES). */
const MODEL_SOURCE: Record<string, CreationMethod> = { import: 'upload', manual: 'manual', copy: 'manual', ai: 'task' };
/** Drawing.kind: an uploaded drawing file vs. one the kernel generated from the model. */
const DRAWING_KIND: Record<string, CreationMethod> = { uploaded: 'upload', generated: 'task' };
/** DrawingDocumentVersion.origin: every origin is a person's save in the 2D editor except AI drafting from text. */
const DRAFTING_ORIGIN: Record<string, CreationMethod> = {
  empty: 'manual',
  file: 'manual',
  image: 'manual',
  edit: 'manual',
  restore: 'manual',
  calibrate: 'manual',
  text: 'task',
};
/** Intake.method: a person's form vs. AI extraction. */
const INTAKE_METHOD: Record<string, CreationMethod> = { manual: 'manual', ai: 'task' };
/**
 * EngineeringSpecVersion.origin: an `answer` is a person's reply on the spec page and `rule` a server rule's
 * derivation inside a run. `create` / `reextract` / `continue` are written both by the spec page and by the
 * orchestration workflow (services/orchestration.py create_spec) and the version keeps no run marker, so the
 * records cannot tell them apart → unknown.
 */
const SPEC_ORIGIN: Record<string, CreationMethod> = { answer: 'manual', rule: 'task' };

const fixed = (c: CreationMethod) => () => c;
const byField = (field: string, table: Record<string, CreationMethod>) => (s: Summary) => pick(table, str(s, field)) ?? 'unknown';
/** A summary field that records how a version was made: its values give the creation method and, with `label`, their message keys. */
const recorded = (field: string, table: Record<string, CreationMethod>, label: string) => ({ creation: byField(field, table), origin: { field, label } });

export interface AssetRule {
  /** Creation method from the version's own records (run provenance and task writes are checked first, for every type). */
  creation: (s: Summary) => CreationMethod;
  /** The summary field that records how a version was made, and the message prefix of its values. */
  origin?: { field: string; label: string };
  /** How a version is shown (absent → only in its professional page); the list's preview state follows from it. */
  content?: ContentMode;
  /** The professional page at this exact version (S6). */
  versionPage?: (v: AssetVersion) => string | null;
  /** The professional page of the object itself: it shows the current state, so it serves the latest version only. */
  objectPage?: (v: AssetVersion) => string | null;
  /** Role that publishes / deprecates and edits it (mirrors api/assets.py publish); absent → design. */
  role?: 'process' | 'quote';
  /** Identity of the File this asset is (documents). */
  fileId?: (a: Asset) => UUID | null;
  /** Identity of the DrawingDocument this asset is (standalone drawings). */
  documentId?: (s: Summary) => UUID | null;
}

const partPage = (v: AssetVersion) => assetWorkspacePath(v.asset_type, v.target);
const assemblyPage = (tab: string, revision: (v: AssetVersion) => string | null) => (v: AssetVersion) => {
  const asm = str(summaryOf(v), 'assembly_id');
  return asm ? assemblyRevisionPath(asm, revision(v), tab) : null;
};

const ASSET_RULES: Readonly<Record<keyof typeof ASSET_BUSINESS_TYPE, AssetRule>> = {
  part_model: { ...recorded('source', MODEL_SOURCE, 'revSource'), content: 'summary', versionPage: partPage },
  assembly_model: { ...recorded('source', MODEL_SOURCE, 'asm.history.source'), versionPage: assemblyPage('structure', (v) => v.target.id) },
  drawing: { creation: byField('kind', DRAWING_KIND), content: 'svg', versionPage: partPage },
  drawing_document: {
    ...recorded('origin', DRAFTING_ORIGIN, 'dr.originKind'),
    objectPage: (v) => {
      const doc = str(summaryOf(v), 'document_id');
      return doc ? draftingPath(doc) : null;
    },
    documentId: (s) => str(s, 'document_id'),
  },
  assembly_drawing: {
    // Version 1 of a package is generated; every later version is a person's layout overlay on its parent.
    creation: (s) => (str(s, 'parent_id') ? 'manual' : 'task'),
    // The drawings tab of the package's assembly revision shows that revision's current package.
    objectPage: assemblyPage('drawings', (v) => str(summaryOf(v), 'assembly_revision_id')),
  },
  process_plan: { creation: fixed('task'), content: 'summary', versionPage: partPage, role: 'process' },
  quote: { creation: fixed('task'), content: 'summary', versionPage: partPage, role: 'quote' },
  intake: { creation: byField('method', INTAKE_METHOD), content: 'summary', versionPage: partPage },
  document: { creation: fixed('upload'), content: 'file', fileId: (a) => a.file_id },
  process_module: { creation: fixed('manual'), content: 'module', role: 'process' },
  delivery_manifest: { creation: fixed('task') },
  // In the server enum but never registered; kept so such a row would still be typed honestly.
  engineering_spec: { creation: fixed('unknown') },
};

/** A type the client does not know yet: kept visible as a generic document with its raw kind, never dropped. */
const UNKNOWN_RULE: AssetRule = { creation: fixed('unknown') };

export const assetRule = (assetType: string): AssetRule => pick(ASSET_RULES, assetType) ?? UNKNOWN_RULE;
const businessType = (assetType: string): BusinessType => pick(ASSET_BUSINESS_TYPE, assetType) ?? 'document';

/** The professional page of one asset version: its exact-version page, else (latest only) the object's page; null → none. */
export function assetPage(v: AssetVersion, isLatest: boolean): string | null {
  const rule = assetRule(v.asset_type);
  return rule.versionPage?.(v) || (isLatest && rule.objectPage?.(v)) || null;
}

// ─── rows ──────────────────────────────────────────────────────────────────

interface Ctx {
  project: CatalogProject;
  files: ReadonlyMap<UUID, ProjectFile>;
  documents: ReadonlyMap<UUID, DrawingDocument>;
  parts: ReadonlyMap<UUID, Part>;
  /** Latest version_no of every asset of the project (upstream-newer check). */
  latestNo: ReadonlyMap<UUID, number>;
  written: ReadonlyMap<UUID, ReadonlySet<UUID>>;
  formal: ReadonlyMap<UUID, readonly TaskFormalVersion[]>;
}

interface PartFacts {
  number: string | null;
  label: string | null;
  archived: boolean;
}

function partFacts(partId: UUID | null, ctx: Ctx, registered?: Asset['part']): PartFacts | null {
  if (!partId) return null;
  const p = ctx.parts.get(partId) ?? (registered?.id === partId ? registered : undefined);
  if (!p) return { number: null, label: null, archived: false };
  return { number: p.number, label: `${p.number} ${p.name}`.trim(), archived: !!(p as Partial<Part>).archived_at };
}

/** The object's own archive wins (it is what "Restore" undoes); else its part's. */
const archivedBy = (self: string | null | undefined, part: PartFacts | null): ArchivedBy | null => (self ? 'self' : part?.archived ? 'part' : null);

const uniq = (xs: (string | null | undefined)[]): string[] => [...new Set(xs.filter((x): x is string => !!x))];

/** Context under the name: the part, a code / drawing number, the original file name when it differs. */
function contextOf(name: string, part: PartFacts | null, code: string | null, originalName: string | null | undefined): string[] {
  return uniq([part?.label, code, originalName && originalName !== name ? originalName : null]);
}

const searchOf = (xs: (string | null | undefined)[]) => uniq(xs).join('\n').toLowerCase();

const assetBrief = (v: AssetVersion): CatalogVersionBrief => ({
  id: v.id,
  label: v.label,
  createdAt: v.created_at,
  publishState: v.publish_state,
  availability: v.availability,
  labels: v.labels ?? [],
  engineering: v.engineering ?? null,
});

function assetItem(asset: Asset, ctx: Ctx): CatalogItem {
  const v = asset.latest_version;
  const summary: Summary = v ? summaryOf(v) : {};
  const rule = assetRule(asset.type);
  const fileId = rule.fileId?.(asset) ?? null;
  const documentId = rule.documentId?.(summary) ?? null;
  const file = fileId ? ctx.files.get(fileId) : undefined;
  const doc = documentId ? ctx.documents.get(documentId) : undefined;

  const name = file?.name ?? doc?.title ?? asset.name;
  const partId = file ? file.part_id : (asset.part_id ?? v?.target.part_id ?? null);
  const part = partFacts(partId, ctx, asset.part);
  const originalName = str(summary, 'original_name');
  return {
    key: `asset:${asset.id}`,
    ref: { kind: 'asset', assetId: asset.id, assetType: asset.type, fileId, drawingDocumentId: documentId },
    project: ctx.project,
    type: businessType(asset.type),
    subtype: asset.type,
    name,
    context: contextOf(name, part, doc?.number ?? null, originalName),
    description: doc?.description || null,
    partId,
    partNumber: part?.number ?? null,
    creation: v ? (v.run_id || v.node_run_id || ctx.written.get(asset.id)?.has(v.id) ? 'task' : rule.creation(summary)) : 'unknown',
    latest: v ? assetBrief(v) : null,
    versionCount: asset.version_count,
    preview: contentPreview(versionContent(rule.content ?? 'page', v ? assetStored(v) : null)),
    upstreamNewer: (v?.dependencies ?? []).some((e) => !!e.asset_id && e.version_no != null && (ctx.latestNo.get(e.asset_id) ?? 0) > e.version_no),
    formal: [...(ctx.formal.get(asset.id) ?? [])],
    taskWritten: [...(ctx.written.get(asset.id) ?? [])],
    // The row IS its File / DrawingDocument: their state is the item's own (Asset.archived_at is never written).
    archived: archivedBy((file ?? doc ?? asset).archived_at, part),
    updatedAt: v?.created_at ?? asset.created_at,
    openPath: (v && assetPage(v, true)) || paths.asset(asset.id, v?.id),
    search: searchOf([name, asset.name, part?.label, doc?.number, doc?.title, originalName, ...(file?.versions ?? []).map((fv) => fv.original_name)]),
  };
}

/** An uploaded file without a document asset (older data): its records cannot say how it was made. */
function fileItem(f: ProjectFile, ctx: Ctx): CatalogItem {
  const fv = f.versions[0];
  const part = partFacts(f.part_id, ctx);
  return {
    key: `file:${f.id}`,
    ref: { kind: 'file', fileId: f.id },
    project: ctx.project,
    type: 'document',
    subtype: 'file',
    name: f.name,
    context: contextOf(f.name, part, null, fv?.original_name),
    description: null,
    partId: f.part_id,
    partNumber: part?.number ?? null,
    creation: 'unknown',
    latest: fv ? { id: null, label: `v${fv.version}`, createdAt: fv.created_at, publishState: null, availability: null, labels: [], engineering: null } : null,
    versionCount: f.versions.length,
    preview: contentPreview(versionContent('file', fv ? fileStored(fv, f.kind) : null)),
    upstreamNewer: false,
    formal: [],
    taskWritten: [],
    archived: archivedBy(f.archived_at, part),
    updatedAt: fv?.created_at ?? f.created_at,
    openPath: null,
    search: searchOf([f.name, part?.label, ...f.versions.map((x) => x.original_name)]),
  };
}

/** A standalone drawing document with no catalogued version yet. */
function drawingDocumentItem(d: DrawingDocument, ctx: Ctx): CatalogItem {
  const h = d.head;
  return {
    key: `drawing_document:${d.id}`,
    ref: { kind: 'drawing_document', documentId: d.id },
    project: ctx.project,
    type: 'drawing',
    subtype: 'drawing_document',
    name: d.title,
    context: contextOf(d.title, null, d.number, null),
    description: d.description || null,
    partId: null,
    partNumber: null,
    creation: (h && pick(DRAFTING_ORIGIN, h.origin)) || 'unknown',
    latest: h ? { id: null, label: h.label, createdAt: h.created_at, publishState: null, availability: null, labels: h.labels ?? [], engineering: null } : null,
    versionCount: d.head_version_no,
    preview: h ? 'page' : 'none',
    upstreamNewer: false,
    formal: [],
    taskWritten: [],
    archived: archivedBy(d.archived_at, null),
    updatedAt: h?.created_at ?? d.updated_at,
    openPath: draftingPath(d.id),
    search: searchOf([d.title, d.number]),
  };
}

/** An engineering spec (not an asset): the list endpoint returns only non-archived specs. */
function specItem(s: EngineeringSpec, ctx: Ctx): CatalogItem {
  const v = s.latest_version;
  const stored = v ? specStored(v) : null;
  return {
    key: `spec:${s.id}`,
    ref: { kind: 'spec', specId: s.id },
    project: ctx.project,
    type: 'spec',
    subtype: 'engineering_spec',
    name: s.title,
    context: contextOf(s.title, null, s.code, null),
    description: null,
    partId: null,
    partNumber: null,
    creation: (v && pick(SPEC_ORIGIN, v.origin)) || 'unknown',
    latest: v
      ? { id: v.id, label: `v${v.version_no}`, createdAt: v.created_at, publishState: null, availability: stored?.availability ?? null, labels: v.simulated ? ['simulated'] : [], engineering: null }
      : null,
    versionCount: s.versions_count,
    preview: contentPreview(versionContent('spec', stored)),
    upstreamNewer: false,
    formal: [],
    taskWritten: [],
    archived: null,
    updatedAt: v?.created_at ?? s.updated_at ?? s.created_at,
    openPath: specPath(s.id),
    search: searchOf([s.title, s.code]),
  };
}

const time = (iso: string | null) => (iso ? Date.parse(iso) || 0 : 0);
/** Newest first; rows without a time last; the key keeps the order stable. */
const byUpdated = (a: CatalogItem, b: CatalogItem) => time(b.updatedAt) - time(a.updatedAt) || a.key.localeCompare(b.key);

/** The catalogue rows of one project, joined by identity, newest first. */
export function buildProjectCatalog(project: CatalogProject, src: ProjectSources): ProjectCatalog {
  const byId = <T extends { id: UUID }>(xs: readonly T[]) => new Map(xs.map((x) => [x.id, x] as const));
  const ctx: Ctx = {
    project,
    files: byId(src.files),
    documents: byId(src.drawingDocuments),
    parts: byId(src.parts ?? []),
    latestNo: new Map(src.assets.flatMap((a) => (a.latest_version ? [[a.id, a.latest_version.version_no] as const] : []))),
    written: src.taskOutputs?.written ?? new Map(),
    formal: src.taskOutputs?.formal ?? new Map(),
  };
  const assetRows = src.assets.map((a) => assetItem(a, ctx));
  const folded = (pickId: (r: CatalogRef) => UUID | null) => new Set(assetRows.map((i) => pickId(i.ref)).filter((x): x is UUID => !!x));
  const filesInAssets = folded((r) => (r.kind === 'asset' ? r.fileId : null));
  const docsInAssets = folded((r) => (r.kind === 'asset' ? r.drawingDocumentId : null));
  const items = [
    ...assetRows,
    ...src.files.filter((f) => !filesInAssets.has(f.id)).map((f) => fileItem(f, ctx)),
    ...src.drawingDocuments.filter((d) => !docsInAssets.has(d.id)).map((d) => drawingDocumentItem(d, ctx)),
    ...src.specs.items.map((s) => specItem(s, ctx)),
  ].sort(byUpdated);
  const truncated = [src.specs.total > src.specs.items.length ? 'specs' : '', src.taskOutputs?.truncated ? 'task_outputs' : ''].filter(Boolean);
  return { items, truncated };
}

/** Rows of several projects merged into one list, newest first. */
export const mergeCatalogs = (lists: readonly (readonly CatalogItem[])[]): CatalogItem[] => lists.flat().sort(byUpdated);

/**
 * Task-formal versions other than the latest, one entry per version with the tasks that accepted it — shown beside
 * the latest version ("Latest P2 · current task accepted P1", handoff §6), never folded into it.
 */
export function formalBesideLatest(item: Pick<CatalogItem, 'formal' | 'latest'>): { versionId: UUID; label: string; taskNumbers: number[] }[] {
  const out = new Map<UUID, { versionId: UUID; label: string; taskNumbers: number[] }>();
  for (const f of item.formal) {
    if (f.versionId === item.latest?.id) continue;
    const entry = out.get(f.versionId) ?? { versionId: f.versionId, label: f.label, taskNumbers: [] };
    if (f.taskNumber != null && !entry.taskNumbers.includes(f.taskNumber)) entry.taskNumbers.push(f.taskNumber);
    out.set(f.versionId, entry);
  }
  return [...out.values()];
}

// ─── filter, lookup, counts ────────────────────────────────────────────────

const STATUS_TESTS: Record<CatalogStatus, (i: CatalogItem) => boolean> = {
  deprecated: (i) => i.latest?.publishState === 'deprecated',
  upstream_newer: (i) => i.upstreamNewer,
  published: (i) => i.latest?.publishState === 'published',
  failed: (i) => i.latest?.availability === 'failed',
};

/** Items matching the filter: every search word in CatalogItem.search; archived rows only when f.archived. */
export function filterCatalog(items: readonly CatalogItem[], f: CatalogFilter): CatalogItem[] {
  const words = f.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const status = f.status ? STATUS_TESTS[f.status] : null;
  return items.filter(
    (i) =>
      (f.archived || !i.archived) &&
      (!f.type || i.type === f.type) &&
      (!f.subtype || i.subtype === f.subtype) &&
      (!f.creation || i.creation === f.creation) &&
      (!f.partId || i.partId === f.partId) &&
      (!status || status(i)) &&
      words.every((w) => i.search.includes(w)),
  );
}

/** Key prefixes of objects that fold into their asset row, and how that row names them. */
const FOLDED_REFS: Readonly<Record<string, (r: CatalogRef) => UUID | null>> = {
  file: (r) => (r.kind === 'asset' ? r.fileId : null),
  drawing_document: (r) => (r.kind === 'asset' ? r.drawingDocumentId : null),
};

/**
 * The row an `item` URL value names: an exact key, or — for `file:<id>` / `drawing_document:<id>` — the asset row
 * that folded that file / drawing document in (so a fresh upload or an old deep link still finds its row).
 */
export function findCatalogItem(items: readonly CatalogItem[], key: string): CatalogItem | undefined {
  const exact = items.find((i) => i.key === key);
  if (exact) return exact;
  const sep = key.indexOf(':');
  const ofRef = sep > 0 ? pick(FOLDED_REFS, key.slice(0, sep)) : undefined;
  const id = key.slice(sep + 1);
  return ofRef && id ? items.find((i) => ofRef(i.ref) === id) : undefined;
}

export interface CatalogCounts {
  /** Rows that are not archived (the default list). */
  active: number;
  archived: number;
  /** Non-archived rows per business type. */
  byType: Record<BusinessType, number>;
}

export function catalogCounts(items: readonly CatalogItem[]): CatalogCounts {
  const byType = Object.fromEntries(BUSINESS_TYPES.map((t) => [t, 0])) as Record<BusinessType, number>;
  let archived = 0;
  for (const i of items) {
    if (i.archived) archived++;
    else byType[i.type]++;
  }
  return { active: items.length - archived, archived, byType };
}
