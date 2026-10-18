// Edits of the assembly workspace: every change posts ops against the viewed revision as base and creates a new
// revision. Changes that reach shared definitions or the BOM are previewed first (impact: affected instances,
// BOM quantities, what goes stale). A 409 (someone else changed it) reloads and asks to apply again.

import { useCallback,useEffect,useState } from 'react';
import { editAssembly,previewImpact,type AssemblyRevision,type EditOp,type Impact } from '../../api/assemblies';
import { isApiError } from '../../api/client';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice,Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';

/** Ops whose effect reaches beyond one pose: preview the impact before creating the revision. */
const PREVIEW_OPS = new Set(['update_definition', 'replace_definition', 'remove_instance', 'suppress_instance', 'set_quantity', 'pattern_linear', 'pattern_circular', 'add_instance']);

export interface EditApi {
  busy: boolean;
  dirty: boolean;
  beginDraft: () => () => void;
  submit: (ops: EditOp[], opts?: { note?: string; preview?: boolean }) => Promise<boolean>;
  dialog: React.ReactNode;
}

/** Open forms keep their inputs and original revision when an assistant result arrives. */
export function useAssemblyDraft(edit: EditApi) {
  useEffect(() => edit.beginDraft(), [edit.beginDraft]);
}

export function useAssemblyEdit(rev: AssemblyRevision | undefined, onSaved: (r: AssemblyRevision) => void, onConflict: () => void): EditApi {
  const { t } = useI18n();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState(0);
  const beginDraft = useCallback(() => {
    setDrafts((count) => count + 1);
    let open = true;
    return () => { if (open) { open = false; setDrafts((count) => count - 1); } };
  }, []);
  const [pending, setPending] = useState<{ base: Pick<AssemblyRevision, 'id' | 'assembly_id'>; ops: EditOp[]; note?: string; impact: Impact | null; error: unknown } | null>(null);

  const apply = useCallback(
    async (base: Pick<AssemblyRevision, 'id' | 'assembly_id'>, ops: EditOp[], note?: string): Promise<boolean> => {
      setBusy(true);
      try {
        // The server freezes current project defaults for this new edit.
        const saved = await editAssembly(base.assembly_id, { base_revision_id: base.id, ops, note });
        toast(t('asm.edit.saved', { label: saved.label }));
        setPending(null);
        onSaved(saved);
        return true;
      } catch (e) {
        if (isApiError(e) && e.status === 409) {
          toast(t('asm.edit.conflict'), { tone: 'err' });
          setPending(null);
          onConflict();
          return false;
        }
        if (pending) setPending((p) => (p ? { ...p, error: e } : p));
        else toast(isApiError(e) ? e.message : String(e), { tone: 'err' });
        return false;
      } finally {
        setBusy(false);
      }
    },
    [toast, t, onSaved, onConflict, pending],
  );

  const submit = useCallback(
    async (ops: EditOp[], opts: { note?: string; preview?: boolean } = {}) => {
      if (!rev) return false;
      const base = { id: rev.id, assembly_id: rev.assembly_id };
      const wantPreview = opts.preview ?? ops.some((o) => PREVIEW_OPS.has(o.op));
      if (!wantPreview) return apply(base, ops, opts.note);
      const preview = { base, ops, note: opts.note, impact: null, error: null };
      setPending(preview);
      try {
        const impact = await previewImpact(base.assembly_id, { base_revision_id: base.id, ops });
        // A response from a cancelled/replaced preview must not reopen its dialog.
        setPending((current) => current === preview ? { ...current, impact } : current);
      } catch (e) {
        setPending((current) => current === preview ? { ...current, error: e } : current);
      }
      return true;
    },
    [rev, apply],
  );

  const dialog = pending ? (
    <Dialog
      title={t('asm.impact.title')}
      onClose={() => setPending(null)}
      busy={busy}
      size="lg"
      footer={
        <>
          <button type="button" className="btn" onClick={() => setPending(null)} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn primary" onClick={() => void apply(pending.base, pending.ops, pending.note)} disabled={busy || (!pending.impact && !pending.error)}>
            {busy ? t('common.saving') : t('asm.edit.apply')}
          </button>
        </>
      }
    >
      {pending.error ? <ErrorNotice error={pending.error} /> : null}
      {!pending.impact && !pending.error ? <Spinner /> : null}
      {pending.impact ? <ImpactBody impact={pending.impact} /> : null}
    </Dialog>
  ) : null;

  return { busy, dirty: busy || !!pending || drafts > 0, beginDraft, submit, dialog };
}

export function ImpactBody({ impact }: { impact: Impact }) {
  const { t } = useI18n();
  return (
    <div className="stack-sm asm-impact">
      <section>
        <h4>{t('asm.impact.instances')}</h4>
        <p className="mono small">{impact.affected_instances.length ? impact.affected_instances.join(', ') : '—'}</p>
      </section>
      <section>
        <h4>{t('asm.impact.bom')}</h4>
        {impact.bom_changes.length ? (
          <ul className="small">
            {impact.bom_changes.map((b) => (
              <li key={b.def_key}>{t('asm.history.qty', { name: b.name, from: b.from, to: b.to })}</li>
            ))}
          </ul>
        ) : (
          <p className="faint small">—</p>
        )}
      </section>
      {impact.process_quote_inputs.length > 0 && (
        <section>
          <h4>{t('asm.impact.process')}</h4>
          <ul className="small">
            {impact.process_quote_inputs.map((p) => (
              <li key={p.def_key}>
                {p.name} · {t(`asm.impact.process.${p.status}`)}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h4>{t('asm.impact.stale')}</h4>
        {impact.will_be_stale.length ? (
          <ul className="small">
            {impact.will_be_stale.map((d, i) => (
              <li key={i}>
                {d.kind}
                {d.asset_version?.asset_name ? ` · ${d.asset_version.asset_name} ${d.asset_version.label ?? ''}` : d.id ? ` · ${d.id.slice(0, 8)}` : ''}
              </li>
            ))}
          </ul>
        ) : (
          <p className="faint small">{t('asm.impact.none')}</p>
        )}
      </section>
      {impact.parents_update_available.length > 0 && (
        <section>
          <h4>{t('asm.impact.parents')}</h4>
          <ul className="small">
            {impact.parents_update_available.map((d, i) => (
              <li key={i}>
                {d.assembly?.number} {d.assembly?.name} · A{d.rev_no}
              </li>
            ))}
          </ul>
        </section>
      )}
      {impact.notes.map((n, i) => (
        <p key={i} className="callout slim warn">
          {n.message}
        </p>
      ))}
    </div>
  );
}
