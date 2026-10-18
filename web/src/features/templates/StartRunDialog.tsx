import { useFeature } from '../../api/capabilities';
// The one "start a run" dialog (design §5.2, contract §4): used from the task
// page, the template page and the asset page.
//
// 1. Context: linked task, or independent exploration — always an explicit choice
//    (fixed when opened from a task).
// 2. Template and published version (vN, publish date, deprecated state). A draft
//    test run is offered only on the template page to template managers, and only
//    for exploration.
// 3. Inputs from the version's input_schema (asset version pickers filtered by
//    asset type, prefilled from the asset page), parameters (review stages are
//    locked), title and purpose.
// 4. Server refusals are shown in place, including each 422 input_quality reason.

import { Compass,FlaskConical,Info,ListChecks,Play,TriangleAlert } from 'lucide-react';
import { useEffect,useMemo,useState } from 'react';
import type { AssetVersion as CatalogueVersion } from '../../api/assets';
import { getTemplateVersion,listParts,listTemplateVersions,listWorkflowNodeTypes,listWorkflows,startWorkflowRun } from '../../api/endpoints';
import { listTasks,startTaskRun,type AssetVersion } from '../../api/pm';
import type { TemplateInputSchema,TemplatePolicy,TemplateVersion,UUID,Workflow,WorkflowGraph } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { actionLabel,nodeDisplayName } from '../workflow/labels';
import { useLookups,useRevision } from '../workflow/lookups';
import { defMap,paramSpecs,PART_NODE_TYPES } from '../workflow/model';
import '../workflow/workflow.css';
import {
draftInputs,
draftOverridable,
forbiddenActions,
graphErrorsOf,
GraphIssueList,
inputAssetType,
inputQualityIssues,
normalizePolicy,
PolicySummary,
qualityText,
TPL_ERRORS,
} from './common';
import { buildRunParams,missingInputs,ParamOverridesForm,RunInputsForm,type InputValues,type ParamOverrides } from './RunInputs';
import './templates.css';

export interface StartRunTask {
  id: UUID;
  key: string;
  title: string;
  part_id?: UUID | null;
}

export interface StartRunDialogProps {
  projectId: UUID;
  /** Fixed task context (task page). */
  task?: StartRunTask;
  /** Fixed template (template page / canvas). */
  workflow?: Workflow;
  /** Preselect this published version. */
  versionId?: UUID;
  /** Offer a draft test run (template page, template managers). */
  allowDraft?: boolean;
  /** Prefill the first input of this asset type (asset page "Run with this version"). */
  prefill?: AssetVersion | CatalogueVersion;
  /** Preselect the run's part (part workspace "Start a run"). */
  partId?: UUID;
  onClose: () => void;
  onStarted: (run: { id: UUID }) => void;
}

type Context = 'task' | 'explore' | null;
const DRAFT = 'draft';

/** Versions newest first; the default is the newest one still published. */
function defaultVersion(versions: TemplateVersion[]): TemplateVersion | undefined {
  return versions.find((v) => v.status === 'published');
}

/** Two big radio cards: linked task / independent exploration. */
function ContextChoice({ value, onChange, disabled }: { value: Context; onChange: (c: Context) => void; disabled?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="tpl-choice" role="radiogroup" aria-label={t('tpl.start.context')} aria-required="true">
      <button type="button" role="radio" aria-checked={value === 'task'} disabled={disabled} className={`wf-tpl-card ${value === 'task' ? 'selected' : ''}`} onClick={() => onChange('task')}>
        <span className="wf-tpl-ico">
          <ListChecks aria-hidden />
        </span>
        <span className="wf-tpl-name">{t('tpl.start.ctxTask')}</span>
        <span className="wf-tpl-desc">{t('tpl.start.ctxTaskHint')}</span>
      </button>
      <button type="button" role="radio" aria-checked={value === 'explore'} disabled={disabled} className={`wf-tpl-card ${value === 'explore' ? 'selected' : ''}`} onClick={() => onChange('explore')}>
        <span className="wf-tpl-ico">
          <Compass aria-hidden />
        </span>
        <span className="wf-tpl-name">{t('tpl.start.ctxExplore')}</span>
        <span className="wf-tpl-desc">{t('tpl.start.ctxExploreHint')}</span>
      </button>
    </div>
  );
}

