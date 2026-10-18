// AI-native main agent (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §3): pure view models of a main-agent
// turn — its step timeline and the object each step points at —, the sub-agents it started, the "sub-agent finished"
// notice, and the right column's overview of the conversation's work. No React here (orchestrator.test.ts).

import {
  orchestratorResult,
  type AiConversationDetail,
  type AiOperation,
  type AiOperationKind,
  type AiOperationStatus,
  type AiQuestion,
  type AiResultItem,
  type AiTaskDraft,
  type AiWork,
  type OrchestratorStep,
  type OrchestratorStepRefs,
  type OrchestratorStepStatus,
} from '../../api/assistant';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { roleForOperation, type EngineeringRole } from '../agent-team/roles';
import { planOutcome, type PlanOutcome } from './planApply';
import type { PanelObject } from './panel/WorkPanelContext';

// ─── sub-agents ────────────────────────────────────────────────────────────

/** Operation responsibility: a coordinating workflow or a specialist (shared by cards, targets and notices). */
export type SubAgent = EngineeringRole;

export function subAgentOf(kind: string | null | undefined): SubAgent | null {
  return roleForOperation(kind);
}

/** Result items of an operation (none for results without items: task drafts, plans, turns). */
export function itemsOf(op: Pick<AiOperation, 'result'>): AiResultItem[] {
  const items = (op.result as { items?: unknown } | null)?.items;
  // Historical raw service results can lack a normalized reference. They cannot form an actionable card.
  return Array.isArray(items) ? (items as AiResultItem[]).filter((item) => !!item?.ref?.key) : [];
}

// ─── steps ─────────────────────────────────────────────────────────────────

/** What a step can point at, and the ref field that names it. */
const REF_OF = { draft: 'draft_id', part: 'part_id', task: 'task_id', operation: 'operation_id', revision: 'revision_id', asset: 'asset_version_id' } as const satisfies Record<
  string,
  keyof OrchestratorStepRefs
>;
export type StepObject = keyof typeof REF_OF;

/**
 * Per tool: the object its step is about, and the sub-agent kind it starts. A step shows its tool's object when it has
 * one; otherwise (another tool, or the step failed before creating it) the first object it names in OBJECT_ORDER.
 */
const TOOLS: Record<string, { object: StepObject; spawns?: AiOperationKind }> = {
  draft_task: { object: 'draft' },
  update_task_draft: { object: 'draft' },
  create_part: { object: 'part' },
  create_task: { object: 'task' },
  create_model: { object: 'operation', spawns: 'model_create' },
  modify_model: { object: 'operation', spawns: 'model_modify' },
  generate_drawing: { object: 'operation', spawns: 'drawing_generate' },
  create_process_plan: { object: 'asset' },
  create_quote: { object: 'asset' },
  create_assembly: { object: 'operation', spawns: 'assembly_create' },
  modify_assembly: { object: 'operation', spawns: 'assembly_modify' },
  create_workflow: { object: 'operation', spawns: 'workflow_create' },
  edit_workflow: { object: 'operation', spawns: 'workflow_edit' },
  read_drawing: { object: 'operation', spawns: 'drawing_intake' },
  reconstruct_drawing: { object: 'operation', spawns: 'drawing_reconstruct' },
  extract_spec: { object: 'operation', spawns: 'spec_extract' },
  create_drawing_document: { object: 'operation', spawns: 'drafting_create' },
  modify_drawing_document: { object: 'operation', spawns: 'drafting_modify' },
  get_operation_status: { object: 'operation' },
  get_part: { object: 'part' },
  get_revision: { object: 'revision' },
};
const OBJECT_ORDER: readonly StepObject[] = ['operation', 'task', 'draft', 'asset', 'part', 'revision'];

