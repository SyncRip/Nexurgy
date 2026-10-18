import { describe, expect, it } from 'vitest';
import type { Asset, AssetEdge, AssetVersion } from '../../api/assets';
import type { DrawingDocument } from '../../api/drafting';
import type { WorkflowResourceItem } from '../../api/resources';
import type { EngineeringSpec } from '../../api/spec';
import type { Part, ProjectFile } from '../../api/types';
import { buildProjectCatalog, catalogCounts, EMPTY_FILTER, filterCatalog, findCatalogItem, formalBesideLatest, type ProjectSources, type TaskOutputs } from './catalog';
import { taskOutputsOf } from './sources';

const P1 = { id: 'p1', code: 'P1', name: 'Project one' };
const P2 = { id: 'p2', code: 'P2', name: 'Project two' };

let n = 0;
const id = (prefix: string) => `${prefix}-${++n}`;

function version(type: string, summary: Record<string, unknown>, extra: Partial<AssetVersion> = {}): AssetVersion {
  return {
    id: id('v'),
    asset_id: 'x',
    asset_type: type as AssetVersion['asset_type'],
    asset_name: '',
    project_id: 'p1',
    version_no: 1,
    label: 'v1',
    content_hash: 'h',
    availability: 'ready',
    publish_state: 'draft',
    labels: [],
    parent_version_id: null,
    created_at: '2026-10-01T00:00:00+00:00',
    created_by: 'u',
    node_run_id: null,
    run_id: null,
    target: { kind: 'file_version', id: id('t'), part_id: null, summary },
    engineering: null,
    dependencies: [],
    dependents: [],
    ...extra,
  };
}

function asset(type: string, name: string, v: AssetVersion | null, extra: Partial<Asset> = {}): Asset {
  const a: Asset = {
    id: id('a'),
    project_id: 'p1',
    type: type as Asset['type'],
    name,
    part_id: null,
    part: null,
    file_id: null,
    natural_key: '',
    source_asset_id: null,
    owner_id: null,
    latest_version: v,
    version_count: v?.version_no ?? 0,
    created_at: '2026-09-01T00:00:00+00:00',
    archived_at: null,
    ...extra,
  };
  if (v) v.asset_id = a.id;
  return a;
}

function file(name: string, extra: Partial<ProjectFile> = {}, versionExtra: Partial<ProjectFile['versions'][number]> = {}): ProjectFile {
  const fid = id('f');
  return {
    id: fid,
    project_id: 'p1',
    part_id: null,
    kind: 'document',
    name,
    source_file_id: null,
    created_at: '2026-09-01T00:00:00+00:00',
    archived_at: null,
    versions: [
      { id: id('fv'), file_id: fid, version: 1, blob_sha256: 's', original_name: name, note: '', created_at: '2026-09-02T00:00:00+00:00', size: 1, content_type: 'application/pdf', ...versionExtra },
    ],
    ...extra,
  };
}

/** A document asset that IS the file (Asset.file_id; its target is the file version). */
function documentAsset(f: ProjectFile, assetName = f.name, extra: Partial<AssetVersion> = {}): Asset {
  const fv = f.versions[0];
  const v = version('document', { file_id: f.id, version: fv.version, original_name: fv.original_name, kind: f.kind, blob_sha256: fv.blob_sha256 }, extra);
  v.target.id = fv.id;
  return asset('document', assetName, v, { file_id: f.id, natural_key: `file:${f.id}` });
}

function drawingDocument(title: string, extra: Partial<DrawingDocument> = {}): DrawingDocument {
  return {
    id: id('dd'),
    project_id: 'p1',
    number: 'DR-001',
    title,
    description: '',
    status: 'draft',
    head_version_id: null,
    head_version_no: 0,
    created_by: 'u',
    created_at: '2026-09-01T00:00:00+00:00',
    updated_at: '2026-09-03T00:00:00+00:00',
    archived_at: null,
    head: null,
    ...extra,
  };
}

