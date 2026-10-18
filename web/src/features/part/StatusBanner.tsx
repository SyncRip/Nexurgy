// Part pipeline status (GET /parts/{id}/status): current design + design
// approval, plan / quote staleness with reasons, and whether the quote is
// formal (every confirmation holds on current data) or what blocks it.

import { BadgeCheck,CircleSlash,FileBox,ListChecks,Receipt } from 'lucide-react';
import type { PartBlocker,PartPipelineStatus } from '../../api/types';
import type { ProjectCapabilities } from '../../auth/permissions';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice,Skeleton } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { ApprovalActions,ApprovalChip,EstimatedBadge,ReasonList } from './common';

export function StatusBanner({
  status,
  error,
  onRetry,
  caps,
  names,
  onChanged,
}: {
  status: PartPipelineStatus | undefined;
  error: unknown;
  onRetry: () => void;
  caps: ProjectCapabilities;
  names: (id: string) => string;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  if (error && !status) return <ErrorNotice error={error} onRetry={onRetry} />;
  if (!status)
    return (
      <div className="ws-status skel">
        <Skeleton w="60%" h={14} />
      </div>
    );
  const d = status.design;
  return (
    <section className={`ws-status ${status.formal ? 'formal' : ''}`} aria-label={t('ws.st.title')}>
      <div className="ws-status-cell">
        <span className="ws-status-label">
          <FileBox aria-hidden /> {t('ws.st.design')}
        </span>
        {d ? (
          <>
            <span className="mono strong">{d.kind === 'model_revision' ? `R${d.rev_no}` : t('ws.st.intake')}</span>
            {d.kind === 'model_revision' && d.mesh_only && <Chip tone="pending">{t('rev.meshOnly')}</Chip>}
            {d.kind === 'intake' && <EstimatedBadge />}
            <ApprovalChip approval={d.approval} names={names} />
            {!(d.approval?.valid) && (
              <ApprovalActions partId={status.part_id} compact subjectType={d.kind === 'model_revision' ? 'model_revision' : 'intake'} subjectId={d.id} stage="design" can={caps.design} onDone={onChanged} />
            )}
          </>
        ) : (
          <span className="faint">{t('ws.st.noDesign')}</span>
        )}
      </div>
      <div className="ws-status-cell">
        <span className="ws-status-label">
          <ListChecks aria-hidden /> {t('ws.st.plan')}
        </span>
        {status.plan ? (
          <>
            {status.plan.stale ? <Chip tone="stale">{t('ws.st.stale')}</Chip> : <Chip tone="ok">{t('ws.st.current')}</Chip>}
            {status.plan.estimated && <EstimatedBadge />}
            {!status.plan.priceable && <Chip tone="err">{t('ws.plan.notPriceable')}</Chip>}
            <ApprovalChip approval={status.plan.approval} names={names} />
          </>
        ) : (
          <span className="faint">{t('ws.st.none')}</span>
        )}
      </div>
      <div className="ws-status-cell">
        <span className="ws-status-label">
          <Receipt aria-hidden /> {t('ws.st.quote')}
        </span>
        {status.quote ? (
          <>
            {status.quote.stale ? <Chip tone="stale">{t('ws.st.stale')}</Chip> : <Chip tone="ok">{t('ws.st.current')}</Chip>}
            {status.quote.estimated && <EstimatedBadge />}
            <ApprovalChip approval={status.quote.approval} names={names} />
          </>
        ) : (
          <span className="faint">{t('ws.st.none')}</span>
        )}
      </div>
      <div className="ws-status-cell ws-status-formal">
        {status.formal ? (
          <Chip tone="ok" icon={<BadgeCheck aria-hidden />}>
            {t('ws.st.formal')}
          </Chip>
        ) : (
          <>
            <Chip tone="pending" icon={<CircleSlash aria-hidden />}>
              {t('ws.st.notFormal')}
            </Chip>
            <span className="small muted">{status.blockers.map((b: PartBlocker) => t(`ws.blocker.${b}`)).join(' · ')}</span>
          </>
        )}
      </div>
      {(status.plan?.stale || status.quote?.stale) && (
        <div className="ws-status-reasons">
          {status.plan?.stale && (
            <div>
              <span className="small strong">{t('ws.st.planStaleBecause')}</span>
              <ReasonList reasons={status.plan.reasons} />
            </div>
          )}
          {status.quote?.stale && (
            <div>
              <span className="small strong">{t('ws.st.quoteStaleBecause')}</span>
              <ReasonList reasons={status.quote.reasons} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
