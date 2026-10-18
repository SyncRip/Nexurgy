import { Bot, GitBranch, UserRound } from 'lucide-react';
import type { DesignPlanSummary as DesignPlan, DesignValue } from '../../api/assistant';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import './operationPresentation.css';

export function designValueText(value: DesignValue, t: TFunction): string {
  return typeof value === 'boolean' ? t(value ? 'as.design.yes' : 'as.design.no') : String(value);
}

/** Saved design inputs and origins, independent of generated CAD results and engineering acceptance. */
export function DesignPlanSummary({ plan, compact = false }: { plan: DesignPlan; compact?: boolean }) {
  const { t } = useI18n();
  const content = (
    <section className="as-design-plan" aria-label={t('as.design.plan')}>
      <div className="as-design-heading">
        <GitBranch aria-hidden />
        <strong>{plan.title || t('as.design.plan')}</strong>
        <span className="tag">{t(`as.design.status.${plan.status}`)}</span>
        <span className="faint small">{t('as.design.version', { n: plan.ref.version })}</span>
      </div>
      {plan.summary && <p className="small as-design-copy">{plan.summary}</p>}
      <p className="small faint">{t('as.design.structure', { assemblies: plan.structure.assembly_definitions, parts: plan.structure.part_definitions, instances: plan.structure.instances })}</p>
      {plan.decisions.length > 0 && (
        <ul className="as-design-decisions">
          {plan.decisions.map((decision) => (
            <li key={decision.id}>
              <div className="as-design-decision-line">
                <strong>{decision.label}</strong>
                <span>{designValueText(decision.value, t)}{decision.unit ? ` ${decision.unit}` : ''}</span>
                <span className="as-design-origin">
                  {decision.origin === 'ai_design' ? <Bot aria-hidden /> : <UserRound aria-hidden />}
                  {t(`as.design.origin.${decision.origin}`)}
                </span>
              </div>
              {decision.reason && <p className="faint small as-design-copy">{decision.reason}</p>}
            </li>
          ))}
        </ul>
      )}
      {plan.questions.length > 0 && (
        <details className="as-design-history">
          <summary>{t('as.design.savedQuestions', { n: plan.questions.length })}</summary>
          <ul className="as-design-decisions">
            {plan.questions.map((q) => (
              <li key={q.id}>
                <strong className="small">{q.label}{q.unit ? ` (${q.unit})` : ''}</strong>
                {q.description && <p className="small faint as-design-copy">{q.description}</p>}
                {q.options.length > 0 && <ul className="small as-design-saved-options">{q.options.map((option) => <li key={option.id}>{option.label}{q.recommended?.kind === 'option' && q.recommended.option_id === option.id ? ` · ${t('as.design.recommended')}` : ''}{option.reason ? ` — ${option.reason}` : ''}</li>)}</ul>}
                {q.recommended?.kind === 'value' && <p className="small faint">{t('as.design.recommendedValue', { value: `${designValueText(q.recommended.value, t)}${q.unit ? ` ${q.unit}` : ''}` })}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {plan.last_response && <p className="small faint" role="status">{t(`as.design.receipt.${plan.last_response.action}`)}</p>}
    </section>
  );
  if (!compact || plan.status === 'awaiting_choice') return content;
  return (
    <details className="as-op-disclosure as-op-design">
      <summary>
        {t('as.journey.design')}
        <span className="as-op-design-title">{plan.title || t('as.design.plan')}</span>
        <span className="tag">{t(`as.design.status.${plan.status}`)}</span>
      </summary>
      {content}
    </details>
  );
}
