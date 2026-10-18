import { useFeature } from '../../api/capabilities';
// Project strip (prototype projectStrip, R1.2 §09): what a manager scans first
// on every project tab — the objective (editable by the owner), the stage bar
// with the current stage and what blocks its review, and the task numbers.
// Everything comes from the overview and the current stage's readiness; the
// stage review itself is started on the stages tab.

import './board.css';
import { Pencil } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getOverview } from '../../api/pm';
import { getMilestoneReadiness, normalizeBlockers } from '../../api/stages';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import type { ProjectCapabilities } from '../../auth/permissions';
import { Chip } from '../../components/ui/Badges';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { blockerText } from '../stages/meta';
import { currentStageIndex, stageSteps, taskTally } from './board';
import { LifecycleChip } from './common';
import { EditPlanDialog } from './EditPlanDialog';

export function ProjectStrip({ project, caps, onChanged }: { project: Project; caps: ProjectCapabilities; onChanged: () => void }) {
  const { t, tn, fmt } = useI18n();
  const stagesEnabled = useFeature('stages');
  const tasksEnabled = useFeature('tasks');
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const ov = useResource((signal) => getOverview(project.id, { signal }), [project.id, project.updated_at]);
  const o = ov.data;
  const current = o ? (o.current_milestone ?? o.project.current_milestone) : null;
  const open = !!current && (current.status ?? 'open') === 'open';
  const readiness = useResource((signal) => getMilestoneReadiness(current!.id, { signal }), [current?.id, current?.version, o], { enabled: open && stagesEnabled });
  if (!o) return <div className="pa-strip is-loading" aria-busy="true" />;

  const p = o.project;
  const archived = p.status === 'archived' || !!p.archived_at;
  const canEdit = caps.manage && !archived && p.lifecycle !== 'completed' && p.lifecycle !== 'cancelled';
  const steps = stageSteps(o.milestones, current?.id);
  const pos = currentStageIndex(steps);
  const tally = taskTally(o.tasks.by_status);
  const blockers = readiness.data ? normalizeBlockers(readiness.data.blockers) : null;
  const blockerCount = blockers ? blockers.length : (current?.readiness?.blocker_codes.length ?? 0);
  const blockerLine = blockers?.map((b) => blockerText(t, b)).join(t('pa.strip.sep')) ?? '';

  let stageState;
  if (!steps.length) stageState = <span className="faint">{t('pa.strip.noStages')}</span>;
  else if (!current) stageState = <Chip tone="ok">{t('pa.strip.allAccepted')}</Chip>;
  else if (current.status === 'in_review') stageState = <Chip tone="warn">{t('pa.strip.inReview')}</Chip>;
  else if (current.status === 'accepted') stageState = <Chip tone="ok">{t('pa.strip.stageAccepted')}</Chip>;
  else if (!readiness.data && !current.readiness) stageState = null;
  else if (blockerCount === 0) stageState = <Chip tone="ok">{t('pa.strip.ready')}</Chip>;
  else
    stageState = (
      <>
        <Chip tone="err">{tn('pa.strip.blockers', blockerCount, { count: fmt.number(blockerCount) })}</Chip>
        {blockerLine && (
          <span className="faint pa-block-text" title={blockerLine}>
            {blockerLine}
          </span>
        )}
      </>
    );

  return (
    <div className="pa-strip" aria-label={t('area.project.strip')}>
      <div className="pa-goal">
        <span className="pa-lab">{t('pa.strip.objective')}</span>
        {p.objective ? <p>{p.objective}</p> : <p className="faint">{t('pa.strip.noObjective')}</p>}
        {canEdit && (
          <button type="button" className="btn sm" onClick={() => setEditing(true)}>
            <Pencil /> {p.objective ? t('pa.strip.editObjective') : t('pa.strip.setObjective')}
          </button>
        )}
      </div>
      {stagesEnabled && <div className="pa-stage">
        <span className="pa-lab">
          {current && pos ? t('pa.strip.stage', { n: pos, total: steps.length, name: current.name }) : t('pa.strip.stages')}
          {p.lifecycle !== 'active' && <LifecycleChip lifecycle={p.lifecycle} />}
        </span>
        {steps.length > 0 && (
          <ol className="pa-steps" style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}>
            {steps.map((s) => (
              <li key={s.id} className={s.state} aria-current={s.state === 'current' ? 'step' : undefined}>
                <i aria-hidden />
                <span className="ellip" title={s.name}>
                  {s.name}
                </span>
                <span className="sr-only">{t(`pa.strip.state.${s.state}`)}</span>
              </li>
            ))}
          </ol>
        )}
        <div className="pa-block">
          {stageState}
          <Link className="pa-lnk" to={paths.projectTab('overview')}>
            {t('pa.strip.stageDetail')}
          </Link>
        </div>
      </div>}
      {tasksEnabled && <div className="pa-num">
        <div>
          <b className="mono">
            {fmt.number(tally.accepted)}
            <small>/{fmt.number(tally.total)}</small>
          </b>
          <span>{t('pa.strip.accepted')}</span>
        </div>
        <div>
          <b className="mono">{fmt.number(tally.awaiting)}</b>
          <span>{t('pa.strip.awaiting')}</span>
        </div>
        <div>
          <b className="mono">{project.part_count === null ? '—' : fmt.number(project.part_count)}</b>
          <span>{t('pa.strip.parts')}</span>
        </div>
      </div>}
      {editing && (
        <EditPlanDialog
          ov={o}
          onClose={() => setEditing(false)}
          onReload={() => {
            setEditing(false);
            ov.reload();
          }}
          onSaved={() => {
            setEditing(false);
            toast(t('tk.ov.planSaved'));
            ov.reload();
            onChanged();
          }}
        />
      )}
    </div>
  );
}
