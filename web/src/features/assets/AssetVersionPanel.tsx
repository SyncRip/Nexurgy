import { useFeature } from '../../api/capabilities';
// One asset version: trust labels, states, content summary, publish action,
// provenance and usages. Publishing is not task acceptance, and a newer
// version is only a hint — it does not make this one invalid. "Add to AI chat"
// references exactly this version (and selected content text as an excerpt).

import { Archive,BadgeCheck,ExternalLink,PlayCircle,TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { publishAssetVersion,type AssetVersion } from '../../api/assets';
import { isApiError } from '../../api/client';
import { paths } from '../../app/nav';
import type { ProjectCapabilities } from '../../auth/permissions';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';

import { useAssistantPageContext } from '../assistant/AssistantProvider';
import { assetVersionRef } from '../assistant/refs';
import { AutoSelectionCapture,aiSourceProps } from '../assistant/SelectionContext';
import { ModuleContentLoader,ModuleVersionActions,SaveAsModuleButton } from '../modules/ModuleView';
import { DependencyGraph,UsagesPanel } from './AssetRelations';
import { AvailabilityChip,EngineeringChip,HashText,LabelCallouts,PublishChip,SourceLabels,VersionToken,dynLabel,engineeringStateOf,runPath,stageText } from './meta';
import { Facts,OpenInWorkspace,VersionSummary } from './VersionSummary';

function PublishError({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  if (isApiError(error) && error.code === 'approval_required') {
    const stage = stageText(t, String((error.details as { stage?: string }).stage ?? ''));
    const module = (error.details as { subject_type?: string }).subject_type === 'process_module';
    return (
      <div className="callout warn" role="alert">
        <TriangleAlert aria-hidden />
        <div className="stack-sm">
          <span>{t(module ? 'as.err.approval_required_module' : 'as.err.approval_required', { stage })}</span>
          <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
        </div>
      </div>
    );
  }
  if (isApiError(error) && error.code === 'asset_not_ready') {
    const availability = String((error.details as { availability?: string }).availability ?? '');
    return (
      <div className="callout warn" role="alert">
        <TriangleAlert aria-hidden />
        <div className="stack-sm">
          <span>{t('as.err.asset_not_ready', { availability: dynLabel(t, `as.avail.${availability}`, availability) })}</span>
          <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
        </div>
      </div>
    );
  }
  return <ErrorNotice error={error} overrides={{ forbidden: 'as.err.forbidden' }} />;
}

function PublishControl({ version, onUpdated }: { version: AssetVersion; onUpdated: (v: AssetVersion) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [target, setTarget] = useState<'published' | 'deprecated' | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const modulesEnabled = useFeature('process_modules');
  const state = version.publish_state;
  const eng = engineeringStateOf(version.engineering);
  const needsApproval = eng !== 'not_required' && eng !== 'approved';

  const run = async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const v = await publishAssetVersion(version.id, target);
      onUpdated(v);
      toast(t(target === 'published' ? 'as.publish.publishedToast' : 'as.publish.deprecatedToast', { label: v.label }));
      setTarget(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  if (version.asset_type === 'process_module' && !modulesEnabled) return null;
  return (
    <div className="stack-sm">
      <div className="row">
        {state !== 'published' && (
          <button type="button" className="btn sm" onClick={() => setTarget('published')}>
            <BadgeCheck /> {state === 'deprecated' ? t('as.publish.republish') : t('as.publish.publish')}
          </button>
        )}
        {state === 'published' && (
          <button type="button" className="btn sm" onClick={() => setTarget('deprecated')}>
            <Archive /> {t('as.publish.deprecate')}
          </button>
        )}
      </div>
      {state !== 'published' && needsApproval && version.engineering && (
        <p className="small faint">
          {t(version.asset_type === 'process_module' ? 'as.publish.needsModuleApproval' : 'as.publish.needsApproval', { stage: stageText(t, version.engineering.stage) })}
        </p>
      )}
      {target && (
        <ConfirmDialog
          title={t(target === 'published' ? 'as.publish.confirmTitle' : 'as.publish.deprecateTitle', { label: version.label, name: version.asset_name })}
          body={t(target === 'published' ? 'as.publish.confirmBody' : 'as.publish.deprecateBody')}
          confirmLabel={target === 'published' ? t('as.publish.publish') : t('as.publish.deprecate')}
          danger={target === 'deprecated'}
          busy={busy}
          error={<PublishError error={error} />}
          onConfirm={() => void run()}
          onClose={() => {
            setTarget(null);
            setError(null);
          }}
        />
      )}
    </div>
  );
}

/** Role that publishes this asset type (mirrors api/assets.py publish). */
function canPublish(caps: ProjectCapabilities, type: string): boolean {
  if (type === 'process_plan' || type === 'process_module') return caps.process;
  if (type === 'quote') return caps.quote;
  return caps.design;
}

export function AssetVersionPanel({
  version,
  latest,
  caps,
  memberName,
  onUpdated,
  onSelect,
  onVersionsChanged,
  versions = [],
}: {
  version: AssetVersion;
  latest: AssetVersion | undefined;
  /** All versions of the asset (resolves the parent revision label). */
  versions?: AssetVersion[];
  caps: ProjectCapabilities;
  memberName: Map<string, string>;
  onUpdated: (v: AssetVersion) => void;
  onSelect: (versionId: string) => void;
  /** Reload the version list (a new module version or review request); selects `newVersionId` when given. */
  onVersionsChanged?: (newVersionId?: string) => void;
}) {
  const { t, fmt } = useI18n();
  const isLatest = !latest || latest.id === version.id;
  const decision = version.engineering?.decision;
  const isModule = version.asset_type === 'process_module';
  const pendingReview = version.engineering?.pending_request_id ?? null;
  const aiScope = t('ar.asset.scope', { name: version.asset_name, label: version.label });
  useAssistantPageContext({
    projectId: version.project_id,
    label: aiScope,
    suggestions: [{ ...assetVersionRef(version.project_id, version.id), label: aiScope }],
  });

  return (
    <div className="stack">
      {!isLatest && latest && (
        // S6: the same "historical version" banner as every other exact-version page.
        <HistoricalBanner current={latest.label} onOpenCurrent={() => onSelect(latest.id)}>
          <span className="faint small">{t('as.newerHint', { latest: latest.label, current: version.label })}</span>
        </HistoricalBanner>
      )}
      <LabelCallouts labels={version.labels} />

      <section className="panel" aria-labelledby="as-ver-h">
        <div className="panel-h">
          <VersionToken label={version.label} />
          <h2 id="as-ver-h">{t('as.versionTitle', { n: version.version_no })}</h2>
          <span className="grow" />
          <></>
          <OpenInWorkspace version={version} size="sm" />
        </div>
        <div className="panel-b stack">
          <Facts
            rows={[
              [t('as.fact.availability'), <AvailabilityChip availability={version.availability} />],
              [
                t('as.fact.publish'),
                <div className="stack-sm">
                  <PublishChip state={version.publish_state} />
                  {canPublish(caps, version.asset_type) && <PublishControl version={version} onUpdated={onUpdated} />}
                </div>,
              ],
              [
                t('as.fact.engineering'),
                <div className="stack-sm">
                  <EngineeringChip engineering={version.engineering} />
                  {pendingReview && (
                    <Link to={paths.approvalRequest(pendingReview)} className="small as-use-link">
                      <ExternalLink aria-hidden className="inline-ico" /> {t('as.eng.reviewPending')}
                    </Link>
                  )}
                  {decision && (
                    <span className="small muted">
                      {t(decision.decision === 'approved' ? 'as.eng.decidedApproved' : 'as.eng.decidedRejected', {
                        who: decision.user_id ? (memberName.get(decision.user_id) ?? t('as.someone')) : t('rs.actor.agentDecided'),
                        role: dynLabel(t, `role.${decision.role}`, decision.role),
                        time: fmt.dateTime(decision.at),
                      })}
                      {decision.comment && <> · “{decision.comment}”</>}
                    </span>
                  )}
                </div>,
              ],
              [t('as.fact.labels'), <SourceLabels labels={version.labels} max={8} />],
              [t('as.fact.contentHash'), <HashText hash={version.content_hash} chars={16} />],
              [
                t('as.fact.created'),
                <span>
                  <span title={fmt.dateTime(version.created_at)}>{fmt.dateTime(version.created_at)}</span>
                  {version.created_by && <span className="faint"> · {memberName.get(version.created_by) ?? t('as.someone')}</span>}
                </span>,
              ],
              [
                t('as.fact.sourceRun'),
                version.run_id ? (
                  <Link to={runPath(version.run_id)} className="as-use-link">
                    <PlayCircle aria-hidden className="inline-ico" /> {t('as.openRun')}
                  </Link>
                ) : (
                  <span className="faint small">{t('as.noRun')}</span>
                ),
              ],
            ]}
          />
        </div>
      </section>

      <section className="panel" aria-labelledby="as-content-h">
        <div className="panel-h">
          <h2 id="as-content-h">{t('as.content.title')}</h2>
          <span className="grow" />
          {isModule && (
            <ModuleVersionActions version={version} canProcess={caps.process} pendingRequestId={pendingReview} onVersionsChanged={(id) => onVersionsChanged?.(id)} />
          )}
          {version.asset_type === 'process_plan' && caps.process && (
            <SaveAsModuleButton projectId={version.project_id} planId={version.target.id} label={version.label} labels={version.labels} />
          )}
        </div>
        <div className="panel-b stack-sm" {...aiSourceProps('asset_version', version.id, version.project_id)}>
          {isModule ? <ModuleContentLoader versionId={version.id} /> : <VersionSummary version={version} />}
          {!isModule && <p className="faint small">{t('as.content.note')}</p>}
        </div>
      </section>

      <DependencyGraph version={version} parent={version.parent_version_id ? versions.find((v) => v.id === version.parent_version_id) : null} />
      <UsagesPanel version={version} />
      <AutoSelectionCapture />
    </div>
  );
}
