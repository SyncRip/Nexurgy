import { useAssistantPageContext } from '../../features/assistant/AssistantProvider';
// Project / part files: drag-and-drop upload, kind badges, version history,
// new versions, authenticated download, rename and archive (PATCH /files/{id}
// takes query parameters; the new-version and rename dialogs are shared with the
// resources detail, FileDialogs.tsx). Used by the project Files tab and the part
// workspace Files tab (with `part` set, uploads are linked to that part).
// `compact` (resources area): an upload button in the toolbar instead of the
// drop zone; dropping files on the panel still works.

import { Archive,ArchiveRestore,ChevronDown,ChevronRight,Download,FilePlus2,MoreHorizontal,Pencil,Upload,X } from 'lucide-react';
import { Fragment,useMemo,useRef,useState,type DragEvent } from 'react';
import { Link } from 'react-router-dom';
import { downloadBlob } from '../../api/blobs';
import { listFiles,listParts,patchFile,uploadFile } from '../../api/endpoints';
import { FILE_KINDS,type FileKind,type FileVersion,type Part,type Project,type ProjectFile } from '../../api/types';
import { paths } from '../../app/nav';
import type { ProjectCapabilities } from '../../auth/permissions';
import { CadDrawingFileAiActions,DrawingFileAiActions } from '../../features/assistant/LegacyOperationActions';
import { isDrawingFileKind } from '../../features/assistant/refs';
import { cadDrawingFormat } from '../../features/cad/status';
import { useResource } from '../../hooks/useResource';
import { describeError } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import { guessKind } from '../../lib/catalog';
import { ArchivedChip,KindBadge } from '../ui/Badges';
import { ConfirmDialog,Dialog } from '../ui/Dialog';
import { Field } from '../ui/Form';
import { Menu,type MenuItem } from '../ui/Menu';
import { EmptyState,ErrorNotice,ErrorState,ErrorText,SkeletonRows,Spinner } from '../ui/States';
import { useToast } from '../ui/Toast';
import { FileMetaDialog,NewFileVersionDialog } from './FileDialogs';

interface QueueItem {
  key: string;
  file: File;
  kind: FileKind;
  state: 'queued' | 'uploading' | 'done' | 'failed';
  error?: unknown;
}

