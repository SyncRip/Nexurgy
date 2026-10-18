// Narrow drawing operations remain available until the main tools cover every saved-output path.
import { FileSearch,Wand2 } from 'lucide-react';
import type { ProjectFile } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistant } from './AssistantProvider';
import { assetVersionRef,fileVersionRef,type AiRefWithProject } from './refs';

function OperationEntry({ input, scope, label, blocked, compact, reconstruct }: { input: AiRefWithProject | null; scope: string; label: string; blocked?: string | null; compact?: boolean; reconstruct?: boolean }) {
  const assistant = useAssistant();
  return <button type="button" className={`btn sm ${compact ? 'icon ghost' : ''}`} title={blocked || `${label} · ${scope}`} aria-label={`${label} · ${scope}`} disabled={!!blocked || !input}
    onClick={() => input && void assistant.addContext(input, { asTarget: true, text: label })}>{reconstruct ? <Wand2 /> : <FileSearch />}{!compact && label}</button>;
}
export function DrawingFileAiActions({ projectId, file, analyzeBlocked }: { projectId: string; file: Pick<ProjectFile, 'name' | 'versions' | 'archived_at'>; analyzeBlocked?: string | null }) {
  const { t } = useI18n(); const v = file.versions[0];
  return v ? <OperationEntry input={fileVersionRef(projectId, v.id)} scope={`${file.name} · v${v.version}`} label={t('ar.dw.analyze')} blocked={file.archived_at ? t('ar.dw.archived') : analyzeBlocked} /> : null;
}
export function CadDrawingFileAiActions({ projectId, file, blocked, compact }: { projectId: string; file: Pick<ProjectFile, 'name' | 'versions' | 'archived_at'>; blocked?: string | null; compact?: boolean }) {
  const { t } = useI18n(); const v = file.versions[0];
  return v ? <OperationEntry input={fileVersionRef(projectId, v.id)} scope={`${file.name} · v${v.version}`} label={t('cad.dw.reconstruct')} blocked={file.archived_at ? t('ar.dw.archived') : blocked} compact={compact} reconstruct /> : null;
}
export function DrawingAssetReconstructButton({ projectId, assetVersionId, scope, blocked, compact }: { projectId: string; assetVersionId: string | null; scope: string; blocked?: string | null; compact?: boolean }) {
  const { t } = useI18n();
  return <OperationEntry input={assetVersionId ? assetVersionRef(projectId, assetVersionId) : null} scope={scope} label={t('cad.dw.reconstruct')} blocked={blocked ?? (assetVersionId ? null : t('cad.dw.reconstructNoAsset'))} compact={compact} reconstruct />;
}
