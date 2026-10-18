// Agent profile vocabulary: purpose labels, enabled / disabled chips, model
// readiness and the profile list hook shared by the collaboration forms.

import './agents.css';
import { Bot, CheckCircle2, CircleDashed, CircleX, PauseCircle, Sparkles, UserCheck, Wrench } from 'lucide-react';
import type { ReactNode } from 'react';
import { listAgentProfiles, type ActorRole, type AgentModelConfigRef, type AgentProfile, type AgentPurpose, type ExecutionMode, isSelectableRuntime, purposeAllows } from '../../api/agents';
import type { UUID } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { useResource, type Resource } from '../../hooks/useResource';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';

export function PurposeIcon({ purpose }: { purpose: AgentPurpose | string | null | undefined }) {
  switch (purpose) {
    case 'execute':
      return <Wrench aria-hidden />;
    case 'review':
      return <UserCheck aria-hidden />;
    case 'planner':
      return <Sparkles aria-hidden />;
    default:
      return <Bot aria-hidden />;
  }
}

export const purposeLabel = (t: TFunction, purpose: string | null | undefined) => (purpose ? t(`agents.purpose.${purpose}` as 'agents.purpose.execute') : '—');

export function PurposeChip({ purpose }: { purpose: AgentPurpose }) {
  const { t } = useI18n();
  return (
    <Chip tone="measure" icon={<PurposeIcon purpose={purpose} />}>
      {purposeLabel(t, purpose)}
    </Chip>
  );
}

export function EnabledChip({ enabled }: { enabled: boolean }) {
  const { t } = useI18n();
  return enabled ? (
    <Chip tone="ok" icon={<CheckCircle2 aria-hidden />}>
      {t('agents.enabled')}
    </Chip>
  ) : (
    <Chip tone="pending" icon={<PauseCircle aria-hidden />}>
      {t('agents.disabledState')}
    </Chip>
  );
}

/** Readiness of the model configuration a profile points to (null = the project default). */
export function ModelReadiness({ config, enabled }: { config: AgentModelConfigRef | null | undefined; enabled?: boolean }): ReactNode {
  const { t } = useI18n();
  if (!config) return <span className="faint small">{t('agents.field.modelConfigDefault')}</span>;
  if (enabled === false)
    return (
      <Chip tone="pending" icon={<CircleX aria-hidden />}>
        {t('agents.modelDisabled')}
      </Chip>
    );
  return config.ready ? (
    <Chip tone="ok" icon={<CheckCircle2 aria-hidden />}>
      {t('agents.modelReady')}
    </Chip>
  ) : (
    <Chip tone="warn" icon={<CircleDashed aria-hidden />}>
      {t('agents.modelNotReady')}
    </Chip>
  );
}

/** Enabled profiles of a project (the collaboration forms pick actors from it). */
export function useAgentProfiles(projectId: UUID | undefined, enabledOnly = true): Resource<AgentProfile[]> {
  return useResource((signal) => listAgentProfiles(projectId!, enabledOnly ? { enabled: true } : {}, { signal }), [projectId, enabledOnly], { enabled: !!projectId });
}

/** Profiles that may take `role` in `mode`. */
export function eligibleAgents(list: AgentProfile[] | undefined, role: ActorRole, mode: ExecutionMode | null): AgentProfile[] {
  return (list ?? []).filter((a) => a.enabled && isSelectableRuntime(a.runtime ?? 'native') && purposeAllows(a.purpose, role) && (!mode || a.execution_modes.includes(mode)));
}
