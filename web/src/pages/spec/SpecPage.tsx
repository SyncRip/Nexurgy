import { FocusNotice } from '../../features/todos/FocusNotice';
// /specs/:specId[?version=<id>] — one engineering spec (W2): the chosen (default: latest) immutable version with
// its sources and read state, definitions / instance tree with quantities and readiness, relations, facts with
// evidence locators, questions (answer form → new version), assumptions, conflicts and the version history.
// An extracting version is polled; a failed one shows stage / code / call ids / recovery, can be continued from the chunks
// already read (S3, recovery continue_extract) or re-extracted.

import { ArrowLeft,CircleAlert,FlaskConical,HelpCircle,Info,Loader2,RefreshCw,Zap } from 'lucide-react';
import { useEffect,useState } from 'react';
import { Link,useNavigate,useParams,useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { getProject } from '../../api/endpoints';
import type { CheckStatus } from '../../api/engineering';
import { continueSpec,getSpec,getSpecVersion,isUsable,reextractSpec,specPath,type SpecError,type SpecVersion } from '../../api/spec';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { ContinueAction } from '../../components/engineering/ContinueAction';
import { CheckStatusChip,PolicyChip,UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { recoveryText } from '../../components/engineering/recovery';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { Dialog } from '../../components/ui/Dialog';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { EmptyState,ErrorNotice,ErrorState,Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useAssistantPageContext } from '../../features/assistant/AssistantProvider';
import { SimulatedChip,SpecStatusChip,tk } from '../../features/spec/common';
import '../../features/spec/spec.css';
import {
AssumptionsSection,
ChunkDialog,
ConflictsSection,
FactsSection,
HistorySection,
NotesSection,
QuestionsSection,
RelationsSection,
SourcesSection,
StructureSection,
} from '../../features/spec/SpecSections';
import { UpstreamConfirmPanel } from '../../features/turbo/UpstreamConfirm';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

const POLL_MS = 2000;
const CHECK_STATUSES: readonly string[] = ['not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown'];

function FailureCallout({
  error,
  onRetry,
  canRetry,
  onContinue,
  continuedBy,
  specId,
}: {
  error: SpecError;
  onRetry: () => void;
  canRetry: boolean;
  onContinue: () => Promise<unknown>;
  continuedBy?: { id: string; version_no: number } | null;
  specId: string;
}) {
  const { t } = useI18n();
  const recovery = recoveryText(t, error);
  const detail = (error.detail ?? {}) as { unread?: { source: string; label: string; locators?: string[]; chunks?: number }[]; problems?: string[] };
  return (
    <div className="callout err spec-err" role="alert">
      <CircleAlert aria-hidden />
      <div className="grow">
        <div className="strong">
          {t('spec.page.failed', { stage: tk(t, `spec.stage.${error.stage}`, error.stage), code: error.code })}
          {error.retryable ? <span className="faint small"> · {t('spec.page.retryable')}</span> : null}
        </div>
        <div className="small">{error.message}</div>
        <dl>
          {recovery ? (
            <>
              <dt>{t('spec.page.recovery')}</dt>
              <dd>{recovery}</dd>
            </>
          ) : null}
          {error.call_ids?.length ? (
            <>
              <dt>{t('spec.page.calls')}</dt>
              <dd className="mono">{error.call_ids.join(', ')}</dd>
            </>
          ) : null}
          {detail.unread?.length ? (
            <>
              <dt>{t('spec.page.unreadParts')}</dt>
              <dd>
                {detail.unread.map((u) => (
                  <div key={u.source}>
                    <span className="mono">{u.source}</span> {u.label}: <span className="mono small">{(u.locators ?? []).slice(0, 6).join('; ')}</span>
                    {(u.locators?.length ?? 0) > 6 ? ` … (+${(u.locators?.length ?? 0) - 6})` : ''}
                  </div>
                ))}
              </dd>
            </>
          ) : null}
          {detail.problems?.length ? (
            <>
              <dt>{t('spec.page.problems')}</dt>
              <dd>
                {detail.problems.slice(0, 10).map((p) => (
                  <div key={p} className="small">
                    {p}
                  </div>
                ))}
              </dd>
            </>
          ) : null}
        </dl>
        {continuedBy ? (
          <div className="small" style={{ marginTop: 8 }}>
            <Link to={specPath(specId, continuedBy.id)} className="linkish">
              {t('recovery.continuedAs', { label: t('spec.page.versionOf', { n: continuedBy.version_no, origin: t('spec.origin.continue') }) })}
            </Link>
          </div>
        ) : null}
        <div className="row wrap" style={{ marginTop: 8, alignItems: 'flex-start' }}>
          <ContinueAction
            failure={error}
            onContinue={onContinue}
            canWrite={canRetry}
            showText={false}
            blocked={continuedBy ? t('recovery.disabled.already_continued') : null}
          />
          {canRetry ? (
            <button type="button" className="btn sm" onClick={onRetry} title={t('spec.page.reextractBody')}>
              <RefreshCw aria-hidden /> {t('spec.page.reextract')}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ReextractDialog({ onClose, onConfirm, busy, error }: { onClose: () => void; onConfirm: () => void; busy: boolean; error: unknown }) {
  const { t } = useI18n();
  return (
    <Dialog
      title={t('spec.page.reextractTitle')}
      subtitle={t('spec.page.reextractBody')}
      size="sm"
      busy={busy}
      onClose={onClose}
      onSubmit={() => onConfirm()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <RefreshCw aria-hidden /> {t('spec.page.reextract')}
          </button>
        </>
      }
    >
      {null}
      {error ? <ErrorNotice error={error} /> : null}
    </Dialog>
  );
}

export default function SpecPage() {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { specId = '' } = useParams();
  const [search] = useSearchParams();
  const wanted = search.get('version');
  const specRes = useResource((signal) => getSpec(specId, { signal }), [specId]);
  const spec = specRes.data && specRes.data.id === specId ? specRes.data : undefined;
  const versionId = wanted || spec?.latest_version?.id || '';
  const verRes = useResource((signal) => getSpecVersion(versionId, { signal }), [versionId], { enabled: !!versionId });
  const version: SpecVersion | undefined = verRes.data && verRes.data.id === versionId ? verRes.data : undefined;
  const projectRes = useResource((signal) => getProject(spec!.project_id, { signal }), [spec?.project_id], { enabled: !!spec });
  const project = projectRes.data;
  const caps = projectCapabilities(project);
  const [chunk, setChunk] = useState<{ seq: number; quote?: string | null } | null>(null);
  const [reextract, setReextract] = useState(false);
  const [rxBusy, setRxBusy] = useState(false);
  const [rxError, setRxError] = useState<unknown>(null);
  // S5: the AI-adopted values of a Turbo version, confirmed by a person in one go (a standard run can then use it).
  const [confirmOpen, setConfirmOpen] = useState(() => !!search.get('assumption'));

  useShellProject(project);
  useCrumbs([
    { label: t('area.resources'), to: paths.resources('files', spec?.project_id) },
    { label: t('spec.tab'), to: paths.materials(spec?.project_id, { type: 'spec' }) },
    ...(spec ? [{ label: spec.code, mono: true }] : []),
  ]);

  // Poll an extracting version until it finishes; then refresh the version list.
  const extracting = version?.status === 'extracting';
  const reloadVersion = verRes.reload;
  const reloadSpec = specRes.reload;
  useEffect(() => {
    if (!extracting) return;
    const h = window.setInterval(reloadVersion, POLL_MS);
    return () => window.clearInterval(h);
  }, [extracting, reloadVersion]);
  // A finished version, or one the version list does not know yet (just created): refresh the spec and its history.
  const listed = !!version && !!spec?.versions?.some((v) => v.id === version.id);
  useEffect(() => {
    if (version && (version.status !== 'extracting' || !listed)) reloadSpec();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version?.id, version?.status, reloadSpec]);

  useAssistantPageContext(spec && version ? { projectId: spec.project_id, label: `${spec.code} · v${version.version_no}`,
    object: { type: 'engineering_spec', id: spec.id, version: version.id },
    target: { kind: 'engineering_spec_version', object_id: version.id, project_id: spec.project_id } } : null);
  if (specRes.error && !spec) {
    const missing = isApiError(specRes.error) && specRes.error.status === 404;
    return <div className="page">{missing ? <EmptyState title={t('spec.page.notFound')} /> : <ErrorState error={specRes.error} onRetry={specRes.reload} />}</div>;
  }
  if (!spec || (!version && !verRes.error)) {
    return (
      <div className="page stack" role="status" aria-label={t('common.loading')}>
        <Skeleton w="40%" h={24} />
        <Skeleton w="70%" />
        <Skeleton w="100%" h={160} />
      </div>
    );
  }
  if (!version) return <div className="page"><ErrorState error={verRes.error} onRetry={verRes.reload} /></div>;

  const isLatest = spec.latest_version?.id === version.id;
  const usable = isUsable(version.status);
  const gate = version.checks.find((c) => c.check === 'spec.clarification_gate');
  const openQ = version.counts.questions_open;
  const goTo = (id: string) => navigate(specPath(spec.id, id));

  const doContinue = async () => {
    const out = await continueSpec(version.id);
    toast(t('spec.page.continued'));
    goTo(out.id);
  };

  const doReextract = async () => {
    setRxBusy(true);
    setRxError(null);
    try {
      const out = await reextractSpec(version.id, {});
      setReextract(false);
      toast(t('spec.page.reextracted'));
      goTo(out.id);
    } catch (e) {
      setRxError(e);
    } finally {
      setRxBusy(false);
    }
  };

  return (
    <div className="page spec-page">
      <FocusNotice kind="questions" version={version.id} />
      <FocusNotice kind="assumption" version={version.id} />
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{project?.code ?? ''}</span>
          <h1 className="ellip" title={spec.title}>
            <span className="mono">{spec.code}</span> · {spec.title}
          </h1>
          <div className="spec-head-chips">
            <label className="small faint" htmlFor="spec-version">
              {t('spec.page.version')}
            </label>
            <select id="spec-version" className="select sm" value={version.id} onChange={(e) => goTo(e.target.value)}>
              {(spec.versions ?? []).map((v) => (
                <option key={v.id} value={v.id}>
                  {t('spec.page.versionOf', { n: v.version_no, origin: tk(t, `spec.origin.${v.origin}`, v.origin) })}
                </option>
              ))}
            </select>
            <SpecStatusChip status={version.status} />
            <PolicyChip policy={version.execution_policy} />
            {version.simulated ? <SimulatedChip /> : null}
            {gate && CHECK_STATUSES.includes(gate.status) ? <CheckStatusChip status={gate.status as CheckStatus} label={t('spec.page.gate')} /> : null}
            {usable && version.execution_policy === 'turbo' && gate?.status !== 'failed' ? <UnverifiedChip /> : null}
          </div>
        </div>
        <div className="page-actions">

          {caps.workflow && version.status !== 'extracting' ? (
            <button type="button" className="btn" onClick={() => setReextract(true)}>
              <RefreshCw aria-hidden /> {t('spec.page.reextract')}
            </button>
          ) : null}
          <Link className="btn" to={paths.materials(spec.project_id, { type: 'spec', item: `spec:${spec.id}` })}>
            <ArrowLeft aria-hidden /> {t('spec.page.back')}
          </Link>
        </div>
      </header>

      <div className="spec-layout">
        {/* S6: an exact older version (?version=) says so and offers the current one. */}
        {!isLatest && spec.latest_version ? (
          <HistoricalBanner current={`v${spec.latest_version.version_no}`} onOpenCurrent={() => goTo(spec.latest_version!.id)} />
        ) : null}
        {version.status === 'extracting' ? (
          <div className="callout pending slim" role="status">
            <Loader2 className="spin-icon" aria-hidden />
            <span>
              {version.origin === 'answer'
                ? t('spec.page.extractingAnswer')
                : t('spec.page.extracting', { sent: version.planning.chunks_sent ?? 0, total: version.planning.chunks_total ?? version.chunks.length })}
              {version.planning.chunks_reused ? ` · ${t('spec.page.chunksReused', { n: version.planning.chunks_reused })}` : ''}
            </span>
          </div>
        ) : null}
        {version.status === 'failed' && version.error ? (
          <FailureCallout
            error={version.error}
            canRetry={caps.workflow}
            onRetry={() => void doReextract()}
            onContinue={doContinue}
            continuedBy={version.continued_by}
            specId={spec.id}
          />
        ) : null}
        {version.status === 'needs_answers' ? (
          <div className="callout warn slim" role="note">
            <HelpCircle aria-hidden />
            <span>{t('spec.page.needsAnswers', { q: openQ, ready: version.readiness?.buildable_definitions.length ?? 0 })}</span>
          </div>
        ) : null}
        {usable && version.execution_policy === 'turbo' && version.counts.assumptions ? (
          <div className="callout warn slim" role="note">
            <Zap aria-hidden />
            <span className="grow">{t('spec.page.turbo', { n: version.counts.assumptions })}</span>
            {caps.design && (
              <button type="button" className="btn sm" aria-expanded={confirmOpen} onClick={() => setConfirmOpen((o) => !o)}>
                {t('pol.confirm.open')}
              </button>
            )}
          </div>
        ) : null}
        {confirmOpen && usable && version.execution_policy === 'turbo' ? (
          <UpstreamConfirmPanel projectId={spec.project_id} source={{ source_type: 'engineering_spec_version', source_id: version.id }} />
        ) : null}
        {version.simulated ? (
          <div className="callout pending slim" role="note">
            <FlaskConical aria-hidden />
            <span>{t('spec.simulatedHint')}</span>
          </div>
        ) : null}
        {version.planning.provider ? (
          <div className="small faint row">
            <Info className="inline-ico" aria-hidden />
            {t('spec.page.model')}:{' '}
            {t('spec.page.modelValue', {
              provider: version.planning.provider ?? '',
              model: version.planning.model_id ?? '',
              calls: version.planning.ai_call_ids?.length ?? 0,
              repairs: version.planning.repairs ?? 0,
            })}
          </div>
        ) : null}

        <SourcesSection version={version} onViewChunk={(seq, quote) => setChunk({ seq, quote })} />
        {usable ? (
          <>
            <QuestionsSection
              version={version}
              canAnswer={caps.workflow}
              isLatest={isLatest}
              onAnswered={(v) => {
                toast(t('spec.q.submitted'));
                goTo(v.id);
              }}
            />
            <StructureSection version={version} />
            <RelationsSection version={version} />
            <FactsSection version={version} onViewChunk={(seq, quote) => setChunk({ seq, quote })} />
            <ConflictsSection version={version} />
            <AssumptionsSection
              version={version}
              canCorrect={caps.workflow}
              isLatest={isLatest}
              onCorrected={(v) => {
                toast(t('tb.decision.correctDone'));
                goTo(v.id);
              }}
            />
            <NotesSection version={version} onViewChunk={(seq, quote) => setChunk({ seq, quote })} />
          </>
        ) : null}
        <HistorySection spec={spec} currentId={version.id} />
      </div>

      {chunk ? <ChunkDialog versionId={version.id} seq={chunk.seq} quote={chunk.quote} onClose={() => setChunk(null)} /> : null}
      {reextract ? (
        <ReextractDialog busy={rxBusy} error={rxError} onClose={() => setReextract(false)} onConfirm={() => void doReextract()} />
      ) : null}
    </div>
  );
}
