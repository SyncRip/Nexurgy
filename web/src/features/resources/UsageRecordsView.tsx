// /resources/process — usage records of one project (handoff §3: "Process resources → Material details → Usage records; More → View usage within the scope
// records"): every use of an exact asset version in a run / node, with the existing filters and detail (ResourcesTab).
// Records are listed per project on the server, so the cross-project scopes ask the user to choose a project.

import '../assets/assets.css';
import { ArrowLeft, Waypoints } from 'lucide-react';
import { Suspense, lazy } from 'react';
import { Link } from 'react-router-dom';
import type { Project } from '../../api/types';
import { projectCapabilities } from '../../auth/permissions';
import { EmptyState, SkeletonRows } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { scopeUrl } from './scope';
import type { ViewScope } from './types';

const ResourcesTab = lazy(() => import('../assets/ResourcesTab'));

export default function UsageRecordsView({ scope, project, reloadProject }: { scope: ViewScope; project: Project | null; reloadProject: () => void }) {
  const { t } = useI18n();
  return (
    <div className="rp-records stack">
      <div className="rp-records-head">
        <Link to={scopeUrl('files', scope)} className="linkish small rp-back">
          <ArrowLeft aria-hidden /> {t('rp.records.back')}
        </Link>
        <h2 className="rp-records-title">{t('rp.records.title')}</h2>
        <p className="faint small">{t('rp.records.sub')}</p>
      </div>
      {scope.kind === 'project' && project ? (
        <Suspense fallback={<SkeletonRows />}>
          <ResourcesTab project={project} caps={projectCapabilities(project)} reloadProject={reloadProject} />
        </Suspense>
      ) : (
        <EmptyState compact icon={<Waypoints className="big-ico" aria-hidden />} title={t('rp.records.needProject.title')} body={t('rp.records.needProject.body')} />
      )}
    </div>
  );
}
