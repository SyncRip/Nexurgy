// Asset vocabulary: type names and icons, source labels, publish / availability
// / engineering-review chips, hashes and the "open in workspace" mapping.
// Status is always icon + text; colour only reinforces it.

import {
  Archive,
  BadgeCheck,
  Ban,
  Blocks,
  Box,
  Calculator,
  CircleCheck,
  CircleDashed,
  CircleX,
  ClipboardList,
  Copy,
  File,
  FileText,
  FlaskConical,
  Import,
  LoaderCircle,
  Ruler,
  ScanText,
  Shapes,
  ShieldAlert,
  ShieldCheck,
  Tag,
} from 'lucide-react';
import type { ReactNode } from 'react';
import type { AssetAvailability, AssetType, AssetVersion, EngineeringState, KnownAssetLabel, PublishState } from '../../api/assets';
import { assetWorkspacePath } from '../../app/versionLinks';
import { Chip, type Tone } from '../../components/ui/Badges';
import { useToast } from '../../components/ui/Toast';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';

/** Translate a dynamic key, falling back to readable text for values the app does not know yet. */
export function dynLabel(t: TFunction, key: string, fallback: string): string {
  return hasKey(key) ? t(key as MessageKey) : fallback.replace(/_/g, ' ');
}

export const runPath = (runId: string, nodeId?: string | null) => (nodeId ? `/runs/${runId}?node=${encodeURIComponent(nodeId)}` : `/runs/${runId}`);

// ─── types ─────────────────────────────────────────────────────────────────

export function AssetTypeIcon({ type }: { type: AssetType | string }) {
  switch (type) {
    case 'part_model':
      return <Box aria-hidden />;
    case 'drawing':
      return <FileText aria-hidden />;
    case 'process_plan':
      return <ClipboardList aria-hidden />;
    case 'quote':
      return <Calculator aria-hidden />;
    case 'intake':
      return <ScanText aria-hidden />;
    case 'process_module':
      return <Blocks aria-hidden />;
    default:
      return <File aria-hidden />;
  }
}

export const assetTypeLabel = (t: TFunction, type: string) => dynLabel(t, `as.type.${type}`, type);

export function AssetTypeBadge({ type, withLabel = true }: { type: AssetType | string; withLabel?: boolean }) {
  const { t } = useI18n();
  return (
    <span className={`as-type as-type-${type}`} title={assetTypeLabel(t, type)}>
      <AssetTypeIcon type={type} />
      {withLabel && <span>{assetTypeLabel(t, type)}</span>}
    </span>
  );
}

/** The human version label (R3 / D2 / P1 / Q1 / v2 / I1) as a mono token. */
export function VersionToken({ label, title }: { label: string; title?: string }) {
  return (
    <span className="as-ver mono" title={title}>
      {label}
    </span>
  );
}

// ─── source labels ─────────────────────────────────────────────────────────

const LABEL_META: Record<KnownAssetLabel, { tone: Tone; icon: ReactNode }> = {
  simulated: { tone: 'stale', icon: <FlaskConical aria-hidden /> },
  estimated: { tone: 'warn', icon: <Ruler aria-hidden /> },
  imported: { tone: 'measure', icon: <Import aria-hidden /> },
  mesh_reference: { tone: 'pending', icon: <Shapes aria-hidden /> },
  from_failed_run: { tone: 'err', icon: <CircleX aria-hidden /> },
};

const isKnownLabel = (l: string): l is KnownAssetLabel => Object.prototype.hasOwnProperty.call(LABEL_META, l);

export const sourceLabelText = (t: TFunction, label: string) => dynLabel(t, `as.label.${label}`, label);

export function SourceLabelChip({ label }: { label: string }) {
  const { t } = useI18n();
  const meta = isKnownLabel(label) ? LABEL_META[label] : { tone: 'pending' as Tone, icon: <Tag aria-hidden /> };
  return (
    <Chip tone={meta.tone} icon={meta.icon} title={dynLabel(t, `as.labelHint.${label}`, label)} className="as-label-chip">
      {sourceLabelText(t, label)}
    </Chip>
  );
}

/** Label chips; simulated / estimated / from_failed_run always first so they are never cut off. */
export function SourceLabels({ labels, max = 4, emptyDash = true }: { labels: string[]; max?: number; emptyDash?: boolean }) {
  if (!labels.length) return emptyDash ? <span className="faint">—</span> : null;
  const order = ['simulated', 'estimated', 'from_failed_run', 'mesh_reference', 'imported'];
  const sorted = [...labels].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  const shown = sorted.slice(0, max);
  return (
    <span className="as-labels">
      {shown.map((l) => (
        <SourceLabelChip key={l} label={l} />
      ))}
      {sorted.length > max && <span className="tag more">+{sorted.length - max}</span>}
    </span>
  );
}

