import type { CatalogItem } from './types';

/** Adjacent previews follow the current filtered order exactly; do not infer neighbors for external links outside the results. */
export function catalogNeighbors(rows: readonly Pick<CatalogItem, 'key'>[], key: string | null) {
  const index = rows.findIndex((row) => row.key === key);
  return {
    index,
    total: rows.length,
    previous: index > 0 ? rows[index - 1].key : null,
    next: index >= 0 && index + 1 < rows.length ? rows[index + 1].key : null,
  };
}
