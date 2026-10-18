// Cost breakdown bar for a quote tier (design-system §4.5): a lightweight
// self-drawn stacked bar over the tier's real cost lines — nothing invented,
// each segment is one CostLine's share of the total. Segments grow in on mount;
// reduced motion renders them at full width at once. The dense cost table below
// stays plain (work-surface restraint) — only this summary bar is animated.

import { useEffect, useState } from 'react';
import type { CostLine } from '../../api/types';
import { prefersReduced } from '../../hooks/useReveal';
import { useI18n } from '../../i18n/I18nProvider';
import { useLabel } from './common';

// One colour per cost key, reusing palette tokens so dark / light follow along.
const KEY_COLOR: Record<CostLine['key'], string> = {
  material: 'var(--measure)',
  machining: 'var(--brand-cyan)',
  setup: 'var(--warn)',
  subcontract: 'var(--stale)',
  loss: 'var(--pending)',
};

export function CostBreakdownBar({ lines, money }: { lines: readonly CostLine[]; money: (fen: number) => string }) {
  const { t } = useI18n();
  const label = useLabel();
  // grown flips true one frame after mount so the width transition runs; under
  // reduced motion it starts true (segments appear at full width immediately).
  const [grown, setGrown] = useState(() => prefersReduced());
  useEffect(() => {
    if (grown) return;
    const id = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(id);
  }, [grown]);

  // Only positive lines carry a share; a zero or negative total has no bar.
  const shown = lines.filter((l) => l.fen > 0);
  const total = shown.reduce((sum, l) => sum + l.fen, 0);
  if (!total) return null;

  return (
    <div className="qt-breakdown">
      <div className="qt-breakdown-bar" role="img" aria-label={t('ws.quote.breakdownAria')}>
        {shown.map((l) => (
          <span
            key={l.key}
            className="qt-breakdown-seg"
            style={{ width: grown ? `${(l.fen / total) * 100}%` : '0%', background: KEY_COLOR[l.key] }}
            title={`${label(l)} · ${money(l.fen)}`}
          />
        ))}
      </div>
      <ul className="qt-breakdown-legend">
        {shown.map((l) => (
          <li key={l.key}>
            <span className="qt-breakdown-sw" style={{ background: KEY_COLOR[l.key] }} aria-hidden />
            <span className="qt-breakdown-name">{label(l)}</span>
            <span className="qt-breakdown-pct">{Math.round((l.fen / total) * 100)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
