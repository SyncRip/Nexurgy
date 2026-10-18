// Model configuration form (R1.2 P3, Settings; W1 credentials): admins create and edit the server's model
// configurations. A key comes either from a server environment variable (the form names the variable,
// secret_ref "env:NAME") or is entered directly: then it is sent once, write-only, and the server stores it
// encrypted. The typed key lives only in this form's component state until submit — never in
// localStorage / sessionStorage, the URL or a later response. Editing a stored key is explicit:
// keep it, replace it with a new one, or clear it; the masked placeholder is never submitted.

import type { CredentialSource,ModelConfig,ModelConfigCreate,ModelConfigPatch,ModelListRequest,ModelPurpose,Provider } from '../../api/types';

export type KeyAction = 'keep' | 'replace' | 'clear';

export interface ModelConfigForm {
  label: string;
  provider: Provider;
  model_id: string;
  base_url: string;
  /** Where the key comes from. */
  credential_source: CredentialSource;
  /** Variable name only, without the "env:" prefix. */
  env_name: string;
  /** A directly entered key (component state only; sent once on submit). */
  api_key: string;
  /** Editing: keep the stored key, replace it, or clear it (env: clear removes the reference). */
  key_action: KeyAction;
  /** The edited config currently has a usable stored direct key (keep is possible). */
  stored_direct: boolean;
  default_for: ModelPurpose[];
  is_default: boolean;
  enabled: boolean;
}

export type FormErrors = Partial<
  Record<'label' | 'model_id' | 'base_url' | 'env_name' | 'api_key', 'required' | 'invalid_env' | 'invalid_url' | 'masked' | 'invalid_key'>
>;

export interface Preset {
  id: 'gemini';
  form: Omit<ModelConfigForm, 'is_default' | 'enabled' | 'credential_source' | 'api_key' | 'key_action' | 'stored_direct'>;
}

export const DEFAULT_ENV: Record<Provider, string> = { openai: 'OPENAI_API_KEY', openai_compatible: 'MODEL_API_KEY', gemini: 'GEMINI_API_KEY' };
/** Shown (as a placeholder, never as a value) where a stored key is kept. */
export const KEY_MASK = '••••••••••••';

/** Optional provider presets; custom endpoints are configured manually. */
export const PRESETS: Preset[] = [
  {
    id: 'gemini',
    form: {
      label: 'Gemini',
      provider: 'gemini',
      model_id: 'gemini-2.5-pro',
      base_url: '',
      env_name: 'GEMINI_API_KEY',
      default_for: [],
    },
  },
];

const ENV_RE = /^[A-Z][A-Z0-9_]{1,80}$/;
const MASK_CHARS = /^[•●*·…xX]+$/;

/** A new configuration starts without a selected model or custom endpoint. */
export function emptyForm(isFirst: boolean): ModelConfigForm {
  return {
    label: '',
    provider: 'openai',
    model_id: '',
    base_url: '',
    credential_source: 'env',
    env_name: DEFAULT_ENV.openai,
    api_key: '',
    key_action: 'keep',
    stored_direct: false,
    default_for: [],
    is_default: isFirst,
    enabled: true,
  };
}

export function presetForm(id: Preset['id'], isFirst: boolean): ModelConfigForm {
  const p = PRESETS.find((x) => x.id === id)!;
  return {
    ...p.form,
    default_for: [...p.form.default_for],
    credential_source: 'env',
    api_key: '',
    key_action: 'keep',
    stored_direct: false,
    is_default: isFirst,
    enabled: true,
  };
}

function sourceOf(m: ModelConfig): CredentialSource {
  return m.credential_source ?? 'env';
}

export function formFromConfig(m: ModelConfig): ModelConfigForm {
  const source = sourceOf(m);
  return {
    label: m.label,
    provider: m.provider,
    model_id: m.model_id,
    base_url: m.base_url ?? '',
    credential_source: source,
    env_name: (m.secret_ref ?? '').replace(/^env:/, '') || (source === 'env' ? '' : DEFAULT_ENV[m.provider]),
    api_key: '',
    key_action: 'keep',
    stored_direct: source === 'direct' && (m.credential_status ?? 'active') === 'active',
    default_for: [...(m.default_for ?? [])],
    is_default: m.is_default,
    enabled: m.enabled,
  };
}

/** Accept "env:NAME" pasted as well as NAME; never anything that looks like a key value. */
export function normalizeEnvName(raw: string): string {
  return raw.trim().replace(/^env:/i, '').toUpperCase();
}

