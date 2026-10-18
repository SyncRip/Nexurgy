// Provenance and use of one asset version: what it rests on, what rests on it,
// and where tasks, runs, approval requests, process resources, knowledge items
// and knowledge usages reference this exact version. The three collaboration
// groups (C11) page independently through `relation=`; each row states use
// kind, publish state, this task's acceptance and knowledge eligibility
// separately, so a historical approval never reads as "accepted now".

import { ArrowDown, ArrowRight, BookOpen, ClipboardCheck, Flag, GitCommitVertical, Link2, ListChecks, PackageCheck, PlayCircle, Send, Waypoints } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { USAGE_GROUP_RELATION, getAssetVersionUsages, type AssetEdge, type AssetUsages, type AssetVersion, type UsageGroup } from '../../api/assets';
import type { KnowledgeItemRef, KnowledgeUsage } from '../../api/knowledge';
import type { WorkflowResourceItem } from '../../api/resources';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { RequestStatusChip, requestKindText } from '../approvals/meta';
import { AcceptanceChip, ActorName, ConfirmationChip, ExecutionModeChip, ResourceKindChip, ReusableChip, TaskLink, UsageKindChip, runPageOf } from './collabMeta';
import { AssetTypeIcon, PublishChip, VersionToken, assetTypeLabel, dynLabel, runPath } from './meta';
import { Pager } from './Pager';

// ─── dependency graph ───────────────────────────────────────────────────────

function EdgeItem({ edge }: { edge: AssetEdge }) {
  const { t } = useI18n();
  const name = edge.asset_name || (edge.asset_type ? assetTypeLabel(t, edge.asset_type) : t('as.unknownAsset'));
  const inner = (
    <>
      {edge.asset_type && <AssetTypeIcon type={edge.asset_type} />}
      <span className="as-edge-name">{name}</span>
      {edge.label && <VersionToken label={edge.label} />}
    </>
  );
  return (
    <li className="as-edge">
      <span className="as-rel small faint">{dynLabel(t, `as.rel.${edge.relation}`, edge.relation)}</span>
      {edge.asset_id ? (
        <Link className="as-edge-link" to={paths.asset(edge.asset_id, edge.version_id)}>
          {inner}
        </Link>
      ) : (
        <span className="as-edge-link is-missing">{inner}</span>
      )}
    </li>
  );
}

/**
 * Upstream (what this version rests on, plus the previous revision it was
 * edited from), this version, downstream (what was made from it).
 * `parent` is the parent version of the same asset, resolved by the caller.
 */
