// The assistant panel: one component for the right dock, the part model page's
// embedded tab and the full workspace (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §7.3).
// All three show the provider's conversation; switching layouts only changes
// the frame (no new conversation, no copied messages).
//
//   dock / embedded:  header (switcher, menu, expand[, close]) · messages · composer
//   full:             conversation list | header · messages · composer | context inspector or work panel
//
// The full layout provides the AI work panel (.trellis/tasks/archive/2026-10/10-12-ai-work-panel): cards open their object in the
// right column, which the panel and the context inspector share (opening one closes the other). AI-native
// (.trellis/tasks/archive/2026-10/10-15-ai-native-orchestrator): the header holds the main agent's autonomy, and the right column shows
// the conversation's sub-agents and drafts above the context inspector.

import { Archive,ArrowLeft,Info,Maximize2,MessageSquarePlus,MoreHorizontal,PanelRight,PanelRightClose,Pencil,RotateCcw,ScanSearch,X } from 'lucide-react';
import { lazy,Suspense,useEffect,useRef,useState } from 'react';
import { Link,useNavigate,useSearchParams } from 'react-router-dom';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { Menu,type MenuItem } from '../../components/ui/Menu';
import { ErrorState,Skeleton } from '../../components/ui/States';
import { useI18n } from '../../i18n/I18nProvider';
import { AgentRoleIcon } from '../agent-team/AgentRole';
import { EngineeringTeam } from '../agent-team/EngineeringTeam';
import { nodeDisplayName } from '../workflow/labels';
import './assistant.css';
import { useAssistant,useAssistantSession,type AssistantNotice } from './AssistantProvider';
import { AsError,refKindLabel,SimChip } from './common';
import { Composer,type ComposerPrefill } from './Composer';
import { ContextInspector } from './ContextInspector';
import { MessageList } from './MessageList';
import { revealOperation } from './OperationCard';
import { useWorkPanel } from './panel/WorkPanelContext';
import { WorkPanelProvider } from './panel/WorkPanelProvider';
import { SessionOverview } from './SessionOverview';
import { RenameDialog,SessionList,SessionSwitcher } from './Sessions';
import { StartPage } from './StartPage';

// The work panel's canvases (React Flow, run views) load only when an object opens beside the chat.
const WorkPanel = lazy(() => import('./panel/WorkPanel').then((m) => ({ default: m.WorkPanel })));

export type AssistantPanelVariant = 'dock' | 'full';

export function AssistantPanel(props: { variant: AssistantPanelVariant }) {
  // Only the workspace's full layout opens objects beside the chat; elsewhere useWorkPanel() navigates there.
  if (props.variant === 'full') {
    return (
      <WorkPanelProvider>
        <PanelView {...props} />
      </WorkPanelProvider>
    );
  }
  return <PanelView {...props} />;
}

