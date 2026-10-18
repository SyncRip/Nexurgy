// Internal steps of a node run (AI: prompt / model_call / validation / build /
// repair; handlers: import / drawing; people: decision / clarification /
// manual_entry), grouped by attempt, each expandable to its detail JSON.

import { ChevronRight, CircleCheck, CircleX } from 'lucide-react';
import { useState } from 'react';
import type { NodeStep } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import type { TFunction } from '../../../i18n/I18nProvider';
import { stepKindLabel } from '../labels';
import { formatDuration } from '../model';
import { SimulatedBadge } from './Artifacts';

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const n = (v: unknown) => (typeof v === 'number' ? v : null);
const str = (v: unknown) => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v));

function summary(t: TFunction, step: NodeStep): string {
  const d = isObj(step.detail) ? step.detail : {};
  const parts: string[] = [];
  switch (step.kind) {
    case 'prompt':
      if (d.mode) parts.push(t('wf.stepSum.mode', { mode: str(d.mode) }));
      if (d.model_id) parts.push(str(d.model_id));
      if (n(d.request_chars) !== null) parts.push(t('wf.stepSum.chars', { n: n(d.request_chars)! }));
      break;
    case 'model_call':
      if (d.provider || d.model_id) parts.push([str(d.provider), str(d.model_id)].filter(Boolean).join(' / '));
      if (n(d.latency_ms) !== null) parts.push(formatDuration(n(d.latency_ms)!));
      if (n(d.tokens_in) !== null || n(d.tokens_out) !== null) parts.push(t('wf.stepSum.tokens', { in: n(d.tokens_in) ?? 0, out: n(d.tokens_out) ?? 0 }));
      if (d.error) parts.push(str(d.error));
      break;
    case 'validation':
      if (n(d.ops) !== null) parts.push(t('wf.stepSum.ops', { n: n(d.ops)! }));
      if (n(d.problem_count) !== null) parts.push(t('wf.stepSum.problems', { n: n(d.problem_count)! }));
      if (d.status) parts.push(str(d.status));
      break;
    case 'build': {
      const m = isObj(d.metrics) ? d.metrics : {};
      if (Array.isArray(m.bbox_size)) parts.push(`${(m.bbox_size as number[]).map((x) => Math.round(x * 100) / 100).join(' × ')} mm`);
      if (Array.isArray(d.errors) && d.errors.length) parts.push(t('wf.stepSum.errors', { n: d.errors.length }));
      break;
    }
    case 'repair':
      if (d.reason) parts.push(str(d.reason));
      if (Array.isArray(d.problems)) parts.push(t('wf.stepSum.problems', { n: d.problems.length }));
      break;
    case 'import':
      if (d.file) parts.push(str(d.file));
      if (d.revision !== undefined) parts.push(t('wf.revisionLabel', { n: str(d.revision) }));
      if (d.mesh_only === true) parts.push(t('rev.meshOnly'));
      break;
    case 'drawing':
      if (d.revision !== undefined) parts.push(t('wf.revisionLabel', { n: str(d.revision) }));
      if (n(d.views) !== null) parts.push(t('wf.stepSum.views', { n: n(d.views)! }));
      break;
    case 'decision':
    case 'clarification':
    case 'manual_entry':
      if (d.by) parts.push(str(d.by));
      if (d.decision) parts.push(t(d.decision === 'rejected' ? 'wf.decision.rejected' : 'wf.decision.approved'));
      if (Array.isArray(d.answers)) parts.push(t('wf.stepSum.answers', { n: d.answers.length }));
      if (d.comment) parts.push(`“${str(d.comment).slice(0, 80)}”`);
      break;
    default:
      break;
  }
  return parts.join(' · ');
}

const MAX_JSON = 20000;

function StepRow({ step }: { step: NodeStep }) {
  const { t, fmt } = useI18n();
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const when = step.at ?? step.started_at;
  const json = open ? JSON.stringify(step.detail ?? {}, null, 2) : '';
  const sim = isObj(step.detail) && step.detail.simulated === true;
  return (
    <li className={`wf-step ${step.ok ? 'ok' : 'bad'} ${open ? 'open' : ''}`}>
      <button type="button" className="wf-step-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <ChevronRight aria-hidden className="wf-step-chev" />
        {step.ok ? <CircleCheck aria-hidden className="wf-step-ok" /> : <CircleX aria-hidden className="wf-step-bad" />}
        <span className="wf-step-kind">{stepKindLabel(t, step.kind)}</span>
        <span className="wf-step-sum ellip">{summary(t, step)}</span>
        {sim && <SimulatedBadge />}
        <span className="wf-step-time mono faint">
          {when ? fmt.time(when) : ''}
          {typeof step.duration_ms === 'number' && step.duration_ms > 0 ? ` · ${formatDuration(step.duration_ms)}` : ''}
        </span>
      </button>
      <div className={`pr-expand ${open ? 'is-open' : ''}`}>
        <div>
          {open && (
            <div className="wf-step-body">
              <pre className="wf-json">{all || json.length <= MAX_JSON ? json : `${json.slice(0, MAX_JSON)}\n…`}</pre>
              {json.length > MAX_JSON && !all && (
                <button type="button" className="btn ghost sm" onClick={() => setAll(true)}>
                  {t('wf.steps.showAll', { n: json.length })}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

export function StepList({ steps }: { steps: NodeStep[] }) {
  const { t } = useI18n();
  if (!steps.length) return <p className="faint small">{t('wf.steps.none')}</p>;
  const attempts = new Map<number, NodeStep[]>();
  for (const st of steps) {
    const a = typeof st.attempt === 'number' ? st.attempt : 0;
    attempts.set(a, [...(attempts.get(a) ?? []), st]);
  }
  const keys = [...attempts.keys()].sort((a, b) => a - b);
  return (
    <div className="stack-sm">
      {keys.map((a) => (
        <section key={a} className="wf-attempt">
          {keys.length > 1 || a > 0 ? <h4 className="wf-attempt-h">{a > 0 ? t('wf.steps.attempt', { n: a }) : t('wf.steps.afterRun')}</h4> : null}
          <ol className="wf-steps">
            {attempts.get(a)!.map((st, i) => (
              <StepRow key={i} step={st} />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
