import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { DEFAULT_LIMITS, type AgentProfile } from '../../api/agents';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { AgentProfileEditor } from './AgentProfileEditor';

const profile: AgentProfile = {
  id: 'a1', project_id: 'p1', name: 'Historical agent', purpose: 'execute', description: 'Saved project role',
  role_prompt: 'Write a document.', model_config: null, allowed_tools: ['document'], execution_modes: ['semi_auto'],
  knowledge_scope: { scope: 'none' }, limits: DEFAULT_LIMITS, pricing: null, enabled: true, version: 3,
  created_by: 'u1', created_at: null, updated_at: null, disabled_at: null,
  usage: { runs_active: 0, executions_total: 4 }, runtime: 'workspace_agent',
};

function render(saved: AgentProfile | null, canManage: boolean) {
  return renderToStaticMarkup(createElement(I18nProvider, null,
    createElement(ToastProvider, null,
      createElement(MemoryRouter, null,
        createElement(AgentProfileEditor, {
          project: { id: 'p1', code: 'P1' }, profile: saved, canManage, configs: undefined, configsError: null,
          members: { list: undefined, error: null, reload() {}, byId: new Map(), name: () => 'Ada' },
          usage: undefined, kbTotal: null, onSaved() {}, onBack() {}, onCopy() {}, onReload() {},
        })))));
}

describe('agent profile retirement', () => {
  it('keeps an old workspace profile readable without copy, enable or save actions even for an owner', () => {
    const html = render(profile, true);
    expect(html).toContain('Historical agent');
    expect(html).toContain('Workspace agent');
    expect(html).toContain('This runtime is retired. The saved profile is read-only.');
    expect(html).toContain('<fieldset disabled=""');
    expect(html).not.toContain('Save as v4');
    expect(html).not.toMatch(/> Copy<|> Disable<|> Enable</);
    expect(html).not.toContain('No workflow uses it yet');
    expect(html).not.toContain('Not connected');
  });

  it('keeps supported profiles read-only when collaboration is frozen', () => {
    const html = render({ ...profile, runtime: 'tool_agent' }, false);
    expect(html).toContain('<fieldset disabled=""');
    expect(html).not.toContain('Save as v4');
    expect(html).not.toMatch(/> Copy<|> Disable<|> Enable</);
  });

  it('offers only supported runtimes when creating a profile', () => {
    const html = render(null, true);
    expect(html).toContain('Native loop');
    expect(html).toContain('Tool agent');
    expect(html).toContain('Create agent');
    expect(html).not.toContain('Workspace agent');
  });
});
