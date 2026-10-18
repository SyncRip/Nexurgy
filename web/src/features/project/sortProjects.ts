import type { Project } from '../../api/types';

export type ProjectSort = 'updated' | 'name' | 'code';

/** Sort only loaded projects; break ties by code and ID to keep positions stable on refresh. */
export function sortProjects<T extends Pick<Project, 'id' | 'name' | 'code' | 'updated_at'>>(projects: readonly T[], sort: ProjectSort, locale: string): T[] {
  const compare = new Intl.Collator(locale, { numeric: true, sensitivity: 'base' }).compare;
  return [...projects].sort((a, b) => {
    const primary = sort === 'updated'
      ? (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0)
      : compare(a[sort], b[sort]);
    return primary || compare(a.code, b.code) || a.id.localeCompare(b.id);
  });
}
