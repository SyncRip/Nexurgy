// Home hero: the GB-01 gear module around the PR-2041 bracket. Four stages
// explain the product chain: design → drawing → process → quote. Labels are
// DOM elements positioned from projected 3D anchors (crisp text, accessible).

import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { layout } from '../domain/bracket';
import type { BracketParams } from '../domain/types';
import { buildBracket, gearMesh, makeMaterials, setHighlight, type BracketBuild } from './geometry';

export type HeroStage = 'design' | 'drawing' | 'process' | 'quote';

export interface HeroLabel {
  id: string;
  stage: HeroStage | 'all';
  anchor: [number, number, number];
  title: string;
  value?: string;
  tone?: 'accent' | 'measure' | 'plain';
}

interface Part {
  obj: THREE.Object3D;
  home: THREE.Vector3;
  explode: THREE.Vector3;
  hideIn: HeroStage[];
}

const BLUE = 0x42b8ff;

export class HeroScene {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private clock = new THREE.Clock();
  private raf = 0;
  private running = false;
  private visible = true;
  private disposed = false;
  private bracket: BracketBuild;
  private parts: Part[] = [];
  private gears: { g1: THREE.Object3D; g2: THREE.Object3D; ratio: number; phase2: number };
  private dims = new THREE.Group();
  private tool = new THREE.Group();
  private toolPath: THREE.Vector3[] = [];
  private mats = makeMaterials();
  private paper: THREE.MeshStandardMaterial;
  private stage: HeroStage = 'design';
  private stageT = 1; // 0 → 1 transition
  private explodeT = 0;
  private explodeTarget = 0;
  private gearAngle = 0;
  private toolT = 0;
  private labels: HeroLabel[] = [];
  private labelEls = new Map<string, HTMLElement>();
  private idleTimer = 0;
  private reduced = false;
  private offsetX = 0;
  private offsetY = 0;
  private io: IntersectionObserver;
  private ro: ResizeObserver;
  private onVis = () => this.syncRunning();
  private radius = 100;
  private target = new THREE.Vector3(0, 40, 0);
  private camGoal = new THREE.Vector3();
  private camFrom = new THREE.Vector3();

