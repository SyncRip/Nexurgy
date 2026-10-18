import { createElement as h, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiConversationDetail, AiDraftingModifyResult, AiOperation, CadResult, OrchestratorStep } from '../../api/assistant';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ThemeProvider } from '../../prefs/ThemeProvider';
import { run, stage, unit } from '../progress/test-fixtures';
import { DesignPlanSummary } from './DesignPlanSummary';
import { OperationCard, revealOperation } from './OperationCard';
import { TurnCard } from './TurnCard';
import { SessionOverview } from './SessionOverview';
import { designChoiceOperation, designChoicePlan } from './designChoiceFixtures.test-support';
import { createDesignChoiceDrafts, designChoiceScope, emptyDesignDraft } from './designChoices';

const session = {
  userId: 'user', designDrafts: createDesignChoiceDrafts(null, 'user'),
  store: { detail: null, undoStep: vi.fn(), confirm: vi.fn(), answerDesign: vi.fn() },
  keepDockOnLeave: vi.fn(), requestIds: { forget: vi.fn() },
};
vi.mock('./AssistantProvider', () => ({
  useAssistantSession: () => session,
  useAssistant: () => ({ pending: { references: [] } }),
}));
vi.mock('../progress/OperationProgress', () => ({
  OperationProgress: () => h('section', { className: 'test-run-progress' }, 'Execution summary'),
}));
vi.mock('../progress/useRunProgress', () => ({
  useRunProgress: (runId?: string) => ({
    progress: runId ? run('running', [stage('assembly', 'running', [unit('definition:wheel', 'built', {
      title: 'Wheel', quantity: 4, revision: { id: 'wheel-r1', part_id: 'wheel', label: 'R1', status: 'ready', size: null, operations: [] },
    })])]) : null,
    error: null, reload: vi.fn(), expanded: false, setExpanded: vi.fn(),
  }),
}));

beforeEach(() => { session.designDrafts = createDesignChoiceDrafts(null, 'user'); vi.clearAllMocks(); });
afterEach(() => vi.unstubAllGlobals());

const contextBuild: NonNullable<AiOperation['context_build']> = {
  id: 'build', ok: true, model_label: 'Test model', usage: { calls: 1, tokens_in: 100, tokens_out: 20, unknown: false },
  budget: { input_limit: 5000, output_reserve: 1000, safety: 500, available: 3500, required_est: 100, used_est: 100, limit_source: 'test', estimated: true },
};
const saved: CadResult = {
  event_id: 'event', object_type: 'assembly', object_id: 'fixture', version_id: 'a2', version_label: 'A2',
  base_version_id: 'a1', asset_version_id: null, requirements: [], changes: [], checks: {}, artifacts: [],
  undo: { eligible: true, expected_head: 'a2' },
};
function operation(patch: Partial<AiOperation> = {}): AiOperation {
  return { ...designChoiceOperation, kind: 'model_create', status: 'succeeded', question: null, result: null, context_build: contextBuild, ...patch };
}
function turn(steps: Partial<OrchestratorStep>[], patch: Partial<AiOperation> = {}): AiOperation {
  return operation({ kind: 'ask', result: {
    kind: 'orchestrator', trigger: { type: 'request' }, simulated: true,
    steps: steps.map((step, index) => ({ index, tool: 'project_overview', title: 'Read project', args: {}, status: 'ok', message: '', refs: {}, started_at: '2026-10-18T00:00:00Z', finished_at: null, ...step })),
  } as unknown as AiOperation['result'], ...patch });
}
function render(node: ReactNode): string {
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ThemeProvider, null, h(ToastProvider, null, node)))));
}
function renderTurn(op: AiOperation, presentation: 'standalone' | 'journey' = 'journey', extra: AiOperation[] = []): string {
  return render(h(TurnCard, { op, presentation, archived: false, detail: { operations: [op, ...extra] } as AiConversationDetail }));
}
/** Static markup has no layout engine; exclude disclosure bodies to check which controls start visible. */
function outsideDetails(html: string): string {
  let depth = 0;
  return html.split(/(<\/?details\b[^>]*>)/g).filter((token) => {
    if (token.startsWith('<details')) { depth += 1; return false; }
    if (token.startsWith('</details')) { depth -= 1; return false; }
    return depth === 0;
  }).join('');
}

