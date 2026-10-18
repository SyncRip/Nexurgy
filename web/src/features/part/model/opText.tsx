// Icons and one-line summaries for modeling ops (feature tree, diffs).

import { Box, CircleDot, Copy, Cylinder, FoldVertical, Layers, Move3d, PackageOpen, RotateCw, Scissors, Spline, SquareRoundCorner, TriangleRight } from 'lucide-react';
import type { ReactNode } from 'react';
import type { ModelOp, ModelOpType, Profile } from '../../../api/types';
import type { TFunction } from '../../../i18n/I18nProvider';
import { planeText, sketchSummary } from '../../editor/geometry';

export const OP_ICON: Record<ModelOpType, ReactNode> = {
  box: <Box aria-hidden />,
  cylinder: <Cylinder aria-hidden />,
  extrude: <Layers aria-hidden />,
  hole: <CircleDot aria-hidden />,
  fillet: <SquareRoundCorner aria-hidden />,
  chamfer: <TriangleRight aria-hidden />,
  transform: <Move3d aria-hidden />,
  cut: <Scissors aria-hidden />,
  revolve: <RotateCw aria-hidden />,
  sweep: <Spline aria-hidden />,
  bent_plate: <FoldVertical aria-hidden />,
  shell: <PackageOpen aria-hidden />,
  pattern: <Copy aria-hidden />,
  coil: <Spline aria-hidden />,
};

const n = (v: number | null | undefined) => (v === null || v === undefined || Number.isNaN(v) ? '?' : String(Math.round(v * 1000) / 1000));

function profileText(p: Profile, t: TFunction): string {
  const prof =
    p.kind === 'rect'
      ? `${n(p.width)}×${n(p.length)}`
      : p.kind === 'circle'
        ? `Ø${n(p.radius * 2)}`
        : p.kind === 'slot'
          ? `${n(p.length)}×${n(p.width)}`
          : p.kind === 'ring'
            ? `Ø${n(p.outer_radius * 2)}/Ø${n(p.inner_radius * 2)}`
            : p.kind === 'polygon'
              ? t('ws.op.polygonPoints', { n: p.points.length })
              : t('geo.sketch.summary', sketchSummary(p));
  return `${t(`ws.profile.${p.kind}`)} ${prof}`;
}

/** Short technical summary, e.g. "80×50×10", "Ø8 THRU ×4". */
export function opSummary(op: ModelOp, t: TFunction): string {
  switch (op.type) {
    case 'box':
      return `${n(op.size[0])}×${n(op.size[1])}×${n(op.size[2])}`;
    case 'cylinder':
      return `Ø${n(op.radius * 2)} × ${n(op.height)} · ${(op.axis ?? 'z').toUpperCase()}`;
    case 'extrude':
      return `${profileText(op.profile, t)} · h${n(op.height)} · ${planeText(op.plane)}`;
    case 'hole': {
      const count = op.pattern ? (op.pattern.kind === 'linear' ? op.pattern.count[0] * op.pattern.count[1] : op.pattern.count) : 1;
      const depth = op.depth === null || op.depth === undefined ? 'THRU' : `↧${n(op.depth)}`;
      const extra = op.counterbore ? ` ⌴Ø${n(op.counterbore.diameter)}` : op.countersink ? ` ⌵Ø${n(op.countersink.diameter)}` : '';
      return `Ø${n(op.diameter)} ${depth}${extra}${count > 1 ? ` ×${count}` : ''}${op.plane ? ` · ${planeText(op.plane)}` : ''}`;
    }
    case 'fillet':
      return `R${n(op.radius)} · ${t(`ws.edges.${op.edges.group ?? 'all'}`)}${op.edges.near ? ` · ${t('ws.edges.nearShort')}` : ''}`;
    case 'chamfer':
      return `C${n(op.distance)} · ${t(`ws.edges.${op.edges.group ?? 'all'}`)}${op.edges.near ? ` · ${t('ws.edges.nearShort')}` : ''}`;
    case 'transform':
      return `${op.target} · Δ(${(op.translate ?? [0, 0, 0]).map(n).join(', ')})`;
    case 'cut':
      return `${profileText(op.profile, t)} · ${op.depth === null || op.depth === undefined ? 'THRU' : `↧${n(op.depth)}`} · ${planeText(op.plane)}`;
    case 'revolve':
      return `${t('geo.sketch.summary', sketchSummary(op.profile))} · ${n(op.angle ?? 360)}°`;
    case 'sweep':
      return `${profileText(op.profile, t)} · ${op.path.segments.length} seg`;
    case 'bent_plate':
      return `t${n(op.thickness)} · ${op.legs.map(n).join('/')} · ${op.bends.map((b) => `${n(b)}°`).join('/')} · R${n(op.inner_radius ?? 0)}`;
    case 'shell':
      return `t${n(op.thickness)} · ${(op.open_faces ?? []).length}`;
    case 'pattern':
      return `${op.target} ×${op.layout.count * (op.layout.kind === 'linear' ? (op.layout.count2 ?? 1) : 1)} · ${t(`geo.pattern.${op.layout.kind}`)}`;
    case 'coil': {
      const wire = op.section.kind === 'circle' ? `Ø${n(op.section.radius * 2)}` : `${n(op.section.width)}×${n(op.section.length)}`;
      const len = op.length !== null && op.length !== undefined ? `L${n(op.length)}` : `p${n(op.pitch)}`;
      return `${wire} · Dm${n(op.mean_diameter)} · n${n(op.turns)} · ${len} · ${t(`geo.coil.end.${op.end_form ?? 'open'}`)}`;
    }
  }
}

export function opTitle(op: ModelOp, t: TFunction): string {
  return op.label?.trim() || t(`ws.op.${op.type}`);
}

const KNOWN: readonly string[] = ['box', 'cylinder', 'extrude', 'hole', 'fillet', 'chamfer', 'transform', 'cut', 'revolve', 'sweep', 'bent_plate', 'shell', 'pattern',
  'coil'];

/** Display name of an op type coming from the server (diffs may include "base_step"). */
export function opTypeLabel(type: string, t: TFunction): string {
  if (KNOWN.includes(type)) return t(`ws.op.${type as ModelOpType}`);
  if (type === 'base_step') return t('ws.tree.base');
  return type;
}
