// SVG drafting canvas: renders a drawing document version (model space in mm, y up; SVG y = -model y), pans and
// zooms (wheel, drag with the pan tool / middle button, pinch), snaps (endpoint, midpoint, centre, quadrant,
// intersection) and turns tool clicks into edit commands. Geometry and snap logic live in pure modules.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { underlayUrl, type DimensionEntity, type DraftBlock, type DraftCommand, type DraftContent, type DraftEntity, type DraftLayer, type HatchLoop, type Pt, type Underlay } from '../../api/drafting';
import { useI18n } from '../../i18n/I18nProvider';
import { add, angleOf, arcSweep, bboxOf, bulgeArc, dimensionGeometry, dist, distanceTo, entityPoints, hatchAt, inWindow, normAngle, pick, polySegments, scale, sub, unitAt } from './geometry';
import { frameGeometry } from './sheet';
import { findSnap, type Snap, type SnapKind } from './snaps';

export type Tool =
  | 'select'
  | 'pan'
  | 'line'
  | 'polyline'
  | 'circle'
  | 'arc'
  | 'text'
  | 'leader'
  | 'dim_linear'
  | 'dim_aligned'
  | 'dim_angular'
  | 'dim_diameter'
  | 'dim_radius'
  | 'hatch'
  | 'insert'
  | 'move'
  | 'copy'
  | 'rotate'
  | 'calibrate';

export const DRAW_TOOLS: Tool[] = ['line', 'polyline', 'circle', 'arc', 'text', 'leader', 'dim_linear', 'dim_aligned', 'dim_angular', 'dim_diameter', 'dim_radius', 'hatch', 'insert'];
export const EDIT_TOOLS: Tool[] = ['move', 'copy', 'rotate'];

// AutoCAD colour index → screen colour (7 follows the theme ink).
const ACI: Record<number, string> = { 1: '#ff4d4d', 2: '#f2c94c', 3: '#3ecf8e', 4: '#42d3e0', 5: '#4f8bff', 6: '#d36bff', 8: '#8b95a3', 9: '#c0c6ce' };
const DASH: Record<string, string | undefined> = { CENTER: '12 3 2 3', HIDDEN: '4 3', DASHED: '8 4', PHANTOM: '14 3 3 3 3 3', DOT: '1 3' };

export interface CanvasRequests {
  text: (kind: 'text' | 'leader') => Promise<string | null>;
  number: (kind: 'rotate' | 'calibrate') => Promise<number | null>;
}

interface Props {
  content: DraftContent;
  versionId: string;
  tool: Tool;
  selection: string[];
  onSelect: (ids: string[], additive: boolean) => void;
  onCommand: (cmds: DraftCommand[]) => void;
  onCalibrate: (underlayId: string, p1: Pt, p2: Pt, distance: number) => void;
  requests: CanvasRequests;
  snapKinds: ReadonlySet<SnapKind>;
  readOnly: boolean;
  /** Static saved-version thumbnail: same renderer, without editor input or controls. */
  previewLabel?: string;
  activeLayer: string;
  activeBlock: string | null;
  fitKey: number;
  onStatus?: (s: { cursor: Pt | null; snap: Snap | null; step: number }) => void;
}

interface View {
  cx: number;
  cy: number;
  s: number; // screen px per mm
}

function layerMap(layers: DraftLayer[]) {
  return new Map(layers.map((l) => [l.name, l]));
}

const Y = (p: Pt): [number, number] => [p[0], -p[1]];

function polyPath(points: Pt[], bulges: number[] | null | undefined, closed: boolean): string {
  if (!points.length) return '';
  let d = `M${points[0][0]} ${-points[0][1]}`;
  for (const [a, b, bulge] of polySegments(points, bulges, closed)) {
    if (bulge) {
      const arc = bulgeArc(a, b, bulge);
      const large = Math.abs(4 * Math.atan(bulge)) > Math.PI ? 1 : 0;
      // model CCW (bulge > 0) is clockwise on screen (y flipped): sweep flag 0
      d += ` A${arc.r} ${arc.r} 0 ${large} ${bulge > 0 ? 0 : 1} ${b[0]} ${-b[1]}`;
    } else d += ` L${b[0]} ${-b[1]}`;
  }
  return closed ? `${d} Z` : d;
}

