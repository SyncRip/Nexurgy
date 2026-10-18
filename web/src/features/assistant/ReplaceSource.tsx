// Generation supplement I (A-04 / IN-08): "Replace the file and continue" on a failed assembly request whose spec step could not read a
// source. The person picks among the files referenced in this conversation (the composer's references — added there
// from the project files as usual); the request continues in its own run with them (the unread files dropped, a new
// spec version). The server re-checks everything; the same files again while it runs change nothing.

import { FolderOpen, Paperclip, StepForward } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { continueAiOperation, type AiOperation, type AiRef } from '../../api/assistant';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';
import { AsError } from './common';
import { replaceCandidates } from './revise';

export function ReplaceSource({
  op,
  composerRefs,
  projectId,
  disabled,
  onDone,
}: {
  op: AiOperation;
  composerRefs: readonly AiRef[];
  projectId: UUID | null;
  disabled: boolean;
  onDone: () => void;
}) {
  const { t } = useI18n();
  const candidates = replaceCandidates(op, composerRefs);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(candidates.length === 1 ? [candidates[0].object_id] : []));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await continueAiOperation(op.id, { fileVersionIds: [...picked] });
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const toggle = (id: string) =>
    setPicked((p) => {
      const next = new Set(p);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <div className="callout slim as-attach" role="group" aria-label={t('rv.replace.title')}>
      <Paperclip aria-hidden />
      <div className="grow stack-sm">
        <div className="strong">{t('rv.replace.title')}</div>
        <div className="faint small">{t('rv.replace.hint')}</div>
        {candidates.length > 0 ? (
          <>
            <div className="small">{t('rv.replace.pick')}</div>
            <ul className="as-plain">
              {candidates.map((r) => (
                <li key={r.key}>
                  <label className="row">
                    <input type="checkbox" checked={picked.has(r.object_id)} onChange={() => toggle(r.object_id)} disabled={disabled || busy} />
                    <span className="ellip">
                      {r.title} {r.version_label ? <span className="faint small">{r.version_label}</span> : null}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <div className="row wrap">
              <button type="button" className="btn sm primary" disabled={disabled || busy || picked.size === 0} onClick={() => void go()}>
                <StepForward aria-hidden /> {busy ? t('rv.replace.going') : t('rv.replace.go')}
              </button>
            </div>
          </>
        ) : (
          <div className="row wrap">
            <span className="faint small grow">{t('rv.replace.none')}</span>
            <Link className="btn sm ghost" to={paths.resources('files', projectId ?? undefined)}>
              <FolderOpen aria-hidden /> {t('as.attach.addFromFiles')}
            </Link>
          </div>
        )}
        <AsError error={error} />
      </div>
    </div>
  );
}