/** A direct key is needed: a new direct config, a switch to direct, a replacement, or no stored key to keep. */
export function needsKey(f: ModelConfigForm): boolean {
  return f.credential_source === 'direct' && (f.key_action === 'replace' || (!f.stored_direct && f.key_action !== 'clear'));
}

/** The masked placeholder (or any run of mask characters) is never a key. */
export function looksMasked(key: string): boolean {
  const k = key.trim();
  return MASK_CHARS.test(k) || k.includes('•') || k.includes('●') || k.includes('…') || k.includes('***');
}

export function validateForm(f: ModelConfigForm): FormErrors {
  const e: FormErrors = {};
  if (!f.label.trim()) e.label = 'required';
  if (!f.model_id.trim()) e.model_id = 'required';
  if (f.credential_source === 'env' && f.key_action !== 'clear') {
    const env = normalizeEnvName(f.env_name);
    if (!env) e.env_name = 'required';
    else if (!ENV_RE.test(env)) e.env_name = 'invalid_env';
  }
  if (needsKey(f)) {
    const key = f.api_key.trim();
    if (!key) e.api_key = 'required';
    else if (looksMasked(key)) e.api_key = 'masked';
    else if (key.length < 8 || key.length > 1000 || /\s/.test(key)) e.api_key = 'invalid_key';
  }
  const url = f.base_url.trim();
  if (f.provider === 'openai_compatible' && !url) e.base_url = 'required';
  else if (url && !/^https?:\/\/[^\s/]+/i.test(url)) e.base_url = 'invalid_url';
  return e;
}

export function toCreate(f: ModelConfigForm): ModelConfigCreate {
  const base = {
    label: f.label.trim(),
    provider: f.provider,
    model_id: f.model_id.trim(),
    base_url: f.base_url.trim() || null,
    default_for: [...f.default_for],
    is_default: f.is_default,
    enabled: f.enabled,
  };
  return f.credential_source === 'direct'
    ? { ...base, credential: { api_key: f.api_key.trim() } }
    : { ...base, secret_ref: `env:${normalizeEnvName(f.env_name)}` };
}

/** Only the fields that changed (the server audits exactly what was sent; a key only on explicit replace). */
export function toPatch(f: ModelConfigForm, before: ModelConfig): ModelConfigPatch {
  const next = toCreate(f);
  const out: ModelConfigPatch = {};
  if (next.label !== before.label) out.label = next.label;
  if (next.model_id !== before.model_id) out.model_id = next.model_id;
  if ((next.base_url ?? null) !== (before.base_url ?? null)) out.base_url = next.base_url;
  const wasSource = sourceOf(before);
  if (f.key_action === 'clear') {
    if ((before.credential_status ?? 'active') === 'active') out.credential = { clear: true };
  } else if (f.credential_source === 'env') {
    if (wasSource !== 'env' || next.secret_ref !== before.secret_ref) out.secret_ref = next.secret_ref;
  } else if (needsKey(f)) {
    out.credential = { api_key: f.api_key.trim() };
  }
  if ([...(before.default_for ?? [])].sort().join(',') !== [...f.default_for].sort().join(',')) out.default_for = [...f.default_for];
  if (next.is_default !== before.is_default) out.is_default = next.is_default;
  if (next.enabled !== before.enabled) out.enabled = next.enabled;
  return out;
}

/** The "fetch models" request, or null while the form has no usable key yet: the named variable, the typed key
 * (sent once for this call only, like on save) or, when editing, the saved key of `config`. */
export function toListRequest(f: ModelConfigForm, config?: ModelConfig): ModelListRequest | null {
  const base = { provider: f.provider, base_url: f.base_url.trim() || null };
  if (f.key_action === 'clear') return null;
  if (f.credential_source === 'env') {
    const env = normalizeEnvName(f.env_name);
    return ENV_RE.test(env) ? { ...base, credential: { env_name: env } } : null;
  }
  if (needsKey(f)) {
    const key = f.api_key.trim();
    return key.length >= 8 && key.length <= 1000 && !/\s/.test(key) && !looksMasked(key) ? { ...base, credential: { api_key: key } } : null;
  }
  return config && f.stored_direct ? { ...base, config_id: config.id, credential: { keep: true } } : null;
}

/** Credential versions that were replaced but still serve operations frozen with them (revocable). */
export function revocableOld(m: ModelConfig): number[] {
  return (m.credential_versions ?? []).filter((v) => v.status === 'replaced').map((v) => v.version);
}
