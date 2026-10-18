// S5: the execution policy of a request and where it comes from — explicit request > inherited from the version it
// continues from > project default > standard (design §3.4). The field shows the policy the request will be frozen
// with and its source before anything is sent; picking the other policy explicitly explains what that means (e.g.
// "standard will require confirming the N upstream assumptions") and opens the "confirm these assumptions" entry.

import './policy.css';
import { CheckCheck, Info, Link2, Undo2 } from 'lucide-react';
import { useState } from 'react';
import { useFeature } from '../../api/capabilities';
import type { ExecutionPolicy } from '../../api/engineering';
import { getPolicyResolution, type PolicyResolution, type PolicySource, type PolicySourceType, type SourceVersion } from '../../api/turbo';
import type { UUID } from '../../api/types';
import { PolicyChip } from '../../components/engineering/EngineeringChips';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { effectivePolicy, policyConsequence, type ResolvedPolicy } from './policyModel';
import { PolicySelector, PolicyToggle } from './Turbo';
import { UpstreamConfirmDialog, type UpstreamSource } from './UpstreamConfirm';

/** The policy, its source and the consequence of an explicit switch (presentational). */
export function PolicySourceNote({
  resolved,
  sourceVersion,
  upstream,
  onConfirm,
  onFollow,
}: {
  resolved: ResolvedPolicy;
  sourceVersion?: SourceVersion | null;
  upstream?: PolicyResolution['upstream'];
  /** Opens the "confirm these assumptions" entry when the consequence asks for it. */
  onConfirm?: () => void;
  /** Drops the explicit choice (back to the inherited / default policy). */
  onFollow?: () => void;
}) {
  const { t } = useI18n();
  const consequence = policyConsequence(resolved.policy, sourceVersion, upstream);
  const policyName = (p: ExecutionPolicy) => t(`me.policy.${p}`);
  let source: string;
  if (resolved.source === 'inherited') source = t('pol.source.inherited', { label: sourceVersion?.label || '—' });
  else if (resolved.source === 'explicit')
    source = sourceVersion
      ? `${t('pol.source.explicit')} · ${t('pol.source.of', { label: sourceVersion.label, policy: policyName(sourceVersion.execution_policy) })}`
      : t('pol.source.explicit');
  else source = t(`pol.source.${resolved.source}`);
  const canConfirm = !!onConfirm && !!consequence && (consequence.code === 'confirm_required' || consequence.code === 'confirm_upstream');
  return (
    <div className="pol-note" data-source={resolved.source}>
      <span className="pol-note-line">
        <Link2 aria-hidden />
        <span className="faint">{t('pol.source.label')}</span>
        <PolicyChip policy={resolved.policy} />
        <span className="pol-note-source">{source}</span>
        {onFollow && resolved.source === 'explicit' && sourceVersion && (
          <button type="button" className="btn ghost sm" onClick={onFollow}>
            <Undo2 /> {t('pol.follow')}
          </button>
        )}
      </span>
      {consequence && (
        <span className={`pol-cons ${consequence.code}`}>
          <Info aria-hidden />
          <span className="grow">{t(`pol.cons.${consequence.code}`, 'n' in consequence ? { n: consequence.n } : {})}</span>
          {canConfirm && (
            <button type="button" className="btn sm" onClick={onConfirm}>
              <CheckCheck /> {t('pol.confirm.open')}
            </button>
          )}
        </span>
      )}
    </div>
  );
}

/** Next to a run's / operation's policy chip: why it got that policy when the person did not pick it (inherited from
 * a version, or the project default). Nothing for an explicit choice or the plain default. */
export function PolicySourceTag({ source, sourceVersion }: { source?: PolicySource | null; sourceVersion?: SourceVersion | null }) {
  const { t } = useI18n();
  if (source !== 'inherited' && source !== 'project_default') return null;
  const full = source === 'inherited' ? t('pol.source.inherited', { label: sourceVersion?.label || '—' }) : t('pol.source.project_default');
  return (
    <span className="pol-tag faint small" title={full}>
      <Link2 aria-hidden /> {t(`pol.source.short.${source}`)}
      {source === 'inherited' && sourceVersion?.label ? ` · ${sourceVersion.label}` : ''}
    </span>
  );
}

/** The version a request continues from, as the preview takes it (``part``: the part's latest revision; ``run``: the
 * run a rerun repeats). */
export type PolicyPreviewSource = { source_type: PolicySourceType | 'part' | 'run'; source_id: UUID };

const UPSTREAM_TYPES = new Set<string>(['engineering_spec_version', 'assembly_revision', 'model_revision']);

/** The product whose upstream assumptions the "confirm" entry lists: the source version the preview resolved. */
export function confirmSourceFor(preview: Pick<PolicyResolution, 'source_version'> | null | undefined): UpstreamSource | null {
  const v = preview?.source_version;
  return v && UPSTREAM_TYPES.has(v.type) ? { source_type: v.type as PolicySourceType, source_id: v.id } : null;
}

/** What a request continuing from ``source`` would be frozen with (no explicit choice), from the server. */
export function usePolicyPreview(projectId: UUID | null | undefined, source: PolicyPreviewSource | null | undefined) {
  const res = useResource(
    (signal) => getPolicyResolution(projectId!, source ? { source_type: source.source_type, source_id: source.source_id } : {}, { signal }),
    [projectId, source?.source_type, source?.source_id],
    { enabled: !!projectId },
  );
  return { preview: res.data && !res.error ? res.data : null, reload: res.reload };
}

/** Policy choice of a request with its source. ``value`` is the person's explicit choice (null = follow the source /
 * project default); the field fetches what the request would be frozen with. Without a project it falls back to the
 * client mirror (standard unless chosen). A click is always an explicit choice sent with the request ("follow the
 * source" drops it), so what the person picked is what gets frozen even if the preview missed the real source. */
export function PolicyField({
  projectId,
  source,
  value,
  onChange,
  disabled,
  variant = 'toggle',
  note,
}: {
  projectId: UUID | null | undefined;
  source?: PolicyPreviewSource | null;
  value: ExecutionPolicy | null;
  onChange: (p: ExecutionPolicy | null) => void;
  disabled?: boolean;
  variant?: 'toggle' | 'cards';
  note?: string | null;
}) {
  const policyEnabled = useFeature('turbo_choice');
  const { preview, reload } = usePolicyPreview(projectId, source);
  const [confirming, setConfirming] = useState(false);
  const resolved = effectivePolicy(policyEnabled ? value : null, preview);
  const confirmSource = confirmSourceFor(preview);
  return (
    <div className="stack-sm pol-field">
      {variant === 'cards' ? (
        <PolicySelector value={resolved.policy} onChange={onChange} disabled={disabled} note={note} />
      ) : (
        <PolicyToggle value={resolved.policy} onChange={onChange} disabled={disabled} />
      )}
      <PolicySourceNote
        resolved={resolved}
        sourceVersion={preview?.source_version ?? null}
        upstream={preview?.upstream ?? null}
        onConfirm={projectId && confirmSource ? () => setConfirming(true) : undefined}
        onFollow={policyEnabled && value ? () => onChange(null) : undefined}
      />
      {confirming && projectId && confirmSource && (
        <UpstreamConfirmDialog projectId={projectId} source={confirmSource} onClose={() => setConfirming(false)} onConfirmed={() => reload()} />
      )}
    </div>
  );
}
