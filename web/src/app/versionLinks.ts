// Exact-version links (S6, agent execution layer design §3.3): every link to an output names the version it refers
// to — a model revision ?rev=, a drawing ?drawing=, a process plan ?plan=, a quote ?quote=, an assembly revision
// ?rev=, a spec / asset version ?version= — and the target page opens exactly that version, saying so when it is
// not the current one ("this is a historical version"). Never link an output by its part / assembly alone: that
// opens whatever is newest, not what the run / result produced.

/** The version query parameter of each part workspace tab that shows an output. */
export const PART_VERSION_PARAM = { model: 'rev', drawings: 'drawing', process: 'plan', quote: 'quote' } as const;
export type PartOutputTab = keyof typeof PART_VERSION_PARAM;

type Extra = Record<string, string | number | null | undefined>;

function withQuery(base: string, query: Extra): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  const s = q.toString();
  return s ? `${base}?${s}` : base;
}

/** A part workspace tab opened at one exact output version (without a version: the tab itself). */
export function partOutputPath(partId: string, tab: PartOutputTab, versionId?: string | null, extra: Extra = {}): string {
  return withQuery(`/parts/${partId}/${tab}`, { [PART_VERSION_PARAM[tab]]: versionId, ...extra });
}

/** The assembly workspace at one exact revision. */
export function assemblyRevisionPath(assemblyId: string, revisionId?: string | null, tab = 'structure'): string {
  return withQuery(`/assemblies/${assemblyId}/${tab}`, { rev: revisionId });
}

const ASSET_PART_TAB: Record<string, PartOutputTab | undefined> = { part_model: 'model', drawing: 'drawings', process_plan: 'process', quote: 'quote' };

/** The part workspace tab of an asset version's target, at that exact version (intakes: the process tab, which lists
 * every intake). Null when the asset has no part workspace view. */
export function assetWorkspacePath(assetType: string, target: { id: string; part_id: string | null }): string | null {
  if (!target.part_id) return null;
  if (assetType === 'intake') return `/parts/${target.part_id}/process`;
  const tab = ASSET_PART_TAB[assetType];
  return tab ? partOutputPath(target.part_id, tab, target.id) : null;
}

/** Workflow port values (server/app/workflow/handlers.py) → the exact output they carry. */
export function portValuePath(value: Record<string, unknown>, partId: string | null): string | null {
  if (!partId) return null;
  const id = (k: string) => (typeof value[k] === 'string' && value[k] ? (value[k] as string) : null);
  switch (value.type) {
    case 'model':
      return partOutputPath(partId, 'model', id('revision_id'));
    case 'drawing':
      return partOutputPath(partId, 'drawings', id('drawing_id'));
    case 'plan':
      return partOutputPath(partId, 'process', id('process_plan_id'));
    case 'quote':
      return partOutputPath(partId, 'quote', id('quote_id'));
    case 'spec':
      return `/parts/${partId}/process`;
    default:
      return null;
  }
}

/** A reference's source page at its exact version. Snapshots of assembly revision references taken before S6 name
 * only the assembly; the revision is added from the reference itself. */
export function exactSourcePath(ref: { kind: string; object_id: string; source_path: string }): string {
  const path = ref.source_path;
  if (!path || ref.kind !== 'assembly_revision' || /[?&]rev=/.test(path)) return path;
  return `${path}${path.includes('?') ? '&' : '?'}rev=${encodeURIComponent(ref.object_id)}`;
}

/** Whether the version on screen is not the current one. Unknown (nothing opened, list not loaded) is not historical. */
export function isHistorical(openedId: string | null | undefined, currentId: string | null | undefined): boolean {
  return !!openedId && !!currentId && openedId !== currentId;
}
