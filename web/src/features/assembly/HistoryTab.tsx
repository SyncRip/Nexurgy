// History tab: every immutable revision (source, policy, check state), compare with the previous one and what
// is bound to each revision (stale once a newer revision exists). Old baselines stay openable.

import { useState } from 'react';
import { compareRevisions, getDownstream, listAssemblyRevisions, type RevisionBrief } from '../../api/assemblies';
import { CheckStatusChip, PolicyChip, StaleChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { unverifiedBesideCheck } from '../turbo/model';
import { ErrorState, SkeletonRows, Spinner } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { safeCheckStatus } from './model';

export function HistoryTab({ assemblyId, current, onOpen }: { assemblyId: string; current: string; onOpen: (rid: string) => void }) {
  const res = useResource((signal) => listAssemblyRevisions(assemblyId, { signal }), [assemblyId, current]);
  if (res.error && !res.data) return <ErrorState error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={4} cols={4} />;
  const latest = res.data[0]?.id;
  return (
    <ul className="stack asm-history">
      {res.data.map((r) => (
        <RevisionItem key={r.id} rev={r} current={r.id === current} latest={r.id === latest} onOpen={onOpen} />
      ))}
    </ul>
  );
}

function RevisionItem({ rev, current, latest, onOpen }: { rev: RevisionBrief; current: boolean; latest: boolean; onOpen: (rid: string) => void }) {
  const { t, fmt } = useI18n();
  const [open, setOpen] = useState(false);
  const cmp = useResource((signal) => compareRevisions(rev.id, undefined, { signal }), [rev.id], { enabled: open && !!rev.parent_id });
  const down = useResource((signal) => getDownstream(rev.id, { signal }), [rev.id], { enabled: open });
  const ops = rev.change?.ops ?? [];
  return (
    <li className={`panel ${current ? 'asm-hist-current' : ''}`}>
      <div className="panel-h row">
        <strong className="mono">{rev.label}</strong>
        <span className="faint small">{t(`asm.history.source.${rev.source}`)}</span>
        <PolicyChip policy={rev.execution_policy} />
        <CheckStatusChip status={safeCheckStatus(rev.check_status)} />
        {unverifiedBesideCheck(rev.verification, rev.check_status) && <UnverifiedChip />}
        {!latest && <StaleChip />}
        <span className="faint small">{fmt.dateTime(rev.created_at)}</span>
        <span className="grow" />
        {!current && (
          <button type="button" className="btn sm" onClick={() => onOpen(rev.id)}>
            {t('asm.history.open')}
          </button>
        )}
        <button type="button" className="btn sm" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? t('asm.history.hideCompare') : rev.parent_id ? t('asm.history.compare') : t('asm.history.downstream')}
        </button>
      </div>
      <div className="panel-b small stack-sm">
        {rev.note && <span>{rev.note}</span>}
        {ops.length > 0 && (
          <span className="faint">
            {t('asm.history.ops', { n: ops.length })}: <span className="mono">{ops.map((o) => String(o.op)).join(', ')}</span>
          </span>
        )}
        <span className="faint">
          {t('asm.list.leaves', { n: rev.stats.leaf_instances })} · {t('asm.list.defs', { n: rev.stats.part_definitions })}
        </span>
        {open && rev.parent_id && (cmp.data ? <CompareBody cmp={cmp.data} /> : cmp.error ? <ErrorState error={cmp.error} /> : <Spinner />)}
        {open && down.data && (
          <div>
            <strong>{t('asm.history.downstream')}</strong>
            {down.data.items.length === 0 ? (
              <p className="faint">{t('asm.impact.none')}</p>
            ) : (
              <ul>
                {down.data.items.map((d, i) => (
                  <li key={i}>
                    {d.kind}
                    {d.asset_version?.asset_name ? ` · ${d.asset_version.asset_name} ${d.asset_version.label ?? ''}` : ''}
                    {d.assembly ? ` · ${d.assembly.number} A${d.rev_no}` : ''}
                    {d.stale && <StaleChip />}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function CompareBody({ cmp }: { cmp: Awaited<ReturnType<typeof compareRevisions>> }) {
  const { t } = useI18n();
  if (!cmp.instances.length && !cmp.mates.length && !cmp.bom.length) return <p className="faint">{t('asm.history.noChanges')}</p>;
  return (
    <ul className="asm-findings">
      {cmp.instances.map((c) => (
        <li key={c.instance_id}>
          <span className="mono">{c.instance_id}</span> {c.name} · {t(`asm.history.${c.change}`)}
          {c.fields ? <span className="faint"> ({c.fields.map((f) => f.field).join(', ')})</span> : null}
        </li>
      ))}
      {cmp.mates.map((m) => (
        <li key={m.mate_id}>
          <span className="mono">{m.mate_id}</span> {m.kind} · {t(`asm.history.${m.change as 'added'}`)}
        </li>
      ))}
      {cmp.bom.map((b) => (
        <li key={b.def_key}>{t('asm.history.qty', { name: b.name, from: b.from, to: b.to })}</li>
      ))}
    </ul>
  );
}
