import { operationChoices } from './capabilities';
// Static assemblies (W4) API: server/app/api/assemblies.py. Revisions are immutable; every edit posts ops against
// the latest revision as base (409 revision_conflict otherwise). Execution policy is explicit per request.

import { request, type ApiError } from './client';
import type { Assumption, CheckStatus, AcceptanceStatus, ExecutionPolicy, Matrix4, ProcurementKind, RepresentationLevel, Verification } from './engineering';
import type { AssemblyCoverage } from './orchestration';

type UUID = string;
type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

export type DefinitionKind = 'part' | 'assembly';
export type MateKind = 'fixed' | 'coaxial' | 'planar' | 'parallel' | 'perpendicular' | 'offset' | 'angle' | 'pattern_match';
export type MateStatus = 'pending' | 'solved' | 'redundant' | 'conflict' | 'broken' | 'invalid' | 'dropped' | 'suppressed' | 'skipped_turbo';
export type InstanceSolveStatus = 'fixed' | 'constrained' | 'under_constrained' | 'floating' | 'unplaced' | 'auto_placed' | 'suppressed';

export const MATE_KINDS: MateKind[] = ['fixed', 'coaxial', 'planar', 'parallel', 'perpendicular', 'offset', 'angle', 'pattern_match'];

export interface AssemblyStats {
  leaf_instances: number;
  part_definitions: number;
  assembly_definitions: number;
  subassembly_instances: number;
  suppressed_instances: number;
  depth: number;
  direct_children: number;
}

export interface RevisionBrief {
  id: UUID;
  assembly_id: UUID;
  rev_no: number;
  label: string;
  parent_id: UUID | null;
  source: 'manual' | 'import' | 'ai' | 'copy';
  execution_policy: ExecutionPolicy;
  status: 'ready' | 'failed';
  check_status: CheckStatus;
  acceptance_status: AcceptanceStatus;
  verification: Verification;
  /** Stored build coverage, projected without the full spec; absent for older servers and manual/imported assemblies. */
  coverage?: AssemblyCoverage | null;
  stats: AssemblyStats;
  bom_sha256: string | null;
  content_sha256: string;
  note: string;
  change: { ops?: Record<string, unknown>[]; affected_instances?: string[]; notes?: Note[] };
  created_by: UUID;
  created_at: string;
}

export interface Assembly {
  id: UUID;
  project_id: UUID;
  number: string;
  name: string;
  description: string;
  status: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  used_by_assemblies: number;
  latest_revision: RevisionBrief | null;
}

export interface DefinitionRef {
  kind: DefinitionKind;
  id: UUID;
  part_id?: UUID;
  assembly_id?: UUID;
  number: string | null;
  name: string | null;
  rev_no: number | null;
  label: string | null;
  glb_sha?: string | null;
  mesh_sha?: string | null;
  bbox?: { min: number[]; max: number[]; size: number[] } | null;
  material?: string | null;
}

export interface Component {
  instance_id: string;
  name: string;
  definition: DefinitionRef;
  transform: Matrix4;
  fixed: boolean;
  suppressed: boolean;
  placement: { mode?: string; source?: string };
  display: { color?: string; hidden?: boolean };
  source_ref: string | null;
  meta?: Record<string, unknown>;
  update_available: { latest_revision_id: UUID; latest_rev_no: number } | null;
}

export interface MateSide {
  instance: string;
  datum: string;
  definition_revision_id?: string;
}

export interface Mate {
  mate_id: string;
  kind: MateKind;
  a: MateSide;
  b: MateSide | null;
  params: Record<string, unknown>;
  priority: number;
  suppressed: boolean;
  source: string;
  status: MateStatus;
  result: { residual?: { position_mm: number; angle_rad: number }; message?: string; rebound?: unknown[] };
}

export interface Note {
  code: string;
  message: string;
  subject?: string;
  details?: Record<string, unknown>;
}

export interface SolveInstanceInfo {
  status: InstanceSolveStatus;
  dof: number | null;
  free: { motion: 'translate' | 'rotate'; axis: number[] }[];
  message?: string;
  over_constrained?: boolean;
}

