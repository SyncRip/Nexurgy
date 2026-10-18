// Revision pane: one exact model revision — the 3D preview, the revision facts and its requirement checks (the
// model page's inspect panel). A generated revision is "generated", never "accepted"; editing and every other model
// tool stay on the model page ("open full page" opens this exact revision there). Lazy: three.js is its own chunk.

import '../../part/workspace.css';
import { Box } from 'lucide-react';
import { useState } from 'react';
import { getPart, getRevision } from '../../../api/endpoints';
import type { UUID } from '../../../api/types';
import { ErrorState } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import type { ToolMode } from '../../../three/viewer';
import { RevisionChecksPanel } from '../../cad/RevisionChecks';
import { GeomCheckChip } from '../../cad/Verdicts';
import { RevSourceChip } from '../../part/common';
import { useRevisionMesh } from '../../part/model/hooks';
import { Viewport } from '../../part/model/Viewport';
import { fullPageHref } from './model';
import { PaneFrame, PaneLoading } from './PaneFrame';

const NO_MARKERS = { points: [] };

export default function RevisionPane({ revisionId }: { revisionId: UUID }) {
  const { t, fmt } = useI18n();
  const revRes = useResource((signal) => getRevision(revisionId, { signal }), [revisionId]);
  const rev = revRes.data ?? null;
  const partRes = useResource((signal) => getPart(rev!.part_id, { signal }), [rev?.part_id], { enabled: !!rev });
  const mesh = useRevisionMesh(rev);
  const [mode, setMode] = useState<ToolMode>('select');

  const icon = <Box />;
  const kind = t('wp.kind.revision');
  if (!rev) {
    if (revRes.error) {
      return (
        <PaneFrame icon={icon} kind={kind} title={kind}>
          <div className="wp-pad">
            <ErrorState error={revRes.error} onRetry={revRes.reload} />
          </div>
        </PaneFrame>
      );
    }
    return <PaneLoading icon={icon} kind={kind} />;
  }

  const part = partRes.data;
  return (
    <PaneFrame
      icon={icon}
      kind={kind}
      title={part ? `${part.number} · ${rev.label}` : rev.label}
      chips={
        <>
          <RevSourceChip source={rev.source} />
          <GeomCheckChip status={rev.check_status} />
        </>
      }
      fullHref={fullPageHref({ kind: 'revision', id: rev.id }, { partId: rev.part_id })}
    >
      <div className="wp-pad stack">
        <Viewport
          mesh={mesh.doc}
          glb={mesh.glb}
          fitToken={rev.id}
          mode={mode}
          onModeChange={setMode}
          selection={null}
          candidates={[]}
          markers={NO_MARKERS}
          onPick={() => {}}
          loading={mesh.loading}
          empty={mesh.error ? t('wp.rev.noPreview') : rev.status === 'failed' ? t('wp.rev.failed') : t('wp.rev.noMesh')}
        />
        <dl className="wp-facts">
          <div>
            <dt>{t('wp.rev.part')}</dt>
            <dd>{part ? `${part.number} · ${part.name}` : '—'}</dd>
          </div>
          <div>
            <dt>{t('wp.rev.material')}</dt>
            <dd>{rev.material || '—'}</dd>
          </div>
          <div>
            <dt>{t('wp.rev.created')}</dt>
            <dd>{rev.created_at ? fmt.dateTime(rev.created_at) : '—'}</dd>
          </div>
          {rev.mesh_only && (
            <div>
              <dt>{t('wp.rev.meshOnly')}</dt>
              <dd>{t('wp.rev.meshOnlyHint')}</dd>
            </div>
          )}
        </dl>
        {rev.note && <p className="muted small">{rev.note}</p>}
        <RevisionChecksPanel rev={rev} />
      </div>
    </PaneFrame>
  );
}