function PanelView({ variant }: { variant: AssistantPanelVariant }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const { store } = session;
  const detail = store.detail;
  const [prefill, setPrefill] = useState<ComposerPrefill | null>(null);
  const [inspector, setInspector] = useState(false);
  const seq = useRef(0);
  const full = variant === 'full';
  const [query] = useSearchParams();
  const focusOperation = query.get('operation');
  const focusStep = query.get('step');
  const focused = useRef<string | null>(null);
  const panelElement = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!full || !focusOperation) { focused.current = null; return; }
    if (!detail?.operations.some((op) => op.id === focusOperation)) return;
    const key = `${detail.conversation.id}:${focusOperation}:${focusStep ?? ''}`;
    if (focused.current === key) return;
    const frame = requestAnimationFrame(() => {
      const stepIndex = focusStep !== null && /^\d+$/.test(focusStep) ? Number(focusStep) : null;
      if (panelElement.current && revealOperation(panelElement.current, focusOperation, stepIndex)) focused.current = key;
    });
    return () => cancelAnimationFrame(frame);
  }, [full, focusOperation, focusStep, detail]);
  const wp = useWorkPanel();
  const panelOpen = full && wp.inline && !!wp.state.object && !!session.conversationId;

  // The work panel takes the right column from the context inspector.
  useEffect(() => {
    if (panelOpen) setInspector(false);
  }, [panelOpen]);
  const toggleInspector = () => {
    if (panelOpen) {
      if (wp.close()) setInspector(true);
    } else setInspector((v) => !v);
  };
  const closeInspector = () => {
    setInspector(false);
    panelElement.current?.querySelector<HTMLButtonElement>('[data-context-toggle]')?.focus();
  };

  const select = (id: UUID) => {
    if (full) navigate(paths.assistant(id));
    else assistant.selectConversation(id);
  };
  const startNew = () => {
    assistant.selectConversation(null);
    if (full) navigate(paths.assistant());
  };

  let body;
  if (session.conversationId && !detail) {
    body =
      store.error && !store.loading ? (
        <div className="as-body-pad">
          <ErrorState error={store.error} onRetry={store.reload} />
          <div className="row">
            <button type="button" className="btn sm" onClick={startNew}>
              <MessageSquarePlus /> {t('as.session.new')}
            </button>
          </div>
        </div>
      ) : (
        <div className="as-body-pad stack-sm" role="status" aria-label={t('common.loading')}>
          <Skeleton h={18} w="60%" />
          <Skeleton h={64} />
          <Skeleton h={64} />
        </div>
      );
  } else if (!detail) {
    body = (
      <div className="as-scroll">
        <StartPage onSelect={select} compact={!full} />
      </div>
    );
  } else {
    body = (
      <>
        <MessageList detail={detail} onPrefill={(p) => setPrefill({ ...p, nonce: ++seq.current })} />
        <Composer detail={detail} projectId={detail.conversation.project.id} prefill={prefill} compact={!full || panelOpen} />
      </>
    );
  }

  const main = (
    <section ref={panelElement} className={`as-panel as-${variant}`} aria-label={t('as.title')}>
      <PanelHeader variant={variant} onSelect={select} onNew={startNew} inspectorOpen={inspector && !panelOpen} onToggleInspector={toggleInspector} />
      <EngineeringTeam />
      <Notices />
      {full && focusOperation && detail && !detail.operations.some((op) => op.id === focusOperation) && <p className="callout warn">{t('core.focus.missing')}</p>}
      {body}
    </section>
  );

  if (!full) return main;
  return (
    <div className={`as-fullpage as-journey-layout ${inspector ? 'insp-open' : ''} ${panelOpen ? 'wp-on' : ''} ${panelOpen && wp.state.wide ? 'wp-wide' : ''}`}>
      <SessionList onSelect={select} onNew={startNew} />
      {main}
      {panelOpen ? (
        <Suspense fallback={<aside className="wp" aria-busy="true" />}>
          <WorkPanel />
        </Suspense>
      ) : (
        inspector && detail && (
          <aside className="as-full-insp" aria-label={t('as.insp.title')}>
            <div className="as-insp-head">
              <h2>{t('as.insp.title')}</h2>
              <button type="button" className="btn sm ghost icon as-insp-close" onClick={closeInspector} aria-label={t('as.insp.close')}>
                <X />
              </button>
            </div>
            <SessionOverview detail={detail} />
            <ContextInspector detail={detail} />
          </aside>
        )
      )}
    </div>
  );
}

function noticeText(t: ReturnType<typeof useI18n>['t'], n: AssistantNotice): string {
  if (n.kind === 'conversation_switched') return t('as.notice.switched', { title: n.title || t('as.session.untitled') });
  const node = n.ref.node ? ` / ${nodeDisplayName(t, n.ref.node)}` : '';
  const name = `${refKindLabel(t, n.ref.kind)} · ${n.ref.title}${n.ref.version_label ? ` ${n.ref.version_label}` : ''}${node}`;
  return t(`as.notice.${n.kind}`, { name });
}

/** addContext outcome (target preselected / kept, conversation switched) and its errors. */
function Notices() {
  const { t } = useI18n();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const n = assistant.notice;
  return (
    <>
      {n && (
        <div className="as-banner" role="status">
          <Info aria-hidden />
          <span className="grow">{noticeText(t, n)}</span>
          <button type="button" className="as-icon-btn" onClick={assistant.clearNotice} aria-label={t('as.notice.dismiss')}>
            <X />
          </button>
        </div>
      )}
      {assistant.lastError ? (
        <div className="as-banner-err">
          <AsError error={assistant.lastError} />
          <button type="button" className="as-icon-btn" onClick={session.clearError} aria-label={t('as.notice.dismiss')}>
            <X />
          </button>
        </div>
      ) : null}
    </>
  );
}

