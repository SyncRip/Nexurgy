import { describe, expect, it } from 'vitest';
import type { UpstreamAssumption } from '../../api/turbo';
import { confirmSourceOf, effectivePolicy, groupByOwner, pendingKeys, pinnedSpecVersion, policyConsequence, requestPolicySource, resolvePolicy } from './policyModel';

const item = (key: string, owner: string, state: UpstreamAssumption['state'], type = 'engineering_spec_version'): UpstreamAssumption => ({
  key,
  id: key.split('#')[1] ?? key,
  subject: 'D-02.length',
  field: 'length',
  adopted: 20,
  unit: 'mm',
  author: 'ai_turbo',
  state,
  owner: { type, id: owner, label: owner.toUpperCase() },
  basis: 'typical',
  alternatives: [],
});

describe('S5 policy source (design §3.4)', () => {
  it('resolves explicit > inherited > project default > standard', () => {
    expect(resolvePolicy(null)).toEqual({ policy: 'standard', source: 'default' });
    expect(resolvePolicy(undefined, null, 'turbo')).toEqual({ policy: 'turbo', source: 'project_default' });
    expect(resolvePolicy(null, null, 'standard')).toEqual({ policy: 'standard', source: 'default' });
    expect(resolvePolicy(null, 'standard', 'turbo')).toEqual({ policy: 'standard', source: 'inherited' });
    expect(resolvePolicy(null, 'turbo')).toEqual({ policy: 'turbo', source: 'inherited' });
    expect(resolvePolicy('standard', 'turbo', 'turbo')).toEqual({ policy: 'standard', source: 'explicit' });
    expect(resolvePolicy(null, 'weird', 'weird')).toEqual({ policy: 'standard', source: 'default' });
  });

  it('follows the server preview unless the person picked a policy', () => {
    const preview = { execution_policy: 'turbo' as const, policy_source: 'inherited' as const, source_version: null, project_default: 'standard' as const };
    expect(effectivePolicy(null, preview)).toEqual({ policy: 'turbo', source: 'inherited' });
    expect(effectivePolicy('standard', preview)).toEqual({ policy: 'standard', source: 'explicit' });
    expect(effectivePolicy(null, null, { projectDefault: 'turbo' })).toEqual({ policy: 'turbo', source: 'project_default' });
    expect(effectivePolicy(null, null)).toEqual({ policy: 'standard', source: 'default' });
  });

  it('explains an explicit switch away from the source', () => {
    const turboSpec = { type: 'engineering_spec_version', execution_policy: 'turbo' as const };
    const turboAsm = { type: 'assembly_revision', execution_policy: 'turbo' as const };
    const stdAsm = { type: 'assembly_revision', execution_policy: 'standard' as const };
    expect(policyConsequence('standard', turboSpec, { unconfirmed: 3 })).toEqual({ code: 'confirm_required', n: 3 });
    expect(policyConsequence('standard', turboAsm, { unconfirmed: 2 })).toEqual({ code: 'confirm_upstream', n: 2 });
    expect(policyConsequence('standard', turboSpec, { unconfirmed: 1, corrected: 1 })).toEqual({ code: 'corrected', n: 1 });
    expect(policyConsequence('standard', turboSpec, { unconfirmed: 0 })).toBeNull(); // every value confirmed
    expect(policyConsequence('turbo', stdAsm, null)).toEqual({ code: 'turbo_over_standard' });
    expect(policyConsequence('turbo', turboAsm, { unconfirmed: 5 })).toBeNull(); // following the source asks nothing
    expect(policyConsequence('standard', null, { unconfirmed: 5 })).toBeNull(); // no source version
  });

  it('groups the upstream list by owner in server order and lists what waits', () => {
    const items = [item('s:1#A-01', 'spec', 'unconfirmed'), item('a:1#A-02', 'asm', 'confirmed', 'assembly_revision'), item('s:1#A-03', 'spec', 'corrected')];
    const groups = groupByOwner(items);
    expect(groups.map((g) => [g.owner.id, g.items.length])).toEqual([['spec', 2], ['asm', 1]]);
    expect(pendingKeys(items)).toEqual(['s:1#A-01']);
  });

  it('reads the confirm entry from a standard-on-Turbo-spec error only', () => {
    expect(confirmSourceOf({ code: 'upstream_assumptions_unconfirmed', details: { spec_version_id: 'v1', count: 2 } }))
      .toEqual({ source_type: 'engineering_spec_version', source_id: 'v1', count: 2 });
    expect(confirmSourceOf({ code: 'spec_policy_mismatch', details: { spec_version_id: 'v1' } })).toBeNull();
    expect(confirmSourceOf({ code: 'upstream_assumptions_unconfirmed', details: {} })).toBeNull();
    expect(confirmSourceOf(null)).toBeNull();
  });

  it('picks the version a workspace request continues from like the server', () => {
    const asm = { kind: 'assembly_revision', object_id: 'r1' };
    const rev = { kind: 'part_revision', object_id: 'm1' };
    const part = { kind: 'part', object_id: 'p1' };
    const spec = { kind: 'engineering_spec_version', object_id: 's1' };
    expect(requestPolicySource('assembly_modify', asm)).toEqual({ source_type: 'assembly_revision', source_id: 'r1' });
    expect(requestPolicySource('model_modify', rev)).toEqual({ source_type: 'model_revision', source_id: 'm1' });
    expect(requestPolicySource('drawing_generate', part)).toEqual({ source_type: 'part', source_id: 'p1' });
    expect(requestPolicySource('model_create', part)).toBeNull();
    expect(requestPolicySource('assembly_create', null, [spec])).toEqual({ source_type: 'engineering_spec_version', source_id: 's1' });
    expect(requestPolicySource('assembly_create', null, [spec, { ...spec, object_id: 's2' }])).toBeNull(); // ambiguous
    // a run continues from the one spec version its graph pins (run params applied), like graph_source
    const node = (id: string, spec_version_id?: string) => ({ id, type: 'engineering_spec', params: spec_version_id ? { spec_version_id } : {} });
    expect(pinnedSpecVersion({ nodes: [node('a', 's1'), { id: 'm', type: 'model_input' }] })).toBe('s1');
    expect(pinnedSpecVersion({ nodes: [node('a')] }, { a: { spec_version_id: 's2' } })).toBe('s2');
    expect(pinnedSpecVersion({ nodes: [node('a', 's1'), node('b', 's2')] })).toBeNull();
  });
});
