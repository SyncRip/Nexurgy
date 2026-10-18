import { describe, expect, it } from 'vitest';
import type { AssetUsages, AssetVersion } from '../../api/assets';
import type { SpecVersionBrief } from '../../api/spec';
import type { Project } from '../../api/types';
import { contentPreview, fileState, isConverted } from './catalog';
import {
  ACTION_CHECKS,
  contentOf,
  defaultSaveLocation,
  detailCaps,
  fromAssetVersion,
  fromFileVersion,
  fromSpecVersion,
  pagePath,
  relationCounts,
  resolveVersion,
  saveTargets,
  usageTotal,
  versionOrigin,
  versionParam,
  withReturnTo,
  type ActionContext,
} from './detailModel';
import type { CatalogItem, CatalogRef } from './types';

const P = '11111111-1111-4111-8111-111111111111';
const RETURN = `/resources/files?project=${P}&item=asset%3Aa1`;

function av(over: Partial<AssetVersion> & { summary?: Record<string, unknown>; part_id?: string | null } = {}): AssetVersion {
  const { summary = {}, part_id = null, ...rest } = over;
  return {
    id: 'v1',
    asset_id: 'a1',
    asset_type: 'part_model',
    asset_name: 'Bracket',
    project_id: P,
    version_no: 1,
    label: 'R1',
    content_hash: 'h',
    availability: 'ready',
    publish_state: 'draft',
    labels: [],
    parent_version_id: null,
    created_at: '2026-10-10T00:00:00Z',
    created_by: 'u1',
    node_run_id: null,
    run_id: null,
    target: { kind: 'model_revision', id: 'rev1', part_id, summary },
    engineering: null,
    dependencies: [],
    dependents: [],
    ...rest,
  } as AssetVersion;
}

const assetRef = (assetType: string, extra: Partial<Extract<CatalogRef, { kind: 'asset' }>> = {}): CatalogRef => ({ kind: 'asset', assetId: 'a1', assetType, fileId: null, drawingDocumentId: null, ...extra });
const item = (ref: CatalogRef, openPath: string | null = null): Pick<CatalogItem, 'ref' | 'openPath'> => ({ ref, openPath });

const spec = (over: Partial<SpecVersionBrief> = {}): SpecVersionBrief =>
  ({ id: 's1v2', spec_id: 's1', version_no: 2, origin: 'answer', status: 'ready', execution_policy: 'standard', simulated: false, parent_version_id: null, counts: {}, error: null, created_by: 'u1', created_at: null, finished_at: null, ...over }) as SpecVersionBrief;

const project = (my_role: Project['my_role'], status: Project['status'] = 'active', id = P): Project => ({ id, code: 'P', name: 'P', my_role, status }) as Project;

describe('capabilities follow the real identity', () => {
  it('maps each ref kind to its version source, content and mutation APIs', () => {
    expect(detailCaps(assetRef('document', { fileId: 'f1' }))).toEqual({ versions: 'asset', content: 'file', fileId: 'f1', target: { api: 'file', id: 'f1' }, publish: true });
    expect(detailCaps(assetRef('drawing_document', { drawingDocumentId: 'd1' }))).toMatchObject({ content: 'page', fileId: null, target: { api: 'drawing_document', id: 'd1' }, publish: true });
    expect(detailCaps(assetRef('part_model'))).toMatchObject({ content: 'summary', target: null, publish: true });
    expect(detailCaps(assetRef('drawing')).content).toBe('svg');
    expect(detailCaps(assetRef('delivery_manifest')).content).toBe('page');
    expect(detailCaps({ kind: 'spec', specId: 's1' })).toEqual({ versions: 'spec', content: 'spec', fileId: null, target: null, publish: false });
    expect(detailCaps({ kind: 'file', fileId: 'f2' })).toEqual({ versions: 'file', content: 'file', fileId: 'f2', target: { api: 'file', id: 'f2' }, publish: false });
    expect(detailCaps({ kind: 'drawing_document', documentId: 'd2' })).toMatchObject({ versions: 'none', target: { api: 'drawing_document', id: 'd2' }, publish: false });
  });
});

