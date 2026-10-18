// The Agent profile editor's "Tool contract" section (agent execution layer P2; prototype #agent.<id> → Tools): every tool of
// the registry (GET /agent-tools) with its kind badge (write / read-only / acceptance-only / finish) and an expandable parameter table
// built from the same argument schema the executors give the model. Server tools are ticked into allowed_tools as
// before (review-only tools locked unless the purpose may review; the server checks every call anyway); the end tools
// deliver / report_blocked are always available and never ticked. P4: the read-only evidence tools (kind read) are
// listed in their own group, never ticked: every review / pre-review on tool_agent gets them. While the contract
// loads, or when it cannot be read, the known tool names are listed without parameters so the selection keeps working.

import { Lock } from 'lucide-react';
import { useMemo } from 'react';
import { listAgentTools, REVIEW_ONLY_TOOLS, TOOL_NAMES, type AgentToolContract, type ToolKind } from '../../api/agents';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n } from '../../i18n/I18nProvider';
import { schemaParams } from './toolParams';

const KIND_TONE: Record<ToolKind, string> = { write: 'accent', read: 'ok', review_only: 'stale', end: 'measure' };

const fallbackRow = (name: string): AgentToolContract => ({
  name,
  description: '',
  kind: REVIEW_ONLY_TOOLS.has(name) ? 'review_only' : 'write',
  needs_part: false,
  produces_assets: false,
  args_schema: {},
});

export function ToolContract({
  projectId,
  allowed,
  reviewTools,
  onChange,
}: {
  projectId: string;
  allowed: string[];
  /** The profile's purpose may review (review-only tools can be ticked). */
  reviewTools: boolean;
  onChange: (allowed: string[]) => void;
}) {
  const { t } = useI18n();
  const res = useResource((signal) => listAgentTools(projectId, { signal }), [projectId]);
  const rows = useMemo(() => res.data ?? TOOL_NAMES.map(fallbackRow), [res.data]);
  const label = (name: string) => {
    const key = `agents.tool.${name}`;
    return hasKey(key) ? t(key as MessageKey) : name;
  };
  const serverTools = rows.filter((r) => r.kind !== 'end' && r.kind !== 'read');
  // P4: the read-only evidence tools are given to every review / pre-review on tool_agent; never ticked.
  const evidenceTools = rows.filter((r) => r.kind === 'read');
  const endTools = rows.filter((r) => r.kind === 'end');

  const row = (tool: AgentToolContract) => {
    const fixed = tool.kind === 'end' || tool.kind === 'read';
    const fixedNote = t(tool.kind === 'read' ? 'agents.contract.evidence' : 'agents.contract.always');
    const on = fixed || allowed.includes(tool.name);
    const locked = tool.kind === 'review_only' && !reviewTools;
    const id = `ag-tool-${tool.name}`;
    const params = schemaParams(tool.args_schema);
    const notes = [
      locked ? t('agents.toolReviewOnly') : null,
      tool.needs_part ? t('agents.contract.needsPart') : null,
      tool.produces_assets ? t('agents.contract.producesAssets') : null,
    ].filter(Boolean);
    return (
      <li key={tool.name} className={`ag-tool${on ? ' is-on' : ''}`}>
        <span className="ag-tool-pick">
          {fixed ? (
            <Lock aria-label={fixedNote} />
          ) : (
            <input
              type="checkbox"
              id={id}
              checked={on}
              disabled={locked}
              onChange={() => onChange(on ? allowed.filter((x) => x !== tool.name) : [...allowed, tool.name])}
            />
          )}
        </span>
        <div className="ag-tool-main">
          <label htmlFor={fixed ? undefined : id} className="ag-tool-name">
            <b>{label(tool.name)}</b> <code className="mono">{tool.name}</code>
          </label>
          {(notes.length > 0 || fixed) && <div className="faint small">{fixed ? fixedNote : notes.join(' · ')}</div>}
          {res.data && (
            <details className="ag-tool-params">
              <summary>{t('agents.contract.params', { n: params.length })}</summary>
              <p className="small">
                <span className="faint">{t('agents.contract.modelText')}</span> {tool.description}
              </p>
              {params.length ? (
                <div className="table-wrap">
                  <table className="t dense ag-param-table">
                    <thead>
                      <tr>
                        <th>{t('agents.contract.col.name')}</th>
                        <th>{t('agents.contract.col.type')}</th>
                        <th>{t('agents.contract.col.required')}</th>
                        <th>{t('agents.contract.col.description')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {params.map((p) => (
                        <tr key={p.name}>
                          <td>
                            <code className="mono">{p.name}</code>
                          </td>
                          <td className="mono">{p.type}</td>
                          <td>{p.required ? t('agents.contract.required') : <span className="faint">{t('agents.contract.optional')}</span>}</td>
                          <td>
                            {p.description}
                            {p.defaultValue !== undefined && <span className="faint"> {t('agents.contract.default', { v: p.defaultValue })}</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="faint small">{t('agents.contract.noParams')}</p>
              )}
            </details>
          )}
        </div>
        <span className={`chip ${KIND_TONE[tool.kind] ?? 'pending'}`}>{t(`agents.contract.kind.${tool.kind}` as MessageKey)}</span>
      </li>
    );
  };

  return (
    <>
      <p className="ag-section-hint">{t('agents.contract.intro')}</p>
      {res.error ? <ErrorNotice error={res.error} onRetry={res.reload} /> : res.initial && <p className="faint small">{t('agents.contract.loading')}</p>}
      <ul className="ag-tools" role="group" aria-label={t('agents.field.tools')}>
        {serverTools.map(row)}
      </ul>
      {evidenceTools.length > 0 && (
        <>
          <p className="ag-section-hint">{t('agents.contract.evidenceNote')}</p>
          <ul className="ag-tools">{evidenceTools.map(row)}</ul>
        </>
      )}
      {endTools.length > 0 && (
        <>
          <p className="ag-section-hint">{t('agents.contract.endNote')}</p>
          <ul className="ag-tools">{endTools.map(row)}</ul>
        </>
      )}
    </>
  );
}
