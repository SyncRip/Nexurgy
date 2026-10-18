import { notifyTodosChanged } from '../todos/events';
// S5 "Confirm assumptions" of one exact assembly revision, consistent with part revisions and drawings: every AI / rule value it
// rests on — its own (relation datums, solver placements), its spec version's and those of the parts / sub-assemblies it
// is made of — in one list. A person confirms one value or all of them (recorded as that person); a value copied from
// the spec is decided on the spec version. Every confirmation goes by the owner-qualified key (ids repeat across owners). The revision is verified only when every check passed and every value is
// confirmed (W0 §2.2); the panel says which checks still have to pass and which inputs are unverified.

import { ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import type { AssemblyRevision } from '../../api/assemblies';
import { confirmUpstreamAssumptions, listAssemblyAssumptions, type UpstreamAssumption } from '../../api/turbo';
import type { UUID } from '../../api/types';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { VerificationChips } from '../turbo/Turbo';
import { UpstreamAssumptionList } from '../turbo/UpstreamConfirm';
import { pendingKeys } from '../turbo/policyModel';
import '../turbo/policy.css';

export function AssemblyAssumptionsPanel({
  rev,
  projectId,
  canConfirm,
  onChanged,
}: {
  rev: AssemblyRevision;
  projectId: UUID;
  canConfirm: boolean;
  onChanged?: () => void;
}) {
  const { t } = useI18n();
  const toast = useToast();
  const res = useResource((signal) => listAssemblyAssumptions(rev.id, { signal }), [rev.id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const data = res.data;
  if (!data && res.error) return <ErrorNotice error={res.error} onRetry={res.reload} />;
  if (!data) return <SkeletonRows rows={2} cols={3} />;
  const v = data.verification;
  const listing = data.upstream;
  const pending = pendingKeys(listing.items);
  const upstreamItems = v.upstream_unverified?.items ?? [];
  if (!listing.items.length && !v.pending_checks?.length && !upstreamItems.length && !v.unverified) return null;

  const act = async (key: string, call: () => Promise<unknown>, n: number) => {
    setBusy(key);
    setError(null);
    try {
      await call();
      notifyTodosChanged();
      toast(t('pol.confirm.done', { n }));
      res.reload();
      onChanged?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const confirmOne = (a: UpstreamAssumption) =>
    void act(a.key, () => confirmUpstreamAssumptions(projectId, { source_type: 'assembly_revision', source_id: rev.id, keys: [a.key] }), 1);
  const confirmAll = () =>
    void act('*', () => confirmUpstreamAssumptions(projectId, { source_type: 'assembly_revision', source_id: rev.id }), pending.length);

  return (
    <section className="panel" aria-label={t('pol.asm.title')}>
      <div className="panel-h row">
        <strong className="grow">{t('pol.asm.title')}</strong>
        <VerificationChips verification={v} />
      </div>
      <div className="panel-b stack-sm">
        {v.unverified ? <p className="tb-note">{t('pol.asm.hint')}</p> : <p className="tb-note">{t('pol.asm.verified')}</p>}
        {v.pending_checks?.length ? <p className="tb-note">{t('pol.asm.pending', { list: v.pending_checks.join(', ') })}</p> : null}
        {upstreamItems.length > 0 && <p className="tb-note">{t('pol.asm.upstream', { n: upstreamItems.length })}</p>}
        <UpstreamAssumptionList listing={listing} busy={busy} onConfirmOne={canConfirm ? confirmOne : undefined} />
        {listing.blocked && <p className="callout slim warn">{t('pol.confirm.blocked')}</p>}
        {canConfirm && pending.length > 1 && (
          <div className="row">
            <button type="button" className="btn primary sm" disabled={!!busy} onClick={confirmAll}>
              <ShieldCheck /> {busy === '*' ? t('pol.confirm.running') : t('pol.confirm.all', { n: pending.length })}
            </button>
          </div>
        )}
        <ErrorNotice error={error} />
      </div>
    </section>
  );
}
