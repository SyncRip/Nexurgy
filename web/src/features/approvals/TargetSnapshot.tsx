// What a request asks the reviewer to decide, rendered from the frozen target:
// - engineering: the exact asset version, whether it changed since, and the
//   waiting run step;
// - task acceptance: the frozen criteria, deliverable versions and the check
//   results at submission and (for pending requests) now.

import { CircleCheck,CircleX,Download,ExternalLink,Info,ShieldCheck,TriangleAlert,Workflow } from 'lucide-react';
import { useEffect,useState,type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { ApprovalRequestDetail,CheckProblem,CheckReport,CriterionCheck,FrozenCriterion,FrozenItem,ItemCheck } from '../../api/approvals';
import { getAssetVersion,type AssetVersion } from '../../api/assets';
import { blobUrl,downloadBlob } from '../../api/blobs';
import { isAbortError } from '../../api/client';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice,Skeleton } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n,type TFunction } from '../../i18n/I18nProvider';
import {
AssetTypeBadge,
AssetTypeIcon,
AvailabilityChip,
EngineeringChip,
HashText,
LabelCallouts,
SourceLabels,
VersionToken,
assetTypeLabel,
dynLabel,
runPath,
stageText,
} from '../assets/meta';
import { Facts,OpenInWorkspace,VersionSummary } from '../assets/VersionSummary';
import { ModuleContentLoader,ModuleContentView } from '../modules/ModuleView';

// ─── check problems ─────────────────────────────────────────────────────────

/** Localized text of one check problem; the server's English message stays as tooltip. */
export function problemText(t: TFunction, p: CheckProblem): string {
  const key = `ap.chk.${p.code}`;
  if (!hasKey(key)) return p.message;
  return t(key as MessageKey, {
    label: p.label ?? '',
    stage: stageText(t, p.stage),
    slot: p.slot ?? '',
    sources: (p.source_labels ?? []).join(', ') || '—',
    expected: (p.expected_labels ?? []).join(', ') || '—',
  });
}

export function ProblemList({ problems, tone = 'err' }: { problems: CheckProblem[]; tone?: 'err' | 'warn' }) {
  const { t } = useI18n();
  if (!problems.length) return null;
  return (
    <ul className={`ap-problems ${tone}`}>
      {problems.map((p, i) => (
        <li key={`${p.code}-${i}`} title={p.message}>
          {tone === 'err' ? <CircleX aria-hidden /> : <TriangleAlert aria-hidden />}
          <span>{problemText(t, p)}</span>
          <code className="error-code">{p.code}</code>
        </li>
      ))}
    </ul>
  );
}

function CheckResult({
  label,
  ok,
  blocking = true,
  problems,
  warnings = [],
}: {
  label: string;
  ok: boolean;
  /** false = an optional criterion that is not met (does not block acceptance). */
  blocking?: boolean;
  problems: CheckProblem[];
  warnings?: CheckProblem[];
}) {
  const { t } = useI18n();
  return (
    <div className="ap-check">
      <span className="ap-check-h small faint">{label}</span>
      {ok ? (
        <Chip tone="ok" icon={<CircleCheck aria-hidden />}>
          {t('ap.check.pass')}
        </Chip>
      ) : blocking ? (
        <Chip tone="err" icon={<CircleX aria-hidden />}>
          {t('ap.check.fail')}
        </Chip>
      ) : (
        <Chip tone="warn" icon={<TriangleAlert aria-hidden />}>
          {t('ap.check.notBlocking')}
        </Chip>
      )}
      <ProblemList problems={problems} tone={ok || blocking ? 'err' : 'warn'} />
      <ProblemList problems={warnings} tone="warn" />
    </div>
  );
}

