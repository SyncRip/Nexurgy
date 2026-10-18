// /parts — the current project's parts (R1.2 §07, prototype #parts): a card
// wall by default (thumbnail, latest run state, flow progress, workflow name)
// or the parts table, filtered by the latest run's state. Opening a part leads
// to its workspace, whose default tab is its model.

import { LayoutGrid, Table2 } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { listParts } from '../../api/endpoints';
import type { Part } from '../../api/types';
import { paths } from '../../app/nav';
import { SearchInput } from '../../components/ui/Form';
import { Select } from '../../components/ui/Select';
import { EmptyState, ErrorNotice, ErrorState, SkeletonCards } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { FlowBar, PartCard, RunStateChip } from '../../features/parts/PartCard';
import { filterCounts, flowSummary, latestRunByPart, matchesFilter, PARTS_FILTERS, runFlowName, type PartFlowSummary, type PartsFilter } from '../../features/parts/model';
import { RUN_WINDOW, useProjectRuns } from '../../features/parts/useProjectRuns';
import { NewPartButton, UploadStartButton } from '../../features/workbench/StartActions';
import { AssembliesPanel } from '../../features/assembly/AssembliesPanel';
import '../../features/parts/parts.css';
import { PartsTab } from '../project/PartsTab';
import { AreaFrame, type AreaContext } from './AreaFrame';

type View = 'cards' | 'table';
const VIEW_KEY = 'partroute.parts.view';

function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'cards';
  } catch {
    return 'cards';
  }
}

function writeView(v: View) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    /* per-viewer convenience only */
  }
}

/** Table cell: the latest run's state, progress and workflow. */
function FlowCell({ summary }: { summary: PartFlowSummary | undefined }) {
  const { t } = useI18n();
  if (!summary || summary.state === 'unknown') return <span className="faint">—</span>;
  if (!summary.run) return <span className="faint small">{t('home.card.noRun')}</span>;
  const p = summary.progress;
  return (
    <div className="pt-flow">
      <div className="pt-flow-row">
        <RunStateChip state={summary.state} />
        {p && p.total > 0 && (
          <>
            <FlowBar progress={p} />
            <span className="mono">
              {p.done}/{p.total}
            </span>
          </>
        )}
      </div>
      <span className="faint small ellip">{runFlowName(summary.run)}</span>
    </div>
  );
}

