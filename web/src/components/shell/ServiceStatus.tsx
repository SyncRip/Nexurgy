// Service status pill from GET /system/capabilities. It reports only what the
// server says: a model key being present is "configured", never "connected".

import { Cpu, Database, HardDrive, RefreshCw, Sparkles, Workflow } from 'lucide-react';
import type { ReactNode } from 'react';
import { API_ORIGIN } from '../../api/client';
import { useCapabilities, type CapabilityState } from '../../api/capabilities';
export { useCapabilities } from '../../api/capabilities';
export type { CapabilityState } from '../../api/capabilities';
import type { Capabilities } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { ErrorText } from '../ui/States';
import { Popover } from '../ui/Popover';

type Tone = 'ok' | 'warn' | 'err' | 'pending';

export function capabilityTones(c: Capabilities | null, failed: boolean) {
  if (failed || !c) return { db: 'err' as Tone, kernel: 'pending' as Tone, ai: 'pending' as Tone, overall: (failed ? 'err' : 'pending') as Tone };
  const db: Tone = c.database.ok ? 'ok' : 'err';
  const kernel: Tone = c.kernel.ok ? 'ok' : 'warn';
  const ai: Tone = c.ai.any_ready ? 'ok' : 'pending';
  const overall: Tone = db === 'err' ? 'err' : kernel === 'ok' && ai === 'ok' ? 'ok' : 'warn';
  return { db, kernel, ai, overall };
}

function Row({ icon, label, tone, value, note }: { icon: ReactNode; label: string; tone: Tone; value: ReactNode; note?: ReactNode }) {
  return (
    <div className={`svc-row tone-${tone}`}>
      <span className="svc-ico">{icon}</span>
      <div className="svc-txt">
        <span className="svc-label">{label}</span>
        <span className="svc-value">
          <span className={`led ${tone}`} aria-hidden />
          {value}
        </span>
        {note && <span className="svc-note">{note}</span>}
      </div>
    </div>
  );
}

/** The rows, reused by the Settings page. */
export function CapabilityList({ state }: { state: CapabilityState }) {
  const { t, fmt } = useI18n();
  const { data, error } = state;
  const tones = capabilityTones(data, !!error);
  if (error) {
    return (
      <div className="svc-list">
        <Row icon={<Database />} label={t('status.apiDown')} tone="err" value={<ErrorText error={error} />} note={t('status.apiBase', { url: API_ORIGIN })} />
      </div>
    );
  }
  if (!data) return <div className="svc-list faint">{t('status.checking')}</div>;
  const providers = Object.entries(data.ai.providers)
    .filter(([, p]) => p.with_key > 0)
    .map(([k]) => (k === 'gemini' || k === 'openai' || k === 'openai_compatible' ? t(`provider.${k}`) : k))
    .join(', ');
  return (
    <div className="svc-list">
      <Row icon={<Database />} label={t('status.database')} tone={tones.db} value={data.database.ok ? t('status.db.ok') : t('status.db.down')} />
      <Row
        icon={<Cpu />}
        label={t('status.kernel')}
        tone={tones.kernel}
        value={data.kernel.ok ? t('status.kernel.ok', { version: data.kernel.version }) : t('status.kernel.missing')}
        note={!data.kernel.ok ? <code className="error-code">{data.kernel.error}</code> : undefined}
      />
      <Row
        icon={<Sparkles />}
        label={t('status.ai')}
        tone={tones.ai}
        value={data.ai.any_ready ? t('status.ai.configured', { providers }) : t('status.ai.notConfigured')}
        note={t('status.ai.note')}
      />
      <Row
        icon={<HardDrive />}
        label={t('status.storage')}
        // Configuration only — the server does not health-check storage here.
        tone="pending"
        value={data.storage.backend === 'gcs' ? t('status.storage.gcs') : data.storage.backend === 'local' ? t('status.storage.local') : data.storage.backend}
      />
      <Row
        icon={<Workflow />}
        label={t('status.worker')}
        tone="pending"
        value={data.worker.inline ? t('status.worker.inline', { threads: fmt.number(data.worker.threads) }) : t('status.worker.external')}
      />
    </div>
  );
}

export function ServiceStatus() {
  const { t, fmt } = useI18n();
  const state = useCapabilities();
  const tones = capabilityTones(state.data, !!state.error);
  const summary = state.loading && !state.data && !state.error
    ? t('status.checking')
    : state.error
      ? t('status.apiDown')
      : tones.overall === 'ok'
        ? t('status.summary.ok')
        : tones.overall === 'err'
          ? t('status.summary.down')
          : t('status.summary.partial');

  return (
    <Popover
      label={`${t('status.title')}: ${summary}`}
      triggerClassName={`svc-pill tone-${tones.overall}`}
      trigger={
        <>
          <span className="svc-leds" aria-hidden>
            <span className={`led ${tones.kernel}`} title={t('status.kernel')} />
            <span className={`led ${tones.db}`} title={t('status.database')} />
            <span className={`led ${tones.ai}`} title={t('status.ai')} />
          </span>
          {/* R1.2: the pill names the three services (each in its own state colour); the summary stays the accessible label. */}
          <span className="svc-pill-label svc-trio" aria-hidden>
            <span className={`tone-${tones.kernel}`}>{t('status.short.kernel')}</span> · <span className={`tone-${tones.db}`}>{t('status.short.db')}</span> ·{' '}
            <span className={`tone-${tones.ai}`}>{t('status.short.ai')}</span>
          </span>
        </>
      }
    >
      <div className="svc-pop">
        <div className="svc-pop-h">
          <b>{t('status.title')}</b>
          <span className="grow" />
          <button type="button" className="btn ghost icon sm" onClick={state.refresh} aria-label={t('status.refresh')} disabled={state.loading}>
            <RefreshCw className={state.loading ? 'spin-icon' : ''} />
          </button>
        </div>
        <CapabilityList state={state} />
        <div className="svc-foot">
          <span>{t('status.honest')}</span>
          <span className="mono faint">
            {t('status.apiBase', { url: API_ORIGIN })}
            {state.checkedAt && ` · ${t('status.checkedAt', { time: fmt.time(state.checkedAt) })}`}
          </span>
        </div>
      </div>
    </Popover>
  );
}
