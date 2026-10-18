// React wrapper of AssemblyViewer: loads each unique definition's mesh_json once (existing part revision
// blobs) and places one mesh per leaf occurrence with its world transform.

import { Focus } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { Scene } from '../../api/assemblies';
import { useI18n } from '../../i18n/I18nProvider';
import { useTheme } from '../../prefs/ThemeProvider';
import type { MeshDoc } from '../../three/viewer';
import { loadMeshDoc } from '../part/model/hooks';
import { AssemblyViewer } from './AssemblyViewer';
import { pathKey } from './model';

const VIEWS = ['iso', 'front', 'top', 'right'] as const;

export function AssemblyViewport({
  scene,
  selected,
  hidden,
  isolated,
  onPick,
}: {
  scene: Scene | undefined;
  selected: string | null;
  hidden: ReadonlySet<string>;
  isolated: string | null;
  onPick: (key: string | null) => void;
}) {
  const { t } = useI18n();
  const { resolved } = useTheme();
  const host = useRef<HTMLDivElement>(null);
  const viewer = useRef<AssemblyViewer | null>(null);
  const [noGl, setNoGl] = useState(false);
  const [loading, setLoading] = useState(false);
  const [missing, setMissing] = useState(0);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const fitted = useRef<string | null>(null);

  useEffect(() => {
    if (!host.current) return;
    let v: AssemblyViewer;
    try {
      v = new AssemblyViewer(host.current, { theme: resolved });
    } catch (e) {
      console.error(e);
      setNoGl(true);
      return;
    }
    v.onPick = (k) => pickRef.current(k);
    viewer.current = v;
    return () => {
      v.dispose();
      viewer.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => viewer.current?.setTheme(resolved), [resolved]);

  useEffect(() => {
    if (!scene) return;
    const ctrl = new AbortController();
    const docs = new Map<string, MeshDoc>();
    setLoading(true);
    (async () => {
      let miss = 0;
      await Promise.all(
        Object.entries(scene.definitions).map(async ([key, def]) => {
          if (!def.mesh_sha) {
            miss += 1;
            return;
          }
          try {
            docs.set(key, await loadMeshDoc(def.mesh_sha, ctrl.signal));
          } catch {
            miss += 1;
          }
        }),
      );
      if (ctrl.signal.aborted) return;
      setMissing(miss);
      setLoading(false);
      const fit = fitted.current !== scene.revision_id;
      viewer.current?.setOccurrences(
        docs,
        scene.occurrences.map((o) => ({ key: pathKey(o.path), partKey: o.part_key, world: o.world })),
        { fit },
      );
      fitted.current = scene.revision_id;
    })();
    return () => ctrl.abort();
  }, [scene]);

  useEffect(() => viewer.current?.setSelected(selected), [selected, scene]);
  useEffect(() => viewer.current?.setHidden(hidden), [hidden, scene]);
  useEffect(() => viewer.current?.setIsolated(isolated), [isolated, scene]);

  return (
    <div className="asm-vw">
      <div className="asm-vw-host" ref={host} role="img" aria-label={t('asm.vw.label')} />
      {noGl && <div className="asm-vw-empty">{t('asm.vw.noWebgl')}</div>}
      {loading && (
        <div className="asm-vw-empty">
          <span className="spinner" aria-hidden /> {t('asm.vw.loading')}
        </div>
      )}
      <div className="asm-vw-toolbar" role="toolbar">
        <div className="seg" role="group">
          {VIEWS.map((v) => (
            <button key={v} type="button" className="seg-btn" onClick={() => viewer.current?.setView(v)}>
              {t(`asm.vw.view.${v}`)}
            </button>
          ))}
        </div>
        <button type="button" className="btn sm icon" onClick={() => viewer.current?.fit()} aria-label={t('asm.vw.fit')} title={t('asm.vw.fit')}>
          <Focus />
        </button>
      </div>
      {missing > 0 && <div className="asm-vw-note faint small">{t('asm.vw.missing', { n: missing })}</div>}
    </div>
  );
}
