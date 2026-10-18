// Working state of the built-in simplified editor: the op list being edited,
// undo / redo history, the op being edited and the viewport selection.
// Framework-free (React reads it with useSyncExternalStore) so the
// EditorAdapter can wrap it.

import type { ModelOp, ModelOpType, ModelRevision, StepBase } from '../../api/types';
import { canonical, defaultOp, isBodyOp, nextOpNumber, normalizeModes, opIdFromNumber, opsEqual, type DefaultContext } from './ops';

export type ViewTarget = { kind: 'face'; id: number } | { kind: 'edge'; id: number } | { kind: 'op'; id: string } | null;

export interface EditorState {
  /** Revision the working list started from (null = empty part / blank design). */
  revision: ModelRevision | null;
  base: StepBase | null;
  ops: ModelOp[];
  /** Next op number to hand out (ids are never reused within a part). */
  nextNo: number;
  selectedOpId: string | null;
  target: ViewTarget;
  canUndo: boolean;
  canRedo: boolean;
  dirty: boolean;
  /** False for mesh references and read-only users. */
  editable: boolean;
}

interface Snapshot {
  ops: ModelOp[];
  base: StepBase | null;
  nextNo: number;
  selectedOpId: string | null;
}

export interface UpdateOptions {
  /** Consecutive updates with the same key within a short window form one undo step (typing). */
  coalesce?: string;
}

const COALESCE_MS = 900;
const HISTORY_LIMIT = 200;

export interface StoredDraft {
  revisionId: string | null;
  base: StepBase | null;
  ops: ModelOp[];
  nextNo: number;
  savedAt: string;
}

export class EditorStore {
  private state: EditorState = {
    revision: null,
    base: null,
    ops: [],
    nextNo: 1,
    selectedOpId: null,
    target: null,
    canUndo: false,
    canRedo: false,
    dirty: false,
    editable: true,
  };
  private past: Snapshot[] = [];
  private future: Snapshot[] = [];
  private lastKey: string | null = null;
  private lastAt = 0;
  private listeners = new Set<() => void>();
  private draftTimer: ReturnType<typeof setTimeout> | null = null;
  /** The user changed something since the last load (drafts are only written / cleared after that). */
  private touched = false;

  constructor(private draftKey: string | null = null) {}

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getState = (): EditorState => this.state;

  private emit(next: Partial<EditorState>) {
    const merged = { ...this.state, ...next };
    merged.canUndo = this.past.length > 0;
    merged.canRedo = this.future.length > 0;
    const rev = merged.revision;
    merged.dirty = !opsEqual(merged.ops, rev?.ops ?? []) || canonical(merged.base ?? null) !== canonical(rev?.base ?? null);
    this.state = merged;
    this.listeners.forEach((fn) => fn());
    this.scheduleDraft();
  }

  private snapshot(): Snapshot {
    const { ops, base, nextNo, selectedOpId } = this.state;
    return { ops, base, nextNo, selectedOpId };
  }

  private record(key?: string) {
    const now = Date.now();
    if (key && key === this.lastKey && now - this.lastAt < COALESCE_MS) {
      this.lastAt = now;
      return;
    }
    this.past.push(this.snapshot());
    if (this.past.length > HISTORY_LIMIT) this.past.shift();
    this.future = [];
    this.lastKey = key ?? null;
    this.lastAt = now;
  }

  /** Start editing `revision` (or an empty part). Clears history. */
  load(revision: ModelRevision | null, opts: { nextOpId?: string | null; editable?: boolean; draft?: StoredDraft | null } = {}) {
    this.past = [];
    this.future = [];
    this.lastKey = null;
    this.touched = !!opts.draft;
    const base = revision?.base ?? null;
    const draft = opts.draft;
    const ops = draft ? draft.ops : (revision?.ops ?? []).map((o) => ({ ...o }));
    const nextNo = Math.max(nextOpNumber(opts.nextOpId ?? revision?.next_op_id, ops), draft?.nextNo ?? 0);
    this.emit({
      revision,
      base: draft ? draft.base : base,
      ops,
      nextNo,
      selectedOpId: null,
      target: null,
      editable: opts.editable ?? !(revision?.mesh_only ?? false),
    });
  }

  setEditable(editable: boolean) {
    if (editable !== this.state.editable) this.emit({ editable });
  }

  private commit(ops: ModelOp[], extra: Partial<Snapshot> = {}, key?: string) {
    if (!this.state.editable) return;
    this.touched = true;
    this.record(key);
    const base = extra.base !== undefined ? extra.base : this.state.base;
    this.emit({ ...extra, ops: normalizeModes(ops, !!base) });
  }

  /** Context for default values of new ops. */
  defaultContext(bbox?: DefaultContext['bbox']): DefaultContext {
    const bodies = this.state.ops.filter((o) => isBodyOp(o) && !o.suppressed);
    const targets = this.state.ops.filter((o) => (isBodyOp(o) || o.type === 'hole' || o.type === 'cut') && !o.suppressed);
    return { bbox, hasBase: !!this.state.base, bodies: bodies.length, lastBodyId: bodies[bodies.length - 1]?.id ?? null,
      lastTargetId: targets[targets.length - 1]?.id ?? null };
  }