export interface CheckRecord {
  check: string;
  status: CheckStatus;
  policy?: ExecutionPolicy;
  at?: string;
  [k: string]: unknown;
}

export interface AssemblyRevision extends RevisionBrief {
  assembly: { id: UUID; number: string; name: string } | null;
  is_latest: boolean;
  latest_revision_id: UUID | null;
  components: Component[];
  mates: Mate[];
  solve: { mates?: Record<string, unknown>; instances?: Record<string, SolveInstanceInfo>; converged?: boolean; notes?: Note[] };
  checks: Record<string, CheckRecord | null>;
  check_history: Record<string, number>;
  assumptions: Assumption[];
  notes: Note[];
  spec_ref: Record<string, unknown> | null;
  /** The engineering spec version it was built from (S5: its copied assumptions are decided on that version). */
  spec_version_id?: UUID | null;
  bom_counts: Record<string, number> | null;
  updates_available: number;
}

export interface SceneNode extends Omit<Component, 'meta'> {
  path: string[];
  world: Matrix4;
  children: SceneNode[];
}

export interface SceneDefinition {
  part_id: UUID;
  number: string | null;
  name: string | null;
  rev_no: number | null;
  label: string | null;
  glb_sha: string | null;
  mesh_sha: string | null;
  bbox: { min: number[]; max: number[]; size: number[] } | null;
  material: string | null;
}

export interface Scene {
  revision_id: UUID;
  tree: SceneNode[];
  definitions: Record<string, SceneDefinition>;
  occurrences: { path: string[]; names: string[]; part_key: string; world: Matrix4 }[];
}

export interface BomLine {
  item: string;
  level: number;
  def_key: string;
  def_kind: DefinitionKind;
  name: string;
  number: string | null;
  material: string | null;
  procurement: ProcurementKind;
  representation: RepresentationLevel;
  quantity: number;
  total_quantity: number;
  instance_paths: string[][];
  rev_no?: number | null;
  expanded_at?: string | null;
}

export interface Bom {
  flattened: BomLine[];
  hierarchical: BomLine[];
  counts: Record<string, number>;
  suppressed: { path: string[]; name: string; def_kind: DefinitionKind; def_key: string }[];
  bom_sha256: string;
  frozen_bom_sha256: string | null;
  verification: Verification;
}

export interface Datum {
  name: string;
  kind: 'frame' | 'axis' | 'plane' | 'point';
  origin: number[];
  direction: number[];
}

export interface Compare {
  instances: { instance_id: string; change: 'added' | 'removed' | 'modified'; name: string; fields?: { field: string }[] }[];
  mates: { mate_id: string; change: string; kind: string }[];
  bom: { def_key: string; name: string; from: number; to: number }[];
  bom_changed: boolean;
}

export interface DownstreamItem {
  kind: string;
  stale: boolean;
  id?: string;
  asset_version?: { label?: string; asset_name?: string; asset_type?: string };
  assembly?: { id: UUID; number: string; name: string };
  revision_id?: UUID;
  rev_no?: number;
  update_available?: boolean;
}

export interface Downstream {
  revision: RevisionBrief;
  latest_revision_id: UUID | null;
  stale: boolean;
  items: DownstreamItem[];
}

export interface Impact {
  affected_instances: string[];
  bom_changes: { def_key: string; name: string; from: number; to: number }[];
  will_be_stale: DownstreamItem[];
  parents_update_available: DownstreamItem[];
  process_quote_inputs: { def_key: string; name: string; status: 'ready' | 'plan_only' | 'missing' }[];
  notes: Note[];
}

export interface WhereUsed {
  assembly: { id: UUID; number: string; name: string };
  revision_id: UUID;
  rev_no: number;
  is_latest: boolean;
  instances: { instance_id: string; name: string; suppressed: boolean }[];
  update_available: boolean;
}

export type EditOp = Record<string, unknown> & { op: string };

export interface ComponentInput {
  name?: string;
  definition: { kind: DefinitionKind; revision_id?: UUID; part_id?: UUID; assembly_id?: UUID };
  placement?: { translate: number[]; rotate: number[] };
  transform?: Matrix4;
  fixed?: boolean;
}

