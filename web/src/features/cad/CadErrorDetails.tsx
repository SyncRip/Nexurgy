// Details of a failed CAD operation / import, by error family: the requirement
// check that failed, the annotation items, the STEP audit, the PROFILE problems,
// the DWG converter message. The headline message comes from error.<code>;
// this block only adds the server's evidence (never a success state).

import type { ReactNode } from 'react';
import type { CheckSummary, ProfileReport, StepAudit } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { cadErrorFamily, n } from './status';
import { CheckSummaryView } from './Verdicts';
import './cad.css';

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown) => (typeof v === 'string' ? v : v === null || v === undefined ? '' : JSON.stringify(v));

export function CadErrorDetails({ code, details, action }: { code: string; details: Record<string, unknown> | null | undefined; action?: ReactNode }) {
  const { t } = useI18n();
  const d = details ?? {};
  const family = cadErrorFamily(code);
  switch (family) {
    case 'requirements': {
      const check = isObj(d.check) ? (d.check as unknown as CheckSummary) : null;
      const errors = Array.isArray(d.errors) ? d.errors.map(str).filter(Boolean) : [];
      return (
        <div className="stack-sm">
          {check ? (
            <CheckSummaryView summary={check} acceptance={false} />
          ) : errors.length ? (
            <ul className="cad-list small">
              {errors.slice(0, 5).map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          ) : null}
        </div>
      );
    }
    case 'unsupported':
      return typeof d.reason === 'string' && d.reason ? <p className="small">{t('cad.as.err.reason', { reason: d.reason })}</p> : null;
    case 'simulation':
      return <p className="small">{t('cad.as.err.simHint')}</p>;
    case 'annotation': {
      const report = isObj(d.report) ? d.report : {};
      const items = Array.isArray(report.items) ? (report.items as Obj[]) : [];
      if (!items.length) return null;
      return (
        <div className="small">
          <div className="cad-sub">{t('cad.as.err.reportItems')}</div>
          <ul className="cad-list">
            {items.slice(0, 10).map((it, i) => (
              <li key={i}>
                <span className="mono">{t('cad.dw.reportItem', { page: str(it.page) || '?', check: str(it.check) })}</span> {str(it.message)}
                {it.expected !== undefined && it.expected !== null ? <span className="faint"> · {str(it.expected)} → {str(it.measured)}</span> : null}
              </li>
            ))}
            {items.length > 10 && <li className="faint">… +{items.length - 10}</li>}
          </ul>
        </div>
      );
    }
    case 'step': {
      const a = isObj(d.audit) ? (d.audit as unknown as StepAudit) : null;
      return a ? <StepAuditFacts audit={a} /> : null;
    }
    case 'profile': {
      const p = isObj(d.profile) ? (d.profile as unknown as ProfileReport) : null;
      const problems = p?.problems ?? (p?.refusal ? [p.refusal] : []);
      return (
        <div className="stack-sm small">
          {problems.length > 0 && (
            <div>
              <div className="cad-sub">{t('cad.profile.problems')}</div>
              <ul className="cad-list">
                {problems.slice(0, 8).map((x, i) => (
                  <li key={i}>
                    <code className="error-code">{x.code}</code> {x.message}
                    {x.ids?.length ? <span className="faint mono"> · {x.ids.slice(0, 6).join(', ')}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {code === 'drawing_needs_reconstruct' && action}
        </div>
      );
    }
    case 'dwg': {
      const dwg = isObj(d.dwg) ? d.dwg : {};
      const msg = str(dwg.message ?? dwg.stderr ?? '');
      return msg ? <p className="faint small">{msg.slice(0, 300)}</p> : null;
    }
    default:
      return null;
  }
}

/** Facts of a STEP source audit: verdict, products / solids, unit, originating system. */
export function StepAuditFacts({ audit }: { audit: StepAudit }) {
  const { t } = useI18n();
  const facts = [
    t('cad.audit.facts', { products: audit.products ?? '?', solids: audit.solids ?? '?', valid: audit.valid_solids ?? '?' }),
    audit.length_unit ? t('cad.audit.unit', { unit: audit.length_unit }) : null,
    audit.originating_system ? t('cad.audit.system', { system: audit.originating_system }) : null,
    typeof audit.volume_mm3 === 'number' ? t('cad.audit.volume', { v: n(audit.volume_mm3) }) : null,
  ].filter(Boolean);
  return (
    <div className="small stack-sm">
      <div>
        <strong>{audit.code === 'accepted' ? t('cad.audit.accepted') : t('cad.audit.refused')}</strong>
        {audit.code !== 'accepted' && <code className="error-code"> {audit.code}</code>}
        {audit.message ? <span className="faint"> · {audit.message}</span> : null}
      </div>
      <div className="faint">{facts.join(' · ')}</div>
      {!!audit.product_names?.length && <div className="faint">{t('cad.audit.names', { names: audit.product_names.slice(0, 6).join(', ') })}</div>}
    </div>
  );
}