/** Failing criteria and items of a report (409 submission_no_longer_valid details, current checks). */
export function FailingChecks({ report }: { report: CheckReport }) {
  const { t } = useI18n();
  const crits = report.criteria.filter((c) => c.blocking);
  const items = report.items.filter((i) => !i.ok);
  return (
    <div className="stack-sm">
      {report.snapshot_tampered && <p className="small">{t('ap.check.tampered')}</p>}
      {crits.map((c) => (
        <div key={c.criterion_id} className="small">
          <b>
            #{c.seq ?? '?'} {c.description}
          </b>
          <ProblemList problems={c.problems} />
        </div>
      ))}
      {items.map((i) => (
        <div key={i.asset_version_id} className="small">
          <b>
            {assetTypeLabel(t, i.asset_type ?? '')} <span className="mono">{i.label}</span>
          </b>
          <ProblemList problems={i.problems} />
        </div>
      ))}
    </div>
  );
}

// ─── engineering ────────────────────────────────────────────────────────────

const isFullVersion = (v: unknown): v is AssetVersion => !!v && typeof v === 'object' && 'asset_type' in (v as object) && 'target' in (v as object);

export function EngineeringTarget({ detail }: { detail: ApprovalRequestDetail }) {
  const { t } = useI18n();
  const v = detail.target.asset_version;
  const subject = detail.target.subject;
  const node = detail.target.node;
  const stage = stageText(t, detail.stage ?? 'design');
  const pending = detail.status === 'pending';
  // A review opened directly on an asset version (process module), not by a run step.
  const direct = !detail.run_id && !detail.node_run_id;
  const isModule = (isFullVersion(v) && v.asset_type === 'process_module') || !!detail.target.module;

  return (
    <div className="stack">
      <div className="callout info" role="note">
        <Info aria-hidden />
        <span>{t(direct ? (isModule ? 'ap.eng.explainModule' : 'ap.eng.explainDirect') : 'ap.eng.explain', { stage })}</span>
      </div>
      {detail.target.part && detail.target.source_path && <Link to={detail.target.source_path} className="btn sm"><ExternalLink />{detail.target.part.number} · {detail.target.part.name}</Link>}
      {subject && 'changed' in subject && subject.changed && pending && (
        <div className="callout warn" role="alert">
          <TriangleAlert aria-hidden />
          <span>{t(direct ? 'ap.eng.changedDirect' : 'ap.eng.changed')}</span>
        </div>
      )}
      {subject && 'error' in subject && (
        <div className="callout warn" role="alert">
          <TriangleAlert aria-hidden />
          <span>
            {t('ap.eng.subjectError')} <code className="error-code">{subject.error}</code>
          </span>
        </div>
      )}
      {subject && 'approved_now' in subject && subject.approved_now && (
        <div className="callout ok" role="note">
          <ShieldCheck aria-hidden />
          <span>{t('ap.eng.approvedNow', { stage })}</span>
        </div>
      )}

      <section className="panel" aria-labelledby="ap-subject-h">
        <div className="panel-h">
          <h2 id="ap-subject-h">{t('ap.eng.subject')}</h2>
          <span className="grow" />
          {isFullVersion(v) && <OpenInWorkspace version={v} size="sm" />}
        </div>
        <div className="panel-b stack">
          {isFullVersion(v) ? (
            <>
              <div className="ap-version-head">
                <AssetTypeBadge type={v.asset_type} />
                <Link to={paths.asset(v.asset_id, v.id)} className="ap-version-link">
                  <span className="strong">{v.asset_name}</span> <VersionToken label={v.label} />
                </Link>
              </div>
              <LabelCallouts labels={v.labels} />
              <Facts
                rows={[
                  [t('as.fact.availability'), <AvailabilityChip availability={v.availability} />],
                  [t('as.fact.engineering'), <EngineeringChip engineering={v.engineering} />],
                  [t('as.fact.labels'), <SourceLabels labels={v.labels} max={8} />],
                  [t('as.fact.contentHash'), <HashText hash={v.content_hash} chars={16} />],
                ]}
              />
              {isModule ? (
                detail.target.module ? (
                  <ModuleContentView module={detail.target.module} />
                ) : (
                  <ModuleContentLoader versionId={v.id} />
                )
              ) : (
                <VersionSummary version={v} />
              )}
            </>
          ) : (
            <p className="faint small">{t('ap.eng.noVersion')}</p>
          )}
        </div>
      </section>

      {node && (
        <section className="panel" aria-labelledby="ap-node-h">
          <div className="panel-h">
            <Workflow aria-hidden className="faint" />
            <h2 id="ap-node-h">{t('ap.eng.step')}</h2>
            <span className="grow" />
            {detail.run_id && (
              <Link className="btn sm" to={runPath(detail.run_id, node.node_id)}>
                <ExternalLink /> {t('ap.eng.openStep')}
              </Link>
            )}
          </div>
          <div className="panel-b stack-sm">
            <Facts
              rows={[
                [t('ap.eng.run'), <span>{node.run_title || t('as.use.untitledRun')}</span>],
                [t('ap.eng.runStatus'), dynLabel(t, `as.runStatus.${node.run_status}`, node.run_status)],
                [t('ap.eng.stepStatus'), dynLabel(t, `ap.nodeStatus.${node.status}`, node.status)],
                [t('ap.eng.node'), <span className="mono">{node.node_id}</span>],
              ]}
            />
            {node.instructions ? (
              <div>
                <span className="small faint">{t('ap.eng.instructions')}</span>
                <p className="ap-instructions">{node.instructions}</p>
              </div>
            ) : null}
          </div>
        </section>
      )}
    </div>
  );
}

