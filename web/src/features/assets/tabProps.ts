// Props of a resources tab body (process resources, knowledge, asset catalogue):
// the project tab props plus an optional callback that receives the server
// count of the unfiltered list.

import type { ProjectTabProps } from '../../pages/project/tabProps';

export interface AssetTabContentProps extends ProjectTabProps {
  onCount?: (n: number | null) => void;
}
