// Project section "Agents" (UI06, R1.2 prototype #flows.agents / #agent.<id>):
// the card wall, or — with ?agent=<id> — that profile's detail page. ?agent=new
// opens an empty form; ?agent=new&copy=<id> a form prefilled from that profile.
// The selection lives in the URL so a refresh or a shared link keeps it.

import { useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getAgentProfile, type AgentProfile } from '../../api/agents';
import { useFeature } from '../../api/capabilities';
import { listCollaborationDrafts } from '../../api/collaboration';
import { listModelConfigs, listWorkflows } from '../../api/endpoints';
import { listKnowledge } from '../../api/knowledge';
import { paths } from '../../app/nav';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { FeatureGate, FeatureNotice } from '../capabilities/FeatureGate';
import { useFlowsData } from '../flows/data';
import { useMembers } from '../tasks/common';
import { AgentProfileEditor } from './AgentProfileEditor';
import { AgentWall } from './AgentWall';
import { useAgentProfiles } from './common';
import { agentUsage, uniqueName, type AgentUse } from './model';

/** Frozen history resolves the exact profile without loading hidden lists or knowledge counts. */
function HistoricalAgentProfile({ project, profileId }: Pick<ProjectTabProps, 'project'> & { profileId: string }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const profile = useResource((signal) => getAgentProfile(profileId, { signal }), [profileId]);
  const members = useMembers(project.id);
  if (profile.error) return <ErrorState error={profile.error} onRetry={profile.reload} />;
  if (!profile.data) return <SkeletonRows />;
  if (profile.data.project_id !== project.id) return <EmptyState compact title={t('agents.notFound')} body={t('agents.notFoundBody')} />;
  return (
    <div className="stack">
      <FeatureNotice feature="collaboration" />
      <AgentProfileEditor
        project={project}
        profile={profile.data}
        configs={undefined}
        configsError={null}
        members={members}
        canManage={false}
        usage={undefined}
        kbTotal={null}
        onSaved={profile.setData}
        onBack={() => navigate(paths.flows('runs', project.id))}
        onReload={profile.reload}
      />
    </div>
  );
}

export default function ProjectAgentsTab(props: ProjectTabProps) {
  const collaboration = useFeature('collaboration');
  const [params] = useSearchParams();
  const selected = params.get('agent');
  if (!collaboration && selected && selected !== 'new' && !params.has('copy')) {
    return <HistoricalAgentProfile key={`${props.project.id}:${selected}`} project={props.project} profileId={selected} />;
  }
  return <FeatureGate feature="collaboration"><AgentProfiles {...props} /></FeatureGate>;
}

function AgentProfiles({ project, caps }: ProjectTabProps) {
  const { t } = useI18n();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const selected = params.get('agent') ?? '';
  const copyOf = params.get('copy') ?? '';
  const shared = useFlowsData(project.id);
  const own = useAgentProfiles(shared ? undefined : project.id, false);
  const profiles = shared?.agents ?? own;
  const configs = useResource((signal) => listModelConfigs({ signal }), []);
  const members = useMembers(project.id);
  const kb = useResource((signal) => listKnowledge(project.id, { status: 'active', limit: 1 }, { signal }), [project.id]);
  // Where agents are used: project templates and collaboration workflows (drafts and started ones).
  const ownTemplates = useResource((signal) => listWorkflows(project.id, {}, { signal }), [project.id], { enabled: !shared });
  const ownCollab = useResource((signal) => listCollaborationDrafts(project.id, { signal }), [project.id], { enabled: !shared });
  const templates = (shared?.templates ?? ownTemplates).data;
  const collab = (shared?.drafts ?? ownCollab).data;
  const usage = useMemo(() => {
    const uses = [
      ...(templates ?? []).map((w) => ({ ...w, kind: 'template' as const })),
      ...(collab ?? []).map((w) => ({ ...w, kind: 'collaboration' as const })),
    ];
    const byAgent = agentUsage(uses);
    return new Map([...byAgent].map(([id, ws]) => [id, ws.map((w): AgentUse => ({ id: w.id, name: w.name, kind: w.kind }))]));
  }, [templates, collab]);

  const select = (id: string, extra: Record<string, string> = {}) => {
    const next = new URLSearchParams(params);
    next.delete('copy');
    if (id) next.set('agent', id);
    else next.delete('agent');
    for (const [k, v] of Object.entries(extra)) next.set(k, v);
    setParams(next, { replace: !id });
  };

  const all = profiles.data ?? [];
  const current: AgentProfile | null = selected && selected !== 'new' ? all.find((a) => a.id === selected) ?? null : null;
  const creating = selected === 'new';
  const source = creating && copyOf ? all.find((a) => a.id === copyOf) ?? null : null;
  const prefill = useMemo(
    () => (source ? { from: source, name: uniqueName(source.name, all.map((a) => a.name), t('fl.ag.copyWord')) } : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [source?.id, source?.version],
  );

  const upsert = (p: AgentProfile) =>
    profiles.setData((prev) => {
      const xs = prev ?? [];
      return xs.some((x) => x.id === p.id) ? xs.map((x) => (x.id === p.id ? p : x)) : [...xs, p];
    });

  if (creating || current) {
    return (
      <AgentProfileEditor
        key={creating ? `new:${source?.id ?? ''}` : current?.id}
        project={project}
        profile={current}
        prefill={prefill}
        configs={configs.data}
        configsError={configs.error}
        members={members}
        canManage={caps.manage}
        usage={current ? usage.get(current.id) ?? [] : []}
        kbTotal={kb.data?.total ?? null}
        onSaved={(p) => {
          upsert(p);
          select(p.id);
        }}
        onBack={() => select('')}
        onCopy={current ? () => select('new', { copy: current.id }) : undefined}
        onReload={profiles.reload}
      />
    );
  }

  if (selected && !profiles.initial && !profiles.error) {
    return (
      <EmptyState
        compact
        title={t('agents.notFound')}
        body={t('agents.notFoundBody')}
        action={
          <button type="button" className="btn" onClick={() => select('')}>
            {t('agents.backToList')}
          </button>
        }
      />
    );
  }

  return (
    <AgentWall
      project={project}
      canManage={caps.manage}
      profiles={profiles}
      configs={configs.data}
      usage={usage}
      kbTotal={kb.data?.total ?? null}
      onNew={() => select('new')}
      onCopied={(created) => {
        for (const p of created) upsert(p);
        if (created.length) toast(t('fl.ag.copiedToast', { n: created.length }));
      }}
    />
  );
}
