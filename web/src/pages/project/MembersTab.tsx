import { useFeature } from '../../api/capabilities';
// Project → Members (prototype #project.members): one table — member, role,
// template-manager grant, open duties (owns / accepts / reviews), status —
// with "Add member" in a dialog, then the role permission matrix.
// Only the owner manages members; the server checks every
// change (last owner, viewer capabilities…), the UI only makes it convenient.

import './members.css';
import { ExternalLink, ShieldCheck, Trash2, UserPlus } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { listApprovalRequests } from '../../api/approvals';
import { listMembers, removeMember, upsertMember } from '../../api/endpoints';
import { listTasks } from '../../api/pm';
import { ROLES, type Member, type MemberCapability, type Role } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { Chip, RoleBadge } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, ErrorState, ErrorText, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { isLastOwner, MATRIX_ACTIONS, memberDuties, permissionCell, type Duties } from './membersModel';
import type { ProjectTabProps } from './tabProps';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MEMBER_ERRORS = { not_found: 'members.userNotFound', read_only_member: 'tpl.err.read_only_member' } as const;

/** Template manager: create, edit, publish and deprecate workflow templates (owners always can). */
function hasTemplateManager(m: Member): boolean {
  return m.role === 'owner' || !!m.capabilities?.includes('template_manager');
}

function AddMemberDialog({ projectId, onClose, onAdded }: { projectId: string; onClose: () => void; onAdded: (m: Member) => void }) {
  const { t } = useI18n();
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('designer');
  const [templateManager, setTemplateManager] = useState(false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const emailErr = !email.trim() ? t('validation.required', { field: t('members.add.email') }) : !EMAIL_RE.test(email.trim()) ? t('validation.email') : null;
  const capOk = role !== 'viewer' && role !== 'owner';
  const submit = async () => {
    setTouched(true);
    if (emailErr) return;
    setBusy(true);
    setError(null);
    try {
      const caps: MemberCapability[] = capOk && templateManager ? ['template_manager'] : [];
      onAdded(await upsertMember(projectId, { email: email.trim().toLowerCase(), role, capabilities: caps }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('members.add')}
      subtitle={t('pa.mem.addBody')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            <UserPlus /> {busy ? t('common.working') : t('members.add')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('members.add.email')} error={touched ? emailErr : null} hint={t('members.add.hint')}>
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              setError(null);
            }}
            placeholder="name@company.com"
            autoComplete="off"
            data-autofocus
          />
        </Field>
        <Field label={t('members.add.role')}>
          <select className="select" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {t(`role.${r}`)}
              </option>
            ))}
          </select>
        </Field>
        <label className={`mem-pchip ${capOk && templateManager ? 'on' : ''} ${capOk ? '' : 'dis'}`}>
          <input type="checkbox" checked={capOk && templateManager} disabled={!capOk} onChange={(e) => setTemplateManager(e.target.checked)} />
          {t('pa.mem.grantTemplate')}
        </label>
      </div>
      <ErrorNotice error={error} overrides={MEMBER_ERRORS} />
    </Dialog>
  );
}

