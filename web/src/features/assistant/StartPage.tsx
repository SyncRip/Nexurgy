// Start page (design §3.3): shown when no conversation is selected. Input +
// four clear actions; the project comes from the page (or is picked here when
// there is none). The conversation is created on the first send or when a
// page object is added.

import { useId } from 'react';
import { listAiConversations } from '../../api/assistant';
import { listProjects } from '../../api/endpoints';
import type { UUID } from '../../api/types';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from '../agent-team/AgentRole';
import { useAssistantSession } from './AssistantProvider';
import { Composer } from './Composer';

export function StartPage({ onSelect, compact }: { onSelect: (id: UUID) => void; compact?: boolean }) {
  const { t, fmt } = useI18n();
  const session = useAssistantSession();
  const projectId = session.startProjectId;
  const projects = useResource((signal) => listProjects({}, { signal }), []);
  const recent = useResource((signal) => listAiConversations({ project_id: projectId!, archived: 'false', limit: 5 }, { signal }), [projectId, session.listRevision], { enabled: !!projectId });
  const selId = useId();
  const list = (projects.data ?? []).filter((p) => !p.archived_at || p.id === projectId);
  const current = list.find((p) => p.id === projectId) ?? null;

  return (
    <div className={`as-start ${compact ? 'compact' : ''}`}>
      <div className="as-start-intro">
        <AgentRoleBadge role="manager" />
        <h2>{t('as.start.title')}</h2>
        <p>{t('as.start.body')}</p>
      </div>

      <div className="as-start-project">
        <label htmlFor={selId}>{t('as.start.project')}</label>
        <select id={selId} className="select sm" value={projectId ?? ''} onChange={(e) => session.setStartProjectId(e.target.value || null)} disabled={!projects.data}>
          <option value="">{projects.data ? t('as.start.pickProject') : t('common.loading')}</option>
          {list.map((p) => (
            <option key={p.id} value={p.id}>
              {p.code} · {p.name}
            </option>
          ))}
        </select>
        {projectId && !current && projects.data && <span className="faint small">{t('as.start.projectUnknown')}</span>}
        {session.startTaskId && <span className="faint small">{t('as.start.taskBound')}</span>}
      </div>
      {projects.error ? <ErrorNotice error={projects.error} onRetry={projects.reload} /> : null}

      {projectId ? (
        <Composer detail={null} projectId={projectId} compact={compact} />
      ) : (
        <p className="as-ctx-warn">{t('as.start.needProject')}</p>
      )}

      {!!recent.data?.length && (
        <section className="as-recent" aria-label={t('as.start.recent')}>
          <h3>{t('as.start.recent')}</h3>
          <ul>
            {recent.data.map((c) => (
              <li key={c.id}>
                <button type="button" className="as-switch-item" onClick={() => onSelect(c.id)}>
                  <span className="as-switch-item-t">{c.title || t('as.session.untitled')}</span>
                  <span className="as-switch-item-s">
                    {c.task ? `${c.task.title} · ` : ''}
                    {fmt.relative(c.last_activity_at)}
                    {c.active_operations > 0 ? ` · ${t('as.session.active', { n: c.active_operations })}` : ''}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
