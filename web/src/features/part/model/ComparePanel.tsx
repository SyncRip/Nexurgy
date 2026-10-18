// Revision comparison: op-level changes and geometric deltas
// (GET /revisions/{id}/diff?against=, defaults to the parent revision).

import { GitCompare } from 'lucide-react';
import { useState } from 'react';
import { diffRevision } from '../../../api/endpoints';
import type { MetricDelta, ModelRevision, RevisionDiff } from '../../../api/types';
import { Chip } from '../../../components/ui/Badges';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { opTypeLabel } from './opText';

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return String(Math.round(v * 1000) / 1000);
  if (Array.isArray(v)) return `[${v.map(fmtVal).join(', ')}]`;
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function Delta({ d, unit, digits = 2 }: { d: MetricDelta | undefined; unit: string; digits?: number }) {
  const { fmt } = useI18n();
  if (!d) return <span className="faint">—</span>;
  const f = (x: number | null) => fmt.number(x, { maximumFractionDigits: digits });
  const tone = d.delta === null || d.delta === 0 ? '' : d.delta > 0 ? 'up' : 'down';
  return (
    <span className="mono">
      {f(d.before)} → {f(d.after)} {unit}
      {d.delta !== null && d.delta !== 0 && (
        <span className={`ws-delta ${tone}`}>
          {' '}
          ({d.delta > 0 ? '+' : ''}
          {f(d.delta)}
          {d.pct !== null ? `, ${d.pct > 0 ? '+' : ''}${fmt.number(d.pct, { maximumFractionDigits: 1 })}%` : ''})
        </span>
      )}
    </span>
  );
}

export function DiffView({ diff }: { diff: RevisionDiff }) {
  const { t } = useI18n();
  const m = diff.metrics ?? {};
  const bbox = m.bbox_size as { before: number[] | null; after: number[] | null; delta: number[] | null } | undefined;
  const holes = m.holes as { total_before: number; total_after: number; added: unknown[]; removed: unknown[]; changed: unknown[] } | undefined;
  return (
    <div className="stack-sm">
      {Object.keys(m).length > 0 && (
        <dl className="ws-kv">
          <div>
            <dt>{t('ws.metric.volume')}</dt>
            <dd>
              <Delta d={m.volume_mm3} unit="mm³" digits={1} />
            </dd>
          </div>
          <div>
            <dt>{t('ws.metric.area')}</dt>
            <dd>
              <Delta d={m.area_mm2} unit="mm²" digits={1} />
            </dd>
          </div>
          {bbox?.before && bbox?.after && (
            <div>
              <dt>{t('ws.metric.bbox')}</dt>
              <dd className="mono">
                {bbox.before.map(fmtVal).join('×')} → {bbox.after.map(fmtVal).join('×')}
                {bbox.delta && bbox.delta.some((x) => x !== 0) && <span className="ws-delta"> (Δ {bbox.delta.map((x) => `${x > 0 ? '+' : ''}${fmtVal(x)}`).join(', ')})</span>}
              </dd>
            </div>
          )}
          {holes && (
            <div>
              <dt>{t('ws.metric.holes')}</dt>
              <dd className="mono">
                {holes.total_before} → {holes.total_after}
              </dd>
            </div>
          )}
        </dl>
      )}
      {diff.ops.length === 0 ? (
        <p className="faint small">{t('ws.diff.noOpChanges')}</p>
      ) : (
        <ul className="ws-diff">
          {diff.ops.map((c) => (
            <li key={`${c.op_id}-${c.change}`} className={`ch-${c.change}`}>
              <div className="ws-diff-h">
                <Chip tone={c.change === 'added' ? 'ok' : c.change === 'removed' ? 'err' : 'measure'}>{t(`ws.diff.${c.change}`)}</Chip>
                <span className="mono strong">{c.op_id}</span>
                <span>{opTypeLabel(c.type, t)}</span>
                {c.label && <span className="faint">“{c.label}”</span>}
              </div>
              {c.fields.length > 0 && (
                <table className="ws-diff-fields">
                  <tbody>
                    {c.fields.map((f) => (
                      <tr key={f.path}>
                        <td className="mono faint">{f.path === '(order)' ? t('ws.diff.order') : f.path}</td>
                        <td className="mono">{fmtVal(f.before)}</td>
                        <td aria-hidden>→</td>
                        <td className="mono">{fmtVal(f.after)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ComparePanel({ revision, revisions }: { revision: ModelRevision | null; revisions: ModelRevision[] }) {
  const { t } = useI18n();
  const [against, setAgainst] = useState<string>('');
  const others = revisions.filter((r) => r.id !== revision?.id);
  const target = against || revision?.parent_id || '';
  const res = useResource((signal) => diffRevision(revision!.id, target || undefined, { signal }), [revision?.id, target], { enabled: !!revision && !!target });
  if (!revision) return <EmptyState compact title={t('ws.diff.noRevision')} icon={<GitCompare />} />;
  return (
    <div className="stack-sm">
      <div className="ws-f">
        <label htmlFor="ws-diff-against">{t('ws.diff.against')}</label>
        <select id="ws-diff-against" className="select sm" value={target} onChange={(e) => setAgainst(e.target.value)}>
          <option value="" disabled>
            {t('ws.diff.pick')}
          </option>
          {others.map((r) => (
            <option key={r.id} value={r.id}>
              {r.label}
              {r.id === revision.parent_id ? ` · ${t('ws.diff.parent')}` : ''}
            </option>
          ))}
        </select>
      </div>
      <p className="small muted">
        {t('ws.diff.direction', { from: revisions.find((r) => r.id === target)?.label ?? '—', to: revision.label })}
      </p>
      {!target ? (
        <p className="faint small">{t('ws.diff.noParent')}</p>
      ) : res.initial && res.loading ? (
        <SkeletonRows rows={3} cols={3} />
      ) : res.error ? (
        <ErrorNotice error={res.error} onRetry={res.reload} />
      ) : res.data ? (
        <DiffView diff={res.data} />
      ) : null}
    </div>
  );
}
