// Status chip: colour, icon and text always appear together. There is no tone
// prop, so a caller can't pass colour alone; the icon comes from `status` and
// `label` is required. Skin: StatusChip.css (`pr-status`).

import { CircleCheck, CircleDashed, CircleX, History, Info, LoaderCircle, TriangleAlert, type LucideIcon } from 'lucide-react';
import './StatusChip.css';

export type StatusKind = 'ok' | 'warn' | 'err' | 'running' | 'pending' | 'stale' | 'info';

const STATUS_ICON: Record<StatusKind, LucideIcon> = {
  ok: CircleCheck,
  warn: TriangleAlert,
  err: CircleX,
  running: LoaderCircle,
  pending: CircleDashed,
  stale: History,
  info: Info,
};

export interface StatusChipProps {
  status: StatusKind;
  /** Visible text; colour and icon never stand in for it. */
  label: string;
  /** Extra detail on hover (e.g. the raw state or a timestamp). */
  title?: string;
  className?: string;
}

export function StatusChip({ status, label, title, className }: StatusChipProps) {
  const Icon = STATUS_ICON[status];
  return (
    <span className={['pr-status', `pr-status--${status}`, className].filter(Boolean).join(' ')} data-status={status} title={title}>
      <Icon aria-hidden className="pr-status__icon" />
      <span className="pr-status__label">{label}</span>
    </span>
  );
}
