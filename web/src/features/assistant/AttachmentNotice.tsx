// Generation supplement A-01 (design .trellis/tasks/archive/2026-10/10-12-generation-supplement/design.md §2.5): while the person types
// text that clearly points at an attachment and the composer holds no file, the composer asks the server which file it
// means. A unique file is added as a visible, removable reference with a short note; several are listed to pick from;
// none says a file must be referenced (with the project files entry). A file the person removed is never added back,
// and the send then says so (auto_references=false). The send's own refusals (attachment_ambiguous / _required,
// spec_and_sources) are shown here with their choices too.

import { FolderOpen, Paperclip, Plus, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { resolveAiAttachments, type AiAttachmentResolution, type AiAutoReferenceReason, type AiRef } from '../../api/assistant';
import { isAbortError, isApiError, type ApiError } from '../../api/client';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { fileLabel, shouldResolveAttachment } from './attachments';
import { tk } from './common';

type Candidate = AiAttachmentResolution['candidates'][number];

export interface AttachState {
  /** The text the server answered for. */
  text: string;
  /** dismissed: the unique file was added and the person removed it (never added back). */
  status: 'unique' | 'ambiguous' | 'required' | 'dismissed';
  candidates: Candidate[];
  reason: AiAutoReferenceReason | null;
  /** The composer key of the file added automatically. */
  addedKey?: string;
}

/** Codes of a refused send this notice explains (with their choices). */
export const ATTACH_ISSUES = ['attachment_ambiguous', 'attachment_required', 'spec_and_sources'] as const;

export function attachIssue(error: unknown): ApiError | null {
  return isApiError(error) && (ATTACH_ISSUES as readonly string[]).includes(error.code) ? error : null;
}

/**
 * The composer's pre-resolve. ``addRef`` adds a candidate (the store's add-reference). Returns the state and whether
 * a send should carry ``auto_references: false`` (the person saw the automatic file and kept or removed it).
 */
export function useAttachmentResolve({
  conversationId,
  text,
  refs,
  enabled,
  addRef,
  operation = 'auto',
  target = null,
}: {
  conversationId: UUID | null;
  text: string;
  refs: AiRef[];
  enabled: boolean;
  /** The operation a send would ask for (resolved) and the composer's target: see shouldResolveAttachment. */
  operation?: string;
  target?: AiRef | null;
  addRef: (fileVersionId: UUID) => Promise<string>;
}): { state: AttachState | null; clear: () => void; manual: boolean } {
  const [state, setState] = useState<AttachState | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const dismissed = useRef(new Set<string>());
  const trimmed = text.trim();
  const refsKey = refs.map((r) => r.key).join('|');
  const wanted = enabled && !!conversationId && shouldResolveAttachment(trimmed, refs, operation, target);

  useEffect(() => {
    const prev = stateRef.current;
    const kept = !!prev?.addedKey && refs.some((r) => r.key === prev.addedKey);
    if (prev?.status === 'unique' && prev.addedKey && !kept) dismissed.current.add(prev.candidates[0].object_id);
    if (!wanted || !conversationId) {
      if (!kept) setState(null);
      return;
    }
    const ctl = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const res = await resolveAiAttachments(conversationId, { text: trimmed, operation }, { signal: ctl.signal });
          const first = res.candidates[0];
          if (res.status === 'unique' && first && !dismissed.current.has(first.object_id)) {
            const key = await addRef(first.object_id);
            setState({ text: trimmed, status: 'unique', candidates: [first], reason: res.reason, addedKey: key });
          } else if (res.status === 'unique' && first) {
            setState({ text: trimmed, status: 'dismissed', candidates: [first], reason: res.reason });
          } else if (res.status === 'ambiguous' || res.status === 'required') {
            setState({ text: trimmed, status: res.status, candidates: res.candidates, reason: res.reason });
          } else {
            setState(null);
          }
        } catch (e) {
          if (!isAbortError(e)) setState(null); // the send still binds on the server
        }
      })();
    }, 600);
    return () => {
      window.clearTimeout(timer);
      ctl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted, trimmed, conversationId, refsKey, operation]);

  const manual = !!state && (state.status === 'unique' || state.status === 'dismissed');
  return { state, clear: () => setState(null), manual };
}

function reasonText(t: TFunction, reason: string | null | undefined): string {
  return reason ? tk(t, `as.attach.reason.${reason}`, reason) : '';
}

