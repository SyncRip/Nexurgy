// /resources/:tab — resources & knowledge (docs/resources-knowledge-handoff.zh-CN.md): two main views, materials
// (/resources/files) and experience (/resources/knowledge), plus a project's usage records (/resources/process), all
// under one in-page view scope (features/resources/ResourcesFrame). Old URLs — /resources, the old specs / drafting /
// machines tabs, the asset center (/resources/assets?tab=…) and the old files-view keys — redirect with their query
// mapped (app/nav.tsx resourcesRedirect). The page has its own scope picker, so the top-bar project switcher is hidden.

import { Navigate, useLocation, useParams } from 'react-router-dom';
import { isResourceTab, paths, resourcesRedirect } from '../../app/nav';
import { useHideProjectSwitcher } from '../../components/shell/ShellContext';
import ResourcesFrame from '../../features/resources/ResourcesFrame';

export default function ResourcesPage() {
  const { tab } = useParams();
  const location = useLocation();
  useHideProjectSwitcher();
  const target = resourcesRedirect(tab, location.search);
  if (target || !isResourceTab(tab)) return <Navigate to={target ?? paths.resources()} replace />;
  return <ResourcesFrame view={tab} />;
}
