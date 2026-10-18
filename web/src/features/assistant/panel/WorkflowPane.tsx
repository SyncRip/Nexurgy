// Workflow pane: the part workflow on the real editor (prd §3.3). Saving writes the workflow's next version (only the
// next run uses it), re-pins the composer's references to it and tells other canvases; "Run" saves first and starts
// the run from the conversation (a run card appears there and the panel switches to the run). A plan applied or
// undone from the chat reloads the canvas — or, with unsaved edits, offers to.

import { ReactFlowProvider } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { RefreshCw,Workflow as WorkflowIcon } from 'lucide-react';
import { useCallback,useEffect,useRef,useState } from 'react';
import { repinComposer,startConversationRun } from '../../../api/assistant';
import { getProject,getWorkflow,listWorkflowNodeTypes } from '../../../api/endpoints';
import type { UUID,Workflow } from '../../../api/types';
import { Chip } from '../../../components/ui/Badges';
import { ErrorState } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { WorkflowEditor,type WorkflowEditorController } from '../../workflow/editor/WorkflowEditor';
import { emitWorkflowChanged,onWorkflowChanged } from '../../workflow/events';
import '../../workflow/workflow.css';
import { useAssistantSession } from '../AssistantProvider';
import { AsError } from '../common';
import { newRequestId } from '../model';
import { fullPageHref,type NodeSource } from './model';
import { PaneFrame,PaneLoading } from './PaneFrame';
import { useWorkPanel } from './WorkPanelContext';
import { usePanelInternals } from './WorkPanelProvider';

