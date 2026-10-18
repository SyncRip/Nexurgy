// "Start stage review": choose the reviewer, add extra stage deliverables
// (exact asset versions) and a note. The server freezes the accepted
// submissions of the stage's required tasks plus these extras into an
// immutable snapshot and opens a milestone_review request for the reviewer.

import { CircleCheck, Info, Plus, Send, Sparkles, Trash2, TriangleAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../api/client';
import type { AssetVersion, Milestone } from '../../api/pm';
import { createMilestoneReview, getMilestoneReadiness, type MilestoneReview, type Readiness, type SuggestedItem } from '../../api/stages';
import type { UUID } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetTypeIcon, SourceLabels, VersionToken, assetTypeLabel } from '../assets/meta';
import { AssetVersionPicker } from '../tasks/AssetVersionPicker';
import { TkError, useMembers } from '../tasks/common';
import { BlockerList, dyn } from './meta';
import { LimitedTextarea } from '../../components/ui/LimitedText';

interface Extra {
  id: UUID;
  asset_id?: UUID;
  asset_type?: string;
  asset_name?: string | null;
  label?: string;
  labels?: string[];
}

function fromSuggestion(s: SuggestedItem): Extra | null {
  const v = s.version;
  const id = s.asset_version_id ?? v?.id ?? s.id;
  if (!id) return null;
  return { id, asset_id: s.asset_id ?? v?.asset_id, asset_type: s.asset_type ?? v?.asset_type, asset_name: s.asset_name ?? v?.asset_name, label: s.label ?? v?.label, labels: s.labels ?? v?.labels };
}

function fromVersion(v: AssetVersion): Extra {
  return { id: v.id, asset_id: v.asset_id, asset_type: v.asset_type, asset_name: v.asset_name, label: v.label, labels: v.labels };
}

export function ExtraRef({ x }: { x: Extra }) {
  const { t } = useI18n();
  const body = (
    <>
      {x.asset_type && <AssetTypeIcon type={x.asset_type} />}
      <span className="ellip">{x.asset_name || (x.asset_type ? assetTypeLabel(t, x.asset_type) : t('as.unknownAsset'))}</span>
      {x.label && <VersionToken label={x.label} />}
    </>
  );
  return (
    <span className="stg-ref grow">
      {x.asset_id ? (
        <Link to={paths.asset(x.asset_id, x.id)} className="stg-ref">
          {body}
        </Link>
      ) : (
        <span className="stg-ref">
          {body}
        </span>
      )}
      {x.labels && x.labels.length > 0 && <SourceLabels labels={x.labels} emptyDash={false} />}
    </span>
  );
}

