// The AI work panel beside the conversation (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/prd.md §3.1): one frame (header with
// the object, version / status chips, the chat target line, lock, wide / restore, open full page, close) around the
// pane of the open object. Phones show it as a full-screen overlay with a back button. Esc inside the panel closes
// it; focus moves into the panel when an object opens and back to the opener on close.

import { lazy, Suspense, useEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { isTypingTarget } from '../../../hooks/keyboard';
import { useI18n } from '../../../i18n/I18nProvider';
import { DraftPane } from './DraftPane';
import { PaneLoading } from './PaneFrame';
import { PlanPane } from './PlanPane';
import { RunPane } from './RunPane';
import { TaskPane } from './TaskPane';
import { WorkflowPane } from './WorkflowPane';
import { panelParam, useWorkPanel } from './WorkPanelContext';
import './panel.css';

// The model preview pulls in three.js: its own chunk, loaded when a revision opens.
const RevisionPane = lazy(() => import('./RevisionPane'));

export function WorkPanel() {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const ref = useRef<HTMLElement>(null);
  const o = wp.state.object;
  const key = o ? panelParam(o) : '';

  // Focus moves into the panel when an object opens (the frame stays while the pane loads; a pane may move it
  // further, e.g. a node drawer).
  useEffect(() => {
    if (key) ref.current?.focus({ preventScroll: true });
  }, [key]);

  if (!o) return null;

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Escape' || e.defaultPrevented || isTypingTarget(e.target)) return;
    // Dialogs and menus render in portals: their Esc is theirs.
    if (!ref.current?.contains(e.target as Node)) return;
    e.preventDefault();
    wp.close();
  };

  let pane: ReactNode;
  switch (o.kind) {
    case 'workflow':
      pane = <WorkflowPane key={key} workflowId={o.id} />;
      break;
    case 'plan':
      pane = <PlanPane key={key} operationId={o.operationId} />;
      break;
    case 'run':
      pane = <RunPane key={key} runId={o.id} />;
      break;
    case 'revision':
      pane = (
        <Suspense fallback={<PaneLoading />}>
          <RevisionPane key={key} revisionId={o.id} />
        </Suspense>
      );
      break;
    case 'task':
      pane = <TaskPane key={key} taskId={o.id} />;
      break;
    case 'draft':
      pane = <DraftPane key={key} draftId={o.id} />;
      break;
  }

  return (
    <aside ref={ref} className={`wp ${wp.state.wide ? 'wide' : ''}`} aria-label={t('wp.title')} tabIndex={-1} onKeyDown={onKeyDown}>
      {pane}
    </aside>
  );
}
