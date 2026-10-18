// React wrapper around MeshViewer: toolbar (views, display, tools), hover
// read-out, measurement read-out and status overlays.

import { Axis3d, Box, BoxSelect, Crosshair, Focus, Grid3x3, MousePointer2, Ruler, ScanEye } from 'lucide-react';
import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from 'react';
import { useI18n } from '../../../i18n/I18nProvider';
import { useTheme } from '../../../prefs/ThemeProvider';
import { MeshViewer, fmtMm, type DisplayMode, type MeasureState, type MeshDoc, type Pick, type ToolMode, type Vec3, type ViewName } from '../../../three/viewer';

export interface ViewportHandle {
  setView: (v: ViewName) => void;
  fit: () => void;
  clearMeasure: () => void;
}

export interface ViewportProps {
  mesh: MeshDoc | null;
  glb: ArrayBuffer | null;
  /** Changing this re-fits the camera (e.g. another revision). */
  fitToken: string;
  mode: ToolMode;
  onModeChange: (m: ToolMode) => void;
  selection: { faces?: number[]; edges?: number[] } | null;
  candidates: number[];
  markers: { points: Vec3[]; direction?: Vec3 | null };
  onPick: (p: Pick | null, ev: { shift: boolean }) => void;
  /** Banner over the viewport (pick hints). */
  banner?: ReactNode;
  /** Status chip (top-right): preview state. */
  status?: ReactNode;
  /** Shown centred when there is nothing to draw. */
  empty?: ReactNode;
  loading?: boolean;
  /** Dim the model (stale preview). */
  dimmed?: boolean;
}

const VIEWS = ['iso', 'front', 'top', 'right'] as const satisfies readonly ViewName[];
const DISPLAYS: DisplayMode[] = ['shaded_edges', 'shaded', 'wireframe', 'xray'];

const r2 = (v: number) => (Math.round(v * 100) / 100).toString();
const vec = (v: readonly number[]) => `(${v.map(r2).join(', ')})`;

