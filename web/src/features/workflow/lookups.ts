// Small read caches used to show human names for ids stored in node params
// (file versions, model configs, revisions) without refetching per node.

import { useEffect, useMemo, useState } from 'react';
import { getRevision, listFiles, listModelConfigs } from '../../api/endpoints';
import type { FileVersion, ModelConfig, ModelRevision, ProjectFile, UUID } from '../../api/types';
import { useResource } from '../../hooks/useResource';

export interface FileVersionInfo {
  file: ProjectFile;
  version: FileVersion;
}

export interface Lookups {
  files: ProjectFile[] | undefined;
  filesError: unknown;
  reloadFiles: () => void;
  modelConfigs: ModelConfig[] | undefined;
  modelConfigsError: unknown;
  fileVersion: (id: unknown) => FileVersionInfo | undefined;
  modelConfig: (id: unknown) => ModelConfig | undefined;
}

export function useLookups(projectId: UUID | undefined): Lookups {
  const files = useResource((signal) => listFiles(projectId!, {}, { signal }), [projectId], { enabled: !!projectId });
  const configs = useResource((signal) => listModelConfigs({ signal }), []);
  return useMemo(() => {
    const fv = new Map<string, FileVersionInfo>();
    for (const f of files.data ?? []) for (const v of f.versions) fv.set(v.id, { file: f, version: v });
    const mc = new Map((configs.data ?? []).map((c) => [c.id, c]));
    return {
      files: files.data,
      filesError: files.error,
      reloadFiles: files.reload,
      modelConfigs: configs.data,
      modelConfigsError: configs.error,
      fileVersion: (id: unknown) => (typeof id === 'string' ? fv.get(id) : undefined),
      modelConfig: (id: unknown) => (typeof id === 'string' ? mc.get(id) : undefined),
    };
  }, [files.data, files.error, files.reload, configs.data, configs.error]);
}

// Revisions: module-level cache so every node card / picker shares one fetch.
const revisionCache = new Map<string, Promise<ModelRevision | null>>();

export function rememberRevision(rev: ModelRevision): void {
  revisionCache.set(rev.id, Promise.resolve(rev));
}

export function fetchRevisionCached(id: string): Promise<ModelRevision | null> {
  let p = revisionCache.get(id);
  if (!p) {
    p = getRevision(id).catch(() => null);
    revisionCache.set(id, p);
  }
  return p;
}

/** The revision behind an id (null while loading or when it cannot be read). */
export function useRevision(id: unknown): ModelRevision | null {
  const [rev, setRev] = useState<ModelRevision | null>(null);
  useEffect(() => {
    if (typeof id !== 'string' || !id) {
      setRev(null);
      return;
    }
    let alive = true;
    void fetchRevisionCached(id).then((r) => alive && setRev(r));
    return () => {
      alive = false;
    };
  }, [id]);
  return rev;
}
