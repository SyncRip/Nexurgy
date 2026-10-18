// Right column of the knowledge tab: one collection at one immutable manifest
// version. Archive status and index status are independent badges; the four
// sections are formal results / all resources / acceptance records / usage
// history; the adopt panel targets the selected item. Reindexing never changes
// the manifest, hashes, versions or acceptance.

import { Archive, ArchiveRestore, BadgeCheck, ClipboardCheck, Clock, History, Layers, ListChecks, PlayCircle, RefreshCw, TriangleAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useFeature } from '../../api/capabilities';
import { patchKnowledgeCollection, reindexKnowledgeCollection, scopeText, type KnowledgeCollectionDetail, type KnowledgeItem } from '../../api/knowledge';
import { effectiveAcceptance } from '../../api/resources';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import type { ProjectCapabilities } from '../../auth/permissions';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { AcceptanceChip, CollectionStatusChip, IndexStatusChip, acceptanceActor } from '../assets/collabMeta';
import { HashText, VersionToken } from '../assets/meta';
import { TabList, tabPanelProps } from '../assets/TabList';
import { useMembers } from '../tasks/common';
import { AdoptPanel } from './AdoptPanel';
import { ItemCard } from './ItemCard';
import { UsageHistory } from './UsageHistory';

type Section = 'formal' | 'all' | 'acceptance' | 'usages';

