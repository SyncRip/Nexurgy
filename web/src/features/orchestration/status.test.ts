// Generation reliability N: the object-state vocabulary, the Turbo boundary line and the execution context (pure).
import { describe, expect, it } from 'vitest';
import { en } from '../../i18n/en';
import { zh } from '../../i18n/zh';
import {
  asObjectState,
  budgetParts,
  checksLine,
  levelStateCounts,
  OBJECT_STATES,
  operationExecutionContext,
  runDeliveryState,
  runExecutionContext,
  stateTone,
  toolExecutionContext,
} from './status';

describe('object states', () => {
  it('maps every state to its tone; generated_ai is information, never the failure style', () => {
    expect(OBJECT_STATES.map((s) => [s, stateTone(s)])).toEqual([
      ['verified', 'ok'],
      ['generated', 'pending'],
      ['generated_ai', 'measure'],
      ['partial', 'warn'],
      ['blocked', 'err'],
      ['display_only', 'warn'],
      ['simulated', 'stale'],
    ]);
    expect(zh['st.state.generated_ai']).toBe('含 AI 补定，建议复核');
    for (const s of OBJECT_STATES) expect(en[`st.state.${s}`] && zh[`st.hint.${s}`]).toBeTruthy();
  });

  it('never reads an unknown value as success', () => {
    expect(asObjectState('complete')).toBeNull();
    expect(asObjectState(undefined)).toBeNull();
    expect(stateTone('passed')).toBe('warn');
  });

  it('counts the states of a layered delivery and finds the run summary state', () => {
    const o = (state?: string) => ({ state }) as never;
    expect(levelStateCounts({ assembly: [o('partial')], subassembly: [o('generated_ai'), o('generated_ai')], part: [o('blocked'), o(), o('x')] })).toEqual([
      { state: 'generated_ai', n: 2 },
      { state: 'partial', n: 1 },
      { state: 'blocked', n: 1 },
    ]);
    expect(runDeliveryState({ nodes: [{ outputs: { spec: { type: 'engineering_spec' } } }, { outputs: { delivery: { type: 'delivery', state: 'partial' } } }] })).toBe('partial');
    expect(runDeliveryState({ nodes: [{ outputs: {} }] })).toBeNull();
  });
});

describe('checks line', () => {
  it('Turbo: closed checks and their real calls; a call is reported, an incomplete record is said', () => {
    const closed = ['model.conformance', 'assembly.bom'];
    expect(checksLine({ policy: 'turbo', closed, invoked_closed: 0, invoked: {}, skipped: { 'assembly.bom': 2 }, nodes_unrecorded: [] })).toEqual({
      kind: 'turbo_clean', closed: 2, invokedClosed: 0, invoked: 0, skipped: 2, incomplete: false,
    });
    expect(checksLine({ policy: 'turbo', closed, invoked_closed: 1, invoked: { 'assembly.bom': 1 }, tool_calls_unrecorded: 1 })).toMatchObject({
      kind: 'turbo_invoked', invokedClosed: 1, incomplete: true,
    });
    expect(checksLine({ policy: 'standard', closed: [], invoked_closed: 0, invoked: { 'model.gate': 3, 'assembly.bom': 1 } })).toMatchObject({ kind: 'standard', invoked: 4 });
    expect(checksLine(null)).toBeNull();
  });
});

describe('execution context', () => {
  it('a run: policy, its source, the pinned inputs once, the version it continues from and the budget', () => {
    const run = {
      execution_policy: 'turbo',
      policy_source: 'inherited',
      policy_source_version: { type: 'engineering_spec_version', id: 's1', label: 'SP-3 v2', execution_policy: 'turbo' as const },
      budget: { max_calls: 30, max_seconds: 1200 },
      input_bindings: [{ port_key: 'doc', asset_version_id: 'av1', version: { asset_name: '说明', label: 'v4' } }],
      graph: { nodes: [{ id: 'spec', type: 'engineering_spec', params: { spec_version_id: 'sv-1', file_version_ids: ['fv-1', 'fv-2-abcdefgh'] } }] },
      nodes: [{ node_id: 'spec', outputs: { spec: { type: 'engineering_spec', spec_version_id: 'sv-1', code: 'SP-3', title: 'x', version_no: 2, sources: [{ file_version_id: 'fv-1', label: 'BHD1.md', name: 'BHD1.md', version: 1 }] } } }],
    };
    expect(runExecutionContext(run)).toEqual({
      policy: 'turbo', source: 'inherited', sourceLabel: 'SP-3 v2', target: 'SP-3 v2', budget: { max_calls: 30, max_seconds: 1200 },
      inputs: ['说明 v4', 'SP-3 v2', 'BHD1.md v1', '#fv-2-abc'],
    });
    expect(budgetParts({ max_calls: 30, max_seconds: 1200 })).toEqual({ calls: 30, minutes: 20, seconds: null });
    // older runs: no source recorded, no budget — said as such, never guessed
    expect(runExecutionContext({ execution_policy: undefined, policy_source: null, budget: { max_calls: 'x' } })).toMatchObject({ policy: 'standard', source: null, budget: null, inputs: [] });
  });

  it('an operation and an Agent tool result', () => {
    const op = {
      execution_policy: 'standard',
      policy_source: 'project_default',
      request: { target: { title: 'BHD1', version_label: 'A3', kind: 'assembly_revision' }, references: [{ title: 'spec.md', version_label: 'v2' }, { title: 'note' }] },
      run: { id: 'r', status: 'running', title: 't' },
    };
    expect(operationExecutionContext(op)).toEqual({ policy: 'standard', source: 'project_default', sourceLabel: null, inputs: ['spec.md v2'], target: 'BHD1 A3', budget: null });
    expect(toolExecutionContext({ execution_policy: 'turbo', policy_source: 'explicit', inputs: [{ kind: 'file_version', id: '0123456789' }],
      target: { type: 'engineering_spec_version', id: 'abcdef123456' }, budget: { max_calls: 5 } })).toEqual({
      policy: 'turbo', source: 'explicit', sourceLabel: null, inputs: ['file_version #01234567'], target: 'engineering_spec_version #abcdef12',
      budget: { max_calls: 5, max_seconds: null },
    });
    expect(toolExecutionContext(undefined)).toBeNull();
  });
});