function UploadDialog({
  project,
  files,
  parts,
  fixedPart,
  onClose,
  onUploaded,
}: {
  project: Project;
  files: File[];
  parts: Part[];
  fixedPart?: Part;
  onClose: () => void;
  onUploaded: (count: number) => void;
}) {
  const { t, fmt } = useI18n();
  const [queue, setQueue] = useState<QueueItem[]>(() => files.map((f, i) => ({ key: `${i}-${f.name}-${f.size}`, file: f, kind: guessKind(f.name), state: 'queued' })));
  const [partId, setPartId] = useState(fixedPart?.id ?? '');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const update = (key: string, patch: Partial<QueueItem>) => setQueue((q) => q.map((x) => (x.key === key ? { ...x, ...patch } : x)));

  const start = async () => {
    setBusy(true);
    let ok = 0;
    let failed = 0;
    for (const item of queue) {
      if (item.state === 'done') continue;
      update(item.key, { state: 'uploading', error: undefined });
      try {
        await uploadFile(project.id, { file: item.file, kind: item.kind, part_id: partId || null, note: note.trim() });
        update(item.key, { state: 'done' });
        ok++;
      } catch (e) {
        update(item.key, { state: 'failed', error: e });
        failed++;
      }
    }
    setBusy(false);
    if (ok > 0) onUploaded(ok);
    // Everything went through: close. Otherwise stay open so failures can be read and retried.
    if (failed === 0) onClose();
  };

  const pending = queue.filter((x) => x.state !== 'done');
  const allDone = queue.length > 0 && pending.length === 0;

  return (
    <Dialog
      title={t('files.upload.title')}
      subtitle={<span className="mono">{fixedPart ? `${project.code} / ${fixedPart.number}` : project.code}</span>}
      onClose={onClose}
      busy={busy}
      size="lg"
      onSubmit={() => (allDone ? onClose() : void start())}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {allDone ? t('common.close') : t('common.cancel')}
          </button>
          {!allDone && (
            <button type="submit" className="btn primary" disabled={busy || pending.length === 0}>
              <Upload /> {busy ? t('files.upload.uploading') : t('files.upload.submit')}
            </button>
          )}
        </>
      }
    >
      <ul className="upload-list">
        {queue.map((item) => (
          <li key={item.key} className={`upload-item state-${item.state}`}>
            <KindBadge kind={item.kind} withLabel={false} />
            <div className="upload-name">
              <span className="ellip">{item.file.name}</span>
              <span className="faint small mono">{fmt.bytes(item.file.size)}</span>
              {item.state === 'failed' && (
                <span className="small err-text">
                  <ErrorText error={item.error} />
                </span>
              )}
            </div>
            <select
              className="select sm"
              value={item.kind}
              onChange={(e) => update(item.key, { kind: e.target.value as FileKind })}
              disabled={busy || item.state === 'done'}
              aria-label={t('files.upload.kindFor', { name: item.file.name })}
            >
              {FILE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(`fileKind.${k}`)}
                </option>
              ))}
            </select>
            <span className="upload-state small">
              {item.state === 'uploading' ? <Spinner /> : null}
              {t(`files.upload.${item.state}`)}
            </span>
            {item.state !== 'done' && !busy && (
              <button type="button" className="btn ghost icon sm" onClick={() => setQueue((q) => q.filter((x) => x.key !== item.key))} aria-label={t('files.upload.removeFile', { name: item.file.name })}>
                <X />
              </button>
            )}
          </li>
        ))}
      </ul>
      <div className="form-grid">
        {fixedPart ? (
          <Field label={t('files.upload.part')}>
            <input className="input mono" value={`${fixedPart.number} · ${fixedPart.name}`} disabled readOnly />
          </Field>
        ) : (
          <Field label={t('files.upload.part')} optional>
            <select className="select" value={partId} onChange={(e) => setPartId(e.target.value)} disabled={busy}>
              <option value="">{t('files.upload.noPart')}</option>
              {parts
                .filter((p) => !p.archived_at)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.number} · {p.name}
                  </option>
                ))}
            </select>
          </Field>
        )}
        <Field label={t('files.upload.note')} optional>
          <input className="input" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder={t('files.upload.notePlaceholder')} disabled={busy} />
        </Field>
      </div>
    </Dialog>
  );
}

