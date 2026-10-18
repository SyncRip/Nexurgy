// The public Nexurgy homepage keeps its interactive sample separate from engineering records.
import { ArrowDown, ArrowRight, Boxes, CircleDashed, GitBranch, Hand, History, Layers3, Maximize2, Menu, Minimize2, Pause, Play, ShieldCheck, Workflow, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { paths } from '../app/nav';
import { useAuth } from '../auth/AuthProvider';
import { Wordmark } from '../components/Logo';
import { LanguageToggle, ThemeToggle } from '../components/shell/Prefs';
import { DEFAULT_PARAMS, layout } from '../domain/bracket';
import type { BracketParams } from '../domain/types';
import { AgentRoleIcon } from '../features/agent-team/AgentRole';
import type { MessageKey } from '../i18n/en';
import { useI18n } from '../i18n/I18nProvider';
import { useMediaQuery, useReducedMotion } from '../prefs/ThemeProvider';
import type { HeroLabel, HeroScene, HeroStage } from '../three/hero';

const STAGES: { id: HeroStage; n: string }[] = [
  { id: 'design', n: '01' },
  { id: 'drawing', n: '02' },
  { id: 'process', n: '03' },
  { id: 'quote', n: '04' },
];
const EMPLOYEES = ['modeling', 'drawing', 'process', 'quote'] as const;
const STAGE_MS = 6500;

/** Static isometric line drawing of the same sample (WebGL fallback). */
function IsoFallback({ p, label }: { p: BracketParams; label: string }) {
  const L = layout(p);
  const c = Math.cos(Math.PI / 6);
  const s = 0.5;
  const pr = (x: number, y: number, z: number) => [(x - z) * c, -y + (x + z) * s] as const;
  const poly = (pts: [number, number, number][]) => pts.map((q) => pr(...q).map((v) => v.toFixed(1)).join(',')).join(' ');
  const hl = p.length / 2;
  const hd = p.baseDepth / 2;
  const T = p.baseThk;
  const H = p.wallHeight;
  const circle = (cx: number, cy: number, z: number, r: number) =>
    Array.from({ length: 49 }, (_, i) => {
      const a = (i / 48) * Math.PI * 2;
      return [cx + r * Math.cos(a), cy + r * Math.sin(a), z] as [number, number, number];
    });
  return (
    <svg viewBox="-150 -150 300 230" role="img" aria-label={label}>
      <g fill="none" stroke="var(--measure)" strokeWidth="0.8" strokeLinejoin="round">
        <polygon points={poly([[-hl, 0, hd], [hl, 0, hd], [hl, T, hd], [-hl, T, hd]])} fill="color-mix(in srgb, var(--measure) 8%, transparent)" />
        <polygon points={poly([[hl, 0, hd], [hl, 0, -hd], [hl, H, -hd], [hl, H, L.wallZ1], [hl, T, L.wallZ1], [hl, T, hd]])} fill="color-mix(in srgb, var(--measure) 5%, transparent)" />
        <polygon points={poly([[-hl, T, hd], [hl, T, hd], [hl, T, L.wallZ1], [-hl, T, L.wallZ1]])} />
        <polygon points={poly([[-hl, T, L.wallZ1], [hl, T, L.wallZ1], [hl, H, L.wallZ1], [-hl, H, L.wallZ1]])} fill="color-mix(in srgb, var(--measure) 10%, transparent)" />
        <polygon points={poly([[-hl, H, L.wallZ1], [hl, H, L.wallZ1], [hl, H, -hd], [-hl, H, -hd]])} />
        {[-p.centerDist / 2, p.centerDist / 2].map((x) => (
          <polyline key={x} points={poly(circle(x, p.boreHeight, L.wallZ1, p.boreDia / 2))} stroke="var(--accent)" />
        ))}
      </g>
    </svg>
  );
}

function HeroView({ labels }: { labels: HeroLabel[] }) {
  const { t } = useI18n();
  const reduced = useReducedMotion();
  const narrow = useMediaQuery('(max-width: 767px)');
  const hostRef = useRef<HTMLDivElement>(null);
  const labelRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<HeroScene | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'fallback'>('loading');
  const [failReason, setFailReason] = useState<{ key: MessageKey; message?: string } | null>(null);
  const [stage, setStage] = useState<HeroStage>('design');
  const [auto, setAuto] = useState(true);
  const [explode, setExplode] = useState(false);
  const [interactive, setInteractive] = useState(false);
  const [nudge, setNudge] = useState(0);
  const initial = useRef({ reduced });

  useEffect(() => {
    let alive = true;
    let scene: HeroScene | null = null;
    import('../three/hero')
      .then(({ HeroScene, webglAvailable }) => {
        if (!alive || !hostRef.current || !labelRef.current) return;
        if (!webglAvailable() || new URLSearchParams(window.location.search).has('nowebgl')) {
          setFailReason({ key: 'home.hero.noWebgl' });
          setStatus('fallback');
          return;
        }
        try {
          scene = new HeroScene(hostRef.current, labelRef.current, DEFAULT_PARAMS, {
            reducedMotion: initial.current.reduced,
            layout: 'stack',
          });
          sceneRef.current = scene;
          setStatus('ready');
        } catch (e) {
          setFailReason({ key: 'home.hero.initFailed', message: (e as Error).message });
          setStatus('fallback');
        }
      })
      .catch(() => {
        if (!alive) return;
        setFailReason({ key: 'home.hero.loadFailed' });
        setStatus('fallback');
      });
    return () => {
      alive = false;
      scene?.dispose();
      sceneRef.current = null;
    };
  }, []);

  useEffect(() => sceneRef.current?.setLabels(labels), [labels, status]);
  useEffect(() => sceneRef.current?.setReducedMotion(reduced || !auto), [reduced, auto, status]);
  useEffect(() => sceneRef.current?.setStage(stage), [stage, status]);
  useEffect(() => sceneRef.current?.setExplode(explode), [explode, status]);
  useEffect(() => sceneRef.current?.setInteractive(!narrow || interactive), [narrow, interactive, status]);

  useEffect(() => {
    if (!auto || reduced || status !== 'ready') return;
    const tm = setTimeout(() => {
      if (document.hidden) return setNudge((n) => n + 1);
      setExplode(false);
      setStage((s) => STAGES[(STAGES.findIndex((x) => x.id === s) + 1) % STAGES.length].id);
    }, STAGE_MS);
    return () => clearTimeout(tm);
  }, [stage, auto, reduced, status, nudge]);

  const pick = (id: HeroStage) => {
    setAuto(false);
    setExplode(false);
    setStage(id);
  };
  const reason = failReason ? t(failReason.key, { message: failReason.message ?? '' }) : '';

  return (
    <>
      <div className="hero-showcase" role="region" aria-label={t('landing.specimen')}>
        <div className="hero-showcase-head">
          <span><Layers3 aria-hidden /> {t('landing.specimen')}</span>
          <span className="hero-sample-id">GB-01</span>
        </div>
        <div className="hero-stage-area">
          <div className="hero-canvas" ref={hostRef} />
          <div className="hero-labels" ref={labelRef} aria-hidden />
          {status === 'loading' && (
            <div className="hero-loading" role="status">
              <IsoFallback p={DEFAULT_PARAMS} label={t('home.hero.fallbackAlt')} />
              <span>{t('home.hero.loading')}</span>
            </div>
          )}
          {status === 'fallback' && (
            <div className="hero-fallback">
              <IsoFallback p={DEFAULT_PARAMS} label={t('home.hero.fallbackAlt')} />
              <div className="hero-fallback-note" role="status">
                <CircleDashed aria-hidden />
                <span>{t('home.hero.fallbackNote', { reason })}</span>
              </div>
            </div>
          )}
        </div>
        <div className="hero-showcase-foot">
          <div className="hero-sample-caption">
            <strong>{t('landing.sampleName')}</strong>
            <span>{t('landing.sampleNote')}{!narrow && ` · ${t('home.hero.dragHint')}`}</span>
          </div>
          {status === 'ready' && (
            <div className="hero-tools">
              {stage === 'design' && (
                <button type="button" className="btn sm" aria-pressed={explode} onClick={() => { setAuto(false); setExplode((v) => !v); }}>
                  {explode ? <Minimize2 /> : <Maximize2 />} {explode ? t('home.hero.assemble') : t('home.hero.explode')}
                </button>
              )}
              {narrow && (
                <button type="button" className="btn sm" aria-pressed={interactive} onClick={() => { setAuto(false); setInteractive((v) => !v); }}>
                  <Hand /> {interactive ? t('home.hero.done') : t('home.hero.rotate')}
                </button>
              )}
              {!reduced && (
                <button type="button" className="btn sm icon" aria-label={t(auto ? 'landing.pause' : 'landing.play')} title={t(auto ? 'landing.pause' : 'landing.play')} onClick={() => setAuto((v) => !v)}>
                  {auto ? <Pause /> : <Play />}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="hero-stages" role="group" aria-label={t('home.stagesLabel')}>
        {STAGES.map((st) => (
          <button key={st.id} type="button" className="stage-btn" aria-pressed={stage === st.id} disabled={status !== 'ready'} onClick={() => pick(st.id)}>
            <span className="stage-index">{st.n}</span>
            <span className="stage-text">
              <strong>{t(`home.stage.${st.id}.h`)}</strong>
              <span>{t(`landing.stage.${st.id}`)}</span>
            </span>
            <ArrowRight className="stage-arrow" aria-hidden />
            {stage === st.id && auto && !reduced && status === 'ready' && <span key={stage} className="stage-progress" style={{ animationDuration: `${STAGE_MS}ms` }} />}
          </button>
        ))}
      </div>
    </>
  );
}

export default function Home() {
  const { t } = useI18n();
  const { status } = useAuth();
  const reduced = useReducedMotion();
  const [menuOpen, setMenuOpen] = useState(false);
  const signedIn = status === 'signed_in';
  const entry = signedIn ? paths.workbench() : paths.login();
  const entryLabel = t(signedIn ? 'landing.openWorkbench' : 'landing.signIn');
  const teamPath = paths.flows('team');

  const goToSection = (id: string) => {
    setMenuOpen(false);
    const section = document.getElementById(id);
    section?.focus({ preventScroll: true });
    section?.scrollIntoView({ behavior: reduced ? 'instant' : 'smooth', block: 'start' });
  };

  const labels = useMemo<HeroLabel[]>(() => {
    const p = DEFAULT_PARAMS;
    const L = layout(p);
    return [
      { id: 'd1', stage: 'design', anchor: [-p.length / 2, p.wallHeight, L.wallZ0], title: t('landing.tag.design'), value: t('landing.tag.designValue'), tone: 'accent' },
      { id: 'g1', stage: 'drawing', anchor: [0, 0, p.baseDepth / 2 + 18], title: t('home.tag.length'), value: `${p.length} mm`, tone: 'measure' },
      { id: 'g2', stage: 'drawing', anchor: [p.length / 2 + 16, p.wallHeight / 2, L.wallZ0], title: t('home.tag.height'), value: `${p.wallHeight} mm`, tone: 'measure' },
      { id: 'g3', stage: 'drawing', anchor: [0, p.wallHeight + 8, L.wallZ1], title: t('home.tag.centers'), value: `${p.centerDist} mm`, tone: 'measure' },
      { id: 'p1', stage: 'process', anchor: [p.pocketLen / 2, p.baseThk, L.pocketCz], title: t('landing.tag.feature'), value: t('landing.tag.featureValue'), tone: 'accent' },
      { id: 'q1', stage: 'quote', anchor: [-p.length / 2, 0, p.baseDepth / 2], title: t('home.tag.material.title'), value: t('home.tag.material.value') },
      { id: 'q2', stage: 'quote', anchor: [0, p.wallHeight, L.wallZ0], title: t('home.tag.machining.title'), value: t('home.tag.machining.value') },
    ];
  }, [t]);

  return (
    <div className="home">
      <header className="home-nav">
        <div className="home-nav-inner">
          <Link to={paths.home()} className="home-brand" aria-label={t('app.name')}><Wordmark size={32} /></Link>
          <nav id="home-navigation" className={`home-nav-links${menuOpen ? ' is-open' : ''}`} aria-label={t('nav.main')}>
            <button type="button" onClick={() => goToSection('how')}>{t('home.navHow')}</button>
            <button type="button" onClick={() => goToSection('capabilities')}>{t('landing.nav.team')}</button>
            <button type="button" onClick={() => goToSection('trust')}>{t('landing.nav.delivery')}</button>
          </nav>
          <div className="home-nav-actions">
            <LanguageToggle />
            <ThemeToggle />
            <Link to={entry} className="btn primary home-nav-entry">{entryLabel}<ArrowRight aria-hidden /></Link>
            <button type="button" className="btn ghost icon home-menu-toggle" aria-controls="home-navigation" aria-expanded={menuOpen} aria-label={t(menuOpen ? 'landing.nav.close' : 'landing.nav.open')} onClick={() => setMenuOpen((v) => !v)}>
              {menuOpen ? <X /> : <Menu />}
            </button>
          </div>
        </div>
      </header>

      <main id="home-content">
        <section className="home-hero" aria-labelledby="home-title">
          <div className="hero-copy">
            <span className="home-eyebrow">{t('landing.eyebrow')}</span>
            <h1 id="home-title">{t('landing.title.first')}<br /><span>{t('landing.title.second')}</span></h1>
            <p>{t('landing.intro')}</p>
            <div className="home-hero-actions">
              <Link to={entry} className="btn primary">{entryLabel}<ArrowRight aria-hidden /></Link>
              <button type="button" className="home-text-link" onClick={() => goToSection('how')}>{t('landing.explore')}<ArrowDown aria-hidden /></button>
            </div>
          </div>
          <HeroView labels={labels} />
        </section>

        <section className="home-section home-team" id="capabilities" tabIndex={-1} aria-labelledby="home-team-title">
          <div className="home-section-heading">
            <h2 id="home-team-title">{t('landing.team.title')}</h2>
            <p>{t('landing.team.body')}</p>
          </div>
          <div className="home-team-grid">
            <div className="home-team-lead">
              <span className="home-role-icon"><AgentRoleIcon role="manager" /></span>
              <h3>{t('team.role.manager')}</h3>
              <p>{t('landing.team.manager')}</p>
              <ol className="home-manager-steps">
                {(['goal', 'coordinate', 'deliver'] as const).map((step) => <li key={step}>{t(`landing.team.${step}`)}</li>)}
              </ol>
              <Link className="home-text-link" to={`${teamPath}?role=manager`}>{t('landing.team.configure')}<ArrowRight aria-hidden /></Link>
            </div>
            <div className="home-team-specialists">
              {EMPLOYEES.map((role) => (
                <Link className="home-specialist" key={role} to={`${teamPath}?role=${role}`}>
                  <AgentRoleIcon role={role} className="home-specialist-icon" />
                  <div><h3>{t(`team.role.${role}`)}</h3><p>{t(`landing.team.${role}`)}</p></div>
                  <ArrowRight className="home-specialist-arrow" aria-hidden />
                </Link>
              ))}
            </div>
          </div>
        </section>

        <section className="home-section" id="how" tabIndex={-1} aria-labelledby="home-how-title">
          <div className="home-section-heading">
            <h2 id="home-how-title">{t('landing.how.title')}</h2>
            <p>{t('landing.how.body')}</p>
          </div>
          <div className="home-entry-grid">
            <article className="home-entry">
              <Boxes className="home-entry-icon" aria-hidden />
              <h3>{t('landing.how.parts')}</h3>
              <p>{t('landing.how.partsBody')}</p>
              <div className="home-part-tabs" aria-hidden>
                {(['model', 'drawings', 'process', 'quote'] as const).map((tab) => <span key={tab}>{t(`part.tab.${tab}`)}</span>)}
              </div>
              <Link className="home-text-link" to={paths.parts()}>{t('landing.how.openParts')}<ArrowRight aria-hidden /></Link>
            </article>
            <article className="home-entry home-entry-flow">
              <Workflow className="home-entry-icon" aria-hidden />
              <h3>{t('landing.how.flows')}</h3>
              <p>{t('landing.how.flowsBody')}</p>
              <div className="home-workflow-path" aria-hidden>
                <span>{t('landing.how.input')}</span><ArrowRight /><span><ShieldCheck />{t('landing.how.review')}</span><ArrowRight /><span>{t('landing.how.delivery')}</span>
              </div>
              <Link className="home-text-link" to={paths.flows('templates')}>{t('landing.how.openFlows')}<ArrowRight aria-hidden /></Link>
            </article>
          </div>
        </section>

        <section className="home-section home-trust" id="trust" tabIndex={-1} aria-labelledby="home-trust-title">
          <div className="home-section-heading">
            <h2 id="home-trust-title">{t('landing.trust.title')}</h2>
            <p>{t('landing.trust.body')}</p>
            <Link to={entry} className="btn primary">{entryLabel}<ArrowRight aria-hidden /></Link>
          </div>
          <div className="home-trust-points">
            {([{ key: 'version', Icon: GitBranch }, { key: 'change', Icon: History }, { key: 'review', Icon: ShieldCheck }] as const).map(({ key, Icon }) => (
              <div key={key} className="home-trust-point"><Icon aria-hidden /><div><h3>{t(`landing.trust.${key}`)}</h3><p>{t(`landing.trust.${key}Body`)}</p></div></div>
            ))}
          </div>
        </section>
      </main>

      <footer className="home-foot">
        <div className="home-foot-top"><Wordmark size={28} /><p>{t('landing.footer.line')}</p><Link className="home-text-link" to={teamPath}>{t('landing.nav.team')}<ArrowRight aria-hidden /></Link></div>
        <p className="home-foot-note">{t('landing.footer.data')}</p>
      </footer>
    </div>
  );
}
