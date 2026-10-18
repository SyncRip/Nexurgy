// Pure model of the catalogue detail and "Add material" (docs/resources-knowledge-handoff.zh-CN.md §4.1, §6–§9).
// One detail is driven by the item's real identity (`ref.kind` / asset type): where its versions come from, which
// existing mutation API it has (file new version, file / drawing-document rename and archive, asset-version publish)
// and which role the server asks for. How a version is shown and which page opens it come from the catalogue's per
// asset type rules (catalog.ts), the same ones the list uses. Versions are normalised from the source that owns them;
// a requested version that is not one of them is an explicit error, never "the latest" (same rule as AssetDetailPage
// D07). No requests here.

import type { AssetUsages, AssetVersion } from '../../api/assets';
import type { SpecVersionBrief } from '../../api/spec';
import { specPath } from '../../api/spec';
import type { FileVersion, Project, UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { canInProject } from '../../auth/permissions';
import { assetPage, assetRule, assetStored, fileStored, specStored, versionContent, type ContentMode, type StoredContent, type VersionContent } from './catalog';
import type { CatalogItem, CatalogRef, ViewScope } from './types';

// ─── capabilities ───────────────────────────────────────────────────────────

/** Where the versions of an item come from. */
export type VersionSource = 'asset' | 'spec' | 'file' | 'none';
/** The object a PATCH renames / archives: a File (name, part) or a DrawingDocument (title, description). */
export interface PatchTarget {
  api: 'file' | 'drawing_document';
  id: UUID;
}

export interface DetailCaps {
  versions: VersionSource;
  content: ContentMode;
  /** The stored file behind the item: upload a new version (POST /files/{id}/versions). */
  fileId: UUID | null;
  /** Rename / relink and archive / restore (PATCH /files/{id}, PATCH /drawing-documents/{id}); other objects have neither. */
  target: PatchTarget | null;
  /** Deprecate / restore use of the selected version (POST /asset-versions/{id}/publish). */
  publish: boolean;
}

type RefCaps = { [K in CatalogRef['kind']]: (ref: Extract<CatalogRef, { kind: K }>) => DetailCaps };

const REF_CAPS: RefCaps = {
  asset: (r) => ({
    versions: 'asset',
    content: assetRule(r.assetType).content ?? 'page',
    fileId: r.fileId,
    target: r.fileId ? { api: 'file', id: r.fileId } : r.drawingDocumentId ? { api: 'drawing_document', id: r.drawingDocumentId } : null,
    publish: true,
  }),
  spec: () => ({ versions: 'spec', content: 'spec', fileId: null, target: null, publish: false }),
  file: (r) => ({ versions: 'file', content: 'file', fileId: r.fileId, target: { api: 'file', id: r.fileId }, publish: false }),
  drawing_document: (r) => ({ versions: 'none', content: 'page', fileId: null, target: { api: 'drawing_document', id: r.documentId }, publish: false }),
};

export function detailCaps(ref: CatalogRef): DetailCaps {
  return (REF_CAPS[ref.kind] as (r: CatalogRef) => DetailCaps)(ref);
}

/** Roles of the actions the detail offers. */
export type RoleAction = 'design' | 'process' | 'quote' | 'workflow';

/** Role that publishes / deprecates a version of an asset type (the catalogue's per type rule; mirrors api/assets.py publish). */
export const publishRole = (assetType: string): RoleAction => assetRule(assetType).role ?? 'design';

/** Role that edits an item's content in its professional page (specs: any workflow role; else as publishing). */
export function editRole(item: Pick<CatalogItem, 'ref'>): RoleAction {
  if (item.ref.kind === 'spec') return 'workflow';
  return item.ref.kind === 'asset' ? publishRole(item.ref.assetType) : 'design';
}

// ─── versions ───────────────────────────────────────────────────────────────

/** One version of the item, whatever source owns it. */
export interface DetailVersion {
  /** Asset version id / spec version id / file version id — the `version` URL value. */
  id: UUID;
  label: string;
  versionNo: number;
  createdAt: string | null;
  createdBy: UUID | null;
  /** Change note recorded with the version (file version note, model revision note). */
  note: string | null;
  /** What it stores that the browser could show (the catalogue's content rule reads it). */
  stored: StoredContent;
  asset: AssetVersion | null;
  spec: SpecVersionBrief | null;
}

const summaryStr = (s: Record<string, unknown>, k: string): string | null => (typeof s[k] === 'string' && s[k] ? (s[k] as string) : null);

export function fromAssetVersion(v: AssetVersion): DetailVersion {
  return {
    id: v.id,
    label: v.label,
    versionNo: v.version_no,
    createdAt: v.created_at,
    createdBy: v.created_by,
    note: summaryStr(v.target.summary ?? {}, 'note'),
    stored: assetStored(v),
    asset: v,
    spec: null,
  };
}

export function fromFileVersion(v: FileVersion, fileKind: string | null): DetailVersion {
  return {
    id: v.id,
    label: `v${v.version}`,
    versionNo: v.version,
    createdAt: v.created_at,
    createdBy: null,
    note: v.note || null,
    stored: fileStored(v, fileKind),
    asset: null,
    spec: null,
  };
}

export function fromSpecVersion(v: SpecVersionBrief): DetailVersion {
  return {
    id: v.id,
    label: `v${v.version_no}`,
    versionNo: v.version_no,
    createdAt: v.created_at,
    createdBy: v.created_by,
    note: null,
    stored: specStored(v),
    asset: null,
    spec: v,
  };
}

/** Newest first, whatever order the source returned. */
export const newestFirst = (vs: DetailVersion[]): DetailVersion[] => [...vs].sort((a, b) => b.versionNo - a.versionNo);

export interface ResolvedVersion {
  selected: DetailVersion | null;
  latest: DetailVersion | null;
  /** A version was requested and is not one of this item's versions: explicit error, no fallback. */
  missing: boolean;
  isLatest: boolean;
}

/**
 * `requested` null = the latest version. `versions` undefined = still loading. A version is named by its own id or by
 * the id of the exact row it registers (an asset version's target: the file version of an uploaded file, the model
 * revision …) — the same version under another id, never a different one.
 */
export function resolveVersion(versions: readonly DetailVersion[] | undefined, requested: string | null): ResolvedVersion {
  if (!versions) return { selected: null, latest: null, missing: false, isLatest: false };
  const latest = versions[0] ?? null;
  if (!requested) return { selected: latest, latest, missing: false, isLatest: !!latest };
  const hit = versions.find((v) => v.id === requested) ?? versions.find((v) => v.asset?.target.id === requested) ?? null;
  return { selected: hit, latest, missing: !hit, isLatest: !!hit && hit === latest };
}

/** URL value for picking a version: the latest one is "no version" (null = latest). */
export const versionParam = (versions: readonly DetailVersion[], id: string): string | null => (versions[0]?.id === id ? null : id);

// ─── content and professional pages ─────────────────────────────────────────

/** How the selected version is shown — the catalogue's content rule, the one the list's preview state comes from. */
export const contentOf = (caps: Pick<DetailCaps, 'content'>, v: DetailVersion | null): VersionContent => versionContent(caps.content, v?.stored ?? null);

/** The asset version page (version list, publish, provenance) at this exact version, returning to `returnTo`. */
export const assetVersionPath = (assetId: UUID, versionId: UUID, returnTo: string) => paths.assetVersion(assetId, versionId, returnTo || undefined);

/** A professional page path with `return_to` when the page honours it (only the asset version page does). */
export function withReturnTo(path: string, returnTo: string): string {
  if (!returnTo || !path.startsWith('/assets/') || /[?&]return_to=/.test(path)) return path;
  return `${path}${path.includes('?') ? '&' : '?'}return_to=${encodeURIComponent(returnTo)}`;
}

/**
 * The page that shows (and edits) the selected version: the catalogue's page for that asset version (its exact-version
 * page; the object's own page only for the latest version), else the asset version page at that exact version — never
 * a page that would silently show a newer version.
 */
export function pagePath(item: Pick<CatalogItem, 'ref' | 'openPath'>, v: DetailVersion | null, isLatest: boolean, returnTo: string): string | null {
  const ref = item.ref;
  if (ref.kind === 'spec') return specPath(ref.specId, v?.id);
  if (ref.kind === 'file') return null;
  if (ref.kind === 'drawing_document') return item.openPath ?? `/drafting/${ref.documentId}`;
  const a = v?.asset ?? null;
  if (!a) return item.openPath ? withReturnTo(item.openPath, returnTo) : null;
  return assetPage(a, isLatest) ?? assetVersionPath(ref.assetId, a.id, returnTo);
}

// ─── provenance ─────────────────────────────────────────────────────────────

export type VersionOrigin = { kind: 'run'; runId: UUID } | { kind: 'task' } | { kind: 'upload' } | { kind: 'label'; key: string; raw: string } | { kind: 'unknown' };

/**
 * How one version was made, from its own records only (a run, a task's write registration — `taskWritten`, the item's
 * versions a run / agent wrote —, an upload, the recorded source) — never guessed.
 */
export function versionOrigin(v: DetailVersion, taskWritten: readonly UUID[] = []): VersionOrigin {
  if (v.asset?.run_id) return { kind: 'run', runId: v.asset.run_id };
  if (v.asset && taskWritten.includes(v.asset.id)) return { kind: 'task' };
  if (v.asset) {
    if (v.asset.asset_type === 'document') return { kind: 'upload' };
    const o = assetRule(v.asset.asset_type).origin;
    const raw = o ? summaryStr(v.asset.target.summary ?? {}, o.field) : null;
    return o && raw ? { kind: 'label', key: `${o.label}.${raw}`, raw } : { kind: 'unknown' };
  }
  if (v.spec) return { kind: 'label', key: `spec.origin.${v.spec.origin}`, raw: v.spec.origin };
  if (v.stored.file) return { kind: 'upload' };
  return { kind: 'unknown' };
}

/** Edge counts of a version (the previous revision counts upstream, as DependencyGraph shows it). */
export function relationCounts(v: Pick<AssetVersion, 'dependencies' | 'dependents' | 'parent_version_id'>): { upstream: number; downstream: number } {
  const deps = v.dependencies ?? [];
  const parent = v.parent_version_id && !deps.some((e) => e.version_id === v.parent_version_id) ? 1 : 0;
  return { upstream: deps.length + parent, downstream: (v.dependents ?? []).length };
}

/** Total records of one version's usages (the paged groups by their totals). */
export function usageTotal(u: AssetUsages): number {
  const legacy = u.baselines.length + u.deliverables.length + u.submissions.length + u.run_inputs.length + u.run_outputs.length + u.approval_requests.length;
  const paged = (['workflow_resources', 'knowledge_items', 'knowledge_usages'] as const).reduce((n, g) => n + (u.pagination?.[g]?.total ?? u[g]?.length ?? 0), 0);
  return legacy + paged;
}

// ─── actions ────────────────────────────────────────────────────────────────

export type Unavailable =
  | 'no_file_api'
  | 'no_meta_api'
  | 'no_publish_api'
  | 'no_archive_api'
  | 'no_delete_api'
  | 'no_version'
  | 'draft'
  | 'part_archived'
  | 'project_archived'
  | `role_${RoleAction}`;

export type ActionCheck = { ok: true } | { ok: false; why: Unavailable };

const OK: ActionCheck = { ok: true };
const no = (why: Unavailable): ActionCheck => ({ ok: false, why });

export function roleCheck(project: Pick<Project, 'my_role' | 'status'>, action: RoleAction): ActionCheck {
  if (project.status === 'archived') return no('project_archived');
  return canInProject(project, action) ? OK : no(`role_${action}`);
}

export interface ActionContext {
  caps: DetailCaps;
  project: Pick<Project, 'my_role' | 'status'>;
  /** Why the item is hidden from the daily list (null: shown). */
  archived: CatalogItem['archived'];
  /** The selected version (null while none / missing). */
  version: DetailVersion | null;
}

export type DetailAction = 'newVersion' | 'edit' | 'meta' | 'publish' | 'archive' | 'delete';

/**
 * Whether each action can run now, or the reason it cannot — one table, no hidden fake actions. The server takes a new
 * version or a rename of an archived file / drawing document, so being archived blocks neither. Publish can only set
 * `published` or `deprecated`: deprecating a draft would make "restore use" publish a never-reviewed draft, so only a
 * published version is deprecated. Restore undoes the item's own archive; one hidden by its archived part cannot be
 * shown again from here.
 */
export const ACTION_CHECKS: Record<DetailAction, (c: ActionContext, item: Pick<CatalogItem, 'ref'>) => ActionCheck> = {
  newVersion: (c) => (!c.caps.fileId ? no('no_file_api') : roleCheck(c.project, 'design')),
  edit: (c, item) => roleCheck(c.project, editRole(item)),
  meta: (c) => (!c.caps.target ? no('no_meta_api') : roleCheck(c.project, 'design')),
  publish: (c) => {
    const a = c.version?.asset;
    if (!c.caps.publish) return no('no_publish_api');
    if (!a) return no('no_version');
    return a.publish_state === 'draft' ? no('draft') : roleCheck(c.project, publishRole(a.asset_type));
  },
  archive: (c) => (c.archived === 'part' ? no('part_archived') : !c.caps.target ? no('no_archive_api') : roleCheck(c.project, 'design')),
  delete: () => no('no_delete_api'),
};

// ─── add material: save location ────────────────────────────────────────────

export const ADD_ACTIONS = ['upload', 'spec', 'drawing', 'module'] as const;
export type AddAction = (typeof ADD_ACTIONS)[number];

/** Role each "Add material" action needs in the target project (server checks the same). */
export const ADD_ACTION_ROLE: Record<AddAction, RoleAction> = { upload: 'design', spec: 'workflow', drawing: 'design', module: 'process' };

/** Active projects where the user may run the action — the only possible save locations. */
export function saveTargets<P extends Pick<Project, 'my_role' | 'status'>>(projects: readonly P[], action: AddAction): P[] {
  return projects.filter((p) => canInProject(p, ADD_ACTION_ROLE[action]));
}

/** Default save location: the scope's project when the user may save there; no default across projects. */
export function defaultSaveLocation(scope: ViewScope, targets: readonly Pick<Project, 'id'>[]): UUID | '' {
  return scope.kind === 'project' && targets.some((p) => p.id === scope.projectId) ? scope.projectId : '';
}
