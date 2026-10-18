import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AiAssemblyChain, AiOperation, AiRef, AiResultItem, CadResult } from '../../api/assistant';
import type { AssemblyValue, DeliveryValue } from '../../api/orchestration';
import type { RunProgress } from '../../api/progress';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ThemeProvider } from '../../prefs/ThemeProvider';
import { run, stage, unit } from '../progress/test-fixtures';
import { OperationCard } from './OperationCard';

const drawingProgress = vi.hoisted(() => ({ current: undefined as RunProgress | undefined }));
afterEach(() => { drawingProgress.current = undefined; });

vi.mock('./AssistantProvider', () => ({
  useAssistantSession: () => ({ store: { detail: { conversation: { id: 'conversation-1', project: { id: 'project-1' } } } }, keepDockOnLeave: vi.fn(), requestIds: { forget: vi.fn() } }),
  useAssistant: () => ({ pending: { references: [] } }),
}));
vi.mock('../progress/OperationProgress', () => ({
  OperationProgress: () => h('section', { className: 'test-run-progress' }, 'Execution summary'),
}));
vi.mock('../progress/useRunProgress', () => ({
  useRunProgress: (runId?: string) => ({
    progress: drawingProgress.current ?? (runId ? { ...run('succeeded', [stage('assembly', 'succeeded', [unit('definition:wheel', 'built', {
      title: 'Wheel', quantity: 4, revision: { id: 'wheel-r1', part_id: 'wheel', label: 'R1', status: 'ready', size: null, operations: [] },
    })])]), run_id: runId } : null),
    error: null, reload: vi.fn(), expanded: false, setExpanded: vi.fn(),
  }),
}));

const saved: CadResult = {
  event_id: 'event', object_type: 'assembly', object_id: 'cart', version_id: 'a1', version_label: 'A1',
  base_version_id: null, asset_version_id: null, requirements: [], changes: [], checks: {}, artifacts: [],
  undo: { eligible: false, reason: 'no_previous_version', expected_head: 'a1' },
};
const assembly: AssemblyValue = {
  type: 'assembly', spec_version_id: 'spec-v1', execution_policy: 'standard', complete: true,
  definitions: [], parts: [], unplaced_definitions: [], simulated: false,
  assembly_id: 'cart', assembly_revision_id: 'a1', label: 'A1', check_status: 'passed',
};
const delivery: DeliveryValue = {
  type: 'delivery', delivery_id: 'delivery-1', number: 'DEL-1', completeness: 'complete',
  execution_policy: 'standard', check_status: 'passed', simulated: false, files: 3, missing: [],
  manifest_sha256: 'manifest', zip_sha256: 'zip', assembly_revision_id: 'a1',
  verification: { execution_policy: 'standard', check_status: 'passed', acceptance_status: 'not_requested', unverified: true, assumptions: [] },
  decisions: { count: 4, needs_confirmation: 4, with_problems: 0, by_trigger: { missing: 4 } },
};
function operation(patch: Partial<AiOperation> = {}, chainPatch: Partial<AiAssemblyChain> = {}): AiOperation {
  return {
    id: 'op-1', kind: 'assembly_create', status: 'succeeded', request: { references: [] },
    question: null, error: null, run: null, task: null, part: null,
    result: { kind: 'assembly', items: [], cad_result: saved, chain: {
      run_status: 'succeeded', execution_policy: 'standard', questions: [], assumptions: [],
      steps: [
        { node_id: 'assembly', node_type: 'assembly', status: 'succeeded', value: assembly, error: null },
        { node_id: 'delivery', node_type: 'delivery', status: 'succeeded', value: delivery, error: null },
      ], ...chainPatch,
    } }, ...patch,
  } as AiOperation;
}
function render(op: AiOperation, archived = false): string {
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ThemeProvider, null,
    h(ToastProvider, null, h(OperationCard, { op, archived }))))));
}