describe('versions', () => {
  const vs = [fromAssetVersion(av({ id: 'v3', version_no: 3, label: 'R3' })), fromAssetVersion(av({ id: 'v2', version_no: 2, label: 'R2' }))];

  it('null is the latest; an exact id is that version; a foreign id is an explicit miss (no fallback)', () => {
    expect(resolveVersion(undefined, 'v2')).toEqual({ selected: null, latest: null, missing: false, isLatest: false });
    expect(resolveVersion(vs, null)).toMatchObject({ missing: false, isLatest: true });
    expect(resolveVersion(vs, null).selected?.id).toBe('v3');
    expect(resolveVersion(vs, 'v2')).toMatchObject({ missing: false, isLatest: false });
    expect(resolveVersion(vs, 'v3').isLatest).toBe(true);
    expect(resolveVersion(vs, 'other')).toMatchObject({ selected: null, missing: true, isLatest: false });
    expect(resolveVersion([], 'other').missing).toBe(true);
    expect(resolveVersion([], null)).toMatchObject({ selected: null, missing: false });
  });

  it('accepts the exact row id a version registers (an uploaded file version), nothing else', () => {
    const docs = [fromAssetVersion(av({ id: 'av2', version_no: 2, asset_type: 'document', target: { kind: 'file_version', id: 'fv2', part_id: null, summary: {} } }))];
    expect(resolveVersion(docs, 'fv2').selected?.id).toBe('av2');
    expect(resolveVersion(docs, 'fv1').missing).toBe(true);
  });

  it('picking the latest clears the URL version', () => {
    expect(versionParam(vs, 'v3')).toBeNull();
    expect(versionParam(vs, 'v2')).toBe('v2');
  });

  it('normalises file and spec versions', () => {
    const fv = fromFileVersion({ id: 'fv', file_id: 'f', version: 2, blob_sha256: 'sha', original_name: 'a.pdf', note: '', created_at: 't', size: 1, content_type: null }, 'drawing_pdf');
    expect(fv).toMatchObject({ label: 'v2', note: null, stored: { availability: null, file: { sha: 'sha', name: 'a.pdf', kind: 'drawing_pdf' }, svg: null, converted: false } });
    expect(fromSpecVersion(spec())).toMatchObject({ id: 's1v2', label: 'v2', versionNo: 2 });
  });
});

