import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiConversationDetail, AiMessage, AiOperation, OrchestratorStep } from '../../api/assistant';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { designChoicePlan } from './designChoiceFixtures.test-support';
import { draftKey, EMPTY_DRAFT, type ComposerDraft } from './drafts';
import { followUpDraftKey, prepareFollowUp } from './followUps';
import { conversationEntries, type GenerationJourney, type JourneyStatus } from './journey';
import { prominentJourneyOperation } from './JourneyCard';
import { JourneyNavigator } from './JourneyNavigator';
import { MessageList } from './MessageList';

// The existing operation suites own form semantics. Here their identities make accidental duplicate mounts visible.
const savedDrafts = new Map<string, ComposerDraft>();
const session = {
  store: { send: vi.fn(), refresh: vi.fn() }, capabilities: null, bumpList: vi.fn(),
  drafts: { key: (id: string) => draftKey('user', id), read: (key: string) => savedDrafts.get(key) ?? EMPTY_DRAFT,
    write: (key: string, draft: ComposerDraft) => savedDrafts.set(key, draft), persistent: true },
};
vi.mock('./AssistantProvider', () => ({ useAssistantSession: () => session }));
vi.mock('./OperationCard', () => ({
  OperationCard: ({ op, presentation }: { op: AiOperation; presentation?: string }) => h('article', {
    'data-op-id': op.id, 'data-presentation': presentation,
  }, op.question ? h('input', { 'data-form-id': op.id }) : `Saved operation ${op.id}`),
  revealOperation: vi.fn(),
}));
vi.mock('./TurnCard', () => ({
  TurnCard: ({ op, presentation }: { op: AiOperation; presentation?: string }) => h('article', {
    'data-op-id': op.id, 'data-presentation': presentation,
  }, op.question ? h('input', { 'data-form-id': op.id }) : `Saved turn ${op.id}`),
  toolLabel: (_t: unknown, tool: string) => tool,
}));

const TIME = '2026-10-18T00:00:00Z';
function operation(id: string, patch: Partial<AiOperation> = {}): AiOperation {
  return {
    id, conversation_id: 'conversation', message_id: 'request', kind: 'ask', status: 'succeeded',
    request: { text: 'One short request', operation: 'ask', requested_operation: 'auto', target: null, references: [],
      constraints: [], task_id: null, model_config_id: null, client_request_id: 'request-id', runtime: 'orchestrator',
      root_op_id: 'root', trigger: id === 'root' ? { type: 'request' } : { type: 'sub_agent', operation_id: 'child', status: 'succeeded' } },
    task: null, part: null, run: null, question: null, result: null, error: null, simulated: true,
    retry_of: null, context_build: null, created_at: TIME, updated_at: TIME, finished_at: TIME, ...patch,
  };
}
function message(id: string, seq: number, patch: Partial<AiMessage> = {}): AiMessage {
  return { id, seq, role: 'assistant', kind: 'operation', text: id, target: null, references: [], operation_id: null,
    payload: {}, created_at: TIME, ref_status: {}, ...patch };
}
function fixture(childPatch: Partial<AiOperation> = {}): AiConversationDetail {
  return {
    conversation: { id: 'conversation', title: 'Conversation', project: { id: 'project', code: 'TEST', name: 'Simulated QA' }, task: null,
      archived: false, archived_at: null, constraints: [], summary_status: null, forked_from: null, version: 1,
      last_activity_at: TIME, created_at: TIME },
    operations: [operation('root'), operation('child', { kind: 'model_create', parent_operation_id: 'root',
      request: { ...operation('root').request, operation: 'model_create', runtime: 'tool_agent', trigger: undefined },
      result: { kind: 'model', items: [] }, ...childPatch }), operation('wake')],
    messages: [message('request', 1, { role: 'user', kind: 'request', operation_id: 'root', text: 'One short request' }),
      message('root-message', 2, { operation_id: 'root' }), message('child-message', 3, { operation_id: 'child' }),
      message('earlier-answer', 4, { kind: 'answer', operation_id: 'root', text: 'Earlier explanation' }),
      message('notice', 5, { kind: 'notice', payload: { event: 'sub_agent_finished', operation_id: 'child' } }),
      message('wake-message', 6, { operation_id: 'wake' }),
      message('latest-answer', 7, { kind: 'answer', operation_id: 'wake', text: 'Latest explanation' })],
    composer: { target_key: null, references: [], ref_status: {} },
  };
}
function render(detail: AiConversationDetail): string {
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ToastProvider, null,
    h(MessageList, { detail, onPrefill: () => {} })))));
}
function journey(detail: AiConversationDetail): GenerationJourney {
  const entry = conversationEntries(detail)[0];
  if (entry.kind !== 'journey') throw new Error('Expected a projected journey');
  return entry.journey;
}

