// Inspect: what the kernel measured for the model on screen (saved revision
// or live preview), explicit holes, heuristic bores on imported bases,
// warnings, and details of the current face / edge selection — with the
// explicit "Add to AI chat" / "Set as AI target" for a face / edge of a saved
// revision (hole candidates matched from the revision's hole records).

import { AlertTriangle,Info,Lock } from 'lucide-react';
import { useId,type ReactNode } from 'react';
import type { AiOpCandidate } from '../../../api/assistant';
import type { BuildIssue,DetectedHole,HoleRecord } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { fmtMm,type EdgePick,type FacePick } from '../../../three/viewer';

import type { AiRefWithProject } from '../../assistant/refs';
import { KV } from '../common';
import { edgeKey,surfaceKey } from './Viewport';

/** The AI entry for the current face / edge selection (built by ModelTab). */
export interface InspectAi {
  /** Builds the cad_selection reference at click time (null when not referenceable). */
  input: () => AiRefWithProject | null;
  /** "R3 · face #12" — goes into the buttons' accessible names. */
  scope: string;
  /** Why the selection cannot be referenced (unsaved preview …). */
  disabledReason: string | null;
  /** Saved revision the ids belong to. */
  revisionLabel: string | null;
  candidates: AiOpCandidate[];
  resolvedOpId: string | null;
  onResolve: (opId: string | null) => void;
  /** Picked face is a cylinder (a hole match was attempted). */
  cylinder: boolean;
}

export interface InspectMetrics {
  valid?: boolean;
  solids?: number;
  faces?: number;
  edges?: number;
  volume_mm3?: number | null;
  area_mm2?: number | null;
  bbox?: { size: number[] } | null;
  center_of_mass?: number[];
  triangles?: number;
  watertight?: boolean;
  units_assumed?: string;
}

const r = (v: number) => Math.round(v * 1000) / 1000;

