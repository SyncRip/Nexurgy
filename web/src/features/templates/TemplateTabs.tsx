import { useFeature } from '../../api/capabilities';
// Template detail tabs other than the canvas: overview (Overview), published versions
// (Published versions) and usage history (Usage records). The canvas (Configuration) is the draft editor in
// features/workflow/editor.

import { ArrowUpCircle,Ban,ChevronDown,ChevronRight,ExternalLink,FilePen,History,Library,MoreHorizontal,Pencil,Play,Rocket,Share2 } from 'lucide-react';
import { Fragment,useEffect,useMemo,useState } from 'react';
import { Link,useNavigate,useSearchParams } from 'react-router-dom';
import { getTemplateVersion,listMembers,listRuns,listTemplateVersionRuns,listWorkflows } from '../../api/endpoints';
import type { Project,TemplateVersion,UUID,Workflow } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { Chip } from '../../components/ui/Badges';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import type { Resource } from '../../hooks/useResource';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { RunTable } from '../workflow/RunTable';
import { Kv,LibrarySourceChip,PublishStateChips,VersionNo,VersionStatusChip } from './common';
import { ImportSourceReceipt } from './ImportSourceReceipt';
import { PolicyEditor } from './PolicyEditor';
import { VersionContract } from './VersionContract';

export interface TemplateTabProps {
  workflow: Workflow;
  project: Project;
  versions: Resource<TemplateVersion[]>;
  /** Owner or template manager (and not archived). */
  canManage: boolean;
  /** May start runs (workflow action). */
  canRun: boolean;
  onWorkflow: (w: Workflow) => void;
  onStartRun: (versionId?: UUID) => void;
  onPublish: () => void;
  onShare: (versionId?: UUID) => void;
  onDeprecate: (v: TemplateVersion) => void;
  onUpgrade: () => void;
}

function ContractOf({ versionId }: { versionId: UUID }) {
  const detail = useResource((signal) => getTemplateVersion(versionId, { signal }), [versionId]);
  if (detail.error && !detail.data) return <ErrorNotice error={detail.error} onRetry={detail.reload} />;
  if (!detail.data) return <SkeletonRows rows={3} cols={3} />;
  return <VersionContract detail={detail.data} />;
}

// ─── overview ──────────────────────────────────────────────────────────────

