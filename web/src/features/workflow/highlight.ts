// Canvas highlights (AI work panel, .trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §4): extra classes on nodes and
// edges of the editor / run canvas — what a change plan adds, changes or removes, the node it is about, and run
// steps waiting for a person. Styling lives at the end of workflow.css (.wf-hl-*).

export type NodeHighlight = 'added' | 'changed' | 'focus' | 'attention' | 'removed';
export type EdgeHighlight = 'added' | 'removed';

export interface CanvasHighlight {
  nodes?: Readonly<Record<string, NodeHighlight>>;
  edges?: Readonly<Record<string, EdgeHighlight>>;
}

/** Class of a highlighted node's React Flow wrapper (undefined: none). */
export function nodeHighlightClass(h: NodeHighlight | undefined): string | undefined {
  return h ? `wf-hl wf-hl-${h}` : undefined;
}

/** Class appended to a highlighted edge ('' when none). */
export function edgeHighlightClass(h: EdgeHighlight | undefined): string {
  return h ? ` wf-hl-${h}` : '';
}
