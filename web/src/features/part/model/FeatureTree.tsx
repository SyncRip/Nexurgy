// Feature tree: add-op toolbar, ordered op list with build status, suppress
// and delete, undo / redo and the save / revert footer.

import { AlertCircle, AlertTriangle, CheckCircle2, CircleDashed, Eye, EyeOff, FileBox, Library, Redo2, RotateCcw, Save, Trash2, Undo2 } from 'lucide-react';
import type { FeatureCheckReport, TemplateMeta } from '../../../api/geometry';
import type { ModelOp, ModelOpType, StepBase } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { OP_ICON, opSummary, opTitle } from './opText';

export type OpStatus = 'ok' | 'error' | 'suppressed' | 'skipped' | 'invalid' | 'pending';

const ADDABLE: Exclude<ModelOpType, 'transform' | 'coil'>[] = ['box', 'cylinder', 'extrude', 'revolve', 'sweep', 'bent_plate', 'hole', 'cut', 'shell',
  'pattern', 'fillet', 'chamfer'];
const NEEDS_BODY: readonly ModelOpType[] = ['hole', 'fillet', 'chamfer', 'cut', 'shell', 'pattern'];

export interface FeatureTreeProps {
  ops: ModelOp[];
  base: StepBase | null;
  selectedOpId: string | null;
  statuses: Record<string, OpStatus>;
  editable: boolean;
  hasBody: boolean;
  canUndo: boolean;
  canRedo: boolean;
  dirty: boolean;
  saveDisabledReason: string | null;
  onSelect: (id: string | null) => void;
  onSuppress: (id: string, v: boolean) => void;
  onRemove: (id: string) => void;
  onAdd: (type: ModelOpType) => void;
  onUndo: () => void;
  onRedo: () => void;
  onSave: () => void;
  onRevert: () => void;
  readOnlyNote?: string | null;
  /** W3: open the part-template dialog. */
  onTemplate?: () => void;
  /** W3: template provenance of the shown revision (representation labels of its ops). */
  template?: TemplateMeta | null;
  /** W3: independent feature check of the draft / revision. */
  featureCheck?: FeatureCheckReport | null;
}

function StatusIcon({ status }: { status: OpStatus | undefined }) {
  const { t } = useI18n();
  if (!status) return null;
  const label = t(`ws.status.${status}`);
  switch (status) {
    case 'ok':
      return <CheckCircle2 className="ws-st ok" aria-label={label} />;
    case 'error':
      return <AlertTriangle className="ws-st err" aria-label={label} />;
    case 'invalid':
      return <AlertCircle className="ws-st warn" aria-label={label} />;
    case 'suppressed':
      return <EyeOff className="ws-st pending" aria-label={label} />;
    case 'skipped':
      return <CircleDashed className="ws-st pending" aria-label={label} />;
    default:
      return <CircleDashed className="ws-st pending spin-slow" aria-label={label} />;
  }
}

function featureProblems(rep: FeatureCheckReport): string {
  return rep.checks
    .flatMap((c) => c.items ?? [])
    .filter((i) => i.status !== 'passed')
    .map((i) => i.message)
    .join('\n');
}

