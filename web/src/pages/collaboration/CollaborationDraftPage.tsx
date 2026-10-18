import { useFeature } from '../../api/capabilities';
// /collaboration/drafts/:workflowId — edit and start a collaboration draft
// (UI01, design §5.2, C01 / C02). The form saves only the fields that changed;
// node edits patch the selected node only (A54); validation errors point at
// their node (A03); "confirm and start" shows why it is disabled and derives its
// idempotency key from the workflow, draft version and mode (A04), so a reload or a
// second tab replays the same start instead of creating a second run. A draft that
// already has a run shows the link to it instead of the start button.

import '../../features/workflow/workflow.css';
import '../../features/collaboration/collaboration.css';
import { ArrowLeft, ExternalLink, FlaskConical, Info, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { sameActor } from '../../api/agents';
import { isApiError } from '../../api/client';
import {
  getCollaborationDraft,
  patchCollaborationDraft,
  planCollaboration,
  startCollaborationRun,
  type BusinessTaskParams,
  type DraftOut,
  type DraftPatch,
} from '../../api/collaboration';
import { getProject, listParts } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { projectCapabilities } from '../../auth/permissions';
import { AiFailureDetails, asDiagnostic } from '../../components/ai/AiFailureDetails';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { EmptyState, ErrorState, Skeleton } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { PolicyChip } from '../../components/engineering/EngineeringChips';
import { useAgentProfiles } from '../../features/agents/common';
import { ModeChip } from '../../features/collaboration/common';
import { composeDemand, DemandForm, formIssues, initialFormValue, patchBodyOf, planBodyOf, type DemandFormValue } from '../../features/collaboration/DemandForm';
import { DraftGraph } from '../../features/collaboration/DraftGraph';
import { idempotencyKey, newSeed } from '../../features/collaboration/model';
import { CollabError } from '../../features/collaboration/NodeActions';
import { useMembers } from '../../features/tasks/common';
import { policyOf } from '../../features/turbo/model';
import { AssumptionList } from '../../features/turbo/Turbo';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

/**
 * One start key per workflow + draft version + mode (no per-load seed): a repeated click, a reload
 * or another tab sends the same key, and the server answers with the run it already created.
 */
function startIdempotencyKey(workflowId: string, version: number, mode: string, policy: string): string {
  // The policy is part of the fingerprint (W7): a standard start keeps the key it always had.
  return idempotencyKey('start', workflowId, policy === 'turbo' ? { workflow: workflowId, version, mode, policy } : { workflow: workflowId, version, mode });
}

/** The draft-level fields that differ between the stored draft and the form (nothing else is sent). */
function changedFields(draft: DraftOut, v: DemandFormValue, t: Parameters<typeof composeDemand>[1]): Omit<DraftPatch, 'expected_version'> {
  const stored = initialFormValue(draft);
  const full = patchBodyOf(v, t, draft.draft.version);
  const out: Omit<DraftPatch, 'expected_version'> = {};
  if (composeDemand(v, t) !== composeDemand(stored, t)) out.demand = full.demand;
  if (v.mode !== stored.mode) out.execution_mode = v.mode;
  if (!sameActor(full.executor, patchBodyOf(stored, t, 1).executor)) out.executor = full.executor;
  if (!sameActor(full.reviewer, patchBodyOf(stored, t, 1).reviewer)) out.reviewer = full.reviewer;
  if ((full.pre_reviewer_agent_id ?? null) !== (draft.draft.pre_reviewer_agent_id ?? null)) out.pre_reviewer_agent_id = full.pre_reviewer_agent_id ?? null;
  if ((full.part_id ?? null) !== (draft.draft.part_id ?? null)) out.part_id = full.part_id ?? null;
  const fi = (xs: { slot_key: string; asset_version_id: string }[]) => JSON.stringify(xs.map((x) => [x.slot_key, x.asset_version_id]));
  if (fi(full.fixed_inputs ?? []) !== fi(draft.draft.fixed_inputs ?? [])) out.fixed_inputs = full.fixed_inputs;
  return out;
}

export default function CollaborationDraftPage() {
  const enabled = useFeature('collaboration');
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const { workflowId = '' } = useParams();
  const [search, setSearch] = useSearchParams();
  const res = useResource((signal) => getCollaborationDraft(workflowId, { signal }), [workflowId]);
  const draft = res.data && res.data.workflow.id === workflowId ? res.data : undefined;
  const projectId = draft?.draft.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const parts = useResource((signal) => listParts(projectId!, {}, { signal }), [projectId], { enabled: !!projectId });
  const members = useMembers(projectId);
  const agents = useAgentProfiles(projectId);
  const [value, setValue] = useState<DemandFormValue>(() => initialFormValue(null));
  const [formFor, setFormFor] = useState<string | null>(null);
  const [showIssues, setShowIssues] = useState(false);
  const [busy, setBusy] = useState<'plan' | 'save' | null>(null);
  const [formError, setFormError] = useState<unknown>(null);
  const [nodeBusy, setNodeBusy] = useState(false);
  const [nodeError, setNodeError] = useState<unknown>(null);
  const [startBusy, setStartBusy] = useState(false);
  const [startError, setStartError] = useState<unknown>(null);
  const [missingBusy, setMissingBusy] = useState(false);
  const [seed] = useState(newSeed);
  const selected = search.get('node');
  const setSelected = (id: string | null) => {
    const next = new URLSearchParams(search);
    if (id) next.set('node', id);
    else next.delete('node');
    setSearch(next, { replace: true });
  };

  // The form follows the stored draft when it first loads and after an explicit reload or save;
  // node edits (which bump the version) never overwrite unsaved form input.
  useEffect(() => {
    if (draft && formFor !== draft.draft.id) {
      setValue(initialFormValue(draft));
      setFormFor(draft.draft.id);
    }
  }, [draft, formFor]);

  const reloadAll = () => {
    setFormFor(null);
    setFormError(null);
    setNodeError(null);
    setStartError(null);
    res.reload();
  };

  useShellProject(project.data);
  useCrumbs([
    { label: t('area.flows'), to: paths.flows() },
    ...(projectId ? [{ label: t('nav.collaboration'), to: paths.collaboration(projectId) }] : []),
    ...(draft ? [{ label: draft.workflow.name || t('collab.draft.untitled') }] : []),
  ]);

  if (res.error && !draft) {
    if (isApiError(res.error) && res.error.status === 404)
      return (
        <div className="page">
          <EmptyState title={t('collab.draft.notFound')} body={t('collab.draft.notFoundBody')} action={<Link className="btn" to={paths.projects()}>{t('nav.projects')}</Link>} />
        </div>
      );
    return (
      <div className="page">
        <ErrorState error={res.error} onRetry={res.reload} />
      </div>
    );
  }
  if (!draft || !projectId) {
    return (
      <div className="page">
        <Skeleton w="30%" h={28} />
        <Skeleton w="100%" h={320} />
      </div>
    );
  }

  const caps = projectCapabilities(project.data);
  const canEdit = enabled && !!project.data && caps.workflow;
  const issues = formIssues(value, t);
  const hasIssues = Object.keys(issues).length > 0;
  const pending = changedFields(draft, value, t);
  const dirty = Object.keys(pending).length > 0;
  const planning = draft.planning ?? draft.draft.planning ?? {};
  const started = draft.run;
  const storedPolicy = policyOf(draft.draft);
  // The policy is frozen with the plan: another policy is a re-plan (a new version), never a PATCH (W7).
  const policyChanged = value.policy !== storedPolicy;

  const save = async () => {
    setShowIssues(true);
    if (!canEdit || hasIssues) return;
    if (!dirty) {
      toast(t('collab.form.nothingToSave'), { tone: 'info' });
      return;
    }
    setBusy('save');
    setFormError(null);
    try {
      const out = await patchCollaborationDraft(workflowId, { expected_version: draft.draft.version, ...pending });
      res.setData(out);
      setValue(initialFormValue(out));
      toast(t('collab.form.saved'));
    } catch (e) {
      setFormError(e);
    } finally {
      setBusy(null);
    }
  };

  const replan = async () => {
    setShowIssues(true);
    if (!canEdit || hasIssues) return;
    setBusy('plan');
    setFormError(null);
    const body = planBodyOf(value, t, workflowId);
    try {
      const out = await planCollaboration(draft.draft.project_id, { ...body, idempotency_key: idempotencyKey('plan', seed, { ...body, version: draft.draft.version }) });
      if (out.run && out.run.id !== started?.id) {
        toast(t('collab.form.autoStarted'));
        navigate(paths.collaborationRun(out.run.id));
        return;
      }
      res.setData(out);
      setValue(initialFormValue(out));
      toast(t('collab.form.planned'));
    } catch (e) {
      setFormError(e);
      if (isApiError(e) && e.code === 'ai_unavailable') res.reload();
    } finally {
      setBusy(null);
    }
  };

  const saveNode = async (nodeId: string, patch: Partial<BusinessTaskParams>) => {
    if (!canEdit) return;
    setNodeBusy(true);
    setNodeError(null);
    try {
      const out = await patchCollaborationDraft(workflowId, { expected_version: draft.draft.version, nodes: { [nodeId]: patch } });
      res.setData(out);
      toast(t('collab.draft.nodeSaved'));
    } catch (e) {
      setNodeError(e);
    } finally {
      setNodeBusy(false);
    }
  };

  const resolveMissing = async (index: number) => {
    setMissingBusy(true);
    setFormError(null);
    try {
      const out = await patchCollaborationDraft(workflowId, { expected_version: draft.draft.version, missing_information: draft.missing_information.filter((_, i) => i !== index) });
      res.setData(out);
    } catch (e) {
      setFormError(e);
    } finally {
      setMissingBusy(false);
    }
  };

  const start = async () => {
    if (!canEdit) return;
    setStartBusy(true);
    setStartError(null);
    const mode = draft.draft.execution_mode;
    try {
      const out = await startCollaborationRun(workflowId, {
        execution_mode: mode,
        execution_policy: storedPolicy,
        expected_version: draft.draft.version,
        idempotency_key: startIdempotencyKey(workflowId, draft.draft.version, mode, storedPolicy),
      });
      toast(t('collab.draft.started'));
      navigate(paths.collaborationRun(out.run.id));
    } catch (e) {
      // Started meanwhile (another tab / person): open that run instead of starting a second one.
      const existing = isApiError(e) && e.code === 'draft_already_started' && typeof e.details.run_id === 'string' ? e.details.run_id : null;
      if (existing) {
        toast(t('collab.draft.alreadyStartedToast'), { tone: 'info' });
        navigate(paths.collaborationRun(existing));
        return;
      }
      setStartError(e);
      setStartBusy(false);
      // Refresh the stored validation so the reasons shown match the server.
      if (isApiError(e) && (e.code === 'draft_not_ready' || e.code === 'version_conflict' || e.code === 'draft_already_started')) res.reload();
    }
  };

  const startBlock = started
    ? t('collab.draft.cannot.started')
    : !canEdit
      ? t('collab.draft.cannot.permission')
      : policyChanged
        ? t('error.replan_required')
        : dirty
          ? t('collab.draft.cannot.unsaved')
          : null;

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{project.data?.code ?? ''}</span>
          <h1 className="ellip" title={draft.workflow.name}>
            {draft.workflow.name || t('collab.draft.untitled')}
          </h1>
          <div className="row">
            <ModeChip mode={draft.draft.execution_mode} />
            <PolicyChip policy={storedPolicy} />
            <span className="faint small">{t('collab.draft.storedMode')}</span>
          </div>
        </div>
        <div className="page-actions">
          <Link className="btn" to={paths.collaboration(draft.draft.project_id)}>
            <ArrowLeft /> {t('collab.backToList')}
          </Link>
        </div>
      </header>

      {started && (
        <div className="callout info slim" role="status">
          <Info aria-hidden />
          <span className="row">
            {t('collab.draft.alreadyStarted')}
            <Link to={paths.collaborationRun(started.id)}>
              <ExternalLink className="inline-ico" aria-hidden /> {started.title || t('collab.run.untitled')}
            </Link>
          </span>
        </div>
      )}
      {planning.status === 'unavailable' && (
        <div className="callout warn slim" role="note">
          <TriangleAlert aria-hidden />
          <div className="grow stack-sm">
            <span>{t('collab.draft.planningUnavailable')}</span>
            <AiFailureDetails diagnostic={asDiagnostic(planning.diagnostic)} />
          </div>
        </div>
      )}
      {planning.status === 'failed' && (
        <div className="callout err slim" role="note">
          <TriangleAlert aria-hidden />
          <div className="grow stack-sm">
            <span>{t('collab.draft.planningFailed')}</span>
            <AiFailureDetails diagnostic={asDiagnostic(planning.diagnostic)} />
          </div>
        </div>
      )}
      {planning.simulated && (
        <div className="callout pending slim" role="note">
          <FlaskConical aria-hidden />
          <span>{t('collab.simulatedHint')}</span>
        </div>
      )}
      {dirty && (
        <div className="callout warn slim" role="status">
          <Info aria-hidden />
          <span>{t('collab.form.unsaved')}</span>
        </div>
      )}
      <CollabError error={formError} onReload={reloadAll} />
      {(draft.draft.assumptions ?? []).length > 0 && (
        <div className="panel">
          <div className="panel-b">
            <AssumptionList items={draft.draft.assumptions} />
          </div>
        </div>
      )}

      <div className="cl-draft">
        <DemandForm
          projectId={draft.draft.project_id}
          value={value}
          onChange={setValue}
          members={members}
          agents={agents}
          parts={parts.data}
          issues={showIssues ? issues : null}
          showDeliverables={false}
          busy={busy}
          disabled={!canEdit}
          canSave={!policyChanged}
          onPlan={() => void replan()}
          onSave={() => void save()}
          isReplan
          policyNote={t('tb.policy.replanNote')}
        />
        <DraftGraph
          draft={draft}
          members={members}
          agents={agents.data}
          selected={selected}
          onSelect={setSelected}
          nodeBusy={nodeBusy}
          nodeError={nodeError}
          onSaveNode={saveNode}
          onReload={reloadAll}
          startBlock={startBlock}
          startBusy={startBusy}
          startError={startError}
          onStart={() => void start()}
          onResolveMissing={canEdit && !started ? (i) => void resolveMissing(i) : undefined}
          missingBusy={missingBusy}
        />
      </div>
    </div>
  );
}