export function DependencyGraph({ version, parent }: { version: AssetVersion; parent?: Pick<AssetVersion, 'id' | 'label' | 'asset_id' | 'asset_type' | 'asset_name' | 'version_no'> | null }) {
  const { t } = useI18n();
  const parentEdge: AssetEdge[] =
    version.parent_version_id && !(version.dependencies ?? []).some((e) => e.version_id === version.parent_version_id)
      ? [
          parent
            ? { version_id: parent.id, relation: 'parent_revision', asset_id: parent.asset_id, asset_type: parent.asset_type, asset_name: parent.asset_name, version_no: parent.version_no, label: parent.label }
            : { version_id: version.parent_version_id, relation: 'parent_revision', asset_id: version.asset_id, asset_type: version.asset_type, asset_name: version.asset_name },
        ]
      : [];
  const up = [...(version.dependencies ?? []), ...parentEdge];
  const down = version.dependents ?? [];
  return (
    <section className="panel" aria-labelledby="as-deps-h">
      <div className="panel-h">
        <GitCommitVertical aria-hidden className="faint" />
        <h2 id="as-deps-h">{t('as.deps.title')}</h2>
      </div>
      <div className="panel-b as-graph">
        <div className="as-graph-col">
          <h3 className="as-graph-h">{t('as.deps.upstream')}</h3>
          {up.length ? (
            <ul className="as-edges">
              {up.map((e) => (
                <EdgeItem key={`${e.version_id}-${e.relation}`} edge={e} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('as.deps.noUpstream')}</p>
          )}
        </div>
        <div className="as-graph-arrow" aria-hidden>
          <ArrowRight className="hide-sm" />
          <ArrowDown className="only-sm" />
        </div>
        <div className="as-graph-col as-graph-self">
          <h3 className="as-graph-h">{t('as.deps.this')}</h3>
          <div className="as-edge-link is-self">
            <AssetTypeIcon type={version.asset_type} />
            <span className="as-edge-name">{version.asset_name}</span>
            <VersionToken label={version.label} />
          </div>
        </div>
        <div className="as-graph-arrow" aria-hidden>
          <ArrowRight className="hide-sm" />
          <ArrowDown className="only-sm" />
        </div>
        <div className="as-graph-col">
          <h3 className="as-graph-h">{t('as.deps.downstream')}</h3>
          {down.length ? (
            <ul className="as-edges">
              {down.map((e) => (
                <EdgeItem key={`${e.version_id}-${e.relation}`} edge={e} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('as.deps.noDownstream')}</p>
          )}
        </div>
      </div>
    </section>
  );
}

// ─── usages ────────────────────────────────────────────────────────────────

function UseGroup({ icon, title, count, children, footer }: { icon: ReactNode; title: string; count: number; children: ReactNode; footer?: ReactNode }) {
  if (!count) return null;
  return (
    <div className="as-use-group">
      <h3 className="as-use-h">
        {icon}
        {title}
        <span className="faint"> · {count}</span>
      </h3>
      <ul className="as-uses">{children}</ul>
      {footer}
    </div>
  );
}

/** Labelled value inside a relation row ("Purpose · accepted"), so the four judgements never merge. */
function Kv({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="as-kv">
      <span className="as-kv-k">{label}</span>
      {children}
    </span>
  );
}

function ResourceRow({ r }: { r: WorkflowResourceItem }) {
  const { t, fmt } = useI18n();
  return (
    <li className="as-use-row">
      <span className="as-use-main">
        <Link to={runPageOf(r.run_id, r.node_id, r.execution_mode)} className="as-use-link">
          <PlayCircle aria-hidden className="inline-ico" /> <span className="ellip">{r.run_title || t('as.use.untitledRun')}</span>
        </Link>
        {r.task_id && <TaskLink id={r.task_id} number={r.task_number} title={r.task_title} />}
        {r.slot_key && <span className="small faint mono">{r.slot_key}</span>}
        <ExecutionModeChip mode={r.execution_mode} />
      </span>
      <span className="as-use-states">
        <Kv label={t('as.use.k.kind')}>
          <ResourceKindChip kind={r.kind} />
        </Kv>
        <Kv label={t('as.use.k.publish')}>
          <PublishChip state={r.asset.publish_state} />
        </Kv>
        <Kv label={t('as.use.k.acceptance')}>
          <AcceptanceChip acceptance={r.task_acceptance} kind={r.kind} />
        </Kv>
        {r.created_at && (
          <span className="small faint nowrap" title={fmt.dateTime(r.created_at)}>
            {fmt.relative(r.created_at)}
          </span>
        )}
      </span>
    </li>
  );
}

function KnowledgeItemRow({ k, projectId }: { k: KnowledgeItemRef; projectId: string }) {
  const { t } = useI18n();
  return (
    <li className="as-use-row">
      <span className="as-use-main">
        <Link to={paths.knowledge(projectId, k.collection_id, k.collection_version_id)} className="as-use-link">
          <BookOpen aria-hidden className="inline-ico" /> <span className="ellip">{k.collection_title || t('rs.detail.collection')}</span>
        </Link>
        <Link to={runPath(k.run_id)} className="as-use-link small">
          <PlayCircle aria-hidden className="inline-ico" /> {t('as.use.sourceRun')}
        </Link>
        {k.task_id && (
          <Link to={paths.task(k.task_id)} className="as-use-link small">
            {t('as.use.sourceTask')}
          </Link>
        )}
      </span>
      <span className="as-use-states">
        {k.kind && (
          <Kv label={t('as.use.k.itemKind')}>
            <Chip tone={k.kind === 'formal' ? 'ok' : 'stale'}>{dynLabel(t, `kb.itemKind.${k.kind}`, k.kind)}</Chip>
          </Kv>
        )}
        <Kv label={t('as.use.k.eligibility')}>
          <ReusableChip reusable={k.reusable} />
        </Kv>
      </span>
    </li>
  );
}

function KnowledgeUsageRow({ u }: { u: KnowledgeUsage }) {
  const { t, fmt } = useI18n();
  return (
    <li className="as-use-row">
      <span className="as-use-main">
        <UsageKindChip usage={u.usage} />
        {u.target_task_id ? (
          <TaskLink id={u.target_task_id} number={u.target_task?.number ?? null} title={u.target_task?.title ?? t('as.use.targetTask')} />
        ) : u.target_run_id ? (
          <Link to={runPath(u.target_run_id)} className="as-use-link">
            <PlayCircle aria-hidden className="inline-ico" /> {t('as.use.targetRun')}
          </Link>
        ) : (
          <span className="faint small">{t('as.use.targetProject')}</span>
        )}
        {u.slot_key && <span className="small faint mono">{u.slot_key}</span>}
      </span>
      <span className="as-use-states">
        <Kv label={t('as.use.k.usedBy')}>
          <ActorName actor={u.used_by} />
        </Kv>
        <Kv label={t('as.use.k.confirmation')}>
          <ConfirmationChip confirmation={u.confirmation} />
        </Kv>
        {u.reason && (
          <span className="small muted ellip as-use-reason" title={u.reason}>
            {u.reason}
          </span>
        )}
        {u.created_at && (
          <span className="small faint nowrap" title={fmt.dateTime(u.created_at)}>
            {fmt.relative(u.created_at)}
          </span>
        )}
      </span>
    </li>
  );
}

const PAGE = 20;

/** One paged C11 group: page 0 comes from the shared `relation=all` load; later pages fetch only that relation. */
function usePagedGroup(versionId: string, group: UsageGroup, base: AssetUsages | undefined) {
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [versionId]);
  const page = useResource(
    (signal) => getAssetVersionUsages(versionId, { relation: USAGE_GROUP_RELATION[group], limit: PAGE, offset }, { signal }),
    [versionId, group, offset],
    { enabled: offset > 0 },
  );
  const src = offset > 0 ? page.data : base;
  const rows = (src?.[group] ?? []) as AssetUsages[typeof group];
  const total = base?.pagination?.[group]?.total ?? (base?.[group]?.length ?? 0);
  return { rows, total, offset, setOffset, loading: offset > 0 && !page.data, error: offset > 0 ? page.error : null, reload: page.reload };
}

export function UsagesPanel({ version }: { version: Pick<AssetVersion, 'id' | 'project_id'> }) {
  const { t, fmt } = useI18n();
  const versionId = version.id;
  const res = useResource((signal) => getAssetVersionUsages(versionId, { relation: 'all', limit: PAGE }, { signal }), [versionId]);
  const u = res.data;
  const wr = usePagedGroup(versionId, 'workflow_resources', u);
  const ki = usePagedGroup(versionId, 'knowledge_items', u);
  const ku = usePagedGroup(versionId, 'knowledge_usages', u);
  const legacyTotal = u ? u.baselines.length + u.deliverables.length + u.submissions.length + u.run_inputs.length + u.run_outputs.length + u.approval_requests.length : 0;
  const total = legacyTotal + wr.total + ki.total + ku.total;
  const status = (prefix: string, s: string) => <span className="faint small nowrap">{dynLabel(t, `${prefix}.${s}`, s)}</span>;

  const pagedFooter = (g: ReturnType<typeof usePagedGroup>) => (
    <>
      {g.error && <ErrorNotice error={g.error} onRetry={g.reload} />}
      {g.loading && <Skeleton w="50%" />}
      {g.total > PAGE && <Pager total={g.total} limit={PAGE} offset={g.offset} onOffset={g.setOffset} />}
    </>
  );

  let body: ReactNode;
  if (res.error && !u) body = <ErrorNotice error={res.error} onRetry={res.reload} />;
  else if (!u)
    body = (
      <div className="stack-sm" role="status" aria-label={t('common.loading')}>
        <Skeleton w="60%" />
        <Skeleton w="40%" />
      </div>
    );
  else if (!total) body = <p className="faint small">{t('as.use.none')}</p>;
  else
    body = (
      <div className="as-use-grid">
        <UseGroup icon={<Waypoints aria-hidden />} title={t('as.use.workflowResources')} count={wr.total} footer={pagedFooter(wr)}>
          {(wr.rows as WorkflowResourceItem[]).map((r) => (
            <ResourceRow key={r.id} r={r} />
          ))}
        </UseGroup>
        <UseGroup icon={<BookOpen aria-hidden />} title={t('as.use.knowledgeItems')} count={ki.total} footer={pagedFooter(ki)}>
          {(ki.rows as KnowledgeItemRef[]).map((k) => (
            <KnowledgeItemRow key={k.item_id} k={k} projectId={version.project_id} />
          ))}
        </UseGroup>
        <UseGroup icon={<Link2 aria-hidden />} title={t('as.use.knowledgeUsages')} count={ku.total} footer={pagedFooter(ku)}>
          {(ku.rows as KnowledgeUsage[]).map((x) => (
            <KnowledgeUsageRow key={x.id} u={x} />
          ))}
        </UseGroup>
        <UseGroup icon={<Flag aria-hidden />} title={t('as.use.baselines')} count={u.baselines.length}>
          {u.baselines.map((b) => (
            <li key={`${b.snapshot_id}-${b.slot_key}`}>
              <TaskLink id={b.task_id} number={b.task_number} title={b.task_title} />
              <span className="small faint">{t('as.use.slot', { slot: b.slot_key, seq: b.snapshot_seq })}</span>
              {b.current ? <Chip tone="ok">{t('as.use.currentBaseline')}</Chip> : <Chip tone="pending">{t('as.use.earlierBaseline')}</Chip>}
            </li>
          ))}
        </UseGroup>
        <UseGroup icon={<ListChecks aria-hidden />} title={t('as.use.deliverables')} count={u.deliverables.length}>
          {u.deliverables.map((d) => (
            <li key={d.id}>
              <TaskLink id={d.task_id} number={d.task_number} title={d.task_title} />
              {status('as.taskStatus', d.task_status)}
              {d.note && <span className="small muted ellip">{d.note}</span>}
            </li>
          ))}
        </UseGroup>
        <UseGroup icon={<Send aria-hidden />} title={t('as.use.submissions')} count={u.submissions.length}>
          {u.submissions.map((s) => (
            <li key={s.item_id}>
              <TaskLink id={s.task_id} number={s.task_number} title={s.task_title} />
              <span className="small faint nowrap">{t('as.use.submissionSeq', { seq: s.seq })}</span>
              {status('as.subStatus', s.status)}
              <span className="small faint nowrap" title={fmt.dateTime(s.submitted_at)}>
                {fmt.relative(s.submitted_at)}
              </span>
            </li>
          ))}
        </UseGroup>
        <UseGroup icon={<PlayCircle aria-hidden />} title={t('as.use.runInputs')} count={u.run_inputs.length}>
          {u.run_inputs.map((r) => (
            <li key={`${r.run_id}-${r.port_key}`}>
              <Link to={runPath(r.run_id, r.port_key)} className="as-use-link">
                <span className="ellip">{r.run_title || t('as.use.untitledRun')}</span>
              </Link>
              {status('as.runStatus', r.run_status)}
              <span className="small faint mono">{r.port_key}</span>
            </li>
          ))}
        </UseGroup>
        <UseGroup icon={<PackageCheck aria-hidden />} title={t('as.use.runOutputs')} count={u.run_outputs.length}>
          {u.run_outputs.map((r) => (
            <li key={`${r.node_run_id}-${r.output_key}`}>
              <Link to={runPath(r.run_id)} className="as-use-link">
                <span className="ellip">{r.run_title || t('as.use.untitledRun')}</span>
              </Link>
              {status('as.runStatus', r.run_status)}
              <span className="small faint">{t('as.use.attempt', { n: r.attempt })}</span>
            </li>
          ))}
        </UseGroup>
        <UseGroup icon={<ClipboardCheck aria-hidden />} title={t('as.use.requests')} count={u.approval_requests.length}>
          {u.approval_requests.map((a) => (
            <li key={a.id}>
              <Link to={paths.approvalRequest(a.id)} className="as-use-link">
                <span className="ellip">{a.title || requestKindText(t, a.kind, a.stage, a.kind === 'engineering' && !a.run_id)}</span>
              </Link>
              <span className="small faint">{requestKindText(t, a.kind, a.stage, a.kind === 'engineering' && !a.run_id)}</span>
              <RequestStatusChip status={a.status} />
            </li>
          ))}
        </UseGroup>
      </div>
    );

  return (
    <section className="panel" aria-labelledby="as-use-h">
      <div className="panel-h">
        <ListChecks aria-hidden className="faint" />
        <h2 id="as-use-h">{t('as.use.title')}</h2>
        {u && total > 0 && <span className="faint small">· {fmt.number(total)}</span>}
      </div>
      <div className="panel-b stack-sm">
        {u && (wr.total > 0 || ki.total > 0) && <p className="faint small">{t('as.use.separateJudgements')}</p>}
        {body}
      </div>
    </section>
  );
}
