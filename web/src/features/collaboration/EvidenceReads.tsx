// What a tool_agent review read for one criterion (agent execution layer P4, prototype #review.<id>): each evidence
// tool call — the version, the page and sections read, or the measured items — numbered by its call in the attempt
// (the execution timeline shows the same calls). Used by the verdict table (acceptance detail, node detail) and the
// execution timeline's verdict entry.

import type { EvidenceRead } from '../../api/collaboration';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';

export function EvidenceReads({ reads }: { reads: EvidenceRead[] }) {
  const { t } = useI18n();
  const toolLabel = (tool: string) => {
    const key = `collab.verdict.tool.${tool}`;
    return hasKey(key) ? t(key as MessageKey) : tool;
  };
  return (
    <details className="cl-evidence">
      <summary>{t('collab.verdict.consulted', { n: reads.length })}</summary>
      <ul className="cl-evidence-list">
        {reads.map((r) => (
          <li key={`${r.call_index}-${r.asset_version_id}`}>
            <span className="mono faint" title={t('collab.verdict.call', { n: r.call_index + 1 })}>
              #{r.call_index + 1}
            </span>{' '}
            {toolLabel(r.tool)} <b>{r.label ?? r.asset_version_id.slice(0, 8)}</b>
            {r.page != null && r.pages != null && <span className="faint"> · {t('collab.verdict.page', { page: r.page, pages: r.pages })}</span>}
            {r.sections && r.sections.length > 0 && <span className="faint"> · {t('collab.verdict.sections', { list: r.sections.join('、') })}</span>}
            {r.items && r.items.length > 0 && (
              <span className="faint"> · {t('collab.verdict.items', { list: r.items.slice(0, 8).join('、') + (r.items.length > 8 ? ` +${r.items.length - 8}` : '') })}</span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
