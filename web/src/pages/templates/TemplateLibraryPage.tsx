// The cross-project public template library (contract §2.8): browse, open a
// template's versions, import a version into a project. Shown as the
// "Template library" tab of AI workflows (/flows/library); /template-library
// redirects there.

import '../../features/workflow/workflow.css';
import '../../features/templates/templates.css';
import { Download, Library } from 'lucide-react';
import { useState } from 'react';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { listTemplateLibrary } from '../../api/endpoints';
import type { PublicTemplate } from '../../api/types';
import { paths } from '../../app/nav';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, ErrorState, SkeletonCards } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { VersionNo } from '../../features/templates/common';
import { ImportDialog } from '../../features/templates/LibraryDialogs';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export function TemplateLibraryView() {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const [importing, setImporting] = useState<PublicTemplate | null>(null);
  const res = useResource((signal) => listTemplateLibrary({ q: dq || undefined }, { signal }), [dq]);
  const items = res.data ?? [];

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonCards count={4} />;
  else if (!items.length) body = <EmptyState title={dq ? t('common.noMatches') : t('tpl.lib.empty')} body={dq ? undefined : t('tpl.lib.emptyBody')} />;
  else
    body = (
      <div className={`tpl-lib-grid ${res.loading ? 'is-refreshing' : ''}`}>
        {items.map((p) => (
          <article key={p.id} className="panel tpl-lib-card">
            <div className="tpl-lib-card-top">
              <span className="wf-tpl-ico">
                <Library aria-hidden />
              </span>
              <Link to={paths.publicTemplate(p.id)} className="tpl-lib-name strong">
                {p.name}
              </Link>
              {p.latest_version && <VersionNo n={p.latest_version.version_no} />}
            </div>
            {p.description ? <p className="tpl-clamp small muted">{p.description}</p> : <p className="faint small">{t('tpl.overview.noDescription')}</p>}
            <p className="faint small">
              {t('tpl.lib.meta', { n: p.version_count, by: p.created_by?.name ?? '—' })}
              {p.source_project ? ` · ${t('tpl.lib.fromProject', { code: p.source_project.code })}` : ''}
              {p.latest_version?.published_at ? ` · ${fmt.date(p.latest_version.published_at)}` : ''}
            </p>
            <div className="row">
              <Link to={paths.publicTemplate(p.id)} className="btn sm">
                {t('tpl.lib.versions')}
              </Link>
              <button type="button" className="btn sm primary" onClick={() => setImporting(p)} disabled={!p.latest_version}>
                <Download /> {t('tpl.import.action')}
              </button>
            </div>
          </article>
        ))}
      </div>
    );

  return (
    <>
      <p className="muted small">{t('tpl.lib.intro')}</p>
      <div className="toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t('tpl.lib.search')} />
      </div>
      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}
      {importing && (
        <ImportDialog
          templateId={importing.id}
          defaultVersionId={importing.latest_version?.id}
          onClose={() => setImporting(null)}
          onImported={(w) => {
            setImporting(null);
            toast(t('tpl.import.done', { name: w.name }));
            navigate(paths.workflow(w.id));
          }}
        />
      )}
    </>
  );
}

/** Old standalone route → the AI workflows tab. */
export default function TemplateLibraryPage() {
  return <Navigate to={paths.templateLibrary()} replace />;
}
