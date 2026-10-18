// "Materials" — one list over the catalogue adapter for every scope (handoff §5–6): toolbar (search → type → creation →
// part → more → add), "N materials" heading, the table of latest versions and the detail of the item named by the URL.
// Filters live in the URL (q, type, subtype, source, part_id, status, archived, item, version — scope.ts) so refresh,
// back and professional pages' return links restore the view.

import { ChevronDown, FilterX, RefreshCw, TriangleAlert, Waypoints, X } from 'lucide-react';
import { useMemo, type KeyboardEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { listParts } from '../../api/endpoints';
import { useFeature } from '../../api/capabilities';
import type { Project } from '../../api/types';
import { useShell } from '../../components/shell/ShellContext';
import { Chip } from '../../components/ui/Badges';
import { SearchInput } from '../../components/ui/Form';
import { Popover } from '../../components/ui/Popover';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { hasKey } from '../../i18n/core';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { VersionToken } from '../assets/meta';
import { useAssetQuery, useUrlSearchText } from '../assets/useAssetQuery';
import { AddMaterialMenu } from './AddMaterial';
import { CatalogDetail } from './CatalogDetail';
import { catalogCounts, filterCatalog, findCatalogItem, formalBesideLatest } from './catalog';
import { creationLabel, previewLabel, statusLabel, subtypeLabel, typeLabel } from './meta';
import { catalogNeighbors } from './navigation';
import { MATERIAL_FILTER_KEYS, isFiltered, readMaterialQuery, sameScope, scopeUrl } from './scope';
import { BUSINESS_TYPES, CATALOG_STATUSES, CREATION_METHODS, type BusinessType, type CatalogItem, type CatalogState, type CreationMethod, type ViewScope } from './types';

/** Short tag of a type in the name cell (codes, not words: same in both languages). */
const TYPE_TAG: Record<BusinessType, string> = {
  model: '3D',
  drawing: '2D',
  spec: 'SPEC',
  process_plan: 'PLAN',
  quote: 'QUO',
  document: 'DOC',
  process_module: 'MOD',
  intake: 'INT',
  delivery: 'ZIP',
};

/** A document shows its file extension (display only; the type itself never comes from the extension). */
function typeTag(item: CatalogItem): string {
  if (item.type === 'document') {
    const m = /\.([a-z0-9]{1,4})$/i.exec(item.name);
    if (m) return m[1].toUpperCase();
  }
  return TYPE_TAG[item.type];
}

function sourceText(t: TFunction, source: string): string {
  const key = `rp.source.${source}`;
  return hasKey(key) ? t(key) : source;
}

export interface MaterialsViewProps {
  scope: ViewScope;
  /** The scope's project (fresh row) in a project scope, else null. */
  project: Project | null;
  /** Accessible projects (rows for the detail's caps, save locations of "Add material"). */
  projects: readonly Project[];
  catalog: CatalogState;
  /** Switch the page scope (keeps only the scope keys, plus `extra`). */
  onScope: (scope: ViewScope, extra?: Record<string, string>) => void;
}

export default function MaterialsView({ scope, project, projects, catalog, onScope }: MaterialsViewProps) {
  const { t, tn, fmt } = useI18n();
  const { listDensity, setListDensity } = useShell();
  const collaborationEnabled = useFeature('collaboration');
  const navigate = useNavigate();
  const location = useLocation();
  const query = useAssetQuery();
  const { search, patch, clear } = query;
  const [q, setQ] = useUrlSearchText(query, 'q');
  const { filter } = readMaterialQuery(search, scope);
  const itemKey = search.get('item');
  const versionId = search.get('version');
  const all = scope.kind === 'all';
  const projectScope = scope.kind === 'project';

  const parts = useResource((signal) => listParts(project!.id, {}, { signal }), [project?.id], { enabled: projectScope && !!project });

  const { q: fq, type, subtype, creation, partId, status, archived } = filter;
  const rows = useMemo(
    () => filterCatalog(catalog.items, { q: fq, type, subtype, creation, partId, status, archived }),
    [catalog.items, fq, type, subtype, creation, partId, status, archived],
  );
  const counts = useMemo(() => catalogCounts(catalog.items), [catalog.items]);
  const hasUnknown = creation === 'unknown' || catalog.items.some((i) => i.creation === 'unknown');
  const filtered = isFiltered(filter);

  const projectsById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);
  const selected = itemKey ? findCatalogItem(catalog.items, itemKey) : undefined;
  const neighbors = catalogNeighbors(rows, selected?.key ?? null);
  const detailProject = selected ? (project && selected.project.id === project.id ? project : projectsById.get(selected.project.id)) : undefined;
  const settled = !catalog.initial && !catalog.loading;
  const missingItem = !!itemKey && settled && !(selected && detailProject);

  const clearFilters = () => {
    setQ('');
    clear(MATERIAL_FILTER_KEYS);
  };
  const open = (item: CatalogItem) => patch({ item: item.key, version: null });
  const close = () => patch({ item: null, version: null });
  const onRowKey = (e: KeyboardEvent, item: CatalogItem) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(item);
    }
  };
  const onCreated = ({ projectId, itemKey: key }: { projectId: string; itemKey?: string }) => {
    const target: ViewScope = { kind: 'project', projectId };
    if (sameScope(target, scope)) {
      catalog.reload();
      if (key) patch({ item: key, version: null });
    } else onScope(target, key ? { item: key } : undefined);
  };

  const addMenu = <AddMaterialMenu scope={scope} projects={projects} onCreated={onCreated} />;
  const activeFilters: { key: string; label: string; remove: () => void }[] = [];
  if (fq) activeFilters.push({ key: 'q', label: t('rp.filters.search', { q: fq }), remove: () => { setQ(''); patch({ q: null }); } });
  if (type) activeFilters.push({ key: 'type', label: typeLabel(t, type), remove: () => patch({ type: null, subtype: null }) });
  if (subtype) activeFilters.push({ key: 'subtype', label: subtypeLabel(t, subtype), remove: () => patch({ subtype: null }) });
  if (creation) activeFilters.push({ key: 'source', label: creationLabel(t, creation), remove: () => patch({ source: null }) });
  if (partId) activeFilters.push({ key: 'part_id', label: parts.data?.find(p => p.id === partId)?.number ?? partId.slice(0, 8), remove: () => patch({ part_id: null }) });
  if (status) activeFilters.push({ key: 'status', label: statusLabel(t, status), remove: () => patch({ status: null }) });
  if (archived) activeFilters.push({ key: 'archived', label: t('rp.more.archived'), remove: () => patch({ archived: null }) });

  const loads = catalog.projects;
  const failed = loads.filter((p) => p.status === 'error');
  const loading = loads.filter((p) => p.status === 'loading');
  const truncated = loads.filter((p) => p.truncated?.length);
  const projectFailed = projectScope && failed.length > 0;

  let body;
  if (catalog.initial && !projectFailed) body = <SkeletonRows cols={all ? 8 : 7} />;
  else if (projectFailed && !catalog.items.length) body = <ErrorState title={t('rp.load.projectFailed')} error={failed[0].error} onRetry={catalog.reload} />;
  else if (!counts.active && !filtered)
    body = (
      <EmptyState
        compact
        title={t('rp.empty.title')}
        body={counts.archived ? `${t('rp.empty.body')} ${t('rp.empty.archivedHint')}` : t('rp.empty.body')}
      />
    );
  else if (!rows.length)
    body = (
      <EmptyState
        compact
        title={t('rp.noMatch.title')}
        body={t('rp.noMatch.body')}
        action={
          <button type="button" className="btn" onClick={clearFilters}>
            <FilterX aria-hidden /> {t('rp.more.clear')}
          </button>
        }
      />
    );
  else
    body = (
      <div className={`table-wrap rp-table-wrap ${catalog.loading ? 'is-refreshing' : ''}`}>
        <table className={`t rp-table ${all ? 'with-project' : ''}`}>
          <caption className="sr-only">{t('rp.table.caption')}</caption>
          <thead>
            <tr>
              <th className="rp-col-name">{t('rp.col.name')}</th>
              <th className="rp-col-type">{t('rp.col.type')}</th>
              <th className="rp-col-creation">{t('rp.col.creation')}</th>
              {all && <th className="rp-col-project">{t('rp.col.project')}</th>}
              <th className="rp-col-version">{t('rp.col.version')}</th>
              <th className="rp-col-preview">{t('rp.col.preview')}</th>
              <th className="rp-col-time">{t('rp.col.updated')}</th>
              <th className="rp-col-act">{t('rp.col.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => {
              const v = item.latest;
              const sub = [...item.context, ...(item.archived ? [t('common.archived')] : [])];
              return (
                <tr
                  key={item.key}
                  className={`clickable ${item.archived ? 'is-archived' : ''} ${selected?.key === item.key ? 'is-current' : ''}`}
                  tabIndex={0}
                  aria-label={t('rp.row.open', { name: item.name })}
                  onClick={(event) => {
                    event.currentTarget.focus({ preventScroll: true });
                    open(item);
                  }}
                  onKeyDown={(e) => onRowKey(e, item)}
                >
                  <td className="rp-col-name">
                    <div className="rp-name">
                      <span className={`rp-tag rp-tag-${item.type} ${item.creation === 'upload' ? 'is-upload' : ''}`} aria-hidden>
                        {typeTag(item)}
                      </span>
                      <div className="rp-name-copy">
                        <span className="rp-name-text" title={item.name}>
                          {item.name}
                        </span>
                        {sub.length > 0 && (
                          <span className="rp-name-sub" title={sub.join(' · ')}>
                            {sub.join(' · ')}
                          </span>
                        )}
                        <span className="rp-name-meta">
                          {typeLabel(t, item.type)} · {creationLabel(t, item.creation)}
                          {all ? ` · ${item.project.code}` : ''}
                        </span>
                      </div>
                    </div>
                  </td>
                  <td className="rp-col-type">{typeLabel(t, item.type)}</td>
                  <td className="rp-col-creation">
                    <span className={`rp-creation is-${item.creation}`}>{creationLabel(t, item.creation)}</span>
                  </td>
                  {all && (
                    <td className="rp-col-project mono" title={item.project.name}>
                      {item.project.code}
                    </td>
                  )}
                  <td className="rp-col-version">
                    {v ? (
                      <div className="rp-version">
                        <VersionToken label={v.label} />
                        {formalBesideLatest(item).map((f) => (
                          <Chip key={f.versionId} tone="measure" title={t('rp.chip.formalTitle', { tasks: f.taskNumbers.map((n) => `#${n}`).join(', ') || '—', label: f.label })}>
                            {t('rp.chip.formal', { label: f.label })}
                          </Chip>
                        ))}
                        {v.publishState === 'deprecated' && (
                          <Chip tone="warn" title={statusLabel(t, 'deprecated')}>
                            {t('rp.chip.deprecated')}
                          </Chip>
                        )}
                        {item.upstreamNewer && (
                          <Chip tone="stale" title={statusLabel(t, 'upstream_newer')}>
                            {t('rp.chip.upstream')}
                          </Chip>
                        )}
                        {v.availability === 'failed' && (
                          <Chip tone="err" icon={<TriangleAlert aria-hidden />} title={statusLabel(t, 'failed')}>
                            {t('rp.chip.failed')}
                          </Chip>
                        )}
                      </div>
                    ) : (
                      <span className="faint small">{t('rp.noVersion')}</span>
                    )}
                  </td>
                  <td className={`rp-col-preview small is-${item.preview}`}>{previewLabel(t, item.preview)}</td>
                  <td className="rp-col-time faint small" title={fmt.dateTime(item.updatedAt)}>
                    {fmt.relative(item.updatedAt)}
                  </td>
                  <td className="rp-col-act">
                    <button
                      type="button"
                      className="linkish small"
                      tabIndex={-1}
                      onClick={(e) => {
                        e.stopPropagation();
                        e.currentTarget.closest('tr')?.focus({ preventScroll: true });
                        open(item);
                      }}
                    >
                      {t('rp.row.view')}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="rp-materials" data-density={listDensity}>
      <div className="rp-toolbar">
        <div className="rp-field rp-search">
          <span className="rp-field-label" aria-hidden>
            {t('rp.search.label')}
          </span>
          <SearchInput value={q} onChange={setQ} placeholder={t('rp.search.placeholder')} label={t('rp.search.label')} />
        </div>
        <label className="rp-field">
          <span className="rp-field-label">{t('rp.filter.type')}</span>
          <select className="select" value={type} onChange={(e) => patch({ type: e.target.value, subtype: null })}>
            <option value="">{t('rp.filter.allTypes')}</option>
            {BUSINESS_TYPES.map((ty) => (
              <option key={ty} value={ty}>
                {typeLabel(t, ty)}
              </option>
            ))}
          </select>
        </label>
        <label className="rp-field">
          <span className="rp-field-label">{t('rp.filter.creation')}</span>
          <select className="select" value={creation} onChange={(e) => patch({ source: e.target.value })}>
            <option value="">{t('rp.filter.allCreation')}</option>
            {([...CREATION_METHODS, ...(hasUnknown ? ['unknown' as const] : [])] as CreationMethod[]).map((c) => (
              <option key={c} value={c}>
                {creationLabel(t, c)}
              </option>
            ))}
          </select>
        </label>
        {projectScope && (
          <label className="rp-field">
            <span className="rp-field-label">{t('rp.filter.part')}</span>
            <select className="select" value={partId} onChange={(e) => patch({ part_id: e.target.value })} disabled={!parts.data?.length && !partId}>
              <option value="">{t('rp.filter.allParts')}</option>
              {partId && !(parts.data ?? []).some((p) => p.id === partId) && <option value={partId}>{partId.slice(0, 8)}…</option>}
              {(parts.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.number} · {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className="rp-toolbar-spacer" />
        <div className="rp-toolbar-actions">
          <Popover
            label={t('rp.more')}
            triggerClassName={`btn ${status || archived ? 'active' : ''}`}
            trigger={
              <>
                {t('rp.more')} <ChevronDown aria-hidden />
              </>
            }
          >
            {(closeMore) => (
              <div className="rp-more">
                <label className="field">
                  <span>{t('rp.filter.status')}</span>
                  <select className="select" value={status} onChange={(e) => patch({ status: e.target.value })}>
                    <option value="">{t('rp.filter.allStatus')}</option>
                    {CATALOG_STATUSES.map((s) => (
                      <option key={s} value={s}>
                        {statusLabel(t, s)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="check">
                  <input type="checkbox" checked={archived} onChange={(e) => patch({ archived: e.target.checked })} />
                  {t('rp.more.archived')}
                </label>
                <hr />
                {collaborationEnabled && <button
                  type="button"
                  className="menu-item"
                  disabled={!projectScope}
                  aria-describedby={projectScope ? undefined : 'rp-records-reason'}
                  onClick={() => {
                    closeMore();
                    navigate(scopeUrl('process', scope));
                  }}
                >
                  <Waypoints aria-hidden /> {t('rp.more.records')}
                </button>}
                {collaborationEnabled && !projectScope && (
                  <p className="faint small rp-more-reason" id="rp-records-reason">
                    {t('rp.more.recordsNeedProject')}
                  </p>
                )}
                <button
                  type="button"
                  className="menu-item"
                  disabled={!filtered}
                  onClick={() => {
                    closeMore();
                    clearFilters();
                  }}
                >
                  <FilterX aria-hidden /> {t('rp.more.clear')}
                </button>
              </div>
            )}
          </Popover>
          {addMenu}
        </div>
      </div>

      {!!activeFilters.length && (
        <div className="rp-chips" role="group" aria-label={t('rp.filters.active')}>
          {activeFilters.map(({ key, label, remove }) => (
            <button key={key} className="rp-filter-chip" type="button" onClick={remove} aria-label={t('rp.filters.remove', { label })} title={label}>
              <span>{label}</span><X aria-hidden />
            </button>
          ))}
        </div>
      )}

      {missingItem && (
        <div className="callout warn rp-notice" role="status">
          <TriangleAlert aria-hidden />
          <div className="grow">{t('rp.item.missing')}</div>
          <button type="button" className="btn ghost icon sm" aria-label={t('rp.notice.dismiss')} onClick={close}>
            <X />
          </button>
        </div>
      )}

      <div className="rp-list-head">
        <strong aria-live="polite">{catalog.initial ? t('common.loading') : tn('rp.count', rows.length, { count: fmt.number(rows.length) })}</strong>
        {filtered && (
          <button type="button" className="linkish small" onClick={clearFilters}>
            {t('rp.more.clear')}
          </button>
        )}
        <label className="rp-density">
          <span>{t('layout.density')}</span>
          <select className="select sm" aria-label={t('layout.density')} value={listDensity} onChange={(event) => setListDensity(event.target.value === 'compact' ? 'compact' : 'comfortable')}>
            <option value="comfortable">{t('layout.comfortable')}</option>
            <option value="compact">{t('layout.compact')}</option>
          </select>
        </label>
      </div>

      {(all || failed.length > 0 || truncated.length > 0) && (
        <div className="rp-load small" role="status">
          {all && (
            <span>
              {t('rp.load.status', { loaded: loads.filter((p) => p.status === 'ok').length, total: loads.length })}
              {loading.length > 0 && <span className="faint"> · {t('rp.load.loading', { count: loading.length })}</span>}
            </span>
          )}
          {failed.length > 0 && !(projectFailed && !catalog.items.length) && (
            <span className="rp-load-failed">
              <TriangleAlert aria-hidden /> {t('rp.load.failed')}
              {failed.map((p) => (
                <button key={p.project.id} type="button" className="btn sm" onClick={() => catalog.retryProject(p.project.id)} title={p.project.name}>
                  <RefreshCw aria-hidden /> {t('rp.load.retry', { code: p.project.code })}
                </button>
              ))}
            </span>
          )}
          {truncated.map((p) => (
            <span key={p.project.id} className="faint">
              {t('rp.load.truncated', { code: p.project.code, sources: (p.truncated ?? []).map((s) => sourceText(t, s)).join(' · ') })}
            </span>
          ))}
        </div>
      )}

      {body}

      {selected && detailProject && (
        <CatalogDetail
          item={selected}
          project={detailProject}
          versionId={versionId}
          onVersion={(v) => patch({ version: v })}
          onClose={close}
          onChanged={catalog.reload}
          returnTo={`${location.pathname}${location.search}`}
          navigation={neighbors.index < 0 ? undefined : { ...neighbors, onSelect: (key) => patch({ item: key, version: null }) }}
        />
      )}
    </div>
  );
}
