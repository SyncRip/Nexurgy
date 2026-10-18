import { useCallback, useState } from 'react';
import type { RunProgress } from '../../api/progress';
import { progressDisclosure, type DisclosureState } from './model';

export function useProgressDisclosure(runId: string, progress: RunProgress | null, active: boolean) {
  const [saved, setSaved] = useState<DisclosureState | null>(null);
  const state = progressDisclosure(saved, runId, progress, active);
  if (saved !== state) setSaved(state);
  const setExpanded = useCallback((open: boolean) => setSaved((previous) => previous ? { ...previous, open } : previous), []);
  return { expanded: state.open, setExpanded };
}
