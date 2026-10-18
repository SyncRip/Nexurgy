// Generic part viewer for the kernel's mesh_json preview format
// (server/app/cad/meshing.py: mm, world +Z up, faces with local triangles,
// edges as polylines). Render-on-demand, orbit / pan / zoom, standard views,
// display modes, face / edge picking (triangles map back to face ids),
// point-to-point measuring and a bounding-box dimension overlay.
//
// GLB is only used for mesh references that have no mesh_json (glTF is
// metres, +Y up: converted to mm / +Z up on load). Picking uses mesh_json.

import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js';

export type Vec3 = [number, number, number];
export type ViewName = 'iso' | 'front' | 'back' | 'top' | 'bottom' | 'right' | 'left';
export type DisplayMode = 'shaded_edges' | 'shaded' | 'wireframe' | 'xray';
/** select: faces + edges; pick_face / pick_edge: one kind only; measure: points. */
export type ToolMode = 'select' | 'pick_face' | 'pick_edge' | 'measure';

export interface MeshFace {
  id: number | string;
  surface: string;
  normal?: number[];
  center: number[];
  area: number;
  positions: number[];
  indices: number[];
  radius?: number;
  axis?: { origin: number[]; direction: number[] };
}

export interface MeshEdge {
  id: number | string;
  kind: string;
  points: number[][];
  seam?: boolean;
}

export interface MeshDoc {
  format?: string;
  bbox: { min: number[]; max: number[]; size: number[] } | Record<string, unknown>;
  faces: MeshFace[];
  edges: MeshEdge[];
}

export interface FacePick {
  kind: 'face';
  index: number;
  id: number | string;
  surface: string;
  /** Planar faces: the face normal. Otherwise the triangle normal at the hit. */
  normal: Vec3;
  planar: boolean;
  center: Vec3;
  area: number;
  radius?: number;
  point: Vec3;
}

export interface EdgePick {
  kind: 'edge';
  index: number;
  id: number | string;
  edgeKind: string;
  length: number;
  start: Vec3;
  end: Vec3;
  /** Closest point on the edge to the cursor ray. */
  point: Vec3;
}

export interface PointPick {
  kind: 'point';
  point: Vec3;
}

export type Pick = FacePick | EdgePick | PointPick;

export interface MeasureState {
  points: Vec3[];
  distance: number | null;
  delta: Vec3 | null;
}

export interface ViewerCallbacks {
  onHover?: (pick: Pick | null) => void;
  onPick?: (pick: Pick | null, ev: { shift: boolean }) => void;
  onMeasure?: (m: MeasureState) => void;
}

interface Palette {
  body: number;
  edge: number;
  hover: number;
  select: number;
  candidate: number;
  measure: number;
  grid1: number;
  grid2: number;
  marker: number;
}

const PALETTES: Record<'dark' | 'light', Palette> = {
  dark: { body: 0xb9c2cc, edge: 0x10151b, hover: 0x42b8ff, select: 0x829bff, candidate: 0xf5b93b, measure: 0x42b8ff, grid1: 0x3a4450, grid2: 0x232a33, marker: 0x829bff },
  light: { body: 0xc9d0d8, edge: 0x1b222b, hover: 0x0a73b4, select: 0x3158d9, candidate: 0xd98a00, measure: 0x0a73b4, grid1: 0x98a2ae, grid2: 0xc9cfd6, marker: 0x3158d9 },
};

const VIEW_DIRS: Record<ViewName, Vec3> = {
  iso: [1, -1, 0.85],
  front: [0, -1, 0],
  back: [0, 1, 0],
  right: [1, 0, 0],
  left: [-1, 0, 0],
  top: [0, -1e-4, 1],
  bottom: [0, -1e-4, -1],
};

function bboxOf(doc: MeshDoc): THREE.Box3 | null {
  const b = doc.bbox as { min?: number[]; max?: number[] };
  if (Array.isArray(b?.min) && Array.isArray(b?.max) && b.min.length === 3) {
    return new THREE.Box3(new THREE.Vector3(...(b.min as Vec3)), new THREE.Vector3(...(b.max as Vec3)));
  }
  return null;
}

const v3 = (v: THREE.Vector3): Vec3 => [v.x + 0, v.y + 0, v.z + 0];

