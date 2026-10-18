import { useFeature } from '../api/capabilities';
import { Archive, ArchiveRestore, Boxes, LayoutGrid, List, MoreHorizontal, Plus } from 'lucide-react';
import { useMemo, useState, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { isApiError } from '../api/client';
import { archiveProject, createProject, listProjects } from '../api/endpoints';
import { createProjectWithStages } from '../api/stages';
import type { Project } from '../api/types';
import { paths } from '../app/nav';
import { roleCan } from '../auth/permissions';
import { useCurrentProject } from '../components/shell/CurrentProject';
import { useCrumbs, useHideProjectSwitcher } from '../components/shell/ShellContext';
import { ArchivedChip, RoleBadge } from '../components/ui/Badges';
import { ConfirmDialog, Dialog } from '../components/ui/Dialog';
import { Field, SearchInput } from '../components/ui/Form';
import { Menu, type MenuItem } from '../components/ui/Menu';
import { EmptyState, ErrorNotice, ErrorState, SkeletonCards, SkeletonRows } from '../components/ui/States';
import { useToast } from '../components/ui/Toast';
import { useDebounced } from '../hooks/keyboard';
import { useResource } from '../hooks/useResource';
import { useI18n } from '../i18n/I18nProvider';
import { sortProjects, type ProjectSort } from '../features/project/sortProjects';
import '../features/project/projects.css';
// [F stages] stage list chosen when a project is created.
import { DEFAULT_STAGE_VALUE, StageListField, stageBody, stageListError, type StageListValue } from '../features/stages/StageListField';

const VIEW_KEY = 'partroute.projects.view';
const CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

function readView(): 'cards' | 'table' {
  try {
    return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'cards';
  } catch {
    return 'cards';
  }
}

function CreateProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Project) => void }) {
  const { t, lang } = useI18n();
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const stagesEnabled = useFeature('stages');
  const [stages, setStages] = useState<StageListValue>(DEFAULT_STAGE_VALUE);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const codeErr = !code.trim()
    ? t('validation.required', { field: t('projects.field.code') })
    : code.trim().length < 2 || code.trim().length > 40 || !CODE_RE.test(code.trim())
      ? t('validation.projectCode')
      : isApiError(error) && error.code === 'project_code_taken'
        ? t('error.project_code_taken')
        : null;
  const nameErr = !name.trim() ? t('validation.required', { field: t('projects.field.name') }) : name.length > 200 ? t('validation.maxLength', { field: t('projects.field.name'), max: 200 }) : null;
  const descErr = description.length > 5000 ? t('validation.maxLength', { field: t('projects.field.description'), max: 5000 }) : null;
  const stagesErr = stagesEnabled ? stageListError(stages, t) : null;

  const submit = async () => {
    setTouched(true);
    if ((codeErr && !(isApiError(error) && error.code === 'project_code_taken')) || nameErr || descErr || stagesErr) return;
    setBusy(true);
    setError(null);
    try {
      const body = { code: code.trim(), name: name.trim(), description: description.trim() };
      const p = stagesEnabled ? await createProjectWithStages({ ...body, ...stageBody(stages), lang }) : await createProject(body);
      onCreated(p);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const generalError = error && !(isApiError(error) && error.code === 'project_code_taken') ? error : null;
  return (
    <Dialog
      title={t('projects.create.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.creating') : t('common.create')}
          </button>
        </>
      }
    >
      <div className="form-grid">
        <Field label={t('projects.field.code')} hint={t('projects.field.codeHint')} error={touched || (isApiError(error) && error.code === 'project_code_taken') ? codeErr : null}>
          <input
            className="input mono"
            value={code}
            maxLength={40}
            onChange={(e) => {
              setCode(e.target.value);
              if (isApiError(error) && error.code === 'project_code_taken') setError(null);
            }}
            autoCapitalize="characters"
            spellCheck={false}
          />
        </Field>
        <Field label={t('projects.field.name')} error={touched ? nameErr : null}>
          <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t('projects.field.description')} optional error={touched ? descErr : null} className="span-2">
          <textarea className="input" rows={3} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {stagesEnabled && <StageListField value={stages} onChange={setStages} error={touched ? stagesErr : null} />}
      </div>
      <ErrorNotice error={generalError} />
    </Dialog>
  );
}

