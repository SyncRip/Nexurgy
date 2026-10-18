// Process resources tab of the asset center (UI04, design §5.3 / C10): every
// row is one use relationship of an exact asset version in a run / node / slot,
// so one file used in several nodes appears as several rows and the counts are
// counts of relationships. Filters, paging and search live in the URL; the
// server filters, pages and computes the six-kind facets. The selected row's
// panel shows publish state, this task's acceptance and knowledge eligibility
// as three separate judgements and links to the exact version page.

import { ClipboardCheck, Download, Eye, History, PlayCircle, RotateCcw, TriangleAlert } from 'lucide-react';
import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ASSET_TYPES, PUBLISH_STATES, getAssetVersionUsages } from '../../api/assets';
import { downloadBlob } from '../../api/blobs';
import { listMembers, listRuns } from '../../api/endpoints';
import {
  AVAILABILITIES,
  RESOURCE_KINDS,
  RESOURCE_PAGE_DEFAULT,
  RESOURCE_PAGE_MAX,
  RESOURCE_TASK_STATUSES,
  effectiveAcceptance,
  facetsByKind,
  listProjectResources,
  listRunResources,
  type ResourceKind,
  type WorkflowResourceItem,
} from '../../api/resources';
import { paths, withProjectParam } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { describeError } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import type { AssetTabContentProps } from './tabProps';
import { AcceptanceChip, ExecutionModeChip, ResourceKindChip, ReusableChip, TaskLink, acceptanceActor, resourceKindText, runPageOf } from './collabMeta';
import { AssetTypeBadge, AssetTypeIcon, HashText, PublishChip, VersionToken, assetTypeLabel, dynLabel } from './meta';
import { Pager } from './Pager';
import { enumParam, readPaging } from './urlState';
import { useAssetQuery, useUrlSearchText } from './useAssetQuery';
import { Facts } from './VersionSummary';

const FILTER_KEYS = ['q', 'run_id', 'node_id', 'kind', 'asset_type', 'availability', 'publish_state', 'task_status', 'limit', 'offset'] as const;

// ─── facet chips ────────────────────────────────────────────────────────────

const FACET_TONE: Record<ResourceKind, string> = { input: 'measure', candidate: 'pending', formal: 'ok', failed: 'err', superseded: 'warn', evidence: 'stale' };

function KindFacets({ total, byKind, active, onPick }: { total: number; byKind: Record<ResourceKind, number>; active: ResourceKind | ''; onPick: (k: ResourceKind | '') => void }) {
  const { t, tn, fmt } = useI18n();
  return (
    <div className="rs-facets" role="group" aria-label={t('rs.facets.label')}>
      <span className="strong">{tn('rs.relationCount', total, { count: fmt.number(total) })}</span>
      {RESOURCE_KINDS.map((k) => (
        <button
          key={k}
          type="button"
          className={`rs-facet chip ${FACET_TONE[k]} ${active === k ? 'is-active' : ''}`}
          aria-pressed={active === k}
          onClick={() => onPick(active === k ? '' : k)}
          title={t('rs.facets.pick', { kind: resourceKindText(t, k) })}
        >
          <span>{resourceKindText(t, k)}</span>
          <span className="mono strong">{fmt.number(byKind[k])}</span>
        </button>
      ))}
    </div>
  );
}

// ─── detail panel ───────────────────────────────────────────────────────────

function DownloadButton({ item, fallbackTo }: { item: WorkflowResourceItem; fallbackTo: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const summary = (item.asset.target?.summary ?? {}) as { blob_sha256?: unknown; original_name?: unknown };
  const sha = item.asset.asset_type === 'document' && typeof summary.blob_sha256 === 'string' ? summary.blob_sha256 : null;
  if (!sha)
    return (
      <Link className="btn sm" to={fallbackTo} title={t('rs.detail.downloadOnPage')}>
        <Download /> {t('common.download')}
      </Link>
    );
  const name = typeof summary.original_name === 'string' && summary.original_name ? summary.original_name : item.asset.asset_name;
  return (
    <button
      type="button"
      className="btn sm"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await downloadBlob(sha, name);
        } catch (e) {
          toast(`${t('error.downloadFailed')} ${t(describeError(e).key)}`, { tone: 'err' });
        } finally {
          setBusy(false);
        }
      }}
    >
      <Download /> {busy ? t('common.working') : t('common.download')}
    </button>
  );
}

