import { FilesPanel } from '../../components/files/FilesPanel';
import type { ProjectTabProps } from './tabProps';

export function ProjectFilesTab({ project, caps }: ProjectTabProps) {
  return <FilesPanel project={project} caps={caps} />;
}