export function AttachmentNotice({
  state,
  issue,
  projectId,
  busy,
  onUse,
  onRemove,
  onRevise,
}: {
  state: AttachState | null;
  /** A refused send (attachment_ambiguous / attachment_required / spec_and_sources): wins over the pre-resolve state. */
  issue: ApiError | null;
  projectId: UUID | null;
  busy: boolean;
  onUse: (fileVersionId: UUID) => void;
  onRemove: (keys: string[]) => void;
  /** Generation supplement I (A-04 / IN-07): send again with the choice — true "Generate a new specification version", false "Generate from the original specification". */
  onRevise?: (revise: boolean) => void;
}) {
  const { t } = useI18n();
  const filesLink = (
    <Link className="btn sm ghost" to={paths.resources('files', projectId ?? undefined)}>
      <FolderOpen aria-hidden /> {t('as.attach.addFromFiles')}
    </Link>
  );
  if (issue?.code === 'spec_and_sources') {
    const d = issue.details as { spec_key?: string; file_keys?: string[]; text_changes?: boolean };
    const fileKeys = d.file_keys ?? [];
    return (
      <div className="callout warn slim as-attach" role="alert">
        <Paperclip aria-hidden />
        <div className="grow stack-sm">
          <div>{t('as.attach.specAndSources')}</div>
          {d.text_changes && <div className="faint small">{t('as.attach.textChanges')}</div>}
          {onRevise && (
            <div className="row wrap">
              <button type="button" className="btn sm primary" disabled={busy} title={t('rv.choice.reviseHint')} onClick={() => onRevise(true)}>
                {t('rv.choice.revise')}
              </button>
              <button type="button" className="btn sm" disabled={busy} title={t('rv.choice.keepHint')} onClick={() => onRevise(false)}>
                {t('rv.choice.keep')}
              </button>
            </div>
          )}
          <div className="row wrap">
            {fileKeys.length > 0 && (
              <button type="button" className="btn sm" disabled={busy} onClick={() => onRemove(fileKeys)}>
                {t('as.attach.keepSpec')}
              </button>
            )}
            {d.spec_key && (
              <button type="button" className="btn sm" disabled={busy} onClick={() => onRemove([d.spec_key!])}>
                {t('as.attach.useNew')}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }
  const status = issue?.code === 'attachment_ambiguous' ? 'ambiguous' : issue?.code === 'attachment_required' ? 'required' : state?.status;
  const candidates: Candidate[] = issue?.code === 'attachment_ambiguous' ? ((issue.details.candidates as Candidate[]) ?? []) : (state?.candidates ?? []);
  if (!status) return null;
  if (status === 'unique' && candidates[0]) {
    return (
      <p className="as-ctx-note as-attach" role="status">
        <Paperclip className="inline-ico" aria-hidden /> {t('as.attach.unique', { file: fileLabel(candidates[0]), reason: reasonText(t, state?.reason) })}
      </p>
    );
  }
  if (status === 'dismissed' && candidates[0]) {
    return (
      <div className="callout warn slim as-attach" role="status">
        <X aria-hidden />
        <div className="grow stack-sm">
          <div>{t('as.attach.dismissed', { file: fileLabel(candidates[0]) })}</div>
          <div className="row wrap">
            <button type="button" className="btn sm" disabled={busy} onClick={() => onUse(candidates[0].object_id)}>
              <Plus aria-hidden /> {t('as.attach.useAgain')}
            </button>
            {filesLink}
          </div>
        </div>
      </div>
    );
  }
  if (status === 'ambiguous') {
    return (
      <div className="callout warn slim as-attach" role={issue ? 'alert' : 'status'}>
        <Paperclip aria-hidden />
        <div className="grow stack-sm">
          <div>{t('as.attach.ambiguous', { n: candidates.length })}</div>
          <ul className="as-plain">
            {candidates.map((c) => (
              <li key={c.key} className="row wrap">
                <span className="grow">
                  {fileLabel(c)} <span className="faint small">· {reasonText(t, c.binding_reason)}</span>
                </span>
                <button type="button" className="btn sm" disabled={busy} onClick={() => onUse(c.object_id)}>
                  <Plus aria-hidden /> {t('as.attach.use')}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }
  return (
    <div className="callout warn slim as-attach" role={issue ? 'alert' : 'status'}>
      <Paperclip aria-hidden />
      <div className="grow stack-sm">
        <div>{t('as.attach.required')}</div>
        <div className="row wrap">{filesLink}</div>
      </div>
    </div>
  );
}