const wheelItem: AiResultItem = {
  ref: { key: 'part_revision:wheel-r1', kind: 'part_revision', object_id: 'wheel-r1', part_id: 'wheel',
    project_id: 'project-1', title: 'Wheel', source_path: '/parts/wheel/model?rev=wheel-r1' } as AiRef,
  asset_type: 'part_model', asset_version_id: 'wheel-av1', asset_id: 'wheel-asset', label: 'R1',
  base: null, run_id: 'run-1', node_id: 'model', availability: 'ready', publish_state: 'draft',
  actions: ['view', 'continue', 'open_run', 'fork'], labels: ['unverified'], check_status: 'skipped_turbo',
};
function wheelOperation(): AiOperation {
  return operation({ conversation_id: 'conversation-1', kind: 'model_create',
    run: { id: 'run-1', status: 'succeeded', title: 'Wheel run' },
    part: { id: 'wheel', number: 'P-1', name: 'Wheel' },
    result: { kind: 'model', items: [wheelItem], check: { status: 'skipped_turbo', reason: 'The requirement check was skipped' },
      cad_result: { ...saved, object_type: 'part_model', object_id: 'wheel', version_id: 'wheel-r1', version_label: 'R1',
        asset_version_id: 'wheel-av1', requirements: [{ text: 'Diameter 50 mm' }],
        artifacts: [{ kind: 'mesh', sha256: 'wheel-mesh' }, { kind: 'step', sha256: 'wheel-step', filename: 'wheel.step' }],
        undo: { eligible: true, expected_head: 'wheel-r1' } } },
  });
}

describe('one saved part delivery', () => {
  it('composes the model identity, preview, exact actions and checks in one primary card', () => {
    const html = render(wheelOperation());
    expect(html.match(/<section class="as-result(?:\s|")/g)).toHaveLength(1);
    expect(html.match(/class="as-cad-preview"/g)).toHaveLength(1);
    expect(html).not.toContain('prog-result-card');
    expect(html).toContain('Wheel');
    expect(html).toContain('/parts/wheel/model?rev=wheel-r1');
    expect(html).toContain('Continue from this result');
    expect(html).toContain('More actions for this result');
    expect(html).toContain('Diameter 50 mm');
    expect(html).toContain('wheel.step');
    expect(html).toContain('Undo this result');
    expect(html).toContain('The requirement check was skipped');
  });

  it('keeps unmatched historical result versions separate from the primary delivery', () => {
    const op = wheelOperation();
    if (op.result?.kind === 'model') op.result.items.push({ ...wheelItem, asset_version_id: 'wheel-av0', label: 'R0',
      ref: { ...wheelItem.ref, key: 'part_revision:wheel-r0', object_id: 'wheel-r0', source_path: '/parts/wheel/model?rev=wheel-r0' } });
    const html = render(op);
    expect(html.match(/<section class="as-result(?:\s|")/g)).toHaveLength(2);
    expect(html).toContain('/parts/wheel/model?rev=wheel-r0');
  });

  it('does not suppress legacy or cross-project references lacking reliable identity proof', () => {
    const op = wheelOperation();
    if (op.result?.kind === 'model') op.result.items = [{ ...wheelItem, ref: { ...wheelItem.ref, project_id: 'other-project' } }];
    const html = render(op);
    expect(html.match(/<section class="as-result(?:\s|")/g)).toHaveLength(2);
    expect(html).toContain('prog-result-card');
  });

  it('keeps failed-check warnings and simulation visible even when a saved result was produced', () => {
    const op = wheelOperation();
    op.simulated = true;
    if (op.result?.kind === 'model') {
      op.result.items = [{ ...wheelItem, labels: ['simulated'], check_status: 'failed' }];
      op.result.check = { status: 'failed', reason: 'Diameter exceeds the requirement', problems: ['R1: diameter is too large'] };
    }
    const html = render(op);
    const primary = html.slice(html.indexOf('<section class="as-result'));
    const [visible] = primary.split('<div class="as-cad-details">');
    expect(visible).toContain('Diameter exceeds the requirement');
    expect(visible).toContain('Check failed');
    expect(visible).toContain('Simulated');
    expect(visible).toContain('Business: not accepted');
    expect(primary).toContain('<details open=""><summary>Check details</summary>');
    expect(primary).toContain('R1: diameter is too large');
  });

  it('keeps archived viewing and downloads while disabling Continue and Undo', () => {
    const html = render(wheelOperation(), true);
    const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) ?? [];
    expect(buttons.find((button) => button.includes('Continue from this result'))).toContain('disabled=""');
    expect(buttons.find((button) => button.includes('Undo this result'))).toContain('disabled=""');
    expect(buttons.find((button) => button.includes('wheel.step'))).not.toContain('disabled=""');
    expect(html).toContain('/parts/wheel/model?rev=wheel-r1');
  });

  it('does not add actions when the normalized result grants only read access', () => {
    const op = wheelOperation();
    if (op.result?.kind === 'model') op.result.items = [{ ...wheelItem, actions: ['view'] }];
    const html = render(op);
    expect(html).not.toContain('Continue from this result');
    expect(html).not.toContain('More actions for this result');
    expect(html).toContain('Open workspace');
    expect(html).toContain('wheel.step');
  });
});

