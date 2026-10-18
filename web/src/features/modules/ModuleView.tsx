import { useFeature } from '../../api/capabilities';
// Process module version content (applicability, parameters, steps,
// validation, source plan) and its actions: request a process review, create
// a new version, and the "save as process module" entry used by the process
// tab and process-plan asset versions. Documentation only — nothing here runs.

import './modules.css';
import { Blocks, ExternalLink, Info, Send } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { getProcessModuleVersion, requestAssetVersionReview, type AssetVersion, type ProcessModuleContent } from '../../api/assets';
import { getProcessPlan } from '../../api/endpoints';
import type { ProcessPlan, UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { partOutputPath } from '../../app/versionLinks';
import { ACTION_ROLES } from '../../auth/permissions';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { notifyApprovalsChanged } from '../approvals/meta';
import { Facts } from '../assets/VersionSummary';
import { useMembers } from '../tasks/common';
import { ModuleDialog } from './ModuleDialog';

function Tags({ list }: { list: string[] | undefined }) {
  if (!list?.length) return <span className="faint">—</span>;
  return (
    <span className="tag-list">
      {list.map((x) => (
        <span key={x} className="tag">
          {x}
        </span>
      ))}
    </span>
  );
}

function SourcePlanLink({ planId, steps }: { planId: UUID; steps: number[] }) {
  const { t } = useI18n();
  const res = useResource((signal) => getProcessPlan(planId, { signal }), [planId]);
  const stepsText = steps.length ? t('mod.sourceSteps', { list: steps.map((i) => i + 1).join(', ') }) : '';
  if (res.data)
    return (
      <span>
        <Link to={partOutputPath(res.data.part_id, 'process', planId)} className="row">
          <ExternalLink aria-hidden className="inline-ico" /> {t('mod.openSourcePlan')}
        </Link>
        {stepsText && <span className="faint small"> · {stepsText}</span>}
      </span>
    );
  return (
    <span className="mono small" title={planId}>
      {planId.slice(0, 8)}
      {stepsText && <span className="faint"> · {stepsText}</span>}
    </span>
  );
}

/** Read-only content of one module version. */
export function ModuleContentView({ module }: { module: ProcessModuleContent }) {
  const { t, fmt } = useI18n();
  const app = module.applicability ?? ({} as ProcessModuleContent['applicability']);
  const size = app.size_range_mm ?? { max: [] };
  const mm = (v: number[] | null | undefined) => (v && v.length ? `${v.map((x) => fmt.number(x, { maximumFractionDigits: 2 })).join(' × ')} mm` : '—');
  const steps = module.steps ?? [];
  const params = module.parameters ?? [];
  const validation = module.validation ?? { scope: '', evidence: '', validated_plan_ids: [] };
  return (
    <div className="stack">
      <div className="callout pending slim" role="note">
        <Info aria-hidden />
        <span>{t('mod.docOnly')}</span>
      </div>
      {module.summary && <p className="mod-text">{module.summary}</p>}

      <div className="stack-sm">
        <h3 className="mod-h">{t('mod.applicability')}</h3>
        <Facts
          rows={[
            [t('mod.materials'), <Tags list={app.materials} />],
            [t('mod.sizeRange'), <span className="mono">{size.min && size.min.length ? `${mm(size.min)} – ${mm(size.max)}` : t('mod.upTo', { size: mm(size.max) })}</span>],
            [t('mod.equipment'), <Tags list={app.equipment} />],
            [t('mod.prerequisites'), app.prerequisites ? <span className="mod-text">{app.prerequisites}</span> : <span className="faint">—</span>],
            [t('mod.limitations'), app.limitations ? <span className="mod-text">{app.limitations}</span> : <span className="faint">—</span>],
          ]}
        />
      </div>

      <div className="stack-sm">
        <h3 className="mod-h">
          {t('mod.steps')} · {fmt.number(steps.length)}
        </h3>
        {steps.length ? (
          <ol className="mod-steps">
            {steps.map((s, i) => (
              <li key={i}>
                <span className="strong">{s.name}</span>
                <span className="small faint">
                  {[s.machine, typeof s.minutes === 'number' ? t('as.sum.minutes', { n: fmt.number(s.minutes, { maximumFractionDigits: 2 }) }) : ''].filter(Boolean).join(' · ') || '—'}
                </span>
                {s.description && <span className="small mod-step-desc">{s.description}</span>}
              </li>
            ))}
          </ol>
        ) : (
          <p className="faint small">—</p>
        )}
      </div>

      <div className="stack-sm">
        <h3 className="mod-h">
          {t('mod.parameters')} · {fmt.number(params.length)}
        </h3>
        {params.length ? (
          <div className="table-wrap flat">
            <table className="t dense mod-params">
              <thead>
                <tr>
                  <th>{t('mod.param.name')}</th>
                  <th>{t('mod.param.unit')}</th>
                  <th className="r">{t('mod.param.default')}</th>
                  <th className="r">{t('mod.param.min')}</th>
                  <th className="r">{t('mod.param.max')}</th>
                </tr>
              </thead>
              <tbody>
                {params.map((p, i) => (
                  <tr key={i}>
                    <td>{p.name}</td>
                    <td className="mono small">{p.unit || '—'}</td>
                    <td className="r mono">{p.default ?? '—'}</td>
                    <td className="r mono">{p.min ?? '—'}</td>
                    <td className="r mono">{p.max ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="faint small">{t('mod.noParams')}</p>
        )}
      </div>

      <div className="stack-sm">
        <h3 className="mod-h">{t('mod.validation')}</h3>
        <Facts
          rows={[
            [t('mod.validationScope'), validation.scope ? <span className="mod-text">{validation.scope}</span> : <span className="faint">—</span>],
            [t('mod.evidence'), validation.evidence ? <span className="mod-text">{validation.evidence}</span> : <span className="faint">—</span>],
            [
              t('mod.validatedPlans'),
              validation.validated_plan_ids?.length ? (
                <span className="mono small">{validation.validated_plan_ids.map((id) => id.slice(0, 8)).join(', ')}</span>
              ) : (
                <span className="faint">—</span>
              ),
            ],
            [t('mod.sourcePlan'), module.source_plan_id ? <SourcePlanLink planId={module.source_plan_id} steps={module.source_steps ?? []} /> : <span className="faint">{t('mod.noSourcePlan')}</span>],
          ]}
        />
      </div>
    </div>
  );
}

/** Loads and shows the content of a module version (asset page, approval detail). */
export function ModuleContentLoader({ versionId }: { versionId: UUID }) {
  const res = useResource((signal) => getProcessModuleVersion(versionId, { signal }), [versionId]);
  if (res.error && !res.data) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!res.data) return <SkeletonRows rows={4} cols={2} />;
  return <ModuleContentView module={res.data.module} />;
}

// ─── request review ─────────────────────────────────────────────────────────

function RequestReviewDialog({ version, onClose, onDone }: { version: AssetVersion; onClose: () => void; onDone: (requestId: string | null) => void }) {
  const { t } = useI18n();
  const members = useMembers(version.project_id);
  const eligible = (members.list ?? []).filter((m) => (ACTION_ROLES.process as readonly string[]).includes(m.role));
  const [assignee, setAssignee] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await requestAssetVersionReview(version.id, { assignee_id: assignee || null });
      notifyApprovalsChanged();
      onDone(r?.id ?? null);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('mod.review.title', { name: version.asset_name, label: version.label })}
      subtitle={t('mod.review.hint')}
      onClose={onClose}
      busy={busy}
      size="sm"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <Send /> {busy ? t('common.working') : t('mod.review.submit')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label={t('mod.review.assignee')} optional hint={t('mod.review.assigneeHint')}>
          <select className="select" value={assignee} onChange={(e) => setAssignee(e.target.value)} disabled={!members.list}>
            <option value="">{t('mod.review.anyReviewer')}</option>
            {eligible.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {t(`role.${m.role}`)}
              </option>
            ))}
          </select>
        </Field>
        <ErrorNotice
          error={error ?? members.error}
          overrides={{
            request_pending: 'mod.err.reviewPending',
            forbidden: 'mod.err.forbidden',
            not_reviewable: 'mod.err.notModule',
            assignee_cannot_decide: 'ap.err.assignee_cannot_decide',
            not_a_member: 'ap.err.not_a_member',
            project_not_active: 'mod.err.projectNotActive',
          }}
        />
      </div>
    </Dialog>
  );
}

/** Actions of a module version on the asset page: request review, new version. */
export function ModuleVersionActions({
  version,
  canProcess,
  pendingRequestId,
  onVersionsChanged,
}: {
  version: AssetVersion;
  canProcess: boolean;
  /** An open review request on this version: no second request. */
  pendingRequestId?: string | null;
  onVersionsChanged: (newVersionId?: string) => void;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<'review' | 'version' | null>(null);
  const content = useResource((signal) => getProcessModuleVersion(version.id, { signal }), [version.id], { enabled: dialog === 'version' });
  const enabled = useFeature('process_modules');
  if (!enabled || !canProcess) return null;
  return (
    <div className="row">
      {version.engineering?.approved || pendingRequestId ? null : (
        <button type="button" className="btn sm" onClick={() => setDialog('review')} disabled={version.availability !== 'ready'}>
          <Send /> {t('mod.review.open')}
        </button>
      )}
      <button type="button" className="btn sm" onClick={() => setDialog('version')}>
        <Blocks /> {t('mod.newVersion')}
      </button>
      {dialog === 'review' && (
        <RequestReviewDialog
          version={version}
          onClose={() => setDialog(null)}
          onDone={(rid) => {
            setDialog(null);
            toast(t('mod.review.toast'), rid ? { action: { label: t('mod.review.openRequest'), onClick: () => navigate(paths.approvalRequest(rid)) } } : undefined);
            onVersionsChanged();
          }}
        />
      )}
      {dialog === 'version' &&
        (content.data ? (
          <ModuleDialog
            projectId={version.project_id}
            base={{ assetId: version.asset_id, module: content.data.module, labels: version.labels }}
            onClose={() => setDialog(null)}
            onCreated={(v) => {
              setDialog(null);
              toast(t('mod.versionCreated', { label: v?.label ?? '' }));
              onVersionsChanged(v?.id);
            }}
          />
        ) : (
          <Dialog title={t('mod.newVersion')} onClose={() => setDialog(null)} size="sm">
            {content.error ? <ErrorNotice error={content.error} onRetry={content.reload} /> : <Skeleton w="60%" />}
          </Dialog>
        ))}
    </div>
  );
}

/**
 * "Save as process module" for one process plan. Pass the plan when it is at
 * hand (process tab) or its id (process-plan asset version); the plan is
 * loaded on demand.
 */
export function SaveAsModuleButton({
  projectId,
  plan,
  planId,
  label,
  labels,
  size = 'sm',
}: {
  projectId: UUID;
  plan?: ProcessPlan | null;
  planId?: UUID;
  label?: string;
  labels?: string[];
  size?: '' | 'sm';
}) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const id = plan?.id ?? planId ?? '';
  const loaded = useResource((signal) => getProcessPlan(id, { signal }), [id], { enabled: open && !plan && !!id });
  const thePlan = plan ?? loaded.data ?? null;
  const enabled = useFeature('process_modules');
  if (!enabled) return null;
  return (
    <>
      <button type="button" className={`btn ${size}`} onClick={() => setOpen(true)} disabled={!id}>
        <Blocks /> {t('mod.saveAs')}
      </button>
      {open &&
        (thePlan ? (
          <ModuleDialog
            projectId={projectId}
            source={{ plan: thePlan, label, labels }}
            onClose={() => setOpen(false)}
            onCreated={(v) => {
              setOpen(false);
              toast(t('mod.created'), v ? { action: { label: t('mod.openModule'), onClick: () => navigate(paths.asset(v.asset_id, v.id)) } } : undefined);
            }}
          />
        ) : (
          <Dialog title={t('mod.saveAs')} onClose={() => setOpen(false)} size="sm">
            {loaded.error ? <ErrorNotice error={loaded.error} onRetry={loaded.reload} /> : <Skeleton w="60%" />}
          </Dialog>
        ))}
    </>
  );
}

/** "New process module" without a source plan (asset catalogue). */
export function NewModuleButton({ projectId }: { projectId: UUID }) {
  const enabled = useFeature('process_modules');
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  if (!enabled) return null;
  return (
    <>
      <button type="button" className="btn sm" onClick={() => setOpen(true)}>
        <Blocks /> {t('mod.new')}
      </button>
      {open && (
        <ModuleDialog
          projectId={projectId}
          onClose={() => setOpen(false)}
          onCreated={(v) => {
            setOpen(false);
            toast(t('mod.created'));
            if (v) navigate(paths.asset(v.asset_id, v.id));
          }}
        />
      )}
    </>
  );
}