  constructor(
    private host: HTMLElement,
    private labelLayer: HTMLElement,
    params: BracketParams,
    opts: { reducedMotion: boolean; layout: 'split' | 'stack' },
  ) {
    this.reduced = opts.reducedMotion;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.domElement.setAttribute('aria-hidden', 'true');
    host.appendChild(this.renderer.domElement);

    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();

    this.camera = new THREE.PerspectiveCamera(30, 1, 5, 4000);
    this.camera.position.set(210, 170, 290);

    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(140, 260, 180);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    const sc = key.shadow.camera as THREE.OrthographicCamera;
    sc.left = sc.bottom = -160;
    sc.right = sc.top = 160;
    sc.near = 50;
    sc.far = 700;
    key.shadow.bias = -0.0004;
    const rim = new THREE.DirectionalLight(0x9fd8ff, 0.9);
    rim.position.set(-220, 120, -200);
    this.scene.add(key, rim, new THREE.HemisphereLight(0xdfe7f0, 0x0b0d10, 0.35));

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(900, 900), new THREE.ShadowMaterial({ opacity: 0.32 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.05;
    ground.receiveShadow = true;
    this.scene.add(ground);
    const grid = new THREE.GridHelper(600, 60, 0x3a4250, 0x232a33);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.12;
    grid.name = 'grid';
    this.scene.add(grid);

    this.paper = new THREE.MeshStandardMaterial({ color: 0x16202b, metalness: 0, roughness: 0.9, transparent: true, opacity: 0.55 });

    this.bracket = buildBracket(params, this.mats, { edges: true, edgeColor: BLUE });
    this.bracket.edges.forEach((e) => ((e.material as THREE.LineBasicMaterial).opacity = 0));
    this.scene.add(this.bracket.group);
    this.parts.push({ obj: this.bracket.group, home: new THREE.Vector3(), explode: new THREE.Vector3(), hideIn: [] });

    this.gears = this.buildAssembly(params);
    this.buildDims(params);
    this.buildTool(params);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.enablePan = false;
    this.controls.enableZoom = false;
    this.controls.minPolarAngle = 0.25;
    this.controls.maxPolarAngle = 1.45;
    this.controls.rotateSpeed = 0.7;
    this.controls.autoRotate = !this.reduced;
    this.controls.autoRotateSpeed = 0.55;
    this.controls.target.copy(this.target);
    this.controls.addEventListener('start', () => {
      this.controls.autoRotate = false;
      window.clearTimeout(this.idleTimer);
    });
    this.controls.addEventListener('end', () => {
      window.clearTimeout(this.idleTimer);
      if (!this.reduced) this.idleTimer = window.setTimeout(() => (this.controls.autoRotate = true), 5000);
    });

    this.setLayout(opts.layout);
    this.io = new IntersectionObserver((es) => {
      this.visible = es.some((e) => e.isIntersecting);
      this.syncRunning();
    });
    this.io.observe(host);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    document.addEventListener('visibilitychange', this.onVis);
    this.resize();
    this.applyStageInstant();
    this.syncRunning();
  }

  private buildAssembly(p: BracketParams) {
    const L = layout(p);
    const plateThk = 4;
    const zPlate = L.wallZ1;
    const gearZ = zPlate + plateThk + 9;
    const xs = [-p.centerDist / 2, p.centerDist / 2];

    // Bearing retainer plate (dark anodised) with 8 screw heads.
    const w = p.centerDist + 2 * (L.tapRadius + 5);
    const h = 2 * (L.tapRadius + 5);
    const s = new THREE.Shape();
    const r = 8;
    s.moveTo(-w / 2 + r, -h / 2);
    s.lineTo(w / 2 - r, -h / 2);
    s.absarc(w / 2 - r, -h / 2 + r, r, -Math.PI / 2, 0, false);
    s.lineTo(w / 2, h / 2 - r);
    s.absarc(w / 2 - r, h / 2 - r, r, 0, Math.PI / 2, false);
    s.lineTo(-w / 2 + r, h / 2);
    s.absarc(-w / 2 + r, h / 2 - r, r, Math.PI / 2, Math.PI, false);
    s.lineTo(-w / 2, -h / 2 + r);
    s.absarc(-w / 2 + r, -h / 2 + r, r, Math.PI, Math.PI * 1.5, false);
    xs.forEach((x) => {
      const hole = new THREE.Path();
      hole.absarc(x, 0, 10, 0, Math.PI * 2, true);
      s.holes.push(hole);
    });
    const pg = new THREE.ExtrudeGeometry(s, { depth: plateThk, bevelEnabled: true, bevelThickness: 0.4, bevelSize: 0.4, bevelSegments: 1, curveSegments: 32 });
    const plate = new THREE.Group();
    const pm = new THREE.Mesh(pg, this.mats.dark);
    pm.castShadow = true;
    plate.add(pm);
    xs.forEach((bx) => {
      for (let k = 0; k < 4; k++) {
        const a = Math.PI / 4 + (k * Math.PI) / 2;
        const sh = new THREE.Mesh(new THREE.CylinderGeometry(3.4, 3.4, 4, 20), this.mats.steel);
        sh.rotation.x = Math.PI / 2;
        sh.position.set(bx + L.tapRadius * Math.cos(a), L.tapRadius * Math.sin(a), plateThk + 2);
        plate.add(sh);
      }
    });
    plate.position.set(0, p.boreHeight, zPlate);
    this.scene.add(plate);
    this.parts.push({ obj: plate, home: plate.position.clone(), explode: new THREE.Vector3(0, 0, 38), hideIn: ['drawing', 'process'] });

    // Shafts + bearings.
    const shaftLen = p.wallThk + 48;
    xs.forEach((x, i) => {
      const d = i === 0 ? 12 : 15;
      const g = new THREE.Group();
      const sh = new THREE.Mesh(new THREE.CylinderGeometry(d / 2, d / 2, shaftLen, 32), this.mats.steel);
      sh.rotation.x = Math.PI / 2;
      sh.castShadow = true;
      const brg = new THREE.Mesh(new THREE.TorusGeometry(11, 4.6, 16, 40), this.mats.steel);
      g.add(sh, brg);
      g.position.set(x, p.boreHeight, L.wallZ0 + shaftLen / 2 - 12);
      brg.position.z = L.wallZ0 + 5 - g.position.z + p.wallThk / 2;
      this.scene.add(g);
      this.parts.push({ obj: g, home: g.position.clone(), explode: new THREE.Vector3(0, 0, i === 0 ? 70 : 82), hideIn: ['drawing', 'process'] });
    });

    // Gears: m1.5, z20 / z32 → a = 39.
    const g1 = gearMesh(1.5, 20, 10, 6, this.mats.steel);
    const g2 = gearMesh(1.5, 32, 10, 7.5, this.mats.steel);
    g1.group.position.set(xs[0], p.boreHeight, gearZ);
    g2.group.position.set(xs[1], p.boreHeight, gearZ);
    this.scene.add(g1.group, g2.group);
    this.parts.push({ obj: g1.group, home: g1.group.position.clone(), explode: new THREE.Vector3(0, 0, 52), hideIn: ['drawing', 'process'] });
    this.parts.push({ obj: g2.group, home: g2.group.position.clone(), explode: new THREE.Vector3(0, 0, 60), hideIn: ['drawing', 'process'] });

    // Base screws (M6 SHCS) sitting in the counterbores.
    [
      [L.holeX, L.holeZFront],
      [-L.holeX, L.holeZFront],
      [L.holeX, L.holeZRear],
      [-L.holeX, L.holeZRear],
    ].forEach(([x, z]) => {
      const sc = new THREE.Group();
      const head = new THREE.Mesh(new THREE.CylinderGeometry(4.9, 4.9, 6, 24), this.mats.dark);
      head.position.y = 3;
      const sock = new THREE.Mesh(new THREE.CylinderGeometry(2.4, 2.4, 0.6, 6), new THREE.MeshBasicMaterial({ color: 0x050607 }));
      sock.position.y = 6.05;
      const body = new THREE.Mesh(new THREE.CylinderGeometry(3, 3, 16, 16), this.mats.dark);
      body.position.y = -8;
      sc.add(head, sock, body);
      sc.position.set(x, p.baseThk - p.cboreDepth, z);
      this.scene.add(sc);
      this.parts.push({ obj: sc, home: sc.position.clone(), explode: new THREE.Vector3(0, 46, 0), hideIn: ['drawing', 'process'] });
    });

    return { g1: g1.group, g2: g2.group, ratio: 20 / 32, phase2: Math.PI + Math.PI / 32 };
  }

  private arrow(from: THREE.Vector3, to: THREE.Vector3, mat: THREE.Material) {
    const dir = to.clone().sub(from).normalize();
    const cone = new THREE.Mesh(new THREE.ConeGeometry(1.4, 5, 12), mat);
    cone.position.copy(to).addScaledVector(dir, -2.5);
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
    return cone;
  }

  private dimLine(a: THREE.Vector3, b: THREE.Vector3, ext: THREE.Vector3, lm: THREE.LineBasicMaterial, am: THREE.Material) {
    const a2 = a.clone().add(ext);
    const b2 = b.clone().add(ext);
    const pts = [a, a2.clone().addScaledVector(ext.clone().normalize(), 3), b, b2.clone().addScaledVector(ext.clone().normalize(), 3), a2, b2];
    const geo = new THREE.BufferGeometry().setFromPoints(pts);
    const ls = new THREE.LineSegments(geo, lm);
    this.dims.add(ls, this.arrow(b2, a2, am), this.arrow(a2, b2, am));
  }

  private buildDims(p: BracketParams) {
    const L = layout(p);
    const lm = new THREE.LineBasicMaterial({ color: BLUE, transparent: true, opacity: 0.95 });
    const am = new THREE.MeshBasicMaterial({ color: BLUE, transparent: true });
    const zf = p.baseDepth / 2;
    this.dimLine(new THREE.Vector3(-p.length / 2, 0, zf), new THREE.Vector3(p.length / 2, 0, zf), new THREE.Vector3(0, 0, 18), lm, am);
    this.dimLine(new THREE.Vector3(p.length / 2, 0, L.wallZ0), new THREE.Vector3(p.length / 2, p.wallHeight, L.wallZ0), new THREE.Vector3(16, 0, 0), lm, am);
    this.dimLine(
      new THREE.Vector3(-p.centerDist / 2, p.boreHeight, L.wallZ1),
      new THREE.Vector3(p.centerDist / 2, p.boreHeight, L.wallZ1),
      new THREE.Vector3(0, p.wallHeight - p.boreHeight + 8, 0),
      lm,
      am,
    );
    // centre marks on bores
    [-p.centerDist / 2, p.centerDist / 2].forEach((x) => {
      const geo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(x - 22, p.boreHeight, L.wallZ1 + 0.3),
        new THREE.Vector3(x + 22, p.boreHeight, L.wallZ1 + 0.3),
        new THREE.Vector3(x, p.boreHeight - 22, L.wallZ1 + 0.3),
        new THREE.Vector3(x, p.boreHeight + 22, L.wallZ1 + 0.3),
      ]);
      this.dims.add(new THREE.LineSegments(geo, lm));
    });
    this.dims.visible = false;
    this.scene.add(this.dims);
  }