describe('one saved part drawing delivery', () => {
  function drawingOperation(): AiOperation {
    return operation({ conversation_id: 'conversation-1', kind: 'drawing_generate',
      run: { id: 'drawing-run', status: 'succeeded', title: 'Drawing' }, part: { id: 'wheel', name: 'Wheel' },
      result: { kind: 'drawing', items: [{ ...wheelItem, asset_type: 'drawing', node_id: 'drawing', run_id: 'drawing-run',
        base: wheelItem.ref, ref: { ...wheelItem.ref, kind: 'asset_version', object_id: 'wheel-av1', asset_type: 'drawing' } }],
      cad_result: { ...saved, object_type: 'part_drawing', object_id: 'wheel', version_id: 'drawing-1', asset_version_id: 'wheel-av1',
        artifacts: [{ kind: 'svg', sha256: 'page-1' }, { kind: 'dxf', sha256: 'dxf-1', filename: 'wheel.dxf' }] },
      drawings: [{ drawing_id: 'drawing-1', pages: 2, manifest_sha256: 'manifest-1', annotation_status: 'failed', dwg_status: 'failed' }],
    } });
  }
  function progress(): RunProgress {
    return { ...run('succeeded', [stage('drawing', 'succeeded')]), run_id: 'drawing-run',
      drawings: [{ kind: 'drawing', id: 'drawing-1', node_id: 'drawing', model_revision_id: 'wheel-r1', manifest_sha256: 'manifest-1' }] };
  }

  it('mounts the complete gallery once inside the primary card and keeps package actions and failed checks there', () => {
    drawingProgress.current = progress();
    const html = render(drawingOperation());
    const [before, primary] = html.split('<section class="as-result as-cad-result"');
    expect(before).not.toContain('Loading saved drawing pages');
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(1);
    expect(primary.indexOf('Loading saved drawing pages')).toBeLessThan(primary.indexOf('as-cad-details'));
    expect(html).not.toContain('as-cad-preview-link');
    expect(html).not.toContain('prog-results-title');
    expect(html.match(/Download package/g)).toHaveLength(1);
    expect(primary).toContain('wheel.dxf');
    expect(primary).toContain('/parts/wheel/drawings?drawing=drawing-1');
    expect(primary).toContain('DWG: failed');
    expect(primary).toContain('Annotation check: failed');
    expect(primary).toContain('<details open=""><summary>Whole package</summary>');
  });

  it('keeps the original saved thumbnail and supporting output when progress is absent or its identity conflicts', () => {
    expect(render(drawingOperation())).toContain('as-cad-preview-link');
    drawingProgress.current = { ...progress(), run_id: 'other-run' };
    const html = render(drawingOperation());
    expect(html).toContain('as-cad-preview-link');
    expect(html).toContain('prog-results-title');
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(1);
    expect(html).not.toContain('<summary>Whole package</summary>');
  });

  it('keeps a second package and assembly pages visible beside the matched drawing', () => {
    const op = drawingOperation();
    if (op.result?.kind === 'drawing') op.result.drawings!.push({ drawing_id: 'drawing-2', pages: 1,
      manifest_sha256: 'manifest-2', annotation_status: null, dwg_status: null });
    drawingProgress.current = progress();
    drawingProgress.current.drawings!.push({ kind: 'assembly_drawing', id: 'assembly-drawing', node_id: 'assembly',
      assembly_revision_id: 'a1', manifest_sha256: 'assembly-manifest', completeness: 'partial' });
    const html = render(op);
    expect(html.match(/Loading saved drawing pages/g)).toHaveLength(2);
    expect(html.match(/Download package/g)).toHaveLength(2);
    expect(html).toContain('/parts/wheel/drawings?drawing=drawing-2');
    expect(html).toContain('prog-results-title');
  });
});

