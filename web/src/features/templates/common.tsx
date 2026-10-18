// Shared vocabulary of the template area (P3): version chips, run provenance,
// error-code mapping, input-rule checks. Mirrors server/app/api/workflows.py and
// services/templates.py; the server stays the authority for every rule here.

import { Archive, CircleCheck, FilePen, FlaskConical, History, Library, ShieldCheck, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { listMembers } from '../../api/endpoints';
import type { AssetVersion } from '../../api/pm';
import type {
  ApprovalStage,
  GraphError,
  InputQualityIssue,
  Project,
  RunSummary,
  TemplateInputRules,
  TemplateInputSpec,
  TemplatePolicy,
  TemplateVersion,
  TemplateVersionStatus,
  WorkflowGraph,
} from '../../api/types';
import { DEFAULT_TEMPLATE_POLICY } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { canInProject } from '../../auth/permissions';
import { useResource } from '../../hooks/useResource';
import { Chip } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n, useT, type TFunction } from '../../i18n/I18nProvider';
import { graphErrorText, nodeDisplayName, stageLabel } from '../workflow/labels';

export function dynT(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

/** Server error code → message key for template / run calls. */
export const TPL_ERRORS: ErrorOverrides = {
  template_invalid: 'tpl.err.template_invalid',
  gate_missing: 'tpl.err.gate_missing',
  no_changes: 'tpl.err.no_changes',
  version_conflict: 'tpl.err.version_conflict',
  template_version_deprecated: 'tpl.err.template_version_deprecated',
  template_not_published: 'tpl.err.template_not_published',
  draft_run_needs_exploration: 'tpl.err.draft_run_needs_exploration',
  gate_param_locked: 'tpl.err.gate_param_locked',
  input_quality: 'tpl.err.input_quality',
  invalid_graph: 'tpl.err.invalid_graph',
  forbidden: 'tpl.err.forbidden',
  part_required: 'error.part_required',
  task_cancelled: 'tpl.err.task_cancelled',
  workflow_archived: 'tpl.err.workflow_archived',
  project_not_active: 'tpl.err.project_not_active',
  project_archived: 'tpl.err.project_not_active',
  not_a_member: 'tpl.err.not_a_member',
  read_only_member: 'tpl.err.read_only_member',
  draft_with_version: 'tpl.err.draft_with_version',
  template_version_required: 'tpl.err.template_version_required',
  rerun_unsupported: 'tpl.err.rerun_unsupported',
  upgrade_wrong_template: 'tpl.err.upgrade_wrong_template',
  upgrade_not_newer: 'tpl.err.upgrade_not_newer',
  reason_required: 'tpl.deprecate.reasonRequired',
};

/**
 * May the current user create, edit, publish and deprecate templates in this project
 * (owner, or a member with the template_manager capability)? undefined while loading.
 * The server re-checks every call ("template" action).
 */
export function useTemplateAccess(project: Pick<Project, 'id' | 'my_role' | 'status'> | null | undefined): boolean | undefined {
  const me = useUser();
  const working = canInProject(project, 'workflow');
  const isOwner = canInProject(project, 'manage') && project?.my_role === 'owner';
  const members = useResource((signal) => listMembers(project!.id, { signal }), [project?.id], { enabled: !!project && working && !isOwner });
  if (!project) return undefined;
  if (!working) return false;
  if (isOwner) return true;
  if (!members.data) return members.error ? false : undefined;
  return !!members.data.find((m) => m.user.id === me.id)?.capabilities?.includes('template_manager');
}

// ─── version vocabulary ────────────────────────────────────────────────────

export function VersionNo({ n }: { n: number | null | undefined }) {
  return <span className="tpl-vno mono">v{n ?? '?'}</span>;
}

export function VersionStatusChip({ status }: { status: TemplateVersionStatus }) {
  const t = useT();
  return status === 'deprecated' ? (
    <Chip tone="pending" icon={<Archive aria-hidden />}>
      {t('tpl.status.deprecated')}
    </Chip>
  ) : (
    <Chip tone="ok" icon={<CircleCheck aria-hidden />}>
      {t('tpl.status.published')}
    </Chip>
  );
}

/** "Published v3" / "Not published" / "Draft changed" for a template row or header. */
export function PublishStateChips({ latest, draftDirty, publishedCount }: { latest: TemplateVersion | null | undefined; draftDirty?: boolean; publishedCount?: number }) {
  const t = useT();
  return (
    <span className="tpl-chips">
      {latest ? (
        <Chip tone={latest.status === 'deprecated' ? 'pending' : 'ok'} icon={<CircleCheck aria-hidden />} title={publishedCount ? t('tpl.publishedCount', { n: publishedCount }) : undefined}>
          {t('tpl.chip.published', { n: latest.version_no })}
        </Chip>
      ) : (
        <Chip tone="pending" icon={<FilePen aria-hidden />}>
          {t('tpl.chip.notPublished')}
        </Chip>
      )}
      {latest && draftDirty && (
        <Chip tone="warn" icon={<FilePen aria-hidden />} title={t('tpl.chip.draftChangedHint')}>
          {t('tpl.chip.draftChanged')}
        </Chip>
      )}
    </span>
  );
}

export function LibrarySourceChip({ name, adopted, upgrade }: { name: string; adopted: number | null; upgrade?: boolean }) {
  const t = useT();
  return (
    <span className="tpl-chips">
      <Chip tone="measure" icon={<Library aria-hidden />} title={t('tpl.library.sourceHint')}>
        {t('tpl.library.source', { name, n: adopted ?? '?' })}
      </Chip>
      {upgrade && (
        <Chip tone="accent" icon={<History aria-hidden />}>
          {t('tpl.library.upgradeAvailable')}
        </Chip>
      )}
    </span>
  );
}

// ─── run provenance ────────────────────────────────────────────────────────

export type RunTemplateKind = 'version' | 'draft' | 'legacy' | 'workspace';

export function runTemplateKind(r: Pick<RunSummary, 'origin' | 'template_version' | 'template_version_id' | 'from_draft'>): RunTemplateKind {
  if (r.origin === 'workspace') return 'workspace';
  if (r.template_version || r.template_version_id) return 'version';
  if (r.from_draft) return 'draft';
  return 'legacy';
}

/** Template version a run used: "Name v3", or draft test run / legacy run / AI modeling. */
export function RunTemplateRef({ run, name, link = true }: { run: RunSummary; name?: string; link?: boolean }) {
  const t = useT();
  const kind = runTemplateKind(run);
  if (kind === 'workspace') return <span className="faint">{t('wf.run.meta.workspace')}</span>;
  const label = run.template_version?.name ?? name ?? t('wf.runs.deletedWorkflow');
  if (kind === 'version') {
    const tv = run.template_version;
    const body = (
      <>
        <span className="ellip">{label}</span> <VersionNo n={tv?.version_no} />
        {tv?.status === 'deprecated' && <span className="faint small"> · {t('tpl.status.deprecated')}</span>}
      </>
    );
    return link && run.workflow_id ? (
      <Link className="tpl-ref" to={paths.workflow(run.workflow_id, 'versions')}>
        {body}
      </Link>
    ) : (
      <span className="tpl-ref">{body}</span>
    );
  }
  const tag =
    kind === 'draft' ? (
      <Chip tone="stale" icon={<FlaskConical aria-hidden />} title={t('tpl.run.draftHint')}>
        {t('tpl.run.draft')}
      </Chip>
    ) : (
      <Chip tone="pending" icon={<History aria-hidden />} title={t('tpl.run.legacyHint')}>
        {t('tpl.run.legacy')}
      </Chip>
    );
  return (
    <span className="tpl-ref">
      {run.workflow_id && link ? (
        <Link to={paths.workflow(run.workflow_id)} className="ellip">
          {label}
        </Link>
      ) : (
        <span className="ellip">{label}</span>
      )}
      {kind === 'legacy' && run.workflow_version ? <span className="mono faint"> #{run.workflow_version}</span> : null} {tag}
    </span>
  );
}

// ─── policy ────────────────────────────────────────────────────────────────

export function normalizePolicy(p: Partial<TemplatePolicy> | null | undefined): TemplatePolicy {
  return {
    mode: p?.mode === 'exploratory' ? 'exploratory' : 'standard',
    input_rules: { ...DEFAULT_TEMPLATE_POLICY.input_rules, ...(p?.input_rules ?? {}) },
  };
}

export const RULE_FLAGS = ['allow_estimated', 'allow_simulated', 'allow_mesh_reference', 'allow_failed_source', 'allow_unverified'] as const;
export type RuleFlag = (typeof RULE_FLAGS)[number];

/** One-line summary of what inputs a version accepts. */
export function PolicySummary({ policy }: { policy: Partial<TemplatePolicy> | null | undefined }) {
  const t = useT();
  const p = normalizePolicy(policy);
  if (p.mode === 'exploratory') {
    return (
      <span className="tpl-policy">
        <Chip tone="stale" icon={<FlaskConical aria-hidden />}>
          {t('tpl.policy.exploratory')}
        </Chip>
        <span className="faint small">{t('tpl.policy.exploratoryHint')}</span>
      </span>
    );
  }
  const allowed = RULE_FLAGS.filter((f) => p.input_rules[f]);
  return (
    <span className="tpl-policy">
      <Chip tone="ok" icon={<ShieldCheck aria-hidden />}>
        {t('tpl.policy.standard')}
      </Chip>
      <span className="faint small">
        {allowed.length ? t('tpl.policy.accepts', { list: allowed.map((f) => t(`tpl.rule.${f}.short` as MessageKey)).join(', ') }) : t('tpl.policy.acceptsNone')}
        {p.input_rules.require_approved_stage ? ` · ${t('tpl.policy.needsApproval', { stage: stageLabel(t, p.input_rules.require_approved_stage) })}` : ''}
      </span>
    </span>
  );
}

// ─── run inputs ────────────────────────────────────────────────────────────

/** Input node type → the node parameter that carries the bound value. */
export const INPUT_PARAM: Readonly<Record<string, string>> = { model_input: 'revision_id', file_input: 'file_version_id', text_input: 'text' };
export const INPUT_ASSET_TYPE: Readonly<Record<string, string>> = { model_input: 'part_model', file_input: 'document' };
export const INPUT_NODE_TYPES: ReadonlySet<string> = new Set(Object.keys(INPUT_PARAM));

export function inputAssetType(spec: Pick<TemplateInputSpec, 'node_type' | 'asset_type'>): string | null {
  return spec.asset_type ?? INPUT_ASSET_TYPE[spec.node_type] ?? null;
}

/** Inputs of a draft graph, the same shape the server derives on publish. */
export function draftInputs(graph: WorkflowGraph, policy: TemplatePolicy): TemplateInputSpec[] {
  return graph.nodes
    .filter((n) => INPUT_NODE_TYPES.has(n.type))
    .map((n) => ({
      node_id: n.id,
      node_type: n.type,
      port_type: n.type === 'model_input' ? 'model' : n.type === 'file_input' ? 'file' : 'text',
      asset_type: INPUT_ASSET_TYPE[n.type] ?? null,
      required: true,
      default_params: n.params ?? {},
      require_approved_stage: policy.input_rules.require_approved_stage,
    }));
}

/** Params a draft run may override: everything except input nodes and the review stage. */
export function draftOverridable(graph: WorkflowGraph, paramNames: (type: string) => string[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const n of graph.nodes) {
    if (INPUT_NODE_TYPES.has(n.type)) continue;
    const names = paramNames(n.type).filter((p) => !(n.type === 'human_review' && p === 'stage'));
    if (names.length) out[n.id] = names;
  }
  return out;
}

export type QualityProblem = 'not_ready' | 'estimated' | 'simulated' | 'mesh_reference' | 'from_failed_run' | 'unverified' | 'not_approved';

/** Local mirror of the server's input quality check (feedback only; the server decides). */
export function qualityProblems(v: Pick<AssetVersion, 'availability' | 'labels' | 'engineering'>, rules: TemplateInputRules, requireStage: ApprovalStage | null, exploratory: boolean): QualityProblem[] {
  if (exploratory) return [];
  const out: QualityProblem[] = [];
  if (v.availability !== 'ready') out.push('not_ready');
  const labels = new Set(v.labels ?? []);
  if (labels.has('estimated') && !rules.allow_estimated) out.push('estimated');
  if (labels.has('simulated') && !rules.allow_simulated) out.push('simulated');
  if (labels.has('mesh_reference') && !rules.allow_mesh_reference) out.push('mesh_reference');
  if (labels.has('from_failed_run') && !rules.allow_failed_source) out.push('from_failed_run');
  if (labels.has('unverified') && !rules.allow_unverified) out.push('unverified');
  const stage = requireStage ?? rules.require_approved_stage;
  if (stage && !(v.engineering && v.engineering.stage === stage && v.engineering.approved)) out.push('not_approved');
  return out;
}

export function qualityText(t: TFunction, code: string, fallback: string, stage?: string | null): string {
  return dynT(t, `tpl.q.${code}`, fallback, { stage: stage ? stageLabel(t, stage) : '' });
}

// ─── error details ─────────────────────────────────────────────────────────

export function inputQualityIssues(error: unknown): InputQualityIssue[] {
  if (!isApiError(error) || error.code !== 'input_quality') return [];
  const issues = (error.details as { issues?: unknown }).issues;
  return Array.isArray(issues) ? (issues as InputQualityIssue[]) : [];
}

export function graphErrorsOf(error: unknown): GraphError[] {
  if (!isApiError(error)) return [];
  if (!['template_invalid', 'invalid_graph', 'gate_missing'].includes(error.code)) return [];
  const errors = (error.details as { errors?: unknown }).errors;
  return Array.isArray(errors) ? (errors as GraphError[]) : [];
}

export function forbiddenActions(error: unknown): string[] {
  if (!isApiError(error) || error.code !== 'forbidden') return [];
  const a = (error.details as { actions?: unknown }).actions;
  return Array.isArray(a) ? (a as string[]) : [];
}

/** Text for a publish / graph problem, including the approval-gate rules. */
export function templateErrorText(t: TFunction, e: GraphError, graph?: WorkflowGraph): string {
  if (e.code === 'gate_missing') {
    // {node_id: the review, subject_node_id, required_stage: the review missing upstream}
    const subject = e.subject_node_id ? graph?.nodes.find((n) => n.id === e.subject_node_id) : undefined;
    const vars = { subject: subject ? nodeDisplayName(t, subject) : (e.subject_node_id ?? '') };
    if (e.required_stage === 'process') return t('tpl.gate.quote', vars);
    if (e.required_stage === 'design') return t('tpl.gate.process', vars);
    return e.message || t('tpl.err.gate_missing');
  }
  const type = e.node_id ? graph?.nodes.find((n) => n.id === e.node_id)?.type : undefined;
  return graphErrorText(t, e, type);
}

/** Bulleted list of graph / gate problems with node names. */
export function GraphIssueList({ errors, graph }: { errors: GraphError[]; graph?: WorkflowGraph }) {
  const { t } = useI18n();
  if (!errors.length) return null;
  const nameOf = (id: string) => {
    const n = graph?.nodes.find((x) => x.id === id);
    return n ? nodeDisplayName(t, n) : id;
  };
  return (
    <ul className="tpl-issues">
      {errors.map((e, i) => (
        <li key={i} className={e.code === 'gate_missing' ? 'gate' : undefined} title={e.message}>
          <TriangleAlert aria-hidden />
          <span>
            {e.node_id ? <span className="strong">{nameOf(e.node_id)}: </span> : null}
            {templateErrorText(t, e, graph)}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Small "label: value" definition list item. */
export function Kv({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