function PermissionMatrix() {
  const { t } = useI18n();
  return (
    <section className="panel mem-card" aria-label={t('members.roles.title')}>
      <div className="panel-h">
        <h3>{t('members.roles.title')}</h3>
        <span className="grow" />
        <span className="faint small">{t('pa.mem.matrixSource')}</span>
      </div>
      <div className="mem-perm-wrap">
        <table className="mem-perm">
          <thead>
            <tr>
              <th>{t('pa.mem.action')}</th>
              {ROLES.map((r) => (
                <th key={r}>{t(`role.${r}`)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {MATRIX_ACTIONS.map((a) => (
              <tr key={a}>
                <td>{t(`pa.mem.act.${a}`)}</td>
                {ROLES.map((r) => {
                  const c = permissionCell(r, a);
                  return (
                    <td key={r}>
                      {c === 'yes' ? (
                        <span className="yes" aria-label={t('pa.mem.yes')}>
                          ✓
                        </span>
                      ) : c === 'grantable' ? (
                        <span className="maybe" title={t('tpl.members.capHelp')}>
                          {t('pa.mem.grantable')}
                        </span>
                      ) : (
                        <span className="no" aria-label={t('pa.mem.no')}>
                          —
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="faint small mem-note">{t('pa.mem.matrixNote')}</p>
    </section>
  );
}

function DutyList({ duties }: { duties: Duties }) {
  const { t } = useI18n();
  if (!duties.items.length) return null;
  return (
    <div className="stack-sm">
      <p className="small">{t('pa.mem.dutiesFirst', { n: duties.items.length })}</p>
      <ul className="mem-duties">
        {duties.items.map((d) => (
          <li key={`${d.kind}-${d.id}`}>
            <span className="ellip">{d.label}</span>
            <span className="faint small">{t(`pa.mem.duty.${d.kind}`)}</span>
            <Link className="small" to={d.kind === 'review' ? paths.approvalRequest(d.id) : paths.task(d.id)}>
              <ExternalLink className="inline-ico" aria-hidden /> {t('common.open')}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MembersTab({ project, caps, reloadProject }: ProjectTabProps) {
  const tasksEnabled = useFeature('tasks');
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const me = useUser();
  const navigate = useNavigate();
  const res = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const tasks = useResource((signal) => listTasks(project.id, {}, { signal }), [project.id, tasksEnabled], { enabled: tasksEnabled });
  const requests = useResource((signal) => listApprovalRequests({ scope: 'all', project_id: project.id, status: 'pending', limit: 200 }, { signal }), [project.id]);
  const members = res.data ?? [];
  const [adding, setAdding] = useState(false);
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Member | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeError, setRemoveError] = useState<unknown>(null);

  const canManage = caps.manage;
  const dutiesOf = useMemo(() => {
    const ready = (!tasksEnabled || !!tasks.data) && !!requests.data;
    return (id: string): Duties | null => (ready ? memberDuties(id, tasksEnabled ? tasks.data! : [], requests.data!) : null);
  }, [tasks.data, requests.data, tasksEnabled]);

  const changeRole = async (m: Member, next: Role) => {
    if (next === m.role) return;
    setBusyRow(m.user.id);
    try {
      const updated = await upsertMember(project.id, { email: m.user.email, role: next });
      res.setData((prev) => (prev ?? []).map((x) => (x.user.id === updated.user.id ? updated : x)));
      toast(t('members.roleChangedToast', { name: updated.user.name, role: t(`role.${updated.role}`) }));
      if (m.user.id === me.id) reloadProject();
    } catch (e) {
      toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusyRow(null);
    }
  };

  const toggleTemplateManager = async (m: Member, on: boolean) => {
    const rest = (m.capabilities ?? []).filter((c) => c !== 'template_manager');
    const next: MemberCapability[] = on ? [...rest, 'template_manager'] : rest;
    setBusyRow(m.user.id);
    try {
      const updated = await upsertMember(project.id, { email: m.user.email, role: m.role, capabilities: next });
      res.setData((prev) => (prev ?? []).map((x) => (x.user.id === updated.user.id ? { ...updated, capabilities: updated.capabilities ?? next } : x)));
      toast(t(on ? 'tpl.members.grantedToast' : 'tpl.members.revokedToast', { name: updated.user.name }));
      if (m.user.id === me.id) reloadProject();
    } catch (e) {
      toast(<ErrorText error={e} overrides={MEMBER_ERRORS} />, { tone: 'err' });
    } finally {
      setBusyRow(null);
    }
  };

  const doRemove = async () => {
    if (!removing) return;
    setRemoveBusy(true);
    setRemoveError(null);
    try {
      await removeMember(project.id, removing.user.id);
      toast(t('members.removedToast', { name: removing.user.name }));
      if (removing.user.id === me.id) {
        navigate(paths.projects(), { replace: true });
        return;
      }
      res.setData((prev) => (prev ?? []).filter((x) => x.user.id !== removing.user.id));
      setRemoving(null);
    } catch (e) {
      setRemoveError(e);
    } finally {
      setRemoveBusy(false);
    }
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={5} rows={4} />;
  else if (!members.length) body = <EmptyState compact title={t('common.noMatches')} />;
  else
    body = (
      <div className="table-wrap mem-table-wrap">
        <table className="t mem-table">
          <thead>
            <tr>
              <th>{t('pa.mem.col.member')}</th>
              <th>{t('members.col.role')}</th>
              <th className="hide-sm">
                <span title={t('tpl.members.capHint')}>{t('pa.mem.col.caps')}</span>
              </th>
              <th className="hide-sm" title={t('pa.mem.col.dutiesHint')}>
                {t('pa.mem.col.duties')}
              </th>
              <th className="hide-md">{t('pa.mem.col.status')}</th>
              {canManage && (
                <th className="act">
                  <span className="sr-only">{t('common.actions')}</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const lastOwner = isLastOwner(m, members);
              const d = dutiesOf(m.user.id);
              return (
                <tr key={m.user.id}>
                  <td>
                    <div className="mem">
                      <span className="mem-av" aria-hidden>
                        {(m.user.name || m.user.email).slice(0, 1).toUpperCase()}
                      </span>
                      <div>
                        <b>
                          {m.user.name}
                          {m.user.id === me.id && <span className="faint"> ({t('common.you')})</span>}
                          {m.user.is_admin && <ShieldCheck className="inline-ico measure-ico" aria-label={t('user.admin')} />}
                        </b>
                        <small className="mono">{m.user.email}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    {canManage ? (
                      <select
                        className="select sm"
                        value={m.role}
                        onChange={(e) => void changeRole(m, e.target.value as Role)}
                        disabled={busyRow === m.user.id}
                        aria-label={t('members.roleFor', { name: m.user.name })}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r} disabled={lastOwner && r !== 'owner'}>
                            {t(`role.${r}`)}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <RoleBadge role={m.role} />
                    )}
                    {lastOwner && <small className="faint mem-sub">{t('pa.mem.onlyOwner')}</small>}
                  </td>
                  <td className="hide-sm">
                    <label
                      className={`mem-pchip ${m.role !== 'owner' && hasTemplateManager(m) ? 'on' : ''} ${!canManage || m.role === 'owner' || m.role === 'viewer' ? 'dis' : ''}`}
                      title={m.role === 'owner' ? t('tpl.members.ownerAlways') : m.role === 'viewer' ? t('tpl.members.viewerNever') : t('tpl.members.capHint')}
                    >
                      <input
                        type="checkbox"
                        checked={hasTemplateManager(m)}
                        disabled={!canManage || m.role === 'owner' || m.role === 'viewer' || busyRow === m.user.id}
                        onChange={(e) => void toggleTemplateManager(m, e.target.checked)}
                        aria-label={t('tpl.members.toggleFor', { name: m.user.name })}
                      />
                      {t('tpl.members.short')}
                    </label>
                  </td>
                  <td className="hide-sm mono">{d ? `${fmt.number(d.owner)} / ${fmt.number(d.reviewer)} / ${fmt.number(d.review)}` : <span className="faint">…</span>}</td>
                  <td className="hide-md">
                    <Chip tone="ok">{t('pa.mem.active')}</Chip>
                  </td>
                  {canManage && (
                    <td className="act">
                      <button
                        type="button"
                        className="btn ghost sm danger"
                        onClick={() => setRemoving(m)}
                        disabled={lastOwner}
                        title={lastOwner ? t('pa.mem.keepOneOwner') : t('common.remove')}
                        aria-label={`${t('common.remove')} ${m.user.name}`}
                      >
                        <Trash2 /> <span className="hide-sm">{t('common.remove')}</span>
                      </button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );

  const removingDuties = removing ? dutiesOf(removing.user.id) : null;

  return (
    <div className="stack">
      <div className="toolbar">
        <span className="faint small">
          {canManage ? t('pa.mem.ownerHint') : t('members.readOnly')} {t('pa.mem.serverChecks')}
        </span>
        <span className="grow" />
        {res.data && <span className="faint mono small">{tn('members.count', members.length, { count: fmt.number(members.length) })}</span>}
        {canManage && (
          <button type="button" className="btn primary" onClick={() => setAdding(true)}>
            <UserPlus /> {t('members.add')}
          </button>
        )}
      </div>
      {body}
      <PermissionMatrix />
      {adding && (
        <AddMemberDialog
          projectId={project.id}
          onClose={() => setAdding(false)}
          onAdded={(m) => {
            setAdding(false);
            toast(t('members.addedToast', { name: m.user.name, role: t(`role.${m.role}`) }));
            res.setData((prev) => {
              const rest = (prev ?? []).filter((x) => x.user.id !== m.user.id);
              return [...rest, m].sort((a, b) => a.user.name.localeCompare(b.user.name));
            });
          }}
        />
      )}
      {removing && (
        <Dialog
          title={t('members.remove.title', { name: removing.user.name })}
          size="sm"
          role="alertdialog"
          busy={removeBusy}
          onClose={() => {
            setRemoving(null);
            setRemoveError(null);
          }}
          footer={
            <>
              <button
                type="button"
                className="btn"
                disabled={removeBusy}
                onClick={() => {
                  setRemoving(null);
                  setRemoveError(null);
                }}
              >
                {t('common.cancel')}
              </button>
              <button type="button" className="btn danger-solid" onClick={() => void doRemove()} disabled={removeBusy} data-autofocus>
                {removeBusy ? t('common.working') : t('common.remove')}
              </button>
            </>
          }
        >
          <div className="stack-sm">
            <p className="muted">{removing.user.id === me.id ? t('members.remove.self') : t('members.remove.body')}</p>
            {removingDuties && <DutyList duties={removingDuties} />}
            {removeError ? <ErrorNotice error={removeError} /> : null}
          </div>
        </Dialog>
      )}
    </div>
  );
}
