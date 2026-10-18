import { Box, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../../i18n/I18nProvider';
import { useTheme } from '../../prefs/ThemeProvider';
import { loadSavedAssemblyPreview, type AssemblyPreviewSnapshot, type SavedAssemblyTarget } from './assemblyPreviewSnapshot';

/** Static saved-version preview. The surrounding result link owns navigation, never the temporary canvas. */
export default function SavedAssemblyPreview({ assemblyId, revisionId, label }: SavedAssemblyTarget & { label: string }) {
  const { t } = useI18n();
  const { resolved } = useTheme();
  const host = useRef<HTMLDivElement>(null);
  const key = `${assemblyId}:${revisionId}:${resolved}`;
  const [state, setState] = useState<{ key: string; snapshot?: AssemblyPreviewSnapshot; failed?: boolean }>({ key: '' });
  useEffect(() => {
    if (!host.current) return;
    const controller = new AbortController();
    void loadSavedAssemblyPreview({ assemblyId, revisionId }, host.current, resolved, controller.signal).then((snapshot) => {
      if (!controller.signal.aborted) setState({ key, snapshot });
    }).catch(() => { if (!controller.signal.aborted) setState({ key, failed: true }); });
    return () => controller.abort();
  }, [assemblyId, revisionId, resolved, key]);
  const current = state.key === key ? state : null;
  const snapshot = current?.snapshot;
  return <div className="as-assembly-preview">
    <div className="as-cad-preview-host" ref={host} aria-hidden />
    {snapshot?.status === 'ready' ? <>
      <img src={snapshot.url} alt={t('core.result.preview', { version: label })} />
      {snapshot.missing > 0 && <span className="as-cad-preview-status as-cad-preview-note small" role="status">
        <TriangleAlert aria-hidden />{t('core.result.assemblyIncomplete', snapshot)}
      </span>}
    </> : <span className="as-cad-preview-status faint small" role="status">
      {current?.failed || snapshot?.status === 'unavailable' ? <TriangleAlert aria-hidden /> : snapshot?.status === 'empty' ? <Box aria-hidden /> : <span className="spinner" aria-hidden />}
      {t(current?.failed ? 'core.result.assemblyUnavailable' : snapshot?.status === 'empty' ? 'core.result.assemblyEmpty'
        : snapshot?.status === 'unavailable' ? 'core.result.assemblyIncomplete' : 'core.result.assemblyLoading', snapshot)}
    </span>}
  </div>;
}
