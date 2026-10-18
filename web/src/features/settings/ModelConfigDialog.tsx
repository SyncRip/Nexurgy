// Create / edit a model configuration (admins). The key comes from a server environment variable (the form
// names it) or is entered directly (W1): then it is a password field whose value lives only in this
// component's state until submit — never in localStorage / sessionStorage — and the server stores it
// encrypted and never returns it. Editing a stored key is explicit: keep, replace or clear. "Test" in the
// list makes a live call with exactly the saved configuration. "Fetch models" lists the models the address
// and key can use (a listing call, nothing saved) so the model id can be picked instead of typed.

import { KeyRound,RefreshCw,TriangleAlert } from 'lucide-react';
import { useId,useState } from 'react';
import { createModelConfig,listProviderModels,updateModelConfig } from '../../api/endpoints';
import { MODEL_PURPOSES,PROVIDERS,type CredentialSource,type ListedModel,type ModelConfig,type ModelListResult,type Provider } from '../../api/types';
import '../../components/ai/aiFailure.css';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';
import {
DEFAULT_ENV,
KEY_MASK,
PRESETS,
emptyForm,
formFromConfig,
needsKey,
presetForm,
toCreate,
toListRequest,
toPatch,
validateForm,
type FormErrors,
type KeyAction,
type ModelConfigForm,
} from './modelConfigForm';


