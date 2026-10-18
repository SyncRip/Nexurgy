import { Download,RefreshCw } from 'lucide-react';
import { useMemo,useRef,useState } from 'react';
import { listAssetVersions,listProjectAssets } from '../../api/assets';
import { isApiError } from '../../api/client';
import { getTemplateVersion,listFiles,listProjects,listTemplateVersions,listWorkflows } from '../../api/endpoints';
import { getSpec,listSpecs } from '../../api/spec';
import { importProjectTemplate,type TemplateImportResult,type TemplateReferenceKind } from '../../api/templateImport';
import type { WorkflowGraph } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { newRequestId } from '../assistant/model';

const REF_FIELDS: Record<string, { field: string; kind: TemplateReferenceKind }> = {
  file_input: { field: 'file_version_id', kind: 'file_version' }, model_input: { field: 'revision_id', kind: 'model_revision' },
  drawing_reconstruct: { field: 'drawing_id', kind: 'drawing' }, engineering_spec: { field: 'spec_version_id', kind: 'engineering_spec_version' },
};
export function templateReferences(graph: WorkflowGraph) {
  const seen = new Set<string>();
  return graph.nodes.flatMap((node) => {
    const rule = REF_FIELDS[node.type];
    const source = rule && node.params?.[rule.field];
    if (typeof source !== 'string' || !source || seen.has(`${rule.kind}:${source}`)) return [];
    seen.add(`${rule.kind}:${source}`);
    return [{ kind: rule.kind, source_id: source, label: node.label || node.id }];
  });
}
async function targetReferences(projectId: string, kinds: TemplateReferenceKind[], signal: AbortSignal) {
  const options: { kind: TemplateReferenceKind; id: string; label: string }[] = [];
  await Promise.all(kinds.map(async (kind) => {
    if (kind === 'file_version') {
      const files = await listFiles(projectId, {}, { signal });
      for (const file of files.filter((f) => !f.archived_at)) for (const version of file.versions) options.push({ kind, id: version.id, label: `${file.name} · v${version.version}` });
    } else if (kind === 'engineering_spec_version') {
      const specs = [];
      for (let offset = 0; ; ) {
        const page = await listSpecs(projectId, { offset, limit: 100 }, { signal });
        specs.push(...page.items);
        offset += page.items.length;
        if (!page.items.length || offset >= page.total) break;
      }
      const details = await Promise.all(specs.map((spec) => getSpec(spec.id, { signal })));
      for (const spec of details) for (const version of spec.versions ?? []) if (version.status === 'ready' || version.status === 'needs_answers') options.push({ kind, id: version.id, label: `${spec.code} · ${spec.title} · v${version.version_no}` });
    } else {
      const assets = await listProjectAssets(projectId, { type: kind === 'model_revision' ? 'part_model' : 'drawing' }, { signal });
      const lists = await Promise.all(assets.map(async (asset) => ({ asset, versions: await listAssetVersions(asset.id, { signal }) })));
      for (const { asset, versions } of lists) for (const version of versions) if (version.target?.kind === kind && version.availability === 'ready' && (kind !== 'drawing' || version.target.summary.kind === 'generated')) options.push({ kind, id: version.target.id, label: `${asset.name} · ${version.label}` });
    }
  }));
  return options.sort((a, b) => a.label.localeCompare(b.label));
}
export function ProjectTemplateImport({ projectId, onClose, onImported }: { projectId: string; onClose(): void; onImported(result: TemplateImportResult): void }) {
  const { t } = useI18n();
  const projects = useResource((signal) => listProjects({}, { signal }), []);
  const [source, setSource] = useState(''); const [workflow, setWorkflow] = useState(''); const [version, setVersion] = useState('');
  const [name, setName] = useState(''); const [mapping, setMapping] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  const attempt = useRef<{ fingerprint: string; id: string } | null>(null);
  const workflows = useResource((signal) => listWorkflows(source, { kind: 'tool' }, { signal }), [source], { enabled: !!source });
  const versions = useResource((signal) => listTemplateVersions(workflow, { signal }), [workflow], { enabled: !!workflow });
  const detail = useResource((signal) => getTemplateVersion(version, { signal }), [version], { enabled: !!version });
  const current = detail.data?.id === version ? detail.data : null;
  const refs = useMemo(() => current && source !== projectId ? templateReferences(current.graph) : [], [current, source, projectId]);
  const kinds = [...new Set(refs.map((r) => r.kind))].sort();
  const targets = useResource((signal) => targetReferences(projectId, kinds, signal), [projectId, kinds.join(',')], { enabled: kinds.length > 0 });
  const ready = !!current && refs.every((ref) => mapping[`${ref.kind}:${ref.source_id}`]);
  const errorDetails = isApiError(error) ? error.details as Record<string, unknown> : null;
  const issues = errorDetails ? [errorDetails, ...(Array.isArray(errorDetails.references) ? errorDetails.references : []), ...(Array.isArray(errorDetails.issues) ? errorDetails.issues : [])].flatMap((item: unknown) => {
    if (!item || typeof item !== 'object') return [];
    const row = item as Record<string, unknown>;
    const label = [row.node_id, row.field, ...(Array.isArray(row.fields) ? row.fields : []), row.code].filter((value): value is string => typeof value === 'string').join(' · ');
    return label ? [label] : [];
  }) : [];
  const submit = async () => {
    if (!current || !ready || busy) return;
    const body = { source_template_version_id: current.id, expected_source_hash: current.hash, ...(name.trim() ? { name: name.trim() } : {}),
      reference_map: refs.map((ref) => ({ kind: ref.kind, source_id: ref.source_id, target_id: mapping[`${ref.kind}:${ref.source_id}`] })) };
    const fingerprint = JSON.stringify(body);
    if (attempt.current?.fingerprint !== fingerprint) attempt.current = { fingerprint, id: newRequestId() };
    setBusy(true); setError(null);
    try { onImported(await importProjectTemplate(projectId, { ...body, client_request_id: attempt.current.id })); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  return <Dialog title={t('core.import.title')} onClose={onClose} busy={busy} onSubmit={() => void submit()} footer={<><button type="button" className="btn" disabled={busy} onClick={onClose}>{t('common.cancel')}</button><button type="submit" className="btn primary" disabled={!ready || busy}><Download />{t('core.import.action')}</button></>}>
    <div className="form-grid one">
      <Field label={t('core.import.source')}><select className="select" value={source} disabled={busy} onChange={(e) => { setSource(e.target.value); setWorkflow(''); setVersion(''); setMapping({}); }}><option value="">—</option>{(projects.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></Field>
      <Field label={t('core.import.template')}><select className="select" value={workflow} disabled={!source || busy} onChange={(e) => { setWorkflow(e.target.value); setVersion(''); setMapping({}); }}><option value="">—</option>{(workflows.data ?? []).filter((w) => w.project_id === source && !w.part_id && !w.archived_at && (!w.kind || w.kind === 'tool')).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</select></Field>
      <Field label={t('core.import.version')}><select className="select" value={version} disabled={!workflow || busy} onChange={(e) => { setVersion(e.target.value); setMapping({}); }}><option value="">—</option>{(versions.data ?? []).filter((v) => v.workflow_id === workflow && v.status === 'published').map((v) => <option key={v.id} value={v.id}>v{v.version_no} · {v.name}</option>)}</select></Field>
      <Field label={t('core.import.name')} optional><input className="input" value={name} maxLength={200} placeholder={current?.name} disabled={busy} onChange={(e) => setName(e.target.value)} /></Field>
      {!!refs.length && <fieldset><legend>{t('core.import.references')}</legend>{refs.map((ref) => <Field key={`${ref.kind}:${ref.source_id}`} label={ref.label}><select className="select" value={mapping[`${ref.kind}:${ref.source_id}`] ?? ''} disabled={busy || !targets.data} onChange={(e) => setMapping((old) => ({ ...old, [`${ref.kind}:${ref.source_id}`]: e.target.value }))}><option value="">—</option>{(targets.data ?? []).filter((target) => target.kind === ref.kind).map((target) => <option key={target.id} value={target.id}>{target.label}</option>)}</select></Field>)}</fieldset>}
      <ErrorNotice error={error || projects.error || workflows.error || versions.error || detail.error || targets.error} />
      {!!issues.length && <ul className="small">{issues.map((issue, index) => <li key={`${index}:${issue}`}>{issue}</li>)}</ul>}
      {!!error && <button type="button" className="btn sm" disabled={busy} onClick={() => { detail.reload(); versions.reload(); workflows.reload(); targets.reload(); }}><RefreshCw />{t('core.import.refresh')}</button>}
    </div>
  </Dialog>;
}