export function TemplateOverviewTab({ workflow: w, versions, canManage, canRun, onWorkflow, onStartRun, onPublish, onShare, onUpgrade }: TemplateTabProps) {
  const { t, fmt } = useI18n();
  const latest = w.latest_published ?? (versions.data ?? [])[0] ?? null;
  const current = (versions.data ?? []).find((v) => v.status === 'published') ?? null;
  const totalRuns = (versions.data ?? []).reduce((a, v) => a + (v.run_count ?? 0), 0);
  const libraryEnabled = useFeature('template_library');
  const readOnly = !canManage;

  return (
    <div className="tpl-grid">
      <div className="stack">
        <section className="panel">
          <div className="panel-h">
            <h3>{t('tpl.overview.about')}</h3>
            <span className="grow" />
            {canManage && (
              <Link className="btn ghost sm" to={paths.workflow(w.id, 'config')}>
                <Pencil /> {t('tpl.overview.editDraft')}
              </Link>
            )}
          </div>
          <div className="panel-b">{w.description ? <p className="tpl-pre">{w.description}</p> : <p className="faint">{t('tpl.overview.noDescription')}</p>}</div>
        </section>

        <section className="panel">
          <div className="panel-h">
            <h3>{current ? t('tpl.overview.contractOf', { n: current.version_no }) : t('tpl.overview.contract')}</h3>
          </div>
          <div className="panel-b">
            {current ? (
              <ContractOf versionId={current.id} />
            ) : versions.data ? (
              <EmptyState
                compact
                title={(versions.data ?? []).length ? t('tpl.overview.allDeprecated') : t('tpl.overview.notPublished')}
                body={canManage ? t('tpl.overview.notPublishedBody') : t('tpl.overview.notPublishedReadOnly')}
                action={
                  canManage ? (
                    <button type="button" className="btn primary" onClick={onPublish}>
                      <Rocket /> {t('tpl.publish.action')}
                    </button>
                  ) : undefined
                }
              />
            ) : versions.error ? (
              <ErrorNotice error={versions.error} onRetry={versions.reload} />
            ) : (
              <SkeletonRows rows={3} cols={3} />
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-h">
            <h3>{t('tpl.policy.title')}</h3>
            <span className="faint small">· {t('tpl.policy.draftNote')}</span>
          </div>
          <div className="panel-b">
            <PolicyEditor key={`${w.id}:${w.version}`} workflow={w} readOnly={readOnly} onSaved={onWorkflow} />
          </div>
        </section>
      </div>

      <aside className="stack">
        <section className="panel">
          <div className="panel-h">
            <h3>{t('tpl.overview.status')}</h3>
          </div>
          <div className="panel-b stack-sm">
            <PublishStateChips latest={latest} draftDirty={w.draft_dirty} publishedCount={w.published_count} />
            <ImportSourceReceipt source={w.import_source} canOpen={w.can_open_import_source} />
            <dl className="tpl-kv one">
              <Kv label={t('tpl.overview.latest')}>
                {latest ? (
                  <>
                    <VersionNo n={latest.version_no} /> · {fmt.dateTime(latest.published_at)}
                    {latest.published_by ? ` · ${latest.published_by.name}` : ''}
                  </>
                ) : (
                  <span className="faint">{t('tpl.chip.notPublished')}</span>
                )}
              </Kv>
              <Kv label={t('tpl.publish.draft')}>
                <span className="mono">#{w.version}</span> · {fmt.relative(w.updated_at)}
              </Kv>
              <Kv label={t('tpl.overview.runs')}>
                <Link to={paths.workflow(w.id, 'usage')} className="linkish">
                  {fmt.number(totalRuns)}
                </Link>
              </Kv>
            </dl>
            {w.library && (
              <div className="stack-sm">
                <LibrarySourceChip name={w.library.name} adopted={w.library.adopted_version_no} upgrade={w.library.upgrade_available} />
                <div className="row">
                  <Link className="btn ghost sm" to={paths.publicTemplate(w.library.public_template_id)}>
                    <Library /> {t('tpl.library.open')}
                  </Link>
                  {libraryEnabled && canManage && w.library.upgrade_available && (
                    <button type="button" className="btn sm" onClick={onUpgrade}>
                      <ArrowUpCircle /> {t('tpl.upgrade.action', { n: w.library.latest_version_no ?? '?' })}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-h">
            <h3>{t('common.actions')}</h3>
          </div>
          <div className="panel-b tpl-actions">
            {canRun && (
              <button type="button" className="btn primary" onClick={() => onStartRun()} disabled={!current && !canManage}>
                <Play /> {t('tpl.action.startRun')}
              </button>
            )}
            {canManage && (
              <button type="button" className="btn" onClick={onPublish}>
                <Rocket /> {t('tpl.publish.action')}
              </button>
            )}
            {libraryEnabled && canManage && current && (
              <button type="button" className="btn" onClick={() => onShare(current.id)}>
                <Share2 /> {t('tpl.share.action')}
              </button>
            )}
            {libraryEnabled && <Link className="btn ghost" to={paths.templateLibrary()}>
              <Library /> {t('tpl.lib.title')}
            </Link>}
          </div>
        </section>
      </aside>
    </div>
  );
}

// ─── versions ──────────────────────────────────────────────────────────────

export function TemplateVersionsTab({ workflow: w, versions, canManage, canRun, onStartRun, onPublish, onShare, onDeprecate }: TemplateTabProps) {
  const libraryEnabled = useFeature('template_library');
  const { t, fmt } = useI18n();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const requested = search.get('version');
  const [open, setOpen] = useState<UUID | null>(requested);
  useEffect(() => { setOpen(requested); }, [requested]);
  const list = versions.data ?? [];

  const actions = (v: TemplateVersion): MenuItem[] => {
    const items: MenuItem[] = [];
    if (canRun && v.status === 'published') items.push({ id: 'run', label: t('tpl.versions.runThis'), icon: <Play />, onSelect: () => onStartRun(v.id) });
    items.push({ id: 'usage', label: t('tpl.versions.usage'), icon: <History />, onSelect: () => navigate(`${paths.workflow(w.id, 'usage')}?version=${v.id}`) });
    if (canManage && v.status === 'published') {
      if (libraryEnabled) items.push({ id: 'share', label: t('tpl.share.action'), icon: <Share2 />, onSelect: () => onShare(v.id) });
      items.push({ id: 'deprecate', label: t('tpl.deprecate.action'), icon: <Ban />, danger: true, separator: true, onSelect: () => onDeprecate(v) });
    }
    return items;
  };

  let body;
  if (versions.error && !versions.data) body = <ErrorState error={versions.error} onRetry={versions.reload} />;
  else if (versions.initial) body = <SkeletonRows cols={5} rows={3} />;
  else if (!list.length)
    body = (
      <EmptyState
        title={t('tpl.overview.notPublished')}
        body={canManage ? t('tpl.overview.notPublishedBody') : t('tpl.overview.notPublishedReadOnly')}
        action={
          canManage ? (
            <button type="button" className="btn primary" onClick={onPublish}>
              <Rocket /> {t('tpl.publish.action')}
            </button>
          ) : undefined
        }
      />
    );
  else
    body = (
      <div className={`table-wrap ${versions.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('tpl.versions.col.version')}</th>
              <th className="hide-sm">{t('tpl.versions.col.published')}</th>
              <th>{t('tpl.versions.col.notes')}</th>
              <th className="hide-md">{t('tpl.versions.col.source')}</th>
              <th className="hide-sm">{t('tpl.versions.col.runs')}</th>
              <th className="act">
                <span className="sr-only">{t('common.actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {list.map((v) => {
              const expanded = open === v.id;
              return (
                <Fragment key={v.id}>
                  <tr className={v.status === 'deprecated' ? 'is-archived' : undefined}>
                    <td>
                      <div className="cell-main">
                        <button type="button" className="btn ghost icon sm" aria-expanded={expanded} aria-label={t('tpl.versions.details', { n: v.version_no })} onClick={() => setOpen(expanded ? null : v.id)}>
                          {expanded ? <ChevronDown /> : <ChevronRight />}
                        </button>
                        <VersionNo n={v.version_no} />
                        <VersionStatusChip status={v.status} />
                      </div>
                      <div className="only-sm faint small">{fmt.date(v.published_at)}</div>
                    </td>
                    <td className="hide-sm small">
                      <span title={fmt.dateTime(v.published_at)}>{fmt.date(v.published_at)}</span>
                      {v.published_by && <div className="faint">{v.published_by.name}</div>}
                    </td>
                    <td className="small">
                      <span className="tpl-clamp">{v.notes || <span className="faint">{t('tpl.versions.noNotes')}</span>}</span>
                      {v.status === 'deprecated' && v.deprecation_reason && <div className="faint">{t('tpl.versions.deprecatedBecause', { reason: v.deprecation_reason })}</div>}
                    </td>
                    <td className="hide-md small">
                      {v.import_source ? <ImportSourceReceipt source={v.import_source} canOpen={v.can_open_import_source} /> : v.source_public_version ? (
                        <Link to={paths.publicTemplate(v.source_public_version.public_template_id)} className="tpl-ref">
                          <Library aria-hidden className="inline-ico" /> <span className="ellip">{v.source_public_version.name}</span> <VersionNo n={v.source_public_version.version_no} />
                        </Link>
                      ) : (
                        <span className="faint">{t('tpl.versions.fromDraft', { n: v.draft_version })}</span>
                      )}
                    </td>
                    <td className="hide-sm mono small">
                      <Link to={`${paths.workflow(w.id, 'usage')}?version=${v.id}`}>{fmt.number(v.run_count ?? 0)}</Link>
                    </td>
                    <td className="act">
                      <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actions(v)} />
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="sub-row">
                      <td colSpan={6}>
                        <div className="stack-sm tpl-version-detail">
                          <p className="faint small mono" title={v.hash}>
                            {t('tpl.versions.hash')}: {v.hash.slice(0, 16)}
                          </p>
                          <ImportSourceReceipt source={v.import_source} canOpen={v.can_open_import_source} />
                          <ContractOf versionId={v.id} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="stack">
      <div className="toolbar">
        <p className="muted small grow">{t('tpl.versions.intro')}</p>
        {w.draft_dirty && (
          <Chip tone="warn" icon={<FilePen aria-hidden />} title={t('tpl.chip.draftChangedHint')}>
            {t('tpl.chip.draftChanged')}
          </Chip>
        )}
        {canManage && (
          <button type="button" className="btn primary" onClick={onPublish}>
            <Rocket /> {t('tpl.publish.action')}
          </button>
        )}
      </div>
      {requested && versions.data && !list.some((version) => version.id === requested) && <p className="callout err">{t('ver.missing')}</p>}
      {versions.error && versions.data ? <ErrorNotice error={versions.error} onRetry={versions.reload} /> : null}
      {body}
    </div>
  );
}

// ─── usage ─────────────────────────────────────────────────────────────────

const PAGE = 50;

export function TemplateUsageTab({ workflow: w, project, versions, versionFilter, onVersionFilter }: TemplateTabProps & { versionFilter: string; onVersionFilter: (v: string) => void }) {
  const { t } = useI18n();
  const me = useUser();
  const [limit, setLimit] = useState(PAGE);
  const runs = useResource(
    (signal) => (versionFilter ? listTemplateVersionRuns(versionFilter, { signal }) : listRuns(project.id, { workflow_id: w.id, limit: limit + 1 }, { signal })),
    [project.id, w.id, versionFilter, limit],
  );
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const memberName = useMemo(() => new Map((members.data ?? []).map((m) => [m.user.id, m.user.name])), [members.data]);
  const wfs = useResource((signal) => listWorkflows(project.id, { include_archived: true }, { signal }), [project.id]);
  const all = (runs.data ?? []).filter((r) => r.workflow_id === w.id);
  const hasMore = !versionFilter && all.length > limit;
  const shown = versionFilter ? all : all.slice(0, limit);

  return (
    <div className="stack">
      <div className="toolbar">
        <select className="select" value={versionFilter} onChange={(e) => onVersionFilter(e.target.value)} aria-label={t('tpl.mon.filterVersion')}>
          <option value="">{t('tpl.mon.allVersions')}</option>
          {(versions.data ?? []).map((v) => (
            <option key={v.id} value={v.id}>
              v{v.version_no}
              {v.status === 'deprecated' ? ` · ${t('tpl.status.deprecated')}` : ''}
            </option>
          ))}
        </select>
        <span className="grow" />
        <Link className="btn ghost sm" to={paths.runs(project.id, { workflow: w.id, version: versionFilter || undefined })}>
          <ExternalLink /> {t('tpl.usage.openMonitor')}
        </Link>
      </div>
      {runs.error && !runs.data ? (
        <ErrorState error={runs.error} onRetry={runs.reload} />
      ) : runs.initial ? (
        <SkeletonRows cols={6} />
      ) : !shown.length ? (
        <EmptyState compact title={t('tpl.usage.empty')} body={t('tpl.usage.emptyBody')} />
      ) : (
        <>
          <RunTable
            runs={shown}
            refreshing={runs.loading}
            workflowName={(id) => wfs.data?.find((x) => x.id === id)?.name}
            personName={(id) => (id ? (id === me.id ? t('common.you') : memberName.get(id) ?? id.slice(0, 8)) : '—')}
          />
          {hasMore && (
            <div className="tpl-more">
              <button type="button" className="btn" onClick={() => setLimit((n) => n + PAGE)} disabled={runs.loading}>
                {t('tpl.mon.loadMore')}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