export function WorkflowPane({ workflowId }: { workflowId: UUID }) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const { selectNode, setDraft, locked } = usePanelInternals();
  const { store, bumpList } = useAssistantSession();
  const conversationId = store.detail?.conversation.id ?? null;
  const archived = !!store.detail?.conversation.archived;

  const wfRes = useResource((signal) => getWorkflow(workflowId, { signal }), [workflowId]);
  const projectId = wfRes.data?.project_id;
  const projectRes = useResource((signal) => getProject(projectId!, { signal }), [projectId], { enabled: !!projectId });
  const defsRes = useResource((signal) => listWorkflowNodeTypes({ signal }), []);

  /** The editor's server state (name, version after saves). */
  const [current, setCurrent] = useState<Workflow | null>(null);
  /** A selected node that is not in the saved graph yet (it cannot be the chat target before a save). */
  const [unsavedNode, setUnsavedNode] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  /** A newer version from outside, adopted by the clean editor. */
  const [external, setExternal] = useState<Workflow | undefined>(undefined);
  /** A newer version arrived while the canvas had unsaved edits. */
  const [outdated, setOutdated] = useState<number | null>(null);
  /** Remount the editor on a newer version (reload discards unsaved edits). */
  const [mount, setMount] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const ctl = useRef<WorkflowEditorController>(null);
  /** Re-pin of the last save, awaited by "save and send" and by "run". */
  const afterSave = useRef<Promise<void> | null>(null);
  /** client_request_id of the run being started: kept until the server answered, so a retry is the same run. */
  const runRequest = useRef<string | null>(null);

  const wf = current ?? wfRes.data ?? null;
  const versionRef = useRef(0);
  versionRef.current = wf?.version ?? 0;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // A plan applied / undone from the chat (or a save elsewhere): reload a clean canvas, offer it to a dirty one.
  useEffect(
    () =>
      onWorkflowChanged(({ workflowId: id, version }) => {
        if (id !== workflowId || version <= versionRef.current) return;
        if (dirtyRef.current) {
          setOutdated(version);
          return;
        }
        getWorkflow(workflowId)
          .then((w) => setExternal(w))
          .catch(() => setOutdated(version));
      }),
    [workflowId],
  );

  const reload = async () => {
    setError(null);
    try {
      const latest = await getWorkflow(workflowId);
      wfRes.setData(latest);
      setCurrent(null);
      setExternal(undefined);
      setOutdated(null);
      setMount((n) => n + 1);
    } catch (e) {
      setError(e);
    }
  };

  /** Every save: the composer's references follow the new version, other canvases hear about it. */
  const afterSaved = useCallback(
    async (w: Workflow) => {
      runRequest.current = null;
      setOutdated(null);
      if (conversationId && !archived) {
        try {
          store.applyComposer(conversationId, await repinComposer(conversationId, w.id));
        } catch (e) {
          // The workflow is saved; only the references still point at the previous version (shown as stale).
          setError(e);
        }
        store.reload();
      }
      emitWorkflowChanged(w.id, w.version);
    },
    // store.applyComposer / store.reload are stable
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conversationId, archived],
  );

  const saveDraft = useCallback(async () => {
    const c = ctl.current;
    if (!c) throw new Error(t('wp.editor.notSaved'));
    const w = await c.save();
    await afterSave.current;
    return w;
  }, [t]);

  // While the canvas has unsaved edits the composer can offer "save and send".
  useEffect(() => setDraft(dirty ? { workflowId, dirty: true, save: saveDraft } : null), [dirty, workflowId, saveDraft, setDraft]);
  useEffect(() => () => setDraft(null), [setDraft]);

  const startRun = async () => {
    if (!conversationId || running) return;
    const requestId = runRequest.current ?? (runRequest.current = newRequestId());
    setRunning(true);
    setError(null);
    try {
      await afterSave.current;
      const res = await startConversationRun(conversationId, { workflow_id: workflowId, client_request_id: requestId });
      runRequest.current = null;
      store.reload();
      bumpList();
      setDraft(null); // saved before the run (the editor runs only a saved graph)
      wp.open({ kind: 'run', id: res.run.id });
    } catch (e) {
      setError(e);
    } finally {
      setRunning(false);
    }
  };

  const icon = <WorkflowIcon />;
  const kind = t('wp.kind.workflow');
  if (wfRes.error && !wfRes.data) {
    return (
      <PaneFrame icon={icon} kind={kind} title={t('wp.kind.workflow')} fullHref={fullPageHref({ kind: 'workflow', id: workflowId })}>
        <div className="wp-pad">
          <ErrorState error={wfRes.error} onRetry={wfRes.reload} />
        </div>
      </PaneFrame>
    );
  }
  if (!wf || !wfRes.data || !projectRes.data || !defsRes.data) {
    if (projectRes.error || defsRes.error) {
      return (
        <PaneFrame icon={icon} kind={kind} title={wf?.name ?? kind}>
          <div className="wp-pad">
            <ErrorState error={projectRes.error || defsRes.error} onRetry={() => (projectRes.reload(), defsRes.reload())} />
          </div>
        </PaneFrame>
      );
    }
    return <PaneLoading icon={icon} kind={kind} />;
  }

  const partWorkflow = !!wf.part_id;
  const source: NodeSource = { kind: 'workflow', objectId: workflowId, projectId: wf.project_id, version: wf.version };
  const canRunHere = partWorkflow && !!conversationId && !archived;

  return (
    <PaneFrame
      icon={icon}
      kind={kind}
      title={wf.name}
      chips={
        <>
          <span className="mono faint">v{wf.version}</span>
          <Chip tone={partWorkflow ? 'measure' : 'pending'} className="sm">
            {partWorkflow ? t('wp.wf.part') : t('wp.wf.template')}
          </Chip>
          {dirty && <Chip tone="warn" className="sm">{t('wf.unsaved')}</Chip>}
        </>
      }
      fullHref={fullPageHref({ kind: 'workflow', id: workflowId }, { partId: wf.part_id })}
      source={source}
      bodyClass="wp-fill"
    >
      {(outdated !== null || error || running) && (
        <div className="wp-notes">
          {outdated !== null && (
            <div className="callout info slim">
              <RefreshCw aria-hidden />
              <span className="grow">{t('wp.wf.outdated', { v: outdated })}</span>
              <button type="button" className="btn sm" onClick={() => void reload()}>
                {t('wp.wf.reload')}
              </button>
              <button type="button" className="btn ghost sm" onClick={() => setOutdated(null)}>
                {t('wp.wf.keepEditing')}
              </button>
            </div>
          )}
          {running && (
            <p className="faint small" role="status">
              {t('wp.wf.starting')}
            </p>
          )}
          <AsError error={error} />
        </div>
      )}
      {unsavedNode && wp.state.nodeId === unsavedNode && (
        <p className="wp-note faint small" role="status">
          {t('wp.wf.unsavedNode')}
        </p>
      )}
      <div className="wp-editor">
        <ReactFlowProvider>
          <WorkflowEditor
            key={`${workflowId}:${mount}`}
            initial={wfRes.data}
            project={projectRes.data}
            defs={defsRes.data}
            compact
            back={null}
            external={external}
            selectedNodeId={wp.state.nodeId}
            lockAssistantTarget={locked}
            onSelectNode={(id) => {
              // The server pins node references to the saved graph: a node that exists only on the unsaved canvas is
              // selected here and becomes the chat target after a save.
              const saved = !id || !!wf?.graph.nodes.some((n) => n.id === id);
              setUnsavedNode(saved ? null : id);
              selectNode(id, saved ? source : null);
            }}
            controllerRef={ctl}
            onSaved={(w) => {
              afterSave.current = afterSaved(w);
            }}
            onDirtyChange={setDirty}
            onWorkflowChange={setCurrent}
            onRequestRun={canRunHere ? () => void startRun() : undefined}
          />
        </ReactFlowProvider>
      </div>
      <p className="wp-foot faint small">{partWorkflow ? t('wp.wf.nextRunNote') : t('wp.wf.templateNote')}</p>
    </PaneFrame>
  );
}
