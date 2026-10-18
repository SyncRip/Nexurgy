import { describe, expect, it } from 'vitest';
import type { AiWorkflowDiff } from '../../../api/assistant';
import type { WorkflowGraph, WorkflowRun } from '../../../api/types';
import { autoRefChange, fullPageHref, nextPanelSearch, planPreview, readPanelState, refIsNode, runHighlight, targetLine, type NodeSource } from './model';
import { panelParam, parsePanelParam } from './WorkPanelContext';

const ID = '0f8fad5b-d9cb-469f-a165-70867728950e';
const ID2 = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

describe('panel URL', () => {
  it('round-trips every object kind and refuses malformed values', () => {
    for (const o of [
      { kind: 'workflow', id: ID },
      { kind: 'plan', operationId: ID },
      { kind: 'run', id: ID },
      { kind: 'revision', id: ID },
      { kind: 'task', id: ID },
      { kind: 'draft', id: ID },
    ] as const) {
      expect(parsePanelParam(panelParam(o))).toEqual(o);
    }
    expect(panelParam({ kind: 'plan', operationId: ID })).toBe(`plan:${ID}`);
    for (const bad of [null, '', 'run', `:${ID}`, `quote:${ID}`, 'run:', 'run:not an id', `run:${ID}/x`]) expect(parsePanelParam(bad)).toBeNull();
  });

  it('reads node and wide only while an object is open', () => {
    expect(readPanelState(new URLSearchParams(`panel=run:${ID}&node=model&wide=1`))).toEqual({ object: { kind: 'run', id: ID }, nodeId: 'model', wide: true });
    expect(readPanelState(new URLSearchParams('node=model&wide=1'))).toEqual({ object: null, nodeId: null, wide: false });
    expect(readPanelState(new URLSearchParams(`panel=run:${ID}&node=${'x'.repeat(61)}`)).nodeId).toBeNull();
  });

  it('pushes only when another object opens, keeps other parameters', () => {
    const cur = new URLSearchParams(`tab=1&panel=run:${ID}&node=model&wide=1`);
    const other = nextPanelSearch(cur, { object: { kind: 'workflow', id: ID2 } });
    expect(other.push).toBe(true);
    expect(other.search.get('panel')).toBe(`workflow:${ID2}`);
    expect(other.search.get('node')).toBeNull(); // another object starts without a node
    expect(other.search.get('wide')).toBe('1');
    expect(other.search.get('tab')).toBe('1');

    const node = nextPanelSearch(cur, { nodeId: 'review' });
    expect(node).toMatchObject({ push: false, changed: true });
    expect(node.search.get('node')).toBe('review');

    const same = nextPanelSearch(cur, { object: { kind: 'run', id: ID } });
    expect(same).toMatchObject({ push: false, changed: false });
    expect(nextPanelSearch(cur, { object: { kind: 'run', id: ID }, nodeId: 'quote' }).search.get('node')).toBe('quote');

    const narrow = nextPanelSearch(cur, { wide: false });
    expect(narrow.push).toBe(false);
    expect(narrow.search.has('wide')).toBe(false);

    const closed = nextPanelSearch(cur, { object: null });
    expect(closed.push).toBe(false);
    expect(closed.search.toString()).toBe('tab=1');
  });
});

const node = (id: string, type = 'quote', x = 0) => ({ id, type, position: { x, y: 0 }, params: {} });
const edge = (source: string, target: string, id = `${source}-${target}`) => ({ id, source, source_port: 'out', target, target_port: 'in' });
const emptyDiff: AiWorkflowDiff = { added_nodes: [], removed_nodes: [], changed_params: [], added_edges: [], removed_edges: [], renamed: [], empty: false };