  /** Append a new op (defaults filled in, `patch` applied on top). Returns its id. */
  addOp(type: ModelOpType, patch: Partial<ModelOp> = {}, bbox?: DefaultContext['bbox']): string | null {
    if (!this.state.editable) return null;
    const id = opIdFromNumber(this.state.nextNo);
    const op = { ...defaultOp(type, id, this.defaultContext(bbox)), ...patch, id, type } as ModelOp;
    this.commit([...this.state.ops, op], { nextNo: this.state.nextNo + 1, selectedOpId: id });
    return id;
  }

  /** Append ops coming from outside (adapter command); missing ids are allocated. */
  insertOp(op: Omit<ModelOp, 'id'> & { id?: string }): string | null {
    if (!this.state.editable) return null;
    const id = op.id && !this.state.ops.some((o) => o.id === op.id) ? op.id : opIdFromNumber(this.state.nextNo);
    const n = Number(id.slice(2));
    this.commit([...this.state.ops, { ...op, id } as ModelOp], { nextNo: Math.max(this.state.nextNo, n + 1), selectedOpId: id });
    return id;
  }

  updateOp(id: string, updater: Partial<ModelOp> | ((op: ModelOp) => ModelOp), opts: UpdateOptions = {}) {
    const idx = this.state.ops.findIndex((o) => o.id === id);
    if (idx < 0) return;
    const cur = this.state.ops[idx];
    const next = typeof updater === 'function' ? updater(cur) : ({ ...cur, ...updater, id: cur.id, type: cur.type } as ModelOp);
    if (canonical(next) === canonical(cur)) return;
    const ops = [...this.state.ops];
    ops[idx] = next;
    this.commit(ops, {}, opts.coalesce ? `${id}:${opts.coalesce}` : undefined);
  }

  removeOp(id: string) {
    if (!this.state.ops.some((o) => o.id === id)) return;
    const ops = this.state.ops.filter((o) => o.id !== id);
    this.commit(ops, { selectedOpId: this.state.selectedOpId === id ? null : this.state.selectedOpId });
  }

  setSuppressed(id: string, suppressed: boolean) {
    this.updateOp(id, (op) => ({ ...op, suppressed }));
  }

  /** Replace the whole list (adapter submitChange with a full op list). */
  replaceOps(ops: ModelOp[], base?: StepBase | null) {
    const nextNo = nextOpNumber(opIdFromNumber(this.state.nextNo), ops);
    const selected = ops.some((o) => o.id === this.state.selectedOpId) ? this.state.selectedOpId : null;
    this.commit(ops, { nextNo, selectedOpId: selected, ...(base !== undefined ? { base } : {}) });
  }

  /** Discard edits and go back to the revision's op list (undoable). */
  revert() {
    const rev = this.state.revision;
    this.commit((rev?.ops ?? []).map((o) => ({ ...o })), { base: rev?.base ?? null, selectedOpId: null });
  }

  undo() {
    const prev = this.past.pop();
    if (!prev) return;
    this.touched = true;
    this.future.push(this.snapshot());
    this.lastKey = null;
    this.emit({ ...prev });
  }

  redo() {
    const next = this.future.pop();
    if (!next) return;
    this.touched = true;
    this.past.push(this.snapshot());
    this.lastKey = null;
    this.emit({ ...next });
  }

  selectOp(id: string | null) {
    if (id === this.state.selectedOpId) return;
    this.emit({ selectedOpId: id });
  }

  setTarget(target: ViewTarget) {
    this.emit({ target, ...(target?.kind === 'op' ? { selectedOpId: target.id } : {}) });
  }

  // ─── unsynced draft (localStorage; cleared on save / revert) ─────────────

  private scheduleDraft() {
    if (!this.draftKey) return;
    if (this.draftTimer) clearTimeout(this.draftTimer);
    this.draftTimer = setTimeout(() => this.writeDraft(), 600);
  }

  private writeDraft() {
    if (!this.draftKey || !this.touched) return;
    try {
      if (!this.state.dirty || !this.state.editable) {
        // Only clear a draft that belongs to the revision being shown.
        const cur = readDraft(this.draftKey);
        if (cur && cur.revisionId === (this.state.revision?.id ?? null)) localStorage.removeItem(this.draftKey);
        return;
      }
      const d: StoredDraft = { revisionId: this.state.revision?.id ?? null, base: this.state.base, ops: this.state.ops, nextNo: this.state.nextNo, savedAt: new Date().toISOString() };
      localStorage.setItem(this.draftKey, JSON.stringify(d));
    } catch {
      /* storage full or unavailable: drafts are a convenience only */
    }
  }

  clearDraft() {
    if (!this.draftKey) return;
    try {
      localStorage.removeItem(this.draftKey);
    } catch {
      /* ignore */
    }
  }

  dispose() {
    if (this.draftTimer) {
      clearTimeout(this.draftTimer);
      this.writeDraft();
    }
    this.listeners.clear();
  }
}

export function readDraft(key: string): StoredDraft | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const d = JSON.parse(raw) as StoredDraft;
    return Array.isArray(d.ops) ? d : null;
  } catch {
    return null;
  }
}