function spec(title: string, extra: Partial<EngineeringSpec> = {}, status: 'ready' | 'failed' = 'ready', origin: 'create' | 'rule' = 'create'): EngineeringSpec {
  const sid = id('s');
  return {
    id: sid,
    project_id: 'p1',
    number: 1,
    code: 'SP-1',
    title,
    created_by: { id: 'u', name: 'U' },
    created_at: '2026-09-01T00:00:00+00:00',
    updated_at: '2026-09-04T00:00:00+00:00',
    versions_count: 1,
    latest_version: {
      id: id('sv'),
      spec_id: sid,
      version_no: 1,
      origin,
      status,
      execution_policy: 'standard',
      simulated: false,
      parent_version_id: null,
      counts: {} as never,
      error: null,
      created_by: 'u',
      created_at: '2026-09-04T00:00:00+00:00',
      finished_at: null,
    },
    ...extra,
  };
}

const src = (s: Partial<ProjectSources>): ProjectSources => ({ assets: [], files: [], drawingDocuments: [], specs: { items: [], total: 0 }, ...s });

/** A workflow resource row of kind `kind` registered for asset version `v` with `reason` (only the fields the reader uses). */
function resource(v: AssetVersion, kind: WorkflowResourceItem['kind'], reason: string, extra: Partial<WorkflowResourceItem> = {}): WorkflowResourceItem {
  return {
    kind,
    asset_version_id: v.id,
    metadata: { reason },
    asset: { id: v.id, asset_id: v.asset_id, label: v.label },
    task_number: 7,
    task_acceptance: { status: kind === 'formal' ? 'approved' : 'not_applicable', current: true },
    ...extra,
  } as WorkflowResourceItem;
}

const outputs = (o: Partial<TaskOutputs>): TaskOutputs => ({ written: new Map(), formal: new Map(), truncated: false, ...o });

