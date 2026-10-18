import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { capabilitiesSnapshot, capabilityFeatures, featureEnabled, FEATURE_NAMES, loadCapabilities, operationChoices, refreshCapabilities, runChoices, setCapabilitiesIdentity } from './capabilities';
import { getCapabilities } from './endpoints';
import { loadLimits } from './limits';
import type { Capabilities, FeatureFlags } from './types';

vi.mock('./endpoints', () => ({ getCapabilities: vi.fn() }));
const flags = (disabled?: keyof FeatureFlags): FeatureFlags => Object.fromEntries(FEATURE_NAMES.map((key) => [key, key !== disabled])) as FeatureFlags;
const response = (features?: FeatureFlags) => ({ features, limits: { inputs: { chat: 42 }, context: {} } }) as Capabilities;

beforeEach(() => { vi.mocked(getCapabilities).mockReset(); setCapabilitiesIdentity('user-a'); });
afterEach(() => setCapabilitiesIdentity(null));

describe('shared authenticated system capabilities', () => {
  it('hides writes until one shared status/limits/features request succeeds', async () => {
    let finish!: (v: Capabilities) => void;
    vi.mocked(getCapabilities).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const first = loadCapabilities();
    const limits = loadLimits();
    expect(getCapabilities).toHaveBeenCalledTimes(1);
    expect(featureEnabled('tasks')).toBe(false);
    expect(capabilitiesSnapshot().status).toBe('loading');
    finish(response(flags()));
    await first;
    expect((await limits)?.inputs.chat).toBe(42);
    expect(featureEnabled('tasks')).toBe(true);
    expect(capabilitiesSnapshot().status).toBe('ready');
  });

  it.each(FEATURE_NAMES)('honors the %s switch and re-enables it after refresh', async (feature) => {
    vi.mocked(getCapabilities).mockResolvedValue(response(flags(feature)));
    await loadCapabilities();
    expect(featureEnabled(feature)).toBe(false);
    if (feature === 'tasks') expect(featureEnabled('collaboration')).toBe(false);
    vi.mocked(getCapabilities).mockResolvedValue(response(flags()));
    refreshCapabilities();
    expect(featureEnabled(feature)).toBe(false);
    await loadCapabilities();
    expect(featureEnabled(feature)).toBe(true);
  });

  it('clears old limits/features immediately, aborts old requests, and ignores late identity responses', async () => {
    let finishOld!: (v: Capabilities) => void;
    vi.mocked(getCapabilities).mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }));
    const old = loadCapabilities();
    const signal = vi.mocked(getCapabilities).mock.calls[0][0]?.signal;
    setCapabilitiesIdentity('user-b');
    expect(signal?.aborted).toBe(true);
    expect(capabilitiesSnapshot().data).toBeNull();
    expect(featureEnabled('tasks')).toBe(false);
    vi.mocked(getCapabilities).mockResolvedValue(response(flags('tasks')));
    await loadCapabilities();
    finishOld(response(flags()));
    expect(await old).toBeNull();
    expect(featureEnabled('tasks')).toBe(false);
    setCapabilitiesIdentity(null);
    expect(capabilitiesSnapshot().data).toBeNull();
    expect(await loadLimits()).toBeNull();
  });

  it('fails closed on refresh errors without reporting a known disabled feature', async () => {
    vi.mocked(getCapabilities).mockResolvedValue(response(flags()));
    await loadCapabilities();
    vi.mocked(getCapabilities).mockRejectedValue(new Error('offline'));
    refreshCapabilities();
    await loadCapabilities();
    expect(capabilitiesSnapshot()).toMatchObject({ status: 'error', data: null, features: null, loading: false });
    expect(featureEnabled('tasks')).toBe(false);
  });

  it('supports successful old responses explicitly, but refuses malformed feature maps', async () => {
    vi.mocked(getCapabilities).mockResolvedValue(response());
    await loadCapabilities();
    expect(capabilitiesSnapshot().legacy).toBe(true);
    expect(featureEnabled('tasks')).toBe(true);
    expect(() => capabilityFeatures({ tasks: true })).toThrow();
    expect(() => capabilityFeatures(null)).toThrow();
    expect(() => capabilityFeatures({ ...flags(), model_choice: 'false' })).toThrow();
  });

  it('omits hidden fresh-request choices without changing the draft id or template overrides', async () => {
    vi.mocked(getCapabilities).mockResolvedValue(response({ ...flags(), turbo_choice: false, model_choice: false }));
    await loadCapabilities();
    const body = { client_request_id: 'same-request', text: 'make a part', model_config_id: 'private-choice', execution_policy: 'turbo', params: { node: { model_config_id: 'template-advanced' } } };
    expect(operationChoices(body)).toEqual({ client_request_id: 'same-request', text: 'make a part', params: body.params });
    expect(body.model_config_id).toBe('private-choice');
    expect(body.execution_policy).toBe('turbo');
  });

  it('removes hidden runtime model overrides without changing saved graph parameters or the input draft', async () => {
    vi.mocked(getCapabilities).mockResolvedValue(response(flags('model_choice')));
    await loadCapabilities();
    const body = {
      client_request_id: 'same-run',
      execution_policy: 'standard',
      params: { generate: { model_config_id: 'runtime-model', temperature: 0.4 }, verify: { strict: true } },
      graph: { nodes: [{ params: { model_config_id: 'authored-model' } }] },
    };
    expect(runChoices(body)).toEqual({ ...body, params: { generate: { temperature: 0.4 }, verify: { strict: true } } });
    expect(body.params.generate.model_config_id).toBe('runtime-model');
    expect(body.graph.nodes[0].params.model_config_id).toBe('authored-model');
  });
});
