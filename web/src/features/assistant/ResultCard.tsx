import { useFeature } from '../../api/capabilities';
// Result of a succeeded operation (design §4.4, §6.2): type, name, exact
// version, base version, run source, availability, simulated label. Primary
// actions "view" and "continue from this result" (= this exact version becomes
// the next target); business actions in a menu: use as task input, add as a
// deliverable, open the run, new conversation from this result. Each action is
// its own service call with its own checks; none implies acceptance.

import { ArrowRightCircle,ClipboardPlus,Eye,GitFork,Layers,MoreHorizontal,PackagePlus,PlayCircle,Sparkles } from 'lucide-react';
import { lazy,Suspense,useState } from 'react';
import { Link,useNavigate } from 'react-router-dom';
import type { AiRef } from '../../api/assistant';
import { forkAiConversation,type AiOperation,type AiResultItem } from '../../api/assistant';
import type { TaskDraft } from '../../api/pm';
import { paths } from '../../app/nav';
import { exactSourcePath } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { GeomCheckChip } from '../cad/Verdicts';
import { useAssistant,useAssistantSession } from './AssistantProvider';
import { AsError,refScope,ResultLabels,SimChip,tk } from './common';
import { pendingFromRef,refIdentity } from './pageContext';

const UseAsInputDialog = lazy(() => import('./ResultDialogs').then((m) => ({ default: m.UseAsInputDialog })));
const AddDeliverableDialog = lazy(() => import('./ResultDialogs').then((m) => ({ default: m.AddDeliverableDialog })));
const DraftTaskDialog = lazy(() => import('./ResultDialogs').then((m) => ({ default: m.DraftTaskDialog })));

function useResultTarget(item: AiResultItem): boolean {
  const assistant = useAssistant();
  return assistant.pending.references.some((r) => r.target && refIdentity(r.input) === refIdentity(pendingFromRef(item.ref).input));
}

/** Shared identity and truth labels for standalone results and a saved CAD delivery. */
export function ResultIdentity({ op, item }: { op: AiOperation; item: AiResultItem }) {
  const { t } = useI18n();
  const isTarget = useResultTarget(item);
  const typeLabel = tk(t, `tk.assetType.${item.asset_type ?? ''}`, item.asset_type ?? tk(t, `as.refKind.${item.ref.kind}`, item.ref.kind));
  return <>
    <Layers aria-hidden className="as-result-ico" />
    <span className="as-result-type">{typeLabel}</span>
    <span className="as-result-name ellip" title={item.ref.title}>{item.ref.title}</span>
    <span className="as-result-ver mono">{item.label}</span>
    {op.simulated && !item.labels?.includes('simulated') && <SimChip />}
    <ResultLabels labels={item.labels} />
    {item.asset_type === 'part_model' && <GeomCheckChip status={item.check_status} short />}
    {isTarget && <Chip tone="accent" className="sm">{t('as.result.isTarget')}</Chip>}
  </>;
}

export function ResultMetadata({ op, item }: { op: AiOperation; item: AiResultItem }) {
  const { t } = useI18n();
  return <dl className="as-result-meta">
    <div><dt>{t('as.result.base')}</dt><dd>{item.base ? <BaseLabel base={item.base} /> : t('as.result.noBase')}</dd></div>
    {item.run_id && <div><dt>{t('as.result.source')}</dt><dd>
      <Link to={paths.run(item.run_id)}>{op.run?.title || t('as.result.run')}</Link>
      {item.node_id ? <span className="faint mono"> · {item.node_id}</span> : null}
    </dd></div>}
    {item.availability && <div><dt>{t('as.result.availability')}</dt><dd>
      {tk(t, `as.avail.${item.availability}`, item.availability)}
      {item.publish_state ? ` · ${tk(t, `as.publish.${item.publish_state}`, item.publish_state)}` : ''}
    </dd></div>}
  </dl>;
}

