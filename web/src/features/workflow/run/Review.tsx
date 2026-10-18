// Pending human actions of a run: approvals (human_review), clarification
// questions (model_3d) and manual drawing intake (drawing_intake). The server
// checks roles and subject versions; this UI only explains and submits.

import { Check, Download, Hand, MessageCircleQuestionMark, Plus, RefreshCw, ScanText, Trash, TriangleAlert, UserCheck, X } from 'lucide-react';
import { useId, useMemo, useState } from 'react';
import { isApiError } from '../../../api/client';
import { downloadBlob } from '../../../api/blobs';
import { createManualIntake, decideRunNode } from '../../../api/endpoints';
import type { ApprovalStage, NodeRun, StaleReason, WorkflowRun } from '../../../api/types';
import { ACTION_ROLES, STAGE_ACTION } from '../../../auth/permissions';
import { TagInput } from '../../../components/ui/Form';
import { ErrorNotice } from '../../../components/ui/States';
import { useToast } from '../../../components/ui/Toast';
import { hasKey } from '../../../i18n/core';
import type { MessageKey } from '../../../i18n/en';
import { useI18n } from '../../../i18n/I18nProvider';
import { KNOWN_MATERIALS } from '../../../lib/catalog';
import { stageLabel } from '../labels';
import type { Lookups } from '../lookups';
import { PortValue } from './Artifacts';
import { LimitedTextarea } from '../../../components/ui/LimitedText';

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);

