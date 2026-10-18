// A conversation view only: saved identities establish ownership; saved states establish outcomes.
// The projection never rewrites operation history, engineering results or action identities.
import {
  cardPayload, isOrchestratorTurn, orchestratorResult,
  type AiCompletionEvent, type AiConversationDetail, type AiMessage, type AiOperation,
} from '../../api/assistant';
import { isTerminal, retriedByMap } from './model';
import { itemsOf, subAgentNotice } from './orchestrator';
import { planOutcome } from './planApply';

export type JourneyStatus = 'waiting' | 'running' | 'plan_ready' | 'failed' | 'partial' | 'cancelled' | 'generated' | 'complete';
export type JourneyAnswerRole = 'progress' | 'delivery' | 'response';

export interface GenerationJourney {
  id: string;
  request: AiMessage;
  root: AiOperation;
  operations: AiOperation[];
  /** All related messages except the original request, each once in saved sequence order. */
  messages: AiMessage[];
  status: JourneyStatus;
  focusOperationId: string | null;
  hasSavedResult: boolean;
  latestAnswer: AiMessage | null;
}

export type ConversationEntry = { kind: 'journey'; journey: GenerationJourney } | { kind: 'message'; message: AiMessage };

function parentIds(op: AiOperation): string[] {
  const trigger = op.request.trigger ?? orchestratorResult(op)?.trigger;
  return [...new Set([op.request.parent_op_id, op.parent_operation_id, op.request.spawned_by?.operation_id,
    trigger && trigger.type !== 'request' ? trigger.operation_id : undefined,
  ].filter((id): id is string => !!id))];
}

/** A saved design-choice response is bound to an existing card, unlike text sent from the composer. */
function isNewRequest(message: AiMessage): boolean {
  return message.role === 'user' && message.kind === 'request' && message.payload.event !== 'design_choice';
}

/** Old snapshots may omit message.operation_id; require an exact, unambiguous request identity. */
function requestOperation(message: AiMessage, operations: readonly AiOperation[], byId: ReadonlyMap<string, AiOperation>): AiOperation | null {
  if (message.operation_id) return byId.get(message.operation_id) ?? null;
  const candidates = operations.filter((op) => op.message_id === message.id && !parentIds(op).length
    && (!op.request.root_op_id || op.request.root_op_id === op.id));
  const exact = candidates.filter((op) => op.request.client_request_id === message.payload.client_request_id);
  return exact.length === 1 ? exact[0] : candidates.length === 1 ? candidates[0] : null;
}

/** Follow causal edges, stopping at any explicit human request (including a retry of an old child/wake). */
function operationOwners(operations: readonly AiOperation[], roots: ReadonlyMap<string, AiMessage>): Map<string, string> {
  const byId = new Map(operations.map((op) => [op.id, op]));
  const owners = new Map<string, string>();
  type Resolution = { owner: string | null; invalid: boolean };
  const resolved = new Map<string, Resolution>();
  const resolve = (id: string, visiting: ReadonlySet<string>): Resolution => {
    if (roots.has(id)) return { owner: id, invalid: false };
    if (visiting.has(id)) return { owner: null, invalid: true };
    const cached = resolved.get(id);
    if (cached) return cached;
    const op = byId.get(id);
    if (!op) return { owner: null, invalid: false };
    const next = new Set(visiting).add(id);
    const parentResults = parentIds(op).map((parent) => resolve(parent, next));
    const parents = new Set(parentResults.map((result) => result.owner).filter((owner): owner is string => !!owner));
    const invalid = parentResults.some((result) => result.invalid) || parents.size > 1;
    // A direct causal continuation of a human retry belongs to that retry even when an old root was copied.
    const root = op.request.root_op_id;
    const result = invalid ? { owner: null, invalid: true } : parents.size ? { owner: [...parents][0], invalid: false }
      : root && root !== id ? resolve(root, next) : { owner: null, invalid: false };
    resolved.set(id, result);
    return result;
  };
  for (const op of operations) {
    const { owner } = resolve(op.id, new Set());
    if (owner) owners.set(op.id, owner);
  }
  return owners;
}

function messageOperation(message: AiMessage, operations: readonly AiOperation[]): string | null {
  if (message.kind === 'notice') {
    // A free-form payload.operation_id is not ownership evidence for unknown notices.
    const notice = subAgentNotice(message.payload, operations);
    return notice ? message.operation_id ?? notice.operationId : null;
  }
  if (message.kind === 'card' && !cardPayload(message)) return null;
  return message.operation_id;
}

function latestCompletion(op: AiOperation): AiCompletionEvent | null {
  let latest: AiCompletionEvent | null = null;
  for (const event of op.completion_events ?? []) {
    if (event.generation !== (op.generation ?? 1)) continue;
    if (!latest || event.event_seq > latest.event_seq) latest = event;
  }
  return latest;
}

