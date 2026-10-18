// Generation supplement B (partial assembly): the coverage of an assembly build — target vs actual, the missing items
// grouped by stage with their reason and recovery — and the marks "Partial assembly", "Contains AI decisions", "Unverified" shown apart
// (design §2.2: none of them stands in for another). Used by the chain values (run card, node drawer, AI workspace
// card), the assembly page and the delivery.

import { Bot, PackageX, ShieldQuestion, StepForward } from 'lucide-react';
import { useState } from 'react';
import type { AssemblyCoverage, DecisionUse } from '../../api/orchestration';
import { recoveryText } from '../../components/engineering/recovery';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { canContinueBuild, coverageCounts, coverageMarks, decisionUses, groupMissing, subassemblyPlacement } from './coverageModel';
import { CoverageIdentity } from './CoverageIdentity';
import './orchestration.css';

function tx(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

/** One line: "Target: N part definitions, M instances; identified …; valid geometry …; installed … (including … AI-decided positions); failed …" (+ sub-assemblies,
 *  arranged). */
export function coverageLine(t: TFunction, c: AssemblyCoverage): string {
  const n = coverageCounts(c);
  const extra = [
    n.subassemblies ? t('cov.summary.subs', { n: n.subassemblies }) : null,
    n.localOnly ? t('cov.summary.localOnly', { n: n.localOnly }) : null, // generation reliability §2.6
    n.arranged ? t('cov.summary.arranged', { n: n.arranged }) : null,
  ].filter(Boolean);
  // generation supplement F: how many of the installed instances sit where a Turbo AI decision placed them; round 2 (P):
  // how many of them at an AI-estimated position (installed, shown apart)
  const placed =
    `${n.placed}` +
    (n.decided ? t('cov.summary.decided', { n: n.decided }) : '') +
    (n.estimated ? t('cov.summary.estimated', { n: n.estimated }) : '');
  return [
    t('cov.summary', { defs: n.targetDefinitions, insts: n.targetInstances, recognized: n.recognized, built: n.built ?? t('cov.summary.unknown'), placed, failed: n.failed }),
    ...extra,
  ].join(t('cov.sep'));
}

/** The three marks, each on its own: partial / no assembly, AI decisions, not verified. */
export function CoverageMarks({ coverage, aiDecisions, unverified }: { coverage?: AssemblyCoverage | null; aiDecisions?: number | null; unverified?: boolean }) {
  const { t } = useI18n();
  const m = coverageMarks(coverage, aiDecisions);
  return (
    <>
      {m.partial && (
        <Chip tone="warn" icon={<PackageX aria-hidden />}>
          {t('cov.mark.partial')}
        </Chip>
      )}
      {m.noAssembly && (
        <Chip tone="err" icon={<PackageX aria-hidden />}>
          {t(m.parts === 'none' ? 'cov.mark.noGeometry' : m.parts === 'available' ? 'cov.mark.noAssembly' : 'cov.mark.noAssemblyUnknown')}
        </Chip>
      )}
      {m.aiDecisions > 0 && (
        <Chip tone="measure" icon={<Bot aria-hidden />}>
          {t('cov.mark.ai', { n: m.aiDecisions })}
        </Chip>
      )}
      {unverified && (
        <Chip tone="warn" icon={<ShieldQuestion aria-hidden />} title={t('me.unverified.hint')}>
          {t('cov.mark.unverified')}
        </Chip>
      )}
    </>
  );
}

/** "Continue remaining work" of a partial build: shown only when continuing can rebuild what is missing (the server re-checks). */
export function ContinueMissing({ failure, onContinue }: { failure: Parameters<typeof canContinueBuild>[0]; onContinue?: () => Promise<unknown> }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!onContinue || !canContinueBuild(failure)) return null;
  return (
    <div className="stack-sm">
      <div className="row wrap">
        <button
          type="button"
          className="btn sm primary"
          disabled={busy}
          title={t('cov.continue.hint')}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await onContinue();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          <StepForward aria-hidden /> {busy ? t('cov.continuing') : t('cov.continue')}
        </button>
        <span className="faint small">{t('cov.continue.hint')}</span>
      </div>
      {error ? <ErrorNotice error={error} /> : null}
    </div>
  );
}