describe('buildProjectCatalog — join by identity', () => {
  it('folds a document asset into its File and shows the File name after a rename', () => {
    const f = file('bracket-spec-renamed.pdf');
    const a = documentAsset(f, 'bracket-spec.pdf'); // asset name kept the upload-time name
    const { items } = buildProjectCatalog(P1, src({ assets: [a], files: [f] }));
    expect(items).toHaveLength(1);
    const [row] = items;
    expect(row.key).toBe(`asset:${a.id}`);
    expect(row.ref).toEqual({ kind: 'asset', assetId: a.id, assetType: 'document', fileId: f.id, drawingDocumentId: null });
    expect(row.name).toBe('bracket-spec-renamed.pdf');
    expect(row.search).toContain('bracket-spec.pdf'); // the old asset name still finds it
    expect([row.type, row.creation, row.preview]).toEqual(['document', 'upload', 'preview']);
  });

  it('never merges by name: same-named files in two projects, and a file without an asset, stay separate rows', () => {
    const f1 = file('drawing.pdf');
    const f2 = file('drawing.pdf', { project_id: 'p2' });
    const orphan = file('drawing.pdf');
    const a1 = documentAsset(f1);
    const one = buildProjectCatalog(P1, src({ assets: [a1], files: [f1, orphan] })).items;
    const two = buildProjectCatalog(P2, src({ files: [f2] })).items;
    expect(one.map((i) => i.key).sort()).toEqual([`asset:${a1.id}`, `file:${orphan.id}`].sort());
    expect(two.map((i) => [i.key, i.project.code])).toEqual([[`file:${f2.id}`, 'P2']]);
    // A file without an asset is older data: its records cannot say how it was made.
    expect(one.find((i) => i.key === `file:${orphan.id}`)!.creation).toBe('unknown');
  });

  it('folds a drawing_document asset into its document by summary.document_id; a document without a version stays its own row', () => {
    const doc = drawingDocument('Flange layout', { number: 'DR-007', archived_at: '2026-10-01T00:00:00+00:00', head_version_no: 2 });
    const bare = drawingDocument('Empty sheet');
    doc.description = 'Bolt circle for the pump flange';
    const a = asset('drawing_document', 'DR-007 Old title', version('drawing_document', { document_id: doc.id, origin: 'edit' }, { label: 'V2', version_no: 2 }));
    const { items } = buildProjectCatalog(P1, src({ assets: [a], drawingDocuments: [doc, bare] }));
    const folded = items.find((i) => i.key === `asset:${a.id}`)!;
    expect([folded.name, folded.context, folded.description, folded.archived, folded.openPath, folded.creation]).toEqual([
      'Flange layout',
      ['DR-007'],
      'Bolt circle for the pump flange',
      'self',
      `/drafting/${doc.id}`,
      'manual',
    ]);
    const own = items.find((i) => i.key === `drawing_document:${bare.id}`)!;
    expect([own.type, own.subtype, own.preview, own.openPath]).toEqual(['drawing', 'drawing_document', 'none', `/drafting/${bare.id}`]);
    expect(items).toHaveLength(2);
  });

  it('takes a document part from the File (relinked); an archived part hides the row without archiving the file itself', () => {
    const part: Part = { id: 'part-1', number: 'BR-01', name: 'Bracket', archived_at: '2026-10-02T00:00:00+00:00' } as Part;
    const f = file('notes.txt', { part_id: 'part-1' }, { content_type: 'text/plain' });
    const a = documentAsset(f);
    a.part_id = null; // registered before the file was linked
    const { items } = buildProjectCatalog(P1, src({ assets: [a], files: [f], parts: [part] }));
    expect([items[0].partId, items[0].partNumber, items[0].context, items[0].archived]).toEqual(['part-1', 'BR-01', ['BR-01 Bracket'], 'part']);
    // The file's own archive is what "restore" undoes, so it wins over the part's.
    f.archived_at = '2026-10-03T00:00:00+00:00';
    expect(buildProjectCatalog(P1, src({ assets: [a], files: [f], parts: [part] })).items[0].archived).toBe('self');
    expect(buildProjectCatalog(P1, src({ files: [f], parts: [part] })).items[0].archived).toBe('self');
  });

  it('marks specs beyond the page as truncated and sorts newest first', () => {
    const old = spec('Old', { updated_at: '2026-01-01T00:00:00+00:00' });
    old.latest_version!.created_at = '2026-01-01T00:00:00+00:00';
    const fresh = spec('Fresh');
    const { items, truncated } = buildProjectCatalog(P1, src({ specs: { items: [old, fresh], total: 250 } }));
    expect(truncated).toEqual(['specs']);
    expect(items.map((i) => i.name)).toEqual(['Fresh', 'Old']);
    expect([items[0].key, items[0].openPath, items[0].latest?.label, items[0].creation]).toEqual([`spec:${fresh.id}`, `/specs/${fresh.id}`, 'v1', 'unknown']);
    expect(buildProjectCatalog(P1, src({ specs: { items: [fresh], total: 1 } })).truncated).toEqual([]);
  });
});