/** A successful turn is a delivery summary only when its saved answer owns current completion receipts. */
export function journeyAnswerRole(journey: GenerationJourney): JourneyAnswerRole {
  const answer = journey.latestAnswer;
  if (!answer) return 'response';
  if (journey.status === 'running' || journey.status === 'waiting') return 'progress';
  if (journey.status !== 'generated' || !journey.hasSavedResult) return 'response';

  const manager = journey.operations.find((op) => op.id === answer.operation_id
    && op.conversation_id === journey.root.conversation_id && isOrchestratorTurn(op) && op.status === 'succeeded');
  if (!manager) return 'response';
  const children = journey.operations.filter((op) => !isOrchestratorTurn(op));
  if (children.some((op) => op.conversation_id !== manager.conversation_id)) return 'response';
  const events = children.map(latestCompletion);
  if (!events.length || events.some((event) => !event || event.wake_state !== 'consumed'
    || !Number.isInteger(event.notice_seq) || event.notice_seq! < 1 || event.notice_seq! >= answer.seq)) return 'response';

  const receipts = (op: AiOperation): ReadonlySet<string> => {
    const context = orchestratorResult(op)?.completion_context;
    return new Set(Array.isArray(context) ? context.filter((id): id is string => typeof id === 'string' && !!id) : []);
  };
  const ownReceipts = receipts(manager);
  // A root/confirmation reply cannot become final merely because a later child eventually finished.
  if (!events.some((event) => event && ownReceipts.has(event.event_id))) return 'response';

  const answeredManagers = journey.operations.filter((op) => op.conversation_id === manager.conversation_id
    && isOrchestratorTurn(op) && op.status === 'succeeded').map((op) => ({
      receipts: receipts(op),
      answers: journey.messages.filter((message) => message.operation_id === op.id && message.role === 'assistant'
        && message.kind === 'answer' && message.seq <= answer.seq),
    }));
  // Separate late children can have separate wakes. Earlier exact receipts count only after their own notice.
  return events.every((event) => event && answeredManagers.some((turn) => turn.receipts.has(event.event_id)
    && turn.answers.some((message) => message.seq > event.notice_seq!))) ? 'delivery' : 'response';
}

function runStatus(op: AiOperation) {
  return op.run?.status ?? (op.result?.kind === 'assembly' ? op.result.chain?.run_status : undefined);
}

function waiting(op: AiOperation): boolean {
  if (op.status === 'waiting_input' || runStatus(op) === 'waiting') return true;
  return op.status === 'succeeded' && op.result?.kind === 'spec_extract' && op.result.spec?.status === 'needs_answers';
}

function running(op: AiOperation): boolean {
  const delivery = latestCompletion(op)?.wake_state;
  return (!isTerminal(op) && op.status !== 'waiting_input') || runStatus(op) === 'running' || runStatus(op) === 'queued'
    || delivery === 'pending' || delivery === 'assigned';
}

/** Recover only from a later successful call's saved scope, never its prose reply. */
function failedTools(op: AiOperation): boolean {
  const failedByCall = new Map<string, string>();
  for (const step of [...(orchestratorResult(op)?.steps ?? [])].sort((a, b) => a.index - b.index)) {
    const args = JSON.stringify(step.args, (_key, value: unknown) => value && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);
    const call = JSON.stringify([step.tool, args]);
    if (step.status === 'failed') failedByCall.set(call, step.tool);
    else if (step.status === 'ok') {
      if (step.tool === 'plan_design') {
        // This tool ignores description arguments and uses the turn's bound human goal/choice.
        // Its saved version proves recovery even when the model rewrote the description.
        const version = step.refs && 'design_version_id' in step.refs ? step.refs.design_version_id : null;
        if (typeof version === 'string' && version && version === op.result?.design_plan?.ref.version_id) {
          for (const [key, tool] of failedByCall) if (tool === 'plan_design') failedByCall.delete(key);
        }
      } else failedByCall.delete(call);
    }
  }
  return failedByCall.size > 0;
}

/** Keep an operation's unresolved error visible even while the journey as a whole is still running. */
export function operationHasFailure(op: AiOperation): boolean {
  const result = op.result;
  return op.status === 'failed' || runStatus(op) === 'failed' || runStatus(op) === 'partial'
    || latestCompletion(op)?.wake_state === 'delivery_failed' || failedTools(op)
    || (!!result && 'check' in result && result.check?.status === 'failed')
    || itemsOf(op).some((item) => item.check_status === 'failed')
    || (result?.kind === 'spec_extract' && result.spec?.status === 'failed');
}

function cancelled(op: AiOperation): boolean {
  return op.status === 'cancelled' || runStatus(op) === 'cancelled';
}