export function ModelConfigDialog({
  config,
  isFirst,
  directKeysAvailable,
  onClose,
  onSaved,
}: {
  config?: ModelConfig;
  isFirst: boolean;
  /** From /system/capabilities: false = no master key on the server (direct keys cannot be saved); undefined = unknown. */
  directKeysAvailable?: boolean;
  onClose: () => void;
  onSaved: (m: ModelConfig) => void;
}) {
  const { t } = useI18n();
  const editing = !!config;
  const [form, setForm] = useState<ModelConfigForm>(() => (config ? formFromConfig(config) : emptyForm(isFirst)));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const modelsId = useId();
  const errors: FormErrors = validateForm(form);
  const shown = touched ? errors : {};
  const set = <K extends keyof ModelConfigForm>(k: K, v: ModelConfigForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  // Fetched models belong to the provider + address they were listed for; another address hides them.
  const [listed, setListed] = useState<{ for: string; models: ListedModel[] } | null>(null);
  const [listing, setListing] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const listFor = `${form.provider}|${form.base_url.trim()}`;
  const models = listed?.for === listFor ? listed.models : null;
  const listRequest = toListRequest(form, config);

  const errText = (k: keyof FormErrors, label: string) => {
    const e = shown[k];
    if (!e) return null;
    if (e === 'required') return t('validation.required', { field: label });
    if (e === 'masked') return t('cred.err.masked');
    if (e === 'invalid_key') return t('cred.err.invalidKey');
    return t(e === 'invalid_env' ? 'mc.err.env' : 'mc.err.url');
  };

  const submit = async () => {
    setTouched(true);
    if (Object.keys(errors).length) return;
    setBusy(true);
    setError(null);
    try {
      const saved = config ? await updateModelConfig(config.id, { ...toPatch(form, config), expected_config_version: config.config_version }) : await createModelConfig(toCreate(form));
      setForm((f) => ({ ...f, api_key: '' })); // the typed key leaves memory with the dialog
      onSaved(saved);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const listFailure = (r: ModelListResult) => {
    const key = `diag.code.${r.error_code ?? 'unknown'}`;
    return hasKey(key) ? t(key as MessageKey) : r.error_message || t('diag.code.unknown');
  };

  const fetchModels = async () => {
    if (!listRequest) return;
    const forKey = listFor;
    setListing(true);
    setListError(null);
    setError(null);
    try {
      const r = await listProviderModels(listRequest);
      if (r.ok) setListed({ for: forKey, models: r.models });
      else setListError(listFailure(r));
    } catch (e) {
      setError(e);
    } finally {
      setListing(false);
    }
  };

  const changeProvider = (p: Provider) =>
    setForm((f) => ({
      ...f,
      provider: p,
      // Keep a custom variable name; switch only the provider's usual one.
      env_name: Object.values(DEFAULT_ENV).includes(f.env_name) ? DEFAULT_ENV[p] : f.env_name,
    }));

  const changeSource = (s: CredentialSource) => setForm((f) => ({ ...f, credential_source: s, key_action: 'keep', api_key: '' }));
  const storedVersion = config?.credential_version ?? null;
  const keyInputShown = needsKey(form);

  return (
    <Dialog
      title={t(editing ? 'mc.editTitle' : 'mc.newTitle')}
      subtitle={editing ? <span className="mono">{config!.model_id}</span> : t('mc.newSubtitle')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      {!editing && (
        <div className="mc-presets" role="group" aria-label={t('mc.presets')}>
          <span className="faint small">{t('mc.presets')}</span>
          {PRESETS.map((p) => (
            <button key={p.id} type="button" className="btn sm" onClick={() => setForm(presetForm(p.id, isFirst))}>
              {t(`mc.preset.${p.id}`)}
            </button>
          ))}
        </div>
      )}
      <div className="form-grid">
        <Field label={t('settings.models.col.label')} error={errText('label', t('settings.models.col.label'))}>
          <input className="input" value={form.label} maxLength={120} onChange={(e) => set('label', e.target.value)} />
        </Field>
        <Field label={t('cred.protocol.label')} hint={editing ? t('mc.providerFixed') : t(`cred.protocol.${form.provider}`)}>
          <select className="select" value={form.provider} disabled={editing} onChange={(e) => changeProvider(e.target.value as Provider)}>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {t(`provider.${p}`)} — {t(`cred.protocol.${p}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('settings.models.col.model')} error={errText('model_id', t('settings.models.col.model'))}>
          <input className="input mono" list={modelsId} value={form.model_id} maxLength={120} onChange={(e) => set('model_id', e.target.value)} />
        </Field>
        <Field
          label={t('mc.baseUrl')}
          optional={form.provider !== 'openai_compatible'}
          hint={t(form.provider === 'openai_compatible' ? 'mc.baseUrlHintCompatible' : 'mc.baseUrlHint')}
          error={errText('base_url', t('mc.baseUrl'))}
        >
          <input className="input mono" value={form.base_url} maxLength={500} placeholder="https://…/v1" onChange={(e) => set('base_url', e.target.value)} />
        </Field>
      </div>
      <div className="mc-models">
        <button type="button" className="btn sm" disabled={!listRequest || !!errors.base_url || listing} onClick={() => void fetchModels()}>
          <RefreshCw aria-hidden /> {listing ? t('mc.models.loading') : t('mc.models.fetch')}
        </button>
        {models && models.length > 0 && (
          <select
            className="select mono"
            aria-label={t('mc.models.pick', { n: models.length })}
            value={models.some((m) => m.id === form.model_id) ? form.model_id : ''}
            onChange={(e) => e.target.value && set('model_id', e.target.value)}
          >
            <option value="">{t('mc.models.pick', { n: models.length })}</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name && m.name !== m.id ? `${m.id} — ${m.name}` : m.id}
              </option>
            ))}
          </select>
        )}
        <span className={listError ? 'field-error' : 'faint small'} role="status">
          {listError ??
            (models
              ? t(models.length ? 'mc.models.found' : 'mc.models.none', { n: models.length })
              : t(listRequest && !errors.base_url ? 'mc.models.hint' : 'mc.models.needKey'))}
        </span>
      </div>
      <p className="faint small">{t('cred.protocol.note')}</p>
      <datalist id={modelsId}>
        {(models ?? []).map((m) => (
          <option key={m.id} value={m.id} />
        ))}
      </datalist>

      <fieldset className="mc-cred">
        <legend>{t('cred.source.label')}</legend>
        <div className="seg" role="group" aria-label={t('cred.source.label')}>
          {(['env', 'direct'] as const).map((s) => (
            <button key={s} type="button" className="seg-btn" aria-pressed={form.credential_source === s} onClick={() => changeSource(s)}>
              {t(s === 'env' ? 'cred.source.env' : 'cred.source.direct')}
            </button>
          ))}
        </div>
        {form.credential_source === 'env' ? (
          <>
            <Field label={t('mc.keyVar')} hint={t('cred.source.envHint')} error={errText('env_name', t('mc.keyVar'))}>
              <input
                className="input mono"
                value={form.env_name}
                maxLength={84}
                autoComplete="off"
                spellCheck={false}
                placeholder="OPENAI_API_KEY"
                disabled={form.key_action === 'clear'}
                onChange={(e) => set('env_name', e.target.value)}
              />
            </Field>
            {editing && config?.credential_source !== 'direct' && config?.credential_status === 'active' && (
              <label className="check">
                <input type="checkbox" checked={form.key_action === 'clear'} onChange={(e) => set('key_action', e.target.checked ? 'clear' : 'keep')} />
                <span className="small">{t('cred.envClear')}</span>
              </label>
            )}
          </>
        ) : (
          <>
            {directKeysAvailable === false && (
              <div className="callout warn slim" role="note">
                <TriangleAlert aria-hidden />
                <span className="small">{t('cred.masterKeyMissing')}</span>
              </div>
            )}
            {form.stored_direct && (
              <div className="stack-sm">
                <div className="seg" role="group" aria-label={t('cred.keyAction.label')}>
                  {(['keep', 'replace', 'clear'] as KeyAction[]).map((a) => (
                    <button key={a} type="button" className="seg-btn" aria-pressed={form.key_action === a} onClick={() => setForm((f) => ({ ...f, key_action: a, api_key: '' }))}>
                      {t(`cred.keyAction.${a}`)}
                    </button>
                  ))}
                </div>
                <span className="faint small">
                  {form.key_action === 'keep'
                    ? t('cred.keyAction.keepHint', { version: storedVersion ?? '—' })
                    : t(form.key_action === 'replace' ? 'cred.keyAction.replaceHint' : 'cred.keyAction.clearHint')}
                </span>
                {form.key_action === 'keep' && <input className="input mono" type="password" value="" placeholder={KEY_MASK} disabled aria-label={t('cred.apiKey')} />}
              </div>
            )}
            {keyInputShown && (
              <Field label={t('cred.apiKey')} hint={t('cred.source.directHint')} error={errText('api_key', t('cred.apiKey'))}>
                <input
                  className="input mono"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  data-lpignore="true"
                  data-1p-ignore
                  maxLength={1000}
                  value={form.api_key}
                  placeholder={t('cred.apiKey.placeholder')}
                  onChange={(e) => set('api_key', e.target.value)}
                />
              </Field>
            )}
          </>
        )}
        {form.credential_source === 'env' && (
          <div className="callout slim">
            <KeyRound aria-hidden />
            <span className="small">{t('mc.keyCallout')}</span>
          </div>
        )}
      </fieldset>

      <fieldset className="mc-caps">
        <legend>{t('core.models.purposes')}</legend>
        {MODEL_PURPOSES.map((purpose) => (
          <label key={purpose} className="check">
            <input type="checkbox" checked={form.default_for.includes(purpose)} onChange={(e) => set('default_for', e.target.checked ? [...form.default_for, purpose] : form.default_for.filter((p) => p !== purpose))} />
            <span>{t(`core.purpose.${purpose}`)}</span>
          </label>
        ))}
        <p className="faint small">{t('core.models.detectOnSave')}</p>
      </fieldset>
      <div className="mc-flags">
        <label className="check">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set('enabled', e.target.checked)} />
          <span>{t('settings.models.enabled')}</span>
        </label>
        <label className="check">
          <input type="checkbox" checked={form.is_default} onChange={(e) => set('is_default', e.target.checked)} />
          <span>{t('mc.makeDefault')}</span>
        </label>
      </div>
      <ErrorNotice error={error} />
    </Dialog>
  );
}