describe('creation method, preview and upstream from records', () => {
  const row = (a: Asset, extra: Partial<ProjectSources> = {}) => buildProjectCatalog(P1, src({ assets: [a], ...extra })).items.find((i) => i.key === `asset:${a.id}`)!;

  it('maps each type through its record field; a run always means task; unproven → unknown', () => {
    const cases: [Asset, string][] = [
      [asset('part_model', 'm', version('part_model', { source: 'import' })), 'upload'],
      [asset('part_model', 'm', version('part_model', { source: 'copy' })), 'manual'],
      [asset('part_model', 'm', version('part_model', { source: 'ai' })), 'task'],
      [asset('part_model', 'm', version('part_model', { source: 'manual' }, { run_id: 'r1' })), 'task'],
      [asset('part_model', 'm', version('part_model', { source: 'mystery' })), 'unknown'],
      [asset('assembly_model', 'a', version('assembly_model', { source: 'manual', assembly_id: 'asm' })), 'manual'],
      [asset('drawing', 'd', version('drawing', { kind: 'generated' })), 'task'],
      [asset('drawing', 'd', version('drawing', { kind: 'uploaded' })), 'upload'],
      [asset('drawing_document', 'dd', version('drawing_document', { origin: 'text' })), 'task'],
      [asset('assembly_drawing', 'ad', version('assembly_drawing', { assembly_id: 'asm', parent_id: null })), 'task'],
      [asset('assembly_drawing', 'ad', version('assembly_drawing', { assembly_id: 'asm', parent_id: 'prev' })), 'manual'],
      [asset('intake', 'i', version('intake', { method: 'manual' })), 'manual'],
      [asset('quote', 'q', version('quote', {})), 'task'],
      [asset('process_module', 'pm', version('process_module', {})), 'manual'],
      [asset('delivery_manifest', 'dl', version('delivery_manifest', {})), 'task'],
      [asset('future_type', 'x', version('future_type', {})), 'unknown'],
      [asset('quote', 'q', null), 'unknown'],
    ];
    expect(cases.map(([a]) => row(a).creation)).toEqual(cases.map(([, c]) => c));
    expect(row(cases[15][0]).type).toBe('document'); // an unknown type stays visible, typed generically
  });

  it('a document a run wrote (agent write without a run id) is task output, an upload otherwise', () => {
    const written = file('GEN-02.md');
    const uploaded = file('spec.pdf');
    const a = documentAsset(written);
    const b = documentAsset(uploaded);
    const out = buildProjectCatalog(P1, src({ assets: [a, b], files: [written, uploaded], taskOutputs: outputs({ written: new Map([[a.id, new Set([a.latest_version!.id])]]), truncated: true }) }));
    expect(out.items.map((i) => [i.name, i.creation]).sort()).toEqual([['GEN-02.md', 'task'], ['spec.pdf', 'upload']]);
    expect(out.truncated).toEqual(['task_outputs']);
  });

  it('counts a workflow resource as a run write only when its registration reason says the run wrote it', () => {
    const v = (label: string) => version('document', {}, { label });
    const [tool, workspace, delivered, ownUpload, forked, rejected, input] = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(v);
    const out = taskOutputsOf(
      [
        resource(tool, 'candidate', 'tool:document'),
        resource(workspace, 'candidate', 'workspace_file'),
        resource(delivered, 'candidate', 'deliver'), // an agent hands in a version that may already exist
        resource(ownUpload, 'candidate', 'add_deliverable'), // a person delivers their own upload
        resource(forked, 'candidate', 'forked'), // copied into a forked run
        resource(rejected, 'failed', 'tool: wrong flange'), // a reviewer's comment, not a code
        resource(input, 'input', 'bound_input'),
      ],
      false,
    );
    expect([...out.written.values()].flatMap((ids) => [...ids]).sort()).toEqual([tool.id, workspace.id].sort());
  });

  it('keeps current task-formal versions per asset and shows those other than the latest beside it', () => {
    const p1 = version('process_plan', {}, { label: 'P1', version_no: 1 });
    const p2 = version('process_plan', {}, { label: 'P2', version_no: 2 });
    const plan = asset('process_plan', 'Plan', p2);
    p1.asset_id = plan.id;
    const stale = version('process_plan', {}, { label: 'P0' });
    stale.asset_id = plan.id;
    const records = taskOutputsOf(
      [
        resource(p1, 'formal', 'accepted', { task_number: 12 }),
        resource(p1, 'formal', 'accepted', { task_number: 12, slot_key: 'other' }), // same task and version: once
        resource(p1, 'formal', 'accepted', { task_number: 15 }),
        resource(p2, 'formal', 'accepted', { task_number: 20 }),
        resource(stale, 'formal', 'accepted', { task_acceptance: { status: 'invalidated', current: false } as WorkflowResourceItem['task_acceptance'] }),
        resource(p1, 'superseded', 'reopened: fix'),
      ],
      false,
    );
    const row = buildProjectCatalog(P1, src({ assets: [plan], taskOutputs: records })).items[0];
    expect(row.formal).toEqual([
      { versionId: p1.id, label: 'P1', taskNumber: 12 },
      { versionId: p1.id, label: 'P1', taskNumber: 15 },
      { versionId: p2.id, label: 'P2', taskNumber: 20 },
    ]);
    expect([row.latest?.label, formalBesideLatest(row)]).toEqual(['P2', [{ versionId: p1.id, label: 'P1', taskNumbers: [12, 15] }]]);
  });

  it('spec creation: a rule derivation is task, create (page or workflow, no run marker) unknown', () => {
    const items = buildProjectCatalog(P1, src({ specs: { items: [spec('a', {}, 'ready', 'rule'), spec('b', {}, 'failed')], total: 2 } })).items;
    expect(items.map((i) => [i.name, i.creation, i.preview, i.latest?.availability])).toEqual([
      ['a', 'task', 'page', 'ready'],
      ['b', 'unknown', 'failed', 'failed'],
    ]);
  });

  it('previews what the inline viewer renders (by the stored name), CAD as needs conversion, failed and building first', () => {
    const doc = (name: string, kind: ProjectFile['kind'], extra: Partial<AssetVersion> = {}) => {
      const f = file(name, { kind });
      return row(documentAsset(f, name, extra), { files: [f] }).preview;
    };
    const imported: AssetEdge = { version_id: 'mv', relation: 'imported_from' };
    expect([
      doc('std.pdf', 'drawing_pdf'),
      doc('bom.csv', 'other'),
      doc('notes.html', 'document'), // text/html, but the viewer does not render HTML
      doc('scan.bmp', 'drawing_image'), // an image kind the viewer cannot show
      doc('part.step', 'model_step'),
      doc('plan.dwg', 'other'),
      doc('part.step', 'model_step', { dependents: [imported] }),
      doc('bundle.zip', 'other'),
      doc('std.pdf', 'drawing_pdf', { availability: 'failed' }),
      doc('std.pdf', 'drawing_pdf', { availability: 'building' }),
    ]).toEqual(['preview', 'preview', 'download', 'download', 'convert', 'convert', 'download', 'download', 'failed', 'none']);
    const orphan = file('scan.tif', { kind: 'drawing_image' });
    expect(buildProjectCatalog(P1, src({ files: [orphan] })).items[0].preview).toBe('download');
    expect(row(asset('drawing', 'd', version('drawing', { svg_sha: 'abc' }))).preview).toBe('preview');
    expect(row(asset('quote', 'q', version('quote', {}))).preview).toBe('page');
  });

  it('opens the professional page of the exact version, else the asset page', () => {
    const pm = asset('part_model', 'm', version('part_model', { source: 'manual' }));
    pm.latest_version!.target.part_id = 'part-9';
    const asm = asset('assembly_model', 'a', version('assembly_model', { source: 'manual', assembly_id: 'asm-1' }));
    const ad = asset('assembly_drawing', 'ad', version('assembly_drawing', { assembly_id: 'asm-1', assembly_revision_id: 'rev-2' }));
    const dl = asset('delivery_manifest', 'dl', version('delivery_manifest', {}));
    expect(row(pm).openPath).toBe(`/parts/part-9/model?rev=${pm.latest_version!.target.id}`);
    expect(row(asm).openPath).toBe(`/assemblies/asm-1/structure?rev=${asm.latest_version!.target.id}`);
    expect(row(ad).openPath).toBe('/assemblies/asm-1/drawings?rev=rev-2');
    expect(row(dl).openPath).toBe(`/assets/${dl.id}?version=${dl.latest_version!.id}`);
    expect(row(pm).formal).toEqual([]);
  });

  it('flags upstream newer only when a dependency asset of the same project has a later version', () => {
    const model = asset('part_model', 'm', version('part_model', { source: 'manual' }, { version_no: 3 }));
    const stale = asset('drawing', 'd', version('drawing', { kind: 'generated' }, { dependencies: [{ version_id: 'v', relation: 'derived_from', asset_id: model.id, version_no: 2 }] }));
    const current = asset('drawing', 'd2', version('drawing', { kind: 'generated' }, { dependencies: [{ version_id: 'v', relation: 'derived_from', asset_id: model.id, version_no: 3 }] }));
    const elsewhere = asset('quote', 'q', version('quote', {}, { dependencies: [{ version_id: 'v', relation: 'priced_from', asset_id: 'other-project', version_no: 1 }] }));
    const items = buildProjectCatalog(P1, src({ assets: [model, stale, current, elsewhere] })).items;
    const flag = (a: Asset) => items.find((i) => i.key === `asset:${a.id}`)!.upstreamNewer;
    expect([flag(stale), flag(current), flag(elsewhere), flag(model)]).toEqual([true, false, false, false]);
  });
});