export interface StepTarget {
  object: StepObject;
  id: UUID;
  /** What the conversation knows of it: a draft / task title, "number · name" of a part, a revision's label. */
  name: string | null;
  /** operation: the sub-agent and its current status (status null when the operation is not shown). */
  agent: SubAgent | null;
  /** Distinguishes a coordinated workflow target from an individual specialist. */
  operationKind?: AiOperationKind | null;
  status: AiOperationStatus | null;
  outcome?: PlanOutcome | null;
  /** Where a click goes: a work-panel object, else a page, else (a sub-agent without a run) its card in the chat. */
  panel: PanelObject | null;
  href: string | null;
}

export interface StepView {
  index: number;
  tool: string;
  /** The server's one-line description of the call. */
  title: string;
  status: OrchestratorStepStatus;
  error: { code: string; message: string } | null;
  target: StepTarget | null;
  /** The step can be undone now. */
  undo: boolean;
}

export interface StepLookups {
  operations: readonly AiOperation[];
  work?: AiWork | null;
}

const joined = (a: string | null | undefined, b: string | null | undefined) => [a, b].filter((x) => !!x).join(' · ') || null;

/** The model revision item some sub-agent produced (its label, e.g. "R2"). */
function revisionItem(operations: readonly AiOperation[], revisionId: UUID): AiResultItem | null {
  for (const op of operations) for (const it of itemsOf(op)) if (it.ref?.kind === 'part_revision' && it.ref.object_id === revisionId) return it;
  return null;
}

export function stepTarget(step: Pick<OrchestratorStep, 'tool' | 'refs'>, look: StepLookups): StepTarget | null {
  const refs = step.refs ?? {};
  const tool = TOOLS[step.tool];
  const object = tool && refs[REF_OF[tool.object]] ? tool.object : OBJECT_ORDER.find((o) => !!refs[REF_OF[o]]);
  if (!object) return null;
  const id = refs[REF_OF[object]] as UUID;
  const base: StepTarget = { object, id, name: null, agent: null, status: null, panel: null, href: null };
  switch (object) {
    case 'draft':
      return { ...base, name: look.work?.drafts.find((d) => d.id === id)?.title || null, panel: { kind: 'draft', id } };
    case 'part': {
      const p = look.work?.parts.find((x) => x.id === id);
      return { ...base, name: p ? joined(p.number, p.name) : null, href: paths.part(id) };
    }
    case 'task': {
      const k = look.work?.tasks.find((x) => x.id === id);
      return { ...base, name: k ? joined(k.number, k.title) : null, panel: { kind: 'task', id } };
    }
    case 'operation': {
      const op = look.operations.find((o) => o.id === id);
      const operationKind = op?.kind ?? tool?.spawns ?? null;
      return { ...base, operationKind, agent: subAgentOf(operationKind), status: op?.status ?? null, outcome: op ? planOutcome(op) : null, panel: op?.run ? { kind: 'run', id: op.run.id } : null };
    }
    case 'revision':
      return { ...base, name: revisionItem(look.operations, id)?.label || null, panel: { kind: 'revision', id } };
    case 'asset':
      return refs.asset_id ? { ...base, href: paths.assetVersion(refs.asset_id, id) } : null;
  }
}

/** The timeline of a turn, in step order. */
export function stepViews(op: AiOperation, look: StepLookups): StepView[] {
  const steps = orchestratorResult(op)?.steps ?? [];
  return [...steps]
    .sort((a, b) => a.index - b.index)
    .map((s) => ({
      index: s.index,
      tool: s.tool,
      title: s.title ?? '',
      status: s.status,
      error: s.error ?? null,
      target: stepTarget(s, look),
      undo: s.undo === true,
    }));
}

/** The draft a confirmation shows: the conversation's live copy (edited in the panel meanwhile), else the question's. */
export function confirmDraft(q: AiQuestion | null | undefined, work: AiWork | null | undefined): AiTaskDraft | null {
  if (!q || q.kind !== 'confirm') return null;
  const id = q.draft?.id ?? (typeof q.args?.draft_id === 'string' ? q.args.draft_id : null);
  return (id ? work?.drafts.find((d) => d.id === id) : undefined) ?? q.draft ?? null;
}

// ─── notices ───────────────────────────────────────────────────────────────

