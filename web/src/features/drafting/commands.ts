// Local preview of edit commands before they are saved. The server applies the same commands again on the head
// version and is the authority (cad/drafting.py apply_commands); this mirror lets the editor show pending edits and
// assigns entity ids up front so the saved ids match what the user saw.

import type { DimensionEntity, DraftCommand, DraftContent, DraftEntity, DraftLayer, DraftSheet, Pt } from '../../api/drafting';
import { displayedText, measure, readingConsistent, selectionCenter, transformEntity } from './geometry';

export function nextIdNumber(content: Pick<DraftContent, 'entities' | 'next_entity_no'>): number {
  let n = content.next_entity_no ?? 1;
  for (const e of content.entities) {
    const m = /^E-(\d+)$/.exec(e.id);
    if (m) n = Math.max(n, parseInt(m[1], 10) + 1);
  }
  return n;
}

/** Ids for `count` new entities, unique in the content. */
export function newIds(content: Pick<DraftContent, 'entities' | 'next_entity_no'>, count: number): string[] {
  const taken = new Set(content.entities.map((e) => e.id));
  const out: string[] = [];
  let n = nextIdNumber(content);
  while (out.length < count) {
    const id = `E-${String(n).padStart(4, '0')}`;
    n++;
    if (!taken.has(id)) out.push(id);
  }
  return out;
}

function markModified(e: DraftEntity): DraftEntity {
  return (e.origin === 'source' || e.origin === 'ai') && !e.modified ? { ...e, modified: true } : e;
}

function replace(entities: DraftEntity[], next: DraftEntity): DraftEntity[] {
  return entities.map((e) => (e.id === next.id ? next : e));
}

export class CommandError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

/** Apply commands to a copy of the content (pure). Throws CommandError for an unknown id / layer / block. */
export function applyLocal(content: DraftContent, commands: DraftCommand[]): DraftContent {
  let c: DraftContent = { ...content, entities: [...content.entities], layers: [...content.layers], sheets: [...content.sheets], provenance: { ...content.provenance } };
  const get = (id: string) => {
    const e = c.entities.find((x) => x.id === id);
    if (!e) throw new CommandError('entity_not_found', id);
    return e;
  };
  for (const cmd of commands) {
    switch (cmd.op) {
      case 'add_entities': {
        const ids = newIds(c, cmd.entities.filter((e) => !e.id).length);
        let k = 0;
        for (const raw of cmd.entities) {
          const e = { layer: '0', ...raw, id: raw.id || ids[k++], origin: 'person', modified: false } as DraftEntity;
          if (c.entities.some((x) => x.id === e.id)) throw new CommandError('duplicate_entity_id', e.id);
          c.entities.push(e);
        }
        c.next_entity_no = nextIdNumber(c);
        break;
      }
      case 'delete_entities': {
        const ids = new Set(cmd.ids);
        c.entities = c.entities.filter((e) => !ids.has(e.id));
        break;
      }
      case 'transform': {
        const ents = cmd.ids.map(get);
        const center: Pt = cmd.center ?? selectionCenter(ents);
        if (cmd.copy) {
          const ids = newIds(c, ents.length);
          ents.forEach((e, i) => c.entities.push({ ...transformEntity(e, cmd.dx ?? 0, cmd.dy ?? 0, cmd.rotate ?? 0, center), id: ids[i], origin: 'person', modified: false, source_ref: null }));
          c.next_entity_no = nextIdNumber(c);
        } else {
          for (const e of ents) c.entities = replace(c.entities, markModified(transformEntity(e, cmd.dx ?? 0, cmd.dy ?? 0, cmd.rotate ?? 0, center)));
        }
        break;
      }
      case 'update_entity':
        c.entities = replace(c.entities, markModified({ ...get(cmd.id), ...cmd.set } as DraftEntity));
        break;
      case 'set_entity_layer':
        for (const id of cmd.ids) c.entities = replace(c.entities, markModified({ ...get(id), layer: cmd.layer }));
        if (!c.layers.some((l) => l.name === cmd.layer)) c.layers.push(defaultLayer(cmd.layer));
        break;
      case 'upsert_layer': {
        const i = c.layers.findIndex((l) => l.name.toUpperCase() === cmd.layer.name.toUpperCase());
        if (i >= 0) c.layers[i] = cmd.layer;
        else c.layers.push(cmd.layer);
        break;
      }
      case 'set_layer_visibility': {
        const i = c.layers.findIndex((l) => l.name === cmd.name);
        if (i < 0) throw new CommandError('layer_not_found', cmd.name);
        c.layers[i] = { ...c.layers[i], visible: cmd.visible };
        break;
      }
      case 'edit_text':
        c.entities = replace(c.entities, markModified({ ...get(cmd.id), text: cmd.text, ...(cmd.height ? { height: cmd.height } : {}) } as DraftEntity));
        break;
      case 'edit_dimension': {
        const e = get(cmd.id);
        c.entities = replace(c.entities, markModified({ ...e, text_override: cmd.text_override || null } as DraftEntity));
        const rec = c.provenance[cmd.id] ?? { unit: 'mm', displayed: '', flags: [] };
        c.provenance[cmd.id] = { ...rec, override: { text: cmd.text_override || null, author: 'person' } };
        break;
      }
      case 'insert_block':
        if (!c.blocks.some((b) => b.name === cmd.block)) throw new CommandError('unknown_block', cmd.block);
        c.entities.push({ id: newIds(c, 1)[0], type: 'insert', block: cmd.block, insert: cmd.insert, rotation: cmd.rotation ?? 0, xscale: cmd.xscale ?? 1, yscale: cmd.yscale ?? 1, layer: cmd.layer ?? '0', origin: 'person' });
        c.next_entity_no = nextIdNumber(c);
        break;
      case 'define_block': {
        const i = c.blocks.findIndex((b) => b.name.toUpperCase() === cmd.block.name.toUpperCase());
        c.blocks = i >= 0 ? c.blocks.map((b, j) => (j === i ? cmd.block : b)) : [...c.blocks, cmd.block];
        break;
      }
      case 'set_title_block':
        c.sheets = c.sheets.map((s) => {
          if (s.id !== (cmd.sheet_id ?? 'S1')) return s;
          const tb: Record<string, string> = { ...s.title_block, ...cmd.fields };
          for (const k of Object.keys(tb)) if (!tb[k]) delete tb[k];
          return { ...s, title_block: tb } as DraftSheet;
        });
        break;
      case 'set_bom':
        c.sheets = c.sheets.map((s) => (s.id === (cmd.sheet_id ?? 'S1') ? { ...s, bom: cmd.rows, ...(cmd.columns ? { bom_columns: cmd.columns } : {}) } : s));
        break;
      case 'set_sheet':
        c.sheets = c.sheets.map((s) => (s.id === (cmd.sheet_id ?? 'S1') ? ({ ...s, ...cmd.set } as DraftSheet) : s));
        break;
      case 'set_underlay':
        c.underlays = c.underlays.map((u) => (u.id === cmd.id ? { ...u, ...cmd.set } : u));
        break;
      case 'resolve_conflict':
        c.conflicts = c.conflicts.map((x) => (x.id === cmd.conflict_id ? { ...x, status: 'resolved', resolution: { by: 'person', value: cmd.value ?? null, note: cmd.note ?? '' } } : x));
        break;
      case 'confirm_assumption':
        c.assumptions = c.assumptions.map((a) =>
          a.id === cmd.assumption_id ? { ...a, confirmation: { author: 'person', by: null, at: null, value: a.adopted, decision: 'confirmed' } } : a,
        );
        break;
      case 'calibrate':
        break; // calibration goes through its own endpoint (a new version right away)
    }
  }
  c = refreshProvenance(c);
  return c;
}

