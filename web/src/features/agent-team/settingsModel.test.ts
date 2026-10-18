import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import type { TeamConfig, TeamSettingsOut } from '../../api/engineeringTeam';
import { ENGINEERING_ROLES } from './roles';
import { createTeamDraftStore, initialTeamEditor, removeTeamSkill, resetTeamRole, teamConfigCharacters, teamDirty, teamEditorReducer, teamRoleFromQuery, toggleTeamSkill } from './settingsModel';

const config = (): TeamConfig => ({
  agents: Object.fromEntries(ENGINEERING_ROLES.map((role) => [role, { display_name: '', profile: '', tone: '', personality: '', skill_ids: ['builtin-evidence'] }])) as TeamConfig['agents'],
  skills: [{ id: 'custom-method', name: 'Method', description: 'Guide', instructions: 'Keep evidence.' }],
});
const saved = (project_id = 'p1', version = 3): TeamSettingsOut => ({ project_id, version, updated_at: null, config: config(), defaults: config(), builtin_skills: [] });
const loadedEditor = (canManage = true) => teamEditorReducer(initialTeamEditor('p1', canManage), { type: 'loaded', projectId: 'p1', value: saved() });
const editingEditor = () => teamEditorReducer(loadedEditor(), { type: 'beginEdit', projectId: 'p1' });

