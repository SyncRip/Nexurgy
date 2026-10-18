// AI work panel contract (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §4): the side panel a conversation's cards
// open next to the chat (workflow canvas, plan preview, run, model, task, task draft). The AI workspace's full layout provides
// it; everywhere else useWorkPanel() returns the fallback below, whose open() goes to the workspace with the panel.
//
// The URL is the only source of the panel state: ?panel=<kind>:<id>&node=<node id>&wide=1 on
// /assistant/:conversationId, so reload and back / forward restore it.

import { createContext,useContext,useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import type { UUID,Workflow } from '../../../api/types';
import { paths } from '../../../app/nav';
import { useAssistant } from '../AssistantProvider';

export type PanelObject =
  | { kind: 'workflow'; id: UUID }
  /** A workflow_edit / workflow_create plan, previewed on the canvas. */
  | { kind: 'plan'; operationId: UUID }
  | { kind: 'run'; id: UUID }
  | { kind: 'revision'; id: UUID }
  | { kind: 'task'; id: UUID }
  /** A task draft of the main agent (AI-native, .trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator). */
  | { kind: 'draft'; id: UUID };

export interface WorkPanelState {
  object: PanelObject | null;
  nodeId: string | null;
  wide: boolean;
}

export interface PanelDraft {
  workflowId: UUID;
  dirty: boolean;
  /** Save the canvas; on success the composer is already re-pinned. Throws on failure. */
  save(): Promise<Workflow>;
}

export interface WorkPanelApi {
  /** The panel opens next to the chat (only the AI workspace's full layout). */
  inline: boolean;
  state: WorkPanelState;
  /** Open an object; outside the workspace this goes to /assistant/<conversation>?panel=… */
  open(object: PanelObject, opts?: { nodeId?: string | null }): void;
  /** False when nothing closed (nothing open, or the person kept unsaved canvas edits). */
  close(): boolean;
  setWide(wide: boolean): void;
  /** Locate a node (a card's "view"); never changes the chat target. */
  focusNode(nodeId: string | null): void;
  /** Registered by the panel while its canvas has unsaved changes. */
  draft: PanelDraft | null;
}

export const PANEL_PARAM = 'panel';
export const NODE_PARAM = 'node';
export const WIDE_PARAM = 'wide';

const KINDS = ['workflow', 'plan', 'run', 'revision', 'task', 'draft'] as const;

export function panelParam(o: PanelObject): string {
  return `${o.kind}:${o.kind === 'plan' ? o.operationId : o.id}`;
}

export function parsePanelParam(v: string | null | undefined): PanelObject | null {
  if (!v) return null;
  const i = v.indexOf(':');
  if (i <= 0) return null;
  const kind = v.slice(0, i);
  const id = v.slice(i + 1);
  if (!id || !/^[0-9a-f-]{8,64}$/i.test(id) || !(KINDS as readonly string[]).includes(kind)) return null;
  if (kind === 'plan') return { kind: 'plan', operationId: id };
  return { kind: kind as Exclude<PanelObject['kind'], 'plan'>, id };
}

/** /assistant/<conversation>?panel=…[&node=…] — where a panel object opens from outside the workspace. */
export function panelHref(conversationId: UUID | null, object: PanelObject, nodeId?: string | null): string {
  const q = new URLSearchParams();
  q.set(PANEL_PARAM, panelParam(object));
  if (nodeId) q.set(NODE_PARAM, nodeId);
  return `${paths.assistant(conversationId ?? undefined)}?${q.toString()}`;
}

export const WorkPanelCtx = createContext<WorkPanelApi | null>(null);

const CLOSED: WorkPanelState = { object: null, nodeId: null, wide: false };

/** Outside the provider (dock, embedded panel, other pages): nothing is open; open() goes to the workspace. */
export function useWorkPanel(): WorkPanelApi {
  const ctx = useContext(WorkPanelCtx);
  const navigate = useNavigate();
  const { conversationId, open } = useAssistant();
  const fallback = useMemo<WorkPanelApi>(
    () => ({
      inline: false,
      state: CLOSED,
      open: (object, opts) => { open({ full: true }); navigate(panelHref(conversationId, object, opts?.nodeId)); },
      close: () => false,
      setWide: () => {},
      focusNode: () => {},
      draft: null,
    }),
    [navigate, conversationId, open],
  );
  return ctx ?? fallback;
}
