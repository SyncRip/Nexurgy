// Requirements and checks of one saved revision (model tab): the accepted
// requirement list (id, kind, key values, basis, evidence) next to the
// independent check items of its STEP, unrequested features and note
// coverage; for imports the STEP source audit, the PROFILE import report and
// the drawing source. Business acceptance is shown apart, never inferred.

import { CheckCircle2, CircleDashed, FileInput, ListChecks, ScanSearch, XCircle } from 'lucide-react';
import type { CheckItem, ModelRevision, Requirement } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { StepAuditFacts } from './CadErrorDetails';
import { evidenceParts, itemsByRequirement, n, requirementBasis, requirementFacts, type Fact } from './status';
import { AcceptanceChip, dyn, GeomCheckChip } from './Verdicts';
import type { ReconstructSource } from '../../api/types';
import { ReconstructSourceView } from './Verdicts';
import { ArtifactVerificationPanel } from '../turbo/ArtifactVerification';
import './cad.css';

function ItemLine({ item }: { item: CheckItem }) {
  const { t } = useI18n();
  const tone = item.status === 'passed' ? 'ok' : item.status === 'failed' ? 'err' : 'pending';
  return (
    <div className={`cad-item ${tone}`}>
      {tone === 'ok' ? <CheckCircle2 aria-hidden /> : tone === 'err' ? <XCircle aria-hidden /> : <CircleDashed aria-hidden />}
      <span>
        <span className="strong">{dyn(t, `cad.req.item.${item.status}`, String(item.status))}</span>
        {typeof item.instance === 'number' ? <span className="mono faint"> {t('cad.req.instance', { n: item.instance + 1 })}</span> : null}
        {item.message ? <span className="faint"> · {item.message}</span> : null}
        {item.failures?.length ? <span className="cad-quote">{item.failures.slice(0, 3).join('; ')}</span> : null}
      </span>
    </div>
  );
}

function FactList({ facts }: { facts: Fact[] }) {
  const { t } = useI18n();
  return (
    <span className="cad-facts">
      {facts.map((f, i) => (
        <span key={i}>
          <span className="faint">{t(`cad.req.fact.${f.key}`)}</span>{' '}
          <span className="mono">{f.bool !== undefined ? (f.bool ? t('cad.req.yes') : t('cad.req.no')) : f.key === 'category' ? dyn(t, `cad.kind.${f.value}`, f.value) : f.value}</span>
        </span>
      ))}
    </span>
  );
}

function RequirementRow({ r, items }: { r: Requirement; items: CheckItem[] }) {
  const { t } = useI18n();
  const basis = requirementBasis(r);
  const fields = basis === 'assumed' ? r.assumed ?? [] : basis === 'derived' ? r.derived ?? [] : [];
  const { quotes, ids } = evidenceParts(r.evidence);
  return (
    <tr>
      <td className="mono strong">{r.id}</td>
      <td>
        {dyn(t, `cad.kind.${r.kind}`, r.kind)}
        <div className="faint">{r.label}</div>
      </td>
      <td>
        <FactList facts={requirementFacts(r)} />
      </td>
      <td>
        <Chip tone={basis === 'assumed' ? 'warn' : basis === 'derived' ? 'measure' : 'ok'} className="sm">
          {fields.length ? t('cad.req.basisFields', { basis: t(`cad.req.basis.${basis}`), fields: fields.join(', ') }) : t(`cad.req.basis.${basis}`)}
        </Chip>
        {basis === 'derived' && r.derivation ? <span className="cad-quote faint">{r.derivation}</span> : null}
      </td>
      <td>
        {!quotes.length && !ids.length ? <span className="faint">{t('cad.req.noEvidence')}</span> : null}
        {quotes.map((e, i) => (
          <span key={`q${i}`} className="cad-quote">
            <span className="faint">{t(`cad.req.evidence.${e.source}`)}:</span> “{e.ref}”
          </span>
        ))}
        {ids.length > 0 && (
          <span className="cad-quote">
            {ids.map((e, i) => (
              <span key={`i${i}`}>
                {i > 0 ? ' · ' : ''}
                <span className="faint">{t(`cad.req.evidence.${e.source}`)}</span> <span className="mono">{e.ref}</span>
                {e.value ? <span className="mono"> = {e.value}</span> : null}
              </span>
            ))}
          </span>
        )}
      </td>
      <td>{r.kind === 'note' ? <span className="faint">{t('cad.req.notGeometric')}</span> : items.length ? items.map((it, i) => <ItemLine key={i} item={it} />) : <span className="faint">{t('cad.req.noItems')}</span>}</td>
    </tr>
  );
}

