// Conversation management (design §7, U21): new, switch, rename, archive,
// restore, "show archived". Archived conversations stay readable and can be
// restored even when every conversation is archived. Archiving never cancels
// a running operation. The full workspace shows a list (a select + buttons
// under 768 px); the dock and the embedded panel use a compact switcher.

import { Archive, ChevronDown, Loader2, MessageSquarePlus, Pencil, RotateCcw } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { listAiConversations, patchAiConversation, type AiConversationSummary } from '../../api/assistant';
import type { UUID } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { Popover } from '../../components/ui/Popover';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistantSession } from './AssistantProvider';
import { AsError, OpStatusIcon } from './common';

/** All of the user's conversations (archived included; filtered on screen). */
function useConversationList() {
  const { listRevision } = useAssistantSession();
  return useResource((signal) => listAiConversations({ archived: 'all', limit: 100 }, { signal }), [listRevision]);
}

/** Rename / archive / restore: the active conversation goes through the store (so the panel updates). */
function useSessionActions() {
  const session = useAssistantSession();
  const { store, bumpList } = session;
  return async (id: UUID, body: { title?: string; archived?: boolean }) => {
    if (store.detail?.conversation.id === id) await store.patch({ ...body, expected_version: store.detail.conversation.version });
    else await patchAiConversation(id, body);
    bumpList();
  };
}

export function RenameDialog({ id, title, onClose }: { id: UUID; title: string; onClose: () => void }) {
  const { t } = useI18n();
  const act = useSessionActions();
  const [value, setValue] = useState(title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    const v = value.trim();
    if (!v) return;
    setBusy(true);
    setError(null);
    try {
      await act(id, { title: v.slice(0, 200) });
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('as.session.renameTitle')}
      onClose={onClose}
      size="sm"
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !value.trim()}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label={t('as.session.name')}>
          <input className="input" value={value} maxLength={200} onChange={(e) => setValue(e.target.value)} onFocus={(e) => e.currentTarget.select()} />
        </Field>
        <AsError error={error} />
      </div>
    </Dialog>
  );
}

function SessionStatus({ c }: { c: AiConversationSummary }) {
  const { t } = useI18n();
  if (c.archived)
    return (
      <Chip tone="pending" icon={<Archive aria-hidden />} className="sm">
        {t('common.archived')}
      </Chip>
    );
  if (c.active_operations > 0)
    return (
      <Chip tone="measure" icon={<Loader2 className="spin-icon" aria-hidden />} className="sm">
        {t('as.session.active', { n: c.active_operations })}
      </Chip>
    );
  if (c.last_status === 'failed' || c.last_status === 'waiting_input')
    return (
      <Chip tone={c.last_status === 'failed' ? 'err' : 'warn'} icon={<OpStatusIcon status={c.last_status} />} className="sm">
        {t(`as.status.${c.last_status}`)}
      </Chip>
    );
  return null;
}