function PanelHeader({
  variant,
  onSelect,
  onNew,
  inspectorOpen,
  onToggleInspector,
}: {
  variant: AssistantPanelVariant;
  onSelect: (id: UUID) => void;
  onNew: () => void;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
}) {
  const { t } = useI18n();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const { store, capabilities } = session;
  const conv = store.detail?.conversation ?? null;
  const [renaming, setRenaming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const full = variant === 'full';
  const title = conv ? conv.title || t('as.session.untitled') : t('as.title');
  const backLabel = session.backTo === paths.workbench() ? t('home.back.workbench') : t('home.back.page');
  const hasHeaderStatus = !!(conv?.archived || conv?.task || capabilities?.simulation);

  const setArchived = async (archived: boolean) => {
    if (!conv) return;
    setError(null);
    try {
      await store.patch({ archived, expected_version: conv.version });
      session.bumpList();
    } catch (e) {
      setError(e);
    }
  };

  const items: MenuItem[] = [];
  if (conv && !conv.archived) items.push({ id: 'rename', label: t('common.rename'), icon: <Pencil />, onSelect: () => setRenaming(true) });
  if (conv) items.push(conv.archived ? { id: 'restore', label: t('as.session.restore'), icon: <RotateCcw />, onSelect: () => void setArchived(false) } : { id: 'archive', label: t('common.archive'), icon: <Archive />, onSelect: () => void setArchived(true) });
  if (!full) items.push({ id: 'new', label: t('as.session.new'), icon: <MessageSquarePlus />, onSelect: onNew, separator: !!conv });

  return (
    <header className="as-head">
      {full && (
        // Leaves the workspace for the page it was opened from (the dock stays closed; also on phones).
        <Link to={session.backTo} className="btn sm ghost as-back" aria-label={backLabel} title={backLabel}>
          <ArrowLeft aria-hidden /> <span className="as-back-label">{backLabel}</span>
        </Link>
      )}
      <div className="as-head-main">
        <AgentRoleIcon role="manager" className="as-head-ico" />
        <div className="as-head-titles">
          {full ? <h1 className="as-head-title" title={title}>{title}</h1> : <SessionSwitcher title={title} onSelect={onSelect} onNew={onNew} />}
          <div className={`as-head-sub ${hasHeaderStatus ? 'as-head-sub-important' : ''}`}>
            {conv ? (
              <>
                <span className="as-head-project">
                  <span className="mono" title={conv.project.name}>
                    {conv.project.code}
                  </span>
                  <span className="ellip">{conv.project.name}</span>
                </span>
                {conv.task && (
                  <>
                    <span className="as-head-project" aria-hidden>·</span>
                    <Link to={paths.task(conv.task.id)} className="ellip" title={t('as.head.task')}>
                      {conv.task.title}
                    </Link>
                  </>
                )}
              </>
            ) : (
              <span>{t('as.head.noConversation')}</span>
            )}
            {conv?.archived && (
              <Chip tone="pending" icon={<Archive aria-hidden />} className="sm">
                {t('common.archived')}
              </Chip>
            )}
            {capabilities?.simulation && <SimChip title={t('as.head.simulatedHint')} />}

          </div>
        </div>
      </div>
      <div className="as-head-acts">
        {full && conv && (
          <button type="button" data-context-toggle className={`btn sm ghost as-insp-toggle ${inspectorOpen ? 'active' : ''}`} aria-label={t('as.insp.title')} aria-expanded={inspectorOpen} onClick={onToggleInspector}>
            <ScanSearch /> <span className="as-hide-narrow">{t('as.insp.title')}</span>
          </button>
        )}
        {items.length > 0 && <Menu label={t('as.head.menu')} trigger={<MoreHorizontal />} items={items} />}
        {!full && (
          <button type="button" className="btn ghost icon" onClick={session.expand} aria-label={t('as.head.expand')} title={t('as.head.expand')}>
            <Maximize2 />
          </button>
        )}
        {full && (
          <button type="button" className="btn ghost icon hide-mobile" onClick={session.collapse} aria-label={t('as.head.collapse')} title={t('as.head.collapse')}>
            <PanelRight />
          </button>
        )}
        {variant === 'dock' && (
          <button type="button" className="btn ghost icon" onClick={assistant.close} aria-label={t('as.head.close')} title={`${t('as.head.close')} (Esc)`}>
            <PanelRightClose />
          </button>
        )}
      </div>
      {error ? (
        <div className="as-head-err">
          <AsError error={error} />
        </div>
      ) : null}
      {renaming && conv && <RenameDialog id={conv.id} title={conv.title} onClose={() => setRenaming(false)} />}
    </header>
  );
}
