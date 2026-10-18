// In-page notice that a workflow got a new version from outside the editor
// (an AI change plan applied or undone in the assistant), so a canvas showing
// that workflow can reload it. Runs never change; only the next run uses it.

const bus = new EventTarget();

export function emitWorkflowChanged(workflowId: string, version: number): void {
  bus.dispatchEvent(new CustomEvent('changed', { detail: { workflowId, version } }));
}

export function onWorkflowChanged(cb: (e: { workflowId: string; version: number }) => void): () => void {
  const h = (e: Event) => cb((e as CustomEvent<{ workflowId: string; version: number }>).detail);
  bus.addEventListener('changed', h);
  return () => bus.removeEventListener('changed', h);
}
