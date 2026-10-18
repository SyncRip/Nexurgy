// Actions of the catalogue detail (handoff §9), each on the existing API it names — POST /files/{id}/versions,
// PATCH /files/{id}, POST /asset-versions/{id}/publish, PATCH /drawing-documents/{id} — or shown disabled with the
// reason it cannot run. Nothing is changed locally before the server answers; success reloads the catalogue.

import { Archive, ArchiveRestore, BadgeCheck, Ban, FilePlus2, Pencil, PencilRuler, Trash2, TriangleAlert } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../../api/client';
import { useFeature } from '../../api/capabilities';
import { publishAssetVersion, type AssetVersion } from '../../api/assets';
import { patchDrawingDocument } from '../../api/drafting';
import { patchFile } from '../../api/endpoints';
import type { Project, UUID } from '../../api/types';
import { FileMetaDialog, NewFileVersionDialog } from '../../components/files/FileDialogs';
import { ConfirmDialog, Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice, ErrorText } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { dynLabel, stageText } from '../assets/meta';
import { ACTION_CHECKS, type ActionCheck, type ActionContext, type PatchTarget } from './detailModel';
import type { CatalogItem } from './types';

/** PATCH /drawing-documents DocumentPatch limits (title 1–200, description ≤ DESCRIPTION_CHARS). */
const TITLE_MAX = 200;
const DESCRIPTION_MAX = 4000;

/** A button that is either usable or disabled with its reason shown next to it (never a hidden fake action). */
export function ActionButton({ check, label, icon, onClick, primary, danger, busy }: { check: ActionCheck; label: ReactNode; icon: ReactNode; onClick?: () => void; primary?: boolean; danger?: boolean; busy?: boolean }) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="rd-action">
      <button type="button" className={`btn sm ${primary ? 'primary' : ''} ${danger && check.ok ? 'danger' : ''}`} disabled={!check.ok || busy} aria-describedby={check.ok ? undefined : id} onClick={onClick}>
        {icon} {label}
      </button>
      {!check.ok && (
        <span id={id} className="rd-why">
          {t(`rd.why.${check.why}`)}
        </span>
      )}
    </div>
  );
}

// ─── dialogs ────────────────────────────────────────────────────────────────