describe('content and preview', () => {
  it('classifies stored originals by what the inline viewer renders and the recorded kind', () => {
    expect(fileState('a.PDF', 'other', false)).toBe('preview');
    expect(fileState('photo.png', 'drawing_image', false)).toBe('preview');
    expect(fileState('notes.md', 'document', false)).toBe('preview');
    expect(fileState('notes.html', 'document', false)).toBe('download');
    expect(fileState('scan.tif', 'drawing_image', false)).toBe('download');
    expect(fileState('part.step', 'model_step', false)).toBe('convert');
    expect(fileState('plan.dwg', 'other', false)).toBe('convert');
    expect(fileState('weird.bin', 'model_mesh', false)).toBe('convert');
    expect(fileState('bom.xlsx', 'document', false)).toBe('download');
    expect(fileState('delivery.zip', 'other', false)).toBe('download');
    // A converted original is download-only: the system object it became is downstream.
    expect(fileState('part.step', 'model_step', true)).toBe('download');
    expect(isConverted({ dependents: [{ version_id: 'r', relation: 'imported_from' }] })).toBe(true);
    expect(isConverted({ dependents: [{ version_id: 'q', relation: 'derived_from' }] })).toBe(false);
  });

  it('shows the stored file, the generated SVG, a summary or the failed / building state', () => {
    const doc = fromAssetVersion(av({ asset_type: 'document', label: 'v1', summary: { original_name: 'std.pdf', blob_sha256: 'sha', kind: 'drawing_pdf' } }));
    expect(contentOf({ content: 'file' }, doc)).toEqual({ kind: 'blob', sha: 'sha', name: 'std.pdf', state: 'preview', converted: false });
    expect(contentOf({ content: 'svg' }, fromAssetVersion(av({ asset_type: 'drawing', label: 'D2', summary: { svg_sha: 'svg' } })))).toEqual({ kind: 'blob', sha: 'svg', name: 'Bracket-D2.svg', state: 'preview', converted: false });
    expect(contentOf({ content: 'svg' }, fromAssetVersion(av({ asset_type: 'drawing', summary: {} })))).toEqual({ kind: 'summary' });
    expect(contentOf({ content: 'summary' }, fromAssetVersion(av({ availability: 'failed' })))).toEqual({ kind: 'failed' });
    expect(contentOf({ content: 'summary' }, fromAssetVersion(av({ availability: 'building' })))).toEqual({ kind: 'none' });
    expect(contentOf({ content: 'page' }, null)).toEqual({ kind: 'none' });
    // A spec version that failed / is still extracting says so, like the list.
    expect(contentOf({ content: 'spec' }, fromSpecVersion(spec({ status: 'failed' })))).toEqual({ kind: 'failed' });
    expect(contentOf({ content: 'spec' }, fromSpecVersion(spec()))).toEqual({ kind: 'spec' });
  });

  it('gives the list and the detail one preview state per version', () => {
    const versions = [
      av({ asset_type: 'document', summary: { original_name: 'notes.html', blob_sha256: 's', kind: 'document' } }),
      av({ asset_type: 'document', summary: { original_name: 'std.pdf', blob_sha256: 's', kind: 'drawing_pdf' } }),
      av({ asset_type: 'drawing', summary: { svg_sha: 'svg' } }),
      av({ asset_type: 'quote' }),
      av({ asset_type: 'part_model', availability: 'failed' }),
    ];
    const detail = versions.map((v) => contentPreview(contentOf(detailCaps(assetRef(v.asset_type)), fromAssetVersion(v))));
    expect(detail).toEqual(['download', 'preview', 'preview', 'page', 'failed']);
  });
});

describe('professional pages', () => {
  it('opens the exact version, never a newer one', () => {
    const model = fromAssetVersion(av({ part_id: 'p1', target: { kind: 'model_revision', id: 'rev1', part_id: 'p1', summary: {} } }));
    expect(pagePath(item(assetRef('part_model'), '/parts/p1/model'), model, false, RETURN)).toBe('/parts/p1/model?rev=rev1');

    const dd = fromAssetVersion(av({ id: 'dv1', asset_type: 'drawing_document' as AssetVersion['asset_type'], summary: { document_id: 'd1' } }));
    const ddItem = item(assetRef('drawing_document', { drawingDocumentId: 'd1' }), '/drafting/d1');
    expect(pagePath(ddItem, dd, true, RETURN)).toBe('/drafting/d1');
    expect(pagePath(ddItem, dd, false, RETURN)).toBe(`/assets/a1?version=dv1&return_to=${encodeURIComponent(RETURN)}`);

    const asm = fromAssetVersion(av({ asset_type: 'assembly_model' as AssetVersion['asset_type'], target: { kind: 'model_revision', id: 'ar1', part_id: null, summary: { assembly_id: 'as1' } } }));
    expect(pagePath(item(assetRef('assembly_model'), '/assemblies/as1'), asm, false, RETURN)).toBe('/assemblies/as1/structure?rev=ar1');

    expect(pagePath(item({ kind: 'spec', specId: 's1' }), fromSpecVersion(spec()), true, RETURN)).toBe('/specs/s1?version=s1v2');
    expect(pagePath(item({ kind: 'file', fileId: 'f1' }), null, true, RETURN)).toBeNull();
    expect(pagePath(item({ kind: 'drawing_document', documentId: 'd2' }), null, true, RETURN)).toBe('/drafting/d2');
  });

  it('adds return_to only where the page honours it', () => {
    expect(withReturnTo('/assets/a1', RETURN)).toBe(`/assets/a1?return_to=${encodeURIComponent(RETURN)}`);
    expect(withReturnTo('/parts/p1/model?rev=r', RETURN)).toBe('/parts/p1/model?rev=r');
    expect(withReturnTo('/assets/a1', '')).toBe('/assets/a1');
  });
});