// ─── task acceptance ────────────────────────────────────────────────────────

function RuleChips({ c }: { c: FrozenCriterion }) {
  const { t } = useI18n();
  const spec = c.rule_spec ?? {};
  const types = [...new Set([...(c.allowed_types ?? []), ...(spec.asset_types ?? [])])];
  const chips: ReactNode[] = [];
  chips.push(
    <span key="types" className="tag">
      {types.length ? types.map((x) => assetTypeLabel(t, x)).join(' / ') : t('ap.rule.anyType')}
    </span>,
  );
  if ((spec.min_count ?? 1) > 1) chips.push(<span key="min" className="tag">{t('ap.rule.minCount', { n: spec.min_count ?? 1 })}</span>);
  if (spec.require_approval_stage) chips.push(<span key="stage" className="tag">{t('ap.rule.needsApproval', { stage: stageText(t, spec.require_approval_stage) })}</span>);
  chips.push(<span key="est" className="tag">{spec.allow_estimated ? t('ap.rule.estimatedOk') : t('ap.rule.noEstimated')}</span>);
  chips.push(<span key="sim" className="tag">{spec.allow_simulated ? t('ap.rule.simulatedOk') : t('ap.rule.noSimulated')}</span>);
  chips.push(<span key="unv" className="tag">{spec.allow_unverified ? t('ap.rule.unverifiedOk') : t('ap.rule.noUnverified')}</span>);
  if (spec.consistent_with_slot) chips.push(<span key="slot" className="tag">{t('ap.rule.consistentWith', { slot: spec.consistent_with_slot })}</span>);
  return <span className="tag-list">{chips}</span>;
}

function BaselineSlot({ slot, versionId }: { slot: string; versionId: string }) {
  const { t } = useI18n();
  const res = useResource((signal) => getAssetVersion(versionId, { signal }), [versionId]);
  const v = res.data;
  return (
    <li>
      <span className="mono small">{slot}</span>
      {v ? (
        <Link to={paths.asset(v.asset_id, v.id)} className="ap-version-link">
          <AssetTypeIcon type={v.asset_type} />
          <span className="ellip">{v.asset_name}</span> <VersionToken label={v.label} />
        </Link>
      ) : res.error ? (
        <span className="faint small mono" title={versionId}>
          {t('as.unknownAsset')}
        </span>
      ) : (
        <Skeleton w={120} />
      )}
    </li>
  );
}

