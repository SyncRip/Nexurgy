// Mates tab: the mates of this revision with their solve status / residual, the remaining freedom per instance,
// and (on the latest revision) add / suppress / remove. Datums are semantic names of each definition
// (origin, <op>.axis, <op>.<side>, port.<name>), loaded per instance.

import { Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { getDatums, MATE_KINDS, type AssemblyRevision, type Mate, type MateKind, type MateStatus } from '../../api/assemblies';
import { Chip, type Tone } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssemblyDraft, type EditApi } from './edit';
import { fmtNum } from './model';

const STATUS_TONE: Record<MateStatus, Tone> = {
  pending: 'pending',
  solved: 'ok',
  redundant: 'measure',
  conflict: 'err',
  broken: 'err',
  invalid: 'err',
  dropped: 'warn',
  suppressed: 'pending',
  skipped_turbo: 'warn',
};

export function MateStatusChip({ status }: { status: MateStatus }) {
  const { t } = useI18n();
  const known = status in STATUS_TONE ? status : 'invalid';
  return <Chip tone={STATUS_TONE[known]}>{t(`asm.mates.status.${known}`)}</Chip>;
}

export function MatesTab({ rev, canEdit, edit }: { rev: AssemblyRevision; canEdit: boolean; edit: EditApi }) {
  const { t } = useI18n();
  const [adding, setAdding] = useState(false);
  const names = Object.fromEntries(rev.components.map((c) => [c.instance_id, c.name]));
  const side = (s: Mate['a'] | null) => (s ? `${names[s.instance] ?? s.instance} · ${s.datum}` : '—');
  const solveRows = Object.entries(rev.solve.instances ?? {});
  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-h row">
          <strong>{t('asm.mates.title')}</strong>
          <span className="grow" />
          {canEdit && (
            <button type="button" className="btn sm" onClick={() => setAdding(true)}>
              <Plus aria-hidden /> {t('asm.mates.add')}
            </button>
          )}
        </div>
        {rev.mates.length === 0 ? (
          <p className="panel-b faint small">{t('asm.mates.empty')}</p>
        ) : (
          <div className="table-wrap">
            <table className="t dense">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>{t('asm.mates.kind')}</th>
                  <th>{t('asm.mates.a')}</th>
                  <th>{t('asm.mates.b')}</th>
                  <th>{t('asm.mates.residual')}</th>
                  <th />
                  {canEdit && <th />}
                </tr>
              </thead>
              <tbody>
                {rev.mates.map((m) => (
                  <tr key={m.mate_id}>
                    <td className="mono">{m.mate_id}</td>
                    <td>
                      {t(`asm.mates.kind.${m.kind}`)}
                      {Object.keys(m.params).length > 0 && <span className="mono faint small"> {JSON.stringify(m.params)}</span>}
                    </td>
                    <td className="small">{side(m.a)}</td>
                    <td className="small">{side(m.b)}</td>
                    <td className="mono small">
                      {m.result.residual ? `${fmtNum(m.result.residual.position_mm, 6)} mm · ${fmtNum(m.result.residual.angle_rad, 6)} rad` : '—'}
                    </td>
                    <td>
                      <MateStatusChip status={m.status} />
                      {m.result.message && <div className="faint small">{m.result.message}</div>}
                    </td>
                    {canEdit && (
                      <td className="act">
                        <button type="button" className="btn sm" disabled={edit.busy} onClick={() => void edit.submit([{ op: 'edit_mate', mate_id: m.mate_id, suppressed: !m.suppressed }])}>
                          {m.suppressed ? t('asm.mates.unsuppress') : t('asm.mates.suppress')}
                        </button>{' '}
                        <button type="button" className="btn sm icon" aria-label={t('asm.mates.remove')} title={t('asm.mates.remove')} disabled={edit.busy} onClick={() => void edit.submit([{ op: 'remove_mate', mate_id: m.mate_id }])}>
                          <Trash2 />
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {solveRows.length > 0 && (
        <section className="panel">
          <div className="panel-h">
            <strong>{t('asm.mates.dof')}</strong>
          </div>
          <ul className="panel-b asm-dof">
            {solveRows.map(([iid, info]) => (
              <li key={iid}>
                <span className="mono">{iid}</span> {names[iid] ?? ''} · {t(`asm.solve.${info.status}`)}
                {info.dof !== null && info.dof > 0 ? ` · ${t('asm.props.dofValue', { n: info.dof })}` : ''}
                {info.over_constrained ? ` · ${t('asm.solve.over')}` : ''}
              </li>
            ))}
          </ul>
        </section>
      )}
      {adding && <AddMateDialog rev={rev} edit={edit} onClose={() => setAdding(false)} />}
    </div>
  );
}

function DatumSelect({ rev, instance, value, onChange, label }: { rev: AssemblyRevision; instance: string; value: string; onChange: (v: string) => void; label: string }) {
  const { t } = useI18n();
  const res = useResource((signal) => getDatums(rev.id, instance, { signal }), [rev.id, instance], { enabled: !!instance });
  return (
    <Field label={label} hint={res.loading ? t('asm.mates.datumLoading') : res.data ? t(res.data.source === 'w3' ? 'asm.mates.source.w3' : 'asm.mates.source.fallback') : undefined}>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)} disabled={!instance}>
        <option value="">—</option>
        {(res.data?.datums ?? []).map((d) => (
          <option key={d.name} value={d.name}>
            {d.name} ({d.kind})
          </option>
        ))}
      </select>
    </Field>
  );
}

function AddMateDialog({ rev, edit, onClose }: { rev: AssemblyRevision; edit: EditApi; onClose: () => void }) {
  useAssemblyDraft(edit);
  const { t } = useI18n();
  const [kind, setKind] = useState<MateKind>('planar');
  const [a, setA] = useState('');
  const [da, setDa] = useState('');
  const [b, setB] = useState('');
  const [db, setDb] = useState('');
  const [distance, setDistance] = useState('0');
  const [angle, setAngle] = useState('90');
  const [flip, setFlip] = useState(false);
  const [priority, setPriority] = useState('100');
  const oneSided = kind === 'fixed' && !b;
  const ok = !!a && !!da && (oneSided || (!!b && !!db && b !== a));
  const params: Record<string, unknown> = {};
  if (kind === 'offset') params.distance = Number(distance);
  if (kind === 'planar' && Number(distance) !== 0) params.offset = Number(distance);
  if (kind === 'angle') params.angle = Number(angle);
  if ((kind === 'planar' || kind === 'offset') && flip) params.flip = true;
  const comps = rev.components.filter((c) => !c.suppressed);
  return (
    <Dialog
      title={t('asm.mates.add')}
      onClose={onClose}
      size="lg"
      onSubmit={async () => {
        if (!ok) return;
        onClose();
        await edit.submit(
          [{ op: 'add_mate', kind, a: { instance: a, datum: da }, b: oneSided ? null : { instance: b, datum: db }, params, priority: Number(priority) || 100 }],
          { preview: false },
        );
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ok}>
            {t('asm.edit.apply')}
          </button>
        </>
      }
    >
      <div className="form-grid">
        <Field label={t('asm.mates.kind')} hint={kind === 'fixed' ? t('asm.mates.groundHint') : undefined}>
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value as MateKind)}>
            {MATE_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`asm.mates.kind.${k}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('asm.mates.priority')}>
          <input className="input mono" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} />
        </Field>
        <Field label={t('asm.mates.a')}>
          <select className="select" value={a} onChange={(e) => (setA(e.target.value), setDa(''))}>
            <option value="">—</option>
            {comps.map((c) => (
              <option key={c.instance_id} value={c.instance_id}>
                {c.instance_id} · {c.name}
              </option>
            ))}
          </select>
        </Field>
        <DatumSelect rev={rev} instance={a} value={da} onChange={setDa} label={t('asm.mates.datum')} />
        <Field label={t('asm.mates.b')} optional={kind === 'fixed'}>
          <select className="select" value={b} onChange={(e) => (setB(e.target.value), setDb(''))}>
            <option value="">—</option>
            {comps
              .filter((c) => c.instance_id !== a)
              .map((c) => (
                <option key={c.instance_id} value={c.instance_id}>
                  {c.instance_id} · {c.name}
                </option>
              ))}
          </select>
        </Field>
        <DatumSelect rev={rev} instance={b} value={db} onChange={setDb} label={t('asm.mates.datum')} />
        {(kind === 'offset' || kind === 'planar') && (
          <Field label={t('asm.mates.distance')}>
            <input className="input mono" inputMode="decimal" value={distance} onChange={(e) => setDistance(e.target.value)} />
          </Field>
        )}
        {kind === 'angle' && (
          <Field label={t('asm.mates.angle')}>
            <input className="input mono" inputMode="decimal" value={angle} onChange={(e) => setAngle(e.target.value)} />
          </Field>
        )}
        {(kind === 'planar' || kind === 'offset') && (
          <label className="check span-2">
            <input type="checkbox" checked={flip} onChange={(e) => setFlip(e.target.checked)} /> {t('asm.mates.flip')}
          </label>
        )}
      </div>
    </Dialog>
  );
}
