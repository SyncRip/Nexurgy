import { AlertTriangle, BookOpen, Check, LockKeyhole, Pencil, RefreshCw, Save, Users } from 'lucide-react';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { isAbortError, isApiError } from '../../api/client';
import { getEngineeringTeam, updateEngineeringTeam, type TeamConfig } from '../../api/engineeringTeam';
import { inputLimit, useLimits } from '../../api/limits';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { ErrorNotice, ErrorState, SkeletonRows } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import type { ProjectTabProps } from '../../pages/project/tabProps';
import { AgentRoleIcon } from './AgentRole';
import { ENGINEERING_ROLES } from './roles';
import { createTeamDraftStore, initialTeamEditor, resetTeamRole, teamConfigCharacters, teamDirty, teamEditorReducer, teamRoleFromQuery } from './settingsModel';
import { TeamRoleEditor } from './TeamRoleEditor';
import { TeamSkillLibrary } from './TeamSkillLibrary';
import './teamSettings.css';

const navigationDrafts = createTeamDraftStore();

/** Project identity owns the draft and request lifecycle, including when the area frame reuses its tab. */
export default function TeamSettings(props: ProjectTabProps) {
  const { user } = useAuth();
  return user ? <ProjectTeamSettings key={`${user.id}:${props.project.id}`} userId={user.id} {...props} /> : null;
}

