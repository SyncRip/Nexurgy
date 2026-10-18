// Stage review history of one stage: every review stays (approved, rejected,
// withdrawn, superseded) with its reviewer, note and request. A pending review
// can be withdrawn by its submitter or a project owner.

import { ExternalLink, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { listMilestoneReviews, withdrawMilestoneReview, type MilestoneReview } from '../../api/stages';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { notifyApprovalsChanged } from '../approvals/meta';
import { ReasonDialog, type Members } from '../tasks/common';
import { ReviewStatusChip } from './meta';

export function WithdrawReviewDialog({ review, onClose, onDone }: { review: Pick<MilestoneReview, 'id'>; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  return (
    <ReasonDialog
      title={t('stg.review.withdrawTitle')}
      body={t('stg.review.withdrawBody')}
      reasonLabel={t('tk.reason')}
      confirmLabel={t('stg.review.withdraw')}
      danger
      onConfirm={async (reason) => {
        await withdrawMilestoneReview(review.id, reason);
        notifyApprovalsChanged();
        toast(t('stg.review.withdrawnToast'));
        onDone();
      }}
      onClose={onClose}
    />
  );
}

export function ReviewHistory({ milestoneId, members, meId, canManage, onChanged }: { milestoneId: UUID; members: Members; meId: string; canManage: boolean; onChanged: () => void }) {
  const { t, fmt } = useI18n();
  const res = useResource((signal) => listMilestoneReviews(milestoneId, { signal }), [milestoneId]);
  const [withdrawing, setWithdrawing] = useState<MilestoneReview | null>(null);

  if (res.error && !res.data) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!res.data)
    return (
      <div className="stack-sm" role="status" aria-label={t('common.loading')}>
        <Skeleton w="60%" />
        <Skeleton w="40%" />
      </div>
    );
  const rows = [...res.data].sort((a, b) => b.seq - a.seq);
  if (!rows.length) return <p className="faint small">{t('stg.review.none')}</p>;

  return (
    <>
      <ul className="stg-reviews">
        {rows.map((r) => (
          <li key={r.id}>
            <div className="row">
              <span className="mono strong">#{r.seq}</span>
              <ReviewStatusChip status={r.status} />
              <span className="grow" />
              {r.request_id && (
                <Link to={paths.approvalRequest(r.request_id)} className="btn ghost sm">
                  <ExternalLink /> {t('stg.review.openRequest')}
                </Link>
              )}
              {r.status === 'pending' && (r.submitted_by === meId || canManage) && (
                <button type="button" className="btn sm" onClick={() => setWithdrawing(r)}>
                  <Undo2 /> {t('stg.review.withdraw')}
                </button>
              )}
            </div>
            <span className="small faint">
              {t('stg.review.byLine', {
                who: r.submitted_by_name ?? members.name(r.submitted_by),
                time: fmt.dateTime(r.submitted_at),
                reviewer: r.reviewer?.name ?? (r.reviewer_id ? members.name(r.reviewer_id) : t('tk.unassigned')),
              })}
              {r.decided_at && ` · ${t('stg.review.decidedAt', { time: fmt.dateTime(r.decided_at) })}`}
            </span>
            {r.note && <p className="small stg-note">“{r.note}”</p>}
          </li>
        ))}
      </ul>
      {withdrawing && (
        <WithdrawReviewDialog
          review={withdrawing}
          onClose={() => setWithdrawing(null)}
          onDone={() => {
            setWithdrawing(null);
            res.reload();
            onChanged();
          }}
        />
      )}
    </>
  );
}
