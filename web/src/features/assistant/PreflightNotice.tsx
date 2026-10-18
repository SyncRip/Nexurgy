// Generation reliability K (design .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.3): while the person types, the
// composer asks the server what a send would do (POST …/preflight: no model call, nothing written) and shows it —
// "Will execute: <operation> · target <object> · n references". When something is missing (part_required, target_required …, the same checks a
// send fails with) the send is held back and the entry that fixes it is offered (add the suggested part, open the
// project files). A preflight that failed or is not yet answered never blocks a send: the server checks again.

import { CircleAlert, FolderOpen, Plus, Route } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { preflightAiMessage, type AiPreflight, type AiSendBody } from '../../api/assistant';
import { isAbortError } from '../../api/client';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { opKindLabel } from './common';
import { blocksSend, missingAction, ownMissing, preflightKey, targetLabel } from './preflight';

type Body = Omit<AiSendBody, 'client_request_id'>;

function tx(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

/** The debounced preflight of the composer's current request. ``block``: the fresh answer for exactly this body says
 * something is missing (a stale or failed answer never blocks). */
export function usePreflight({ conversationId, body, enabled }: { conversationId: UUID | null; body: Body | null; enabled: boolean }): {
  preflight: AiPreflight | null;
  block: boolean;
} {
  const [state, setState] = useState<{ key: string; value: AiPreflight } | null>(null);
  const key = body && conversationId ? `${conversationId}:${preflightKey(body)}` : '';
  const bodyRef = useRef(body);
  bodyRef.current = body;
  useEffect(() => {
    const b = bodyRef.current;
    if (!enabled || !conversationId || !b || !b.text.trim()) {
      setState(null);
      return;
    }
    const ctl = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const value = await preflightAiMessage(conversationId, b, { signal: ctl.signal });
          if (!ctl.signal.aborted) setState({ key, value });
        } catch (e) {
          if (!isAbortError(e)) setState(null); // the send checks again on the server
        }
      })();
    }, 500);
    return () => {
      window.clearTimeout(timer);
      ctl.abort();
    };
  }, [enabled, conversationId, key]);
  const fresh = enabled && state && state.key === key ? state.value : null;
  return { preflight: fresh, block: blocksSend(fresh) };
}

export function PreflightNotice({
  preflight,
  projectId,
  busy,
  attachShown,
  onAdd,
}: {
  preflight: AiPreflight | null;
  projectId: UUID | null;
  busy: boolean;
  /** The attachment notice already explains attachment / spec-and-sources problems (their choices are there). */
  attachShown: boolean;
  /** Add a suggested object (e.g. the part of a task) to the composer. */
  onAdd: (ref: { kind: string; object_id: string }) => void;
}) {
  const { t } = useI18n();
  if (!preflight) return null;
  const op = preflight.operation_source === 'main_agent' ? t('pf.op.assistant')
    : preflight.operation_source === 'fallback' ? t('pf.op.readonly')
    : preflight.operation ? opKindLabel(t, preflight.operation) : t('pf.op.assistant');
  const target = targetLabel(preflight.target);
  const type = tx(t, `pf.type.${preflight.target.type}`, preflight.target.type);
  const missing = attachShown ? ownMissing(preflight) : preflight.missing;
  const autos = preflight.references.filter((r) => r.auto).length;
  return (
    <div className={`as-preflight ${missing.length ? 'callout warn slim' : 'as-ctx-note'}`} role="status" aria-live="polite">
      <span className="row wrap">
        <Route className="inline-ico" aria-hidden />
        <span>
          {t('pf.will', {
            op,
            target: preflight.target.type === 'none'
              ? t(preflight.operation === 'assembly_create' ? 'pf.target.newAssembly' : 'pf.target.none')
              : `${type} ${target}`.trim(),
            n: preflight.references.length,
          })}
          {autos > 0 && <span className="faint"> {t('pf.auto', { n: autos })}</span>}
          {preflight.target.via === 'latest_revision' && <span className="faint"> · {t('pf.latest')}</span>}
          {preflight.operation && !preflight.will_call_model && !preflight.missing.length && <span className="faint"> · {t('pf.noModel')}</span>}
        </span>
      </span>
      {missing.length > 0 && (
        <ul className="as-plain stack-sm">
          {missing.map((m) => {
            const a = missingAction(m);
            return (
              <li key={`${m.code}:${m.message}`} className="row wrap">
                <CircleAlert className="inline-ico" aria-hidden />
                <span className="grow">
                  {tx(t, `pf.missing.${m.code}`, m.message)} <code className="error-code">{m.code}</code>
                </span>
                {a.kind === 'add' && (
                  <button type="button" className="btn sm" disabled={busy} onClick={() => onAdd(a.ref)}>
                    <Plus aria-hidden /> {t('pf.add', { label: a.label || a.ref.kind })}
                  </button>
                )}
                {a.kind === 'files' && (
                  <Link className="btn sm ghost" to={paths.resources('files', projectId ?? undefined)}>
                    <FolderOpen aria-hidden /> {t('as.attach.addFromFiles')}
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {blocksSend(preflight) && <span className="faint small">{t('pf.held')}</span>}
    </div>
  );
}
