// Run-mode parts of a node card (R1.2 prototype "Part canvas"): the result block
// (preview + metrics), the state block (waiting / failed / not run yet), the
// footer (who ran it, attempts, time) and the one-line summary shown instead of
// the full card when the canvas is zoomed out. Everything comes from the node
// run as recorded; nothing is estimated here.

import { Bot, Clock, Cog, FlaskConical, RotateCw, User } from 'lucide-react';
import { useEffect, useState } from 'react';
import { blobUrl, type BlobHandle } from '../../../api/blobs';
import type { NodeRun } from '../../../api/types';
import type { MessageKey } from '../../../i18n/en';
import { useI18n, type TFunction } from '../../../i18n/I18nProvider';
import type { Formatters } from '../../../i18n/format';
import { durationBetween, formatDuration } from '../model';
import { nodeErrorText } from '../labels';
import { nodeSummary, quoteReviewed, type NodeSummary } from '../run/summary';
import { chainLine, chainValueOf, ChainValueView } from '../../orchestration/ChainValue';
import { manufacturingValueOf } from '../../../api/manufacturing';
import { ManufacturingBundleNotice, ManufacturingValueView } from '../../manufacturing/ManufacturingValue';
import { manufacturingLine } from '../../manufacturing/model';

const INPUT_TYPES = new Set(['text_input', 'file_input', 'model_input', 'human_review']);

function money(fmt: Formatters, fen: number, currency: string): string {
  try {
    return fmt.number(fen / 100, { style: 'currency', currency });
  } catch {
    return `${fmt.number(fen / 100)} ${currency}`;
  }
}

function checkLabel(t: TFunction, status: string | null): string {
  if (!status) return '—';
  return ['passed', 'failed', 'not_evaluated', 'unavailable'].includes(status) ? t(`cv.check.${status}` as MessageKey) : status;
}

function subjectLabel(t: TFunction, subject: string | null): string | null {
  return subject && ['model', 'drawing', 'spec', 'plan', 'quote'].includes(subject) ? t(`wf.ptype.${subject}` as MessageKey) : null;
}

/** One line for the zoomed-out card (prototype cmpLine). */
export function compactLine(t: TFunction, fmt: Formatters, nr: NodeRun | undefined, s: NodeSummary | null): string {
  if (!nr) return t('cv.state.pending');
  switch (nr.status) {
    case 'running':
      return t('cv.state.running');
    case 'ready':
      return t('cv.state.ready');
    case 'pending':
      return t('cv.state.pending');
    case 'waiting_human':
      return nr.review?.kind === 'clarification' ? t('cv.state.answer') : nr.review?.kind === 'manual_entry' ? t('cv.state.manual') : t('cv.state.review');
    case 'failed': {
      const batch = manufacturingValueOf(nr);
      if (batch) return `${t('cv.state.failed')} · ${manufacturingLine(t, batch)}`;
      const chain = chainValueOf(nr); // W8: a failed chain step can still hold real products (e.g. a partial delivery)
      return chain ? `${t('cv.state.failed')} · ${chainLine(t, chain)}` : t('cv.state.failed');
    }
    case 'skipped':
      return t('cv.state.skipped');
    case 'cancelled':
      return t('cv.state.cancelled');
  }
  if (!s) {
    const batch = manufacturingValueOf(nr);
    if (batch) return manufacturingLine(t, batch);
    const chain = chainValueOf(nr);
    return chain ? chainLine(t, chain) : t('ws.nodeStatus.succeeded');
  }
  switch (s.kind) {
    case 'text':
      return s.text.length > 16 ? `${s.text.slice(0, 16)}…` : s.text || t('wf.art.emptyText');
    case 'file':
      return s.manufacturing ? `${s.name} · ${t('mfg.estimate')}${s.manufacturing.price_complete ? '' : ` · ${t('mfg.pricePending')}`}` : s.name;
    case 'model':
      if (s.rev === null) return t('ws.nodeStatus.succeeded');
      return s.reqTotal !== null ? t('cv.sum.modelReq', { rev: s.rev, ok: s.reqPassed ?? 0, n: s.reqTotal }) : t('cv.sum.rev', { n: s.rev });
    case 'drawing':
      return s.rev !== null ? t('cv.sum.drawing', { pages: s.pages ?? '?', rev: s.rev }) : t('cv.sum.drawingNoRev', { pages: s.pages ?? '?' });
    case 'quote':
      return s.unitFen !== null ? money(fmt, s.unitFen, s.currency) : t('wf.ptype.quote');
    case 'plan':
      return s.priceable ? t('cv.sum.plan') : t('cv.sum.notPriceable');
    case 'spec':
      return t('cv.sum.spec');
    case 'review': {
      const subject = subjectLabel(t, s.subject);
      return subject ? t('cv.sum.approvedSubject', { subject }) : t('cv.sum.approved');
    }
  }
}

