// Left column of the knowledge tab: searchable, pageable list of collections
// with their current manifest version, reuse eligibility and index status.
// The all-projects experience view passes `projectOf` (each row names its
// source project) and no paging (it merges per-project lists).

import { BookOpen } from 'lucide-react';
import type { KeyboardEvent } from 'react';
import { scopeText, type KnowledgeCollection } from '../../api/knowledge';
import type { Project } from '../../api/types';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, SkeletonRows } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { CollectionStatusChip, IndexStatusChip, ReusableChip } from '../assets/collabMeta';
import { Pager } from '../assets/Pager';

/** A collection offers default-reusable items when it is active and its current manifest has formal results. */
export function collectionReusable(c: KnowledgeCollection): boolean {
  return c.status === 'active' && (c.current_version?.counts.formal ?? 0) > 0;
}

export function CollectionList({
  items,
  total,
  initial,
  loading,
  selectedId,
  onSelect,
  q,
  onQ,
  reusableOnly,
  onReusable,
  limit,
  offset,
  onOffset,
  onClear,
  projectOf,
}: {
  items: KnowledgeCollection[];
  total: number;
  initial: boolean;
  loading: boolean;
  selectedId: string;
  onSelect: (id: string) => void;
  q: string;
  onQ: (v: string) => void;
  reusableOnly: boolean;
  onReusable: (v: boolean) => void;
  /** Server paging; omitted for a list that is not paged. */
  limit?: number;
  offset?: number;
  onOffset?: (o: number) => void;
  onClear: () => void;
  /** Source project of a row, shown under its title (lists spanning several projects). */
  projectOf?: (c: KnowledgeCollection) => Pick<Project, 'name' | 'code'> | undefined;
}) {
  const { t, tn, fmt } = useI18n();
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const next = items[Math.min(items.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    document.getElementById(`kb-c-${next.id}`)?.focus();
  };
  return (
    <nav className="panel kb-list" aria-label={t('kb.list.label')}>
      <div className="panel-b kb-list-tools stack-sm">
        <SearchInput value={q} onChange={onQ} placeholder={t('kb.list.search')} />
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={reusableOnly} onChange={(e) => onReusable(e.target.checked)} />
            {t('kb.list.reusableOnly')}
          </label>
          <span className="grow" />
          {!initial && <span className="faint small">{tn('kb.list.count', total, { count: fmt.number(total) })}</span>}
        </div>
      </div>
      {initial ? (
        <div className="panel-b">
          <SkeletonRows rows={4} cols={2} />
        </div>
      ) : !items.length ? (
        <EmptyState
          compact
          title={t('common.noMatches')}
          action={
            <button type="button" className="btn sm" onClick={onClear}>
              {t('common.clearFilters')}
            </button>
          }
        />
      ) : (
        <ul className={`kb-rows ${loading ? 'is-refreshing' : ''}`}>
          {items.map((c, i) => {
            const scope = scopeText(c.scope);
            const owner = projectOf?.(c);
            return (
              <li key={c.id}>
                <button
                  id={`kb-c-${c.id}`}
                  type="button"
                  className={`kb-row ${c.id === selectedId ? 'active' : ''}`}
                  aria-current={c.id === selectedId ? 'true' : undefined}
                  onClick={() => onSelect(c.id)}
                  onKeyDown={(e) => onKey(e, i)}
                >
                  <span className="kb-row-ico" aria-hidden>
                    <BookOpen />
                  </span>
                  <span className="kb-row-body">
                    <span className="kb-row-title ellip" title={c.title}>
                      {c.title}
                    </span>
                    {owner && (
                      <span className="small muted ellip" title={`${owner.name} · ${owner.code}`}>
                        {owner.name} · <span className="mono">{owner.code}</span>
                      </span>
                    )}
                    <span className="faint small ellip" title={scope || c.run_title || undefined}>
                      {scope || c.run_title || t('kb.noScope')}
                    </span>
                    <span className="kb-row-meta">
                      {c.current_version && <span className="mono strong">V{c.current_version.version_no}</span>}
                      {c.status === 'deprecated' ? <CollectionStatusChip status={c.status} /> : <ReusableChip reusable={collectionReusable(c)} />}
                      {c.index_status !== 'ready' && <IndexStatusChip status={c.index_status} />}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {onOffset && limit !== undefined && total > limit && (
        <div className="panel-b kb-list-pager">
          <Pager total={total} limit={limit} offset={offset ?? 0} onOffset={onOffset} summaryKey="as.pager.summaryCollections" />
        </div>
      )}
    </nav>
  );
}