function ItemRow({ item, crits, frozen, now }: { item: FrozenItem; crits: Map<string, FrozenCriterion>; frozen?: ItemCheck; now?: ItemCheck | null }) {
  const { t } = useI18n();
  const seqs = item.criterion_ids.map((id) => crits.get(id)?.seq).filter((x): x is number => typeof x === 'number');
  return (
    <li className="ap-item">
      <div className="ap-item-head">
        <AssetTypeBadge type={item.asset_type} withLabel={false} />
        <Link to={paths.asset(item.asset_id, item.asset_version_id)} className="ap-version-link">
          <span className="strong">{assetTypeLabel(t, item.asset_type)}</span> <VersionToken label={item.label} />
        </Link>
        <span className="grow" />
        {item.engineering && (
          <Chip tone={item.engineering.approved ? 'ok' : 'pending'} icon={<ShieldCheck aria-hidden />} title={t('ap.item.engAtSubmission')}>
            {item.engineering.approved ? t('ap.item.engApproved', { stage: stageText(t, item.engineering.stage) }) : t('ap.item.engNotApproved', { stage: stageText(t, item.engineering.stage) })}
          </Chip>
        )}
      </div>
      <div className="row small">
        <SourceLabels labels={item.labels} emptyDash={false} />
        {seqs.length > 0 && <span className="faint">{t('ap.item.forCriteria', { list: seqs.map((s) => `#${s}`).join(', ') })}</span>}
        <span className="faint">{t('ap.item.hash')}</span>
        <HashText hash={item.content_hash} copy={false} />
      </div>
      {item.note && <p className="small muted ap-note">{item.note}</p>}
      {(frozen || now) && (
        <div className="ap-check-row">
          {frozen && <CheckResult label={t('ap.check.atSubmission')} ok={frozen.ok} problems={frozen.problems} warnings={frozen.warnings} />}
          {now && <CheckResult label={t('ap.check.now')} ok={now.ok} problems={now.problems} warnings={now.warnings} />}
        </div>
      )}
    </li>
  );
}

function CriterionRow({ c, items, frozen, now }: { c: FrozenCriterion; items: FrozenItem[]; frozen?: CriterionCheck; now?: CriterionCheck | null }) {
  const { t } = useI18n();
  const evidence = items.filter((i) => i.criterion_ids.includes(c.id));
  return (
    <li className="ap-crit">
      <div className="ap-crit-head">
        <span className="mono faint">#{c.seq}</span>
        <span className="strong grow">{c.description || t('ap.crit.noDescription')}</span>
        <Chip tone={c.required ? 'accent' : 'pending'}>{c.required ? t('ap.crit.required') : t('ap.crit.optional')}</Chip>
        {c.manual_required && <Chip tone="measure">{t('ap.crit.manual')}</Chip>}
      </div>
      <RuleChips c={c} />
      <div className="small">
        <span className="faint">{t('ap.crit.evidence')} </span>
        {evidence.length ? (
          evidence.map((i) => (
            <Link key={i.asset_version_id} to={paths.asset(i.asset_id, i.asset_version_id)} className="ap-evidence">
              <AssetTypeIcon type={i.asset_type} /> <span className="mono">{i.label}</span>
            </Link>
          ))
        ) : (
          <span className="faint">{t('ap.crit.noEvidence')}</span>
        )}
      </div>
      {(frozen || now) && (
        <div className="ap-check-row">
          {frozen && <CheckResult label={t('ap.check.atSubmission')} ok={frozen.ok} blocking={frozen.blocking} problems={frozen.problems} />}
          {now && <CheckResult label={t('ap.check.now')} ok={now.ok} blocking={now.blocking} problems={now.problems} />}
        </div>
      )}
    </li>
  );
}