function ResourceDetail({ item, projectId, returnTo, memberName }: { item: WorkflowResourceItem; projectId: string; returnTo: string; memberName: (id: string | null | undefined) => string | undefined }) {
  const { t, fmt } = useI18n();
  const a = item.asset;
  const acc = item.task_acceptance;
  const status = effectiveAcceptance(acc, item.kind);
  // Knowledge eligibility and later use come from the version's own relations (C11), never from kind or publish state.
  const usages = useResource((signal) => getAssetVersionUsages(item.asset_version_id, { relation: 'all', limit: 50 }, { signal }), [item.asset_version_id]);
  const versionItems = usages.data?.knowledge_items ?? [];
  // Eligibility belongs to this use relationship: the knowledge items archived from this exact resource row.
  const knowledgeItems = versionItems.filter((k) => k.resource_id === item.id);
  const knowledgeUsages = usages.data?.knowledge_usages ?? [];
  const collections = useMemo(() => {
    const seen = new Map<string, (typeof versionItems)[number]>();
    for (const k of knowledgeItems.length ? knowledgeItems : versionItems) if (!seen.has(k.collection_id)) seen.set(k.collection_id, k);
    return [...seen.values()];
  }, [knowledgeItems, versionItems]);
  const usageTotal = usages.data?.pagination?.knowledge_usages?.total ?? knowledgeUsages.length;
  const refCount = knowledgeUsages.filter((u) => u.usage === 'reference').length;
  const baseCount = knowledgeUsages.filter((u) => u.usage === 'baseline').length;
  const versionPage = paths.assetVersion(a.asset_id, item.asset_version_id, returnTo);
  const decider = acceptanceActor(t, acc, memberName);

  let knowledge: ReactNode;
  if (usages.error && !usages.data) knowledge = <span className="faint small">{t('rs.detail.knowledgeUnavailable')}</span>;
  else if (!usages.data) knowledge = <Skeleton w="70%" h={18} />;
  else if (!knowledgeItems.length) knowledge = <Chip tone="pending">{t('rs.detail.notArchived')}</Chip>;
  else knowledge = <ReusableChip reusable={knowledgeItems.some((k) => k.reusable)} />;

  return (
    <aside className="panel rs-detail" aria-label={t('rs.detail.label')}>
      <div className="panel-h">
        <AssetTypeIcon type={a.asset_type} />
        <h2 className="ellip" title={a.asset_name}>
          {a.asset_name}
        </h2>
        <VersionToken label={a.label} />
      </div>
      <div className="panel-b stack">
        <div className="row small">
          <AssetTypeBadge type={a.asset_type} />
          <HashText hash={a.content_hash ?? null} chars={14} />
        </div>
        <dl className="rs-states">
          <div className="rs-state">
            <dt>{t('rs.detail.publish')}</dt>
            <dd>
              <PublishChip state={a.publish_state} />
            </dd>
          </div>
          <div className="rs-state">
            <dt>{t('rs.detail.acceptance')}</dt>
            <dd>
              <AcceptanceChip acceptance={acc} kind={item.kind} />
            </dd>
          </div>
          <div className="rs-state">
            <dt>{t('rs.detail.knowledge')}</dt>
            <dd>{knowledge}</dd>
          </div>
        </dl>
        <Facts
          rows={[
            [t('rs.detail.use'), <ResourceUse item={item} />],
            [
              t('rs.detail.sourceRun'),
              <span className="row">
                <Link to={runPageOf(item.run_id, item.node_id, item.execution_mode)} className="as-use-link">
                  <PlayCircle aria-hidden className="inline-ico" /> <span className="ellip">{item.run_title || t('as.use.untitledRun')}</span>
                </Link>
                <ExecutionModeChip mode={item.execution_mode} />
              </span>,
            ],
            [
              t('rs.detail.node'),
              item.task_id ? (
                <span className="row">
                  {item.node_id && <span className="mono small faint">{item.node_id}</span>}
                  <TaskLink id={item.task_id} number={item.task_number} title={item.task_title} />
                  {item.task_status && <span className="faint small">{dynLabel(t, `as.taskStatus.${item.task_status}`, item.task_status)}</span>}
                </span>
              ) : (
                <span className="faint small">{t('rs.detail.noNode')}</span>
              ),
            ],
            [
              t('rs.detail.finalAcceptance'),
              acc.decision_id ? (
                <span className="stack-sm">
                  <span>
                    {decider}
                    {acc.decided_at && <span className="faint"> · {fmt.dateTime(acc.decided_at)}</span>}
                    {status === 'invalidated' && <span className="faint"> · {t('rs.acc.invalidated')}</span>}
                  </span>
                  {acc.request_id && (
                    <Link to={paths.approvalRequest(acc.request_id)} className="as-use-link small">
                      <ClipboardCheck aria-hidden className="inline-ico" /> {t('rs.detail.openAcceptance')}
                    </Link>
                  )}
                </span>
              ) : acc.request_id ? (
                <Link to={paths.approvalRequest(acc.request_id)} className="as-use-link small">
                  <ClipboardCheck aria-hidden className="inline-ico" /> {t('rs.detail.acceptancePending')}
                </Link>
              ) : (
                <span className="faint small">{t(`rs.acc.${status}`)}</span>
              ),
            ],
            [
              t('rs.detail.collections'),
              collections.length ? (
                <span className="stack-sm">
                  {collections.map((k) => (
                    <Link key={k.collection_id} to={paths.knowledge(projectId, k.collection_id, k.collection_version_id)} className="as-use-link small">
                      <span className="ellip">{k.collection_title || t('rs.detail.collection')}</span> · {t('rs.detail.openCollection')}
                    </Link>
                  ))}
                </span>
              ) : (
                <span className="faint small">{usages.data ? t('rs.detail.noCollections') : '…'}</span>
              ),
            ],
            [
              t('rs.detail.laterUse'),
              usages.data ? (
                <span>
                  {t('rs.detail.useCounts', { reference: fmt.number(refCount), baseline: fmt.number(baseCount) })}
                  {usageTotal > knowledgeUsages.length && <span className="faint"> · {t('rs.detail.useTotal', { total: fmt.number(usageTotal) })}</span>}
                </span>
              ) : (
                <span className="faint small">…</span>
              ),
            ],
          ]}
        />
        <div className="row rs-actions">
          <Link className="btn sm" to={versionPage}>
            <Eye /> {t('rs.detail.preview')}
          </Link>
          <DownloadButton item={item} fallbackTo={versionPage} />
          <Link className="btn sm" to={versionPage}>
            <History /> {t('rs.detail.history')}
          </Link>
        </div>
      </div>
    </aside>
  );
}

