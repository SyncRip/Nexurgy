// Ways to start work from the workbench and the parts list (prototype #home
// "Upload drawings / STEP", "New part", chips under the AI box). All of them reuse
// existing flows: a drawing upload becomes the AI's explicit target with the
// matching operation preselected (the same path as the model tab's "Rebuild
// with AI"); a STEP file is stored in the project files (importing it needs a
// part); "New part" is the part dialog; a template run is StartRunDialog.

import { Plus, Upload } from 'lucide-react';
import { useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { listMembers, uploadFile } from '../../api/endpoints';
import type { Part, Project } from '../../api/types';
import { paths } from '../../app/nav';
import { useUser } from '../../auth/AuthProvider';
import { Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useResource } from '../../hooks/useResource';
import { describeError } from '../../i18n/errors';
import { useI18n } from '../../i18n/I18nProvider';
import { guessKind } from '../../lib/catalog';
import { PartDialog } from '../../pages/project/PartDialogs';
import { useAssistant } from '../assistant/AssistantProvider';
import { fileVersionRef, isDrawingFileKind } from '../assistant/refs';
import { cadDrawingFormat } from '../cad/status';

const DRAWING_ACCEPT = '.pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.dxf,.dwg';
const ANY_ACCEPT = `${DRAWING_ACCEPT},.step,.stp`;

export interface UploadStart {
  /** Open the file chooser (drawings only, or drawings and STEP). */
  pick: (drawingsOnly?: boolean) => void;
  /** Name of the file being uploaded. */
  busy: string | null;
  /** The hidden file input; render it once. */
  input: ReactNode;
}

export function useUploadStart(projectId: string | null): UploadStart {
  const { t } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();
  const assistant = useAssistant();
  const ref = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const start = async (file: File) => {
    if (!projectId) return;
    setBusy(file.name);
    try {
      const pf = await uploadFile(projectId, { file, kind: guessKind(file.name) });
      const v = pf.versions[0];
      const cad = v ? cadDrawingFormat(v.original_name, pf.kind) : null;
      if (v && (cad || isDrawingFileKind(pf.kind))) {
        // An explicit user action: this exact file version becomes the target.
        const added = await assistant.addContext(fileVersionRef(projectId, v.id), { asTarget: true, operation: cad ? 'drawing_reconstruct' : 'drawing_intake' });
        if (added) toast(t('home.upload.toAi', { name: file.name }), { tone: 'info' });
      } else {
        toast(t('home.upload.stored', { name: file.name }), { tone: 'info', action: { label: t('home.upload.openFiles'), onClick: () => navigate(paths.resources('files', projectId)) } });
      }
    } catch (e) {
      toast(t('home.upload.failed', { reason: t(describeError(e).key) }), { tone: 'err' });
    } finally {
      setBusy(null);
    }
  };

  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) void start(file);
  };

  return {
    busy,
    pick: (drawingsOnly = false) => {
      const el = ref.current;
      if (!el || busy) return;
      el.accept = drawingsOnly ? DRAWING_ACCEPT : ANY_ACCEPT;
      el.click();
    },
    input: <input ref={ref} type="file" hidden onChange={onChange} aria-label={t('home.upload.pick')} tabIndex={-1} />,
  };
}

export function UploadStartButton({ projectId, short }: { projectId: string; short?: boolean }) {
  const { t } = useI18n();
  const up = useUploadStart(projectId);
  return (
    <>
      <button type="button" className="btn" onClick={() => up.pick(false)} disabled={!!up.busy} title={up.busy ? t('home.upload.busy', { name: up.busy }) : undefined}>
        {up.busy ? <Spinner /> : <Upload />} {short ? t('home.upload.short') : t('home.upload')}
      </button>
      {up.input}
      {up.busy && (
        <span className="sr-only" role="status">
          {t('home.upload.busy', { name: up.busy })}
        </span>
      )}
    </>
  );
}

/** "New part": the existing part dialog. */
export function NewPartButton({ project, folders = [], onSaved }: { project: Project; folders?: string[]; onSaved: (p: Part) => void }) {
  const { t } = useI18n();
  const me = useUser();
  const [open, setOpen] = useState(false);
  const members = useResource((signal) => listMembers(project.id, { signal }), [project.id], { enabled: open });
  return (
    <>
      <button type="button" className="btn primary" onClick={() => setOpen(true)}>
        <Plus /> {t('parts.new')}
      </button>
      {open && (
        <PartDialog
          mode="create"
          project={project}
          members={members.data}
          folders={folders}
          meId={me.id}
          onClose={() => setOpen(false)}
          onSaved={(p) => {
            setOpen(false);
            onSaved(p);
          }}
        />
      )}
    </>
  );
}
