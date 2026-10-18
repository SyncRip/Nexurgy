// Node inputs / outputs rendered as artifact links (design.md §7 port values).

import { Box, Calculator, ClipboardList, Download, ExternalLink, FileText, FlaskConical, Package, ScanText, Type } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { blobUrl, downloadBlob, type BlobHandle } from '../../../api/blobs';
import { portValuePath } from '../../../app/versionLinks';
import { Spinner } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useI18n } from '../../../i18n/I18nProvider';
import { describeError } from '../../../i18n/errors';
import { portLabel } from '../labels';
import type { CheckSummary, DrawingBrief, ReconstructSource } from '../../../api/types';
import { DrawingPackageCard } from '../../cad/DrawingPackage';
import { CheckSummaryView, ReconstructSourceView } from '../../cad/Verdicts';
import { isChainValue } from '../../../api/orchestration';
import { ChainValueView } from '../../orchestration/ChainValue';
import { manufacturingBundleInfo, readManufacturingValue } from '../../../api/manufacturing';
import { ManufacturingBundleNotice, ManufacturingEstimateBadge, ManufacturingValueView } from '../../manufacturing/ManufacturingValue';

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function s(v: unknown): string {
  return typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v);
}

export function isSimulated(outputs: unknown, steps?: { detail?: unknown }[]): boolean {
  if (isObj(outputs) && outputs.simulated === true) return true;
  if (isObj(outputs)) {
    for (const v of Object.values(outputs)) if (isObj(v) && v.simulated === true) return true;
  }
  return !!steps?.some((st) => isObj(st.detail) && st.detail.simulated === true);
}

export function SimulatedBadge() {
  const { t } = useI18n();
  return (
    <span className="wf-sim-badge" title={t('wf.simulated.title')}>
      <FlaskConical aria-hidden /> {t('wf.simulated.label')}
    </span>
  );
}

function DownloadButton({ sha, filename, label }: { sha: string; filename: string; label: string }) {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="btn sm"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await downloadBlob(sha, filename);
        } catch (e) {
          toast(`${t('error.downloadFailed')} ${t(describeError(e).key)}`, { tone: 'err' });
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <Spinner /> : <Download />} {label}
    </button>
  );
}

/** Authenticated SVG preview; the image links to the full-size object URL. */
function SvgPreview({ sha }: { sha: string }) {
  const { t } = useI18n();
  const [h, setH] = useState<BlobHandle | null>(null);
  const [failed, setFailed] = useState<unknown>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    let handle: BlobHandle | null = null;
    blobUrl(sha, { signal: ctrl.signal })
      .then((x) => {
        handle = x;
        setH(x);
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setFailed(e);
      });
    return () => {
      ctrl.abort();
      handle?.revoke();
    };
  }, [sha]);
  if (failed) return <span className="faint small">{t(describeError(failed).key)}</span>;
  if (!h) return <Spinner label={t('common.loading')} />;
  return (
    <a href={h.url} target="_blank" rel="noopener noreferrer" className="wf-svg-preview" title={t('wf.art.openSvg')}>
      <img src={h.url} alt={t('wf.art.drawingAlt')} />
      <span className="wf-svg-open">
        <ExternalLink aria-hidden /> {t('wf.art.openSvg')}
      </span>
    </a>
  );
}

/** Link to the exact output a port value carries (S6: model ?rev=, drawing ?drawing=, plan ?plan=, quote ?quote=),
 * never the part tab alone — a past run's output must open as it was, not the newest version. */
function OutputLink({ value, partId, children }: { value: Obj; partId: string | null; children: ReactNode }) {
  const to = portValuePath(value, partId);
  if (!to) return <span>{children}</span>;
  return (
    <Link to={to} className="wf-art-link">
      {children}
      <ExternalLink aria-hidden />
    </Link>
  );
}

