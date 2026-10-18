import type { FeatureName } from '../api/types';

/** List and creation routes only: object URLs remain readable, including their original query parameters. */
export function routeFeature(pathname: string, search = ''): FeatureName | null {
  const query = new URLSearchParams(search);
  if (/^\/projects\/[^/]+\/collaboration\/new\/?$/.test(pathname)) return 'collaboration';
  if (/^\/flows\/agents\/?$/.test(pathname)) {
    const agent = query.get('agent');
    if (!agent || agent === 'new' || query.has('copy')) return 'collaboration';
  }
  if (/^\/flows\/(collaboration|executions)\/?$/.test(pathname)) return 'collaboration';
  if (/^\/(flows\/library|template-library)\/?$/.test(pathname)) return 'template_library';
  if (/^\/project\/tasks\/?$/.test(pathname)) return 'tasks';
  if (/^\/project\/overview\/?$/.test(pathname)) return 'stages';
  // These historical details share their list route; exact object links stay readable.
  if (/^\/resources\/knowledge\/?$/.test(pathname) && !query.get('collection_id')) return 'collaboration';
  if (/^\/resources\/process\/?$/.test(pathname) && !query.get('run_id') && !query.get('node_id')) return 'collaboration';
  return null;
}

export function flowTabFeature(tab: string): FeatureName | null {
  return tab === 'library' ? 'template_library' : ['collaboration', 'agents', 'executions'].includes(tab) ? 'collaboration' : null;
}

export function projectTabFeature(tab: string): FeatureName | null {
  return tab === 'tasks' ? 'tasks' : tab === 'overview' ? 'stages' : null;
}
