// Task pane: a compact summary of one task — objective, inputs (the adopted baseline's slots and exact versions),
// deliverable drafts and the acceptance criteria with their current check (a dry run of the submission checks).
// Changing any of it, submitting and approving stay on the task page ("open full page").

import { CheckCircle2, CircleDashed, ListChecks, XCircle } from 'lucide-react';
import { getTask, getTaskChecks, type CriterionCheck } from '../../../api/pm';
import type { UUID } from '../../../api/types';
import { ErrorState } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { problemText } from '../../tasks/ChecksView';
import { BlockedChip, SubmissionStatusChip, TaskStatusChip, VersionRef } from '../../tasks/common';
import { fullPageHref } from './model';
import { PaneFrame, PaneLoading } from './PaneFrame';

function CheckMark({ check }: { check: CriterionCheck | undefined }) {
  const { t } = useI18n();
  if (!check) {
    return (
      <span className="wp-check pending" title={t('wp.task.unchecked')}>
        <CircleDashed aria-hidden /> <span className="sr-only">{t('wp.task.unchecked')}</span>
      </span>
    );
  }
  return check.ok ? (
    <span className="wp-check ok" title={t('wp.task.met')}>
      <CheckCircle2 aria-hidden /> <span className="sr-only">{t('wp.task.met')}</span>
    </span>
  ) : (
    <span className="wp-check err" title={t('wp.task.notMet')}>
      <XCircle aria-hidden /> <span className="sr-only">{t('wp.task.notMet')}</span>
    </span>
  );
}

export function TaskPane({ taskId }: { taskId: UUID }) {
  const { t, tn } = useI18n();
  const taskRes = useResource((signal) => getTask(taskId, { signal }), [taskId]);
  // The dry run is informative only (it may be refused, e.g. for viewers): the summary works without it.
  const checksRes = useResource((signal) => getTaskChecks(taskId, { signal }), [taskId]);
  const task = taskRes.data;

  const icon = <ListChecks />;
  const kind = t('wp.kind.task');
  const fullHref = fullPageHref({ kind: 'task', id: taskId });
  if (!task) {
    if (taskRes.error) {
      return (
        <PaneFrame icon={icon} kind={kind} title={kind} fullHref={fullHref}>
          <div className="wp-pad">
            <ErrorState error={taskRes.error} onRetry={taskRes.reload} />
          </div>
        </PaneFrame>
      );
    }
    return <PaneLoading icon={icon} kind={kind} />;
  }

  const checks = new Map((checksRes.data?.criteria ?? []).map((c) => [c.criterion_id, c]));
  const baseline = task.baseline;
  const sub = task.latest_submission;

  return (
    <PaneFrame
      icon={icon}
      kind={kind}
      title={`${task.key} · ${task.title}`}
      chips={
        <>
          <TaskStatusChip status={task.status} />
          {task.blocked_reason && <BlockedChip reason={task.blocked_reason} />}
          {sub && <SubmissionStatusChip status={sub.status} />}
        </>
      }
      fullHref={fullHref}
    >
      <div className="wp-pad wp-task">
        <section className="wp-sec">
          <h3>{t('wp.task.objective')}</h3>
          <p className={task.objective ? '' : 'faint'}>{task.objective || t('wp.task.noObjective')}</p>
        </section>

        <section className="wp-sec">
          <h3>
            {t('wp.task.inputs')}
            {baseline && <span className="mono faint"> #{baseline.seq}</span>}
          </h3>
          {baseline && baseline.items.length ? (
            <ul className="wp-rows">
              {baseline.items.map((it) => (
                <li key={`${it.slot}:${it.asset_version_id}`}>
                  <span className="mono faint wp-slot">{it.slot}</span>
                  <VersionRef v={it.version} compact />
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('wp.task.noInputs')}</p>
          )}
        </section>

        <section className="wp-sec">
          <h3>{t('wp.task.deliverables')}</h3>
          {task.deliverables.length ? (
            <ul className="wp-rows">
              {task.deliverables.map((d) => (
                <li key={d.id}>
                  <VersionRef v={d.version} compact />
                  {d.criterion_ids.length > 0 && <span className="faint small">{tn('wp.task.covers', d.criterion_ids.length, { n: d.criterion_ids.length })}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('wp.task.noDeliverables')}</p>
          )}
        </section>

        <section className="wp-sec">
          <h3>
            {t('wp.task.criteria')}
            {checksRes.data && <span className={`wp-task-verdict ${checksRes.data.ok ? 'ok' : 'err'}`}>{checksRes.data.ok ? t('wp.task.allMet') : t('wp.task.someOpen')}</span>}
          </h3>
          {task.criteria.length ? (
            <ul className="wp-crit">
              {task.criteria.map((c) => {
                const ck = checks.get(c.id);
                return (
                  <li key={c.id}>
                    <CheckMark check={checksRes.data ? ck : undefined} />
                    <div className="wp-crit-main">
                      <span>
                        <span className="mono faint">#{c.seq}</span> {c.description}
                        {!c.required && <span className="faint small"> · {t('common.optional')}</span>}
                      </span>
                      {ck && !ck.ok && ck.problems.length > 0 && <span className="faint small">{problemText(t, ck.problems[0])}</span>}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="faint small">{t('wp.task.noCriteria')}</p>
          )}
          <p className="faint small">{t('wp.task.note')}</p>
        </section>
      </div>
    </PaneFrame>
  );
}