describe('provenance and relations', () => {
  it('reads how a version was made from its own records only', () => {
    expect(versionOrigin(fromAssetVersion(av({ run_id: 'r1' })))).toEqual({ kind: 'run', runId: 'r1' });
    expect(versionOrigin(fromAssetVersion(av({ asset_type: 'document' })))).toEqual({ kind: 'upload' });
    const written = fromAssetVersion(av({ asset_type: 'document' }));
    expect(versionOrigin(written, [written.asset!.id])).toEqual({ kind: 'task' }); // an agent's write registration, no run id
    expect(versionOrigin(fromAssetVersion(av({ summary: { source: 'import' } })))).toEqual({ kind: 'label', key: 'revSource.import', raw: 'import' });
    expect(versionOrigin(fromAssetVersion(av({ asset_type: 'quote' })))).toEqual({ kind: 'unknown' });
    expect(versionOrigin(fromSpecVersion(spec()))).toEqual({ kind: 'label', key: 'spec.origin.answer', raw: 'answer' });
    expect(versionOrigin(fromFileVersion({ id: 'x', file_id: 'f', version: 1, blob_sha256: 's', original_name: 'a', note: '', created_at: 't', size: null, content_type: null }, null))).toEqual({ kind: 'upload' });
  });

  it('counts the previous revision upstream once and totals paged usages', () => {
    const edge = { version_id: 'p', relation: 'derived_from' };
    expect(relationCounts({ dependencies: [], dependents: [edge], parent_version_id: 'p' })).toEqual({ upstream: 1, downstream: 1 });
    expect(relationCounts({ dependencies: [edge], dependents: [], parent_version_id: 'p' })).toEqual({ upstream: 1, downstream: 0 });
    const u = { baselines: [{}], deliverables: [], submissions: [], run_inputs: [{}, {}], run_outputs: [], approval_requests: [], workflow_resources: [{}], knowledge_items: [], pagination: { workflow_resources: { total: 7, limit: 20, offset: 0 } } } as unknown as AssetUsages;
    expect(usageTotal(u)).toBe(10);
  });
});