function DecisionError({ error, onReload }: { error: unknown; onReload: () => void }) {
  const { t, lang } = useI18n();
  if (!error) return null;
  if (isApiError(error) && error.code === 'subject_stale') {
    const reasons = ((error.details as { reasons?: StaleReason[] }).reasons ?? []) as StaleReason[];
    return (
      <div className="callout err" role="alert">
        <TriangleAlert aria-hidden />
        <div className="stack-sm">
          <span>{t('wf.review.stale')}</span>
          {reasons.length > 0 && (
            <ul className="wf-plain-list">
              {reasons.map((r, i) => (
                <li key={i}>{lang === 'zh' ? r.message_zh || r.message_en : r.message_en || r.message_zh}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    );
  }
  if (isApiError(error) && (error.code === 'subject_changed' || error.code === 'not_waiting')) {
    return (
      <div className="callout warn" role="alert">
        <TriangleAlert aria-hidden />
        <span className="grow">{error.code === 'subject_changed' ? t('wf.review.changed') : t('error.not_waiting')}</span>
        <button type="button" className="btn sm" onClick={onReload}>
          <RefreshCw /> {t('wf.review.reload')}
        </button>
      </div>
    );
  }
  return <ErrorNotice error={error} overrides={{ forbidden: 'wf.review.forbidden', answers_required: 'wf.apiErr.answers_required', intake_required: 'wf.apiErr.intake_required' }} />;
}

function RoleNote({ stage }: { stage: ApprovalStage | 'design' }) {
  const { t } = useI18n();
  const roles = ACTION_ROLES[STAGE_ACTION[stage]].map((r) => t(`role.${r}`)).join(' / ');
  return (
    <p className="small muted">
      <Hand aria-hidden className="inline-ico" /> {t('wf.review.cannotDecide', { roles })}
    </p>
  );
}

// ─── approval ──────────────────────────────────────────────────────────────

function ApprovalForm({ run, node, onRun, onReload }: { run: WorkflowRun; node: NodeRun; onRun: (r: WorkflowRun) => void; onReload: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const review = node.review!;
  const stage = (review.stage ?? 'design') as ApprovalStage;
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState<'approved' | 'rejected' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const cid = useId();
  const decide = async (decision: 'approved' | 'rejected') => {
    setBusy(decision);
    setError(null);
    try {
      const res = await decideRunNode(run.id, node.node_id, { decision, comment: comment.trim() });
      onRun(res);
      toast(decision === 'approved' ? t('wf.review.approvedToast') : t('wf.review.rejectedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="stack-sm">
      <div className="row">
        <span className="chip warn">
          <UserCheck aria-hidden /> {t('wf.review.stageReview', { stage: stageLabel(t, stage) })}
        </span>
      </div>
      {review.instructions && <p className="wf-instructions">{review.instructions}</p>}
      {review.subject && (
        <div className="wf-review-subject">
          <span className="wf-insp-label">{t('wf.review.subject')}</span>
          <PortValue value={review.subject} partId={run.part_id} />
        </div>
      )}
      <p className="faint small">{t('wf.review.responsibility')}</p>
      {review.can_decide ? (
        <>
          <div className="field">
            <label htmlFor={cid}>
              {t('wf.review.comment')} <span className="opt">· {t('common.optional')}</span>
            </label>
            <LimitedTextarea id={cid} className="input" rows={2} limit="description" value={comment} placeholder={t('wf.review.commentPlaceholder')} onChange={(e) => setComment(e.target.value)} />
          </div>
          <DecisionError error={error} onReload={onReload} />
          <div className="row">
            <button type="button" className="btn primary" disabled={!!busy} onClick={() => void decide('approved')}>
              <Check /> {busy === 'approved' ? t('common.working') : t('wf.review.approve')}
            </button>
            <button type="button" className="btn danger" disabled={!!busy} onClick={() => void decide('rejected')}>
              <X /> {busy === 'rejected' ? t('common.working') : t('wf.review.reject')}
            </button>
          </div>
        </>
      ) : (
        <RoleNote stage={stage} />
      )}
    </div>
  );
}

// ─── clarification ─────────────────────────────────────────────────────────

function ClarificationForm({ run, node, onRun, onReload }: { run: WorkflowRun; node: NodeRun; onRun: (r: WorkflowRun) => void; onReload: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const clar = node.review?.clarification;
  const questions = clar?.questions ?? [];
  const previous = clar?.answers ?? [];
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ''));
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState<'approved' | 'rejected' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const baseId = useId();
  const complete = answers.length === questions.length && answers.every((a) => a.trim());
  const submit = async (decision: 'approved' | 'rejected') => {
    if (decision === 'approved') {
      setTouched(true);
      if (!complete) return;
    }
    setBusy(decision);
    setError(null);
    try {
      const res = await decideRunNode(run.id, node.node_id, decision === 'approved' ? { decision, answers: answers.map((a) => a.trim()) } : { decision, comment: comment.trim() });
      onRun(res);
      toast(decision === 'approved' ? t('wf.clar.sentToast') : t('wf.review.rejectedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="stack-sm">
      <div className="row">
        <span className="chip warn">
          <MessageCircleQuestionMark aria-hidden /> {t('wf.clar.title')}
        </span>
      </div>
      <p className="small muted">{t('wf.clar.body')}</p>
      {previous.length > 0 && (
        <details className="wf-prev-answers">
          <summary className="small">{t('wf.clar.previous', { n: previous.length })}</summary>
          <dl>
            {previous.map((a, i) => (
              <div key={i}>
                <dt>{a.question}</dt>
                <dd>{a.answer}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {node.review?.can_decide ? (
        <form
          className="stack-sm"
          onSubmit={(e) => {
            e.preventDefault();
            void submit('approved');
          }}
        >
          {questions.map((q, i) => (
            <div key={i} className={`field ${touched && !answers[i]?.trim() ? 'has-error' : ''}`}>
              <label htmlFor={`${baseId}-${i}`}>
                {i + 1}. {q}
              </label>
              <textarea id={`${baseId}-${i}`} className="input" rows={2} maxLength={2000} value={answers[i] ?? ''} onChange={(e) => setAnswers((xs) => xs.map((x, j) => (j === i ? e.target.value : x)))} />
              {touched && !answers[i]?.trim() && <span className="field-error">{t('wf.clar.required')}</span>}
            </div>
          ))}
          <DecisionError error={error} onReload={onReload} />
          <div className="row">
            <button type="submit" className="btn primary" disabled={!!busy}>
              <Check /> {busy === 'approved' ? t('common.working') : t('wf.clar.send')}
            </button>
          </div>
          <details className="wf-reject-box">
            <summary className="small">{t('wf.clar.rejectInstead')}</summary>
            <div className="stack-sm">
              <LimitedTextarea className="input" rows={2} limit="description" value={comment} placeholder={t('wf.review.commentPlaceholder')} aria-label={t('wf.review.comment')} onChange={(e) => setComment(e.target.value)} />
              <button type="button" className="btn danger sm" disabled={!!busy} onClick={() => void submit('rejected')}>
                <X /> {busy === 'rejected' ? t('common.working') : t('wf.clar.reject')}
              </button>
            </div>
          </details>
        </form>
      ) : (
        <>
          <ol className="wf-plain-list">
            {questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ol>
          <RoleNote stage="design" />
        </>
      )}
    </div>
  );
}

// ─── manual drawing intake ─────────────────────────────────────────────────

interface HoleRow {
  diameter: string;
  count: string;
  through: boolean;
  depth: string;
  counterbore: boolean;
  countersink: boolean;
}

interface TolRow {
  text: string;
  critical: boolean;
}

function numStr(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : '';
}

function prefillFrom(prefill: Obj) {
  const material = isObj(prefill.material) && typeof prefill.material.value === 'string' ? prefill.material.value : '';
  const size = isObj(prefill.overall_size_mm) ? prefill.overall_size_mm : {};
  const holes: HoleRow[] = Array.isArray(prefill.holes)
    ? prefill.holes.filter(isObj).map((h) => ({
        diameter: numStr(h.diameter),
        count: numStr(h.count) || '1',
        through: h.through !== false,
        depth: numStr(h.depth),
        counterbore: h.counterbore === true,
        countersink: h.countersink === true,
      }))
    : [];
  const tols: TolRow[] = Array.isArray(prefill.tolerances) ? prefill.tolerances.filter(isObj).map((x) => ({ text: String(x.text ?? ''), critical: x.critical === true })) : [];
  const notes = [
    ...(Array.isArray(prefill.surface_finish) ? prefill.surface_finish.filter(isObj).map((x) => String(x.text ?? '')) : []),
    ...(Array.isArray(prefill.other_requirements) ? prefill.other_requirements.map(String) : []),
  ]
    .filter(Boolean)
    .join('\n');
  return {
    material,
    x: numStr(size.x),
    y: numStr(size.y),
    z: numStr(size.z),
    holes,
    tols,
    setup: numStr(prefill.setup_directions) || '1',
    notes,
    hasPrefill: Object.keys(prefill).length > 0,
  };
}

function reasonText(t: (k: MessageKey, v?: Record<string, string | number>) => string, reason: string): string {
  if (reason.startsWith('incomplete:')) return t('wf.manual.reasonIncomplete', { fields: reason.slice('incomplete:'.length).split(',').join(', ') });
  const k = `wf.manual.reason.${reason}`;
  return hasKey(k) ? t(k as MessageKey) : t('wf.manual.reasonOther', { code: reason });
}

function ManualEntryForm({ run, node, lookups, onRun, onReload }: { run: WorkflowRun; node: NodeRun; lookups: Lookups; onRun: (r: WorkflowRun) => void; onReload: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const me = node.review?.manual_entry;
  const init = useMemo(() => prefillFrom(isObj(me?.prefill) ? me!.prefill : {}), [me]);
  const [material, setMaterial] = useState(init.material);
  const [x, setX] = useState(init.x);
  const [y, setY] = useState(init.y);
  const [z, setZ] = useState(init.z);
  const [volume, setVolume] = useState('');
  const [holes, setHoles] = useState<HoleRow[]>(init.holes);
  const [tols, setTols] = useState<TolRow[]>(init.tols);
  const [setup, setSetup] = useState(init.setup);
  const [fillets, setFillets] = useState('0');
  const [chamfers, setChamfers] = useState('0');
  const [unsupported, setUnsupported] = useState<string[]>([]);
  const [notes, setNotes] = useState(init.notes);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<'approved' | 'rejected' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [intakeId, setIntakeId] = useState<string | null>(null);
  const [rejectComment, setRejectComment] = useState('');
  const listId = useId();
  const fileInfo = lookups.fileVersion(me?.file_version_id);

  const pos = (s: string) => s.trim() !== '' && Number(s) > 0;
  const int = (s: string, min: number, max = Infinity) => s.trim() !== '' && Number.isInteger(Number(s)) && Number(s) >= min && Number(s) <= max;
  const bboxOk = pos(x) && pos(y) && pos(z);
  const holesOk = holes.every((h) => pos(h.diameter) && int(h.count, 1) && (h.through || pos(h.depth)));
  const valid = bboxOk && holesOk && int(setup, 1, 6) && int(fillets, 0) && int(chamfers, 0) && (volume.trim() === '' || pos(volume)) && tols.every((tl) => tl.text.trim());

  const submit = async () => {
    setTouched(true);
    if (!valid) return;
    if (!run.part_id) return;
    setBusy('approved');
    setError(null);
    try {
      let id = intakeId;
      if (!id) {
        const intake = await createManualIntake(run.part_id, {
          file_version_id: me?.file_version_id ?? null,
          material: material.trim() || null,
          bbox_mm: [Number(x), Number(y), Number(z)],
          volume_mm3: volume.trim() ? Number(volume) : null,
          holes: holes.map((h) => ({
            diameter: Number(h.diameter),
            count: Number(h.count),
            through: h.through,
            depth: h.through ? null : Number(h.depth),
            counterbore: h.counterbore,
            countersink: h.countersink,
          })),
          fillets: Number(fillets),
          chamfers: Number(chamfers),
          setup_directions: Number(setup),
          tolerances: tols.map((tl) => ({ text: tl.text.trim(), critical: tl.critical })),
          unsupported,
          notes: notes.trim(),
        });
        id = intake.id;
        setIntakeId(id);
      }
      const res = await decideRunNode(run.id, node.node_id, { decision: 'approved', intake_id: id });
      onRun(res);
      toast(t('wf.manual.savedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const reject = async () => {
    setBusy('rejected');
    setError(null);
    try {
      const res = await decideRunNode(run.id, node.node_id, { decision: 'rejected', comment: rejectComment.trim() });
      onRun(res);
      toast(t('wf.review.rejectedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const err = (bad: boolean) => (touched && bad ? 'is-invalid' : '');

  return (
    <div className="stack-sm">
      <div className="row">
        <span className="chip warn">
          <ScanText aria-hidden /> {t('wf.manual.title')}
        </span>
        <span className="chip warn">{t('wf.art.estimated')}</span>
      </div>
      <p className="small muted">{me ? reasonText(t, me.reason) : null}</p>
      {fileInfo && (
        <div className="row small">
          <span className="faint">{t('wf.manual.drawing')}</span>
          <span className="strong ellip">
            {fileInfo.file.name} · v{fileInfo.version.version}
          </span>
          <button type="button" className="btn sm" onClick={() => void downloadBlob(fileInfo.version.blob_sha256, fileInfo.version.original_name).catch(() => toast(t('error.downloadFailed'), { tone: 'err' }))}>
            <Download /> {t('common.download')}
          </button>
        </div>
      )}
      {init.hasPrefill && <p className="callout info slim small">{t('wf.manual.prefilled')}</p>}
      {!node.review?.can_decide ? (
        <RoleNote stage="design" />
      ) : !run.part_id ? (
        <p className="callout err slim">{t('wf.manual.noPart')}</p>
      ) : (
        <form
          className="wf-manual stack-sm"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <fieldset disabled={!!intakeId || !!busy} className="stack-sm">
            <div className="form-grid">
              <div className="field">
                <label htmlFor={`${listId}-mat`}>{t('wf.manual.material')}</label>
                <input id={`${listId}-mat`} className="input" list={`${listId}-mats`} value={material} placeholder={t('wf.manual.materialPlaceholder')} onChange={(e) => setMaterial(e.target.value)} />
                <datalist id={`${listId}-mats`}>
                  {KNOWN_MATERIALS.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </div>
              <div className="field">
                <label htmlFor={`${listId}-vol`}>
                  {t('wf.manual.volume')} <span className="opt">· {t('common.optional')}</span>
                </label>
                <input id={`${listId}-vol`} className={`input mono ${err(volume.trim() !== '' && !pos(volume))}`} type="number" min={0} step="any" value={volume} onChange={(e) => setVolume(e.target.value)} />
                <span className="field-hint">{t('wf.manual.volumeHint')}</span>
              </div>
            </div>
            <div className="field">
              <span className="wf-insp-label">{t('wf.manual.bbox')}</span>
              <div className="wf-xyz">
                {(
                  [
                    ['X', x, setX],
                    ['Y', y, setY],
                    ['Z', z, setZ],
                  ] as const
                ).map(([axis, v, set]) => (
                  <label key={axis} className="wf-xyz-cell">
                    <span className="mono">{axis}</span>
                    <input className={`input mono ${err(!pos(v))}`} type="number" min={0} step="any" value={v} aria-label={`${t('wf.manual.bbox')} ${axis}`} onChange={(e) => set(e.target.value)} />
                    <span className="faint small">mm</span>
                  </label>
                ))}
              </div>
              {touched && !bboxOk && <span className="field-error">{t('wf.manual.bboxRequired')}</span>}
            </div>
            <div className="stack-sm">
              <div className="row">
                <span className="wf-insp-label grow">{t('wf.manual.holes')}</span>
                <button type="button" className="btn sm" onClick={() => setHoles((hs) => [...hs, { diameter: '', count: '1', through: true, depth: '', counterbore: false, countersink: false }])}>
                  <Plus /> {t('wf.manual.addHole')}
                </button>
              </div>
              {holes.length === 0 ? (
                <p className="faint small">{t('wf.manual.noHoles')}</p>
              ) : (
                <div className="table-wrap">
                  <table className="t dense wf-hole-table">
                    <thead>
                      <tr>
                        <th>{t('wf.manual.diameter')}</th>
                        <th>{t('wf.manual.count')}</th>
                        <th>{t('wf.manual.through')}</th>
                        <th>{t('wf.manual.depth')}</th>
                        <th>{t('wf.manual.cbore')}</th>
                        <th>{t('wf.manual.csink')}</th>
                        <th className="act">
                          <span className="sr-only">{t('common.actions')}</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {holes.map((h, i) => {
                        const set = (patch: Partial<HoleRow>) => setHoles((hs) => hs.map((x2, j) => (j === i ? { ...x2, ...patch } : x2)));
                        return (
                          <tr key={i}>
                            <td>
                              <input className={`input sm mono ${err(!pos(h.diameter))}`} type="number" min={0} step="any" value={h.diameter} aria-label={t('wf.manual.diameter')} onChange={(e) => set({ diameter: e.target.value })} />
                            </td>
                            <td>
                              <input className={`input sm mono ${err(!int(h.count, 1))}`} type="number" min={1} step={1} value={h.count} aria-label={t('wf.manual.count')} onChange={(e) => set({ count: e.target.value })} />
                            </td>
                            <td>
                              <input type="checkbox" checked={h.through} aria-label={t('wf.manual.through')} onChange={(e) => set({ through: e.target.checked })} />
                            </td>
                            <td>
                              <input className={`input sm mono ${err(!h.through && !pos(h.depth))}`} type="number" min={0} step="any" value={h.through ? '' : h.depth} disabled={h.through} aria-label={t('wf.manual.depth')} onChange={(e) => set({ depth: e.target.value })} />
                            </td>
                            <td>
                              <input type="checkbox" checked={h.counterbore} aria-label={t('wf.manual.cbore')} onChange={(e) => set({ counterbore: e.target.checked })} />
                            </td>
                            <td>
                              <input type="checkbox" checked={h.countersink} aria-label={t('wf.manual.csink')} onChange={(e) => set({ countersink: e.target.checked })} />
                            </td>
                            <td className="act">
                              <button type="button" className="btn ghost icon sm" aria-label={t('common.remove')} onClick={() => setHoles((hs) => hs.filter((_, j) => j !== i))}>
                                <Trash />
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {touched && !holesOk && <span className="field-error">{t('wf.manual.holesInvalid')}</span>}
            </div>
            <div className="stack-sm">
              <div className="row">
                <span className="wf-insp-label grow">{t('wf.manual.tolerances')}</span>
                <button type="button" className="btn sm" onClick={() => setTols((ts) => [...ts, { text: '', critical: false }])}>
                  <Plus /> {t('wf.manual.addTolerance')}
                </button>
              </div>
              {tols.map((tl, i) => (
                <div key={i} className="row nowrap-row">
                  <input className={`input grow ${err(!tl.text.trim())}`} value={tl.text} placeholder={t('wf.manual.tolerancePlaceholder')} aria-label={t('wf.manual.tolerances')} onChange={(e) => setTols((ts) => ts.map((x2, j) => (j === i ? { ...x2, text: e.target.value } : x2)))} />
                  <label className="check">
                    <input type="checkbox" checked={tl.critical} onChange={(e) => setTols((ts) => ts.map((x2, j) => (j === i ? { ...x2, critical: e.target.checked } : x2)))} />
                    <span>{t('wf.manual.critical')}</span>
                  </label>
                  <button type="button" className="btn ghost icon sm" aria-label={t('common.remove')} onClick={() => setTols((ts) => ts.filter((_, j) => j !== i))}>
                    <Trash />
                  </button>
                </div>
              ))}
            </div>
            <div className="form-grid wf-three">
              <div className="field">
                <label htmlFor={`${listId}-setup`}>{t('wf.manual.setups')}</label>
                <input id={`${listId}-setup`} className={`input mono ${err(!int(setup, 1, 6))}`} type="number" min={1} max={6} step={1} value={setup} onChange={(e) => setSetup(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor={`${listId}-fil`}>{t('wf.manual.fillets')}</label>
                <input id={`${listId}-fil`} className={`input mono ${err(!int(fillets, 0))}`} type="number" min={0} step={1} value={fillets} onChange={(e) => setFillets(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor={`${listId}-cha`}>{t('wf.manual.chamfers')}</label>
                <input id={`${listId}-cha`} className={`input mono ${err(!int(chamfers, 0))}`} type="number" min={0} step={1} value={chamfers} onChange={(e) => setChamfers(e.target.value)} />
              </div>
            </div>
            <div className="field">
              <label htmlFor={`${listId}-uns`}>
                {t('wf.manual.unsupported')} <span className="opt">· {t('common.optional')}</span>
              </label>
              <TagInput id={`${listId}-uns`} value={unsupported} onChange={setUnsupported} placeholder={t('wf.manual.unsupportedPlaceholder')} />
            </div>
            <div className="field">
              <label htmlFor={`${listId}-notes`}>
                {t('wf.manual.notes')} <span className="opt">· {t('common.optional')}</span>
              </label>
              <LimitedTextarea id={`${listId}-notes`} className="input" rows={2} limit="description" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </div>
          </fieldset>
          {intakeId && <p className="callout ok slim small">{t('wf.manual.intakeCreated')}</p>}
          <DecisionError error={error} onReload={onReload} />
          <div className="row">
            <button type="submit" className="btn primary" disabled={!!busy}>
              <Check /> {busy === 'approved' ? t('common.working') : intakeId ? t('wf.manual.retrySubmit') : t('wf.manual.submit')}
            </button>
          </div>
          <details className="wf-reject-box">
            <summary className="small">{t('wf.manual.rejectInstead')}</summary>
            <div className="stack-sm">
              <LimitedTextarea className="input" rows={2} limit="description" value={rejectComment} placeholder={t('wf.review.commentPlaceholder')} aria-label={t('wf.review.comment')} onChange={(e) => setRejectComment(e.target.value)} />
              <button type="button" className="btn danger sm" disabled={!!busy} onClick={() => void reject()}>
                <X /> {busy === 'rejected' ? t('common.working') : t('wf.review.reject')}
              </button>
            </div>
          </details>
        </form>
      )}
    </div>
  );
}

export function ReviewPanel({ run, node, lookups, onRun, onReload }: { run: WorkflowRun; node: NodeRun; lookups: Lookups; onRun: (r: WorkflowRun) => void; onReload: () => void }) {
  if (!node.review) return null;
  // Remount per waiting episode so form state never leaks between questions / attempts.
  const key = `${node.node_id}:${node.attempt}`;
  if (node.review.kind === 'clarification') return <ClarificationForm key={key} run={run} node={node} onRun={onRun} onReload={onReload} />;
  if (node.review.kind === 'manual_entry') return <ManualEntryForm key={key} run={run} node={node} lookups={lookups} onRun={onRun} onReload={onReload} />;
  return <ApprovalForm key={key} run={run} node={node} onRun={onRun} onReload={onReload} />;
}
