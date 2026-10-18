// Stage timeline: the same node states as the canvas, as an ordered list
// (topological layer, then start time). Works on narrow screens.

import { Hand } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { NodeRun, WorkflowRun } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { NodeStatusIcon } from '../icons';
import { nodeDisplayName, nodeStatusLabel, nodeTypeLabel } from '../labels';
import { depths, durationBetween, formatDuration } from '../model';

export function orderedNodes(run: WorkflowRun): NodeRun[] {
  const d = depths(
    run.graph.nodes.map((n) => n.id),
    run.graph.edges,
  );
  return [...run.nodes].sort((a, b) => {
    const da = d.get(a.node_id) ?? 0;
    const db = d.get(b.node_id) ?? 0;
    if (da !== db) return da - db;
    const sa = a.started_at ? Date.parse(a.started_at) : Infinity;
    const sb = b.started_at ? Date.parse(b.started_at) : Infinity;
    if (sa !== sb) return sa - sb;
    return a.node_id.localeCompare(b.node_id);
  });
}

/** Re-render every second while something is running (elapsed times). */
function useTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, [active]);
  return now;
}

export function Timeline({ run, selected, onSelect }: { run: WorkflowRun; selected: string | null; onSelect: (id: string) => void }) {
  const { t, fmt } = useI18n();
  const nodes = orderedNodes(run);
  const now = useTick(nodes.some((n) => n.status === 'running'));
  const labels = new Map(run.graph.nodes.map((n) => [n.id, n]));
  return (
    <ol className="wf-timeline" aria-label={t('wf.run.timeline')}>
      {nodes.map((n) => {
        const g = labels.get(n.node_id);
        const name = g ? nodeDisplayName(t, g) : n.node_id;
        const dur = durationBetween(n.started_at, n.finished_at, now);
        let detail = '';
        if (n.status === 'running') detail = t('wf.tl.runningFor', { d: formatDuration(dur ?? 0) });
        else if (n.status === 'succeeded' && dur !== null) detail = t('wf.tl.took', { d: formatDuration(dur) });
        else if (n.status === 'waiting_human') detail = t('wf.tl.waitingSince', { time: fmt.time(n.updated_at) });
        else if (n.status === 'pending') detail = t('wf.tl.pending');
        else if (n.status === 'ready') detail = t('wf.tl.ready');
        else if (n.status === 'skipped') detail = t('wf.tl.skipped');
        else if (n.status === 'failed') detail = n.error?.code ? `${t('wf.tl.failed')} · ${n.error.code}` : t('wf.tl.failed');
        return (
          <li key={n.node_id} className={`wf-tl-item ns-${n.status} ${selected === n.node_id ? 'selected' : ''}`}>
            <button type="button" className="wf-tl-btn" onClick={() => onSelect(n.node_id)} aria-current={selected === n.node_id ? 'true' : undefined}>
              <span className="wf-tl-dot" aria-hidden>
                <NodeStatusIcon status={n.status} />
              </span>
              <span className="wf-tl-main">
                <span className="wf-tl-name">
                  <span className="ellip">{name}</span>
                  {n.status === 'waiting_human' && (
                    <span className="wf-needs-you sm">
                      <Hand aria-hidden /> {t('wf.run.needsYou')}
                    </span>
                  )}
                </span>
                <span className="wf-tl-sub">
                  <span className={`wf-tl-status ns-${n.status}`}>{nodeStatusLabel(t, n.status)}</span>
                  <span className="faint">· {nodeTypeLabel(t, n.node_type)}</span>
                  {n.attempt > 1 && <span className="faint mono">· {t('wf.tl.attempt', { a: n.attempt, m: n.max_attempts })}</span>}
                </span>
                {detail && <span className="wf-tl-detail faint small">{detail}</span>}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
