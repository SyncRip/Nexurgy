// Old project URLs (/projects/:id, /projects/:id/:section, /projects/:id/knowledge)
// → the R1.2 area that now shows that section, naming the project so it
// becomes current. Query strings (monitor filters, asset center state) are kept.

import { Navigate, useLocation, useParams } from 'react-router-dom';
import { legacyProjectUrl } from '../../app/nav';

export default function LegacyProjectRedirect() {
  const { projectId = '', section } = useParams();
  const { search } = useLocation();
  return <Navigate to={legacyProjectUrl(projectId, section, search)} replace />;
}
