// EditorAdapter — the seam between the part workspace and a CAD editor.
//
// The workspace (revision bar, AI panel, drawings, process, quote …) talks to
// whatever editor is mounted only through this interface. The built-in
// simplified editor implements it in simpleAdapter.ts. A future complex editor
// (a React component, or a third-party app in an iframe bridged with
// postMessage) implements the same contract; see docs/editor-integration.md.
//
// Rules every implementation must keep:
// - Geometry is saved ONLY as a new model revision through the PartRoute API
//   (today POST /parts/{id}/revisions with base + ops; later a STEP upload /
//   base+ops path). No direct database or storage access.
// - The editor never receives the user's bearer token unless it runs in the
//   PartRoute origin. Iframe editors get data through the host bridge only.
// - Op ids come from the part-wide counter (revision.next_op_id) and are never
//   reused; the server rejects reuse with 422 op_id_reused.

import type { BodyMode, EdgeGroup, ModelOp, ModelOpType, ModelRevision, Profile, StepBase, Vec3 } from '../../api/types';

export type Unsubscribe = () => void;

/** Something the user (or the host) can select. Face / edge ids are mesh_json ids of the current build only. */
export type EditorTarget =
  | { kind: 'face'; id: number }
  | { kind: 'edge'; id: number }
  | { kind: 'op'; id: string }
  | null;

export type ExportFormat = 'step' | 'glb' | 'mesh_json';

export interface EditorCapabilities {
  /** Stable id of the editor implementation, e.g. "partroute-simple". */
  id: string;
  name: string;
  version: string;
  /** Op types this editor can create / edit (subset of server/app/cad/ops.py). */
  operations: ModelOpType[];
  profiles: Profile['kind'][];
  edgeGroups: EdgeGroup[];
  booleanModes: BodyMode[];
  selection: ('face' | 'edge' | 'op' | 'point')[];
  undoRedo: boolean;
  livePreview: boolean;
  measure: boolean;
  exportFormats: ExportFormat[];
  /** Whether the loaded model can be changed right now. */
  editable: boolean;
  /** Why not, when editable is false. */
  readOnlyReason: 'mesh_reference' | 'no_permission' | 'not_loaded' | null;
  /** How changes are persisted: always as a new revision via the PartRoute API. */
  saveTarget: 'revision';
}

/** Fine-grained edit commands (what an embedded editor sends instead of a full list). */
export type EditorCommand =
  | { type: 'add_op'; op: Omit<ModelOp, 'id'> & { id?: string } }
  | { type: 'update_op'; id: string; patch: Partial<ModelOp> }
  | { type: 'remove_op'; id: string }
  | { type: 'suppress_op'; id: string; suppressed: boolean }
  | { type: 'set_base'; base: StepBase | null }
  | { type: 'undo' }
  | { type: 'redo' };

export interface ChangeResult {
  ok: boolean;
  /** Working op list after the change. */
  ops: ModelOp[];
  /** Id of an op created by the change, if any. */
  createdId?: string | null;
  error?: { code: string; message: string } | null;
}

export interface SelectionEvent {
  target: EditorTarget;
  /** World point (mm, +Z up) of the pick, when the selection came from the viewport. */
  point?: Vec3 | null;
  /** Face normal / edge kind etc., implementation-defined extras. */
  info?: Record<string, unknown> | null;
}

export interface EditorEvents {
  ready: { capabilities: EditorCapabilities; revision: ModelRevision | null };
  selection: SelectionEvent;
  change: { ops: ModelOp[]; base: StepBase | null; dirty: boolean; canUndo: boolean; canRedo: boolean };
  saved: { revision: ModelRevision };
  error: { code: string; message: string; opId?: string | null; details?: Record<string, unknown> };
}

export type EditorEventName = keyof EditorEvents;

export interface EditorAdapter {
  readonly id: string;
  /** Load a revision (null = empty part). Resolves when the editor is ready. */
  loadModel(revision: ModelRevision | null, opts?: { nextOpId?: string | null }): Promise<void>;
  getCapabilities(): EditorCapabilities;
  /** Host-driven selection (e.g. clicking an op in a diff). */
  select(target: EditorTarget, extra?: Omit<SelectionEvent, 'target'>): void;
  onSelection(cb: (e: SelectionEvent) => void): Unsubscribe;
  /** Apply a full replacement op list or one command to the working model. */
  submitChange(change: ModelOp[] | EditorCommand): Promise<ChangeResult>;
  /** Persist the working model as a new revision (parent = the loaded revision). */
  saveRevision(note: string): Promise<ModelRevision>;
  /** Export the loaded (saved) revision. Unsaved edits are not exported. */
  export(format: ExportFormat): Promise<Blob>;
  on<K extends EditorEventName>(event: K, cb: (payload: EditorEvents[K]) => void): Unsubscribe;
  dispose(): void;
}

/** Tiny typed event emitter shared by adapter implementations. */
export class Emitter<E extends object> {
  private map = new Map<keyof E, Set<(p: never) => void>>();

  on<K extends keyof E>(event: K, cb: (payload: E[K]) => void): Unsubscribe {
    let set = this.map.get(event);
    if (!set) {
      set = new Set();
      this.map.set(event, set);
    }
    set.add(cb as (p: never) => void);
    return () => set!.delete(cb as (p: never) => void);
  }

  emit<K extends keyof E>(event: K, payload: E[K]): void {
    this.map.get(event)?.forEach((cb) => {
      try {
        (cb as (p: E[K]) => void)(payload);
      } catch (e) {
        console.error(`editor ${String(event)} listener failed`, e);
      }
    });
  }

  clear() {
    this.map.clear();
  }
}
