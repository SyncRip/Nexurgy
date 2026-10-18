// Execution timeline (plan §5.5): model messages, tool calls (call + result as one entry), questions, delivery /
// blocking, verdicts and system notes. While running, new entries are appended at the bottom; if the user scrolled
// up the view stays put and a "N new events" pill appears. Filters by type once the execution has ended.

import {
  Bot,
  CheckCircle2,
  CircleDashed,
  CircleX,
  FilePen,
  Hand,
  Info,
  LoaderCircle,
  MessageCircleQuestion,
  OctagonAlert,
  Package,
  PackageCheck,
  Radio,
  RefreshCw,
  ShieldCheck,
  SquareTerminal,
  TriangleAlert,
  Wrench,
} from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Chip } from '../../components/ui/Badges';
import { ErrorText } from '../../components/ui/States';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { dynT } from '../collaboration/common';
import { EvidenceReads } from '../collaboration/EvidenceReads';
import { ExecutionContextLine } from '../orchestration/StateBadge';
import { toolExecutionContext } from '../orchestration/status';
import { formatDuration } from '../workflow/model';
import { VersionLink } from './common';
import type { FollowState } from './follow';
import { isAtBottom, matchesFilter, TAIL_START, tailOnAppend, tailOnScroll, TIMELINE_FILTERS, type TimelineFilter, type TimelineItem } from './model';

export const evAnchor = (seq: number) => `ax-ev-${seq}`;

function itemTone(i: TimelineItem): string {
  switch (i.kind) {
    case 'tool':
      return i.result && !i.result.ok ? 'tone-err' : 'tone-tool';
    case 'message':
      return 'tone-msg';
    case 'question':
      return 'tone-need';
    case 'deliver':
      return 'tone-ok';
    case 'blocked':
      return 'tone-err';
    case 'verdict':
      return 'tone-ok';
    case 'system':
      return ['lease_lost', 'budget_exceeded', 'timeout', 'failed', 'workspace_cleanup_failed'].includes(i.code)
        ? 'tone-err'
        : i.code === 'stopped' || i.code === 'paused'
          ? 'tone-need'
          : 'tone-sys';
    case 'command':
      return i.timeout || (i.exitCode !== null && i.exitCode !== 0) ? 'tone-err' : 'tone-tool';
    case 'file_change':
      return 'tone-tool';
    case 'confirmation':
      return i.state === 'pending' ? 'tone-need' : i.state === 'rejected' ? 'tone-err' : 'tone-ok';
    default:
      return 'tone-sys';
  }
}

