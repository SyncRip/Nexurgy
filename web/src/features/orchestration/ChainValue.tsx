// Port values of the general mechanical engineering chain (W8): engineering spec version, assembly (or the parts
// built when it could not be made), drawing package and delivery manifest. Shown on the run canvas, in the node
// drawer and in the AI workspace operation card. Everything comes from the recorded value; a partial delivery or a
// blocked definition is shown as such, never as success.

import { Boxes, Download, ExternalLink, FileStack, PackageCheck, ScrollText } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { saveBlob } from '../../api/blobs';
import type { CheckStatus } from '../../api/engineering';
import {
  downloadDelivery,
  getDelivery,
  isChainValue,
  type AiDecision,
  type AssemblyValue,
  type ChainValue,
  type DeliveryValue,
  type DeliveryVerification,
  type DrawingPackageValue,
  type SourceCoverage,
  type SpecValue,
} from '../../api/orchestration';
import type { NodeRun } from '../../api/types';
import { Chip, type Tone } from '../../components/ui/Badges';
import { CheckStatusChip, PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { hasKey } from '../../i18n/core';
import { describeError } from '../../i18n/errors';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { assemblyRevisionPath, partOutputPath } from '../../app/versionLinks';
import { artifactCheckState, unverifiedBesideCheck } from '../turbo/model';
import { AssumptionList } from '../turbo/Turbo';
import { coverageSummary, fileLabel, givenSources, sourceState, sourceTone } from '../assistant/attachments';
import { specRevision } from '../assistant/revise';
import { ContinueMissing, CoverageMarks, CoverageView, DecisionUses } from './Coverage';
import { DeliveryLevelsView, SubassemblyPackages } from './Levels';
import { ClosedChecksNote, ObjectStateBadge } from './StateBadge';
import { asObjectState } from './status';
import './orchestration.css';

const CHECKS: readonly CheckStatus[] = ['not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown'];
const DEF_TONE: Record<string, Tone> = { built: 'ok', blocked: 'warn', needs_clarification: 'warn', failed: 'err', not_started: 'stale' };

function tx(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

function asCheck(s: string | undefined | null): CheckStatus | null {
  return s && (CHECKS as readonly string[]).includes(s) ? (s as CheckStatus) : s ? 'unknown' : null;
}

/** The chain value among a node run's outputs (first port value of a chain type). */
export function chainValueOf(nr: Pick<NodeRun, 'outputs'> | undefined): ChainValue | null {
  for (const v of Object.values(nr?.outputs ?? {})) if (isChainValue(v)) return v;
  return null;
}

/** One line for a zoomed-out run card. */
export function chainLine(t: TFunction, v: ChainValue): string {
  switch (v.type) {
    case 'engineering_spec':
      return t('orch.line.spec', { code: v.code ?? '', v: v.version_no, status: tx(t, `orch.specStatus.${v.status}`, v.status) });
    case 'assembly':
      // generation supplement B: a partial assembly is a product, shown as partial (never as complete, never as nothing)
      if (v.assembly_revision_id && v.coverage && v.coverage.status !== 'complete')
        return t('orch.line.partial', { label: v.label ?? '', placed: v.coverage.placed.instances, target: v.coverage.target.instances });
      return v.assembly_revision_id
        ? t('orch.line.assembly', { label: v.label ?? '', leaves: v.stats?.leaf_instances ?? '?' })
        : t(v.coverage ? 'orch.line.noAssembly' : 'orch.line.parts', { built: v.parts.length, total: v.definitions.length });
    case 'drawing_package':
      return t('orch.line.drawings', { pages: v.pages, completeness: tx(t, `orch.completeness.${v.completeness}`, v.completeness) });
    case 'delivery':
      return t('orch.line.delivery', { number: v.number, completeness: tx(t, `orch.completeness.${v.completeness}`, v.completeness) });
  }
}

function SpecView({ v, compact }: { v: SpecValue; compact?: boolean }) {
  const { t } = useI18n();
  const tone: Tone = v.status === 'ready' ? 'ok' : v.status === 'needs_answers' ? 'warn' : v.status === 'failed' ? 'err' : 'pending';
  return (
    <div className="orch-val stack-sm">
      <div className="row wrap">
        <span className="orch-kind">
          <ScrollText aria-hidden /> {t('orch.kind.spec')}
        </span>
        <Link to={v.path} className="orch-link">
          {v.code} v{v.version_no} <ExternalLink aria-hidden />
        </Link>
        <Chip tone={tone}>{tx(t, `orch.specStatus.${v.status}`, v.status)}</Chip>
        {v.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
        {v.simulated && <Chip tone="measure">{t('orch.simulated')}</Chip>}
      </div>
      <span className="faint small">
        {t('orch.spec.counts', { d: v.counts.definitions ?? 0, i: v.counts.instances ?? 0, f: v.counts.facts ?? 0 })}
      </span>
      {!compact && v.open_questions.length > 0 && (
        <div className="orch-questions">
          <span className="small strong">{t('orch.questions', { n: v.open_questions.length })}</span>
          <ul>
            {v.open_questions.slice(0, 6).map((q) => (
              <li key={q.id}>
                <span className="mono">{q.id}</span> {q.question}
              </li>
            ))}
          </ul>
          <Link to={v.path} className="linkish small">
            {t('orch.answerInSpec')}
          </Link>
        </div>
      )}
      {!compact && v.assumptions.length > 0 && <span className="small">{t('orch.assumptions', { n: v.assumptions.length })}</span>}
      <SpecSources v={v} compact={compact} />
      {v.error?.message && <span className="small err-text">{v.error.message}</span>}
    </div>
  );
}

/** Generation supplement A-02: what the spec step really read, per source (Read / partially read / referenced / read failed);
 * compact shows only the sources that were not fully read. */
function SpecSources({ v, compact }: { v: SpecValue; compact?: boolean }) {
  const { t } = useI18n();
  const all = givenSources(v.sources);
  const shown = compact ? all.filter((s) => sourceState(s) !== 'read') : all;
  const dedupe = v.input_dedupe;
  const revision = specRevision(v); // generation supplement I (A-04): a new version of the same spec
  if (!shown.length && !dedupe && !v.derived && !revision) return null;
  return (
    <div className="stack-sm">
      {shown.length > 0 && (
        <ul className="orch-defs" aria-label={t('as.src.title')}>
          {shown.map((s) => {
            const state = sourceState(s);
            return (
              <li key={s.id}>
                <Chip tone={sourceTone(state)} className="sm">
                  {t(`as.src.${state}`)}
                </Chip>{' '}
                <span className="ellip">
                  {s.kind === 'text' ? (s.id && s.id === v.revision?.note_source ? t('rv.src.note') : t('as.src.text')) : s.label || s.name || s.file_version_id}
                </span>
                {s.unread ? <span className="faint small"> · {t('as.src.unreadChunks', { n: s.unread })}</span> : null}
                {s.error?.code && <code className="error-code">{s.error.code}</code>}
                {!compact && s.coverage && <SourceCoverageLine c={s.coverage} />}
              </li>
            );
          })}
        </ul>
      )}
      {!compact && dedupe && (
        <span className="faint small">{t('as.src.dedupe', { lines: dedupe.lines_removed, files: dedupe.files.map((f) => fileLabel(f)).join('、') })}</span>
      )}
      {!compact && v.derived && (
        <span className="small">
          {v.derived.rule === 'authorized_engineering_design'
            ? t('as.src.authorizedDesign')
            : v.derived.rule === 'turbo_alias_resolution'
              ? t('as.src.aliases', {
                  merged: (v.derived.merged ?? []).map((m) => `${m.alias_name || m.alias} → ${m.into_name || m.into}`).join('、') || '—',
                  hung: (v.derived.hung ?? []).length,
                  left: (v.derived.not_a_part ?? []).length + (v.derived.unknown ?? []).length,
                })
              : t('as.src.derived', { defs: (v.derived.definitions ?? []).join('、') })}
        </span>
      )}
      {revision && (
        <span className="small">
          {revision.kind === 'replaced'
            ? t('rv.spec.replaced', { of: revision.of, added: revision.added.join('、') || t('rv.none'), dropped: revision.dropped.join('、') || t('rv.none') })
            : t('rv.spec.revised', { of: revision.of, added: revision.added.join('、') || t('rv.none'), note: revision.note ? t('rv.spec.revisedNote') : '' })}
        </span>
      )}
    </div>
  );
}

/** Generation reliability K (design §2.4): a drawing's source coverage — chunks sent and where every entity went (in
 * frames, outside frames read / excluded with reason / unexplained, quarantined) — shown apart from the read state. */
function SourceCoverageLine({ c }: { c: SourceCoverage }) {
  const { t } = useI18n();
  const s = coverageSummary(c);
  return (
    <div className="faint small stack-sm">
      <span>
        {t('src.cov.line', { frames: s.frames, sent: s.sent, chunks: s.chunks, total: s.total, inFrames: s.inFrames, read: s.read,
          excluded: s.excluded, unexplained: s.unexplained, quarantined: s.quarantined })}
      </span>
      {s.reasons.length > 0 && (
        <span>{t('src.cov.excluded', { reasons: s.reasons.map((r) => tx(t, `src.cov.reason.${r.reason}`, `${r.reason} (${r.n})`, { n: r.n })).join('、') })}</span>
      )}
      {s.unexplained > 0 && <span className="cov-recovery">{t('src.cov.unexplained', { n: s.unexplained })}</span>}
    </div>
  );
}

function AssemblyView({ v, compact, onContinue }: { v: AssemblyValue; compact?: boolean; onContinue?: () => Promise<unknown> }) {
  const { t } = useI18n();
  const check = asCheck(v.check_status);
  const built = !!(v.assembly_id && v.assembly_revision_id);
  const covered = v.coverage ?? null;
  const unverified = unverifiedBesideCheck(v.verification, check) && v.execution_policy === 'turbo';
  return (
    <div className="orch-val stack-sm">
      <div className="row wrap">
        <span className="orch-kind">
          <Boxes aria-hidden /> {t('orch.kind.assembly')}
        </span>
        {built ? (
          <Link to={assemblyRevisionPath(v.assembly_id!, v.assembly_revision_id)} className="orch-link">
            {v.number} {v.label} <ExternalLink aria-hidden />
          </Link>
        ) : (
          !covered && <Chip tone="err">{t('orch.assemblyNotBuilt')}</Chip>
        )}
        {check && <CheckStatusChip status={check} />}
        {v.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
        {/* generation supplement B: partial / no assembly, AI decisions and not verified are separate marks (a partial
            product never reads "done · not verified"); S6: never "not verified" beside a failed check */}
        <CoverageMarks coverage={covered} aiDecisions={v.decisions?.count} unverified={unverified && !!covered && covered.status !== 'complete'} />
        {unverified && (!covered || covered.status === 'complete') && <UnverifiedChip />}
        {v.simulated && <Chip tone="measure">{t('orch.simulated')}</Chip>}
      </div>
      {covered && <CoverageView coverage={covered} compact={compact} />}
      {built && v.stats && (
        <span className="faint small">
          {t('orch.assembly.stats', {
            parts: v.stats.part_definitions ?? 0,
            leaves: v.stats.leaf_instances ?? 0,
            asms: v.stats.assembly_definitions ?? 0,
          })}
        </span>
      )}
      {!compact && (
        <ul className="orch-defs">
          {v.definitions.map((d) => (
            <li key={d.key}>
              <Chip tone={DEF_TONE[d.status] ?? 'stale'} className="sm">
                {tx(t, `orch.def.${d.status}`, d.status)}
              </Chip>
              <span className="mono small">{d.key}</span> <span className="ellip">{d.name}</span>
              {d.part_id && (
                <Link to={partOutputPath(d.part_id, 'model', d.revision_id)} className="linkish small">
                  {d.part_number} R{d.rev_no}
                </Link>
              )}
              {d.source && <span className="faint small">{tx(t, `orch.source.${d.source}`, d.source)}</span>}
              {d.reused && (
                <Chip tone="pending" className="sm" title={t('recovery.reusedHint')}>
                  {t('recovery.reused')}
                </Chip>
              )}
              {d.error?.message && <span className="small err-text">{d.error.message}</span>}
              {(d.questions ?? []).filter((q) => q.question).map((q, i) => (
                <span key={i} className="small orch-q">
                  {q.id ? <span className="mono">{q.id} </span> : null}
                  {q.question}
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
      {!compact && v.progress && v.progress.reused > 0 && (
        <span className="faint small">{t('recovery.progress', { built: v.progress.built, total: v.progress.total, reused: v.progress.reused })}</span>
      )}
      {v.failure && <span className="small err-text">{tx(t, `wf.nerr.${v.failure.code}`, v.failure.message)}</span>}
      {!compact && <ContinueMissing failure={v.failure} onContinue={onContinue} />}
    </div>
  );
}

function DrawingsView({ v, compact }: { v: DrawingPackageValue; compact?: boolean }) {
  const { t } = useI18n();
  const subs = v.subassembly_packages ?? [];
  return (
    <div className="orch-val stack-sm">
      <div className="row wrap">
        <span className="orch-kind">
          <FileStack aria-hidden /> {t('orch.kind.drawings')}
        </span>
        <Chip tone={v.completeness === 'complete' ? 'ok' : v.completeness === 'failed' ? 'err' : 'warn'}>
          {tx(t, `orch.completeness.${v.completeness}`, v.completeness)}
        </Chip>
        {v.coverage?.status === 'partial' && <Chip tone="warn">{t('cov.drawings.partial')}</Chip>}
        <span className="faint small">{t('orch.drawings.pages', { pages: v.pages, files: v.files })}</span>
      </div>
      {!v.package_id && v.error && (
        <span className="small err-text">
          {t('lv.rootFailed')} · <code className="error-code">{v.error.code}</code>
        </span>
      )}
      {/* generation supplement E: the layered step's sub-assembly packages */}
      {!compact && <SubassemblyPackages items={subs} />}
    </div>
  );
}

export function DeliveryDownload({ id, label }: { id: string; label?: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="btn sm"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const { blob, filename } = await downloadDelivery(id);
          saveBlob(blob, filename ?? 'delivery.zip');
        } catch (e) {
          toast(`${t('error.downloadFailed')} ${t(describeError(e).key)}`, { tone: 'err' });
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <Spinner /> : <Download aria-hidden />} {label ?? t('orch.download')}
    </button>
  );
}

function DeliveryView({ v, compact }: { v: DeliveryValue; compact?: boolean }) {
  const { t } = useI18n();
  // S6: one state, failed > not verified > passed; the row's check_status is the delivery's real one.
  const ver: Partial<DeliveryVerification> = v.verification;
  const state = artifactCheckState({ ...ver, check_status: v.check_status ?? ver.check_status });
  const failedSources = (ver.source_checks ?? []).filter((s) => s.check_status === 'failed');
  // generation supplement B: the coverage items are listed (grouped by stage) with the coverage, the rest here
  const others = v.coverage ? v.missing.filter((m) => m.item !== 'assembly_coverage') : v.missing;
  return (
    <div className="orch-val stack-sm">
      <div className="row wrap">
        <span className="orch-kind">
          <PackageCheck aria-hidden /> {t('orch.kind.delivery')}
        </span>
        <span className="mono">{v.number}</span>
        {/* generation reliability N: the delivery's state (deliveries from before it: completeness) */}
        {asObjectState(v.state) ? (
          <ObjectStateBadge state={v.state} />
        ) : (
          <Chip tone={v.completeness === 'complete' ? 'ok' : 'warn'}>{tx(t, `orch.completeness.${v.completeness}`, v.completeness)}</Chip>
        )}
        {v.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
        <CoverageMarks coverage={v.coverage} aiDecisions={v.decisions?.count}
          unverified={state === 'unverified' && !!v.coverage && v.coverage.status !== 'complete'} />
        {state === 'unverified' && (!v.coverage || v.coverage.status === 'complete') && <UnverifiedChip />}
        {state === 'failed' && <CheckStatusChip status="failed" />}
        {v.simulated && v.state !== 'simulated' && <Chip tone="measure">{t('orch.simulated')}</Chip>}
        <DeliveryDownload id={v.delivery_id} />
      </div>
      <span className="faint small">{t('orch.delivery.files', { n: v.files })}</span>
      <ClosedChecksNote checks={v.checks} />
      {v.coverage && v.coverage.status !== 'complete' && <CoverageView coverage={v.coverage} compact={compact} />}
      {v.levels && <DeliveryLevelsView levels={v.levels} compact={compact} />}
      {!compact && v.decisions && v.decisions.count > 0 && <DeliveryDecisions id={v.delivery_id} n={v.decisions.count} pending={v.decisions.needs_confirmation} />}
      {failedSources.length > 0 && <span className="small err-text">{t('ver.delivery.failedSources', { list: failedSources.map((s) => s.label).join(', ') })}</span>}
      {!compact && others.length > 0 && (
        <div className="orch-missing">
          <span className="small strong">{t('orch.delivery.missing', { n: others.length })}</span>
          <ul>
            {others.map((m, i) => (
              <li key={i}>
                <span className="strong">{tx(t, `orch.item.${m.item}`, m.item)}</span> · <code className="error-code">{m.code}</code>{' '}
                <span className="faint">{m.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Generation supplement D-03: the AI decisions a delivery rests on, read from its manifest when asked for (the port
 * value carries only the counts). */
function DeliveryDecisions({ id, n, pending }: { id: string; n: number; pending: number }) {
  const { t } = useI18n();
  const [items, setItems] = useState<AiDecision[] | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'error'>('idle');
  const load = async () => {
    setState('loading');
    try {
      const d = await getDelivery(id);
      setItems(d.manifest.decisions?.items ?? []);
      setState('idle');
    } catch {
      setState('error');
    }
  };
  return (
    <div className="stack-sm">
      <div className="row wrap">
        <span className="faint small">{t('cov.decisions', { n, pending })}</span>
        {items === null && (
          <button type="button" className="btn sm ghost" onClick={() => void load()} disabled={state === 'loading'}>
            {state === 'loading' ? <Spinner /> : null} {t('cov.decisionsShow')}
          </button>
        )}
        {state === 'error' && <span className="small err-text">{t('cov.decisionsError')}</span>}
      </div>
      {/* generation reliability §2.6: each decision says where it was actually used */}
      {items && <AssumptionList items={items} bare actions={(item) => <DecisionUses uses={(item as AiDecision).consumed_by} />} />}
    </div>
  );
}

/** ``onContinue`` (assembly values): "Continue remaining work" — continue the run from the assembly step (shown only when continuing
 * can rebuild what is missing). */
export function ChainValueView({ value, compact, onContinue }: { value: ChainValue; compact?: boolean; onContinue?: () => Promise<unknown> }) {
  switch (value.type) {
    case 'engineering_spec':
      return <SpecView v={value} compact={compact} />;
    case 'assembly':
      return <AssemblyView v={value} compact={compact} onContinue={onContinue} />;
    case 'drawing_package':
      return <DrawingsView v={value} compact={compact} />;
    case 'delivery':
      return <DeliveryView v={value} compact={compact} />;
  }
}

/** The chain values of a run's node runs in chain order (spec → assembly → drawings → delivery). */
export function chainValuesOf(nodes: Pick<NodeRun, 'outputs'>[]): ChainValue[] {
  const order = ['engineering_spec', 'assembly', 'drawing_package', 'delivery'];
  return nodes
    .map((n) => chainValueOf(n))
    .filter((v): v is ChainValue => v !== null)
    .sort((a, b) => order.indexOf(a.type) - order.indexOf(b.type));
}