/** The same permission gates, exact target and dialogs back both result presentations. */
export function ResultActions({ op, item, archived, viewPath: savedPath, viewLabel, viewPrimary = false }: {
  op: AiOperation; item: AiResultItem; archived: boolean; viewPath?: string; viewLabel?: string; viewPrimary?: boolean;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const session = useAssistantSession();
  const assistant = useAssistant();
  const { store } = session;
  const [dialog, setDialog] = useState<null | 'input' | 'deliverable'>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const tasksEnabled = useFeature('tasks');
  const can = new Set(item.actions);
  const projectId = store.detail?.conversation.project.id ?? item.ref.project_id;
  const taskId = store.detail?.conversation.task?.id ?? op.task?.id ?? null;
  const isTarget = useResultTarget(item);
  const viewPath = savedPath ?? (exactSourcePath(item.ref) || (item.asset_id && item.asset_version_id ? paths.assetVersion(item.asset_id, item.asset_version_id) : null));

  const continueFrom = async () => {
    setBusy(true);
    setError(null);
    try {
      assistant.queuePending(pendingFromRef(item.ref, true));
      toast(t('as.result.continued', { label: item.label }), { tone: 'info' });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const fork = async () => {
    const convId = store.detail?.conversation.id;
    if (!convId) return;
    setBusy(true);
    setError(null);
    try {
      // Only the clicked item is carried over (not every item of the operation's result).
      const created = await forkAiConversation(convId, { operation_id: op.id, reference_keys: [item.ref.key] });
      // The full workspace follows the provider's conversation in its URL.
      session.adoptConversation(created);
      toast(t('as.result.forked', { label: item.label }), { tone: 'info' });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const menu: MenuItem[] = [];
  if (tasksEnabled && can.has('use_as_input') && item.asset_version_id)
    menu.push({ id: 'input', label: t('as.result.useAsInput'), icon: <ClipboardPlus />, onSelect: () => setDialog('input'), disabled: archived });
  if (tasksEnabled && can.has('add_deliverable') && item.asset_version_id)
    menu.push({ id: 'deliv', label: t('as.result.addDeliverable'), icon: <PackagePlus />, onSelect: () => setDialog('deliverable'), disabled: archived });
  if (can.has('open_run') && item.run_id) menu.push({ id: 'run', label: t('as.result.openRun'), icon: <PlayCircle />, onSelect: () => navigate(paths.run(item.run_id!)) });
  if (can.has('fork') && (tasksEnabled || !taskId)) menu.push({ id: 'fork', label: t('as.result.fork'), icon: <GitFork />, onSelect: () => void fork(), separator: menu.length > 0 });

  return (
    <>
      <div className="as-result-actions">
        {viewPath && (
          <Link className={`btn sm${viewPrimary ? ' primary' : ''}`} to={viewPath} onClick={() => session.keepDockOnLeave()}>
            <Eye /> {viewLabel ?? t('as.result.view')}
          </Link>
        )}
        {can.has('continue') && (
          <button type="button" className={`btn sm${viewPrimary ? '' : ' primary'}`} onClick={() => void continueFrom()} disabled={busy || archived || isTarget} title={t('as.result.continueHint', { label: item.label })}>
            <ArrowRightCircle /> {t('as.result.continue')}
          </button>
        )}
        {menu.length > 0 && <Menu label={t('as.result.more')} triggerClassName="btn sm ghost icon" trigger={<MoreHorizontal />} items={menu} />}
      </div>
      <AsError error={error} />
      {tasksEnabled && dialog && (
        <Suspense fallback={null}>
          {dialog === 'input' ? (
            <UseAsInputDialog projectId={projectId} item={item} defaultTaskId={taskId} onClose={() => setDialog(null)} />
          ) : (
            <AddDeliverableDialog projectId={projectId} item={item} defaultTaskId={taskId} onClose={() => setDialog(null)} />
          )}
        </Suspense>
      )}
    </>
  );
}

export function ResultCard({ op, item, archived }: { op: AiOperation; item: AiResultItem; archived: boolean }) {
  const { t } = useI18n();
  const typeLabel = tk(t, `tk.assetType.${item.asset_type ?? ''}`, item.asset_type ?? tk(t, `as.refKind.${item.ref.kind}`, item.ref.kind));
  return <section className="as-result" aria-label={`${typeLabel} ${item.ref.title} ${item.label}`}>
    <div className="as-result-h"><ResultIdentity op={op} item={item} /></div>
    <ResultMetadata op={op} item={item} />
    <ResultActions op={op} item={item} archived={archived} />
  </section>;
}

/** task_draft result: a proposal that is never stored until the person creates the task in the dialog. */
export function TaskDraftResult({ op, draft, archived }: { op: AiOperation; draft: TaskDraft; archived: boolean }) {
  const tasksEnabled = useFeature('tasks');
  const { t } = useI18n();
  const { store } = useAssistantSession();
  const [open, setOpen] = useState(false);
  const projectId = store.detail?.conversation.project.id ?? null;
  return (
    <section className="as-result" aria-label={t('as.result.draftTitle')}>
      <div className="as-result-h">
        <Sparkles aria-hidden className="as-result-ico" />
        <span className="as-result-type">{t('as.result.draftTitle')}</span>
        <span className="as-result-name ellip">{draft.title}</span>
        {(draft.simulated || op.simulated) && <SimChip />}
      </div>
      {draft.objective && <p className="as-result-text">{draft.objective}</p>}
      {draft.criteria.length > 0 && (
        <ul className="as-plain small">
          {draft.criteria.slice(0, 5).map((c, i) => (
            <li key={i}>
              {c.description}
              {c.required ? '' : ` (${t('as.result.optional')})`}
            </li>
          ))}
          {draft.criteria.length > 5 && <li className="faint">{t('as.result.moreCriteria', { n: draft.criteria.length - 5 })}</li>}
        </ul>
      )}
      <div className="as-result-actions">
        <button type="button" className="btn sm primary" onClick={() => setOpen(true)} disabled={archived || !projectId || !tasksEnabled}>
          <ClipboardPlus /> {t('as.result.createTask')}
        </button>
      </div>
      <p className="as-op-note">{t('as.result.draftNote')}</p>
      {tasksEnabled && open && projectId && (
        <Suspense fallback={null}>
          <DraftTaskDialog projectId={projectId} draft={draft} onClose={() => setOpen(false)} />
        </Suspense>
      )}
    </section>
  );
}

/** Base of a result: the request target's exact version (a CAD pick shows its revision and scope). */
function BaseLabel({ base }: { base: AiRef }) {
  const { t, lang } = useI18n();
  const scope = base.kind === 'cad_selection' ? refScope(t, base, lang) : null;
  return (
    <>
      <span className="mono strong">{base.version_label ?? base.title}</span>
      {scope ? <span className="faint"> · {scope}</span> : null}
    </>
  );
}
