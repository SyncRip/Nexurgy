// Rerun (contract §2.5): a new run with rerun_of = this run and the same task.
// Version defaults to the original one; a deprecated or missing (legacy) version
// must be replaced explicitly (or a draft test run, for exploration runs and
// template managers). Inputs default to the original run's frozen values.

import { Info,RotateCw,TriangleAlert } from 'lucide-react';
import { useEffect,useMemo,useState } from 'react';
import { getTemplateVersion,getWorkflow,listTemplateVersions,listWorkflowNodeTypes,rerunRun } from '../../api/endpoints';
import type { TemplateInputSchema,TemplatePolicy,UUID,WorkflowGraph,WorkflowRun } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { nodeDisplayName } from '../workflow/labels';
import { useLookups } from '../workflow/lookups';
import { defMap,paramSpecs } from '../workflow/model';
import { draftInputs,draftOverridable,graphErrorsOf,GraphIssueList,INPUT_NODE_TYPES,inputQualityIssues,normalizePolicy,qualityText,TPL_ERRORS } from './common';
import { buildRunParams,missingInputs,ParamOverridesForm,RunInputsForm,type InputValues,type ParamOverrides } from './RunInputs';

const DRAFT = 'draft';

export function RerunDialog({ run, onClose, onStarted }: { run: WorkflowRun; onClose: () => void; onStarted: (run: { id: UUID }) => void }) {
  const { t, fmt } = useI18n();
  const wfId = run.workflow_id;
  const wf = useResource((signal) => getWorkflow(wfId!, { signal }), [wfId], { enabled: !!wfId });
  const versions = useResource((signal) => listTemplateVersions(wfId!, { signal }), [wfId], { enabled: !!wfId });
  const nodeTypes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const defs = useMemo(() => defMap(nodeTypes.data ?? []), [nodeTypes.data]);
  const lookups = useLookups(run.project_id);
  const original = run.template_version ?? null;
  const [choice, setChoice] = useState<string>(original && original.status !== 'deprecated' ? original.id : '');
  const [values, setValues] = useState<InputValues>({});
  const [overrides, setOverrides] = useState<ParamOverrides>({});
  const [title, setTitle] = useState(run.title);
  const [purpose, setPurpose] = useState(run.purpose ?? '');
  // S5: null = inherited from the run it repeats (shown with its source before anything is sent); a click is explicit.
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const isDraft = choice === DRAFT;
  const draftAllowed = !!wf.data?.can_manage && !run.task_id && !run.task;
  const detail = useResource((signal) => getTemplateVersion(choice, { signal }), [choice], { enabled: !!choice && !isDraft });

  useEffect(() => {
    setValues({});
    setOverrides({});
  }, [choice]);

  // Original frozen params of the input nodes: what the rerun uses unless changed.
  const frozen = useMemo(() => {
    const out: Record<string, Record<string, unknown>> = {};
    for (const n of run.graph.nodes) if (INPUT_NODE_TYPES.has(n.type)) out[n.id] = n.params ?? {};
    return out;
  }, [run.graph]);

  const resolved = useMemo((): { graph: WorkflowGraph; schema: TemplateInputSchema; policy: TemplatePolicy } | null => {
    if (isDraft && wf.data) {
      const policy = normalizePolicy(wf.data.policy);
      return {
        graph: wf.data.graph,
        policy,
        schema: {
          inputs: draftInputs(wf.data.graph, policy),
          overridable: draftOverridable(wf.data.graph, (type) => {
            const d = defs.get(type);
            return d ? paramSpecs(d).map((s) => s.name) : [];
          }),
        },
      };
    }
    if (detail.data && detail.data.id === choice) return { graph: detail.data.graph, schema: detail.data.input_schema ?? { inputs: [], overridable: {} }, policy: normalizePolicy(detail.data.policy) };
    return null;
  }, [isDraft, wf.data, defs, detail.data, choice]);

  const selected = (versions.data ?? []).find((v) => v.id === choice);
  const missing = resolved ? missingInputs(resolved.schema.inputs, values, frozen) : [];
  const blocked = !choice || !resolved || selected?.status === 'deprecated' || missing.length > 0;

  const submit = async () => {
    setTouched(true);
    if (blocked || !resolved) return;
    setBusy(true);
    setError(null);
    try {
      const params = buildRunParams(resolved.schema.inputs, values, overrides, frozen);
      onStarted(
        await rerunRun(run.id, {
          ...(isDraft ? { draft: true } : { template_version_id: choice }),
          params,
          title: title.trim(),
          purpose: purpose.trim(),

        }),
      );
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const issues = inputQualityIssues(error);
  const graphErrors = graphErrorsOf(error);
  const nodeName = (id: string) => {
    const n = (resolved?.graph ?? run.graph).nodes.find((x) => x.id === id);
    return n ? nodeDisplayName(t, n) : id;
  };

  return (
    <Dialog
      title={t('tpl.rerun.title')}
      subtitle={run.title || t('wf.run.untitled')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || (touched && blocked)}>
            <RotateCw /> {busy ? t('wf.runDialog.starting') : t('tpl.rerun.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim">
          <Info aria-hidden />
          <span>{run.task ? t('tpl.rerun.explainTask', { key: run.task.key }) : t('tpl.rerun.explain')}</span>
        </div>
        {!wfId ? (
          <p className="faint">{t('tpl.rerun.noTemplate')}</p>
        ) : (
          <>
            {original?.status === 'deprecated' && (
              <div className="callout warn slim">
                <TriangleAlert aria-hidden />
                <span>{t('tpl.rerun.deprecated', { n: original.version_no })}</span>
              </div>
            )}
            {!original && (
              <div className="callout warn slim">
                <TriangleAlert aria-hidden />
                <span>{t('tpl.rerun.legacy')}</span>
              </div>
            )}
            <Field label={t('tpl.start.version')} error={touched && !choice ? t('tpl.start.needVersion') : selected?.status === 'deprecated' ? t('tpl.err.template_version_deprecated') : null}>
              <select className="select" value={choice} onChange={(e) => setChoice(e.target.value)} disabled={!versions.data}>
                <option value="">{versions.data ? t('tpl.start.chooseVersion') : t('common.loading')}</option>
                {(versions.data ?? []).map((v) => (
                  <option key={v.id} value={v.id} disabled={v.status === 'deprecated'}>
                    v{v.version_no} · {fmt.date(v.published_at)}
                    {v.id === original?.id ? ` · ${t('tpl.rerun.original')}` : ''}
                    {v.status === 'deprecated' ? ` · ${t('tpl.status.deprecated')}` : ''}
                  </option>
                ))}
                {draftAllowed && <option value={DRAFT}>{t('tpl.start.draftOption', { n: wf.data?.version ?? '?' })}</option>}
              </select>
            </Field>
            {versions.error ? <ErrorNotice error={versions.error} onRetry={versions.reload} /> : null}
            {detail.error ? <ErrorNotice error={detail.error} onRetry={detail.reload} /> : null}
            {resolved && (
              <section className="stack-sm" aria-label={t('tpl.start.inputs')}>
                <h3 className="tk-label">{t('tpl.start.inputs')}</h3>
                <p className="faint small">{t('tpl.rerun.inputsHint')}</p>
                <RunInputsForm
                  projectId={run.project_id}
                  inputs={resolved.schema.inputs}
                  graph={resolved.graph}
                  values={values}
                  defaults={frozen}
                  onChange={(nodeId, v) => setValues((x) => ({ ...x, [nodeId]: v }))}
                  rules={resolved.policy.input_rules}
                  exploratory={resolved.policy.mode === 'exploratory' || isDraft}
                  lookups={lookups}
                  showMissing={touched}
                />
                <ParamOverridesForm projectId={run.project_id} graph={resolved.graph} overridable={resolved.schema.overridable ?? {}} defs={defs} lookups={lookups} overrides={overrides} onChange={setOverrides} />
              </section>
            )}
          </>
        )}
        <></>
        <div className="form-grid">
          <Field label={t('wf.runDialog.runTitle')} optional>
            <input className="input" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label={t('tpl.start.purpose')} optional hint={t('tpl.start.purposeHint')}>
            <LimitedTextarea className="input" rows={2} limit="description" value={purpose} onChange={(e) => setPurpose(e.target.value)} />
          </Field>
        </div>
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
        {graphErrors.length > 0 && <GraphIssueList errors={graphErrors} graph={resolved?.graph} />}
      </div>
    </Dialog>
  );
}