describe('journey turn presentation', () => {
  it.each(['running', 'failed'] as const)('links the %s assembly workflow without delegating to a second manager', (status) => {
    const child = operation({ id: 'child', kind: 'assembly_create', status });
    const html = renderTurn(turn([{ tool: 'create_assembly', refs: { operation_id: child.id } }]), 'journey', [child]);
    const target = html.match(/<button\b[^>]*class="as-step-obj"[\s\S]*?<\/button>/)?.[0];
    expect(target).toContain(`Assembly generation workflow · ${status === 'running' ? 'Running' : 'Failed'}`);
    expect(target).not.toContain('Project manager');
    expect(target).not.toContain('Assembly engineer');
    expect(html).toContain('Start assembly generation workflow');
    expect(html.match(/data-engineering-role="manager"/g)).toHaveLength(1);
  });

  it('names the recorded workflow outcome when the manager resumes, retaining its failure', () => {
    const child = operation({ id: 'child', kind: 'assembly_create', status: 'running' });
    const op = turn([], { request: { ...operation().request,
      trigger: { type: 'sub_agent', operation_id: child.id, status: 'failed' } } });
    const html = renderTurn(op, 'journey', [child]);
    expect(html).toContain('Received the outcome of Assembly generation workflow (Failed)');
    expect(html).not.toContain('after Project manager ended');
    expect(html).not.toContain('after Assembly engineer ended');
  });

  it('identifies the manager without treating a finished turn with a failed design tool as a saved result', () => {
    const html = renderTurn(turn([{ tool: 'plan_design', status: 'failed', error: { code: 'bad_response', message: 'No design was saved' } }]));
    expect(html).toContain('data-engineering-role="manager"');
    expect(html).toContain('data-engineering-role="design"');
    expect(html).toContain('aria-label="Project manager · Turn complete"');
    expect(html).toContain('No design was saved');
    expect(html).not.toContain('as-cad-result');
  });

  it('gives direct and child drawing plans the same role while keeping them unapplied', () => {
    for (const parent_operation_id of [null, 'manager-turn']) {
      const op = operation({ kind: 'drafting_modify', parent_operation_id, result: draftingPlan() as unknown as AiOperation['result'] });
      const html = render(h(OperationCard, { op, archived: false }));
      expect(html).toContain('data-engineering-role="drawing"');
      expect(html).toContain('Plan ready');
      expect(html).toContain('not applied');
      expect(html).not.toContain('as-cad-result');
      expect(html).not.toContain('Sub-agent');
    }
  });

  it('keeps failed tools and Undo visible while folding completed history and usage once', () => {
    const op = turn([
      { index: 0, tool: 'project_overview' },
      { index: 1, tool: 'plan_design', status: 'failed', error: { code: 'bad_response', message: 'Design still needs repair' } },
      { index: 2, tool: 'create_part', refs: { part_id: 'part' }, undo: true },
    ]);
    const html = renderTurn(op);
    const visible = outsideDetails(html);
    expect(visible).toContain('Turn complete');
    expect(visible).toContain('data-step-index="1"');
    expect(visible).toContain('Design still needs repair');
    expect(visible).toContain('as-step-undo');
    expect(visible).not.toContain('data-step-index="0"');
    expect(visible).not.toContain('Test model');
    expect(html.match(/data-step-index="0"/g)).toHaveLength(1);
    expect(html.match(/Test model/g)).toHaveLength(1);
    expect(html).toContain('<details class="as-op-disclosure as-op-history">');
    expect(html).toContain('<details class="as-op-disclosure as-op-technical">');
  });

  it('keeps live work, child plans and Stop outside completed activity', () => {
    const child = operation({ id: 'child', kind: 'drafting_modify', result: draftingPlan() as unknown as AiOperation['result'] });
    const html = renderTurn(turn([
      { index: 0, tool: 'project_overview' },
      { index: 1, tool: 'plan_design', status: 'running' },
      { index: 2, tool: 'modify_drawing', refs: { operation_id: child.id } },
    ], { status: 'running' }), 'journey', [child]);
    const visible = outsideDetails(html);
    expect(visible).toContain('data-step-index="1"');
    expect(visible).toContain('data-step-index="2"');
    expect(visible).toContain('Plan ready');
    expect(visible).toContain('Stop');
    expect(visible).not.toContain('data-step-index="0"');
  });

  it('keeps confirmation and server-defined choices visible, exactly once, with their stored draft', () => {
    const confirm = turn([{ status: 'waiting_confirm' }], {
      status: 'waiting_input', question: { kind: 'confirm', tool: 'create_part', title: 'Create fixture', args: {}, can_answer: true },
    });
    const confirmHtml = outsideDetails(renderTurn(confirm));
    expect(confirmHtml).toContain('as-confirm-acts');
    expect(confirmHtml).toContain('Create fixture');
    const question = designChoiceOperation.question;
    if (!question || question.kind !== 'design_choice') throw new Error('Expected a choice fixture');
    session.designDrafts.write(designChoiceScope(designChoiceOperation.conversation_id, designChoiceOperation.id, question), {
      ...emptyDesignDraft(), freeText: 'My installation constraint', values: { width: { kind: 'value', text: '42' } },
    });
    const choiceHtml = renderTurn(designChoiceOperation);
    const visible = outsideDetails(choiceHtml);
    expect(visible).toContain('value="42"');
    expect(visible).toContain('My installation constraint');
    expect(visible).toContain('Keep my changes; use recommendations');
    expect(visible).toContain('Keep my changes; choose the rest for me');
    expect(choiceHtml.match(/class="as-design-actions"/g)).toHaveLength(1);
    expect(session.store.answerDesign).not.toHaveBeenCalled();
  });

  it('keeps standalone history and model usage expanded by default', () => {
    const html = renderTurn(turn([{ index: 0 }]), 'standalone');
    expect(html).not.toContain('as-op-history');
    expect(html).not.toContain('as-op-technical');
    expect(outsideDetails(html)).toContain('data-step-index="0"');
    expect(outsideDetails(html)).toContain('Test model');
  });
});

