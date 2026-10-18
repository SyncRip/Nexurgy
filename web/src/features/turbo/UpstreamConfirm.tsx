import { notifyTodosChanged } from '../todos/events';
// S5 "Confirm these assumptions": every AI / rule assumption a product rests on (its own and those of the spec version, assembly
// revisions and part revisions upstream of it) in one list, confirmed by a person in one go. Each confirmation is an
// append-only record with that person as the confirmer (never the AI). A wrong value is not changed here: it is an
// engineering change made as a new version. Server: app/api/execution_policy.py, services/verification.py.

import './policy.css';
import { CheckCheck, Info } from 'lucide-react';
import { useState } from 'react';
import { confirmUpstreamAssumptions, listUpstreamAssumptions, type PolicySourceType, type UpstreamAssumption, type UpstreamAssumptions } from '../../api/turbo';
import type { UUID } from '../../api/types';
import { Chip, type Tone } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { MessageKey } from '../../i18n/en';
import { decisionLine, decisionView } from './decisions';
import { formatAdopted } from './model';
import { groupByOwner, pendingKeys } from './policyModel';

export type UpstreamSource = { source_type: PolicySourceType; source_id: UUID };

const STATE_TONE: Record<UpstreamAssumption['state'], Tone> = { unconfirmed: 'warn', confirmed: 'ok', corrected: 'err' };
const OWNER_KEYS = new Set(['engineering_spec_version', 'assembly_revision', 'model_revision']);

/** The list, grouped by the version that carries each value (presentational; no requests). ``onConfirmOne`` adds a
 * per-value button; ``busy`` disables them while a request runs. */
