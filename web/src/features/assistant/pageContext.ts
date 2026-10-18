import { AI_REF_KINDS,type AiRef,type AiRefInput } from '../../api/assistant';

export interface PendingReference {
  input: AiRefInput & { project_id: string };
  label: string;
  target?: boolean;
  key?: string;
  automatic?: boolean;
  origin?: 'page' | 'selection' | 'manual';
}
export interface PageContext {
  projectId: string;
  label: string;
  scopeKey?: string;
  object?: { type: string; id: string; version: string | number | null };
  target?: PendingReference['input'] | null;
  references?: PendingReference[];
  unavailable?: string | null;
  partId?: string;
  taskId?: string;
  /** Temporary adapter while retained historical pages are migrated. */
  suggestions?: (AiRefInput & { project_id: string; label: string })[];
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, canonical(v)]));
}
export function refIdentity(ref: AiRefInput & { project_id?: string }): string {
  const normalized = inputOnly({ ...ref, project_id: ref.project_id ?? '' }) as unknown as Record<string, unknown>;
  if (ref.selection) {
    const { entity, id, build_key, mesh_sha, resolved_op_id } = ref.selection;
    normalized.selection = { entity, id, build_key, mesh_sha, resolved_op_id: resolved_op_id ?? null };
  }
  if (ref.entities) normalized.entities = { ...ref.entities, ids: [...new Set(ref.entities.ids)].sort() };
  return JSON.stringify(canonical(normalized));
}
export function pageScope(page: PageContext): string {
  return page.scopeKey ?? `${page.projectId}:${page.object ? `${page.object.type}:${page.object.id}:${page.object.version ?? ''}` : page.target ? refIdentity(page.target) : page.suggestions?.[0] ? refIdentity(page.suggestions[0]) : page.partId ?? 'project'}`;
}
/** The work-panel lock freezes the target within one saved object, never across versions/projects. */
export function lockPageTarget(previous: PageContext | null, next: PageContext | null, locked: boolean): PageContext | null {
  if (!locked || !previous || !next || pageScope(previous) !== pageScope(next)) return next;
  return { ...next, label: previous.label, target: previous.target, references: previous.references };
}
export function pageReferences(page: PageContext | null): PendingReference[] {
  if (!page) return [];
  if (page.target !== undefined || page.references) return [
    ...(page.target ? [{ input: inputOnly(page.target), label: page.label, target: true }] : []), ...(page.references ?? []),
  ].map((entry) => ({ ...entry, origin: 'page', automatic: true }));
  // Transitional declarations choose the version actually shown, not every offered "latest" suggestion.
  const first = page.suggestions?.[0];
  return first ? [{ input: inputOnly(first), label: first.label, target: true, origin: 'page', automatic: true }] : [];
}
export function inputOnly(value: AiRefInput & { project_id: string }): PendingReference['input'] {
  const { kind, object_id, project_id, selection, field, excerpt, output, node, instance, entities, version } = value;
  return { kind, object_id, project_id, ...(selection ? { selection } : {}), ...(field ? { field: { tier_qty: field.tier_qty, key: field.key } } : {}), ...(excerpt ? { excerpt: { source_kind: excerpt.source_kind, text: excerpt.text } } : {}), ...(output ? { output: { run_id: output.run_id, node_id: output.node_id, output_key: output.output_key } } : {}), ...(node ? { node: { node_id: node.node_id } } : {}), ...(instance ? { instance: { path: [...instance.path] } } : {}), ...(entities ? { entities: { ids: [...entities.ids], ...(entities.sheet_id ? { sheet_id: entities.sheet_id } : {}) } } : {}), ...(version !== undefined ? { version } : {}) };
}
export function pendingFromRef(ref: AiRef, target = false): PendingReference {
  return { input: inputOnly({ ...ref, selection: ref.selection ?? undefined, field: ref.field ?? undefined, excerpt: ref.excerpt ?? undefined, output: ref.output ?? undefined, node: ref.node ?? undefined, version: ref.kind === 'workflow' ? ref.version ?? undefined : undefined }), label: [ref.title, ref.version_label].filter(Boolean).join(' · '), target, key: ref.key };
}
export function visibleReferences(entries: PendingReference[], suppressed: readonly string[]): PendingReference[] {
  const seen = new Set<string>();
  let target = false;
  return entries.flatMap((entry) => {
    const identity = refIdentity(entry.input);
    if (seen.has(identity) || suppressed.includes(identity)) return [];
    seen.add(identity);
    const isTarget = !!entry.target && !target;
    target ||= isTarget;
    return [{ ...entry, target: isTarget }];
  });
}
export function parsePending(value: unknown): PendingReference[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return [];
    const item = raw as Partial<PendingReference>;
    const input = item.input;
    if (!input || !AI_REF_KINDS.includes(input.kind) || typeof input.object_id !== 'string' || typeof input.project_id !== 'string' || typeof item.label !== 'string') return [];
    if (input.version !== undefined && (!Number.isInteger(input.version) || input.version < 1)) return [];
    if (input.instance && (!Array.isArray(input.instance.path) || !input.instance.path.length || input.instance.path.length > 10 || !input.instance.path.every((id) => typeof id === 'string' && id.length >= 1 && id.length <= 40))) return [];
    if (input.entities && (!Array.isArray(input.entities.ids) || !input.entities.ids.length || input.entities.ids.length > 20_000 || !input.entities.ids.every((id) => typeof id === 'string' && id.length >= 1 && id.length <= 64) || (input.entities.sheet_id !== undefined && (typeof input.entities.sheet_id !== 'string' || !input.entities.sheet_id.length || input.entities.sheet_id.length > 40)))) return [];
    if (input.node && typeof input.node.node_id !== 'string') return [];
    if (input.field && (!Number.isInteger(input.field.tier_qty) || input.field.tier_qty < 1 || typeof input.field.key !== 'string')) return [];
    if (input.excerpt && (typeof input.excerpt.text !== 'string' || typeof input.excerpt.source_kind !== 'string')) return [];
    if (input.output && ![input.output.run_id, input.output.node_id, input.output.output_key].every((v) => typeof v === 'string')) return [];
    if (input.selection && (!['face', 'edge', 'op'].includes(input.selection.entity) || !['string', 'number'].includes(typeof input.selection.id) || typeof input.selection.build_key !== 'string' || typeof input.selection.mesh_sha !== 'string')) return [];
    return [{ input: inputOnly(input), label: item.label, target: item.target === true, automatic: item.automatic === true, ...(item.origin === 'page' || item.origin === 'selection' || item.origin === 'manual' ? { origin: item.origin } : {}), ...(typeof item.key === 'string' ? { key: item.key } : {}) }];
  });
}

