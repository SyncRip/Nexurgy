// Agent card wall (R1.2 prototype #flows.agents): one card per profile of the
// project — purpose, model (red when it cannot run), knowledge, tools, step and
// cost ceilings, enabled state, configuration issues and where it is used.
// "Copy from another project" and "New agent" for project managers.

import { CheckCircle2, CircleAlert, Copy, LoaderCircle, Plus } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { AgentProfile } from '../../api/agents';
import { isApiError } from '../../api/client';
import type { ModelConfig, Project } from '../../api/types';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useDebounced } from '../../hooks/keyboard';
import type { Resource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { Trans } from '../../i18n/Trans';
import { useMoney } from '../part/common';
import { RuntimeChip } from '../agentExec/common';
import { EnabledChip, PurposeIcon, purposeLabel } from './common';
import { CopyAgentsDialog } from './CopyAgentsDialog';
import { issueLabel, knowledgeLabel, modelBad, modelLabel } from './labels';
import { agentIssues, modelState, type AgentUse } from './model';

type Filter = 'all' | 'enabled' | 'disabled';

const PURPOSE_TONE: Record<string, string> = { execute: 'measure', review: 'warn', planner: 'stale', general: 'pending' };

/** Execution layer P1 (design §6.5 profile stats): running count, 7-day executions and cost (unknown never 0). */
function CardStats({ a }: { a: AgentProfile }) {
  const { t } = useI18n();
  const money = useMoney();
  const s = a.stats;
  if (!s) return null;
  return (
    <span className="ax-card-stats">
      <span>
        <b>{s.executions_7d}</b>
        <span>{t('ax.card.exec7d')}</span>
      </span>
      <span>
        <b className={s.cost_unknown ? 'faint' : ''} title={s.cost_unknown ? t('ax.budget.unknownHint') : undefined}>
          {s.cost_unknown ? t('ax.budget.unknown') : money(s.cost_fen_7d ?? 0)}
        </b>
        <span>{t('ax.card.cost7d')}</span>
      </span>
    </span>
  );
}

function AgentCard({ a, configs, usage, kbTotal }: { a: AgentProfile; configs: ModelConfig[] | undefined; usage: AgentUse[]; kbTotal: number | null | undefined }) {
  const { t, tn } = useI18n();
  const money = useMoney();
  const m = modelState(a, configs);
  const issues = agentIssues(a, configs);
  const budget = `${t('fl.ag.steps', { n: a.limits.max_steps })}${a.limits.max_cost_fen !== null && a.limits.max_cost_fen !== undefined ? ` · ≤ ${money(a.limits.max_cost_fen)}` : ''}`;
  return (
    <Link to={`?agent=${encodeURIComponent(a.id)}`} className={`ag-wall-card ${a.enabled ? '' : 'is-disabled'}`} aria-label={t('fl.ag.open', { name: a.name })}>
      <span className={`ag-wall-ico tone-${PURPOSE_TONE[a.purpose] ?? 'pending'}`} aria-hidden>
        <PurposeIcon purpose={a.purpose} />
      </span>
      <b className="ag-wall-name">{a.name}</b>
      <p className="ag-wall-desc">{a.description || t('fl.ag.noDescription')}</p>
      <span className="ag-wall-chips">
        <span className="chip accent">{purposeLabel(t, a.purpose)}</span>
        {a.runtime && <RuntimeChip runtime={a.runtime} small />}
        {a.stats && a.stats.running > 0 && (
          <span className="chip measure">
            <LoaderCircle aria-hidden /> {tn('ax.card.running', a.stats.running)}
          </span>
        )}
        {a.model_available !== undefined &&
          (a.model_available ? (
            <span className="chip ok">
              <CheckCircle2 aria-hidden /> {t('ax.card.modelOk')}
            </span>
          ) : (
            <span className="chip err">
              <CircleAlert aria-hidden /> {t('ax.card.modelBad')}
            </span>
          ))}
        <span className={`chip mono ${modelBad(m) ? 'err' : 'pending'}`} title={m.kind === 'own' ? `${m.label} · ${m.modelId}` : undefined}>
          {modelLabel(t, m)}
        </span>
        <span className="chip pending" title={a.knowledge_scope?.scope === 'none' ? undefined : t('fl.ag.kbScopeHint')}>
          {knowledgeLabel(t, a.knowledge_scope?.scope, kbTotal)}
        </span>
        <span className="chip pending">{t('fl.ag.tools', { n: a.allowed_tools.length })}</span>
        <span className="chip pending mono">{budget}</span>
        <EnabledChip enabled={a.enabled} />
        {issues.length > 0 && (
          <span className="chip err" title={issues.map((x) => issueLabel(t, x)).join('；')}>
            {t('fl.ag.issues', { n: issues.length })}
          </span>
        )}
      </span>
      <CardStats a={a} />
      <small className="ag-wall-foot">
        v{a.version} · {usage.length ? t('fl.ag.usedIn', { n: usage.length }) : t('fl.ag.unused')}
        {a.usage?.runs_active ? ` · ${t('fl.ag.runsActive', { n: a.usage.runs_active })}` : ''}
      </small>
    </Link>
  );
}

export function AgentWall({
  project,
  canManage,
  profiles,
  configs,
  usage,
  kbTotal,
  onNew,
  onCopied,
}: {
  project: Project;
  canManage: boolean;
  profiles: Resource<AgentProfile[]>;
  configs: ModelConfig[] | undefined;
  usage: Map<string, AgentUse[]>;
  kbTotal: number | null | undefined;
  onNew: () => void;
  onCopied: (created: AgentProfile[]) => void;
}) {
  const { t } = useI18n();
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim().toLowerCase(), 200);
  const [filter, setFilter] = useState<Filter>('all');
  const [copying, setCopying] = useState(false);

  const all = profiles.data ?? [];
  const list = useMemo(
    () =>
      all
        .filter((a) => (filter === 'all' ? true : filter === 'enabled' ? a.enabled : !a.enabled))
        .filter((a) => !dq || a.name.toLowerCase().includes(dq) || a.description.toLowerCase().includes(dq))
        .sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.name.localeCompare(b.name)),
    [all, filter, dq],
  );

  const actions = canManage ? (
    <>
      <button type="button" className="btn" onClick={() => setCopying(true)}>
        <Copy /> {t('fl.ag.copyFrom')}
      </button>
      <button type="button" className="btn primary" onClick={onNew}>
        <Plus /> {t('agents.new')}
      </button>
    </>
  ) : null;

  let body;
  if (profiles.error && !profiles.data) {
    body = isApiError(profiles.error) && profiles.error.isNotFound ? <ErrorState error={profiles.error} title={t('agents.unavailable')} onRetry={profiles.reload} /> : <ErrorState error={profiles.error} onRetry={profiles.reload} />;
  } else if (profiles.initial) body = <SkeletonRows rows={3} cols={3} />;
  else if (!all.length)
    body = (
      <EmptyState
        title={t('fl.ag.empty', { code: project.code })}
        body={t('fl.ag.emptyBody')}
        action={actions ? <div className="row">{actions}</div> : undefined}
      />
    );
  else if (!list.length)
    body = (
      <EmptyState
        compact
        title={t('common.noMatches')}
        action={
          <button
            type="button"
            className="btn"
            onClick={() => {
              setQ('');
              setFilter('all');
            }}
          >
            {t('common.clearFilters')}
          </button>
        }
      />
    );
  else
    body = (
      <div className={`ag-wall ${profiles.loading ? 'is-refreshing' : ''}`} aria-label={t('agents.listLabel')}>
        {list.map((a) => (
          <AgentCard key={a.id} a={a} configs={configs} usage={usage.get(a.id) ?? []} kbTotal={kbTotal} />
        ))}
      </div>
    );

  return (
    <div className="stack">
      <div className="ag-wall-bar">
        <p className="muted small grow">
          <Trans k="fl.ag.intro" values={{ code: <b className="mono ag-code">{project.code}</b> }} />
        </p>
        {all.length > 0 && actions}
      </div>
      {all.length > 4 && (
        <div className="toolbar">
          <SearchInput value={q} onChange={setQ} placeholder={t('agents.search')} />
          <select className="select" value={filter} onChange={(e) => setFilter(e.target.value as Filter)} aria-label={t('agents.filter.label')}>
            <option value="all">{t('agents.filter.all')}</option>
            <option value="enabled">{t('agents.filter.enabled')}</option>
            <option value="disabled">{t('agents.filter.disabled')}</option>
          </select>
        </div>
      )}
      {profiles.error && profiles.data ? <ErrorNotice error={profiles.error} onRetry={profiles.reload} /> : null}
      {body}
      {copying && (
        <CopyAgentsDialog
          project={project}
          existing={all}
          onClose={() => setCopying(false)}
          onCopied={(created) => {
            setCopying(false);
            onCopied(created);
          }}
        />
      )}
    </div>
  );
}
