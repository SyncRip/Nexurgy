import { request } from './client';
import type { ImportSource } from './types';
export type TemplateReferenceKind = 'file_version' | 'model_revision' | 'drawing' | 'engineering_spec_version';
export interface TemplateReferenceMap { kind: TemplateReferenceKind; source_id: string; target_id: string }
export interface TemplateImportBody { client_request_id: string; source_template_version_id: string; expected_source_hash: string; name?: string; reference_map: TemplateReferenceMap[] }
export interface TemplateImportResult { workflow_id: string; template_version_id: string; project_id: string; version: number; version_no: number; reused: boolean; import_source: ImportSource }
export const importProjectTemplate = (projectId: string, body: TemplateImportBody) => request<TemplateImportResult>(`/projects/${encodeURIComponent(projectId)}/templates/import`, { method: 'POST', body });