function draftingPlan(): AiDraftingModifyResult {
  const document = '4dcd0d4a-bb77-4408-8c52-79b1689b3ce3';
  const head = '26c6ca3f-768b-492e-86e5-3b3128abef80';
  return {
    kind: 'drafting_modify', document_id: document, base_version_id: head, plan_hash: 'a'.repeat(64),
    summary: 'Move selected line', changes: [], warnings: [], questions: [], removals: [], application: null,
    plan: { schema_version: 1, document_id: document, base_version_id: head, base_version_no: 2, state_sha256: 'b'.repeat(64),
      selection: { ids: ['E-1'] }, commands: [{ op: 'transform', ids: ['E-1'], dx: 10 }], affected_entity_ids: ['E-1'], removed_entity_ids: [],
      summary: 'Move selected line', changes: [], warnings: [], questions: [] },
  };
}

describe('journey operation presentation', () => {
  it.each([
    ['assembly_create', 'manager', true], ['assembly_modify', 'assembly', false], ['model_create', 'modeling', false],
  ] as const)('labels %s at its own responsibility level in cards and overview', (kind, role, coordinated) => {
    const op = operation({ kind, parent_operation_id: 'parent' });
    const card = render(h(OperationCard, { op, archived: false, presentation: 'journey' }));
    const overview = render(h(SessionOverview, { detail: { operations: [op] } as AiConversationDetail }));
    for (const html of [card, overview]) {
      expect(html).toContain(`data-engineering-role="${role}"`);
      if (coordinated) {
        expect(html).toContain('Workflow coordinator');
        expect(html).toContain('Assembly generation workflow');
        expect(html).not.toContain('data-engineering-role="assembly"');
      } else {
        expect(html).not.toContain('Workflow coordinator');
        expect(html).not.toContain('data-engineering-role="manager"');
      }
    }
  });

  it('keeps failed assembly coordination and saved partial outputs visible together', () => {
    const op = operation({ kind: 'assembly_create', status: 'failed', run: { id: 'run', status: 'partial', title: 'G203' },
      error: { code: 'missing_geometry', message: 'Some parts could not be built' },
      result: { kind: 'assembly', items: [], cad_result: saved } });
    const visible = outsideDetails(render(h(OperationCard, { op, archived: false, presentation: 'journey' })));
    expect(visible).toContain('data-engineering-role="manager"');
    expect(visible).toContain('Workflow coordinator');
    expect(visible).toContain('Failed');
    expect(visible).toContain('Some parts could not be built');
    expect(visible).toContain('/assemblies/fixture/structure?rev=a2');
    expect(visible).toContain('test-run-progress');
    expect(visible).toContain('Retry');
  });

  it('keeps exact saved results, Undo, previews and Turbo disclosures visible while folding technical context', () => {
    const op = operation({ kind: 'assembly_create', execution_policy: 'turbo',
      run: { id: 'run', status: 'succeeded', title: 'Fixture' }, result: { kind: 'assembly', items: [], cad_result: saved } });
    const html = render(h(OperationCard, { op, archived: false, presentation: 'journey' }));
    const visible = outsideDetails(html);
    expect(visible).toContain('/assemblies/fixture/structure?rev=a2');
    expect(visible).toContain('as-cad-result');
    expect(visible).toContain('prog-results');
    expect(visible).toContain('Undo');
    expect(visible).toContain('Turbo');
    expect(visible).toContain('not verified');
    expect(visible).not.toContain('st-ctx');
    expect(visible).not.toContain('Test model');
    expect(html).toContain('Test model');
  });

  it.each(['running', 'failed'] as const)('keeps %s progress, saved partial results and recovery outside details', (status) => {
    const op = operation({ status, kind: 'model_create', run: { id: 'run', status, title: 'Fixture' },
      error: status === 'failed' ? { code: 'bad_response', message: 'Model failed' } : null,
      result: { kind: 'model', items: [], cad_result: saved } });
    const visible = outsideDetails(render(h(OperationCard, { op, archived: false, presentation: 'journey' })));
    expect(visible).toContain('test-run-progress');
    expect(visible).toContain('prog-results');
    expect(visible).toContain('as-cad-result');
    expect(visible).toContain(status === 'failed' ? 'as-op-fail' : 'Stop');
    if (status === 'failed') expect(visible).toContain('Retry');
  });

  it('keeps the exact unapplied plan and Apply visible', () => {
    const plan = draftingPlan();
    const op = operation({ kind: 'drafting_modify', result: plan as unknown as AiOperation['result'] });
    const html = render(h(OperationCard, { op, archived: false, presentation: 'journey' }));
    const visible = outsideDetails(html);
    expect(visible).toContain('Plan ready');
    expect(visible).toContain('Move selected line');
    expect(visible).toContain('Apply');
    expect(visible).toContain(`version=${plan.base_version_id}`);
    expect(html).toContain(plan.plan_hash);
    expect(visible).not.toContain('as-cad-result');
  });
});