export function StageReviewDialog({
  projectId,
  milestone,
  readiness: initialReadiness,
  onClose,
  onCreated,
  onReload,
}: {
  projectId: UUID;
  milestone: Milestone;
  readiness: Readiness | null;
  onClose: () => void;
  onCreated: (r: MilestoneReview) => void;
  onReload: () => void;
}) {
  const { t } = useI18n();
  const me = useUser();
  const members = useMembers(projectId);
  const eligible = useMemo(() => (members.list ?? []).filter((m) => m.role !== 'viewer'), [members.list]);
  const [reviewer, setReviewer] = useState<string>('');
  const [note, setNote] = useState('');
  const [extras, setExtras] = useState<Extra[]>([]);
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState<AssetVersion | null>(null);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const extraKey = extras.map((x) => x.id).join(',');
  // Re-check with the chosen extras: they can satisfy required deliverable types.
  const live = useResource(
    (signal) => getMilestoneReadiness(milestone.id, { signal, extraIds: extraKey ? extraKey.split(',') : [] }),
    [milestone.id, extraKey],
  );
  const readiness: Readiness | null = live.data ?? (extraKey ? null : initialReadiness);
  const reviewerId = reviewer || eligible.find((m) => m.id !== me.id)?.id || '';
  const suggestions = (readiness?.suggested_items ?? []).map(fromSuggestion).filter((x): x is Extra => !!x && !extras.some((e) => e.id === x.id));
  const frozenTasks = (readiness?.tasks ?? []).filter((x) => x.submission_id);
  const add = (x: Extra) => setExtras((xs) => (xs.some((e) => e.id === x.id) ? xs : [...xs, x]));
  const blockers = isApiError(error) ? (error.details as { blockers?: unknown }).blockers : undefined;

  const submit = async () => {
    setTouched(true);
    if (!reviewerId) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await createMilestoneReview(milestone.id, {
          reviewer_id: reviewerId,
          note: note.trim(),
          extra_asset_version_ids: extras.map((x) => x.id),
          expected_version: milestone.version ?? 1,
        }),
      );
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('stg.review.startTitle', { name: milestone.name })}
      subtitle={t('stg.review.startHint')}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !members.list || (!!readiness && !readiness.ready)}>
            <Send /> {busy ? t('common.working') : t('stg.review.submit')}
          </button>
        </>
      }
    >
      <div className="stack">
        {readiness && !readiness.ready && (
          <div className="callout err" role="alert">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('stg.review.notReady')}</span>
              <BlockerList blockers={readiness.blockers} />
            </div>
          </div>
        )}
        {readiness?.ready && (
          <div className="callout ok slim" role="note">
            <CircleCheck aria-hidden />
            <span>{t('stg.readiness.ready')}</span>
          </div>
        )}

        <section className="stack-sm" aria-label={t('stg.review.frozenTasks')}>
          <span className="tk-label">{t('stg.review.frozenTasks')}</span>
          {frozenTasks.length ? (
            <ul className="stg-tasks">
              {frozenTasks.map((x, i) => {
                const id = x.task_id ?? x.id;
                const seq = x.submission_seq ?? x.seq;
                return (
                  <li key={id ?? i}>
                    {id ? (
                      <Link to={paths.task(id)}>
                        <span className="mono">{x.key}</span> {x.title}
                      </Link>
                    ) : (
                      <span>
                        <span className="mono">{x.key}</span> {x.title}
                      </span>
                    )}
                    {typeof seq === 'number' && <span className="faint">{t('stg.review.submissionSeq', { seq })}</span>}
                    {x.required === false && <span className="faint">· {t('tk.crit.optional')}</span>}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="faint small">{readiness ? t('stg.review.noFrozenTasks') : t('stg.review.frozenUnknown')}</p>
          )}
        </section>

        <div className="form-grid">
          <Field label={t('stg.review.reviewer')} hint={t('stg.review.reviewerHint')} error={touched && !reviewerId ? t('stg.review.reviewerRequired') : null}>
            <select className="select" value={reviewerId} onChange={(e) => setReviewer(e.target.value)} disabled={!members.list}>
              {!members.list && <option value="">{t('common.loading')}</option>}
              {members.list && !eligible.length && <option value="">{t('stg.review.noReviewer')}</option>}
              {eligible.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} · {dyn(t, `role.${m.role}`, m.role)}
                  {m.id === me.id ? ` (${t('common.you')})` : ''}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('stg.review.note')} optional className="span-2">
            <LimitedTextarea className="input" rows={3} limit="description" value={note} placeholder={t('stg.review.notePlaceholder')} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </div>

        <fieldset className="tk-fieldset stack-sm">
          <legend className="tk-label">{t('stg.review.extras')}</legend>
          <p className="faint small">{t('stg.review.extrasHint')}</p>
          {extras.length > 0 && (
            <ul className="stg-picked">
              {extras.map((x) => (
                <li key={x.id}>
                  <ExtraRef x={x} />
                  <button type="button" className="btn ghost icon sm" onClick={() => setExtras((xs) => xs.filter((e) => e.id !== x.id))} aria-label={t('stg.review.removeExtra', { label: x.label ?? '' })}>
                    <Trash2 />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {suggestions.length > 0 && (
            <div className="stack-sm">
              <span className="small faint row">
                <Sparkles aria-hidden className="inline-ico" /> {t('stg.review.suggested')}
              </span>
              <div className="stg-suggest">
                {suggestions.map((x) => (
                  <button key={x.id} type="button" className="btn sm" onClick={() => add(x)}>
                    <Plus /> {x.asset_name ?? (x.asset_type ? assetTypeLabel(t, x.asset_type) : '')} {x.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {picking ? (
            <div className="stg-picker-box">
              <AssetVersionPicker projectId={projectId} value={picked} onChange={setPicked} />
              <div className="row">
                <button
                  type="button"
                  className="btn sm primary"
                  disabled={!picked}
                  onClick={() => {
                    if (picked) add(fromVersion(picked));
                    setPicked(null);
                    setPicking(false);
                  }}
                >
                  <Plus /> {t('stg.review.addPicked')}
                </button>
                <button type="button" className="btn sm" onClick={() => setPicking(false)}>
                  {t('common.cancel')}
                </button>
              </div>
            </div>
          ) : (
            <div>
              <button type="button" className="btn sm" onClick={() => setPicking(true)}>
                <Plus /> {t('stg.review.addExtra')}
              </button>
            </div>
          )}
        </fieldset>

        <div className="callout info slim" role="note">
          <Info aria-hidden />
          <span>{t('stg.review.freezeNote')}</span>
        </div>
        <TkError error={error ?? members.error} onReload={onReload} />
        {blockers !== undefined && <BlockerList blockers={blockers} />}
      </div>
    </Dialog>
  );
}