export interface PendingContext {
  scope: string | null;
  references: PendingReference[];
  suppressed: string[];
}
export const EMPTY_CONTEXT: PendingContext = { scope: null, references: [], suppressed: [] };

/** Scope changes discard the previous object's selection. Polling preserves explicit removal. */
export function declareContext(current: PendingContext, page: PageContext | null): PendingContext {
  if (!page) return EMPTY_CONTEXT;
  const scope = pageScope(page);
  if (scope !== current.scope) return { scope, references: pageReferences(page), suppressed: [] };
  const incoming = pageReferences(page);
  const inputs = new Set(incoming.map((r) => refIdentity(r.input)));
  const retained = current.references.filter((r) => r.input.project_id === page.projectId && r.origin !== 'page' && !(page.unavailable && r.automatic) && !inputs.has(refIdentity(r.input)));
  return { scope, references: visibleReferences([...retained, ...incoming], current.suppressed), suppressed: current.suppressed };
}
export function removePending(current: PendingContext, identity: string): PendingContext {
  return { ...current, references: current.references.filter((r) => refIdentity(r.input) !== identity), suppressed: [...new Set([...current.suppressed, identity])] };
}
export function queuePending(current: PendingContext, entry: PendingReference): PendingContext {
  const identity = refIdentity(entry.input);
  if (entry.automatic && current.suppressed.includes(identity)) return current;
  const sameProject = current.references.every((r) => r.input.project_id === entry.input.project_id);
  return { scope: sameProject ? current.scope : null,
    references: visibleReferences([{ ...entry, origin: entry.origin ?? (entry.automatic ? 'selection' : 'manual') }, ...(sameProject ? current.references.filter((r) => refIdentity(r.input) !== identity && !(entry.automatic && r.origin === 'selection' && r.input.kind === entry.input.kind)) : [])], []),
    suppressed: sameProject ? current.suppressed.filter((s) => s !== identity) : [] };
}

/** A new conversation starts from its own draft and the current page, never the previous conversation's manual refs. */
export function restoreContext(current: PendingContext, restored: PendingContext, page: PageContext | null): PendingContext {
  if (page && pageScope(page) !== restored.scope) return declareContext(EMPTY_CONTEXT, page);
  if (page) return declareContext(restored, page);
  if (current.scope && current.scope !== restored.scope) return current;
  return restored;
}

export interface PageRegistration { owner?: symbol; page: PageContext | null; handedOver: boolean }
export function registerPage(current: PageRegistration, page: PageContext | null, owner?: symbol): PageRegistration {
  if (!page && (owner !== current.owner || current.handedOver)) return current;
  return { owner, page, handedOver: false };
}
export function sendFingerprint(projectId: string, text: string, references: PendingReference[], revise?: boolean): string {
  return JSON.stringify([projectId, text.trim(), references.map((r) => [refIdentity(r.input), !!r.target]), revise ?? null]);
}
