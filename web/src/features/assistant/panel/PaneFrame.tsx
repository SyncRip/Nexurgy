// Frame of a work panel pane (.trellis/tasks/archive/2026-10/10-12-ai-work-panel/design.md §4): the header with the object, its
// version / status chips, lock, wide / restore, open full page and close (a back arrow on phones), the chat target
// line, then the pane body.

import { ArrowLeft,Crosshair,ExternalLink,Lock,LockOpen,Maximize2,Minimize2,X } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { AiRef } from '../../../api/assistant';
import { Skeleton } from '../../../components/ui/States';
import { useI18n,type TFunction } from '../../../i18n/I18nProvider';
import { nodeDisplayName } from '../../workflow/labels';
import { useAssistant,useAssistantSession } from '../AssistantProvider';
import { targetLine,type NodeSource } from './model';
import { useWorkPanel } from './WorkPanelContext';
import { usePanelInternals } from './WorkPanelProvider';

/** "Bracket flow / Modeling": the reference title with the localized node name (design §2.1). */
export function refTargetLabel(t: TFunction, ref: Pick<AiRef, 'title' | 'node'>): string {
  return ref.node ? `${ref.title} / ${nodeDisplayName(t, ref.node)}` : ref.title;
}

/** Placeholder frame while a pane loads its object. */
export function PaneLoading({ icon, kind }: { icon?: ReactNode; kind?: string }) {
  const { t } = useI18n();
  return (
    <PaneFrame icon={icon} title={t('common.loading')} kind={kind}>
      <div className="wp-pad stack-sm" role="status" aria-label={t('common.loading')}>
        <Skeleton h={18} w="50%" />
        <Skeleton h={160} />
        <Skeleton h={48} />
      </div>
    </PaneFrame>
  );
}

/**
 * Header and body of a pane. `source`: the object whose nodes can become the chat target (workflow / run panes:
 * lock toggle and "set as target"); `fullHref`: its full page.
 */
export function PaneFrame({
  icon,
  title,
  kind,
  chips,
  fullHref,
  source,
  children,
  bodyClass = '',
}: {
  icon?: ReactNode;
  title: string;
  /** Object kind label above the title ("Workflow", "Run" …). */
  kind?: string;
  chips?: ReactNode;
  fullHref?: string | null;
  source?: NodeSource | null;
  children: ReactNode;
  bodyClass?: string;
}) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const internals = usePanelInternals();
  const session = useAssistantSession();
  const wide = wp.state.wide;
  return (
    <>
      <header className="wp-head">
        <button type="button" className="btn ghost icon wp-back" onClick={wp.close} aria-label={t('wp.back')}>
          <ArrowLeft />
        </button>
        {icon && <span className="wp-ico" aria-hidden>{icon}</span>}
        <div className="wp-titles">
          {kind && <span className="wp-kind">{kind}</span>}
          <h2 className="wp-title ellip" tabIndex={-1} title={title}>
            {title}
          </h2>
          {chips && <div className="wp-chips">{chips}</div>}
        </div>
        <div className="wp-acts">
          {source && (
            <button
              type="button"
              className={`btn sm ghost wp-lock ${internals.locked ? 'active' : ''}`}
              aria-pressed={internals.locked}
              onClick={() => internals.setLocked(!internals.locked)}
              title={internals.locked ? t('wp.lock.onHint') : t('wp.lock.offHint')}
            >
              {internals.locked ? <Lock /> : <LockOpen />} <span className="wp-hide-narrow">{t('wp.lock')}</span>
            </button>
          )}
          <button type="button" className="btn ghost icon wp-wide-btn" onClick={() => wp.setWide(!wide)} aria-pressed={wide} aria-label={wide ? t('wp.restore') : t('wp.widen')} title={wide ? t('wp.restore') : t('wp.widen')}>
            {wide ? <Minimize2 /> : <Maximize2 />}
          </button>
          {fullHref && (
            <Link to={fullHref} className="btn ghost icon" aria-label={t('wp.openFull')} title={t('wp.openFull')} onClick={session.keepDockOnLeave}>
              <ExternalLink />
            </Link>
          )}
          <button type="button" className="btn ghost icon wp-close" onClick={wp.close} aria-label={t('wp.close')} title={`${t('wp.close')} (Esc)`}>
            <X />
          </button>
        </div>
      </header>
      <TargetBar source={source ?? null} />
      <div className={`wp-body ${bodyClass}`}>{children}</div>
    </>
  );
}

/** "Chat target: Bracket flow / Modeling · follows the selection" — or "(not following) [Set as target]". */
function TargetBar({ source }: { source: NodeSource | null }) {
  const { t } = useI18n();
  const wp = useWorkPanel();
  const internals = usePanelInternals();
  const { store } = useAssistantSession();
  const { pending } = useAssistant();
  const detail = store.detail;
  if (!detail) return null;
  const target = pending.references.find((r) => r.target) ?? null;
  const nodeId = wp.state.nodeId;
  const sel = source && nodeId ? { src: source, nodeId } : null;
  const line = targetLine(target ? { ...target.input, node: target.input.node ? { ...target.input.node, type: '', label: null } : null } : null, sel, internals.locked);
  const name = target?.label ?? '';
  const text =
    line === 'none'
      ? t('wp.target.none')
      : line === 'following'
        ? t('wp.target.following', { name })
        : line === 'not_following'
          ? t('wp.target.notFollowing', { name })
          : t('wp.target.plain', { name });
  const canSet = !!sel && (line === 'none' || line === 'not_following') && !detail.conversation.archived;
  return (
    <div className={`wp-target ${line}`}>
      <Crosshair aria-hidden />
      <span className="wp-target-text" title={text}>
        {text}
      </span>
      {canSet && sel && (
        <button type="button" className="btn sm" onClick={() => void internals.targetNode(sel.src, sel.nodeId)}>
          {t('wp.target.set')}
        </button>
      )}
    </div>
  );
}
