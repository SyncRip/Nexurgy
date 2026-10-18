import { notifyTodosChanged } from '../todos/events';
// Verification of one exact artifact (model revision / drawing package) and what a person can do about it (W7,
// AC-T08 + W0 §2.2): policy + "done · not verified" / check status chips, the per-check statuses, the checks still
// to pass, an unverified source revision, the AI / rule assumptions waiting for a person ("Confirm assumptions") and buttons
// that run the normal checks on these exact bytes. Every action appends a record; the skipped record made at
// creation stays as it was. The artifact is verified only when every check passed and a person confirmed every
// assumption; a wrong value is an engineering change (a new revision), never edited here.

import './turbo.css';
import { CheckCheck, Info, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import type { AssumptionRef, CheckStatus } from '../../api/engineering';
import {
  confirmDrawingAssumption,
  confirmRevisionAssumption,
  runDrawingCheck,
  runRevisionCheck,
  type ArtifactCheckResult,
  type ArtifactVerification,
  type DrawingCheckName,
  type RevisionCheckName,
} from '../../api/turbo';
import type { UUID } from '../../api/types';
import { CheckStatusChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { formatAdopted, needsVerification } from './model';
import { AssumptionList, VerificationChips } from './Turbo';

function ownerRev(a: AssumptionRef): number | null {
  const o = a.owner;
  return o && typeof o === 'object' && o.type === 'model_revision' && typeof o.rev_no === 'number' ? o.rev_no : null;
}

export function ArtifactVerificationPanel({
  kind,
  id,
  verification,
  canCheck,
  hasDwg,
}: {
  kind: 'revision' | 'drawing';
  id: UUID;
  verification: ArtifactVerification | null | undefined;
  /** Whether a standard check can run here (a requirement list / a package); the server decides permission. */
  canCheck: boolean;
  hasDwg?: boolean;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const [current, setCurrent] = useState<ArtifactVerification | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const v = current ?? verification ?? null;
  // Shown whenever there is something to verify: Turbo origin, open checks, undecided assumptions or an
  // unverified source (a standard drawing of a Turbo revision too).
  if (!v || !needsVerification(v)) return null;

  const act = async (key: string, call: () => Promise<ArtifactCheckResult>, done: (out: ArtifactCheckResult) => string) => {
    setBusy(key);
    setError(null);
    try {
      const out = await call();
      setCurrent(out.verification);
      notifyTodosChanged();
      toast(done(out));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const run = (check: RevisionCheckName | DrawingCheckName) =>
    act(check, () => (kind === 'revision' ? runRevisionCheck(id, check as RevisionCheckName) : runDrawingCheck(id, check as DrawingCheckName)),
      (out) => t('tb.check.recorded', { status: t(`me.check.${out.record.status}`) }));
  const confirm = (a: AssumptionRef) =>
    act(`confirm:${a.id}`, () => (kind === 'revision' ? confirmRevisionAssumption(id, a.id) : confirmDrawingAssumption(id, a.id)),
      () => t('tb.confirm.recorded', { id: a.id }));

  const checks = Object.entries(v.checks ?? {}) as [string, CheckStatus][];
  const pending = v.pending_checks ?? [];
  const unconfirmed = v.unconfirmed_assumptions ?? [];
  const corrected = v.corrected_assumptions ?? [];
  const satisfied = Object.keys(v.satisfied_checks ?? {});
  const up = v.upstream_unverified;
  const buttons: { check: RevisionCheckName | DrawingCheckName; label: string }[] = [];
  if (kind === 'revision') {
    if (canCheck && pending.includes('model.conformance')) buttons.push({ check: 'model.conformance', label: t('tb.check.run') });
    if (pending.some((c) => c.startsWith('geometry.feature.'))) buttons.push({ check: 'geometry.features', label: t('tb.check.runFeatures') });
  } else if (canCheck) {
    if (pending.includes('drawing.annotation')) buttons.push({ check: 'drawing.annotation', label: t('tb.check.run') });
    if (hasDwg && pending.includes('drawing.dwg_semantic')) buttons.push({ check: 'drawing.dwg_semantic', label: t('tb.check.runDwg') });
  }

  return (
    <section className="stack-sm" aria-label={t('tb.check.title')}>
      <VerificationChips verification={v} />
      {checks.length > 0 && (
        <span className="tb-chips">
          {checks.map(([name, status]) => (
            <CheckStatusChip key={name} status={status} label={name} />
          ))}
        </span>
      )}
      {up && (
        <p className="tb-note">
          <Info aria-hidden /> {t('tb.verify.upstream', { rev: up.rev_no ?? '?' })}
        </p>
      )}
      {pending.length > 0 && <p className="tb-note">{t('tb.verify.pending', { list: pending.join(', ') })}</p>}
      {satisfied.length > 0 && <p className="tb-note">{t('tb.verify.satisfied', { list: satisfied.join(', ') })}</p>}
      {buttons.length > 0 && (
        <div className="stack-sm">
          <p className="tb-note">{t('tb.check.hint')}</p>
          <span className="tb-chips">
            {buttons.map((b) => (
              <button key={b.check} type="button" className="btn sm" disabled={!!busy} onClick={() => void run(b.check)}>
                <ShieldCheck /> {busy === b.check ? t('tb.check.running') : b.label}
              </button>
            ))}
          </span>
        </div>
      )}
      {(unconfirmed.length > 0 || corrected.length > 0) && (
        <section className="stack-sm" aria-label={t('tb.confirm.title')}>
          <span className="tk-label">{t('tb.confirm.title')}</span>
          <p className="tb-note">
            <Info aria-hidden /> {t('tb.confirm.hint')}
          </p>
          <ul className="tb-assumptions">
            {unconfirmed.map((a) => {
              const rev = kind === 'drawing' ? ownerRev(a) : null;
              return (
                <li key={`u-${a.id}-${rev ?? ''}`} tabIndex={-1} data-assumption-id={a.id}>
                  <span className="tb-assumption-head">
                    <span className="tb-assumption-subject">{a.subject}</span>
                    <span className="tb-assumption-value">{formatAdopted({ adopted: a.adopted, unit: a.unit ?? null }) ?? t('tb.assumptions.open')}</span>
                    {a.author && (
                      <Chip tone="warn" className="sm">
                        {t(`tb.assumptions.author.${a.author}`)}
                      </Chip>
                    )}
                    <button type="button" className="btn sm" disabled={!!busy} onClick={() => void confirm(a)}>
                      <CheckCheck /> {busy === `confirm:${a.id}` ? t('tb.confirm.running') : t('tb.confirm.action')}
                    </button>
                  </span>
                  {rev !== null && <span className="tb-assumption-basis">{t('tb.confirm.ofSource', { rev })}</span>}
                </li>
              );
            })}
            {corrected.map((a) => (
              <li key={`c-${a.id}`}>
                <span className="tb-assumption-head">
                  <span className="tb-assumption-subject">{a.subject}</span>
                  <Chip tone="err" className="sm">
                    {t('tb.confirm.corrected', { value: formatAdopted({ adopted: a.value, unit: a.unit ?? null }) ?? '—' })}
                  </Chip>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {error ? <ErrorNotice error={error} /> : null}
      <AssumptionList items={v.assumptions} />
    </section>
  );
}