export function InspectPanel({
  sourceLabel,
  metrics,
  holes,
  detected,
  warnings,
  kernelVersion,
  meshOnly,
  selection,
  ai = null,
}: {
  sourceLabel: string;
  metrics: InspectMetrics | null;
  holes: HoleRecord[];
  detected: DetectedHole[];
  warnings: BuildIssue[];
  kernelVersion: string | null;
  meshOnly: boolean;
  selection: FacePick | EdgePick | null;
  ai?: InspectAi | null;
}) {
  const { t, fmt } = useI18n();
  const num = (v: number | null | undefined, d = 2) => fmt.number(v, { maximumFractionDigits: d });
  return (
    <div className="stack-sm ws-inspect">
      {selection && (
        <section className="ws-sec">
          <h4>{t('ws.inspect.selection')}</h4>
          {selection.kind === 'face' ? (
            <KV
              items={[
                [t('ws.inspect.face'), <span className="mono">#{selection.id}</span>],
                [t('ws.inspect.surface'), t(`ws.surface.${surfaceKey(selection.surface)}`)],
                ...(selection.planar ? [[t('ws.inspect.normal'), <span className="mono">({selection.normal.map(r).join(', ')})</span>] as [ReactNode, ReactNode]] : []),
                ...(selection.radius ? [[t('ws.inspect.radius'), <span className="mono">{num(selection.radius, 3)} mm</span>] as [ReactNode, ReactNode]] : []),
                [t('ws.inspect.area'), <span className="mono">{num(selection.area)} mm²</span>],
                [t('ws.inspect.center'), <span className="mono">({selection.center.map(r).join(', ')})</span>],
                [t('ws.inspect.point'), <span className="mono">({selection.point.map(r).join(', ')})</span>],
              ]}
            />
          ) : (
            <KV
              items={[
                [t('ws.inspect.edge'), <span className="mono">E{selection.id}</span>],
                [t('ws.inspect.kind'), t(`ws.edgeKind.${edgeKey(selection.edgeKind)}`)],
                [t('ws.inspect.length'), <span className="mono">{fmtMm(selection.length)} mm</span>],
                [t('ws.inspect.from'), <span className="mono">({selection.start.map(r).join(', ')})</span>],
                [t('ws.inspect.to'), <span className="mono">({selection.end.map(r).join(', ')})</span>],
              ]}
            />
          )}
          <p className="faint small">{t('ws.inspect.idsNote')}</p>
          {ai && <SelectionAi ai={ai} />}
        </section>
      )}

      <section className="ws-sec">
        <h4>
          {t('ws.inspect.metrics')} <span className="faint small">· {sourceLabel}</span>
        </h4>
        {!metrics ? (
          <p className="faint small">{t('ws.inspect.noMetrics')}</p>
        ) : (
          <KV
            items={[
              [t('ws.metric.bbox'), metrics.bbox?.size ? <span className="mono">{metrics.bbox.size.map((x) => fmtMm(x)).join(' × ')} mm</span> : '—'],
              [t('ws.metric.volume'), <span className="mono">{num(metrics.volume_mm3, 1)} mm³{meshOnly && metrics.volume_mm3 ? ` (${t('ws.inspect.approx')})` : ''}</span>],
              [t('ws.metric.area'), <span className="mono">{num(metrics.area_mm2, 1)} mm²</span>],
              ...(meshOnly
                ? ([
                    [t('ws.inspect.triangles'), <span className="mono">{num(metrics.triangles, 0)}</span>],
                    [t('ws.inspect.watertight'), metrics.watertight ? t('ws.yes') : t('ws.no')],
                    [t('ws.inspect.units'), <span className="mono">{metrics.units_assumed ?? '—'}</span>],
                  ] as [ReactNode, ReactNode][])
                : ([
                    [t('ws.inspect.solidsFaces'), <span className="mono">{num(metrics.solids, 0)} / {num(metrics.faces, 0)} / {num(metrics.edges, 0)}</span>],
                    [t('ws.inspect.valid'), metrics.valid ? t('ws.yes') : t('ws.no')],
                    ...(metrics.center_of_mass ? [[t('ws.inspect.com'), <span className="mono">({metrics.center_of_mass.map(r).join(', ')})</span>] as [ReactNode, ReactNode]] : []),
                  ] as [ReactNode, ReactNode][])),
            ]}
          />
        )}
        {kernelVersion && <p className="faint small mono">{kernelVersion}</p>}
      </section>

      {holes.length > 0 && (
        <section className="ws-sec">
          <h4>{t('ws.inspect.holes')}</h4>
          <table className="t dense ws-mini">
            <thead>
              <tr>
                <th>{t('ws.inspect.op')}</th>
                <th>Ø</th>
                <th>{t('ws.hole.depth')}</th>
                <th className="r">{t('ws.inspect.count')}</th>
              </tr>
            </thead>
            <tbody>
              {holes.map((h) => (
                <tr key={h.op_id}>
                  <td className="mono">{h.op_id}</td>
                  <td className="mono">
                    {r(h.diameter)}
                    {h.counterbore ? ` ⌴${r(h.counterbore.diameter ?? 0)}` : h.countersink ? ` ⌵${r(h.countersink.diameter ?? 0)}` : ''}
                  </td>
                  <td className="mono">{h.through ? 'THRU' : r(h.depth ?? 0)}</td>
                  <td className="r mono">{h.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {detected.length > 0 && (
        <section className="ws-sec">
          <h4>{t('ws.inspect.detected')}</h4>
          <div className="callout warn slim">
            <Info aria-hidden />
            <span>{t('ws.inspect.detectedNote')}</span>
          </div>
          <ul className="ws-plain small">
            {detected.map((h, i) => (
              <li key={i} className="mono">
                Ø{r(h.diameter)} × {r(h.length)} · ×{h.count} · {h.through === null ? '?' : h.through ? 'THRU' : t('ws.inspect.blind')}
              </li>
            ))}
          </ul>
        </section>
      )}

      {warnings.length > 0 && (
        <section className="ws-sec">
          <h4>{t('ws.inspect.warnings')}</h4>
          <ul className="ws-plain small">
            {warnings.map((w, i) => (
              <li key={i}>
                <AlertTriangle className="inline-ico warn-ico" aria-hidden /> {w.op_id && <span className="mono">{w.op_id} </span>}
                <code className="error-code">{w.code}</code> {w.message}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** Add the picked face / edge to the assistant; choose among hole candidates first when there are several. */
function SelectionAi({ ai }: { ai: InspectAi }) {
  const { t } = useI18n();
  const groupId = useId();
  const n = ai.candidates.length;
  return (
    <div className="ar-sel-ai">
      <div className="small strong">{t('ar.model.selTitle')}</div>
      {ai.disabledReason ? (
        <div className="callout pending slim">
          <Lock aria-hidden />
          <span>{ai.disabledReason}</span>
        </div>
      ) : n > 0 ? (
        <fieldset className="ar-cands">
          <legend className="small">{n > 1 ? t('ar.model.candidatesMany', { n }) : t('ar.model.candidatesOne')}</legend>
          {ai.candidates.map((c) => (
            <label key={c.op_id} className="ar-cand small">
              <input type="radio" name={groupId} checked={ai.resolvedOpId === c.op_id} onChange={() => ai.onResolve(c.op_id)} />
              <span className="mono strong">{c.label}</span>
              <span className="ar-cand-reason faint">{c.reason}</span>
            </label>
          ))}
          <label className="ar-cand small">
            <input type="radio" name={groupId} checked={!ai.resolvedOpId} onChange={() => ai.onResolve(null)} />
            <span>{t('ar.model.candidateNone')}</span>
            <span className="ar-cand-reason faint">{t('ar.model.candidateNoneHint')}</span>
          </label>
        </fieldset>
      ) : ai.cylinder ? (
        <p className="faint small">{t('ar.model.noCandidates')}</p>
      ) : null}
      {!ai.disabledReason && ai.revisionLabel && <p className="faint small">{t('ar.model.selIdsNote', { rev: ai.revisionLabel })}</p>}
      <></>
    </div>
  );
}
