import { describe, expect, it } from 'vitest';
import { FLOW_TAB_META, FLOW_TABS, PROJECT_SECTION_IDS, RESOURCE_TAB_META, SECTION_TARGET, areaOf, assetCenterTarget, flowMainTab, isFlowTab, isResourceTab, legacyProjectUrl, paths, projectSwitchTarget, resourcesRedirect, sectionUrl, withProjectParam } from './nav';

const P = '11111111-1111-4111-8111-111111111111';

describe('R1.2 area URLs', () => {
  it('maps every old project section to an area', () => {
    for (const s of PROJECT_SECTION_IDS) expect(SECTION_TARGET[s]).toMatch(/^\/(parts|flows|resources|project|settings)(\/|$)/);
    expect(sectionUrl('runs', P)).toBe(`/flows/runs?project=${P}`);
    expect(sectionUrl('workflows', P)).toBe(`/flows/templates?project=${P}`);
    expect(sectionUrl('overview', P)).toBe(`/project/overview?project=${P}`);
    expect(sectionUrl('parts')).toBe('/parts');
  });

  it('keeps the old query and names the project once', () => {
    expect(sectionUrl('assets', P, `?tab=knowledge&project=other&q=x`)).toBe(`/resources/knowledge?project=${P}&q=x`);
    expect(paths.runs(P, { task: 't1', status: 'failed' })).toBe(`/flows/runs?project=${P}&task=t1&status=failed`);
    expect(paths.assetsTab(P, 'resources', { kind: 'formal' })).toBe(`/resources/process?project=${P}&kind=formal`);
    expect(paths.assetsTab(P, 'all', { asset_type: 'drawing' })).toBe(`/resources/files?project=${P}&type=drawing&subtype=drawing`);
    expect(paths.assetsTab(P, 'all', { asset_type: 'quote', q: 'x', label: 'reviewed', offset: 50 })).toBe(`/resources/files?project=${P}&type=quote&q=x`);
    expect(paths.assetsTab(P, 'knowledge')).toBe(`/resources/knowledge?project=${P}`);
    expect(paths.knowledge(P, 'c1')).toBe(`/resources/knowledge?project=${P}&collection_id=c1`);
    expect(paths.resources()).toBe('/resources/files');
    expect(paths.materials(P, { type: 'spec' })).toBe(`/resources/files?project=${P}&type=spec`);
    expect(paths.materials(null, { type: 'drawing', subtype: 'drawing_document' })).toBe('/resources/files?type=drawing&subtype=drawing_document');
    expect(paths.project(P)).toBe(`/project/tasks?project=${P}`);
    expect(paths.collaboration(P)).toBe(`/flows/collaboration?project=${P}`);
    expect(paths.agents(P)).toBe(`/flows/agents?project=${P}`);
    expect(paths.templateLibrary()).toBe('/flows/library');
  });

  it('redirects old project links', () => {
    expect(legacyProjectUrl(P, undefined)).toBe(`/project/tasks?project=${P}`);
    expect(legacyProjectUrl(P, 'nonsense', '?x=1')).toBe(`/project/tasks?project=${P}`);
    expect(legacyProjectUrl(P, 'runs', '?workflow=w&unlinked=1')).toBe(`/flows/runs?project=${P}&workflow=w&unlinked=1`);
    expect(legacyProjectUrl(P, 'members')).toBe(`/settings?project=${P}&section=project`);
    expect(legacyProjectUrl(P, 'knowledge', '?collection_id=c&offset=50&q=a')).toBe(`/resources/knowledge?project=${P}&collection_id=c&q=a`);
    expect(legacyProjectUrl(P, 'assets')).toBe(`/resources/files?project=${P}`);
    expect(legacyProjectUrl(P, 'assets', '?tab=resources&run_id=r')).toBe(`/resources/process?project=${P}&run_id=r`);
  });

  it('maps the old asset center onto materials / experience / usage records', () => {
    const at = (search: string) => {
      const { path, query } = assetCenterTarget(search);
      const s = query.toString();
      return `${path}${s ? `?${s}` : ''}`;
    };
    expect(at('')).toBe('/resources/files');
    expect(at(`?project=${P}&tab=all&asset_type=quote&q=x`)).toBe(`/resources/files?project=${P}&type=quote&q=x`);
    expect(at(`?project=${P}&tab=all&asset_type=part_model&part_id=p1&archived=1&label=reviewed&limit=20`)).toBe(
      `/resources/files?project=${P}&type=model&subtype=part_model&part_id=p1&archived=1`,
    );
    expect(at(`?project=${P}&tab=resources&kind=formal&offset=50`)).toBe(`/resources/process?project=${P}&kind=formal&offset=50`);
    expect(at(`?tab=knowledge&collection_id=c`)).toBe('/resources/knowledge?collection_id=c');
    expect(at('?tab=bogus&asset_type=nope')).toBe('/resources/files');
  });

  it('maps an old asset_type through the catalogue business types, naming the kind only when the type has several', () => {
    const at = (assetType: string) => resourcesRedirect('files', `?asset_type=${assetType}`);
    expect(at('assembly_model')).toBe('/resources/files?type=model&subtype=assembly_model');
    expect(at('assembly_drawing')).toBe('/resources/files?type=drawing&subtype=assembly_drawing');
    expect(at('engineering_spec')).toBe('/resources/files?type=spec');
    expect(at('document')).toBe('/resources/files?type=document');
    expect(at('delivery_manifest')).toBe('/resources/files?type=delivery');
    expect(at('process_module')).toBe('/resources/files?type=process_module');
    expect(at('constructor')).toBe('/resources/files');
  });

  it('has one main resource view and retains historical redirects', () => {
    expect(RESOURCE_TAB_META.map((m) => m.id)).toEqual(['files']);
    expect(isResourceTab('process')).toBe(true);
    expect(isResourceTab('specs')).toBe(false);
    expect(resourcesRedirect('files', `?project=${P}&q=a&type=spec`)).toBeNull();
    expect(resourcesRedirect('knowledge', `?scope=all`)).toBeNull();
    expect(resourcesRedirect('process', `?project=${P}&run_id=r`)).toBeNull();
    expect(resourcesRedirect('specs', `?project=${P}&q=a`)).toBe(`/resources/files?project=${P}&q=a&type=spec`);
    expect(resourcesRedirect('specs')).toBe('/resources/files?type=spec');
    expect(resourcesRedirect('drafting', `?project=${P}`)).toBe(`/resources/files?project=${P}&type=drawing&subtype=drawing_document`);
    expect(resourcesRedirect('machines', `?project=${P}`)).toBe(`/resources/process?project=${P}`);
    expect(resourcesRedirect('assets', `?project=${P}&tab=knowledge&collection_id=c`)).toBe(`/resources/knowledge?project=${P}&collection_id=c`);
    expect(resourcesRedirect('files', `?project=${P}&view=assets&asset_type=drawing_document`)).toBe(`/resources/files?project=${P}&type=drawing&subtype=drawing_document`);
    expect(resourcesRedirect('files', `?view=files`)).toBe('/resources/files');
    expect(resourcesRedirect(undefined, `?project=${P}`)).toBe(`/resources/files?project=${P}`);
    expect(resourcesRedirect(undefined, '?scope=shared')).toBe('/resources/files?scope=shared');
    expect(resourcesRedirect('bogus')).toBe('/resources/files');
  });

  it('adds the project to a return link', () => {
    expect(withProjectParam('/resources/assets?tab=knowledge', P)).toBe(`/resources/assets?project=${P}&tab=knowledge`);
    expect(withProjectParam(`/resources/assets?project=zzz`, P)).toBe(`/resources/assets?project=${P}`);
    expect(withProjectParam('/parts', P)).toBe(`/parts?project=${P}`);
  });
});