export function defaultLayer(name: string): DraftLayer {
  return { name, color: 7, linetype: 'CONTINUOUS', lineweight: -3, visible: true, locked: false };
}

/** Measured / displayed / consistent of every dimension (server: refresh_provenance). */
export function refreshProvenance(c: DraftContent): DraftContent {
  const prov: DraftContent['provenance'] = {};
  for (const e of c.entities) {
    if (e.type !== 'dimension') continue;
    const d = e as DimensionEntity;
    const rec = c.provenance[d.id] ?? { unit: 'mm' as const, displayed: '', flags: [] };
    const { value, unit } = measure(d);
    const displayed = displayedText(d);
    const consistent = readingConsistent(displayed, value * (unit === 'mm' ? d.dimlfac ?? 1 : 1));
    const flags = rec.flags.filter((f) => f !== 'reading_mismatch');
    if (consistent === false) flags.push('reading_mismatch');
    prov[d.id] = { ...rec, measured: value, unit, displayed, consistent, flags };
  }
  return { ...c, provenance: prov };
}

/** Commands that move an entity by the vector a → b (rigid). */
export function moveCommand(ids: string[], a: Pt, b: Pt, copy = false): DraftCommand {
  return { op: 'transform', ids, dx: b[0] - a[0], dy: b[1] - a[1], copy };
}

export function rotateCommand(ids: string[], center: Pt, degrees: number): DraftCommand {
  return { op: 'transform', ids, rotate: degrees, center };
}

/** A block from the selected entities: copies relative to `base`; the originals stay. */
export function blockFromSelection(name: string, base: Pt, entities: DraftEntity[]): DraftCommand {
  const allowed = new Set(['line', 'polyline', 'circle', 'arc', 'text', 'mtext', 'hatch']);
  const ents = entities
    .filter((e) => allowed.has(e.type))
    .map((e, i) => {
      const moved = transformEntity(e, -base[0], -base[1], 0, [0, 0]);
      const { source_ref: _s, modified: _m, ...rest } = moved as DraftEntity & { source_ref?: unknown; modified?: unknown };
      return { ...rest, id: `B${i + 1}`, origin: 'person' } as DraftEntity;
    });
  return { op: 'define_block', block: { name, base: [0, 0], entities: ents, description: '', origin: 'person' } };
}