function ProjectTeamSettings({ project, caps, userId }: ProjectTabProps & { userId: string }) {
  const { t } = useI18n();
  const limits = useLimits();
  const [params, setParams] = useSearchParams();
  const role = teamRoleFromQuery(params.get('role'));
  const library = params.get('view') === 'skills';
  const projectId = project.id;
  const [state, dispatch] = useReducer(teamEditorReducer, null, () => initialTeamEditor(projectId, caps.manage));
  const [confirm, setConfirm] = useState<'reload' | 'discard' | 'reset' | null>(null);
  const loadController = useRef<AbortController | null>(null);
  const saveController = useRef<AbortController | null>(null);
  const reload = useCallback((preserveDraft = false) => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    dispatch({ type: 'loading', projectId });
    void getEngineeringTeam(projectId, { signal: controller.signal }).then((value) => {
      // Fetch first: cached configuration is never shown after access has been revoked.
      if (!controller.signal.aborted) dispatch({ type: 'loaded', projectId, value, preserved: preserveDraft ? navigationDrafts.read(userId, projectId) : undefined });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted && !isAbortError(error)) dispatch({ type: 'failed', projectId, error });
    });
  }, [projectId, userId]);
  useEffect(() => {
    reload(true);
    return () => { loadController.current?.abort(); saveController.current?.abort(); };
  }, [reload]);
  useEffect(() => { navigationDrafts.remember(userId, state); }, [userId, state]);
  useEffect(() => { dispatch({ type: 'permission', projectId, canManage: caps.manage }); }, [projectId, caps.manage]);
  const dirty = teamDirty(state.saved?.config, state.draft);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);

  const pending = state.loading || state.busy;
  const editing = caps.manage && state.editing;
  const editable = editing && !pending;
  const conflict = isApiError(state.error) && state.error.code === 'version_conflict';
  const total = state.draft ? teamConfigCharacters(state.draft) : 0;
  const maxTotal = inputLimit(limits, 'team_total');
  const tooLong = maxTotal !== undefined && total > maxTotal;
  const edit = (config: TeamConfig) => { if (editable) dispatch({ type: 'edit', projectId, config }); };
  const save = async () => {
    if (!editable || !dirty || !state.saved || !state.draft || conflict || tooLong) return;
    const controller = new AbortController();
    saveController.current = controller;
    dispatch({ type: 'saving', projectId });
    try {
      const value = await updateEngineeringTeam(projectId, { expected_version: state.saved.version, config: state.draft }, { signal: controller.signal });
      if (!controller.signal.aborted) dispatch({ type: 'saved', projectId, value });
    } catch (error) {
      if (!controller.signal.aborted && !isAbortError(error)) dispatch({ type: 'failed', projectId, error });
    }
  };
  const select = (view: 'roles' | 'skills', selectedRole = role) => {
    const next = new URLSearchParams(params);
    next.set('role', selectedRole);
    if (view === 'skills') next.set('view', 'skills'); else next.delete('view');
    setParams(next, { replace: true });
  };

  if (!state.saved || !state.draft) return state.error ? <ErrorState error={state.error} onRetry={() => reload(true)} /> : <SkeletonRows rows={5} cols={2} />;
  const saved = state.saved;
  const draft = state.draft;
  return <div className="et-settings" data-team-project={projectId} data-team-mode={editing ? 'edit' : 'view'}>
    <div className="et-toolbar">
      <div className="et-toolbar-copy"><strong>{t('teamSettings.scope', { project: project.name })}</strong><span className={`et-save-status${dirty ? ' dirty' : ''}`} role="status" aria-live="polite">{dirty ? <AlertTriangle /> : <Check />}{t(dirty ? 'teamSettings.dirty' : saved.version === 0 ? 'teamSettings.defaults' : 'teamSettings.saved', { version: saved.version })}</span></div>
      <div className="et-toolbar-actions">
        <button type="button" className="btn sm" disabled={pending} onClick={() => dirty ? setConfirm('reload') : reload()}><RefreshCw />{t('teamSettings.reload')}</button>
        {editing ? <>
          <button type="button" className="btn" disabled={pending} onClick={() => dirty ? setConfirm('discard') : dispatch({ type: 'discard', projectId })}>{t('common.cancel')}</button>
          <button type="button" className="btn primary" disabled={!editable || !dirty || conflict || tooLong} onClick={() => void save()}><Save />{t(state.busy ? 'common.saving' : 'teamSettings.save')}</button>
        </> : caps.manage && <button type="button" className="btn primary" disabled={pending} onClick={() => dispatch({ type: 'beginEdit', projectId })}><Pencil />{t('teamSettings.edit')}</button>}
      </div>
    </div>
    <p className="et-effect">{t('teamSettings.effect')}</p>
    {state.restored && dirty && <p className="et-draft-note" role="status">{t(editing ? 'teamSettings.draftHint' : 'teamSettings.restored')}</p>}
    {!caps.manage && <div className="et-readonly" role="note"><LockKeyhole />{t('teamSettings.readOnly')}</div>}
    {conflict ? <div className="callout warn" role="alert"><AlertTriangle /><p>{t('teamSettings.conflict')}</p></div> : <ErrorNotice error={state.error} />}
    {tooLong && <div className="callout err" role="alert"><AlertTriangle /><p>{t('teamSettings.totalExceeded')}</p></div>}
    <div className="et-view-tabs" role="group" aria-label={t('teamSettings.title')}>
      <button type="button" className="seg-btn" aria-pressed={!library} onClick={() => select('roles')}><Users />{t('teamSettings.roster')}<span>{t('teamSettings.rolesCount')}</span></button>
      <button type="button" className="seg-btn" aria-pressed={library} onClick={() => select('skills')}><BookOpen />{t('teamSettings.library')}</button>
      {editing && maxTotal !== undefined && <span className={`et-budget${tooLong ? ' exceeded' : ''}`}>{t('teamSettings.total', { count: total, max: maxTotal })}</span>}
    </div>
    {library ? <TeamSkillLibrary config={draft} builtinSkills={saved.builtin_skills} editing={editing} editable={editable} onChange={edit} /> : <div className="et-role-layout">
      <nav className="et-roster" aria-label={t('teamSettings.roster')}>
        {ENGINEERING_ROLES.map((item) => {
          const agent = draft.agents[item];
          return <button type="button" key={item} className={`et-roster-role${item === role ? ' selected' : ''}`} aria-current={item === role ? 'true' : undefined} onClick={() => select('roles', item)} data-team-role={item}><span className="et-roster-icon"><AgentRoleIcon role={item} /></span><span><strong>{agent.display_name.trim() || t(`team.role.${item}`)}</strong><small>{agent.display_name.trim() ? t(`team.role.${item}`) : 'AI'}</small></span></button>;
        })}
      </nav>
      <TeamRoleEditor role={role} config={draft} builtinSkills={saved.builtin_skills} editing={editing} editable={editable} onChange={edit} onReset={() => { if (editable) setConfirm('reset'); }} onLibrary={() => select('skills')} />
    </div>}
    {confirm && <ConfirmDialog title={t(confirm === 'reload' ? 'teamSettings.reloadTitle' : confirm === 'reset' ? 'teamSettings.resetTitle' : 'teamSettings.discardTitle')} body={t(confirm === 'reload' ? 'teamSettings.reloadBody' : confirm === 'reset' ? 'teamSettings.resetBody' : 'teamSettings.discardBody', { role: t(`team.role.${role}`) })} confirmLabel={t(confirm === 'reload' ? 'teamSettings.reload' : confirm === 'reset' ? 'teamSettings.reset' : 'teamSettings.discard')} onClose={() => setConfirm(null)} onConfirm={() => {
      if (confirm === 'reload') reload();
      else if (editable && confirm === 'reset') edit(resetTeamRole(draft, saved.defaults, role));
      else if (confirm === 'discard' && !pending) dispatch({ type: 'discard', projectId });
      setConfirm(null);
    }} />}
  </div>;
}