export const listAssemblies = (projectId: UUID, q: { q?: string } = {}, o: Opt = {}) =>
  request<Assembly[]>(`/projects/${enc(projectId)}/assemblies`, { query: q, signal: o.signal });

export const getAssembly = (id: UUID, o: Opt = {}) => request<Assembly>(`/assemblies/${enc(id)}`, { signal: o.signal });

export const createAssembly = (
  projectId: UUID,
  body: { number: string; name: string; components: ComponentInput[]; mates?: Record<string, unknown>[]; execution_policy?: ExecutionPolicy; note?: string },
) => request<{ assembly: Assembly; revision: AssemblyRevision }>(`/projects/${enc(projectId)}/assemblies`, { method: 'POST', body: operationChoices(body) });

export const importAssembly = (projectId: UUID, body: { file_version_id: UUID; number?: string; execution_policy?: ExecutionPolicy }) =>
  request<{ assembly: Assembly; revision: RevisionBrief; import: { counts: Record<string, number>; notes: Note[] } }>(
    `/projects/${enc(projectId)}/assemblies/import`,
    { method: 'POST', body: operationChoices(body) },
  );

export const listAssemblyRevisions = (id: UUID, o: Opt = {}) => request<RevisionBrief[]>(`/assemblies/${enc(id)}/revisions`, { signal: o.signal });

export const editAssembly = (
  id: UUID,
  body: { base_revision_id: UUID; ops: EditOp[]; execution_policy?: ExecutionPolicy; note?: string; run_checks?: boolean },
) => request<AssemblyRevision>(`/assemblies/${enc(id)}/revisions`, { method: 'POST', body: operationChoices(body) });

export const previewImpact = (id: UUID, body: { base_revision_id: UUID; ops: EditOp[] }) =>
  request<Impact>(`/assemblies/${enc(id)}/impact`, { method: 'POST', body });

export const getAssemblyRevision = (rid: UUID, o: Opt = {}) => request<AssemblyRevision>(`/assembly-revisions/${enc(rid)}`, { signal: o.signal });
export const getScene = (rid: UUID, o: Opt = {}) => request<Scene>(`/assembly-revisions/${enc(rid)}/scene`, { signal: o.signal });
export const getBom = (rid: UUID, o: Opt = {}) => request<Bom>(`/assembly-revisions/${enc(rid)}/bom`, { signal: o.signal });
export const getDatums = (rid: UUID, instance: string, o: Opt = {}) =>
  request<{ datums: Datum[]; source: 'w3' | 'fallback' }>(`/assembly-revisions/${enc(rid)}/datums`, { query: { instance }, signal: o.signal });
export const runChecks = (rid: UUID, body: { execution_policy?: ExecutionPolicy; clearance_mm?: number | null }) =>
  request<AssemblyRevision>(`/assembly-revisions/${enc(rid)}/checks`, { method: 'POST', body: operationChoices(body) });
export const compareRevisions = (rid: UUID, against?: UUID, o: Opt = {}) =>
  request<Compare>(`/assembly-revisions/${enc(rid)}/compare`, { query: { against }, signal: o.signal });
export const getDownstream = (rid: UUID, o: Opt = {}) => request<Downstream>(`/assembly-revisions/${enc(rid)}/downstream`, { signal: o.signal });
export const partWhereUsed = (partId: UUID, o: Opt = {}) => request<WhereUsed[]>(`/parts/${enc(partId)}/where-used`, { signal: o.signal });

/** Authenticated download of an export (assembly STEP or the part-set zip). */
export async function downloadExport(rid: UUID, kind: 'step' | 'parts.zip'): Promise<{ blob: Blob; filename: string | null }> {
  const res = await request<Response>(`/assembly-revisions/${enc(rid)}/${kind}`, { as: 'response' });
  const header = res.headers.get('Content-Disposition');
  const m = header ? /filename\*=UTF-8''([^;]+)/i.exec(header) : null;
  return { blob: await res.blob(), filename: m ? decodeURIComponent(m[1]) : null };
}

export type { ApiError };
