import { FocusNotice } from '../../features/todos/FocusNotice';
// /assemblies/:assemblyId/:tab? — the static assembly workspace (W4): structure (tree ↔ 3D view, properties,
// edits), mates, BOM, checks, drawings (W5) and history of one exact revision (?rev=<id>, default the latest). Every change
// creates a new revision on the latest one; old baselines stay readable and downloadable.

import { Download } from 'lucide-react';
import { useCallback,useEffect,useRef,useState } from 'react';
import { Navigate,NavLink,useNavigate,useParams,useSearchParams } from 'react-router-dom';
import { downloadExport,getAssembly,getAssemblyRevision,getScene,listAssemblyRevisions,type AssemblyRevision } from '../../api/assemblies';
import { saveBlob } from '../../api/blobs';
import { isApiError } from '../../api/client';
import { getProject } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { CheckStatusChip,PolicyChip,StaleChip,UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { useCrumbs,useShellProject } from '../../components/shell/ShellContext';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { EmptyState,ErrorState,Skeleton,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { DrawingsTab } from '../../features/assembly-drawings/DrawingsTab';
import { assemblyPath } from '../../features/assembly/AssembliesPanel';
import '../../features/assembly/assembly.css';
import { BomTab } from '../../features/assembly/BomTab';
import { ChecksTab } from '../../features/assembly/ChecksTab';
import { RevisionCoverage } from '../../features/assembly/CoveragePanel';
import { useAssemblyEdit } from '../../features/assembly/edit';
import { HistoryTab } from '../../features/assembly/HistoryTab';
import { MatesTab } from '../../features/assembly/MatesTab';
import { safeCheckStatus } from '../../features/assembly/model';
import { StructureTab } from '../../features/assembly/StructureTab';
import { useAssistant,useAssistantPageContext } from '../../features/assistant/AssistantProvider';
import { CoverageMarks } from '../../features/orchestration/Coverage';
import { aiDecisionCount,coverageOf } from '../../features/orchestration/coverageModel';
import { unverifiedBesideCheck } from '../../features/turbo/model';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

const TABS = ['structure', 'mates', 'bom', 'checks', 'drawings', 'history'] as const;
type Tab = (typeof TABS)[number];
const isTab = (x: string | undefined): x is Tab => !!x && (TABS as readonly string[]).includes(x);

export default function AssemblyWorkspacePage() {
  const { assemblyId } = useParams();
  return <AssemblyWorkspace key={assemblyId} />;
}

function AssemblyWorkspace() {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { assemblyId = '', tab } = useParams();
  const [search, setSearch] = useSearchParams();
  const asmRes = useResource((signal) => getAssembly(assemblyId, { signal }), [assemblyId]);
  const asm = asmRes.data?.id === assemblyId ? asmRes.data : undefined;
  const projectRes = useResource((signal) => getProject(asm!.project_id, { signal }), [asm?.project_id], { enabled: !!asm });
  const project = projectRes.data?.id === asm?.project_id ? projectRes.data : undefined;
  const revsRes = useResource((signal) => listAssemblyRevisions(assemblyId, { signal }), [assemblyId]);
  const latestId = asm?.latest_revision?.id;
  const revId = search.get('rev') || latestId || '';
  const revRes = useResource((signal) => getAssemblyRevision(revId, { signal }), [revId], { enabled: !!revId });
  const sceneRes = useResource((signal) => getScene(revId, { signal }), [revId], { enabled: !!revId && (!tab || tab === 'structure') });
  const revisionMismatch = revRes.data?.id === revId && revRes.data.assembly_id !== assemblyId;
  const rev = revRes.data?.id === revId && !revisionMismatch ? revRes.data : undefined;
  // New edits resolve the project's current policy on the server.
  const [downloading, setDownloading] = useState<string | null>(null);
  const [newer, setNewer] = useState(false);

  useShellProject(project);
  useCrumbs([{ label: t('nav.parts'), to: paths.parts(project?.id) }, ...(asm ? [{ label: asm.number, mono: true }] : [])]);

  const reloadAll = useCallback(() => {
    asmRes.reload();
    revsRes.reload();
    revRes.reload();
    sceneRes.reload();
  }, [asmRes, revsRes, revRes, sceneRes]);

  const onSaved = useCallback(
    (saved: AssemblyRevision) => {
      setNewer(false);
      const next = new URLSearchParams(search);
      next.delete('rev');
      setSearch(next, { replace: true });
      revRes.setData(saved);
      asmRes.reload();
      revsRes.reload();
      sceneRes.reload();
    },
    [search, setSearch, revRes, asmRes, revsRes, sceneRes],
  );
  const edit = useAssemblyEdit(rev, onSaved, reloadAll);

  const [instance, setInstance] = useState<{ revId: string; path: string[] | null } | null>(null);
  const selectInstance = useCallback((path: string[] | null) => setInstance({ revId, path }), [revId]);
  const instancePath = instance?.revId === revId && tab === 'structure' ? instance.path : null;
  useAssistantPageContext(asm && rev ? { projectId: asm.project_id, label: `${asm.number} · ${rev.label}${instancePath ? ` · ${instancePath.join('/')}` : ''}`,
    object: { type: 'assembly', id: asm.id, version: rev.id },
    target: { kind: 'assembly_revision', object_id: rev.id, project_id: asm.project_id, ...(instancePath ? { instance: { path: instancePath } } : {}) } } : null);

  const { onOperationEvent } = useAssistant();
  const refresh = useRef(() => {});
  refresh.current = () => {
    if (edit.dirty) { setNewer(true); return; }
    reloadAll();
  };
  useEffect(() => onOperationEvent((event) => {
    const result = event.operation.result?.cad_result;
    if (result?.object_type === 'assembly' && result.object_id === assemblyId && event.projectId === asm?.project_id) refresh.current();
  }), [onOperationEvent, assemblyId, asm?.project_id]);
  useEffect(() => {
    if (search.has('assumption') && tab !== 'checks') navigate(`${assemblyPath(assemblyId, 'checks')}?${search}`, { replace: true });
  }, [search, tab, assemblyId, navigate]);

  if (!isTab(tab)) return <Navigate to={`${assemblyPath(assemblyId)}${search.toString() ? `?${search}` : ''}`} replace />;
  const notFound = isApiError(asmRes.error) && asmRes.error.status === 404;
  if (notFound)
    return (
      <div className="page">
        <EmptyState title={t('asm.notFound.title')} body={t('asm.notFound.body')} />
      </div>
    );
  if (asmRes.error && !asm)
    return (
      <div className="page">
        <ErrorState error={asmRes.error} onRetry={asmRes.reload} />
      </div>
    );

  const caps = projectCapabilities(project);
  const isLatest = !!rev && rev.id === latestId;
  const canEdit = caps.design && isLatest && !asm?.archived_at;
  const openRev = (rid: string) => {
    const next = new URLSearchParams(search);
    if (rid === latestId) next.delete('rev');
    else next.set('rev', rid);
    setSearch(next);
  };
  const download = async (kind: 'step' | 'parts.zip') => {
    if (!rev || !asm) return;
    setDownloading(kind);
    try {
      const { blob, filename } = await downloadExport(rev.id, kind);
      saveBlob(blob, filename ?? `${asm.number}_${rev.label}${kind === 'step' ? '.step' : '_parts.zip'}`);
    } catch (e) {
      toast(`${t('asm.download.failed')}: ${isApiError(e) ? e.message : String(e)}`, { tone: 'err' });
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="page workspace asm-page">
      <FocusNotice kind="assumption" version={rev?.id ?? revId} />
      {!asm ? (
        <div className="part-bar skeleton-tb" aria-hidden>
          <Skeleton w={90} h={22} />
          <Skeleton w={220} h={18} />
        </div>
      ) : (
        <header className="part-bar asm-bar">
          <div className="part-bar-id">
            <span className="part-number mono">{asm.number}</span>
            <h1 title={asm.description || undefined}>{asm.name}</h1>
            <label className="asm-rev-select">
              <span className="sr-only">{t('asm.rev.select')}</span>
              <select className="select sm mono" value={revId} onChange={(e) => openRev(e.target.value)} aria-label={t('asm.rev.select')}>
                {(revsRes.data ?? []).map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                    {r.id === latestId ? ` · ${t('asm.rev.latest')}` : ''}
                  </option>
                ))}
              </select>
            </label>
            {rev && (
              <>
                <PolicyChip policy={rev.execution_policy} />
                <CheckStatusChip status={safeCheckStatus(rev.check_status)} />
                {/* generation supplement B: partial assembly, AI decisions and not verified, each its own mark (a partial
                    revision never reads "done · not verified") */}
                <CoverageMarks coverage={coverageOf(rev.spec_ref)} aiDecisions={aiDecisionCount(rev.assumptions)}
                  unverified={unverifiedBesideCheck(rev.verification, rev.check_status) && coverageOf(rev.spec_ref)?.status === 'partial'} />
                {unverifiedBesideCheck(rev.verification, rev.check_status) && coverageOf(rev.spec_ref)?.status !== 'partial' && <UnverifiedChip />}
                {!isLatest && <StaleChip />}
              </>
            )}
          </div>
          <nav className="part-bar-tabs" aria-label={t('asm.tabs')}>
            {TABS.map((x) => (
              <NavLink key={x} to={`${assemblyPath(assemblyId, x)}${search.toString() ? `?${search}` : ''}`} className={({ isActive }) => `part-tab ${isActive ? 'active' : ''}`}>
                <span>{x === 'drawings' ? t('asmd.tab') : t(`asm.tab.${x}`)}</span>
              </NavLink>
            ))}
          </nav>
          <div className="part-bar-actions">
            <button type="button" className="btn sm" disabled={!rev || downloading !== null} onClick={() => void download('step')} aria-label={t('asm.download.step')} title={t('asm.download.step')}>
              <Download aria-hidden /> <span className="hide-sm">{t('asm.download.step')}</span>
            </button>
            <button type="button" className="btn sm" disabled={!rev || downloading !== null} onClick={() => void download('parts.zip')} aria-label={t('asm.download.parts')} title={t('asm.download.parts')}>
              <Download aria-hidden /> <span className="hide-sm">{t('asm.download.parts')}</span>
            </button>
          </div>
        </header>
      )}
      {rev && !isLatest && latestId && (
        // S6: the exact revision a link asked for (?rev=), never silently the latest.
        <HistoricalBanner className="asm-banner" current={asm?.latest_revision?.label ?? null} onOpenCurrent={() => openRev(latestId)}>
          <span className="faint small">{t('asm.rev.old')}</span>
        </HistoricalBanner>
      )}
      {rev && isLatest && rev.updates_available > 0 && <p className="callout slim warn asm-banner">{t('asm.rev.updates', { n: rev.updates_available })}</p>}
      {newer && <div className="callout slim info asm-banner" role="status">
        <span className="grow">{t('core.result.newer')}</span>
        <button type="button" className="btn sm" disabled={edit.dirty} onClick={() => { setNewer(false); reloadAll(); }}>{t('core.result.refresh')}</button>
      </div>}
      {rev && <RevisionCoverage rev={rev} />}
      {project && !caps.design && <p className="faint small asm-banner">{t('asm.edit.readOnly')}</p>}
      <div className="asm-body">
        {revisionMismatch ? <p className="callout warn">{t('ver.missing')}</p> : revRes.error && !rev ? (
          <ErrorState error={revRes.error} onRetry={revRes.reload} />
        ) : !rev || !asm ? (
          <SkeletonRows rows={6} cols={4} />
        ) : tab === 'structure' ? (
          <StructureTab onSelection={selectInstance} rev={rev} scene={sceneRes.data && sceneRes.data.revision_id === rev.id ? sceneRes.data : undefined} projectId={asm.project_id} canEdit={canEdit} edit={edit} />
        ) : tab === 'mates' ? (
          <MatesTab rev={rev} canEdit={canEdit} edit={edit} />
        ) : tab === 'bom' ? (
          <BomTab revisionId={rev.id} frozenSha={rev.bom_sha256} />
        ) : tab === 'checks' ? (
          <ChecksTab rev={rev} projectId={asm.project_id} canRun={caps.design} onUpdated={(r) => revRes.setData(r)} onReload={reloadAll} />
        ) : tab === 'drawings' ? (
          <DrawingsTab assemblyId={asm.id} assemblyNumber={asm.number} revision={rev} projectId={asm.project_id} latestId={latestId} canDesign={caps.design && !asm.archived_at} onOpenRevision={openRev} />
        ) : (
          <HistoryTab assemblyId={asm.id} current={rev.id} onOpen={(rid) => (openRev(rid), navigate(`${assemblyPath(assemblyId, 'structure')}?rev=${rid}`))} />
        )}
      </div>
      {edit.dialog}
    </div>
  );
}
