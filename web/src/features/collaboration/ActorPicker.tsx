// Pick the person or agent profile for a role (executor / final reviewer /
// pre-reviewer / planner). Which kinds are offered follows the execution mode
// (design §0.4): full_auto = agents only, semi_auto reviewer = a person.

import { Bot, UserRound } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { ActorKind, ActorRole, AgentProfile, ExecutionMode } from '../../api/agents';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { Field } from '../../components/ui/Form';
import type { Resource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { eligibleAgents, purposeLabel } from '../agents/common';
import type { Members } from '../tasks/common';

export interface ActorValue {
  kind: ActorKind;
  id: string;
}

export function ActorPicker({
  projectId,
  label,
  hint,
  role,
  mode,
  kinds,
  value,
  onChange,
  members,
  agents,
  excludeAgentId,
  excludeUserId,
  error,
  optional,
  disabled,
}: {
  projectId: UUID;
  label: string;
  hint?: string;
  role: ActorRole;
  mode: ExecutionMode;
  /** Kinds the mode allows for this role. */
  kinds: readonly ActorKind[];
  value: ActorValue;
  onChange: (v: ActorValue) => void;
  members: Members;
  agents: Resource<AgentProfile[]>;
  /** Full auto: the reviewer must differ from the executor (self_review). */
  excludeAgentId?: string;
  excludeUserId?: string;
  error?: string | null;
  optional?: boolean;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const kind: ActorKind = kinds.includes(value.kind) ? value.kind : kinds[0];
  const agentList = eligibleAgents(agents.data, role, mode).filter((a) => a.id !== excludeAgentId);
  const people = (members.list ?? []).filter((m) => m.role !== 'viewer' && m.id !== excludeUserId);
  const loading = kind === 'agent' ? !agents.data && !agents.error : !members.list && !members.error;
  const loadError = kind === 'agent' ? agents.error : members.error;

  return (
    <Field label={label} hint={!loadError ? hint : undefined} error={error ?? (loadError ? t('collab.form.listFailed') : null)} optional={optional}>
      <div className="cl-actor-pick">
        {kinds.length > 1 && (
          <div className="seg cl-kind-seg" role="group" aria-label={t('collab.form.actorKind')}>
            {kinds.map((k) => (
              <button key={k} type="button" className="seg-btn" aria-pressed={kind === k} disabled={disabled} onClick={() => onChange({ kind: k, id: '' })}>
                {k === 'agent' ? <Bot /> : <UserRound />} {t(`collab.actor.${k}`)}
              </button>
            ))}
          </div>
        )}
        <select className="select" value={value.id} disabled={disabled || loading} onChange={(e) => onChange({ kind, id: e.target.value })} aria-label={label}>
          <option value="">{loading ? t('common.loading') : optional ? t('collab.form.none') : t('collab.form.choose')}</option>
          {kind === 'agent'
            ? agentList.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {purposeLabel(t, a.purpose)}
                </option>
              ))
            : people.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} · {t(`role.${m.role}`)}
                </option>
              ))}
          {kind === 'agent' && value.id && !agentList.some((a) => a.id === value.id) && <option value={value.id}>{t('collab.actor.unknownAgent')}</option>}
          {kind === 'human' && value.id && !people.some((m) => m.id === value.id) && <option value={value.id}>{members.name(value.id)}</option>}
        </select>
        {kind === 'agent' && agents.data && !agentList.length && (
          <span className="small faint">
            {t('collab.form.noAgents')} <Link to={paths.agents(projectId)}>{t('collab.form.openAgents')}</Link>
          </span>
        )}
      </div>
    </Field>
  );
}