export interface SubAgentNotice {
  operationId: UUID | null;
  agent: SubAgent | null;
  kind: string | null;
  /** The sub-agent's status when it woke the main agent (succeeded / failed). */
  status: string | null;
  /** Immutable event outcome; never inferred from a later generation of the child. */
  outcome?: PlanOutcome;
  generation?: number;
}

/** A `notice` with payload.event "sub_agent_finished" (null for other notices). */
export function subAgentNotice(payload: Record<string, unknown> | null | undefined, operations: readonly AiOperation[]): SubAgentNotice | null {
  if (payload?.event !== 'sub_agent_finished') return null;
  const id = typeof payload.operation_id === 'string' ? payload.operation_id : null;
  const op = id ? operations.find((o) => o.id === id) : undefined;
  const kind = (typeof payload.kind === 'string' ? payload.kind : null) ?? op?.kind ?? null;
  const status = (typeof payload.status === 'string' ? payload.status : null) ?? op?.status ?? null;
  const outcome = payload.outcome === 'plan_ready' || payload.outcome === 'applied' || payload.outcome === 'undone' ? payload.outcome : undefined;
  return { operationId: id, agent: subAgentOf(kind), kind, status,
    ...(outcome ? { outcome } : {}), ...(typeof payload.generation === 'number' ? { generation: payload.generation } : {}) };

}

// ─── overview (right column) ───────────────────────────────────────────────

export interface OverviewOutput {
  key: string;
  assetType: string | null;
  label: string;
  /** A model revision opens its revision pane; anything else its run, on the node that produced it. */
  panel: PanelObject;
  nodeId: string | null;
}

export interface OverviewAgent {
  op: AiOperation;
  agent: SubAgent | null;
  /** The run pane (null before the sub-agent started a run). */
  panel: PanelObject | null;
  outputs: OverviewOutput[];
}

/** A 2D drawing draft a drawing sub-agent produced (a drawing version, publish_state draft until published). */
export interface OverviewDrawing {
  key: string;
  title: string;
  label: string;
  runId: UUID;
  nodeId: string;
  simulated: boolean;
}

export interface SessionOverviewModel {
  agents: OverviewAgent[];
  drafts: AiTaskDraft[];
  tasks: AiWork['tasks'];
  drawings: OverviewDrawing[];
}

function outputOf(item: AiResultItem): OverviewOutput | null {
  const at = { key: item.ref.key, assetType: item.asset_type, label: item.label || item.ref.version_label || '' };
  if (item.ref.kind === 'part_revision') return { ...at, panel: { kind: 'revision', id: item.ref.object_id }, nodeId: null };
  if (item.run_id) return { ...at, panel: { kind: 'run', id: item.run_id }, nodeId: item.node_id ?? null };
  return null;
}

/** Sub-agents of the conversation (operations a turn started) and the work the main agent created. */
export function sessionOverview(d: Pick<AiConversationDetail, 'operations' | 'work'>): SessionOverviewModel {
  const children = d.operations.filter((o) => !!o.parent_operation_id);
  const agents = children.map((op) => ({
    op,
    agent: subAgentOf(op.kind),
    panel: op.run ? ({ kind: 'run', id: op.run.id } as const) : null,
    outputs: itemsOf(op)
      .map(outputOf)
      .filter((o): o is OverviewOutput => !!o),
  }));
  const drawings = children
    .filter((op) => op.kind === 'drawing_generate')
    .flatMap((op) =>
      itemsOf(op)
        .filter((it) => it.asset_type === 'drawing' && !!it.run_id)
        .map((it) => ({ key: it.ref.key, title: it.ref.title, label: it.label, runId: it.run_id!, nodeId: it.node_id || 'drawing', simulated: op.simulated || !!it.labels?.includes('simulated') })),
    );
  return { agents, drafts: d.work?.drafts ?? [], tasks: d.work?.tasks ?? [], drawings };
}

export function overviewEmpty(m: SessionOverviewModel): boolean {
  return !m.agents.length && !m.drafts.length && !m.tasks.length && !m.drawings.length;
}
