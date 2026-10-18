// Checks tab: each engineering check of this exact revision with its own status chip (a skipped or missing check
// never reads as passed), the findings (interference pairs and volumes, contacts, clearance, mate problems,
// BOM / spec problems), notes and the assumptions to confirm (S5). Standard runs real checks; on a Turbo revision a
// standard run appends real results while the skipped records stay in history.

import { useState } from 'react';
import { runChecks, type AssemblyRevision, type CheckRecord } from '../../api/assemblies';
import { CheckStatusChip, PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { unverifiedBesideCheck } from '../turbo/model';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { AssemblyAssumptionsPanel } from './AssumptionsPanel';
import { checkRows, fmtNum, safeCheckStatus } from './model';

type Pair = { a: { names: string[] }; b: { names: string[] }; volume_mm3?: number; distance_mm?: number; required_mm?: number };
const nm = (p: { names: string[] }) => p.names.join(' / ');
const SHOWN = 30;

export function ChecksTab({ rev, projectId, canRun, onUpdated, onReload }: { rev: AssemblyRevision; projectId: string; canRun: boolean; onUpdated: (r: AssemblyRevision) => void; onReload?: () => void }) {
  const { t } = useI18n();
  const [clearance, setClearance] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async (policy?: 'standard') => {
    setBusy(true);
    setError(null);
    try {
      const c = clearance.trim() === '' ? null : Number(clearance);
      onUpdated(await runChecks(rev.id, { execution_policy: policy, clearance_mm: c !== null && Number.isFinite(c) ? c : null }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-h row">
          <strong>{t('asm.checks.title')}</strong>
          <span className="grow" />
          <span className="faint small">{t('asm.checks.overall')}</span>
          <CheckStatusChip status={safeCheckStatus(rev.check_status)} />
          <PolicyChip policy={rev.execution_policy} />
          {unverifiedBesideCheck(rev.verification, rev.check_status) && <UnverifiedChip />}
        </div>
        {canRun && (
          <div className="panel-b row asm-run">
            <Field label={t('asm.checks.clearance')} hint={t('asm.checks.clearanceHint')} optional>
              <input className="input mono" inputMode="decimal" value={clearance} onChange={(e) => setClearance(e.target.value)} placeholder="0.1" />
            </Field>
            <button type="button" className="btn primary" disabled={busy} onClick={() => void run()}>
              {busy ? t('asm.checks.running') : t('asm.checks.run')}
            </button>
            {rev.execution_policy === 'turbo' && (
              <button type="button" className="btn" disabled={busy} onClick={() => void run('standard')}>
                {t('asm.checks.runStandard')}
              </button>
            )}
          </div>
        )}
        {error ? <ErrorNotice error={error} /> : null}
        <ul className="asm-checks">
          {checkRows(rev).map(({ name, record }) => (
            <li key={name}>
              <div className="row">
                <strong>{t(`asm.checks.name.${name}` as 'asm.checks.name.assembly.bom')}</strong>
                <CheckStatusChip status={record ? safeCheckStatus(record.status) : 'not_requested'} />
                {!record && <span className="faint small">{t('asm.checks.notRun')}</span>}
                {rev.check_history[name] > 1 && <span className="faint small">{t('asm.checks.history', { n: rev.check_history[name] })}</span>}
              </div>
              {record && <Findings name={name} record={record} />}
            </li>
          ))}
        </ul>
      </section>
      {(rev.notes.length > 0 || (rev.solve.notes ?? []).length > 0) && (
        <section className="panel">
          <div className="panel-h">
            <strong>{t('asm.notes.title')}</strong>
          </div>
          <ul className="panel-b small stack-sm">
            {[...rev.notes, ...(rev.solve.notes ?? [])].map((n, i) => (
              <li key={i}>
                <span className="mono faint">{n.code}</span> {n.message}
              </li>
            ))}
          </ul>
        </section>
      )}
      {/* S5: Confirm assumptions of this exact revision (its own, its spec's and its parts' values) under the W0 §2.2 rule */}
      <AssemblyAssumptionsPanel key={`${rev.id}:${rev.check_status}`} rev={rev} projectId={projectId} canConfirm={canRun} onChanged={onReload} />
    </div>
  );
}

function Findings({ name, record }: { name: string; record: CheckRecord }) {
  const { t } = useI18n();
  const r = record as CheckRecord & Record<string, unknown>;
  if (name === 'assembly.interference') {
    const clashes = (r.interferences as Pair[] | undefined) ?? [];
    return (
      <div className="small stack-sm">
        {typeof r.pairs_checked === 'number' && (
          <span className="faint">{t('asm.checks.pairs', { checked: r.pairs_checked as number, candidates: r.candidate_pairs as number, s: r.elapsed_s as number })}</span>
        )}
        {r.budget_exceeded ? <p className="callout slim warn">{t('asm.checks.budget')}</p> : null}
        {clashes.length > 0 && (
          <ul className="asm-findings">
            {clashes.slice(0, SHOWN).map((p, i) => (
              <li key={i}>{t('asm.checks.clash', { a: nm(p.a), b: nm(p.b), v: fmtNum(p.volume_mm3 ?? 0, 3) })}</li>
            ))}
            {Number(r.interference_count ?? clashes.length) > SHOWN && <li className="faint">… {Number(r.interference_count ?? clashes.length) - SHOWN}</li>}
          </ul>
        )}
        {typeof r.contact_count === 'number' && r.contact_count > 0 && <span className="faint">{t('asm.checks.contacts', { n: r.contact_count as number })}</span>}
        {Array.isArray(r.errors) && r.errors.length > 0 && <span className="faint">{t('asm.checks.errors', { n: r.errors.length })}</span>}
      </div>
    );
  }
  if (name === 'assembly.clearance') {
    const v = (r.violations as Pair[] | undefined) ?? [];
    return v.length ? (
      <ul className="asm-findings small">
        {v.map((p, i) => (
          <li key={i}>{t('asm.checks.tight', { a: nm(p.a), b: nm(p.b), d: fmtNum(p.distance_mm ?? 0, 4), req: p.required_mm ?? 0 })}</li>
        ))}
      </ul>
    ) : null;
  }
  if (name === 'assembly.mate_residual') {
    const probs = (r.problems as { mate_id: string; status: string; message?: string }[] | undefined) ?? [];
    const under = (r.under_constrained as { instance: string; dof: number }[] | undefined) ?? [];
    const unplaced = (r.unplaced as string[] | undefined) ?? [];
    return (
      <div className="small stack-sm">
        {probs.length > 0 && (
          <ul className="asm-findings">
            {probs.map((p) => (
              <li key={p.mate_id}>
                <span className="mono">{p.mate_id}</span> {t(`asm.mates.status.${p.status}` as 'asm.mates.status.conflict')} {p.message ?? ''}
              </li>
            ))}
          </ul>
        )}
        {under.length > 0 && <span className="faint">{t('asm.checks.under', { list: under.map((u) => `${u.instance} (${u.dof})`).join(', ') })}</span>}
        {unplaced.length > 0 && <span className="faint">{t('asm.checks.unplaced', { list: unplaced.join(', ') })}</span>}
      </div>
    );
  }
  const problems = (r.problems as { code: string; message: string }[] | undefined) ?? [];
  return problems.length ? (
    <ul className="asm-findings small">
      {problems.map((p, i) => (
        <li key={i}>
          <span className="mono faint">{p.code}</span> {p.message}
        </li>
      ))}
    </ul>
  ) : null;
}