beforeEach(() => { savedDrafts.clear(); vi.clearAllMocks(); });

describe('task journey composition', () => {
  it('renders every operation and saved message once, with latest explanation before folded activity', () => {
    const detail = fixture();
    const html = render(detail);
    expect(html.match(/data-journey-id=/g)).toHaveLength(1);
    for (const op of detail.operations) expect(html.match(new RegExp(`data-op-id="${op.id}"`, 'g'))).toHaveLength(1);
    for (const m of detail.messages) expect(html.match(new RegExp(`data-message-id="${m.id}"`, 'g'))).toHaveLength(1);
    const [visible, activity] = html.split('<details class="as-journey-activity">');
    expect(visible).toContain('Saved operation child');
    expect(visible).toContain('Latest explanation');
    expect(visible).not.toContain('Earlier explanation');
    expect(activity).toContain('Earlier explanation');
    expect(activity).toContain('Saved turn root');
    expect(activity).toContain('Saved turn wake');
    expect(html.match(/data-presentation="journey"/g)).toHaveLength(3);
  });

  it('formats current and historical answers while keeping user text literal and follow-ups unchanged', () => {
    const detail = fixture();
    detail.messages[0].text = '**用户要求**：保留 * 原字符';
    detail.messages[3].text = '**历史说明**：模型仍在生成。';
    detail.messages[6].text = '### 交付资料\n\n1. **零件 R1**：50×50×50 mm。\n\n未经工程审核。';
    detail.messages[6].payload.follow_ups = ['查看工程图', '修改尺寸'];
    const html = render(detail);
    const [current, history] = html.split('<details class="as-journey-activity">');
    expect(current).toContain('<p class="as-msg-text">**用户要求**：保留 * 原字符</p>');
    expect(current).toContain('<h3>交付资料</h3>');
    expect(current).toContain('<strong>零件 R1</strong>：50×50×50 mm。');
    expect(current).toContain('未经工程审核。');
    expect(history).toContain('<strong>历史说明</strong>：模型仍在生成。');
    expect(html.match(/class="as-msg-text as-markdown"/g)).toHaveLength(2);
    expect(current.match(/class="as-followup-text"/g)).toHaveLength(2);
    expect(current).toContain('<span class="as-followup-text">查看工程图</span>');
    expect(current).toContain('<span class="as-followup-text">修改尺寸</span>');
    expect(session.store.send).not.toHaveBeenCalled();
  });

  it('keeps active child work visible after the main reply ended', () => {
    const html = render(fixture({ status: 'running' }));
    expect(html).toContain('data-journey-state="running"');
    expect(html).toContain('View progress');
    expect(html.split('<details class="as-journey-activity">')[0]).toContain('data-op-id="child"');
    expect(html.split('<details class="as-journey-activity">')[0]).toContain('data-engineering-role="modeling"');
    expect(html).toContain('data-answer-role="progress"');
    expect(html).toContain('Progress update');
  });

  it.each(['running', 'failed', 'cancelled'] as const)('keeps task coordination distinct from %s assembly workflow activity', (status) => {
    const html = render(fixture({ kind: 'assembly_create', status, result: { kind: 'assembly', items: [] } }));
    const heading = html.match(/<header class="as-journey-head">[\s\S]*?<\/header>/)?.[0];
    expect(heading).toContain('data-engineering-role="manager"');
    expect(heading).toContain('Task coordinator');
    expect(heading).not.toContain('data-engineering-role="assembly"');
    expect(html).toContain(`data-journey-state="${status}"`);
    if (status === 'running') {
      const activity = html.match(/<div class="as-journey-current">[\s\S]*?<\/div>/)?.[0];
      expect(activity).toContain('Current activity');
      expect(activity).toContain('Assembly generation workflow');
      expect(activity).not.toContain('data-engineering-role');
    }
  });

  it('shows one current delivery summary and omits only its redundant generated-state paragraph', () => {
    const detail = fixture({ result: { kind: 'model', items: [], cad_result: {
      event_id: 'saved-model', object_type: 'part_model', object_id: 'part', version_id: 'revision',
      base_version_id: null, version_label: 'R1', asset_version_id: null, requirements: [], changes: [], checks: {},
      artifacts: [], undo: { eligible: false, expected_head: 'revision' },
    } }, completion_events: [{ event_id: 'completion', generation: 1, event_seq: 1,
      phase: 'terminal', outcome: 'succeeded', wake_state: 'consumed', notice_seq: 5 }] });
    detail.operations[2].result = { kind: 'orchestrator', steps: [], trigger: detail.operations[2].request.trigger!,
      simulated: true, completion_context: ['completion'] } as unknown as AiOperation['result'];
    const html = render(detail);
    expect(html.match(/data-answer-role="delivery"/g)).toHaveLength(1);
    expect(html).toContain('Delivery summary');
    expect(html).not.toContain('class="as-journey-state"');
    expect(html.match(/Latest explanation/g)).toHaveLength(1);
    expect(html.match(/Earlier explanation/g)).toHaveLength(1);
    expect(html).toContain('Earlier update');
    // Existing records without exact consumption evidence still retain their useful saved-result hint.
    detail.operations[2].result = null;
    expect(render(detail)).toContain('class="as-journey-state"');
    expect(render(detail)).toContain('data-answer-role="response"');
  });

  it('folds old suggestions but keeps an uncertain historical retry accessible with its original draft', () => {
    const detail = fixture();
    detail.operations[0].status = 'running';
    const old = detail.messages[3];
    old.payload.follow_ups = ['Old suggestion', 'Retry original action', 'Already submitted'];
    detail.messages[6].payload.follow_ups = ['Current suggestion'];
    const original = prepareFollowUp({ target: null, references: [] }, 'Retry original action', EMPTY_DRAFT, () => 'lost-request');
    const uncertainKey = followUpDraftKey(session.drafts.key(detail.conversation.id), old.id, 'Retry original action');
    session.drafts.write(uncertainKey, original);
    session.drafts.write(followUpDraftKey(session.drafts.key(detail.conversation.id), old.id, 'Already submitted'),
      prepareFollowUp({ target: null, references: [] }, 'Already submitted', EMPTY_DRAFT, () => 'recorded-request'));
    detail.messages.push(message('acknowledgement', 8, { role: 'user', kind: 'request', text: 'Recorded action',
      payload: { client_request_id: 'recorded-request' } }));
    const html = render(detail);
    const [current, history] = html.split('<details class="as-journey-activity">');
    expect(current).toContain('Current suggestion');
    expect(current).not.toContain('Old suggestion');
    const [retry, suggestions] = history.split('<details class="as-followups-history">');
    expect(retry).toContain('data-followup-state="retry"');
    expect(retry).not.toContain('data-followup-state="retry" disabled');
    expect(retry).toContain('Retry original action');
    expect(suggestions).toContain('Old suggestion');
    expect(suggestions).toContain('data-followup-state="submitted" disabled=""');
    expect(html.match(/<span class="as-followup-text">Retry original action<\/span>/g)).toHaveLength(1);
    expect(savedDrafts.get(uncertainKey)).toEqual(original);
    expect(savedDrafts.get(uncertainKey)?.requestId).toBe('lost-request');
    expect(session.store.send).not.toHaveBeenCalled();
    expect(render(detail)).toContain('data-followup-state="retry"');
    detail.messages.push(message('late-ack', 9, { role: 'user', kind: 'request', text: 'Recorded retry',
      payload: { client_request_id: 'lost-request' } }));
    const acknowledged = render(detail);
    expect(acknowledged).not.toContain('data-followup-state="retry"');
    expect(acknowledged.match(/data-followup-state="submitted" disabled=""/g)).toHaveLength(2);
    expect(savedDrafts.get(uncertainKey)).toEqual(original);
  });

  it.each(['waiting_input', 'failed', 'cancelled'] as const)('keeps the %s hint and avoids a delivery claim', (status) => {
    const html = render(fixture({ status }));
    expect(html).toContain('class="as-journey-state"');
    expect(html).not.toContain('data-answer-role="delivery"');
  });

  it('keeps the manager reply and immutable engineer failure notice truthful even after later child success', () => {
    const detail = fixture();
    const notice = detail.messages.find((m) => m.kind === 'notice')!;
    notice.payload = { event: 'sub_agent_finished', operation_id: 'child', kind: 'model_create', status: 'failed' };
    const html = render(detail);
    expect(html).toContain('data-engineering-role="manager"');
    expect(html).toContain('3D modeling engineer · AI: operation failed');
    expect(html).not.toContain('the project manager continues');
    expect(html).toContain('Latest explanation');
    expect(html).toContain('Earlier explanation');
  });

  it.each([
    ['succeeded', 'Assembly generation workflow finished. Generated results remain in the work record.'],
    ['failed', 'Assembly generation workflow failed. See the error in the work record.'],
    ['cancelled', 'Assembly generation workflow ended (Cancelled).'],
  ])('names the workflow in its immutable %s notice instead of another manager employee', (status, expected) => {
    const detail = fixture({ kind: 'assembly_create', status: 'running', result: { kind: 'assembly', items: [] } });
    detail.messages.find((m) => m.kind === 'notice')!.payload = {
      event: 'sub_agent_finished', operation_id: 'child', kind: 'assembly_create', status,
    };
    const html = render(detail);
    expect(html).toContain(expected);
    expect(html).not.toContain('Project manager · AI: operation');
    expect(html).not.toContain('Assembly engineer · AI: operation');
    expect(html).toContain('data-journey-state="running"');
  });

  it('keeps a bound decision form outside activity with its single operation identity', () => {
    const detail = fixture({ status: 'waiting_input', question: { kind: 'clarification', questions: ['Choose the material'], can_answer: true } });
    const html = render(detail);
    expect(html).toContain('Go to choices');
    expect(html.match(/data-form-id="child"/g)).toHaveLength(1);
    expect(html.split('<details class="as-journey-activity">')[0]).toContain('data-form-id="child"');
  });

  it('retains an extra operation message without mounting another form and leaves unknown history readable', () => {
    const detail = fixture();
    detail.messages.push(message('extra', 8, { operation_id: 'child', text: 'Extra saved operation notice' }),
      message('legacy', 9, { kind: 'answer', text: 'Unrelated legacy answer' }));
    const html = render(detail);
    expect(html.match(/data-op-id="child"/g)).toHaveLength(1);
    expect(html.match(/Extra saved operation notice/g)).toHaveLength(1);
    expect(html).toContain('data-message-id="legacy"');
    expect(html).toContain('Unrelated legacy answer');
  });

  it('keeps a failed tool in the main work even when its containing turn succeeded', () => {
    const detail = fixture();
    detail.operations[0].result = { kind: 'orchestrator', steps: [{ index: 0, tool: 'plan_design', status: 'failed' } as OrchestratorStep] } as unknown as AiOperation['result'];
    const html = render(detail);
    expect(html).toContain('data-journey-state="failed"');
    expect(html.split('<details class="as-journey-activity">')[0]).toContain('data-op-id="root"');
  });

  it('keeps an unresolved tool error visible alongside running child work, while recovered calls stay in history', () => {
    const detail = fixture({ status: 'running' });
    const steps = [{ index: 0, tool: 'plan_design', status: 'failed' } as OrchestratorStep];
    detail.operations[0].result = { kind: 'orchestrator', steps } as unknown as AiOperation['result'];
    const active = render(detail);
    expect(active).toContain('data-journey-state="running"');
    expect(active.split('<details class="as-journey-activity">')[0]).toContain('data-op-id="root"');
    steps.push({ index: 1, tool: 'plan_design', status: 'ok' } as OrchestratorStep);
    expect(render(detail).split('<details class="as-journey-activity">')[0]).toContain('data-op-id="root"');
    const refs: OrchestratorStep['refs'] & { design_version_id: string } = { design_version_id: designChoicePlan.ref.version_id };
    steps[1].refs = refs;
    detail.operations[0].result = { ...detail.operations[0].result!, design_plan: { ...designChoicePlan, status: 'adopted' } };
    const recovered = render(detail);
    expect(recovered.split('<details class="as-journey-activity">')[0]).not.toContain('data-op-id="root"');
    expect(recovered.split('<details class="as-journey-activity">')[1]).toContain('data-op-id="root"');
  });

  it('does not fold terminal orchestration with current questions, saved CAD or Undo actions', () => {
    const detail = fixture();
    const j = journey(detail);
    const turn = detail.operations[0];
    expect(prominentJourneyOperation(turn, j)).toBe(false);
    expect(prominentJourneyOperation({ ...turn, question: { kind: 'confirm', tool: 'create_part', args: {} } }, j)).toBe(true);
    const undo = { kind: 'orchestrator', steps: [{ index: 0, tool: 'create_part', status: 'ok', undo: true }] } as unknown as AiOperation['result'];
    expect(prominentJourneyOperation({ ...turn, result: undo }, j)).toBe(true);
  });
});

describe('current task navigation', () => {
  it.each<[JourneyStatus, string]>([
    ['waiting', 'Go to choices'], ['running', 'View progress'], ['plan_ready', 'Review plan'],
    ['failed', 'View issue'], ['partial', 'View unfinished work'], ['generated', 'View results'],
    ['complete', 'View reply'], ['cancelled', 'View record'],
  ])('gives %s a specific navigation action', (status, label) => {
    const html = renderToStaticMarkup(h(I18nProvider, null, h(JourneyNavigator, { journey: { ...journey(fixture()), status }, onNavigate: () => {} })));
    expect(html).toContain(label);
    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).not.toContain('<input');
  });
});
