// Node-type and status icons. Status is always shown as icon + text label; the
// colour (CSS classes in workflow.css) only reinforces it.

import {
  Ban,
  Box,
  Boxes,
  Briefcase,
  Calculator,
  CircleCheck,
  CircleDashed,
  CircleX,
  ClipboardList,
  Clock,
  FileText,
  FileUp,
  Hand,
  LoaderCircle,
  Package,
  ScanText,
  SkipForward,
  Type,
  UserCheck,
  Wand2,
  Workflow,
} from 'lucide-react';
import type { NodeStatus, RunStatus } from '../../api/types';
import { useT } from '../../i18n/I18nProvider';
import { nodeStatusLabel, runStatusLabel } from './labels';

export function NodeTypeIcon({ type }: { type: string }) {
  switch (type) {
    case 'text_input':
      return <Type aria-hidden />;
    case 'file_input':
      return <FileUp aria-hidden />;
    case 'model_input':
      return <Box aria-hidden />;
    case 'model_3d':
      return <Boxes aria-hidden />;
    case 'drawing':
      return <FileText aria-hidden />;
    case 'drawing_intake':
      return <ScanText aria-hidden />;
    case 'drawing_reconstruct':
    case 'concept_design':
      return <Wand2 aria-hidden />;
    case 'process_plan':
    case 'assembly_process_plan':
      return <ClipboardList aria-hidden />;
    case 'quote':
    case 'assembly_quote':
      return <Calculator aria-hidden />;
    case 'human_review':
      return <UserCheck aria-hidden />;
    case 'export':
    case 'manufacturing_export':
      return <Package aria-hidden />;
    // One business task of a collaboration workflow (design §3.3).
    case 'business_task':
      return <Briefcase aria-hidden />;
    default:
      return <Workflow aria-hidden />;
  }
}

export function NodeStatusIcon({ status }: { status: NodeStatus | RunStatus }) {
  switch (status) {
    case 'running':
      return <LoaderCircle aria-hidden className="wf-spin" />;
    case 'ready':
    case 'queued':
      return <Clock aria-hidden />;
    case 'waiting_human':
    case 'waiting':
      return <Hand aria-hidden />;
    case 'succeeded':
      return <CircleCheck aria-hidden />;
    case 'failed':
    case 'partial': // S6: a required step failed — the run reads failed (its products show their own states)
      return <CircleX aria-hidden />;
    case 'cancelled':
      return <Ban aria-hidden />;
    case 'skipped':
      return <SkipForward aria-hidden />;
    default:
      return <CircleDashed aria-hidden />;
  }
}

export function NodeStatusChip({ status, compact }: { status: NodeStatus; compact?: boolean }) {
  const t = useT();
  return (
    <span className={`wf-status ns-${status} ${compact ? 'compact' : ''}`}>
      <NodeStatusIcon status={status} />
      <span>{nodeStatusLabel(t, status)}</span>
    </span>
  );
}

export function RunStatusChip({ status, large }: { status: RunStatus; large?: boolean }) {
  const t = useT();
  return (
    <span className={`wf-status rs-${status} ${large ? 'large' : ''}`}>
      <NodeStatusIcon status={status} />
      <span>{runStatusLabel(t, status)}</span>
    </span>
  );
}
