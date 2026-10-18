// Admin settings: live server capabilities and model configurations. Admins
// create, edit, enable, set the default and live-test configurations
// (/model-configs). A key comes from a server environment variable or is
// entered once in the dialog (W1): the server stores it encrypted and never
// returns it — the list shows its source, status and versions only. Ready,
// the connection probe and real use are shown separately. Configuration API
// errors remain visible and retryable.
// Capability check (agent execution layer P1): per-configuration capability check (tool calling, parallel calls, no strict,
// usage fields, streaming); ?diagnose=<config id> opens it (linked from the agent profile editor).

import { Ban,MoreHorizontal,Pencil,Plus,Power,RefreshCw,Star,Stethoscope } from 'lucide-react';
import { useEffect,useState } from 'react';
import { Link,useSearchParams } from 'react-router-dom';
import { listModelConfigs,updateModelConfig } from '../api/endpoints';
import { revokeModelCredential } from '../api/modelCredentials';
import { getModelConfig } from '../api/settings';
import type { ModelConfig } from '../api/types';
import { paths } from '../app/nav';
import { useUser } from '../auth/AuthProvider';
import { CapabilityList,useCapabilities } from '../components/shell/ServiceStatus';
import { useCrumbs } from '../components/shell/ShellContext';
import { Chip } from '../components/ui/Badges';
import { Menu } from '../components/ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../components/ui/States';
import { useToast } from '../components/ui/Toast';
import { ModelConfigDialog } from '../features/settings/ModelConfigDialog';
import { PersonalSettings } from '../features/settings/PersonalSettings';
import { ProbeDialog,ProbeState } from '../features/settings/ProbeDialog';
import { ProjectSettings } from '../features/settings/ProjectSettings';
import { revocableOld } from '../features/settings/modelConfigForm';
import { acceptProbeUpdate,probePending } from '../features/settings/probe';
import { useResource } from '../hooks/useResource';
import { useI18n } from '../i18n/I18nProvider';
import { hasKey } from '../i18n/core';
import type { MessageKey } from '../i18n/en';