export function UpstreamAssumptionList({
  listing,
  busy,
  onConfirmOne,
}: {
  listing: Pick<UpstreamAssumptions, 'items' | 'counts'>;
  busy?: string | null;
  onConfirmOne?: (item: UpstreamAssumption) => void;
}) {
  const { t } = useI18n();
  if (!listing.items.length) return <p className="faint small">{t('pol.confirm.none')}</p>;
  return (
    <div className="stack-sm pol-upstream">
      <span className="faint small" data-testid="pol-counts">
        {t('pol.confirm.counts', { pending: listing.counts.unconfirmed, confirmed: listing.counts.confirmed, corrected: listing.counts.corrected })}
      </span>
      {groupByOwner(listing.items).map((g) => (
        <section key={`${g.owner.type}:${g.owner.id}`} className="pol-owner" aria-label={g.owner.label}>
          <span className="tk-label">
            {t((OWNER_KEYS.has(g.owner.type) ? `pol.confirm.owner.${g.owner.type}` : 'pol.confirm.owner.other') as MessageKey, { label: g.owner.label })}
          </span>
          <ul className="tb-assumptions">
            {g.items.map((a, i) => {
              const view = decisionView(a, i);
              return (
                <li key={a.key} tabIndex={-1} data-assumption-id={a.id} data-state={a.state} data-problems={view.problems.length || undefined}>
                  <span className="tb-assumption-head">
                    <span className="tb-assumption-subject">{view.subject}</span>
                    <Chip tone={view.trigger ? 'accent' : 'pending'} className="sm">
                      {t(`tb.decision.trigger.${view.trigger ?? 'unstated'}`)}
                    </Chip>
                    {a.author && (
                      <Chip tone="measure" className="sm">
                        {t(`tb.assumptions.author.${a.author}`)}
                      </Chip>
                    )}
                    <Chip tone={STATE_TONE[a.state]} className="sm">
                      {t(`pol.confirm.state.${a.state}`)}
                    </Chip>
                    {onConfirmOne && a.state === 'unconfirmed' && (
                      <button type="button" className="btn sm" disabled={!!busy} onClick={() => onConfirmOne(a)}>
                        <CheckCheck /> {busy === a.key ? t('pol.confirm.running') : t('pol.confirm.one')}
                      </button>
                    )}
                  </span>
                  {/* generation supplement D: the decision in one line, with what is wrong with it */}
                  <span className="tb-decision-line">{decisionLine(view, t)}</span>
                  {view.sources.length ? <span className="tb-assumption-basis">{t('tb.decision.sources', { list: view.sources.join('；') })}</span> : null}
                  {view.problems.length ? (
                    <span className="tb-chips">
                      {view.problems.map((p) => (
                        <Chip key={p} tone="err" className="sm">
                          {t(`tb.decision.problem.${p}`)}
                        </Chip>
                      ))}
                    </span>
                  ) : null}
                  {a.state === 'confirmed' && a.by && <span className="tb-assumption-basis">{t('pol.confirm.by', { name: a.by.name })}</span>}
                  {a.state === 'corrected' && (
                    <span className="tb-assumption-basis">
                      {t('pol.confirm.correctedTo', { value: formatAdopted({ adopted: a.value, unit: a.unit ?? null }) ?? '—' })}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** Load the list for one product and confirm it (all, or one value) as the signed-in person. */
export function useUpstreamConfirm(projectId: UUID, source: UpstreamSource) {
  const res = useResource((signal) => listUpstreamAssumptions(projectId, source, { signal }), [projectId, source.source_type, source.source_id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const confirm = async (keys?: string[]): Promise<UpstreamAssumptions | null> => {
    setBusy(keys?.length === 1 ? keys[0] : '*');
    setError(null);
    try {
      const out = await confirmUpstreamAssumptions(projectId, { ...source, ...(keys ? { keys } : {}) });
      res.setData(out);
      notifyTodosChanged();
      return out;
    } catch (e) {
      setError(e);
      return null;
    } finally {
      setBusy(null);
    }
  };
  return { res, busy, error, confirm };
}

/** Inline entry (node detail, assembly page): the list, "confirm all" and the retry hint. */
export function UpstreamConfirmPanel({
  projectId,
  source,
  onConfirmed,
  hint,
}: {
  projectId: UUID;
  source: UpstreamSource;
  onConfirmed?: (out: UpstreamAssumptions) => void;
  hint?: string;
}) {
  const { t } = useI18n();
  const { res, busy, error, confirm } = useUpstreamConfirm(projectId, source);
  const listing = res.data;
  const pending = listing ? pendingKeys(listing.items).length : 0;
  const done = async (keys?: string[]) => {
    const out = await confirm(keys);
    if (out) onConfirmed?.(out);
  };
  return (
    <section className="stack-sm pol-confirm" aria-label={t('pol.confirm.title')}>
      <p className="tb-note">
        <Info aria-hidden /> {t('pol.confirm.hint')}
      </p>
      {res.error && !listing ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {!listing && !res.error ? <SkeletonRows rows={3} cols={3} /> : null}
      {listing && <UpstreamAssumptionList listing={listing} busy={busy} onConfirmOne={(a) => void done([a.key])} />}
      {listing?.blocked && <p className="callout slim warn">{t('pol.confirm.blocked')}</p>}
      {listing && pending > 0 && (
        <div className="row">
          <button type="button" className="btn primary sm" disabled={!!busy} onClick={() => void done()}>
            <CheckCheck /> {busy === '*' ? t('pol.confirm.running') : t('pol.confirm.all', { n: pending })}
          </button>
          {hint && <span className="faint small">{hint}</span>}
        </div>
      )}
      <ErrorNotice error={error} />
    </section>
  );
}

/** The same entry as a dialog (policy fields, error callouts). */
export function UpstreamConfirmDialog({
  projectId,
  source,
  onClose,
  onConfirmed,
}: {
  projectId: UUID;
  source: UpstreamSource;
  onClose: () => void;
  onConfirmed?: (out: UpstreamAssumptions) => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog
      title={t('pol.confirm.title')}
      onClose={onClose}
      size="lg"
      footer={
        <button type="button" className="btn" onClick={onClose}>
          {t('common.close')}
        </button>
      }
    >
      <UpstreamConfirmPanel projectId={projectId} source={source} onConfirmed={onConfirmed} />
    </Dialog>
  );
}
