// Knowledge tab of the asset center (UI05, design §5.3 / C09): archived
// collections on the left (search, reusable filter, index status), the chosen
// collection on the right with its immutable manifest version, four sections
// (formal results / all resources / acceptance records / usage history) and
// the "adopt into a new task" panel. URL keys: collection_id,
// collection_version_id, q, reusable, limit, offset. A late response for a
// collection that is no longer selected is never shown.

import './knowledge.css';
import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { getKnowledgeCollection, listKnowledge } from '../../api/knowledge';
import { withProjectParam } from '../../app/nav';
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { AssetTabContentProps } from '../assets/tabProps';
import { readPaging } from '../assets/urlState';
import { useAssetQuery, useUrlSearchText } from '../assets/useAssetQuery';
import { CollectionDetail } from './CollectionDetail';
import { CollectionList } from './CollectionList';

const LIST_LIMIT = 50;

export default function KnowledgeTab({ project, caps, onCount }: AssetTabContentProps) {
  const { t } = useI18n();
  const location = useLocation();
  const query = useAssetQuery();
  const { search, patch } = query;
  const [q, setQ] = useUrlSearchText(query, 'q');
  const dq = search.get('q') ?? '';
  const reusableOnly = search.get('reusable') === '1';
  const collectionId = search.get('collection_id') ?? '';
  const versionId = search.get('collection_version_id') ?? '';
  const paging = readPaging(search, LIST_LIMIT, 100);
  const filtered = !!(dq || reusableOnly);

  const list = useResource(
    (signal) => listKnowledge(project.id, { q: dq, reusable: reusableOnly, limit: paging.limit, offset: paging.offset }, { signal }),
    [project.id, dq, reusableOnly, paging.limit, paging.offset],
  );
  useEffect(() => {
    if (list.data && !filtered) onCount?.(list.data.total);
  }, [list.data, filtered, onCount]);

  // No collection chosen yet: show the first one of the list (URL replaced, paging kept).
  useEffect(() => {
    if (collectionId || !list.data?.items.length) return;
    patch({ collection_id: list.data.items[0].id, offset: paging.offset || null });
  }, [collectionId, list.data, patch, paging.offset]);

  const detail = useResource((signal) => getKnowledgeCollection(collectionId, { version_id: versionId }, { signal }), [collectionId, versionId], { enabled: !!collectionId });
  // Only the response for the current selection is shown; a previous collection's data never fills the gap.
  const current = detail.data && detail.data.id === collectionId && (!versionId || detail.data.version?.id === versionId) ? detail.data : undefined;
  const returnTo = withProjectParam(`${location.pathname}${location.search}`, project.id);

  const selectCollection = (id: string) => {
    // Re-clicking the open collection still clears an explicit (possibly unreadable) manifest version.
    if (id === collectionId && !versionId) return;
    patch({ collection_id: id, collection_version_id: null, offset: paging.offset || null });
  };

  let main;
  if (!collectionId) {
    main =
      list.data && !list.data.items.length ? null : (
        <div className="panel kb-main-empty">
          <div className="panel-b faint small">{t('kb.pickCollection')}</div>
        </div>
      );
  } else if (detail.error && !current) {
    main = <ErrorState error={detail.error} onRetry={detail.reload} />;
  } else if (!current) {
    main = (
      <div className="panel">
        <div className="panel-b stack-sm" role="status" aria-label={t('common.loading')}>
          <Skeleton w="40%" h={22} />
          <Skeleton w="60%" />
          <SkeletonRows rows={3} cols={3} />
        </div>
      </div>
    );
  } else {
    main = (
      <CollectionDetail
        key={`${current.id}:${current.version?.id ?? ''}`}
        project={project}
        caps={caps}
        collection={current}
        refreshing={detail.loading}
        returnTo={returnTo}
        onVersion={(id) => patch({ collection_version_id: id, offset: paging.offset || null })}
        onChanged={() => {
          detail.reload();
          list.reload();
        }}
      />
    );
  }

  if (list.error && !list.data) return <ErrorState error={list.error} onRetry={list.reload} />;
  if (list.data && !list.data.items.length && !filtered && !collectionId) return <EmptyState title={t('kb.empty.title')} body={t('kb.empty.body')} />;

  return (
    <div className="kb-layout">
      <CollectionList
        items={list.data?.items ?? []}
        total={list.data?.total ?? 0}
        initial={list.initial}
        loading={list.loading}
        selectedId={collectionId}
        onSelect={selectCollection}
        q={q}
        onQ={setQ}
        reusableOnly={reusableOnly}
        onReusable={(v) => patch({ reusable: v })}
        limit={paging.limit}
        offset={paging.offset}
        onOffset={(o) => patch({ offset: o || null })}
        onClear={() => {
          setQ('');
          patch({ q: null, reusable: null });
        }}
      />
      <div className="kb-main">{main}</div>
    </div>
  );
}
