// Right-hand detail sheet of one catalogue item (handoff §6–§10): version select with separate state chips →
// preview / content → Material information → folded upstream & downstream, usage & task results, version history, related
// experience → actions on the existing APIs (or disabled with the reason). One component for every object: the
// item's real identity (`ref.kind` / asset type) picks the version source, content mode and mutation APIs from the
// capability tables in detailModel.ts. Professional editors stay on their own pages; a `version` that is not one of
// this item's versions is an explicit error (no fallback to the latest, AssetDetailPage D07).

import '../assets/assets.css';
import './detail.css';
import { ArrowDown, ArrowUp, BookOpen, ChevronDown, ExternalLink, FileWarning, GitBranch, History, PlayCircle, ShieldAlert, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getAssetVersionUsages, type AssetUsages } from '../../api/assets';
import { listMembers } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { paths } from '../../app/nav';
import { PolicyChip } from '../../components/engineering/EngineeringChips';
import { ArchivedChip, Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { HistoricalBanner } from '../../components/ui/HistoricalBanner';
import { ErrorNotice, Skeleton, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { isTypingTarget } from '../../hooks/keyboard';
import { useI18n } from '../../i18n/I18nProvider';
import { BlobPreview } from '../approvals/TargetSnapshot';
import { DependencyGraph, UsagesPanel } from '../assets/AssetRelations';
import { ItemKindChip, ReusableChip } from '../assets/collabMeta';
import { EngineeringChip, LabelCallouts, PublishChip, SourceLabels, VersionToken, dynLabel, runPath } from '../assets/meta';
import { VersionSummary } from '../assets/VersionSummary';
import { ModuleContentLoader } from '../modules/ModuleView';
import { SimulatedChip, SpecStatusChip } from '../spec/common';
import { contentPreview, type FileState, type VersionContent } from './catalog';
import { MoreActions, PrimaryActions, type DetailActionsProps } from './detailActions';
import { useDetailVersions } from './detailData';
import {
  assetVersionPath,
  contentOf,
  detailCaps,
  pagePath,
  relationCounts,
  resolveVersion,
  usageTotal,
  versionOrigin,
  versionParam,
  type DetailCaps,
  type DetailVersion,
} from './detailModel';
import { creationLabel, previewLabel, typeLabel } from './meta';
import type { CatalogItem } from './types';

export interface CatalogDetailProps {
  item: CatalogItem;
  /** Full row of item.project (role / caps / status). */
  project: Project;
  /** URL `version`; null = the latest version. */
  versionId: string | null;
  onVersion: (versionId: string | null) => void;
  onClose: () => void;
  /** Something was saved (new version, rename, archive…): the page reloads the catalogue. */
  onChanged: () => void;
  /** Current in-app URL (scope, filters, item, version) for professional pages' return links. */
  returnTo: string;
  navigation?: { index: number; total: number; previous: string | null; next: string | null; onSelect: (key: string) => void };
}

const USAGE_PAGE = 20;

// ─── small pieces ───────────────────────────────────────────────────────────

/** Disclosure row with a count; the body mounts (and loads) only when opened. */
function Fold({ title, count, defaultOpen = false, children }: { title: string; count?: string | null; defaultOpen?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <section className={`rd-fold ${open ? 'open' : ''}`}>
      <h3 className="rd-fold-h">
        <button type="button" className="rd-fold-btn" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen((v) => !v)}>
          <span className="rd-fold-title">{title}</span>
          {count ? <span className="rd-fold-count">{count}</span> : null}
          <ChevronDown aria-hidden className="rd-fold-ico" />
        </button>
      </h3>
      {open && (
        <div id={id} className="rd-fold-body">
          {children}
        </div>
      )}
    </section>
  );
}

function NoPreviewNote({ state, converted }: { state: FileState; converted: boolean }) {
  const { t } = useI18n();
  const body = state === 'convert' ? 'rd.preview.convertBody' : state === 'preview' ? 'rd.preview.browserBody' : converted ? 'rd.preview.convertedBody' : 'rd.preview.downloadBody';
  return (
    <div className="rd-nopreview" role="note">
      <FileWarning aria-hidden />
      <div>
        <b>{previewLabel(t, state === 'preview' ? 'download' : state)}</b>
        <p>{t(body)}</p>
      </div>
    </div>
  );
}

/** How a version was made; `plain` (inside another control) never renders the run link. */
function OriginText({ v, taskWritten, plain = false }: { v: DetailVersion; taskWritten: readonly string[]; plain?: boolean }) {
  const { t } = useI18n();
  const o = versionOrigin(v, taskWritten);
  switch (o.kind) {
    case 'run':
      return plain ? (
        <span>{t('rd.origin.task')}</span>
      ) : (
        <Link to={runPath(o.runId)} className="as-use-link">
          <PlayCircle aria-hidden className="inline-ico" /> {t('rd.origin.run')}
        </Link>
      );
    case 'task':
      return <span>{t('rd.origin.task')}</span>;
    case 'upload':
      return <span>{t('rd.origin.upload')}</span>;
    case 'label':
      return <span>{dynLabel(t, o.key, o.raw)}</span>;
    default:
      return <span className="faint">{t('rd.origin.unknown')}</span>;
  }
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="rd-facts">
      {rows.map(([k, v]) => (
        <div key={k} className="rd-fact">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

// ─── version bar ────────────────────────────────────────────────────────────

function VersionBar({ item, loaded, selected, isLatest, missing, content, onVersion }: { item: CatalogItem; loaded: DetailVersion[] | undefined; selected: DetailVersion | null; isLatest: boolean; missing: boolean; content: VersionContent | null; onVersion: (id: string | null) => void }) {
  const { t, fmt } = useI18n();
  const id = useId();
  const versions = loaded ?? [];
  const a = selected?.asset ?? null;
  const s = selected?.spec ?? null;
  return (
    <div className="rd-versionbar">
      <div className="rd-vselect">
        <label htmlFor={id}>{t('rd.version.label')}</label>
        <select id={id} className="select sm" value={missing || !selected ? '' : selected.id} disabled={!versions.length} onChange={(e) => e.target.value && onVersion(versionParam(versions, e.target.value))}>
          {(missing || !selected) && (
            <option value="" disabled>
              {!loaded ? t('common.loading') : versions.length ? t('rd.version.choose') : t('rd.version.none')}
            </option>
          )}
          {versions.map((v, i) => (
            <option key={v.id} value={v.id}>
              {[v.label, fmt.date(v.createdAt), i === 0 ? t('rd.chip.latest') : null, v.asset?.publish_state === 'deprecated' ? t('rd.version.deprecated') : null].filter(Boolean).join(' · ')}
            </option>
          ))}
        </select>
      </div>
      {selected && (
        <div className="rd-chips">
          <Chip tone={isLatest ? 'measure' : 'pending'}>{t(isLatest ? 'rd.chip.latest' : 'rd.chip.history')}</Chip>
          {a && <PublishChip state={a.publish_state} />}
          {a?.engineering && <EngineeringChip engineering={a.engineering} compact />}
          {s && <SpecStatusChip status={s.status} />}
          {s?.simulated && <SimulatedChip />}
          {item.archived && <ArchivedChip />}
          {isLatest && item.upstreamNewer && (
            <Chip tone="warn" icon={<GitBranch aria-hidden />}>
              {t('rd.chip.upstreamNewer')}
            </Chip>
          )}
          {a && <SourceLabels labels={a.labels} max={4} emptyDash={false} />}
          {content && <span className="rd-preview-state">{previewLabel(t, contentPreview(content))}</span>}
        </div>
      )}
    </div>
  );
}

// ─── content ────────────────────────────────────────────────────────────────

function ContentView({ content, version }: { content: VersionContent; version: DetailVersion | null }) {
  const { t } = useI18n();
  const a = version?.asset ?? null;
  const s = version?.spec ?? null;
  switch (content.kind) {
    case 'blob':
      return <BlobPreview sha={content.sha} name={content.name} noPreview={<NoPreviewNote state={content.state} converted={content.converted} />} />;
    case 'summary':
      return a ? (
        <div className="stack-sm">
          <VersionSummary version={a} />
          <p className="faint small">{t('rd.content.summaryNote')}</p>
        </div>
      ) : null;
    case 'module':
      return a ? <ModuleContentLoader versionId={a.id} /> : null;
    case 'spec':
      return s ? (
        <Facts
          rows={[
            [t('rd.spec.status'), <SpecStatusChip status={s.status} />],
            [t('rd.spec.policy'), <PolicyChip policy={s.execution_policy} />],
            [t('rd.spec.origin'), dynLabel(t, `spec.origin.${s.origin}`, s.origin)],
            [t('rd.spec.counts'), t('spec.list.counts', { d: s.counts.definitions, i: s.counts.instances, q: s.counts.questions_open })],
          ]}
        />
      ) : null;
    case 'page':
      return <p className="muted small">{t('rd.content.page')}</p>;
    case 'failed':
      return (
        <div className="callout err" role="note">
          <TriangleAlert aria-hidden />
          <span>{t('rd.content.failed')}</span>
        </div>
      );
    case 'none':
      return <p className="faint small">{t(version ? 'rd.content.none' : 'rd.content.noVersion')}</p>;
  }
}

// ─── folded sections ────────────────────────────────────────────────────────

function HistoryList({ versions, selectedId, taskWritten, memberName, onVersion }: { versions: DetailVersion[]; selectedId: string | null; taskWritten: readonly string[]; memberName: Map<string, string>; onVersion: (id: string | null) => void }) {
  const { t, fmt } = useI18n();
  return (
    <ol className="rd-history">
      {versions.map((v, i) => (
        <li key={v.id}>
          <button type="button" className={`rd-history-item ${v.id === selectedId ? 'active' : ''}`} aria-current={v.id === selectedId ? 'true' : undefined} onClick={() => onVersion(versionParam(versions, v.id))}>
            <span className="rd-history-top">
              <VersionToken label={v.label} />
              {i === 0 && <span className="chip measure as-latest">{t('rd.chip.latest')}</span>}
              {v.asset && <PublishChip state={v.asset.publish_state} />}
              {v.spec && <SpecStatusChip status={v.spec.status} />}
              <span className="grow" />
              <span className="faint small nowrap" title={fmt.dateTime(v.createdAt)}>
                {fmt.relative(v.createdAt)}
              </span>
            </span>
            <span className="small muted">
              <OriginText v={v} taskWritten={taskWritten} plain />
              {v.createdBy && <> · {memberName.get(v.createdBy) ?? t('as.someone')}</>}
            </span>
            {v.note && <span className="small rd-history-note">{v.note}</span>}
          </button>
        </li>
      ))}
    </ol>
  );
}

function ExperienceList({ items, total, projectId }: { items: NonNullable<AssetUsages['knowledge_items']>; total: number; projectId: string }) {
  const { t, fmt } = useI18n();
  if (!items.length) return <p className="faint small">{t('rd.experience.none')}</p>;
  return (
    <div className="stack-sm">
      <ul className="as-uses">
        {items.map((k) => (
          <li key={k.item_id} className="as-use-row">
            <span className="as-use-main">
              <Link to={paths.knowledge(projectId, k.collection_id, k.collection_version_id)} className="as-use-link">
                <BookOpen aria-hidden className="inline-ico" /> <span className="ellip">{k.collection_title || t('rd.experience.collection')}</span>
              </Link>
            </span>
            <span className="as-use-states">
              {k.kind && <ItemKindChip kind={k.kind} />}
              <ReusableChip reusable={k.reusable} />
            </span>
          </li>
        ))}
      </ul>
      {total > items.length && <p className="faint small">{t('rd.experience.more', { n: fmt.number(total - items.length) })}</p>}
    </div>
  );
}

// ─── body of a resolved version ─────────────────────────────────────────────

function DetailBody({
  item,
  project,
  caps,
  versions,
  selected,
  latest,
  isLatest,
  content,
  memberName,
  returnTo,
  onVersion,
  onChanged,
  reloadVersions,
}: {
  item: CatalogItem;
  project: Project;
  caps: DetailCaps;
  versions: DetailVersion[];
  selected: DetailVersion | null;
  latest: DetailVersion | null;
  isLatest: boolean;
  content: VersionContent;
  memberName: Map<string, string>;
  returnTo: string;
  onVersion: (id: string | null) => void;
  onChanged: () => void;
  reloadVersions: () => void;
}) {
  const { t, fmt } = useI18n();
  const infoId = useId();
  const a = selected?.asset ?? null;
  const usages = useResource((signal) => getAssetVersionUsages(a!.id, { relation: 'all', limit: USAGE_PAGE }, { signal }), [a?.id], { enabled: !!a });
  const u = a && usages.data?.version_id === a.id ? usages.data : undefined;
  const counts = a ? relationCounts(a) : null;
  const upstreamStale = isLatest && item.upstreamNewer;

  // The professional page of this exact version, and the asset version page (provenance, publish) when different.
  const fullPage = pagePath(item, selected, isLatest, returnTo);
  const exactVersionPage = item.ref.kind === 'asset' && a ? assetVersionPath(item.ref.assetId, a.id, returnTo) : null;
  const versionPage = exactVersionPage && exactVersionPage !== fullPage ? exactVersionPage : null;
  const actionProps: DetailActionsProps = {
    item,
    project,
    ctx: { caps, project, archived: item.archived, version: selected },
    latestLabel: latest?.label ?? null,
    editPath: caps.fileId ? null : fullPage,
    onChanged,
    onNewVersion: () => {
      reloadVersions();
      onVersion(null);
    },
    onVersionChanged: reloadVersions,
  };
  const parent = a?.parent_version_id ? (versions.find((v) => v.asset?.id === a.parent_version_id)?.asset ?? null) : null;
  const oldest = versions[versions.length - 1] ?? null;
  const noAsset = <p className="faint small">{t('rd.noAssetVersion')}</p>;
  const dash = <span className="faint">—</span>;

  return (
    <>
      {selected && !isLatest && latest && (
        <HistoricalBanner current={latest.label} onOpenCurrent={() => onVersion(null)}>
          <span className="faint small">{t('rd.note.history')}</span>
        </HistoricalBanner>
      )}
      {upstreamStale && (
        <div className="callout warn" role="note">
          <GitBranch aria-hidden />
          <div className="stack-sm">
            <b>{t('rd.note.upstreamNewer')}</b>
            <span className="small">{t('rd.note.upstreamHint')}</span>
          </div>
        </div>
      )}
      {a?.publish_state === 'deprecated' && (
        <div className="callout pending" role="note">
          <ShieldAlert aria-hidden />
          <span>{t('rd.note.deprecated')}</span>
        </div>
      )}
      {item.archived && (
        <div className="callout info" role="note">
          <span>{t(item.archived === 'part' ? 'rd.note.partArchived' : 'rd.note.archived')}</span>
        </div>
      )}
      {a && <LabelCallouts labels={a.labels} />}

      <section className="rd-content" aria-label={t('rd.content.title')}>
        <ContentView content={content} version={selected} />
        {(fullPage || versionPage) && (
          <div className="rd-pagelinks">
            {fullPage && (
              <Link className="btn sm" to={fullPage}>
                <ExternalLink /> {t('rd.action.open')}
              </Link>
            )}
            {versionPage && (
              <Link className="btn sm ghost" to={versionPage}>
                <History /> {t('rd.action.versionPage')}
              </Link>
            )}
          </div>
        )}
      </section>
      <PrimaryActions {...actionProps} />

      <section className="rd-info" aria-labelledby={infoId}>
        <h3 id={infoId} className="rd-section-h">
          {t('rd.info.title')}
        </h3>
        <Facts
          rows={[
            [t('rd.info.project'), `${item.project.name} · ${item.project.code}`],
            [
              t('rd.info.part'),
              item.partId && item.partNumber ? (
                <Link to={paths.part(item.partId)} className="mono">
                  {item.partNumber}
                </Link>
              ) : (
                <span className="faint">{t('rd.info.noPart')}</span>
              ),
            ],
            ...(item.description ? ([[t('rd.info.description'), <span className="rd-note">{item.description}</span>]] as [string, ReactNode][]) : []),
            [t('rd.info.creation'), isLatest || !selected ? creationLabel(t, item.creation) : t('rd.info.creationLatest', { method: creationLabel(t, item.creation) })],
            [t('rd.info.versionSource'), selected ? <OriginText v={selected} taskWritten={item.taskWritten} /> : dash],
            [t('rd.info.originalSource'), oldest ? <OriginText v={oldest} taskWritten={item.taskWritten} /> : dash],
            ...(selected?.stored.file ? ([[t('rd.info.fileName'), <span className="mono rd-break">{selected.stored.file.name}</span>]] as [string, ReactNode][]) : []),
            [
              t('rd.info.created'),
              selected?.createdAt ? (
                <span>
                  {selected.createdBy ? `${memberName.get(selected.createdBy) ?? t('as.someone')} · ` : ''}
                  {fmt.dateTime(selected.createdAt)}
                </span>
              ) : (
                dash
              ),
            ],
            [t('rd.info.note'), selected?.note ? <span className="rd-note">{selected.note}</span> : dash],
          ]}
        />
      </section>

      <div className="rd-folds">
        <Fold title={t('rd.fold.relations')} count={counts ? t('rd.fold.relationsCount', { up: counts.upstream, down: counts.downstream }) : null} defaultOpen={upstreamStale}>
          {a ? <DependencyGraph version={a} parent={parent} /> : noAsset}
        </Fold>
        <Fold title={t('rd.fold.usages')} count={u ? t('rd.fold.count', { n: fmt.number(usageTotal(u)) }) : null}>
          {a ? <UsagesPanel version={a} /> : noAsset}
        </Fold>
        <Fold title={t('rd.fold.history')} count={t('rd.fold.versions', { n: fmt.number(versions.length) })}>
          {versions.length ? <HistoryList taskWritten={item.taskWritten} versions={versions} selectedId={selected?.id ?? null} memberName={memberName} onVersion={onVersion} /> : <p className="faint small">{t('rd.content.noVersion')}</p>}
        </Fold>
        <Fold title={t('rd.fold.experience')} count={u ? t('rd.fold.count', { n: fmt.number(u.pagination?.knowledge_items?.total ?? u.knowledge_items?.length ?? 0) }) : null}>
          {!a ? (
            noAsset
          ) : usages.error && !u ? (
            <ErrorNotice error={usages.error} onRetry={usages.reload} />
          ) : !u ? (
            <Skeleton w="60%" />
          ) : (
            <ExperienceList items={u.knowledge_items ?? []} total={u.pagination?.knowledge_items?.total ?? u.knowledge_items?.length ?? 0} projectId={project.id} />
          )}
        </Fold>
      </div>

      <footer className="rd-foot">
        <h3 className="rd-section-h">{t('rd.actions.more')}</h3>
        <MoreActions {...actionProps} />
      </footer>
    </>
  );
}

// ─── the sheet ──────────────────────────────────────────────────────────────

function CatalogDetailContent({ item, project, versionId, onVersion, onChanged, returnTo }: CatalogDetailProps) {
  const { t } = useI18n();
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    root.current?.parentElement?.scrollTo({ top: 0 });
  }, []);
  const caps = useMemo(() => detailCaps(item.ref), [item.ref]);
  const vs = useDetailVersions(item);
  const r = resolveVersion(vs.versions, versionId);
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id]);
  const memberName = useMemo(() => new Map((members.data ?? []).map((m) => [m.user.id, m.user.name])), [members.data]);
  const content = vs.versions && !r.missing ? contentOf(caps, r.selected) : null;

  let body: ReactNode;
  if (!vs.versions) {
    body = vs.error ? (
      <ErrorNotice error={vs.error} onRetry={vs.reload} />
    ) : (
      <div className="stack" role="status" aria-label={t('common.loading')}>
        <Skeleton w="100%" h={220} />
        <SkeletonRows rows={4} cols={2} />
      </div>
    );
  } else if (r.missing) {
    body = (
      <div className="callout err rd-missing" role="alert">
        <ShieldAlert aria-hidden />
        <div className="stack-sm">
          <b>{t('rd.missing.title')}</b>
          <span>{t('rd.missing.body')}</span>
          <code className="mono small rd-break">{versionId}</code>
          <span className="small">{t('rd.missing.noFallback')}</span>
          <span className="small faint">{t('rd.missing.actions')}</span>
        </div>
      </div>
    );
  } else {
    body = (
      <DetailBody
        item={item}
        project={project}
        caps={caps}
        versions={vs.versions}
        selected={r.selected}
        latest={r.latest}
        isLatest={r.isLatest}
        content={content ?? { kind: 'none' }}
        memberName={memberName}
        returnTo={returnTo}
        onVersion={onVersion}
        onChanged={onChanged}
        reloadVersions={vs.reload}
      />
    );
  }

  return (
    <div className="rd" ref={root}>
      <VersionBar item={item} loaded={vs.versions} selected={r.selected} isLatest={r.isLatest} missing={r.missing} content={content} onVersion={onVersion} />
      {body}
    </div>
  );
}

export function CatalogDetail(props: CatalogDetailProps) {
  const { t } = useI18n();
  const { item, navigation, onClose } = props;
  const moveTo = (key: string | null, target: HTMLElement) => {
    if (!key || !navigation) return;
    // Keep focus inside the stable dialog when end navigation buttons are disabled or detail content is rebuilt.
    target.closest<HTMLElement>('[role="dialog"]')?.focus({ preventScroll: true });
    navigation.onSelect(key);
  };
  return (
    <Dialog
      placement="side"
      title={item.name}
      subtitle={`${typeLabel(t, item.type)} · ${item.project.name} · ${item.project.code}`}
      onClose={onClose}
      headerActions={navigation && (
        <nav className="detail-navigation" aria-label={t('rp.preview.navigation')}>
          <span className="detail-navigation-count" aria-live="polite">{navigation.index + 1}/{navigation.total}</span>
          <button type="button" className="btn ghost icon" aria-label={t('rp.preview.previous')} title={t('rp.preview.previous')} disabled={!navigation.previous} onClick={(event) => moveTo(navigation.previous, event.currentTarget)}>
            <ArrowUp aria-hidden />
          </button>
          <button type="button" className="btn ghost icon" aria-label={t('rp.preview.next')} title={t('rp.preview.next')} disabled={!navigation.next} onClick={(event) => moveTo(navigation.next, event.currentTarget)}>
            <ArrowDown aria-hidden />
          </button>
        </nav>
      )}
      onKeyDown={(event) => {
        if (!navigation || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || isTypingTarget(event.target)) return;
        const target = event.target as HTMLElement;
        if (target.closest('[role="dialog"], [role="alertdialog"]') !== event.currentTarget || target.closest('[role="menu"], [role="listbox"], .popover')) return;
        const key = event.key === 'ArrowUp' ? navigation.previous : event.key === 'ArrowDown' ? navigation.next : null;
        if (key) {
          event.preventDefault();
          moveTo(key, event.currentTarget);
        }
      }}
    >
      <CatalogDetailContent key={item.key} {...props} />
    </Dialog>
  );
}
