// Top-bar project switcher (R1.2 §04): shows the current project and switches
// it for every area page. "All projects" opens the project list (create,
// archive, import).

import { Check, ChevronDown, FolderKanban, Plus } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';
import { Menu, type MenuItem } from '../ui/Menu';
import { useCurrentProject } from './CurrentProject';

export function ProjectSwitcher() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { projects, project, projectId, switchProject, projectsLoading } = useCurrentProject();

  if (projects && !projects.length) {
    return (
      <Link to={paths.projects()} className="btn sm pswitch-empty">
        <Plus /> <span className="hide-mobile">{t('pswitch.create')}</span>
      </Link>
    );
  }

  const list = (projects ?? []).filter((p) => p.status !== 'archived' || p.id === projectId);
  const items: MenuItem[] = [
    ...list.map((p) => ({
      id: p.id,
      icon: p.id === projectId ? <Check /> : <span className="pswitch-gap" aria-hidden />,
      label: (
        <span className="pswitch-item">
          <span className="mono">{p.code}</span>
          <span className="pswitch-item-name">{p.name}</span>
        </span>
      ),
      onSelect: () => {
        if (p.id !== projectId) switchProject(p.id);
      },
    })),
    { id: '__all', icon: <FolderKanban />, label: t('pswitch.all'), separator: list.length > 0, onSelect: () => navigate(paths.projects()) },
  ];

  return (
    <Menu
      label={t('pswitch.label')}
      align="start"
      triggerClassName="pswitch"
      header={<div className="pswitch-head">{t('pswitch.title')}</div>}
      trigger={
        <>
          <span className="pswitch-code mono">{project?.code ?? (projectsLoading ? '…' : '—')}</span>
          <span className="pswitch-name hide-mobile">{project?.name ?? (projectsLoading ? t('common.loading') : t('pswitch.none'))}</span>
          <ChevronDown className="pswitch-caret" aria-hidden />
        </>
      }
      items={items}
    />
  );
}
