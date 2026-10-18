// Assembly viewport: one mesh per unique part definition (its mesh_json: mm, +Z up), instanced per leaf
// occurrence with the leaf's world transform (never merged into one mesh). Click picks a leaf; selection,
// hide and isolate work on path prefixes, so a sub-assembly node acts on all its leaves.

import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { Matrix4 } from '../../api/engineering';
import type { MeshDoc } from '../../three/viewer';
import { underPath } from './model';

export interface ViewerOccurrence {
  key: string; // path key
  partKey: string;
  world: Matrix4;
}

type Theme = 'dark' | 'light';

const PALETTE: Record<Theme, { edge: number; select: number; colors: number[] }> = {
  light: { edge: 0x3b4450, select: 0xe0671f, colors: [0xa9b4c2, 0x9fb7a5, 0xc2b19a, 0xa6a9c9, 0xb9a3b4, 0x9cbcc4, 0xc6c0a0] },
  dark: { edge: 0x10141a, select: 0xff8a3d, colors: [0x7f8b9a, 0x78917f, 0x9a8a72, 0x7f82a2, 0x92798d, 0x739399, 0x9c9676] },
};

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function geometryOf(doc: MeshDoc): { faces: THREE.BufferGeometry; edges: THREE.BufferGeometry | null } {
  let nVerts = 0;
  let nIdx = 0;
  for (const f of doc.faces) {
    nVerts += f.positions.length / 3;
    nIdx += f.indices.length;
  }
  const pos = new Float32Array(nVerts * 3);
  const idx = new Uint32Array(nIdx);
  let vo = 0;
  let io = 0;
  for (const f of doc.faces) {
    pos.set(f.positions, vo * 3);
    for (let i = 0; i < f.indices.length; i++) idx[io + i] = f.indices[i] + vo;
    vo += f.positions.length / 3;
    io += f.indices.length;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setIndex(new THREE.BufferAttribute(idx, 1));
  g.computeVertexNormals();
  g.computeBoundingBox();
  g.computeBoundingSphere();
  let edges: THREE.BufferGeometry | null = null;
  const segs: number[] = [];
  for (const e of doc.edges ?? []) {
    if (e.seam || e.points.length < 2) continue;
    for (let i = 0; i + 1 < e.points.length; i++) segs.push(...e.points[i], ...e.points[i + 1]);
  }
  if (segs.length) {
    edges = new THREE.BufferGeometry();
    edges.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segs), 3));
  }
  return { faces: g, edges };
}

function toMatrix(m: Matrix4): THREE.Matrix4 {
  const out = new THREE.Matrix4();
  out.set(m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3], m[2][0], m[2][1], m[2][2], m[2][3], 0, 0, 0, 1);
  return out;
}

export class AssemblyViewer {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(30, 1, 0.1, 1e6);
  private controls: OrbitControls;
  private root = new THREE.Group();
  private grid: THREE.GridHelper | null = null;
  private gridVisible = true;
  private theme: Theme;
  private geoms = new Map<string, { faces: THREE.BufferGeometry; edges: THREE.BufferGeometry | null }>();
  private mats = new Map<string, THREE.MeshStandardMaterial>();
  private selectMat: THREE.MeshStandardMaterial;
  private ghostMat: THREE.MeshStandardMaterial;
  private edgeMat: THREE.LineBasicMaterial;
  private meshes: THREE.Mesh[] = [];
  private selected: string | null = null;
  private hidden = new Set<string>();
  private isolated: string | null = null;
  private ro: ResizeObserver;
  private frame = 0;
  private disposed = false;
  private down: { x: number; y: number } | null = null;
  private raycaster = new THREE.Raycaster();
  onPick: (key: string | null) => void = () => {};

