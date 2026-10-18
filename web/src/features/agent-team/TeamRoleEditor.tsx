import { BookOpen, RotateCcw } from 'lucide-react';
import type { BuiltinTeamSkill, TeamAgentConfig, TeamConfig } from '../../api/engineeringTeam';
import { LimitedInput, LimitedTextarea } from '../../components/ui/LimitedText';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleBadge } from './AgentRole';
import type { EngineeringRole } from './roles';
import { MAX_ROLE_SKILLS, toggleTeamSkill } from './settingsModel';

export interface TeamSkillView { id: string; name: string; description: string; instructions: string; builtin: boolean }
export function teamSkillViews(config: TeamConfig, builtin: BuiltinTeamSkill[], lang: 'en' | 'zh'): TeamSkillView[] {
  return [...builtin.map((skill) => ({ id: skill.id, name: lang === 'zh' ? skill.name_zh : skill.name_en, description: lang === 'zh' ? skill.description_zh : skill.description_en, instructions: skill.instructions, builtin: true })),
    ...config.skills.map((skill) => ({ ...skill, builtin: false }))];
}

export function TeamSkillInstructions({ instructions }: { instructions: string }) {
  const { t } = useI18n();
  return <details className="et-skill-instructions"><summary>{t('teamSettings.skillMethod')}</summary><pre>{instructions}</pre></details>;
}

export function TeamProfileValue({ label, value, empty, field }: { label: string; value: string; empty: string; field?: string }) {
  return <div className="et-profile-field" data-team-field={field}><h3>{label}</h3><p className={`et-profile-copy${value.trim() ? '' : ' unset'}`}>{value.trim() ? value : empty}</p></div>;
}

