import { Archive, ArchiveRestore, Copy, ExternalLink, FolderOpen, GitBranch, MoreHorizontal, Plus } from 'lucide-react';
import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { archivePart, listMembers, listParts } from '../../api/endpoints';
import { PART_STATUSES, type Part, type PartStatus } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { ArchivedChip, PartStatusChip, TagList } from '../../components/ui/Badges';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { SearchInput } from '../../components/ui/Form';
import { Menu, type MenuItem } from '../../components/ui/Menu';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { CopyPartDialog, PartDialog } from './PartDialogs';
import type { ProjectTabProps } from './tabProps';

const GROUP_KEY = 'partroute.parts.groupByFolder';

function readGroup(): boolean {
  try {
    return localStorage.getItem(GROUP_KEY) === '1';
  } catch {
    return false;
  }
}

/** Optional hooks for the /parts page, which owns search, the run-state filter and "New part" there. */
export interface PartsTabExtras {
  /** Search text from the page (hides this tab's own search box). */
  query?: string;
  /** Extra client-side filter (the page's workflow-state chips). */
  rowFilter?: (p: Part) => boolean;
  /** The page has its own "New part" button. */
  hideCreate?: boolean;
  /** Bump to reload (after the page created a part). */
  reloadKey?: number;
  /** An extra column after the status (the latest run's workflow). */
  extraColumn?: { header: string; cell: (p: Part) => ReactNode };
}

