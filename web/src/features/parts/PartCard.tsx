// Part card (prototype pcard()): thumbnail with the model revision and the
// latest run's state, then number, name · material, the flow progress bar and
// "workflow · done/total". The thumbnail shows the current model revision,
// with its drawing's iso view as a fallback when the model cannot be previewed.

import { Ban, CheckCircle2, CircleDashed, Clock, Hand, HelpCircle, Loader2, XCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { getBlob, listDrawings } from '../../api/endpoints';
import type { Part } from '../../api/types';
import { paths } from '../../app/nav';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { pickPreviewDrawing, runFlowName, viewPaths, type DrawingPreview, type FlowProgress, type PartFlowSummary, type PartRunState } from './model';
import './parts.css';

const STATE_TONE: Record<PartRunState, Tone> = {
  none: 'pending',
  unknown: 'pending',
  queued: 'measure',
  running: 'measure',
  waiting: 'warn',
  failed: 'err',
  cancelled: 'pending',
  done: 'ok',
};

function StateIcon({ state }: { state: PartRunState }) {
  switch (state) {
    case 'done':
      return <CheckCircle2 aria-hidden />;
    case 'running':
      return <Loader2 className="pc-spin" aria-hidden />;
    case 'queued':
      return <Clock aria-hidden />;
    case 'waiting':
      return <Hand aria-hidden />;
    case 'failed':
      return <XCircle aria-hidden />;
    case 'cancelled':
      return <Ban aria-hidden />;
    case 'unknown':
      return <HelpCircle aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

/** State of a part's (or task's) latest run: colour, icon and text. */
export function RunStateChip({ state, className = '' }: { state: PartRunState; className?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone={STATE_TONE[state]} icon={<StateIcon state={state} />} className={`pc-state ${className}`}>
      {t(`home.state.${state}`)}
    </Chip>
  );
}

/** One square per node, finished first; the label carries the numbers. */
export function FlowBar({ progress }: { progress: FlowProgress }) {
  const { t } = useI18n();
  return (
    <span className="pc-bar" role="img" aria-label={t('home.card.progress', { done: progress.done, total: progress.total })}>
      {progress.segments.map((s, i) => (
        <i key={i} className={`seg seg-${s}`} />
      ))}
    </span>
  );
}

// ─── thumbnail ─────────────────────────────────────────────────────────────

/** Drawing lookups per part revision, shared by all cards for a minute (lists re-render on filter changes). */
const lookups = new Map<string, { at: number; p: Promise<DrawingPreview | null> }>();
const LOOKUP_TTL = 60_000;

function lookupPreview(part: Part): Promise<DrawingPreview | null> {
  const rev = part.latest_revision;
  if (!rev || rev.status !== 'ready') return Promise.resolve(null);
  const key = `${part.id}:${rev.id}`;
  const hit = lookups.get(key);
  if (hit && Date.now() - hit.at < LOOKUP_TTL) return hit.p;
  const p = listDrawings(part.id).then((ds) => pickPreviewDrawing(ds, rev.id));
  p.catch(() => lookups.delete(key));
  lookups.set(key, { at: Date.now(), p });
  return p;
}

/** Outline paths per drawing and crop (blobs are immutable by hash). */
const shapes = new Map<string, Promise<string[]>>();

function loadShape(pick: DrawingPreview): Promise<string[]> {
  const crop = pick.viewBox;
  if (!crop) return Promise.resolve([]);
  const key = `${pick.sha}:${crop.join(',')}`;
  let p = shapes.get(key);
  if (!p) {
    p = getBlob(pick.sha)
      .then((b) => b.text())
      .then((text) => viewPaths(text, crop));
    p.catch(() => shapes.delete(key));
    shapes.set(key, p);
  }
  return p;
}

interface Loaded {
  paths: string[];
  crop: [number, number, number, number];
  revNo: number | null;
}

function Placeholder({ title }: { title: string }) {
  return (
    <svg className="pc-ph" viewBox="0 0 112 84" role="img" aria-label={title}>
      <title>{title}</title>
      <path d="M56 14 L88 30 L88 58 L56 74 L24 58 L24 30 Z M24 30 L56 46 L88 30 M56 46 L56 74" />
    </svg>
  );
}

export function PartThumb({ part }: { part: Part }) {
  return <RevisionPartThumb key={`${part.id}:${part.latest_revision?.id ?? 'none'}`} part={part} />;
}

function RevisionPartThumb({ part }: { part: Part }) {
  const { t } = useI18n();
  const host = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [modelSrc, setModelSrc] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const revId = part.latest_revision?.status === 'ready' ? part.latest_revision.id : null;

  // Only cards on (or near) the screen request a preview.
  useEffect(() => {
    const el = host.current;
    if (!el || visible) return;
    const box = el.getBoundingClientRect();
    if (typeof IntersectionObserver === 'undefined' || (box.bottom > -120 && box.top < window.innerHeight + 120)) {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          io.disconnect();
        }
      },
      { rootMargin: '120px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visible]);

  useEffect(() => {
    setLoaded(null);
    setModelSrc(null);
    setDone(false);
    if (!visible || !revId) {
      setDone(true);
      return;
    }
    let live = true;
    (async () => {
      try {
        const { loadModelThumbnail } = await import('./modelThumbnail');
        if (!live) return;
        const src = await loadModelThumbnail(part.id, revId);
        if (!live) return;
        if (src) {
          setModelSrc(src);
          return;
        }
      } catch {
        // A drawing of this same revision can still provide a preview.
      }
      if (!live) return;
      const pick = await lookupPreview(part);
      if (pick?.viewBox && live) {
        const paths = await loadShape(pick);
        if (live && paths.length) setLoaded({ paths, crop: pick.viewBox, revNo: pick.revNo });
      }
    })()
      .catch(() => {
        /* the placeholder stays: a preview is decoration, the card still works */
      })
      .finally(() => {
        if (live) setDone(true);
      });
    return () => {
      live = false;
    };
    // A revision change remounts this component so the previous image cannot linger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, revId, part.id]);

  let body;
  if (modelSrc) {
    body = <img className="pc-model" src={modelSrc} width={300} height={164} decoding="async" draggable={false} alt={t('home.card.modelPreviewAlt', { number: part.number, rev: `R${part.latest_revision!.rev_no}` })} />;
  } else if (loaded) {
    const alt = t('home.card.previewAlt', { number: part.number, rev: loaded.revNo ? `R${loaded.revNo}` : '' });
    body = (
      <svg className="pc-drawing" viewBox={loaded.crop.join(' ')} preserveAspectRatio="xMidYMid meet" role="img" aria-label={alt}>
        <title>{alt}</title>
        {loaded.paths.map((d, i) => (
          <path key={i} d={d} vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
    );
  } else {
    body = <Placeholder title={done ? t('home.card.noPreview') : t('home.card.loadingPreview')} />;
  }
  return (
    <span ref={host} className={`pc-pic ${modelSrc ? 'has-model' : loaded ? 'has-drawing' : ''}`}>
      {body}
    </span>
  );
}

// ─── card ──────────────────────────────────────────────────────────────────

export function PartCard({ part, summary }: { part: Part; summary: PartFlowSummary | undefined }) {
  const { t } = useI18n();
  const rev = part.latest_revision;
  const run = summary?.run ?? null;
  const state = summary?.state ?? 'unknown';
  const name = run ? runFlowName(run) : '';
  return (
    <Link to={paths.part(part.id)} className="pc">
      <span className="pc-th">
        <span className="pc-rev">{rev ? `R${rev.rev_no}` : t('home.card.noModel')}</span>
        <PartThumb part={part} />
      </span>
      <span className="pc-meta">
        <span className="pc-heading"><span className="pc-pn">{part.number}</span>{state !== 'unknown' && <RunStateChip state={state} />}</span>
        <span className="pc-nm" title={part.name}>{part.name}</span>
        {part.material && <span className="pc-material" title={part.material}>{part.material}</span>}
        {summary?.progress && summary.progress.total > 0 ? <FlowBar progress={summary.progress} /> : <span className="pc-bar empty" aria-hidden />}
        <span className="pc-prog">
          <span className="ellip">{run ? name || t('home.card.noRun') : state === 'unknown' ? t('home.card.unknownRun') : t('home.card.noRun')}</span>
          {summary?.progress && summary.progress.total > 0 && (
            <span className="nowrap">
              {summary.progress.done}/{summary.progress.total}
            </span>
          )}
        </span>
      </span>
    </Link>
  );
}