export function PortValue({ value, partId, preview = true }: { value: unknown; partId: string | null; preview?: boolean }) {
  const { t, fmt } = useI18n();
  if (Array.isArray(value)) {
    if (!value.length) return <span className="faint">—</span>;
    return (
      <ul className="wf-art-list">
        {value.map((v, i) => (
          <li key={i}>
            <PortValue value={v} partId={partId} preview={false} />
          </li>
        ))}
      </ul>
    );
  }
  if (!isObj(value)) {
    if (value === null || value === undefined) return <span className="faint">—</span>;
    return <span className="mono small">{s(value)}</span>;
  }
  switch (value.type) {
    case 'text':
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            <Type aria-hidden /> {t('wf.ptype.text')}
          </span>
          <blockquote className="wf-art-text">{s(value.text) || <span className="faint">{t('wf.art.emptyText')}</span>}</blockquote>
        </div>
      );
    case 'model':
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            <Box aria-hidden /> {t('wf.ptype.model')}
          </span>
          <OutputLink value={value} partId={partId}>
            {t('wf.revisionLabel', { n: s(value.rev_no) })}
          </OutputLink>
          {value.mesh_only === true && <span className="chip warn">{t('rev.meshOnly')}</span>}
        </div>
      );
    case 'drawing':
      return (
        <div className="wf-art stack-sm">
          <div className="row">
            <span className="wf-art-kind">
              <FileText aria-hidden /> {t('wf.ptype.drawing')}
            </span>
            <OutputLink value={value} partId={partId}>
              {t('wf.art.openDrawings')}
            </OutputLink>
            {typeof value.dxf_sha === 'string' && value.dxf_sha && <DownloadButton sha={value.dxf_sha} filename={`drawing_${s(value.drawing_id).slice(0, 8)}.dxf`} label="DXF" />}
            {typeof value.svg_sha === 'string' && value.svg_sha && <DownloadButton sha={value.svg_sha} filename={`drawing_${s(value.drawing_id).slice(0, 8)}.svg`} label="SVG" />}
          </div>
          {preview && typeof value.svg_sha === 'string' && value.svg_sha && <SvgPreview sha={value.svg_sha} />}
          {/* The whole package: pages with every format, annotation / DWG checks, package.zip. */}
          {typeof value.drawing_id === 'string' && value.drawing_id && <DrawingPackageCard brief={value as unknown as DrawingBrief} partId={partId} showAcceptance={false} showPages={preview} />}
        </div>
      );
    case 'spec':
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            <ScanText aria-hidden /> {t('wf.ptype.spec')}
          </span>
          <OutputLink value={value} partId={partId}>
            {t('wf.art.intake')}
          </OutputLink>
          {value.estimated === true && <span className="chip warn">{t('wf.art.estimated')}</span>}
          {value.simulated === true && <SimulatedBadge />}
          {typeof value.method === 'string' && <span className="faint small">{t(`wf.art.method.${value.method === 'ai' ? 'ai' : 'manual'}`)}</span>}
        </div>
      );
    case 'plan':
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            <ClipboardList aria-hidden /> {t('wf.ptype.plan')}
          </span>
          <OutputLink value={value} partId={partId}>
            {t('wf.art.openPlan')}
          </OutputLink>
          {value.priceable === false && <span className="chip err">{t('wf.art.notPriceable')}</span>}
          {value.estimated === true && <span className="chip warn">{t('wf.art.estimated')}</span>}
        </div>
      );
    case 'quote': {
      const tier = isObj(value.first_tier) ? value.first_tier : {};
      const fen = typeof tier.unit_inc_tax_fen === 'number' ? tier.unit_inc_tax_fen : null;
      const currency = s(value.currency) || 'CNY';
      let price = '—';
      if (fen !== null) {
        try {
          price = fmt.number(fen / 100, { style: 'currency', currency });
        } catch {
          price = `${fmt.number(fen / 100)} ${currency}`;
        }
      }
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            <Calculator aria-hidden /> {t('wf.ptype.quote')}
          </span>
          <OutputLink value={value} partId={partId}>
            {t('wf.art.openQuote')}
          </OutputLink>
          {fen !== null && <span className="mono small">{t('wf.art.firstTier', { qty: s(tier.qty), price })}</span>}
          {value.pricing_basis === 'synthetic_demo_rates' && <ManufacturingEstimateBadge />}
        </div>
      );
    }
    // W8: general mechanical engineering chain (spec, assembly, drawing package, delivery)
    case 'engineering_spec':
    case 'assembly':
    case 'drawing_package':
    case 'delivery':
      return isChainValue(value) ? <ChainValueView value={value} compact={!preview} /> : <pre className="wf-json">{JSON.stringify(value, null, 2)}</pre>;
    case 'manufacturing_plan':
    case 'manufacturing_quote': {
      const batch = readManufacturingValue(value);
      return batch ? <ManufacturingValueView value={batch} compact={!preview} /> : null;
    }
    case 'file': {
      const manufacturing = manufacturingBundleInfo(value);
      return (
        <div className="wf-art">
          <span className="wf-art-kind">
            {value.blob_sha256 ? <Package aria-hidden /> : <FileText aria-hidden />} {t('wf.ptype.file')}
          </span>
          <span className="ellip strong">{s(value.name) || s(value.file_version_id).slice(0, 8)}</span>
          {typeof value.blob_sha256 === 'string' && value.blob_sha256 && <DownloadButton sha={value.blob_sha256} filename={s(value.name) || 'export.zip'} label={t('common.download')} />}
          {manufacturing && <ManufacturingBundleNotice value={manufacturing} compact={!preview} />}
        </div>
      );
    }
    default:
      return <pre className="wf-json">{JSON.stringify(value, null, 2)}</pre>;
  }
}

