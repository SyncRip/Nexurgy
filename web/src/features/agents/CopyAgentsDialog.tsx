// "Copy from another project" (prototype ag-import): pick a project you are a
// member of, tick its agents, and each becomes a new profile of this project
// (POST /projects/{id}/agent-profiles with the source configuration). Copies
// start at v1 and belong to this project only; nothing of the source changes.

import { Check } from 'lucide-react';
import { useMemo, useState } from 'react';
import { createAgentProfile, isSelectableRuntime, listAgentProfiles, type AgentProfile } from '../../api/agents';
import { listProjects } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { ErrorNotice } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { PurposeIcon, purposeLabel } from './common';
import { copyBody, uniqueName } from './model';

export function CopyAgentsDialog({ project, existing, onClose, onCopied }: { project: Project; existing: AgentProfile[]; onClose: () => void; onCopied: (created: AgentProfile[]) => void }) {
  const { t } = useI18n();
  const projects = useResource((signal) => listProjects({}, { signal }), []);
  const sources = (projects.data ?? []).filter((p) => p.id !== project.id);
  const [sourceId, setSourceId] = useState('');
  const source = useResource((signal) => listAgentProfiles(sourceId, {}, { signal }), [sourceId], { enabled: !!sourceId });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<{ name: string; error: unknown }[]>([]);
  const [created, setCreated] = useState<AgentProfile[]>([]);

  // Names the copies will get (free in this project, and distinct from each other).
  const names = useMemo(() => {
    const taken = [...existing.map((a) => a.name), ...created.map((a) => a.name)];
    const out = new Map<string, string>();
    for (const a of source.data ?? []) {
      if (!picked.has(a.id) || !isSelectableRuntime(a.runtime ?? 'native')) continue;
      const n = uniqueName(a.name, [...taken, ...out.values()], t('fl.ag.copyWord'));
      out.set(a.id, n);
    }
    return out;
  }, [source.data, picked, existing, created, t]);

  const toggle = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const submit = async () => {
    const list = (source.data ?? []).filter((a) => picked.has(a.id) && isSelectableRuntime(a.runtime ?? 'native'));
    if (!list.length) return;
    setBusy(true);
    setFailed([]);
    const ok: AgentProfile[] = [];
    const bad: { name: string; error: unknown }[] = [];
    for (const a of list) {
      try {
        ok.push(await createAgentProfile(project.id, copyBody(a, names.get(a.id) ?? a.name)));
      } catch (e) {
        bad.push({ name: a.name, error: e });
      }
    }
    setBusy(false);
    const all = [...created, ...ok];
    if (!bad.length) {
      onCopied(all);
      return;
    }
    // Keep the dialog: the failed ones stay ticked so they can be retried after a fix.
    setCreated(all);
    setFailed(bad);
    setPicked(new Set(list.filter((a) => bad.some((b) => b.name === a.name)).map((a) => a.id)));
  };

  const list = (source.data ?? []).filter((a) => isSelectableRuntime(a.runtime ?? 'native'));
  return (
    <Dialog
      title={t('fl.ag.copyTitle', { code: project.code })}
      subtitle={t('fl.ag.copySubtitle')}
      size="lg"
      busy={busy}
      onClose={() => (created.length ? onCopied(created) : onClose())}
      onSubmit={() => void submit()}
      footer={
        <>
          <button type="button" className="btn" onClick={() => (created.length ? onCopied(created) : onClose())} disabled={busy}>
            {created.length ? t('common.close') : t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={busy || !picked.size}>
            {busy ? t('common.saving') : t('fl.ag.copyN', { n: picked.size })}
          </button>
        </>
      }
    >
      <div className="stack">
        <Field label={t('fl.ag.copySource')}>
          <select
            className="select"
            value={sourceId}
            onChange={(e) => {
              setSourceId(e.target.value);
              setPicked(new Set());
            }}
            data-autofocus
          >
            <option value="">{t('fl.ag.copyPickProject')}</option>
            {sources.map((p: Project) => (
              <option key={p.id} value={p.id}>
                {p.code} · {p.name}
              </option>
            ))}
          </select>
        </Field>
        {projects.error ? <ErrorNotice error={projects.error} onRetry={projects.reload} /> : null}
        {projects.data && !sources.length && <p className="faint small">{t('fl.ag.copyNoProjects')}</p>}
        {source.error ? <ErrorNotice error={source.error} onRetry={source.reload} /> : null}
        {sourceId && source.data && !list.length && <p className="faint small">{t('fl.ag.copyNoAgents')}</p>}
        {list.length > 0 && (
          <div className="ag-cards" role="group" aria-label={t('fl.ag.copyPick')}>
            {list.map((a) => {
              const on = picked.has(a.id);
              return (
                <button key={a.id} type="button" className="ag-card" aria-pressed={on} onClick={() => toggle(a.id)} disabled={busy}>
                  <span className="ag-card-mark">{on && <Check aria-hidden />}</span>
                  <span className="ag-card-title">
                    <PurposeIcon purpose={a.purpose} />
                    {a.name}
                  </span>
                  <span className="ag-card-desc">
                    {purposeLabel(t, a.purpose)} · v{a.version}
                    {on && names.get(a.id) !== a.name ? ` · ${t('fl.ag.copyAs', { name: names.get(a.id) ?? '' })}` : ''}
                  </span>
                </button>
              );
            })}
          </div>
        )}
        <p className="faint small">{t('fl.ag.copyNote')}</p>
        {created.length > 0 && <p className="small">{t('fl.ag.copyDone', { n: created.length })}</p>}
        {failed.map((f, i) => (
          <div key={i} className="stack-sm">
            <span className="small strong">{f.name}</span>
            <ErrorNotice error={f.error} overrides={{ model_config_unusable: 'agents.err.model_config_unusable', name_taken: 'agents.err.name_taken' }} />
          </div>
        ))}
      </div>
    </Dialog>
  );
}
