// The single "View scope" of resources & knowledge (docs/resources-knowledge-handoff.zh-CN.md §4) and the URL state of the
// materials list. Pure helpers, unit-tested in scope.test.ts.
//
// URL: a project scope is `?project=<uuid>` (every existing paths.resources(tab, projectId) link keeps working);
// `?scope=all` spans accessible projects. Legacy `shared` normalizes to `all`. A URL without either uses the remembered scope
// (if still valid), else the current project. Changing the scope keeps only the scope keys.

import { PROJECT_PARAM, RESOURCE_SCOPE_PARAM, type ResourceTab } from '../../app/nav';
import { isProjectId, type KeyValueStorage } from '../project/currentProject';
import type { Project } from '../../api/types';
import { BUSINESS_TYPES, CATALOG_STATUSES, CREATION_METHODS, type BusinessType, type CatalogFilter, type CatalogStatus, type CreationMethod, type ViewScope } from './types';

export const SCOPE_STORAGE_KEY = 'partroute.resources.scope.v1';

/** Keys that name the scope; every other key belongs to one view and is dropped when the scope changes. */
export const SCOPE_KEYS = [PROJECT_PARAM, RESOURCE_SCOPE_PARAM] as const;

/** Value of the scope `<select>` and of the remembered scope: a project id or `all`. */
export function scopeValue(scope: ViewScope): string {
  return scope.kind === 'project' ? scope.projectId : scope.kind;
}

export function parseScopeValue(v: string | null | undefined): ViewScope | null {
  if (v === 'all' || v === 'shared') return { kind: 'all' };
  return isProjectId(v) ? { kind: 'project', projectId: v } : null;
}

export function sameScope(a: ViewScope | null | undefined, b: ViewScope | null | undefined): boolean {
  return !!a && !!b && scopeValue(a) === scopeValue(b);
}

export interface UrlScope {
  /** The scope the URL names; null when it names none (or an unusable one). */
  scope: ViewScope | null;
  /** The URL carried a scope key with an unusable value (not a UUID, unknown scope name). */
  invalid: boolean;
  /** Normalize a retired scope without dropping object or filter parameters. */
  normalize?: boolean;
}

/** `?project=<uuid>` wins over `?scope=` (it is the established link vocabulary); our own URLs never carry both. */
export function readUrlScope(search: URLSearchParams): UrlScope {
  const project = search.get(PROJECT_PARAM);
  if (project) return isProjectId(project) ? { scope: { kind: 'project', projectId: project }, invalid: false } : { scope: null, invalid: true };
  const s = search.get(RESOURCE_SCOPE_PARAM);
  if (!s) return { scope: null, invalid: false };
  if (s === 'shared') return { scope: { kind: 'all' }, invalid: false, normalize: true };
  return s === 'all' ? { scope: { kind: 'all' }, invalid: false } : { scope: null, invalid: true };
}

/** Only the scope keys of a scope. */
export function scopeSearch(scope: ViewScope): URLSearchParams {
  const q = new URLSearchParams();
  if (scope.kind === 'project') q.set(PROJECT_PARAM, scope.projectId);
  else q.set(RESOURCE_SCOPE_PARAM, scope.kind);
  return q;
}

/** The query with its scope keys replaced by `scope`'s and every other key kept (naming the scope of a URL that had none). */
export function withScope(search: URLSearchParams, scope: ViewScope): URLSearchParams {
  const out = scopeSearch(scope);
  search.forEach((v, k) => {
    if (!(SCOPE_KEYS as readonly string[]).includes(k)) out.append(k, v);
  });
  return out;
}

/** URL of a resources view in a scope; only the scope keys (plus `extra`, e.g. the item to open) survive. */
export function scopeUrl(view: ResourceTab, scope: ViewScope, extra: Record<string, string> = {}): string {
  const q = scopeSearch(scope);
  for (const [k, v] of Object.entries(extra)) if (v) q.set(k, v);
  return `/resources/${view}?${q.toString()}`;
}

// ─── remembered scope ───────────────────────────────────────────────────────

/** localStorage when it can be used, else null (private mode, blocked site data). */
export function scopeStorage(): KeyValueStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The scope this user viewed last in this browser (one entry; another user's entry is ignored). */
export function readSavedScope(storage: KeyValueStorage | null, userId: string): ViewScope | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(SCOPE_STORAGE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as { user?: unknown; scope?: unknown };
    if (v.user !== userId || typeof v.scope !== 'string') return null;
    return parseScopeValue(v.scope);
  } catch {
    return null;
  }
}

export function writeSavedScope(storage: KeyValueStorage | null, userId: string, scope: ViewScope): void {
  if (!storage) return;
  try {
    storage.setItem(SCOPE_STORAGE_KEY, JSON.stringify({ user: userId, scope: scopeValue(scope) }));
  } catch {
    /* quota / blocked storage: the scope lasts until a reload */
  }
}

// ─── resolution ─────────────────────────────────────────────────────────────

/** Why the page shows another scope than the one asked for (shown to the user). */
export type ScopeFallbackReason = 'url_invalid' | 'url_project' | 'saved_project';

export interface ScopeResolution {
  /** The scope to show; null while the project list it depends on is loading (or the user has no project). */
  scope: ViewScope | null;
  /** Set when the requested scope could not be used and `scope` replaces it. */
  fallback: ScopeFallbackReason | null;
  /** Where the scope came from: the URL (no rewrite needed) or elsewhere (the URL should be rewritten to name it). */
  fromUrl: boolean;
}