describe('AI workflow tabs', () => {
  it('shows runs, templates and the retained engineering team while preserving historical routes', () => {
    expect(FLOW_TAB_META.map((m) => m.id)).toEqual(['runs', 'templates', 'team']);
    for (const tab of FLOW_TABS) expect(isFlowTab(tab)).toBe(true);
    expect(flowMainTab('collaboration')).toBe('templates');
    expect(flowMainTab('library')).toBe('templates');
    expect(flowMainTab('agents')).toBe('templates');
    expect(flowMainTab('team')).toBe('team');
    expect(paths.flows('team', P)).toBe(`/flows/team?project=${P}`);
    expect(SECTION_TARGET.collaboration).toBe('/flows/collaboration');
    expect(projectSwitchTarget('/flows/library')).toBe('/flows/library');
    expect(projectSwitchTarget('/flows/team')).toBe('/flows/team');
  });
});

describe('rail area of a page', () => {
  it('highlights the area of detail pages', () => {
    expect(areaOf('/home')).toBe('home');
    expect(areaOf('/parts/abc/workflow')).toBe('parts');
    expect(areaOf('/runs/r1')).toBe('flows');
    expect(areaOf('/workflows/w1/config')).toBe('flows');
    expect(areaOf('/template-library/x')).toBe('flows');
    expect(areaOf('/collaboration/runs/r')).toBe('flows');
    expect(areaOf('/approval-requests/a')).toBe('approvals');
    expect(areaOf('/assets/a')).toBe('resources');
    expect(areaOf('/tasks/t')).toBe('project');
    expect(areaOf(`/projects/${P}/collaboration/new`)).toBe('flows');
    expect(areaOf('/settings')).toBeNull();
  });
});

describe('switching the current project', () => {
  it('stays on area tabs and leaves pages of one object', () => {
    expect(projectSwitchTarget('/flows/agents')).toBe('/flows/agents');
    expect(projectSwitchTarget('/flows/bogus')).toBe('/flows');
    expect(projectSwitchTarget('/resources/files')).toBe('/resources/files');
    expect(projectSwitchTarget('/resources/knowledge')).toBe('/resources/knowledge');
    expect(projectSwitchTarget('/resources/specs')).toBe('/resources');
    expect(projectSwitchTarget('/resources/assets')).toBe('/resources');
    expect(projectSwitchTarget('/project/members')).toBe('/project/members');
    expect(projectSwitchTarget('/parts/p1/model')).toBe('/parts');
    expect(projectSwitchTarget('/runs/r1')).toBe('/flows');
    expect(projectSwitchTarget('/tasks/t1')).toBe('/project');
    expect(projectSwitchTarget('/assets/a1')).toBe('/resources');
    expect(projectSwitchTarget('/approval-requests/a1')).toBe('/approvals');
    expect(projectSwitchTarget('/home')).toBe('/home');
    expect(projectSwitchTarget('/assistant/c1')).toBe('/assistant/c1');
    expect(projectSwitchTarget('/projects')).toBe('/project');
    expect(projectSwitchTarget('/')).toBe('/home');
  });
});
