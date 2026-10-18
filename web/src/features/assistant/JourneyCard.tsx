import { History, Sparkles } from 'lucide-react';
import { useId, type ReactNode } from 'react';
import { isOrchestratorTurn, orchestratorResult, type AiConversationDetail, type AiMessage, type AiOperation } from '../../api/assistant';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from '../agent-team/AgentRole';
import { roleForOperation, roleForTool } from '../agent-team/roles';
import { opKindLabel } from './common';
import { journeyAnswerRole, operationHasFailure, type GenerationJourney } from './journey';
import { JourneyStatusChip } from './JourneyNavigator';
import { isTerminal } from './model';
import { OperationCard } from './OperationCard';
import { planOutcome } from './planApply';
import { toolLabel, TurnCard } from './TurnCard';

/** Only finished orchestration with no current action/result belongs in the activity record. */
export function prominentJourneyOperation(op: AiOperation, journey: GenerationJourney): boolean {
  if (!isTerminal(op) || op.question || !isOrchestratorTurn(op)) return true;
  if (operationHasFailure(op) && journey.status !== 'generated' && journey.status !== 'complete' && journey.status !== 'plan_ready') return true;
  if (op.result?.cad_result || planOutcome(op)) return true;
  const steps = orchestratorResult(op)?.steps ?? [];
  if (steps.some((step) => step.undo || step.status === 'waiting_confirm' || step.status === 'waiting_design')) return true;
  return (journey.status === 'failed' || journey.status === 'partial' || journey.status === 'cancelled')
    && (journey.focusOperationId === op.id || op.status === 'failed' || op.status === 'cancelled');
}

export function JourneyCard({ journey, detail, retried, renderMessage }: {
  journey: GenerationJourney;
  detail: AiConversationDetail;
  retried: ReadonlyMap<string, string>;
  renderMessage: (message: AiMessage, historical?: boolean) => ReactNode;
}) {
  const { t } = useI18n();
  const headingId = useId();
  const answerRole = journeyAnswerRole(journey);
  const answerLabel = t(`as.journey.answer.${answerRole}`);
  const showStateHint = !(journey.status === 'generated' && journey.hasSavedResult && answerRole === 'delivery');
  const prominent = new Set(journey.operations.filter((op) => prominentJourneyOperation(op, journey)).map((op) => op.id));
  const operations = new Map(journey.operations.map((op) => [op.id, op]));
  const cardMessages = new Map<string, AiMessage>();
  for (const message of journey.messages) {
    if (message.kind === 'operation' && message.operation_id && operations.has(message.operation_id) && !cardMessages.has(message.operation_id)) {
      cardMessages.set(message.operation_id, message);
    }
  }
  const isCardMessage = (m: AiMessage) => !!m.operation_id && cardMessages.get(m.operation_id)?.id === m.id;
  // Flow cards may carry actions, so they never enter the collapsed activity record.
  const visibleMessages = journey.messages.filter((m) => !isCardMessage(m) && (m.kind === 'card' || m.kind === 'choice') && m.id !== journey.latestAnswer?.id);
  const visibleIds = new Set(visibleMessages.map((m) => m.id));
  const activity = journey.messages.filter((m) => m.id !== journey.latestAnswer?.id && !visibleIds.has(m.id)
    && (!isCardMessage(m) || !prominent.has(m.operation_id!)));
  const unmessaged = journey.operations.filter((op) => !cardMessages.has(op.id) && !prominent.has(op.id));
  const currentOp = journey.operations.find((op) => op.id === journey.focusOperationId);
  const currentStep = currentOp ? [...(orchestratorResult(currentOp)?.steps ?? [])].sort((a, b) => b.index - a.index)
    .find((step) => step.status === 'running' || step.status === 'waiting_confirm' || step.status === 'waiting_design') : undefined;
  const currentActivity = currentStep ? toolLabel(t, currentStep.tool, currentStep.title)
    : currentOp && journey.status === 'running' ? opKindLabel(t, currentOp.kind) : null;
  const currentRole = currentStep ? roleForTool(currentStep.tool) : currentOp && journey.status === 'running' ? roleForOperation(currentOp.kind) : null;

  const renderOperation = (op: AiOperation) => <div className="as-journey-operation" key={op.id} data-message-id={cardMessages.get(op.id)?.id}>
    {isOrchestratorTurn(op)
      ? <TurnCard op={op} detail={detail} retriedBy={retried.get(op.id) ?? null} archived={detail.conversation.archived} presentation="journey" />
      : <OperationCard op={op} retriedBy={retried.get(op.id) ?? null} archived={detail.conversation.archived} presentation="journey" />}
  </div>;
  const renderSavedMessage = (m: AiMessage, historical = false) => <div key={m.id} data-message-id={m.id}>
    {/* Extra historical operation messages remain readable without mounting their form a second time. */}
    {m.kind === 'operation' && m.operation_id && operations.has(m.operation_id)
      ? <p className="as-notice">{m.text || opKindLabel(t, operations.get(m.operation_id)!.kind)}</p>
      : renderMessage(m, historical)}
  </div>;

  return <article className={`as-journey st-${journey.status}`} data-journey-id={journey.id} data-journey-state={journey.status} tabIndex={-1} aria-labelledby={headingId}>
    <header className="as-journey-head">
      <div className="as-journey-heading">
        <h2 id={headingId}><Sparkles aria-hidden />{t('as.journey.task')}</h2>
        <span className="agent-coordination"><AgentRoleBadge role="manager" compact /><span>{t('as.journey.coordinator')}</span></span>
      </div>
      <JourneyStatusChip status={journey.status} />
    </header>
    <div className="as-journey-request" data-message-id={journey.request.id}>{renderMessage(journey.request)}</div>
    {showStateHint && <div className="as-journey-state" role="status">
      {currentActivity && <div className="as-journey-current"><span className="faint">{t('as.journey.currentActivity')}</span>{currentRole && currentRole !== 'manager' && <AgentRoleBadge role={currentRole} compact />}<strong>{currentActivity}</strong></div>}
      <p>{t(`as.journey.hint.${journey.status}`)}</p>
    </div>}
    <div className="as-journey-work">
      {journey.operations.filter((op) => prominent.has(op.id)).map(renderOperation)}
      {visibleMessages.map((m) => renderSavedMessage(m))}
    </div>
    {journey.latestAnswer && <section className="as-journey-answer" data-journey-answer={journey.latestAnswer.id} data-answer-role={answerRole} data-message-id={journey.latestAnswer.id} tabIndex={-1} aria-label={answerLabel}>
      <h3>{answerLabel}</h3>
      {renderMessage(journey.latestAnswer)}
    </section>}
    {(activity.length > 0 || unmessaged.length > 0) && <details className="as-journey-activity">
      <summary><History aria-hidden /><span>{t('as.journey.activity')}</span><span className="faint">{activity.length + unmessaged.length}</span></summary>
      <div className="as-journey-activity-body">
        {activity.map((m) => isCardMessage(m) ? renderOperation(operations.get(m.operation_id!)!) : renderSavedMessage(m, true))}
        {unmessaged.map(renderOperation)}
      </div>
    </details>}
  </article>;
}
