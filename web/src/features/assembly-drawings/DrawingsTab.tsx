// "Drawings" tab of the assembly workspace (W5): the drawing packages of this revision (or of every revision), each
// with its completeness, check, policy, unverified and stale chips; the selected package's pages, downloads, BOM,
// balloons, manifest and errors; generating a new package and editing the layout of the latest version.

import { FilePlus2 } from 'lucide-react';
import { useState } from 'react';
import { listAssemblyDrawings, type AssemblyDrawingPackage } from '../../api/assemblyDrawings';
import { EmptyState, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { GenerateDialog } from './GenerateDialog';
import { PackageChips, PackageView } from './PackageView';
import './asmDrawing.css';

export function DrawingsTab({
  assemblyId,
  assemblyNumber,
  revision,
  latestId,
  canDesign,
  onOpenRevision,
  projectId,
}: {
  assemblyId: string;
  assemblyNumber: string;
  revision: { id: string; label: string; status: string };
  /** S5: with the project the dialog shows the inherited policy and its source. */
  projectId?: string;
  latestId: string | undefined;
  /** Owner / designer of a project that is not archived. */
  canDesign: boolean;
  onOpenRevision: (revisionId: string) => void;
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const [scope, setScope] = useState<'rev' | 'all'>('rev');
  const [selected, setSelected] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const listRes = useResource(
    (signal) => listAssemblyDrawings(assemblyId, scope === 'rev' ? { revision_id: revision.id } : {}, { signal }),
    [assemblyId, revision.id, scope],
  );
  const items = listRes.data ?? [];
  const selectedId = selected && items.some((x) => x.id === selected) ? selected : (items.find((x) => !x.superseded_by) ?? items[0])?.id ?? null;
  const ready = revision.status === 'ready';
  const created = (pkg: AssemblyDrawingPackage, how: 'generated' | 'overlay' | 'continued') => {
    setGenerating(false);
    setSelected(pkg.id);
    listRes.reload();
    const key = how === 'generated' ? 'asmd.gen.done' : how === 'continued' ? 'asmd.continuation.done' : 'asmd.overlay.done';
    toast(t(key, { label: pkg.label, completeness: t(`asmd.completeness.${pkg.completeness === 'complete' ? 'complete' : 'partial'}`) }), {
      tone: pkg.completeness === 'complete' ? 'ok' : 'info',
    });
  };
  return (
    <div className="asmd">
      <div className="asmd-toolbar">
        <div className="seg" role="group" aria-label={t('asmd.scope')}>
          <button type="button" className="seg-btn" aria-pressed={scope === 'rev'} onClick={() => setScope('rev')}>
            {t('asmd.scope.rev', { rev: revision.label })}
          </button>
          <button type="button" className="seg-btn" aria-pressed={scope === 'all'} onClick={() => setScope('all')}>
            {t('asmd.scope.all')}
          </button>
        </div>
        {canDesign && (
          <button type="button" className="btn primary sm" disabled={!ready} title={ready ? undefined : t('asmd.err.notReady')} onClick={() => setGenerating(true)}>
            <FilePlus2 aria-hidden /> {t('asmd.gen.open')}
          </button>
        )}
      </div>
      <p className="faint small asmd-intro">{t('asmd.intro')}</p>
      {listRes.error && !listRes.data ? (
        <ErrorState error={listRes.error} onRetry={listRes.reload} />
      ) : !listRes.data ? (
        <SkeletonRows rows={4} cols={3} />
      ) : !items.length ? (
        <EmptyState compact title={t('asmd.empty.title')} body={canDesign ? t('asmd.empty.body') : t('asmd.empty.readOnly')} />
      ) : (
        <div className="asmd-grid">
          <ul className="asmd-list" aria-label={t('asmd.list')}>
            {items.map((x) => (
              <li key={x.id}>
                <button type="button" className={`asmd-card ${x.id === selectedId ? 'active' : ''}`} aria-pressed={x.id === selectedId} onClick={() => setSelected(x.id)}>
                  <span className="asmd-card-top">
                    <strong className="mono">{x.label}</strong>
                    <span className="faint small">
                      {x.rev_label} · {t('asmd.pages', { n: x.page_count })} · {fmt.dateTime(x.created_at)}
                    </span>
                  </span>
                  <PackageChips pkg={x} />
                </button>
              </li>
            ))}
          </ul>
          <div className="asmd-detail">
            {selectedId && (
              <PackageView
                key={selectedId}
                id={selectedId}
                canDesign={canDesign}
                stem={assemblyNumber}
                onOpenRevision={onOpenRevision}
                onSaved={(pkg) => created(pkg, 'overlay')}
                onContinued={(pkg) => created(pkg, 'continued')}
                onOpenPackage={(pid) => {
                  setScope('all');
                  setSelected(pid);
                }}
              />
            )}
          </div>
        </div>
      )}
      {generating && <GenerateDialog revision={revision} projectId={projectId} isLatest={revision.id === latestId} onClose={() => setGenerating(false)} onCreated={(pkg) => created(pkg, 'generated')} />}
    </div>
  );
}
