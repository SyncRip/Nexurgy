import { useFeature } from '../../api/capabilities';
// W7 Turbo UI: the explicit execution-policy choice (standard preselected, never carried over silently), the
// assumption list a Turbo plan / model rests on, and the verification chips of runs, nodes and artifacts.
// Execution, engineering check and acceptance stay separate chips (W0 §2.2): a finished Turbo result reads
// "done · not verified", never "accepted" or "passed".

import './turbo.css';
import { Gauge, Info, Zap } from 'lucide-react';
import type { ReactNode } from 'react';
import type { ExecutionPolicy, Verification } from '../../api/engineering';
import { CheckStatusChip, PolicyChip, UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { decisionCounts, decisionLine, decisionView, groupDecisions, type DecisionLike, type DecisionState, type DecisionView } from './decisions';
import { artifactCheckState } from './model';

export const POLICIES: readonly ExecutionPolicy[] = ['standard', 'turbo'];

/** Two cards (Standard / Turbo) with what each one does. Standard is the default; Turbo is always a deliberate click. */
export function PolicySelector({ value, onChange, disabled, note }: { value: ExecutionPolicy; onChange: (p: ExecutionPolicy) => void; disabled?: boolean; note?: string | null }) {
  const { t } = useI18n();
  const enabled = useFeature('turbo_choice');
  if (!enabled) return null;
  return (
    <div className="stack-sm">
      <span className="tk-label">{t('tb.policy.label')}</span>
      <div className="tb-policy" role="radiogroup" aria-label={t('tb.policy.label')}>
        {POLICIES.map((p) => (
          <button
            key={p}
            type="button"
            role="radio"
            aria-checked={value === p}
            className={`tb-policy-card ${p}`}
            disabled={disabled}
            onClick={() => onChange(p)}
          >
            <span className="tb-policy-radio" aria-hidden />
            <span className="tb-policy-title">
              {p === 'turbo' ? <Zap aria-hidden /> : <Gauge aria-hidden />}
              {t(`me.policy.${p}`)}
            </span>
            <span className="tb-policy-desc">{t(`tb.policy.desc.${p}`)}</span>
          </button>
        ))}
      </div>
      {note && (
        <p className="tb-note">
          <Info aria-hidden /> {note}
        </p>
      )}
    </div>
  );
}

/** Compact two-way switch (composer, run buttons); the title explains each choice. */
export function PolicyToggle({ value, onChange, disabled }: { value: ExecutionPolicy; onChange: (p: ExecutionPolicy) => void; disabled?: boolean }) {
  const { t } = useI18n();
  const enabled = useFeature('turbo_choice');
  if (!enabled) return null;
  return (
    <span className="tb-policy-compact" role="radiogroup" aria-label={t('tb.policy.label')}>
      {POLICIES.map((p) => (
        <button
          key={p}
          type="button"
          role="radio"
          aria-checked={value === p}
          className={p}
          disabled={disabled}
          title={t(`me.policy.${p}Hint`)}
          onClick={() => onChange(p)}
        >
          {p === 'turbo' ? <Zap aria-hidden /> : <Gauge aria-hidden />}
          {t(`me.policy.${p}`)}
        </button>
      ))}
    </span>
  );
}

const STATE_TONE: Record<DecisionState, Tone> = { adopted: 'warn', confirmed: 'ok', corrected: 'err', replaced: 'stale', person: 'measure' };

/** One decision (generation supplement D): object, trigger, state, author, the one-line "AI decision: adopt …; reason: …;
 * impact: …", alternatives, sources and what is wrong with the record (kept and shown, never hidden). */
export function DecisionItem({ view, actions }: { view: DecisionView; actions?: ReactNode }) {
  const { t } = useI18n();
  return (
    <li tabIndex={-1} data-assumption-id={view.id ?? undefined} data-state={view.state} data-problems={view.problems.length || undefined}>
      <span className="tb-assumption-head">
        <span className="tb-assumption-subject">{view.subject}</span>
        <Chip tone={view.trigger ? 'accent' : 'pending'} className="sm">
          {t(`tb.decision.trigger.${view.trigger ?? 'unstated'}`)}
        </Chip>
        {view.author !== 'person' && (
          <Chip tone={STATE_TONE[view.state]} className="sm">
            {t(`tb.decision.state.${view.state}`)}
          </Chip>
        )}
        <Chip tone={view.author === 'person' ? 'measure' : 'pending'} className="sm">
          {t(`tb.assumptions.author.${view.author}`)}
        </Chip>
        {actions}
      </span>
      <span className="tb-decision-line">{decisionLine(view, t)}</span>
      {view.alternatives.length ? <span className="tb-assumption-basis">{t('tb.assumptions.alternatives', { list: view.alternatives.join(', ') })}</span> : null}
      {view.sources.length ? <span className="tb-assumption-basis">{t('tb.decision.sources', { list: view.sources.join('；') })}</span> : null}
      {view.problems.length ? (
        <span className="tb-chips">
          {view.problems.map((p) => (
            <Chip key={p} tone="err" className="sm">
              {t(`tb.decision.problem.${p}`)}
            </Chip>
          ))}
        </span>
      ) : null}
    </li>
  );
}

/** The AI / rule decisions a Turbo product rests on, one line each (author AI, never "confirmed by the user"); nothing
 * when empty. Accepts stored assumptions and the server's decision records (``AiDecision``, e.g. a delivery's
 * ``decisions.items``). More than ``collapseAfter`` items are grouped by object, each group collapsible. ``bare`` leaves
 * out the header (a section that has its own); ``actions`` adds per-item controls. */
export function AssumptionList({
  items,
  title,
  bare,
  collapseAfter = 6,
  actions,
}: {
  items: DecisionLike[] | null | undefined;
  title?: string;
  bare?: boolean;
  collapseAfter?: number;
  actions?: (item: DecisionLike, view: DecisionView) => ReactNode;
}) {
  const { t } = useI18n();
  if (!items?.length) return null;
  const views = items.map((a, i) => decisionView(a, i));
  const byKey = new Map(views.map((v, i) => [v.key, items[i]]));
  const counts = decisionCounts(views);
  const row = (v: DecisionView) => <DecisionItem key={v.key} view={v} actions={actions?.(byKey.get(v.key) as DecisionLike, v)} />;
  const groups = views.length > collapseAfter ? groupDecisions(views) : null;
  return (
    <section className="stack-sm" aria-label={title ?? t('tb.assumptions.title')}>
      {!bare && (
        <>
          <div className="row">
            <span className="tk-label grow">{title ?? t('tb.assumptions.title')}</span>
            <span className="faint small">
              {t('tb.assumptions.count', { n: items.length })}
              {counts.withProblems ? ` · ${t('tb.decision.withProblems', { n: counts.withProblems })}` : ''}
            </span>
          </div>
          <p className="tb-note">
            <Info aria-hidden /> {t('tb.assumptions.hint')}
          </p>
        </>
      )}
      {groups && groups.length > 1 ? (
        <div className="tb-decision-groups">
          {groups.map((g, i) => (
            <details key={g.object} className="tb-decision-group" open={i === 0 && views.length <= collapseAfter * 2}>
              <summary>
                <span className="grow">{t('tb.decision.group', { object: g.object, n: g.items.length })}</span>
                {g.problems ? <Chip tone="err" className="sm">{t('tb.decision.withProblems', { n: g.problems })}</Chip> : null}
              </summary>
              <ul className="tb-assumptions">{g.items.map(row)}</ul>
            </details>
          ))}
        </div>
      ) : (
        <ul className="tb-assumptions">{views.map(row)}</ul>
      )}
    </section>
  );
}

/** Policy + verification of a run, node or artifact: PolicyChip for Turbo, then one artifact state (S6 precedence
 * failed > not verified > passed): "check failed" whenever a check failed, "done · not verified" while checks were
 * skipped / not done or a value waits for a person, else the check status itself. Standard rows without a
 * verification block show nothing extra. */
export function VerificationChips({
  verification,
  policy,
  showStandard,
}: {
  verification?: Verification | null;
  policy?: ExecutionPolicy | null;
  showStandard?: boolean;
}) {
  const p = verification?.execution_policy ?? policy ?? 'standard';
  const state = artifactCheckState(verification);
  return (
    <span className="tb-chips">
      {(p === 'turbo' || showStandard) && <PolicyChip policy={p} />}
      {state === 'unverified' && <UnverifiedChip />}
      {state && state !== 'unverified' && <CheckStatusChip status={state} />}
    </span>
  );
}