/** Title and description of a standalone drawing document (PATCH /drawing-documents/{id}); its versions stay as they are. */
function DrawingDocumentMetaDialog({ documentId, title, description, onClose, onDone }: { documentId: UUID; title: string; description: string; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
  const [nextTitle, setNextTitle] = useState(title);
  const [nextDescription, setNextDescription] = useState(description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const titleErr = !nextTitle.trim() ? t('validation.required', { field: t('rd.metaDoc.name') }) : null;
  const patch = {
    ...(nextTitle.trim() !== title ? { title: nextTitle.trim() } : {}),
    ...(nextDescription !== description ? { description: nextDescription } : {}),
  };
  const changed = Object.keys(patch).length > 0;
  const submit = async () => {
    if (titleErr || !changed) return;
    setBusy(true);
    setError(null);
    try {
      await patchDrawingDocument(documentId, patch);
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('rd.metaDoc.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !!titleErr || !changed}>
            {busy ? t('common.saving') : t('common.save')}
          </button>
        </>
      }
    >
      <div className="form-grid one">
        <Field label={t('rd.metaDoc.name')} error={titleErr}>
          <input className="input" value={nextTitle} maxLength={TITLE_MAX} onChange={(e) => setNextTitle(e.target.value)} disabled={busy} />
        </Field>
        <Field label={t('rd.metaDoc.description')} optional>
          <textarea className="input" rows={3} value={nextDescription} maxLength={DESCRIPTION_MAX} onChange={(e) => setNextDescription(e.target.value)} disabled={busy} />
        </Field>
      </div>
      <p className="faint small">{t('rd.metaDoc.note')}</p>
      <ErrorNotice error={error} />
    </Dialog>
  );
}

/** Publish errors with the server's reason: approval_required (which review) / asset_not_ready (which state). */
function PublishError({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  if (isApiError(error) && (error.code === 'approval_required' || error.code === 'asset_not_ready')) {
    const d = error.details as { stage?: string; subject_type?: string; availability?: string };
    const text =
      error.code === 'approval_required'
        ? t(d.subject_type === 'process_module' ? 'as.err.approval_required_module' : 'as.err.approval_required', { stage: stageText(t, String(d.stage ?? '')) })
        : t('as.err.asset_not_ready', { availability: dynLabel(t, `as.avail.${String(d.availability ?? '')}`, String(d.availability ?? '')) });
    return (
      <div className="callout warn" role="alert">
        <TriangleAlert aria-hidden />
        <div className="stack-sm">
          <span>{text}</span>
          <code className="error-code">{t('common.errorCode', { code: error.code })}</code>
        </div>
      </div>
    );
  }
  return <ErrorNotice error={error} overrides={{ forbidden: 'as.err.forbidden' }} />;
}

// ─── action bars ────────────────────────────────────────────────────────────

export interface DetailActionsProps {
  item: CatalogItem;
  project: Project;
  ctx: ActionContext;
  /** The latest version's label (for the new-version note). */
  latestLabel: string | null;
  /** Professional page that edits the selected version (null for stored files: they get a new upload instead). */
  editPath: string | null;
  onChanged: () => void;
  /** A new version was saved: reload versions and show the latest. */
  onNewVersion: () => void;
  /** The selected version changed state: reload versions. */
  onVersionChanged: () => void;
}

/** Primary row: upload a new version (stored files) or edit the content in its professional page (saves a new version there). */
export function PrimaryActions({ item, ctx, latestLabel, editPath, onChanged, onNewVersion }: DetailActionsProps) {
  const { t } = useI18n();
  const toast = useToast();
  const [uploading, setUploading] = useState(false);
  const fileId = ctx.caps.fileId;
  const edit = ACTION_CHECKS.edit(ctx, item);
  if (!fileId && !editPath) return null;
  return (
    <div className="rd-primary">
      {fileId ? (
        <ActionButton check={ACTION_CHECKS.newVersion(ctx, item)} primary icon={<FilePlus2 />} label={t('rd.action.newVersion')} onClick={() => setUploading(true)} />
      ) : editPath && edit.ok ? (
        <div className="rd-action">
          <Link className="btn sm primary" to={editPath}>
            <PencilRuler /> {t('rd.action.edit')}
          </Link>
        </div>
      ) : (
        <ActionButton check={edit} primary icon={<PencilRuler />} label={t('rd.action.edit')} />
      )}
      {uploading && fileId && (
        <NewFileVersionDialog
          file={{ id: fileId, name: item.name }}
          intro={latestLabel ? t('rd.newVersion.intro', { label: latestLabel }) : t('rd.newVersion.introFirst')}
          noteRequired
          onClose={() => setUploading(false)}
          onDone={(f) => {
            setUploading(false);
            toast(t('rd.newVersion.done', { label: `v${f.versions[0]?.version ?? ''}` }));
            onChanged();
            onNewVersion();
          }}
        />
      )}
    </div>
  );
}

/**
 * Footer: name / part / description, deprecate (a published version) or restore use (a deprecated one), archive or
 * restore (the item's own archive), delete — each wired or disabled with its reason.
 */
export function MoreActions({ item, project, ctx, onChanged, onVersionChanged }: DetailActionsProps) {
  const modulesEnabled = useFeature('process_modules');
  const { t } = useI18n();
  const toast = useToast();
  const [dialog, setDialog] = useState<'meta' | 'publish' | 'archive' | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const asset: AssetVersion | null = ctx.version?.asset ?? null;
  const publishEnabled = asset?.asset_type !== 'process_module' || modulesEnabled;
  const deprecated = asset?.publish_state === 'deprecated';
  const target: PatchTarget | null = ctx.caps.target;

  const close = () => {
    setDialog(null);
    setError(null);
  };
  const metaDone = () => {
    close();
    toast(t('rd.meta.done'));
    onChanged();
  };

  const publish = async () => {
    if (!asset || !publishEnabled) return;
    setBusy(true);
    setError(null);
    try {
      const v = await publishAssetVersion(asset.id, deprecated ? 'published' : 'deprecated');
      toast(t(deprecated ? 'rd.restoreUse.done' : 'rd.deprecate.done', { label: v.label }));
      close();
      onChanged();
      onVersionChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const setArchived = async (archived: boolean) => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      if (target.api === 'file') await patchFile(target.id, { archived });
      else await patchDrawingDocument(target.id, { archived });
      toast(t(archived ? 'rd.archive.done' : 'rd.unarchive.done'));
      close();
      onChanged();
    } catch (e) {
      if (archived) setError(e);
      else toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rd-more" role="group" aria-label={t('rd.actions.more')}>
      <ActionButton check={ACTION_CHECKS.meta(ctx, item)} icon={<Pencil />} label={t(target?.api === 'drawing_document' ? 'rd.action.metaDoc' : 'rd.action.meta')} onClick={() => setDialog('meta')} />
      {publishEnabled && <ActionButton
        check={ACTION_CHECKS.publish(ctx, item)}
        icon={deprecated ? <BadgeCheck /> : <Ban />}
        label={t(deprecated ? 'rd.action.restoreUse' : 'rd.action.deprecate')}
        onClick={() => setDialog('publish')}
      />}
      <ActionButton
        check={ACTION_CHECKS.archive(ctx, item)}
        icon={item.archived ? <ArchiveRestore /> : <Archive />}
        label={t(item.archived ? 'rd.action.unarchive' : 'rd.action.archive')}
        busy={busy && !dialog}
        onClick={() => (item.archived === 'self' ? void setArchived(false) : setDialog('archive'))}
      />
      <ActionButton check={ACTION_CHECKS.delete(ctx, item)} icon={<Trash2 />} label={t('rd.action.delete')} danger />

      {dialog === 'meta' && target?.api === 'file' && (
        <FileMetaDialog file={{ id: target.id, name: item.name, part_id: item.partId }} partsOf={project.id} onClose={close} onDone={metaDone} />
      )}
      {dialog === 'meta' && target?.api === 'drawing_document' && (
        <DrawingDocumentMetaDialog documentId={target.id} title={item.name} description={item.description ?? ''} onClose={close} onDone={metaDone} />
      )}
      {publishEnabled && dialog === 'publish' && asset && (
        <ConfirmDialog
          title={t(deprecated ? 'rd.restoreUse.title' : 'rd.deprecate.title', { name: item.name, label: asset.label })}
          body={t(deprecated ? 'rd.restoreUse.body' : 'rd.deprecate.body')}
          confirmLabel={t(deprecated ? 'rd.restoreUse.confirm' : 'rd.deprecate.confirm')}
          danger={!deprecated}
          busy={busy}
          error={<PublishError error={error} />}
          onConfirm={() => void publish()}
          onClose={close}
        />
      )}
      {dialog === 'archive' && (
        <ConfirmDialog
          title={t('rd.archive.title', { name: item.name })}
          body={t('rd.archive.body')}
          confirmLabel={t('rd.archive.confirm')}
          danger
          busy={busy}
          error={<ErrorNotice error={error} />}
          onConfirm={() => void setArchived(true)}
          onClose={close}
        />
      )}
    </div>
  );
}
