// Local, removable context is normalized only when the person sends a request.
import { Archive,Hand,RotateCcw,Save,Send,Target,X } from 'lucide-react';
import { useEffect,useId,useMemo,useRef,useState,type ReactNode } from 'react';
import { addAiReference,createAiConversation,type AiConversationDetail,type AiRef,type AiRefInput,type AiSendBody } from '../../api/assistant';
import { inputLimit,useLimits } from '../../api/limits';
import type { UUID } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { useAssistant,useAssistantSession } from './AssistantProvider';
import { attachIssue,AttachmentNotice,useAttachmentResolve } from './AttachmentNotice';
import { AsError } from './common';
import { afterSent,editText,prepareSend,type ComposerDraft } from './drafts';
import { conversationEntries,currentJourney } from './journey';
import { focusJourney } from './JourneyNavigator';
import { applySendResult,becomesMaterial,newRequestId,operationEvents,requestRecorded,sendBlock,type ComposerOperation } from './model';
import { inputOnly,pendingFromRef,refIdentity,sendFingerprint,visibleReferences,type PendingReference } from './pageContext';
import { useWorkPanel } from './panel/WorkPanelContext';
import { preflightBody } from './preflight';
import { PreflightNotice,usePreflight } from './PreflightNotice';

export interface ComposerPrefill { text?: string; operation?: ComposerOperation; nonce: number }
let handledFocusNonce = 0;

function previewRef(entry: PendingReference): AiRef {
  return { ...entry.input, key: entry.key ?? refIdentity(entry.input), title: entry.label, summary: '', part_id: null, task_id: null, version_label: null,
    selection: entry.input.selection ?? null, field: entry.input.field ? { ...entry.input.field, value: null } : null,
    excerpt: entry.input.excerpt ?? null, output: entry.input.output ?? null, source_path: '', snapshot_at: '',
    node: entry.input.node ? { ...entry.input.node, type: '', label: null } : null } as AiRef;
}

