// Task engineering baseline (design §6.5): named slots bound to exact asset
// versions. Adopting creates an immutable snapshot and moves the task's
// pointer; the server returns the impact notices it raised. History stays.

import { History, Layers, Pencil, Plus, Trash2, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { adoptBaseline, isMissingVersion, listBaselines, type AssetType, type AssetVersion, type BaselineAdopted, type BaselineSnapshot, type ImpactNotice } from '../../../api/pm';
import { Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { AssetVersionPicker } from '../AssetVersionPicker';
import { Count, dynT, PersonName, TkError, VersionRef, type Members } from '../common';
import { ImpactNoticeList } from '../ImpactNotices';
import { SLOT_PRESETS, SLOT_RE } from '../RuleSpecEditor';
import type { TaskSectionProps } from './shared';
import { LimitedTextarea } from '../../../components/ui/LimitedText';

interface SlotRow {
  key: number;
  slot: string;
  /** Version currently in the baseline for this slot (null = new slot). */
  current: AssetVersion | null;
  currentId: string | null;
  /** Newly chosen version (null = unchanged). */
  next: AssetVersion | null;
  removed: boolean;
}

function slotType(slot: string): AssetType | undefined {
  return SLOT_PRESETS.find((s) => s.slot === slot)?.type;
}

function PickVersionDialog({ projectId, slot, onClose, onPick }: { projectId: string; slot: string; onClose: () => void; onPick: (v: AssetVersion) => void }) {
  const { t } = useI18n();
  const [v, setV] = useState<AssetVersion | null>(null);
  const type = slotType(slot);
  return (
    <Dialog
      title={t('tk.bl.pickTitle', { slot: dynT(t, `tk.slot.${slot}`, slot) })}
      onClose={onClose}
      size="lg"
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn primary" disabled={!v} onClick={() => v && onPick(v)}>
            {t('tk.bl.useVersion')}
          </button>
        </>
      }
    >
      <AssetVersionPicker projectId={projectId} types={type ? [type] : undefined} value={v} onChange={setV} />
    </Dialog>
  );
}

function AdoptDialog({ task, onClose, onDone, onReload }: { task: TaskSectionProps['task']; onClose: () => void; onDone: (r: BaselineAdopted) => void; onReload: () => void }) {
  const { t } = useI18n();
  const [rows, setRows] = useState<SlotRow[]>(() =>
    task.baseline?.items.length
      ? task.baseline.items.map((it, i) => ({
          key: i,
          slot: it.slot,
          current: isMissingVersion(it.version) ? null : it.version,
          currentId: it.asset_version_id,
          next: null,
          removed: false,
        }))
      : // First baseline: start with the target model slot.
        [{ key: 0, slot: 'target_model', current: null, currentId: null, next: null, removed: false }],
  );
  const [seq, setSeq] = useState(rows.length + 1);
  const [picking, setPicking] = useState<number | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const update = (key: number, patch: Partial<SlotRow>) => setRows((xs) => xs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const items: Record<string, string | null> = {};
  for (const r of rows) {
    if (r.currentId && r.removed) items[r.slot] = null;
    else if (r.next && r.next.id !== r.currentId) items[r.slot] = r.next.id;
  }
  const slotNames = rows.map((r) => r.slot.trim());
  const badSlot = rows.some((r) => !r.currentId && (!SLOT_RE.test(r.slot.trim()) || slotNames.filter((s) => s === r.slot.trim()).length > 1));
  const newWithoutVersion = rows.some((r) => !r.currentId && !r.removed && !r.next);
  const nothing = Object.keys(items).length === 0;
  const allRemoved = rows.every((r) => r.removed || (!r.currentId && !r.next));

  const submit = async () => {
    setTouched(true);
    if (badSlot || newWithoutVersion || nothing || allRemoved) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await adoptBaseline(task.id, { items, reason: reason.trim(), expected_version: task.version }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const pickRow = rows.find((r) => r.key === picking);

  return (
    <Dialog
      title={t('tk.bl.adoptTitle')}
      subtitle={t('tk.bl.adoptHint')}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.working') : t('tk.bl.adopt')}
          </button>
        </>
      }
    >
      <div className="stack">
        <ul className="tk-slot-rows">
          {rows.map((r) => {
            const shown = r.next ?? r.current;
            return (
              <li key={r.key} className={r.removed ? 'removed' : ''}>
                {r.currentId ? (
                  <span className="mono strong tk-slot-name">{dynT(t, `tk.slot.${r.slot}`, r.slot)}</span>
                ) : (
                  <input
                    className="input mono tk-slot-input"
                    list="tk-bl-slots"
                    value={r.slot}
                    maxLength={80}
                    placeholder="target_model"
                    aria-label={t('tk.bl.slotName')}
                    aria-invalid={touched && (!SLOT_RE.test(r.slot.trim()) || slotNames.filter((s) => s === r.slot.trim()).length > 1) ? true : undefined}
                    onChange={(e) => update(r.key, { slot: e.target.value.trim() })}
                  />
                )}
                <span className="grow tk-slot-version">
                  {r.removed ? (
                    <span className="faint">{t('tk.bl.willRemove')}</span>
                  ) : shown ? (
                    <>
                      <VersionRef v={shown} link={false} compact />
                      {r.next && r.currentId && <span className="chip accent">{t('tk.bl.changed')}</span>}
                    </>
                  ) : r.currentId ? (
                    <span className="faint">{t('tk.version.missing')}</span>
                  ) : (
                    <span className="faint">{t('tk.bl.noVersionYet')}</span>
                  )}
                </span>
                <span className="row-actions">
                  {!r.removed && (
                    <button type="button" className="btn sm" onClick={() => setPicking(r.key)} disabled={!r.currentId && !SLOT_RE.test(r.slot.trim())}>
                      <Pencil /> {shown ? t('tk.bl.change') : t('tk.bl.choose')}
                    </button>
                  )}
                  {r.currentId ? (
                    <button type="button" className="btn ghost icon sm" onClick={() => update(r.key, { removed: !r.removed, next: null })} aria-label={r.removed ? t('tk.bl.keepSlot', { slot: r.slot }) : t('tk.bl.removeSlot', { slot: r.slot })}>
                      {r.removed ? <Undo2 /> : <Trash2 />}
                    </button>
                  ) : (
                    <button type="button" className="btn ghost icon sm" onClick={() => setRows((xs) => xs.filter((x) => x.key !== r.key))} aria-label={t('tk.bl.removeSlot', { slot: r.slot || '?' })}>
                      <Trash2 />
                    </button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
        <datalist id="tk-bl-slots">
          {SLOT_PRESETS.filter((p) => !slotNames.includes(p.slot)).map((p) => (
            <option key={p.slot} value={p.slot}>
              {dynT(t, `tk.slot.${p.slot}`, p.slot)}
            </option>
          ))}
        </datalist>
        <div>
          <button
            type="button"
            className="btn sm"
            onClick={() => {
              const free = SLOT_PRESETS.find((p) => !slotNames.includes(p.slot))?.slot ?? '';
              setRows((xs) => [...xs, { key: seq, slot: free, current: null, currentId: null, next: null, removed: false }]);
              setSeq((n) => n + 1);
            }}
          >
            <Plus /> {t('tk.bl.addSlot')}
          </button>
        </div>
        {touched && badSlot && <span className="field-error">{t('tk.slotInvalid')}</span>}
        {touched && newWithoutVersion && <span className="field-error">{t('tk.bl.chooseVersions')}</span>}
        {touched && nothing && !badSlot && !newWithoutVersion && <span className="field-error">{t('tk.bl.nothingChanged')}</span>}
        {touched && allRemoved && !nothing && <span className="field-error">{t('tk.bl.cannotBeEmpty')}</span>}
        <Field label={t('tk.reason')} optional hint={t('tk.bl.reasonHint')}>
          <LimitedTextarea className="input" rows={2} limit="description" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <TkError error={error} onReload={onReload} />
      </div>
      {pickRow && (
        <PickVersionDialog
          projectId={task.project_id}
          slot={pickRow.slot}
          onClose={() => setPicking(null)}
          onPick={(v) => {
            update(pickRow.key, { next: v.id === pickRow.currentId ? null : v, removed: false });
            setPicking(null);
          }}
        />
      )}
    </Dialog>
  );
}

function SnapshotItems({ snap }: { snap: BaselineSnapshot }) {
  const { t } = useI18n();
  if (!snap.items.length) return <p className="faint small">{t('tk.bl.emptySnapshot')}</p>;
  return (
    <table className="t dense tk-slot-table">
      <thead>
        <tr>
          <th>{t('tk.bl.slot')}</th>
          <th>{t('tk.bl.version')}</th>
        </tr>
      </thead>
      <tbody>
        {snap.items.map((it) => (
          <tr key={it.slot}>
            <td className="mono small nowrap" title={it.slot}>
              {dynT(t, `tk.slot.${it.slot}`, it.slot)}
            </td>
            <td>
              <VersionRef v={it.version} showEngineering />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SnapshotMeta({ snap, members }: { snap: BaselineSnapshot; members: Members }) {
  const { t, fmt } = useI18n();
  return (
    <p className="faint small">
      {t('tk.bl.adoptedBy')} <PersonName id={snap.adopted_by} members={members} /> · <span title={fmt.dateTime(snap.adopted_at)}>{fmt.relative(snap.adopted_at)}</span> ·{' '}
      <span className="mono" title={snap.hash}>
        #{snap.hash.slice(0, 10)}
      </span>
      {snap.reason && <> · {snap.reason}</>}
    </p>
  );
}

export function BaselineSection({ task, reloadTask, members, projectActive }: TaskSectionProps) {
  const { t } = useI18n();
  const toast = useToast();
  const history = useResource((signal) => listBaselines(task.id, { signal }), [task.id, task.current_baseline_id]);
  const [adopting, setAdopting] = useState(false);
  const [lastImpacts, setLastImpacts] = useState<ImpactNotice[] | null>(null);
  const canAdopt = task.permissions.edit && projectActive && task.status !== 'in_review' && task.status !== 'cancelled';
  const cur = task.baseline;

  return (
    <div className="stack">
      <section className="panel tk-panel" aria-label={t('tk.bl.current')}>
        <div className="panel-h">
          <Layers className="tk-panel-ico" aria-hidden />
          <h3>{t('tk.bl.current')}</h3>
          {cur && <span className="mono faint small">#{cur.seq}</span>}
          <span className="grow" />
          {canAdopt && (
            <button type="button" className="btn sm primary" onClick={() => setAdopting(true)}>
              <Plus /> {cur ? t('tk.bl.adoptNew') : t('tk.bl.adoptFirst')}
            </button>
          )}
        </div>
        <div className="panel-b stack-sm">
          {task.status === 'in_review' && task.permissions.edit && <p className="faint small">{t('tk.bl.inReview')}</p>}
          {task.permissions.edit && !projectActive && <p className="faint small">{t('tk.bl.projectInactive')}</p>}
          {cur ? (
            <>
              <SnapshotMeta snap={cur} members={members} />
              <SnapshotItems snap={cur} />
            </>
          ) : (
            <EmptyState compact title={t('tk.bl.none')} body={t('tk.bl.noneBody')} />
          )}
          <p className="faint small">{t('tk.bl.explain')}</p>
        </div>
      </section>

      {lastImpacts && lastImpacts.length > 0 && (
        <section className="panel tk-panel" aria-label={t('tk.bl.newImpacts')}>
          <div className="panel-h">
            <h3>{t('tk.bl.newImpacts')}</h3>
            <Count n={lastImpacts.length} />
          </div>
          <div className="panel-b">
            <ImpactNoticeList notices={lastImpacts} canResolve={false} onChanged={reloadTask} />
          </div>
        </section>
      )}

      {task.open_impacts.length > 0 && (
        <section className="panel tk-panel" aria-label={t('tk.bl.openImpacts')}>
          <div className="panel-h">
            <h3>{t('tk.bl.openImpacts')}</h3>
            <Count n={task.open_impacts.length} />
          </div>
          <div className="panel-b">
            <ImpactNoticeList notices={task.open_impacts} canResolve={task.permissions.edit} onChanged={reloadTask} />
          </div>
        </section>
      )}

      <section className="panel tk-panel" aria-label={t('tk.bl.history')}>
        <div className="panel-h">
          <History className="tk-panel-ico" aria-hidden />
          <h3>{t('tk.bl.history')}</h3>
          {history.data && <Count n={history.data.length} />}
        </div>
        <div className="panel-b">
          {history.error && !history.data ? (
            <ErrorNotice error={history.error} onRetry={history.reload} />
          ) : !history.data ? (
            <SkeletonRows rows={3} cols={2} />
          ) : !history.data.length ? (
            <p className="faint small">{t('tk.bl.noHistory')}</p>
          ) : (
            <ol className="tk-snapshots">
              {history.data.map((s) => (
                <li key={s.id}>
                  <details open={s.current}>
                    <summary>
                      <span className="mono strong">#{s.seq}</span>
                      {s.current && <span className="chip ok">{t('tk.bl.isCurrent')}</span>}
                      <span className="faint small">{t('tk.bl.slotCount', { n: s.items.length })}</span>
                      {s.reason && <span className="small ellip">{s.reason}</span>}
                    </summary>
                    <div className="stack-sm tk-details-body">
                      <SnapshotMeta snap={s} members={members} />
                      <SnapshotItems snap={s} />
                    </div>
                  </details>
                </li>
              ))}
            </ol>
          )}
        </div>
      </section>

      {adopting && (
        <AdoptDialog
          task={task}
          onClose={() => setAdopting(false)}
          onReload={() => {
            setAdopting(false);
            reloadTask();
          }}
          onDone={(r) => {
            setAdopting(false);
            setLastImpacts(r.impacts);
            toast(r.created ? t('tk.bl.adopted', { n: r.baseline.seq, impacts: r.impacts.length }) : t('tk.bl.unchanged'));
            reloadTask();
            history.reload();
          }}
        />
      )}
    </div>
  );
}
