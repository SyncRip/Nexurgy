// Props shared by the task detail sections.

import type { ProjectFull, TaskDetail } from '../../../api/pm';
import type { Members } from '../common';

export interface TaskSectionProps {
  task: TaskDetail;
  /** Replace the task after a mutation that returned the full detail. */
  setTask: (t: TaskDetail) => void;
  /** Re-fetch the task (after mutations that return something else). */
  reloadTask: () => void;
  project: ProjectFull | undefined;
  members: Members;
  /** Project accepts new runs / deliverables / baselines / submissions. */
  projectActive: boolean;
  /** Project plan may be edited (draft / active / on_hold and not archived). */
  projectPlannable: boolean;
}

export const LOCKED_STATUSES = new Set(['in_review', 'done']);