export function TeamRoleEditor({ role, config, builtinSkills, editing, editable, onChange, onReset, onLibrary }: {
  role: EngineeringRole;
  config: TeamConfig;
  builtinSkills: BuiltinTeamSkill[];
  editing: boolean;
  editable: boolean;
  onChange: (config: TeamConfig) => void;
  onReset: () => void;
  onLibrary: () => void;
}) {
  const { t, lang } = useI18n();
  const agent = config.agents[role];
  const patch = (values: Partial<TeamAgentConfig>) => {
    if (editable) onChange({ ...config, agents: { ...config.agents, [role]: { ...agent, ...values } } });
  };
  const skills = teamSkillViews(config, builtinSkills, lang).filter((skill) => editing || agent.skill_ids.includes(skill.id));
  const limitReached = agent.skill_ids.length >= MAX_ROLE_SKILLS;
  return <section className="et-editor panel" data-team-editor={role} aria-label={t(`team.role.${role}`)}>
    <header className="et-editor-head">
      <div><AgentRoleBadge role={role} /><h2>{agent.display_name.trim() || t(`team.role.${role}`)}</h2><p>{t(`team.role.${role}.description`)}</p></div>
      {editing && <button type="button" className="btn sm ghost" onClick={onReset} disabled={!editable}><RotateCcw />{t('teamSettings.reset')}</button>}
    </header>
    {(role === 'process' || role === 'quote') && <p className="et-effect et-calculation">{t('teamSettings.deterministic')}</p>}
    {editing ? <><div className="et-form-section">
      <label className="et-field"><span>{t('teamSettings.alias')}</span><LimitedInput className="input" limit="team_display_name" value={agent.display_name} placeholder={t(`team.role.${role}`)} readOnly={!editable} onChange={(e) => patch({ display_name: e.target.value })} /><small>{t('teamSettings.aliasHint')}</small></label>
      <label className="et-field"><span>{t('teamSettings.profile')}</span><LimitedTextarea className="input" limit="team_profile" rows={6} value={agent.profile} placeholder={t('teamSettings.profilePlaceholder')} readOnly={!editable} onChange={(e) => patch({ profile: e.target.value })} /><small>{t('teamSettings.profileHint')}</small></label>
    </div>
    <div className="et-form-section">
      <h3>{t('teamSettings.style')}</h3>
      <div className="et-style-grid">
        <div>
          <label className="et-field"><span>{t('teamSettings.tone')}</span><LimitedTextarea className="input" limit="team_tone" rows={3} value={agent.tone} placeholder={t('teamSettings.tonePlaceholder')} readOnly={!editable} onChange={(e) => patch({ tone: e.target.value })} /></label>
          <div className="et-presets" aria-label={`${t('teamSettings.tone')} · ${t('teamSettings.presets')}`}>
            {(['concise', 'professional', 'guiding'] as const).map((preset) => <button type="button" key={preset} className="et-preset" disabled={!editable} aria-pressed={agent.tone === t(`teamSettings.preset.${preset}.value`)} onClick={() => patch({ tone: t(`teamSettings.preset.${preset}.value`) })}>{t(`teamSettings.preset.${preset}`)}</button>)}
          </div>
        </div>
        <div>
          <label className="et-field"><span>{t('teamSettings.personality')}</span><LimitedTextarea className="input" limit="team_personality" rows={3} value={agent.personality} placeholder={t('teamSettings.personalityPlaceholder')} readOnly={!editable} onChange={(e) => patch({ personality: e.target.value })} /></label>
          <div className="et-presets" aria-label={`${t('teamSettings.personality')} · ${t('teamSettings.presets')}`}>
            {(['careful', 'proactive', 'collaborative'] as const).map((preset) => <button type="button" key={preset} className="et-preset" disabled={!editable} aria-pressed={agent.personality === t(`teamSettings.preset.${preset}.value`)} onClick={() => patch({ personality: t(`teamSettings.preset.${preset}.value`) })}>{t(`teamSettings.preset.${preset}`)}</button>)}
          </div>
        </div>
      </div>
      <p className="et-hint">{t('teamSettings.presets')}</p>
    </div></> : <>
      <div className="et-form-section"><TeamProfileValue label={t('teamSettings.profile')} value={agent.profile} empty={t('teamSettings.unset')} field="profile" /></div>
      <div className="et-form-section et-style-grid">
        <TeamProfileValue label={t('teamSettings.tone')} value={agent.tone} empty={t('teamSettings.unset')} field="tone" />
        <TeamProfileValue label={t('teamSettings.personality')} value={agent.personality} empty={t('teamSettings.unset')} field="personality" />
      </div>
    </>}
    <div className="et-form-section">
      <div className="et-section-heading"><h3>{t('teamSettings.skills')}</h3><span>{t(editing ? 'teamSettings.skillCount' : 'teamSettings.assignedCount', { count: agent.skill_ids.length, max: MAX_ROLE_SKILLS })}</span><button type="button" className="btn sm ghost" onClick={onLibrary}><BookOpen />{t('teamSettings.library')}</button></div>
      {editing && <p className="et-hint">{t('teamSettings.skillHint')}</p>}
      {editing && limitReached && <p className="et-hint">{t('teamSettings.skillLimit')}</p>}
      {!editing && skills.length === 0 && <p className="et-hint">{t('teamSettings.skillsEmpty')}</p>}
      <div className="et-skill-choices">
        {skills.map((skill) => {
          const selected = agent.skill_ids.includes(skill.id);
          return <div key={skill.id} className={`et-skill-choice${selected ? ' selected' : ''}`} data-team-skill={skill.id}>
            {editing ? <label className="et-skill-label"><input type="checkbox" checked={selected} disabled={!editable || (!selected && limitReached)} onChange={() => { if (editable) onChange(toggleTeamSkill(config, role, skill.id)); }} /><span><strong>{skill.name}</strong><small>{skill.builtin ? t('teamSettings.builtin') : t('teamSettings.custom')}</small><span>{skill.description}</span></span></label>
              : <div className="et-skill-label et-skill-assigned"><span><strong>{skill.name}</strong><small>{skill.builtin ? t('teamSettings.builtin') : t('teamSettings.custom')}</small><span>{skill.description}</span></span></div>}
            <TeamSkillInstructions instructions={skill.instructions} />
          </div>;
        })}
      </div>
    </div>
  </section>;
}
