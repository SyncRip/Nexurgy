// Context inspector (design §7.3, §8.2, U16): what the next request will use
// (target + references), pinned constraints, what the last request actually
// used (the server's context build: layer, included or why not, estimated
// tokens labelled as estimates, real usage only from the provider or
// "unknown"), the history summary status and references used in history.

import { AlertTriangle, CheckCircle2, MinusCircle, Pin, Plus, Scissors, X } from 'lucide-react';
import { useState } from 'react';
import { getAiConversationContext, getAiOperation, type AiContextItem, type AiConversationDetail } from '../../api/assistant';
import { Chip } from '../../components/ui/Badges';
import { ErrorNotice, Skeleton } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { ReadStateChip } from '../spec/common';
import { useAssistantSession } from './AssistantProvider';
import { AsError, MaterialPlacements, modelLabelText, OverBudget, RefChip, tk, usageText } from './common';
import { historyRefs, MAX_CONSTRAINT_TEXT } from './model';

const MAX_CONSTRAINTS = 20;

export function ContextInspector({ detail }: { detail: AiConversationDetail }) {
  const { t } = useI18n();
  const conv = detail.conversation;
  const composer = detail.composer;
  const target = composer.target_key ? composer.references.find((r) => r.key === composer.target_key) ?? null : null;
  const supporting = composer.references.filter((r) => r.key !== target?.key);
  const history = historyRefs(detail.messages);
  // Reload the server's context view when an operation was added or changed state.
  const opsKey = detail.operations.map((o) => `${o.id}:${o.status}`).join(',');
  const ctx = useResource((signal) => getAiConversationContext(conv.id, { signal }), [conv.id, opsKey, conv.version]);

  return (
    <div className="as-inspector">
      <section className="as-insp-sec">
        <h3>{t('as.insp.next')}</h3>
        <p className="as-insp-hint">{t('as.insp.nextHint')}</p>
        <div className="as-insp-refs">
          {target ? <RefChip refItem={target} status={composer.ref_status[target.key]} target /> : <span className="faint small">{t('as.target.none')}</span>}
          {supporting.map((r) => (
            <RefChip key={r.key} refItem={r} status={composer.ref_status[r.key]} />
          ))}
        </div>
      </section>

      <Constraints detail={detail} />

      <section className="as-insp-sec">
        <h3>{t('as.insp.lastBuild')}</h3>
        {ctx.error && !ctx.data ? (
          <ErrorNotice error={ctx.error} onRetry={ctx.reload} />
        ) : !ctx.data ? (
          <Skeleton h={60} />
        ) : !ctx.data.last_build ? (
          <p className="faint small">{t('as.insp.noBuild')}</p>
        ) : (
          <BuildView build={ctx.data.last_build} />
        )}
      </section>

      <section className="as-insp-sec">
        <h3>{t('as.insp.summary')}</h3>
        {ctx.data?.summary ? (
          <div className="small stack-sm">
            <p>{t('as.insp.summaryThrough', { seq: ctx.data.summary.through_seq })}</p>
            <ul className="as-plain">
              <li>{t('as.insp.summaryCounts', { goals: ctx.data.summary.goals.length, constraints: ctx.data.summary.constraints.length, decisions: ctx.data.summary.decisions.length, open: ctx.data.summary.open_questions.length })}</li>
              {ctx.data.summary.sources.length > 0 && <li className="faint">{t('as.insp.summarySources', { seqs: ctx.data.summary.sources.slice(0, 12).map((s) => `#${s}`).join(' ') })}</li>}
            </ul>
            <p className="faint">{t('as.insp.summaryNote')}</p>
          </div>
        ) : (
          <p className="faint small">{conv.summary_status ? t('as.insp.summaryThrough', { seq: conv.summary_status.through_seq }) : t('as.insp.noSummary')}</p>
        )}
      </section>

      <section className="as-insp-sec">
        <h3>{t('as.insp.history')}</h3>
        {history.length ? (
          <ul className="as-insp-list">
            {history.map((h) => (
              <li key={h.ref.key}>
                <RefChip refItem={h.ref} status={h.status} target={h.asTarget} compact />
                <span className="faint small mono">#{h.seq}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="faint small">{t('as.insp.noHistory')}</p>
        )}
      </section>
    </div>
  );
}

function Constraints({ detail }: { detail: AiConversationDetail }) {
  const { t } = useI18n();
  const { store } = useAssistantSession();
  const conv = detail.conversation;
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const list = conv.constraints;
  const save = async (next: { id?: string; text: string; source_seq?: number | null }[]) => {
    setBusy(true);
    setError(null);
    try {
      await store.patch({ constraints: next, expected_version: conv.version });
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const current = list.map((c) => ({ id: c.id, text: c.text, source_seq: c.source_seq }));
  return (
    <section className="as-insp-sec">
      <h3>
        {t('as.insp.constraints')} <span className="faint mono">{list.length}/{MAX_CONSTRAINTS}</span>
      </h3>
      <p className="as-insp-hint">{t('as.insp.constraintsHint')}</p>
      {list.length ? (
        <ul className="as-constraints">
          {list.map((c) => (
            <li key={c.id}>
              <Pin aria-hidden />
              <span className="grow">
                {c.text}
                {c.source_seq ? <span className="faint mono"> · #{c.source_seq}</span> : null}
              </span>
              {!conv.archived && (
                <button type="button" className="as-icon-btn" disabled={busy} onClick={() => void save(current.filter((x) => x.id !== c.id))} aria-label={t('as.insp.removeConstraint', { text: c.text.slice(0, 40) })}>
                  <X />
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="faint small">{t('as.insp.noConstraints')}</p>
      )}
      {!conv.archived && list.length < MAX_CONSTRAINTS && (
        <form
          className="as-constraint-add"
          onSubmit={(e) => {
            e.preventDefault();
            const v = text.trim();
            if (v) void save([...current, { text: v.slice(0, MAX_CONSTRAINT_TEXT) }]).then((ok) => ok && setText(''));
          }}
        >
          <input
            className="input sm"
            value={text}
            maxLength={MAX_CONSTRAINT_TEXT}
            onChange={(e) => setText(e.target.value)}
            placeholder={t('as.insp.addConstraintPh')}
            aria-label={t('as.insp.addConstraint')}
            title={t('as.insp.constraintMax', { n: MAX_CONSTRAINT_TEXT })}
          />
          <button type="submit" className="btn sm" disabled={busy || !text.trim()}>
            <Plus /> {t('common.add')}
          </button>
        </form>
      )}
      <AsError error={error} />
    </section>
  );
}

function ItemRow({ it }: { it: AiContextItem }) {
  const { t } = useI18n();
  const clipped = it.truncated === true || it.reason === 'clipped';
  return (
    <li className={`as-build-item ${it.included ? 'in' : 'out'}`}>
      {it.included ? <CheckCircle2 aria-hidden className="ok" /> : <MinusCircle aria-hidden />}
      <span className="grow">
        <span className="as-build-label">{it.label}</span>
        {it.read ? (
          <>
            {' '}
            <ReadStateChip read={it.read} ranges={it.ranges} />
          </>
        ) : null}
        {it.method ? <span className="faint small"> · {tk(t, `as.material.short.${it.method}`, it.method)}</span> : null}
        {clipped && (
          <>
            {' '}
            <Chip tone="stale" icon={<Scissors aria-hidden />} className="sm as-build-tag" title={t('as.insp.clippedHint')}>
              {t('as.reason.clipped')}
            </Chip>
          </>
        )}
        <span className="faint small">
          {' '}
          · {tk(t, `as.layer.${it.layer}`, it.layer)} · {tk(t, `as.source.${it.source}`, it.source)}
          {!it.included && it.reason && it.reason !== 'clipped' ? ` · ${t('as.insp.notIncluded', { why: tk(t, `as.reason.${it.reason}`, it.reason) })}` : ''}
        </span>
      </span>
      <span className="mono small faint as-build-est" title={t('as.budget.estimatedHint')}>
        {t('as.budget.tokensEst', { n: it.tokens_est })}
        {typeof it.chars === 'number' ? ` · ${t('as.budget.chars', { n: it.chars })}` : ''}
      </span>
    </li>
  );
}

function BuildView({ build }: { build: NonNullable<Awaited<ReturnType<typeof getAiConversationContext>>['last_build']> }) {
  const { t, fmt } = useI18n();
  const b = build.budget;
  const u = build.usage;
  // The conversation view carries no item list; the operation's own build does.
  const needItems = !build.items?.length && !!build.operation_id;
  const opRes = useResource((signal) => getAiOperation(build.operation_id, { signal }), [build.operation_id, build.id], { enabled: needItems });
  const items = build.items?.length ? build.items : opRes.data?.context_build?.items ?? [];
  const layers: AiContextItem['layer'][] = ['required', 'memory', 'retrieved'];
  return (
    <div className="stack-sm small">
      <div className="row">
        {build.ok ? (
          <Chip tone="measure" icon={<CheckCircle2 aria-hidden />}>
            {t('as.insp.buildOk')}
          </Chip>
        ) : (
          <Chip tone="err" icon={<AlertTriangle aria-hidden />}>
            {t('as.insp.buildFailed')}
          </Chip>
        )}
        {build.model_label && <span className="mono faint">{modelLabelText(t, build.model_label)}</span>}
        {build.created_at && <span className="faint">{fmt.relative(build.created_at)}</span>}
      </div>
      {b && (
        <dl className="as-budget">
          <div>
            <dt>{t('as.budget.available')}</dt>
            <dd className="mono">{t('as.budget.tokensEst', { n: b.available })}</dd>
          </div>
          <div>
            <dt>{t('as.budget.required')}</dt>
            <dd className="mono">{t('as.budget.tokensEst', { n: b.required_est })}</dd>
          </div>
          <div>
            <dt>{t('as.budget.used')}</dt>
            <dd className="mono">{t('as.budget.tokensEst', { n: b.used_est })}</dd>
          </div>
          <div>
            <dt>{t('as.budget.limit')}</dt>
            <dd className="mono">
              {b.input_limit} · {tk(t, `as.budget.src.${b.limit_source}`, b.limit_source)}
            </dd>
          </div>
        </dl>
      )}
      <MaterialPlacements items={b?.materials} />
      <p className="faint">{t('as.budget.estimatedHint')}</p>
      <p>
        <span className="strong">{t('as.usage.title')}</span>{' '}
        {usageText(t, u)}
        {u && u.calls ? <span className="faint"> · {t('as.usage.calls', { n: u.calls })}</span> : null}
      </p>
      {build.error && (
        <div className="callout err slim" role="status">
          <AlertTriangle aria-hidden />
          <div className="grow stack-sm">
            <span>
              {tk(t, `as.err.${build.error.code}`, build.error.message)} <code className="error-code">{t('common.errorCode', { code: build.error.code })}</code>
            </span>
            {build.error.code === 'context_over_budget' && <OverBudget details={build.error.details} />}
          </div>
        </div>
      )}
      {items.length ? (
        layers.map((layer) => {
          const rows = items.filter((i) => i.layer === layer);
          if (!rows.length) return null;
          return (
            <div key={layer}>
              <div className="as-build-layer">{tk(t, `as.layer.${layer}`, layer)}</div>
              <ul className="as-build-items">
                {rows.map((it, i) => (
                  <ItemRow key={`${it.label}-${i}`} it={it} />
                ))}
              </ul>
            </div>
          );
        })
      ) : needItems && !opRes.data && !opRes.error ? (
        <Skeleton h={40} />
      ) : (
        <p className="faint">{t('as.insp.noItems')}</p>
      )}
      {build.summary_through_seq ? <p className="faint">{t('as.insp.usedSummary', { seq: build.summary_through_seq })}</p> : null}
    </div>
  );
}