export function RevisionChecksPanel({ rev, dirty }: { rev: ModelRevision; dirty?: boolean }) {
  const { t } = useI18n();
  const checks = rev.checks ?? null;
  const report = checks?.conformance ?? null;
  const reqs = rev.requirements ?? null;
  const byReq = itemsByRequirement(report?.items);
  const orphan = byReq.get('') ?? [];
  const audit = checks?.step_audit ?? null;
  const profile = checks?.profile ?? null;
  const profileCheck = checks?.profile_check ?? null;
  const source = checks?.source ?? null;
  const evidence = checks?.evidence ?? null;
  const conflicts = Array.isArray(evidence?.scale_classification?.conflicts) ? evidence!.scale_classification!.conflicts!.length : undefined;
  const hasContract = reqs !== null || !!report;
  const notes = report?.notes;

  return (
    <section className="ws-sec stack-sm" aria-label={t('cad.req.title')}>
      <h4>
        <ListChecks aria-hidden className="inline-ico" /> {t('cad.req.title')}
      </h4>
      {dirty && <p className="faint small">{t('cad.req.dirty')}</p>}
      <div className="cad-chips">
        <GeomCheckChip status={rev.check_status} showNone />
        <AcceptanceChip />
      </div>
      {/* W7: a Turbo revision is not verified; a standard check on these exact bytes is a new record. */}
      <ArtifactVerificationPanel key={rev.id} kind="revision" id={rev.id} verification={rev.verification} canCheck={!!rev.requirements && !rev.mesh_only} />
      {hasContract ? (
        <>
          <p className="faint small">{t('cad.req.sub', { rev: rev.label })}</p>
          {reqs && reqs.length > 0 && (
            <div className="cad-req-wrap" tabIndex={0} role="region" aria-label={t('cad.req.title')}>
              <table className="cad-req">
                <thead>
                  <tr>
                    <th>{t('cad.req.colId')}</th>
                    <th>{t('cad.req.colKind')}</th>
                    <th>{t('cad.req.colValues')}</th>
                    <th>{t('cad.req.colBasis')}</th>
                    <th>{t('cad.req.colEvidence')}</th>
                    <th>{t('cad.req.colCheck')}</th>
                  </tr>
                </thead>
                <tbody>
                  {reqs.map((r) => (
                    <RequirementRow key={r.id} r={r} items={byReq.get(r.id) ?? []} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {orphan.length > 0 && (
            <div className="small">
              {orphan.map((it, i) => (
                <ItemLine key={i} item={it} />
              ))}
            </div>
          )}
          {!!report?.unrequested?.length && (
            <div className="small">
              <div className="cad-sub">{t('cad.req.unrequested')}</div>
              <ul className="cad-list">
                {report.unrequested.map((u, i) => (
                  <li key={i}>
                    Ø{n(u.diameter)} · {u.message}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {notes && (notes.covered.length > 0 || notes.uncovered.length > 0 || notes.unknown.length > 0) && (
            <div className="small">
              <div className="cad-sub">{t('cad.req.notes')}</div>
              <ul className="cad-list">
                {notes.covered.map((x, i) => (
                  <li key={`c${i}`}>
                    <Chip tone="ok" className="sm">
                      {t('cad.sum.covered')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                  </li>
                ))}
                {notes.uncovered.map((x, i) => (
                  <li key={`u${i}`}>
                    <Chip tone="warn" className="sm">
                      {t('cad.sum.uncovered')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                    {x.reason ? <span className="faint"> · {x.reason}</span> : null}
                  </li>
                ))}
                {notes.unknown.map((x, i) => (
                  <li key={`k${i}`}>
                    <Chip tone="pending" className="sm">
                      {t('cad.sum.unknown')}
                    </Chip>{' '}
                    <span className="mono">{x.requirement_id}</span> {x.text}
                    {x.reason ? <span className="faint"> · {x.reason}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report && (
            <p className="faint small mono">
              {t('cad.sum.validator', { v: report.validator_version })} · {t('cad.req.sha', { sha: report.requirements_sha256?.slice(0, 10) ?? '—', step: report.step_sha256?.slice(0, 10) ?? '—' })}
            </p>
          )}
        </>
      ) : (
        !audit && !profile && <p className="faint small">{t('cad.req.none')}</p>
      )}

      {audit && (
        <div className="cad-block">
          <div className="cad-block-h">
            <ScanSearch aria-hidden /> {t('cad.audit.title')}
          </div>
          <StepAuditFacts audit={audit} />
        </div>
      )}

      {profile && (
        <div className="cad-block">
          <div className="cad-block-h">
            <FileInput aria-hidden /> {t('cad.profile.title')}
            {profileCheck && (
              <Chip tone={profileCheck.status === 'passed' ? 'ok' : 'err'} icon={profileCheck.status === 'passed' ? <CheckCircle2 aria-hidden /> : <XCircle aria-hidden />} className="sm">
                {t('cad.profile.check')}: {t(`cad.req.item.${profileCheck.status}`)}
              </Chip>
            )}
          </div>
          <p className="small">
            {t('cad.profile.facts', { layer: profile.layer, thickness: n(profile.thickness_mm), scale: n(profile.scale), z: n(profile.base_z ?? 0) })}
            {profile.units?.used ? ` · ${t('cad.profile.units', { units: profile.units.used, source: profile.units.source ?? '—' })}` : ''}
          </p>
          <p className="faint small">{t('cad.profile.entities', { used: profile.used?.length ?? 0, rejected: profile.rejected?.length ?? 0, ignored: profile.ignored?.length ?? 0 })}</p>
        </div>
      )}

      {source && <ReconstructSourceView source={sourceBrief(source, evidence, conflicts)} />}
    </section>
  );
}

/** revision.checks.source (+ evidence) in the shape of a reconstruct source brief. */
function sourceBrief(s: NonNullable<ModelRevision['checks']>['source'], evidence: NonNullable<ModelRevision['checks']>['evidence'], conflicts: number | undefined): ReconstructSource | null {
  if (!s) return null;
  return {
    format: s.format,
    name: s.name,
    file_version_id: s.file_version_id,
    drawing_id: s.drawing_id,
    source_sha256: s.source_sha256,
    dxf_sha256: s.dxf_sha256,
    dxf_role: s.dxf_role,
    dwg: s.dwg
      ? {
          version: s.dwg.source?.version ?? null,
          version_status: s.dwg.version_status ?? null,
          converter: s.dwg.converter ?? null,
          losses: Array.isArray(s.dwg.losses) ? s.dwg.losses.length : 0,
        }
      : undefined,
    frame_id: evidence?.frame_id ?? null,
    frame: evidence?.frame ?? null,
    conflicts,
  };
}
