// S5 pure helpers (no React; unit-tested in policyModel.test.ts): where a request's execution policy comes from and
// what an explicit switch means. The server (app/policy.py resolve_policy) is authoritative and records the source;
// these mirror it so the UI can show the policy and its source before anything is sent.

import type { ExecutionPolicy } from '../../api/engineering';
import type { PolicyResolution, PolicySource, SourceVersion, UpstreamAssumption } from '../../api/turbo';

export interface ResolvedPolicy {
  policy: ExecutionPolicy;
  source: PolicySource;
}

/** explicit request > inherited from the source version > project default > standard (design §3.4). Only Turbo counts
 * as a project default: the switch off is the plain default. */
export function resolvePolicy(
  explicit: ExecutionPolicy | null | undefined,
  sourcePolicy?: ExecutionPolicy | string | null,
  projectDefault?: ExecutionPolicy | string | null,
): ResolvedPolicy {
  if (explicit === 'standard' || explicit === 'turbo') return { policy: explicit, source: 'explicit' };
  if (sourcePolicy === 'standard' || sourcePolicy === 'turbo') return { policy: sourcePolicy, source: 'inherited' };
  if (projectDefault === 'turbo') return { policy: 'turbo', source: 'project_default' };
  return { policy: 'standard', source: 'default' };
}

/** The policy a request will be frozen with, given the server's preview (without an explicit choice) and the person's
 * explicit choice (null = follow it). Falls back to the client mirror while the preview loads or failed. */
export function effectivePolicy(
  explicit: ExecutionPolicy | null | undefined,
  preview: Pick<PolicyResolution, 'execution_policy' | 'policy_source' | 'source_version' | 'project_default'> | null | undefined,
  fallback?: { sourcePolicy?: ExecutionPolicy | null; projectDefault?: ExecutionPolicy | null },
): ResolvedPolicy {
  if (explicit === 'standard' || explicit === 'turbo') return { policy: explicit, source: 'explicit' };
  if (preview) return { policy: preview.execution_policy, source: preview.policy_source };
  return resolvePolicy(null, fallback?.sourcePolicy, fallback?.projectDefault);
}

/** What picking ``chosen`` means compared with the source version (null: nothing to explain).
 * - ``confirm_required``: standard over a Turbo spec — a person must confirm its N values first (the server refuses
 *   otherwise);
 * - ``confirm_upstream``: standard over another product with N unconfirmed values — the result stays not verified until
 *   a person confirms them;
 * - ``corrected``: a person corrected values upstream — a standard takeover needs a new version;
 * - ``turbo_over_standard``: Turbo over a standard source — checks are skipped and the result is not verified. */
export type PolicyConsequence =
  | { code: 'confirm_required' | 'confirm_upstream' | 'corrected'; n: number }
  | { code: 'turbo_over_standard' }
  | null;

export function policyConsequence(
  chosen: ExecutionPolicy,
  source: Pick<SourceVersion, 'type' | 'execution_policy'> | null | undefined,
  upstream?: { unconfirmed?: number; corrected?: number } | null,
): PolicyConsequence {
  if (!source) return null;
  if (chosen === 'turbo') return source.execution_policy === 'standard' ? { code: 'turbo_over_standard' } : null;
  const corrected = upstream?.corrected ?? 0;
  const unconfirmed = upstream?.unconfirmed ?? 0;
  if (corrected > 0 && source.execution_policy === 'turbo') return { code: 'corrected', n: corrected };
  if (unconfirmed <= 0) return null;
  if (source.type === 'engineering_spec_version' && source.execution_policy === 'turbo') return { code: 'confirm_required', n: unconfirmed };
  return { code: 'confirm_upstream', n: unconfirmed };
}

export interface OwnerGroup {
  owner: UpstreamAssumption['owner'];
  items: UpstreamAssumption[];
}

/** Upstream assumptions grouped by the version that carries them, in the server's order (spec → assemblies → parts). */
export function groupByOwner(items: readonly UpstreamAssumption[]): OwnerGroup[] {
  const groups = new Map<string, OwnerGroup>();
  for (const item of items) {
    const k = `${item.owner.type}:${item.owner.id}`;
    const g = groups.get(k);
    if (g) g.items.push(item);
    else groups.set(k, { owner: item.owner, items: [item] });
  }
  return [...groups.values()];
}

/** Keys still waiting for a person. */
export function pendingKeys(items: readonly UpstreamAssumption[]): string[] {
  return items.filter((i) => i.state === 'unconfirmed').map((i) => i.key);
}

/** The version an AI workspace request continues from, as the server picks it (services/execution_policy.py
 * request_source): an assembly revision / part revision target, a part target's latest revision for a modeling change
 * or a drawing, the one spec version an assembly_create references. ``kind`` is the operation the request resolves to. */
export function requestPolicySource(
  kind: string | null | undefined,
  target: { kind: string; object_id: string } | null | undefined,
  references: readonly { kind: string; object_id: string }[] = [],
): { source_type: 'engineering_spec_version' | 'assembly_revision' | 'model_revision' | 'part'; source_id: string } | null {
  if (target?.kind === 'assembly_revision') return { source_type: 'assembly_revision', source_id: target.object_id };
  if (target?.kind === 'part_revision' || target?.kind === 'cad_selection') return { source_type: 'model_revision', source_id: target.object_id };
  if (target?.kind === 'part' && (kind === 'model_modify' || kind === 'drawing_generate')) return { source_type: 'part', source_id: target.object_id };
  if (kind === 'assembly_create') {
    const specs = [...new Set([target, ...references].filter((r) => r?.kind === 'engineering_spec_version').map((r) => r!.object_id))];
    if (specs.length === 1) return { source_type: 'engineering_spec_version', source_id: specs[0] };
  }
  return null;
}

/** The one spec version a run graph pins (an ``engineering_spec`` node's ``spec_version_id``, run params applied), as the
 * server picks the source of a run that names no policy (services/execution_policy.py graph_source); null for none or
 * several. */
export function pinnedSpecVersion(
  graph: { nodes: readonly { id: string; type: string; params?: Record<string, unknown> | null }[] } | null | undefined,
  params: Record<string, Record<string, unknown>> = {},
): string | null {
  const ids = new Set<string>();
  for (const n of graph?.nodes ?? []) {
    if (n.type !== 'engineering_spec') continue;
    const id = { ...(n.params ?? {}), ...(params[n.id] ?? {}) }.spec_version_id;
    if (typeof id === 'string' && id) ids.add(id);
  }
  return ids.size === 1 ? [...ids][0] : null;
}

/** The source of a request error that asks for the "confirm these assumptions" entry (a standard run on a Turbo spec):
 * 409 upstream_assumptions_unconfirmed carries the spec version in its details. */
export function confirmSourceOf(error: { code?: string | null; details?: Record<string, unknown> | null } | null | undefined):
  { source_type: 'engineering_spec_version'; source_id: string; count: number } | null {
  if (!error || error.code !== 'upstream_assumptions_unconfirmed') return null;
  const id = error.details?.spec_version_id;
  if (typeof id !== 'string' || !id) return null;
  const n = error.details?.count;
  return { source_type: 'engineering_spec_version', source_id: id, count: typeof n === 'number' ? n : 0 };
}