function ResourceUse({ item }: { item: WorkflowResourceItem }) {
  const { t, fmt } = useI18n();
  return (
    <span className="row">
      <ResourceKindChip kind={item.kind} />
      {item.slot_key && <span className="mono small">{t('rs.detail.slot', { slot: item.slot_key })}</span>}
      {item.attempt !== null && <span className="faint small">{t('as.use.attempt', { n: item.attempt })}</span>}
      {item.created_at && (
        <span className="faint small" title={fmt.dateTime(item.created_at)}>
          {fmt.relative(item.created_at)}
        </span>
      )}
    </span>
  );
}

// ─── tab ────────────────────────────────────────────────────────────────────

export default function ResourcesTab({ project, onCount }: AssetTabContentProps) {
  const { t, fmt } = useI18n();
  const location = useLocation();
  const query = useAssetQuery();
  const { search, patch, clear } = query;
  const [q, setQ] = useUrlSearchText(query, 'q');
  const dq = search.get('q') ?? '';
  const runId = search.get('run_id') ?? '';
  const nodeId = search.get('node_id') ?? '';
  const kind = enumParam(search, 'kind', RESOURCE_KINDS);
  const assetType = enumParam(search, 'asset_type', ASSET_TYPES);
  const availability = enumParam(search, 'availability', AVAILABILITIES);
  const publishState = enumParam(search, 'publish_state', PUBLISH_STATES);
  const taskStatus = enumParam(search, 'task_status', RESOURCE_TASK_STATUSES);
  const paging = readPaging(search, RESOURCE_PAGE_DEFAULT, RESOURCE_PAGE_MAX);
  const invalid = [
    ...paging.invalid,
    kind.invalid ? 'kind' : '',
    assetType.invalid ? 'asset_type' : '',
    availability.invalid ? 'availability' : '',
    publishState.invalid ? 'publish_state' : '',
    taskStatus.invalid ? 'task_status' : '',
  ].filter(Boolean);
  const filtered = !!(dq || runId || nodeId || kind.value || assetType.value || availability.value || publishState.value || taskStatus.value);

  const res = useResource(
    (signal) =>
      listProjectResources(
        project.id,
        {
          q: dq,
          run_id: runId,
          node_id: nodeId,
          kind: kind.value,
          asset_type: assetType.value,
          availability: availability.value,
          publish_state: publishState.value,
          task_status: taskStatus.value,
          limit: paging.limit,
          offset: paging.offset,
        },
        { signal },
      ),
    [project.id, dq, runId, nodeId, kind.value, assetType.value, availability.value, publishState.value, taskStatus.value, paging.limit, paging.offset],
  );
  const runs = useResource((signal) => listRuns(project.id, { limit: 200 }, { signal }), [project.id]);
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const memberName = useMemo(() => {
    const map = new Map((members.data ?? []).map((m) => [m.user.id, m.user.name]));
    return (id: string | null | undefined) => (id ? map.get(id) : undefined);
  }, [members.data]);

  const page = res.data;
  const items = page?.items ?? [];
  const total = page?.total ?? 0;
  const byKind = useMemo(() => facetsByKind(page?.facets), [page?.facets]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = items.find((i) => i.id === selectedId) ?? null;
  const returnTo = withProjectParam(`${location.pathname}${location.search}`, project.id);

  useEffect(() => {
    if (page && !filtered) onCount?.(page.total);
  }, [page, filtered, onCount]);

  // Node options: the nodes of the chosen workflow (C08), else the node ids seen on this page; the URL value stays selectable.
  const runNodes = useResource((signal) => listRunResources(runId, { limit: RESOURCE_PAGE_MAX }, { signal }), [runId], { enabled: !!runId });
  const nodeSource = runId && runNodes.data ? runNodes.data.items : items;
  const nodeOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const i of nodeSource) if (i.node_id && !seen.has(i.node_id)) seen.set(i.node_id, i.task_title ? `${i.node_id} · ${i.task_title}` : i.node_id);
    if (nodeId && !seen.has(nodeId)) seen.set(nodeId, nodeId);
    return [...seen.entries()];
  }, [nodeSource, nodeId]);
  const runOptions = useMemo(() => {
    const list = (runs.data ?? []).map((r) => [r.id, r.title || t('as.use.untitledRun')] as [string, string]);
    if (runId && !list.some(([id]) => id === runId)) list.unshift([runId, items.find((i) => i.run_id === runId)?.run_title || runId.slice(0, 8)]);
    return list;
  }, [runs.data, runId, items, t]);

  const reset = () => {
    setQ('');
    clear(FILTER_KEYS);
  };
  const onRowKey = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setSelectedId(id);
    }
  };

  let body: ReactNode;
  if (res.error && !page) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={5} />;
  else if (!items.length && !filtered) body = <EmptyState title={t('rs.empty.title')} body={t('rs.empty.body')} />;
  else if (!items.length)
    body = (
      <EmptyState
        compact
        title={t('rs.empty.noMatch')}
        body={paging.offset > 0 ? t('rs.empty.pastEnd') : undefined}
        action={
          <div className="row">
            {paging.offset > 0 && (
              <button type="button" className="btn" onClick={() => patch({ offset: 0 })}>
                {t('as.pager.first')}
              </button>
            )}
            <button type="button" className="btn" onClick={reset}>
              {t('common.clearFilters')}
            </button>
          </div>
        }
      />
    );
  else
    body = (
      <div className={`table-wrap rs-table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense rs-table">
          <caption className="sr-only">{t('rs.tableCaption')}</caption>
          <thead>
            <tr>
              <th>{t('rs.col.name')}</th>
              <th className="hide-sm">{t('rs.col.version')}</th>
              <th className="hide-sm">{t('rs.col.node')}</th>
              <th className="hide-sm">{t('rs.col.kind')}</th>
              <th>{t('rs.col.acceptance')}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr
                key={i.id}
                className={`clickable ${i.id === selectedId ? 'is-selected' : ''}`}
                tabIndex={0}
                aria-selected={i.id === selectedId}
                onClick={() => setSelectedId(i.id)}
                onKeyDown={(e) => onRowKey(e, i.id)}
              >
                <td>
                  <div className="cell-main as-name">
                    <AssetTypeIcon type={i.asset.asset_type} />
                    <span className="ellip strong" title={i.asset.asset_name}>
                      {i.asset.asset_name}
                    </span>
                    <span className="only-sm">
                      <VersionToken label={i.asset.label} />
                    </span>
                  </div>
                  <div className="faint small as-sub rs-sub">
                    <span>{assetTypeLabel(t, i.asset.asset_type)}</span>
                    {i.slot_key && <span className="mono ellip rs-slot" title={i.slot_key}>{i.slot_key}</span>}
                  </div>
                  <div className="only-sm as-mobile-meta rs-mobile-meta">
                    <ResourceKindChip kind={i.kind} />
                    {i.task_title && <span className="small ellip">{i.task_title}</span>}
                  </div>
                </td>
                <td className="hide-sm nowrap">
                  <VersionToken label={i.asset.label} />
                  {i.asset.content_hash && <span className="faint small mono"> {i.asset.content_hash.slice(0, 8)}</span>}
                </td>
                <td className="hide-sm">
                  {i.task_id ? (
                    <span className="stack-sm rs-node">
                      <span className="ellip" title={i.task_title ?? undefined}>
                        {i.task_title || i.node_id}
                      </span>
                      <span className="faint small">
                        {i.task_number !== null && <span className="mono">T-{i.task_number}</span>} <span className="ellip">{i.run_title || t('as.use.untitledRun')}</span>
                      </span>
                    </span>
                  ) : (
                    <span className="faint small">{i.run_title || t('as.use.untitledRun')}</span>
                  )}
                </td>
                <td className="hide-sm">
                  <ResourceKindChip kind={i.kind} />
                </td>
                <td>
                  <AcceptanceChip acceptance={i.task_acceptance} kind={i.kind} plain />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="stack rs-tab">
      <div className="panel rs-filters">
        <div className="panel-b stack">
          <div className="rs-filter-grid">
            <label className="field">
              <span>{t('rs.filter.run')}</span>
              <select className="select" value={runId} onChange={(e) => patch({ run_id: e.target.value, node_id: null })} disabled={!runOptions.length}>
                <option value="">{t('rs.filter.allRuns')}</option>
                {runOptions.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t('rs.filter.node')}</span>
              <select className="select" value={nodeId} onChange={(e) => patch({ node_id: e.target.value })} disabled={!nodeOptions.length}>
                <option value="">{t('rs.filter.allNodes')}</option>
                {nodeOptions.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t('rs.filter.kind')}</span>
              <select className="select" value={kind.value} onChange={(e) => patch({ kind: e.target.value })}>
                <option value="">{t('rs.filter.allKinds')}</option>
                {RESOURCE_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {resourceKindText(t, k)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t('rs.filter.type')}</span>
              <select className="select" value={assetType.value} onChange={(e) => patch({ asset_type: e.target.value })}>
                <option value="">{t('rs.filter.allTypes')}</option>
                {ASSET_TYPES.map((ty) => (
                  <option key={ty} value={ty}>
                    {assetTypeLabel(t, ty)}
                  </option>
                ))}
              </select>
            </label>
            <div className="field rs-filter-search">
              <span>{t('rs.filter.search')}</span>
              <SearchInput value={q} onChange={setQ} placeholder={t('rs.searchPlaceholder')} />
            </div>
            <div className="field rs-filter-reset">
              <span aria-hidden>&nbsp;</span>
              <button type="button" className="btn" onClick={reset} disabled={!filtered && paging.offset === 0}>
                <RotateCcw /> {t('rs.filter.reset')}
              </button>
            </div>
          </div>
          <details className="rs-more">
            <summary className="small faint">{t('rs.filter.more')}</summary>
            <div className="rs-filter-grid rs-filter-grid-more">
              <label className="field">
                <span>{t('rs.filter.availability')}</span>
                <select className="select" value={availability.value} onChange={(e) => patch({ availability: e.target.value })}>
                  <option value="">{t('common.all')}</option>
                  {AVAILABILITIES.map((v) => (
                    <option key={v} value={v}>
                      {dynLabel(t, `as.avail.${v}`, v)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t('rs.filter.publish')}</span>
                <select className="select" value={publishState.value} onChange={(e) => patch({ publish_state: e.target.value })}>
                  <option value="">{t('common.all')}</option>
                  {PUBLISH_STATES.map((v) => (
                    <option key={v} value={v}>
                      {dynLabel(t, `as.publish.${v}`, v)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{t('rs.filter.taskStatus')}</span>
                <select className="select" value={taskStatus.value} onChange={(e) => patch({ task_status: e.target.value })}>
                  <option value="">{t('common.all')}</option>
                  {RESOURCE_TASK_STATUSES.map((v) => (
                    <option key={v} value={v}>
                      {dynLabel(t, `as.taskStatus.${v}`, v)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </details>
          {invalid.length > 0 && (
            <div className="callout warn slim" role="status">
              <TriangleAlert aria-hidden />
              <span>{t('as.center.invalidFilters', { keys: invalid.join(', ') })}</span>
            </div>
          )}
          {page && <KindFacets total={total} byKind={byKind} active={kind.value} onPick={(k) => patch({ kind: k })} />}
        </div>
      </div>

      <div className={`rs-layout ${selected || (page && items.length > 0) ? 'has-side' : ''}`}>
        <div className="stack rs-main">
          {body}
          {page && items.length > 0 && <Pager total={total} limit={paging.limit} offset={paging.offset} onOffset={(o) => patch({ offset: o || null })} />}
          {page && items.length > 0 && (
            <p className="faint small">{t('rs.rowsAreRelations', { n: fmt.number(total) })}</p>
          )}
        </div>
        {selected ? (
          <ResourceDetail key={selected.id} item={selected} projectId={project.id} returnTo={returnTo} memberName={memberName} />
        ) : (
          page && items.length > 0 && (
            <aside className="panel rs-detail rs-detail-empty hide-sm" aria-label={t('rs.detail.label')}>
              <div className="panel-b faint small">{t('rs.detail.pickRow')}</div>
            </aside>
          )
        )}
      </div>
    </div>
  );
}
