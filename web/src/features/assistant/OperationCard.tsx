import { CadResultCard } from './CadResultCard';
// One operation under the request that started it (design §6.3, §7.3): kind,
// status as icon + text, the exact basis (target version), run link, waiting
// questions (clarification form / manual entry on the run page), failure with
// the original request kept and an explicit retry (once: a retried operation
// points to the newer request instead), cancel showing the real result, and
// the result card once it succeeded. Generation finished is not acceptance —
// the card says so.

import { AlertTriangle,ArrowDownCircle,ChevronRight,ExternalLink,Hand,Paperclip,RotateCcw,Send,Square } from 'lucide-react';
import { useEffect,useState,type MouseEvent } from 'react';
import { Link } from 'react-router-dom';
import { assemblyModifyResult,draftingModifyResult,continueAiOperation,workflowCreateResult,workflowEditResult,type AiOperation,type AiResultItem } from '../../api/assistant';
import { useFeature } from '../../api/capabilities';
import { isApiError } from '../../api/client';
import type { ChainValue, SpecSourceBrief } from '../../api/orchestration';
import { paths } from '../../app/nav';
import { AiFailureDetails } from '../../components/ai/AiFailureDetails';
import { AiFailureCauses } from '../../components/ai/AiFailureCauses';
import { ContinueAction } from '../../components/engineering/ContinueAction';
import { PolicyChip,UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from '../agent-team/AgentRole';
import { isCoordinatedOperation, roleForOperation } from '../agent-team/roles';
import { CadErrorDetails } from '../cad/CadErrorDetails';
import { DrawingPackageCard } from '../cad/DrawingPackage';
import { cadErrorFamily } from '../cad/status';
import { CheckSummaryView,ClarificationGaps,ReconstructSourceView } from '../cad/Verdicts';
import { AssemblyChainView,AssemblyModifyCard } from '../orchestration/AssemblyOps';
import { ChainValueView } from '../orchestration/ChainValue';
import { ExecutionContextLine } from '../orchestration/StateBadge';
import { operationExecutionContext } from '../orchestration/status';
import { confirmSourceOf } from '../turbo/policyModel';
import { decisionState } from '../turbo/decisions';
import { OperationProgress } from '../progress/OperationProgress';
import { ProgressResults } from '../progress/ProgressResults';
import { useRunProgress } from '../progress/useRunProgress';
import { isLegacyModelNote } from '../progress/model';
import { PolicySourceTag } from '../turbo/PolicySource';
import { UpstreamConfirmPanel } from '../turbo/UpstreamConfirm';
import { useAssistantSession } from './AssistantProvider';
import { fileLabel,sourceState } from './attachments';
import { AsError,MaterialPlacements,modelLabelText,opKindLabel,OperationStatusChip,OverBudget,RefChip,SimChip,tk,usageText } from './common';
import { DraftingPlanCard } from './DraftingPlanCard';
import { planOutcome } from './planApply';
import { retryScope } from './drafts';
import { isTerminal } from './model';
import { ReplaceSource } from './ReplaceSource';
import { ResultCard,TaskDraftResult } from './ResultCard';
import { savedResultPresentation } from './savedResultPresentation';
import { canReplaceSource,choiceFiles,requestFileLabels } from './revise';
import { WorkflowCreateCard } from './WorkflowCreateCard';
import { WorkflowEditCard } from './WorkflowEditCard';
import './operationPresentation.css';

export type OperationPresentation = 'standalone' | 'journey';

const RUNLESS_CANCELLABLE = new Set(['spec_extract', 'drafting_create', 'drafting_modify']);
const CANCELLABLE = new Set(['model_create', 'model_modify', 'drawing_intake', 'drawing_generate', 'drawing_reconstruct', 'assembly_create', 'spec_extract', 'drafting_create', 'drafting_modify']);

/**
 * Whether the result shows the modeling step's requirement check: always for model / reconstruct results
 * (a missing check says so), for drawing packages only when the operation modeled the part first.
 */
function showsCheck(op: AiOperation): boolean {
  const r = op.result;
  if (!r || r.kind === 'task_draft' || r.check === undefined) return false;
  if (r.kind === 'model' || r.kind === 'reconstruct') return true;
  return r.kind === 'drawing' && r.check !== null;
}

const attentionCheck = (status: string | null | undefined) => status === 'failed' || status === 'pending' || status === 'unknown';

/** Incomplete products and actionable questions stay expanded. Unaccepted saved decisions get a visible summary. */
function completedChainValue(value: ChainValue): boolean {
  switch (value.type) {
    case 'engineering_spec':
      return value.status === 'ready' && !value.error && !value.open_questions.length
        && !value.readiness?.blocked_definitions.length
        && !(value.sources ?? []).some((source) => source.error || ['partial', 'unread', 'failed'].includes(source.read_status));
    case 'assembly':
      return value.complete && !!value.assembly_revision_id && !value.failure && !attentionCheck(value.check_status)
        && !attentionCheck(value.verification?.check_status) && !value.unplaced_definitions.length
        && (!value.coverage || value.coverage.status === 'complete') && !value.decisions?.with_problems
        && value.definitions.every((definition) => definition.status === 'built' && !definition.error && !definition.questions?.length);
    case 'drawing_package':
      return value.completeness === 'complete' && !!value.package_id && !value.error
        && (!value.coverage || value.coverage.status === 'complete')
        && (value.subassembly_packages ?? []).every((item) => item.completeness === 'complete' && !item.error);
    case 'delivery':
      return value.completeness === 'complete' && !value.missing.length && !attentionCheck(value.check_status)
        && !attentionCheck(value.verification.check_status) && !value.decisions?.with_problems
        && !(value.verification.source_checks ?? []).some((source) => attentionCheck(source.check_status))
        && (!value.coverage || value.coverage.status === 'complete');
  }
}

function assemblyDetailsSummary(op: AiOperation): { count: number; pending: number; unverified: boolean } | null {
  const result = op.result;
  const saved = result?.cad_result;
  if (op.status !== 'succeeded' || op.error || op.question || (op.run && op.run.status !== 'succeeded')
    || saved?.object_type !== 'assembly' || result?.kind !== 'assembly' || !result.chain) return null;
  const chain = result.chain;
  if (chain.run_status !== 'succeeded' || chain.questions.length
    || chain.steps.some((step) => step.error || !['succeeded', 'skipped'].includes(step.status)
      || (step.value ? !completedChainValue(step.value) : step.status !== 'skipped'))) return null;
  const assembly = chain.steps.map((step) => step.value).find((value) => value?.type === 'assembly');
  if (assembly?.assembly_id !== saved.object_id || assembly.assembly_revision_id !== saved.version_id) return null;
  if (result.items.some((item) => attentionCheck(item.check_status))) return null;
  for (const value of Object.values(saved.checks)) {
    if (!value || typeof value !== 'object') continue;
    const check = value as { latest?: { status?: string }; status?: string };
    if (attentionCheck(check.latest?.status ?? check.status)) return null;
  }
  const delivery = chain.steps.map((step) => step.value).find((value) => value?.type === 'delivery');
  const decisions = delivery?.decisions ?? assembly.decisions;
  const assumptions = chain.assumptions.filter((item) => !isLegacyModelNote({ ...item, kind: 'assumption' }));
  const counts = decisions ? { count: decisions.count, pending: decisions.needs_confirmation }
    : { count: assumptions.length, pending: assumptions.filter((item) => decisionState(item) === 'adopted').length };
  return { ...counts, unverified: !!(assembly.verification?.unverified || delivery?.verification.unverified) };
}

/** Open the exact operation/step's disclosures before focusing it, including a journey's activity record. */
export function revealOperation(root: ParentNode, opId: string, stepIndex?: number | null): HTMLElement | null {
  const card = [...root.querySelectorAll<HTMLElement>('[data-op-id]')].find((el) => el.dataset.opId === opId);
  if (!card) return null;
  const el = stepIndex != null && Number.isInteger(stepIndex)
    ? card.querySelector<HTMLElement>(`[data-step-index="${stepIndex}"]`) ?? card
    : card;
  for (let parent = el.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') (parent as HTMLDetailsElement).open = true;
  }
  const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  el.focus({ preventScroll: true });
  el.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
  card.classList.remove('as-flash');
  void card.offsetWidth; // restart the highlight when clicked again
  card.classList.add('as-flash');
  window.setTimeout(() => card.classList.remove('as-flash'), 1800);
  return el;
}

/** Bring a retry or sub-agent into view within the same conversation. */
export function revealOpCard(e: MouseEvent<HTMLElement>, opId: string) {
  revealOperation(e.currentTarget.closest('.as-messages') ?? document, opId);
}

/**
 * The actions of an operation card (also the main agent's turn card): one at a time (`busy` names it), errors shown
 * on the card. Cancel reports a stop that is still pending; a retry keeps its client_request_id (across remounts and
 * reloads) until the server confirmed it, so a retry re-sent after a lost response returns the same request.
 */
export function useOpActions(op: AiOperation, newer: string | null) {
  const tasksEnabled = useFeature('tasks');
  const canRetry = tasksEnabled || !(op.task || op.request.task_id || op.kind === 'task_draft');
  const { store, bumpList, requestIds } = useAssistantSession();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [cancelAsked, setCancelAsked] = useState(false);

  // Once the server shows the retry, its kept client_request_id is no longer needed.
  useEffect(() => {
    if (newer) requestIds.forget(retryScope(op.id));
  }, [newer, op.id, requestIds]);

  const act = async (kind: string, fn: () => Promise<unknown>) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const cancel = () =>
    act('cancel', async () => {
      const res = await store.cancel(op.id);
      setCancelAsked(!isTerminal(res));
    });

  const retry = () =>
    act('retry', async () => {
      if (!canRetry) return;
      const scope = retryScope(op.id);
      try {
        await store.retry(op.id, requestIds.take(scope));
        requestIds.forget(scope);
        bumpList();
      } catch (e) {
        if (isApiError(e) && e.code === 'already_retried') {
          // Retried elsewhere (another tab / device): fetch the newer request so the card points to it.
          requestIds.forget(scope);
          store.reload();
        }
        throw e;
      }
    });

  return { busy, error, cancelAsked, act, cancel, retry, canRetry };
}

export function OperationCard({ op, retriedBy, archived, presentation = 'standalone' }: { op: AiOperation; retriedBy?: string | null; archived: boolean; presentation?: OperationPresentation }) {
  const { t } = useI18n();
  const { store, bumpList } = useAssistantSession();
  const newer = retriedBy ?? op.retried_by ?? null;
  const { busy, error, cancelAsked, act, cancel, retry, canRetry } = useOpActions(op, newer);

  const outcome = planOutcome(op);
  const run = op.run;
  const execution = useRunProgress(run?.id, { active: !isTerminal(op), refreshKey: op.status, followResults: true });
  const runLink = run ? paths.run(run.id) : null;
  const basis = op.request.target;
  const journey = presentation === 'journey';
  const role = roleForOperation(op.kind);
  const assemblySummary = assemblyDetailsSummary(op);
  const conversation = store.detail?.conversation;
  const delivery = savedResultPresentation(op, conversation?.id === op.conversation_id ? conversation?.project.id : null, execution.progress);
  // Completed assembly progress belongs to the supporting records; its async load must not shift the saved preview.
  const progress = run ? (
    <OperationProgress {...execution}>
      {!isTerminal(op) && op.status !== 'waiting_input' && (
        <p className="as-op-progress" role="status">
          {op.status === 'pending' ? t('as.op.pendingNote') : t('as.op.runningNote')}
        </p>
      )}
    </OperationProgress>
  ) : (
    !isTerminal(op) && op.status !== 'waiting_input' && (
      <p className="as-op-progress" role="status">
        {op.status === 'pending' ? t('as.op.pendingNote') : t('as.op.runningNote')}
      </p>
    )
  );
  const supportingResults = <>
      {/* W8: the chain of an assembly request, whatever its status (a partial delivery is shown, never as success) */}
      {op.kind === 'assembly_create' && op.result && op.result.kind === 'assembly' && op.result.chain && (
        <AssemblyChainView
          chain={op.result.chain}
          // generation supplement B: "Continue remaining work" on a partial assembly continues this request's run from the assembly step
          onContinue={
            op.status === 'failed' && !newer && !archived
              ? async () => {
                  await continueAiOperation(op.id);
                  store.reload();
                  bumpList();
                }
              : undefined
          }
        />
      )}
      {op.status === 'succeeded' && assemblyModifyResult(op) && <AssemblyModifyCard op={op} plan={assemblyModifyResult(op)!} archived={archived} />}
      {op.status === 'succeeded' && op.result && !workflowEditResult(op) && !workflowCreateResult(op) && !assemblyModifyResult(op) && !draftingModifyResult(op) && (
        <div className="as-results">
          {op.result.kind === 'task_draft' ? (
            <TaskDraftResult op={op} draft={op.result.draft} archived={archived} />
          ) : (
            <>
              {op.result.kind === 'spec_extract' && op.result.spec && <section className="as-spec-result stack-sm">
                <p className="as-op-note">{t('core.spec.extracted')}</p>
                <ChainValueView value={op.result.spec} />
                {op.result.spec.readiness && <p className="small">{t('core.spec.readiness', { buildable: op.result.spec.readiness.buildable_definitions.length, blocked: op.result.spec.readiness.blocked_definitions.length })}</p>}
              </section>}
              {/* CAD results: the independent geometry check, the drawing source and the drawing package, each with its own verdict. */}
              {!delivery.primary && showsCheck(op) && <CheckSummaryView summary={op.result.check} />}
              {op.result.kind === 'reconstruct' && <ReconstructSourceView source={op.result.source} />}
              {delivery.remaining.map((item: AiResultItem) => (
                <ResultCard key={item.ref.key} op={op} item={item} archived={archived} />
              ))}
              {(op.result.drawings ?? []).filter((d) => d !== delivery.primaryDrawing?.brief).map((d) => (
                <DrawingPackageCard key={d.drawing_id} brief={d} partId={op.part?.id} partNumber={op.part?.number} />
              ))}
            </>
          )}
          {!assemblySummary && !delivery.primary && <p className="as-op-note">{t('as.result.notAccepted')}</p>}
        </div>
      )}
  </>;

  return (
    <article className={`as-op-card st-${op.status}${journey ? ' as-op-journey' : ''}`} data-op-id={op.id} tabIndex={-1} aria-label={`${opKindLabel(t, op.kind)} · ${outcome ? t(`core.plan.${outcome}`) : t(`as.status.${op.status}`)}`}>
      <header className="as-op-h">
        {role && (isCoordinatedOperation(op.kind)
          ? <span className="agent-coordination"><AgentRoleBadge role={role} compact /><span>{t('as.op.coordinator')}</span></span>
          : <AgentRoleBadge role={role} compact />)}
        <span className="as-op-kind">{opKindLabel(t, op.kind)}</span>
        <OperationStatusChip op={op} />
        {op.simulated && <SimChip />}
        {op.execution_policy === 'turbo' && <PolicyChip policy="turbo" />}
        {op.execution_policy === 'turbo' && <PolicySourceTag source={op.policy_source} sourceVersion={op.request?.policy_source_version} />}
        {/* S6: a failed operation (its run failed, products shown below) never reads "done · not verified". */}
        {op.execution_policy === 'turbo' && op.status === 'succeeded' && op.result && outcome !== 'plan_ready' && <UnverifiedChip />}
        <span className="grow" />
        {runLink && (
          <Link className="btn sm ghost" to={runLink} title={run?.title || t('as.op.openRun')}>
            <ExternalLink /> {t('as.op.openRun')}
          </Link>
        )}
      </header>
      {/* generation reliability N: policy, its source, input versions, target and budget of the frozen request */}
      {!journey && <ExecutionContextLine ctx={operationExecutionContext(op)} />}

      {(basis || op.task || op.part) && (
        <div className="as-op-basis">
          {basis && (
            <span className="as-op-basis-item">
              <span className="faint">{op.kind === 'model_modify' ? t('as.op.base') : t('as.op.target')}</span>
              <RefChip refItem={basis} compact target />
            </span>
          )}
          {op.part && !basis && (
            <span className="as-op-basis-item">
              <span className="faint">{t('as.op.part')}</span> {op.part.number ? <span className="mono">{op.part.number}</span> : null} {op.part.name}
            </span>
          )}
          {op.task && (
            <span className="as-op-basis-item">
              <span className="faint">{t('as.op.task')}</span>
              <Link to={paths.task(op.task.id)}>{op.task.title}</Link>
            </span>
          )}
        </div>
      )}

      {/* Generation supplement A-01: a file the server bound because the request pointed at it (exact version, why) */}
      {(op.request?.auto_references ?? []).length > 0 && (
        <p className="as-op-note">
          <Paperclip className="inline-ico" aria-hidden />{' '}
          {(op.request.auto_references ?? [])
            .map((a) => t('as.attach.autoLine', { file: fileLabel(a), reason: tk(t, `as.attach.reason.${a.reason}`, a.reason) }))
            .join('；')}
        </p>
      )}

      {/* Generation supplement I (A-04): the choice next to a referenced spec version, and the files the request continued with */}
      <SpecChoiceLines op={op} />

      {/* The run's execution process (stages, parts, calls, decisions); the note stays until it loads. */}
      {!assemblySummary && progress}
      {!isTerminal(op) && (cancelAsked || op.run?.cancel_requested) && (
        <p className="as-op-progress" role="status">
          {t('as.op.cancelAsked')}
        </p>
      )}

      {op.status === 'waiting_input' && op.question?.kind === 'clarification' && (
        <Clarification op={op} disabled={archived || busy !== null} onAnswer={(answers) => void act('answer', () => store.answer(op.id, answers))} />
      )}
      {op.status === 'waiting_input' && op.question?.kind === 'manual_entry' && (
        <div className="callout warn slim">
          <Hand aria-hidden />
          <div className="grow">
            <div>{t('as.op.manualEntry')}</div>
            {op.question.reason && <div className="faint small">{op.question.reason}</div>}
            <Link className="linkish small" to={op.question.run_path || runLink || '#'}>
              {t('as.op.manualEntryGo')}
            </Link>
          </div>
        </div>
      )}

      {op.status === 'failed' && op.error && <OpFailure op={op} />}
      {/* Generation supplement I (IN-08): the spec step could not read a source — continue in the same run with other files */}
      {!newer && canReplaceSource(op) && (
        <ReplaceSource
          op={op}
          composerRefs={store.detail?.composer?.references ?? []}
          projectId={store.detail?.conversation.project.id ?? null}
          disabled={archived}
          onDone={() => {
            store.reload();
            bumpList();
          }}
        />
      )}
      {/* S3: an assembly request continues from its failed step in its own run (what is done is kept) */}
      {op.status === 'failed' && op.kind === 'assembly_create' && op.error && !newer && !canReplaceSource(op) && (
        <ContinueAction
          failure={op.error}
          canWrite={!archived}
          also={['re_extract']}
          onContinue={async () => {
            await continueAiOperation(op.id);
            store.reload();
            bumpList();
          }}
        />
      )}
      {op.status === 'cancelled' && <p className="as-op-progress">{t('as.op.cancelledNote')}</p>}

      {op.result?.cad_result && <CadResultCard key={`${op.result.cad_result.object_type}:${op.result.cad_result.object_id}:${op.result.cad_result.version_id}`}
        op={op} result={op.result.cad_result} archived={archived} item={delivery.primary} drawing={delivery.primaryDrawing}
        checkSummary={delivery.primary && showsCheck(op) && 'check' in op.result ? op.result.check : undefined} />}
      {op.status === 'succeeded' && draftingModifyResult(op) && <DraftingPlanCard op={op} plan={draftingModifyResult(op)!} archived={archived} />}
      {op.status === 'succeeded' && workflowEditResult(op) && <WorkflowEditCard op={op} plan={workflowEditResult(op)!} archived={archived} />}
      {op.status === 'succeeded' && workflowCreateResult(op) && <WorkflowCreateCard op={op} plan={workflowCreateResult(op)!} archived={archived} />}
      {execution.progress && <ProgressResults progress={execution.progress} representedParts={delivery.representedParts}
        representedDrawings={delivery.primaryDrawing ? [delivery.primaryDrawing] : []} />}
      {assemblySummary ? <>
        <div className="as-op-note stack-sm">
          {!delivery.primary && <span>{t('as.result.notAccepted')}</span>}
          {assemblySummary.count > 0 && <span>{t('cov.decisions', { n: assemblySummary.count, pending: assemblySummary.pending })}</span>}
          {assemblySummary.unverified && <div><UnverifiedChip /></div>}
        </div>
        <details className="as-result-support" key={op.result?.cad_result?.version_id}>
          <summary>{t('core.result.assemblyDetails')}</summary>
          {progress}
          {supportingResults}
        </details>
      </> : supportingResults}
      {op.status === 'succeeded' && !op.result && op.kind !== 'ask' && <p className="as-op-progress">{t('as.result.none')}</p>}

      {journey ? (
        <details className="as-op-disclosure as-op-technical">
          <summary>{t('as.journey.technical')}</summary>
          <div className="as-op-disclosure-body">
            <ExecutionContextLine ctx={operationExecutionContext(op)} />
            {op.context_build && <BuildLine build={op.context_build} />}
          </div>
        </details>
      ) : op.context_build && <BuildLine build={op.context_build} />}

      <div className="as-op-actions">
        {!isTerminal(op) && CANCELLABLE.has(op.kind) && (run || RUNLESS_CANCELLABLE.has(op.kind)) && (
          <button type="button" className="btn sm" onClick={() => void cancel()} disabled={busy !== null || archived || !!op.run?.cancel_requested}>
            <Square /> {busy === 'cancel' ? t('as.op.cancelling') : t('as.op.cancel')}
          </button>
        )}
        {canRetry && (op.status === 'failed' || op.status === 'cancelled') && !newer && (
          <button type="button" className="btn sm" onClick={() => void retry()} disabled={busy !== null || archived} title={t('as.op.retryHint')}>
            <RotateCcw /> {busy === 'retry' ? t('as.op.retrying') : t('as.op.retry')}
          </button>
        )}
        {newer && (
          <button type="button" className="linkish small as-op-newer" onClick={(e) => revealOpCard(e, newer)}>
            <ArrowDownCircle aria-hidden /> {t('as.op.retriedGo')}
          </button>
        )}
        {op.retry_of && <span className="faint small">{t('as.op.isRetry')}</span>}
      </div>
      <AsError error={error} />
    </article>
  );
}

function SpecChoiceLines({ op }: { op: AiOperation }) {
  const { t } = useI18n();
  const choice = op.request?.spec_choice;
  const revs = op.request?.input_revisions ?? [];
  if (!choice && !revs.length) return null;
  const labels = requestFileLabels(op);
  const none = t('rv.none');
  return (
    <>
      {choice?.choice === 'revise' && (
        <p className="as-op-note">
          <Paperclip className="inline-ico" aria-hidden /> {t('rv.card.revise', { files: [...choiceFiles(choice), t('rv.card.reviseNote')].join('、') })}
        </p>
      )}
      {choice?.choice === 'keep' && (
        <p className="as-op-note">
          <Paperclip className="inline-ico" aria-hidden />{' '}
          {t('rv.card.keep', { files: [...choiceFiles(choice), ...(choice.text_changes ? [t('rv.card.keepText')] : [])].join('、') || none })}
        </p>
      )}
      {revs.map((r) => (
        <p key={r.n} className="as-op-note">
          <Paperclip className="inline-ico" aria-hidden />{' '}
          {t('rv.card.inputRevision', {
            n: r.n,
            added: r.added.map((a) => `${a.name} v${a.version}`).join('、') || none,
            dropped: r.dropped.map((f) => labels[f] ?? f).join('、') || none,
          })}
        </p>
      ))}
    </>
  );
}

function Clarification({ op, disabled, onAnswer }: { op: AiOperation; disabled: boolean; onAnswer: (answers: string[]) => void }) {
  const { t } = useI18n();
  const question = op.question?.kind === 'clarification' ? op.question : null;
  const questions = question?.questions ?? [];
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ''));
  const qKey = questions.join('\n');
  useEffect(() => {
    setAnswers(questions.map(() => ''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qKey]);
  const canAnswer = question?.can_answer !== false;
  return (
    <form
      className="as-clarify"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled && canAnswer && answers.every((a) => a.trim())) onAnswer(answers.map((a) => a.trim()));
      }}
    >
      <div className="as-clarify-h">
        <Hand aria-hidden /> {t('as.op.questions')}
      </div>
      <ClarificationGaps gaps={question?.gaps} />
      {questions.map((q, i) => (
        <label key={i} className="as-clarify-q">
          <span>
            <ChevronRight className="inline-ico" aria-hidden /> {q}
          </span>
          <input className="input sm" value={answers[i] ?? ''} onChange={(e) => setAnswers((a) => a.map((x, j) => (j === i ? e.target.value : x)))} disabled={disabled || !canAnswer} required />
        </label>
      ))}
      {canAnswer ? (
        <div>
          <button type="submit" className="btn sm primary" disabled={disabled || answers.some((a) => !a.trim())}>
            <Send /> {t('as.op.answer')}
          </button>
        </div>
      ) : (
        <p className="faint small">{t('as.op.cannotAnswer')}</p>
      )}
    </form>
  );
}