function arcPath(c: Pt, r: number, start: number, end: number): string {
  const sweep = arcSweep(start, end);
  if (sweep >= 359.999) return `M${c[0] + r} ${-c[1]} A${r} ${r} 0 1 0 ${c[0] - r} ${-c[1]} A${r} ${r} 0 1 0 ${c[0] + r} ${-c[1]}`;
  const s = add(c, scale(unitAt(start), r));
  const e = add(c, scale(unitAt(end), r));
  return `M${s[0]} ${-s[1]} A${r} ${r} 0 ${sweep > 180 ? 1 : 0} 0 ${e[0]} ${-e[1]}`;
}

function loopPath(lp: HatchLoop): string {
  if (lp.points) return polyPath(lp.points, lp.bulges, true);
  if (lp.center && lp.radius != null) return arcPath(lp.center, lp.radius, 0, 360) + ' Z';
  return '';
}

function arrowPath(tip: Pt, dir: Pt, size: number): string {
  const back = sub(tip, scale(dir, size));
  const n: Pt = [-dir[1] * size * 0.3, dir[0] * size * 0.3];
  const p1 = add(back, n);
  const p2 = sub(back, n);
  return `M${tip[0]} ${-tip[1]} L${p1[0]} ${-p1[1]} L${p2[0]} ${-p2[1]} Z`;
}

function TextEl({ at, text, height, rotation = 0, anchor = 'start', className, lines }: { at: Pt; text?: string; height: number; rotation?: number; anchor?: 'start' | 'middle' | 'end'; className?: string; lines?: string[] }) {
  const [x, y] = Y(at);
  const tr = rotation ? `rotate(${-rotation} ${x} ${y})` : undefined;
  if (lines) {
    return (
      <text x={x} y={y} fontSize={height} textAnchor={anchor} transform={tr} className={className}>
        {lines.map((l, i) => (
          <tspan key={i} x={x} dy={i === 0 ? height : height * 1.67}>
            {l || ' '}
          </tspan>
        ))}
      </text>
    );
  }
  return (
    <text x={x} y={y} fontSize={height} textAnchor={anchor} transform={tr} className={className}>
      {text}
    </text>
  );
}

function EntityShape({ e, blocks, arrow, dimText, hatchId }: { e: DraftEntity; blocks: Map<string, DraftBlock>; arrow: number; dimText: number; hatchId: string }): ReactNode {
  switch (e.type) {
    case 'line':
      return <line x1={e.p1[0]} y1={-e.p1[1]} x2={e.p2[0]} y2={-e.p2[1]} />;
    case 'polyline':
      return <path d={polyPath(e.points, e.bulges, e.closed)} />;
    case 'circle':
      return <circle cx={e.center[0]} cy={-e.center[1]} r={e.radius} />;
    case 'arc':
      return <path d={arcPath(e.center, e.radius, e.start_angle, e.end_angle)} />;
    case 'text':
      return <TextEl at={e.insert} text={e.text} height={e.height} rotation={e.rotation} anchor={e.align === 'center' ? 'middle' : e.align === 'right' ? 'end' : 'start'} className="dr-text" />;
    case 'mtext':
      return <TextEl at={e.insert} lines={e.text.split('\n')} height={e.height} rotation={e.rotation} className="dr-text" />;
    case 'leader': {
      const pts = e.points;
      const dir = sub(pts[0], pts[1]);
      const l = Math.hypot(dir[0], dir[1]) || 1;
      const end = pts[pts.length - 1];
      const right = end[0] >= pts[pts.length - 2][0];
      return (
        <g>
          <path d={polyPath(pts, null, false)} />
          <path className="dr-fill" d={arrowPath(pts[0], [dir[0] / l, dir[1] / l], arrow)} />
          {e.text && <TextEl at={[end[0] + (right ? 1 : -1) * e.height * 0.5, end[1] + e.height / 2]} lines={e.text.split('\n')} height={e.height} anchor={right ? 'start' : 'end'} className="dr-text" />}
        </g>
      );
    }
    case 'dimension': {
      const g = dimensionGeometry(e as DimensionEntity);
      return (
        <g>
          {g.lines.map(([a, b], i) => (
            <line key={i} x1={a[0]} y1={-a[1]} x2={b[0]} y2={-b[1]} />
          ))}
          {g.arcs.map((a, i) => (
            <path key={`a${i}`} d={arcPath(a.c, a.r, a.start, a.end)} />
          ))}
          {g.arrows.map((a, i) => (
            <path key={`r${i}`} className="dr-fill" d={arrowPath(a.tip, a.dir, arrow)} />
          ))}
          <TextEl at={g.text.at} text={g.text.value} height={dimText} rotation={g.text.rotation} anchor="middle" className="dr-text dr-dimtext" />
        </g>
      );
    }
    case 'hatch':
      return <path className={e.solid ? 'dr-hatch-solid' : 'dr-hatch'} fillRule="evenodd" d={e.loops.map(loopPath).join(' ')} style={e.solid ? undefined : { fill: `url(#${hatchId})` }} />;
    case 'insert': {
      const b = blocks.get(e.block);
      if (!b) return null;
      const [x, y] = Y(e.insert);
      return (
        <g transform={`translate(${x} ${y}) rotate(${-e.rotation}) scale(${e.xscale} ${e.yscale}) translate(${-b.base[0]} ${b.base[1]})`}>
          {b.entities.map((be) => (
            <EntityShape key={be.id} e={be} blocks={blocks} arrow={arrow} dimText={dimText} hatchId={hatchId} />
          ))}
        </g>
      );
    }
  }
}

