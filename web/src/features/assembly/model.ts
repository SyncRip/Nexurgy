// Pure helpers of the assembly workspace (unit-tested): transform read-out, tree flattening, path keys,
// check summaries. Transforms are 4x4 row-major rigid matrices, rotation order X → Y → Z (R = Rz·Ry·Rx),
// the same convention as server/app/cad/assembly_model.py.

import type { AssemblyRevision, CheckRecord, RevisionBrief, SceneNode } from '../../api/assemblies';
import type { CheckStatus, Matrix4 } from '../../api/engineering';
import { coverageOf } from '../orchestration/coverageModel';
import { unverifiedBesideCheck } from '../turbo/model';

/** Build completeness and check status are separate: a usable partial revision is not a completed assembly. */
export function assemblyListStatus(revision: Pick<RevisionBrief, 'status' | 'coverage' | 'verification' | 'check_status'>) {
  const coverage = coverageOf({ coverage: revision.coverage });
  const unverified = revision.status !== 'failed' && unverifiedBesideCheck(revision.verification, revision.check_status);
  const incomplete = !!coverage && coverage.status !== 'complete';
  return { coverage, failed: revision.status === 'failed', incompleteUnverified: unverified && incomplete, completedUnverified: unverified && !incomplete };
}

export const pathKey = (path: readonly string[]): string => path.join('/');

/** True when `key` is `prefix` itself or a leaf / node below it. */
export function underPath(key: string, prefix: string): boolean {
  return key === prefix || key.startsWith(`${prefix}/`);
}

const round = (v: number, d = 4) => {
  const r = Math.round(v * 10 ** d) / 10 ** d;
  return Object.is(r, -0) ? 0 : r;
};

/** Translation and X→Y→Z rotation angles (degrees) of a rigid transform. */
export function decompose(m: Matrix4): { translate: [number, number, number]; rotate: [number, number, number] } {
  const r20 = Math.max(-1, Math.min(1, m[2][0]));
  const b = Math.asin(-r20);
  let a: number;
  let c: number;
  if (Math.abs(Math.cos(b)) > 1e-9) {
    a = Math.atan2(m[2][1], m[2][2]);
    c = Math.atan2(m[1][0], m[0][0]);
  } else {
    // gimbal lock: put everything into the Z angle
    a = 0;
    c = Math.atan2(-m[0][1], m[1][1]);
  }
  const deg = (x: number) => round((x * 180) / Math.PI);
  return { translate: [round(m[0][3]), round(m[1][3]), round(m[2][3])], rotate: [deg(a), deg(b), deg(c)] };
}

/** Rotation X → Y → Z (degrees) then translation, as a row-major 4x4 (server `placement`). */
export function placementMatrix(translate: readonly number[], rotate: readonly number[]): Matrix4 {
  const [ax, ay, az] = rotate.map((d) => (d * Math.PI) / 180);
  const [cx, sx, cy, sy, cz, sz] = [Math.cos(ax), Math.sin(ax), Math.cos(ay), Math.sin(ay), Math.cos(az), Math.sin(az)];
  // R = Rz · Ry · Rx
  const r = [
    [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
    [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
    [-sy, cy * sx, cy * cx],
  ];
  return [
    [r[0][0], r[0][1], r[0][2], translate[0]],
    [r[1][0], r[1][1], r[1][2], translate[1]],
    [r[2][0], r[2][1], r[2][2], translate[2]],
    [0, 0, 0, 1],
  ];
}

export interface TreeRow {
  node: SceneNode;
  key: string;
  depth: number;
  hasChildren: boolean;
  position: number;
  siblings: number;
}

/** Depth-first rows of the instance tree; children of collapsed keys are skipped. */
export function flattenTree(nodes: SceneNode[], collapsed: ReadonlySet<string>, depth = 0, out: TreeRow[] = []): TreeRow[] {
  for (const [index, n] of nodes.entries()) {
    const key = pathKey(n.path);
    const hasChildren = n.children.length > 0;
    out.push({ node: n, key, depth, hasChildren, position: index + 1, siblings: nodes.length });
    if (hasChildren && !collapsed.has(key)) flattenTree(n.children, collapsed, depth + 1, out);
  }
  return out;
}

/** The node at a path key (or undefined). */
export function findNode(nodes: SceneNode[], key: string): SceneNode | undefined {
  for (const n of nodes) {
    const k = pathKey(n.path);
    if (k === key) return n;
    if (underPath(key, k)) {
      const hit = findNode(n.children, key);
      if (hit) return hit;
    }
  }
  return undefined;
}

export const CHECK_ORDER = ['assembly.interference', 'assembly.clearance', 'assembly.mate_residual', 'assembly.bom', 'assembly.spec'] as const;
export type CheckName = (typeof CHECK_ORDER)[number];

/** Known checks in display order with their latest record (null = not run on this revision). */
export function checkRows(rev: Pick<AssemblyRevision, 'checks'>): { name: CheckName; record: CheckRecord | null }[] {
  return CHECK_ORDER.filter((n) => n in rev.checks || n === 'assembly.interference' || n === 'assembly.mate_residual' || n === 'assembly.bom').map(
    (name) => ({ name, record: rev.checks[name] ?? null }),
  );
}

const KNOWN: readonly CheckStatus[] = ['not_requested', 'skipped_turbo', 'pending', 'passed', 'failed', 'unknown'];

/** An unknown enum value from the server never reads as success. */
export function safeCheckStatus(s: unknown): CheckStatus {
  return (KNOWN as readonly unknown[]).includes(s) ? (s as CheckStatus) : 'unknown';
}

export function fmtNum(v: number, digits = 3): string {
  return String(round(v, digits));
}
