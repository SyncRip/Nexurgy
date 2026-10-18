import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { probeModelConfig } from '../../api/settings';
import { MODEL_PURPOSES,type ModelConfig } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { probePending } from './probe';

export function ProbeState({ config }: { config: ModelConfig }) {
  const { t } = useI18n();
  const p = config.probe;
  const status = p && (p.status === 'queued' || p.status === 'running') && !probePending(config) ? 'interrupted' : p?.status ?? 'not_tested';
  return <span className="tag-list"><Chip tone={p?.stale ? 'stale' : status === 'passed' ? 'ok' : status === 'failed' ? 'err' : 'pending'}>{t(`core.probe.${status}`)}</Chip>{p?.stale && <Chip tone="stale">{t('cred.test.stale')}</Chip>}</span>;
}

export function ProbeDialog({ config, onClose, onChanged }: { config: ModelConfig; onClose: () => void; onChanged: (config: ModelConfig) => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    if (config.config_version == null) return;
    setBusy(true); setError(null);
    try { onChanged(await probeModelConfig(config.id, config.config_version)); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  return <Dialog title={t('core.probe.title', { label: config.label })} onClose={onClose} footer={<><button className="btn" onClick={onClose}>{t('common.close')}</button><button className="btn primary" disabled={busy || probePending(config) || config.config_version == null} onClick={() => void run()}><RefreshCw className={busy || probePending(config) ? 'spin-icon' : ''} />{t('core.probe.action')}</button></>}>
    <div className="stack">
      <p>{t('core.probe.saved')} · <span className="mono">v{config.config_version ?? '—'}</span></p>
      <ProbeState config={config} />
      <div className="tag-list">{MODEL_PURPOSES.map((purpose) => <Chip key={purpose} tone={config.ready_for?.[purpose] ? 'ok' : 'warn'}>{t(`core.purpose.${purpose}`)} · {t(config.ready_for?.[purpose] ? 'core.probe.available' : 'core.probe.unavailable')}</Chip>)}</div>
      <dl className="kv">{(['text', 'structured', 'vision', 'tools'] as const).map((cap) => <div key={cap}><dt>{t(`cap.${cap}`)}</dt><dd>{t(config.probe?.detected[cap] == null ? 'core.probe.unknown' : config.probe.detected[cap] ? 'core.probe.available' : 'core.probe.unsupported')}</dd></div>)}</dl>
      {Object.entries(config.probe?.checks ?? {}).map(([name, check]) => <div className="row" key={name}><span className="mono">{name}</span><Chip tone={check.status === 'passed' ? 'ok' : check.status === 'failed' ? 'err' : 'pending'}>{t(`core.probe.${check.status === 'passed' ? 'passed' : check.status}`)}</Chip>{check.code && <code>{check.code}</code>}</div>)}
      {config.probe?.error && <p className="callout err"><code>{config.probe.error.code}</code> · {config.probe.error.message}</p>}
      <ErrorNotice error={error} />
    </div>
  </Dialog>;
}
