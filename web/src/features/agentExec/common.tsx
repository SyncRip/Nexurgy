// Shared pieces of the agent execution pages: status / runtime / simulated chips (icon + text), budget meters,
// the stop button (with confirmation) and exact-version links of produced outputs.

import './agentExec.css';
import { Ban, Box, CheckCircle2, CircleDashed, CircleStop, CircleX, Clock, Cpu, ExternalLink, FlaskConical, Gauge, Hand, LoaderCircle, MessageCircleQuestion, PauseCircle, Repeat, RotateCcw } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { stopAgentExecution } from '../../api/agentExecutions';
import { getAssetVersion, type AssetVersion } from '../../api/assets';
import { paths } from '../../app/nav';
import { assetWorkspacePath } from '../../app/versionLinks';
import { Chip, type Tone } from '../../components/ui/Badges';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { dynT } from '../collaboration/common';
import { useMoney } from '../part/common';
import { formatDuration } from '../workflow/model';
import { budgetText, type BudgetRow, type ExecDisplay } from './model';

// ─── chips ─────────────────────────────────────────────────────────────────

const STATUS_META: Record<string, { tone: Tone; icon: ReactNode }> = {
  running: { tone: 'measure', icon: <LoaderCircle aria-hidden /> },
  queued: { tone: 'pending', icon: <Clock aria-hidden /> },
  paused: { tone: 'pending', icon: <PauseCircle aria-hidden /> },
  stopping: { tone: 'warn', icon: <CircleStop aria-hidden /> },
  question: { tone: 'warn', icon: <MessageCircleQuestion aria-hidden /> },
  budget: { tone: 'err', icon: <Gauge aria-hidden /> },
  stopped: { tone: 'pending', icon: <CircleStop aria-hidden /> },
  delivered: { tone: 'ok', icon: <CheckCircle2 aria-hidden /> },
  blocked: { tone: 'err', icon: <Hand aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
  superseded: { tone: 'stale', icon: <RotateCcw aria-hidden /> },
  cancelled: { tone: 'pending', icon: <Ban aria-hidden /> },
};

export const execStatusText = (t: TFunction, s: string) => dynT(t, `ax.status.${s}`, s);

export function ExecStatusChip({ display, small }: { display: ExecDisplay; small?: boolean }) {
  const { t } = useI18n();
  const m = STATUS_META[display] ?? { tone: 'pending' as Tone, icon: <CircleDashed aria-hidden /> };
  return (
    <Chip tone={m.tone} icon={m.icon} className={small ? 'cl-chip-sm' : ''}>
      {execStatusText(t, display)}
    </Chip>
  );
}

export const runtimeText = (t: TFunction, rt: string | null | undefined) => (rt ? dynT(t, `ax.rt.${rt}`, rt) : '—');

export function RuntimeIcon({ runtime }: { runtime: string | null | undefined }) {
  if (runtime === 'tool_agent') return <Cpu aria-hidden />;
  if (runtime === 'workspace_agent') return <Box aria-hidden />;
  return <Repeat aria-hidden />;
}

export function RuntimeChip({ runtime, small }: { runtime: string | null | undefined; small?: boolean }) {
  const { t } = useI18n();
  const tone: Tone = runtime === 'tool_agent' ? 'measure' : runtime === 'workspace_agent' ? 'stale' : 'pending';
  return (
    <Chip tone={tone} icon={<RuntimeIcon runtime={runtime} />} className={small ? 'cl-chip-sm' : ''}>
      {runtimeText(t, runtime ?? 'native')}
    </Chip>
  );
}

/** Simulated executions carry this everywhere (handoff §6 rule 7). */
export function SimTag({ small }: { small?: boolean }) {
  const { t } = useI18n();
  return (
    <Chip tone="warn" icon={<FlaskConical aria-hidden />} title={t('ax.sim.hint')} className={small ? 'cl-chip-sm' : ''}>
      {t('ax.sim')}
    </Chip>
  );
}

// ─── budget ────────────────────────────────────────────────────────────────

/** Budget rows as meters: used / limit, a bar when there is a limit, "unknown" for an unpriced cost. */
export function BudgetMeters({ rows, only }: { rows: BudgetRow[]; only?: BudgetRow['key'][] }) {
  const { t, fmt } = useI18n();
  const money = useMoney();
  const f = { count: (n: number) => fmt.number(n), money: (fen: number) => money(fen), duration: formatDuration, unknown: t('ax.budget.unknown') };
  const shown = only ? rows.filter((r) => only.includes(r.key)) : rows;
  return (
    <div className="ax-meters">
      {shown.map((r) => {
        const txt = budgetText(r, f);
        const label = t(`ax.budget.${r.key}`);
        return (
          <div key={r.key} className="ax-meter">
            <div className="ax-meter-h">
              <span>{label}</span>
              <span className="mono">
                {r.used === null ? (
                  <span className="faint" title={t('ax.budget.unknownHint')}>
                    {txt.used}
                  </span>
                ) : (
                  txt.used
                )}
                {txt.max !== null && <> / {txt.max}</>}
              </span>
            </div>
            {txt.max === null ? (
              <div className="ax-meter-note">{t('ax.budget.noLimit')}</div>
            ) : (
              <div className="ax-meter-bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={r.pct ?? undefined} aria-valuetext={r.used === null ? t('ax.budget.unknown') : undefined}>
                <i className={r.pct === null ? 'unknown' : r.pct >= 90 ? 'hot' : r.pct >= 70 ? 'warm' : ''} style={{ width: `${r.pct ?? 0}%` }} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── stop ──────────────────────────────────────────────────────────────────

export function StopButton({ executionId, onStopped, size = 'sm' }: { executionId: string; onStopped?: () => void; size?: 'sm' | 'md' }) {
  const { t } = useI18n();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      await stopAgentExecution(executionId);
      toast(t('ax.act.stopRequested'));
      setOpen(false);
      onStopped?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" className={`btn danger ${size === 'sm' ? 'sm' : ''}`} onClick={() => setOpen(true)}>
        <CircleStop /> {t('ax.act.stop')}
      </button>
      {open && (
        <ConfirmDialog
          title={t('ax.act.stopTitle')}
          body={t('ax.act.stopBody')}
          confirmLabel={t('ax.act.stop')}
          danger
          busy={busy}
          error={error ? <ErrorNotice error={error} /> : null}
          onConfirm={() => void stop()}
          onClose={() => {
            setOpen(false);
            setError(null);
          }}
        />
      )}
    </>
  );
}

// ─── exact-version links ───────────────────────────────────────────────────

const versionCache = new Map<string, Promise<AssetVersion>>();
function loadVersion(id: string, signal: AbortSignal): Promise<AssetVersion> {
  let p = versionCache.get(id);
  if (!p) {
    p = getAssetVersion(id);
    p.catch(() => versionCache.delete(id));
    versionCache.set(id, p);
  }
  return signal.aborted ? Promise.reject(new DOMException('aborted', 'AbortError')) : p;
}

/** Where an output version opens: its part workspace tab at that version (S6), else the asset page at that version. */
export function versionPath(v: AssetVersion): string {
  return assetWorkspacePath(v.asset_type, v.target) ?? paths.assetVersion(v.asset_id, v.id);
}

/** A produced / output version, linked to exactly that version once it resolves. */
export function VersionLink({ versionId, label }: { versionId: string; label: string }) {
  const { t } = useI18n();
  const v = useResource((signal) => loadVersion(versionId, signal), [versionId]);
  const text = label || versionId.slice(0, 8);
  if (v.data)
    return (
      <Link to={versionPath(v.data)} className="ax-vlink" title={t('ax.out.open', { label: `${v.data.asset_name} ${v.data.label}` })}>
        <span className="ellip">{v.data.asset_name ? `${v.data.asset_name} · ` : ''}</span>
        <b className="mono">{v.data.label || text}</b>
        <ExternalLink aria-hidden />
      </Link>
    );
  return (
    <span className="ax-vlink is-plain" title={v.error ? t('ax.out.missing') : undefined}>
      <b className="mono">{text}</b>
    </span>
  );
}
