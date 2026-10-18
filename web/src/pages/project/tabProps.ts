import type { Project } from '../../api/types';
import type { ProjectCapabilities } from '../../auth/permissions';

/** Shared project data passed into area tabs and retained historical views. */
export interface ProjectTabProps {
  project: Project;
  caps: ProjectCapabilities;
  /** Re-fetch the project when its role or part count changes. */
  reloadProject: () => void;
}
