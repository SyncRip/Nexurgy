// What a stage review (kind milestone_review) asks the reviewer to decide,
// rendered from the frozen snapshot: the stage, the accepted task submissions,
// the extra stage deliverables and the guard results at submission — plus,
// for pending requests, whether the stage still passes its guards now.

import '../stages/stages.css';
import { CircleCheck, CircleX, ExternalLink, Info, ListChecks, Milestone as MilestoneIcon, Package, TriangleAlert, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { ApprovalRequestDetail } from '../../api/approvals';
import { getAssetVersion } from '../../api/assets';
import type { MilestoneReviewItem, ReviewCheck, ReviewSnapshotExtra } from '../../api/stages';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetTypeIcon, HashText, SourceLabels, VersionToken, assetTypeLabel } from '../assets/meta';
import { Facts } from '../assets/VersionSummary';
import { BlockerList, ReviewStatusChip, dyn } from '../stages/meta';
import { WithdrawReviewDialog } from '../stages/ReviewHistory';

function ExtraRow({ x, item }: { x: ReviewSnapshotExtra; item?: MilestoneReviewItem }) {
  const { t } = useI18n();
  const info = item?.version ?? null;
  // The frozen snapshot keeps hashes only; load the version for its source labels (simulated / estimated stay visible).
  const live = useResource((signal) => getAssetVersion(x.asset_version_id, { signal }), [x.asset_version_id], { enabled: !info?.missing });
  const v = live.data;
  const assetId = x.asset_id ?? info?.asset_id ?? v?.asset_id;
  const type = x.asset_type ?? info?.asset_type ?? v?.asset_type;
  const name = x.asset_name ?? info?.asset_name ?? v?.asset_name ?? (type ? assetTypeLabel(t, type) : t('as.unknownAsset'));
  const labels = x.labels ?? v?.labels ?? [];
  const changed = !!v && v.content_hash !== x.content_hash;
  const inner = (
    <>
      {type && <AssetTypeIcon type={type} />}
      <span className="strong">{name}</span> <VersionToken label={x.label || info?.label || v?.label || '?'} />
    </>
  );
  return (
    <li>
      {assetId ? (
        <Link to={paths.asset(assetId, x.asset_version_id)} className="stg-ref">
          {inner}
        </Link>
      ) : (
        <span className="stg-ref">{inner}</span>
      )}
      <SourceLabels labels={labels} emptyDash={false} />
      {info?.missing && <Chip tone="err">{t('tk.version.missing')}</Chip>}
      {changed && <Chip tone="err">{t('stg.target.extraChanged')}</Chip>}
      <span className="grow" />
      <span className="small faint">{t('ap.item.hash')}</span>
      <HashText hash={x.content_hash} copy={false} />
    </li>
  );
}

function CheckRow({ c }: { c: ReviewCheck }) {
  const { t } = useI18n();
  const ok = c.ok !== false;
  const text = c.code ? dyn(t, `stg.check.${c.code}`, c.message || c.code) : c.message || '—';
  return (
    <li>
      {ok ? (
        <Chip tone="ok" icon={<CircleCheck aria-hidden />}>
          {t('ap.check.pass')}
        </Chip>
      ) : (
        <Chip tone="err" icon={<CircleX aria-hidden />}>
          {t('ap.check.fail')}
        </Chip>
      )}
      <span className="grow small" title={c.message}>
        {text}
      </span>
      {c.code && <code className="error-code">{c.code}</code>}
    </li>
  );
}

