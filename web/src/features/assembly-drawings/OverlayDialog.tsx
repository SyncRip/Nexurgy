// Layout / annotation editor of one package version: move a balloon or a dimension text by dx / dy (sheet mm) and
// add notes, saved as the next version of the package. Layout edits never change the model: dimension values,
// quantities and item numbers follow the assembly; changing them means changing the model and regenerating.

import { Info, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { applyLayoutOverlay, type AssemblyDrawingPackage, type DrawingPage } from '../../api/assemblyDrawings';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { badRows, editablePages, overlayEdits, type MoveRow } from './model';

export const OVERLAY_ERRORS = {
  engineering_change_required: 'asmd.err.engineering',
  invalid_layout_overlay: 'asmd.err.overlay',
  package_superseded: 'asmd.err.superseded',
} as const;

function rowsFor(page: DrawingPage | undefined, dimLabel: (kind: string) => string, names: Map<string, string>): MoveRow[] {
  if (!page) return [];
  const all = [...(page.balloons ?? [])].sort((a, b) => a.item.localeCompare(b.item, undefined, { numeric: true }));
  const balloons: MoveRow[] = all.map((b) => ({
    kind: 'balloon',
    ref: b.item,
    occurrence: all.filter((x) => x.item === b.item).length > 1 ? b.occurrence : undefined,
    label: [names.get(b.item), b.occurrence.join(' / ')].filter(Boolean).join(' · '),
    dx: '',
    dy: '',
  }));
  const dims: MoveRow[] = (page.dimensions ?? []).map((d) => ({
    kind: 'dimension',
    ref: d.id,
    label: `${d.text} · ${dimLabel(d.kind)}${d.axis ? ` ${d.axis.toUpperCase()}` : ''} · ${d.view}`,
    dx: '',
    dy: '',
  }));
  return [...balloons, ...dims];
}

export function OverlayDialog({ pkg, onClose, onSaved }: { pkg: AssemblyDrawingPackage; onClose: () => void; onSaved: (next: AssemblyDrawingPackage) => void }) {
  const { t } = useI18n();
  const pages = editablePages(pkg.pages);
  const [pageKey, setPageKey] = useState(() => (pages.find((p) => p.role === 'assembly') ?? pages[0])?.key ?? '');
  const page = pages.find((p) => p.key === pageKey);
  const dimLabel = (kind: string) => t(`asmd.dim.${kind === 'pitch' || kind === 'key' ? kind : 'overall'}`);
  const names = new Map(pkg.pages.flatMap((p) => p.bom_rows ?? []).map((r) => [r.item, r.name]));
  const [rows, setRows] = useState<MoveRow[]>(() => rowsFor(page, dimLabel, names));
  const [notes, setNotes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const bad = badRows(rows);
  const edits = overlayEdits(pageKey, rows, notes);
  const choosePage = (key: string) => {
    setPageKey(key);
    setRows(rowsFor(pages.find((p) => p.key === key), dimLabel, names));
    setNotes([]);
  };
  const set = (i: number, patch: Partial<MoveRow>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const save = async () => {
    if (!edits.length || bad.length) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(await applyLayoutOverlay(pkg.id, { base_manifest_sha256: pkg.manifest_sha256, edits }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('asmd.overlay.title', { label: pkg.label })}
      subtitle={t('asmd.overlay.subtitle', { version: pkg.version + 1 })}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void save()}
      footer={
        <>
          <span className="faint small grow">{t('asmd.overlay.count', { n: edits.length })}</span>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !edits.length || bad.length > 0}>
            {busy ? t('common.saving') : t('asmd.overlay.save')}
          </button>
        </>
      }
    >
      <p className="callout slim info">
        <Info aria-hidden />
        <span>{t('asmd.overlay.layoutOnly')}</span>
      </p>
      {error ? <ErrorNotice error={error} overrides={OVERLAY_ERRORS} /> : null}
      <Field label={t('asmd.overlay.page')}>
        <select className="select" value={pageKey} onChange={(e) => choosePage(e.target.value)}>
          {pages.map((p) => (
            <option key={p.key} value={p.key}>
              {p.key} · {p.title}
            </option>
          ))}
        </select>
      </Field>
      {rows.length === 0 ? (
        <p className="faint small">{t('asmd.overlay.nothingToMove')}</p>
      ) : (
        <div className="table-wrap">
          <table className="t dense asmd-moves">
            <caption className="sr-only">{t('asmd.overlay.moves')}</caption>
            <thead>
              <tr>
                <th>{t('asmd.overlay.what')}</th>
                <th className="r">{t('asmd.overlay.dx')}</th>
                <th className="r">{t('asmd.overlay.dy')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const name = r.kind === 'balloon' ? t('asmd.overlay.balloon', { item: r.ref }) : t('asmd.overlay.dimension', { id: r.ref });
                return (
                  <tr key={`${r.kind}:${r.ref}:${(r.occurrence ?? []).join('/')}`} className={bad.includes(i) ? 'asmd-bad' : undefined}>
                    <td>
                      <span className={r.kind === 'balloon' ? 'asmd-balloon mono' : 'mono'}>{r.kind === 'balloon' ? r.ref : '↔'}</span> {name}
                      {r.label ? <div className="faint small asmd-path">{r.label}</div> : null}
                    </td>
                    <td className="r">
                      <input className="input mono asmd-num" inputMode="decimal" value={r.dx} placeholder="0" aria-label={t('asmd.overlay.dxOf', { what: name })} onChange={(e) => set(i, { dx: e.target.value })} />
                    </td>
                    <td className="r">
                      <input className="input mono asmd-num" inputMode="decimal" value={r.dy} placeholder="0" aria-label={t('asmd.overlay.dyOf', { what: name })} onChange={(e) => set(i, { dy: e.target.value })} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {bad.length > 0 && <p className="asmd-err small">{t('asmd.overlay.badNumber')}</p>}
      <div className="stack-sm">
        <div className="asmd-sub">{t('asmd.overlay.notes')}</div>
        {notes.map((n, i) => (
          <div key={i} className="row asmd-note-row">
            <input className="input grow" value={n} maxLength={120} aria-label={t('asmd.overlay.noteN', { n: i + 1 })} onChange={(e) => setNotes((ns) => ns.map((x, j) => (j === i ? e.target.value : x)))} />
            <button type="button" className="btn ghost icon sm" aria-label={t('common.remove')} onClick={() => setNotes((ns) => ns.filter((_, j) => j !== i))}>
              <Trash2 aria-hidden />
            </button>
          </div>
        ))}
        <button type="button" className="btn sm asmd-add" onClick={() => setNotes((ns) => [...ns, ''])}>
          <Plus aria-hidden /> {t('asmd.overlay.addNote')}
        </button>
        <p className="faint small">{t('asmd.overlay.noteHint')}</p>
      </div>
    </Dialog>
  );
}