export default function Projects() {
  const tasksEnabled = useFeature('tasks');
  const { t, tn, fmt, locale } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [sort, setSort] = useState<ProjectSort>('updated');
  const [view, setViewState] = useState<'cards' | 'table'>(readView);
  const [creating, setCreating] = useState(false);
  const [confirm, setConfirm] = useState<{ project: Project; restore: boolean } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<unknown>(null);
  const dq = useDebounced(q.trim(), 250);

  useCrumbs([{ label: t('pswitch.all') }]);
  useHideProjectSwitcher();
  const { reloadProjects, projectId } = useCurrentProject();

  const res = useResource((signal) => listProjects({ q: dq || undefined, include_archived: showArchived }, { signal }), [dq, showArchived]);
  const projects = useMemo(() => sortProjects(res.data ?? [], sort, locale), [res.data, sort, locale]);
  const clearFilters = () => { setQ(''); setShowArchived(false); };

  const setView = (v: 'cards' | 'table') => {
    setViewState(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* ignore */
    }
  };

  const open = (p: Project) => navigate(tasksEnabled ? paths.project(p.id) : paths.parts(p.id));

  const doArchive = async () => {
    if (!confirm) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const updated = await archiveProject(confirm.project.id, confirm.restore);
      toast(t(confirm.restore ? 'projects.restoredToast' : 'projects.archivedToast', { code: updated.code }));
      setConfirm(null);
      res.reload();
      reloadProjects();
    } catch (e) {
      setConfirmError(e);
    } finally {
      setConfirmBusy(false);
    }
  };

  const actionsFor = (p: Project): MenuItem[] => {
    const items: MenuItem[] = [{ id: 'open', label: t('common.open'), icon: <Boxes />, onSelect: () => open(p) }];
    if (roleCan(p.my_role, 'manage')) {
      items.push(
        p.status === 'archived'
          ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, separator: true, onSelect: () => setConfirm({ project: p, restore: true }) }
          : { id: 'archive', label: t('common.archive'), icon: <Archive />, separator: true, danger: true, onSelect: () => setConfirm({ project: p, restore: false }) },
      );
    }
    return items;
  };

  const onRowKey = (e: KeyboardEvent, p: Project) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(p);
    }
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = view === 'cards' ? <SkeletonCards /> : <SkeletonRows cols={5} />;
  else if (!projects.length && !dq && !showArchived)
    body = (
      <EmptyState
        title={t('projects.empty.title')}
        body={t('projects.empty.body')}
        action={
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            <Plus /> {t('projects.new')}
          </button>
        }
      />
    );
  else if (!projects.length) body = <EmptyState compact title={dq ? t('projects.noMatch', { q: dq }) : t('common.noMatches')} action={<button className="btn" onClick={clearFilters}>{t('common.clearFilters')}</button>} />;
  else if (view === 'cards')
    body = (
      <div className={`card-grid ${res.loading ? 'is-refreshing' : ''}`}>
        {projects.map((p) => (
          <article key={p.id} className={`pcard ${p.status === 'archived' ? 'archived' : ''} ${p.id === projectId ? 'is-current' : ''}`} tabIndex={0} onClick={() => open(p)} onKeyDown={(e) => onRowKey(e, p)} aria-label={`${p.code} ${p.name}`}>
            <div className="pcard-top">
              <span className="pcard-code mono">{p.code}</span>
              {p.id === projectId && <span className="project-current">{t('projects.current')}</span>}
              {p.status === 'archived' && <ArchivedChip />}
              <span className="grow" />
              <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actionsFor(p)} />
            </div>
            <h3 className="pcard-name">{p.name}</h3>
            <p className="pcard-desc">{p.description || <span className="faint">{t('project.noDescription')}</span>}</p>
            <div className="pcard-foot">
              <RoleBadge role={p.my_role} />
              <span className="pcard-stat mono">
                <Boxes aria-hidden /> {tn('projects.partCount', p.part_count ?? 0, { count: fmt.number(p.part_count ?? 0) })}
              </span>
              <span className="grow" />
              <time className="faint small" dateTime={p.updated_at} title={fmt.dateTime(p.updated_at)}>
                {fmt.relative(p.updated_at)}
              </time>
            </div>
          </article>
        ))}
      </div>
    );
  else
    body = (
      <div className={`table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t">
          <thead>
            <tr>
              <th>{t('projects.col.code')}</th>
              <th>{t('projects.col.name')}</th>
              <th>{t('projects.col.role')}</th>
              <th className="r">{t('projects.col.parts')}</th>
              <th className="hide-sm">{t('projects.col.updated')}</th>
              <th className="act">
                <span className="sr-only">{t('common.actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id} className={`clickable ${p.status === 'archived' ? 'is-archived' : ''}`} tabIndex={0} onClick={() => open(p)} onKeyDown={(e) => onRowKey(e, p)}>
                <td className="mono strong">{p.code}</td>
                <td>
                  <div className="cell-main">
                    <span>{p.name}</span>
                    {p.id === projectId && <span className="project-current">{t('projects.current')}</span>}
                    {p.status === 'archived' && <ArchivedChip />}
                  </div>
                </td>
                <td>
                  <RoleBadge role={p.my_role} />
                </td>
                <td className="r mono">{fmt.number(p.part_count ?? 0)}</td>
                <td className="hide-sm faint" title={fmt.dateTime(p.updated_at)}>
                  {fmt.relative(p.updated_at)}
                </td>
                <td className="act">
                  <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actionsFor(p)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="page projects-page">
      <header className="page-head">
        <div className="page-titles">
          <h1>{t('projects.title')}</h1>
          <p className="muted">
            {t('projects.subtitle')}
          </p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn primary" onClick={() => setCreating(true)}>
            <Plus /> {t('projects.new')}
          </button>
        </div>
      </header>

      <div className="toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t('projects.searchPlaceholder')} />
        <label className="check">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          <span>{t('common.showArchived')}</span>
        </label>
        <span className="grow" />
        <label className="project-sort"><span>{t('projects.sort')}</span><select className="select" aria-label={t('projects.sort')} value={sort} onChange={(event) => setSort(event.target.value as ProjectSort)}>
          <option value="updated">{t('projects.sort.updated')}</option>
          <option value="name">{t('projects.sort.name')}</option>
          <option value="code">{t('projects.sort.code')}</option>
        </select></label>
        <div className="seg" role="group" aria-label={t('projects.viewLabel')}>
          <button type="button" className="seg-btn" aria-pressed={view === 'cards'} onClick={() => setView('cards')} aria-label={t('projects.view.cards')} title={t('projects.view.cards')}>
            <LayoutGrid /> <span className="hide-sm">{t('projects.view.cards')}</span>
          </button>
          <button type="button" className="seg-btn" aria-pressed={view === 'table'} onClick={() => setView('table')} aria-label={t('projects.view.table')} title={t('projects.view.table')}>
            <List /> <span className="hide-sm">{t('projects.view.table')}</span>
          </button>
        </div>
      </div>

      <div className="project-results" aria-live="polite">
        <span>{res.data ? tn('projects.count', projects.length, { count: fmt.number(projects.length) }) : res.loading ? t('common.loading') : null}</span>
        {(q || showArchived) && <button className="linkish" onClick={clearFilters}>{t('common.clearFilters')}</button>}
      </div>
      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}

      {creating && (
        <CreateProjectDialog
          onClose={() => setCreating(false)}
          onCreated={(p) => {
            setCreating(false);
            toast(t('projects.createdToast', { code: p.code }));
            reloadProjects();
            navigate(tasksEnabled ? paths.project(p.id) : paths.parts(p.id));
          }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={t(confirm.restore ? 'projects.restore.title' : 'projects.archive.title', { code: confirm.project.code })}
          body={t(confirm.restore ? 'projects.restore.body' : 'projects.archive.body')}
          confirmLabel={t(confirm.restore ? 'common.restore' : 'common.archive')}
          danger={!confirm.restore}
          busy={confirmBusy}
          error={<ErrorNotice error={confirmError} />}
          onConfirm={() => void doArchive()}
          onClose={() => {
            setConfirm(null);
            setConfirmError(null);
          }}
        />
      )}
    </div>
  );
}
