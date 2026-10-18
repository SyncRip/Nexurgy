// "View scope" — the only scope entry of resources & knowledge (handoff §4): one select with the projects (name · code)
// and a cross-project group (all accessible project materials), a one-line summary of what the scope
// shows in the current view, and — in a project scope only — the way to the project's task results (usage records).

import { Waypoints } from 'lucide-react';
import { useId } from 'react';
import type { Project } from '../../api/types';
import type { ResourceTab } from '../../app/nav';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { parseScopeValue, scopeValue } from './scope';
import type { ViewScope } from './types';

/** Display name of a scope (notices, the view bar context). */
export function scopeName(t: TFunction, scope: ViewScope, projects: readonly Project[]): string {
  if (scope.kind === 'all') return t('rp.scope.all');
  const p = projects.find((x) => x.id === scope.projectId);
  return p ? t('rp.scope.projectOption', { name: p.name, code: p.code }) : scope.projectId.slice(0, 8);
}

function summary(t: TFunction, scope: ViewScope, project: Project | undefined, experience: boolean): { title: string; desc: string } {
  if (scope.kind === 'all') return { title: t('rp.scope.all.title'), desc: t(experience ? 'rp.scope.all.descExp' : 'rp.scope.all.desc') };
  const code = project?.code ?? '…';
  return {
    title: t(experience ? 'rp.scope.project.titleExp' : 'rp.scope.project.title', { code }),
    desc: t(experience ? 'rp.scope.project.descExp' : 'rp.scope.project.desc'),
  };
}

export function ScopePanel({
  scope,
  view,
  projects,
  onChange,
  onTaskResults,
}: {
  scope: ViewScope;
  view: ResourceTab;
  /** Accessible projects (plus the scope's own project when it is not listed, e.g. archived). */
  projects: readonly Project[];
  onChange: (scope: ViewScope) => void;
  onTaskResults?: () => void;
}) {
  const { t } = useI18n();
  const id = useId();
  const project = scope.kind === 'project' ? projects.find((p) => p.id === scope.projectId) : undefined;
  const { title, desc } = summary(t, scope, project, view === 'knowledge');

  return (
    <section className="rp-scope" aria-label={t('rp.scope.region')}>
      <div className="rp-scope-picker">
        <label htmlFor={`${id}-select`}>{t('rp.scope.label')}</label>
        <select
          id={`${id}-select`}
          className="select"
          value={scopeValue(scope)}
          aria-describedby={`${id}-desc`}
          onChange={(e) => {
            const next = parseScopeValue(e.target.value);
            if (next) onChange(next);
          }}
        >
          {projects.length > 0 && (
            <optgroup label={t('rp.scope.groupProjects')}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {t(p.status === 'archived' ? 'rp.scope.projectOptionArchived' : 'rp.scope.projectOption', { name: p.name, code: p.code })}
                </option>
              ))}
            </optgroup>
          )}
          <optgroup label={t('rp.scope.groupCross')}>
            <option value="all">{t('rp.scope.all')}</option>
          </optgroup>
        </select>
      </div>
      <details className="rp-scope-summary">
        <summary>{t('rp.scope.explain')}</summary>
        <div className="rp-scope-help">
          <strong>{title}</strong>
          <p id={`${id}-desc`}>{desc}</p>
          <p>{t('rp.scope.ownership')}</p>
        </div>
      </details>
      {onTaskResults && scope.kind === 'project' && view !== 'process' && (
        <button type="button" className="btn rp-scope-action" onClick={onTaskResults} aria-label={project ? t('rp.scope.taskResultsOf', { name: project.name }) : undefined}>
          <Waypoints aria-hidden /> {t('rp.scope.taskResults')}
        </button>
      )}
    </section>
  );
}
