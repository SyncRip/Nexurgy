import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { BuiltinTeamSkill, TeamConfig } from '../../api/engineeringTeam';
import { I18nProvider } from '../../i18n/I18nProvider';
import { agentTeamEn } from '../../i18n/messages/agentTeam.en';
import { agentTeamZh } from '../../i18n/messages/agentTeam.zh';
import { teamSettingsEn } from '../../i18n/messages/teamSettings.en';
import { teamSettingsZh } from '../../i18n/messages/teamSettings.zh';
import { ENGINEERING_ROLES, type EngineeringRole } from './roles';
import { TeamRoleEditor } from './TeamRoleEditor';
import { TeamSkillLibrary } from './TeamSkillLibrary';

const builtinSkills: BuiltinTeamSkill[] = [{ id: 'builtin-evidence', name_en: 'Keep evidence', name_zh: '保留依据', description_en: 'Separate observations and assumptions.', description_zh: '区分观察与假设。', instructions: 'Preserve <source> exactly.' }];
const config: TeamConfig = {
  agents: Object.fromEntries(ENGINEERING_ROLES.map((role) => [role, { display_name: role === 'modeling' ? 'Geometry specialist' : '', profile: 'Check dimensions', tone: '', personality: '', skill_ids: ['builtin-evidence'] }])) as TeamConfig['agents'],
  skills: [{ id: 'custom-review', name: 'My review method', description: 'Review the evidence', instructions: 'Check source dimensions.' }],
};
const renderRole = (role: EngineeringRole, editing = false, editable = editing, value = config) => renderToStaticMarkup(h(I18nProvider, null, h(TeamRoleEditor, { role, config: value, builtinSkills, editing, editable, onChange: () => {}, onReset: () => {}, onLibrary: () => {} })));
const renderLibrary = (editing: boolean) => renderToStaticMarkup(h(I18nProvider, null, h(TeamSkillLibrary, { config, builtinSkills, editing, editable: editing, onChange: () => {} })));

describe('engineering team role and skill presentation', () => {
  it('shows a custom alias alongside the canonical AI profession', () => {
    const html = renderRole('modeling');
    expect(html).toContain('Geometry specialist');
    expect(html).toContain('3D modeling engineer');
    expect(html).toContain('data-engineering-role="modeling"');
    expect(html).toContain('data-team-editor="modeling"');
  });

  it('keeps configuration and instructions readable when members cannot edit', () => {
    const html = renderRole('manager', false);
    expect(html).toContain('Check dimensions');
    expect(html).not.toMatch(/<(?:input|textarea)\b/);
    expect(html).not.toContain('et-preset');
    expect(html).not.toContain('Reset this employee');
    expect(html).not.toContain('My review method');
    expect(html).toContain('Preserve &lt;source&gt; exactly.');
    expect(html).toContain('Built-in · read-only');
    expect(html).not.toContain('<source>');
  });

  it('explains the real deterministic boundary for process and quotation roles', () => {
    for (const role of ['process', 'quote'] as const) {
      const html = renderRole(role);
      expect(html).toContain('calculated by existing rules');
      expect(html).toContain('they do not change the calculated values');
    }
    expect(renderRole('modeling')).not.toContain('calculated by existing rules');
  });

  it('offers inspectable built-in methods and read-only custom skills without a writable member control', () => {
    const html = renderLibrary(false);
    expect(html).toContain('Keep evidence');
    expect(html).toContain('My review method');
    expect(html).toContain('Inspect skill');
    expect(html).not.toContain('Edit skill');
    expect(html).toContain('Assigned to Project manager');
    expect(html).not.toContain('Create skill');
    expect(html).not.toContain('Remove skill');
    expect(html).not.toMatch(/<(?:input|textarea)\b/);
  });

  it('exposes editable fields, presets and assignment controls only in edit mode', () => {
    const html = renderRole('modeling', true);
    expect(html.match(/<textarea\b/g)).toHaveLength(3);
    expect(html).toContain('Reset this employee');
    expect(html).toContain('et-presets');
    expect(html).toContain('My review method');
    expect(html).toMatch(/<input[^>]*type="checkbox"[^>]*checked=""/);
    expect(html).not.toContain('readOnly=""');
    const busy = renderRole('modeling', true, false);
    expect(busy.match(/readOnly=""/g)).toHaveLength(4);
    expect(busy).toMatch(/<input[^>]*type="checkbox"[^>]*disabled=""[^>]*checked=""/);
    const library = renderLibrary(true);
    expect(library).toContain('Create skill');
    expect(library).toContain('Edit skill');
    expect(library).toContain('Remove skill');
  });

  it('shows truthful unset copy for saved blanks and the canonical name when the alias is blank', () => {
    const value = structuredClone(config);
    value.agents.modeling = { display_name: '  ', profile: '', tone: '', personality: '', skill_ids: [] };
    const html = renderRole('modeling', false, false, value);
    expect(html).toContain('<h2>3D modeling engineer</h2>');
    expect(html.match(/Not set\. Follows the system/g)).toHaveLength(3);
    expect(html).toContain('No skills assigned.');
    expect(html).not.toContain('Check dimensions');
    expect(value.agents.modeling.profile).toBe('');
  });

  it('uses the AI employee name in both settings navigation and assistant disclosure', () => {
    expect(teamSettingsEn['teamSettings.title']).toBe('AI employees');
    expect(teamSettingsZh['teamSettings.title']).toBe('AI 员工');
    expect(agentTeamEn['team.title']).toBe('AI employees');
    expect(agentTeamZh['team.title']).toContain('AI 员工');
    for (const dictionary of [teamSettingsEn, teamSettingsZh, agentTeamEn, agentTeamZh]) {
      expect(Object.values(dictionary).join(' ')).not.toMatch(/Engineering team|工程团队/);
    }
  });
});
