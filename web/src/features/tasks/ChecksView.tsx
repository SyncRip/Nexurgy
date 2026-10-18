// Renders a submission check report (services/submissions.run_checks): one row
// per acceptance criterion and per delivered version, each with its exact
// pass / fail / warning reasons. Used for the dry run, a refused submission
// (422 submission_checks_failed) and frozen submission snapshots.

import { CircleAlert, CircleCheck, CircleX, Hand, ShieldAlert } from 'lucide-react';
import type { CheckProblem, CheckReport, CriterionCheck, ItemCheck } from '../../api/pm';
import { Chip } from '../../components/ui/Badges';
import { useT, type TFunction } from '../../i18n/I18nProvider';
import { assetTypeLabel, dynT, VersionLabels } from './common';

function stageText(t: TFunction, stage: string | undefined): string {
  return stage ? dynT(t, `tk.stage.${stage}`, stage) : '';
}

/** Human text for one check problem; falls back to the server's English message. */
export function problemText(t: TFunction, p: CheckProblem): string {
  const vars: Record<string, string | number> = {
    label: p.label ?? '',
    stage: stageText(t, p.stage),
    slot: p.slot ? dynT(t, `tk.slot.${p.slot}`, p.slot) : '',
    sources: (p.source_labels ?? []).join(', ') || '?',
    expected: (p.expected_labels ?? []).join(', ') || '?',
    message: p.message,
  };
  if (p.code === 'missing_evidence') {
    const m = /needs (\d+) item\(s\) of type (.+), has (\d+)/.exec(p.message);
    if (m) {
      const types = m[2] === 'any' ? t('tk.check.anyType') : m[2].split(', ').map((x) => assetTypeLabel(t, x)).join(', ');
      return t('tk.check.missing_evidence', { need: m[1], types, has: m[3] });
    }
  }
  return dynT(t, `tk.check.${p.code}`, p.message, vars);
}

function ProblemList({ problems, tone }: { problems: CheckProblem[]; tone: 'err' | 'warn' }) {
  const t = useT();
  if (!problems.length) return null;
  return (
    <ul className={`tk-problems ${tone}`}>
      {problems.map((p, i) => (
        <li key={i} title={p.message}>
          {tone === 'err' ? <CircleX aria-hidden /> : <CircleAlert aria-hidden />}
          <span>
            {problemText(t, p)} <code className="tk-code">{p.code}</code>
          </span>
        </li>
      ))}
    </ul>
  );
}

function CriterionRow({ c, labelOf }: { c: CriterionCheck; labelOf: (id: string) => string }) {
  const t = useT();
  const state = c.ok ? 'ok' : c.blocking ? 'err' : 'warn';
  return (
    <li className={`tk-check-row ${state}`}>
      <span className="tk-check-ico" aria-hidden>
        {state === 'ok' ? <CircleCheck /> : state === 'err' ? <CircleX /> : <CircleAlert />}
      </span>
      <div className="grow tk-xs">
        <div className="row">
          <span className="sr-only">{t(`tk.check.state.${state}`)}</span>
          {c.seq !== null && <span className="mono faint">#{c.seq}</span>}
          <span className="strong">{c.description}</span>
          {c.required ? <Chip tone="accent">{t('tk.crit.required')}</Chip> : <Chip tone="pending">{t('tk.crit.optional')}</Chip>}
          {c.manual_required && (
            <Chip tone="measure" icon={<Hand aria-hidden />} title={t('tk.crit.manualHint')}>
              {t('tk.crit.manual')}
            </Chip>
          )}
        </div>
        <span className="faint small">
          {c.evidence.length ? t('tk.check.evidence', { list: c.evidence.map(labelOf).join(', ') }) : t('tk.check.noEvidence')}
        </span>
        <ProblemList problems={c.problems} tone={c.required ? 'err' : 'warn'} />
      </div>
    </li>
  );
}

function ItemRow({ it }: { it: ItemCheck }) {
  const t = useT();
  const state = !it.ok ? 'err' : it.warnings.length ? 'warn' : 'ok';
  return (
    <li className={`tk-check-row ${state}`}>
      <span className="tk-check-ico" aria-hidden>
        {state === 'ok' ? <CircleCheck /> : state === 'err' ? <CircleX /> : <CircleAlert />}
      </span>
      <div className="grow tk-xs">
        <div className="row">
          <span className="sr-only">{t(`tk.check.state.${state}`)}</span>
          <span className="mono strong">{it.label}</span>
          <span className="faint small">{assetTypeLabel(t, it.asset_type)}</span>
          <VersionLabels labels={it.labels} />
        </div>
        <ProblemList problems={it.problems} tone="err" />
        <ProblemList problems={it.warnings} tone="warn" />
      </div>
    </li>
  );
}

export function CheckReportView({ report, title }: { report: CheckReport; title?: string }) {
  const t = useT();
  const labels = new Map(report.items.map((i) => [i.asset_version_id, i.label]));
  const labelOf = (id: string) => labels.get(id) ?? id.slice(0, 8);
  const blocking = report.criteria.filter((c) => c.blocking).length + report.items.filter((i) => !i.ok).length;
  return (
    <div className="tk-checks stack-sm">
      {title && <h4 className="tk-label">{title}</h4>}
      <div className={`callout slim ${report.ok ? 'ok' : 'err'}`} role="status">
        {report.ok ? <CircleCheck aria-hidden /> : <ShieldAlert aria-hidden />}
        <span>{report.ok ? t('tk.check.allPass') : t('tk.check.blocking', { n: blocking })}</span>
      </div>
      {report.snapshot_tampered && (
        <div className="callout slim err">
          <ShieldAlert aria-hidden />
          <span>{t('tk.check.tampered')}</span>
        </div>
      )}
      {report.criteria.length > 0 && (
        <section className="tk-xs" aria-label={t('tk.check.byCriterion')}>
          <h5 className="tk-sublabel">{t('tk.check.byCriterion')}</h5>
          <ul className="tk-check-list">
            {report.criteria.map((c) => (
              <CriterionRow key={c.criterion_id} c={c} labelOf={labelOf} />
            ))}
          </ul>
        </section>
      )}
      {report.items.length > 0 && (
        <section className="tk-xs" aria-label={t('tk.check.byItem')}>
          <h5 className="tk-sublabel">{t('tk.check.byItem')}</h5>
          <ul className="tk-check-list">
            {report.items.map((i) => (
              <ItemRow key={i.asset_version_id} it={i} />
            ))}
          </ul>
        </section>
      )}
      <p className="faint small mono">{t('tk.check.policy', { v: report.policy_version })}</p>
    </div>
  );
}
