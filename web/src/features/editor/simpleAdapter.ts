// EditorAdapter implementation for the built-in simplified CAD editor.
// The React UI (features/part/model/*) reads `store` directly; everything a
// host or another module needs goes through the EditorAdapter methods.

import { ApiError } from '../../api/client';
import { createRevision, getBlob } from '../../api/endpoints';
import type { ModelOp, ModelRevision } from '../../api/types';
import { Emitter, type ChangeResult, type EditorAdapter, type EditorCapabilities, type EditorCommand, type EditorEvents, type EditorTarget, type ExportFormat, type SelectionEvent } from './adapter';
import { BODY_MODES, EDGE_GROUPS, OP_TYPES, PROFILE_KINDS } from './ops';
import { EditorStore, readDraft, type StoredDraft } from './store';

export const SIMPLE_EDITOR_ID = 'partroute-simple';

export class SimpleEditorAdapter implements EditorAdapter {
  readonly id = SIMPLE_EDITOR_ID;
  readonly store: EditorStore;
  private events = new Emitter<EditorEvents>();
  private canDesign: boolean;
  private loaded = false;
  private unsubStore: () => void;
  private lastChangeSig = '';
  readonly draftKey: string;

  constructor(private opts: { partId: string; canDesign: boolean }) {
    this.canDesign = opts.canDesign;
    this.draftKey = `partroute.ws.draft.${opts.partId}`;
    this.store = new EditorStore(opts.canDesign ? this.draftKey : null);
    this.unsubStore = this.store.subscribe(() => {
      const s = this.store.getState();
      const sig = `${s.ops.length}|${s.canUndo}|${s.canRedo}|${s.dirty}`;
      // Cheap change detection: op array identity or flags changed.
      if (sig !== this.lastChangeSig || s.ops !== this.lastOps) {
        this.lastChangeSig = sig;
        this.lastOps = s.ops;
        this.events.emit('change', { ops: s.ops, base: s.base, dirty: s.dirty, canUndo: s.canUndo, canRedo: s.canRedo });
      }
    });
  }

  private lastOps: ModelOp[] | null = null;

  setCanDesign(v: boolean) {
    this.canDesign = v;
    const rev = this.store.getState().revision;
    this.store.setEditable(v && !(rev?.mesh_only ?? false));
  }

  /** Unsaved draft stored for this part, if it belongs to `revision`. */
  pendingDraft(revision: ModelRevision | null): StoredDraft | null {
    if (!this.canDesign) return null;
    const d = readDraft(this.draftKey);
    if (!d || d.revisionId !== (revision?.id ?? null)) return null;
    return d;
  }

  async loadModel(revision: ModelRevision | null, opts: { nextOpId?: string | null; draft?: StoredDraft | null } = {}): Promise<void> {
    this.store.load(revision, { nextOpId: opts.nextOpId, editable: this.canDesign && !(revision?.mesh_only ?? false), draft: opts.draft ?? null });
    this.loaded = true;
    this.events.emit('ready', { capabilities: this.getCapabilities(), revision });
  }

  getCapabilities(): EditorCapabilities {
    const s = this.store.getState();
    const reason: EditorCapabilities['readOnlyReason'] = !this.loaded ? 'not_loaded' : s.revision?.mesh_only ? 'mesh_reference' : !this.canDesign ? 'no_permission' : null;
    const rev = s.revision;
    return {
      id: SIMPLE_EDITOR_ID,
      name: 'Nexurgy simplified CAD editor',
      version: '0.4',
      operations: [...OP_TYPES],
      profiles: [...PROFILE_KINDS],
      edgeGroups: [...EDGE_GROUPS],
      booleanModes: [...BODY_MODES],
      selection: ['face', 'edge', 'op', 'point'],
      undoRedo: true,
      livePreview: true,
      measure: true,
      exportFormats: rev ? ([rev.step_sha ? 'step' : null, rev.glb_sha ? 'glb' : null, rev.mesh_sha ? 'mesh_json' : null].filter(Boolean) as ExportFormat[]) : [],
      editable: reason === null,
      readOnlyReason: reason,
      saveTarget: 'revision',
    };
  }

  select(target: EditorTarget, extra: Omit<SelectionEvent, 'target'> = {}): void {
    this.store.setTarget(target);
    this.events.emit('selection', { target, ...extra });
  }

  onSelection(cb: (e: SelectionEvent) => void) {
    return this.events.on('selection', cb);
  }

  async submitChange(change: ModelOp[] | EditorCommand): Promise<ChangeResult> {
    const s0 = this.store.getState();
    if (!s0.editable) {
      const error = { code: s0.revision?.mesh_only ? 'mesh_reference' : 'read_only', message: 'the loaded model cannot be edited' };
      this.events.emit('error', error);
      return { ok: false, ops: s0.ops, error };
    }
    let createdId: string | null = null;
    if (Array.isArray(change)) {
      this.store.replaceOps(change);
    } else {
      switch (change.type) {
        case 'add_op':
          createdId = this.store.insertOp(change.op);
          break;
        case 'update_op':
          this.store.updateOp(change.id, change.patch);
          break;
        case 'remove_op':
          this.store.removeOp(change.id);
          break;
        case 'suppress_op':
          this.store.setSuppressed(change.id, change.suppressed);
          break;
        case 'set_base':
          this.store.replaceOps(this.store.getState().ops, change.base);
          break;
        case 'undo':
          this.store.undo();
          break;
        case 'redo':
          this.store.redo();
          break;
      }
    }
    return { ok: true, ops: this.store.getState().ops, createdId };
  }

  async saveRevision(note: string): Promise<ModelRevision> {
    const s = this.store.getState();
    if (!s.editable) throw new ApiError(0, 'read_only', 'the loaded model cannot be edited');
    try {
      const rev = await createRevision(this.opts.partId, { parent_id: s.revision?.id ?? null, base: s.base, ops: s.ops, note });
      this.store.clearDraft();
      this.events.emit('saved', { revision: rev });
      return rev;
    } catch (e) {
      if (e instanceof ApiError) this.events.emit('error', { code: e.code, message: e.message, details: e.details });
      throw e;
    }
  }

  async export(format: ExportFormat): Promise<Blob> {
    const rev = this.store.getState().revision;
    const sha = format === 'step' ? rev?.step_sha : format === 'glb' ? rev?.glb_sha : rev?.mesh_sha;
    if (!rev || !sha) throw new ApiError(0, 'export_unavailable', `no ${format} export for this revision`);
    return getBlob(sha, { filename: `R${rev.rev_no}.${format === 'mesh_json' ? 'json' : format}` });
  }

  on<K extends keyof EditorEvents>(event: K, cb: (payload: EditorEvents[K]) => void) {
    return this.events.on(event, cb);
  }

  dispose(): void {
    this.unsubStore();
    this.store.dispose();
    this.events.clear();
  }
}
