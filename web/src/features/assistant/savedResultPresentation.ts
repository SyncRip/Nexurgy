import type { AiOperation, AiResultItem, CadResult } from '../../api/assistant';
import type { RunProgress } from '../../api/progress';
import type { DrawingBrief } from '../../api/types';
import { drawingResultIdentity } from '../progress/drawingResults';
import type { RepresentedDrawing, RepresentedPartRevision } from '../progress/ProgressResults';
import { itemsOf } from './orchestrator';

const assetType = { part_model: 'part_model', part_drawing: 'drawing', assembly: 'assembly_model', drawing_document: 'drawing_document' } as const;

export interface SavedDrawingPresentation extends RepresentedDrawing { brief: DrawingBrief }

/** Only whole, project-scoped immutable outputs can share a delivery surface. Labels and paths are not identity. */
export function matchesSavedResult(result: CadResult, item: AiResultItem, projectId: string | null | undefined): boolean {
  const ref = item.ref;
  if (!projectId || ref.project_id !== projectId || !result.object_id || !result.version_id
    || item.asset_type !== assetType[result.object_type] || (ref.asset_type && ref.asset_type !== item.asset_type)
    || ref.selection || ref.instance || ref.entities || ref.field || ref.excerpt || ref.node) return false;
  if (result.asset_version_id && item.asset_version_id && result.asset_version_id !== item.asset_version_id) return false;
  switch (ref.kind) {
    case 'part_revision':
      return result.object_type === 'part_model' && ref.part_id === result.object_id && ref.object_id === result.version_id;
    case 'assembly_revision':
      return result.object_type === 'assembly' && ref.assembly_id === result.object_id && ref.object_id === result.version_id;
    case 'drawing_document_version':
      return result.object_type === 'drawing_document' && ref.document_id === result.object_id && ref.object_id === result.version_id;
    case 'asset_version':
      // An exact asset version identifies its immutable domain output. Reject any conflicting owner evidence.
      return !!result.asset_version_id && item.asset_version_id === result.asset_version_id && ref.object_id === result.asset_version_id
        && (!(result.object_type === 'part_model' || result.object_type === 'part_drawing') || ref.part_id === result.object_id)
        && (!ref.assembly_id || (result.object_type === 'assembly' && ref.assembly_id === result.object_id))
        && (!ref.document_id || (result.object_type === 'drawing_document' && ref.document_id === result.object_id));
    default:
      return false;
  }
}

/** A gallery replaces the thumbnail only with this operation's exact drawing, source and package. */
function primaryDrawing(op: AiOperation, primary: AiResultItem | null, projectId: string | null | undefined,
  progress: RunProgress | null | undefined): SavedDrawingPresentation | null {
  const saved = op.result?.cad_result;
  const runId = op.run?.id;
  if (!primary || saved?.object_type !== 'part_drawing' || op.result?.kind !== 'drawing'
    || !runId || primary.run_id !== runId || progress?.run_id !== runId || !primary.node_id
    || (op.part && op.part.id !== saved.object_id)) return null;
  const matches = (progress.drawings ?? []).filter((drawing) => drawing.kind === 'drawing' && drawing.id === saved.version_id);
  const drawing = matches.find((candidate) => candidate.node_id === primary.node_id);
  const source = primary.base;
  if (!drawing || drawing.kind !== 'drawing' || new Set(matches.map(drawingResultIdentity)).size !== 1 || !drawing.model_revision_id
    || source?.kind !== 'part_revision' || source.project_id !== projectId || source.part_id !== saved.object_id
    || source.object_id !== drawing.model_revision_id || (source.asset_type && source.asset_type !== 'part_model')
    || source.selection || source.instance || source.entities
    || source.field || source.excerpt || source.node) return null;
  const briefs = (op.result.drawings ?? []).filter((brief) => brief.drawing_id === drawing.id);
  const brief = briefs.length === 1 ? briefs[0] : null;
  if (!brief?.manifest_sha256 || brief.manifest_sha256 !== drawing.manifest_sha256) return null;
  return { runId, drawing, brief };
}

/** Preserve unmatched/ambiguous history and operation ownership; this is not deduplication across operations. */
export function savedResultPresentation(op: AiOperation, projectId: string | null | undefined, progress?: RunProgress | null): {
  primary: AiResultItem | null; remaining: AiResultItem[]; representedParts: RepresentedPartRevision[];
  primaryDrawing: SavedDrawingPresentation | null;
} {
  const items = itemsOf(op);
  const saved = op.result?.cad_result;
  const matches = saved && op.status === 'succeeded' ? items.filter((item) => matchesSavedResult(saved, item, projectId)) : [];
  const primary = matches.length === 1 ? matches[0] : null;
  return {
    primary,
    remaining: primary ? items.filter((item) => item !== primary) : items,
    representedParts: primary && saved?.object_type === 'part_model' && op.run?.id && primary.run_id === op.run.id
      ? [{ runId: op.run.id, partId: saved.object_id, revisionId: saved.version_id }] : [],
    primaryDrawing: primaryDrawing(op, primary, projectId, progress),
  };
}
