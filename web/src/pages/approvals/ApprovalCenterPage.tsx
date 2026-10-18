// Cross-project approval centre at /approvals: every request in the projects
// the user belongs to, with the same filters as the project tab plus a
// project column.

import { useMemo } from 'react';
import { listProjects } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { useCrumbs } from '../../components/shell/ShellContext';
import { ApprovalList } from '../../features/approvals/ApprovalList';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export default function ApprovalCenterPage() {
  const { t } = useI18n();
  const projects = useResource((signal) => listProjects({ include_archived: true }, { signal }), []);
  const map = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p])), [projects.data]);
  useCrumbs([{ label: t('ap.centre.title'), to: paths.approvals() }]);
  return (
    <div className="page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{t('ap.centre.eyebrow')}</span>
          <h1>{t('ap.centre.title')}</h1>
          <p className="muted small">{t('ap.centre.subtitle')}</p>
        </div>
      </header>
      <ApprovalList projects={map} />
    </div>
  );
}