/** Prominent explanations for labels that change how a result may be used. */
export function LabelCallouts({ labels }: { labels: string[] }) {
  const { t } = useI18n();
  const important = (['simulated', 'estimated', 'from_failed_run', 'mesh_reference'] as const).filter((l) => labels.includes(l));
  if (!important.length) return null;
  return (
    <div className="stack-sm">
      {important.map((l) => (
        <div key={l} className={`callout ${l === 'from_failed_run' ? 'err' : l === 'mesh_reference' ? 'pending' : 'warn'} as-label-callout`} role="note">
          {LABEL_META[l].icon}
          <div>
            <b>{sourceLabelText(t, l)}</b> · <span>{t(`as.labelHint.${l}`)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── states ────────────────────────────────────────────────────────────────

const PUBLISH_META: Record<PublishState, { tone: Tone; icon: ReactNode }> = {
  draft: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  published: { tone: 'ok', icon: <BadgeCheck aria-hidden /> },
  deprecated: { tone: 'stale', icon: <Archive aria-hidden /> },
};

export function PublishChip({ state }: { state: PublishState }) {
  const { t } = useI18n();
  const m = PUBLISH_META[state] ?? PUBLISH_META.draft;
  return (
    <Chip tone={m.tone} icon={m.icon} title={t('as.publish.hint')}>
      {t(`as.publish.${state}`)}
    </Chip>
  );
}

const AVAIL_META: Record<AssetAvailability, { tone: Tone; icon: ReactNode }> = {
  building: { tone: 'measure', icon: <LoaderCircle aria-hidden /> },
  ready: { tone: 'ok', icon: <CircleCheck aria-hidden /> },
  failed: { tone: 'err', icon: <CircleX aria-hidden /> },
};

export function AvailabilityChip({ availability }: { availability: AssetAvailability }) {
  const { t } = useI18n();
  const m = AVAIL_META[availability] ?? AVAIL_META.ready;
  return (
    <Chip tone={m.tone} icon={m.icon}>
      {t(`as.avail.${availability}`)}
    </Chip>
  );
}

export type EngineeringStateId = 'not_required' | 'approved' | 'rejected' | 'invalidated' | 'not_reviewed';

/** not_required (documents) / approved (valid now) / rejected / invalidated (approved earlier, content changed) / not_reviewed. */
export function engineeringStateOf(e: EngineeringState | null | undefined): EngineeringStateId {
  if (!e) return 'not_required';
  if (e.approved) return 'approved';
  if (e.decision?.decision === 'rejected') return 'rejected';
  if (e.decision?.decision === 'approved') return 'invalidated';
  return 'not_reviewed';
}

const ENG_META: Record<EngineeringStateId, { tone: Tone; icon: ReactNode }> = {
  not_required: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
  approved: { tone: 'ok', icon: <ShieldCheck aria-hidden /> },
  rejected: { tone: 'err', icon: <Ban aria-hidden /> },
  invalidated: { tone: 'stale', icon: <ShieldAlert aria-hidden /> },
  not_reviewed: { tone: 'pending', icon: <CircleDashed aria-hidden /> },
};

export const stageText = (t: TFunction, stage: string | null | undefined) => (stage ? dynLabel(t, `as.stage.${stage}`, stage) : '');

export function EngineeringChip({ engineering, compact }: { engineering: EngineeringState | null | undefined; compact?: boolean }) {
  const { t } = useI18n();
  const id = engineeringStateOf(engineering);
  if (id === 'not_required' && compact) return <span className="faint small">{t('as.eng.not_required')}</span>;
  const m = ENG_META[id];
  const stage = stageText(t, engineering?.stage);
  return (
    <Chip tone={m.tone} icon={m.icon} title={t(`as.engHint.${id}`, { stage })}>
      {compact || !stage ? t(`as.eng.${id}`) : t('as.eng.withStage', { state: t(`as.eng.${id}`), stage })}
    </Chip>
  );
}

// ─── hashes ────────────────────────────────────────────────────────────────

/** Shortened hash in mono with the full value as tooltip and a copy button. */
export function HashText({ hash, chars = 12, copy = true }: { hash: string | null | undefined; chars?: number; copy?: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  if (!hash) return <span className="faint">—</span>;
  return (
    <span className="as-hash">
      <code className="mono" title={hash}>
        {hash.length > chars ? `${hash.slice(0, chars)}…` : hash}
      </code>
      {copy && (
        <button
          type="button"
          className="btn ghost icon sm"
          aria-label={t('as.copyHash')}
          title={t('as.copyHash')}
          onClick={() => {
            const done = () => toast(t('as.hashCopied'), { tone: 'info' });
            if (navigator.clipboard?.writeText) navigator.clipboard.writeText(hash).then(done, () => toast(t('as.copyFailed'), { tone: 'err' }));
            else toast(t('as.copyFailed'), { tone: 'err' });
          }}
        >
          <Copy />
        </button>
      )}
    </span>
  );
}

// ─── workspace links ───────────────────────────────────────────────────────

export type WorkspaceTarget =
  | { kind: 'link'; to: string; labelKey: MessageKey }
  | { kind: 'download'; sha: string; filename: string }
  | null;

/** Where the specialised viewer for this exact version lives (part workspace tab at this version, S6; or a file
 * download). Never the part alone: that would open whatever is newest. */
export function workspaceTarget(v: Pick<AssetVersion, 'asset_type' | 'target'>): WorkspaceTarget {
  if (v.asset_type === 'document') {
    const s = v.target.summary as { blob_sha256?: string; original_name?: string };
    return s.blob_sha256 ? { kind: 'download', sha: s.blob_sha256, filename: s.original_name || 'download' } : null;
  }
  const to = assetWorkspacePath(v.asset_type, v.target);
  if (!to) return null;
  return { kind: 'link', to, labelKey: `as.openIn.${v.asset_type as Exclude<AssetType, 'document' | 'process_module'>}` };
}