export class MeshViewer {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100000);
  private controls: OrbitControls;
  private light = new THREE.DirectionalLight(0xffffff, 1.6);
  private palette: Palette;
  private root = new THREE.Group();
  private overlay = new THREE.Group();
  private grid: THREE.GridHelper | null = null;
  private axes: THREE.AxesHelper;
  private bodyMat: THREE.MeshStandardMaterial;
  private edgeMat: THREE.LineBasicMaterial;
  private hoverMat: THREE.MeshBasicMaterial;
  private selectMat: THREE.MeshBasicMaterial;
  // Fat (screen-space width) lines for highlights; plain GL lines are 1px.
  private hoverEdgeMat: LineMaterial;
  private selectEdgeMat: LineMaterial;
  private candidateMat: LineMaterial;
  private measureLineMat: LineMaterial;
  private mesh: THREE.Mesh | null = null;
  private edges: THREE.LineSegments | null = null;
  private gltf: THREE.Object3D | null = null;
  private doc: MeshDoc | null = null;
  private triFace = new Uint32Array(0);
  private segEdge = new Uint32Array(0);
  private box: THREE.Box3 | null = null;
  private hoverObj: THREE.Object3D | null = null;
  private selectObj: THREE.Object3D | null = null;
  private candidateObj: THREE.Object3D | null = null;
  private markerObj: THREE.Object3D | null = null;
  private measureObj = new THREE.Group();
  private bboxObj: THREE.Object3D | null = null;
  private labels: HTMLDivElement;
  private labelItems: { el: HTMLDivElement; pos: THREE.Vector3 }[] = [];
  private axisLabels: HTMLDivElement[] = [];
  private raycaster = new THREE.Raycaster();
  private pointer = new THREE.Vector2();
  private mode: ToolMode = 'select';
  private display: DisplayMode = 'shaded_edges';
  private showBBox = false;
  private gridVisible = true;
  private measurePts: Vec3[] = [];
  private frame = 0;
  private hoverFrame = 0;
  private lastHoverKey = '';
  private downAt: { x: number; y: number } | null = null;
  private ro: ResizeObserver;
  private disposed = false;
  private cb: ViewerCallbacks = {};
  private hasFitted = false;

  constructor(
    private host: HTMLElement,
    opts: { theme: 'dark' | 'light' },
  ) {
    this.palette = PALETTES[opts.theme];
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.domElement.className = 'vw-canvas';
    host.appendChild(this.renderer.domElement);
    this.labels = document.createElement('div');
    this.labels.className = 'vw-labels';
    host.appendChild(this.labels);

    this.camera.up.set(0, 0, 1);
    this.camera.position.set(200, -200, 160);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a929c, 1.9));
    this.scene.add(this.camera);
    // Headlight that follows the camera (+ its target) so the side you look at is lit.
    this.light.position.set(-0.35, 0.6, 0);
    this.light.target.position.set(0, 0, -1);
    this.camera.add(this.light, this.light.target);
    const fill = new THREE.DirectionalLight(0xffffff, 0.6);
    fill.position.set(-1, 1, 3);
    this.scene.add(fill);
    this.scene.add(this.root, this.overlay);
    this.overlay.add(this.measureObj);
    this.axes = new THREE.AxesHelper(1);
    this.axes.visible = false;
    this.scene.add(this.axes);

    const p = this.palette;
    this.bodyMat = new THREE.MeshStandardMaterial({ color: p.body, metalness: 0, roughness: 0.62, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    this.edgeMat = new THREE.LineBasicMaterial({ color: p.edge });
    this.hoverMat = new THREE.MeshBasicMaterial({ color: p.hover, transparent: true, opacity: 0.38, side: THREE.DoubleSide, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2 });
    this.selectMat = new THREE.MeshBasicMaterial({ color: p.select, transparent: true, opacity: 0.5, side: THREE.DoubleSide, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -3 });
    this.hoverEdgeMat = new LineMaterial({ color: p.hover, linewidth: 4, depthTest: false, transparent: true });
    this.selectEdgeMat = new LineMaterial({ color: p.select, linewidth: 4, depthTest: false, transparent: true });
    this.candidateMat = new LineMaterial({ color: p.candidate, linewidth: 3.5, depthTest: false, transparent: true, opacity: 0.95 });
    this.measureLineMat = new LineMaterial({ color: p.measure, linewidth: 2.5, depthTest: false, transparent: true });

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.14;
    this.controls.screenSpacePanning = true;
    this.controls.zoomToCursor = true;
    this.controls.addEventListener('change', () => this.requestRender());

    const el = this.renderer.domElement;
    el.addEventListener('pointermove', this.onPointerMove);
    el.addEventListener('pointerdown', this.onPointerDown);
    el.addEventListener('pointerup', this.onPointerUp);
    el.addEventListener('pointerleave', this.onPointerLeave);

    for (const name of ['X', 'Y', 'Z']) {
      const d = document.createElement('div');
      d.className = `vw-axis vw-axis-${name.toLowerCase()}`;
      d.textContent = name;
      this.labels.appendChild(d);
      this.axisLabels.push(d);
    }

    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this.resize();
    this.setView('iso');
  }

  setCallbacks(cb: ViewerCallbacks) {
    this.cb = cb;
  }

  setTheme(theme: 'dark' | 'light') {
    const p = (this.palette = PALETTES[theme]);
    this.bodyMat.color.setHex(p.body);
    this.edgeMat.color.setHex(p.edge);
    this.hoverMat.color.setHex(p.hover);
    this.selectMat.color.setHex(p.select);
    this.hoverEdgeMat.color.setHex(p.hover);
    this.selectEdgeMat.color.setHex(p.select);
    this.candidateMat.color.setHex(p.candidate);
    this.measureLineMat.color.setHex(p.measure);
    this.rebuildGrid();
    this.rebuildMeasure();
    this.rebuildBBox();
    this.requestRender();
  }

  // ─── model ───────────────────────────────────────────────────────────────

  /** Show a mesh_json document (null clears). Fits the view on the first model. */
  setMesh(doc: MeshDoc | null, opts: { fit?: boolean } = {}) {
    this.clearModel();
    this.doc = doc;
    if (doc && doc.faces.length) {
      // Plain triangle meshes (STL / OBJ references, surface "mesh") are expanded
      // to unshared triangles so they shade flat; B-rep faces keep shared
      // vertices and smooth normals within the face (never across faces).
      const flat = (f: MeshFace) => f.surface === 'mesh';
      let nVerts = 0;
      let nTris = 0;
      for (const f of doc.faces) {
        nVerts += flat(f) ? f.indices.length : f.positions.length / 3;
        nTris += f.indices.length / 3;
      }
      const pos = new Float32Array(nVerts * 3);
      const idx = nVerts > 65535 ? new Uint32Array(nTris * 3) : new Uint16Array(nTris * 3);
      const triFace = new Uint32Array(nTris);
      const normals = new Float32Array(nVerts * 3);
      let vo = 0;
      let to = 0;
      doc.faces.forEach((f, fi) => {
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(f.positions), 3));
        g.setIndex(f.indices);
        const src = flat(f) ? g.toNonIndexed() : g;
        src.computeVertexNormals();
        const p = src.getAttribute('position').array as Float32Array;
        const n = p.length / 3;
        pos.set(p, vo * 3);
        normals.set(src.getAttribute('normal').array as Float32Array, vo * 3);
        if (flat(f)) for (let i = 0; i < n; i++) idx[to * 3 + i] = vo + i;
        else for (let i = 0; i < f.indices.length; i++) idx[to * 3 + i] = f.indices[i] + vo;
        if (src !== g) src.dispose();
        g.dispose();
        triFace.fill(fi, to, to + f.indices.length / 3);
        vo += n;
        to += f.indices.length / 3;
      });
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      geom.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
      geom.setIndex(new THREE.BufferAttribute(idx, 1));
      geom.computeBoundingSphere();
      geom.computeBoundingBox();
      this.mesh = new THREE.Mesh(geom, this.bodyMat);
      this.root.add(this.mesh);
      this.triFace = triFace;
    }
    if (doc && doc.edges.length) {
      const segs: number[] = [];
      const segEdge: number[] = [];
      doc.edges.forEach((e, ei) => {
        if (e.seam || e.points.length < 2) return;
        for (let i = 0; i + 1 < e.points.length; i++) {
          segs.push(...e.points[i], ...e.points[i + 1]);
          segEdge.push(ei);
        }
      });
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segs), 3));
      g.computeBoundingSphere();
      this.edges = new THREE.LineSegments(g, this.edgeMat);
      this.root.add(this.edges);
      this.segEdge = Uint32Array.from(segEdge);
    }
    this.box = doc ? (bboxOf(doc) ?? (this.mesh ? this.mesh.geometry.boundingBox!.clone() : null)) : null;
    this.applyDisplay();
    this.rebuildGrid();
    this.rebuildBBox();
    if (this.box && (opts.fit || !this.hasFitted)) {
      this.fit();
      this.hasFitted = true;
    }
    this.requestRender();
  }

  /** Mesh references stored only as GLB (metres, +Y up). Display + point measuring only. */
  async setGlb(buffer: ArrayBuffer, opts: { fit?: boolean } = {}) {
    const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
    const loader = new GLTFLoader();
    const gltf = await new Promise<{ scene: THREE.Object3D }>((resolve, reject) => loader.parse(buffer, '', resolve as never, reject));
    if (this.disposed) return;
    this.clearModel();
    const wrap = new THREE.Group();
    wrap.rotation.x = Math.PI / 2; // +Y up -> +Z up
    wrap.scale.setScalar(1000); // m -> mm
    wrap.add(gltf.scene);
    gltf.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.isMesh) m.material = this.bodyMat;
    });
    this.root.add(wrap);
    this.gltf = wrap;
    wrap.updateMatrixWorld(true);
    this.box = new THREE.Box3().setFromObject(wrap);
    this.applyDisplay();
    this.rebuildGrid();
    this.rebuildBBox();
    if (opts.fit || !this.hasFitted) {
      this.fit();
      this.hasFitted = true;
    }
    this.requestRender();
  }

  private clearModel() {
    for (const o of [this.mesh, this.edges]) {
      if (o) {
        this.root.remove(o);
        o.geometry.dispose();
      }
    }
    if (this.gltf) {
      this.root.remove(this.gltf);
      this.gltf.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
    }
    this.mesh = null;
    this.edges = null;
    this.gltf = null;
    this.doc = null;
    this.triFace = new Uint32Array(0);
    this.segEdge = new Uint32Array(0);
    this.setOverlay('hoverObj', null);
    this.setOverlay('selectObj', null);
    this.setOverlay('candidateObj', null);
    this.lastHoverKey = '';
  }

  // ─── display ─────────────────────────────────────────────────────────────

  setDisplay(mode: DisplayMode) {
    this.display = mode;
    this.applyDisplay();
    this.requestRender();
  }

  private applyDisplay() {
    const d = this.display;
    this.bodyMat.wireframe = d === 'wireframe';
    this.bodyMat.transparent = d === 'xray';
    this.bodyMat.opacity = d === 'xray' ? 0.28 : 1;
    this.bodyMat.depthWrite = d !== 'xray';
    this.bodyMat.needsUpdate = true;
    if (this.edges) this.edges.visible = d !== 'shaded' && d !== 'wireframe';
  }

  setBBoxVisible(v: boolean) {
    this.showBBox = v;
    this.rebuildBBox();
    this.requestRender();
  }

  setGridVisible(v: boolean) {
    this.gridVisible = v;
    if (this.grid) this.grid.visible = v;
    this.axes.visible = v && !!this.box;
    this.requestRender();
  }

  private rebuildGrid() {
    if (this.grid) {
      this.scene.remove(this.grid);
      this.grid.geometry.dispose();
      (this.grid.material as THREE.Material).dispose();
      this.grid = null;
    }
    const b = this.box;
    const span = b ? Math.max(b.max.x - b.min.x, b.max.y - b.min.y, 10) : 100;
    const step = niceStep(span / 6);
    const size = step * 20;
    const grid = new THREE.GridHelper(size, 20, this.palette.grid1, this.palette.grid2);
    grid.rotation.x = Math.PI / 2;
    const z = b ? b.min.z : 0;
    grid.position.set(0, 0, z - 0.01);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.7;
    grid.visible = this.gridVisible;
    this.grid = grid;
    this.scene.add(grid);
    this.axes.scale.setScalar(step * 1.5);
    // Triad just outside the part's lower-left-front corner.
    this.axes.position.set(b ? b.min.x - step : 0, b ? b.min.y - step : 0, z);
    this.axes.visible = !!b && this.gridVisible;
  }

  // ─── camera ──────────────────────────────────────────────────────────────

  setView(name: ViewName) {
    const target = this.box ? this.box.getCenter(new THREE.Vector3()) : new THREE.Vector3();
    const dir = new THREE.Vector3(...VIEW_DIRS[name]).normalize();
    const dist = this.fitDistance();
    this.controls.target.copy(target);
    this.camera.position.copy(target).addScaledVector(dir, dist);
    this.camera.lookAt(target);
    this.controls.update();
    this.requestRender();
  }

  fit() {
    const target = this.box ? this.box.getCenter(new THREE.Vector3()) : new THREE.Vector3();
    const dir = this.camera.position.clone().sub(this.controls.target);
    if (dir.lengthSq() < 1e-9) dir.set(...VIEW_DIRS.iso);
    dir.normalize();
    this.controls.target.copy(target);
    this.camera.position.copy(target).addScaledVector(dir, this.fitDistance());
    this.controls.update();
    this.requestRender();
  }

  private radius(): number {
    if (!this.box) return 60;
    return Math.max(this.box.getSize(new THREE.Vector3()).length() / 2, 1);
  }

  private fitDistance(): number {
    const r = this.radius();
    const fov = (this.camera.fov * Math.PI) / 180;
    const aspect = Math.max(this.camera.aspect, 0.3);
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * aspect));
    const d = (r / Math.sin(fit / 2)) * 1.08;
    this.camera.near = Math.max(d / 1000, 0.01);
    this.camera.far = d * 50;
    this.camera.updateProjectionMatrix();
    this.controls.minDistance = r * 0.05;
    this.controls.maxDistance = d * 12;
    return d;
  }

  // ─── tools, picking ──────────────────────────────────────────────────────

  setMode(mode: ToolMode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.setOverlay('hoverObj', null);
    this.lastHoverKey = '';
    this.renderer.domElement.style.cursor = mode === 'select' ? '' : 'crosshair';
    if (mode !== 'measure') this.clearMeasure();
    this.requestRender();
  }

  clearMeasure() {
    if (!this.measurePts.length) return;
    this.measurePts = [];
    this.rebuildMeasure();
    this.emitMeasure();
    this.requestRender();
  }

  private emitMeasure() {
    const pts = this.measurePts;
    if (pts.length === 2) {
      const d: Vec3 = [pts[1][0] - pts[0][0], pts[1][1] - pts[0][1], pts[1][2] - pts[0][2]];
      this.cb.onMeasure?.({ points: pts, distance: Math.hypot(...d), delta: d });
    } else {
      this.cb.onMeasure?.({ points: pts, distance: null, delta: null });
    }
  }

  private worldPerPixel(at: THREE.Vector3): number {
    const dist = this.camera.position.distanceTo(at);
    const h = this.renderer.domElement.clientHeight || 1;
    return (2 * dist * Math.tan((this.camera.fov * Math.PI) / 360)) / h;
  }

  private setPointer(ev: PointerEvent) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  }

  /** Pick under the current pointer for the active mode. */
  private pick(): Pick | null {
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const wantFace = this.mode !== 'pick_edge';
    const wantEdge = this.mode !== 'pick_face';
    let faceHit: THREE.Intersection | null = null;
    if (this.mesh) faceHit = this.raycaster.intersectObject(this.mesh, false)[0] ?? null;
    if (!faceHit && this.gltf) {
      const hit = this.raycaster.intersectObject(this.gltf, true)[0];
      return hit ? { kind: 'point', point: v3(hit.point) } : null;
    }
    let edgeHit: THREE.Intersection | null = null;
    if (wantEdge && this.edges) {
      const ref = faceHit ? faceHit.point : (this.box?.getCenter(new THREE.Vector3()) ?? new THREE.Vector3());
      this.raycaster.params.Line = { threshold: this.worldPerPixel(ref) * 6 };
      const hits = this.raycaster.intersectObject(this.edges, false);
      const slack = this.worldPerPixel(ref) * 8;
      edgeHit = hits.find((h) => !faceHit || h.distance <= faceHit.distance + slack) ?? null;
    }
    if (edgeHit && edgeHit.index !== undefined) {
      const ei = this.segEdge[Math.floor(edgeHit.index / 2)];
      const e = this.doc!.edges[ei];
      const pts = e.points;
      let length = 0;
      for (let i = 0; i + 1 < pts.length; i++) length += Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1], pts[i + 1][2] - pts[i][2]);
      const point = edgeHit.pointOnLine ? v3(edgeHit.pointOnLine) : v3(edgeHit.point);
      const pick: EdgePick = { kind: 'edge', index: ei, id: e.id, edgeKind: e.kind, length, start: pts[0] as Vec3, end: pts[pts.length - 1] as Vec3, point };
      if (this.mode === 'measure') {
        // Snap to an edge end when the cursor is close to it.
        const snap = this.worldPerPixel(edgeHit.point) * 10;
        for (const end of [pick.start, pick.end]) {
          if (Math.hypot(end[0] - point[0], end[1] - point[1], end[2] - point[2]) <= snap) return { kind: 'point', point: [...end] as Vec3 };
        }
        return { kind: 'point', point };
      }
      return pick;
    }
    if (faceHit && wantFace && faceHit.faceIndex !== undefined && faceHit.faceIndex !== null) {
      const fi = this.triFace[faceHit.faceIndex];
      const f = this.doc!.faces[fi];
      const point = v3(faceHit.point);
      if (this.mode === 'measure') return { kind: 'point', point };
      const planar = f.surface === 'plane' && Array.isArray(f.normal);
      const triN = faceHit.face ? v3(faceHit.face.normal.clone().normalize()) : ([0, 0, 1] as Vec3);
      return { kind: 'face', index: fi, id: f.id, surface: f.surface, normal: planar ? (f.normal as Vec3) : triN, planar, center: f.center as Vec3, area: f.area, radius: f.radius, point };
    }
    return null;
  }

  private onPointerMove = (ev: PointerEvent) => {
    this.setPointer(ev);
    if (ev.buttons) return; // orbiting
    if (this.hoverFrame) return;
    this.hoverFrame = requestAnimationFrame(() => {
      this.hoverFrame = 0;
      if (this.disposed) return;
      const p = this.pick();
      const key = p ? `${p.kind}:${p.kind === 'face' ? p.index : p.kind === 'edge' ? p.index : p.point.join(',')}` : '';
      if (key === this.lastHoverKey && p?.kind !== 'point') return;
      this.lastHoverKey = key;
      this.showHover(p);
      this.cb.onHover?.(p);
    });
  };

  private onPointerDown = (ev: PointerEvent) => {
    this.downAt = { x: ev.clientX, y: ev.clientY };
  };

  private onPointerUp = (ev: PointerEvent) => {
    const d = this.downAt;
    this.downAt = null;
    if (!d || ev.button !== 0 || Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > 4) return;
    this.setPointer(ev);
    const p = this.pick();
    if (this.mode === 'measure') {
      if (!p) return;
      if (this.measurePts.length >= 2) this.measurePts = [];
      this.measurePts.push(p.point);
      this.rebuildMeasure();
      this.emitMeasure();
      this.requestRender();
      return;
    }
    this.cb.onPick?.(p, { shift: ev.shiftKey });
  };

  private onPointerLeave = () => {
    if (this.lastHoverKey) {
      this.lastHoverKey = '';
      this.showHover(null);
      this.cb.onHover?.(null);
    }
  };

  // ─── overlays ────────────────────────────────────────────────────────────

  private setOverlay(slot: 'hoverObj' | 'selectObj' | 'candidateObj' | 'markerObj', obj: THREE.Object3D | null) {
    const prev = this[slot];
    if (prev) {
      this.overlay.remove(prev);
      prev.traverse((o) => {
        const g = (o as THREE.Mesh).geometry;
        if (g) g.dispose();
      });
    }
    this[slot] = obj;
    if (obj) this.overlay.add(obj);
  }

  private faceMesh(index: number, mat: THREE.Material): THREE.Object3D | null {
    const f = this.doc?.faces[index];
    if (!f) return null;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(f.positions), 3));
    g.setIndex(f.indices);
    const m = new THREE.Mesh(g, mat);
    m.renderOrder = 2;
    return m;
  }

  private edgeLines(indices: number[], mat: LineMaterial): THREE.Object3D | null {
    if (!this.doc) return null;
    const segs: number[] = [];
    for (const i of indices) {
      const pts = this.doc.edges[i]?.points ?? [];
      for (let k = 0; k + 1 < pts.length; k++) segs.push(...pts[k], ...pts[k + 1]);
    }
    if (!segs.length) return null;
    const g = new LineSegmentsGeometry();
    g.setPositions(segs);
    const l = new LineSegments2(g, mat);
    l.renderOrder = 4;
    return l;
  }

  private dot(at: Vec3, color: number, sizePx = 5): THREE.Object3D {
    const r = this.worldPerPixel(new THREE.Vector3(...at)) * sizePx;
    const m = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 8), new THREE.MeshBasicMaterial({ color, depthTest: false }));
    m.position.set(...at);
    m.renderOrder = 6;
    return m;
  }

  private showHover(p: Pick | null) {
    let obj: THREE.Object3D | null = null;
    if (p?.kind === 'face') obj = this.faceMesh(p.index, this.hoverMat);
    else if (p?.kind === 'edge') obj = this.edgeLines([p.index], this.hoverEdgeMat);
    else if (p?.kind === 'point' && this.mode === 'measure') obj = this.dot(p.point, this.palette.measure, 4);
    this.setOverlay('hoverObj', obj);
    this.requestRender();
  }

  /** Persistent selection highlight (faces / edges by mesh index). */
  setSelection(sel: { faces?: number[]; edges?: number[] } | null) {
    const group = new THREE.Group();
    for (const fi of sel?.faces ?? []) {
      const m = this.faceMesh(fi, this.selectMat);
      if (m) group.add(m);
    }
    if (sel?.edges?.length) {
      const l = this.edgeLines(sel.edges, this.selectEdgeMat);
      if (l) group.add(l);
    }
    this.setOverlay('selectObj', group.children.length ? group : null);
    this.requestRender();
  }

  /** Edges an edge selector would pick (preview colour). */
  setCandidates(edgeIndices: number[]) {
    this.setOverlay('candidateObj', edgeIndices.length ? this.edgeLines(edgeIndices, this.candidateMat) : null);
    this.requestRender();
  }

  /** Small markers (hole entries, `near` points) with an optional direction stub. */
  setMarkers(points: Vec3[], direction?: Vec3 | null) {
    if (!points.length) {
      this.setOverlay('markerObj', null);
      this.requestRender();
      return;
    }
    const g = new THREE.Group();
    const r = this.radius();
    for (const p of points.slice(0, 400)) {
      g.add(this.dot(p, this.palette.marker, 4));
      if (direction) {
        const dir = new THREE.Vector3(...direction).normalize();
        const arrow = new THREE.ArrowHelper(dir, new THREE.Vector3(...p), r * 0.18, this.palette.marker, r * 0.05, r * 0.03);
        arrow.traverse((o) => {
          const mm = (o as THREE.Mesh).material as THREE.Material | undefined;
          if (mm) mm.depthTest = false;
          o.renderOrder = 6;
        });
        g.add(arrow);
      }
    }
    this.setOverlay('markerObj', g);
    this.requestRender();
  }

  private rebuildMeasure() {
    for (const c of [...this.measureObj.children]) {
      this.measureObj.remove(c);
      c.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
    }
    this.labelItems = this.labelItems.filter((l) => {
      if (l.el.dataset.kind === 'measure') {
        l.el.remove();
        return false;
      }
      return true;
    });
    const pts = this.measurePts;
    for (const p of pts) this.measureObj.add(this.dot(p, this.palette.measure, 5));
    if (pts.length === 2) {
      const g = new LineSegmentsGeometry();
      g.setPositions([...pts[0], ...pts[1]]);
      const line = new LineSegments2(g, this.measureLineMat);
      line.renderOrder = 7;
      this.measureObj.add(line);
      const d = Math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1], pts[1][2] - pts[0][2]);
      const mid = new THREE.Vector3((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2, (pts[0][2] + pts[1][2]) / 2);
      this.addLabel(`${fmtMm(d)} mm`, mid, 'measure');
    }
  }

  private rebuildBBox() {
    if (this.bboxObj) {
      this.overlay.remove(this.bboxObj);
      this.bboxObj.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
      this.bboxObj = null;
    }
    this.labelItems = this.labelItems.filter((l) => {
      if (l.el.dataset.kind === 'bbox') {
        l.el.remove();
        return false;
      }
      return true;
    });
    if (!this.showBBox || !this.box) return;
    const b = this.box;
    const helper = new THREE.Box3Helper(b, new THREE.Color(this.palette.measure));
    (helper.material as THREE.LineBasicMaterial).transparent = true;
    (helper.material as THREE.LineBasicMaterial).opacity = 0.6;
    this.bboxObj = helper;
    this.overlay.add(helper);
    const s = b.getSize(new THREE.Vector3());
    this.addLabel(`X ${fmtMm(s.x)}`, new THREE.Vector3((b.min.x + b.max.x) / 2, b.min.y, b.min.z), 'bbox');
    this.addLabel(`Y ${fmtMm(s.y)}`, new THREE.Vector3(b.max.x, (b.min.y + b.max.y) / 2, b.min.z), 'bbox');
    this.addLabel(`Z ${fmtMm(s.z)}`, new THREE.Vector3(b.max.x, b.min.y, (b.min.z + b.max.z) / 2), 'bbox');
  }

  private addLabel(text: string, pos: THREE.Vector3, kind: string) {
    const el = document.createElement('div');
    el.className = `vw-label vw-label-${kind}`;
    el.dataset.kind = kind;
    el.textContent = text;
    this.labels.appendChild(el);
    this.labelItems.push({ el, pos });
  }

  private updateLabels() {
    const w = this.renderer.domElement.clientWidth;
    const h = this.renderer.domElement.clientHeight;
    const v = new THREE.Vector3();
    for (const { el, pos } of this.labelItems) {
      v.copy(pos).project(this.camera);
      const hidden = v.z > 1 || v.z < -1;
      el.style.display = hidden ? 'none' : '';
      el.style.transform = `translate(${((v.x + 1) / 2) * w}px, ${((1 - v.y) / 2) * h}px) translate(-50%, -50%)`;
    }
    // Axis labels at the triad tips.
    const tips: Vec3[] = [
      [1, 0, 0],
      [0, 1, 0],
      [0, 0, 1],
    ];
    this.axisLabels.forEach((el, i) => {
      if (!this.axes.visible) {
        el.style.display = 'none';
        return;
      }
      v.set(...tips[i]).multiplyScalar(this.axes.scale.x * 1.12).add(this.axes.position).project(this.camera);
      el.style.display = v.z > 1 ? 'none' : '';
      el.style.transform = `translate(${((v.x + 1) / 2) * w}px, ${((1 - v.y) / 2) * h}px) translate(-50%, -50%)`;
    });
  }

  // ─── render loop ─────────────────────────────────────────────────────────

  requestRender() {
    if (this.frame || this.disposed) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (this.disposed) return;
      const moving = this.controls.update();
      this.renderer.render(this.scene, this.camera);
      this.updateLabels();
      if (moving) this.requestRender();
    });
  }

  private resize() {
    const w = Math.max(this.host.clientWidth, 1);
    const h = Math.max(this.host.clientHeight, 1);
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    for (const m of [this.hoverEdgeMat, this.selectEdgeMat, this.candidateMat, this.measureLineMat]) m.resolution.set(w, h);
    this.requestRender();
  }

  /** PNG snapshot of the current view. */
  snapshot(): string {
    this.renderer.render(this.scene, this.camera);
    return this.renderer.domElement.toDataURL('image/png');
  }

  dispose() {
    this.disposed = true;
    if (this.frame) cancelAnimationFrame(this.frame);
    if (this.hoverFrame) cancelAnimationFrame(this.hoverFrame);
    this.ro.disconnect();
    const el = this.renderer.domElement;
    el.removeEventListener('pointermove', this.onPointerMove);
    el.removeEventListener('pointerdown', this.onPointerDown);
    el.removeEventListener('pointerup', this.onPointerUp);
    el.removeEventListener('pointerleave', this.onPointerLeave);
    this.clearModel();
    this.setOverlay('markerObj', null);
    this.measurePts = [];
    this.rebuildMeasure();
    this.controls.dispose();
    for (const m of [this.bodyMat, this.edgeMat, this.hoverMat, this.selectMat, this.hoverEdgeMat, this.selectEdgeMat, this.candidateMat, this.measureLineMat]) m.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    el.remove();
    this.labels.remove();
  }
}

function niceStep(v: number): number {
  const p = 10 ** Math.floor(Math.log10(Math.max(v, 1e-6)));
  const m = v / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}

export function fmtMm(v: number): string {
  const a = Math.abs(v);
  return a >= 10000 ? v.toFixed(0) : a >= 1000 ? v.toFixed(1) : v.toFixed(2);
}
