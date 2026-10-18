// The business-task node card of the workbench (UI02): number, title, phase,
// objective, executor, final reviewer, the four internal stages as progress
// dots and the blocked / accepted note. Shared by the React Flow canvas and the
// list view; it imports nothing from React Flow so the list stays light.
// One card = one real Task (design §0.1).

import { CheckCircle2, CircleDashed, Eye, Hand, RotateCcw, TriangleAlert } from 'lucide-react';
import type { NodeTaskOut } from '../../api/collaboration';
import { useI18n } from '../../i18n/I18nProvider';
import { TurboNodeNote } from '../turbo/TurboNode';
import { ActorLabel, PhaseChip, StageDots, blockedReasonText } from './common';
import { displayPhase } from './model';

export interface NodeTaskCardProps {
  seq: number;
  title: string;
  task: NodeTaskOut | undefined;
  selected: boolean;
  /** Omitted inside the canvas: React Flow handles the click. */
  onSelect?: () => void;
}

export function NodeTaskCard({ seq, title, task, selected, onSelect }: NodeTaskCardProps) {
  const { t, fmt } = useI18n();
  const phase = task?.phase;
  const cls = ['cl-node', selected ? 'selected' : '', phase ? `ph-${phase}` : '', onSelect ? '' : 'is-static'].join(' ');
  const body = (
    <>
      <span className="cl-node-head">
        <span className="cl-node-seq">{String(seq).padStart(2, '0')}</span>
        <span className="cl-node-title ellip" title={title}>
          {title}
        </span>
        {task && phase ? (
          <PhaseChip phase={displayPhase(task)} compact />
        ) : (
          <span className="chip pending cl-chip-sm">
            <CircleDashed aria-hidden /> {t('collab.node.noTask')}
          </span>
        )}
      </span>
      <span className="cl-node-objective" title={task?.objective}>
        {task?.objective || <span className="faint">{t('common.none')}</span>}
      </span>
      {task && (
        <dl className="cl-node-roles">
          <dt>{t('collab.node.executor')}</dt>
          <dd>
            <ActorLabel actor={task.executor} />
          </dd>
          <dt>{t('collab.node.reviewer')}</dt>
          <dd>
            <ActorLabel actor={task.reviewer} fallback={task.verification || task.turbo_binding ? t('tb.draft.noReviewer') : undefined} />
          </dd>
        </dl>
      )}
      {phase && <StageDots phase={phase} />}
      {task && <TurboNodeNote node={task} compact />}
      {task && phase === 'accepted' && (
        <span className="cl-node-note ok">
          <CheckCircle2 aria-hidden />
          <span>
            {task.final_decision?.actor?.kind === 'agent' ? t('collab.node.acceptedByAgent') : t('collab.node.acceptedByHuman')}
            {task.final_decision?.at ? ` · ${fmt.dateTime(task.final_decision.at)}` : ''}
          </span>
        </span>
      )}
      {task && phase === 'blocked' && (
        <span className="cl-node-note err">
          <Hand aria-hidden />
          <span>{task.blocked_reason ? blockedReasonText(t, task.blocked_reason) : t('collab.node.waitingUpstream')}</span>
        </span>
      )}
      {task && phase === 'failed' && (
        <span className="cl-node-note err">
          <TriangleAlert aria-hidden />
          <span>{task.blocked_reason ? blockedReasonText(t, task.blocked_reason) : t('collab.phase.failed')}</span>
        </span>
      )}
      {task && phase === 'in_review' && (
        <span className="cl-node-note warn">
          <Eye aria-hidden />
          <span>{task.reviewer?.kind === 'human' ? t('collab.node.waitingHuman') : t('collab.node.agentReview')}</span>
        </span>
      )}
      {task && phase === 'rework' && (
        <span className="cl-node-note warn">
          <RotateCcw aria-hidden />
          <span>{t('collab.node.reworkNote')}</span>
        </span>
      )}
    </>
  );
  if (onSelect)
    return (
      <button type="button" className={cls} aria-pressed={selected} onClick={onSelect} data-node-id={task?.node_id}>
        {body}
      </button>
    );
  return (
    <div className={cls} data-node-id={task?.node_id}>
      {body}
    </div>
  );
}
