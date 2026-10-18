// Revision strip: R1, R2 … with source / simulated / mesh-only badges, plus
// downloads of the selected revision, the import entry point and "Add to AI"
// (the selected saved revision, or the part while there is none).

import { Download,FileInput,FileWarning,Plus } from 'lucide-react';
import { useState } from 'react';
import { downloadBlob } from '../../../api/blobs';
import type { ModelRevision } from '../../../api/types';
import { ErrorText } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { useI18n } from '../../../i18n/I18nProvider';

import { GeomCheckChip } from '../../cad/Verdicts';
import { RevSourceChip,SimulatedBadge } from '../common';

export function isSimulated(rev: ModelRevision | null | undefined): boolean {
  return !!rev?.ai_summary && (rev.ai_summary as { simulated?: unknown }).simulated === true;
}

export function RevisionBar({
  revisions,
  selectedId,
  blank,
  partNumber,
  onSelect,
  onBlank,
  onImport,
  canDesign,
  ai,
}: {
  revisions: ModelRevision[];
  selectedId: string | null;
  /** Editing a blank design (no base revision). */
  blank: boolean;
  partNumber: string;
  onSelect: (rev: ModelRevision) => void;
  onBlank: () => void;
  onImport: () => void;
  canDesign: boolean;
  /** Assistant entry: the project, the part, and whether the editor has unsaved edits. */
  ai?: { projectId: string; partId: string; dirty: boolean };
}) {
  const { t, fmt } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const sel = blank ? null : (revisions.find((r) => r.id === selectedId) ?? null);
  const latestId = revisions[0]?.id;

  const download = async (sha: string, ext: string) => {
    if (!sel) return;
    setBusy(ext);
    try {
      await downloadBlob(sha, `${partNumber}_R${sel.rev_no}.${ext}`);
    } catch (e) {
      toast(
        <span>
          {t('error.downloadFailed')} <ErrorText error={e} />
        </span>,
        { tone: 'err' },
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="ws-revbar">
      <div className="ws-revs" role="listbox" aria-label={t('ws.rev.list')} aria-orientation="horizontal">
        {revisions.map((r) => {
          const active = !blank && r.id === selectedId;
          return (
            <button
              key={r.id}
              type="button"
              role="option"
              aria-selected={active}
              className={`ws-rev ${active ? 'active' : ''}`}
              onClick={() => onSelect(r)}
              title={`${r.label} · ${fmt.dateTime(r.created_at)}${r.note ? ` — ${r.note}` : ''}${r.request_text ? ` — “${r.request_text}”` : ''}`}
            >
              <span className="ws-rev-top">
                <span className="mono strong">{r.label}</span>
                <RevSourceChip source={r.source} />
                {isSimulated(r) && <SimulatedBadge />}
                {r.mesh_only && (
                  <span className="chip pending sm" title={t('ws.rev.meshOnlyHint')}>
                    <FileWarning aria-hidden /> {t('rev.meshOnly')}
                  </span>
                )}
                {r.id === latestId && <span className="ws-rev-latest">{t('ws.rev.latest')}</span>}
              </span>
              <span className="ws-rev-meta faint">{fmt.relative(r.created_at)}</span>
              {/* Independent geometry check of the revision's STEP (AI revisions); separate from acceptance. */}
              {r.check_status ? (
                <span className="ws-rev-check">
                  <GeomCheckChip status={r.check_status} short />
                </span>
              ) : null}
            </button>
          );
        })}
        {canDesign && (
          <button type="button" className={`ws-rev ws-rev-new ${blank ? 'active' : ''}`} onClick={onBlank} title={t('ws.rev.blankHint')} aria-selected={blank} role="option">
            <span className="ws-rev-top">
              <Plus aria-hidden /> <span>{t('ws.rev.blank')}</span>
            </span>
            <span className="ws-rev-meta faint">{t('ws.rev.blankSub')}</span>
          </button>
        )}
      </div>
      <div className="ws-revbar-act">
        {ai &&
          (sel ? (
            <></>
          ) : (
            <></>
          ))}
        {sel && (
          <>
            <button type="button" className="btn sm" disabled={!sel.step_sha || busy === 'step'} onClick={() => sel.step_sha && void download(sel.step_sha, 'step')} title={sel.step_sha ? t('ws.rev.dlStep') : t('ws.rev.noStep')}>
              <Download /> STEP
            </button>
            <button type="button" className="btn sm" disabled={!sel.glb_sha || busy === 'glb'} onClick={() => sel.glb_sha && void download(sel.glb_sha, 'glb')} title={t('ws.rev.dlGlb')}>
              <Download /> GLB
            </button>
          </>
        )}
        {canDesign && (
          <button type="button" className="btn sm" onClick={onImport}>
            <FileInput /> {t('ws.import.open')}
          </button>
        )}
      </div>
    </div>
  );
}
