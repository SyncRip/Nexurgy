// Approval requests of one project (engineering reviews from runs and task
// acceptance from submissions). The cross-project inbox lives at /approvals.

import { Inbox } from 'lucide-react';
import { Link } from 'react-router-dom';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { ApprovalList } from './ApprovalList';

export default function ProjectApprovalsTab({ project }: ProjectTabProps) {
  const { t } = useI18n();
  return (
    <div className="stack">
      <div className="row ap-tab-intro">
        <p className="muted small grow">{t('ap.projectIntro')}</p>
        <Link className="btn sm" to={paths.approvals()}>
          <Inbox /> {t('ap.openCentre')}
        </Link>
      </div>
      <ApprovalList projectId={project.id} />
    </div>
  );
}
