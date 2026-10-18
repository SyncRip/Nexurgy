import { ImportSourceReceipt } from '../templates/ImportSourceReceipt';
// Project → Workflow templates (R1.2 prototype #flows.templates): a card per
// template with its publish state, description, node chain, draft validity and
// how often it ran; create (blank or from a built-in template), import from the
// public library, run a published version, archive / restore. Creating, editing
// and archiving need the template permission (owner or template manager).

import { Archive,ArchiveRestore,CircleCheck,Download,ExternalLink,FilePlus2,LayoutTemplate,MoreHorizontal,PencilLine,Play,Plus,Sparkles,TriangleAlert } from 'lucide-react';
import { useMemo,useState } from 'react';
import { Link,useNavigate } from 'react-router-dom';
import { archiveWorkflow,createWorkflow,listWorkflowNodeTypes,listWorkflowTemplates,listWorkflows } from '../../api/endpoints';
import type { Workflow,WorkflowTemplate } from '../../api/types';
import { paths } from '../../app/nav';
import type { Tone } from '../../components/ui/Badges';
import { ArchivedChip } from '../../components/ui/Badges';
import { ConfirmDialog,Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { LimitedTextarea } from '../../components/ui/LimitedText';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { runsPerWorkflow } from '../flows/board';
import { BOARD_LIMIT,useFlowsData } from '../flows/data';
import '../flows/flows.css';
import { LibrarySourceChip,PublishStateChips,useTemplateAccess } from '../templates/common';
import { ProjectTemplateImport } from '../templates/ProjectTemplateImport';
import { StartRunDialog } from '../templates/StartRunDialog';
import '../templates/templates.css';
import { NodeTypeIcon } from './icons';
import { nodeDisplayName,nodeTypeLabel } from './labels';
import { localized } from './model';
import './workflow.css';

export const workflowPath = (id: string) => paths.workflow(id);

function TemplatePreview({ tpl }: { tpl: WorkflowTemplate }) {
  const { t } = useI18n();
  const types = tpl.graph.nodes.map((n) => n.type);
  return (
    <span className="wf-tpl-chain" aria-label={types.map((x) => nodeTypeLabel(t, x)).join(' → ')}>
      {types.map((x, i) => (
        <span key={i} className="wf-tpl-step" title={nodeTypeLabel(t, x)}>
          <NodeTypeIcon type={x} />
        </span>
      ))}
    </span>
  );
}

const CATEGORY_TONE: Record<string, Tone> = { input: 'pending', cad: 'accent', manufacturing: 'ok', review: 'warn', output: 'measure', collaboration: 'stale' };
const NODE_CHIPS = 8;

/** The template's nodes as chips (prototype .tpl node row), toned by category. */
function TemplateNodes({ w, category }: { w: Workflow; category: Map<string, string> }) {
  const { t } = useI18n();
  const nodes = w.graph.nodes;
  if (!nodes.length) return <span className="faint small">{t('fl.card.noNodes')}</span>;
  const shown = nodes.length > NODE_CHIPS ? nodes.slice(0, NODE_CHIPS - 1) : nodes;
  return (
    <div className="fl-tpl-nodes" aria-label={t('wf.create.nodeCount', { n: nodes.length })}>
      {shown.map((n) => (
        <span key={n.id} className={`chip ${CATEGORY_TONE[category.get(n.type) ?? ''] ?? 'pending'}`}>
          {nodeDisplayName(t, n)}
        </span>
      ))}
      {nodes.length > shown.length && <span className="chip pending">+{nodes.length - shown.length}</span>}
    </div>
  );
}

function CreateDialog({ projectId, onClose, onCreated }: { projectId: string; onClose: () => void; onCreated: (w: Workflow) => void }) {
  const { t, lang } = useI18n();
  const templates = useResource((signal) => listWorkflowTemplates({ signal }), []);
  const defs = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const [choice, setChoice] = useState<string>('');
  const [name, setName] = useState('');
  const [nameEdited, setNameEdited] = useState(false);
  const [description, setDescription] = useState('');
  const [descEdited, setDescEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const tpl = templates.data?.find((x) => x.key === choice);
  const aiTypes = new Set((defs.data ?? []).filter((d) => d.uses_ai).map((d) => d.type));

  const pick = (key: string) => {
    setChoice(key);
    const next = templates.data?.find((x) => x.key === key);
    if (!nameEdited) setName(next ? localized(next.name, lang) : '');
    if (!descEdited) setDescription(next ? localized(next.description, lang) : '');
  };

  const nameErr = !name.trim() ? t('wf.field.nameRequired') : null;
  const submit = async () => {
    setTouched(true);
    if (nameErr) return;
    setBusy(true);
    setError(null);
    try {
      const w = await createWorkflow(projectId, { name: name.trim(), description: description.trim(), ...(choice ? { template_key: choice } : { graph: { nodes: [], edges: [], groups: [] } }) });
      onCreated(w);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('wf.create.title')}
      subtitle={t('wf.create.subtitle')}
      size="lg"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.creating') : t('wf.create.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="wf-tpl-grid" role="radiogroup" aria-label={t('wf.create.start')}>
          <button type="button" role="radio" aria-checked={choice === ''} className={`wf-tpl-card ${choice === '' ? 'selected' : ''}`} onClick={() => pick('')}>
            <span className="wf-tpl-ico">
              <FilePlus2 aria-hidden />
            </span>
            <span className="wf-tpl-name">{t('wf.create.blank')}</span>
            <span className="wf-tpl-desc">{t('wf.create.blankDesc')}</span>
          </button>
          {templates.data?.map((x) => (
            <button key={x.key} type="button" role="radio" aria-checked={choice === x.key} className={`wf-tpl-card ${choice === x.key ? 'selected' : ''}`} onClick={() => pick(x.key)}>
              <span className="wf-tpl-ico">
                <LayoutTemplate aria-hidden />
              </span>
              <span className="wf-tpl-name">
                {localized(x.name, lang)}
                {x.graph.nodes.some((n) => aiTypes.has(n.type)) && (
                  <span className="wf-ai-badge sm">
                    <Sparkles aria-hidden /> AI
                  </span>
                )}
              </span>
              <span className="wf-tpl-desc">{localized(x.description, lang)}</span>
              <TemplatePreview tpl={x} />
              <span className="faint small">{t('wf.create.nodeCount', { n: x.graph.nodes.length })}</span>
            </button>
          ))}
        </div>
        {templates.error ? <ErrorNotice error={templates.error} onRetry={templates.reload} /> : null}
        {tpl && <p className="faint small">{t('wf.create.templateNote')}</p>}
        <div className="form-grid one">
          <Field label={t('wf.field.name')} error={touched ? nameErr : null}>
            <input
              className="input"
              value={name}
              maxLength={200}
              onChange={(e) => {
                setName(e.target.value);
                setNameEdited(true);
              }}
            />
          </Field>
          <Field label={t('wf.field.description')} optional>
            <LimitedTextarea
              className="input"
              rows={2}
              limit="description"
              value={description}
              onChange={(e) => {
                setDescription(e.target.value);
                setDescEdited(true);
              }}
            />
          </Field>
        </div>
        <ErrorNotice error={error} />
      </div>
    </Dialog>
  );
}

export function WorkflowsTab({ project, caps }: ProjectTabProps) {
  const { t, fmt, lang } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [includeArchived, setIncludeArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const [confirm, setConfirm] = useState<Workflow | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<unknown>(null);
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState<Workflow | null>(null);
  const res = useResource((signal) => listWorkflows(project.id, { include_archived: includeArchived }, { signal }), [project.id, includeArchived]);
  // Owner or template manager; rows also say so (can_manage) once loaded.
  const access = useTemplateAccess(project);
  const canTemplate = (res.data?.[0]?.can_manage ?? access ?? false) && caps.workflow;
  const templates = useResource((signal) => listWorkflowTemplates({ signal }), []);
  const tplName = (key: string | null) => {
    if (!key) return null;
    const x = templates.data?.find((tp) => tp.key === key);
    return x ? localized(x.name, lang) : key;
  };
  const items = res.data ?? [];

  const open = (w: Workflow) => navigate(workflowPath(w.id));
  // Node chips are toned by category; "used N times" comes from the page's run list.
  const defs = useResource((signal) => listWorkflowNodeTypes({ signal }), []);
  const category = useMemo(() => new Map((defs.data ?? []).map((d) => [d.type, d.category])), [defs.data]);
  const shared = useFlowsData(project.id);
  const sharedRuns = shared?.runs.data;
  const used = useMemo(() => (sharedRuns ? runsPerWorkflow(sharedRuns.slice(0, BOARD_LIMIT)) : null), [sharedRuns]);
  const usedCapped = !!sharedRuns && sharedRuns.length > BOARD_LIMIT;

  const doArchive = async () => {
    if (!confirm) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const restore = !!confirm.archived_at;
      const w = await archiveWorkflow(confirm.id, restore);
      toast(restore ? t('wf.restoredToast', { name: w.name }) : t('wf.archivedToast', { name: w.name }));
      setConfirm(null);
      res.reload();
    } catch (e) {
      setConfirmError(e);
    } finally {
      setConfirmBusy(false);
    }
  };

  const actions = (w: Workflow): MenuItem[] => {
    const list: MenuItem[] = [{ id: 'open', label: t('common.open'), icon: <ExternalLink />, onSelect: () => open(w) }];
    if (caps.workflow && !w.archived_at && w.latest_published) list.push({ id: 'run', label: t('tpl.action.startRun'), icon: <Play />, onSelect: () => setRunning(w) });
    if (w.can_manage ?? canTemplate) {
      list.push(
        w.archived_at
          ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, separator: true, onSelect: () => setConfirm(w) }
          : { id: 'archive', label: t('common.archive'), icon: <Archive />, separator: true, danger: true, onSelect: () => setConfirm(w) },
      );
    }
    return list;
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={5} />;
  else if (!items.length)
    body = (
      <EmptyState
        title={includeArchived ? t('wf.list.emptyArchived') : t('wf.list.empty')}
        body={canTemplate ? t('wf.list.emptyBody') : t('tpl.list.emptyReadOnly')}
        action={
          canTemplate ? (
            <button type="button" className="btn primary" onClick={() => setCreating(true)}>
              <Plus /> {t('fl.tpl.new')}
            </button>
          ) : undefined
        }
      />
    );
  else
    body = (
      <div className={`fl-tpls ${res.loading ? 'is-refreshing' : ''}`}>
        {items.map((w) => {
          const issues = (w.publish_errors ?? w.validation).length;
          const runnable = caps.workflow && !w.archived_at && !!w.latest_published;
          const n = used?.get(w.id) ?? 0;
          const more = actions(w).filter((a) => a.id !== 'open' && a.id !== 'run');
          return (
            <article key={w.id} className={`fl-tpl ${w.archived_at ? 'is-archived' : ''}`} aria-label={w.name}>
              <div className="fl-tpl-head">
                <Link to={workflowPath(w.id)} className="fl-tpl-name">
                  {w.name}
                </Link>
                {w.archived_at && <ArchivedChip />}
                <PublishStateChips latest={w.latest_published} draftDirty={w.draft_dirty} publishedCount={w.published_count} />
              </div>
              <p className="fl-tpl-desc">{w.description || t('fl.tpl.noDescription')}</p>
              <TemplateNodes w={w} category={category} />
              <div className="fl-tpl-meta">
                <ImportSourceReceipt source={w.import_source} canOpen={w.can_open_import_source} />
                {w.library ? (
                  <LibrarySourceChip name={w.library.name} adopted={w.library.adopted_version_no} upgrade={w.library.upgrade_available} />
                ) : (
                  <span className="faint small">{tplName(w.template_key) ?? t('wf.list.blank')}</span>
                )}
                {issues ? (
                  <span className="chip err" title={t('tpl.list.publishIssuesHint')}>
                    <TriangleAlert aria-hidden /> {t('wf.issuesCount', { n: issues })}
                  </span>
                ) : (
                  <span className="chip ok">
                    <CircleCheck aria-hidden /> {t('tpl.list.publishable')}
                  </span>
                )}
              </div>
              <div className="fl-tpl-foot">
                <span className="mono faint small" title={fmt.dateTime(w.updated_at)}>
                  {used ? (usedCapped ? t('fl.tpl.usedRecent', { n, limit: BOARD_LIMIT }) : t('fl.tpl.used', { n })) : null}
                  {used ? ' · ' : ''}
                  {fmt.relative(w.updated_at)}
                </span>
                <span className="row fl-tpl-actions">
                  {more.length > 0 && <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} triggerClassName="btn ghost sm icon" items={more} />}
                  <Link className="btn sm" to={workflowPath(w.id)}>
                    {(w.can_manage ?? canTemplate) && !w.archived_at ? <PencilLine aria-hidden /> : <ExternalLink aria-hidden />}
                    {(w.can_manage ?? canTemplate) && !w.archived_at ? t('fl.tpl.edit') : t('common.open')}
                  </Link>
                  {caps.workflow && (
                    <button
                      type="button"
                      className="btn sm primary"
                      disabled={!runnable}
                      title={runnable ? undefined : w.archived_at ? undefined : t('fl.tpl.publishFirst')}
                      onClick={() => setRunning(w)}
                    >
                      <Play aria-hidden /> {t('fl.tpl.use')}
                    </button>
                  )}
                </span>
              </div>
            </article>
          );
        })}
      </div>
    );

  return (
    <div className="stack">
      <div className="toolbar">
        <p className="muted small fl-grow-text">{t('tpl.list.intro')}</p>
        <label className="check">
          <input type="checkbox" checked={includeArchived} onChange={(e) => setIncludeArchived(e.target.checked)} />
          <span>{t('common.showArchived')}</span>
        </label>
        {canTemplate && (
          <button type="button" className="btn" onClick={() => setImporting(true)}>
            <Download /> {t('core.import.title')}
          </button>
        )}
        {canTemplate && (
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            <Plus /> {t('fl.tpl.new')}
          </button>
        )}
      </div>
      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}
      {creating && (
        <CreateDialog
          projectId={project.id}
          onClose={() => setCreating(false)}
          onCreated={(w) => {
            setCreating(false);
            toast(t('wf.createdToast', { name: w.name }));
            open(w);
          }}
        />
      )}
      {importing && (
        <ProjectTemplateImport
          projectId={project.id}
          onClose={() => setImporting(false)}
          onImported={(w) => {
            setImporting(false);
            toast(t('core.import.action'));
            navigate(`${workflowPath(w.workflow_id)}/versions?version=${encodeURIComponent(w.template_version_id)}`);
          }}
        />
      )}
      {running && (
        <StartRunDialog
          projectId={project.id}
          workflow={running}
          onClose={() => setRunning(null)}
          onStarted={(run) => {
            setRunning(null);
            toast(t('wf.runStarted'));
            navigate(paths.run(run.id));
          }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={confirm.archived_at ? t('wf.restore.title', { name: confirm.name }) : t('wf.archive.title', { name: confirm.name })}
          body={confirm.archived_at ? t('wf.restore.body') : t('wf.archive.body')}
          confirmLabel={confirm.archived_at ? t('common.restore') : t('common.archive')}
          danger={!confirm.archived_at}
          busy={confirmBusy}
          error={<ErrorNotice error={confirmError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setConfirm(null);
            setConfirmError(null);
          }}
        />
      )}
    </div>
  );
}
