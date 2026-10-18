// Browser-side analytic geometry for the gear_bracket template (L1, visual).
// Built from stacked extrusions with real holes/pockets so the silhouette,
// pocket corners and bores are geometrically correct for the parameters.
// Units: mm. Y is up; the wall's front face looks toward +Z.

import * as THREE from 'three';
import { layout } from '../domain/bracket';
import { templateFeatures, type FeatureInstance } from '../domain/engineering';
import type { BracketParams, FeatureRef } from '../domain/types';

export interface Materials {
  alu: THREE.Material;
  steel: THREE.Material;
  dark: THREE.Material;
  highlight: THREE.MeshStandardMaterial;
}

export function makeMaterials(): Materials {
  return {
    alu: new THREE.MeshStandardMaterial({ color: 0xc3c8cf, metalness: 0.92, roughness: 0.34, envMapIntensity: 1.1 }),
    steel: new THREE.MeshStandardMaterial({ color: 0x8d949c, metalness: 1, roughness: 0.26 }),
    dark: new THREE.MeshStandardMaterial({ color: 0x24272c, metalness: 0.6, roughness: 0.45 }),
    highlight: new THREE.MeshStandardMaterial({
      color: 0x829bff,
      emissive: 0x3158d9,
      emissiveIntensity: 0.55,
      metalness: 0.2,
      roughness: 0.5,
      transparent: true,
      opacity: 0.85,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -2,
    }),
  };
}

function roundedRectPath(path: THREE.Path, cx: number, cy: number, w: number, h: number, r: number) {
  const x0 = cx - w / 2;
  const y0 = cy - h / 2;
  const rr = Math.min(r, w / 2, h / 2);
  path.moveTo(x0 + rr, y0);
  path.lineTo(x0 + w - rr, y0);
  path.absarc(x0 + w - rr, y0 + rr, rr, -Math.PI / 2, 0, false);
  path.lineTo(x0 + w, y0 + h - rr);
  path.absarc(x0 + w - rr, y0 + h - rr, rr, 0, Math.PI / 2, false);
  path.lineTo(x0 + rr, y0 + h);
  path.absarc(x0 + rr, y0 + h - rr, rr, Math.PI / 2, Math.PI, false);
  path.lineTo(x0, y0 + rr);
  path.absarc(x0 + rr, y0 + rr, rr, Math.PI, Math.PI * 1.5, false);
  return path;
}

function circleHole(cx: number, cy: number, r: number, segs = 48) {
  const p = new THREE.Path();
  p.absarc(cx, cy, r, 0, Math.PI * 2, true);
  void segs;
  return p;
}

function rect(w: number, h: number, cx = 0, cy = 0) {
  const s = new THREE.Shape();
  s.moveTo(cx - w / 2, cy - h / 2);
  s.lineTo(cx + w / 2, cy - h / 2);
  s.lineTo(cx + w / 2, cy + h / 2);
  s.lineTo(cx - w / 2, cy + h / 2);
  s.closePath();
  return s;
}

/** Plate in the XZ plane (shape X → world X, shape Y → world -Z), extruded up along +Y. */
function horizontalSlab(shape: THREE.Shape, y0: number, thk: number, mat: THREE.Material) {
  const g = new THREE.ExtrudeGeometry(shape, { depth: thk, bevelEnabled: false, curveSegments: 40 });
  g.rotateX(-Math.PI / 2);
  g.translate(0, y0, 0);
  g.computeVertexNormals();
  return new THREE.Mesh(g, mat);
}

/** Plate in the XY plane, extruded along +Z from z0. */
function verticalSlab(shape: THREE.Shape, z0: number, thk: number, mat: THREE.Material) {
  const g = new THREE.ExtrudeGeometry(shape, { depth: thk, bevelEnabled: false, curveSegments: 48 });
  g.translate(0, 0, z0);
  return new THREE.Mesh(g, mat);
}

export interface BracketBuild {
  group: THREE.Group;
  features: Record<FeatureRef, THREE.Object3D[]>;
  edges: THREE.LineSegments[];
  dispose: () => void;
}

