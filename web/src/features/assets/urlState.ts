// URL-backed filter state of the asset center (design §5.3): every tab keeps
// its own filters and paging in the query string so refresh and browser back
// restore them. Pure helpers here (unit-tested); the hook is in useAssetQuery.ts.

/** Query keys the asset center owns (requirements §6.2 front-end routing contract) plus the catalogue extras. */
export const ASSET_QUERY_KEYS = [
  'q',
  'run_id',
  'node_id',
  'kind',
  'asset_type',
  'availability',
  'publish_state',
  'task_status',
  'limit',
  'offset',
  'collection_id',
  'collection_version_id',
  'label',
  'part_id',
  'archived',
  'reusable',
] as const;

export type QueryPatch = Record<string, string | number | boolean | null | undefined>;

/** Keys whose change does not reset paging. */
const PAGING_KEYS = new Set(['offset', 'limit']);

/**
 * Apply a patch to a query string: empty / false / null values delete the key.
 * Changing anything but the paging keys resets `offset` (a new filter starts
 * on the first page).
 */
export function applyPatch(search: URLSearchParams, patch: QueryPatch): URLSearchParams {
  const next = new URLSearchParams(search);
  let filterChanged = false;
  for (const [k, raw] of Object.entries(patch)) {
    const v = raw === undefined || raw === null || raw === '' || raw === false ? null : raw === true ? '1' : String(raw);
    const before = next.get(k);
    if (v === null) next.delete(k);
    else next.set(k, v);
    if (!PAGING_KEYS.has(k) && (before ?? null) !== v) filterChanged = true;
  }
  if (filterChanged && !('offset' in patch)) next.delete('offset');
  return next;
}

export interface Paging {
  limit: number;
  offset: number;
  /** Query keys that carried an unusable value and were ignored. */
  invalid: string[];
}

/** limit 1–max (default `def`), offset ≥ 0; anything else falls back and is reported. */
export function readPaging(search: URLSearchParams, def = 50, max = 100): Paging {
  const invalid: string[] = [];
  let limit = def;
  const rawLimit = search.get('limit');
  if (rawLimit !== null) {
    const n = Number(rawLimit);
    if (Number.isInteger(n) && n >= 1 && n <= max) limit = n;
    else invalid.push('limit');
  }
  let offset = 0;
  const rawOffset = search.get('offset');
  if (rawOffset !== null) {
    const n = Number(rawOffset);
    if (Number.isInteger(n) && n >= 0) offset = n;
    else invalid.push('offset');
  }
  return { limit, offset, invalid };
}

export interface EnumParam<T extends string> {
  value: T | '';
  /** The key carried a value outside the allowed set (ignored). */
  invalid: boolean;
}

/** A query value restricted to a closed set; unknown values are ignored and reported, never sent to the server. */
export function enumParam<T extends string>(search: URLSearchParams, key: string, allowed: readonly T[]): EnumParam<T> {
  const raw = search.get(key);
  if (raw === null || raw === '') return { value: '', invalid: false };
  return (allowed as readonly string[]).includes(raw) ? { value: raw as T, invalid: false } : { value: '', invalid: true };
}

/** Query string of the current tab without the `tab` key (what the tab memory stores). */
export function stripTab(search: URLSearchParams): string {
  const next = new URLSearchParams(search);
  next.delete('tab');
  return next.toString();
}

/** Page index (1-based) and page count for a pager. */
export function pageInfo(total: number, limit: number, offset: number): { page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(Math.max(0, total) / Math.max(1, limit)));
  const page = Math.min(pages, Math.floor(Math.max(0, offset) / Math.max(1, limit)) + 1);
  return { page, pages };
}
