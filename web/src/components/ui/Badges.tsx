// Status vocabulary: colour is always paired with an icon and a text label.

import { Archive, CheckCircle2, CircleDashed, Eye, PencilRuler } from 'lucide-react';
import type { ReactNode } from 'react';
import type { FileKind, PartStatus, Role } from '../../api/types';
import { useT } from '../../i18n/I18nProvider';

export type Tone = 'ok' | 'warn' | 'err' | 'pending' | 'accent' | 'measure' | 'stale';

export function Chip({ tone, icon, children, title, className = '' }: { tone: Tone; icon?: ReactNode; children: ReactNode; title?: string; className?: string }) {
  return (
    <span className={`chip ${tone} ${className}`} title={title}>
      {icon}
      {children}
    </span>
  );
}

const PART_STATUS_TONE: Record<PartStatus, Tone> = {
  draft: 'pending',
  in_design: 'measure',
  in_review: 'warn',
  released: 'ok',
  archived: 'pending',
};

function PartStatusIcon({ status }: { status: PartStatus }) {
  switch (status) {
    case 'released':
      return <CheckCircle2 aria-hidden />;
    case 'in_review':
      return <Eye aria-hidden />;
    case 'in_design':
      return <PencilRuler aria-hidden />;
    case 'archived':
      return <Archive aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

export function PartStatusChip({ status }: { status: PartStatus }) {
  const t = useT();
  return (
    <Chip tone={PART_STATUS_TONE[status] ?? 'pending'} icon={<PartStatusIcon status={status} />}>
      {t(`partStatus.${status}`)}
    </Chip>
  );
}

export function RoleBadge({ role, compact }: { role: Role | null | undefined; compact?: boolean }) {
  const t = useT();
  if (!role) return null;
  return (
    <span className={`role-badge role-${role} ${compact ? 'compact' : ''}`}>
      <span className="dot" aria-hidden />
      {t(`role.${role}`)}
    </span>
  );
}

const KIND_SHORT: Record<FileKind, string> = {
  model_step: 'STEP',
  model_mesh: 'MESH',
  drawing_pdf: 'PDF',
  drawing_image: 'IMG',
  drawing_dxf: 'DXF',
  document: 'DOC',
  other: 'FILE',
};

export function KindBadge({ kind, withLabel = true }: { kind: FileKind; withLabel?: boolean }) {
  const t = useT();
  return (
    <span className={`kind-badge kind-${kind}`} title={t(`fileKind.${kind}`)}>
      <span className="kind-code">{KIND_SHORT[kind] ?? 'FILE'}</span>
      {withLabel && <span className="kind-label">{t(`fileKind.${kind}`)}</span>}
    </span>
  );
}

export function ArchivedChip() {
  const t = useT();
  return (
    <Chip tone="pending" icon={<Archive aria-hidden />}>
      {t('common.archived')}
    </Chip>
  );
}

export function TagList({ tags, max = 4 }: { tags: string[]; max?: number }) {
  if (!tags.length) return <span className="faint">—</span>;
  const shown = tags.slice(0, max);
  return (
    <span className="tag-list">
      {shown.map((tg) => (
        <span key={tg} className="tag">
          {tg}
        </span>
      ))}
      {tags.length > max && <span className="tag more">+{tags.length - max}</span>}
    </span>
  );
}