function UnderlayImage({ u, versionId }: { u: Underlay; versionId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let made: string | null = null;
    underlayUrl(versionId, u.id)
      .then((x) => {
        made = x;
        if (alive) setUrl(x);
        else URL.revokeObjectURL(x);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [versionId, u.id, u.sha256]);
  if (!url || !u.visible) return null;
  const mmpp = u.calibration.mm_per_px ?? 1;
  const w = u.width_px * mmpp;
  const h = u.height_px * mmpp;
  const [ox, oy] = u.origin;
  return <image href={url} x={ox} y={-(oy + h)} width={w} height={h} opacity={u.opacity} preserveAspectRatio="none" transform={u.rotation ? `rotate(${-u.rotation} ${ox} ${-oy})` : undefined} />;
}

/** Underlay pixel coordinates (origin top-left, y down) of a model point. */
export function toUnderlayPx(u: Underlay, p: Pt): Pt {
  const mmpp = u.calibration.mm_per_px ?? 1;
  const h = u.height_px * mmpp;
  return [(p[0] - u.origin[0]) / mmpp, (u.origin[1] + h - p[1]) / mmpp];
}

export function DraftingCanvas({ content, versionId, tool, selection, onSelect, onCommand, onCalibrate, requests, snapKinds, readOnly, previewLabel, activeLayer, activeBlock, fitKey, onStatus }: Props) {
  const { t } = useI18n();
  const staticPreview = previewLabel !== undefined;
  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 800, h: 560, measured: false });
  const [view, setView] = useState<View>({ cx: 0, cy: 0, s: 2 });
  const [pts, setPts] = useState<Pt[]>([]);
  const [target, setTarget] = useState<DraftEntity | null>(null);
  const [cursor, setCursor] = useState<Pt | null>(null);
  const [snap, setSnap] = useState<Snap | null>(null);
  const [box, setBox] = useState<{ a: Pt; b: Pt } | null>(null);
  const drag = useRef<{ kind: 'pan' | 'box' | 'click'; x: number; y: number; start: Pt; view: View; moved: boolean } | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ d: number; view: View; mid: { x: number; y: number } } | null>(null);
  const busy = useRef(false);

  const layers = useMemo(() => layerMap(content.layers), [content.layers]);
  const blocks = useMemo(() => new Map(content.blocks.map((b) => [b.name, b])), [content.blocks]);
  const visible = useCallback((e: DraftEntity) => layers.get(e.layer)?.visible !== false, [layers]);
  const shown = useMemo(() => content.entities.filter(visible), [content.entities, visible]);
  const sheet = content.sheets[0];
  const k = sheet ? 1 / sheet.scale : 1;
  const arrow = 2.5 * k;
  const dimText = 3 * k;
  const hatchId = `dr-hatch-${versionId.slice(0, 8)}`;

  // canvas size
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setSize({ w: Math.max(el.clientWidth, 100), h: Math.max(el.clientHeight, 100), measured: true });
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, []);

  // fit to content (and sheet) on load / request
  const fit = () => {
    const pts2: Pt[] = content.entities.flatMap(entityPoints);
    for (const sh of content.sheets) {
      const g = frameGeometry(sh);
      if (sh.draw_frame) pts2.push(...g.border);
    }
    for (const u of content.underlays) {
      const mmpp = u.calibration.mm_per_px ?? 1;
      pts2.push(u.origin, [u.origin[0] + u.width_px * mmpp, u.origin[1] + u.height_px * mmpp]);
    }
    const bb = bboxOf(pts2) ?? [-50, -50, 150, 100];
    const w = Math.max(bb[2] - bb[0], 10);
    const h = Math.max(bb[3] - bb[1], 10);
    setView({ cx: (bb[0] + bb[2]) / 2, cy: (bb[1] + bb[3]) / 2, s: Math.min(size.w / (w * 1.1), size.h / (h * 1.1)) });
  };
  const fitted = useRef<{ key: number; w: number } | null>(null);
  useEffect(() => {
    // fit once per fitKey after the canvas has its real size, and again when its width changes a lot
    // (window resize, phone rotation) — not on every small resize
    if (!size.measured) return;
    const last = fitted.current;
    if (last && last.key === fitKey && Math.abs(size.w - last.w) < last.w * 0.25) return;
    fitted.current = { key: fitKey, w: size.w };
    fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey, size.measured, size.w]);

  // reset the in-progress operation when the tool changes
  useEffect(() => {
    setPts([]);
    setTarget(null);
  }, [tool]);

  useEffect(() => {
    onStatus?.({ cursor, snap, step: pts.length });
  }, [cursor, snap, pts.length, onStatus]);

  const vb = useMemo(() => {
    const w = size.w / view.s;
    const h = size.h / view.s;
    return { x: view.cx - w / 2, y: -(view.cy + h / 2), w, h };
  }, [size, view]);

  const toModel = useCallback(
    (clientX: number, clientY: number): Pt => {
      const svg = svgRef.current;
      if (!svg) return [0, 0];
      const r = svg.getBoundingClientRect();
      const x = vb.x + ((clientX - r.left) / r.width) * vb.w;
      const y = vb.y + ((clientY - r.top) / r.height) * vb.h;
      return [x, -y];
    },
    [vb],
  );

  // wheel zoom around the cursor (non-passive listener so the page does not scroll)
  useEffect(() => {
    const el = svgRef.current;
    if (!el || staticPreview) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const p = toModel(ev.clientX, ev.clientY);
      const f = Math.exp(-ev.deltaY * 0.0015);
      setView((v) => {
        const s = Math.min(Math.max(v.s * f, 0.005), 400);
        return { s, cx: p[0] - (p[0] - v.cx) * (v.s / s), cy: p[1] - (p[1] - v.cy) * (v.s / s) };
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [toModel, staticPreview]);

  const tolModel = 8 / view.s;
  const snapping = !readOnly && tool !== 'select' && tool !== 'pan';
  const snapped = (raw: Pt): { p: Pt; s: Snap | null } => {
    if (!snapping) return { p: raw, s: null };
    const s = findSnap(shown, raw, tolModel, snapKinds, distanceTo);
    return { p: s ? s.point : raw, s };
  };

  const layer = activeLayer || '0';
  const addEntity = (e: Partial<DraftEntity>) => onCommand([{ op: 'add_entities', entities: [{ layer, ...e } as Partial<DraftEntity>] }]);
  const dimLayer = layers.has('DIM') ? 'DIM' : layer;

  const click = async (raw: Pt) => {
    if (busy.current) return;
    const { p } = snapped(raw);
    const next = [...pts, p];
    const done = () => {
      setPts([]);
      setTarget(null);
    };
    switch (tool) {
      case 'line':
        if (next.length === 2) {
          if (dist(next[0], next[1]) > 1e-9) addEntity({ type: 'line', p1: next[0], p2: next[1] });
          setPts([p]); // chain: the end point starts the next segment
        } else setPts(next);
        return;
      case 'polyline':
        if (pts.length >= 2 && dist(p, pts[0]) <= tolModel) {
          addEntity({ type: 'polyline', points: pts, closed: true });
          done();
        } else setPts(next);
        return;
      case 'leader':
        setPts(next);
        return;
      case 'circle':
        if (next.length === 2) {
          if (dist(next[0], next[1]) > 1e-9) addEntity({ type: 'circle', center: next[0], radius: dist(next[0], next[1]) });
          done();
        } else setPts(next);
        return;
      case 'arc':
        if (next.length === 3) {
          const [c, s, e] = next;
          addEntity({ type: 'arc', center: c, radius: dist(c, s), start_angle: normAngle(angleOf(sub(s, c))), end_angle: normAngle(angleOf(sub(e, c))) });
          done();
        } else setPts(next);
        return;
      case 'text': {
        busy.current = true;
        try {
          const text = await requests.text('text');
          if (text) addEntity({ type: 'text', insert: p, text, height: 3.5 * k, rotation: 0, align: 'left' });
        } finally {
          busy.current = false;
        }
        done();
        return;
      }
      case 'dim_linear':
      case 'dim_aligned':
        if (next.length === 3) {
          const [a, b, loc] = next;
          if (tool === 'dim_aligned') addEntity({ type: 'dimension', dimtype: 'aligned', p1: a, p2: b, location: loc, angle: 0, layer: dimLayer } as Partial<DraftEntity>);
          else {
            const m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
            const horizontal = Math.abs(loc[1] - m[1]) >= Math.abs(loc[0] - m[0]);
            addEntity({ type: 'dimension', dimtype: 'linear', p1: a, p2: b, location: loc, angle: horizontal ? 0 : 90, layer: dimLayer } as Partial<DraftEntity>);
          }
          done();
        } else setPts(next);
        return;
      case 'dim_angular':
        if (next.length === 4) {
          const [c, a, b, loc] = next;
          addEntity({ type: 'dimension', dimtype: 'angular', center: c, p1: a, p2: b, location: loc, angle: 0, layer: dimLayer } as Partial<DraftEntity>);
          done();
        } else setPts(next);
        return;
      case 'dim_diameter':
      case 'dim_radius': {
        if (!target) {
          const hit = pick(shown.filter((e) => e.type === 'circle' || e.type === 'arc'), raw, tolModel);
          if (hit) setTarget(hit);
          return;
        }
        if (target.type === 'circle' || target.type === 'arc') {
          addEntity({ type: 'dimension', dimtype: tool === 'dim_diameter' ? 'diameter' : 'radius', center: target.center, radius: target.radius, angle: normAngle(angleOf(sub(raw, target.center))), refs: [target.id], layer: dimLayer } as Partial<DraftEntity>);
        }
        done();
        return;
      }
      case 'hatch': {
        const h = hatchAt(shown, raw);
        if (h) addEntity({ type: 'hatch', loops: h.loops, pattern: 'ANSI31', scale: k, angle: 0, solid: false, boundary_refs: h.refs, layer: layers.has('HATCH') ? 'HATCH' : layer } as Partial<DraftEntity>);
        return;
      }
      case 'insert':
        if (activeBlock) onCommand([{ op: 'insert_block', block: activeBlock, insert: p, rotation: 0, layer }]);
        return;
      case 'move':
      case 'copy':
        if (!selection.length) return;
        if (next.length === 2) {
          onCommand([{ op: 'transform', ids: selection, dx: next[1][0] - next[0][0], dy: next[1][1] - next[0][1], copy: tool === 'copy' }]);
          done();
        } else setPts(next);
        return;
      case 'rotate': {
        if (!selection.length) return;
        busy.current = true;
        try {
          const deg = await requests.number('rotate');
          if (deg != null && deg !== 0) onCommand([{ op: 'transform', ids: selection, rotate: deg, center: p }]);
        } finally {
          busy.current = false;
        }
        done();
        return;
      }
      case 'calibrate': {
        const u = content.underlays[0];
        if (!u) return;
        if (next.length === 2) {
          busy.current = true;
          try {
            const d = await requests.number('calibrate');
            if (d != null && d > 0) onCalibrate(u.id, toUnderlayPx(u, next[0]), toUnderlayPx(u, next[1]), d);
          } finally {
            busy.current = false;
          }
          done();
        } else setPts(next);
        return;
      }
      default:
        return;
    }
  };

  const finish = async () => {
    if (tool === 'polyline' && pts.length >= 2) addEntity({ type: 'polyline', points: pts, closed: false });
    if (tool === 'leader' && pts.length >= 2) {
      busy.current = true;
      try {
        const text = await requests.text('leader');
        addEntity({ type: 'leader', points: pts, text: text ?? '', height: 3.5 * k, layer: dimLayer } as Partial<DraftEntity>);
      } finally {
        busy.current = false;
      }
    }
    setPts([]);
    setTarget(null);
  };

  const onPointerDown = (ev: ReactPointerEvent<SVGSVGElement>) => {
    (ev.target as Element).setPointerCapture?.(ev.pointerId);
    pointers.current.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { d: Math.hypot(a.x - b.x, a.y - b.y), view, mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } };
      drag.current = null;
      return;
    }
    const start = toModel(ev.clientX, ev.clientY);
    const pan = ev.button === 1 || tool === 'pan' || (readOnly && tool === 'select' && ev.pointerType === 'touch');
    drag.current = { kind: pan ? 'pan' : tool === 'select' ? 'box' : 'click', x: ev.clientX, y: ev.clientY, start, view, moved: false };
  };

  const onPointerMove = (ev: ReactPointerEvent<SVGSVGElement>) => {
    if (pointers.current.has(ev.pointerId)) pointers.current.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const f = d / (pinch.current.d || 1);
      const v0 = pinch.current.view;
      setView({ ...v0, s: Math.min(Math.max(v0.s * f, 0.005), 400) });
      return;
    }
    const p = toModel(ev.clientX, ev.clientY);
    const dr = drag.current;
    if (dr) {
      const moved = Math.hypot(ev.clientX - dr.x, ev.clientY - dr.y) > 4;
      dr.moved = dr.moved || moved;
      if (dr.kind === 'pan' && dr.moved) {
        setView({ ...dr.view, cx: dr.view.cx - (ev.clientX - dr.x) / dr.view.s, cy: dr.view.cy + (ev.clientY - dr.y) / dr.view.s });
        return;
      }
      if (dr.kind === 'box' && dr.moved) setBox({ a: dr.start, b: p });
    }
    const sn = snapped(p);
    setCursor(sn.p);
    setSnap(sn.s);
  };

  const onPointerUp = (ev: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.delete(ev.pointerId);
    if (pinch.current) {
      if (pointers.current.size < 2) pinch.current = null;
      drag.current = null;
      return;
    }
    const dr = drag.current;
    drag.current = null;
    if (!dr) return;
    const p = toModel(ev.clientX, ev.clientY);
    if (dr.kind === 'pan') return;
    if (dr.kind === 'box') {
      setBox(null);
      if (dr.moved) onSelect(inWindow(shown, dr.start, p), ev.shiftKey);
      else {
        const hit = pick(shown, p, tolModel, visible);
        onSelect(hit ? [hit.id] : [], ev.shiftKey);
      }
      return;
    }
    if (!readOnly && !dr.moved) void click(p);
  };

  const onKeyDown = (ev: ReactKeyboardEvent) => {
    if (ev.key === 'Escape') {
      if (pts.length && (tool === 'polyline' || tool === 'leader')) void finish();
      else {
        setPts([]);
        setTarget(null);
      }
    } else if (ev.key === 'Enter') void finish();
  };

  // ─── preview of the operation in progress ───
  const preview: ReactNode[] = [];
  if (cursor && !readOnly) {
    const c = cursor;
    const P = (d: string, key: string) => preview.push(<path key={key} d={d} />);
    if ((tool === 'line' || tool === 'move' || tool === 'copy' || tool === 'calibrate') && pts.length === 1) P(`M${pts[0][0]} ${-pts[0][1]} L${c[0]} ${-c[1]}`, 'l');
    if ((tool === 'polyline' || tool === 'leader') && pts.length) P(polyPath([...pts, c], null, false), 'pl');
    if (tool === 'circle' && pts.length === 1) preview.push(<circle key="c" cx={pts[0][0]} cy={-pts[0][1]} r={dist(pts[0], c)} />);
    if (tool === 'arc' && pts.length === 1) P(`M${pts[0][0]} ${-pts[0][1]} L${c[0]} ${-c[1]}`, 'ar');
    if (tool === 'arc' && pts.length === 2) P(arcPath(pts[0], dist(pts[0], pts[1]), angleOf(sub(pts[1], pts[0])), angleOf(sub(c, pts[0]))), 'a');
    if ((tool === 'dim_linear' || tool === 'dim_aligned') && pts.length >= 1) {
      if (pts.length === 1) P(`M${pts[0][0]} ${-pts[0][1]} L${c[0]} ${-c[1]}`, 'd1');
      else {
        const m = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2];
        const horizontal = Math.abs(c[1] - m[1]) >= Math.abs(c[0] - m[0]);
        const d: DimensionEntity = tool === 'dim_aligned'
          ? { id: 'preview', type: 'dimension', dimtype: 'aligned', p1: pts[0], p2: pts[1], location: c, angle: 0, layer: dimLayer, origin: 'person' }
          : { id: 'preview', type: 'dimension', dimtype: 'linear', p1: pts[0], p2: pts[1], location: c, angle: horizontal ? 0 : 90, layer: dimLayer, origin: 'person' };
        preview.push(<EntityShape key="dp" e={d} blocks={blocks} arrow={arrow} dimText={dimText} hatchId={hatchId} />);
      }
    }
    if (tool === 'dim_angular' && pts.length) P(polyPath([...pts.slice(0, 3), c].slice(0, Math.min(pts.length + 1, 3)), null, false), 'ang');
    if ((tool === 'dim_diameter' || tool === 'dim_radius') && target && (target.type === 'circle' || target.type === 'arc')) {
      const d: DimensionEntity = { id: 'preview', type: 'dimension', dimtype: tool === 'dim_diameter' ? 'diameter' : 'radius', center: target.center, radius: target.radius, angle: angleOf(sub(c, target.center)), layer: dimLayer, origin: 'person' };
      preview.push(<EntityShape key="dd" e={d} blocks={blocks} arrow={arrow} dimText={dimText} hatchId={hatchId} />);
    }
  }

  const sel = new Set(selection);
  const markerSize = 6 / view.s;

  return (
    <div ref={wrapRef} className={`dr-canvas tool-${tool}${staticPreview ? ' dr-canvas-preview' : ''}`} tabIndex={staticPreview ? undefined : 0} onKeyDown={staticPreview ? undefined : onKeyDown} aria-label={previewLabel ?? t('dr.canvas.label')} role={staticPreview ? 'img' : 'application'}>
      <svg
        ref={svgRef}
        viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
        width={size.w}
        height={size.h}
        onPointerDown={staticPreview ? undefined : onPointerDown}
        onPointerMove={staticPreview ? undefined : onPointerMove}
        onPointerUp={staticPreview ? undefined : onPointerUp}
        onPointerCancel={staticPreview ? undefined : onPointerUp}
        onPointerLeave={staticPreview ? undefined : () => setCursor(null)}
        onDoubleClick={staticPreview ? undefined : () => void finish()}
        onContextMenu={staticPreview ? undefined : (e) => {
          e.preventDefault();
          void finish();
        }}
      >
        <defs>
          <pattern id={hatchId} patternUnits="userSpaceOnUse" width={3.175 * k} height={3.175 * k} patternTransform="rotate(-45)">
            <line x1={0} y1={1.5 * k} x2={3.175 * k} y2={1.5 * k} className="dr-hatch-line" />
          </pattern>
        </defs>
        <rect x={vb.x} y={vb.y} width={vb.w} height={vb.h} className="dr-paper" />
        {content.underlays.map((u) => (
          <UnderlayImage key={u.id} u={u} versionId={versionId} />
        ))}
        {content.sheets
          .filter((s) => s.draw_frame)
          .map((s) => {
            const g = frameGeometry(s);
            return (
              <g key={s.id} className="dr-frame">
                <path d={polyPath(g.border, null, true)} />
                <path d={polyPath(g.inner, null, true)} className="dr-frame-inner" />
                {g.lines.map(([a, b], i) => (
                  <line key={i} x1={a[0]} y1={-a[1]} x2={b[0]} y2={-b[1]} />
                ))}
                {g.texts.map((tx, i) => (
                  <TextEl key={`t${i}`} at={tx.at} text={tx.text} height={tx.height} className={tx.role === 'label' ? 'dr-frame-label' : 'dr-frame-value'} />
                ))}
              </g>
            );
          })}
        {shown.map((e) => {
          const lay = layers.get(e.layer);
          const color = ACI[e.color ?? lay?.color ?? 7];
          const lt = (e.linetype ?? lay?.linetype ?? 'CONTINUOUS').toUpperCase();
          const heavy = (lay?.lineweight ?? -3) >= 50;
          return (
            <g
              key={e.id}
              className={`dr-ent dr-${e.type}${sel.has(e.id) ? ' is-selected' : ''}${e.origin === 'source' && !e.modified ? ' is-source' : ''}${e.origin === 'ai' ? ' is-ai' : ''}`}
              style={{ ['--ent' as string]: color, strokeDasharray: DASH[lt], strokeWidth: heavy ? 1.6 : undefined }}
              data-id={e.id}
            >
              <EntityShape e={e} blocks={blocks} arrow={arrow} dimText={dimText} hatchId={hatchId} />
            </g>
          );
        })}
        <g className="dr-preview">{preview}</g>
        {pts.map((p, i) => (
          <circle key={i} cx={p[0]} cy={-p[1]} r={markerSize * 0.5} className="dr-pick" />
        ))}
        {target && <g className="dr-target"><EntityShape e={target} blocks={blocks} arrow={arrow} dimText={dimText} hatchId={hatchId} /></g>}
        {box && <rect x={Math.min(box.a[0], box.b[0])} y={-Math.max(box.a[1], box.b[1])} width={Math.abs(box.b[0] - box.a[0])} height={Math.abs(box.b[1] - box.a[1])} className="dr-box" />}
        {snap && (
          <g className={`dr-snap dr-snap-${snap.kind}`}>
            {snap.kind === 'center' || snap.kind === 'quadrant' ? (
              <circle cx={snap.point[0]} cy={-snap.point[1]} r={markerSize} />
            ) : snap.kind === 'midpoint' ? (
              <path d={`M${snap.point[0] - markerSize} ${-snap.point[1] + markerSize} L${snap.point[0]} ${-snap.point[1] - markerSize} L${snap.point[0] + markerSize} ${-snap.point[1] + markerSize} Z`} />
            ) : snap.kind === 'intersection' ? (
              <path d={`M${snap.point[0] - markerSize} ${-snap.point[1] - markerSize} L${snap.point[0] + markerSize} ${-snap.point[1] + markerSize} M${snap.point[0] - markerSize} ${-snap.point[1] + markerSize} L${snap.point[0] + markerSize} ${-snap.point[1] - markerSize}`} />
            ) : (
              <rect x={snap.point[0] - markerSize} y={-snap.point[1] - markerSize} width={markerSize * 2} height={markerSize * 2} />
            )}
          </g>
        )}
      </svg>
      {!staticPreview && <div className="dr-zoom">
        <button type="button" className="btn sm icon" onClick={() => setView((v) => ({ ...v, s: v.s * 1.25 }))} aria-label={t('dr.zoom.in')}>
          +
        </button>
        <button type="button" className="btn sm icon" onClick={() => setView((v) => ({ ...v, s: v.s / 1.25 }))} aria-label={t('dr.zoom.out')}>
          −
        </button>
        <button type="button" className="btn sm" onClick={fit}>
          {t('dr.zoom.fit')}
        </button>
      </div>}
    </div>
  );
}
