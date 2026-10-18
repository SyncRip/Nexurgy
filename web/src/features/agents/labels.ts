// Display text of agent states shared by the wall and the detail page.

import type { TFunction } from '../../i18n/I18nProvider';
import type { AgentIssue, ModelState } from './model';

export const issueLabel = (t: TFunction, issue: AgentIssue) => t(`fl.ag.issue.${issue}`);

export function modelLabel(t: TFunction, m: ModelState): string {
  if (m.kind === 'own') return m.ready ? m.modelId : t('fl.ag.modelUnavailable', { model: m.modelId });
  if (m.kind === 'missing') return t('fl.ag.modelMissing');
  if (!m.modelId) return m.ready === false ? t('fl.ag.noDefaultModel') : t('fl.ag.defaultModel');
  return m.ready === false ? t('fl.ag.defaultUnavailable', { model: m.modelId }) : t('fl.ag.defaultModelNamed', { model: m.modelId });
}

export const modelBad = (m: ModelState) => (m.kind === 'own' ? !m.ready : m.kind === 'missing' ? true : m.ready === false);

/** Knowledge chip text: the scope is project-wide or none (per-collection binding is not stored yet). */
export function knowledgeLabel(t: TFunction, scope: string | undefined, total: number | null | undefined): string {
  if (scope === 'none') return t('fl.ag.kbNone');
  return total === null || total === undefined ? t('fl.ag.kbProject') : t('fl.ag.kbProjectCount', { n: total });
}
