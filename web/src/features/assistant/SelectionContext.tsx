// Selection capture declares local typed excerpts; it never writes conversation history.
import { useEffect } from 'react';
import type { AiExcerpt } from '../../api/assistant';
import { useAssistant } from './AssistantProvider';
import { clampExcerpt,isExcerptSourceKind,textExcerptRef } from './refs';

const ATTR = 'data-ai-source';

export interface AiSourceAttrs {
  'data-ai-source': AiExcerpt['source_kind'];
  'data-ai-source-id': string;
  'data-ai-project': string;
  'data-ai-task'?: string;
}

/** Mark a container whose text may be quoted to the assistant (spread onto the element). */
export function aiSourceProps(kind: AiExcerpt['source_kind'], id: string, projectId: string, taskId?: string | null): AiSourceAttrs {
  const p: AiSourceAttrs = { 'data-ai-source': kind, 'data-ai-source-id': id, 'data-ai-project': projectId };
  if (taskId) p['data-ai-task'] = taskId;
  return p;
}

interface Found {
  kind: AiExcerpt['source_kind'];
  id: string;
  projectId: string;
  taskId: string | null;
  text: string;
  truncated: boolean;
  rect: { top: number; bottom: number; left: number; right: number };
}

function elementOf(n: Node | null): Element | null {
  if (!n) return null;
  return n.nodeType === Node.ELEMENT_NODE ? (n as Element) : n.parentElement;
}

/** The selection, when it lies entirely inside one marked container (and not in a form field). */
function readSelection(): Found | null {
  if (typeof window === 'undefined') return null;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  const common = elementOf(range.commonAncestorContainer);
  const box = common?.closest(`[${ATTR}]`);
  if (!box || common?.closest('input, textarea, select, [contenteditable="true"]')) return null;
  const kind = box.getAttribute(ATTR);
  const id = box.getAttribute('data-ai-source-id');
  const projectId = box.getAttribute('data-ai-project');
  if (!isExcerptSourceKind(kind) || !id || !projectId) return null;
  const c = clampExcerpt(sel.toString());
  if (!c) return null;
  let r = range.getBoundingClientRect();
  if (!r.width && !r.height) {
    const rects = range.getClientRects();
    if (rects.length) r = rects[rects.length - 1];
  }
  if (!r.width && !r.height) return null;
  return { kind, id, projectId, taskId: box.getAttribute('data-ai-task'), text: c.text, truncated: c.truncated, rect: { top: r.top, bottom: r.bottom, left: r.left, right: r.right } };
}


export function AutoSelectionCapture() {
  const { queuePending } = useAssistant();
  useEffect(() => {
    const onSelect = () => {
      const found = readSelection();
      if (!found) return;
      const input = textExcerptRef(found.projectId, found.kind, found.id, found.text);
      if (input) queuePending({ input, label: `“${found.text.slice(0, 60)}${found.text.length > 60 ? '…' : ''}”`, target: true, automatic: true });
    };
    document.addEventListener('selectionchange', onSelect);
    return () => document.removeEventListener('selectionchange', onSelect);
  }, [queuePending]);
  return null;
}