function ModelConfigTable({
  rows,
  onChanged,
  onEdit,
  onDiagnose,
}: {
  rows: ModelConfig[];
  onChanged: (m: ModelConfig, reloadAll?: boolean) => void;
  onEdit: (m: ModelConfig) => void;
  onDiagnose: (m: ModelConfig) => void;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const purposeText = (p: string) => (hasKey(`cred.purpose.${p}`) ? t(`cred.purpose.${p}` as MessageKey) : p);
  const act = async (m: ModelConfig, kind: 'default' | 'toggle' | 'revoke' | 'revokeOld') => {
    if (kind === 'revoke' && !window.confirm(t('cred.revoke.confirm', { label: m.label }))) return;
    if (kind === 'revokeOld' && !window.confirm(t('cred.revoke.oldConfirm', { label: m.label, count: revocableOld(m).length }))) return;
    setBusy(`${m.id}:${kind}`);
    setError(null);
    try {
      if (kind === 'default') {
        // Setting a default clears it on the others: reload the list.
        onChanged(await updateModelConfig(m.id, { is_default: true, expected_config_version: m.config_version }), true);
      } else if (kind === 'revoke') {
        onChanged(await revokeModelCredential(m.id, m.credential_version ?? null));
        toast(t('cred.revoked', { label: m.label }));
      } else if (kind === 'revokeOld') {
        let last = m;
        for (const v of revocableOld(m)) last = await revokeModelCredential(m.id, v);
        onChanged(last);
        toast(t('cred.revoked', { label: m.label }));
      } else {
        onChanged(await updateModelConfig(m.id, { enabled: !m.enabled, expected_config_version: m.config_version }));
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  if (!rows.length) return <EmptyState compact title={t('settings.models.empty')} body={t('mc.emptyBody')} />;
  return (
    <div className="table-wrap">
      <table className="t">
        <thead>
          <tr>
            <th>{t('settings.models.col.label')}</th>
            <th>{t('settings.models.col.provider')}</th>
            <th>{t('settings.models.col.model')}</th>
            <th className="hide-md">{t('settings.models.col.capabilities')}</th>
            <th>{t('settings.models.col.state')}</th>
            <th className="hide-sm">{t('cred.col.key')}</th>
            <th className="hide-md">{t('settings.models.col.lastTest')}</th>
            <th className="act" />
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.id}>
              <td>
                <div className="cell-main">
                  <span>{m.label}</span>
                  {m.is_default && <Chip tone="accent">{t('settings.models.default')}</Chip>}
                </div>
              </td>
              <td>{m.provider === 'gemini' || m.provider === 'openai' || m.provider === 'openai_compatible' ? t(`provider.${m.provider}`) : m.provider}</td>
              <td className="mono small">{m.model_id}</td>
              <td className="hide-md">
                <span className="tag-list">
                  {(['text', 'vision', 'tools', 'structured'] as const)
                    .filter((c) => !m.probe?.stale && m.probe?.detected[c] === true)
                    .map((c) => (
                      <span key={c} className="tag">
                        {t(`cap.${c}`)}
                      </span>
                    ))}
                </span>
              </td>
              <td>
                {m.enabled ? <Chip tone={m.ready ? 'ok' : 'warn'}>{t('settings.models.enabled')}</Chip> : <Chip tone="pending">{t('settings.models.disabled')}</Chip>}
                {m.issues.length > 0 && <div className="faint small">{m.issues.map((issue, index) => {
                  const key = `diag.code.${m.issue_codes?.[index]}`;
                  return hasKey(key) ? t(key as MessageKey) : issue;
                }).join(' · ')}</div>}
              </td>
              <td className="hide-sm small">
                <div className="mc-meta">
                  <Chip tone="measure">{t(m.credential_source === 'direct' ? 'cred.source.short.direct' : 'cred.source.short.env')}</Chip>
                  {m.secret_ref && <span className="mono faint">{m.secret_ref}</span>}
                  {m.credential_status && m.credential_status !== 'active' ? (
                    <Chip tone="warn">{t(`cred.status.${m.credential_status}`)}</Chip>
                  ) : m.key_present ? (
                    <Chip tone="ok">{t('settings.models.keyPresent')}</Chip>
                  ) : (
                    <Chip tone="warn">{t('settings.models.keyMissing')}</Chip>
                  )}
                </div>
                {m.config_version != null && (
                  <div className="faint mono">
                    {m.credential_version != null
                      ? t('cred.versions', { config: m.config_version, key: m.credential_version })
                      : m.credential_status === 'active'
                        ? t('cred.versionsConfig', { config: m.config_version }) // an env reference recorded before its first use
                        : t('cred.versionsNoKey', { config: m.config_version })}
                  </div>
                )}
              </td>
              <td className="hide-md small">
                <ProbeState config={m} />
                <div className="tag-list">{(m.default_for ?? []).map((purpose) => <Chip key={purpose} tone={m.ready_for?.[purpose] ? 'ok' : 'warn'}>{t(`core.purpose.${purpose}`)}</Chip>)}</div>
                <div className="mc-uses" aria-label={t('cred.lastUse.title')}>
                  {Object.entries(m.last_use ?? {}).length === 0 ? (
                    <span className="faint">{t('cred.lastUse.title')}: {t('cred.lastUse.none')}</span>
                  ) : (
                    Object.entries(m.last_use ?? {}).map(([purpose, u]) => (
                      <span key={purpose} title={u.at ?? undefined}>
                        <Chip tone={u.status === 'ok' ? 'ok' : 'err'}>
                          {t(u.status === 'ok' ? 'cred.lastUse.ok' : 'cred.lastUse.failed', { purpose: purposeText(purpose) })}
                        </Chip>
                        {u.error_code && <code className="error-code">{u.error_code}</code>}
                      </span>
                    ))
                  )}
                </div>
              </td>
              <td className="act nowrap">
                <button type="button" className="btn sm" onClick={() => onDiagnose(m)} title={t('core.probe.details')}>
                  <Stethoscope /> <span className="hide-sm">{t('core.probe.details')}</span>
                </button>
                <Menu
                  label={t('mc.actions', { label: m.label })}
                  trigger={<MoreHorizontal />}
                  items={[
                    { id: 'edit', label: t('common.edit'), icon: <Pencil />, onSelect: () => onEdit(m) },
                    { id: 'default', label: t('mc.makeDefault'), icon: <Star />, disabled: m.is_default || !m.enabled || !!busy, onSelect: () => void act(m, 'default') },
                    { id: 'toggle', label: m.enabled ? t('mc.disable') : t('mc.enable'), icon: <Power />, disabled: !!busy, onSelect: () => void act(m, 'toggle') },
                    {
                      id: 'revoke',
                      label: t('cred.revoke.current'),
                      icon: <Ban />,
                      disabled: !!busy || m.credential_version == null || m.credential_status !== 'active',
                      onSelect: () => void act(m, 'revoke'),
                    },
                    ...(revocableOld(m).length
                      ? [{ id: 'revokeOld', label: t('cred.revoke.old'), icon: <Ban />, disabled: !!busy, onSelect: () => void act(m, 'revokeOld') }]
                      : []),
                  ]}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorNotice error={error} />
    </div>
  );
}

export default function Settings() {
  const { t } = useI18n();
  const user = useUser();
  const caps = useCapabilities();
  const models = useResource((signal) => listModelConfigs({ signal }), [], { enabled: user.is_admin });
  const toast = useToast();
  const [dialog, setDialog] = useState<{ config?: ModelConfig } | null>(null);
  const [params, setParams] = useSearchParams();
  const diagnoseId = params.get('diagnose');
  const diagnosing = diagnoseId ? models.data?.find((m) => m.id === diagnoseId) ?? null : null;
  const setDiagnose = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set('diagnose', id);
    else next.delete('diagnose');
    setParams(next, { replace: true });
  };
  const changed = (m: ModelConfig, reloadAll = false) => {
    if (reloadAll || m.is_default) models.reload();
    else models.setData((rows) => (rows ?? []).map((r) => (r.id === m.id ? m : r)));
  };
  useCrumbs([{ label: t('nav.settings'), to: paths.settings() }]);

  const section = params.get('section') ?? (params.has('diagnose') ? 'models' : 'personal');
  const sectionHref = (id: string) => { const next = new URLSearchParams(params); next.set('section', id); next.delete('diagnose'); return `${paths.settings()}?${next}`; };
  const [pollError, setPollError] = useState<unknown>(null);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => { const update = () => setVisible(!document.hidden); document.addEventListener('visibilitychange', update); return () => document.removeEventListener('visibilitychange', update); }, []);
  useEffect(() => {
    const pending = (models.data ?? []).filter((m) => probePending(m));
    if (!user.is_admin || !visible || !pending.length) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      void Promise.allSettled(pending.map(async (m) => {
        const next = await getModelConfig(m.id, { signal: ctrl.signal });
        if (!ctrl.signal.aborted) models.setData((rows) => (rows ?? []).map((r) => r.id === m.id ? acceptProbeUpdate(r, next, m.probe?.id ?? null) : r));
      })).then((results) => { if (!ctrl.signal.aborted) setPollError(results.find((r) => r.status === 'rejected')?.reason ?? null); });
    }, 2000);
    return () => { ctrl.abort(); window.clearTimeout(timer); };
  }, [models.data, models.setData, user.is_admin, visible]);

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-titles">

          <h1>{t('settings.title')}</h1>
          <p className="muted">{t('core.settings.subtitle')}</p>
        </div>
      </header>

      <nav className="tabs" aria-label={t('nav.settings')}>
        {(['personal', 'project', ...(user.is_admin ? ['models'] : [])] as const).map((id) => <Link className={`tab ${section === id ? 'active' : ''}`} aria-current={section === id ? 'page' : undefined} key={id} to={sectionHref(id)}>{t(id === 'personal' ? 'core.settings.personal' : id === 'project' ? 'core.settings.project' : 'core.settings.models')}</Link>)}
      </nav>
      <div className="settings-grid">
        {section !== 'project' && (section !== 'models' || !user.is_admin) && <PersonalSettings />}
        {section === 'project' && <div className="span-2"><ProjectSettings /></div>}
        {section === 'models' && user.is_admin && <>
        <section className="panel span-2">
          <div className="panel-h">
            <h2>{t('settings.models.title')}</h2>
            <span className="grow" />
            <button type="button" className="btn sm primary" onClick={() => setDialog({})} disabled={!models.data}>
              <Plus /> {t('mc.new')}
            </button>
          </div>
          <div className="panel-b stack">
            <p className="muted">{t('cred.settings.body')}</p>
            <p className="faint small">{t('cred.settings.legend')}</p>
            {models.error ? (
              <ErrorState error={models.error} onRetry={models.reload} />
            ) : models.initial ? (
              <SkeletonRows rows={2} cols={5} />
            ) : (
              <ModelConfigTable rows={models.data ?? []} onChanged={changed} onEdit={(m) => setDialog({ config: m })} onDiagnose={(m) => setDiagnose(m.id)} />
            )}
          </div>
        </section>

        <section className="panel span-2"><div className="panel-h"><h2>{t('settings.capabilities.title')}</h2><button className="btn ghost icon sm" onClick={caps.refresh} aria-label={t('status.refresh')}><RefreshCw /></button></div><div className="panel-b"><CapabilityList state={caps} /></div></section>
        <ErrorNotice error={pollError} onRetry={models.reload} />
        </>}

      </div>
      {diagnosing && <ProbeDialog key={diagnosing.id} config={diagnosing} onClose={() => setDiagnose(null)} onChanged={changed} />}
      {dialog && (
        <ModelConfigDialog
          config={dialog.config}
          isFirst={!models.data?.length}
          directKeysAvailable={caps.data?.ai.direct_keys?.available}
          onClose={() => setDialog(null)}
          onSaved={(m) => {
            setDialog(null);
            toast(t(dialog.config ? 'mc.saved' : 'mc.created', { label: m.label }));
            models.reload();
          }}
        />
      )}
    </div>
  );
}
