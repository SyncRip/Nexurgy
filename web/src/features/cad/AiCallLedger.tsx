// Model-call ledger of a run (GET /runs/{id}/ai-calls): purpose, model,
// status, tokens and latency per call, failed calls included. Loaded when the
// disclosure is opened. Missing usage stays "unknown" (never 0); simulated
// calls are labelled and not counted.

import { Cpu } from 'lucide-react';
import { useState } from 'react';
import { listRunAiCalls } from '../../api/endpoints';
import type { AiCallRecord } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';
import { callTokens, ledgerTotals } from './status';
import { purposeLabel, unitLabel } from '../progress/labels';
import './cad.css';
import '../progress/progress.css';

export function AiCallLedger({ runId, refreshKey }: { runId: string; refreshKey?: string | number }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <details className="panel cad-ledger" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="panel-h">
        <Cpu aria-hidden className="measure-ico" />
        <span>{t('cad.run.ledger')}</span>
        <span className="faint small">{t('cad.run.ledgerSub')}</span>
      </summary>
      <div className="panel-b">{open && <LedgerBody runId={runId} refreshKey={refreshKey} />}</div>
    </details>
  );
}

function LedgerBody({ runId, refreshKey }: { runId: string; refreshKey?: string | number }) {
  const res = useResource((signal) => listRunAiCalls(runId, { signal }), [runId, refreshKey]);
  if (res.error && !res.data) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={2} cols={5} />;
  return <>{!!res.error && <ErrorNotice error={res.error} onRetry={res.reload} />}<CallGroups calls={res.data} /></>;
}

export function groupCalls(calls: AiCallRecord[]): { key: string; node: string | null; unit: string | null; calls: AiCallRecord[] }[] {
  const groups = new Map<string, ReturnType<typeof groupCalls>[number]>();
  for (const call of calls) {
    const key = JSON.stringify([call.node_id, call.unit ?? null]);
    const group = groups.get(key) ?? { key, node: call.node_id, unit: call.unit ?? null, calls: [] };
    group.calls.push(call);
    groups.set(key, group);
  }
  return [...groups.values()];
}

export function CallGroups({ calls }: { calls: AiCallRecord[] }) {
  const { t, fmt } = useI18n();
  if (!calls.length) return <p className="faint small">{t('cad.run.ledgerEmpty')}</p>;
  const tot = ledgerTotals(calls);
  const unknown = t('cad.run.unknown');
  const num = (v: number | null | undefined) => (typeof v === 'number' ? fmt.number(v) : unknown);
  return (
    <div className="stack-sm">
      <p className="small">
        {t('cad.run.totals', { calls: tot.calls, failed: tot.failed, tin: num(tot.tokensIn), tout: num(tot.tokensOut) })}
        {tot.incomplete && (tot.tokensIn !== null || tot.tokensOut !== null) ? t('cad.run.totalsIncomplete') : ''}
      </p>
      <div className="table-wrap" tabIndex={0} role="region" aria-label={t('cad.run.ledger')}>
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('cad.run.colNode')}</th>
              <th>{t('cad.run.colPurpose')}</th>
              <th>{t('cad.run.colModel')}</th>
              <th>{t('cad.run.colStatus')}</th>
              <th className="r">{t('cad.run.colTokens')}</th>
              <th className="r">{t('cad.run.colLatency')}</th>
              <th>{t('cad.run.colRequest')}</th>
            </tr>
          </thead>
          {groupCalls(calls).map((group) => <tbody key={group.key}>
            <tr className="prog-ledger-group"><th colSpan={7} scope="rowgroup">
              {group.node ?? unknown} · {group.unit ? unitLabel(t, group.unit) : t('prog.call.ungrouped')}
              {group.unit && <span className="faint mono"> · {group.unit}</span>}
              <span className="faint"> · {t('prog.unit.calls', { n: group.calls.length })}</span>
            </th></tr>
            {group.calls.map((c) => (
              <tr key={c.id}>
                <td className="mono small">{c.node_id ?? '—'}</td>
                <td className="small" title={c.unit ?? undefined}>
                  {purposeLabel(t, c.purpose)}
                  {typeof c.attempt === 'number' ? <span className="faint"> · {t('cad.run.attempt', { n: c.attempt })}</span> : null}
                </td>
                <td className="small">
                  <span className="mono">{[c.provider, c.model_id].filter(Boolean).join(' · ') || unknown}</span>
                  {c.simulated && (
                    <Chip tone="warn" className="sm">
                      {t('cad.run.simulated')}
                    </Chip>
                  )}
                </td>
                <td className="small">
                  <Chip tone={c.status === 'ok' || c.status === 'succeeded' ? 'ok' : 'err'} className="sm">
                    {c.status === 'ok' || c.status === 'succeeded' ? t('cad.run.ok') : t('cad.run.error')}
                  </Chip>
                  {c.error_code ? <code className="error-code"> {c.error_code}</code> : null}
                  {c.failure_class ? <span className="faint"> · {hasKey(`cad.fclass.${c.failure_class}`) ? t(`cad.fclass.${c.failure_class}` as MessageKey) : c.failure_class}</span> : null}
                </td>
                <td className="r mono small">
                  {c.simulated ? t('prog.call.noUsage') : `${num(callTokens(c).tokensIn)} / ${num(callTokens(c).tokensOut)}`}
                </td>
                <td className="r mono small">{typeof c.latency_ms === 'number' ? `${fmt.number(c.latency_ms)} ms` : unknown}</td>
                <td className="mono small faint prog-ledger-request">{c.provider_request_id ?? unknown}</td>
              </tr>
            ))}
          </tbody>)}
        </table>
      </div>
    </div>
  );
}
