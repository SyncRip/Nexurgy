import { useEffect, useState } from 'react';
import { getDrawingVersion, type VersionFull } from '../../api/drafting';
import { useI18n } from '../../i18n/I18nProvider';
import { DraftingCanvas } from './DraftingCanvas';
import './drafting.css';

const ignore = () => undefined;
const noRequests = { text: async () => null, number: async () => null };
const noSnaps = new Set<never>();

/** Fetch only the result's immutable version. Permissions and source data remain server-authoritative. */
export default function SavedDrawingPreview({ documentId, versionId, label }: { documentId: string; versionId: string; label: string }) {
  const { t } = useI18n();
  const key = `${documentId}:${versionId}`;
  const [state, setState] = useState<{ key: string; version?: VersionFull; failed?: boolean }>({ key: '' });
  useEffect(() => {
    const controller = new AbortController();
    void getDrawingVersion(documentId, versionId, { signal: controller.signal }).then((version) => {
      if (controller.signal.aborted) return;
      setState(version.id === versionId && version.document_id === documentId ? { key, version } : { key, failed: true });
    }).catch(() => { if (!controller.signal.aborted) setState({ key, failed: true }); });
    return () => controller.abort();
  }, [documentId, versionId, key]);
  const current = state.key === key ? state : null;
  return current?.version ? <DraftingCanvas key={key} content={current.version} versionId={versionId}
    tool="pan" readOnly previewLabel={t('core.result.preview', { version: label })} selection={[]} onSelect={ignore}
    onCommand={ignore} onCalibrate={ignore} requests={noRequests} snapKinds={noSnaps}
    activeLayer="0" activeBlock={null} fitKey={0} />
    : <span className="faint small">{t(current?.failed ? 'core.result.noPreview' : 'common.loading')}</span>;
}