/**
 * Builds the bracket. Holes, bores, tapped holes and the pocket follow the
 * feature instances (`features`, from resolveFeatures); without them the
 * template's default instances are used. Feature overlays (thin shells inside
 * pockets/bores/holes) are hidden by default and shown when a feature category
 * (`userData.overlay`) or a feature instance (`userData.featureId`) is highlighted.
 */
export function buildBracket(p: BracketParams, mats: Materials, opts: { edges?: boolean; edgeColor?: number } = {}, featureList?: FeatureInstance[]): BracketBuild {
  const L = layout(p);
  const inst = featureList ?? templateFeatures(p);
  const g = new THREE.Group();
  g.name = 'bracket';
  const geos: THREE.BufferGeometry[] = [];
  const features = {} as Record<FeatureRef, THREE.Object3D[]>;
  const addF = (ref: FeatureRef, o: THREE.Object3D) => {
    (features[ref] ??= []).push(o);
  };

  // Shape Y maps to world -Z for horizontal slabs.
  const zToShapeY = (z: number) => -z;
  const cbores = inst.filter((f) => f.kind === 'cbore_hole');
  const thru = inst.filter((f) => f.kind === 'through_hole');
  const pocket = inst.find((f) => f.kind === 'pocket');
  const pocketCz = pocket ? pocket.pos[1] : L.pocketCz;
  const throughHoles = (s: THREE.Shape) => {
    cbores.forEach((f) => s.holes.push(circleHole(f.pos[0], zToShapeY(f.pos[1]), f.dia / 2)));
    thru.forEach((f) => s.holes.push(circleHole(f.pos[0], zToShapeY(f.pos[1]), f.dia / 2)));
  };
  const counterbores = (s: THREE.Shape) => cbores.forEach((f) => s.holes.push(circleHole(f.pos[0], zToShapeY(f.pos[1]), (f.cboreDia ?? p.cboreDia) / 2)));

  // Base, lower layer: through holes only.
  const lowerThk = p.baseThk - Math.max(p.cboreDepth, p.pocketDepth);
  const midTop = p.baseThk - Math.min(p.cboreDepth, p.pocketDepth);
  const lower = rect(p.length, p.baseDepth);
  throughHoles(lower);
  const baseLower = horizontalSlab(lower, 0, lowerThk, mats.alu);

  // Middle layer (between the deeper and shallower of pocket / counterbore floors).
  const mid = rect(p.length, p.baseDepth);
  const deeperIsPocket = p.pocketDepth >= p.cboreDepth;
  if (deeperIsPocket) {
    mid.holes.push(roundedRectPath(new THREE.Path(), 0, zToShapeY(pocketCz), p.pocketLen, p.pocketWidth, p.pocketR));
    throughHoles(mid);
  } else {
    counterbores(mid);
    thru.forEach((f) => mid.holes.push(circleHole(f.pos[0], zToShapeY(f.pos[1]), f.dia / 2)));
  }
  const baseMid = midTop - lowerThk > 1e-3 ? horizontalSlab(mid, lowerThk, midTop - lowerThk, mats.alu) : null;

  // Upper layer: pocket + counterbores + added through holes.
  const upper = rect(p.length, p.baseDepth);
  upper.holes.push(roundedRectPath(new THREE.Path(), 0, zToShapeY(pocketCz), p.pocketLen, p.pocketWidth, p.pocketR));
  counterbores(upper);
  thru.forEach((f) => upper.holes.push(circleHole(f.pos[0], zToShapeY(f.pos[1]), f.dia / 2)));
  const baseUpper = horizontalSlab(upper, midTop, p.baseThk - midTop, mats.alu);

  // Wall (above the base), with bores and tapped holes (drill Ø3.3 × 10 from the front).
  const wallH = p.wallHeight - p.baseThk;
  const bores = inst.filter((f) => f.kind === 'bore');
  const taps = inst.filter((f) => f.kind === 'tapped_hole');
  const tapDepth = Math.min(10, p.wallThk - 2);
  const wallBack = rect(p.length, wallH, 0, p.baseThk + wallH / 2);
  bores.forEach((f) => wallBack.holes.push(circleHole(f.pos[0], f.pos[1], f.dia / 2)));
  const wallFront = rect(p.length, wallH, 0, p.baseThk + wallH / 2);
  bores.forEach((f) => wallFront.holes.push(circleHole(f.pos[0], f.pos[1], f.dia / 2)));
  taps.forEach((f) => wallFront.holes.push(circleHole(f.pos[0], f.pos[1], f.dia / 2)));
  const backThk = p.wallThk - tapDepth;
  const wallB = verticalSlab(wallBack, L.wallZ0, backThk, mats.alu);
  const wallF = verticalSlab(wallFront, L.wallZ0 + backThk, tapDepth, mats.alu);

  // Ribs: triangles in the YZ plane, extruded along X.
  const ribs: THREE.Mesh[] = [];
  [-1, 1].forEach((side) => {
    const s = new THREE.Shape();
    s.moveTo(0, 0);
    s.lineTo(L.ribDepth, 0);
    s.lineTo(0, L.ribHeight);
    s.closePath();
    const rg = new THREE.ExtrudeGeometry(s, { depth: p.ribThk, bevelEnabled: false });
    // shape X → world Z (from wall front face), shape Y → world Y
    rg.rotateY(-Math.PI / 2);
    rg.translate(side > 0 ? p.length / 2 : -p.length / 2 + p.ribThk, p.baseThk, L.wallZ1);
    const m = new THREE.Mesh(rg, mats.alu);
    ribs.push(m);
  });

  const solid = [baseLower, baseMid, baseUpper, wallB, wallF, ...ribs].filter((m): m is THREE.Mesh => !!m);
  solid.forEach((m) => {
    m.castShadow = true;
    m.receiveShadow = true;
    g.add(m);
    geos.push(m.geometry);
  });
  addF('outline', baseLower);
  addF('base', baseUpper);
  addF('wall', wallB);
  ribs.forEach((r) => addF('ribs', r));

  // Feature overlays (hidden until highlighted).
  const overlay = (geo: THREE.BufferGeometry, f: FeatureInstance) => {
    const m = new THREE.Mesh(geo, mats.highlight);
    m.visible = false;
    m.renderOrder = 5;
    m.userData.overlay = f.ref;
    m.userData.featureId = f.id;
    g.add(m);
    geos.push(geo);
    addF(f.ref, m);
    return m;
  };
  if (pocket) {
    const ps = roundedRectPath(new THREE.Shape(), 0, zToShapeY(pocketCz), p.pocketLen - 0.2, p.pocketWidth - 0.2, Math.max(0.1, p.pocketR - 0.1)) as THREE.Shape;
    const pg = new THREE.ExtrudeGeometry(ps, { depth: p.pocketDepth - 0.1, bevelEnabled: false, curveSegments: 40 });
    pg.rotateX(-Math.PI / 2);
    pg.translate(0, p.baseThk - p.pocketDepth + 0.05, 0);
    overlay(pg, pocket);
  }
  bores.forEach((f) => {
    const cg = new THREE.CylinderGeometry(f.dia / 2 - 0.1, f.dia / 2 - 0.1, p.wallThk - 0.2, 48, 1, true);
    cg.rotateX(Math.PI / 2);
    cg.translate(f.pos[0], f.pos[1], L.wallZ0 + p.wallThk / 2);
    const m = overlay(cg, f);
    (m.material as THREE.Material).side = THREE.DoubleSide;
  });
  cbores.forEach((f) => {
    const [x, z] = f.pos;
    const cr = (f.cboreDia ?? p.cboreDia) / 2;
    const cd = f.cboreDepth ?? p.cboreDepth;
    const cg = new THREE.CylinderGeometry(cr - 0.1, cr - 0.1, cd, 32, 1, true);
    cg.translate(x, p.baseThk - cd / 2, z);
    overlay(cg, f);
    const hg = new THREE.CylinderGeometry(f.dia / 2 - 0.1, f.dia / 2 - 0.1, p.baseThk, 24, 1, true);
    hg.translate(x, p.baseThk / 2, z);
    overlay(hg, f);
  });
  thru.forEach((f) => {
    const hg = new THREE.CylinderGeometry(f.dia / 2 - 0.1, f.dia / 2 - 0.1, p.baseThk, 24, 1, true);
    hg.translate(f.pos[0], p.baseThk / 2, f.pos[1]);
    overlay(hg, f);
  });
  taps.forEach((f) => {
    const tg = new THREE.CylinderGeometry(f.dia / 2 - 0.05, f.dia / 2 - 0.05, tapDepth, 16, 1, true);
    tg.rotateX(Math.PI / 2);
    tg.translate(f.pos[0], f.pos[1], L.wallZ1 - tapDepth / 2);
    overlay(tg, f);
  });
  mats.highlight.side = THREE.DoubleSide;

  const edges: THREE.LineSegments[] = [];
  const lm = opts.edges ? new THREE.LineBasicMaterial({ color: opts.edgeColor ?? 0x0b0d10, transparent: true, opacity: 0.35 }) : null;
  if (lm) {
    solid.forEach((m) => {
      const eg = new THREE.EdgesGeometry(m.geometry, 30);
      const ls = new THREE.LineSegments(eg, lm);
      ls.userData.edge = true;
      g.add(ls);
      edges.push(ls);
      geos.push(eg);
    });
  }

  return {
    group: g,
    features,
    edges,
    dispose: () => {
      geos.forEach((x) => x.dispose());
      lm?.dispose(); // per-build edge material; the shared `mats` are owned by the caller
    },
  };
}