function ItemIcon({ item }: { item: TimelineItem }) {
  switch (item.kind) {
    case 'tool':
      return <Wrench aria-hidden />;
    case 'message':
      return <Bot aria-hidden />;
    case 'question':
      return <MessageCircleQuestion aria-hidden />;
    case 'deliver':
      return <PackageCheck aria-hidden />;
    case 'blocked':
      return <OctagonAlert aria-hidden />;
    case 'verdict':
      return <ShieldCheck aria-hidden />;
    case 'system':
      return itemTone(item) === 'tone-err' ? <TriangleAlert aria-hidden /> : <Info aria-hidden />;
    case 'command':
      return <SquareTerminal aria-hidden />;
    case 'file_change':
      return <FilePen aria-hidden />;
    case 'confirmation':
      return <Hand aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

export function itemLabel(t: TFunction, i: TimelineItem): string {
  if (i.kind === 'other') return t('ax.ev.other', { type: i.type });
  return t(`ax.ev.${i.kind}`);
}

/** One line saying what the item is (the "current step" of the compact panel and the workbench). */
export function itemSummary(t: TFunction, i: TimelineItem): string {
  switch (i.kind) {
    case 'tool':
      return `${t('ax.ev.tool')} · ${i.name}${i.result ? ` · ${i.result.ok ? t('ax.ev.toolOk') : t('ax.ev.toolFailed')}` : ''}`;
    case 'message':
      return i.text.split('\n')[0].slice(0, 160);
    case 'system':
      return dynT(t, `ax.sys.${i.code}`, '') || i.code + (i.message ? ` · ${i.message}` : '');
    case 'question':
      return `${t('ax.ev.question')} · ${i.questions[0] ?? ''}`;
    case 'blocked':
      return `${t('ax.ev.blocked')} · ${i.reason}`;
    case 'deliver':
      return t('ax.ev.deliver');
    case 'verdict':
      return `${t('ax.ev.verdict')}${i.summary ? ` · ${i.summary}` : ''}`;
    case 'command':
      return `${t('ax.ev.command')} · ${i.command.split('\n')[0].slice(0, 120)}`;
    case 'file_change':
      return `${t('ax.ev.file_change')} · ${i.path}`;
    case 'confirmation':
      return `${t('ax.ev.confirmation')} · ${confirmStateText(t, i.state)} · ${i.action.slice(0, 120)}`;
    default:
      return itemLabel(t, i);
  }
}

export function confirmStateText(t: TFunction, state: string): string {
  return dynT(t, `ax.cf.state.${state}`, state);
}

/** The risk the security analyzer gave a workspace action (icon + text). */
function RiskChip({ risk }: { risk: string }) {
  const { t } = useI18n();
  const tone = risk === 'high' ? 'err' : risk === 'medium' ? 'warn' : risk === 'low' ? 'ok' : 'pending';
  return (
    <Chip tone={tone} icon={risk === 'high' ? <TriangleAlert aria-hidden /> : <Info aria-hidden />} className="cl-chip-sm">
      {dynT(t, `ax.cf.risk.${risk}`, risk)}
    </Chip>
  );
}

function ConfirmState({ state }: { state: string }) {
  const { t } = useI18n();
  const icon = state === 'approved' ? <CheckCircle2 aria-hidden /> : state === 'rejected' ? <CircleX aria-hidden /> : <Hand aria-hidden />;
  return (
    <Chip tone={state === 'approved' ? 'ok' : state === 'rejected' ? 'err' : 'warn'} icon={icon} className="cl-chip-sm">
      {confirmStateText(t, state)}
    </Chip>
  );
}

function VerdictMark({ result }: { result: string }) {
  const { t } = useI18n();
  if (result === 'pass')
    return (
      <Chip tone="ok" icon={<CheckCircle2 aria-hidden />} className="cl-chip-sm">
        {t('collab.verdict.pass')}
      </Chip>
    );
  if (result === 'fail')
    return (
      <Chip tone="err" icon={<CircleX aria-hidden />} className="cl-chip-sm">
        {t('collab.verdict.fail')}
      </Chip>
    );
  return (
    <Chip tone="warn" icon={<CircleDashed aria-hidden />} className="cl-chip-sm">
      {t('collab.verdict.unknown')}
    </Chip>
  );
}

function errorText(e: unknown): string {
  if (!e) return '';
  if (typeof e === 'string') return e;
  const o = e as { code?: string | null; message?: string | null };
  return [o.message, o.code ? `(${o.code})` : ''].filter(Boolean).join(' ');
}

const json = (v: unknown) => {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
};

function ItemBody({ item, extra }: { item: TimelineItem; extra?: ReactNode }) {
  const { t, tn, fmt } = useI18n();
  switch (item.kind) {
    case 'message':
      return (
        <>
          <div className="ax-ev-body">{item.text}</div>
          {item.call && (
            <div className="ax-badges">
              <span>{item.call.model_id}</span>
              {(item.call.tokens_in !== null || item.call.tokens_out !== null) && <span>{t('ax.ev.tokens', { in: fmt.number(item.call.tokens_in ?? 0), out: fmt.number(item.call.tokens_out ?? 0) })}</span>}
              {item.call.latency_ms !== null && <span>{formatDuration(item.call.latency_ms)}</span>}
            </div>
          )}
        </>
      );
    case 'tool': {
      const r = item.result;
      const err = r && !r.ok ? errorText(r.error) : '';
      return (
        <>
          {item.args && Object.keys(item.args).length > 0 && (
            <details>
              <summary>{t('ax.ev.args')}</summary>
              <pre>{json(item.args)}</pre>
            </details>
          )}
          {r && (r.message || err) && (
            <div className={`ax-ev-res ${r.ok ? '' : 'err'}`}>
              {r.message}
              {err && <span className="err-text small"> {err}</span>}
            </div>
          )}
          {r && r.produced?.length > 0 && (
            <div className="ax-produced">
              <Package aria-hidden /> {t('ax.ev.produced')}
              {r.produced.map((p) => (
                <VersionLink key={p.asset_version_id} versionId={p.asset_version_id} label={p.label} />
              ))}
            </div>
          )}
          {r && (r.ai_calls ?? 0) > 0 && <div className="ax-badges">{tn('ax.ev.aiCalls', r.ai_calls ?? 0)}</div>}
          {/* generation reliability N: how the tool call ran (policy and its source, inputs, target, budget) */}
          {r?.execution && <ExecutionContextLine ctx={toolExecutionContext(r.execution)} />}
        </>
      );
    }
    case 'system': {
      // A known code already reads as its localized title: the server's sentence (English) goes into the details.
      const known = dynT(t, `ax.sys.${item.code}`, '') !== '';
      const raw = known && item.message ? { message: item.message, ...(item.detail != null ? { detail: item.detail } : {}) } : item.detail;
      return (
        <>
          {item.message && !known && <div className="ax-ev-body">{item.message}</div>}
          {raw !== undefined && raw !== null && (
            <details>
              <summary>{t('ax.ev.payload')}</summary>
              <pre>{json(raw)}</pre>
            </details>
          )}
        </>
      );
    }
    case 'question':
      return (
        <>
          <ol className="ax-qs">
            {item.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ol>
          {extra}
        </>
      );
    case 'deliver':
      return (
        <>
          {item.deliverables.length ? (
            <ul className="ax-list">
              {item.deliverables.map((d, i) => (
                <li key={`${d.asset_version_id ?? ''}-${i}`}>
                  {d.slot_key && <span className="cl-slot">{d.slot_key}</span>}
                  {d.asset_version_id ? <VersionLink versionId={d.asset_version_id} label="" /> : null}
                </li>
              ))}
            </ul>
          ) : (
            <div className="ax-ev-body faint">{t('ax.ev.noDeliverables')}</div>
          )}
          {item.note && <div className="ax-ev-body">{item.note}</div>}
        </>
      );
    case 'blocked':
      return (
        <>
          {item.reason && <div className="ax-ev-body">{item.reason}</div>}
          {item.missing.length > 0 && <div className="ax-ev-body">{t('ax.ev.missing', { list: item.missing.join('、') })}</div>}
          {extra}
        </>
      );
    case 'verdict':
      return (
        <>
          {item.summary && <div className="ax-ev-body">{item.summary}</div>}
          <ul className="ax-crit">
            {item.criteria.map((c) => (
              <li key={c.criterion_id}>
                <VerdictMark result={c.result} />
                <span className="ax-ev-body">{c.reason}</span>
                {c.evidence && c.evidence.length > 0 && <EvidenceReads reads={c.evidence} />}
              </li>
            ))}
          </ul>
        </>
      );
    case 'command':
      return (
        <>
          <pre className="ax-cmd">
            <span aria-hidden>$ </span>
            {item.command}
          </pre>
          <div className="ax-badges">
            <span>{item.timeout ? t('ax.cmd.timeout') : item.exitCode === null ? t('ax.cmd.noExit') : t('ax.cmd.exit', { code: item.exitCode })}</span>
            {item.durationMs !== null && <span>{formatDuration(item.durationMs)}</span>}
          </div>
          {item.output && (
            <details>
              <summary>{t('ax.cmd.output')}</summary>
              <pre>{item.output}</pre>
            </details>
          )}
        </>
      );
    case 'file_change':
      return (
        <>
          <div className="ax-badges">
            <span className="ax-add">+{fmt.number(item.added)}</span>
            <span className="ax-del">−{fmt.number(item.removed)}</span>
          </div>
          {item.diff && (
            <details>
              <summary>{t('ax.fc.diff')}</summary>
              <pre className="ax-diff">
                {item.diff.split('\n').map((line, n) => (
                  <span key={n} className={line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : undefined}>
                    {line}
                    {'\n'}
                  </span>
                ))}
              </pre>
            </details>
          )}
        </>
      );
    case 'confirmation':
      return (
        <>
          <div className="ax-ev-body">{item.action}</div>
          {item.actions.length > 1 && (
            <details>
              <summary>{t('ax.cf.batch', { n: item.actions.length })}</summary>
              <pre>{item.actions.join('\n')}</pre>
            </details>
          )}
          {item.state !== 'pending' && (item.by || item.reason) && (
            <div className="ax-ev-res">
              {item.by && t('ax.cf.by', { name: item.by })}
              {item.reason && <span className="small"> · {item.reason}</span>}
            </div>
          )}
          {item.state === 'pending' && extra}
        </>
      );
    default:
      return (
        <details>
          <summary>{t('ax.ev.payload')}</summary>
          <pre>{json(item.payload)}</pre>
        </details>
      );
  }
}

function ToolState({ item }: { item: Extract<TimelineItem, { kind: 'tool' }> }) {
  const { t } = useI18n();
  if (!item.result)
    return (
      <Chip tone="measure" icon={<LoaderCircle aria-hidden />} className="cl-chip-sm">
        {t('ax.ev.toolRunning')}
      </Chip>
    );
  return item.result.ok ? (
    <Chip tone="ok" icon={<CheckCircle2 aria-hidden />} className="cl-chip-sm">
      {t('ax.ev.toolOk')}
    </Chip>
  ) : (
    <Chip tone="err" icon={<CircleX aria-hidden />} className="cl-chip-sm">
      {t('ax.ev.toolFailed')}
    </Chip>
  );
}

export function TimelineEntry({ item, flash, extra }: { item: TimelineItem; flash?: boolean; extra?: ReactNode }) {
  const { t, fmt } = useI18n();
  const title =
    item.kind === 'tool' || item.kind === 'file_change' ? (
      <code className="mono">{item.kind === 'tool' ? item.name : item.path}</code>
    ) : item.kind === 'system' ? (
      dynT(t, `ax.sys.${item.code}`, item.code)
    ) : null;
  return (
    <li className={`ax-ev ${itemTone(item)} ${flash ? 'flash' : ''}`} id={evAnchor(item.seq)} data-kind={item.kind}>
      <span className="ax-ev-ic">
        <ItemIcon item={item} />
      </span>
      <div className="ax-ev-main">
        <div className="ax-ev-head">
          <span className="ax-ev-label">{itemLabel(t, item)}</span>
          {title && <span className="ax-ev-title">{title}</span>}
          {item.kind === 'tool' && <ToolState item={item} />}
          {item.kind === 'tool' && item.result?.duration_ms != null && <span className="ax-ev-dur">{formatDuration(item.result.duration_ms)}</span>}
          {item.kind === 'confirmation' && <RiskChip risk={item.risk} />}
          {item.kind === 'confirmation' && <ConfirmState state={item.state} />}
          <time dateTime={item.at ?? undefined} title={item.at ? fmt.dateTime(item.at) : undefined}>
            {item.at ? fmt.time(item.at) : `#${item.seq}`}
          </time>
        </div>
        <ItemBody item={item} extra={extra} />
      </div>
    </li>
  );
}

function ConnState({ state, error }: { state: FollowState; error: unknown }) {
  const { t } = useI18n();
  if (state === 'live')
    return (
      <span className="ax-conn">
        <i className="ax-pulse" aria-hidden /> {t('ax.tl.live')}
      </span>
    );
  if (state === 'reconnecting')
    return (
      <span className="ax-conn warn" role="status">
        <RefreshCw aria-hidden /> {t('ax.tl.reconnecting')}
      </span>
    );
  if (state === 'ended')
    return (
      <span className="ax-conn">
        <CheckCircle2 aria-hidden /> {t('ax.tl.ended')}
      </span>
    );
  if (state === 'error')
    return (
      <span className="ax-conn err" role="alert">
        <Radio aria-hidden /> {t('ax.tl.streamError')} {error ? <ErrorText error={error} /> : null}
      </span>
    );
  return <span className="ax-conn">{t('ax.tl.loading')}</span>;
}

/**
 * The full timeline card. `jumpTo` (an item anchor seq) scrolls that entry into view and highlights it;
 * `questionExtra` renders under the last question / blocked entry (answering).
 * Historical workspace commands, files and confirmations are read-only timeline entries.
 */
export function Timeline({
  items,
  state,
  streamError,
  jumpTo,
  questionExtra,
}: {
  items: TimelineItem[];
  state: FollowState;
  streamError: unknown;
  jumpTo?: { seq: number; nonce: number } | null;
  questionExtra?: ReactNode;
}) {
  const { t, tn } = useI18n();
  const ended = state === 'ended';
  const live = state === 'live' || state === 'reconnecting';
  const [filter, setFilter] = useState<TimelineFilter>('all');
  const [tail, setTail] = useState(TAIL_START);
  const [flash, setFlash] = useState<number | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const seen = useRef(items.length);
  const pinned = useRef(tail.pinned);
  pinned.current = tail.pinned;
  const shown = useMemo(() => (ended ? items.filter((i) => matchesFilter(i, filter)) : items), [items, filter, ended]);
  const lastAsk = useMemo(() => [...items].reverse().find((i) => i.kind === 'question' || i.kind === 'blocked')?.seq ?? null, [items]);

  // New items while live: follow the tail when the user is at the bottom, otherwise count them.
  useLayoutEffect(() => {
    const added = items.length - seen.current;
    seen.current = items.length;
    if (added <= 0 || !live) return;
    setTail((s) => tailOnAppend(s, added));
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [items.length, live]);

  // Going live (after the back-fill) starts at the newest entry.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (live && el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [live]);

  useEffect(() => {
    if (!jumpTo) return;
    setFilter('all');
    const raf = requestAnimationFrame(() => {
      document.getElementById(evAnchor(jumpTo.seq))?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      setFlash(jumpTo.seq);
    });
    const h = window.setTimeout(() => setFlash(null), 2200);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(h);
    };
  }, [jumpTo]);

  const toBottom = () => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
    setTail({ pinned: true, unseen: 0 });
  };

  return (
    <section className="panel ax-tl-card" aria-label={t('ax.tl.label')}>
      <div className="ax-tl-tools">
        <h2>{t('ax.tl.title')}</h2>
        <ConnState state={state} error={streamError} />
        <span className="grow" />
        {ended ? (
          <span className="row" role="group" aria-label={t('ax.tl.filter.label')}>
            {TIMELINE_FILTERS.map((f) => (
              <button key={f} type="button" className="ax-fchip" aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {t(`ax.tl.filter.${f}`)}
              </button>
            ))}
          </span>
        ) : (
          items.length > 0 && <span className="faint small">{t('ax.tl.filterLater')}</span>
        )}
      </div>
      <div className="ax-tl-scroll" ref={scroller} onScroll={(e) => setTail((s) => tailOnScroll(s, isAtBottom(e.currentTarget)))}>
        {shown.length ? (
          <ol className="ax-tl" aria-live={live ? 'polite' : undefined}>
            {shown.map((i) => (
              <TimelineEntry
                key={i.seq}
                item={i}
                flash={flash === i.seq}
                extra={i.seq === lastAsk ? questionExtra : undefined}
              />
            ))}
          </ol>
        ) : (
          <p className="ax-tl-empty">{state === 'loading' ? t('ax.tl.loading') : items.length ? t('ax.tl.noMatch') : t('ax.tl.empty')}</p>
        )}
        {live && (
          <div className="ax-waiting">
            <i className="ax-pulse" aria-hidden /> {t('ax.tl.waiting')}
          </div>
        )}
        {!tail.pinned && tail.unseen > 0 && (
          <button type="button" className="ax-newpill" onClick={toBottom}>
            {tn('ax.tl.new', tail.unseen)}
          </button>
        )}
      </div>
    </section>
  );
}