export function CollectionDetail({
  project,
  caps,
  collection: c,
  refreshing,
  returnTo,
  onVersion,
  onChanged,
  showProject = false,
}: {
  project: Project;
  caps: ProjectCapabilities;
  collection: KnowledgeCollectionDetail;
  refreshing: boolean;
  returnTo: string;
  onVersion: (versionId: string) => void;
  onChanged: () => void;
  showProject?: boolean;
}) {
  const collaborationEnabled = useFeature('collaboration');
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const members = useMembers(project.id);
  const [section, setSection] = useState<Section>('formal');
  const items = c.items ?? [];
  const usages = c.usages ?? [];
  const formal = useMemo(() => items.filter((i) => i.kind === 'formal'), [items]);
  const withAcceptance = useMemo(() => items.filter((i) => i.acceptance && i.acceptance.status !== 'not_applicable' && i.acceptance.status !== 'unsubmitted'), [items]);
  const [pickedId, setPickedId] = useState<string | null>(null);
  const picked = items.find((i) => i.id === pickedId) ?? formal.find((i) => i.reusable) ?? formal[0] ?? null;
  const versions = useMemo(() => [...(c.versions ?? [])].sort((a, b) => b.version_no - a.version_no), [c.versions]);
  const isCurrent = !c.current_version || c.version?.id === c.current_version.id;
  const archiveMode = c.scope?.execution_mode;
  const scope =
    archiveMode === 'full_auto' || archiveMode === 'semi_auto'
      ? tn(`kb.scopeArchive.${archiveMode}`, Number(c.scope?.node_count ?? 0), { count: fmt.number(Number(c.scope?.node_count ?? 0)) })
      : scopeText(c.scope);

  const [reindexBusy, setReindexBusy] = useState(false);
  const [reindexError, setReindexError] = useState<unknown>(null);
  const [statusTarget, setStatusTarget] = useState<'deprecated' | 'active' | null>(null);
  const [statusBusy, setStatusBusy] = useState(false);
  const [statusError, setStatusError] = useState<unknown>(null);

  const reindex = async () => {
    if (!collaborationEnabled) return;
    setReindexBusy(true);
    setReindexError(null);
    try {
      await reindexKnowledgeCollection(c.id);
      toast(t('kb.reindex.queued'));
      onChanged();
    } catch (e) {
      setReindexError(e);
    } finally {
      setReindexBusy(false);
    }
  };
  const changeStatus = async () => {
    if (!statusTarget || !collaborationEnabled) return;
    setStatusBusy(true);
    setStatusError(null);
    try {
      await patchKnowledgeCollection(c.id, { expected_version: c.row_version ?? 1, status: statusTarget });
      toast(t(statusTarget === 'deprecated' ? 'kb.status.deprecatedToast' : 'kb.status.restoredToast'));
      setStatusTarget(null);
      onChanged();
    } catch (e) {
      setStatusError(e);
    } finally {
      setStatusBusy(false);
    }
  };

  const itemLabel = (itemId: string) => {
    const i = items.find((x) => x.id === itemId);
    return i ? { name: i.version.asset_name, label: i.version.label, hash: i.content_hash } : null;
  };
  const canReindex = collaborationEnabled && (caps.workflow || caps.manage);

  const renderItems = (list: KnowledgeItem[], emptyKey: 'kb.items.noneFormal' | 'kb.items.none') =>
    list.length ? (
      <div className="kb-items">
        {list.map((i) => (
          <ItemCard key={i.id} item={i} selected={picked?.id === i.id} onSelect={(x) => setPickedId(x.id)} returnTo={returnTo} />
        ))}
      </div>
    ) : (
      <p className="faint small">{t(emptyKey)}</p>
    );

  return (
    <div className={`stack kb-detail ${refreshing ? 'is-refreshing' : ''}`}>
      <header className="kb-detail-head">
        <div className="kb-detail-titles">
          <h2 className="kb-detail-title ellip" title={c.title}>
            {c.title}
          </h2>
          <div className="row kb-detail-badges">
            <label className="row small">
              <span className="faint">{t('kb.manifest')}</span>
              <select className="select sm mono" value={c.version?.id ?? ''} onChange={(e) => onVersion(e.target.value)} aria-label={t('kb.manifestVersion')}>
                {versions.map((v) => (
                  <option key={v.id} value={v.id}>
                    V{v.version_no}
                    {c.current_version?.id === v.id ? ` · ${t('kb.manifestCurrent')}` : ''}
                  </option>
                ))}
                {c.version && !versions.some((v) => v.id === c.version.id) && <option value={c.version.id}>V{c.version.version_no}</option>}
              </select>
            </label>
            <CollectionStatusChip status={c.status} />
            <IndexStatusChip status={c.index_status} />
            {!isCurrent && (
              <span className="chip stale">
                <History aria-hidden /> {t('kb.manifestHistorical')}
              </span>
            )}
          </div>
          <dl className="kb-detail-facts small">
            {showProject && (
              <div>
                <dt>{t('rx.detail.project')}</dt>
                <dd className="ellip" title={`${project.name} · ${project.code}`}>
                  {project.name} · <span className="mono">{project.code}</span>
                </dd>
              </div>
            )}
            <div>
              <dt>{t('kb.sourceRun')}</dt>
              <dd>
                <Link to={paths.collaborationRun(c.run_id)} className="as-use-link">
                  <PlayCircle aria-hidden className="inline-ico" /> {c.run_title || t('as.use.untitledRun')}
                </Link>
              </dd>
            </div>
            <div>
              <dt>{t('kb.scope')}</dt>
              <dd className="ellip" title={scope || undefined}>
                {scope || <span className="faint">{t('kb.noScope')}</span>}
              </dd>
            </div>
            {c.version && (
              <div>
                <dt>{t('kb.manifestHash')}</dt>
                <dd className="row">
                  <HashText hash={c.version.manifest_hash} chars={12} />
                  {c.version.archived_at && (
                    <span className="faint" title={fmt.dateTime(c.version.archived_at)}>
                      <Clock aria-hidden className="inline-ico" /> {t('kb.archivedAt', { time: fmt.dateTime(c.version.archived_at) })}
                    </span>
                  )}
                </dd>
              </div>
            )}
          </dl>
        </div>
        <div className="kb-detail-actions">
          {canReindex && (
            <button type="button" className="btn sm" disabled={reindexBusy || c.index_status === 'indexing'} onClick={() => void reindex()} title={t('kb.reindex.hint')}>
              <RefreshCw /> {reindexBusy ? t('common.working') : t('kb.reindex.action')}
            </button>
          )}
          {collaborationEnabled && caps.manage && project.status !== 'archived' && (
            <button type="button" className={`btn sm ${c.status === 'active' ? 'danger' : ''}`} onClick={() => setStatusTarget(c.status === 'active' ? 'deprecated' : 'active')}>
              {c.status === 'active' ? <Archive /> : <ArchiveRestore />} {c.status === 'active' ? t('kb.status.deprecate') : t('kb.status.restore')}
            </button>
          )}
        </div>
      </header>
      {!!reindexError && <ErrorNotice error={reindexError} />}
      {(c.index_status === 'failed' || c.index_status === 'partial') && (
        <div className="callout warn slim" role="status">
          <TriangleAlert aria-hidden />
          <span>
            {t(c.index_status === 'failed' ? 'kb.index.failedNote' : 'kb.index.partialNote')}
            {c.index_error && <span className="faint"> · {c.index_error}</span>}
          </span>
        </div>
      )}
      {c.status === 'deprecated' && (
        <div className="callout pending slim" role="status">
          <Archive aria-hidden />
          <span>{t('kb.status.deprecatedNote')}</span>
        </div>
      )}

      <TabList<Section>
        idPrefix={`kb-${c.id}`}
        label={t('kb.sections')}
        value={section}
        onChange={setSection}
        className="kb-tabs"
        tabs={[
          { id: 'formal', label: t('kb.section.formal'), icon: <BadgeCheck aria-hidden />, count: formal.length },
          { id: 'all', label: t('kb.section.all'), icon: <Layers aria-hidden />, count: items.length },
          { id: 'acceptance', label: t('kb.section.acceptance'), icon: <ClipboardCheck aria-hidden />, count: withAcceptance.length },
          { id: 'usages', label: t('kb.section.usages'), icon: <ListChecks aria-hidden />, count: usages.length },
        ]}
      />
      <div {...tabPanelProps(`kb-${c.id}`, section)} className="stack">
        {section === 'formal' && (
          <>
            {renderItems(formal, 'kb.items.noneFormal')}
            <p className="faint small">{t('kb.items.formalNote')}</p>
          </>
        )}
        {section === 'all' && renderItems(items, 'kb.items.none')}
        {section === 'acceptance' &&
          (withAcceptance.length ? (
            <ul className="kb-acc-list">
              {withAcceptance.map((i) => {
                const status = effectiveAcceptance(i.acceptance, i.kind === 'history' ? 'superseded' : i.kind);
                const who = acceptanceActor(t, i.acceptance, (id) => (id ? members.byId.get(id)?.name : undefined));
                return (
                  <li key={i.id} className="kb-acc-row">
                    <span className="row">
                      <span className="ellip strong" title={i.version.asset_name}>
                        {i.version.asset_name}
                      </span>
                      <VersionToken label={i.version.label} />
                      {i.task_id && (
                        <Link to={paths.task(i.task_id)} className="as-use-link small">
                          {i.task_key || t('kb.item.task')}
                        </Link>
                      )}
                    </span>
                    <span className="row small">
                      <AcceptanceChip acceptance={i.acceptance} kind={i.kind} />
                      {who && <span>{who}</span>}
                      {i.acceptance.decided_at && <span className="faint">{fmt.dateTime(i.acceptance.decided_at)}</span>}
                      {status === 'invalidated' && <span className="faint">{t('rs.accHint.invalidated')}</span>}
                      {i.acceptance.request_id && (
                        <Link to={paths.approvalRequest(i.acceptance.request_id)} className="as-use-link">
                          <ClipboardCheck aria-hidden className="inline-ico" /> {t('rs.detail.openAcceptance')}
                        </Link>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="faint small">{t('kb.acceptance.none')}</p>
          ))}
        {section === 'usages' && <UsageHistory usages={usages} itemLabel={itemLabel} caps={caps} onChanged={onChanged} />}
      </div>

      {collaborationEnabled && (section === 'formal' || section === 'all') && picked && <AdoptPanel key={picked.id} project={project} caps={caps} collection={c} item={picked} onDone={onChanged} />}

      <footer className="kb-detail-foot small">
        <Clock aria-hidden className="inline-ico" />
        <span>
          {t('kb.usageStats', { reference: fmt.number(c.usage_counts?.reference ?? 0), baseline: fmt.number(c.usage_counts?.baseline ?? 0) })} · {t('kb.usageStatsSource')}
        </span>
        <span className="grow" />
        <button type="button" className="btn ghost sm" onClick={() => setSection('usages')}>
          {t('kb.usageStatsAll')} →
        </button>
      </footer>

      {collaborationEnabled && statusTarget && (
        <ConfirmDialog
          title={t(statusTarget === 'deprecated' ? 'kb.status.deprecateTitle' : 'kb.status.restoreTitle', { title: c.title })}
          body={t(statusTarget === 'deprecated' ? 'kb.status.deprecateBody' : 'kb.status.restoreBody')}
          confirmLabel={statusTarget === 'deprecated' ? t('kb.status.deprecate') : t('kb.status.restore')}
          danger={statusTarget === 'deprecated'}
          busy={statusBusy}
          error={<ErrorNotice error={statusError} />}
          onConfirm={() => void changeStatus()}
          onClose={() => {
            setStatusTarget(null);
            setStatusError(null);
          }}
        />
      )}
    </div>
  );
}
