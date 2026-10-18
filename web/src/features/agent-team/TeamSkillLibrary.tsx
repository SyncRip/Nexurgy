import { BookOpen, Pencil, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import type { BuiltinTeamSkill, TeamConfig, TeamSkill } from '../../api/engineeringTeam';
import { ConfirmDialog, Dialog } from '../../components/ui/Dialog';
import { LimitedInput, LimitedTextarea } from '../../components/ui/LimitedText';
import { useI18n } from '../../i18n/I18nProvider';
import { ENGINEERING_ROLES } from './roles';
import { MAX_CUSTOM_SKILLS, removeTeamSkill } from './settingsModel';
import { TeamProfileValue, TeamSkillInstructions, teamSkillViews } from './TeamRoleEditor';

function SkillDraftDialog({ skill, editable, onApply, onClose }: { skill: TeamSkill | null; editable: boolean; onApply: (skill: TeamSkill) => void; onClose: () => void }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<TeamSkill>(() => skill ?? { id: `custom-${crypto.randomUUID()}`, name: '', description: '', instructions: '' });
  const [invalid, setInvalid] = useState(false);
  const patch = (change: Partial<TeamSkill>) => { if (editable) { setDraft((previous) => ({ ...previous, ...change })); setInvalid(false); } };
  const apply = () => {
    if (!editable) return;
    if (!draft.name.trim() || !draft.instructions.trim()) { setInvalid(true); return; }
    onApply(draft);
  };
  return <Dialog title={t(!editable ? 'teamSettings.inspectSkill' : skill ? 'teamSettings.editSkill' : 'teamSettings.addSkill')} subtitle={editable ? t('teamSettings.draftHint') : undefined} onClose={onClose} size="lg" onSubmit={editable ? apply : undefined} footer={<><button type="button" className="btn" onClick={onClose}>{t(editable ? 'common.cancel' : 'common.close')}</button>{editable && <button type="submit" className="btn primary">{t(skill ? 'teamSettings.updateDraft' : 'teamSettings.addToDraft')}</button>}</>}>
    <div className="et-skill-form">
      {editable ? <>
      <label className="et-field"><span>{t('teamSettings.skillName')}</span><LimitedInput limit="team_skill_name" className="input" value={draft.name} readOnly={!editable} onChange={(e) => patch({ name: e.target.value })} required aria-invalid={invalid && !draft.name.trim()} /></label>
      <label className="et-field"><span>{t('teamSettings.skillDescription')}</span><LimitedTextarea limit="team_skill_description" className="input" rows={2} value={draft.description} readOnly={!editable} onChange={(e) => patch({ description: e.target.value })} /></label>
      <label className="et-field"><span>{t('teamSettings.skillInstructions')}</span><LimitedTextarea limit="team_skill_instructions" className="input" rows={10} value={draft.instructions} placeholder={t('teamSettings.skillInstructionsPlaceholder')} readOnly={!editable} onChange={(e) => patch({ instructions: e.target.value })} required aria-invalid={invalid && !draft.instructions.trim()} /></label>
      </> : <>
        <TeamProfileValue label={t('teamSettings.skillName')} value={draft.name} empty={t('teamSettings.unset')} />
        {draft.description && <TeamProfileValue label={t('teamSettings.skillDescription')} value={draft.description} empty={t('teamSettings.unset')} />}
        <TeamProfileValue label={t('teamSettings.skillInstructions')} value={draft.instructions} empty={t('teamSettings.unset')} />
      </>}
      <p className="et-hint">{t('teamSettings.skillsBoundary')}</p>
      {invalid && <p className="et-validation" role="alert">{t('teamSettings.skillRequired')}</p>}
    </div>
  </Dialog>;
}

export function TeamSkillLibrary({ config, builtinSkills, editing, editable, onChange }: { config: TeamConfig; builtinSkills: BuiltinTeamSkill[]; editing: boolean; editable: boolean; onChange: (config: TeamConfig) => void }) {
  const { t, lang } = useI18n();
  const [selectedSkill, setSelectedSkill] = useState<TeamSkill | 'new' | null>(null);
  const [removing, setRemoving] = useState<TeamSkill | null>(null);
  const skills = teamSkillViews(config, builtinSkills, lang);
  const full = config.skills.length >= MAX_CUSTOM_SKILLS;
  const usage = (id: string) => {
    const names = ENGINEERING_ROLES.filter((role) => config.agents[role].skill_ids.includes(id)).map((role) => t(`team.role.${role}`));
    return names.length ? t('teamSettings.usedBy', { roles: names.join(lang === 'zh' ? '、' : ', ') }) : t('teamSettings.unassigned');
  };
  return <section className="et-library" aria-label={t('teamSettings.library')}>
    <div className="et-library-head"><div><h2>{t('teamSettings.library')}</h2><p>{t('teamSettings.skillsBoundary')}</p></div>{editing && <button type="button" className="btn primary" onClick={() => { if (editable && !full) setSelectedSkill('new'); }} disabled={!editable || full}><Plus />{t('teamSettings.addSkill')}</button>}</div>
    <div className="et-section-heading"><h3>{t('teamSettings.customSkills')}</h3><span>{t('teamSettings.customCount', { count: config.skills.length, max: MAX_CUSTOM_SKILLS })}</span></div>
    {full && <p className="et-hint">{t('teamSettings.customLimit')}</p>}
    {config.skills.length === 0 && <div className="et-library-empty"><BookOpen aria-hidden /><div><strong>{t('teamSettings.customEmpty')}</strong><p>{t('teamSettings.customEmptyHint')}</p></div></div>}
    <div className="et-library-grid">
      {config.skills.map((skill) => <article className="et-library-card" key={skill.id} data-team-skill={skill.id}>
        <header><h4>{skill.name}</h4><span className="et-kind">{t('teamSettings.custom')}</span></header>
        {skill.description && <p>{skill.description}</p>}
        <small>{usage(skill.id)}</small><TeamSkillInstructions instructions={skill.instructions} />
        <div className="et-library-actions"><button type="button" className="btn sm" disabled={editing && !editable} onClick={() => setSelectedSkill(skill)}>{editing ? <Pencil /> : <BookOpen />}{t(editing ? 'teamSettings.editSkill' : 'teamSettings.inspectSkill')}</button>{editing && <button type="button" className="btn sm ghost" disabled={!editable} onClick={() => { if (editable) setRemoving(skill); }}><Trash2 />{t('teamSettings.removeSkill')}</button>}</div>
      </article>)}
    </div>
    <div className="et-section-heading"><h3>{t('teamSettings.builtinSkills')}</h3><span>{t('teamSettings.builtin')}</span></div>
    <div className="et-library-grid">
      {skills.filter((skill) => skill.builtin).map((skill) => <article className="et-library-card" key={skill.id} data-team-skill={skill.id}><header><h4>{skill.name}</h4></header><p>{skill.description}</p><small>{usage(skill.id)}</small><TeamSkillInstructions instructions={skill.instructions} /></article>)}
    </div>
    {selectedSkill !== null && <SkillDraftDialog skill={selectedSkill === 'new' ? null : selectedSkill} editable={editing && editable} onClose={() => setSelectedSkill(null)} onApply={(skill) => {
      if (!editing || !editable || (selectedSkill === 'new' && full)) return;
      const updated = config.skills.some((item) => item.id === skill.id) ? config.skills.map((item) => item.id === skill.id ? skill : item) : [...config.skills, skill];
      onChange({ ...config, skills: updated });
      setSelectedSkill(null);
    }} />}
    {removing && <ConfirmDialog title={t('teamSettings.removeTitle')} body={t('teamSettings.removeBody', { name: removing.name })} confirmLabel={t('teamSettings.removeSkill')} danger onClose={() => setRemoving(null)} onConfirm={() => { if (editable) onChange(removeTeamSkill(config, removing.id)); setRemoving(null); }} />}
  </section>;
}
