// Entry point to the project execution monitor (runs across tasks), for the
// overview and other pages: <ExecutionMonitorLink projectId={…} status="active" />.

import { Activity } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { UUID } from '../../api/types';
import { paths, type MonitorFilters } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';

export function ExecutionMonitorLink({
  projectId,
  filters,
  className = 'btn ghost sm',
  label,
}: {
  projectId: UUID;
  /** Pre-filter, e.g. {status: 'active'}, {status: 'failed'}, {task: id}, {unlinked: true}. */
  filters?: MonitorFilters;
  className?: string;
  label?: string;
}) {
  const { t } = useI18n();
  return (
    <Link className={className} to={paths.runs(projectId, filters)}>
      <Activity aria-hidden /> {label ?? t('tpl.nav.monitor')}
    </Link>
  );
}
