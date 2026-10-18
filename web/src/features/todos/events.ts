const TODOS_CHANGED = 'partroute:todos-changed';

export const notifyTodosChanged = () => window.dispatchEvent(new Event(TODOS_CHANGED));
export function onTodosChanged(listener: () => void): () => void {
  window.addEventListener(TODOS_CHANGED, listener);
  return () => window.removeEventListener(TODOS_CHANGED, listener);
}