  private buildTool(p: BracketParams) {
    const L = layout(p);
    const D = 1.6 * p.pocketR;
    const cutter = new THREE.Mesh(new THREE.CylinderGeometry(D / 2, D / 2, 22, 24), this.mats.steel);
    cutter.position.y = 11;
    const holder = new THREE.Mesh(new THREE.CylinderGeometry(6, 9, 18, 32), this.mats.dark);
    holder.position.y = 22 + 9;
    const glow = new THREE.Mesh(
      new THREE.CylinderGeometry(D / 2 + 0.6, D / 2 + 0.6, 1.2, 24),
      new THREE.MeshBasicMaterial({ color: 0x829bff, transparent: true, opacity: 0.9 }),
    );
    glow.position.y = 0.6;
    this.tool.add(cutter, holder, glow);
    this.tool.visible = false;
    this.scene.add(this.tool);

    // Tool-centre path: pocket contour offset inward by tool radius.
    const off = D / 2;
    const w = p.pocketLen - 2 * off;
    const h = p.pocketWidth - 2 * off;
    const r = Math.max(0.01, p.pocketR - off);
    const c = new THREE.Path();
    const x0 = -w / 2;
    const z0 = L.pocketCz - h / 2;
    c.moveTo(x0 + r, z0);
    c.lineTo(x0 + w - r, z0);
    c.absarc(x0 + w - r, z0 + r, r, -Math.PI / 2, 0, false);
    c.lineTo(x0 + w, z0 + h - r);
    c.absarc(x0 + w - r, z0 + h - r, r, 0, Math.PI / 2, false);
    c.lineTo(x0 + r, z0 + h);
    c.absarc(x0 + r, z0 + h - r, r, Math.PI / 2, Math.PI, false);
    c.lineTo(x0, z0 + r);
    c.absarc(x0 + r, z0 + r, r, Math.PI, Math.PI * 1.5, false);
    const y = p.baseThk - p.pocketDepth + 0.2;
    this.toolPath = c.getSpacedPoints(240).map((v) => new THREE.Vector3(v.x, y, v.y));
    const pathGeo = new THREE.BufferGeometry().setFromPoints(this.toolPath);
    const pathLine = new THREE.Line(pathGeo, new THREE.LineDashedMaterial({ color: 0x829bff, dashSize: 3, gapSize: 2 }));
    pathLine.computeLineDistances();
    pathLine.name = 'toolpath';
    pathLine.visible = false;
    this.scene.add(pathLine);
  }