export function PartsTab({ project, caps, reloadProject, query, rowFilter, hideCreate, reloadKey, extraColumn }: ProjectTabProps & PartsTabExtras) {
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const me = useUser();
  const [ownQ, setQ] = useState('');
  const external = query !== undefined;
  const q = external ? query : ownQ;
  const [status, setStatus] = useState<PartStatus | ''>('');
  const [tag, setTag] = useState('');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [group, setGroupState] = useState(readGroup);
  const [creating, setCreating] = useState(false);
  const [copying, setCopying] = useState<Part | null>(null);
  const [confirm, setConfirm] = useState<{ part: Part; restore: boolean } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<unknown>(null);
  const dq = useDebounced(q.trim(), 250);
  // Tags and folders seen so far (filters narrow the response, so accumulate).
  const knownTags = useRef(new Set<string>());
  const knownFolders = useRef(new Set<string>());

  const res = useResource(
    (signal) => listParts(project.id, { q: dq || undefined, status: status || undefined, tag: tag || undefined, include_archived: includeArchived }, { signal }),
    [project.id, dq, status, tag, includeArchived, reloadKey],
  );
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const allParts = useMemo(() => res.data ?? [], [res.data]);
  const parts = useMemo(() => (rowFilter ? allParts.filter(rowFilter) : allParts), [allParts, rowFilter]);
  for (const p of allParts) {
    p.tags.forEach((x) => knownTags.current.add(x));
    if (p.folder) knownFolders.current.add(p.folder);
  }
  const memberName = useMemo(() => new Map((members.data ?? []).map((m) => [m.user.id, m.user.name])), [members.data]);

  const setGroup = (v: boolean) => {
    setGroupState(v);
    try {
      localStorage.setItem(GROUP_KEY, v ? '1' : '0');
    } catch {
      /* ignore */
    }
  };

  const groups = useMemo(() => {
    if (!group) return [{ folder: null as string | null, items: parts }];
    const m = new Map<string, Part[]>();
    for (const p of parts) m.set(p.folder, [...(m.get(p.folder) ?? []), p]);
    return [...m.entries()]
      .sort(([a], [b]) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
      .map(([folder, items]) => ({ folder, items }));
  }, [parts, group]);

  const open = (p: Part) => navigate(paths.part(p.id));
  const onRowKey = (e: KeyboardEvent, p: Part) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(p);
    }
  };

  const actionsFor = (p: Part): MenuItem[] => {
    const items: MenuItem[] = [{ id: 'open', label: t('common.open'), icon: <ExternalLink />, onSelect: () => open(p) }];
    // Copying needs "view" here and "design" in the target; the dialog lists valid targets.
    items.push({ id: 'copy', label: t('parts.copy'), icon: <Copy />, onSelect: () => setCopying(p) });
    if (caps.design) {
      items.push(
        p.archived_at
          ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, separator: true, onSelect: () => setConfirm({ part: p, restore: true }) }
          : { id: 'archive', label: t('common.archive'), icon: <Archive />, separator: true, danger: true, onSelect: () => setConfirm({ part: p, restore: false }) },
      );
    }
    return items;
  };

  const doArchive = async () => {
    if (!confirm) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const updated = await archivePart(confirm.part.id, confirm.restore);
      toast(t(confirm.restore ? 'parts.restoredToast' : 'parts.archivedToast', { number: updated.number }));
      setConfirm(null);
      res.reload();
      reloadProject();
    } catch (e) {
      setConfirmError(e);
    } finally {
      setConfirmBusy(false);
    }
  };

  const ownFilters = !!((!external && dq) || status || tag || includeArchived);
  const filtered = ownFilters || !!dq;
  const cols = (group ? 8 : 9) + (extraColumn ? 1 : 0);

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={7} />;
  else if (!allParts.length && !filtered)
    body = (
      <EmptyState
        title={t('parts.empty.title')}
        body={caps.design ? t('parts.empty.body') : t('parts.empty.readOnly')}
        action={
          caps.design ? (
            <button type="button" className="btn primary" onClick={() => setCreating(true)}>
              <Plus /> {t('parts.new')}
            </button>
          ) : undefined
        }
      />
    );
  else if (!parts.length)
    body = (
      <EmptyState
        compact
        title={t('common.noMatches')}
        action={
          // Filters owned by the page (search, workflow state) are cleared there.
          !ownFilters ? undefined : <button
            type="button"
            className="btn"
            onClick={() => {
              setQ('');
              setStatus('');
              setTag('');
              setIncludeArchived(false);
            }}
          >
            {t('common.clearFilters')}
          </button>
        }
      />
    );
  else
    body = (
      <div className={`table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('parts.col.number')}</th>
              <th>{t('parts.col.name')}</th>
              <th>{t('parts.col.status')}</th>
              {extraColumn && <th>{extraColumn.header}</th>}
              <th className="hide-sm">{t('parts.col.material')}</th>
              <th className="hide-md">{t('parts.col.tags')}</th>
              {!group && <th className="hide-md">{t('parts.col.folder')}</th>}
              <th className="hide-sm">{t('parts.col.model')}</th>
              <th className="hide-md">{t('parts.col.updated')}</th>
              <th className="act">
                <span className="sr-only">{t('common.actions')}</span>
              </th>
            </tr>
          </thead>
          {groups.map((g) => (
            <tbody key={g.folder ?? '__all__'}>
              {g.folder !== null && (
                <tr className="group-row">
                  <th colSpan={cols} scope="colgroup">
                    <FolderOpen aria-hidden />
                    <span className="mono">{g.folder || t('parts.noFolder')}</span>
                    <span className="faint">· {tn('parts.count', g.items.length, { count: fmt.number(g.items.length) })}</span>
                  </th>
                </tr>
              )}
              {g.items.map((p) => (
                <tr key={p.id} className={`clickable ${p.archived_at ? 'is-archived' : ''}`} tabIndex={0} onClick={() => open(p)} onKeyDown={(e) => onRowKey(e, p)} aria-label={t('parts.openPart', { number: p.number })}>
                  <td className="mono strong nowrap">
                    {p.number}
                    {p.source_part_id && <GitBranch className="inline-ico faint" aria-label={t('part.copiedFrom')} />}
                  </td>
                  <td>
                    <div className="cell-main">
                      <span className="ellip">{p.name}</span>
                      {p.archived_at && <ArchivedChip />}
                    </div>
                    <div className="faint small only-sm">{p.material}</div>
                  </td>
                  <td>
                    <PartStatusChip status={p.status} />
                  </td>
                  {extraColumn && <td>{extraColumn.cell(p)}</td>}
                  <td className="hide-sm mono small">{p.material}</td>
                  <td className="hide-md">
                    <TagList tags={p.tags} max={3} />
                  </td>
                  {!group && <td className="hide-md mono small faint">{p.folder || '—'}</td>}
                  <td className="hide-sm mono small">
                    {p.latest_revision ? (
                      <span className="rev-ref" title={`${t(`revSource.${p.latest_revision.source}`)} · ${fmt.dateTime(p.latest_revision.created_at)}`}>
                        R{p.latest_revision.rev_no}
                        <span className="faint"> · {p.latest_revision.mesh_only ? t('rev.meshOnly') : t(`revSource.${p.latest_revision.source}`)}</span>
                      </span>
                    ) : (
                      <span className="faint">{t('rev.none')}</span>
                    )}
                  </td>
                  <td className="hide-md faint small nowrap" title={`${fmt.dateTime(p.updated_at)}${p.owner_id ? ` · ${memberName.get(p.owner_id) ?? ''}` : ''}`}>
                    {fmt.relative(p.updated_at)}
                  </td>
                  <td className="act">
                    <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actionsFor(p)} />
                  </td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    );

  return (
    <div className="stack">
      <div className="toolbar">
        {!external && <SearchInput value={ownQ} onChange={setQ} placeholder={t('parts.searchPlaceholder')} />}
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value as PartStatus | '')} aria-label={t('parts.filter.status')}>
          <option value="">{t('parts.filter.allStatuses')}</option>
          {PART_STATUSES.filter((s) => s !== 'archived').map((s) => (
            <option key={s} value={s}>
              {t(`partStatus.${s}`)}
            </option>
          ))}
        </select>
        <select className="select" value={tag} onChange={(e) => setTag(e.target.value)} aria-label={t('parts.filter.tag')}>
          <option value="">{t('parts.filter.allTags')}</option>
          {[...knownTags.current].sort().map((x) => (
            <option key={x} value={x}>
              {x}
            </option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} />
          <span>{t('parts.groupByFolder')}</span>
        </label>
        <label className="check">
          <input type="checkbox" checked={includeArchived} onChange={(e) => setIncludeArchived(e.target.checked)} />
          <span>{t('common.showArchived')}</span>
        </label>
        <span className="grow" />
        {res.data && <span className="faint mono small">{tn('parts.count', parts.length, { count: fmt.number(parts.length) })}</span>}
        {caps.design && !hideCreate && (
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            <Plus /> {t('parts.new')}
          </button>
        )}
      </div>

      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}

      {creating && (
        <PartDialog
          mode="create"
          project={project}
          members={members.data}
          folders={[...knownFolders.current].sort()}
          meId={me.id}
          onClose={() => setCreating(false)}
          onSaved={(p) => {
            setCreating(false);
            toast(t('parts.createdToast', { number: p.number }), { action: { label: t('common.open'), onClick: () => open(p) } });
            res.reload();
            reloadProject();
          }}
        />
      )}
      {copying && (
        <CopyPartDialog
          part={copying}
          project={project}
          onClose={() => setCopying(null)}
          onCopied={(copy, target) => {
            setCopying(null);
            toast(t('parts.copiedToast', { number: copy.number, code: target.code }), { action: { label: t('common.open'), onClick: () => open(copy) } });
            if (target.id === project.id) {
              res.reload();
              reloadProject();
            }
          }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={t(confirm.restore ? 'parts.restore.title' : 'parts.archive.title', { number: confirm.part.number })}
          body={confirm.restore ? `${confirm.part.number} · ${confirm.part.name}` : t('parts.archive.body')}
          confirmLabel={confirm.restore ? t('common.restore') : t('common.archive')}
          danger={!confirm.restore}
          busy={confirmBusy}
          error={<ErrorNotice error={confirmError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setConfirm(null);
            setConfirmError(null);
          }}
        />
      )}
    </div>
  );
}

