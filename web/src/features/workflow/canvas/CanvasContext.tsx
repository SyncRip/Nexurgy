// Context read by the custom React Flow node components (editor and run view).
// Keeping catalog, errors and run state here keeps node `data` small: only the
// graph fields (type, params, label) live on the nodes themselves.

import { createContext, useContext } from 'react';
import type { GraphError, NodeRun } from '../../../api/types';
import type { Lookups } from '../lookups';
import type { DefMap } from '../model';

export interface CanvasCtx {
  defs: DefMap;
  mode: 'edit' | 'run';
  readOnly: boolean;
  errorsByNode: ReadonlyMap<string, GraphError[]>;
  connectedInputs: ReadonlyMap<string, ReadonlySet<string>>;
  nodeRuns?: ReadonlyMap<string, NodeRun>;
  lookups?: Lookups;
  /** Run view: open a node's details (the waiting-step button on the card). */
  onOpenNode?: (id: string) => void;

}

const Ctx = createContext<CanvasCtx | null>(null);

export const CanvasProvider = Ctx.Provider;

export function useCanvas(): CanvasCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error('useCanvas must be used inside <CanvasProvider>');
  return v;
}
