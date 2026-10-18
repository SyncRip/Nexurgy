import { describe,expect,it } from 'vitest';
import type { ModelConfig } from '../../api/types';
import { DEFAULT_ENV,PRESETS,emptyForm,formFromConfig,needsKey,normalizeEnvName,presetForm,revocableOld,toCreate,toListRequest,toPatch,validateForm } from './modelConfigForm';

const existing: ModelConfig = {
  id: 'm1',
  label: 'Custom model',
  provider: 'openai_compatible',
  model_id: 'custom-model',
  base_url: 'https://models.example.com/v1',
  secret_ref: 'env:MODEL_API_KEY',
  capabilities: { text: true, vision: true, structured: true },
  is_default: true,
  enabled: true,
  key_present: true,
  ready: true,
  issues: [],
  last_test: null,
};

describe('model config form', () => {
  it('starts new configurations without a model or custom endpoint', () => {
    const f = emptyForm(true);
    expect(f.label).toBe('');
    expect(f.model_id).toBe('');
    expect(f.base_url).toBe('');
    expect(f.provider).toBe('openai');
    expect(f.is_default).toBe(true);
    expect(f.env_name).toBe('OPENAI_API_KEY');
    expect(f.api_key).toBe('');
    expect(emptyForm(false).is_default).toBe(false);
    expect(validateForm(f)).toEqual({ label: 'required', model_id: 'required' });
    expect(validateForm({ ...f, label: 'My model', model_id: 'my-model' })).toEqual({});
  });

  it('offers Gemini as an optional preset', () => {
    expect(PRESETS.map((p) => p.id)).toEqual(['gemini']);
    const f = presetForm('gemini', true);
    expect(f.provider).toBe('gemini');
    expect(f.model_id).toBe('gemini-2.5-pro');
    expect(f.is_default).toBe(true);
    expect(f.base_url).toBe('');
    expect(validateForm(f)).toEqual({});
    expect(toCreate(f).secret_ref).toBe('env:GEMINI_API_KEY');
    expect(DEFAULT_ENV.openai_compatible).toBe('MODEL_API_KEY');
  });

  it('only accepts an environment variable name for the key', () => {
    const f = formFromConfig(existing);
    expect(validateForm({ ...f, env_name: 'sk-live-abc123' })).toEqual({ env_name: 'invalid_env' });
    expect(validateForm({ ...f, env_name: '' })).toEqual({ env_name: 'required' });
    expect(normalizeEnvName(' env:my_key ')).toBe('MY_KEY');
    expect(validateForm({ ...f, env_name: 'env:MY_KEY' })).toEqual({});
  });

  it('requires a manually supplied base URL for OpenAI-compatible services', () => {
    const f = formFromConfig(existing);
    expect(validateForm({ ...f, base_url: '' })).toEqual({ base_url: 'required' });
    expect(validateForm({ ...f, base_url: 'models.example.com' })).toEqual({ base_url: 'invalid_url' });
    expect(validateForm(f)).toEqual({});
    expect(validateForm({ ...f, provider: 'openai', base_url: '' })).toEqual({});
    expect(validateForm({ ...presetForm('gemini', false), base_url: '' })).toEqual({});
    expect(validateForm({ ...f, label: ' ', model_id: '' })).toEqual({ label: 'required', model_id: 'required' });
  });

  it('patches only what changed', () => {
    const f = formFromConfig(existing);
    expect(f.provider).toBe('openai_compatible');
    expect(f.base_url).toBe(existing.base_url);
    expect(f.model_id).toBe(existing.model_id);
    expect(f.env_name).toBe('MODEL_API_KEY');
    expect(toPatch(f, existing)).toEqual({});
    expect(toPatch({ ...f, enabled: false }, existing)).toEqual({ enabled: false });
    expect(toPatch({ ...f, base_url: '' }, existing)).toEqual({ base_url: null });
    expect(toPatch({ ...f, default_for: ['assistant'] }, existing)).toEqual({ default_for: ['assistant'] });
    expect(toCreate(f)).not.toHaveProperty('capabilities');
    expect(toPatch({ ...f, env_name: 'OTHER_KEY' }, existing)).toEqual({ secret_ref: 'env:OTHER_KEY' });
  });
});

