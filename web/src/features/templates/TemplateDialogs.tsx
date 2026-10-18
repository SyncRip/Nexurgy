// Publish, deprecate and upgrade dialogs of a project template (contract §2.3, §2.8).

import { ArrowUpCircle, Info, Rocket, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { isApiError } from '../../api/client';
import { deprecateTemplateVersion, getPublicTemplate, publishWorkflow, upgradeWorkflow } from '../../api/endpoints';
import type { GraphError, TemplateVersion, Workflow } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { GraphIssueList, graphErrorsOf, normalizePolicy, PolicySummary, TPL_ERRORS, VersionNo } from './common';
import { LimitedTextarea } from '../../components/ui/LimitedText';

/**
 * Draft problems that block publishing: the server's publish_errors (validation without
 * input values, plus approval gates); older servers only send validation.
 */
export function publishBlockingErrors(w: Workflow): GraphError[] {
  if (w.publish_errors) return w.publish_errors;
  const inputs = new Set(w.graph.nodes.filter((n) => n.type === 'text_input' || n.type === 'file_input' || n.type === 'model_input').map((n) => n.id));
  return w.validation.filter((e) => !(e.code === 'missing_param' && e.node_id && inputs.has(e.node_id)));
}

export function PublishDialog({
  workflow,
  unsavedCanvas,
  onClose,
  onPublished,
  onReload,
}: {
  workflow: Workflow;
  /** The canvas has unsaved edits: they would not be part of the version. */
  unsavedCanvas?: boolean;
  onClose: () => void;
  onPublished: (v: TemplateVersion) => void;
  onReload?: () => void;
}) {
  const { t } = useI18n();
  const [notes, setNotes] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const local = publishBlockingErrors(workflow);
  const serverErrors = graphErrorsOf(error);
  const latest = workflow.latest_published;
  const notesErr = !notes.trim() ? t('tpl.publish.notesRequired') : null;
  const conflict = isApiError(error) && error.code === 'version_conflict';

  const submit = async () => {
    setTouched(true);
    if (notesErr || local.length || unsavedCanvas) return;
    setBusy(true);
    setError(null);
    try {
      onPublished(await publishWorkflow(workflow.id, { expected_version: workflow.version, notes: notes.trim() }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tpl.publish.title')}
      subtitle={t('tpl.publish.subtitle', { name: workflow.name, n: workflow.version })}
      size="md"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || local.length > 0 || unsavedCanvas}>
            <Rocket /> {busy ? t('common.working') : t('tpl.publish.submit', { n: (latest?.version_no ?? 0) + 1 })}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim">
          <Info aria-hidden />
          <span>{t('tpl.publish.explain')}</span>
        </div>
        {unsavedCanvas && (
          <div className="callout warn slim">
            <TriangleAlert aria-hidden />
            <span>{t('tpl.publish.unsavedCanvas')}</span>
          </div>
        )}
        <dl className="tpl-kv">
          <div>
            <dt>{t('tpl.publish.latest')}</dt>
            <dd>{latest ? <VersionNo n={latest.version_no} /> : <span className="faint">{t('tpl.chip.notPublished')}</span>}</dd>
          </div>
          <div>
            <dt>{t('tpl.publish.draft')}</dt>
            <dd className="mono">#{workflow.version}</dd>
          </div>
          <div className="span-2">
            <dt>{t('tpl.policy.title')}</dt>
            <dd>
              <PolicySummary policy={normalizePolicy(workflow.policy)} />
            </dd>
          </div>
        </dl>
        {local.length > 0 ? (
          <div className="callout err">
            <TriangleAlert aria-hidden />
            <div className="stack-sm grow">
              <span>{t('tpl.publish.fixFirst')}</span>
              <GraphIssueList errors={local} graph={workflow.graph} />
            </div>
          </div>
        ) : (
          <p className="faint small">{t('tpl.publish.checksNote')}</p>
        )}
        <Field label={t('tpl.publish.notes')} hint={t('tpl.publish.notesHint')} error={touched ? notesErr : null}>
          <LimitedTextarea className="input" rows={3} limit="description" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        <ErrorNotice error={error} overrides={TPL_ERRORS} />
        {serverErrors.length > 0 && <GraphIssueList errors={serverErrors} graph={workflow.graph} />}
        {conflict && onReload && (
          <div>
            <button type="button" className="btn sm" onClick={onReload}>
              {t('tk.reloadLatest')}
            </button>
          </div>
        )}
      </div>
    </Dialog>
  );
}

export function DeprecateDialog({ version, onClose, onDone }: { version: TemplateVersion; onClose: () => void; onDone: (v: TemplateVersion) => void }) {
  const { t } = useI18n();
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const reasonErr = !reason.trim() ? t('tpl.deprecate.reasonRequired') : null;
  const submit = async () => {
    setTouched(true);
    if (reasonErr) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await deprecateTemplateVersion(version.id, reason.trim()));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('tpl.deprecate.title', { n: version.version_no })}
      size="sm"
      role="alertdialog"
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn danger-solid" disabled={busy}>
            {busy ? t('common.working') : t('tpl.deprecate.submit')}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{t('tpl.deprecate.body')}</p>
        <Field label={t('tpl.deprecate.reason')} error={touched ? reasonErr : null}>
          <textarea className="input" rows={3} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <ErrorNotice error={error} overrides={TPL_ERRORS} />
      </div>
    </Dialog>
  );
}

/** Adopt a newer version of the public template this project template was imported from. */
export function UpgradeDialog({ workflow, unsavedCanvas, onClose, onUpgraded }: { workflow: Workflow; unsavedCanvas?: boolean; onClose: () => void; onUpgraded: (w: Workflow) => void }) {
  const { t, fmt } = useI18n();
  const lib = workflow.library;
  const pub = useResource((signal) => getPublicTemplate(lib!.public_template_id, { signal }), [lib?.public_template_id], { enabled: !!lib });
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const adopted = lib?.adopted_version_no ?? 0;
  const candidates = (pub.data?.versions ?? []).filter((v) => v.version_no > adopted && v.status !== 'deprecated').sort((a, b) => b.version_no - a.version_no);
  const selected = candidates.find((v) => v.id === choice) ?? candidates[0];

  const submit = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      onUpgraded(await upgradeWorkflow(workflow.id, { public_template_version_id: selected.id, expected_version: workflow.version }));
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t('tpl.upgrade.title')}
      subtitle={lib ? t('tpl.library.source', { name: lib.name, n: adopted || '?' }) : undefined}
      busy={busy}
      onClose={onClose}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !selected}>
            <ArrowUpCircle /> {busy ? t('common.working') : selected ? t('tpl.upgrade.submit', { n: selected.version_no }) : t('tpl.upgrade.submitNone')}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="callout info slim">
          <Info aria-hidden />
          <span>{t('tpl.upgrade.explain')}</span>
        </div>
        {(workflow.draft_dirty || unsavedCanvas) && (
          <div className="callout warn slim">
            <TriangleAlert aria-hidden />
            <span>{t('tpl.upgrade.draftWarn')}</span>
          </div>
        )}
        {pub.error && !pub.data ? (
          <ErrorNotice error={pub.error} onRetry={pub.reload} />
        ) : !pub.data ? (
          <SkeletonRows rows={3} cols={3} />
        ) : !candidates.length ? (
          <p className="faint">{t('tpl.upgrade.none')}</p>
        ) : (
          <div className="tpl-pick-list" role="radiogroup" aria-label={t('tpl.upgrade.choose')}>
            {candidates.map((v) => (
              <label key={v.id} className={`tpl-pick-row ${selected?.id === v.id ? 'selected' : ''}`}>
                <input type="radio" name="tpl-upgrade" checked={selected?.id === v.id} onChange={() => setChoice(v.id)} />
                <VersionNo n={v.version_no} />
                <span className="grow tpl-xs">
                  <span className="small">{v.notes || <span className="faint">{t('tpl.versions.noNotes')}</span>}</span>
                  <span className="faint small">{fmt.dateTime(v.published_at)}</span>
                </span>
              </label>
            ))}
          </div>
        )}
        <ErrorNotice error={error} overrides={TPL_ERRORS} />
      </div>
    </Dialog>
  );
}