  setLabels(labels: HeroLabel[]) {
    this.labels = labels;
    const keep = new Set(labels.map((l) => l.id));
    for (const [id, el] of this.labelEls) if (!keep.has(id)) (el.remove(), this.labelEls.delete(id));
    for (const l of labels) {
      let el = this.labelEls.get(l.id);
      if (!el) {
        el = document.createElement('div');
        el.className = `hero-tag tone-${l.tone ?? 'plain'}`;
        this.labelLayer.appendChild(el);
        this.labelEls.set(l.id, el);
      }
      el.innerHTML = `<span class="t">${l.title}</span>${l.value ? `<span class="v">${l.value}</span>` : ''}`;
    }
    this.renderOnce();
  }

  setLayout(mode: 'split' | 'stack') {
    this.offsetX = mode === 'split' ? 0.2 : 0;
    this.offsetY = mode === 'split' ? 0.05 : 0.02;
    this.resize();
  }

  setReducedMotion(v: boolean) {
    window.clearTimeout(this.idleTimer);
    this.reduced = v;
    this.controls.autoRotate = !v;
    if (v) this.applyStageInstant();
    this.syncRunning();
  }

  setInteractive(v: boolean) {
    this.controls.enabled = v;
    this.renderer.domElement.style.touchAction = v ? 'none' : 'pan-y';
    this.renderer.domElement.style.pointerEvents = v ? 'auto' : 'none';
  }