export function StageReviewTarget({ detail, canWithdraw, onChanged }: { detail: ApprovalRequestDetail; canWithdraw: boolean; onChanged: () => void }) {
  const { t, fmt } = useI18n();
  const [withdrawing, setWithdrawing] = useState(false);
  const review = detail.target.milestone_review;
  const readiness = detail.target.readiness ?? review?.readiness ?? null;
  const pending = detail.status === 'pending';
  const snap = review?.snapshot;

  if (!review || !snap) {
    return (
      <div className="callout warn" role="note">
        <TriangleAlert aria-hidden />
        <span>{t('stg.target.noSnapshot')}</span>
      </div>
    );
  }
  const stage = snap.milestone ?? review.milestone;
  const items = new Map((review.items ?? []).filter((i) => i.asset_version_id).map((i) => [i.asset_version_id as string, i]));

  return (
    <div className="stack">
      <div className="callout info" role="note">
        <Info aria-hidden />
        <span>{t('stg.target.explain')}</span>
      </div>
      {pending && readiness && readiness.ready && (
        <div className="callout ok" role="note">
          <CircleCheck aria-hidden />
          <span>{t('stg.target.readyNow')}</span>
        </div>
      )}
      {pending && readiness && !readiness.ready && (
        <div className="callout err" role="alert">
          <CircleX aria-hidden />
          <div className="stack-sm grow">
            <span>{t('stg.target.notReadyNow')}</span>
            <BlockerList blockers={readiness.blockers} />
          </div>
        </div>
      )}

      <section className="panel" aria-labelledby="stg-t-stage-h">
        <div className="panel-h">
          <MilestoneIcon aria-hidden className="faint" />
          <h2 id="stg-t-stage-h">{t('stg.target.stage')}</h2>
          <span className="grow" />
          {pending && (review.can_withdraw ?? canWithdraw) && (
            <button type="button" className="btn sm" onClick={() => setWithdrawing(true)}>
              <Undo2 /> {t('stg.review.withdraw')}
            </button>
          )}
          <Link className="btn sm" to={paths.project(detail.project_id)}>
            <ExternalLink /> {t('stg.target.openOverview')}
          </Link>
        </div>
        <div className="panel-b stack-sm">
          <div className="row">
            <span className="strong">{stage ? `${stage.seq}. ${stage.name}` : '—'}</span>
            <span className="mono faint">#{review.seq}</span>
            <ReviewStatusChip status={review.status} />
          </div>
          <Facts
            rows={[
              [t('stg.target.submittedBy'), <span>{review.submitted_by_name ?? detail.created_by_name ?? '—'}</span>],
              [t('stg.target.submittedAt'), fmt.dateTime(review.submitted_at)],
              [t('stg.target.reviewer'), <span>{review.reviewer?.name ?? detail.assignee?.name ?? '—'}</span>],
              [t('ap.acc.snapshotHash'), <HashText hash={review.snapshot_hash} chars={16} />],
            ]}
          />
          {review.note && (
            <div>
              <span className="small faint">{t('stg.review.note')}</span>
              <p className="ap-instructions">{review.note}</p>
            </div>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="stg-t-tasks-h">
        <div className="panel-h">
          <ListChecks aria-hidden className="faint" />
          <h2 id="stg-t-tasks-h">{t('stg.target.tasks')}</h2>
          <span className="faint small">· {fmt.number(snap.tasks.length)}</span>
        </div>
        <div className="panel-b">
          {snap.tasks.length ? (
            <ul className="stg-snap-list">
              {snap.tasks.map((x) => (
                <li key={x.task_id}>
                  <Link to={paths.task(x.task_id)}>
                    <span className="mono strong">{x.key}</span> {x.title}
                  </Link>
                  <span className="small faint">{t('stg.review.submissionSeq', { seq: x.seq })}</span>
                  <span className="grow" />
                  <span className="small faint">{t('ap.item.hash')}</span>
                  <HashText hash={x.snapshot_hash} copy={false} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('stg.target.noTasks')}</p>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="stg-t-extras-h">
        <div className="panel-h">
          <Package aria-hidden className="faint" />
          <h2 id="stg-t-extras-h">{t('stg.target.extras')}</h2>
          <span className="faint small">· {fmt.number(snap.extras.length)}</span>
        </div>
        <div className="panel-b">
          {snap.extras.length ? (
            <ul className="stg-snap-list">
              {snap.extras.map((x) => (
                <ExtraRow key={x.asset_version_id} x={x} item={items.get(x.asset_version_id)} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('stg.target.noExtras')}</p>
          )}
        </div>
      </section>

      {snap.checks.length > 0 && (
        <section className="panel" aria-labelledby="stg-t-checks-h">
          <div className="panel-h">
            <h2 id="stg-t-checks-h">{t('stg.target.checks')}</h2>
          </div>
          <div className="panel-b">
            <ul className="stg-snap-list">
              {snap.checks.map((c, i) => (
                <CheckRow key={`${c.code ?? 'check'}-${i}`} c={c} />
              ))}
            </ul>
          </div>
        </section>
      )}

      {withdrawing && (
        <WithdrawReviewDialog
          review={{ id: review.id }}
          onClose={() => setWithdrawing(false)}
          onDone={() => {
            setWithdrawing(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}
