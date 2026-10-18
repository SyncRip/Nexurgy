// Generation supplement B: the coverage of the spec build a root assembly revision came from (``spec_ref.coverage``).
// A partial revision says so with what it holds of the target and every missing item (stage, reason, what to do);
// a complete one shows its counts in one line. Revisions not built from a spec show nothing.

import type { AssemblyRevision } from '../../api/assemblies';
import { useI18n } from '../../i18n/I18nProvider';
import { CoverageView } from '../orchestration/Coverage';
import { coverageOf } from '../orchestration/coverageModel';

export function RevisionCoverage({ rev }: { rev: Pick<AssemblyRevision, 'spec_ref'> }) {
  const { t } = useI18n();
  const cov = coverageOf(rev.spec_ref);
  if (!cov) return null;
  if (cov.status === 'complete') {
    return (
      <div className="asm-banner asm-coverage faint small" aria-label={t('cov.revision.title')}>
        <CoverageView coverage={cov} compact />
      </div>
    );
  }
  return (
    <section className="callout slim warn asm-banner asm-coverage" aria-label={t('cov.revision.title')}>
      <details open={cov.missing.length <= 8}>
        <summary className="strong small">{t('cov.revision.partial')}</summary>
        <CoverageView coverage={cov} />
      </details>
    </section>
  );
}
