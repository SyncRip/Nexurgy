// Usage history of a collection version: every reference / adoption with its
// exact source, target, actor and confirmation. Pending adoptions can be
// confirmed or rejected by the target node's human reviewer (the server is the
// authority; `can_confirm` narrows the buttons when the server reports it).

import { Ban, CircleCheck, PlayCircle } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useFeature } from '../../api/capabilities';
import { confirmKnowledgeUsage, type KnowledgeUsage } from '../../api/knowledge';
import type { ProjectCapabilities } from '../../auth/permissions';
import { useI18n } from '../../i18n/I18nProvider';
import { ActorName, ConfirmationChip, TaskLink, UsageKindChip } from '../assets/collabMeta';
import { HashText, VersionToken, runPath } from '../assets/meta';
import { ReasonDialog } from '../tasks/common';

export function UsageHistory({ usages, itemLabel, caps, onChanged }: { usages: KnowledgeUsage[]; itemLabel: (itemId: string) => { name: string; label: string; hash: string } | null; caps: ProjectCapabilities; onChanged: () => void }) {
  const collaborationEnabled = useFeature('collaboration');
  const { t, fmt } = useI18n();
  const [acting, setActing] = useState<{ usage: KnowledgeUsage; decision: 'confirmed' | 'rejected' } | null>(null);
  if (!usages.length) return <p className="faint small">{t('kb.usages.none')}</p>;
  return (
    <>
      <ul className="kb-usages">
        {usages.map((u) => {
          const src = itemLabel(u.item_id);
          const canConfirm = collaborationEnabled && u.confirmation === 'pending' && (u.can_confirm ?? caps.workflow);
          return (
            <li key={u.id} className="kb-usage">
              <div className="kb-usage-main">
                <UsageKindChip usage={u.usage} />
                {src && (
                  <span className="row small">
                    <span className="ellip strong" title={src.name}>
                      {src.name}
                    </span>
                    <VersionToken label={src.label} />
                    <HashText hash={src.hash} chars={10} copy={false} />
                  </span>
                )}
                <span className="faint small">→</span>
                {u.target_task_id ? (
                  <TaskLink id={u.target_task_id} number={u.target_task?.number ?? null} title={u.target_task?.title ?? t('as.use.targetTask')} />
                ) : u.target_run_id ? (
                  <Link to={runPath(u.target_run_id)} className="as-use-link">
                    <PlayCircle aria-hidden className="inline-ico" /> {t('as.use.targetRun')}
                  </Link>
                ) : (
                  <span className="faint small">{t('as.use.targetProject')}</span>
                )}
                {u.slot_key && <span className="mono small">{u.slot_key}</span>}
              </div>
              <div className="kb-usage-meta small">
                <span>
                  {t('kb.usages.by')} <ActorName actor={u.used_by} />
                </span>
                {u.created_at && (
                  <span className="faint" title={fmt.dateTime(u.created_at)}>
                    {fmt.relative(u.created_at)}
                  </span>
                )}
                <ConfirmationChip confirmation={u.confirmation} />
                {u.confirmed_at && <span className="faint">{t('kb.usages.confirmedAt', { time: fmt.dateTime(u.confirmed_at) })}</span>}
                {u.baseline_snapshot_id && <span className="faint mono">{t('kb.usages.snapshot', { id: u.baseline_snapshot_id.slice(0, 8) })}</span>}
              </div>
              {u.reason && (
                <p className="small muted kb-usage-reason" title={u.reason}>
                  {u.reason}
                </p>
              )}
              {canConfirm && (
                <div className="row kb-usage-actions">
                  <button type="button" className="btn sm primary" onClick={() => setActing({ usage: u, decision: 'confirmed' })}>
                    <CircleCheck /> {t('kb.usages.confirm')}
                  </button>
                  <button type="button" className="btn sm danger" onClick={() => setActing({ usage: u, decision: 'rejected' })}>
                    <Ban /> {t('kb.usages.reject')}
                  </button>
                  <span className="faint small">{t('kb.usages.confirmHint')}</span>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {collaborationEnabled && acting && (
        <ReasonDialog
          title={acting.decision === 'confirmed' ? t('kb.usages.confirmTitle') : t('kb.usages.rejectTitle')}
          body={acting.decision === 'confirmed' ? t('kb.usages.confirmBody') : t('kb.usages.rejectBody')}
          reasonLabel={t('kb.usages.comment')}
          reasonRequired={acting.decision === 'rejected'}
          confirmLabel={acting.decision === 'confirmed' ? t('kb.usages.confirm') : t('kb.usages.reject')}
          danger={acting.decision === 'rejected'}
          onClose={() => setActing(null)}
          onConfirm={async (comment) => {
            await confirmKnowledgeUsage(acting.usage.id, { decision: acting.decision, comment });
            setActing(null);
            onChanged();
          }}
        />
      )}
    </>
  );
}
