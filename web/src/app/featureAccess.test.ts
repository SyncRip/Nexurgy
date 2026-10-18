import { describe, expect, it } from 'vitest';
import { flowTabFeature, projectTabFeature, routeFeature } from './featureAccess';

describe('feature navigation boundary', () => {
  it.each([
    ['/projects/p/collaboration/new', 'collaboration'], ['/flows/agents', 'collaboration'], ['/flows/executions', 'collaboration'],
    ['/flows/collaboration', 'collaboration'], ['/flows/library', 'template_library'], ['/template-library', 'template_library'],
    ['/project/tasks', 'tasks'], ['/project/overview', 'stages'],
    ['/resources/knowledge', 'collaboration'], ['/resources/process', 'collaboration'],
  ])('guards creation/list route %s', (path, feature) => expect(routeFeature(path)).toBe(feature));

  it.each(['/collaboration/drafts/a', '/collaboration/runs/a', '/tasks/a', '/agent-executions/a', '/approval-requests/a', '/template-library/a', '/runs/a', '/assets/a', '/parts/a', '/flows/runs', '/project/members', '/approvals'])('preserves history and core route %s', (path) => expect(routeFeature(path)).toBeNull());

  it('uses independent flags for task, stage, and public template navigation', () => {
    expect(projectTabFeature('tasks')).toBe('tasks');
    expect(projectTabFeature('overview')).toBe('stages');
    expect(projectTabFeature('members')).toBeNull();
    expect(flowTabFeature('library')).toBe('template_library');
    expect(flowTabFeature('templates')).toBeNull();
    expect(flowTabFeature('team')).toBeNull();
    expect(routeFeature('/flows/team', '?role=manager')).toBeNull();
  });

  it('retains historical resource deep links and ordinary part/assembly materials', () => {
    expect(routeFeature('/resources/knowledge', '?project=p&collection_id=c&collection_version_id=v')).toBeNull();
    expect(routeFeature('/resources/process', '?project=p&run_id=r&node_id=n')).toBeNull();
    expect(routeFeature('/resources/knowledge', '?project=p')).toBe('collaboration');
    expect(routeFeature('/resources/files', '?type=part')).toBeNull();
    expect(routeFeature('/resources/files', '?type=assembly')).toBeNull();
  });

  it('opens exact historical agent profiles while guarding the list, creation and copying', () => {
    expect(routeFeature('/flows/agents', '?project=p&agent=a1')).toBeNull();
    expect(routeFeature('/flows/agents/', '?project=p&agent=a1')).toBeNull();
    expect(routeFeature('/flows/agents', '?project=p&agent=')).toBe('collaboration');
    expect(routeFeature('/flows/agents', '?project=p&agent=new')).toBe('collaboration');
    expect(routeFeature('/flows/agents', '?project=p&agent=new&copy=a1')).toBe('collaboration');
    expect(routeFeature('/flows/agents', '?project=p&agent=a1&copy=a2')).toBe('collaboration');
  });
});