export function Composer({ detail, projectId, prefill, compact, onSent, placeholder, variant = 'default', leading, barStart, sendLabel, hint }: {
  detail: AiConversationDetail | null; projectId: UUID | null; prefill?: ComposerPrefill | null; compact?: boolean;
  onSent?: (conversationId: UUID) => void; placeholder?: string; variant?: 'default' | 'line'; leading?: ReactNode;
  barStart?: (operation: ComposerOperation) => ReactNode; sendLabel?: string; hint?: string;
}) {
  const { t } = useI18n();
  const assistant = useAssistant();
  const session = useAssistantSession();
  const { store, drafts } = session;
  const panel = useWorkPanel();
  const conversation = detail?.conversation ?? null;
  const archived = !!conversation?.archived;
  const key = drafts.key(conversation?.id ?? null, conversation ? null : projectId);
  const [draft, setDraft] = useState<ComposerDraft>(() => drafts.read(key));
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const keyRef = useRef(key);
  keyRef.current = key;
  const textId = useId();
  const hintId = useId();
  const decisionHintId = useId();
  const current = useMemo(() => detail ? currentJourney(conversationEntries(detail)) : null, [detail?.messages, detail?.operations]);
  const waiting = current?.status === 'waiting' ? current : null;
  const line = variant === 'line';
  const pid = conversation?.project.id ?? projectId;
  const ctx = assistant.pending;
  const fallback = !ctx.scope && !ctx.references.length ? (detail?.composer.references ?? []).map((r) => pendingFromRef(r, r.key === detail?.composer.target_key)) : [];
  const pending = visibleReferences([...ctx.references, ...fallback].filter((r) => r.input.project_id === pid), ctx.suppressed);
  const refs = pending.map(previewRef);
  const target = refs.find((_, i) => pending[i].target) ?? null;
  const save = (d: ComposerDraft, dest = keyRef.current) => {
    const saved = { ...d, operation: 'auto' as const, modelConfigId: null, pending, suppressed: ctx.suppressed, contextScope: ctx.scope };
    drafts.write(dest, saved);
    if (dest === keyRef.current) setDraft(saved);
  };
  useEffect(() => {
    const d = drafts.read(key);
    setDraft(d);
    setError(null);
    if (d.pending || d.suppressed) assistant.restorePending({ scope: d.contextScope ?? null, references: d.pending ?? [], suppressed: d.suppressed ?? [] });
    // A conversation owns its draft; page changes are handled by the provider's semantic scope.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, drafts]);
  useEffect(() => {
    if (!sending && requestRecorded(draft.requestId, detail)) save(afterSent(draft));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.operations, detail?.messages, draft.requestId, sending]);
  useEffect(() => {
    if (prefill?.text !== undefined) save(editText(drafts.read(keyRef.current), prefill.text));
    if (prefill) textRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);
  useEffect(() => {
    if (session.prefillText) { save(editText(drafts.read(keyRef.current), session.prefillText.text)); session.consumeText(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.prefillText?.nonce]);
  useEffect(() => {
    if (session.focusNonce > handledFocusNonce) { handledFocusNonce = session.focusNonce; textRef.current?.focus(); }
  }, [session.focusNonce]);

  const queueFile = async (id: UUID) => {
    const input = { kind: 'file_version' as const, object_id: id, project_id: pid! };
    assistant.queuePending({ input, label: t('as.refKind.file_version') });
    return refIdentity(input);
  };
  const attach = useAttachmentResolve({ conversationId: conversation?.id ?? null, text: draft.text, refs, enabled: !archived, addRef: queueFile, operation: 'auto', target });
  const limits = useLimits();
  const chatLimit = inputLimit(limits, 'chat');
  const materialLimit = inputLimit(limits, 'material');
  const material = becomesMaterial(draft.text, chatLimit);
  const block = sendBlock({ archived, text: draft.text, sending, targetStatus: target && detail?.composer.ref_status[target.key], hasProject: !!pid, maxChars: materialLimit });
  const panelDraft = panel.draft?.dirty ? panel.draft : null;
  const saveFirst = !!panelDraft && pending.some((r) => r.input.kind === 'workflow' && r.input.object_id === panelDraft.workflowId);
  const bodyFor = (d: ComposerDraft, entries: PendingReference[], revise?: boolean): AiSendBody => ({
    client_request_id: d.requestId!, text: d.text.trim(), operation: 'auto',
    target_key: entries.find((r) => r.target)?.key ?? null,
    reference_keys: entries.filter((r) => !r.target).map((r) => r.key!).filter(Boolean), auto_references: false,
    ...(becomesMaterial(d.text, chatLimit) ? { as_material: true } : {}), ...(revise !== undefined ? { revise } : {}),
  });
  const allNormalized = pending.every((r) => r.key && detail?.composer.references.some((v) => v.key === r.key));
  const pf = usePreflight({ conversationId: conversation?.id ?? null,
    body: draft.text.trim() && allNormalized ? preflightBody(bodyFor({ ...draft, requestId: draft.requestId ?? 'preflight' }, pending)) : null,
    enabled: !!conversation && !block && !sending && !saveFirst && allNormalized });

  const remove = (identity: string) => {
    assistant.removePending(identity);
    const next = { ...draft, pending: pending.filter((r) => refIdentity(r.input) !== identity), suppressed: [...new Set([...ctx.suppressed, identity])], contextScope: ctx.scope };
    drafts.write(key, next);
    setDraft(next);
  };
  const send = async (revise?: boolean) => {
    if (block || (pf.block && revise === undefined) || !pid) return;
    setSending(true);
    setError(null);
    let active = detail;
    let dest = key;
    let d = draft;
    try {
      let entries = pending;
      if (saveFirst && panelDraft) {
        const saved = await panelDraft.save();
        entries = entries.map((r) => {
          if (r.input.kind !== 'workflow' || r.input.object_id !== panelDraft.workflowId) return r;
          if (r.input.node && !saved.graph.nodes.some((node) => node.id === r.input.node!.node_id)) throw new Error(t('fc.composer.unsaved'));
          return { ...r, key: undefined, input: { ...r.input, version: saved.version } };
        });
        assistant.restorePending({ ...ctx, references: entries });
      }
      const fingerprint = sendFingerprint(pid, d.text, entries, revise);
      d = prepareSend({ ...d, pending: entries, suppressed: ctx.suppressed, contextScope: ctx.scope }, newRequestId, fingerprint);
      drafts.write(dest, d);
      setDraft(d);
      if (!active) {
        active = await createAiConversation({ project_id: pid });
        dest = drafts.key(active.conversation.id);
        drafts.write(dest, d);
        drafts.remove(key);
      }
      let body = d.attemptedBody;
      if (!body) {
        const normalized: PendingReference[] = [];
        for (const entry of entries) {
          const { project_id: _project, ...input } = inputOnly(entry.input);
          const result = await addAiReference(active.conversation.id, { ref: input, as_target: !!entry.target });
          active = { ...active, composer: result.composer };
          normalized.push({ ...entry, key: result.added.key });
        }
        body = bodyFor(d, normalized, revise);
        d = { ...d, attemptedBody: body, pending: normalized };
        drafts.write(dest, d); // Write the exact attempted body before network I/O.
        if (dest === keyRef.current) setDraft(d);
      }
      const result = await store.send(active.conversation.id, body);
      drafts.write(dest, afterSent(d));
      if (dest === keyRef.current) setDraft(afterSent(d));
      if (!conversation) {
        const sent = applySendResult(active, result);
        session.adoptConversation(sent);
        // The first send is an explicit action, so a synchronous saved result
        // refreshes its source page even before this conversation is mounted.
        operationEvents(active.operations, sent.operations).forEach((event) => assistant.emitOperationEvent({ ...event, projectId: pid }));
      }
      else session.bumpList();
      attach.clear();
      session.clearError();
      onSent?.(active.conversation.id);
    } catch (e) {
      if (active && !conversation) { session.adoptConversation(active); session.reportError(e); }
      else setError(e);
    } finally { setSending(false); }
  };
  const restore = async () => {
    setError(null);
    try { await store.patch({ archived: false, expected_version: conversation!.version }); session.bumpList(); }
    catch (e) { setError(e); }
  };
  if (archived) return <div className="as-composer as-archived-bar" role="status"><Archive /><span className="grow">{t('as.archived.banner')}</span><button type="button" className="btn sm" onClick={() => void restore()}><RotateCcw />{t('as.session.restore')}</button><AsError error={error} /></div>;

  return <div className={`as-composer ${line ? 'as-line' : ''} ${compact ? 'compact' : ''} ${sending ? 'is-sending' : ''}`} aria-busy={sending || undefined}>
    {waiting && <div className="as-journey-composer-note" id={decisionHintId}>
      <Hand aria-hidden /><span>{t('as.journey.composerDecision')}</span>
      <button type="button" className="linkish small" onClick={() => focusJourney(textRef.current?.closest('.as-panel') ?? document, waiting)}>{t('as.journey.jump.waiting')}</button>
    </div>}
    {!!pending.length && <div className="as-pending" aria-label={t('core.context.label')}>{pending.map((entry) => {
      const identity = refIdentity(entry.input);
      return <span key={identity} className={`as-pending-chip ${entry.target ? 'is-target' : ''}`} title={entry.label}>
        {entry.target && <Target size={13} aria-label={t('core.context.target')} />}<span>{entry.label}</span>
        <button type="button" className="as-icon-btn" onClick={() => remove(identity)} disabled={sending} aria-label={t('core.context.remove', { name: entry.label })}><X /></button>
      </span>;
    })}</div>}
    {assistant.pageContext?.unavailable && <p className="as-ctx-warn" role="status">{assistant.pageContext.unavailable}</p>}
    <div className="as-input">
      <label htmlFor={textId} className="sr-only">{t('as.input.label')}</label>
      <div className={line ? 'as-line-in' : undefined}>{leading}<textarea id={textId} ref={textRef} className="input as-textarea" rows={compact || line ? 2 : 3} maxLength={materialLimit} value={draft.text} placeholder={placeholder ?? t('as.ph.auto')} aria-describedby={waiting ? `${decisionHintId} ${hintId}` : hintId} readOnly={sending}
        onChange={(e) => save(editText(draft, e.target.value))} onKeyDown={(e) => {
          if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229
            || (e.altKey && !e.metaKey && !e.ctrlKey)) return;
          e.preventDefault();
          if (!e.repeat) void send();
        }} /></div>
      <div className="as-input-bar">{barStart?.('auto')}<span className="grow" /><span className="as-input-hint" id={hintId}>{block && block !== 'empty' ? t(`as.block.${block}`) : material ? t('as.material.willSave', { n: draft.text.trim().length, max: chatLimit ?? 0 }) : hint ?? t('as.input.hint')}</span>
        <button type="button" className="btn primary" onClick={() => void send()} disabled={!!block || pf.block}>{saveFirst ? <Save /> : <Send />}{sending ? t('as.sending') : saveFirst ? t('fc.composer.saveAndSend') : sendLabel ?? t('as.send')}</button>
      </div>
    </div>
    {saveFirst && <p className="as-ctx-warn"><Save className="inline-ico" />{t('fc.composer.unsaved')}</p>}
    <AttachmentNotice state={attach.state} issue={attachIssue(error)} projectId={pid} busy={sending} onUse={(id) => void queueFile(id)} onRemove={(keys) => pending.filter((r) => keys.includes(r.key ?? refIdentity(r.input))).forEach((r) => remove(refIdentity(r.input)))} onRevise={(revise) => void send(revise)} />
    <PreflightNotice preflight={pf.preflight} projectId={pid} busy={sending} attachShown={!!attach.state || !!attachIssue(error)} onAdd={(ref) => { if (pid) assistant.queuePending({ input: { ...(ref as AiRefInput), project_id: pid }, label: t('core.context.label') }); }} />
    {!drafts.persistent && <p className="as-ctx-note">{t('as.draft.noStorage')}</p>}
    <AsError error={error} />
  </div>;
}
