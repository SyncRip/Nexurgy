import { Box, BriefcaseBusiness, Calculator, Component, DraftingCompass, Lightbulb, Route, type LucideIcon } from 'lucide-react';
import { useI18n } from '../../i18n/I18nProvider';
import type { EngineeringRole } from './roles';
import './agentTeam.css';

const ICONS: Record<EngineeringRole, LucideIcon> = {
  manager: BriefcaseBusiness,
  design: Lightbulb,
  modeling: Box,
  assembly: Component,
  drawing: DraftingCompass,
  process: Route,
  quote: Calculator,
};

export function AgentRoleIcon({ role, className }: { role: EngineeringRole; className?: string }) {
  const Icon = ICONS[role];
  return <Icon className={className} aria-hidden />;
}

/** Identity only: status always comes from the operation or stage beside this badge. */
export function AgentRoleBadge({ role, compact = false }: { role: EngineeringRole; compact?: boolean }) {
  const { t } = useI18n();
  return <span className={`agent-role${compact ? ' compact' : ''}`} data-engineering-role={role} title={t(`team.role.${role}.description`)}>
    <AgentRoleIcon role={role} />
    <span className="agent-role-name">{t(`team.role.${role}`)}</span>
    <span className="agent-role-ai">AI</span>
  </span>;
}