/** Full workspace: list with per-row actions; a select + buttons on phones. */
export function SessionList({ onSelect, onNew }: { onSelect: (id: UUID) => void; onNew: () => void }) {
  const { t, fmt } = useI18n();
  const { conversationId } = useAssistantSession();
  const list = useConversationList();
  const act = useSessionActions();
  const [showArchived, setShowArchived] = useState(false);
  const [renaming, setRenaming] = useState<AiConversationSummary | null>(null);
  const [busy, setBusy] = useState<UUID | null>(null);
  const [error, setError] = useState<unknown>(null);
  const selectId = useId();
  const all = list.data ?? [];
  const open = all.filter((c) => !c.archived);
  const archivedCount = all.length - open.length;
  const active = all.find((c) => c.id === conversationId) ?? null;
  // The active conversation is always listed (it may be archived).
  const shown = showArchived ? all : all.filter((c) => !c.archived || c.id === conversationId);

  const run = async (id: UUID, body: { archived?: boolean }) => {
    setBusy(id);
    setError(null);
    try {
      await act(id, body);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const rowActions = (c: AiConversationSummary, size = 'sm') =>
    c.archived ? (
      <button type="button" className={`btn ${size} ghost icon`} onClick={() => void run(c.id, { archived: false })} disabled={busy === c.id} aria-label={t('as.session.restoreNamed', { title: c.title })} title={t('as.session.restore')}>
        <RotateCcw />
      </button>
    ) : (
      <>
        <button type="button" className={`btn ${size} ghost icon`} onClick={() => setRenaming(c)} aria-label={t('as.session.renameNamed', { title: c.title })} title={t('common.rename')}>
          <Pencil />
        </button>
        <button type="button" className={`btn ${size} ghost icon`} onClick={() => void run(c.id, { archived: true })} disabled={busy === c.id} aria-label={t('as.session.archiveNamed', { title: c.title })} title={t('common.archive')}>
          <Archive />
        </button>
      </>
    );

  return (
    <nav className="as-sessions" aria-label={t('as.session.list')}>
      <div className="as-sessions-h">
        <h2>{t('as.session.list')}</h2>
        <span className="grow" />
        <button type="button" className="btn sm" onClick={onNew}>
          <MessageSquarePlus /> {t('as.session.new')}
        </button>
      </div>

      {/* phones: select + actions */}
      <div className="as-sessions-mobile">
        <label htmlFor={selectId} className="sr-only">
          {t('as.session.switch')}
        </label>
        <select id={selectId} className="select" value={conversationId ?? ''} onChange={(e) => (e.target.value ? onSelect(e.target.value) : onNew())}>
          <option value="">{t('as.session.newItem')}</option>
          {all.map((c) => (
            <option key={c.id} value={c.id}>
              {c.archived ? `${t('common.archived')} · ` : ''}
              {c.title || t('as.session.untitled')} · {c.project.code}
            </option>
          ))}
        </select>
        {active && <span className="as-sessions-mobile-acts">{rowActions(active, '')}</span>}
      </div>

      <label className="check as-sessions-toggle">
        <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
        {t('common.showArchived')}
        {archivedCount > 0 && <span className="faint">({archivedCount})</span>}
      </label>

      {list.error && !list.data ? (
        <ErrorNotice error={list.error} onRetry={list.reload} />
      ) : !list.data ? (
        <div className="stack-sm">
          <Skeleton h={36} />
          <Skeleton h={36} />
        </div>
      ) : (
        <ul className="as-session-items">
          {shown.map((c) => (
            <li key={c.id} className={`as-session ${c.id === conversationId ? 'active' : ''} ${c.archived ? 'archived' : ''}`}>
              <button type="button" className="as-session-main" onClick={() => onSelect(c.id)} aria-current={c.id === conversationId ? 'true' : undefined}>
                <span className="as-session-title">{c.title || t('as.session.untitled')}</span>
                <span className="as-session-sub">
                  <span className="mono">{c.project.code}</span>
                  {c.task ? ` · ${c.task.title}` : ''} · {fmt.relative(c.last_activity_at)}
                </span>
                <SessionStatus c={c} />
              </button>
              <span className="as-session-acts">{rowActions(c)}</span>
            </li>
          ))}
          {!open.length && archivedCount > 0 && !showArchived && (
            <li className="as-session-empty">
              <span>{t('as.session.allArchived')}</span>
              <button type="button" className="linkish small" onClick={() => setShowArchived(true)}>
                {t('as.session.showArchived')}
              </button>
            </li>
          )}
          {!all.length && <li className="as-session-empty faint">{t('as.session.none')}</li>}
        </ul>
      )}
      <AsError error={error} />
      {renaming && <RenameDialog id={renaming.id} title={renaming.title} onClose={() => setRenaming(null)} />}
    </nav>
  );
}

/** Dock / embedded: title button that opens the conversation list. */
export function SessionSwitcher({ title, onSelect, onNew }: { title: string; onSelect: (id: UUID) => void; onNew: () => void }) {
  const { t } = useI18n();
  return (
    <Popover
      label={t('as.session.switch')}
      align="start"
      triggerClassName="as-switch-btn"
      trigger={
        <>
          <span className="as-switch-title">{title}</span>
          <ChevronDown aria-hidden />
        </>
      }
    >
      {(close) => (
        <SwitcherBody
          onSelect={(id) => {
            close();
            onSelect(id);
          }}
          onNew={() => {
            close();
            onNew();
          }}
        />
      )}
    </Popover>
  );
}

function SwitcherBody({ onSelect, onNew }: { onSelect: (id: UUID) => void; onNew: () => void }) {
  const { t, fmt } = useI18n();
  const { conversationId } = useAssistantSession();
  const list = useConversationList();
  const [showArchived, setShowArchived] = useState(false);
  const all = list.data ?? [];
  const shown = (showArchived ? all : all.filter((c) => !c.archived)).slice(0, 30);
  const firstRef = useRef<HTMLButtonElement>(null);
  useEffect(() => firstRef.current?.focus(), []);
  return (
    <div className="as-switcher">
      <button type="button" className="as-switch-item new" onClick={onNew} ref={firstRef}>
        <MessageSquarePlus aria-hidden /> {t('as.session.new')}
      </button>
      {list.error && !list.data ? <ErrorNotice error={list.error} onRetry={list.reload} /> : null}
      {!list.data && !list.error && <Skeleton h={28} />}
      <ul className="as-switch-list" aria-label={t('as.session.list')}>
        {shown.map((c) => (
          <li key={c.id}>
            <button type="button" className={`as-switch-item ${c.id === conversationId ? 'active' : ''}`} aria-current={c.id === conversationId ? 'true' : undefined} onClick={() => onSelect(c.id)}>
              <span className="as-switch-item-t">{c.title || t('as.session.untitled')}</span>
              <span className="as-switch-item-s">
                <span className="mono">{c.project.code}</span> · {fmt.relative(c.last_activity_at)}
                {c.archived ? ` · ${t('common.archived')}` : ''}
                {c.active_operations > 0 ? ` · ${t('as.session.active', { n: c.active_operations })}` : ''}
              </span>
            </button>
          </li>
        ))}
        {list.data && !shown.length && <li className="as-session-empty faint">{all.length ? t('as.session.allArchived') : t('as.session.none')}</li>}
      </ul>
      <label className="check">
        <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
        {t('common.showArchived')}
      </label>
    </div>
  );
}