describe('plan preview', () => {
  const before: WorkflowGraph = { nodes: [node('a', 'file_input', 0), node('b', 'model_3d', 300), node('old', 'human_review', 600), node('q', 'quote', 900)], edges: [edge('a', 'b'), edge('b', 'old'), edge('old', 'q')], groups: [] };
  const after: WorkflowGraph = { nodes: [node('a', 'file_input', 0), node('b', 'model_3d', 300), node('new', 'process_plan', 600), node('q', 'quote', 900)], edges: [edge('a', 'b'), edge('b', 'new', 'e9'), edge('new', 'q', 'e10')], groups: [] };
  const diff: AiWorkflowDiff = {
    ...emptyDiff,
    added_nodes: [{ id: 'new', type: 'process_plan', label: null, params: {}, position: { x: 600, y: 0 } }],
    removed_nodes: [{ id: 'old', type: 'human_review', label: null }],
    changed_params: [{ node_id: 'b', type: 'model_3d', keys: ['model_config_id'], before: {}, after: { model_config_id: 'x' } }],
    renamed: [{ node_id: 'q', before: null, after: 'Price' }],
    // edges compared by connection: the server's ids of added edges may differ from the planned graph's
    added_edges: [edge('b', 'new', 'tmp1'), edge('new', 'q', 'tmp2')],
    removed_edges: [edge('b', 'old'), edge('old', 'q')],
  };

  it('highlights added > changed > focus and draws removals as ghosts', () => {
    const { graph, highlight } = planPreview({ diff, graph_after: after, graph_before: before, focus_node_id: 'b' });
    expect(highlight.nodes).toEqual({ new: 'added', b: 'changed', q: 'changed', old: 'removed' });
    expect(highlight.edges).toEqual({ e9: 'added', e10: 'added', 'ghost:b-old': 'removed', 'ghost:old-q': 'removed' });
    expect(graph.nodes.map((n) => n.id)).toEqual(['a', 'b', 'new', 'q', 'old']);
    expect(graph.edges.map((e) => e.id)).toEqual(['a-b', 'e9', 'e10', 'ghost:b-old', 'ghost:old-q']);
  });

  it('marks the focus node when nothing else changed it', () => {
    expect(planPreview({ diff: emptyDiff, graph_after: after, focus_node_id: 'a' }).highlight.nodes).toEqual({ a: 'focus' });
    expect(planPreview({ diff: emptyDiff, graph_after: after, focus_node_id: 'gone' }).highlight.nodes).toEqual({});
  });

  it('without a base graph (workflow_create) removed nodes are not drawn, nor edges to them', () => {
    const { graph, highlight } = planPreview({ diff, graph_after: after });
    expect(graph.nodes.map((n) => n.id)).toEqual(['a', 'b', 'new', 'q']);
    expect(Object.values(highlight.edges ?? {})).not.toContain('removed');
  });

  it('flags run steps waiting for a person or failed', () => {
    const run = { nodes: [{ node_id: 'a', status: 'succeeded' }, { node_id: 'b', status: 'waiting_human' }, { node_id: 'c', status: 'failed' }, { node_id: 'd', status: 'running' }] } as unknown as WorkflowRun;
    expect(runHighlight(run).nodes).toEqual({ b: 'attention', c: 'attention' });
  });
});

describe('selection and chat target', () => {
  const src: NodeSource = { kind: 'workflow', objectId: ID, projectId: ID2 };

  it('keeps at most one auto-added node reference', () => {
    // first selection: the panel adds and owns the reference
    expect(autoRefChange(null, 'n1', ['part'])).toEqual({ remove: null, auto: 'n1' });
    // next selection: the previous one goes
    expect(autoRefChange('n1', 'n2', ['part', 'n1'])).toEqual({ remove: 'n1', auto: 'n2' });
    // a node the person had added stays theirs (and is never removed later)
    expect(autoRefChange('n1', 'mine', ['n1', 'mine'])).toEqual({ remove: 'n1', auto: null });
    // re-selecting the panel's own node keeps it
    expect(autoRefChange('n1', 'n1', ['n1'])).toEqual({ remove: null, auto: 'n1' });
    // the person removed it meanwhile: nothing to remove
    expect(autoRefChange('n1', 'n2', ['part'])).toEqual({ remove: null, auto: 'n2' });
  });

  it('describes the target line', () => {
    const nodeRef = { kind: 'workflow' as const, object_id: ID, node: { node_id: 'model', type: 'model_3d', label: null } };
    const whole = { kind: 'workflow' as const, object_id: ID, node: null };
    expect(refIsNode(nodeRef, src, 'model')).toBe(true);
    expect(refIsNode(whole, src, 'model')).toBe(false);
    expect(refIsNode(nodeRef, { kind: 'run', objectId: ID }, 'model')).toBe(false);
    const sel = { src, nodeId: 'model' };
    expect(targetLine(null, sel, false)).toBe('none');
    expect(targetLine(nodeRef, null, false)).toBe('plain');
    expect(targetLine(nodeRef, sel, false)).toBe('following');
    expect(targetLine(nodeRef, sel, true)).toBe('plain');
    expect(targetLine(whole, sel, false)).toBe('not_following');
    expect(targetLine(nodeRef, { src, nodeId: 'quote' }, true)).toBe('not_following');
  });

  it('links each object to its full page', () => {
    expect(fullPageHref({ kind: 'workflow', id: ID }, { partId: ID2 })).toBe(`/parts/${ID2}/workflow?flow=${ID}`);
    expect(fullPageHref({ kind: 'workflow', id: ID })).toBe(`/workflows/${ID}`);
    expect(fullPageHref({ kind: 'run', id: ID })).toBe(`/runs/${ID}`);
    expect(fullPageHref({ kind: 'revision', id: ID }, { partId: ID2 })).toBe(`/parts/${ID2}/model?rev=${ID}`);
    expect(fullPageHref({ kind: 'revision', id: ID })).toBeNull();
    expect(fullPageHref({ kind: 'task', id: ID })).toBe(`/tasks/${ID}`);
    expect(fullPageHref({ kind: 'plan', operationId: ID })).toBeNull();
    expect(fullPageHref({ kind: 'draft', id: ID })).toBeNull();
  });
});
