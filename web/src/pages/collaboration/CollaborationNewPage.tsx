// /projects/:projectId/collaboration/new — start a demand (UI01, design §5.2).
// Left: mode, demand, fixed versions, expected deliverables, actors; right: the
// (still empty) task graph. "Generate task graph" plans the draft (C01); a
// ready full-auto draft starts in the same request and opens the workbench.
// Repeated clicks with the same content reuse one idempotency key.

import '../../features/workflow/workflow.css';
import '../../features/collaboration/collaboration.css';
import { ArrowLeft, Info } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { planCollaboration } from '../../api/collaboration';
import { getProject, listParts } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { EmptyState, ErrorNotice, ErrorState, Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useAgentProfiles } from '../../features/agents/common';
import { CL_ERRORS } from '../../features/collaboration/common';
import { DemandForm, formIssues, initialFormValue, planBodyOf, type DemandFormValue } from '../../features/collaboration/DemandForm';
import { DraftGraph } from '../../features/collaboration/DraftGraph';
import { idempotencyKey, newSeed } from '../../features/collaboration/model';
import { useMembers } from '../../features/tasks/common';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

const EMPTY_GRAPH = { nodes: [], edges: [], groups: [] };

export default function CollaborationNewPage() {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { projectId = '' } = useParams();
  const project = useResource((signal) => getProject(projectId, { signal }), [projectId]);
  const parts = useResource((signal) => listParts(projectId, {}, { signal }), [projectId]);
  const members = useMembers(projectId);
  const agents = useAgentProfiles(projectId);
  const [value, setValue] = useState<DemandFormValue>(() => initialFormValue(null));
  const [showIssues, setShowIssues] = useState(false);
  const [busy, setBusy] = useState<'plan' | 'save' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [seed] = useState(newSeed);

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    { label: t('nav.collaboration'), to: paths.collaboration(projectId) },
    { label: t('collab.new') },
  ]);

  if (project.error && !project.data) {
    if (isApiError(project.error) && project.error.status === 404)
      return (
        <div className="page">
          <EmptyState title={t('project.notFound.title')} body={t('project.notFound.body')} />
        </div>
      );
    return (
      <div className="page">
        <ErrorState error={project.error} onRetry={project.reload} />
      </div>
    );
  }
  if (!project.data) {
    return (
      <div className="page">
        <Skeleton w="30%" h={28} />
        <Skeleton w="100%" h={320} />
      </div>
    );
  }

  const caps = projectCapabilities(project.data);
  const issues = formIssues(value, t);
  const hasIssues = Object.keys(issues).length > 0;

  const send = async (kind: 'plan' | 'save') => {
    setShowIssues(true);
    if (hasIssues) return;
    setBusy(kind);
    setError(null);
    const base = planBodyOf(value, t, null);
    const body =
      kind === 'save'
        ? { ...base, planning_source: 'manual' as const, execution_mode: 'semi_auto' as const, graph: EMPTY_GRAPH, auto_start: false }
        : base;
    try {
      const out = await planCollaboration(projectId, { ...body, idempotency_key: idempotencyKey(kind, seed, body) });
      if (out.run) {
        toast(t('collab.form.autoStarted'));
        navigate(paths.collaborationRun(out.run.id));
      } else {
        toast(t(kind === 'save' ? 'collab.form.saved' : 'collab.form.planned'));
        navigate(paths.collaborationDraft(out.workflow.id));
      }
    } catch (e) {
      // The demand is kept as a draft when no model is usable (design C01): continue there.
      const wf = isApiError(e) ? (e.details as { workflow_id?: string }).workflow_id : undefined;
      if (isApiError(e) && e.code === 'ai_unavailable' && wf) {
        toast(t('collab.err.ai_unavailable'), { tone: 'err' });
        navigate(paths.collaborationDraft(wf));
        return;
      }
      setError(e);
      setBusy(null);
    }
  };

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{project.data.code}</span>
          <h1>{t('collab.new')}</h1>
        </div>
        <div className="page-actions">
          <Link className="btn" to={paths.collaboration(projectId)}>
            <ArrowLeft /> {t('collab.backToList')}
          </Link>
        </div>
      </header>
      {!caps.workflow && (
        <div className="callout pending slim" role="note">
          <Info aria-hidden />
          <span>{t('collab.form.readOnly')}</span>
        </div>
      )}
      {error ? <ErrorNotice error={error} overrides={CL_ERRORS} /> : null}
      <div className="cl-draft">
        <DemandForm
          projectId={projectId}
          value={value}
          onChange={setValue}
          members={members}
          agents={agents}
          parts={parts.data}
          issues={showIssues ? issues : null}
          showDeliverables
          busy={busy}
          disabled={!caps.workflow}
          canSave={value.mode === 'semi_auto'}
          onPlan={() => void send('plan')}
          onSave={() => void send('save')}
          isReplan={false}
        />
        <DraftGraph
          draft={null}
          members={members}
          agents={agents.data}
          selected={null}
          onSelect={() => undefined}
          nodeBusy={false}
          nodeError={null}
          onSaveNode={async () => undefined}
          onReload={() => undefined}
          startBlock={t('collab.draft.cannot.noGraph')}
          startBusy={false}
          startError={null}
          onStart={() => undefined}
        />
      </div>
      {value.mode === 'full_auto' && <p className="faint small">{t('collab.form.saveFullAutoHint')}</p>}
    </div>
  );
}
