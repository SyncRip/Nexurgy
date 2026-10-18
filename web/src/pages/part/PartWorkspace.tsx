import { Archive,ArchiveRestore,Copy,LayoutGrid,MoreHorizontal,Pencil } from 'lucide-react';
import { Suspense,useMemo,useState } from 'react';
import { Navigate,NavLink,useLocation,useNavigate,useParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { archivePart,getPart,getProject,listMembers,updatePart } from '../../api/endpoints';
import { PART_STATUSES,type PartStatus } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { ArchivedChip,PartStatusChip } from '../../components/ui/Badges';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,ErrorText,Skeleton,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { CopyPartDialog,PartDialog } from '../project/PartDialogs';
import { defaultPartTab,isPartTab,PART_TABS } from './registry';

export default function PartWorkspace() {
  const { partId } = useParams();
  return <PartWorkspaceContent key={partId} />;
}

function PartWorkspaceContent() {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const me = useUser();
  const { partId = '', tab } = useParams();
  const location = useLocation();
  const partRes = useResource((signal) => getPart(partId, { signal }), [partId]);
  const part = partRes.data?.id === partId ? partRes.data : undefined;
  const projectId = part?.project_id;
  const projectRes = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const membersRes = useResource((signal) => listMembers(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const project = projectRes.data?.id === projectId ? projectRes.data : undefined;
  const [statusBusy, setStatusBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [copying, setCopying] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<unknown>(null);
  // The workflow tab renders its controls into the bar (one row, as in the R1.2 part canvas).
  const [barSlot, setBarSlot] = useState<HTMLDivElement | null>(null);

  const tabDef = PART_TABS.find((x) => x.id === tab);
  const flowMode = tab === 'workflow';
  const owner = useMemo(() => membersRes.data?.find((m) => m.user.id === part?.owner_id)?.user, [membersRes.data, part?.owner_id]);

  useShellProject(project);
  useCrumbs([
    { label: t('nav.parts'), to: paths.parts(project?.id) },
    ...(part ? [{ label: part.number, mono: true }] : []),
  ]);

  // Keep the query: /parts/:id?rev=<revision> (assistant result links) opens that revision on the model tab.
  if (tab === 'history' || tab === 'runs') {
    const query = new URLSearchParams(location.search);
    if (tab === 'history') query.set('section', 'history');
    return <Navigate to={`${paths.part(partId, tab === 'history' ? 'files' : 'workflow')}?${query}`} replace />;
  }
  if (!isPartTab(tab)) return <Navigate to={`${paths.part(partId, defaultPartTab(location.search))}${location.search}`} replace />;

  const notFound = (isApiError(partRes.error) && partRes.error.status === 404) || (isApiError(projectRes.error) && projectRes.error.status === 404);
  if (notFound) {
    return (
      <div className="page">
        <EmptyState title={t('part.notFound.title')} body={t('part.notFound.body')} />
      </div>
    );
  }
  if ((partRes.error && !part) || (projectRes.error && !project)) {
    return (
      <div className="page">
        <ErrorState
          error={partRes.error ?? projectRes.error}
          onRetry={() => {
            partRes.reload();
            projectRes.reload();
          }}
        />
      </div>
    );
  }

  const caps = projectCapabilities(project);
  const canDesign = caps.design && !part?.archived_at;
  const Panel = tabDef!.component;

  const changeStatus = async (next: PartStatus) => {
    if (!part || next === part.status) return;
    setStatusBusy(true);
    try {
      const updated = await updatePart(part.id, { status: next });
      partRes.setData(updated);
      toast(t('part.statusChangedToast', { status: t(`partStatus.${updated.status}`) }));
    } catch (e) {
      toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setStatusBusy(false);
    }
  };

  const doArchive = async () => {
    if (!part) return;
    const restore = !!part.archived_at;
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      const updated = await archivePart(part.id, restore);
      partRes.setData(updated);
      setArchiveOpen(false);
      toast(t(restore ? 'parts.restoredToast' : 'parts.archivedToast', { number: updated.number }));
    } catch (e) {
      setArchiveError(e);
    } finally {
      setArchiveBusy(false);
    }
  };

  const menuItems: MenuItem[] = part
    ? [
        { id: 'copy', label: t('parts.copy'), icon: <Copy />, onSelect: () => setCopying(true) },
        ...(caps.design
          ? [
              part.archived_at
                ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, separator: true, onSelect: () => setArchiveOpen(true) }
                : { id: 'archive', label: t('common.archive'), icon: <Archive />, separator: true, danger: true, onSelect: () => setArchiveOpen(true) },
            ]
          : []),
      ]
    : [];

  const ownerName = part?.owner_id ? (owner?.name ?? (membersRes.loading ? '…' : t('part.unknownOwner'))) : null;

  return (
    <div className={`page workspace ${flowMode ? 'flow-mode' : ''}`}>
      {!part || !project ? (
        <div className="part-bar skeleton-tb" aria-hidden>
          <Skeleton w={90} h={22} />
          <Skeleton w={220} h={18} />
        </div>
      ) : (
        <header className={`part-bar ${flowMode ? 'flow' : ''}`}>
          <div className="part-bar-id">
            <span className="part-number mono">{part.number}</span>
            <h1 title={part.description || undefined}>{part.name}</h1>
            <span className="chip mono" title={t('part.field.material')}>
              {part.material || '—'}
            </span>
            {flowMode ? null : canDesign ? (
              <select
                className={`select sm status-select st-${part.status}`}
                value={part.status}
                onChange={(e) => void changeStatus(e.target.value as PartStatus)}
                disabled={statusBusy}
                aria-label={t('part.statusSelect')}
              >
                {PART_STATUSES.filter((s) => s !== 'archived' || part.status === 'archived').map((s) => (
                  <option key={s} value={s}>
                    {t(`partStatus.${s}`)}
                  </option>
                ))}
              </select>
            ) : (
              <PartStatusChip status={part.status} />
            )}
            {part.archived_at && <ArchivedChip />}
            {!flowMode && (
            <span className="part-bar-meta faint small hide-md">
              {[
                part.latest_revision ? `R${part.latest_revision.rev_no}` : t('rev.none'),
                ownerName,
                part.folder || null,
                part.tags.length ? part.tags.slice(0, 3).join(', ') : null,
                part.source_part_id ? t('part.copiedFrom') : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
            )}
          </div>
          {flowMode ? (
            <div className="part-bar-slot" ref={setBarSlot} />
          ) : (
            <nav className="part-bar-tabs" aria-label={t('part.tabsLabel')}>
              {PART_TABS.map((x) => (
                <NavLink key={x.id} to={paths.part(partId, x.id)} className={({ isActive }) => `part-tab ${isActive ? 'active' : ''}`} title={t(x.labelKey)}>
                  {x.icon}
                  <span>{t(x.labelKey)}</span>
                </NavLink>
              ))}
            </nav>
          )}
          <div className="part-bar-actions">
            {flowMode && (
              <Menu
                label={t('part.tabsLabel')}
                trigger={
                  <>
                    <LayoutGrid /> <span className="hide-sm">{t('cv.results')}</span>
                  </>
                }
                triggerClassName="btn sm"
                items={PART_TABS.filter((x) => x.id !== 'workflow').map((x) => ({ id: x.id, label: t(x.labelKey), icon: x.icon, onSelect: () => navigate(paths.part(partId, x.id)) }))}
              />
            )}
            {canDesign && (
              <button type="button" className="btn sm icon" onClick={() => setEditing(true)} aria-label={t('part.edit')} title={t('part.edit')}>
                <Pencil />
              </button>
            )}
            <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={menuItems} triggerClassName="btn sm icon" />
          </div>
          {(part.archived_at || project.status === 'archived') && (
            <div className="callout warn slim part-bar-banner">
              <Archive aria-hidden />
              <span>{part.archived_at ? t('part.archivedBanner') : t('project.archivedBanner')}</span>
            </div>
          )}
        </header>
      )}

      <div className={flowMode ? 'part-flow-body' : 'section-body'}>
        {part && project ? (
          <Suspense fallback={<SkeletonRows />}>
            <Panel key={part.id} part={part} project={project} caps={caps} onPartChange={partRes.setData} reloadPart={partRes.reload} barSlot={flowMode ? barSlot : null} />
          </Suspense>
        ) : (
          <SkeletonRows />
        )}
      </div>

      {editing && part && project && (
        <PartDialog
          mode="edit"
          project={project}
          part={part}
          members={membersRes.data}
          folders={part.folder ? [part.folder] : []}
          meId={me.id}
          onClose={() => setEditing(false)}
          onSaved={(p) => {
            setEditing(false);
            partRes.setData(p);
            toast(t('part.savedToast'));
          }}
        />
      )}
      {copying && part && project && (
        <CopyPartDialog
          part={part}
          project={project}
          onClose={() => setCopying(false)}
          onCopied={(copy, target) => {
            setCopying(false);
            toast(t('parts.copiedToast', { number: copy.number, code: target.code }), { action: { label: t('common.open'), onClick: () => navigate(paths.part(copy.id)) } });
          }}
        />
      )}
      {archiveOpen && part && (
        <ConfirmDialog
          title={t(part.archived_at ? 'parts.restore.title' : 'parts.archive.title', { number: part.number })}
          body={part.archived_at ? `${part.number} · ${part.name}` : t('parts.archive.body')}
          confirmLabel={part.archived_at ? t('common.restore') : t('common.archive')}
          danger={!part.archived_at}
          busy={archiveBusy}
          error={<ErrorNotice error={archiveError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setArchiveOpen(false);
            setArchiveError(null);
          }}
        />
      )}
    </div>
  );
}

