import { notifyTodosChanged } from '../todos/events';
// Sections of /specs/:specId (W2): sources with read state, definitions / instance tree with quantities and
// readiness, relations, facts with evidence locators, questions with the answer form, assumptions, conflicts,
// manufacturing notes / deliverables and the version history. Everything shown comes from the server; the
// read state is the server's record of what it sent, never the model's claim.

import { CheckCircle2,CircleAlert,Eye,Send } from 'lucide-react';
import { Fragment,useMemo,useState,type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { SpecContent } from '../../api/spec';
import { answerSpecQuestions,getSpecChunk,specPath,type EngineeringSpec,type SpecEvidence,type SpecFact,type SpecInstance,type SpecVersion } from '../../api/spec';
import { PolicyChip,RepresentationChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { LimitedInput } from '../../components/ui/LimitedText';
import { ErrorNotice,Spinner } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { DecisionCorrect } from '../turbo/DecisionCorrect';
import { droppedDecisions,droppedLine } from '../turbo/decisions';
import { AssumptionList } from '../turbo/Turbo';
import { FactStatusChip,ReadinessChip,SimulatedChip,SourceReadChip,SpecStatusChip,fmtValue,fmtXyz,tk } from './common';

export type ViewChunk = (seq: number, quote?: string | null) => void;

function Section({ id, title, count, children }: { id: string; title: string; count?: number; children: ReactNode }) {
  return (
    <section className="panel spec-sec" aria-labelledby={`spec-${id}`} id={`spec-sec-${id}`}>
      <div className="panel-h">
        <h2 id={`spec-${id}`}>
          {title} {typeof count === 'number' ? <span className="count">({count})</span> : null}
        </h2>
      </div>
      <div className="panel-b">{children}</div>
    </section>
  );
}

function content(v: SpecVersion): SpecContent | null {
  return 'definitions' in v.content ? (v.content as SpecContent) : null;
}

// ─── sources ──────────────────────────────────────────────────────────────────────────────────────

export function SourcesSection({ version, onViewChunk }: { version: SpecVersion; onViewChunk: ViewChunk }) {
  const { t } = useI18n();
  return (
    <Section id="sources" title={t('spec.sec.sources')} count={version.sources.length}>
      {version.sources.map((s) => {
        const chunks = version.chunks.filter((c) => c.source_id === s.id);
        return (
          <div key={s.id} className="spec-src">
            <div className="spec-src-head">
              <span className="mono strong">{s.id}</span>
              <span className="strong">{s.label}</span>
              <Chip tone="pending" className="spec-chip-sm">
                {tk(t, `spec.src.kind.${s.kind}`, s.kind)}
              </Chip>
              {s.answer_of ? <span className="faint small">{t('spec.src.answerOf', { q: s.answer_of })}</span> : null}
              <SourceReadChip status={s.read_status} />
              {s.sha256 ? (
                <span className="mono small faint" title={s.sha256}>
                  {t('spec.src.sha')} {s.sha256.slice(0, 12)}…
                </span>
              ) : null}
              {s.chars && s.chars_read !== undefined ? (
                <span className="small faint">{t('spec.src.charsRead', { read: s.chars_read, total: s.chars })}</span>
              ) : null}
            </div>
            {s.error ? (
              <div className="callout err slim">
                <CircleAlert aria-hidden />
                <span>
                  <span className="mono">{s.error.code}</span> · {s.error.message}
                </span>
              </div>
            ) : null}
            {s.ranges.length ? (
              <div className="spec-src-ranges">
                <span className="faint">{t('spec.src.ranges')}:</span>
                {s.ranges.map((r) => (
                  <span key={r} className="mono">
                    {r}
                  </span>
                ))}
              </div>
            ) : null}
            {s.unread?.length ? (
              <div className="spec-src-ranges">
                <span className="faint">{t('spec.src.notRead')}:</span>
                {s.unread.map((u) => (
                  <span key={u.chunk} className="mono">
                    {u.locator} ({tk(t, `spec.chunk.status.${u.status}`, u.status)}{u.reason ? ` · ${u.reason}` : ''})
                  </span>
                ))}
              </div>
            ) : null}
            {s.not_chunked?.length ? (
              <div className="spec-src-ranges">
                <span className="faint">{t('spec.src.notChunked')}:</span>
                {s.not_chunked.map((n) => (
                  <span key={n}>{n}</span>
                ))}
              </div>
            ) : null}
            {chunks.length ? (
              <details>
                <summary>{t('spec.src.chunks', { n: chunks.length })}</summary>
                <div className="table-wrap spec-chunks">
                  <table className="t dense">
                    <tbody>
                      {chunks.map((c) => (
                        <tr key={c.seq}>
                          <td className="mono">#{c.seq}</td>
                          <td className="mono small">
                            {c.locator}
                            {c.overlap ? <span className="faint"> · {t('spec.chunk.overlap', { n: c.overlap })}</span> : null}
                          </td>
                          <td>
                            <Chip tone={c.status === 'sent' ? 'ok' : c.status === 'failed' ? 'err' : 'stale'} className="spec-chip-sm">
                              {tk(t, `spec.chunk.status.${c.status}`, c.status)}
                            </Chip>
                            {c.inherited_from_version_id ? <span className="faint small"> · {t('spec.chunk.inherited')}</span> : null}
                          </td>
                          <td className="small faint">{c.ai_call_id ? t('spec.chunk.call', { id: c.ai_call_id.slice(0, 8) }) : c.reason ?? ''}</td>
                          <td className="act">
                            {c.status === 'sent' ? (
                              <button type="button" className="btn sm ghost" onClick={() => onViewChunk(c.seq)}>
                                <Eye aria-hidden /> {t('spec.chunk.view')}
                              </button>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            ) : null}
          </div>
        );
      })}
    </Section>
  );
}

// ─── structure ────────────────────────────────────────────────────────────────────────────────────

export function StructureSection({ version }: { version: SpecVersion }) {
  const { t } = useI18n();
  const c = content(version);
  const rd = version.readiness;
  const qty = version.quantities ?? {};
  const defs = useMemo(() => new Map((c?.definitions ?? []).map((d) => [d.key, d])), [c]);
  const children = useMemo(() => {
    const m = new Map<string, SpecInstance[]>();
    for (const i of c?.instances ?? []) m.set(i.parent ?? '', [...(m.get(i.parent ?? '') ?? []), i]);
    return m;
  }, [c]);
  if (!c) return null;

  const defLine = (key: string, count?: number) => {
    const d = defs.get(key);
    if (!d) return <span className="mono">{key}</span>;
    const st = rd?.definitions[key];
    return (
      <>
        {typeof count === 'number' ? <span className="qty">{t('spec.struct.qty', { n: count })}</span> : null}
        <span className="key">{d.key}</span>
        <span className="strong">{d.name}</span>
        {d.number ? <span className="mono small">{d.number}</span> : null}
        <span className="faint small">{tk(t, `spec.struct.kind.${d.kind}`, d.kind)}</span>
        {d.material ? <span className="small">{d.material}</span> : null}
        <span className="faint small">{tk(t, `spec.struct.proc.${d.procurement}`, d.procurement)}</span>
        <RepresentationChip level={d.representation} />
        <span className="faint small">{t('spec.struct.total', { n: qty[key]?.total ?? 0 })}</span>
        {st ? <ReadinessChip state={st.state} questions={[...st.questions, ...(st.below ?? [])]} /> : null}
      </>
    );
  };

  const subtree = (key: string, stack: string[]): ReactNode => {
    const kids = children.get(key) ?? [];
    if (!kids.length || stack.includes(key) || stack.length > 10) return null;
    const groups = new Map<string, SpecInstance[]>();
    for (const i of kids) groups.set(i.definition, [...(groups.get(i.definition) ?? []), i]);
    return (
      <ul>
        {[...groups.entries()].map(([def, insts]) => (
          <li key={def}>
            <div className="spec-node">{defLine(def, insts.length)}</div>
            <div className="spec-node inst">
              {insts.map((i) => (
                <span key={i.key} title={i.name}>
                  <span className="mono">{i.key}</span> {i.placement ? t('spec.struct.placement', { xyz: fmtXyz(i.placement.translate) }) : t('spec.struct.noPlacement')}
                  {rd?.instances[i.key]?.state === 'blocked' ? ` · ${t('spec.ready.blocked')}` : ''}
                </span>
              ))}
            </div>
            {subtree(def, [...stack, key])}
          </li>
        ))}
      </ul>
    );
  };

  const placed = new Set((c.instances ?? []).map((i) => i.definition));
  const unplaced = c.definitions.filter((d) => d.key !== c.root && !placed.has(d.key));
  return (
    <Section id="structure" title={t('spec.sec.structure')} count={c.definitions.length}>
      <ul className="spec-tree">
        {c.root ? (
          <li>
            <div className="spec-node">
              <Chip tone="accent" className="spec-chip-sm">
                {t('spec.struct.root')}
              </Chip>
              {defLine(c.root)}
            </div>
            {subtree(c.root, [])}
          </li>
        ) : (
          <li className="faint small">{t('spec.struct.noRoot')}</li>
        )}
      </ul>
      {unplaced.length ? (
        <div className="stack-sm" style={{ marginTop: 12 }}>
          <span className="faint small">{t('spec.struct.unplaced')}</span>
          <ul className="spec-tree">
            {unplaced.map((d) => (
              <li key={d.key}>
                <div className="spec-node">{defLine(d.key)}</div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

export function RelationsSection({ version }: { version: SpecVersion }) {
  const { t } = useI18n();
  const c = content(version);
  if (!c) return null;
  return (
    <Section id="relations" title={t('spec.sec.relations')} count={c.relations.length}>
      {!c.relations.length ? (
        <p className="faint small">{t('spec.rel.none')}</p>
      ) : (
        <div className="table-wrap">
          <table className="t dense">
            <thead>
              <tr>
                <th>ID</th>
                <th>{t('spec.rel.col.kind')}</th>
                <th>{t('spec.rel.col.a')}</th>
                <th>{t('spec.rel.col.b')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {c.relations.map((r) => (
                <tr key={r.key}>
                  <td className="mono">{r.key}</td>
                  <td>{r.kind}</td>
                  <td className="mono small">
                    {r.a.instance}.{r.a.datum}
                  </td>
                  <td className="mono small">
                    {r.b.instance}.{r.b.datum}
                  </td>
                  <td>{version.readiness?.relations[r.key] ? <ReadinessChip state={version.readiness.relations[r.key].state} questions={version.readiness.relations[r.key].questions} /> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ─── facts ────────────────────────────────────────────────────────────────────────────────────────

export function Evidence({ ev, onViewChunk }: { ev: SpecEvidence[]; onViewChunk: ViewChunk }) {
  const { t } = useI18n();
  if (!ev.length) return <span className="faint">—</span>;
  return (
    <div className="spec-ev">
      {ev.map((e, i) => (
        <div key={i} className="spec-ev-line">
          <span className="mono">{e.source}</span>
          <span className="loc">{e.locator}</span>
          {e.verified ? (
            <CheckCircle2 className="inline-ico" aria-label={t('spec.fact.verified')} />
          ) : (
            <CircleAlert className="inline-ico" aria-label={t('spec.fact.unverified')} />
          )}
          {e.quote ? <q className="spec-quote">{e.quote}</q> : null}
          {e.entity ? <span className="mono small">#{e.entity}</span> : null}
          {e.chunk ? (
            <button type="button" className="linkish small" onClick={() => onViewChunk(e.chunk!, e.quote)}>
              {t('spec.chunk.view')}
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function FactsSection({ version, onViewChunk }: { version: SpecVersion; onViewChunk: ViewChunk }) {
  const { t } = useI18n();
  const c = content(version);
  if (!c) return null;
  const demoted = version.validation?.demoted?.length ?? 0;
  return (
    <Section id="facts" title={t('spec.sec.facts')} count={c.facts.length}>
      {demoted ? <p className="small faint">{t('spec.page.validation', { n: demoted })}</p> : null}
      {!c.facts.length ? (
        <p className="faint small">{t('spec.fact.none')}</p>
      ) : (
        <div className="table-wrap">
          <table className="t dense spec-facts">
            <thead>
              <tr>
                <th>ID</th>
                <th>{t('spec.fact.col.subject')}</th>
                <th>{t('spec.fact.col.value')}</th>
                <th>{t('spec.fact.col.status')}</th>
                <th>{t('spec.fact.col.evidence')}</th>
              </tr>
            </thead>
            <tbody>
              {c.facts.map((f: SpecFact) => (
                <tr key={f.id}>
                  <td className="mono nowrap">{f.id}</td>
                  <td className="mono small">{f.subject}</td>
                  <td className="mono nowrap">{fmtValue(f.value, f.unit)}</td>
                  <td>
                    <FactStatusChip status={f.status} />
                  </td>
                  <td className="ev">
                    <Evidence ev={f.evidence} onViewChunk={onViewChunk} />
                    {f.derivation ? <div className="faint small">{t('spec.fact.derivation', { d: f.derivation })}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ─── questions ────────────────────────────────────────────────────────────────────────────────────

export function QuestionsSection({ version, canAnswer, isLatest, onAnswered }: { version: SpecVersion; canAnswer: boolean; isLatest: boolean; onAnswered: (v: SpecVersion) => void }) {
  const { t, fmt } = useI18n();
  const c = content(version);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!c) return null;
  const open = c.questions.filter((q) => !q.answer);
  const filled = Object.entries(drafts).filter(([id, v]) => v.trim() && open.some((q) => q.id === id));
  const editable = canAnswer && isLatest && (version.status === 'ready' || version.status === 'needs_answers');

  const submit = async () => {
    if (!filled.length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await answerSpecQuestions(version.id, filled.map(([question_id, answer]) => ({ question_id, answer: answer.trim() })));
      setDrafts({});
      onAnswered(out);
      notifyTodosChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section id="questions" title={t('spec.sec.questions')} count={c.questions.length}>
      {!c.questions.length ? (
        <p className="faint small">{t('spec.q.none')}</p>
      ) : (
        <form
          className="spec-list"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          {!canAnswer && open.length ? <p className="faint small">{t('spec.q.readOnly')}</p> : null}
          {canAnswer && !isLatest && open.length ? <p className="faint small">{t('spec.q.notCurrent')}</p> : null}
          {c.questions.map((q) => (
            <div key={q.id} data-question-id={q.id} tabIndex={-1} className={`spec-q ${q.answer ? '' : 'open'}`}>
              <div className="spec-q-head">
                <span className="mono strong">{q.id}</span>
                <span className="mono small">{q.subject}</span>
                {q.origin && q.origin !== 'model' ? <span className="faint small">{tk(t, `spec.q.origin.${q.origin}`, q.origin)}</span> : null}
                <span className="faint small">{t('spec.q.affects', { keys: q.affects.join(', ') })}</span>
              </div>
              <div>{q.question}</div>
              {q.answer ? (
                <div className="small">
                  <CheckCircle2 className="inline-ico" aria-hidden /> <span className="strong">{q.answer.answer}</span>{' '}
                  <span className="faint">
                    · {t('spec.q.answered', { who: q.answer.by_name || q.answer.by.slice(0, 8), at: fmt.dateTime(q.answer.at) })} · {q.answer.source}
                  </span>
                </div>
              ) : editable ? (
                <>
                  {q.candidates.length ? (
                    <div className="spec-q-cands">
                      <span className="faint small">{t('spec.q.candidates')}</span>
                      {q.candidates.map((cand, i) => (
                        <button key={i} type="button" className="btn sm" onClick={() => setDrafts((d) => ({ ...d, [q.id]: String(cand) }))}>
                          {String(cand)}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <LimitedInput
                    className="input"
                    aria-label={t('spec.q.answerLabel', { id: q.id })}
                    placeholder={t('spec.q.answerPlaceholder')}
                    value={drafts[q.id] ?? ''}
                    limit="description"
                    onChange={(e) => setDrafts((d) => ({ ...d, [q.id]: e.target.value }))}
                  />
                </>
              ) : null}
            </div>
          ))}
          {error ? <ErrorNotice error={error} /> : null}
          {editable && open.length ? (
            <div className="row">
              <button type="submit" className="btn primary" disabled={busy || !filled.length}>
                <Send aria-hidden /> {t('spec.q.submit', { n: filled.length })}
              </button>
            </div>
          ) : null}
        </form>
      )}
    </Section>
  );
}

// ─── assumptions / conflicts / notes ──────────────────────────────────────────────────────────────

/** The spec's AI decisions in the shared decision view (generation supplement D); on the latest Turbo version a person
 * with workflow rights may correct one, which makes a new version through the answer flow. */
export function AssumptionsSection({
  version,
  canCorrect,
  isLatest,
  onCorrected,
}: {
  version: SpecVersion;
  canCorrect?: boolean;
  isLatest?: boolean;
  onCorrected?: (v: SpecVersion) => void;
}) {
  const { t } = useI18n();
  const c = content(version);
  if (!c) return null;
  const editable = !!canCorrect && !!isLatest && version.execution_policy === 'turbo' && (version.status === 'ready' || version.status === 'needs_answers');
  // decisions the server did not keep: a value the source states stays as stated; a person's answer replaced one
  const dropped = droppedDecisions(version.planning?.merge?.dropped_decisions, version.validation?.dropped_assumptions);
  return (
    <Section id="assumptions" title={t('spec.sec.assumptions')} count={c.assumptions.length}>
      {!c.assumptions.length ? (
        <p className="faint small">{t('spec.a.none')}</p>
      ) : (
        <AssumptionList
          items={c.assumptions}
          bare
          actions={editable ? (a) => (a.author !== 'person' && a.id ? <DecisionCorrect versionId={version.id} id={a.id} onDone={onCorrected} /> : null) : undefined}
        />
      )}
      {dropped.length ? (
        <div className="stack-sm">
          <span className="tk-label">{t('tb.decision.dropped.title')}</span>
          <ul className="tb-assumptions">
            {dropped.map((d) => (
              <li key={`${d.reason}#${d.subject}`} data-state="replaced">
                <span className="tb-decision-line">{droppedLine(d, t)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}

export function ConflictsSection({ version }: { version: SpecVersion }) {
  const { t } = useI18n();
  const c = content(version);
  if (!c) return null;
  const resolution = (r: Record<string, unknown> | null) => {
    if (!r) return <Chip tone="warn" className="spec-chip-sm">{t('spec.c.open')}</Chip>;
    if (typeof r.assumption === 'string') return <span className="small">{t('spec.c.byAssumption', { id: r.assumption })}</span>;
    if (typeof r.answer === 'string') return <span className="small">{t('spec.c.byAnswer', { id: r.answer })}{r.value !== undefined ? ` = ${fmtValue(r.value)}` : ''}</span>;
    return <span className="mono small">{JSON.stringify(r)}</span>;
  };
  return (
    <Section id="conflicts" title={t('spec.sec.conflicts')} count={c.conflicts.length}>
      {!c.conflicts.length ? (
        <p className="faint small">{t('spec.c.none')}</p>
      ) : (
        <div className="table-wrap">
          <table className="t dense">
            <tbody>
              {c.conflicts.map((x) => (
                <tr key={x.id}>
                  <td className="mono">{x.id}</td>
                  <td className="mono small">{x.subject}</td>
                  <td>
                    {x.values.map((val, i) => (
                      <Fragment key={i}>
                        {i ? <span className="faint"> / </span> : null}
                        <span className="mono">{fmtValue(val.value, val.unit)}</span> <span className="faint small">({val.source})</span>
                      </Fragment>
                    ))}
                  </td>
                  <td>
                    <span className="faint small">{t('spec.c.resolution')}: </span>
                    {resolution(x.resolution)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

export function NotesSection({ version, onViewChunk }: { version: SpecVersion; onViewChunk: ViewChunk }) {
  const { t } = useI18n();
  const c = content(version);
  if (!c) return null;
  return (
    <Section id="notes" title={t('spec.sec.notes')}>
      <div className="stack-sm">
        {!c.manufacturing_notes.length ? <p className="faint small">{t('spec.n.none')}</p> : null}
        {c.manufacturing_notes.map((n) => (
          <div key={n.id} className="stack-sm">
            <div>
              <span className="mono strong">{n.id}</span> {n.subject ? <span className="mono small">{n.subject}</span> : null} {n.text}
            </div>
            {n.evidence?.length ? <Evidence ev={n.evidence} onViewChunk={onViewChunk} /> : null}
          </div>
        ))}
        <div className="row">
          {!c.deliverables.length ? <span className="faint small">{t('spec.d.none')}</span> : null}
          {c.deliverables.map((d) => (
            <Chip key={d.kind} tone="measure" className="spec-chip-sm">
              {d.kind}
              {d.required ? '' : ` · ${t('spec.d.optional')}`}
            </Chip>
          ))}
        </div>
      </div>
    </Section>
  );
}

export function HistorySection({ spec, currentId }: { spec: EngineeringSpec; currentId: string }) {
  const { t, fmt } = useI18n();
  const versions = spec.versions ?? [];
  return (
    <Section id="history" title={t('spec.sec.history')} count={versions.length}>
      <div className="table-wrap">
        <table className="t dense">
          <thead>
            <tr>
              <th>{t('spec.h.col.version')}</th>
              <th>{t('spec.h.col.status')}</th>
              <th>{t('spec.h.col.counts')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.id}>
                <td>
                  <Link to={specPath(spec.id, v.id)} className="mono">
                    {t('spec.page.versionOf', { n: v.version_no, origin: tk(t, `spec.origin.${v.origin}`, v.origin) })}
                  </Link>
                  {v.id === currentId ? <span className="faint small"> · {t('spec.h.current')}</span> : null}
                </td>
                <td>
                  <span className="spec-head-chips">
                    <SpecStatusChip status={v.status} />
                    <PolicyChip policy={v.execution_policy} />
                    {v.simulated ? <SimulatedChip /> : null}
                  </span>
                </td>
                <td className="small">{t('spec.list.counts', { d: v.counts.definitions, i: v.counts.instances, q: v.counts.questions_open })}</td>
                <td className="small faint">{fmt.dateTime(v.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// ─── chunk text dialog ────────────────────────────────────────────────────────────────────────────

export function ChunkDialog({ versionId, seq, quote, onClose }: { versionId: string; seq: number; quote?: string | null; onClose: () => void }) {
  const { t } = useI18n();
  const res = useResource((signal) => getSpecChunk(versionId, seq, { signal }), [versionId, seq]);
  const chunk = res.data;
  let body: ReactNode = <Spinner label={t('common.loading')} />;
  if (res.error) body = <ErrorNotice error={res.error} onRetry={res.reload} />;
  else if (chunk) {
    if (chunk.kind === 'attachment') body = <p className="faint small">{t('spec.chunk.attachment')}</p>;
    else {
      const at = quote ? chunk.text.indexOf(quote) : -1;
      body = (
        <pre className="spec-chunk-text">
          {at >= 0 ? (
            <>
              {chunk.text.slice(0, at)}
              <mark aria-label={t('spec.chunk.quote')}>{quote}</mark>
              {chunk.text.slice(at + (quote?.length ?? 0))}
            </>
          ) : (
            chunk.text
          )}
        </pre>
      );
    }
  }
  return (
    <Dialog title={t('spec.chunk.title', { seq, locator: chunk?.locator ?? '' })} size="lg" onClose={onClose} footer={<button type="button" className="btn" onClick={onClose}>{t('common.close')}</button>}>
      {body}
    </Dialog>
  );
}