const META_KEYS = new Set(['simulated', 'provider', 'clarification', 'manual_entry']);

/** All values of a node's inputs or outputs, port by port, plus the AI model meta line. */
export function PortValues({ values, portNames, partId }: { values: Record<string, unknown> | null | undefined; portNames: string[]; partId: string | null }) {
  const { t } = useI18n();
  const v = values ?? {};
  const keys = [...new Set([...portNames.filter((k) => k in v), ...Object.keys(v).filter((k) => !META_KEYS.has(k) && !portNames.includes(k))])];
  // model_3d puts the provider label next to its port values; a string under "model" is that label, not a port value.
  const modelLabel = typeof v.model === 'string' ? v.model : null;
  const shown = keys.filter((k) => !(k === 'model' && modelLabel !== null));
  return (
    <div className="stack-sm">
      {(typeof v.provider === 'string' || modelLabel) && (
        <div className="wf-art-meta small">
          <span className="faint">{t('wf.art.modelUsed')}</span>
          <span className="mono">{[s(v.provider), modelLabel].filter(Boolean).join(' · ')}</span>
          {v.simulated === true && <SimulatedBadge />}
        </div>
      )}
      {shown.length === 0 ? (
        <span className="faint small">—</span>
      ) : (
        shown.map((k) =>
          // CAD outputs that are not ports: the modeling step's requirement check and the drawing source of a reconstruction.
          k === 'requirements_check' && isObj(v[k]) ? (
            <CheckSummaryView key={k} summary={v[k] as unknown as CheckSummary} title={t('cad.run.reqCheck')} acceptance={false} />
          ) : k === 'reconstruct' && isObj(v[k]) ? (
            <ReconstructSourceView key={k} source={v[k] as unknown as ReconstructSource} title={t('cad.run.reconstruct')} />
          ) : (
            <div key={k} className="wf-port-value">
              <span className="wf-port-name">{portLabel(t, k)}</span>
              <PortValue value={v[k]} partId={partId} />
            </div>
          ),
        )
      )}
    </div>
  );
}