export const Viewport = forwardRef<ViewportHandle, ViewportProps>(function Viewport(props, ref) {
  const { mesh, glb, fitToken, mode, onModeChange, selection, candidates, markers, onPick, banner, status, empty, loading, dimmed } = props;
  const { t, fmt } = useI18n();
  const { resolved } = useTheme();
  const host = useRef<HTMLDivElement>(null);
  const viewer = useRef<MeshViewer | null>(null);
  const [hover, setHover] = useState<Pick | null>(null);
  const [measure, setMeasure] = useState<MeasureState>({ points: [], distance: null, delta: null });
  const [display, setDisplay] = useState<DisplayMode>('shaded_edges');
  const [bbox, setBbox] = useState(false);
  const [grid, setGrid] = useState(true);
  const [webglError, setWebglError] = useState(false);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const lastFit = useRef<string | null>(null);

  useEffect(() => {
    if (!host.current) return;
    let v: MeshViewer;
    try {
      v = new MeshViewer(host.current, { theme: resolved });
    } catch (e) {
      console.error(e);
      setWebglError(true);
      return;
    }
    viewer.current = v;
    v.setCallbacks({ onHover: setHover, onPick: (p, ev) => pickRef.current(p, ev), onMeasure: setMeasure });
    return () => {
      v.dispose();
      viewer.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => viewer.current?.setTheme(resolved), [resolved]);

  useEffect(() => {
    const v = viewer.current;
    if (!v) return;
    const fit = lastFit.current !== fitToken;
    if (glb) {
      void v.setGlb(glb, { fit }).catch((e) => console.error('GLB preview failed', e));
      lastFit.current = fitToken;
    } else {
      v.setMesh(mesh, { fit: fit && !!mesh });
      if (mesh) lastFit.current = fitToken;
    }
    setHover(null);
  }, [mesh, glb, fitToken]);

  useEffect(() => viewer.current?.setMode(mode), [mode]);
  useEffect(() => viewer.current?.setDisplay(display), [display]);
  useEffect(() => viewer.current?.setBBoxVisible(bbox), [bbox, mesh, glb]);
  useEffect(() => viewer.current?.setGridVisible(grid), [grid]);
  useEffect(() => viewer.current?.setSelection(selection), [selection, mesh]);
  useEffect(() => viewer.current?.setCandidates(candidates), [candidates, mesh]);
  useEffect(() => viewer.current?.setMarkers(markers.points, markers.direction ?? null), [markers, mesh]);

  useImperativeHandle(
    ref,
    () => ({
      setView: (name) => viewer.current?.setView(name),
      fit: () => viewer.current?.fit(),
      clearMeasure: () => viewer.current?.clearMeasure(),
    }),
    [],
  );

  const hasModel = !!mesh || !!glb;

  return (
    <div className={`vw ${dimmed ? 'dimmed' : ''}`}>
      <div className="vw-host" ref={host} aria-label={t('ws.vw.label')} role="img" />
      {webglError && <div className="vw-empty">{t('ws.vw.noWebgl')}</div>}
      {!hasModel && !loading && empty && <div className="vw-empty">{empty}</div>}
      {loading && <div className="vw-empty"><span className="spinner" aria-hidden /> {t('common.loading')}</div>}

      <div className="vw-toolbar" role="toolbar" aria-label={t('ws.vw.toolbar')}>
        <div className="seg" role="group" aria-label={t('ws.vw.views')}>
          {VIEWS.map((v) => (
            <button key={v} type="button" className="seg-btn" onClick={() => viewer.current?.setView(v)} title={`${t(`ws.vw.view.${v}`)} (${VIEWS.indexOf(v)})`}>
              {v === 'iso' ? <Axis3d /> : null}
              {t(`ws.vw.view.${v}`)}
            </button>
          ))}
        </div>
        <button type="button" className="btn sm icon" onClick={() => viewer.current?.fit()} title={`${t('ws.vw.fit')} (F)`} aria-label={t('ws.vw.fit')}>
          <Focus />
        </button>
        <select className="select sm vw-display" value={display} onChange={(e) => setDisplay(e.target.value as DisplayMode)} aria-label={t('ws.vw.display')}>
          {DISPLAYS.map((d) => (
            <option key={d} value={d}>
              {t(`ws.vw.display.${d}`)}
            </option>
          ))}
        </select>
        <div className="seg" role="group" aria-label={t('ws.vw.tools')}>
          <button type="button" className="seg-btn" aria-pressed={mode === 'select' || mode === 'pick_face' || mode === 'pick_edge'} onClick={() => onModeChange('select')} title={`${t('ws.vw.select')} (Esc)`}>
            <MousePointer2 /> {t('ws.vw.select')}
          </button>
          <button type="button" className="seg-btn" aria-pressed={mode === 'measure'} onClick={() => onModeChange(mode === 'measure' ? 'select' : 'measure')} title={`${t('ws.vw.measure')} (M)`}>
            <Ruler /> {t('ws.vw.measure')}
          </button>
        </div>
        <button type="button" className={`btn sm icon ${bbox ? 'active' : ''}`} aria-pressed={bbox} onClick={() => setBbox((x) => !x)} title={t('ws.vw.bbox')} aria-label={t('ws.vw.bbox')}>
          <BoxSelect />
        </button>
        <button type="button" className={`btn sm icon ${grid ? 'active' : ''}`} aria-pressed={grid} onClick={() => setGrid((x) => !x)} title={t('ws.vw.grid')} aria-label={t('ws.vw.grid')}>
          <Grid3x3 />
        </button>
      </div>

      {status && <div className="vw-status">{status}</div>}
      {banner && (
        <div className="vw-banner" role="status">
          <Crosshair aria-hidden /> {banner}
        </div>
      )}

      <div className="vw-readout" aria-live="polite">
        {mode === 'measure' ? (
          measure.distance !== null && measure.delta ? (
            <span className="vw-measure">
              <Ruler aria-hidden /> <strong className="mono">{fmtMm(measure.distance)} mm</strong>
              <span className="mono faint">
                Δx {fmtMm(measure.delta[0])} · Δy {fmtMm(measure.delta[1])} · Δz {fmtMm(measure.delta[2])}
              </span>
              <button type="button" className="linkish" onClick={() => viewer.current?.clearMeasure()}>
                {t('ws.vw.clear')}
              </button>
            </span>
          ) : (
            <span className="faint">{measure.points.length === 1 ? t('ws.vw.measureSecond') : t('ws.vw.measureFirst')}</span>
          )
        ) : hover?.kind === 'face' ? (
          <span>
            <Box aria-hidden /> <span className="mono">#{hover.id}</span> {t(`ws.surface.${surfaceKey(hover.surface)}`)}
            {hover.planar ? <span className="mono faint"> n{vec(hover.normal)}</span> : hover.radius ? <span className="mono faint"> R{r2(hover.radius)}</span> : null}
            <span className="mono faint"> · {fmt.number(hover.area, { maximumFractionDigits: 2 })} mm²</span>
          </span>
        ) : hover?.kind === 'edge' ? (
          <span>
            <ScanEye aria-hidden /> <span className="mono">E{hover.id}</span> {t(`ws.edgeKind.${edgeKey(hover.edgeKind)}`)}
            <span className="mono faint"> · L {fmtMm(hover.length)} mm</span>
          </span>
        ) : hover?.kind === 'point' ? (
          <span className="mono faint">{vec(hover.point)}</span>
        ) : hasModel ? (
          <span className="faint">{t('ws.vw.hint')}</span>
        ) : null}
      </div>
    </div>
  );
});

const SURFACES = ['plane', 'cylinder', 'cone', 'sphere', 'torus', 'bspline', 'mesh'] as const;
export function surfaceKey(s: string): (typeof SURFACES)[number] | 'other' {
  return (SURFACES as readonly string[]).includes(s) ? (s as (typeof SURFACES)[number]) : 'other';
}
const EDGE_KINDS = ['line', 'circle', 'ellipse', 'bspline'] as const;
export function edgeKey(s: string): (typeof EDGE_KINDS)[number] | 'other' {
  return (EDGE_KINDS as readonly string[]).includes(s) ? (s as (typeof EDGE_KINDS)[number]) : 'other';
}
