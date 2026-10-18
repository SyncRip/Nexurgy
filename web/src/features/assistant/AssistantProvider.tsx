// One shared dock/full assistant; browsing keeps references local until a send.

import { createContext,useCallback,useContext,useEffect,useMemo,useRef,useState,type ReactNode } from 'react';
import { useLocation,useNavigate } from 'react-router-dom';
import {
createAiConversation,
getAiCapabilities,
type AiAutonomy,
type AiCapabilities,
type AiConversationDetail,
type AiRef,
type AiRefInput,
type AiRequestedOperation,
} from '../../api/assistant';
import { isApiError } from '../../api/client';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useAuth } from '../../auth/AuthProvider';
import { useOptionalCurrentProject } from '../../components/shell/CurrentProject';
import { useShell } from '../../components/shell/ShellContext';
import { useResource } from '../../hooks/useResource';
import { draftKey,keptRequestIds,readDraft,removeDraft,safeStorage,writeDraft,type ComposerDraft,type DraftStorage,type KeptRequestIds } from './drafts';
import { createDesignChoiceDrafts, type DesignChoiceDrafts } from './designChoices';
import { newRequestId,type ComposerOperation,type OperationEvent as ModelOperationEvent } from './model';
import { declareContext,EMPTY_CONTEXT,inputOnly,lockPageTarget,queuePending,registerPage,removePending,restoreContext,type PageContext,type PendingContext,type PendingReference } from './pageContext';
import { useConversationStore,type ConversationStore } from './useConversationStore';
import { WorkflowGovernanceProvider } from './WorkflowGovernance';
export type { PageContext } from './pageContext';

export type AssistantLayout = 'dock' | 'full';

export type OperationEvent = ModelOperationEvent;

export interface OpenOptions {
  conversationId?: UUID;
  projectId?: UUID;
  /** With projectId: restore the task's most recent conversation, or start one bound to the task. */
  taskId?: UUID | null;
  newConversation?: boolean;
  focus?: boolean;
  /** Open the full workspace instead of the dock. */
  full?: boolean;
}

export interface AddContextOptions {
  asTarget?: boolean;
  text?: string;
  /** Open the assistant after adding (default true). */
  open?: boolean;
  /** Bind a newly created conversation to this task. */
  taskId?: UUID | null;
  /** Preselect this operation in the composer for the next send (e.g. 'drawing_intake' from "Analyse drawing"). */
  operation?: AiRequestedOperation;
}

export interface AssistantApi {
  isOpen: boolean;
  layout: AssistantLayout;
  conversationId: UUID | null;
  /** Project of the active conversation (null before one exists). */
  conversationProjectId: UUID | null;
  open(opts?: OpenOptions): void;
  close(): void;
  toggle(): void;
  /** Add a reference to the active conversation's composer (switching to / creating a conversation of the ref's project). */
  addContext(ref: AiRefInput & { project_id: UUID }, opts?: AddContextOptions): Promise<AiRef | null>;
  pageContext: PageContext | null;
  setPageContext(ctx: PageContext | null, owner?: symbol): void;
  pending: PendingContext;
  removePending(identity: string): void;
  queuePending(entry: PendingReference): void;
  restorePending(context: PendingContext): void;
  onOperationEvent(cb: (e: OperationEvent) => void): () => void;
  /** Whether an embedded host (the part model page) is mounted. */

  // ── for the assistant's own panels (features/assistant/*) ──
  /** Bumped whenever the composer changed outside the panel (addContext). */
  composerRevision: number;
  /** Last notice from addContext (target preselected / kept, conversation switched). */
  notice: AssistantNotice | null;
  clearNotice(): void;
  selectConversation(id: UUID | null, projectId?: UUID | null): void;
  emitOperationEvent(e: OperationEvent): void;
  /** Error from addContext / open, shown by the panel. */
  lastError: unknown;
}

export type AssistantNotice =
  | { kind: 'target_preselected' | 'target_kept' | 'target_set' | 'already_added'; ref: AiRef }
  | { kind: 'conversation_switched'; projectId: UUID; title?: string };