describe('saved assembly result hierarchy', () => {
  it('keeps loaded execution progress inside completed assembly details so it cannot displace the preview', () => {
    const html = render(operation({ run: { id: 'run-1', status: 'succeeded', title: 'Cart' } }));
    const [outside, inside] = html.split('<details class="as-result-support">');
    expect(outside).not.toContain('test-run-progress');
    expect(inside).toContain('test-run-progress');
    expect(outside).toContain('prog-results');
    expect(outside.indexOf('as-cad-result')).toBeLessThan(outside.indexOf('prog-results'));
    expect(inside).not.toContain('prog-result-image');
  });

  it.each(['failed', 'running'] as const)('keeps %s execution progress in the main card', (status) => {
    const html = render(operation({ status, run: { id: 'run-1', status, title: 'Cart' } }));
    expect(html).not.toContain('as-result-support');
    expect(html).toContain('test-run-progress');
  });

  it('keeps the model and pending-acceptance summary above collapsed supporting records', () => {
    const html = render(operation());
    expect(html).toContain('<details class="as-result-support">');
    const [outside, inside] = html.split('<details class="as-result-support">');
    expect(outside).toContain('as-cad-result');
    expect(outside).toContain('Generated results are not accepted yet');
    expect(outside).toContain('AI decisions: 4 (4 awaiting a person)');
    expect(outside).toContain('not verified');
    expect(inside).toContain('Parts, drawings and delivery details');
    expect(inside).toContain('DEL-1');
    expect(inside).toContain('Download');
    expect(inside).toContain('orch-chain');
  });

  it.each(['failed', 'cancelled', 'waiting_input'] as const)('keeps %s results expanded', (status) => {
    expect(render(operation({ status }))).not.toContain('as-result-support');
  });

  it('does not collapse a partial saved assembly', () => {
    const op = operation();
    if (op.result?.kind === 'assembly' && op.result.chain) op.result.chain.steps[0].value = { ...assembly, complete: false };
    expect(render(op)).not.toContain('as-result-support');
  });

  it('does not collapse a partial delivery or an unanswered question', () => {
    const op = operation();
    if (op.result?.kind === 'assembly' && op.result.chain) op.result.chain.steps[1].value = { ...delivery, completeness: 'partial' };
    expect(render(op)).not.toContain('as-result-support');
    expect(render(operation({}, { questions: [{ source: 'spec', question: 'Which diameter?' }] }))).not.toContain('as-result-support');
  });

  it('does not hide a failed check even when the operation says succeeded', () => {
    const op = operation();
    if (op.result?.kind === 'assembly' && op.result.chain) op.result.chain.steps[0].value = { ...assembly, check_status: 'failed' };
    expect(render(op)).not.toContain('as-result-support');
  });

  it('keeps specification questions and failed saved checks visible', () => {
    const op = operation();
    if (op.result?.kind === 'assembly' && op.result.chain) op.result.chain.steps.unshift({
      node_id: 'spec', node_type: 'spec', status: 'succeeded', error: null,
      value: { type: 'engineering_spec', spec_id: 'spec', spec_version_id: 'spec-v1', code: 'S1', title: 'Cart', version_no: 1,
        status: 'needs_answers', execution_policy: 'standard', simulated: false, counts: {}, path: '/specs/spec?version=spec-v1',
        open_questions: [], assumptions: [] },
    });
    expect(render(op)).not.toContain('as-result-support');
    const failed = operation();
    if (failed.result) failed.result.cad_result = { ...saved, checks: { interference: { latest: { status: 'failed' } } } };
    expect(render(failed)).not.toContain('as-result-support');
  });

  it('keeps an unapplied assembly plan visible', () => {
    const op = operation({ kind: 'assembly_modify', result: {
      kind: 'assembly_modify', assembly_id: 'cart', assembly_number: 'C1', assembly_name: 'Cart', base_revision_id: 'a1', base_rev_no: 1,
      summary: 'Plan to move a wheel', ops: [{ op: 'move_instance', instance: 'wheel', delta: { translate: [1, 0, 0] } }],
      questions: [], impact: null, removals: [], application: null,
    } as unknown as AiOperation['result'] });
    const html = render(op);
    expect(html).not.toContain('as-result-support');
    expect(html).toContain('Plan to move a wheel');
  });

  it('preserves the existing layout without a saved assembly result', () => {
    const op = operation();
    if (op.result) op.result.cad_result = undefined;
    expect(render(op)).not.toContain('as-result-support');
    if (op.result) op.result.cad_result = { ...saved, object_type: 'part_model' };
    expect(render(op)).not.toContain('as-result-support');
  });
});
