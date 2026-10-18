// New workflow for a part (R1.2 prototype): start from a built-in template, a
// published project template (its latest version is copied), or a blank
// canvas. The result is the part's own workflow — editing it never changes the
// template, and templates only change when someone saves one explicitly.

import { FilePlus2, LayoutTemplate, Library } from 'lucide-react';
import { useState } from 'react';
import { createWorkflow, getTemplateVersion, listWorkflowTemplates, listWorkflows } from '../../../api/endpoints';
import type { Part, Workflow, WorkflowGraph } from '../../../api/types';
import { Dialog } from '../../../components/ui/Dialog';
import { Field } from '../../../components/ui/Form';
import { ErrorNotice, SkeletonRows } from '../../../components/ui/States';
import { useResource } from '../../../hooks/useResource';
import { useI18n } from '../../../i18n/I18nProvider';
import { localized } from '../../workflow/model';

type Source = { kind: 'builtin'; key: string; name: string } | { kind: 'project'; workflow: Workflow } | { kind: 'blank' };

const BLANK: WorkflowGraph = { nodes: [], edges: [], groups: [] };

export function NewFlowDialog({ part, onClose, onCreated }: { part: Part; onClose: () => void; onCreated: (w: Workflow) => void }) {
  const { t, lang } = useI18n();
  const builtins = useResource((signal) => listWorkflowTemplates({ signal }), []);
  const projectTemplates = useResource((signal) => listWorkflows(part.project_id, {}, { signal }), [part.project_id]);
  const published = (projectTemplates.data ?? []).filter((w) => w.latest_published);
  const [source, setSource] = useState<Source>({ kind: 'blank' });
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const suggested =
    source.kind === 'builtin' ? source.name : source.kind === 'project' ? source.workflow.name : t('pflow.new.blankName');
  const finalName = (name.trim() || `${part.number} · ${suggested}`).slice(0, 200);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      let w: Workflow;
      if (source.kind === 'builtin') w = await createWorkflow(part.project_id, { name: finalName, template_key: source.key, part_id: part.id });
      else if (source.kind === 'project') {
        // Copy the latest published version, not the template's unpublished draft.
        const v = await getTemplateVersion(source.workflow.latest_published!.id);
        w = await createWorkflow(part.project_id, { name: finalName, description: source.workflow.description, graph: v.graph, part_id: part.id });
      } else w = await createWorkflow(part.project_id, { name: finalName, graph: BLANK, part_id: part.id });
      onCreated(w);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const card = (key: string, selected: boolean, onPick: () => void, icon: React.ReactNode, title: string, desc: string) => (
    <button key={key} type="button" role="radio" aria-checked={selected} className={`wf-tpl-card ${selected ? 'selected' : ''}`} onClick={onPick}>
      <span className="wf-tpl-ico">{icon}</span>
      <span className="wf-tpl-name">{title}</span>
      <span className="wf-tpl-desc">{desc}</span>
    </button>
  );

  return (
    <Dialog
      title={t('pflow.new.title')}
      subtitle={`${part.number} · ${part.name}`}
      size="lg"
      onClose={onClose}
      busy={busy}
      onSubmit={() => void create()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('common.saving') : t('pflow.new.create')}
          </button>
        </>
      }
    >
      <p className="muted small">{t('pflow.new.body')}</p>
      <div className="pflow-sources" role="radiogroup" aria-label={t('pflow.new.source')}>
        {card('blank', source.kind === 'blank', () => setSource({ kind: 'blank' }), <FilePlus2 aria-hidden />, t('pflow.new.blank'), t('pflow.new.blankDesc'))}
        {builtins.data?.map((b) =>
          card(
            `b:${b.key}`,
            source.kind === 'builtin' && source.key === b.key,
            () => setSource({ kind: 'builtin', key: b.key, name: localized(b.name, lang) }),
            <LayoutTemplate aria-hidden />,
            localized(b.name, lang),
            localized(b.description, lang),
          ),
        )}
        {published.map((w) =>
          card(
            `p:${w.id}`,
            source.kind === 'project' && source.workflow.id === w.id,
            () => setSource({ kind: 'project', workflow: w }),
            <Library aria-hidden />,
            w.name,
            t('pflow.new.projectTemplate', { v: w.latest_published!.version_no }),
          ),
        )}
      </div>
      {(builtins.initial || projectTemplates.initial) && <SkeletonRows rows={2} cols={3} />}
      <Field label={t('pflow.new.name')} optional>
        <input className="input" value={name} maxLength={200} placeholder={`${part.number} · ${suggested}`} onChange={(e) => setName(e.target.value)} />
      </Field>
      <ErrorNotice error={error ?? builtins.error ?? projectTemplates.error} />
    </Dialog>
  );
}
