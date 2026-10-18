// Asset detail at /assets/:assetId?version=…&return_to=… — version list on the
// left, the selected version (content, states, provenance, usages) on the
// right. The specialised viewers stay in the part workspace; this page links
// there. D07: a `version` that does not exist or does not belong to this asset
// stops in an explicit error state — no fallback to the latest version, no
// request for any other version, version actions disabled — until the user
// explicitly picks another version. `return_to` is honoured only for in-app
// pages of the same project.

import '../../features/assets/assets.css';
import { ArrowLeft, Boxes, FolderOpen, Play, ShieldAlert } from 'lucide-react';
import { useCallback, useMemo, useState, type KeyboardEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { getAsset, listAssetVersions, type AssetVersion } from '../../api/assets';
import { getPart, getProject, getRun, listMembers } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { ArchivedChip } from '../../components/ui/Badges';
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { AssetVersionPanel } from '../../features/assets/AssetVersionPanel';
import { AssetTypeBadge, EngineeringChip, PublishChip, SourceLabels, VersionToken } from '../../features/assets/meta';
import { safeReturnTo } from '../../features/assets/returnTo';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
// [E templates] "Run with this version": the shared start-run dialog, prefilled with this version.
import { useToast } from '../../components/ui/Toast';
import { StartRunDialog } from '../../features/templates/StartRunDialog';

/** Asset types a template input can take (model_input → part_model, file_input → document). */
const RUNNABLE_INPUT_TYPES = new Set(['part_model', 'document']);

function VersionList({ versions, selectedId, onSelect, title }: { versions: AssetVersion[]; selectedId: string | null; onSelect: (id: string) => void; title?: string }) {
  const { t, fmt } = useI18n();
  // Arrow keys move between versions (roving focus); Enter / Space select.
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const next = versions[Math.min(versions.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    const el = document.getElementById(`as-v-${next.id}`);
    el?.focus();
  };
  return (
    <nav className="panel as-vlist" aria-label={title ?? t('as.versions.title')}>
      <div className="panel-h">
        <h2>{title ?? t('as.versions.title')}</h2>
        <span className="faint small">· {fmt.number(versions.length)}</span>
      </div>
      <ul>
        {versions.map((v, i) => (
          <li key={v.id}>
            <button
              id={`as-v-${v.id}`}
              type="button"
              className={`as-vitem ${v.id === selectedId ? 'active' : ''}`}
              aria-current={v.id === selectedId ? 'true' : undefined}
              onClick={() => onSelect(v.id)}
              onKeyDown={(e) => onKey(e, i)}
            >
              <span className="as-vitem-top">
                <VersionToken label={v.label} />
                {i === 0 && <span className="chip measure as-latest">{t('as.versions.latest')}</span>}
                <span className="grow" />
                <span className="faint small nowrap" title={fmt.dateTime(v.created_at)}>
                  {fmt.relative(v.created_at)}
                </span>
              </span>
              <span className="as-vitem-states">
                <PublishChip state={v.publish_state} />
                {v.engineering && <EngineeringChip engineering={v.engineering} compact />}
              </span>
              {v.labels.length > 0 && <SourceLabels labels={v.labels} max={3} emptyDash={false} />}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export default function AssetDetailPage() {
  const { t } = useI18n();
  const { assetId = '' } = useParams();
  const [search, setSearch] = useSearchParams();
  const asset = useResource((signal) => getAsset(assetId, { signal }), [assetId]);
  const versions = useResource((signal) => listAssetVersions(assetId, { signal }), [assetId]);
  const projectId = asset.data?.project_id;
  const partId = asset.data?.part_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const members = useResource((signal) => listMembers(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const part = useResource((signal) => getPart(partId!, { signal }), [partId], { enabled: !!partId });
  const memberName = useMemo(() => new Map((members.data ?? []).map((m) => [m.user.id, m.user.name])), [members.data]);
  const navigate = useNavigate();
  const toast = useToast();
  const [runWith, setRunWith] = useState<AssetVersion | null>(null);

  const list = versions.data ?? [];
  const requested = search.get('version');
  // D07: a requested version that is not in this asset's list is an error, never "the latest".
  const missingRequested = !!requested && !!versions.data && !list.some((v) => v.id === requested);
  const selected = missingRequested ? undefined : (list.find((v) => v.id === requested) ?? list[0]);

  // return_to: same-project section paths are accepted as-is; run pages only once the run is known to belong to the project.
  const returnTarget = useMemo(() => safeReturnTo(search.get('return_to'), projectId ?? ''), [search, projectId]);
  const returnRunId = returnTarget?.kind === 'run' ? returnTarget.runId : undefined;
  const returnRun = useResource((signal) => getRun(returnRunId!, { signal }), [returnRunId], { enabled: !!returnRunId });
  const returnPath = returnTarget?.kind === 'project' ? returnTarget.path : returnTarget?.kind === 'run' && returnRun.data?.project_id === projectId ? returnTarget.path : null;

  const select = useCallback(
    (id: string) => {
      const next = new URLSearchParams(search);
      next.set('version', id);
      setSearch(next, { replace: true });
    },
    [search, setSearch],
  );
  const onUpdated = useCallback((v: AssetVersion) => versions.setData((prev) => (prev ?? []).map((x) => (x.id === v.id ? v : x))), [versions]);
  // [F modules] a new module version or review request: reload; a new version is the latest, so show the latest.
  const onVersionsChanged = useCallback(
    (newVersionId?: string) => {
      versions.reload();
      asset.reload();
      if (newVersionId) {
        const next = new URLSearchParams(search);
        next.delete('version');
        setSearch(next, { replace: true });
      }
    },
    [versions, asset, search, setSearch],
  );

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.resources'), to: paths.resources() },
    ...(project.data ? [{ label: t('nav.assets'), to: paths.project(project.data.id, 'assets') }] : []),
    ...(asset.data ? [{ label: asset.data.name }] : []),
  ]);

  const err = asset.error || versions.error;
  if (err && (!asset.data || !versions.data)) {
    if (isApiError(err) && err.status === 404)
      return (
        <div className="page">
          <EmptyState title={t('as.notFound.title')} body={t('as.notFound.body')} action={<Link className="btn" to={paths.projects()}>{t('nav.projects')}</Link>} />
        </div>
      );
    return (
      <div className="page">
        <ErrorState
          error={err}
          onRetry={() => {
            asset.reload();
            versions.reload();
          }}
        />
      </div>
    );
  }
  if (!asset.data || !versions.data) {
    return (
      <div className="page">
        <Skeleton w="40%" h={26} />
        <SkeletonRows rows={6} cols={3} />
      </div>
    );
  }

  const a = asset.data;
  const caps = projectCapabilities(project.data);
  const latest = list[0];

  return (
    <div className="page as-page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">
            <AssetTypeBadge type={a.type} />
          </span>
          <h1 className="as-title">
            <span className="ellip" title={a.name}>
              {a.name}
            </span>
            {latest && !missingRequested && <VersionToken label={latest.label} title={t('as.versions.latest')} />}
            {a.archived_at && <ArchivedChip />}
          </h1>
          <div className="row small faint">
            {part.data && (
              <Link to={paths.part(part.data.id)} className="as-use-link">
                <Boxes aria-hidden className="inline-ico" /> <span className="mono">{part.data.number}</span> · {part.data.name}
              </Link>
            )}
            {a.file_id && project.data && (
              <Link to={paths.project(project.data.id, 'files')} className="as-use-link">
                <FolderOpen aria-hidden className="inline-ico" /> {t('as.inFiles')}
              </Link>
            )}
          </div>
        </div>
        <div className="page-actions">
          {returnPath ? (
            <Link className="btn" to={returnPath}>
              <ArrowLeft /> {t('as.returnTo')}
            </Link>
          ) : (
            project.data && (
              <Link className="btn" to={paths.project(project.data.id, 'assets')}>
                <ArrowLeft /> {t('as.backToCatalogue')}
              </Link>
            )
          )}
          {caps.workflow && selected && RUNNABLE_INPUT_TYPES.has(selected.asset_type) && !a.archived_at && (
            <button type="button" className="btn primary" onClick={() => setRunWith(selected)} title={t('tpl.asset.runHint')}>
              <Play /> {t('tpl.asset.run', { label: selected.label })}
            </button>
          )}
        </div>
      </header>

      {missingRequested ? (
        <div className="as-layout">
          <VersionList versions={list} selectedId={null} onSelect={select} title={t('as.versionMissing.choose')} />
          <div className="as-main stack">
            <div className="callout err as-version-missing" role="alert">
              <ShieldAlert aria-hidden />
              <div className="stack-sm">
                <b>{t('as.versionMissing.title')}</b>
                <span>{t('as.versionMissing.body')}</span>
                <code className="mono small as-break">{requested}</code>
                <span className="small faint">{t('as.versionMissing.noFallback')}</span>
              </div>
            </div>
            <section className="panel" aria-labelledby="as-missing-h">
              <div className="panel-h">
                <h2 id="as-missing-h">{t('as.versionMissing.actions')}</h2>
              </div>
              <div className="panel-b stack-sm">
                <p className="small muted">{list.length ? t('as.versionMissing.pickHint') : t('as.noVersionBody')}</p>
                <div className="row">
                  <button type="button" className="btn" disabled title={t('as.versionMissing.disabled')}>
                    {t('as.versionMissing.previewDisabled')}
                  </button>
                  <button type="button" className="btn" disabled title={t('as.versionMissing.disabled')}>
                    {t('as.versionMissing.actionsDisabled')}
                  </button>
                </div>
              </div>
            </section>
          </div>
        </div>
      ) : !list.length || !selected ? (
        <EmptyState compact title={t('as.noVersion')} body={t('as.noVersionBody')} />
      ) : (
        <div className="as-layout">
          <VersionList versions={list} selectedId={selected.id} onSelect={select} />
          <div className="as-main">
            <AssetVersionPanel version={selected} latest={latest} versions={list} caps={caps} memberName={memberName} onUpdated={onUpdated} onSelect={select} onVersionsChanged={onVersionsChanged} />
          </div>
        </div>
      )}
      {runWith && (
        <StartRunDialog
          projectId={a.project_id}
          prefill={runWith}
          onClose={() => setRunWith(null)}
          onStarted={(run) => {
            setRunWith(null);
            toast(t('wf.runStarted'));
            navigate(paths.run(run.id));
          }}
        />
      )}
    </div>
  );
}
