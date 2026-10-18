import { BookmarkPlus } from 'lucide-react';
import { useLayoutEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { createTemplateFromRun } from '../../api/endpoints';
import type { Project, WorkflowRun } from '../../api/types';
import { paths } from '../../app/nav';
import { ErrorNotice, Spinner } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import type { ErrorOverrides } from '../../i18n/errors';
import { useT } from '../../i18n/I18nProvider';
import { newRequestId } from '../assistant/model';
import { TPL_ERRORS, useTemplateAccess } from './common';

const SAVE_ERRORS: ErrorOverrides = {
  ...TPL_ERRORS,
  run_not_succeeded: 'tpl.fromRun.notSucceeded',
  template_source_unsupported: 'tpl.fromRun.unsupported',
  op_id_reused: 'tpl.fromRun.requestReused',
  project_not_active: 'tpl.fromRun.projectInactive',
  project_archived: 'tpl.fromRun.projectInactive',
};

type Props = {
  project: Pick<Project, 'id' | 'my_role' | 'status'> | null | undefined;
  run: Pick<WorkflowRun, 'id' | 'project_id' | 'status'>;
};

/** The selected completed run is the source; no editable graph or original metadata is sent. */
export function SaveRunTemplateAction({ project, run }: Props) {
  const canManage = useTemplateAccess(project);
  if (!canManage || project?.id !== run.project_id || run.status !== 'succeeded') return null;
  return <SaveRunTemplateButton key={run.id} runId={run.id} />;
}

function SaveRunTemplateButton({ runId }: { runId: string }) {
  const t = useT();
  const toast = useToast();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const mounted = useRef(false);
  const request = useRef<Parameters<typeof createTemplateFromRun>[1] | null>(null);

  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const save = async () => {
    if (inFlight.current || !mounted.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    // Keep both fields stable after an ambiguous failure, including across a locale change.
    request.current ??= { name: t('tpl.fromRun.name'), client_request_id: newRequestId() };
    try {
      const template = await createTemplateFromRun(runId, request.current);
      if (!mounted.current) return;
      toast(t('tpl.fromRun.created'), { durationMs: 7000 });
      navigate(paths.workflow(template.id, 'config'));
    } catch (e) {
      if (!mounted.current) return;
      setError(e);
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="tpl-save-run">
      <button type="button" className="btn sm" onClick={() => void save()} disabled={busy} aria-busy={busy} title={t('tpl.fromRun.hint')}>
        {busy ? <Spinner /> : <BookmarkPlus aria-hidden />}
        {t(busy ? 'tpl.fromRun.creating' : 'tpl.fromRun.action')}
      </button>
      <ErrorNotice error={error} overrides={SAVE_ERRORS} onRetry={() => void save()} />
    </div>
  );
}
