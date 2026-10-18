import { describe, expect, it } from 'vitest';
import type { ModelConfig, ModelProbe } from '../../api/types';
import { acceptProbeUpdate, probePending } from './probe';

const probe: ModelProbe = {
  id: 'attempt-1', status: 'running', tested_config_version: 3, applied_config_version: null,
  started_at: null, finished_at: null, expires_at: '2026-10-16T10:05:00Z', stale: false, simulated: false,
  detected: { text: null, structured: null, vision: null, tools: null }, checks: {}, error: null,
};
const config: ModelConfig = {
  id: 'model-1', label: 'Model', provider: 'openai_compatible', model_id: 'model', base_url: null,
  secret_ref: 'env:TEST_KEY', capabilities: { text: true, structured: true, vision: false },
  is_default: false, enabled: true, key_present: true, ready: true, issues: [], last_test: null,
  config_version: 3, probe,
};

describe('model probe generations and expiry', () => {
  it('polls queued/running probes only until their expiry', () => {
    expect(probePending(config, Date.parse('2026-10-16T10:04:59Z'))).toBe(true);
    expect(probePending(config, Date.parse('2026-10-16T10:05:00Z'))).toBe(false);
    expect(probePending({ ...config, probe: { ...probe, status: 'passed' } }, 0)).toBe(false);
    expect(probePending({ ...config, probe: undefined }, 0)).toBe(false);
    expect(probePending({ ...config, probe: { ...probe, expires_at: 'bad' } }, 0)).toBe(false);
  });
  it('discards a poll for another model, older configuration or replaced attempt', () => {
    for (const next of [{ ...config, id: 'other' }, { ...config, config_version: 2 }]) {
      expect(acceptProbeUpdate(config, next, probe.id)).toBe(config);
    }
    const restarted = { ...config, probe: { ...probe, id: 'attempt-2' } };
    expect(acceptProbeUpdate(restarted, config, probe.id)).toBe(restarted);
  });
  it('accepts a completed attempt and never rolls it back to running', () => {
    const passed = { ...config, probe: { ...probe, status: 'passed' as const } };
    expect(acceptProbeUpdate(config, passed, probe.id)).toBe(passed);
    expect(acceptProbeUpdate(passed, config, probe.id)).toBe(passed);
    const newer = { ...passed, config_version: 4 };
    expect(acceptProbeUpdate(config, newer, probe.id)).toBe(newer);
  });
});
