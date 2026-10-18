// The conversation's work at a glance (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator/design.md §3), above the context
// inspector in the full layout's right column while nothing is open in the work panel: the sub-agents the main agent
// started (status, elapsed time, what they produced) and the task drafts, tasks and 2D drawing drafts of this
// conversation. Every row opens the existing pane (run, revision, task, draft); a sub-agent without a run yet scrolls
// to its card. Nothing to list → nothing shown.

import { Bot, ClipboardPen, FileImage, Layers, ListChecks, PlayCircle, UsersRound } from 'lucide-react';
import type { MouseEvent } from 'react';
import type { AiConversationDetail, AiTaskDraft } from '../../api/assistant';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from '../agent-team/AgentRole';
import { isCoordinatedOperation } from '../agent-team/roles';
import { durationBetween, formatDuration } from '../workflow/model';
import { OperationStatusChip, opKindLabel, SimChip, tk } from './common';
import { isTerminal } from './model';
import { revealOpCard } from './OperationCard';
import { overviewEmpty, sessionOverview, type OverviewAgent } from './orchestrator';
import { useWorkPanel } from './panel/WorkPanelContext';

const DRAFT_TONE: Record<AiTaskDraft['status'], Tone> = { open: 'accent', used: 'ok', discarded: 'pending' };

export function DraftStatusChip({ status }: { status: AiTaskDraft['status'] }) {
  const { t } = useI18n();
  return (
    <Chip tone={DRAFT_TONE[status] ?? 'pending'} className="sm">
      {tk(t, `wp.draft.status.${status}`, status)}
    </Chip>
  );
}

export function SessionOverview({ detail }: { detail: AiConversationDetail }) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const m = sessionOverview(detail);
  if (overviewEmpty(m)) return null;
  const hasWork = m.drafts.length + m.tasks.length + m.drawings.length > 0;

  return (
    <div className="as-ov">
      {m.agents.length > 0 && (
        <section className="as-insp-sec" aria-label={t('as.orch.ov.agents')}>
          <h3 className="as-ov-h">
            <UsersRound aria-hidden /> {t('as.orch.ov.agents')}
          </h3>
          <ul className="as-ov-list">
            {m.agents.map((a) => (
              <AgentRow key={a.op.id} a={a} />
            ))}
          </ul>
        </section>
      )}
      {hasWork && (
        <section className="as-insp-sec" aria-label={t('as.orch.ov.work')}>
          <h3 className="as-ov-h">
            <ListChecks aria-hidden /> {t('as.orch.ov.work')}
          </h3>
          <ul className="as-ov-list">
            {m.tasks.map((k) => (
              <li key={k.id}>
                <button type="button" className="as-ov-row" onClick={() => wp.open({ kind: 'task', id: k.id })}>
                  <ListChecks aria-hidden className="as-ov-ico" />
                  <span className="as-ov-name">
                    {k.number && <span className="mono faint">{k.number} </span>}
                    {k.title}
                  </span>
                  <span className="faint small">{tk(t, `tk.status.${k.status}`, k.status)}</span>
                </button>
              </li>
            ))}
            {m.drafts.map((d) => (
              <li key={d.id}>
                <button type="button" className="as-ov-row" onClick={() => wp.open({ kind: 'draft', id: d.id })}>
                  <ClipboardPen aria-hidden className="as-ov-ico" />
                  <span className="as-ov-name">
                    <span className="faint">{t('as.orch.ov.draft')} · </span>
                    {d.title || t('wp.draft.untitled')}
                  </span>
                  <DraftStatusChip status={d.status} />
                </button>
              </li>
            ))}
            {m.drawings.map((dr) => (
              <li key={dr.key}>
                <button type="button" className="as-ov-row" onClick={() => wp.open({ kind: 'run', id: dr.runId }, { nodeId: dr.nodeId })}>
                  <FileImage aria-hidden className="as-ov-ico" />
                  <span className="as-ov-name">
                    <span className="faint">{t('as.orch.ov.drawing')} · </span>
                    {dr.title} <span className="mono">{dr.label}</span>
                  </span>
                  {dr.simulated && <SimChip />}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function AgentRow({ a }: { a: OverviewAgent }) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const op = a.op;
  // Re-rendered by the conversation's 2 s polling while the sub-agent is active.
  const ms = durationBetween(op.created_at, isTerminal(op) ? (op.finished_at ?? op.updated_at) : null);
  const open = (e: MouseEvent<HTMLElement>) => (a.panel ? wp.open(a.panel) : revealOpCard(e, op.id));
  return (
    <li className="as-ov-agent">
      <button type="button" className="as-ov-row" onClick={open} title={a.panel ? t('as.orch.ov.openRun') : t('as.orch.ov.showCard')}>
        {!a.agent && (a.panel ? <PlayCircle aria-hidden className="as-ov-ico" /> : <Bot aria-hidden className="as-ov-ico" />)}
        <span className="as-ov-name">
          {a.agent && (isCoordinatedOperation(op.kind)
            ? <span className="agent-coordination"><AgentRoleBadge role={a.agent} compact /><span>{t('as.op.coordinator')}</span></span>
            : <AgentRoleBadge role={a.agent} compact />)}
          <span>{opKindLabel(t, op.kind)}</span>
        </span>
        <OperationStatusChip op={op} />
        {ms !== null && <span className="mono faint small">{formatDuration(ms)}</span>}
      </button>
      {a.outputs.length > 0 && (
        <div className="as-ov-outs">
          {a.outputs.map((o) => (
            <button key={o.key} type="button" className="as-step-obj" onClick={() => wp.open(o.panel, { nodeId: o.nodeId })}>
              {o.panel.kind === 'revision' ? <Layers aria-hidden /> : <FileImage aria-hidden />}
              <span>
                {tk(t, `tk.assetType.${o.assetType ?? ''}`, o.assetType ?? '')} {o.label}
              </span>
            </button>
          ))}
        </div>
      )}
    </li>
  );
}