  constructor(private host: HTMLElement, opts: { theme: Theme }) {
    this.theme = opts.theme;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.domElement.className = 'asm-canvas';
    host.appendChild(this.renderer.domElement);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(300, -300, 240);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a929c, 1.8));
    const key = new THREE.DirectionalLight(0xffffff, 1.2);
    key.position.set(-0.4, 0.7, 0.2);
    this.camera.add(key);
    this.scene.add(this.camera, this.root);
    const p = PALETTE[this.theme];
    this.selectMat = new THREE.MeshStandardMaterial({ color: p.select, roughness: 0.5, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    this.ghostMat = new THREE.MeshStandardMaterial({ color: p.colors[0], transparent: true, opacity: 0.12, depthWrite: false, side: THREE.DoubleSide });
    this.edgeMat = new THREE.LineBasicMaterial({ color: p.edge });
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.requestRender());
    const el = this.renderer.domElement;
    el.addEventListener('pointerdown', this.onDown);
    el.addEventListener('pointerup', this.onUp);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this.resize();
  }

  setTheme(theme: Theme) {
    this.theme = theme;
    const p = PALETTE[theme];
    this.selectMat.color.setHex(p.select);
    this.edgeMat.color.setHex(p.edge);
    for (const [key, m] of this.mats) m.color.setHex(p.colors[hash(key) % p.colors.length]);
    this.requestRender();
  }

  private material(partKey: string): THREE.MeshStandardMaterial {
    let m = this.mats.get(partKey);
    if (!m) {
      const p = PALETTE[this.theme];
      m = new THREE.MeshStandardMaterial({ color: p.colors[hash(partKey) % p.colors.length], roughness: 0.6, metalness: 0, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      this.mats.set(partKey, m);
    }
    return m;
  }

  /** Replace the displayed occurrences. `docs` holds each definition's mesh_json (missing ones are skipped). */
  setOccurrences(docs: ReadonlyMap<string, MeshDoc>, occurrences: ViewerOccurrence[], opts: { fit?: boolean } = {}) {
    for (const m of this.meshes) this.root.remove(m);
    this.meshes = [];
    for (const [key, doc] of docs) if (!this.geoms.has(key)) this.geoms.set(key, geometryOf(doc));
    for (const o of occurrences) {
      const g = this.geoms.get(o.partKey);
      if (!g) continue;
      const mesh = new THREE.Mesh(g.faces, this.material(o.partKey));
      mesh.matrixAutoUpdate = false;
      mesh.matrix.copy(toMatrix(o.world));
      mesh.userData = { key: o.key, partKey: o.partKey };
      if (g.edges) {
        const lines = new THREE.LineSegments(g.edges, this.edgeMat);
        lines.raycast = () => {};
        mesh.add(lines);
      }
      this.root.add(mesh);
      this.meshes.push(mesh);
    }
    this.root.updateMatrixWorld(true);
    this.applyState();
    this.rebuildGrid();
    if (opts.fit) this.fit();
    this.requestRender();
  }

  setSelected(key: string | null) {
    this.selected = key;
    this.applyState();
  }

  setHidden(keys: ReadonlySet<string>) {
    this.hidden = new Set(keys);
    this.applyState();
  }

  setIsolated(key: string | null) {
    this.isolated = key;
    this.applyState();
  }

  setGridVisible(visible: boolean) {
    this.gridVisible = visible;
    if (this.grid) this.grid.visible = visible;
    this.requestRender();
  }

  private applyState() {
    for (const m of this.meshes) {
      const key = m.userData.key as string;
      const hidden = [...this.hidden].some((h) => underPath(key, h));
      const outside = this.isolated !== null && !underPath(key, this.isolated);
      m.visible = !hidden;
      const sel = this.selected !== null && underPath(key, this.selected);
      m.material = sel ? this.selectMat : outside ? this.ghostMat : this.material(m.userData.partKey as string);
      const lines = m.children[0];
      if (lines) lines.visible = !outside;
    }
    this.requestRender();
  }

  private bounds(onlyVisible = true): THREE.Box3 | null {
    const box = new THREE.Box3();
    let any = false;
    for (const m of this.meshes) {
      if (onlyVisible && !m.visible) continue;
      if (this.isolated !== null && !underPath(m.userData.key as string, this.isolated)) continue;
      box.expandByObject(m);
      any = true;
    }
    return any ? box : null;
  }

  fit() {
    const box = this.bounds() ?? this.bounds(false);
    if (!box) return;
    const c = box.getCenter(new THREE.Vector3());
    const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
    const dir = new THREE.Vector3(1, -1, 0.8).normalize();
    const vertical = (this.camera.fov * Math.PI) / 180;
    const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * this.camera.aspect);
    const dist = r / Math.sin(Math.min(vertical, horizontal) / 2) * 1.1;
    this.camera.position.copy(c).addScaledVector(dir, dist);
    this.camera.near = Math.max(dist / 1000, 0.01);
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(c);
    this.controls.update();
    this.requestRender();
  }

  setView(name: 'iso' | 'front' | 'top' | 'right') {
    const box = this.bounds() ?? this.bounds(false);
    if (!box) return;
    const c = box.getCenter(new THREE.Vector3());
    const dist = this.camera.position.distanceTo(this.controls.target) || 500;
    const dir = { iso: new THREE.Vector3(1, -1, 0.8), front: new THREE.Vector3(0, -1, 0), top: new THREE.Vector3(0, -1e-4, 1), right: new THREE.Vector3(1, 0, 0) }[name].normalize();
    this.camera.position.copy(c).addScaledVector(dir, dist);
    this.controls.target.copy(c);
    this.controls.update();
    this.requestRender();
  }

  private rebuildGrid() {
    if (this.grid) {
      this.scene.remove(this.grid);
      this.grid.geometry.dispose();
      (this.grid.material as THREE.Material).dispose();
    }
    const box = this.bounds(false);
    const size = box ? Math.max(box.getSize(new THREE.Vector3()).length() * 1.5, 100) : 200;
    const step = 10 ** Math.floor(Math.log10(size / 10));
    this.grid = new THREE.GridHelper(Math.ceil(size / step) * step, Math.ceil(size / step), 0x8892a0, 0xb8c0ca);
    this.grid.rotation.x = Math.PI / 2;
    this.grid.visible = this.gridVisible;
    if (box) this.grid.position.z = box.min.z;
    (this.grid.material as THREE.Material).transparent = true;
    (this.grid.material as THREE.Material).opacity = 0.35;
    this.scene.add(this.grid);
  }

  private onDown = (e: PointerEvent) => {
    this.down = { x: e.clientX, y: e.clientY };
  };

  private onUp = (e: PointerEvent) => {
    if (!this.down || Math.hypot(e.clientX - this.down.x, e.clientY - this.down.y) > 4) return;
    this.down = null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObjects(this.meshes.filter((m) => m.visible && m.material !== this.ghostMat), false);
    this.onPick(hits.length ? (hits[0].object.userData.key as string) : null);
  };

  private resize() {
    if (this.disposed) return;
    const w = Math.max(this.host.clientWidth, 1);
    const h = Math.max(this.host.clientHeight, 1);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  private requestRender() {
    if (this.disposed || this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.renderer.render(this.scene, this.camera);
    });
  }

  /** Render synchronously before copying the drawing buffer, then callers can release the WebGL context. */
  snapshot(): string {
    this.renderer.render(this.scene, this.camera);
    return this.renderer.domElement.toDataURL('image/png');
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.ro.disconnect();
    this.controls.dispose();
    const el = this.renderer.domElement;
    el.removeEventListener('pointerdown', this.onDown);
    el.removeEventListener('pointerup', this.onUp);
    for (const g of this.geoms.values()) {
      g.faces.dispose();
      g.edges?.dispose();
    }
    for (const m of this.mats.values()) m.dispose();
    this.selectMat.dispose();
    this.ghostMat.dispose();
    this.edgeMat.dispose();
    this.grid?.geometry.dispose();
    if (this.grid) (this.grid.material as THREE.Material).dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    el.remove();
  }
}
