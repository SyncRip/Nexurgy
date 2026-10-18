// The execution process of a run (design: .trellis/tasks/10-17-agent-progress-visibility/design.md §3): its stages,
// each stage's work units (part definitions, spec chunks …) with their models, calls and decisions. `compact` sits in
// the conversation card (one summary line once the run succeeded, open while it runs or after a failure); `full`
// on the run page (always open). Status is an icon plus text, never colour alone.

import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Circle, Loader2, PauseCircle, XCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { ProgressDecision, ProgressStage, ProgressUnit, RunProgress } from '../../api/progress';
import type { Tone } from '../../components/ui/Badges';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from '../agent-team/AgentRole';
import { roleForNode } from '../agent-team/roles';
import { AssumptionList } from '../turbo/Turbo';
import { decisionProblems, type DecisionLike } from '../turbo/decisions';
import { nodeDisplayName, nodeStatusLabel } from '../workflow/labels';
import { formatDuration } from '../workflow/model';
import { CallDetails } from './CallDetails';
import { failureLabel, translated as tk, unitLabel } from './labels';
import { useDocumentVisible } from './useDocumentVisible';
import { useProgressDisclosure } from './useProgressDisclosure';
import {
  compactTokens,
  elapsedMs,
  isTerminalProgress,
  isLegacyModelNote,
  previewUnits,
  reconcileDisclosure,
  stageFailures,
  stageOpenByDefault,
  stageTone,
  summarize,
  totalTokens,
  unitFailed,
  unitTone,
  visibleUnits,
  type DisclosureState,
} from './model';
import './progress.css';

const COMPACT_UNITS = 12;

function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, [live]);
  return now;
}

function StatusIcon({ tone }: { tone: Tone }) {
  if (tone === 'ok') return <CheckCircle2 className="prog-ico ok" aria-hidden />;
  if (tone === 'err') return <XCircle className="prog-ico err" aria-hidden />;
  if (tone === 'warn') return <PauseCircle className="prog-ico warn" aria-hidden />;
  if (tone === 'accent') return <Loader2 className="prog-ico accent spin-icon" aria-hidden />;
  return <Circle className="prog-ico pending" aria-hidden />;
}

const dur = (ms: number | null | undefined) => (ms === null || ms === undefined ? '' : formatDuration(ms));