/** Only a successful explicit retry inside this journey can retire a failed/cancelled predecessor. */
function recoveredOperations(operations: readonly AiOperation[]): Set<string> {
  const retries = retriedByMap(operations);
  const byId = new Map(operations.map((op) => [op.id, op]));
  const recovered = new Set<string>();
  for (const op of operations) {
    const seen = new Set([op.id]);
    let nextId = retries.get(op.id);
    while (nextId && !seen.has(nextId)) {
      seen.add(nextId);
      const next = byId.get(nextId);
      if (!next || next.kind !== op.kind) break;
      if (next.status === 'succeeded' && !operationHasFailure(next) && !cancelled(next) && !waiting(next) && !running(next)) {
        recovered.add(op.id);
        break;
      }
      nextId = retries.get(nextId);
    }
  }
  return recovered;
}

function savedResult(op: AiOperation): boolean {
  const outcome = planOutcome(op);
  if (outcome === 'plan_ready' || outcome === 'undone' || op.result?.cad_result?.undo.restored) return false;
  const cad = op.result?.cad_result;
  return outcome === 'applied' || !!(cad?.object_id && cad.version_id) || itemsOf(op).length > 0
    || (op.result?.kind === 'spec_extract' && !!op.result.spec?.spec_version_id);
}

function outcome(operations: readonly AiOperation[]): Pick<GenerationJourney, 'status' | 'focusOperationId' | 'hasSavedResult'> {
  const newestFirst = [...operations].reverse();
  const saved = newestFirst.find(savedResult);
  const hasSavedResult = !!saved;
  const at = (status: JourneyStatus, op: AiOperation | undefined) => ({ status, hasSavedResult, focusOperationId: op?.id ?? null });
  const wait = newestFirst.find(waiting);
  if (wait) return at('waiting', wait);
  const active = newestFirst.find(running);
  if (active) return at('running', active);
  const recovered = recoveredOperations(operations);
  const failures = newestFirst.filter((op) => !recovered.has(op.id) && operationHasFailure(op));
  if (failures.length) return at(hasSavedResult || failures.some((op) => runStatus(op) === 'partial') ? 'partial' : 'failed', failures[0]);
  const stopped = newestFirst.find((op) => !recovered.has(op.id) && cancelled(op));
  if (stopped) return at(hasSavedResult ? 'partial' : 'cancelled', stopped);
  const plan = newestFirst.find((op) => planOutcome(op) === 'plan_ready');
  if (plan) return at('plan_ready', plan);
  return saved ? at('generated', saved) : at('complete', newestFirst[0]);
}

export function conversationEntries(detail: Pick<AiConversationDetail, 'messages' | 'operations'>): ConversationEntry[] {
  const messages = [...new Map(detail.messages.map((message) => [message.id, message])).values()]
    .sort((a, b) => a.seq - b.seq || a.id.localeCompare(b.id));
  const operations = [...new Map(detail.operations.map((op) => [op.id, op])).values()];
  const byId = new Map(operations.map((op) => [op.id, op]));
  const roots = new Map<string, AiMessage>();
  for (const message of messages) {
    if (!isNewRequest(message)) continue;
    const op = requestOperation(message, operations, byId);
    if (op && !roots.has(op.id)) roots.set(op.id, message);
  }
  const owners = operationOwners(operations, roots);
  const operationSeq = new Map<string, number>();
  for (const message of messages) {
    if (message.kind === 'operation' && message.operation_id && !operationSeq.has(message.operation_id)) {
      operationSeq.set(message.operation_id, message.seq);
    }
  }
  const groups = new Map<string, GenerationJourney>();
  for (const [id, request] of roots) {
    const owned = operations.filter((op) => owners.get(op.id) === id).sort((a, b) =>
      (a.id === id ? -1 : b.id === id ? 1 : (operationSeq.get(a.id) ?? Infinity) - (operationSeq.get(b.id) ?? Infinity)
        || a.id.localeCompare(b.id)));
    groups.set(id, { id, root: byId.get(id)!, request, operations: owned, messages: [], latestAnswer: null, ...outcome(owned) });
  }
  const entries: ConversationEntry[] = [];
  for (const message of messages) {
    const anchor = message.operation_id ? groups.get(message.operation_id) : undefined;
    // Legacy anchors without operation_id still occupy their own original request position.
    const own = anchor?.request.id === message.id ? anchor : [...groups.values()].find((j) => j.request.id === message.id);
    if (own) {
      entries.push({ kind: 'journey', journey: own });
      continue;
    }
    const operationId = messageOperation(message, operations);
    const owner = operationId ? owners.get(operationId) : undefined;
    const group = owner ? groups.get(owner) : undefined;
    if (group && !isNewRequest(message)) {
      group.messages.push(message);
      if (message.role === 'assistant' && message.kind === 'answer') group.latestAnswer = message;
    } else entries.push({ kind: 'message', message });
  }
  return entries;
}

/** Recent actionable/live work stays reachable even if a newer independent request already finished. */
export function currentJourney(entries: readonly ConversationEntry[]): GenerationJourney | null {
  let latest: GenerationJourney | null = null;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind !== 'journey') continue;
    latest ??= entry.journey;
    if (entry.journey.status === 'waiting' || entry.journey.status === 'running' || entry.journey.status === 'plan_ready') return entry.journey;
  }
  return latest;
}
