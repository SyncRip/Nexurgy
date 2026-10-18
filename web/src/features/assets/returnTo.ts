// `return_to` of the asset version page (design §5.3 D07): only an in-app path
// of the same project is honoured — a project section (`/projects/<id>/…`, or an
// R1.2 area page that names the project: `/resources/assets?project=<id>…`, or the
// resources page in its all-projects / shared scope: `/resources/files?scope=all…`) or a
// run / collaboration-run page whose run the caller still has to verify belongs
// to that project. Anything else (external URLs, other projects, protocol
// tricks) is ignored.

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** R1.2 area pages that work on the project named by ?project=. */
const AREA_ROOTS = new Set(['parts', 'flows', 'resources', 'project']);

/** `?scope=` values of the resources page that span projects (features/resources/scope.ts). */
const CROSS_PROJECT_SCOPES = new Set(['all', 'shared']);

export type ReturnTarget =
  | { kind: 'project'; path: string }
  /** A run page: honour it only once `runId` is confirmed to belong to the project. */
  | { kind: 'run'; path: string; runId: string };

export function safeReturnTo(raw: string | null | undefined, projectId: string): ReturnTarget | null {
  if (!raw || !projectId || raw.length > 2000) return null;
  // Absolute paths only: no scheme, no protocol-relative `//`, no control characters.
  if (!raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\') || /[\s\u0000-\u001f]/.test(raw)) return null;
  const pathOnly = raw.split(/[?#]/, 1)[0];
  const segs = pathOnly.split('/').filter(Boolean);
  if (segs.length >= 2 && segs[0] === 'projects' && segs[1] === projectId) return { kind: 'project', path: raw };
  if (AREA_ROOTS.has(segs[0]) && segs.length <= (segs[0] === 'parts' ? 1 : 2)) {
    const query = raw.includes('?') ? raw.slice(raw.indexOf('?') + 1).split('#', 1)[0] : '';
    const params = new URLSearchParams(query);
    if (params.get('project') === projectId) return { kind: 'project', path: raw };
    // The resources page's cross-project scopes (all projects / global shared) name no project but list this one's items.
    if (segs[0] === 'resources' && !params.has('project') && CROSS_PROJECT_SCOPES.has(params.get('scope') ?? '')) return { kind: 'project', path: raw };
    return null;
  }
  if (segs.length >= 2 && segs[0] === 'runs' && UUID_RE.test(segs[1])) return { kind: 'run', path: raw, runId: segs[1] };
  if (segs.length >= 3 && segs[0] === 'collaboration' && segs[1] === 'runs' && UUID_RE.test(segs[2])) return { kind: 'run', path: raw, runId: segs[2] };
  return null;
}