export interface ScopeInputs {
  url: UrlScope;
  /** Remembered scope (readSavedScope). */
  saved: ViewScope | null;
  /** The shell's current project id (CurrentProject). */
  current: string | null;
  /** Accessible (listed) projects; undefined while loading. */
  projects: readonly Pick<Project, 'id' | 'status'>[] | undefined;
  /** Project ids the server refused (403 / 404). */
  unavailable: ReadonlySet<string>;
}

/**
 * The available scope to fall back to: the current project if listed and usable, else the first active project,
 * else the first project, else all projects. null while the list is loading.
 */
export function fallbackScope(projects: ScopeInputs['projects'], current: string | null, unavailable: ReadonlySet<string>): ViewScope | null {
  if (!projects) return null;
  const usable = projects.filter((p) => !unavailable.has(p.id));
  const pick = usable.find((p) => p.id === current) ?? usable.find((p) => p.status !== 'archived') ?? usable[0];
  return pick ? { kind: 'project', projectId: pick.id } : { kind: 'all' };
}

/**
 * The scope to show:
 * 1. the URL's — a project the server refused (or an unusable value) falls back with a reason; a project that is not
 *    listed is still tried (an archived project is not listed but can be opened; the server decides);
 * 2. else the remembered one — a remembered project must still be listed, else it falls back with a reason;
 * 3. else the current project once the list confirms it, else the first active project; with no project at all, all
 *    projects.
 */
export function resolveScope({ url, saved, current, projects, unavailable }: ScopeInputs): ScopeResolution {
  if (url.scope) {
    if (url.scope.kind !== 'project' || !unavailable.has(url.scope.projectId)) return { scope: url.scope, fallback: null, fromUrl: !url.normalize };
    return { scope: fallbackScope(projects, current === url.scope.projectId ? null : current, unavailable), fallback: 'url_project', fromUrl: false };
  }
  if (url.invalid) return { scope: fallbackScope(projects, current, unavailable), fallback: 'url_invalid', fromUrl: false };
  if (saved) {
    if (saved.kind !== 'project') return { scope: saved, fallback: null, fromUrl: false };
    if (!projects) return { scope: null, fallback: null, fromUrl: false };
    if (projects.some((p) => p.id === saved.projectId) && !unavailable.has(saved.projectId)) return { scope: saved, fallback: null, fromUrl: false };
    return { scope: fallbackScope(projects, current === saved.projectId ? null : current, unavailable), fallback: 'saved_project', fromUrl: false };
  }
  // The current project only once the list confirms it (a stored id the user lost access to is not tried).
  return { scope: fallbackScope(projects, current, unavailable), fallback: null, fromUrl: false };
}

// ─── materials URL state ────────────────────────────────────────────────────

/** Query keys of the materials view (all dropped on a scope change). */
export const MATERIAL_KEYS = ['q', 'type', 'subtype', 'source', 'part_id', 'status', 'archived', 'item', 'version'] as const;
/** The filter keys ("Clear filters" clears these; the open item stays). */
export const MATERIAL_FILTER_KEYS = ['q', 'type', 'subtype', 'source', 'part_id', 'status', 'archived'] as const;

const CREATION_FILTERS: readonly (CreationMethod | '')[] = [...CREATION_METHODS, 'unknown'];

export interface MaterialQuery {
  filter: CatalogFilter;
  /** Keys whose value was unusable and ignored. */
  invalid: string[];
}

function oneOf<T extends string>(search: URLSearchParams, key: string, allowed: readonly T[], invalid: string[]): T | '' {
  const raw = search.get(key);
  if (!raw) return '';
  if ((allowed as readonly string[]).includes(raw)) return raw as T;
  invalid.push(key);
  return '';
}

/**
 * The catalogue filter a URL asks for. `subtype` only narrows a chosen type; the part filter needs a project, so
 * `part_id` is ignored outside a project scope.
 */
export function readMaterialQuery(search: URLSearchParams, scope: ViewScope): MaterialQuery {
  const invalid: string[] = [];
  const type = oneOf<BusinessType>(search, 'type', BUSINESS_TYPES, invalid);
  const rawSubtype = search.get('subtype') ?? '';
  if (rawSubtype && !type) invalid.push('subtype');
  const partId = search.get('part_id') ?? '';
  if (partId && scope.kind !== 'project') invalid.push('part_id');
  return {
    filter: {
      q: (search.get('q') ?? '').trim(),
      type,
      subtype: type ? rawSubtype : '',
      creation: oneOf<CreationMethod | ''>(search, 'source', CREATION_FILTERS, invalid),
      partId: scope.kind === 'project' ? partId : '',
      status: oneOf<CatalogStatus>(search, 'status', CATALOG_STATUSES, invalid),
      archived: search.get('archived') === '1',
    },
    invalid,
  };
}

export function isFiltered(f: CatalogFilter): boolean {
  return !!(f.q || f.type || f.subtype || f.creation || f.partId || f.status || f.archived);
}

/** Accessible projects plus the scope's own project when it is not listed (an archived project opened by link). */
export function withScopeProject(projects: readonly Project[] | undefined, scopeProject: Project | null): readonly Project[] | null {
  if (!projects) return scopeProject ? [scopeProject] : null;
  if (!scopeProject || projects.some((p) => p.id === scopeProject.id)) return projects;
  return [...projects, scopeProject];
}