describe('engineering team draft safety', () => {
  it('removes a custom skill from every role without mutating the saved configuration', () => {
    const original = config();
    original.agents.manager.skill_ids.push('custom-method');
    original.agents.quote.skill_ids.push('custom-method');
    const next = removeTeamSkill(original, 'custom-method');
    expect(next.skills).toEqual([]);
    expect(next.agents.manager.skill_ids).toEqual(['builtin-evidence']);
    expect(next.agents.quote.skill_ids).toEqual(['builtin-evidence']);
    expect(original.skills).toHaveLength(1);
    expect(original.agents.quote.skill_ids).toContain('custom-method');
    expect(removeTeamSkill(original, 'builtin-evidence')).toBe(original);
  });

  it('restores only the chosen role and leaves other edits and the custom library intact', () => {
    const draft = config();
    draft.agents.modeling.profile = 'Use measured dimensions';
    draft.agents.quote.tone = 'Be concise';
    const next = resetTeamRole(draft, config(), 'modeling');
    expect(next.agents.modeling.profile).toBe('');
    expect(next.agents.quote.tone).toBe('Be concise');
    expect(next.skills).toEqual(draft.skills);
    expect(draft.agents.modeling.profile).toBe('Use measured dimensions');
    expect(teamDirty(config(), next)).toBe(true);
  });

  it('toggles a skill once and enforces the role assignment count without losing existing selections', () => {
    const start = config();
    const added = toggleTeamSkill(start, 'assembly', 'custom-method');
    expect(added.agents.assembly.skill_ids).toEqual(['builtin-evidence', 'custom-method']);
    expect(toggleTeamSkill(added, 'assembly', 'custom-method').agents.assembly.skill_ids).toEqual(['builtin-evidence']);
    const full = config();
    full.agents.assembly.skill_ids = Array.from({ length: 12 }, (_, i) => `custom-${i}`);
    expect(toggleTeamSkill(full, 'assembly', 'custom-extra')).toBe(full);
    expect(toggleTeamSkill(full, 'assembly', 'custom-1').agents.assembly.skill_ids).toHaveLength(11);
  });

  it('keeps the baseline version and typed edits after a conflict, network error or invalid save', () => {
    let state = editingEditor();
    const draft = config();
    draft.agents.manager.profile = 'My unsaved instructions';
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: draft });
    for (const error of [new ApiError(409, 'version_conflict', 'changed'), new ApiError(0, 'network', 'offline'), new ApiError(422, 'validation_error', 'too long')]) {
      state = teamEditorReducer(state, { type: 'saving', projectId: 'p1' });
      state = teamEditorReducer(state, { type: 'failed', projectId: 'p1', error });
      expect(state.draft?.agents.manager.profile).toBe('My unsaved instructions');
      expect(state.saved?.version).toBe(3);
      expect(state.error).toBe(error);
      expect(teamDirty(state.saved?.config, state.draft)).toBe(true);
      expect(state.busy).toBe(false);
      expect(state.editing).toBe(true);
    }
  });

  it('ignores stale load/save results from another project and never migrates its draft', () => {
    const first = teamEditorReducer(initialTeamEditor('p1'), { type: 'loaded', projectId: 'p1', value: saved() });
    expect(teamEditorReducer(first, { type: 'loaded', projectId: 'p1', value: saved('p2') })).toBe(first);
    const second = initialTeamEditor('p2');
    expect(teamEditorReducer(second, { type: 'loaded', projectId: 'p1', value: saved() })).toBe(second);
    expect(teamEditorReducer(second, { type: 'saved', projectId: 'p1', value: saved() })).toBe(second);
    expect(second.draft).toBeNull();
  });

  it('changes the baseline only after a successful save or explicit reload/discard', () => {
    let state = editingEditor();
    const draft = config();
    draft.agents.manager.tone = 'Friendly';
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: draft });
    state = teamEditorReducer(state, { type: 'loading', projectId: 'p1' });
    expect(state.draft?.agents.manager.tone).toBe('Friendly');
    state = teamEditorReducer(state, { type: 'failed', projectId: 'p1', error: new Error('offline') });
    expect(state.draft?.agents.manager.tone).toBe('Friendly');
    state = teamEditorReducer(state, { type: 'discard', projectId: 'p1' });
    expect(teamDirty(state.saved?.config, state.draft)).toBe(false);
    expect(state.editing).toBe(false);
    state = teamEditorReducer(state, { type: 'beginEdit', projectId: 'p1' });
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: draft });
    state = teamEditorReducer(state, { type: 'saving', projectId: 'p1' });
    state = teamEditorReducer(state, { type: 'saved', projectId: 'p1', value: { ...saved('p1', 4), config: draft } });
    expect(state.saved?.version).toBe(4);
    expect(state.draft?.agents.manager.tone).toBe('Friendly');
    expect(teamDirty(state.saved?.config, state.draft)).toBe(false);
    expect(state.editing).toBe(false);
  });

  it('counts Unicode characters and keeps role URLs within the canonical catalog', () => {
    const draft = config();
    draft.skills = [];
    draft.agents.manager.profile = '中文😀';
    expect(teamConfigCharacters(draft)).toBe(3);
    expect(teamRoleFromQuery('modeling')).toBe('modeling');
    for (const value of ['constructor', '__proto__', 'custom-engineer', '', null]) expect(teamRoleFromQuery(value)).toBe('manager');
  });

  it('keeps navigation drafts isolated by user and project, clearing only saved or discarded drafts', () => {
    const store = createTeamDraftStore();
    let state = editingEditor();
    const draft = config();
    draft.agents.manager.profile = 'Keep while I visit another project';
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: draft });
    store.remember('u1', state);
    expect(store.read('u2', 'p1')).toBeUndefined();
    expect(store.read('u1', 'p2')).toBeUndefined();
    store.remember('u1', initialTeamEditor('p1'));
    expect(store.read('u1', 'p1')?.draft.agents.manager.profile).toBe('Keep while I visit another project');
    store.remember('u1', teamEditorReducer(state, { type: 'discard', projectId: 'p1' }));
    expect(store.read('u1', 'p1')).toBeUndefined();
  });

  it('restores a navigation draft only after an authorized fresh load and preserves the stale version on conflict', () => {
    const baseline = saved('p1', 3);
    const draft = config();
    draft.agents.manager.profile = 'My local instructions';
    const preserved = { saved: baseline, draft };
    const newer = saved('p1', 4);
    newer.config.agents.manager.profile = 'Another editor saved';
    const restored = teamEditorReducer(initialTeamEditor('p1', true), { type: 'loaded', projectId: 'p1', value: newer, preserved });
    expect(restored.draft?.agents.manager.profile).toBe('My local instructions');
    expect(restored.saved?.version).toBe(3);
    expect(restored.error).toMatchObject({ code: 'version_conflict' });
    expect(restored.restored).toBe(true);
    expect(restored.editing).toBe(false);
    expect(teamEditorReducer(restored, { type: 'edit', projectId: 'p1', config: config() })).toBe(restored);
    const resumed = teamEditorReducer(restored, { type: 'beginEdit', projectId: 'p1' });
    expect(resumed.editing).toBe(true);
    expect(resumed.draft).toBe(draft);
    expect(teamEditorReducer(resumed, { type: 'saving', projectId: 'p1' })).toBe(resumed);
    const otherProject = teamEditorReducer(initialTeamEditor('p2'), { type: 'loaded', projectId: 'p2', value: saved('p2'), preserved });
    expect(otherProject.draft?.agents.manager.profile).toBe('');
    expect(otherProject.restored).toBe(false);
    const reloaded = teamEditorReducer(restored, { type: 'loaded', projectId: 'p1', value: newer });
    expect(reloaded.saved?.version).toBe(4);
    expect(reloaded.draft?.agents.manager.profile).toBe('Another editor saved');
    expect(reloaded.error).toBeNull();
    expect(reloaded.editing).toBe(false);
    const revoked = teamEditorReducer(initialTeamEditor('p1'), { type: 'failed', projectId: 'p1', error: new ApiError(404, 'not_found', 'No project access') });
    expect(revoked.saved).toBeNull();
    expect(revoked.draft).toBeNull();
  });

  it('requires explicit edit mode before changing profiles, skills, or starting a save', () => {
    const view = loadedEditor();
    expect(view.editing).toBe(false);
    for (const draft of [resetTeamRole(view.draft!, config(), 'manager'), removeTeamSkill(view.draft!, 'custom-method'), toggleTeamSkill(view.draft!, 'manager', 'custom-method')]) {
      expect(teamEditorReducer(view, { type: 'edit', projectId: 'p1', config: draft })).toBe(view);
    }
    expect(teamEditorReducer(view, { type: 'saving', projectId: 'p1' })).toBe(view);
    const editing = teamEditorReducer(view, { type: 'beginEdit', projectId: 'p1' });
    expect(editing.editing).toBe(true);
    expect(editing.saved).toBe(view.saved);
    expect(teamEditorReducer(editing, { type: 'saving', projectId: 'p1' })).toBe(editing);
    expect(teamEditorReducer(editing, { type: 'discard', projectId: 'p1' }).editing).toBe(false);
  });

  it('refuses changes for non-managers and re-locks a draft immediately when management permission is removed', () => {
    const member = loadedEditor(false);
    expect(teamEditorReducer(member, { type: 'beginEdit', projectId: 'p1' })).toBe(member);
    expect(teamEditorReducer(member, { type: 'edit', projectId: 'p1', config: config() })).toBe(member);
    expect(teamEditorReducer(member, { type: 'saving', projectId: 'p1' })).toBe(member);
    const draft = config();
    draft.agents.modeling.profile = 'Keep this private draft';
    const editing = teamEditorReducer(editingEditor(), { type: 'edit', projectId: 'p1', config: draft });
    const revoked = teamEditorReducer(editing, { type: 'permission', projectId: 'p1', canManage: false });
    expect(revoked.editing).toBe(false);
    expect(revoked.draft).toBe(draft);
    expect(teamEditorReducer(revoked, { type: 'edit', projectId: 'p1', config: config() })).toBe(revoked);
    expect(teamEditorReducer(revoked, { type: 'beginEdit', projectId: 'p1' })).toBe(revoked);
    const restored = teamEditorReducer(revoked, { type: 'permission', projectId: 'p1', canManage: true });
    expect(restored.editing).toBe(false);
    expect(teamEditorReducer(restored, { type: 'beginEdit', projectId: 'p1' }).draft).toBe(draft);
  });

  it('keeps the whole edit session across employee and library changes and refuses changes during persistence', () => {
    let state = editingEditor();
    const first = config();
    first.agents.modeling.profile = 'First employee draft';
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: first });
    const next = { ...state.draft!, agents: { ...state.draft!.agents, drawing: { ...state.draft!.agents.drawing, tone: 'Second employee draft' } }, skills: [] };
    state = teamEditorReducer(state, { type: 'edit', projectId: 'p1', config: next });
    expect(state.editing).toBe(true);
    expect(state.draft?.agents.modeling.profile).toBe('First employee draft');
    expect(state.draft?.agents.drawing.tone).toBe('Second employee draft');
    expect(state.saved?.config.skills).toHaveLength(1);
    const saving = teamEditorReducer(state, { type: 'saving', projectId: 'p1' });
    expect(saving.busy).toBe(true);
    expect(teamEditorReducer(saving, { type: 'edit', projectId: 'p1', config: config() })).toBe(saving);
    expect(teamEditorReducer(saving, { type: 'discard', projectId: 'p1' })).toBe(saving);
    expect(teamEditorReducer(saving, { type: 'saving', projectId: 'p1' })).toBe(saving);
    const canceled = teamEditorReducer(state, { type: 'discard', projectId: 'p1' });
    expect(canceled.draft).toBe(state.saved?.config);
    expect(canceled.editing).toBe(false);
  });

  it('resets from server defaults only in the draft and preserves explicitly saved empty values until reset', () => {
    const baseline = saved();
    baseline.defaults.agents.modeling = { display_name: '', profile: 'Server modeling profile', tone: 'Server tone', personality: 'Server personality', skill_ids: ['builtin-evidence'] };
    const view = teamEditorReducer(initialTeamEditor('p1', true), { type: 'loaded', projectId: 'p1', value: baseline });
    expect(view.draft?.agents.modeling.profile).toBe('');
    const editing = teamEditorReducer(view, { type: 'beginEdit', projectId: 'p1' });
    const reset = teamEditorReducer(editing, { type: 'edit', projectId: 'p1', config: resetTeamRole(editing.draft!, baseline.defaults, 'modeling') });
    expect(reset.draft?.agents.modeling.profile).toBe('Server modeling profile');
    expect(reset.saved?.config.agents.modeling.profile).toBe('');
    expect(teamDirty(reset.saved?.config, reset.draft)).toBe(true);
    expect(teamEditorReducer(reset, { type: 'discard', projectId: 'p1' }).draft?.agents.modeling.profile).toBe('');
  });
});
