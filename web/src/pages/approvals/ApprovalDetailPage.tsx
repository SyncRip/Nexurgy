// Approval request detail at /approval-requests/:requestId — the frozen
// target on the left, decision / assignment / history on the right.

import '../../features/approvals/approvals.css';
import '../../features/assets/assets.css';
import '../../features/collaboration/collaboration.css';
import { History, Inbox, ListChecks, ShieldAlert, UserCog, Waypoints } from 'lucide-react';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { useCapabilities } from '../../api/capabilities';
import { getApprovalRequest, isCollaborationAcceptance, isDirectEngineering } from '../../api/approvals';
import { getNodeTask } from '../../api/collaboration';
import { getProject } from '../../api/endpoints';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { projectCapabilities } from '../../auth/permissions';
import { useCrumbs, useShellProject } from '../../components/shell/ShellContext';
import { EmptyState, ErrorNotice, ErrorState, Skeleton, SkeletonRows } from '../../components/ui/States';
import { assigneeText } from '../../features/approvals/ApprovalList';
import { CriterionDecisionPanel, DecisionPanel, ReassignDialog } from '../../features/approvals/DecisionPanel';
import { RequestKindChip, RequestStatusChip, requestKindText } from '../../features/approvals/meta';
import { StageReviewTarget } from '../../features/approvals/StageReviewTarget';
import { AcceptanceTarget, CollaborationSubmission, EngineeringTarget } from '../../features/approvals/TargetSnapshot';
import { ActorLabel, ModeChip, PhaseChip } from '../../features/collaboration/common';
import { VerdictTable } from '../../features/collaboration/NodeDetail';
import { Facts } from '../../features/assets/VersionSummary';
import { HashText, dynLabel } from '../../features/assets/meta';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export default function ApprovalDetailPage() {
  const capabilities = useCapabilities();
  const { t, fmt } = useI18n();
  const me = useUser();
  const { requestId = '' } = useParams();
  const res = useResource((signal) => getApprovalRequest(requestId, { signal }), [requestId]);
  const detail = res.data;
  const projectId = detail?.project_id;
  const project = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const [reassigning, setReassigning] = useState(false);
  // Collaboration acceptance (UI03): the node gives the pre-review and the actors.
  const nt = detail?.kind === 'task_acceptance' ? detail.node_task ?? null : null;
  const nodeTask = useResource((signal) => getNodeTask(nt!.run_id, nt!.node_id, { signal }), [nt?.run_id, nt?.node_id, detail?.status, detail?.submission_id], { enabled: !!nt });

  useShellProject(project.data);
  useCrumbs([
    { label: t('nav.approvals'), to: paths.approvals() },
    ...(project.data ? [{ label: project.data.code, mono: true }] : []),
    ...(detail ? [{ label: detail.title || requestKindText(t, detail.kind, detail.stage, isDirectEngineering(detail)) }] : []),
  ]);

  if (res.error && !detail) {
    if (isApiError(res.error) && res.error.status === 404)
      return (
        <div className="page">
          <EmptyState
            title={t('ap.notFound.title')}
            body={t('ap.notFound.body')}
            action={
              <Link className="btn" to={paths.approvals()}>
                <Inbox /> {t('ap.openCentre')}
              </Link>
            }
          />
        </div>
      );
    return (
      <div className="page">
        <ErrorState error={res.error} onRetry={res.reload} />
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="page">
        <Skeleton w="45%" h={26} />
        <SkeletonRows rows={6} cols={3} />
      </div>
    );
  }

  const caps = projectCapabilities(project.data);
  const canReassign = caps.manage && detail.status === 'pending' && capabilities.status === 'ready'
    && (detail.can_reassign ?? (capabilities.legacy && !nt));
  const direct = isDirectEngineering(detail);
  const kindText = requestKindText(t, detail.kind, detail.stage, direct);

  const meta = (
    <>
          <section className="panel" aria-labelledby="ap-meta-h">
            <div className="panel-h">
              <h2 id="ap-meta-h">{t('ap.meta.title')}</h2>
            </div>
            <div className="panel-b stack-sm">
              <Facts
                rows={[
                  [
                    t('ap.meta.assignee'),
                    <div className="row">
                      <span>{assigneeText(t, detail)}</span>
                      {canReassign && (
                        <button type="button" className="btn sm" onClick={() => setReassigning(true)}>
                          <UserCog /> {t('ap.reassign.open')}
                        </button>
                      )}
                    </div>,
                  ],
                  [t('ap.meta.targetHash'), <HashText hash={detail.target_hash} chars={16} />],
                  [t('ap.meta.policy'), detail.policy_version ? <span className="mono small">{detail.policy_version}</span> : <span className="faint">—</span>],
                  [t('ap.meta.createdBy'), <span>{detail.created_by_name ?? '—'}</span>],
                  [t('ap.meta.created'), fmt.dateTime(detail.created_at)],
                  ...(detail.due_at ? ([[t('ap.meta.due'), fmt.dateTime(detail.due_at)]] as [string, string][]) : []),
                  ...(detail.closed_at ? ([[t('ap.meta.closed'), fmt.dateTime(detail.closed_at)]] as [string, string][]) : []),
                ]}
              />
              <p className="small faint">{t('ap.meta.hashNote')}</p>
            </div>
          </section>

    </>
  );
  const history = (
    <>
          <section className="panel" aria-labelledby="ap-hist-h">
            <div className="panel-h">
              <History aria-hidden className="faint" />
              <h2 id="ap-hist-h">{t('ap.history.title')}</h2>
            </div>
            <div className="panel-b">
              {detail.decisions.length ? (
                <ol className="ap-history">
                  {detail.decisions.map((d) => (
                    <li key={d.id}>
                      <div className="row">
                        <RequestStatusChip status={d.decision} />
                        {d.actor ? <ActorLabel actor={d.actor} /> : <span className="strong">{d.actor_name ?? '—'}</span>}
                        <span className="faint small">{dynLabel(t, `role.${d.role}`, d.role)}</span>
                      </div>
                      <div className="small faint">
                        {fmt.dateTime(d.created_at)} · {t('ap.history.onHash')} <HashText hash={d.target_hash} copy={false} />
                      </div>
                      {d.comment && <p className="small ap-note">“{d.comment}”</p>}
                      {d.criterion_decisions && d.criterion_decisions.length > 0 && (
                        <details>
                          <summary className="small muted">{t('ap.cl.verdictsN', { n: d.criterion_decisions.length })}</summary>
                          <VerdictTable decisions={d.criterion_decisions} criteria={new Map((detail.target.submission?.snapshot?.criteria ?? []).map((c) => [c.id, c]))} />
                        </details>
                      )}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="faint small">{t('ap.history.none')}</p>
              )}
            </div>
          </section>
    </>
  );

  return (
    <div className="page ap-page">
      <header className="page-head">
        <div className="page-titles">
          <span className="eyebrow-sm">{kindText}</span>
          <h1 className="ap-title">{detail.title || kindText}</h1>
          <div className="row">
            <RequestStatusChip status={detail.status} />
            <RequestKindChip kind={detail.kind} stage={detail.stage} direct={direct} />
            {project.data && (
              <Link to={paths.project(project.data.id, 'approvals')} className="small mono">
                {project.data.code}
              </Link>
            )}
            {nt && <ModeChip mode={nt.execution_mode} />}
            {nt && <PhaseChip phase={nt.phase} />}
            {nt && (
              <Link to={paths.collaborationRun(nt.run_id, nt.node_id)} className="small">
                <Waypoints aria-hidden className="inline-ico" /> {t('ap.cl.openWorkbench')}
              </Link>
            )}
            {detail.task && (
              <Link to={paths.task(detail.task.id)} className="small" title={detail.task.title}>
                <ListChecks aria-hidden className="inline-ico" /> <span className="mono">{detail.task.key}</span>
                {!(detail.title || '').includes(detail.task.title) && <> {detail.task.title}</>}
              </Link>
            )}
          </div>
        </div>
        <div className="page-actions">
          <Link className="btn" to={paths.approvals()}>
            <Inbox /> {t('ap.openCentre')}
          </Link>
        </div>
      </header>

      {isCollaborationAcceptance(detail) ? (
        <>
          <div className="cl-acc-bar" role="group" aria-label={t('ap.cl.barLabel')}>
            <dl className="cl-kv">
              <dt>{t('ap.acc.submission')}</dt>
              <dd className="mono">#{detail.target.submission?.seq ?? '—'}</dd>
            </dl>
            <dl className="cl-kv">
              <dt>{t('ap.acc.snapshotHash')}</dt>
              <dd>
                <HashText hash={detail.target.submission?.snapshot_hash ?? detail.target_hash} chars={12} />
              </dd>
            </dl>
            <dl className="cl-kv">
              <dt>{t('ap.cl.assignedReviewer')}</dt>
              <dd>{nodeTask.data ? <ActorLabel actor={nodeTask.data.reviewer} /> : <span>{assigneeText(t, detail)}</span>}</dd>
            </dl>
            <span className="grow" />
            <span className="warn-text small row">
              <ShieldAlert className="inline-ico" aria-hidden /> {t('ap.cl.preNotFinal')}
            </span>
          </div>
          {detail.status === 'pending' && nodeTask.data?.pre_review && (
            <div className="callout warn" role="status">
              <ShieldAlert aria-hidden />
              <span>{t('ap.cl.prePassedStillHuman', { name: nodeTask.data.reviewer?.user?.name ?? assigneeText(t, detail) })}</span>
            </div>
          )}
          {nodeTask.error && !nodeTask.data ? <ErrorNotice error={nodeTask.error} onRetry={nodeTask.reload} /> : null}
          <div className="ap-layout cl-acc-layout">
            <div className="ap-main">
              <CollaborationSubmission detail={detail} />
            </div>
            <aside className="cl-acc-side stack">
              <CriterionDecisionPanel key={detail.id} detail={detail} meId={me.id} myRole={project.data?.my_role} nodeTask={nodeTask.data} onDetail={(d) => res.setData(d)} onReload={res.reload} />
              {history}
              {meta}
            </aside>
          </div>
        </>
      ) : (
      <div className="ap-layout">
        <div className="ap-main">
          {detail.kind === 'task_acceptance' ? (
            <AcceptanceTarget detail={detail} />
          ) : detail.kind === 'milestone_review' ? (
            <StageReviewTarget detail={detail} canWithdraw={caps.manage || detail.created_by === me.id} onChanged={res.reload} />
          ) : (
            <EngineeringTarget detail={detail} />
          )}
        </div>
        <aside className="ap-side stack">
          <DecisionPanel detail={detail} meId={me.id} myRole={project.data?.my_role} onDetail={(d) => res.setData(d)} onReload={res.reload} />

          {meta}
          {history}
        </aside>
      </div>
      )}

      {canReassign && reassigning && (
        <ReassignDialog
          detail={detail}
          onClose={() => setReassigning(false)}
          onDone={(d) => {
            res.setData(d);
            setReassigning(false);
          }}
        />
      )}
    </div>
  );
}