export function StartRunDialog({ projectId, task, workflow: fixedWorkflow, versionId, allowDraft = false, prefill: prefillIn, partId: partPreset, onClose, onStarted }: StartRunDialogProps) {
  const { t, fmt } = useI18n();
  // Both catalogue types are the server's version_out; the pickers use the api/pm shape.
  const prefill = prefillIn as AssetVersion | undefined;
  const tasksEnabled = useFeature('tasks');
  const [chosenContext, setContext] = useState<Context>(task ? 'task' : null);
  const context: Context = tasksEnabled ? chosenContext : 'explore';
  const [taskId, setTaskId] = useState<UUID>(task?.id ?? '');
  const [workflowId, setWorkflowId] = useState<UUID>(fixedWorkflow?.id ?? '');
  const [choice, setChoice] = useState<string>('');
  const [partId, setPartId] = useState<UUID>(partPreset ?? task?.part_id ?? prefill?.target.part_id ?? '');
  const [values, setValues] = useState<InputValues>({});
  const [overrides, setOverrides] = useState<ParamOverrides>({});
  const [title, setTitle] = useState('');
  const [purpose, setPurpose] = useState('');
  // W7: chosen for this run only; standard preselected every time the dialog opens.
  // S5: null = inherited from the spec version the graph pins, else the project default (standard unless the project's
  // "default Turbo" is on); a click is always explicit and sent.
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const workflows = useResource((signal) => listWorkflows(projectId, {}, { signal }), [projectId], { enabled: !fixedWorkflow });
  const tasks = useResource((signal) => listTasks(projectId, { status: 'todo,in_progress,in_review' }, { signal }), [projectId], { enabled: tasksEnabled && !task && context === 'task' });
  const parts = useResource((signal) => listParts(projectId, {}, { signal }), [projectId]);
  const nodeTypes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(nodeTypes.data ?? []), [nodeTypes.data]);
  const lookups = useLookups(projectId);

  const wf: Workflow | undefined = fixedWorkflow ?? (workflows.data ?? []).find((w) => w.id === workflowId);
  const versions = useResource((signal) => listTemplateVersions(workflowId, { signal }), [workflowId], { enabled: !!workflowId });
  const draftAllowed = allowDraft && !!fixedWorkflow && (fixedWorkflow.can_manage ?? true);
  const isDraft = choice === DRAFT;
  const selectedVersion = (versions.data ?? []).find((v) => v.id === choice);
  const detail = useResource((signal) => getTemplateVersion(choice, { signal }), [choice], { enabled: !!choice && !isDraft });

  // Pick the default version once the list arrives (or when the template changes).
  useEffect(() => {
    if (!versions.data || choice) return;
    const wanted = versionId ? versions.data.find((v) => v.id === versionId && v.status === 'published') : undefined;
    const d = wanted ?? defaultVersion(versions.data);
    if (d) setChoice(d.id);
    else if (draftAllowed && context !== 'task') setChoice(DRAFT);
  }, [versions.data, choice, versionId, draftAllowed, context]);

  // A draft test run is exploration only.
  useEffect(() => {
    if (context === 'task' && isDraft) setChoice(versions.data ? defaultVersion(versions.data)?.id ?? '' : '');
  }, [context, isDraft, versions.data]);

  // What the run will execute: the version (graph + schema + policy), or the draft.
  const resolved = useMemo((): { graph: WorkflowGraph; schema: TemplateInputSchema; policy: TemplatePolicy } | null => {
    if (isDraft && wf) {
      const policy = normalizePolicy(wf.policy);
      return {
        graph: wf.graph,
        policy,
        schema: {
          inputs: draftInputs(wf.graph, policy),
          overridable: draftOverridable(wf.graph, (type) => {
            const d = defs.get(type);
            return d ? paramSpecs(d).map((s) => s.name) : [];
          }),
        },
      };
    }
    if (detail.data && detail.data.id === choice) {
      return { graph: detail.data.graph, schema: detail.data.input_schema ?? { inputs: [], overridable: {} }, policy: normalizePolicy(detail.data.policy) };
    }
    return null;
  }, [isDraft, wf, defs, detail.data, choice]);

  // Inputs belong to one version: start over when it changes, then prefill once
  // from the asset page into the first input of the same asset type.
  const [prefilledFor, setPrefilledFor] = useState<string | null>(null);
  useEffect(() => {
    setValues({});
    setOverrides({});
  }, [choice]);
  useEffect(() => {
    if (!resolved || !prefill || prefilledFor === choice) return;
    setPrefilledFor(choice);
    const match = resolved.schema.inputs.find((s) => inputAssetType(s) === prefill.asset_type);
    if (match) setValues((x) => (x[match.node_id]?.version ? x : { ...x, [match.node_id]: { version: prefill } }));
  }, [resolved, prefill, choice, prefilledFor]);

  const graph = resolved?.graph;
  const needsPart = !!graph && graph.nodes.some((n) => PART_NODE_TYPES.has(n.type));
  const liveParts = useMemo(() => (parts.data ?? []).filter((p) => !p.archived_at), [parts.data]);
  const chosenTask = task ?? (tasks.data ?? []).find((x) => x.id === taskId);

  // Part follows the chosen model version / task when nothing is set yet.
  const modelPart = Object.values(values).find((v) => v.version?.asset_type === 'part_model')?.version?.target.part_id ?? null;
  const defaultModel = resolved?.schema.inputs.find((s) => s.node_type === 'model_input' && !values[s.node_id]?.version);
  const defaultRev = useRevision(defaultModel ? defaultModel.default_params?.revision_id : undefined);
  const inputPart = modelPart ?? defaultRev?.part_id ?? null;
  useEffect(() => {
    if (partId || !parts.data) return;
    const fromTask = chosenTask && 'part_id' in chosenTask ? chosenTask.part_id : null;
    const next = inputPart ?? fromTask ?? (liveParts.length === 1 ? liveParts[0].id : null);
    if (next && liveParts.some((p) => p.id === next)) setPartId(next);
  }, [inputPart, chosenTask, parts.data, partId, liveParts]);
  const partMismatch = !!inputPart && !!partId && inputPart !== partId;

  const missing = resolved ? missingInputs(resolved.schema.inputs, values) : [];
  const deprecated = selectedVersion?.status === 'deprecated';
  const prefillUnmatched = !!prefill && !!resolved && !resolved.schema.inputs.some((s) => inputAssetType(s) === prefill.asset_type);
  const blockers: string[] = [];
  if (!context) blockers.push(t('tpl.start.needContext'));
  if (context === 'task' && !chosenTask) blockers.push(t('tpl.start.needTask'));
  if (!wf) blockers.push(t('tpl.start.needTemplate'));
  else if (!choice) blockers.push(t('tpl.start.needVersion'));
  if (deprecated) blockers.push(t('tpl.err.template_version_deprecated'));
  if (needsPart && !partId) blockers.push(t('tpl.start.needPart'));
  if (partMismatch) blockers.push(t('tpl.start.partMismatch'));
  if (missing.length) blockers.push(t('tpl.start.needInputs', { n: missing.length }));
  const blocked = blockers.length > 0 || !resolved;
  // S5: a run that names no policy continues from the one spec version its graph pins (the server inherits it).

  const start = async () => {
    setTouched(true);
    if (blocked || !wf || !resolved) return;
    setBusy(true);
    setError(null);
    const params = buildRunParams(resolved.schema.inputs, values, overrides);
    try {
      let run: { id: UUID };
      if (context === 'task' && chosenTask) {
        run = await startTaskRun(chosenTask.id, {
          workflow_id: wf.id,
          template_version_id: choice,
          part_id: partId || null,
          params,
          purpose: purpose.trim(),
          title: title.trim(),

        });
      } else {
        run = await startWorkflowRun(wf.id, {
          ...(isDraft ? { draft: true } : { template_version_id: choice }),
          part_id: partId || null,
          params,
          purpose: purpose.trim(),
          title: title.trim(),

        });
      }
      onStarted(run);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const issues = inputQualityIssues(error);
  const graphErrors = graphErrorsOf(error);
  const forbidden = forbiddenActions(error);
  const nodeName = (id: string) => {
    const n = graph?.nodes.find((x) => x.id === id);
    return n ? nodeDisplayName(t, n) : id;
  };
  const liveWorkflows = (workflows.data ?? []).filter((w) => !w.archived_at);
  const publishedVersions = versions.data ?? [];

  return (
    <Dialog
      title={task ? t('tpl.start.titleTask', { key: task.key }) : t('tpl.start.title')}
      subtitle={fixedWorkflow ? fixedWorkflow.name : prefill ? t('tpl.start.subtitleAsset', { name: prefill.asset_name, label: prefill.label }) : t('tpl.start.subtitle')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void start()}
      footer={
        <>
          {touched && blockers.length > 0 && <span className="tpl-foot-note small">{blockers[0]}</span>}
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || (touched && blocked)}>
            <Play /> {busy ? t('wf.runDialog.starting') : isDraft ? t('tpl.start.submitDraft') : t('tpl.start.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        {/* 1. context */}
        <section className="stack-sm" aria-labelledby="tpl-start-ctx">
          <h3 id="tpl-start-ctx" className="tpl-step">
            <span className="tpl-step-n">1</span> {t('tpl.start.context')}
          </h3>
          {tasksEnabled && task ? (
            <p className="tpl-fixed">
              <ListChecks aria-hidden className="inline-ico" /> <span className="mono">{task.key}</span> · {task.title}
            </p>
          ) : (
            <>
              {tasksEnabled && <ContextChoice value={context} onChange={setContext} disabled={busy} />}
              {touched && !context && <span className="field-error">{t('tpl.start.needContext')}</span>}
              {context === 'task' && (
                <Field label={t('tpl.start.task')} error={touched && !chosenTask ? t('tpl.start.needTask') : null}>
                  <select className="select" value={taskId} onChange={(e) => setTaskId(e.target.value)} disabled={!tasks.data}>
                    <option value="">{tasks.data ? (tasks.data.length ? t('tpl.start.chooseTask') : t('tpl.start.noTasks')) : t('common.loading')}</option>
                    {(tasks.data ?? []).map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.key} · {x.title}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              {context === 'explore' && <p className="faint small">{t('tpl.start.exploreNote')}</p>}
            </>
          )}
          {tasks.error ? <ErrorNotice error={tasks.error} onRetry={tasks.reload} /> : null}
        </section>

        {/* 2. template and version */}
        <section className="stack-sm" aria-labelledby="tpl-start-ver">
          <h3 id="tpl-start-ver" className="tpl-step">
            <span className="tpl-step-n">2</span> {t('tpl.start.template')}
          </h3>
          <div className="form-grid">
            {!fixedWorkflow && (
              <Field label={t('tpl.start.templateField')} error={touched && !wf ? t('tpl.start.needTemplate') : null}>
                <select
                  className="select"
                  value={workflowId}
                  onChange={(e) => {
                    setWorkflowId(e.target.value);
                    setChoice('');
                  }}
                  disabled={!workflows.data}
                >
                  <option value="">{workflows.data ? (liveWorkflows.length ? t('tpl.start.chooseTemplate') : t('tpl.start.noTemplates')) : t('common.loading')}</option>
                  {liveWorkflows.map((w) => {
                    const pub = w.latest_published;
                    const unpublished = w.latest_published === null || (w.published_count ?? 1) === 0;
                    return (
                      <option key={w.id} value={w.id} disabled={unpublished}>
                        {w.name}
                        {pub ? ` · v${pub.version_no}` : ''}
                        {unpublished ? ` (${t('tpl.chip.notPublished')})` : ''}
                      </option>
                    );
                  })}
                </select>
              </Field>
            )}
            <Field
              label={t('tpl.start.version')}
              className={fixedWorkflow ? 'span-2' : undefined}
              hint={selectedVersion ? t('tpl.start.versionHint', { time: fmt.dateTime(selectedVersion.published_at) }) : isDraft ? t('tpl.start.draftHint') : undefined}
              error={deprecated ? t('tpl.err.template_version_deprecated') : touched && wf && !choice ? t('tpl.start.needVersion') : null}
            >
              <select className="select" value={choice} onChange={(e) => setChoice(e.target.value)} disabled={!wf || !versions.data}>
                <option value="">{!wf ? t('tpl.start.chooseTemplateFirst') : versions.data ? (publishedVersions.length ? t('tpl.start.chooseVersion') : t('tpl.start.noVersions')) : t('common.loading')}</option>
                {publishedVersions.map((v) => (
                  <option key={v.id} value={v.id} disabled={v.status === 'deprecated'}>
                    v{v.version_no} · {fmt.date(v.published_at)}
                    {v.status === 'deprecated' ? ` · ${t('tpl.status.deprecated')}` : ''}
                    {v.notes ? ` · ${v.notes.slice(0, 40)}` : ''}
                  </option>
                ))}
                {draftAllowed && (
                  <option value={DRAFT} disabled={context === 'task'}>
                    {t('tpl.start.draftOption', { n: fixedWorkflow?.version ?? '?' })}
                    {context === 'task' ? ` (${t('tpl.start.draftExploreOnly')})` : ''}
                  </option>
                )}
              </select>
            </Field>
          </div>
          {isDraft && (
            <div className="callout warn slim">
              <FlaskConical aria-hidden />
              <span>{t('tpl.start.draftWarn')}</span>
            </div>
          )}
          {resolved && <PolicySummary policy={resolved.policy} />}
          {workflows.error ? <ErrorNotice error={workflows.error} onRetry={workflows.reload} /> : null}
          {versions.error ? <ErrorNotice error={versions.error} onRetry={versions.reload} /> : null}
          {detail.error ? <ErrorNotice error={detail.error} onRetry={detail.reload} /> : null}
        </section>

        {/* 3. inputs and parameters */}
        {resolved && graph && (
          <section className="stack-sm" aria-labelledby="tpl-start-in">
            <h3 id="tpl-start-in" className="tpl-step">
              <span className="tpl-step-n">3</span> {t('tpl.start.inputs')}
            </h3>
            {prefillUnmatched && (
              <div className="callout warn slim">
                <TriangleAlert aria-hidden />
                <span>{t('tpl.start.prefillUnmatched', { label: `${prefill?.asset_name} ${prefill?.label}` })}</span>
              </div>
            )}
            <RunInputsForm
              projectId={projectId}
              inputs={resolved.schema.inputs}
              graph={graph}
              values={values}
              onChange={(nodeId, v) => setValues((x) => ({ ...x, [nodeId]: v }))}
              rules={resolved.policy.input_rules}
              exploratory={resolved.policy.mode === 'exploratory' || isDraft}
              lookups={lookups}
              showMissing={touched}
            />
            <div className="form-grid">
              <Field
                label={t('wf.runDialog.part')}
                optional={!needsPart}
                hint={needsPart ? t('wf.runDialog.partHint') : t('wf.runDialog.partOptional')}
                error={partMismatch ? t('tpl.start.partMismatch') : touched && needsPart && !partId ? t('tpl.start.needPart') : null}
              >
                <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={!parts.data}>
                  <option value="">{parts.data ? (liveParts.length ? t('wf.picker.choosePart') : t('wf.runDialog.noParts')) : t('common.loading')}</option>
                  {liveParts.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.number} · {p.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('wf.runDialog.runTitle')} optional>
                <input className="input" value={title} maxLength={200} placeholder={wf?.name} onChange={(e) => setTitle(e.target.value)} />
              </Field>
              <Field label={t('tpl.start.purpose')} optional hint={t('tpl.start.purposeHint')} className="span-2">
                <LimitedTextarea className="input" rows={2} limit="description" value={purpose} onChange={(e) => setPurpose(e.target.value)} />
              </Field>
            </div>
            <ParamOverridesForm projectId={projectId} graph={graph} overridable={resolved.schema.overridable ?? {}} defs={defs} lookups={lookups} overrides={overrides} onChange={setOverrides} />
          </section>
        )}
        {!!choice && !resolved && !detail.error && <p className="faint small">{t('common.loading')}</p>}

        <></>

        {context === 'task' && (
          <div className="callout info slim">
            <Info aria-hidden />
            <span>{t('tk.run.notCompletion')}</span>
          </div>
        )}

        {/* server refusals */}
        {error ? <ErrorNotice error={error} overrides={TPL_ERRORS} /> : null}
        {issues.length > 0 && (
          <ul className="tpl-issues" aria-label={t('tpl.input.qualityTitle')}>
            {issues.map((x, i) => (
              <li key={i} title={x.message}>
                <TriangleAlert aria-hidden />
                <span>
                  <span className="strong">{nodeName(x.node_id)}</span>
                  {x.version_label ? <span className="mono"> {x.version_label}</span> : null}: {qualityText(t, x.code, x.message)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {graphErrors.length > 0 && <GraphIssueList errors={graphErrors} graph={graph} />}
        {forbidden.length > 0 && <p className="small muted">{t('wf.runDialog.forbiddenActions', { actions: forbidden.map((a) => actionLabel(t, a)).join(', ') })}</p>}
      </div>
    </Dialog>
  );
}