const KEY = 'sk-test-W1-form-0123456789abcdef';
const direct: ModelConfig = {
  ...existing,
  id: 'm2',
  secret_ref: null,
  credential_source: 'direct',
  credential_status: 'active',
  credential_version: 3,
  config_version: 5,
  credential_versions: [
    { version: 3, source: 'direct', status: 'active', created_at: null, replaced_at: null, revoked_at: null, cleared_at: null },
    { version: 2, source: 'direct', status: 'replaced', created_at: null, replaced_at: null, revoked_at: null, cleared_at: null },
    { version: 1, source: 'direct', status: 'revoked', created_at: null, replaced_at: null, revoked_at: null, cleared_at: null },
  ],
};

describe('direct API key (W1)', () => {
  it('creates with a write-only key and refuses the mask or an empty key', () => {
    const f = { ...formFromConfig(existing), credential_source: 'direct' as const, api_key: ` ${KEY} ` };
    expect(validateForm(f)).toEqual({});
    const body = toCreate(f);
    expect(body.credential).toEqual({ api_key: KEY });
    expect(body.secret_ref).toBeUndefined();
    expect(validateForm({ ...f, api_key: '' })).toEqual({ api_key: 'required' });
    expect(validateForm({ ...f, api_key: '••••••••' })).toEqual({ api_key: 'masked' });
    expect(validateForm({ ...f, api_key: 'sk-…cdef' })).toEqual({ api_key: 'masked' });
    expect(validateForm({ ...f, api_key: 'has space inside' })).toEqual({ api_key: 'invalid_key' });
  });

  it('keeps a stored key unless replace or clear is chosen explicitly', () => {
    const f = formFromConfig(direct);
    expect(f.stored_direct).toBe(true);
    expect(f.api_key).toBe('');
    expect(validateForm(f)).toEqual({});
    expect(needsKey(f)).toBe(false);
    expect(toPatch(f, direct)).toEqual({});
    expect(toPatch({ ...f, label: 'Renamed' }, direct)).toEqual({ label: 'Renamed' });
    const replace = { ...f, key_action: 'replace' as const };
    expect(validateForm(replace)).toEqual({ api_key: 'required' });
    expect(toPatch({ ...replace, api_key: KEY }, direct)).toEqual({ credential: { api_key: KEY } });
    expect(toPatch({ ...f, key_action: 'clear' }, direct)).toEqual({ credential: { clear: true } });
    // A cleared / revoked config has nothing to keep: a key is required.
    const cleared = formFromConfig({ ...direct, credential_status: 'cleared', credential_version: null });
    expect(cleared.stored_direct).toBe(false);
    expect(validateForm(cleared)).toEqual({ api_key: 'required' });
  });

  it('switches sources explicitly and lists the replaced versions that can be revoked', () => {
    const toEnv = { ...formFromConfig(direct), credential_source: 'env' as const, env_name: 'CUSTOM_MODEL_KEY' };
    expect(toPatch(toEnv, direct)).toEqual({ secret_ref: 'env:CUSTOM_MODEL_KEY' });
    const toDirect = { ...formFromConfig(existing), credential_source: 'direct' as const };
    expect(validateForm(toDirect)).toEqual({ api_key: 'required' });
    expect(toPatch({ ...toDirect, api_key: KEY }, existing)).toEqual({ credential: { api_key: KEY } });
    expect(revocableOld(direct)).toEqual([2]);
  });
});

describe('fetch models request', () => {
  it('uses the named variable, a typed key, or the saved key of the edited config', () => {
    const f = formFromConfig(existing);
    expect(toListRequest(f)).toEqual({ provider: 'openai_compatible', base_url: 'https://models.example.com/v1', credential: { env_name: 'MODEL_API_KEY' } });

    const direct = { ...f, credential_source: 'direct' as const, api_key: '' };
    expect(toListRequest(direct)).toBeNull(); // no key typed yet
    expect(toListRequest({ ...direct, api_key: '••••••••••' })).toBeNull(); // the mask is never a key
    expect(toListRequest({ ...direct, api_key: ' sk-typed-key-123 ' })?.credential).toEqual({ api_key: 'sk-typed-key-123' });

    const saved: ModelConfig = { ...existing, secret_ref: null, credential_source: 'direct', credential_status: 'active' };
    expect(toListRequest(formFromConfig(saved), saved)).toEqual({
      provider: 'openai_compatible', base_url: 'https://models.example.com/v1', config_id: 'm1', credential: { keep: true },
    });
    expect(toListRequest({ ...formFromConfig(saved), key_action: 'clear' }, saved)).toBeNull();
  });
});
