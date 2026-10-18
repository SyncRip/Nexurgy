import { CadResultCard } from './CadResultCard';
// A turn of the main agent (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §3): an `ask` operation run by the
// orchestrator, shown as "Main agent" with its step timeline — each tool call with its status, the object it created
// or started (a draft, part, task, sub-agent or revision: opened in the work panel, on its page or at its card) and an
// undo button while the server allows one —, "working…" with a stop button while it runs, the light confirmation card
// when a write step waits for the person, and the failure as on any operation card. The reply itself is the answer
// message below the card. Retry only for a turn the person's own request started (not a wake-up or a confirmation).

import { ArrowDownCircle,Ban,Bot,Box,Check,CheckCircle2,CircleMinus,ClipboardPen,Hand,Layers,ListChecks,Loader2,RotateCcw,Square,Undo2,Workflow,X,XCircle } from 'lucide-react';
import { useEffect,useMemo,type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { orchestratorResult,type AiConversationDetail,type AiOperation,type AiLegacyQuestion,type AiTaskDraft,type AiWork,type OrchestratorStepStatus,type OrchestratorTrigger } from '../../api/assistant';
import { useFeature } from '../../api/capabilities';
import { useI18n,type TFunction } from '../../i18n/I18nProvider';
import { AgentRoleBadge, AgentRoleIcon } from '../agent-team/AgentRole';
import { isCoordinatedOperation, roleForTool } from '../agent-team/roles';
import { useAssistantSession } from './AssistantProvider';
import { AsError,opKindLabel,OpStatusChip,SimChip,tk } from './common';
import { BuildLine,OpFailure,revealOpCard,useOpActions,type OperationPresentation } from './OperationCard';
import { confirmDraft,stepViews,subAgentOf,type StepObject,type StepTarget,type StepView } from './orchestrator';
import { useWorkPanel } from './panel/WorkPanelContext';
import { DesignChoiceCard } from './DesignChoiceCard';
import { DesignPlanSummary } from './DesignPlanSummary';
import { designChoiceScope } from './designChoices';
import { AssumptionList } from '../turbo/Turbo';

/** A tool's label: the main agent's own tools, the look-ups it shares with the assistant, else the server's title. */
export function toolLabel(t: TFunction, tool: string, fallback?: string): string {
  return tk(t, `as.orch.tool.${tool}`, tk(t, `as.consulted.tool.${tool}`, fallback || tool));
}

export function TurnCard({ op, detail, retriedBy, archived, presentation = 'standalone' }: { op: AiOperation; detail: AiConversationDetail; retriedBy?: string | null; archived: boolean; presentation?: OperationPresentation }) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const { store, userId, designDrafts } = useAssistantSession();
  const newer = retriedBy ?? op.retried_by ?? null;
  const { busy, error, cancelAsked, act, cancel, retry, canRetry } = useOpActions(op, newer);
  const steps = useMemo(() => stepViews(op, { operations: detail.operations, work: detail.work }), [op, detail.operations, detail.work]);
  const result = orchestratorResult(op);
  const statusLabel = op.status === 'succeeded' ? t('as.orch.status.succeeded') : t(`as.status.${op.status}`);
  const trigger = op.request?.trigger ?? result?.trigger ?? null;
  const active = op.status === 'pending' || op.status === 'running';
  const confirm = op.status === 'waiting_input' && op.question?.kind === 'confirm' ? op.question : null;
  const designQuestion = op.question?.kind === 'design_choice' ? op.question : null;
  const ended = op.status === 'failed' || op.status === 'cancelled';
  const designReceipt = op.result?.design_plan?.last_response;
  const journey = presentation === 'journey';
  const canUndoStep = (step: StepView) => step.undo && !archived && (tasksEnabled || (step.target?.object !== 'task' && step.target?.object !== 'draft'));
  const currentStep = (step: StepView) => !!step.error || canUndoStep(step)
    || ['running', 'failed', 'waiting_confirm', 'waiting_design'].includes(step.status)
    || (step.target?.object === 'operation' && (step.target.outcome === 'plan_ready'
      || ['pending', 'running', 'waiting_input', 'failed'].includes(step.target.status ?? '')));
  const visibleSteps = journey ? steps.filter(currentStep) : steps;
  const pastSteps = journey ? steps.filter((step) => !currentStep(step)) : [];
  const renderSteps = (items: StepView[]) => items.length > 0 && (
    <ol className="as-steps" aria-label={t('as.orch.steps')}>
      {items.map((s) => (
        <StepRow
          key={s.index}
          step={s}
          canUndo={canUndoStep(s)}
          undoing={busy === `undo:${s.index}`}
          disabled={busy !== null}
          onUndo={() => void act(`undo:${s.index}`, () => store.undoStep(op.id, s.index))}
        />
      ))}
    </ol>
  );
  useEffect(() => {
    if (!designReceipt) return;
    // A lost response may remove the question before its form remounts. The durable receipt still clears its draft.
    designDrafts.remove(designChoiceScope(op.conversation_id, op.id, { design_ref: designReceipt.answered_design_ref, question_set_id: designReceipt.question_set_id }));
  }, [designReceipt, designDrafts, op.conversation_id, op.id]);

  return (
    <article className={`as-op-card as-turn st-${op.status}${journey ? ' as-op-journey' : ''}`} data-op-id={op.id} tabIndex={-1} aria-label={`${t('team.role.manager')} · ${statusLabel}`}>
      <header className="as-op-h">
        <AgentRoleBadge role="manager" compact />
        <OpStatusChip status={op.status} label={statusLabel} />
        {(op.simulated || result?.simulated) && <SimChip />}
        <TriggerNote trigger={trigger} operations={detail.operations} />
      </header>

      {renderSteps(visibleSteps)}

      {op.result?.design_plan && <DesignPlanSummary plan={op.result.design_plan} compact={journey} />}
      {op.result?.cad_result && <CadResultCard op={op} result={op.result.cad_result} archived={archived} />}
      <AssumptionList items={result?.assumptions} />
      {active && (
        <p className="as-op-progress as-turn-working" role="status">
          <Loader2 className="spin-icon inline-ico" aria-hidden /> {t('as.orch.working')}
          {cancelAsked && ` ${t('as.orch.stopAsked')}`}
        </p>
      )}
      {confirm && (
        <ConfirmCard
          question={confirm}
          work={detail.work}
          busy={busy}
          disabled={archived || busy !== null}
          onAnswer={(accept) => void act(accept ? 'accept' : 'decline', () => store.confirm(op.id, accept))}
        />
      )}
      {designQuestion && <DesignChoiceCard key={`${userId}.${designChoiceScope(op.conversation_id, op.id, designQuestion)}`} op={op} question={designQuestion} disabled={archived || busy !== null} />}
      {op.status === 'failed' && op.error && <OpFailure op={op} />}
      {op.status === 'cancelled' && <p className="as-op-progress">{t('as.orch.stopped')}</p>}
      {pastSteps.length > 0 && (
        <details className="as-op-disclosure as-op-history">
          <summary>{t('as.journey.execution')} <span className="faint">{pastSteps.length}</span></summary>
          <div className="as-op-disclosure-body">{renderSteps(pastSteps)}</div>
        </details>
      )}
      {op.context_build && (journey ? (
        <details className="as-op-disclosure as-op-technical">
          <summary>{t('as.journey.technical')}</summary>
          <div className="as-op-disclosure-body"><BuildLine build={op.context_build} /></div>
        </details>
      ) : <BuildLine build={op.context_build} />)}

      <div className="as-op-actions">
        {(active || (op.status === 'waiting_input' && !!designQuestion)) && (
          <button type="button" className="btn sm" onClick={() => void cancel()} disabled={busy !== null || archived || cancelAsked}>
            <Square /> {busy === 'cancel' ? t('as.op.cancelling') : t('as.orch.stop')}
          </button>
        )}
        {canRetry && ended && !newer && trigger?.type === 'request' && (
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

/** Why a turn ran without a new request: a sub-agent finished (woke the main agent) or the person confirmed a step. */
function TriggerNote({ trigger, operations }: { trigger: OrchestratorTrigger | null; operations: readonly AiOperation[] }) {
  const { t } = useI18n();
  if (!trigger || trigger.type === 'request') return null;
  if (trigger.type === 'confirmed') return <span className="as-turn-trigger">{t('as.orch.trigger.confirmed')}</span>;
  if (trigger.type === 'design_choice') return <span className="as-turn-trigger">{t('as.orch.trigger.design_choice')}</span>;
  const kind = operations.find((o) => o.id === trigger.operation_id)?.kind;
  const agent = subAgentOf(kind);
  const status = tk(t, `core.plan.${trigger.outcome}`, tk(t, `as.status.${trigger.status}`, trigger.status));
  return (
    <span className="as-turn-trigger">
      {kind && isCoordinatedOperation(kind)
        ? t('as.orch.trigger.workflow', { workflow: opKindLabel(t, kind), status })
        : t('as.orch.trigger.sub_agent', { agent: agent ? t(`team.role.${agent}`) : t('as.orch.agent.generic'), status })}
    </span>
  );
}

function StepIcon({ status }: { status: OrchestratorStepStatus }) {
  switch (status) {
    case 'running':
      return <Loader2 className="spin-icon" />;
    case 'ok':
      return <CheckCircle2 />;
    case 'failed':
      return <XCircle />;
    case 'waiting_confirm':
    case 'waiting_design':
      return <Hand />;
    case 'declined':
      return <Ban />;
    case 'superseded':
      return <CircleMinus />;
    case 'undone':
      return <Undo2 />;
  }
}

function StepRow({ step, canUndo, undoing, disabled, onUndo }: { step: StepView; canUndo: boolean; undoing: boolean; disabled: boolean; onUndo: () => void }) {
  const { t } = useI18n();
  const label = toolLabel(t, step.tool, step.title);
  const role = roleForTool(step.tool);
  return (
    <li data-step-index={step.index} tabIndex={-1} className={`as-step st-${step.status}`} title={step.title || undefined}>
      <span className="as-step-ico" aria-hidden>
        <StepIcon status={step.status} />
      </span>
      <div className="as-step-main">
        <div className="as-step-line">
          {role && step.target?.agent !== role && <AgentRoleBadge role={role} compact />}
          <span className="as-step-tool">{label}</span>
          {/* Status is icon + text: "done" stays for screen readers only, every other state is written out. */}
          <span className={step.status === 'ok' ? 'sr-only' : 'as-step-st'}>{t(`as.orch.step.${step.status}`)}</span>
          {step.target && <StepChip target={step.target} />}
          {canUndo && (
            <button type="button" className="btn sm ghost as-step-undo" onClick={onUndo} disabled={disabled} title={tk(t, `as.orch.undoHint.${step.target?.object ?? ''}`, t('as.orch.undo'))}>
              <Undo2 /> {undoing ? t('as.orch.undoing') : t('as.orch.undo')}
            </button>
          )}
        </div>
        {/* The server's step message is written for the agent (English); the chip names the object, an error says why. */}
        {step.error && (
          <p className="as-step-err">
            {tk(t, `as.err.${step.error.code}`, step.error.message || step.error.code)} <code className="error-code">{step.error.code}</code>
          </p>
        )}
      </div>
    </li>
  );
}

const OBJECT_ICON: Record<StepObject, ReactNode> = {
  draft: <ClipboardPen aria-hidden />,
  part: <Box aria-hidden />,
  task: <ListChecks aria-hidden />,
  operation: <Bot aria-hidden />,
  revision: <Layers aria-hidden />,
  asset: <Layers aria-hidden />,
};

function targetText(t: TFunction, target: StepTarget): string {
  if (target.object === 'operation') {
    const who = target.operationKind && isCoordinatedOperation(target.operationKind)
      ? opKindLabel(t, target.operationKind)
      : target.agent ? `${t(`team.role.${target.agent}`)} · AI` : t('as.orch.obj.operation');
    return target.outcome ? `${who} · ${t(`core.plan.${target.outcome}`)}` : target.status ? `${who} · ${t(`as.status.${target.status}`)}` : who;
  }
  const what = t(`as.orch.obj.${target.object}`);
  return target.name ? `${what} · ${target.name}` : what;
}

/** The object of a step: its work-panel pane, else its page, else (a sub-agent without a run yet) its card. */
function StepChip({ target }: { target: StepTarget }) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const session = useAssistantSession();
  const text = targetText(t, target);
  const icon = isCoordinatedOperation(target.operationKind) ? <Workflow aria-hidden />
    : target.agent ? <AgentRoleIcon role={target.agent} /> : OBJECT_ICON[target.object];
  if (!target.panel && target.href) {
    return (
      <Link className="as-step-obj" to={target.href} onClick={session.keepDockOnLeave} title={text}>
        {icon}
        <span>{text}</span>
      </Link>
    );
  }
  return (
    <button type="button" className="as-step-obj" title={text} onClick={(e) => (target.panel ? wp.open(target.panel) : revealOpCard(e, target.id))}>
      {icon}
      <span>{text}</span>
    </button>
  );
}

/** The light confirmation: what the step will do (for create_task the draft, editable in the panel), confirm / don't. */
function ConfirmCard({ question, work, busy, disabled, onAnswer }: { question: AiLegacyQuestion; work: AiWork | undefined; busy: string | null; disabled: boolean; onAnswer: (accept: boolean) => void }) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const wp = useWorkPanel();
  const draft = confirmDraft(question, work);
  const tool = question.tool ?? '';
  const frozenTaskAction = !tasksEnabled && (['draft_task', 'update_task_draft', 'create_task'].includes(tool) || !!question.args?.task_id);
  const label = toolLabel(t, tool, question.title);
  // The server's summary is English plain text for logs; the card says the same from the draft and the linked part.
  const partId = typeof question.args?.part_id === 'string' ? question.args.part_id : draft?.part_id;
  const part = partId ? work?.parts.find((p) => p.id === partId) : undefined;
  return (
    <div className="callout warn slim as-confirm" role="group" aria-label={t('as.orch.confirm.heading', { tool: label })}>
      <Hand aria-hidden />
      <div className="grow stack-sm">
        <div className="strong">{t('as.orch.confirm.heading', { tool: label })}</div>
        {question.title && question.title !== label && <div className="small">{question.title}</div>}
        {draft && <DraftBrief draft={draft} />}
        {part && <div className="small">{t('as.orch.confirm.part', { part: [part.number, part.name].filter(Boolean).join(' · ') })}</div>}
        <div className="as-confirm-acts">
          <button type="button" className="btn sm primary" onClick={() => onAnswer(true)} disabled={disabled || frozenTaskAction}>
            <Check /> {busy === 'accept' ? t('as.orch.confirm.accepting') : tk(t, `as.orch.confirm.accept.${tool}`, t('as.orch.confirm.accept'))}
          </button>
          <button type="button" className="btn sm" onClick={() => onAnswer(false)} disabled={disabled}>
            <X /> {tk(t, `as.orch.confirm.decline.${tool}`, t('as.orch.confirm.decline'))}
          </button>
          {tasksEnabled && draft?.id && (
            <button type="button" className="btn sm ghost" onClick={() => wp.open({ kind: 'draft', id: draft.id })}>
              <ClipboardPen /> {t('as.orch.confirm.editDraft')}
            </button>
          )}
        </div>
        <p className="faint small">{t('as.orch.confirm.note')}</p>
      </div>
    </div>
  );
}

function DraftBrief({ draft }: { draft: Pick<AiTaskDraft, 'title' | 'objective' | 'criteria' | 'simulated'> }) {
  const { t } = useI18n();
  const criteria = draft.criteria ?? [];
  return (
    <div className="as-confirm-draft">
      <div className="as-confirm-draft-h">
        <span className="strong">{draft.title}</span>
        {draft.simulated && <SimChip />}
      </div>
      {draft.objective && <p className="as-result-text">{draft.objective}</p>}
      {criteria.length > 0 && (
        <ul className="as-plain small">
          {criteria.slice(0, 5).map((c, i) => (
            <li key={i}>
              {c.description}
              {c.required ? '' : ` (${t('as.result.optional')})`}
            </li>
          ))}
          {criteria.length > 5 && <li className="faint">{t('as.result.moreCriteria', { n: criteria.length - 5 })}</li>}
        </ul>
      )}
    </div>
  );
}
