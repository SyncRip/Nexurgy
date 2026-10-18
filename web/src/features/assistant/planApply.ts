import { assemblyModifyResult, draftingModifyResult, workflowCreateResult, workflowEditResult,
  type AiApplyBody, type AiDraftingModifyResult, type AiOperation } from '../../api/assistant';

type PlanBinding =
  | { kind: 'workflow_edit'; base_version: number; plan_hash?: string }
  | { kind: 'workflow_create'; plan_hash?: string }
  | { kind: 'assembly_modify'; base_revision_id: string; plan_hash?: string; removals: readonly unknown[] }
  | Pick<AiDraftingModifyResult, 'kind' | 'document_id' | 'base_version_id' | 'plan_hash' | 'plan' | 'removals'>;

const SHA256 = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A changed plan/base invalidates the person's local removal checkbox. */
export function planConfirmationKey(plan: PlanBinding): string {
  return JSON.stringify([plan.kind, plan.plan_hash ?? null,
    plan.kind === 'drafting_modify' ? plan.base_version_id : plan.kind === 'assembly_modify' ? plan.base_revision_id : plan.kind === 'workflow_edit' ? plan.base_version : null,
    'removals' in plan ? plan.removals : [], plan.kind === 'drafting_modify' ? plan.plan.removed_entity_ids : []]);
}

/** Build only from the displayed saved plan, never current/latest object state. Null requires a reload or consent. */
export function planApplyBody(plan: PlanBinding, confirmRemovals = false): AiApplyBody | null {
  if (plan.plan_hash !== undefined && !SHA256.test(plan.plan_hash)) return null;
  const body: AiApplyBody = plan.plan_hash ? { plan_hash: plan.plan_hash } : {};
  switch (plan.kind) {
    case 'workflow_create': return body;
    case 'workflow_edit':
      return Number.isInteger(plan.base_version) && plan.base_version > 0 ? { ...body, expected_version: plan.base_version } : null;
    case 'drafting_modify':
      if (plan.plan.questions.length > 0 || !plan.plan_hash || plan.base_version_id !== plan.plan.base_version_id || plan.document_id !== plan.plan.document_id) return null;
      if (!UUID.test(plan.base_version_id) || ((plan.removals.length > 0 || plan.plan.removed_entity_ids.length > 0) && !confirmRemovals)) return null;
      return { ...body, expected_head: plan.base_version_id, confirm_removals: confirmRemovals };
    case 'assembly_modify':
      if (!UUID.test(plan.base_revision_id) || (plan.removals.length > 0 && !confirmRemovals)) return null;
      return { ...body, expected_head: plan.base_revision_id, confirm_removals: confirmRemovals };
  }
}

export type PlanOutcome = 'plan_ready' | 'applied' | 'undone';
export function planOutcome(op: Pick<AiOperation, 'kind' | 'status' | 'result'>): PlanOutcome | null {
  if (op.status !== 'succeeded') return null;
  const plan = workflowEditResult(op) ?? workflowCreateResult(op) ?? assemblyModifyResult(op) ?? draftingModifyResult(op);
  if (!plan) return null;
  if (op.result?.cad_result?.undo.restored) return 'undone';
  return plan.application?.state ?? 'plan_ready';
}
