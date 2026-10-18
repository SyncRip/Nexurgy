// One project's raw catalogue sources (design §1): the asset catalogue, files, drawing documents, specs, parts
// and what the workflow resources record about asset versions — every list the adapter (catalog.ts) joins by identity.

import { listProjectAssets } from '../../api/assets';
import { listDrawingDocuments } from '../../api/drafting';
import { listFiles, listParts } from '../../api/endpoints';
import { effectiveAcceptance, listProjectResources, RESOURCE_PAGE_MAX, type WorkflowResourceItem } from '../../api/resources';
import { listSpecs } from '../../api/spec';
import type { UUID } from '../../api/types';
import { SPEC_PAGE_LIMIT, type ProjectSources, type TaskOutputs } from './catalog';
import type { TaskFormalVersion } from './types';

/**
 * Registration reasons (metadata.reason up to the first ':') of the candidate rows that mean the run / agent wrote the
 * version's content (server services/):
 * - `tool:<name>` — an agent tool produced it (agent_tools.py _register_candidate, ~163);
 * - `workspace_file` — historical content written by the retired workspace runtime.
 * Not a write: `deliver` / `turbo_unverified` (agent_runtime.py ~1552 / ~1413 — an agent hands in a version that may
 * already exist), `add_deliverable` (~2758 — a person delivers their own upload), `forked` (~3085 — candidates copied
 * into a forked run). The other kinds record a use, not a write (input, formal, superseded, evidence, and failed —
 * whose reason is the reviewer's free-text comment).
 */
const RUN_WRITE_REASONS: ReadonlySet<string> = new Set(['tool', 'workspace_file']);

const writtenByRun = (r: WorkflowResourceItem) => r.kind === 'candidate' && typeof r.metadata?.reason === 'string' && RUN_WRITE_REASONS.has(r.metadata.reason.split(':')[0]);

/** A formal row a reopen or a newer acceptance has not superseded (the server then reports it invalidated). */
const currentFormal = (r: WorkflowResourceItem) => r.kind === 'formal' && effectiveAcceptance(r.task_acceptance, r.kind) !== 'invalidated';

/** Pages of workflow resources read per project; beyond this the task records are reported as truncated. */
const OUTPUT_PAGES = 10;

/** What workflow resource rows record about asset versions: the versions a run wrote, each asset's current task-formal versions. */
export function taskOutputsOf(rows: readonly WorkflowResourceItem[], truncated: boolean): TaskOutputs {
  const written = new Map<UUID, Set<UUID>>();
  const formal = new Map<UUID, TaskFormalVersion[]>();
  for (const r of rows) {
    if (writtenByRun(r)) written.set(r.asset.asset_id, (written.get(r.asset.asset_id) ?? new Set<UUID>()).add(r.asset_version_id));
    else if (currentFormal(r)) {
      const list = formal.get(r.asset.asset_id) ?? [];
      if (!list.some((f) => f.versionId === r.asset_version_id && f.taskNumber === r.task_number)) list.push({ versionId: r.asset_version_id, label: r.asset.label, taskNumber: r.task_number });
      formal.set(r.asset.asset_id, list);
    }
  }
  return { written, formal, truncated };
}

async function loadTaskOutputs(projectId: UUID, signal: AbortSignal): Promise<TaskOutputs> {
  const rows: WorkflowResourceItem[] = [];
  for (let page = 0; page < OUTPUT_PAGES; page++) {
    const res = await listProjectResources(projectId, { limit: RESOURCE_PAGE_MAX, offset: rows.length }, { signal });
    rows.push(...res.items);
    if (!res.items.length || rows.length >= res.total) return taskOutputsOf(rows, false);
  }
  return taskOutputsOf(rows, true);
}

/** Every source of one project, archived rows included (the list filter hides them). */
export async function loadProjectSources(projectId: UUID, signal: AbortSignal): Promise<ProjectSources> {
  const o = { signal };
  const [assets, files, drawingDocuments, specs, parts, taskOutputs] = await Promise.all([
    listProjectAssets(projectId, { include_archived: true }, o),
    listFiles(projectId, { include_archived: true }, o),
    listDrawingDocuments(projectId, true, o),
    listSpecs(projectId, { limit: SPEC_PAGE_LIMIT }, o),
    listParts(projectId, { include_archived: true }, o),
    loadTaskOutputs(projectId, signal),
  ]);
  return { assets, files, drawingDocuments, specs, parts, taskOutputs };
}
