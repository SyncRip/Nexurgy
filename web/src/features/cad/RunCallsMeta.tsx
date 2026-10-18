// Generation reliability J: two entries of the run page's meta list — the model calls (logical work units apart from
// real requests and retries, failures by class) and the run's frozen budget with what it used. Rendered inside the
// run header's <dl>; nothing when the server sent neither (runs read from an older API).

import type { WorkflowRun } from '../../api/types';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';
import { formatDuration } from '../workflow/model';
import { budgetView, callClasses } from './runCalls';

export function RunCallsMeta({ run }: { run: Pick<WorkflowRun, 'calls' | 'budget'> }) {
  const { t } = useI18n();
  const calls = run.calls;
  const budget = budgetView(run.budget);
  const classes = callClasses(calls);
  const dur = (s: number) => formatDuration(s * 1000);
  const label = (c: string) => (hasKey(`cad.fclass.${c}`) ? t(`cad.fclass.${c}` as MessageKey) : c);
  return (
    <>
      {calls && (
        <div>
          <dt>{t('cad.run.callsMeta')}</dt>
          <dd>
            {t('cad.run.callsLine', { units: calls.units, requests: calls.requests, retries: calls.retries })}
            {classes.length > 0 && (
              <span className="faint">
                {' · '}
                {t('cad.run.callsFailed', {
                  n: calls.failed,
                  classes: classes.map(([c, n]) => `${label(c)} ×${n}`).join(', '),
                })}
              </span>
            )}
          </dd>
        </div>
      )}
      {budget && (
        <div>
          <dt>{t('cad.run.budgetMeta')}</dt>
          <dd>
            {budget.calls.max === null
              ? t('cad.run.budgetCallsOpen', { used: budget.calls.used })
              : t('cad.run.budgetCalls', { used: budget.calls.used, max: budget.calls.max })}
            {' · '}
            {budget.seconds.max === null
              ? t('cad.run.budgetStep', { max: dur(budget.seconds.step) })
              : t('cad.run.budgetTime', { used: dur(budget.seconds.used), max: dur(budget.seconds.max) })}
            {budget.raised ? ` · ${t('cad.run.budgetRaised')}` : ''}
            {budget.exhausted ? ` · ${t('cad.run.budgetUsedUp')}` : ''}
          </dd>
        </div>
      )}
    </>
  );
}