export function AcceptanceTarget({ detail }: { detail: ApprovalRequestDetail }) {
  const { t, fmt } = useI18n();
  const sub = detail.target.submission;
  const snap = sub?.snapshot;
  if (!sub || !snap) {
    return (
      <div className="callout warn" role="note">
        <TriangleAlert aria-hidden />
        <span>{t('ap.acc.noSnapshot')}</span>
      </div>
    );
  }
  const pending = detail.status === 'pending';
  const now = pending ? detail.target.current_checks : undefined;
  const crits = new Map(snap.criteria.map((c) => [c.id, c]));
  const frozenCrit = new Map(snap.checks.criteria.map((c) => [c.criterion_id, c]));
  const nowCrit = new Map((now?.criteria ?? []).map((c) => [c.criterion_id, c]));
  const frozenItem = new Map(snap.checks.items.map((i) => [i.asset_version_id, i]));
  const nowItem = new Map((now?.items ?? []).map((i) => [i.asset_version_id, i]));
  const baselineItems = Object.entries(snap.baseline?.items ?? {});

  return (
    <div className="stack">
      <div className="callout info" role="note">
        <Info aria-hidden />
        <span>{t('ap.acc.explain')}</span>
      </div>
      {pending && now === null && (
        <div className="callout warn" role="note">
          <TriangleAlert aria-hidden />
          <span>{t('ap.acc.recheckUnavailable')}</span>
        </div>
      )}
      {pending && now && now.ok && (
        <div className="callout ok" role="note">
          <CircleCheck aria-hidden />
          <span>{t('ap.acc.checksPassNow')}</span>
        </div>
      )}
      {pending && now && !now.ok && (
        <div className="callout err" role="alert">
          <CircleX aria-hidden />
          <div className="stack-sm">
            <span>{t('ap.acc.checksFailNow')}</span>
            <FailingChecks report={now} />
          </div>
        </div>
      )}

      <section className="panel" aria-labelledby="ap-task-h">
        <div className="panel-h">
          <h2 id="ap-task-h">{t('ap.acc.task')}</h2>
          <span className="grow" />
          <Link className="btn sm" to={paths.task(snap.task.id)}>
            <ExternalLink /> {t('ap.acc.openTask')}
          </Link>
        </div>
        <div className="panel-b stack-sm">
          <div className="ap-task-title">
            <span className="mono strong">T-{snap.task.number}</span> <span className="strong">{snap.task.title}</span>
          </div>
          {snap.task.objective && <p className="muted ap-note">{snap.task.objective}</p>}
          <Facts
            rows={[
              [t('ap.acc.submission'), <span className="mono">#{sub.seq}</span>],
              [t('ap.acc.submittedBy'), <span>{detail.created_by_name ?? '—'}</span>],
              [t('ap.acc.submittedAt'), fmt.dateTime(sub.submitted_at)],
              [t('ap.acc.submissionStatus'), dynLabel(t, `as.subStatus.${sub.status}`, sub.status)],
              [t('ap.acc.snapshotHash'), <HashText hash={sub.snapshot_hash} chars={16} />],
              [t('ap.acc.policy'), <span className="mono small">{snap.policy_version}</span>],
            ]}
          />
          {sub.note && (
            <div>
              <span className="small faint">{t('ap.acc.note')}</span>
              <p className="ap-instructions">{sub.note}</p>
            </div>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="ap-base-h">
        <div className="panel-h">
          <h2 id="ap-base-h">{t('ap.acc.baseline')}</h2>
          {snap.baseline && <span className="faint small">· {t('ap.acc.baselineSeq', { seq: snap.baseline.seq })}</span>}
        </div>
        <div className="panel-b">
          {snap.baseline && baselineItems.length ? (
            <ul className="ap-baseline">
              {baselineItems.map(([slot, vid]) => (
                <BaselineSlot key={slot} slot={slot} versionId={vid} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('ap.acc.noBaseline')}</p>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="ap-crit-h">
        <div className="panel-h">
          <h2 id="ap-crit-h">{t('ap.acc.criteria')}</h2>
          <span className="faint small">· {fmt.number(snap.criteria.length)}</span>
        </div>
        <div className="panel-b">
          {snap.criteria.length ? (
            <ul className="ap-crits">
              {[...snap.criteria]
                .sort((a, b) => a.seq - b.seq)
                .map((c) => (
                  <CriterionRow key={c.id} c={c} items={snap.items} frozen={frozenCrit.get(c.id)} now={now ? nowCrit.get(c.id) ?? null : undefined} />
                ))}
            </ul>
          ) : (
            <p className="faint small">{t('ap.acc.noCriteria')}</p>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="ap-items-h">
        <div className="panel-h">
          <h2 id="ap-items-h">{t('ap.acc.items')}</h2>
          <span className="faint small">· {fmt.number(snap.items.length)}</span>
        </div>
        <div className="panel-b">
          <ul className="ap-items">
            {snap.items.map((i) => (
              <ItemRow key={i.asset_version_id} item={i} crits={crits} frozen={frozenItem.get(i.asset_version_id)} now={now ? nowItem.get(i.asset_version_id) ?? null : undefined} />
            ))}
          </ul>
        </div>
      </section>
    </div>
  );
}

// ─── collaboration acceptance (UI03): the frozen items, previewed ───────────

export type PreviewKind = 'text' | 'pdf' | 'image' | null;
const TEXT_EXT = /\.(md|markdown|txt|json|csv|log|ya?ml|xml)$/i;
const PDF_EXT = /\.pdf$/i;
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg)$/i;
const TEXT_PREVIEW_MAX = 200_000;

/** What the browser can show inline for a stored file, by its file name (null: download only). */
export function previewKind(name: string | null | undefined): PreviewKind {
  if (!name) return null;
  if (TEXT_EXT.test(name)) return 'text';
  if (PDF_EXT.test(name)) return 'pdf';
  if (IMAGE_EXT.test(name)) return 'image';
  return null;
}

/**
 * Inline preview (text / PDF / image) of one stored blob, with a download button that stays available when the
 * preview fails. `noPreview` replaces the default "cannot be previewed" line for other formats.
 */
export function BlobPreview({ sha, name, noPreview }: { sha: string | null; name: string; noPreview?: ReactNode }) {
  const { t } = useI18n();
  const kind = previewKind(name);
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    if (!sha || !kind) return;
    const ctrl = new AbortController();
    let revoke: (() => void) | null = null;
    setUrl(null);
    setText(null);
    setError(null);
    blobUrl(sha, { signal: ctrl.signal })
      .then(async (h) => {
        if (ctrl.signal.aborted) return h.revoke();
        revoke = h.revoke;
        if (kind === 'text') {
          const body = await h.blob.text();
          if (!ctrl.signal.aborted) setText(body.length > TEXT_PREVIEW_MAX ? `${body.slice(0, TEXT_PREVIEW_MAX)}\n…` : body);
        } else setUrl(h.url);
      })
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted && !isAbortError(e)) setError(e);
      });
    return () => {
      ctrl.abort();
      revoke?.();
    };
  }, [sha, kind]);

  if (!sha) return <p className="faint small">{t('ap.cl.noFile')}</p>;
  const unsupported = noPreview ?? <p className="faint small">{t('ap.cl.noPreview')}</p>;
  return (
    <div className="stack-sm">
      <div className="row">
        <span className="mono small ellip grow" title={name}>
          {name}
        </span>
        <button
          type="button"
          className="btn sm"
          disabled={downloading}
          onClick={async () => {
            setDownloading(true);
            try {
              await downloadBlob(sha, name);
            } catch (e) {
              setError(e);
            } finally {
              setDownloading(false);
            }
          }}
        >
          <Download /> {downloading ? t('common.working') : t('common.download')}
        </button>
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      {!kind ? (
        unsupported
      ) : kind === 'text' ? (
        text === null && !error ? <Skeleton w="100%" h={160} /> : text !== null ? <pre className="cl-preview text">{text}</pre> : null
      ) : url ? (
        kind === 'pdf' ? (
          <object className="cl-preview" data={url} type="application/pdf" aria-label={name}>
            {unsupported}
          </object>
        ) : (
          <img className="cl-preview img" src={url} alt={name} />
        )
      ) : !error ? (
        <Skeleton w="100%" h={220} />
      ) : null}
    </div>
  );
}

/** Inline preview (text / PDF / image) of a document version's stored file, with a download button. */
export function DocumentPreview({ version }: { version: AssetVersion }) {
  const s = version.target.summary as { blob_sha256?: string; original_name?: string };
  return <BlobPreview sha={s.blob_sha256 ?? null} name={s.original_name || `${version.asset_name}-${version.label}`} />;
}

function FrozenItemView({ item }: { item: FrozenItem }) {
  const { t } = useI18n();
  const res = useResource((signal) => getAssetVersion(item.asset_version_id, { signal }), [item.asset_version_id]);
  const v = res.data;
  return (
    <li className="cl-acc-item">
      <div className="ap-item-head">
        <AssetTypeBadge type={item.asset_type} withLabel={false} />
        <Link to={paths.asset(item.asset_id, item.asset_version_id)} className="ap-version-link">
          <span className="strong">{v?.asset_name ?? assetTypeLabel(t, item.asset_type)}</span> <VersionToken label={item.label} />
        </Link>
        <span className="grow" />
        <SourceLabels labels={item.labels} emptyDash={false} />
      </div>
      <div className="row small">
        <span className="faint">{t('ap.item.hash')}</span>
        <HashText hash={item.content_hash} chars={16} />
      </div>
      {item.note && <p className="small muted ap-note">{item.note}</p>}
      {res.error && !v ? (
        <ErrorNotice error={res.error} onRetry={res.reload} />
      ) : !v ? (
        <Skeleton w="100%" h={60} />
      ) : v.content_hash !== item.content_hash ? (
        <div className="callout err slim" role="alert">
          <TriangleAlert aria-hidden />
          <span>{t('ap.cl.contentChanged')}</span>
        </div>
      ) : item.asset_type === 'document' ? (
        <DocumentPreview version={v} />
      ) : (
        <div className="stack-sm">
          <VersionSummary version={v} />
          <div>
            <OpenInWorkspace version={v} size="sm" />
          </div>
        </div>
      )}
    </li>
  );
}

/** Left column of the per-criterion acceptance: the exact frozen items of the submission. */
export function CollaborationSubmission({ detail }: { detail: ApprovalRequestDetail }) {
  const { t } = useI18n();
  const sub = detail.target.submission;
  const snap = sub?.snapshot;
  if (!sub || !snap) {
    return (
      <div className="callout warn" role="note">
        <TriangleAlert aria-hidden />
        <span>{t('ap.acc.noSnapshot')}</span>
      </div>
    );
  }
  return (
    <div className="stack">
      <section className="panel" aria-labelledby="ap-cl-items-h">
        <div className="panel-h">
          <h2 id="ap-cl-items-h">{t('ap.cl.items')}</h2>
          <span className="faint small">· {snap.items.length}</span>
          <span className="grow" />
          <Link className="btn sm" to={paths.task(snap.task.id)}>
            <ExternalLink /> {t('ap.acc.openTask')}
          </Link>
        </div>
        <div className="panel-b stack-sm">
          <div className="ap-task-title">
            <span className="mono strong">T-{snap.task.number}</span> <span className="strong">{snap.task.title}</span>
          </div>
          {snap.task.objective && <p className="muted ap-note">{snap.task.objective}</p>}
          {sub.note && <p className="ap-instructions">{sub.note}</p>}
          {snap.items.length ? (
            <ul className="cl-acc-items">
              {snap.items.map((i) => (
                <FrozenItemView key={i.asset_version_id} item={i} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('ap.cl.noItems')}</p>
          )}
        </div>
      </section>
    </div>
  );
}