/** What the panels need besides the public API. */
export interface AssistantSession {
  userId: string;
  conversationId: UUID | null;
  store: ConversationStore;
  /** Project of the start page (no conversation yet): explicit choice, else the page's, else the last used. */
  startProjectId: UUID | null;
  setStartProjectId(id: UUID | null): void;
  /** Task a new conversation is bound to (open() from a task page). */
  startTaskId: UUID | null;
  /** AI-native: the main agent's autonomy chosen on the start page for the conversation the first send creates (null =
   * the server's default, `capabilities.autonomy_default`: the person's last one). */
  startAutonomy: AiAutonomy | null;
  setStartAutonomy(a: AiAutonomy | null): void;
  /** Capabilities of the shown project (conversation or start page). */
  capabilities: AiCapabilities | undefined;
  capabilitiesError: unknown;
  createConversation(projectId: UUID, taskId?: UUID | null, title?: string): Promise<AiConversationDetail>;
  /** Show a conversation returned by create / fork. */
  adoptConversation(detail: AiConversationDetail): void;
  /** Bumped after changes that affect the conversation list (create, rename, archive, send). */
  listRevision: number;
  bumpList(): void;
  drafts: {
    key(conversationId: UUID | null, projectId?: UUID | null): string;
    read(key: string): ComposerDraft;
    write(key: string, d: ComposerDraft): void;
    remove(key: string): void;
    /** False when localStorage is unavailable: drafts last only until a reload. */
    persistent: boolean;
  };
  /** client_request_id of choice re-sends and retries, kept until the server confirmed them (survives remounts / reloads). */
  requestIds: KeptRequestIds;
  designDrafts: DesignChoiceDrafts;
  /** Operation a page asked to preselect (addContext({ operation })), applied once by the composer of that conversation. */
  presetOperation: { conversationId: UUID; operation: ComposerOperation; nonce: number } | null;
  consumePreset(nonce: number): void;
  prefillText: { text: string; nonce: number } | null;
  consumeText(): void;
  /** Bumped when a caller asked to focus the composer (open({ focus: true }), shortcut). */
  focusNonce: number;
  requestFocus(): void;
  /** Before a link leaves the full workspace (view a result): keep the conversation beside the page. */
  keepDockOnLeave(): void;
  /** Full workspace → back to the last page with the dock open. */
  collapse(): void;
  /** The page the full workspace was opened from (its "Back" link; the workbench by default). */
  backTo: string;
  /** Dock / embedded → full workspace. */
  expand(): void;
  clearError(): void;
  /** Show an error in the panel (e.g. a start-page send that failed after its conversation was created). */
  reportError(e: unknown): void;
}

const AssistantContext = createContext<AssistantApi | null>(null);
const SessionContext = createContext<AssistantSession | null>(null);

const MOBILE_MAX = 767;

interface Persisted {
  open: boolean;
  conversationId: UUID | null;
  projectId: UUID | null;
}

function storageKey(userId: string): string {
  return `partroute.assistant.v1.${userId}`;
}

function readPersisted(userId: string): Persisted {
  try {
    const raw = localStorage.getItem(storageKey(userId));
    if (!raw) return { open: false, conversationId: null, projectId: null };
    const v = JSON.parse(raw) as Partial<Persisted>;
    return {
      open: v.open === true,
      conversationId: typeof v.conversationId === 'string' ? v.conversationId : null,
      projectId: typeof v.projectId === 'string' ? v.projectId : null,
    };
  } catch {
    return { open: false, conversationId: null, projectId: null };
  }
}

function writePersisted(userId: string, v: Persisted): void {
  try {
    localStorage.setItem(storageKey(userId), JSON.stringify(v));
  } catch {
    /* storage unavailable: the dock simply starts closed next time */
  }
}

export function isMobileViewport(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.(`(max-width: ${MOBILE_MAX}px)`).matches === true;
}