describe('actions: wired or disabled with the reason', () => {
  const ctx = (over: Partial<ActionContext> = {}): ActionContext => ({
    caps: detailCaps(assetRef('document', { fileId: 'f1' })),
    project: project('designer'),
    archived: null,
    version: fromAssetVersion(av({ asset_type: 'document', publish_state: 'published' })),
    ...over,
  });
  const doc = item(assetRef('document', { fileId: 'f1' }));

  it('follows roles and project status; the server edits archived files, so archiving blocks no edit', () => {
    expect(ACTION_CHECKS.newVersion(ctx(), doc)).toEqual({ ok: true });
    expect(ACTION_CHECKS.newVersion(ctx({ project: project('viewer') }), doc)).toEqual({ ok: false, why: 'role_design' });
    expect(ACTION_CHECKS.meta(ctx({ project: project('owner', 'archived') }), doc)).toEqual({ ok: false, why: 'project_archived' });
    expect(ACTION_CHECKS.meta(ctx({ archived: 'self' }), doc)).toEqual({ ok: true });
    expect(ACTION_CHECKS.newVersion(ctx({ archived: 'part' }), doc)).toEqual({ ok: true });
    expect(ACTION_CHECKS.delete(ctx(), doc)).toEqual({ ok: false, why: 'no_delete_api' });
  });

  it('restores only the item\'s own archive; a row hidden by its archived part says so', () => {
    expect(ACTION_CHECKS.archive(ctx({ archived: 'self' }), doc)).toEqual({ ok: true });
    expect(ACTION_CHECKS.archive(ctx(), doc)).toEqual({ ok: true });
    expect(ACTION_CHECKS.archive(ctx({ archived: 'part' }), doc)).toEqual({ ok: false, why: 'part_archived' });
    const model = { caps: detailCaps(assetRef('part_model')), version: fromAssetVersion(av()) };
    expect(ACTION_CHECKS.archive(ctx({ ...model, archived: 'part' }), item(assetRef('part_model')))).toEqual({ ok: false, why: 'part_archived' });
    expect(ACTION_CHECKS.archive(ctx(model), item(assetRef('part_model')))).toEqual({ ok: false, why: 'no_archive_api' });
  });

  it('deprecates only a published version: a draft cannot go back to draft once deprecated', () => {
    const state = (publish_state: AssetVersion['publish_state']) => ACTION_CHECKS.publish(ctx({ version: fromAssetVersion(av({ asset_type: 'document', publish_state })) }), doc);
    expect([state('draft'), state('published'), state('deprecated')]).toEqual([{ ok: false, why: 'draft' }, { ok: true }, { ok: true }]);
  });

  it('renames a drawing document through its own API', () => {
    const dd = ctx({ caps: detailCaps(assetRef('drawing_document', { drawingDocumentId: 'd1' })) });
    expect(ACTION_CHECKS.meta(dd, item(assetRef('drawing_document', { drawingDocumentId: 'd1' })))).toEqual({ ok: true });
    expect(ACTION_CHECKS.meta(ctx({ caps: detailCaps({ kind: 'drawing_document', documentId: 'd2' }) }), item({ kind: 'drawing_document', documentId: 'd2' }))).toEqual({ ok: true });
  });

  it('uses the publish role of the asset type and has no fake APIs', () => {
    const quote = ctx({ caps: detailCaps(assetRef('quote')), version: fromAssetVersion(av({ asset_type: 'quote', publish_state: 'published' })) });
    expect(ACTION_CHECKS.publish(quote, item(assetRef('quote')))).toEqual({ ok: false, why: 'role_quote' });
    expect(ACTION_CHECKS.publish({ ...quote, project: project('quoter') }, item(assetRef('quote')))).toEqual({ ok: true });
    const specCtx = ctx({ caps: detailCaps({ kind: 'spec', specId: 's1' }), version: fromSpecVersion(spec()) });
    const specItem = item({ kind: 'spec', specId: 's1' });
    expect(ACTION_CHECKS.publish(specCtx, specItem)).toEqual({ ok: false, why: 'no_publish_api' });
    expect(ACTION_CHECKS.archive(specCtx, specItem)).toEqual({ ok: false, why: 'no_archive_api' });
    expect(ACTION_CHECKS.meta(specCtx, specItem)).toEqual({ ok: false, why: 'no_meta_api' });
    expect(ACTION_CHECKS.edit({ ...specCtx, project: project('process') }, specItem)).toEqual({ ok: true });
    expect(ACTION_CHECKS.publish(ctx({ version: null }), doc)).toEqual({ ok: false, why: 'no_version' });
  });
});

describe('add material: save location', () => {
  const ps = [project('designer', 'active', 'a'), project('process', 'active', 'b'), project('owner', 'archived', 'c'), project('viewer', 'active', 'd')];

  it('lists only active projects where the action is allowed', () => {
    expect(saveTargets(ps, 'upload').map((p) => p.id)).toEqual(['a']);
    expect(saveTargets(ps, 'module').map((p) => p.id)).toEqual(['b']);
    expect(saveTargets(ps, 'spec').map((p) => p.id)).toEqual(['a', 'b']);
  });

  it('defaults to the scope project only when allowed; never across projects', () => {
    const targets = saveTargets(ps, 'upload');
    expect(defaultSaveLocation({ kind: 'project', projectId: 'a' }, targets)).toBe('a');
    expect(defaultSaveLocation({ kind: 'project', projectId: 'b' }, targets)).toBe('');
    expect(defaultSaveLocation({ kind: 'all' }, targets)).toBe('');
  });
});
