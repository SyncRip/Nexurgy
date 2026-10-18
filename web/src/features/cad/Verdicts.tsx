// Verdict chips and compact summaries shared by the assistant, the model and
// drawings tabs and the run page. Each verdict is its own chip with icon +
// text (never colour alone): geometry check, annotation check, DWG output and
// business acceptance are never merged into one "success".

import { AlertTriangle, Ban, CheckCircle2, CircleDashed, ClipboardCheck, FileInput, FileText, HelpCircle, ShieldCheck, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import type { CheckSummary, ClarificationGap, ReconstructSource } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { geomCheckState, geomCheckTone, gapKind, n, stateTone, summaryCounts, type AnnotationState, type DwgInfo, type GeomCheck } from './status';
import './cad.css';

/** Translate a dynamic key when it exists, else the fallback. */
export function dyn(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

function StatusIcon({ tone }: { tone: 'ok' | 'err' | 'warn' | 'pending' }) {
  if (tone === 'ok') return <CheckCircle2 aria-hidden />;
  if (tone === 'err') return <XCircle aria-hidden />;
  if (tone === 'warn') return <AlertTriangle aria-hidden />;
  return <CircleDashed aria-hidden />;
}

/** "Geometry check: passed / failed / not evaluated"; nothing (or "no requirement contract" when `showNone`) without a contract. */
export function GeomCheckChip({ status, short, showNone }: { status: string | null | undefined; short?: boolean; showNone?: boolean }) {
  const { t } = useI18n();
  const s: GeomCheck = geomCheckState(status);
  if (s === 'none') {
    return showNone ? (
      <Chip tone="pending" icon={<HelpCircle aria-hidden />} title={t('cad.geom.noneHint')}>
        {t('cad.geom.none')}
      </Chip>
    ) : null;
  }
  const tone = geomCheckTone(s);
  return (
    <Chip tone={tone} icon={<StatusIcon tone={tone} />} title={t('cad.geom.hint')} className={short ? 'sm' : ''}>
      {short ? t(`cad.geom.short.${s}`) : t(`cad.geom.${s}`)}
    </Chip>
  );
}

/** Business acceptance is separate from every check: a generated result is "not accepted" until the task rules decide. */
export function AcceptanceChip() {
  const { t } = useI18n();
  return (
    <Chip tone="pending" icon={<ClipboardCheck aria-hidden />} title={t('cad.accept.hint')}>
      {t('cad.accept.pending')}
    </Chip>
  );
}

export function AnnotationChip({ state, failed }: { state: AnnotationState; failed?: number | null }) {
  const { t } = useI18n();
  const tone = stateTone(state);
  return (
    <Chip tone={tone} icon={<StatusIcon tone={tone} />}>
      {t(`cad.ann.${state}`)}
      {state === 'failed' && failed ? ` · ${t('cad.ann.failedN', { n: failed })}` : ''}
    </Chip>
  );
}

export function DwgChip({ info }: { info: DwgInfo }) {
  const { t } = useI18n();
  const tone = stateTone(info.state);
  const title =
    info.state === 'passed'
      ? t('cad.dwg.passedHint')
      : info.state === 'unavailable'
        ? [info.reason ? t('cad.dwg.reason', { reason: info.reason }) : '', t('cad.dwg.unavailableHint')].filter(Boolean).join(' ')
        : info.reason ?? undefined;
  return (
    <Chip tone={tone} icon={info.state === 'unavailable' ? <Ban aria-hidden /> : <StatusIcon tone={tone} />} title={title}>
      {info.state === 'passed' ? t('cad.dwg.passed', { release: info.release ?? 'R2000' }) : t(`cad.dwg.${info.state}`)}
    </Chip>
  );
}

/** The reason text under a DWG chip (unavailable / failed), so touch users see it too. */
export function DwgReason({ info }: { info: DwgInfo }) {
  const { t } = useI18n();
  if (info.state !== 'unavailable' && info.state !== 'failed') return null;
  const parts = [info.reason ? t('cad.dwg.reason', { reason: info.reason }) : null, info.code ? info.code : null, info.page ? t('cad.dwg.failedPage', { page: info.page }) : null].filter(Boolean);
  if (!parts.length) return null;
  return <p className="faint small">{parts.join(' · ')}</p>;
}

/** check_summary of a modeling step: verdict, requirement / item counts, first problems, uncovered / unknown notes. */
export function CheckSummaryView({ summary, title, acceptance = true }: { summary: CheckSummary | null | undefined; title?: ReactNode; acceptance?: boolean }) {
  const { t } = useI18n();
  if (!summary) {
    return (
      <div className="cad-block">
        <div className="cad-block-h">
          <ShieldCheck aria-hidden /> {title ?? t('cad.as.check')}
        </div>
        <p className="faint small">{t('cad.sum.none')}</p>
      </div>
    );
  }
  const counts = summaryCounts(summary);
  const req = summary.requirements;
  const kinds = req?.by_kind ? Object.entries(req.by_kind) : [];
  const notes = summary.notes;
  return (
    <div className="cad-block">
      <div className="cad-block-h">
        <ShieldCheck aria-hidden /> {title ?? t('cad.as.check')}
      </div>
      <div className="cad-chips">
        <GeomCheckChip status={summary.status} showNone />
        {acceptance && <AcceptanceChip />}
      </div>
      <dl className="cad-kv small">
        {req && (
          <>
            <dt>{t('cad.sum.requirements')}</dt>
            <dd>
              {t('cad.sum.reqCounts', { total: req.total, geometric: req.geometric })}
              {kinds.length > 0 && <span className="faint"> · {kinds.map(([k, v]) => `${dyn(t, `cad.kind.${k}`, k)} ${v}`).join(' · ')}</span>}
            </dd>
          </>
        )}
        {counts.total > 0 && (
          <>
            <dt>{t('cad.sum.items')}</dt>
            <dd>
              {t('cad.sum.itemCounts', { passed: counts.passed, failed: counts.failed })}
              {counts.other ? t('cad.sum.itemOther', { n: counts.other }) : ''}
            </dd>
          </>
        )}
      </dl>
      {summary.reason && <p className="faint small">{t('cad.sum.reason', { reason: summary.reason })}</p>}
      {!!summary.unrequested && <p className="small">{t('cad.sum.unrequested', { n: summary.unrequested })}</p>}
      {!!summary.problems?.length && (
        <div>
          <div className="cad-sub">{t('cad.sum.problems')}</div>
          <ul className="cad-list small">
            {summary.problems.slice(0, 5).map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      {!!notes?.uncovered?.length && <NoteLines label={t('cad.sum.uncovered')} lines={notes.uncovered} />}
      {!!notes?.unknown?.length && <NoteLines label={t('cad.sum.unknown')} lines={notes.unknown} />}
      {summary.validator_version && <p className="faint small mono">{t('cad.sum.validator', { v: summary.validator_version })}</p>}
    </div>
  );
}

function NoteLines({ label, lines }: { label: string; lines: string[] }) {
  return (
    <div>
      <div className="cad-sub">{label}</div>
      <ul className="cad-list small">
        {lines.slice(0, 8).map((l, i) => (
          <li key={i}>{l}</li>
        ))}
        {lines.length > 8 && <li className="faint">… +{lines.length - 8}</li>}
      </ul>
    </div>
  );
}

/** source_brief of a reconstruction: file, sheet, DWG conversion (provisional read support) and logged conflicts. */
export function ReconstructSourceView({ source, title }: { source: ReconstructSource | null | undefined; title?: ReactNode }) {
  const { t } = useI18n();
  if (!source) return null;
  const dwg = source.dwg;
  const status = dwg?.version_status ?? null;
  const frame = source.frame;
  const sheet = frame ? t('cad.src.sheetText', { number: frame.number ?? '', name: frame.name ?? '' }).trim() || source.frame_id || '—' : source.frame_id || '—';
  return (
    <div className="cad-block">
      <div className="cad-block-h">
        <FileInput aria-hidden /> {title ?? t('cad.as.source')}
      </div>
      <dl className="cad-kv small">
        <dt>{t('cad.src.file')}</dt>
        <dd>
          <span className="strong">{source.name}</span> <span className="mono faint">{source.source_sha256?.slice(0, 10)}</span>
        </dd>
        <dt>{t('cad.src.format')}</dt>
        <dd>
          {source.format.toUpperCase()}
          {source.dxf_role === 'derived' ? <span className="faint"> · {t('cad.src.derived')}</span> : null}
        </dd>
        {(frame || source.frame_id) && (
          <>
            <dt>{t('cad.src.sheet')}</dt>
            <dd>
              {sheet}
              {frame?.scale ? <span className="faint"> · {t('cad.src.scale', { scale: frame.scale })}</span> : null}
            </dd>
          </>
        )}
        {dwg && (
          <>
            <dt>{t('cad.src.dwg')}</dt>
            <dd>
              <span className="cad-chips">
                <span>{t('cad.src.dwgVersion', { version: dwg.version ?? '?', status: status ? dyn(t, `cad.src.${status}`, status) : t('cad.run.unknown') })}</span>
                {status === 'provisional' || status === 'untested' ? (
                  <Chip tone="warn" icon={<AlertTriangle aria-hidden />} title={t('cad.src.provisionalHint')}>
                    {dyn(t, `cad.src.${status}`, status)}
                  </Chip>
                ) : null}
              </span>
              {dwg.converter?.name ? <span className="faint"> {t('cad.src.converter', { name: dwg.converter.name, version: dwg.converter.version ?? '' })}</span> : null}
              {dwg.losses ? <div className="faint">{t('cad.src.losses', { n: dwg.losses })}</div> : null}
            </dd>
          </>
        )}
        {typeof source.conflicts === 'number' && (
          <>
            <dt>{t('cad.src.conflicts')}</dt>
            <dd>{source.conflicts ? t('cad.src.conflictsN', { n: source.conflicts }) : t('cad.src.noConflicts')}</dd>
          </>
        )}
      </dl>
      {(status === 'provisional' || status === 'untested') && <p className="faint small">{t('cad.src.provisionalHint')}</p>}
    </div>
  );
}

/** One line per gap: sheet / units / conflicting dimension / missing or assumed critical field. */
export function gapLine(t: TFunction, g: ClarificationGap): string {
  switch (gapKind(g)) {
    case 'sheet':
      return g.reason === 'not_found' ? t('cad.gap.sheetNotFound', { n: g.frames ?? '?' }) : t('cad.gap.sheet', { n: g.frames ?? '?' });
    case 'units':
      return t('cad.gap.units');
    case 'conflict': {
      const measured = typeof g.native_value === 'number' ? n(g.native_value) : typeof g.measurement === 'number' ? n(g.measurement) : '?';
      const base = t('cad.gap.conflict', { label: g.label || g.requirement_id || '', id: g.evidence_id ?? '?', displayed: g.displayed === null || g.displayed === undefined ? '?' : String(g.displayed), measured });
      return typeof g.expected_by_view === 'number' ? base + t('cad.gap.conflictView', { view: g.view ?? '?', expected: n(g.expected_by_view) }) : base;
    }
    default:
      return t(g.reason === 'assumed' ? 'cad.gap.assumed' : 'cad.gap.missing', { label: g.label || g.requirement_id || g.kind, field: g.field });
  }
}

export function ClarificationGaps({ gaps }: { gaps: ClarificationGap[] | null | undefined }) {
  const { t } = useI18n();
  if (!gaps?.length) return null;
  return (
    <div className="small">
      <div className="cad-sub">{t('cad.gap.title')}</div>
      <ul className="cad-list">
        {gaps.map((g, i) => (
          <li key={i}>{gapLine(t, g)}</li>
        ))}
      </ul>
    </div>
  );
}

export function DrawingKindIcon() {
  return <FileText aria-hidden />;
}
