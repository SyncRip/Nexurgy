// Workbench "parts by status" overview: a lightweight self-drawn SVG donut over
// the project's real part statuses (features/parts/model.partStatusBreakdown).
// Nothing is invented — a status with no part stays at zero and still shows in
// the legend. Arcs grow in on mount; reduced motion renders them in place.

import { useEffect, useState } from 'react';
import type { PartStatus } from '../../api/types';
import type { Tone } from '../../components/ui/Badges';
import { prefersReduced } from '../../hooks/useReveal';
import { useI18n } from '../../i18n/I18nProvider';
import { partStatusBreakdown, type StatusSlice } from '../parts/model';

// Donut geometry: a 36-radius ring on a 100×100 box. Circumference drives the
// dash maths so each slice is just a fraction of C.
const R = 36;
const C = 2 * Math.PI * R;
// A hairline gap between slices so adjacent colours stay legible.
const GAP = 1.2;

// Tone per status, matching the part badge (Badges.PART_STATUS_TONE) so the
// donut reads the same as the chips — except archived, promoted from the grey
// "pending" to "stale" (violet) so it never blends into draft.
const STATUS_TONE: Record<PartStatus, Tone> = {
  draft: 'pending',
  in_design: 'measure',
  in_review: 'warn',
  released: 'ok',
  archived: 'stale',
};
const TONE_COLOR: Record<Tone, string> = {
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  err: 'var(--err)',
  pending: 'var(--pending)',
  accent: 'var(--accent)',
  measure: 'var(--measure)',
  stale: 'var(--stale)',
};

/** The arc of one slice, its start offset and length along the ring. */
interface Arc extends StatusSlice {
  color: string;
  /** Clockwise rotation (deg) of this slice's start from 12 o'clock. */
  rotate: number;
  /** Visible arc length (ring units), gap already subtracted. */
  len: number;
}

function arcsOf(slices: readonly StatusSlice[], total: number): Arc[] {
  let acc = 0;
  const arcs: Arc[] = [];
  for (const s of slices) {
    if (!s.count) continue; // empty statuses draw nothing (still in the legend)
    const rotate = (acc / total) * 360;
    // Full-ring single slice keeps no gap, else trim a gap so edges don't touch.
    const raw = s.fraction * C;
    const len = s.count === total ? raw : Math.max(0, raw - GAP);
    arcs.push({ ...s, color: TONE_COLOR[STATUS_TONE[s.status]], rotate, len });
    acc += s.count;
  }
  return arcs;
}

export function StatusDonut({ parts }: { parts: readonly { status: PartStatus }[] }) {
  const { t } = useI18n();
  const { total, slices } = partStatusBreakdown(parts);
  // grown flips to true one frame after mount, letting the dash transition run;
  // under reduced motion it starts true so arcs appear at full length at once.
  const [grown, setGrown] = useState(() => prefersReduced());
  useEffect(() => {
    if (grown) return;
    const id = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(id);
  }, [grown]);

  if (!total) return null; // no parts: the "start" card already covers this
  const arcs = arcsOf(slices, total);

  return (
    <div className="wb-donut-wrap">
      <div className="wb-donut" role="img" aria-label={t('wb.chart.aria', { total })}>
        <svg viewBox="0 0 100 100" aria-hidden>
          <circle className="wb-donut-track" cx="50" cy="50" r={R} />
          {arcs.map((a) => (
            <circle
              key={a.status}
              className="wb-donut-seg"
              cx="50"
              cy="50"
              r={R}
              stroke={a.color}
              // Dash shows one arc; dashoffset shrinks from full ring (hidden)
              // to 0 (drawn), so each slice sweeps in from its start point.
              strokeDasharray={`${a.len} ${C - a.len}`}
              strokeDashoffset={grown ? 0 : a.len}
              transform={`rotate(${a.rotate - 90} 50 50)`}
            />
          ))}
        </svg>
        <div className="wb-donut-center">
          <strong>{total}</strong>
          <span>{t('wb.chart.parts')}</span>
        </div>
      </div>
      <ul className="wb-donut-legend">
        {slices.map((s) => (
          <li key={s.status} className={s.count ? '' : 'is-empty'}>
            <span className="wb-donut-sw" style={{ background: TONE_COLOR[STATUS_TONE[s.status]] }} aria-hidden />
            <span className="wb-donut-name">{t(`partStatus.${s.status}`)}</span>
            <span className="wb-donut-val">{s.count}</span>
            <span className="wb-donut-pct">{Math.round(s.fraction * 100)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