interface FilesPanelProps { project: Project; caps: ProjectCapabilities; part?: Part; compact?: boolean }
export function FilesPanel(props: FilesPanelProps) {
  return <FilesPanelContent key={`${props.project.id}:${props.part?.id ?? ''}`} {...props} />;
}
function FilesPanelContent({ project, caps, part, compact = false }: FilesPanelProps) {
  const { t, tn, fmt } = useI18n();
  const toast = useToast();
  const [kind, setKind] = useState<FileKind | ''>('');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<{ id: string; label: string } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState<File[] | null>(null);
  const [versionFor, setVersionFor] = useState<ProjectFile | null>(null);
  const [renameFor, setRenameFor] = useState<ProjectFile | null>(null);
  const [archiveFor, setArchiveFor] = useState<ProjectFile | null>(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<unknown>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);

  const res = useResource(
    (signal) => listFiles(project.id, { part_id: part?.id, kind: kind || undefined, include_archived: includeArchived }, { signal }),
    [project.id, part?.id, kind, includeArchived],
  );
  // Part numbers for the "Part" column and the link selector (project mode only).
  const partsRes = useResource((signal) => listParts(project.id, { include_archived: true }, { signal }), [project.id], { enabled: !part });
  const parts = useMemo(() => (part ? [part] : (partsRes.data ?? [])), [part, partsRes.data]);
  const partById = useMemo(() => new Map(parts.map((p) => [p.id, p])), [parts]);
  const files = res.data ?? [];
  useAssistantPageContext({ projectId: project.id, partId: part?.id, label: selectedVersion?.label ?? t('area.resources'),
    object: { type: 'file', id: selectedVersion?.id ?? part?.id ?? project.id, version: selectedVersion?.id ?? null },
    target: selectedVersion ? { kind: 'file_version', object_id: selectedVersion.id, project_id: project.id } : null });
  const canEdit = caps.design && !part?.archived_at;

  const replaceFile = (f: ProjectFile) => res.setData((prev) => (prev ?? []).map((x) => (x.id === f.id ? f : x)));

  const download = async (v: FileVersion) => {
    try {
      await downloadBlob(v.blob_sha256, v.original_name);
    } catch (e) {
      const d = describeError(e);
      toast(
        <>
          {t('error.downloadFailed')} {t(d.key)} {d.code && <code className="error-code">{d.code}</code>}
        </>,
        { tone: 'err' },
      );
    }
  };

  const toggle = (id: string) =>
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (!canEdit) return;
    const list = Array.from(e.dataTransfer.files ?? []);
    if (list.length) setPending(list);
  };

  const actionsFor = (f: ProjectFile): MenuItem[] => {
    const latest = f.versions[0];
    const items: MenuItem[] = [];
    if (latest) items.push({ id: 'download', label: t('common.download'), icon: <Download />, onSelect: () => void download(latest) });
    if (canEdit) {
      items.push({ id: 'version', label: t('files.newVersion'), icon: <FilePlus2 />, onSelect: () => setVersionFor(f) });
      items.push({ id: 'rename', label: t('common.rename'), icon: <Pencil />, onSelect: () => setRenameFor(f) });
      items.push(
        f.archived_at
          ? { id: 'restore', label: t('common.restore'), icon: <ArchiveRestore />, separator: true, onSelect: () => void doArchive(f, false) }
          : { id: 'archive', label: t('common.archive'), icon: <Archive />, separator: true, danger: true, onSelect: () => setArchiveFor(f) },
      );
    }
    return items;
  };

  const doArchive = async (f: ProjectFile, archived: boolean) => {
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      const updated = await patchFile(f.id, { archived });
      toast(t(archived ? 'files.archivedToast' : 'files.restoredToast'));
      setArchiveFor(null);
      if (archived && !includeArchived) res.setData((prev) => (prev ?? []).filter((x) => x.id !== f.id));
      else replaceFile(updated);
    } catch (e) {
      if (archived) setArchiveError(e);
      else toast(<ErrorText error={e} />, { tone: 'err' });
    } finally {
      setArchiveBusy(false);
    }
  };

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (res.initial) body = <SkeletonRows cols={6} />;
  else if (!files.length && !kind && !includeArchived) body = <EmptyState compact title={t('files.empty.title')} body={t('files.empty.body')} />;
  else if (!files.length) body = <EmptyState compact title={t('common.noMatches')} />;
  else
    body = (
      <div className={`table-wrap ${res.loading ? 'is-refreshing' : ''}`}>
        <table className="t dense">
          <thead>
            <tr>
              <th className="exp">
                <span className="sr-only">{t('files.col.version')}</span>
              </th>
              <th>{t('files.col.name')}</th>
              <th className="hide-sm">{t('files.col.kind')}</th>
              {!part && <th className="hide-md">{t('files.col.part')}</th>}
              <th>{t('files.col.version')}</th>
              <th className="hide-sm r">{t('files.col.size')}</th>
              <th className="hide-md">{t('files.col.uploaded')}</th>
              <th className="act">
                <span className="sr-only">{t('common.actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => {
              const latest = f.versions[0];
              const open = expanded.has(f.id);
              const linked = f.part_id ? partById.get(f.part_id) : undefined;
              return (
                <Fragment key={f.id}>
                  <tr tabIndex={0} aria-selected={!!latest && selectedVersion?.id === latest.id} onFocus={() => { if (latest) setSelectedVersion({ id: latest.id, label: `${f.name} · v${latest.version}` }); }} onClick={() => { if (latest) setSelectedVersion({ id: latest.id, label: `${f.name} · v${latest.version}` }); }} onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); if (latest) setSelectedVersion({ id: latest.id, label: `${f.name} · v${latest.version}` }); } }} className={f.archived_at ? 'is-archived' : ''}>
                    <td className="exp">
                      <button type="button" className="btn ghost icon sm" aria-expanded={open} onClick={() => { toggle(f.id); if (latest) setSelectedVersion({ id: latest.id, label: `${f.name} · v${latest.version}` }); }} aria-label={t(open ? 'files.hideVersions' : 'files.showVersions', { name: f.name })}>
                        {open ? <ChevronDown /> : <ChevronRight />}
                      </button>
                    </td>
                    <td>
                      <div className="cell-main">
                        <span className="only-sm">
                          <KindBadge kind={f.kind} withLabel={false} />
                        </span>
                        <span className="ellip mono-ish">{f.name}</span>
                        {f.archived_at && <ArchivedChip />}
                      </div>
                      {latest?.note && <div className="faint small ellip">{latest.note}</div>}
                    </td>
                    <td className="hide-sm">
                      <KindBadge kind={f.kind} />
                    </td>
                    {!part && (
                      <td className="hide-md mono small">
                        {f.part_id ? linked ? <Link to={paths.part(linked.id, 'files')}>{linked.number}</Link> : <span className="faint">—</span> : <span className="faint">—</span>}
                      </td>
                    )}
                    <td className="mono small nowrap">
                      {latest ? t('files.versionN', { n: latest.version }) : '—'}
                      {f.versions.length > 1 && <span className="faint"> / {f.versions.length}</span>}
                    </td>
                    <td className="hide-sm r mono small">{fmt.bytes(latest?.size)}</td>
                    <td className="hide-md faint small nowrap" title={fmt.dateTime(latest?.created_at ?? f.created_at)}>
                      {fmt.relative(latest?.created_at ?? f.created_at)}
                    </td>
                    <td className="act">
                      <div className="row-actions">
                        {/* Unified AI workspace: customer drawings can be analysed or referenced from here too. */}
                        {latest && isDrawingFileKind(f.kind) && (
                          <DrawingFileAiActions
                            projectId={project.id}
                            file={f}
                            analyzeBlocked={!caps.design ? t('ar.dw.noDesign') : !f.part_id ? t('ar.dw.noPart') : null}
                          />
                        )}
                        {/* Native CAD drawings (DXF / DWG): rebuild a STEP model from the drawing with the assistant. */}
                        {latest && cadDrawingFormat(latest.original_name, f.kind) && (
                          <CadDrawingFileAiActions
                            projectId={project.id}
                            file={f}
                            compact
                            blocked={!caps.design ? t('cad.dw.reconstructNoDesign') : !f.part_id ? t('cad.dw.reconstructNoPart') : null}
                          />
                        )}
                        {latest && (
                          <button type="button" className="btn ghost icon sm hide-sm" onClick={() => { setSelectedVersion({ id: latest.id, label: `${f.name} · v${latest.version}` }); void download(latest); }} aria-label={`${t('common.download')} ${f.name}`} title={t('common.download')}>
                            <Download />
                          </button>
                        )}
                        <Menu label={t('common.moreActions')} trigger={<MoreHorizontal />} items={actionsFor(f)} />
                      </div>
                    </td>
                  </tr>
                  {open && (
                    <tr className="sub-row">
                      <td />
                      <td colSpan={part ? 6 : 7}>
                        <div className="versions">
                          <div className="versions-h">{tn('files.versionCount', f.versions.length, { count: f.versions.length })}</div>
                          <ol>
                            {f.versions.map((v) => (
                              <li key={v.id} tabIndex={0} aria-selected={selectedVersion?.id === v.id} onFocus={() => setSelectedVersion({ id: v.id, label: `${f.name} · v${v.version}` })} onClick={() => setSelectedVersion({ id: v.id, label: `${f.name} · v${v.version}` })} onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); setSelectedVersion({ id: v.id, label: `${f.name} · v${v.version}` }); } }}>
                                <span className="mono strong">{t('files.versionN', { n: v.version })}</span>
                                <span className="ellip mono-ish">{v.original_name}</span>
                                <span className="faint small ellip">{v.note || '—'}</span>
                                <span className="mono small faint">{fmt.bytes(v.size)}</span>
                                <span className="small faint nowrap">{fmt.dateTime(v.created_at)}</span>
                                <span className="mono small faint hide-sm" title={v.blob_sha256}>
                                  {v.blob_sha256.slice(0, 10)}
                                </span>
                                <button type="button" className="btn ghost icon sm" onClick={() => { setSelectedVersion({ id: v.id, label: `${f.name} · v${v.version}` }); void download(v); }} aria-label={`${t('common.download')} ${v.original_name} ${t('files.versionN', { n: v.version })}`}>
                                  <Download />
                                </button>
                              </li>
                            ))}
                          </ol>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    );

  return (
    <div
      className={`stack files-panel ${dragging ? 'dragging' : ''}`}
      onDragEnter={(e) => {
        if (!canEdit || !e.dataTransfer.types.includes('Files')) return;
        e.preventDefault();
        dragDepth.current++;
        setDragging(true);
      }}
      onDragOver={(e) => {
        if (canEdit && e.dataTransfer.types.includes('Files')) e.preventDefault();
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={onDrop}
    >
      {canEdit && compact ? (
        dragging && (
          <div className="dropzone active">
            <Upload aria-hidden />
            <div>{t('files.dropActive')}</div>
          </div>
        )
      ) : canEdit ? (
        <div className={`dropzone ${dragging ? 'active' : ''}`}>
          <Upload aria-hidden />
          <div>
            <div>
              {dragging ? (
                t('files.dropActive')
              ) : (
                <>
                  {t('files.dropPrefix')}{' '}
                  <button type="button" className="linkish" onClick={() => inputRef.current?.click()}>
                    {t('files.browse')}
                  </button>
                </>
              )}
            </div>
            <div className="faint small">{t('files.dropHint')}</div>
          </div>
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              const list = Array.from(e.target.files ?? []);
              e.target.value = '';
              if (list.length) setPending(list);
            }}
          />
        </div>
      ) : (
        <p className="faint small">{t('files.readOnly')}</p>
      )}

      <div className="toolbar">
        <select className="select" value={kind} onChange={(e) => setKind(e.target.value as FileKind | '')} aria-label={t('files.filter.kind')}>
          <option value="">{t('files.filter.allKinds')}</option>
          {FILE_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`fileKind.${k}`)}
            </option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={includeArchived} onChange={(e) => setIncludeArchived(e.target.checked)} />
          <span>{t('common.showArchived')}</span>
        </label>
        <span className="grow" />
        {res.data && <span className="faint mono small">{tn('files.count', files.length, { count: fmt.number(files.length) })}</span>}
        {compact && canEdit && (
          <>
            <button type="button" className="btn primary sm" onClick={() => inputRef.current?.click()} title={t('files.dropHint')}>
              <Upload /> {t('common.upload')}
            </button>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                const list = Array.from(e.target.files ?? []);
                e.target.value = '';
                if (list.length) setPending(list);
              }}
            />
          </>
        )}
      </div>

      {res.error && res.data ? <ErrorNotice error={res.error} onRetry={res.reload} /> : null}
      {body}

      {pending && (
        <UploadDialog
          project={project}
          files={pending}
          parts={parts}
          fixedPart={part}
          onClose={() => setPending(null)}
          onUploaded={(n) => {
            toast(tn('files.uploadedToast', n, { count: n }));
            res.reload();
          }}
        />
      )}
      {versionFor && (
        <NewFileVersionDialog
          file={versionFor}
          onClose={() => setVersionFor(null)}
          onDone={(f) => {
            setVersionFor(null);
            replaceFile(f);
            setExpanded((s) => new Set(s).add(f.id));
            toast(t('files.versionAddedToast', { n: f.versions[0]?.version ?? '' }));
          }}
        />
      )}
      {renameFor && (
        <FileMetaDialog
          file={renameFor}
          onClose={() => setRenameFor(null)}
          onDone={(f) => {
            setRenameFor(null);
            replaceFile(f);
            toast(t('files.renamedToast'));
          }}
        />
      )}
      {archiveFor && (
        <ConfirmDialog
          title={t('files.archive.title', { name: archiveFor.name })}
          body={t('files.archive.body')}
          confirmLabel={t('common.archive')}
          danger
          busy={archiveBusy}
          error={<ErrorNotice error={archiveError} />}
          onConfirm={() => void doArchive(archiveFor, true)}
          onClose={() => {
            setArchiveFor(null);
            setArchiveError(null);
          }}
        />
      )}
    </div>
  );
}