/** Authenticated drawing thumbnail (no link: the card itself opens the node). */
function SvgThumb({ sha }: { sha: string }) {
  const [h, setH] = useState<BlobHandle | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    let handle: BlobHandle | null = null;
    blobUrl(sha, { signal: ctrl.signal })
      .then((x) => {
        handle = x;
        setH(x);
      })
      .catch(() => undefined);
    return () => {
      ctrl.abort();
      handle?.revoke();
    };
  }, [sha]);
  return h ? <img src={h.url} alt="" draggable={false} /> : null;
}

function Metrics({ items }: { items: [string, string][] }) {
  return (
    <div className="wf-nd-met">
      {items.map(([k, v]) => (
        <span key={k}>
          <small>{k}</small>
          <b title={v}>{v}</b>
        </span>
      ))}
    </div>
  );
}

/** The node's result (succeeded runs only). */
export function ResultBlock({ nr, runs }: { nr: NodeRun; runs: ReadonlyMap<string, NodeRun> | undefined }) {
  const { t, fmt } = useI18n();
  const s = nodeSummary(nr);
  if (!s) {
    const batch = nr.status === 'succeeded' ? manufacturingValueOf(nr) : null;
    if (batch) return <div className="wf-nd-res"><ManufacturingValueView value={batch} compact /></div>;
    const chain = nr.status === 'succeeded' ? chainValueOf(nr) : null;
    return chain ? (
      <div className="wf-nd-res">
        <ChainValueView value={chain} compact />
      </div>
    ) : null;
  }
  switch (s.kind) {
    case 'text':
      return (
        <div className="wf-nd-res">
          <p className="wf-nd-quote">“{s.text}”</p>
          <small className="faint">{t('cv.sum.chars', { n: s.text.length })}</small>
        </div>
      );
    case 'file':
      return (
        <div className="wf-nd-res">
          <div className="wf-nd-file">
            <span className="ext">{s.ext || '—'}</span>
            <b className="ellip" title={s.name}>
              {s.name}
            </b>
          </div>
          {s.manufacturing && <ManufacturingBundleNotice value={s.manufacturing} compact />}
        </div>
      );
    case 'model':
      return (
        <div className="wf-nd-res">
          <Metrics
            items={[
              [t('cv.met.version'), s.rev !== null ? t('cv.sum.rev', { n: s.rev }) : '—'],
              [t('cv.met.reqs'), s.reqTotal !== null ? `${s.reqPassed ?? 0}/${s.reqTotal}` : '—'],
              [t('cv.met.check'), checkLabel(t, s.check)],
            ]}
          />
          {s.meshOnly && <span className="chip warn">{t('cv.sum.meshOnly')}</span>}
        </div>
      );
    case 'drawing':
      return (
        <div className="wf-nd-res">
          {s.svgSha && (
            <div className="wf-nd-prev paper">
              <SvgThumb sha={s.svgSha} />
              <span className="ov">{s.rev !== null ? t('cv.sum.drawing', { pages: s.pages ?? '?', rev: s.rev }) : t('cv.sum.drawingNoRev', { pages: s.pages ?? '?' })}</span>
            </div>
          )}
          <Metrics
            items={[
              [t('cv.met.pages'), s.pages !== null ? String(s.pages) : '—'],
              [t('cv.met.annotation'), checkLabel(t, s.annotation)],
              [t('cv.met.dwg'), checkLabel(t, s.dwg)],
            ]}
          />
        </div>
      );
    case 'quote': {
      const reviewed = quoteReviewed(s.quoteId, runs?.values() ?? []);
      return (
        <div className="wf-nd-res">
          <div className="wf-nd-price">
            <small>{s.qty !== null ? t('cv.sum.unitPrice', { qty: s.qty }) : t('wf.ptype.quote')}</small>
            <b>{s.unitFen !== null ? money(fmt, s.unitFen, s.currency) : '—'}</b>
            <span className={`chip ${reviewed ? 'ok' : 'warn'}`}>{reviewed ? t('cv.sum.reviewed') : t('cv.sum.notReviewed')}</span>
          </div>
        </div>
      );
    }
    case 'plan':
      return (
        <div className="wf-nd-res">
          <Metrics
            items={[
              [t('cv.met.priceable'), s.priceable ? t('cv.yes') : t('cv.no')],
              [t('cv.sum.estimate'), s.estimated ? t('cv.yes') : t('cv.no')],
            ]}
          />
        </div>
      );
    case 'spec':
      return (
        <div className="wf-nd-res">
          <Metrics
            items={[
              [t('cv.met.method'), s.method === 'ai' ? t('wf.art.method.ai') : s.method ? t('wf.art.method.manual') : '—'],
              [t('cv.sum.estimate'), s.estimated ? t('cv.yes') : t('cv.no')],
            ]}
          />
        </div>
      );
    case 'review': {
      const subject = subjectLabel(t, s.subject);
      return (
        <div className="wf-nd-res">
          <small className="faint">{subject ? t('cv.sum.approvedSubject', { subject }) : t('cv.sum.approved')}</small>
        </div>
      );
    }
  }
}