export function isAssistantPath(pathname: string): boolean {
  return pathname === '/assistant' || pathname.startsWith('/assistant/');
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const userId = user?.id ?? 'anonymous';
  const navigate = useNavigate();
  const location = useLocation();
  const { project: shellProject } = useShell();
  // R1.2: without a page project the assistant starts on the current project.
  const currentProjectId = useOptionalCurrentProject()?.projectId ?? null;
  const initial = useMemo(() => readPersisted(userId), [userId]);
  const [isOpen, setIsOpen] = useState(initial.open);
  const [conversationId, setConversationId] = useState<UUID | null>(initial.conversationId);
  /** Project of the active (or, on the start page, the last) conversation. */
  const [lastProjectId, setLastProjectId] = useState<UUID | null>(initial.projectId);
  const conversationProjectId = conversationId ? lastProjectId : null;
  const [pageContext, setPageContextState] = useState<PageContext | null>(null);
  const [pending, setPending] = useState<PendingContext>(EMPTY_CONTEXT);
  const pageOwner = useRef<symbol | undefined>(undefined);
  const handover = useRef(false);
  const pageContextRef = useRef<PageContext | null>(null);
  const setPageContext = useCallback((page: PageContext | null, owner?: symbol) => {
    const previous = { owner: pageOwner.current, page: pageContextRef.current, handedOver: handover.current };
    const next = registerPage(previous, page, owner);
    if (next === previous) return;
    pageOwner.current = next.owner;
    handover.current = next.handedOver;
    pageContextRef.current = next.page;
    setPageContextState(page);
    setPending((current) => declareContext(current, page));
    if (page && page.projectId !== conversationProjectIdRef.current) {
      if (conversationProjectIdRef.current) setConversationId(null);
      setLastProjectId(page.projectId);
      setStartProjectChoice(page.projectId);
    }
  }, []);
  const removePendingRef = useCallback((identity: string) => setPending((current) => removePending(current, identity)), []);
  const queuePendingRef = useCallback((entry: PendingReference) => setPending((current) => queuePending(current, entry)), []);
  const restorePending = useCallback((context: PendingContext) => setPending((current) => restoreContext(current, context, pageContextRef.current)), []);
  const [composerRevision, setComposerRevision] = useState(0);
  const [notice, setNotice] = useState<AssistantNotice | null>(null);
  const [lastError, setLastError] = useState<unknown>(null);
  const [startProjectChoice, setStartProjectChoice] = useState<UUID | null>(null);
  const [startTaskId, setStartTaskId] = useState<UUID | null>(null);
  const [startAutonomy, setStartAutonomy] = useState<AiAutonomy | null>(null);
  const [listRevision, setListRevision] = useState(0);
  const presetOperation = null;
  const [prefillText, setPrefillText] = useState<AssistantSession['prefillText']>(null);
  const consumeText = useCallback(() => setPrefillText(null), []);
  const listeners = useRef(new Set<(e: OperationEvent) => void>());

  const onAssistantRoute = isAssistantPath(location.pathname);
  const pathRef = useRef(location.pathname);
  pathRef.current = location.pathname;
  const [focusNonce, setFocusNonce] = useState(0);
  const lastPage = useRef<string>(paths.workbench());
  useEffect(() => {
    if (!onAssistantRoute) lastPage.current = `${location.pathname}${location.search}`;
  }, [onAssistantRoute, location.pathname, location.search]);

  useEffect(() => {
    writePersisted(userId, { open: isOpen, conversationId, projectId: lastProjectId });
  }, [userId, isOpen, conversationId, lastProjectId]);

  const emitOperationEvent = useCallback((e: OperationEvent) => {
    listeners.current.forEach((cb) => {
      try {
        cb(e);
      } catch (err) {
        console.error('assistant operation listener failed', err);
      }
    });
  }, []);

  const store = useConversationStore(
    conversationId,
    useCallback(
      (events: OperationEvent[]) => {
        events.forEach(emitOperationEvent);
        if (events.some((e) => e.type !== 'status')) setListRevision((n) => n + 1);
      },
      [emitOperationEvent],
    ),
  );
  const detail = store.detail?.conversation.id === conversationId ? store.detail : null;
  const detailRef = useRef(detail);
  detailRef.current = detail;
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  const conversationProjectIdRef = useRef(conversationProjectId);
  conversationProjectIdRef.current = conversationProjectId;
  // The loaded conversation tells which project it belongs to.
  const loadedProject = detail?.conversation.project.id ?? null;
  useEffect(() => {
    if (loadedProject) setLastProjectId(loadedProject);
  }, [loadedProject, conversationId]);

  const selectConversation = useCallback((id: UUID | null, projectId?: UUID | null) => {
    if (id !== conversationIdRef.current) setPending(declareContext(EMPTY_CONTEXT, pageContextRef.current));
    setConversationId(id);
    if (projectId !== undefined && projectId !== null) setLastProjectId(projectId);
    if (id !== null) {
      setStartTaskId(null);
      setStartProjectChoice(null);
    }
  }, []);

  // A remembered conversation that is gone (deleted, other account) → start page.
  useEffect(() => {
    if (isApiError(store.error) && store.error.status === 404 && conversationId) selectConversation(null);
  }, [store.error, conversationId, selectConversation]);

  const reveal = useCallback(
    (full?: boolean, id?: UUID | null) => {
      if (full || isMobileViewport() || isAssistantPath(pathRef.current)) {
        handover.current = true;
        if (!isAssistantPath(pathRef.current) || id) navigate(paths.assistant(id ?? undefined));
        return;
      }
      setIsOpen(true);
    },
    [navigate],
  );

  const adoptConversation = useCallback(
    (d: AiConversationDetail) => {
      store.prime(d);
      setConversationId(d.conversation.id);
      setLastProjectId(d.conversation.project.id);
      setStartTaskId(null);
      setStartProjectChoice(null);
      setListRevision((n) => n + 1);
    },
    // store.prime is stable
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectConversation],
  );

  const createConversation = useCallback(
    async (projectId: UUID, _taskId?: UUID | null, title?: string) => {
      const created = await createAiConversation({ project_id: projectId, ...(title ? { title } : {}) });
      adoptConversation(created);
      return created;
    },
    [adoptConversation],
  );

  const open = useCallback(
    (opts: OpenOptions = {}) => {
      setLastError(null);
      if (opts.focus) setFocusNonce((n) => n + 1);
      if (opts.conversationId) {
        selectConversation(opts.conversationId, opts.projectId ?? null);
        reveal(opts.full, opts.conversationId);
        return;
      }
      if (opts.newConversation) {
        selectConversation(null);
        if (opts.projectId) setStartProjectChoice(opts.projectId);
        setStartTaskId(null);
        reveal(opts.full, null);
        return;
      }
      if (opts.projectId && !conversationIdRef.current) setStartProjectChoice(opts.projectId);
      reveal(opts.full, conversationIdRef.current);
    },
    [reveal, selectConversation, createConversation],
  );

  const close = useCallback(() => setIsOpen(false), []);

  const collapse = useCallback(() => {
    if (!isMobileViewport()) setIsOpen(true);
    navigate(lastPage.current);
  }, [navigate]);

  // Read while on the assistant route, where the ref no longer moves.
  const backTo = lastPage.current;

  const expand = useCallback(() => {
    handover.current = true;
    navigate(paths.assistant(conversationIdRef.current ?? undefined));
  }, [navigate]);

  const toggle = useCallback(() => {
    if (isAssistantPath(location.pathname)) {
      collapse();
      return;
    }
    if (isMobileViewport()) {
      handover.current = true;
      navigate(paths.assistant(conversationIdRef.current ?? undefined));
      return;
    }
    setIsOpen((v) => !v);
  }, [location.pathname, collapse, navigate]);

  const storageRef = useRef<DraftStorage | null | undefined>(undefined);
  if (storageRef.current === undefined) storageRef.current = safeStorage();
  const memoryDrafts = useRef(new Map<string, ComposerDraft>());
  const drafts = useMemo<AssistantSession['drafts']>(
    () => ({
      key: (cid, pid) => draftKey(userId, cid, pid),
      read: (key) => memoryDrafts.current.get(key) ?? readDraft(storageRef.current ?? null, key),
      write: (key, d) => {
        memoryDrafts.current.set(key, d);
        writeDraft(storageRef.current ?? null, key, d);
      },
      remove: (key) => {
        memoryDrafts.current.delete(key);
        removeDraft(storageRef.current ?? null, key);
      },
      persistent: !!storageRef.current,
    }),
    [userId],
  );
  const requestIds = useMemo(() => keptRequestIds(storageRef.current ?? null, userId, newRequestId), [userId]);
  const designDrafts = useMemo(() => createDesignChoiceDrafts(storageRef.current ?? null, userId), [userId]);

  const addContext = useCallback(async (ref: AiRefInput & { project_id: UUID }, opts: AddContextOptions = {}): Promise<AiRef | null> => {
    setLastError(null);
    if (conversationProjectIdRef.current && conversationProjectIdRef.current !== ref.project_id) {
      selectConversation(null, ref.project_id);
      setStartProjectChoice(ref.project_id);
      setNotice({ kind: 'conversation_switched', projectId: ref.project_id });
    } else if (!conversationIdRef.current) setStartProjectChoice(ref.project_id);
    if (opts.text) setPrefillText({ text: opts.text, nonce: Date.now() });
    queuePendingRef({ input: inputOnly(ref), label: pageContext?.label ?? ref.kind, target: opts.asTarget });
    setComposerRevision((n) => n + 1);
    if (opts.open !== false) reveal(false, conversationProjectIdRef.current === ref.project_id ? conversationIdRef.current : null);
    return null;
  }, [pageContext?.label, queuePendingRef, reveal, selectConversation]);

  const onOperationEvent = useCallback((cb: (e: OperationEvent) => void) => {
    listeners.current.add(cb);
    return () => {
      listeners.current.delete(cb);
    };
  }, []);


  const clearNotice = useCallback(() => setNotice(null), []);
  // A notice describes the conversation it came from; switching conversations drops it
  // (addContext sets its own notice after the switch, once the server answered).
  useEffect(() => setNotice(null), [conversationId]);
  const layout: AssistantLayout = onAssistantRoute ? 'full' : 'dock';

  const value = useMemo<AssistantApi>(
    () => ({
      isOpen,
      layout,
      conversationId,
      conversationProjectId,
      open,
      close,
      toggle,
      addContext,
      pageContext,
      setPageContext,
      pending,
      removePending: removePendingRef,
      queuePending: queuePendingRef,
      restorePending,
      onOperationEvent,
      composerRevision,
      notice,
      clearNotice,
      selectConversation,
      emitOperationEvent,
      lastError,
    }),
    [isOpen, layout, pending, removePendingRef, queuePendingRef, restorePending, setPageContext, conversationId, conversationProjectId, open, close, toggle, addContext, pageContext, onOperationEvent,
      composerRevision, notice, clearNotice, selectConversation, emitOperationEvent, lastError],
  );

  // ── session (panels only) ──

  const startProjectId = startProjectChoice ?? pageContext?.projectId ?? shellProject?.id ?? currentProjectId ?? lastProjectId ?? null;
  const capsProjectId = detail?.conversation.project.id ?? (conversationId ? null : startProjectId);
  const caps = useResource((signal) => getAiCapabilities(capsProjectId!, { signal }), [capsProjectId], { enabled: !!capsProjectId });

  const bumpList = useCallback(() => setListRevision((n) => n + 1), []);
  const requestFocus = useCallback(() => setFocusNonce((n) => n + 1), []);
  const consumePreset = useCallback((_nonce: number) => {}, []);
  const keepDockOnLeave = useCallback(() => {
    if (isAssistantPath(pathRef.current) && !isMobileViewport()) setIsOpen(true);
  }, []);
  const clearError = useCallback(() => setLastError(null), []);
  const reportError = useCallback((e: unknown) => setLastError(e), []);

  const session = useMemo<AssistantSession>(
    () => ({
      userId,
      conversationId,
      store,
      startProjectId,
      setStartProjectId: (id) => { setStartProjectChoice(id); setPending((p) => p.references.every((r) => r.input.project_id === id) ? p : EMPTY_CONTEXT); },
      startTaskId,
      startAutonomy,
      setStartAutonomy,
      capabilities: caps.data,
      capabilitiesError: caps.error,
      createConversation,
      adoptConversation,
      listRevision,
      bumpList,
      drafts,
      requestIds,
      designDrafts,
      presetOperation,
      consumePreset,
      prefillText,
      consumeText,
      focusNonce,
      requestFocus,
      keepDockOnLeave,
      collapse,
      backTo,
      expand,
      clearError,
      reportError,
    }),
    // store is a new object per render but its members only change with detail / loading / error
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [userId, conversationId, store.detail, store.loading, store.error, startProjectId, startTaskId, startAutonomy, caps.data, caps.error, createConversation, adoptConversation,
      listRevision, bumpList, drafts, requestIds, designDrafts, presetOperation, consumePreset, prefillText, consumeText, focusNonce, requestFocus, keepDockOnLeave, collapse, backTo, expand, clearError, reportError],
  );

  return (
    <AssistantContext.Provider value={value}>
      <SessionContext.Provider value={session}><WorkflowGovernanceProvider key={userId} userId={userId}>{children}</WorkflowGovernanceProvider></SessionContext.Provider>
    </AssistantContext.Provider>
  );
}

export function useAssistant(): AssistantApi {
  const v = useContext(AssistantContext);
  if (!v) throw new Error('useAssistant must be used inside <AssistantProvider>');
  return v;
}

/** Panel-side state (features/assistant only). */
export function useAssistantSession(): AssistantSession {
  const v = useContext(SessionContext);
  if (!v) throw new Error('useAssistantSession must be used inside <AssistantProvider>');
  return v;
}

/** Declare removable local context. Full-mode handover keeps it across source unmount. */
export function useAssistantPageContext(ctx: PageContext | null, lockTarget = false): void {
  const { setPageContext } = useAssistant();
  const owner = useRef(Symbol());
  const previous = useRef<PageContext | null>(null);
  const next = lockPageTarget(previous.current, ctx, lockTarget);
  previous.current = next;
  const key = JSON.stringify(next);
  useEffect(() => {
    setPageContext(key === 'null' ? null : (JSON.parse(key) as PageContext), owner.current);
  }, [key, setPageContext]);
  useEffect(() => { const token = owner.current; return () => setPageContext(null, token); }, [setPageContext]);
}

