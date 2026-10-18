import { getAssemblyDrawing } from '../../api/assemblyDrawings';
import { getDrawing } from '../../api/endpoints';
import type { ProgressDrawing } from '../../api/progress';
import { assemblyRevisionPath, partOutputPath } from '../../app/versionLinks';
import { drawingPages } from '../cad/status';
import type { ResultPreviewTarget } from './resultPreview';

export type DrawingResultIssue = 'partial' | 'checksFailed' | 'dwgFailed' | 'dwgUnavailable' | 'unverified';
export interface DrawingResultPage {
  key: string;
  index: number;
  title: string;
  status: string;
  preview: ResultPreviewTarget | null;
  path?: string;
}
export interface DrawingResults {
  identity: string;
  label: string;
  sourceLabel: string;
  sourcePath: string | null;
  issues: DrawingResultIssue[];
  pages: DrawingResultPage[];
}

export function drawingResultIdentity(drawing: ProgressDrawing): string {
  return `${drawing.kind}/${drawing.id}/${drawing.kind === 'drawing' ? drawing.model_revision_id ?? '' : drawing.assembly_revision_id}/${drawing.manifest_sha256 ?? ''}`;
}

/** Resolve only the committed output named by progress. Pages are never inferred from current heads or run lists. */
export async function loadDrawingResults(drawing: ProgressDrawing, signal: AbortSignal): Promise<DrawingResults> {
  const identity = drawingResultIdentity(drawing);
  if (drawing.kind === 'assembly_drawing') {
    const saved = await getAssemblyDrawing(drawing.id, { signal });
    signal.throwIfAborted();
    if (saved.id !== drawing.id || saved.assembly_revision_id !== drawing.assembly_revision_id || saved.manifest_sha256 !== drawing.manifest_sha256) {
      throw new Error('Saved drawing identity mismatch');
    }
    const issues: DrawingResultIssue[] = [];
    if (saved.completeness !== 'complete') issues.push('partial');
    if (saved.check_status === 'failed' || saved.verification?.check_status === 'failed') issues.push('checksFailed');
    else if (saved.verification?.unverified || saved.check_status === 'skipped_turbo') issues.push('unverified');
    if (saved.pages.some((page) => page.dwg?.status === 'failed' || page.errors?.some((error) => error.format === 'dwg'))) issues.push('dwgFailed');
    else if (saved.pages.some((page) => !page.files.some((file) => file.format === 'dwg'))) issues.push('dwgUnavailable');
    return {
      identity, label: saved.label, sourceLabel: saved.rev_label,
      sourcePath: assemblyRevisionPath(saved.assembly_id, saved.assembly_revision_id), issues,
      pages: saved.pages.map((page) => {
        const svg = page.files.find((file) => file.format === 'svg');
        return { key: page.key, index: page.index, title: page.title, status: page.status,
          preview: svg ? { kind: 'assembly_svg', drawingId: saved.id, pageKey: page.key, sha: svg.sha256 } : null };
      }),
    };
  }
  const saved = await getDrawing(drawing.id, { signal });
  signal.throwIfAborted();
  if (saved.id !== drawing.id || saved.model_revision_id !== drawing.model_revision_id
    || (drawing.manifest_sha256 && saved.manifest_sha256 !== drawing.manifest_sha256)) throw new Error('Saved drawing identity mismatch');
  const issues: DrawingResultIssue[] = [];
  if (saved.checks?.annotation?.status === 'failed' || saved.verification?.check_status === 'failed') issues.push('checksFailed');
  else if (saved.verification?.unverified || saved.checks?.annotation?.status === 'skipped_turbo') issues.push('unverified');
  const dwg = saved.dwg_status ?? saved.checks?.dwg?.status;
  if (dwg === 'failed') issues.push('dwgFailed');
  else if (dwg === 'unavailable') issues.push('dwgUnavailable');
  return {
    identity, label: '', sourceLabel: saved.rev_no ? `R${saved.rev_no}` : '',
    sourcePath: saved.model_revision_id ? partOutputPath(saved.part_id, 'model', saved.model_revision_id) : null,
    issues,
    pages: drawingPages(saved).map((page) => {
      const svg = page.files.find((file) => file.format === 'svg');
      return { key: String(page.index), index: page.index, title: page.title, status: svg ? 'saved' : 'missing',
        path: partOutputPath(saved.part_id, 'drawings', saved.id, { page: page.index }),
        preview: svg ? { kind: 'drawing_svg', sha: svg.sha256 } : null };
    }),
  };
}
