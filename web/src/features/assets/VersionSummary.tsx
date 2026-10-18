// Per-type content summary of one asset version (services/assets._summary).
// Values are shown as the server reports them; nothing is derived or invented.

import { Download, ExternalLink } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { downloadBlob } from '../../api/blobs';
import type { AssetVersion } from '../../api/assets';
import { useToast } from '../../components/ui/Toast';
import { describeError } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import { HashText, dynLabel, workspaceTarget } from './meta';

type Obj = Record<string, unknown>;
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

function formatBbox(v: unknown): string | null {
  const vals = Array.isArray(v) ? v : v && typeof v === 'object' ? ['x', 'y', 'z'].map((k) => (v as Obj)[k]) : null;
  if (!vals || !vals.length || !vals.every((x) => typeof x === 'number')) return null;
  return `${(vals as number[]).map((x) => Math.round(x * 100) / 100).join(' × ')} mm`;
}

export function Facts({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="as-facts">
      {rows.map(([k, v], i) => (
        <div key={i} className="as-fact">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function VersionSummary({ version }: { version: AssetVersion }) {
  const { t, fmt } = useI18n();
  const s = version.target.summary as Obj;
  const yesNo = (b: unknown) => (b ? t('as.yes') : t('as.no'));
  const dash = <span className="faint">—</span>;
  let rows: [ReactNode, ReactNode][] = [];
  switch (version.asset_type) {
    case 'part_model': {
      const source = str(s.source);
      rows = [
        [t('as.sum.revision'), <span className="mono">R{num(s.rev_no) ?? '?'}</span>],
        [t('as.sum.source'), source ? dynLabel(t, `revSource.${source}`, source) : dash],
        [t('as.sum.buildStatus'), str(s.status) ? dynLabel(t, `as.buildStatus.${String(s.status)}`, String(s.status)) : dash],
        [t('as.sum.meshOnly'), yesNo(s.mesh_only)],
        [t('as.sum.buildKey'), <HashText hash={str(s.build_key)} />],
        ...(str(s.note) ? ([[t('as.sum.note'), <span className="as-note">{String(s.note)}</span>]] as [ReactNode, ReactNode][]) : []),
      ];
      break;
    }
    case 'drawing':
      rows = [
        [t('as.sum.drawingKind'), str(s.kind) ? dynLabel(t, `as.drawingKind.${String(s.kind)}`, String(s.kind)) : dash],
        [t('as.sum.fromRevision'), num(s.rev_no) !== null ? <span className="mono">R{num(s.rev_no)}</span> : <span className="faint">{t('as.sum.noModel')}</span>],
        [t('as.sum.views'), fmt.number(num(s.views))],
        [t('as.sum.svg'), <HashText hash={str(s.svg_sha)} />],
        [t('as.sum.dxf'), <HashText hash={str(s.dxf_sha)} />],
      ];
      break;
    case 'process_plan':
      rows = [
        [t('as.sum.planSource'), s.model_revision_id ? t('as.sum.fromModel') : s.intake_id ? t('as.sum.fromIntake') : dash],
        [t('as.sum.estimated'), yesNo(s.estimated)],
        [t('as.sum.priceable'), yesNo(s.priceable)],
        [t('as.sum.cycle'), num(s.total_cycle_min) !== null ? t('as.sum.minutes', { n: fmt.number(num(s.total_cycle_min), { maximumFractionDigits: 1 }) }) : dash],
        [t('as.sum.inputsHash'), <HashText hash={str(s.inputs_hash)} />],
      ];
      break;
    case 'quote': {
      const tier = (s.first_tier ?? {}) as Obj;
      const fen = num(tier.unit_inc_tax_fen);
      const currency = str(s.currency) ?? 'CNY';
      let price: ReactNode = dash;
      if (fen !== null) {
        try {
          price = <span className="mono">{fmt.number(fen / 100, { style: 'currency', currency })}</span>;
        } catch {
          price = <span className="mono">{`${fmt.number(fen / 100)} ${currency}`}</span>;
        }
      }
      rows = [
        [t('as.sum.firstTier'), num(tier.qty) !== null ? t('as.sum.qty', { n: fmt.number(num(tier.qty)) }) : dash],
        [t('as.sum.unitPrice'), price],
        [t('as.sum.currency'), <span className="mono">{currency}</span>],
        [t('as.sum.estimated'), yesNo(s.estimated)],
        [t('as.sum.inputsHash'), <HashText hash={str(s.inputs_hash)} />],
      ];
      break;
    }
    case 'document': {
      const kind = str(s.kind);
      rows = [
        [t('as.sum.fileName'), <span className="mono as-break">{str(s.original_name) ?? '—'}</span>],
        [t('as.sum.fileVersion'), <span className="mono">v{num(s.version) ?? '?'}</span>],
        [t('as.sum.fileKind'), kind ? dynLabel(t, `fileKind.${kind}`, kind) : dash],
        [t('as.sum.blob'), <HashText hash={str(s.blob_sha256)} />],
        ...(str(s.note) ? ([[t('as.sum.note'), <span className="as-note">{String(s.note)}</span>]] as [ReactNode, ReactNode][]) : []),
      ];
      break;
    }
    case 'process_module': {
      const list = (v: unknown) => (Array.isArray(v) && v.length ? v.map(String).join(', ') : null);
      const size = (s.size_range_mm ?? {}) as Obj;
      rows = [
        [t('as.sum.materials'), list(s.materials) ?? dash],
        [t('as.sum.equipment'), list(s.equipment) ?? dash],
        [t('as.sum.sizeMax'), formatBbox(size.max) ?? dash],
        [t('as.sum.steps'), fmt.number(num(s.step_count))],
        [t('as.sum.parameters'), fmt.number(num(s.parameter_count))],
        [t('as.sum.validationScope'), str(s.validation_scope) ? <span className="as-note">{String(s.validation_scope)}</span> : dash],
      ];
      break;
    }
    case 'intake': {
      const method = str(s.method);
      rows = [
        [t('as.sum.method'), method ? dynLabel(t, `as.intakeMethod.${method}`, method) : dash],
        [t('as.sum.material'), str(s.material) ?? dash],
        [t('as.sum.bbox'), formatBbox(s.bbox_mm) ?? dash],
        [t('as.sum.estimated'), yesNo(s.estimated)],
      ];
      break;
    }
  }
  return <Facts rows={rows} />;
}

/** "Open in workspace" (part tab) or "Download" (documents) for one version. */
export function OpenInWorkspace({ version, size = '' }: { version: AssetVersion; size?: '' | 'sm' }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const target = workspaceTarget(version);
  if (!target) return null;
  if (target.kind === 'link')
    return (
      <Link className={`btn ${size}`} to={target.to}>
        <ExternalLink /> {t(target.labelKey)}
      </Link>
    );
  return (
    <button
      type="button"
      className={`btn ${size}`}
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await downloadBlob(target.sha, target.filename);
        } catch (e) {
          toast(`${t('error.downloadFailed')} ${t(describeError(e).key)}`, { tone: 'err' });
        } finally {
          setBusy(false);
        }
      }}
    >
      <Download /> {busy ? t('common.working') : t('as.download', { name: target.filename })}
    </button>
  );
}
