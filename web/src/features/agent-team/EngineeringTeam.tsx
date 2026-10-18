import { ChevronDown } from 'lucide-react';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from './AgentRole';
import { ENGINEERING_ROLES } from './roles';

/** A capability introduction, never a roster of invented live workers or assignments. */
export function EngineeringTeam() {
  const { t } = useI18n();
  return <details className="engineering-team">
    <summary>
      <AgentRoleBadge role="manager" compact />
      <span className="engineering-team-title">{t('team.title')}</span>
      <ChevronDown className="engineering-team-chevron" aria-hidden />
    </summary>
    <div className="engineering-team-body">
      <p className="engineering-team-intro">{t('team.role.manager.description')}</p>
      <ul className="engineering-team-members">
        {ENGINEERING_ROLES.filter((role) => role !== 'manager').map((role) => <li key={role}>
          <AgentRoleBadge role={role} compact />
          <p>{t(`team.role.${role}.description`)}</p>
        </li>)}
      </ul>
      <p className="engineering-team-note">{t('team.note')}</p>
    </div>
  </details>;
}