function PartsArea({ project, caps, reloadProject, reloadKey }: AreaContext & { reloadKey: number }) {
  const { t, fmt } = useI18n();
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const [filter, setFilter] = useState<PartsFilter>('all');
  const [view, setViewState] = useState<View>(readView);
  const setView = (v: View) => {
    setViewState(v);
    writeView(v);
  };

  const partsRes = useResource((signal) => listParts(project.id, { q: dq || undefined }, { signal }), [project.id, dq, reloadKey], { enabled: view === 'cards' });
  const parts = useMemo(() => (partsRes.data ?? []).filter((p) => p.project_id === project.id), [partsRes.data, project.id]);
  const runs = useProjectRuns(project.id, reloadKey);
  // Until the runs arrive (or when they failed) every state is "unknown", never "not run".
  const latest = useMemo(() => latestRunByPart(runs.runs ?? []), [runs.runs]);
  const unsure = runs.windowFull || !runs.runs;
  const summaryOf = useCallback((p: Pick<Part, 'id'>) => flowSummary(latest.get(p.id), unsure), [latest, unsure]);
  const states = parts.map((p) => summaryOf(p).state);
  const counts = filterCounts(states);
  const shown = parts.filter((_, i) => matchesFilter(states[i], filter));

  // The table loads its own rows (with its own status / tag / archive filters) and looks states up the same way.
  const rowFilter = useCallback((p: Part) => matchesFilter(summaryOf(p).state, filter), [filter, summaryOf]);

  let cards;
  if (partsRes.error && !partsRes.data) cards = <ErrorState error={partsRes.error} onRetry={partsRes.reload} />;
  else if (partsRes.initial) cards = <SkeletonCards count={6} />;
  else if (!shown.length)
    cards = (
      <EmptyState
        compact
        title={parts.length || dq ? t('home.parts.noMatch') : t('parts.empty.title')}
        body={parts.length || dq ? t('home.parts.noMatchBody') : caps.design ? t('parts.empty.body') : t('parts.empty.readOnly')}
        action={
          filter !== 'all' || q ? (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setQ('');
                setFilter('all');
              }}
            >
              {t('common.clearFilters')}
            </button>
          ) : undefined
        }
      />
    );
  else
    cards = (
      <ul className={`pc-grid wide ${partsRes.loading ? 'is-refreshing' : ''}`}>
        {shown.map((p) => (
          <li key={p.id}>
            <PartCard part={p} summary={summaryOf(p)} />
          </li>
        ))}
      </ul>
    );

  // Workflow-state filter options, with live counts in card view.
  const filterOptions = PARTS_FILTERS.map((f) => ({
    value: f,
    label: view === 'cards' && partsRes.data && runs.runs ? `${t(`home.filter.${f}`)} (${fmt.number(counts[f])})` : t(`home.filter.${f}`),
  }));

  return (
    <div className="stack">
      {/* Standard toolbar row (40px): search on the left; filter, result count
          and the view switch on the right. */}
      <div className="pp-toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t('parts.searchPlaceholder')} />
        <span className="grow" />
        <Select
          className="pp-filter-select"
          aria-label={t('home.filter.label')}
          value={filter}
          onChange={(v) => setFilter(v as PartsFilter)}
          options={filterOptions}
          size="sm"
        />
        <div className="pp-results" aria-live="polite">
          {view === 'cards' && partsRes.data && <span>{t('home.parts.results', { n: fmt.number(shown.length) })}</span>}
          {(filter !== 'all' || q) && (
            <button className="linkish" onClick={() => { setQ(''); setFilter('all'); }}>
              {t('common.clearFilters')}
            </button>
          )}
        </div>
        <div className="seg" role="group" aria-label={t('home.view.label')}>
          <button type="button" className="seg-btn" aria-pressed={view === 'cards'} onClick={() => setView('cards')} aria-label={t('home.view.cards')} title={t('home.view.cards')}>
            <LayoutGrid aria-hidden /> <span className="pp-view-label">{t('home.view.cards')}</span>
          </button>
          <button type="button" className="seg-btn" aria-pressed={view === 'table'} onClick={() => setView('table')} aria-label={t('home.view.table')} title={t('home.view.table')}>
            <Table2 aria-hidden /> <span className="pp-view-label">{t('home.view.table')}</span>
          </button>
        </div>
      </div>
      {view === 'cards' && partsRes.data && !!partsRes.error && <ErrorNotice error={partsRes.error} onRetry={partsRes.reload} />}
      {runs.error && !runs.runs ? <ErrorNotice error={runs.error} onRetry={runs.reload} /> : null}
      {runs.windowFull && <p className="pp-note">{t('home.parts.windowFull', { n: RUN_WINDOW })}</p>}
      {view === 'cards' ? (
        cards
      ) : (
        <PartsTab
          project={project}
          caps={caps}
          reloadProject={reloadProject}
          query={q}
          rowFilter={filter === 'all' ? undefined : rowFilter}
          hideCreate
          reloadKey={reloadKey}
          extraColumn={{ header: t('home.col.flow'), cell: (p) => <FlowCell summary={summaryOf(p)} /> }}
        />
      )}
    </div>
  );
}

export default function PartsPage() {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const [reloadKey, setReloadKey] = useState(0);
  // W4: the area lists parts or static assemblies (?kind=assemblies keeps the choice in the URL).
  const [search, setSearch] = useSearchParams();
  const showAssemblies = search.get('kind') === 'assemblies';
  const setKind = (asm: boolean) => {
    const next = new URLSearchParams(search);
    if (asm) next.set('kind', 'assemblies');
    else next.delete('kind');
    setSearch(next, { replace: true });
  };
  return (
    <AreaFrame
      title={t('nav.parts')}
      subtitle={t('home.parts.sub')}
      actions={(ctx) =>
        ctx.caps.design && !showAssemblies ? (
          <>
            <UploadStartButton projectId={ctx.project.id} short />
            <NewPartButton
              project={ctx.project}
              onSaved={(p) => {
                setReloadKey((k) => k + 1);
                ctx.reloadProject();
                toast(t('parts.createdToast', { number: p.number }), { action: { label: t('common.open'), onClick: () => navigate(paths.part(p.id)) } });
              }}
            />
          </>
        ) : null
      }
    >
      {(ctx) => (
        <div className="stack">
          {/* Area bar (40px): parts vs assemblies, on its own standard row. */}
          <div className="seg pp-kind" role="group" aria-label={t('asm.area.switch')}>
            <button type="button" className="seg-btn" aria-pressed={!showAssemblies} onClick={() => setKind(false)}>
              {t('asm.area.parts')}
            </button>
            <button type="button" className="seg-btn" aria-pressed={showAssemblies} onClick={() => setKind(true)}>
              {t('asm.area.assemblies')}
            </button>
          </div>
          {showAssemblies ? <AssembliesPanel projectId={ctx.project.id} caps={ctx.caps} /> : <PartsArea {...ctx} reloadKey={reloadKey} />}
        </div>
      )}
    </AreaFrame>
  );
}
