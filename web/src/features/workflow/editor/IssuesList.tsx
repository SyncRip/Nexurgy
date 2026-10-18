// Validation results with click-to-focus.

import { CircleCheck, Crosshair, ServerCog, TriangleAlert } from 'lucide-react';
import type { GraphError } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { graphErrorText } from '../labels';

export function IssuesList({
  errors,
  source,
  checkedAt,
  nodeInfo,
  edgeTarget,
  onFocus,
}: {
  errors: GraphError[];
  source: 'server' | 'local';
  checkedAt?: string | null;
  nodeInfo: (id: string) => { type: string; name: string } | undefined;
  edgeTarget: (edgeId: string) => string | undefined;
  onFocus: (nodeId: string) => void;
}) {
  const { t, fmt } = useI18n();
  return (
    <section className="wf-issues" aria-label={t('wf.issues.title')}>
      <header className="wf-issues-head">
        <span className="wf-insp-label">{t('wf.issues.title')}</span>
        <span className={`chip ${errors.length ? 'err' : 'ok'}`}>{errors.length ? t('wf.issuesCount', { n: errors.length }) : t('wf.issues.none')}</span>
      </header>
      <p className="faint small wf-issues-src">
        {source === 'server' ? <ServerCog aria-hidden className="inline-ico" /> : null}
        {source === 'server' ? t('wf.issues.server', { time: checkedAt ? fmt.time(checkedAt) : '—' }) : t('wf.issues.local')}
      </p>
      {errors.length === 0 ? (
        <p className="row small muted">
          <CircleCheck aria-hidden className="ok-ico" /> {t('wf.issues.ready')}
        </p>
      ) : (
        <ul className="wf-issue-list">
          {errors.map((e, i) => {
            const nodeId = e.node_id ?? (e.edge_id ? edgeTarget(e.edge_id) : undefined);
            const info = nodeId ? nodeInfo(nodeId) : undefined;
            const text = graphErrorText(t, e, info?.type);
            return (
              <li key={`${e.code}-${i}`}>
                {nodeId ? (
                  <button type="button" className="wf-issue" onClick={() => onFocus(nodeId)} title={e.message}>
                    <TriangleAlert aria-hidden />
                    <span className="wf-issue-text">
                      <span className="strong">{info?.name ?? nodeId}</span>
                      <span>{text}</span>
                    </span>
                    <Crosshair aria-hidden className="wf-issue-go" />
                  </button>
                ) : (
                  <div className="wf-issue static" title={e.message}>
                    <TriangleAlert aria-hidden />
                    <span className="wf-issue-text">
                      <span>{text}</span>
                    </span>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