export function ProgressTimeline({ progress, variant = 'compact', expanded, onExpandedChange }: {
  progress: RunProgress;
  variant?: 'compact' | 'full';
  expanded?: boolean;
  onExpandedChange?: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const terminal = isTerminalProgress(progress);
  const disclosure = useProgressDisclosure(progress.run_id, progress, !terminal);
  const open = variant === 'full' || (expanded ?? disclosure.expanded);
  const setOpen = onExpandedChange ?? disclosure.setExpanded;
  const visible = useDocumentVisible();
  const now = useNow(!terminal && open && visible);
  const sum = summarize(progress);
  const parts = [
    sum.parts > 0 ? t('prog.sum.parts', { built: sum.partsBuilt, total: sum.parts }) : null,
    t('prog.sum.calls', { n: sum.calls }),
    sum.failedCalls > 0 ? t('prog.sum.failedCalls', { n: sum.failedCalls }) : null,
    tokenSummary(t, sum.tokens, sum.calls),
    dur(elapsedMs(progress, now)),
    sum.assumptions > 0 ? t('prog.sum.assumptions', { n: sum.assumptions }) : null,
    sum.checksSkipped ? t('prog.sum.checksSkipped') : null,
    progress.simulated ? t('prog.sum.simulated') : null,
  ].filter(Boolean);
  return (
    <section className={`prog prog-${variant}`} aria-label={t('prog.title')}>
      <div className="prog-h">
        <span className="prog-title">{t('prog.title')}</span>
        <span className="prog-sum">{parts.join(' · ')}</span>
        {variant === 'compact' && (
          <button type="button" className="btn sm ghost prog-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />} {open ? t('prog.hide') : t('prog.show')}
          </button>
        )}
      </div>
      {open && (
        <ol className="prog-stages">
          {progress.stages.map((s) => (
            <StageRow key={`${progress.run_id}/${s.node_id}`} stage={s} now={now} variant={variant} />
          ))}
        </ol>
      )}
      {open && !terminal && <p className="prog-note">{t('prog.liveNote')}</p>}
      {open && progress.truncated && <p className="prog-note">{t('prog.truncated')}</p>}
    </section>
  );
}

function StageRow({ stage, now, variant }: { stage: ProgressStage; now: number; variant: 'compact' | 'full' }) {
  const { t } = useI18n();
  const role = roleForNode(stage.node_type);
  const units = visibleUnits(stage);
  const [saved, setSaved] = useState<DisclosureState | null>(null);
  const disclosure = reconcileDisclosure(saved, {
    identity: `${stage.node_id}/${stage.attempt}`,
    status: stage.status,
    failures: stageFailures(stage),
  }, variant === 'full' || stageOpenByDefault(stage));
  if (saved !== disclosure) setSaved(disclosure);
  const open = disclosure.open;
  const [all, setAll] = useState(variant === 'full');
  const shown = all ? units : previewUnits(units, COMPACT_UNITS);
  const tone = stageTone(stage.status);
  const meta = [
    nodeStatusLabel(t, stage.status),
    stage.started_at ? dur(elapsedMs(stage, now)) : null,
    stage.calls.count > 0 ? t('prog.unit.calls', { n: stage.calls.count }) : null,
    tokenSummary(t, totalTokens(stage.calls), stage.calls.count),
  ].filter(Boolean);
  return (
    <li className={`prog-stage st-${stage.status}`} data-node-id={stage.node_id}>
      <div className="prog-row">
        <StatusIcon tone={tone} />
        {role && <AgentRoleBadge role={role} compact />}
        {units.length > 0 ? (
          <button type="button" className="prog-stage-name linkish" aria-expanded={open} onClick={() => setSaved({ ...disclosure, open: !open })}>
            {nodeDisplayName(t, { type: stage.node_type, label: stage.label })}
          </button>
        ) : (
          <span className="prog-stage-name">{nodeDisplayName(t, { type: stage.node_type, label: stage.label })}</span>
        )}
        <span className="prog-meta">{meta.join(' · ')}</span>
      </div>
      <Decisions decisions={stage.decisions} />
      {stage.error && <p className="prog-err">{failureLabel(t, stage.error)}</p>}
      {open && units.length > 0 && (
        <ul className="prog-units">
          {shown.map((u) => (
            <UnitRow key={u.key} unit={u} />
          ))}
          {shown.length < units.length && (
            <li>
              <button type="button" className="linkish small" onClick={() => setAll(true)}>
                {t('prog.more', { n: units.length - shown.length })}
              </button>
            </li>
          )}
        </ul>
      )}
      {open && stage.units_truncated && <p className="prog-note">{t('prog.truncated')}</p>}
    </li>
  );
}

function unitTitle(t: TFunction, u: ProgressUnit): string {
  return u.title || unitLabel(t, u.key, u.kind);
}

function tokenSummary(t: TFunction, tokens: number | null, calls: number): string | null {
  if (!calls) return null;
  return tokens === null ? t('prog.sum.tokensUnknown') : t('prog.sum.tokens', { n: compactTokens(tokens) });
}

function UnitRow({ unit }: { unit: ProgressUnit }) {
  const { t } = useI18n();
  const tone = unitTone(unit.status);
  const c = unit.calls;
  const meta = [
    unit.status === 'done' && unit.kind !== 'definition' && unit.kind !== 'model' ? t('prog.unit.callsComplete') : tk(t, `prog.unit.${unit.status}`, unit.status),
    c && c.latency_ms > 0 ? formatDuration(c.latency_ms) : null,
    c && c.count > 0 ? t('prog.unit.calls', { n: c.count }) : null,
    c ? tokenSummary(t, totalTokens(c), c.count) : null,
    c && c.failed > 0 && tone === 'ok' ? t('prog.call.recovered', { n: c.failed }) : null,
    unit.attempts && unit.attempts > 1 ? t('prog.unit.attempt', { n: unit.attempts }) : null,
    unit.source === 'template' ? t('prog.unit.template') : null,
  ].filter(Boolean);
  // A failure in the person's language: its failure class (cut off, no response …), else its code's message.
  const reason = unit.error
    ? failureLabel(t, unit.error)
    : c?.last_error && tone !== 'ok'
      ? failureLabel(t, c.last_error)
      : '';
  return (
    <li className={`prog-unit st-${unit.status}`}>
      <div className="prog-row">
        <StatusIcon tone={tone} />
        <span className="prog-unit-name">
          {unitTitle(t, unit)}
          {unit.quantity && unit.quantity > 1 ? <span className="faint"> ×{unit.quantity}</span> : null}
        </span>
        {unit.revision && (
          <span className="prog-rev">
            <span className="mono small">{unit.revision.label}</span>
            {unit.revision.size && <span className="faint small mono">{unit.revision.size.map((n) => +n.toFixed(1)).join('×')}</span>}
          </span>
        )}
        <span className="prog-meta">{meta.join(' · ')}</span>
      </div>
      {!!unit.revision?.operations?.length && (
        <p className="prog-operations">{t('prog.operations', { operations: unit.revision.operations.map(({ op, count }) => `${tk(t, `ws.op.${op}`, op)} ×${count}`).join(' · ') })}</p>
      )}
      <Decisions decisions={unit.decisions} />
      {reason && (
        <p className="prog-err">
          <AlertTriangle className="inline-ico" aria-hidden /> {reason}
        </p>
      )}
      {!!c?.details?.length && (
        <details className="prog-call-details" open={unitFailed(unit)}>
          <summary>{t('prog.call.details', { n: c.details.length })}</summary>
          <CallDetails calls={c.details} />
        </details>
      )}
    </li>
  );
}

function Decisions({ decisions }: { decisions: ProgressDecision[] }) {
  const { t } = useI18n();
  const recorded = decisions.filter((d) => d.kind === 'assumption');
  const notes = recorded.filter(isLegacyModelNote);
  const assumptions: DecisionLike[] = recorded.filter((a) => !isLegacyModelNote(a)).map((a) => ({ ...a, subject: a.subject ?? '', basis: a.basis ?? a.text }));
  const skipped = decisions.flatMap((d) => (d.kind === 'check_skipped' ? d.checks : []));
  const repairs = decisions.reduce((n, d) => n + (d.kind === 'repair' ? d.count : 0), 0);
  if (!assumptions.length && !notes.length && !skipped.length && !repairs) return null;
  return (
    <div className="prog-decisions">
      {notes.length > 0 && (
        <div className="prog-model-notes">
          <span className="faint">{t('prog.decision.notes')}</span>
          <ul>{notes.map((note, index) => <li key={note.id ?? index}>{note.basis || note.text}</li>)}</ul>
        </div>
      )}
      {assumptions.length > 0 && (
        <details className="prog-assumptions" open={assumptions.some((a) => decisionProblems(a).length > 0)}>
          <summary>{t('prog.decision.assumptions', { n: assumptions.length })}</summary>
          <AssumptionList items={assumptions} bare />
        </details>
      )}
      {skipped.length > 0 && (
        <span className="prog-chip" title={t('prog.decision.checkSkippedList', { checks: skipped.join(', ') })}>
          {t('prog.decision.checkSkipped')}
        </span>
      )}
      {repairs > 0 && <span className="prog-chip">{t('prog.decision.repair', { n: repairs })}</span>}
    </div>
  );
}