export function FeatureTree(p: FeatureTreeProps) {
  const { t } = useI18n();
  const mod = navigator.platform?.toLowerCase().includes('mac') ? '⌘' : 'Ctrl+';
  return (
    <div className="ws-tree">
      {p.editable && (
        <div className="ws-add" role="toolbar" aria-label={t('ws.tree.add')}>
          {ADDABLE.map((type) => {
            const disabled = NEEDS_BODY.includes(type) && !p.hasBody;
            return (
              <button key={type} type="button" className={`ws-add-btn t-${type}`} disabled={disabled} onClick={() => p.onAdd(type)} title={disabled ? t('ws.tree.needsBody') : t(`ws.add.hint.${type}`)}>
                {OP_ICON[type]}
                <span>{t(`ws.op.${type}`)}</span>
              </button>
            );
          })}
          {p.onTemplate && (
            <button type="button" className="ws-add-btn t-template" onClick={p.onTemplate} title={t('geo.tpl.title')}>
              <Library aria-hidden />
              <span>{t('geo.tpl.button')}</span>
            </button>
          )}
        </div>
      )}
      {p.template && (
        <div className="callout info slim ws-tree-note">
          <span className="grow">
            {t('geo.tree.template', { label: p.template.spec_label })}{' '}
            <span className={`chip ${p.template.representation === 'exact' ? 'ok' : 'warn'}`}>{t(`geo.repr.${p.template.representation}`)}</span>{' '}
            <span className="chip">{t(`geo.proc.${p.template.procurement}`)}</span>
            {(p.template.modified_ops?.length ?? 0) > 0 && <span className="faint small"> · {t('geo.tree.templateEdited')}</span>}
          </span>
        </div>
      )}
      {p.featureCheck && (
        <div className={`callout slim ws-tree-note ${p.featureCheck.status === 'passed' ? 'ok' : p.featureCheck.status === 'skipped_turbo' ? 'pending' : 'err'}`} title={featureProblems(p.featureCheck)}>
          {t(`geo.check.${p.featureCheck.status}`)}
        </div>
      )}
      <div className="ws-tree-h">
        <h3>
          {t('ws.tree.title')} <span className="faint mono">{p.ops.length}</span>
        </h3>
        {p.editable && (
          <div className="row">
            <button type="button" className="btn sm icon ghost" disabled={!p.canUndo} onClick={p.onUndo} title={`${t('ws.undo')} (${mod}Z)`} aria-label={t('ws.undo')}>
              <Undo2 />
            </button>
            <button type="button" className="btn sm icon ghost" disabled={!p.canRedo} onClick={p.onRedo} title={`${t('ws.redo')} (${mod}Shift+Z)`} aria-label={t('ws.redo')}>
              <Redo2 />
            </button>
          </div>
        )}
      </div>
      {p.readOnlyNote && <div className="callout pending slim ws-tree-note">{p.readOnlyNote}</div>}
      <ol className="ws-tree-list" aria-label={t('ws.tree.title')}>
        {p.base && (
          <li className="ws-node base">
            <span className="ws-node-ico">
              <FileBox aria-hidden />
            </span>
            <span className="mono ws-node-id">F-00</span>
            <span className="ws-node-main">
              <span className="ws-node-title">{t('ws.tree.base')}</span>
              <span className="ws-node-sum mono">{p.base.blob_sha256.slice(0, 10)}…</span>
            </span>
          </li>
        )}
        {p.ops.map((op) => {
          const st = p.statuses[op.id];
          const selected = op.id === p.selectedOpId;
          return (
            <li key={op.id} className={`ws-node ${selected ? 'selected' : ''} ${op.suppressed ? 'suppressed' : ''} st-${st ?? 'none'}`}>
              <button type="button" className="ws-node-btn" onClick={() => p.onSelect(selected ? null : op.id)} aria-pressed={selected}>
                <span className={`ws-node-ico t-${op.type}`}>{OP_ICON[op.type]}</span>
                <span className="mono ws-node-id">{op.id}</span>
                <span className="ws-node-main">
                  <span className="ws-node-title">
                    {opTitle(op, t)}
                    {'mode' in op && op.mode && op.mode !== 'new' && op.mode !== 'add' ? <span className={`ws-mode m-${op.mode}`}>{t(`ws.mode.${op.mode}`)}</span> : null}
                    {p.template?.op_ids.includes(op.id) ? <span className="ws-mode">{t(`geo.repr.${p.template.representation}`)}</span> : null}
                  </span>
                  <span className="ws-node-sum mono">{opSummary(op, t)}</span>
                </span>
                <StatusIcon status={st} />
              </button>
              {p.editable && (
                <span className="ws-node-act">
                  <button type="button" className="btn sm icon ghost" onClick={() => p.onSuppress(op.id, !op.suppressed)} title={op.suppressed ? t('ws.op.unsuppress') : t('ws.op.suppress')} aria-label={`${op.suppressed ? t('ws.op.unsuppress') : t('ws.op.suppress')} ${op.id}`}>
                    {op.suppressed ? <EyeOff /> : <Eye />}
                  </button>
                  <button type="button" className="btn sm icon ghost danger" onClick={() => p.onRemove(op.id)} title={t('ws.op.delete')} aria-label={`${t('ws.op.delete')} ${op.id}`}>
                    <Trash2 />
                  </button>
                </span>
              )}
            </li>
          );
        })}
        {!p.ops.length && !p.base && <li className="ws-tree-empty faint">{p.editable ? t('ws.tree.emptyEdit') : t('ws.tree.empty')}</li>}
      </ol>
      {p.editable && (
        <div className="ws-tree-foot">
          {p.dirty ? <span className="chip warn">{t('ws.unsaved')}</span> : <span className="faint small">{t('ws.saved')}</span>}
          <span className="grow" />
          <button type="button" className="btn sm ghost" disabled={!p.dirty} onClick={p.onRevert} title={t('ws.revertHint')}>
            <RotateCcw /> {t('ws.revert')}
          </button>
          <button type="button" className="btn sm primary" disabled={!p.dirty || !!p.saveDisabledReason} onClick={p.onSave} title={p.saveDisabledReason ?? `${t('ws.save')} (${mod}S)`}>
            <Save /> {t('ws.save')}
          </button>
        </div>
      )}
    </div>
  );
}
