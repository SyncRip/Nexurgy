import { describe, expect, it } from 'vitest';
import { assemblyRevisionPath, assetWorkspacePath, exactSourcePath, isHistorical, partOutputPath, portValuePath } from './versionLinks';

describe('exact-version links (S6)', () => {
  it('every part output tab carries its own version parameter', () => {
    expect(partOutputPath('p1', 'model', 'r1')).toBe('/parts/p1/model?rev=r1');
    expect(partOutputPath('p1', 'drawings', 'd1', { page: 2 })).toBe('/parts/p1/drawings?drawing=d1&page=2');
    expect(partOutputPath('p1', 'process', 'pl1')).toBe('/parts/p1/process?plan=pl1');
    expect(partOutputPath('p1', 'quote', 'q1')).toBe('/parts/p1/quote?quote=q1');
    expect(partOutputPath('p1', 'model', null)).toBe('/parts/p1/model');
  });

  it('assembly links name the exact revision', () => {
    expect(assemblyRevisionPath('a1', 'ar2')).toBe('/assemblies/a1/structure?rev=ar2');
    expect(assemblyRevisionPath('a1', 'ar2', 'drawings')).toBe('/assemblies/a1/drawings?rev=ar2');
  });

  it('"open in workspace" of an asset version opens that version, never the part alone', () => {
    expect(assetWorkspacePath('part_model', { id: 'r1', part_id: 'p1' })).toBe('/parts/p1/model?rev=r1');
    expect(assetWorkspacePath('drawing', { id: 'd1', part_id: 'p1' })).toBe('/parts/p1/drawings?drawing=d1');
    expect(assetWorkspacePath('process_plan', { id: 'pl1', part_id: 'p1' })).toBe('/parts/p1/process?plan=pl1');
    expect(assetWorkspacePath('quote', { id: 'q1', part_id: 'p1' })).toBe('/parts/p1/quote?quote=q1');
    expect(assetWorkspacePath('intake', { id: 'i1', part_id: 'p1' })).toBe('/parts/p1/process');
    expect(assetWorkspacePath('part_model', { id: 'r1', part_id: null })).toBeNull();
    expect(assetWorkspacePath('document', { id: 'f1', part_id: 'p1' })).toBeNull();
  });

  it('run outputs (workflow port values) link the version the run produced', () => {
    expect(portValuePath({ type: 'model', revision_id: 'r1', rev_no: 1 }, 'p1')).toBe('/parts/p1/model?rev=r1');
    expect(portValuePath({ type: 'drawing', drawing_id: 'd1' }, 'p1')).toBe('/parts/p1/drawings?drawing=d1');
    expect(portValuePath({ type: 'plan', process_plan_id: 'pl1' }, 'p1')).toBe('/parts/p1/process?plan=pl1');
    expect(portValuePath({ type: 'quote', quote_id: 'q1' }, 'p1')).toBe('/parts/p1/quote?quote=q1');
    expect(portValuePath({ type: 'spec', intake_id: 'i1' }, 'p1')).toBe('/parts/p1/process');
    expect(portValuePath({ type: 'model', revision_id: 'r1' }, null)).toBeNull();
    expect(portValuePath({ type: 'text', text: 'x' }, 'p1')).toBeNull();
  });

  it('assembly revision references from before S6 get their revision back', () => {
    expect(exactSourcePath({ kind: 'assembly_revision', object_id: 'ar1', source_path: '/assemblies/a1' })).toBe('/assemblies/a1?rev=ar1');
    expect(exactSourcePath({ kind: 'assembly_revision', object_id: 'ar1', source_path: '/assemblies/a1/structure?rev=ar1' })).toBe('/assemblies/a1/structure?rev=ar1');
    expect(exactSourcePath({ kind: 'part_revision', object_id: 'r1', source_path: '/parts/p1/model?rev=r1' })).toBe('/parts/p1/model?rev=r1');
    expect(exactSourcePath({ kind: 'part', object_id: 'p1', source_path: '/parts/p1/model' })).toBe('/parts/p1/model');
    expect(exactSourcePath({ kind: 'assembly_revision', object_id: 'ar1', source_path: '' })).toBe('');
  });

  it('a version is historical only when a different current version is known', () => {
    expect(isHistorical('r1', 'r2')).toBe(true);
    expect(isHistorical('r2', 'r2')).toBe(false);
    expect(isHistorical('r1', undefined)).toBe(false);
    expect(isHistorical(null, 'r2')).toBe(false);
  });
});
