// React side of the asset center URL state: read the query, patch it (filters
// reset paging, replace history so back leaves the page), and a debounced text
// filter that stays in sync when the URL changes from outside (back / forward).

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useDebounced } from '../../hooks/keyboard';
import { applyPatch, type QueryPatch } from './urlState';

export interface AssetQuery {
  search: URLSearchParams;
  /** Set / delete keys; any non-paging change resets `offset`. */
  patch: (p: QueryPatch) => void;
  /** Delete the given keys. */
  clear: (keys: readonly string[]) => void;
}

export function useAssetQuery(): AssetQuery {
  const [search, setSearch] = useSearchParams();
  const patch = useCallback((p: QueryPatch) => setSearch((prev) => applyPatch(prev, p), { replace: true }), [setSearch]);
  const clear = useCallback(
    (keys: readonly string[]) => {
      const p: QueryPatch = {};
      for (const k of keys) p[k] = null;
      setSearch((prev) => applyPatch(prev, p), { replace: true });
    },
    [setSearch],
  );
  return { search, patch, clear };
}

/**
 * A search box bound to a URL key: typing is local and debounced before it
 * reaches the URL; an external URL change (browser back) updates the box.
 */
export function useUrlSearchText(query: AssetQuery, key = 'q', ms = 250): [string, (v: string) => void] {
  const urlValue = query.search.get(key) ?? '';
  const [input, setInput] = useState(urlValue);
  const debounced = useDebounced(input.trim(), ms);
  const { patch } = query;
  useEffect(() => {
    if (debounced !== urlValue) patch({ [key]: debounced });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);
  useEffect(() => {
    setInput((cur) => (cur.trim() === urlValue ? cur : urlValue));
  }, [urlValue]);
  return [input, setInput];
}
