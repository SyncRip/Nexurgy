// Loads the versions of one catalogue item from the source that owns them (asset catalogue, engineering spec, project
// file); drawing documents without a saved version have none. Reloads when the catalogue reports a different latest
// version (another tab saved, or this detail did). Versions of a previously shown item are never returned for the
// current one while it loads (useResource keeps stale data during reloads).

import { listAssetVersions } from '../../api/assets';
import { getFile } from '../../api/endpoints';
import { getSpec } from '../../api/spec';
import { useResource } from '../../hooks/useResource';
import { detailCaps, fromAssetVersion, fromFileVersion, fromSpecVersion, newestFirst, type DetailVersion, type VersionSource } from './detailModel';
import type { CatalogItem, CatalogRef } from './types';

type Loader = (ref: CatalogRef, signal: AbortSignal) => Promise<DetailVersion[]>;

const LOADERS: Record<VersionSource, Loader> = {
  asset: async (ref, signal) => (ref.kind === 'asset' ? (await listAssetVersions(ref.assetId, { signal })).map(fromAssetVersion) : []),
  spec: async (ref, signal) => {
    if (ref.kind !== 'spec') return [];
    const spec = await getSpec(ref.specId, { signal });
    return newestFirst((spec.versions ?? (spec.latest_version ? [spec.latest_version] : [])).map(fromSpecVersion));
  },
  file: async (ref, signal) => {
    if (ref.kind !== 'file') return [];
    const f = await getFile(ref.fileId, { signal });
    return newestFirst(f.versions.map((v) => fromFileVersion(v, f.kind)));
  },
  none: async () => [],
};

export interface DetailVersions {
  /** Newest first; undefined until this item's versions are loaded. */
  versions: DetailVersion[] | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

export function useDetailVersions(item: Pick<CatalogItem, 'key' | 'ref' | 'latest' | 'versionCount'>): DetailVersions {
  const source = detailCaps(item.ref).versions;
  const key = item.key;
  const res = useResource(
    async (signal) => ({ key, versions: await LOADERS[source](item.ref, signal) }),
    [key, source, item.latest?.id ?? '', item.versionCount],
  );
  const mine = res.data?.key === key ? res.data.versions : undefined;
  return { versions: mine, error: mine || res.loading ? null : res.error, loading: res.loading, reload: res.reload };
}
