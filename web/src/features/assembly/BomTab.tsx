// BOM tab: hierarchical (per parent definition, repeated sub-assemblies listed once) and flattened (leaf totals)
// views of the exact revision. Unknown material / procurement stays "unknown"; representation is shown when not exact.

import { useState } from 'react';
import { getBom, type BomLine } from '../../api/assemblies';
import { RepresentationChip } from '../../components/engineering/EngineeringChips';
import { ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export function BomTab({ revisionId, frozenSha }: { revisionId: string; frozenSha: string | null }) {
  const { t } = useI18n();
  const [view, setView] = useState<'hier' | 'flat'>('hier');
  const res = useResource((signal) => getBom(revisionId, { signal }), [revisionId]);
  if (res.error && !res.data) return <ErrorState error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={6} cols={6} />;
  const bom = res.data;
  const lines: BomLine[] = view === 'hier' ? bom.hierarchical : bom.flattened;
  const c = bom.counts;
  return (
    <div className="stack">
      <div className="row">
        <div className="seg" role="group" aria-label={t('asm.bom.view')}>
          <button type="button" className="seg-btn" aria-pressed={view === 'hier'} onClick={() => setView('hier')}>
            {t('asm.bom.hier')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={view === 'flat'} onClick={() => setView('flat')}>
            {t('asm.bom.flat')}
          </button>
        </div>
        <span className="faint small">
          {t('asm.bom.counts', { leaves: c.leaf_instances ?? 0, defs: c.part_definitions ?? 0, asms: c.assembly_definitions ?? 0, subs: c.subassembly_instances ?? 0 })}
        </span>
      </div>
      {frozenSha && frozenSha !== bom.bom_sha256 && <p className="callout slim warn">{t('asm.bom.changed')}</p>}
      <div className="table-wrap">
        <table className="t dense asm-bom">
          <thead>
            <tr>
              <th>{t('asm.bom.item')}</th>
              <th>{t('asm.bom.name')}</th>
              <th>{t('asm.bom.number')}</th>
              <th>{t('asm.bom.rev')}</th>
              {view === 'hier' && <th className="r">{t('asm.bom.qty')}</th>}
              <th className="r">{t('asm.bom.total')}</th>
              <th>{t('asm.bom.material')}</th>
              <th>{t('asm.bom.procurement')}</th>
              <th>{t('asm.bom.representation')}</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l) => (
              <tr key={`${l.item}-${l.def_key}`}>
                <td className="mono" style={view === 'hier' ? { paddingLeft: 8 + (l.level - 1) * 14 } : undefined}>
                  {l.item}
                </td>
                <td>
                  {l.name}
                  {l.def_kind === 'assembly' && l.expanded_at && <span className="faint small"> · {t('asm.bom.expandedAt', { item: l.expanded_at })}</span>}
                </td>
                <td className="mono small">{l.number ?? '—'}</td>
                <td className="mono small">{l.rev_no != null ? `${l.def_kind === 'part' ? 'R' : 'A'}${l.rev_no}` : '—'}</td>
                {view === 'hier' && <td className="r mono">{l.quantity}</td>}
                <td className="r mono">{l.total_quantity}</td>
                <td className="small">{l.material ?? <span className="faint">{t('asm.bom.unknown')}</span>}</td>
                <td className="small">{t(`asm.bom.proc.${['make', 'buy', 'standard'].includes(l.procurement) ? l.procurement : 'unknown'}` as 'asm.bom.proc.make')}</td>
                <td>{l.representation !== 'exact' ? <RepresentationChip level={l.representation} /> : <span className="faint small">exact</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {bom.suppressed.length > 0 && (
        <section className="panel">
          <div className="panel-h">
            <strong>{t('asm.bom.suppressed')}</strong>
          </div>
          <ul className="panel-b small">
            {bom.suppressed.map((s) => (
              <li key={s.path.join('/')}>
                <span className="mono">{s.path.join(' / ')}</span> {s.name}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