describe('filterCatalog, findCatalogItem, counts', () => {
  const f = file('Housing STEP.step', { kind: 'model_step' }, { content_type: 'model/step' });
  const docAsset = documentAsset(f);
  const doc = drawingDocument('Cover plate', { number: 'DR-042' });
  const dd = asset('drawing_document', 'x', version('drawing_document', { document_id: doc.id, origin: 'text' }, { publish_state: 'published' }));
  const failed = asset('quote', 'Q quote', version('quote', {}, { availability: 'failed', publish_state: 'deprecated' }));
  const archivedFile = file('old.pdf', { archived_at: '2026-10-01T00:00:00+00:00' });
  const { items } = buildProjectCatalog(P1, src({ assets: [docAsset, dd, failed], files: [f, archivedFile], drawingDocuments: [doc] }));

  it('hides archived rows unless asked, and matches every search word case-insensitively', () => {
    expect(filterCatalog(items, EMPTY_FILTER)).toHaveLength(3);
    expect(filterCatalog(items, { ...EMPTY_FILTER, archived: true })).toHaveLength(4);
    expect(filterCatalog(items, { ...EMPTY_FILTER, q: 'dr-042 COVER' }).map((i) => i.key)).toEqual([`asset:${dd.id}`]);
    expect(filterCatalog(items, { ...EMPTY_FILTER, q: 'cover housing' })).toEqual([]);
  });

  it('filters by type, subtype, creation and status', () => {
    const keys = (f2: Partial<typeof EMPTY_FILTER>) => filterCatalog(items, { ...EMPTY_FILTER, ...f2 }).map((i) => i.key);
    expect(keys({ type: 'drawing', subtype: 'drawing_document' })).toEqual([`asset:${dd.id}`]);
    expect(keys({ creation: 'upload' })).toEqual([`asset:${docAsset.id}`]);
    expect(keys({ status: 'published' })).toEqual([`asset:${dd.id}`]);
    expect(keys({ status: 'failed' })).toEqual([`asset:${failed.id}`]);
    expect(keys({ status: 'deprecated' })).toEqual([`asset:${failed.id}`]);
    expect(keys({ status: 'upstream_newer' })).toEqual([]);
  });

  it('finds a folded file / drawing document by its own key, and nothing for unknown keys', () => {
    expect(findCatalogItem(items, `file:${f.id}`)?.key).toBe(`asset:${docAsset.id}`);
    expect(findCatalogItem(items, `drawing_document:${doc.id}`)?.key).toBe(`asset:${dd.id}`);
    expect(findCatalogItem(items, `file:${archivedFile.id}`)?.key).toBe(`file:${archivedFile.id}`);
    expect(findCatalogItem(items, 'spec:nope')).toBeUndefined();
    expect(findCatalogItem(items, 'garbage')).toBeUndefined();
  });

  it('counts active and archived rows', () => {
    const c = catalogCounts(items);
    expect([c.active, c.archived, c.byType.document, c.byType.drawing, c.byType.quote]).toEqual([3, 1, 1, 1, 1]);
  });
});