describe('compact saved design', () => {
  it('folds adopted decisions without dropping provenance or values; awaiting choices stay visible', () => {
    const adopted = { ...designChoicePlan, status: 'adopted' as const };
    const html = render(h(DesignPlanSummary, { plan: adopted, compact: true }));
    expect(html).toContain('<details class="as-op-disclosure as-op-design">');
    expect(html).toContain('80 mm');
    expect(html).toContain('AI design');
    expect(outsideDetails(html)).not.toContain('80 mm');
    const waiting = render(h(DesignPlanSummary, { plan: designChoicePlan, compact: true }));
    expect(waiting).not.toContain('as-op-design');
    expect(outsideDetails(waiting)).toContain('80 mm');
  });
});

describe('operation navigation through disclosures', () => {
  it('opens both journey and execution history before focusing and scrolling the exact step', () => {
    const order: string[] = [];
    const outer = { tagName: 'DETAILS', open: false, parentElement: null };
    const card = { tagName: 'ARTICLE', dataset: { opId: 'exact' }, parentElement: outer, offsetWidth: 50,
      classList: { add: vi.fn(), remove: vi.fn() }, querySelector: vi.fn(), focus: vi.fn(), scrollIntoView: vi.fn() };
    const inner = { tagName: 'DETAILS', open: false, parentElement: card };
    const step = { parentElement: inner, focus: vi.fn(() => {
      expect(outer.open && inner.open).toBe(true); order.push('focus');
    }), scrollIntoView: vi.fn(() => { order.push('scroll'); }) };
    card.querySelector.mockReturnValue(step);
    const root = { querySelectorAll: vi.fn(() => [card]) };
    vi.stubGlobal('window', { matchMedia: () => ({ matches: true }), setTimeout: vi.fn() });
    expect(revealOperation(root as unknown as ParentNode, 'exact', 4)).toBe(step);
    expect(card.querySelector).toHaveBeenCalledWith('[data-step-index="4"]');
    expect(order).toEqual(['focus', 'scroll']);
    expect(step.scrollIntoView).toHaveBeenCalledWith({ block: 'center', behavior: 'auto' });
    expect(card.focus).not.toHaveBeenCalled();
    expect(card.classList.add).toHaveBeenCalledWith('as-flash');
  });

  it('leaves unrelated cards untouched and falls back to the exact card when a step is missing', () => {
    const outer = { tagName: 'DETAILS', open: false, parentElement: null };
    const card = { tagName: 'ARTICLE', dataset: { opId: 'exact' }, parentElement: outer, offsetWidth: 50,
      classList: { add: vi.fn(), remove: vi.fn() }, querySelector: vi.fn(() => null), focus: vi.fn(), scrollIntoView: vi.fn() };
    const root = { querySelectorAll: vi.fn(() => [card]) };
    vi.stubGlobal('window', { matchMedia: () => ({ matches: false }), setTimeout: vi.fn() });
    expect(revealOperation(root as unknown as ParentNode, 'other')).toBeNull();
    expect(outer.open).toBe(false);
    expect(card.focus).not.toHaveBeenCalled();
    expect(revealOperation(root as unknown as ParentNode, 'exact', 999)).toBe(card);
    expect(outer.open).toBe(true);
    expect(card.focus).toHaveBeenCalledWith({ preventScroll: true });
  });
});
