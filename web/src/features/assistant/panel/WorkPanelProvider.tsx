// Provider of the AI work panel in the workspace's full layout (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §4).
// The URL is the only source of the panel state (?panel=…&node=…&wide=1): opening another object pushes a history
// entry, every other change replaces it. Besides the public WorkPanelApi the panes get a few internals: the target
// lock, node selection (which follows into the chat target unless locked) and the unsaved-canvas draft.

import { createContext,useCallback,useContext,useMemo,useRef,useState,type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useI18n } from '../../../i18n/I18nProvider';
import { useAssistant,useAssistantSession } from '../AssistantProvider';
import { nextPanelSearch,nodeRefInput,readPanelState,sameObject,type NodeSource,type PanelPatch } from './model';
import { WorkPanelCtx,type PanelDraft,type PanelObject,type WorkPanelApi } from './WorkPanelContext';

export interface PanelInternals {
  /** Locked: selecting a node no longer moves the chat target. */
  locked: boolean;
  setLocked(v: boolean): void;
  /** A node clicked on a pane canvas: the URL node and, unless locked, the chat target (src null: select only). */
  selectNode(nodeId: string | null, src: NodeSource | null): void;
  /** Make a node the chat target (an explicit choice, also while locked). */
  targetNode(src: NodeSource, nodeId: string): Promise<void>;
  /** The workflow pane registers its unsaved canvas (null when saved / gone). */
  setDraft(d: PanelDraft | null): void;
}

const InternalsCtx = createContext<PanelInternals | null>(null);

export function usePanelInternals(): PanelInternals {
  const v = useContext(InternalsCtx);
  if (!v) throw new Error('usePanelInternals must be used inside <WorkPanelProvider>');
  return v;
}

export function WorkPanelProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const [search, setSearch] = useSearchParams();
  const state = useMemo(() => readPanelState(search), [search]);
  const searchRef = useRef(search);
  searchRef.current = search;
  const stateRef = useRef(state);
  stateRef.current = state;
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const queuePending = assistant.queuePending;

  const [draft, setDraftState] = useState<PanelDraft | null>(null);
  const draftRef = useRef<PanelDraft | null>(null);
  const setDraft = useCallback((d: PanelDraft | null) => {
    draftRef.current = d;
    setDraftState(d);
  }, []);
  const [locked, setLocked] = useState(false);
  /** Where focus was when the panel opened (it returns there on close). */
  const opener = useRef<HTMLElement | null>(null);
  const apply = useCallback(
    (patch: PanelPatch) => {
      const { search: next, push, changed } = nextPanelSearch(searchRef.current, patch);
      if (!changed) return;
      searchRef.current = next;
      setSearch(next, { replace: !push });
    },
    [setSearch],
  );

  /** Leaving unsaved canvas edits asks first (the editor also keeps them as a local draft). */
  const leaveOk = useCallback(() => !draftRef.current?.dirty || window.confirm(t('wp.leaveDirty')), [t]);

  const open = useCallback(
    (object: PanelObject, opts?: { nodeId?: string | null }) => {
      const cur = stateRef.current.object;
      if (!sameObject(object, cur) && !leaveOk()) return;
      if (!cur && typeof document !== 'undefined') opener.current = document.activeElement as HTMLElement | null;
      apply({ object, nodeId: opts?.nodeId });
    },
    [apply, leaveOk],
  );

  const close = useCallback(() => {
    if (!stateRef.current.object || !leaveOk()) return false;
    apply({ object: null });
    const el = opener.current;
    opener.current = null;
    if (el) window.setTimeout(() => el.isConnected && el.focus({ preventScroll: true }), 0);
    return true;
  }, [apply, leaveOk]);

  const setWide = useCallback(
    (wide: boolean) => {
      if (stateRef.current.object) apply({ wide });
    },
    [apply],
  );
  const focusNode = useCallback(
    (nodeId: string | null) => {
      if (stateRef.current.object) apply({ nodeId });
    },
    [apply],
  );

  const targetNode = useCallback(async (src: NodeSource, nodeId: string) => {
    const detail = sessionRef.current.store.detail;
    if (detail?.conversation.archived || (src.kind === 'workflow' && src.version === undefined)) return;
    queuePending({ input: nodeRefInput(src, nodeId), label: nodeId, target: true });
  }, [queuePending]);

  const selectNode = useCallback(
    (nodeId: string | null, _src: NodeSource | null) => {
      if (!stateRef.current.object) return;
      apply({ nodeId });
      // Each pane declares its selected saved node, respecting the target lock.
    },
    [apply],
  );

  const api = useMemo<WorkPanelApi>(() => ({ inline: true, state, open, close, setWide, focusNode, draft }), [state, open, close, setWide, focusNode, draft]);
  const internals = useMemo<PanelInternals>(() => ({ locked, setLocked, selectNode, targetNode, setDraft }), [locked, selectNode, targetNode, setDraft]);

  return (
    <WorkPanelCtx.Provider value={api}>
      <InternalsCtx.Provider value={internals}>{children}</InternalsCtx.Provider>
    </WorkPanelCtx.Provider>
  );
}