  setExplode(v: boolean) {
    this.explodeTarget = v ? 1 : 0;
    if (this.reduced) this.explodeT = this.explodeTarget;
    this.renderOnce();
  }

  setStage(s: HeroStage) {
    if (s === this.stage) return;
    this.stage = s;
    this.stageT = 0;
    this.camFrom.copy(this.camera.position);
    this.camGoal.copy(this.stageCamera(s));
    if (s !== 'design') this.explodeTarget = 0;
    // Paused loop (hidden tab, offscreen) or reduced motion: jump to the end state.
    if (this.reduced || !this.running) this.applyStageInstant();
    this.renderOnce();
  }

  private stageCamera(s: HeroStage) {
    const d = this.fitDistance();
    const dir =
      s === 'drawing'
        ? new THREE.Vector3(0.28, 0.32, 1)
        : s === 'process'
          ? new THREE.Vector3(0.55, 1.05, 0.9)
          : s === 'quote'
            ? new THREE.Vector3(-0.75, 0.6, 1)
            : new THREE.Vector3(0.72, 0.55, 1);
    return dir.normalize().multiplyScalar(d).add(this.target);
  }

  private fitDistance() {
    const vfov = (this.camera.fov * Math.PI) / 180;
    const aspect = Math.max(0.3, this.camera.aspect * (1 - this.offsetX * 1.6));
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect);
    // Leave room for the stage cards at the bottom of the hero.
    return (this.radius / Math.sin(Math.min(vfov * (1 - this.offsetY), hfov) / 2)) * 1.02;
  }

  private applyStageInstant() {
    this.stageT = 1;
    this.camera.position.copy(this.stageCamera(this.stage));
    this.explodeT = this.explodeTarget;
    this.updateStageVisuals(1);
    this.controls.update();
    this.renderOnce();
  }

  private updateStageVisuals(k: number) {
    const s = this.stage;
    const drawing = s === 'drawing';
    // materials
    this.bracket.group.traverse((o) => {
      if ((o as THREE.Mesh).isMesh && !o.userData.overlay) (o as THREE.Mesh).material = drawing ? this.paper : this.mats.alu;
    });
    this.bracket.edges.forEach((e) => ((e.material as THREE.LineBasicMaterial).opacity = drawing ? 0.9 * k : s === 'process' ? 0.22 : 0.12));
    this.dims.visible = drawing;
    this.dims.traverse((o) => {
      const m = (o as THREE.Mesh).material as THREE.Material | undefined;
      if (m) m.opacity = k;
    });
    if (k >= 1) this.syncPartScale(1, true);
    this.tool.visible = s === 'process';
    const pl = this.scene.getObjectByName('toolpath');
    if (pl) pl.visible = s === 'process';
    setHighlight(this.bracket, s === 'process' ? ['pocket', 'bores'] : []);
    const grid = this.scene.getObjectByName('grid') as THREE.GridHelper;
    (grid.material as THREE.Material).opacity = drawing ? 0.28 : 0.12;
  }

  /** Parts not used in a stage shrink away; they grow back when the stage needs them. */
  private syncPartScale(dt: number, instant = false) {
    for (const p of this.parts) {
      const target = p.hideIn.includes(this.stage) ? 0 : 1;
      let cur = (p.obj.userData.s as number | undefined) ?? 1;
      cur = instant || this.reduced ? target : cur + (target - cur) * Math.min(1, dt * 5);
      if (Math.abs(cur - target) < 0.002) cur = target;
      p.obj.userData.s = cur;
      p.obj.visible = cur > 0.01;
      p.obj.scale.setScalar(Math.max(0.001, cur));
    }
  }

  private resize() {
    const w = this.host.clientWidth || 1;
    const h = this.host.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    if (this.offsetX || this.offsetY) this.camera.setViewOffset(w, h, -w * this.offsetX, h * this.offsetY, w, h);
    else this.camera.clearViewOffset();
    this.camera.updateProjectionMatrix();
    // keep distance consistent with framing
    const d = this.fitDistance();
    const dir = this.camera.position.clone().sub(this.target).normalize();
    this.camera.position.copy(this.target).addScaledVector(dir, d);
    if (this.stageT < 1) this.camGoal.copy(this.stageCamera(this.stage));
    this.renderOnce();
  }

  private syncRunning() {
    const should = this.visible && !document.hidden && !this.disposed;
    if (should && !this.running) {
      this.running = true;
      this.clock.getDelta();
      this.loop();
    } else if (!should && this.running) {
      this.running = false;
      cancelAnimationFrame(this.raf);
    }
  }

  private loop = () => {
    if (!this.running) return;
    this.raf = requestAnimationFrame(this.loop);
    const dt = Math.min(0.05, this.clock.getDelta());
    this.tick(dt);
  };

  private tick(dt: number) {
    if (this.stageT < 1) {
      this.stageT = Math.min(1, this.stageT + dt / 1.1);
      const e = 1 - Math.pow(1 - this.stageT, 3);
      this.camera.position.lerpVectors(this.camFrom, this.camGoal, e);
      this.updateStageVisuals(e);
    }
    if (this.explodeT !== this.explodeTarget) {
      const d = this.explodeTarget - this.explodeT;
      this.explodeT += Math.sign(d) * Math.min(Math.abs(d), dt / 0.9);
    }
    this.syncPartScale(dt);
    const ek = this.explodeT * this.explodeT * (3 - 2 * this.explodeT);
    for (const p of this.parts) p.obj.position.copy(p.home).addScaledVector(p.explode, ek);

    if (!this.reduced) {
      this.gearAngle += dt * 0.9;
      this.toolT = (this.toolT + dt * 0.08) % 1;
    }
    this.gears.g1.rotation.z = this.gearAngle;
    this.gears.g2.rotation.z = this.gears.phase2 - this.gearAngle * this.gears.ratio;
    if (this.tool.visible && this.toolPath.length) {
      const i = Math.floor((this.reduced ? 0.12 : this.toolT) * (this.toolPath.length - 1));
      this.tool.position.copy(this.toolPath[i]);
      this.tool.rotation.y += this.reduced ? 0 : dt * 20;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.placeLabels();
  }

  private renderOnce() {
    if (this.running) return;
    this.tick(0);
  }

  private placeLabels() {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    const v = new THREE.Vector3();
    for (const l of this.labels) {
      const el = this.labelEls.get(l.id);
      if (!el) continue;
      const on = (l.stage === 'all' || l.stage === this.stage) && this.stageT > 0.6;
      v.set(...l.anchor).project(this.camera);
      const x = (v.x * 0.5 + 0.5) * w;
      const y = (-v.y * 0.5 + 0.5) * h;
      const inView = v.z < 1 && x > 8 && x < w - 8 && y > 8 && y < h - 8;
      el.style.opacity = on && inView ? '1' : '0';
      // Near the right edge, hang the label to the left of its anchor.
      const flip = x + el.offsetWidth + 20 > w && x - el.offsetWidth - 20 > 0;
      el.classList.toggle('flip', flip);
      el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)${flip ? ' translateX(-100%)' : ''}`;
    }
  }

  dispose() {
    this.disposed = true;
    this.running = false;
    cancelAnimationFrame(this.raf);
    window.clearTimeout(this.idleTimer);
    this.io.disconnect();
    this.ro.disconnect();
    document.removeEventListener('visibilitychange', this.onVis);
    this.controls.dispose();
    this.bracket.dispose();
    // Everything (plate, shafts, screws, gears, dims, tool, path, ground, grid) lives in the scene.
    const mats = new Set<THREE.Material>([...Object.values(this.mats), this.paper]);
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose?.();
      const mm = m.material as THREE.Material | THREE.Material[] | undefined;
      if (mm) (Array.isArray(mm) ? mm : [mm]).forEach((x) => mats.add(x));
    });
    mats.forEach((x) => x.dispose());
    this.scene.environment?.dispose();
    this.scene.environment = null;
    this.labelEls.forEach((el) => el.remove());
    this.renderer.forceContextLoss();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

export function webglAvailable(): boolean {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
  } catch {
    return false;
  }
}