/** Summary line and (unless compact) the missing items grouped by stage, with reason and what to do. */
export function CoverageView({ coverage, compact }: { coverage: AssemblyCoverage; compact?: boolean }) {
  const { t } = useI18n();
  const groups = groupMissing(coverage);
  return (
    <div className="cov stack-sm">
      <span className="small cov-line" title={t('cov.summary.basis')}>
        {coverageLine(t, coverage)}
      </span>
      <ChainPlacementLine coverage={coverage} />
      {!compact && <CoverageIdentity coverage={coverage} />}
      {!compact && groups.length > 0 && (
        <div className="cov-missing">
          <span className="small strong">{t('cov.missing.title', { n: coverage.missing.length })}</span>
          {groups.map((g) => (
            <section key={g.stage} className="cov-group">
              <span className="small cov-stage">
                {tx(t, `cov.stage.${g.stage}`, g.stage)} · {g.items.length}
              </span>
              <ul>
                {g.items.map((m) => {
                  const what = recoveryText(t, { recovery: m.recovery });
                  return (
                    <li key={`${g.stage}:${m.key}`}>
                      <span className="strong">{m.name}</span> <span className="mono faint">{m.key}</span>
                      {m.kind === 'relation' || m.kind === 'assembly' ? <span className="faint"> · {tx(t, `cov.kind.${m.kind}`, m.kind)}</span> : null}
                      {m.quantity > 0 && <span className="faint"> {t('cov.qty', { n: m.quantity })}</span>} · <code className="error-code">{m.code}</code>{' '}
                      {hasKey(`cov.code.${m.code}`) && <span className="strong small">{tx(t, `cov.code.${m.code}`, m.code)} </span>}
                      <span className="faint">{m.reason}</span>
                      {what && <span className="cov-recovery small">{t('cov.recovery', { text: what })}</span>}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

// --- generation reliability §2.6 (package L): the parent chain and where decisions were used ----------------------------

/** "Sub-assembly placement: 1/3 placed relative to the root; unplaced: I-10, I-30" — the sub-assembly instances placed up to the root (an instance
 * counts installed only when its whole parent chain is placed); nothing without sub-assemblies. */
export function ChainPlacementLine({ coverage }: { coverage: AssemblyCoverage }) {
  const { t } = useI18n();
  const s = subassemblyPlacement(coverage);
  if (!s) return null;
  return (
    <span className="small cov-line" title={t('cov.subs.hint')}>
      {t('cov.subs.placement', { placed: s.placed, total: s.total })}
      {s.unplaced.length > 0 && <span className="strong">{t('cov.sep') + t('cov.subs.unplaced', { list: s.unplaced.join('、') })}</span>}
    </span>
  );
}

/** "Used for: modeling P-001 R1 (D-04) · positioning D07.00 A2 (I-04) · exporting 2 files" — where one AI decision was actually used. */
export function DecisionUses({ uses }: { uses?: readonly DecisionUse[] | null }) {
  const { t } = useI18n();
  const groups = decisionUses(uses);
  if (!groups.length) return null;
  const parts = groups.map((g) => {
    const what = g.stage === 'export' ? t('cov.use.files', { n: g.objects.length }) : g.objects.join('、');
    const refs = g.stage === 'export' || !g.refs.length ? '' : `（${g.refs.join('、')}）`;
    return `${tx(t, `cov.use.stage.${g.stage}`, g.stage)} ${what}${refs}`;
  });
  return (
    <span className="faint small cov-uses" title={groups.map((g) => `${g.stage}: ${g.objects.join(', ')}`).join('\n')}>
      {t('cov.use.title')}
      {parts.join(' · ')}
    </span>
  );
}
