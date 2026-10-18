// Part workspace tab registry — the extension point for the model viewer / CAD
// editor, drawings, process, quote, history and runs panels.
//
// To connect a panel, replace its `component` here, preferably with
// React.lazy(() => import('./ModelPanel')) so heavy code (three.js, the
// editor) is split into its own chunk; the page renders it inside <Suspense>.
// Every panel receives PartTabProps and must handle its own loading / empty /
// error states. The page itself (PartWorkspace.tsx) does not need to change.

import { Box,Calculator,ClipboardList,FileText,FolderOpen,Workflow } from 'lucide-react';
import { lazy,type ComponentType,type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { Part,Project } from '../../api/types';
import type { ProjectCapabilities } from '../../auth/permissions';
import { FilesPanel } from '../../components/files/FilesPanel';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';

export const PART_TAB_IDS = ['workflow', 'model', 'drawings', 'process', 'quote', 'files'] as const;
export type PartTabId = (typeof PART_TAB_IDS)[number];

export interface PartTabProps {
  part: Part;
  project: Project;
  /** What the signed-in user may do in this project (already accounts for archived projects). */
  caps: ProjectCapabilities;
  /** Replace the part after a mutation that returned it (keeps the header in sync). */
  onPartChange: (part: Part) => void;
  /** Re-fetch the part (e.g. after saving a revision, so latest_revision updates). */
  reloadPart: () => void;
  /** Workflow tab only: the part bar's free space, where the canvas puts its controls. */
  barSlot?: HTMLElement | null;
}

export interface PartTabDef {
  id: PartTabId;
  labelKey: MessageKey;
  icon: ReactNode;
  component: ComponentType<PartTabProps>;
}

// Heavy panels (three.js viewer, CAD editor) are split into their own chunks.
const ModelTab = lazy(() => import('../../features/part/model/ModelTab'));
const DrawingsTab = lazy(() => import('../../features/part/DrawingsTab'));
const ProcessTab = lazy(() => import('../../features/part/ProcessTab'));
const QuoteTab = lazy(() => import('../../features/part/QuoteTab'));
const HistoryTab = lazy(() => import('../../features/part/HistoryTab'));
// The part's workflows on the run canvas.
const WorkflowTab = lazy(() => import('../../features/part/flow/PartFlowTab'));

function PartFilesTab(props: PartTabProps) {
  const { t } = useI18n();
  const [search] = useSearchParams();
  return <div className="stack"><FilesPanel project={props.project} caps={props.caps} part={props.part} /><details open={search.get('section') === 'history'}><summary>{t('core.part.history')}</summary><HistoryTab {...props} /></details></div>;
}

export const PART_TABS: PartTabDef[] = [
  { id: 'workflow', labelKey: 'part.tab.workflow', icon: <Workflow />, component: WorkflowTab },
  { id: 'model', labelKey: 'part.tab.model', icon: <Box />, component: ModelTab },
  { id: 'drawings', labelKey: 'part.tab.drawings', icon: <FileText />, component: DrawingsTab },
  { id: 'process', labelKey: 'part.tab.process', icon: <ClipboardList />, component: ProcessTab },
  { id: 'quote', labelKey: 'part.tab.quote', icon: <Calculator />, component: QuoteTab },
  { id: 'files', labelKey: 'core.part.filesHistory', icon: <FolderOpen />, component: PartFilesTab },
];

export const DEFAULT_PART_TAB: PartTabId = 'model';

/** Default tab for a link: ?rev=<revision> links (assistant results) open that revision on the model tab. */
export function defaultPartTab(search: string): PartTabId {
  return new URLSearchParams(search).has('rev') ? 'model' : DEFAULT_PART_TAB;
}

export function isPartTab(v: string | undefined): v is PartTabId {
  return !!v && (PART_TAB_IDS as readonly string[]).includes(v);
}
