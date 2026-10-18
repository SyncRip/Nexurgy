import { useFeature } from '../../api/capabilities';
// /template-library/:publicTemplateId — one public template: description, source,
// versions (notes, hash, status), import a version into a project, and (creator /
// admin) deprecate a version. Importing creates a project template with its own v1;
// new public versions never change projects that imported earlier ones.

import '../../features/workflow/workflow.css';
import '../../features/templates/templates.css';
import { ArrowLeft, Ban, Download, MoreHorizontal } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { deprecatePublicTemplateVersion, getPublicTemplate } from '../../api/endpoints';
import type { PublicTemplateVersion, UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useCrumbs } from '../../components/shell/ShellContext';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { Menu, type MenuItem } from '../../components/ui/Menu';
import { EmptyState, ErrorNotice, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { Kv, PolicySummary, TPL_ERRORS, VersionNo, VersionStatusChip } from '../../features/templates/common';
import { ImportDialog } from '../../features/templates/LibraryDialogs';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export default function PublicTemplatePage() {
  const libraryEnabled = useFeature('template_library');
  const { t, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { publicTemplateId = '' } = useParams();
  const res = useResource((signal) => getPublicTemplate(publicTemplateId, { signal }), [publicTemplateId]);
  const [importing, setImporting] = useState<{ versionId?: UUID } | null>(null);
  const [deprecating, setDeprecating] = useState<PublicTemplateVersion | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const pt = res.data;

  useCrumbs([{ label: t('area.flows'), to: paths.flows() }, { label: t('tpl.lib.title'), to: paths.templateLibrary() }, ...(pt ? [{ label: pt.name }] : [])]);

  if (res.error && !pt) {
    if (isApiError(res.error) && res.error.status === 404)
      return (
        <div className="page">
          <EmptyState title={t('tpl.lib.notFound')} body={t('tpl.lib.notFoundBody')} action={<Link className="btn" to={paths.templateLibrary()}>{t('tpl.lib.title')}</Link>} />
        </div>
      );
    return (
      <div className="page">
        <ErrorState error={res.error} onRetry={res.reload} />
      </div>
    );
  }
  if (!pt) {
    return (
      <div className="page">
        <Skeleton w="40%" h={26} />
        <SkeletonRows rows={4} cols={4} />
      </div>
    );
  }

  const versions = [...pt.versions].sort((a, b) => b.version_no - a.version_no);
  const doDeprecate = async () => {
    if (!deprecating) return;
    setBusy(true);
    setError(null);
    try {
      await deprecatePublicTemplateVersion(deprecating.id);
      toast(t('tpl.deprecate.done', { n: deprecating.version_no }));
      setDeprecating(null);
      res.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const actions = (v: PublicTemplateVersion): MenuItem[] => {
    const items: MenuItem[] = [];
    if (libraryEnabled && v.status === 'published') items.push({ id: 'import', label: t('tpl.import.action'), icon: <Download />, onSelect: () => setImporting({ versionId: v.id }) });
    if (libraryEnabled && pt.can_manage && v.status === 'published') items.push({ id: 'deprecate', label: t('tpl.deprecate.action'), icon: <Ban />, danger: true, separator: items.length > 0, onSelect: () => setDeprecating(v) });
    return items;
  };
  const latest = versions.find((v) => v.status === 'published');

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{t('tpl.lib.eyebrow')}</span>
          <h1>{pt.name}</h1>
          {pt.description ? <p className="muted tpl-pre">{pt.description}</p> : null}
        </div>
        <div className="page-actions">
          <Link className="btn" to={paths.templateLibrary()}>
            <ArrowLeft /> {t('tpl.lib.back')}
          </Link>
          <button type="button" className="btn primary" onClick={() => setImporting({ versionId: latest?.id })} disabled={!libraryEnabled || !latest}>
            <Download /> {t('tpl.import.action')}
          </button>
        </div>
      </header>

      <dl className="tpl-kv panel panel-b">
        <Kv label={t('tpl.lib.createdBy')}>{pt.created_by?.name ?? '—'}</Kv>
        <Kv label={t('tpl.lib.source')}>
          {pt.source_project ? (
            <Link to={paths.project(pt.source_project.id)} className="mono">
              {pt.source_project.code}
            </Link>
          ) : (
            '—'
          )}
        </Kv>
        <Kv label={t('tpl.lib.latest')}>{latest ? <VersionNo n={latest.version_no} /> : '—'}</Kv>
        <Kv label={t('tpl.policy.title')}>{latest?.policy ? <PolicySummary policy={latest.policy} /> : '—'}</Kv>
      </dl>

      <section className="stack-sm" aria-labelledby="tpl-pub-versions">
        <h2 id="tpl-pub-versions" className="tpl-h2">
          {t('tpl.tab.versions')}
        </h2>
        <p className="faint small">{t('tpl.lib.versionsNote')}</p>
        {!versions.length ? (
          <EmptyState compact title={t('tpl.import.noVersions')} />
        ) : (
          <div className="table-wrap">
            <table className="t dense">
              <thead>
                <tr>
                  <th>{t('tpl.versions.col.version')}</th>
                  <th className="hide-sm">{t('tpl.versions.col.published')}</th>
                  <th>{t('tpl.versions.col.notes')}</th>
                  <th className="hide-md">{t('tpl.versions.hash')}</th>
                  <th className="act">
                    <span className="sr-only">{t('common.actions')}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} className={v.status === 'deprecated' ? 'is-archived' : undefined}>
                    <td>
                      <div className="cell-main">
                        <VersionNo n={v.version_no} />
                        <VersionStatusChip status={v.status} />
                      </div>
                    </td>
                    <td className="hide-sm small">
                      <span title={fmt.dateTime(v.published_at)}>{fmt.date(v.published_at)}</span>
                      {v.published_by && <div className="faint">{v.published_by.name}</div>}
                    </td>
                    <td className="small">
                      <span className="tpl-clamp">{v.notes || <span className="faint">{t('tpl.versions.noNotes')}</span>}</span>
                    </td>
                    <td className="hide-md mono small faint" title={v.hash}>
                      {v.hash.slice(0, 12)}
                    </td>
                    <td className="act">{actions(v).length > 0 && <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actions(v)} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {libraryEnabled && importing && (
        <ImportDialog
          template={pt}
          defaultVersionId={importing.versionId}
          onClose={() => setImporting(null)}
          onImported={(w) => {
            setImporting(null);
            toast(t('tpl.import.done', { name: w.name }));
            navigate(paths.workflow(w.id));
          }}
        />
      )}
      {libraryEnabled && deprecating && (
        <ConfirmDialog
          title={t('tpl.deprecate.title', { n: deprecating.version_no })}
          body={t('tpl.lib.deprecateBody')}
          confirmLabel={t('tpl.deprecate.submit')}
          danger
          busy={busy}
          error={<ErrorNotice error={error} overrides={TPL_ERRORS} />}
          onConfirm={() => void doDeprecate()}
          onClose={() => {
            setDeprecating(null);
            setError(null);
          }}
        />
      )}
    </div>
  );
}