/** Shows the overlays of the given feature categories and/or feature instances. */
export function setHighlight(b: BracketBuild, refs: FeatureRef[], featureIds: string[] = []) {
  const on = new Set(refs);
  const ids = new Set(featureIds);
  b.group.traverse((o) => {
    if (o.userData.overlay) o.visible = on.has(o.userData.overlay as FeatureRef) || ids.has(o.userData.featureId as string);
  });
}

/** Involute spur gear profile. Returns a shape centred at the origin. */
export function gearShape(module: number, teeth: number, boreR: number) {
  const rp = (module * teeth) / 2;
  const ra = rp + module;
  const rf = rp - 1.25 * module;
  const rb = rp * Math.cos((20 * Math.PI) / 180);
  const inv = (a: number) => Math.tan(a) - a;
  const alphaP = Math.acos(rb / rp);
  const halfToothAngle = Math.PI / (2 * teeth) + inv(alphaP);
  const rootExtra = 0.4 * (Math.PI / teeth - halfToothAngle);
  const pts: THREE.Vector2[] = [];
  const steps = 7;
  for (let k = 0; k < teeth; k++) {
    const c = (k * 2 * Math.PI) / teeth;
    const flank = (sign: 1 | -1) => {
      const arr: THREE.Vector2[] = [];
      for (let i = 0; i <= steps; i++) {
        const r = Math.max(rb, rf) + ((ra - Math.max(rb, rf)) * i) / steps;
        const a = Math.acos(Math.min(1, rb / r));
        const th = c + sign * (halfToothAngle - inv(a));
        arr.push(new THREE.Vector2(r * Math.cos(th), r * Math.sin(th)));
      }
      return arr;
    };
    const right = flank(-1);
    const left = flank(1).reverse();
    const rootA = c - halfToothAngle - rootExtra;
    pts.push(new THREE.Vector2(rf * Math.cos(rootA), rf * Math.sin(rootA)));
    pts.push(...right, ...left);
    const rootB = c + halfToothAngle + rootExtra;
    pts.push(new THREE.Vector2(rf * Math.cos(rootB), rf * Math.sin(rootB)));
  }
  const s = new THREE.Shape(pts);
  s.holes.push(circleHole(0, 0, boreR));
  return { shape: s, rp, ra };
}

export function gearMesh(module: number, teeth: number, width: number, boreR: number, mat: THREE.Material) {
  const { shape, rp, ra } = gearShape(module, teeth, boreR);
  const geo = new THREE.ExtrudeGeometry(shape, { depth: width, bevelEnabled: true, bevelThickness: 0.35, bevelSize: 0.25, bevelSegments: 1, curveSegments: 24 });
  geo.translate(0, 0, -width / 2);
  const hub = new THREE.CylinderGeometry(rp * 0.42, rp * 0.42, width + 5, 40, 1, false);
  hub.rotateX(Math.PI / 2);
  const g = new THREE.Group();
  const m = new THREE.Mesh(geo, mat);
  const h = new THREE.Mesh(hub, mat);
  m.castShadow = h.castShadow = true;
  g.add(m, h);
  return { group: g, rp, ra, dispose: () => (geo.dispose(), hub.dispose()) };
}