export function OpFailure({ op }: { op: AiOperation }) {
  const { t } = useI18n();
  const err = op.error!;
  const d = (err.details ?? {}) as { status?: string };
  // Assistant wording first, then the shared CAD / API error vocabulary (error.<code>).
  const translated = tk(t, `as.err.${err.code}`, tk(t, `error.${err.code}`, ''));
  const cad = cadErrorFamily(err.code) !== 'other';
  // S5: a standard request on a Turbo spec — the values to confirm, right here (then retry the request).
  const confirm = confirmSourceOf(err);
  const confirmProject = [op.request?.target, ...(op.request?.references ?? [])].find((r) => r?.project_id)?.project_id;
  // Generation supplement A-02: the sources that were not read, each with its state and error.
  const unread = err.code === 'source_unread' && Array.isArray(err.details?.sources) ? (err.details.sources as SpecSourceBrief[]) : [];
  return (
    <div className="callout err slim as-op-fail" role="alert">
      <AlertTriangle aria-hidden />
      <div className="grow stack-sm">
        <div>
          {translated || err.message || err.code} <code className="error-code">{t('common.errorCode', { code: err.code })}</code>
        </div>
        {translated && err.message && err.code !== 'context_over_budget' && <div className="faint small">{err.message}</div>}
        {(err.code === 'context_over_budget' || err.code === 'material_not_included') && <OverBudget details={err.details} />}
        {unread.length > 0 && (
          <ul className="as-plain small">
            {unread.map((s, i) => (
              <li key={s.id ?? s.file_version_id ?? i}>
                {s.label || s.name || s.file_version_id} · {t(`as.src.${sourceState(s)}`)}
                {s.error?.code && <code className="error-code">{s.error.code}</code>}
              </li>
            ))}
          </ul>
        )}
        {err.code === 'task_not_writable' && d.status && <div className="small">{t('as.err.taskStatus', { status: tk(t, `tk.status.${d.status}`, d.status) })}</div>}
        {cad && <CadErrorDetails code={err.code} details={err.details} action={<div className="faint small">{t('cad.as.err.reconstructHint')}</div>} />}
        {op.diagnostic && <AiFailureDetails diagnostic={op.diagnostic} />}
        <AiFailureCauses details={err.details} />
        {confirm && confirmProject && (
          <UpstreamConfirmPanel projectId={confirmProject} source={{ source_type: confirm.source_type, source_id: confirm.source_id }} />
        )}
        <div className="faint small">{t('as.op.failedKept')}</div>
      </div>
    </div>
  );
}

export function BuildLine({ build }: { build: NonNullable<AiOperation['context_build']> }) {
  const { t } = useI18n();
  const b = build.budget;
  return (
    <>
      <p className="as-op-build">
        {build.model_label && <span className="mono">{modelLabelText(t, build.model_label)} · </span>}
        {b ? t('as.budget.line', { used: b.used_est, available: b.available }) : null}
        {b ? ' · ' : null}
        {t('as.usage.title')} {usageText(t, build.usage)}
      </p>
      {/* S2: how a material saved from the request was read (complete, digest, chunks, spec step). */}
      <MaterialPlacements items={b?.materials} />
    </>
  );
}