/** Waiting / failed / not-run hints; the button opens the node, where people decide (never the canvas or the AI). */
export function StateBlock({ nr, hasUpstream, onOpen }: { nr: NodeRun; hasUpstream: boolean; onOpen?: () => void }) {
  const { t } = useI18n();
  if (nr.status === 'running') {
    return (
      <div className="wf-nd-state run">
        <span>{t('cv.state.running')}</span>
        <span className="wf-nd-bar" aria-hidden>
          <i />
        </span>
      </div>
    );
  }
  if (nr.status === 'waiting_human') {
    const kind = nr.review?.kind;
    const [label, action]: [MessageKey, MessageKey] =
      kind === 'clarification' ? ['cv.state.answer', 'cv.state.goAnswer'] : kind === 'manual_entry' ? ['cv.state.manual', 'cv.state.goEnter'] : ['cv.state.review', 'cv.state.goReview'];
    return (
      <div className="wf-nd-state wait">
        <span>
          <User aria-hidden /> {t(label)}
        </span>
        {onOpen && (
          <button type="button" className="btn sm" onClick={onOpen}>
            {t(action)}
          </button>
        )}
      </div>
    );
  }
  if (nr.status === 'failed') {
    const batch = manufacturingValueOf(nr);
    const chain = chainValueOf(nr); // W8: the products a failed chain step kept (built parts, a partial delivery)
    return (
      <div className="wf-nd-state err">
        <span className="wf-nd-err" title={nr.error?.message ?? undefined}>
          {nodeErrorText(t, nr.error?.code, nr.error?.message || t('cv.state.failed'))}
        </span>
        {batch && <ManufacturingValueView value={batch} compact />}
        {chain && <ChainValueView value={chain} compact />}
      </div>
    );
  }
  if (nr.status === 'pending') {
    return <div className="wf-nd-state pend">{hasUpstream ? t('cv.state.afterUpstream') : t('cv.state.noUpstream')}</div>;
  }
  return null;
}

/** Who ran the node, attempts, time and the simulated marker. */
export function NodeFooter({ nr, nodeType, usesAi }: { nr: NodeRun; nodeType: string; usesAi: boolean }) {
  const { t } = useI18n();
  const out = nr.outputs ?? {};
  const model = typeof out.ai_model === 'string' ? out.ai_model : null;
  const simulated = out.simulated === true || nr.steps?.some((s) => (s.detail as { simulated?: unknown } | undefined)?.simulated === true);
  const dur = durationBetween(nr.started_at, nr.finished_at);
  const human = INPUT_TYPES.has(nodeType);
  return (
    <footer className="wf-nd-foot">
      <span className="who">
        {usesAi ? <Bot aria-hidden /> : human ? <User aria-hidden /> : <Cog aria-hidden />}
        <span className="ellip">{usesAi ? (model ? t('cv.actor.ai', { model }) : t('cv.actor.aiOnly')) : human ? t('cv.actor.person') : t('cv.actor.system')}</span>
      </span>
      <span className="meta">
        {simulated && (
          <span className="wf-sim-mini" title={t('wf.simulated.title')}>
            <FlaskConical aria-hidden />
          </span>
        )}
        {nr.attempt > 0 && nr.max_attempts > 1 && (
          <span title={t('cv.attempts')}>
            <RotateCw aria-hidden /> {nr.attempt}/{nr.max_attempts}
          </span>
        )}
        {dur !== null && (
          <span title={t('cv.duration')}>
            <Clock aria-hidden /> {formatDuration(dur)}
          </span>
        )}
      </span>
    </footer>
  );
}
