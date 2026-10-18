// Building blocks shared by the assistant panels: operation status (icon +
// text), reference chips with version / scope / source / status, error
// vocabulary and labels. Status is never colour alone.

import {
AlertTriangle,
Ban,
Box,
CheckCircle2,
CircleDot,
Clock,
ExternalLink,
FileText,
FlaskConical,
Hand,
Hash,
Layers,
ListChecks,
Loader2,
MessageSquareText,
Package,
PlayCircle,
Receipt,
Target,
TextQuote,
X,
XCircle,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { AiContextUsage,AiMaterial,AiMaterialPlacement,AiOperation,AiOperationKind,AiOperationStatus,AiOverBudgetDetails,AiRef,AiRefInput,AiRefKind,AiRefStatus } from '../../api/assistant';
import { exactSourcePath } from '../../app/versionLinks';
import { Chip,type Tone } from '../../components/ui/Badges';
import { ErrorNotice } from '../../components/ui/States';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import type { ErrorOverrides } from '../../i18n/errors';
import { useI18n,type TFunction } from '../../i18n/I18nProvider';
import { planOutcome } from './planApply';
import { nodeDisplayName,nodeStatusLabel,nodeTypeLabel } from '../workflow/labels';

/** Translate a dynamic key when it exists, else the fallback. */
export function tk(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

// ─── errors ────────────────────────────────────────────────────────────────

const ERROR_CODES = [
  'conversation_archived',
  'ref_not_in_composer',
  'target_unavailable',
  'target_required',
  'drawing_format_mismatch',
  'ref_archived',
  'ref_wrong_project',
  'ref_build_mismatch',
  'ref_op_not_found',
  'ref_field_not_found',
  'ref_output_not_found',
  'ref_not_in_conversation',
  'ref_unavailable',
  'selection_required',
  'field_required',
  'excerpt_required',
  'output_required',
  'reference_keys_required',
  'too_many_references',
  'too_many_constraints',
  'constraint_empty',
  'text_required',
  'text_too_long',
  'mesh_reference',
  'part_required',
  'part_busy',
  'task_not_writable',
  'use_collaboration_actions',
  'context_over_budget',
  'material_not_allowed',
  'material_consent_required',
  'material_read_failed',
  'material_not_included',
  'material_not_read',
  'ai_unavailable',
  'not_waiting',
  'not_cancellable',
  'not_retryable',
  'already_retried',
  'nothing_to_fork',
  'interrupted',
  'run_missing',
  'internal_error',
  'version_conflict',
  'forbidden',
  'not_found',
  'network',
  'project_not_active',
  // generation supplement A (attachments and input checks)
  'attachment_required',
  'attachment_ambiguous',
  'spec_and_sources',
  'several_spec_versions',
  'source_unread',
  'source_frame_unresolved',
  'spec_no_parts',
  'extraction_missed_parts',
  'spec_structure_incomplete',
  // AI-native main agent
  'agent_busy',
  'follow_up_response_missing',
  'follow_up_context_missing',
  'draft_closed',
  'awaiting_confirmation',
  'design_response_missing',
  'design_not_waiting',
  'design_stale',
  'design_hash_mismatch',
  'design_scope_mismatch',
  'design_not_authorized',
  'design_answer_invalid',
  'design_idempotency_conflict',
  'design_plan_cancelled',
  'awaiting_design',

  'phase_2',
  'plan_hash_mismatch',
  'plan_changed',
  'plan_not_ready',
  'head_changed',
  'workflow_changed',
  'removals_need_confirmation',
] as const;

/** Server error codes of the assistant → friendly messages (the raw code stays visible). */
export const AS_ERRORS: ErrorOverrides = Object.fromEntries(ERROR_CODES.map((c) => [c, `as.err.${c}` as MessageKey]));

export function AsError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return <ErrorNotice error={error} overrides={AS_ERRORS} onRetry={onRetry} />;
}

// ─── context budget / usage ────────────────────────────────────────────────

/** Real usage only from the provider's counts; simulated or missing counts are "unknown", never 0. */
export function usageText(t: TFunction, u: AiContextUsage | null | undefined): string {
  if (u?.simulated) return t('as.usage.simulated');
  if (!u || u.unknown || u.tokens_in === null || u.tokens_in === undefined) return t('as.usage.unknown');
  return t('as.usage.actual', { input: u.tokens_in, output: u.tokens_out ?? '—' });
}

export function modelLabelText(t: TFunction, label: string | null | undefined): string {
  if (!label) return '';
  if (label === 'simulated') return t('as.model.simulated');
  if (label === 'unavailable') return t('as.model.unavailable');
  return label;
}

/** Required content does not fit (U18): sizes (estimates), the largest parts and how to narrow the request. */
export function OverBudget({ details }: { details: Partial<AiOverBudgetDetails> | Record<string, unknown> | undefined }) {
  const { t } = useI18n();
  const d = (details ?? {}) as Partial<AiOverBudgetDetails>;
  const suggestions = Array.isArray(d.suggestions) ? d.suggestions : [];
  const largest = Array.isArray(d.largest) ? d.largest : [];
  const notIncluded = Array.isArray(d.not_included) ? d.not_included : [];
  return (
    <div className="small stack-sm as-over">
      {notIncluded.length > 0 && (
        <div>
          <div className="strong">{t('as.budget.notIncluded')}</div>
          <ul className="as-plain">
            {notIncluded.map((m) => (
              <li key={m.key}>
                {m.label}
                {typeof m.chars === 'number' ? <span className="faint"> · {t('as.budget.chars', { n: m.chars })}</span> : null}
                {m.reason ? <span className="faint"> · {tk(t, `as.material.reason.${m.reason}`, m.reason)}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
      {typeof d.required_est === 'number' && typeof d.available === 'number' && (
        <div>
          {t('as.budget.over', { required: d.required_est, available: d.available })}
          {d.limit ? <span className="faint"> · {tk(t, `as.budget.limitKind.${d.limit}`, String(d.limit))}</span> : null}
        </div>
      )}
      {largest.length > 0 && (
        <div>
          <div className="strong">{t('as.budget.largest')}</div>
          <ul className="as-plain">
            {largest.map((x, i) => (
              <li key={i}>
                {x.label}
                {x.source ? <span className="faint"> · {tk(t, `as.source.${x.source}`, x.source)}</span> : null} · {t('as.budget.tokensEst', { n: x.tokens_est })}
              </li>
            ))}
          </ul>
        </div>
      )}
      {suggestions.length > 0 && (
        <div>
          <div className="strong">{t('as.budget.suggest')}</div>
          <ul className="as-plain">
            {suggestions.map((sg, i) => {
              const code = typeof sg === 'string' ? '' : sg.code;
              const msg = typeof sg === 'string' ? sg : sg.message;
              return <li key={i}>{code ? tk(t, `as.err.suggest.${code}`, msg) : msg}</li>;
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

// ─── materials (S2) ─────────────────────────────────────────────────────────

/** Under a request whose text was over the chat limit: it was saved as a project material and referenced. */
export function MaterialSaved({ payload }: { payload: Record<string, unknown> | null | undefined }) {
  const { t } = useI18n();
  const m = payload?.material as Partial<AiMaterial> | undefined;
  if (!m || typeof m !== 'object' || !m.key) return null;
  return (
    <p className="as-ctx-note as-material-note">
      <FileText aria-hidden /> {t('as.material.saved', { n: m.chars ?? 0, name: m.name ?? '', max: m.limit ?? 0 })}
    </p>
  );
}

export function materialMethodText(t: TFunction, m: AiMaterialPlacement): string {
  if (m.method === 'chunked') {
    return m.pending ? t('as.material.method.chunkedPending', { chunks: m.chunks ?? 0 }) : t('as.material.method.chunked', { chunks: m.chunks ?? 0, calls: m.calls ?? 0 });
  }
  return tk(t, `as.material.method.${m.method}`, m.method);
}

/** How each material of a request reached the model (full / digest / chunked / by the spec step / not included). */
export function MaterialPlacements({ items }: { items: AiMaterialPlacement[] | null | undefined }) {
  const { t } = useI18n();
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <ul className="as-plain small as-materials">
      {items.map((m) => (
        <li key={m.key}>
          <FileText aria-hidden /> {t('as.material.line', { label: m.label, method: materialMethodText(t, m) })}
          {m.reason ? <span className="faint"> · {tk(t, `as.material.reason.${m.reason}`, m.reason)}</span> : null}
        </li>
      ))}
    </ul>
  );
}

// ─── operation status ──────────────────────────────────────────────────────

const OP_TONE: Record<AiOperationStatus, Tone> = {
  pending: 'pending',
  running: 'measure',
  waiting_input: 'warn',
  // Generation finished is not acceptance: neutral blue, never the green "approved" look.
  succeeded: 'measure',
  failed: 'err',
  cancelled: 'pending',
};

export function OpStatusIcon({ status }: { status: AiOperationStatus }) {
  switch (status) {
    case 'running':
      return <Loader2 className="spin-icon" aria-hidden />;
    case 'waiting_input':
      return <Hand aria-hidden />;
    case 'succeeded':
      return <CheckCircle2 aria-hidden />;
    case 'failed':
      return <XCircle aria-hidden />;
    case 'cancelled':
      return <Ban aria-hidden />;
    default:
      return <Clock aria-hidden />;
  }
}

export function OpStatusChip({ status, label }: { status: AiOperationStatus; label?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone={OP_TONE[status] ?? 'pending'} icon={<OpStatusIcon status={status} />}>
      {label ?? t(`as.status.${status}`)}
    </Chip>
  );
}

/** Planning succeeded does not imply a saved product version. */
export function OperationStatusChip({ op }: { op: AiOperation }) {
  const { t } = useI18n();
  const outcome = planOutcome(op);
  if (!outcome) return <OpStatusChip status={op.status} />;
  return <Chip tone={outcome === 'applied' ? 'ok' : 'pending'} icon={outcome === 'plan_ready' ? <Hand aria-hidden /> : <CheckCircle2 aria-hidden />}>{t(`core.plan.${outcome}`)}</Chip>;
}

export function opKindLabel(t: TFunction, kind: AiOperationKind | string): string {
  return tk(t, `as.kind.${kind}`, kind);
}

/** Quality / provenance labels of a result version (simulated, estimated …); never hidden. */
export function ResultLabels({ labels }: { labels: string[] | null | undefined }) {
  const { t } = useI18n();
  if (!labels?.length) return null;
  return (
    <>
      {labels.map((l) =>
        l === 'simulated' ? (
          <SimChip key={l} />
        ) : (
          <Chip key={l} tone={l === 'from_failed_run' ? 'err' : l === 'estimated' ? 'stale' : 'pending'} className="sm">
            {tk(t, `as.label.${l}`, l)}
          </Chip>
        ),
      )}
    </>
  );
}

export function SimChip({ title }: { title?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone="warn" icon={<FlaskConical aria-hidden />} title={title ?? t('as.simulated.hint')}>
      {t('as.simulated')}
    </Chip>
  );
}

// ─── references ────────────────────────────────────────────────────────────

export function RefKindIcon({ kind }: { kind: AiRefKind }) {
  switch (kind) {
    case 'part':
      return <Box aria-hidden />;
    case 'part_revision':
      return <Layers aria-hidden />;
    case 'cad_selection':
      return <CircleDot aria-hidden />;
    case 'quote':
    case 'quote_field':
      return <Receipt aria-hidden />;
    case 'asset_version':
      return <Package aria-hidden />;
    case 'run_output':
    case 'run':
      return <PlayCircle aria-hidden />;
    case 'task':
    case 'criterion':
      return <ListChecks aria-hidden />;
    case 'file_version':
      return <FileText aria-hidden />;
    case 'text_excerpt':
      return <TextQuote aria-hidden />;
    default:
      return <Hash aria-hidden />;
  }
}

export function refKindLabel(t: TFunction, kind: AiRefKind): string {
  return tk(t, `as.refKind.${kind}`, kind);
}

function num(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '';
  return String(Math.round(n * 10 ** digits) / 10 ** digits);
}

/** A reference's display title: a node reference reads "workflow / node" (node name localised; `title` stays the object's name). */
export function refTitle(t: TFunction, ref: Pick<AiRef, 'title' | 'node'>): string {
  return ref.node ? `${ref.title} / ${nodeDisplayName(t, ref.node)}` : ref.title;
}

/** "Face #12 · Ø10 mm", "qty 20 · material", "“…”", "Modeling · waiting": what part of the object the reference pins. */
export function refScope(t: TFunction, ref: Pick<AiRef, 'selection' | 'field' | 'excerpt' | 'output' | 'node'>, lang: 'en' | 'zh' = 'en'): string | null {
  if (ref.node) return [nodeTypeLabel(t, ref.node.type), ...(ref.node.status ? [nodeStatusLabel(t, ref.node.status)] : [])].join(' · ');
  if (ref.selection) {
    const s = ref.selection;
    const parts = [tk(t, `as.entity.${s.entity}`, s.entity) + ` #${s.id}`];
    if (s.radius) parts.push(`Ø${num(s.radius * 2)} mm`);
    else if (s.length) parts.push(`${num(s.length)} mm`);
    if (s.resolved_op_id) parts.push(t('as.scope.op', { op: s.resolved_op_id }));
    else if (s.candidates?.length) parts.push(t('as.scope.candidates', { n: s.candidates.length }));
    return parts.join(' · ');
  }
  if (ref.field) {
    const f = ref.field;
    const label = (lang === 'zh' ? f.label_zh || f.label_en : f.label_en || f.label_zh) || f.label || tk(t, `as.quoteField.${f.key}`, f.key);
    return [t('as.scope.qty', { qty: f.tier_qty }), label, ...(f.estimated ? [t('as.scope.estimated')] : [])].join(' · ');
  }
  if (ref.excerpt) {
    const txt = ref.excerpt.text.replace(/\s+/g, ' ').trim();
    return `“${txt.length > 48 ? `${txt.slice(0, 48)}…` : txt}”`;
  }
  if (ref.output) return `${ref.output.node_id} · ${ref.output.output_key}`;
  return null;
}

/** The input that adds this exact reference again (continue from a result, re-add). */
export function refInputOf(ref: AiRef): AiRefInput {
  const input: AiRefInput = { kind: ref.kind, object_id: ref.object_id };
  if (ref.selection) input.selection = ref.selection;
  if (ref.field) input.field = { tier_qty: ref.field.tier_qty, key: ref.field.key };
  if (ref.excerpt) input.excerpt = ref.excerpt;
  if (ref.output) input.output = ref.output;
  if (ref.node) input.node = { node_id: ref.node.node_id };
  if (ref.kind === 'workflow' && ref.version != null) input.version = ref.version;
  if (ref.instance) input.instance = ref.instance;
  if (ref.entities) input.entities = ref.entities;
  return input;
}

export function RefStatusBadge({ status }: { status: AiRefStatus | null | undefined }) {
  const { t } = useI18n();
  if (!status || status.state === 'ok') return null;
  if (status.state === 'stale')
    return (
      <Chip tone="stale" icon={<AlertTriangle aria-hidden />} className="as-ref-badge" title={t('as.ref.staleHint')}>
        {status.latest_label ? t('as.ref.staleTo', { label: status.latest_label }) : t('as.ref.stale')}
      </Chip>
    );
  return (
    <Chip tone="err" icon={<XCircle aria-hidden />} className="as-ref-badge" title={t('as.ref.unavailableHint')}>
      {status.reason ? t('as.ref.unavailableWhy', { why: tk(t, `as.ref.reason.${status.reason}`, String(status.reason)) }) : t('as.ref.unavailable')}
    </Chip>
  );
}

/**
 * One reference: kind icon, title, exact version, scope, status and a link to
 * the source. `role="target"` marks the operation target (separate styling and
 * text, not colour alone). Actions (set as target, remove) are optional.
 */
export function RefChip({
  refItem,
  status,
  target,
  onRemove,
  onSetTarget,
  busy,
  compact,
  extra,
}: {
  refItem: AiRef;
  status?: AiRefStatus | null;
  target?: boolean;
  onRemove?: () => void;
  onSetTarget?: () => void;
  busy?: boolean;
  compact?: boolean;
  extra?: ReactNode;
}) {
  const { t, lang } = useI18n();
  const scope = refScope(t, refItem, lang);
  const title = refTitle(t, refItem);
  const name = `${refKindLabel(t, refItem.kind)} · ${title}${refItem.version_label ? ` ${refItem.version_label}` : ''}`;
  const unavailable = status?.state === 'unavailable';
  return (
    <span className={`as-ref ${target ? 'is-target' : ''} ${unavailable ? 'is-unavailable' : ''} ${compact ? 'compact' : ''}`} title={refItem.summary || name}>
      <span className="as-ref-kind" aria-hidden>
        {target ? <Target /> : <RefKindIcon kind={refItem.kind} />}
      </span>
      <span className="as-ref-main">
        <span className="as-ref-title">
          {target && <span className="sr-only">{t('as.target')}: </span>}
          <span className="as-ref-name">{title}</span>
          {refItem.version_label && <span className="as-ref-ver mono">{refItem.version_label}</span>}
        </span>
        {!compact && (
          <span className="as-ref-sub">
            {refKindLabel(t, refItem.kind)}
            {scope ? ` · ${scope}` : ''}
          </span>
        )}
      </span>
      <RefStatusBadge status={status} />
      {extra}
      {refItem.source_path && !compact && (
        <Link className="as-ref-act" to={exactSourcePath(refItem)} aria-label={t('as.ref.openSource', { name })} title={t('as.ref.openSource', { name })}>
          <ExternalLink />
        </Link>
      )}
      {onSetTarget && (
        <button type="button" className="as-ref-act" onClick={onSetTarget} disabled={busy} aria-label={t('as.ref.setTarget', { name })} title={t('as.ref.setTarget', { name })}>
          <Target />
        </button>
      )}
      {onRemove && (
        <button type="button" className="as-ref-act" onClick={onRemove} disabled={busy} aria-label={t('as.ref.remove', { name })} title={t('as.ref.remove', { name })}>
          <X />
        </button>
      )}
    </span>
  );
}

/** Small "assistant" avatar mark used by message groups. */
export function AsMark() {
  return (
    <span className="as-mark" aria-hidden>
      <MessageSquareText />
    </span>
  );
}
