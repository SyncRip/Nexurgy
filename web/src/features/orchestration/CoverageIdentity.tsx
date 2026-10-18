// Generation reliability K (design .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.5): the identity classes of
// the spec's candidates (parts, sub-assemblies, aliases, not-parts excluded, unresolved) and the original target next to
// the current one, under the coverage summary. Its own block so the coverage view stays easy to merge.

import type { AssemblyCoverage } from '../../api/orchestration';
import { useI18n } from '../../i18n/I18nProvider';
import { identityView } from './identityModel';

export function CoverageIdentity({ coverage }: { coverage: AssemblyCoverage }) {
  const { t } = useI18n();
  const v = identityView(coverage);
  if (!v) return null;
  const c = v.counts;
  return (
    <div className="cov-identity small stack-sm" aria-label={t('idn.title')}>
      <span title={c.excluded ? t('idn.excludedHint') : undefined}>
        <span className="strong">{t('idn.title')}</span> ·{' '}
        {t('idn.line', { parts: c.parts, subs: c.subs, aliases: c.aliases, excluded: c.excluded, unresolved: c.unresolved })}
        {c.unbound > 0 && (
          <span title={(coverage.unbound ?? []).map((u) => u.name + (u.quote ? `：${u.quote}` : '')).join('\n')}>
            {' · '}{t('idn.unbound', { n: c.unbound })}
          </span>
        )}
      </span>
      {(coverage.identity?.catalog_alternatives ?? 0) > 0 && (
        <details>
          <summary>{t('idn.catalog', { n: coverage.identity?.catalog_alternatives ?? 0 })}</summary>
          <p className="faint">{t('idn.catalogHint')}</p>
          <ul>
            {(coverage.catalog_alternatives ?? []).map((item) => (
              <li key={item.key}>
                <span className="strong">{item.name}</span> · {item.reason}
                {!!item.sources?.length && <div className="faint">{item.sources.join(' · ')}</div>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {v.baseline && (
        <span className={v.changed ? '' : 'faint'}>
          {t('idn.baseline', { label: v.baseline.label, defs: v.baseline.defs, insts: v.baseline.insts, cdefs: v.current.defs, cinsts: v.current.insts })}
          {v.changed && <span className="faint"> · {t('idn.changed')}</span>}
        </span>
      )}
    </div>
  );
}
