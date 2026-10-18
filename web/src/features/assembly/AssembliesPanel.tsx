// Assemblies of the current project (Parts area, "Assembly" view): list with the latest revision's counts and
// check / policy state, structured STEP import and a new assembly from an existing part.

import { Layers,Plus,Upload,XCircle } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { createAssembly,importAssembly,listAssemblies } from '../../api/assemblies';
import { uploadFile } from '../../api/endpoints';
import type { ProjectCapabilities } from '../../auth/permissions';
import { CheckStatusChip,PolicyChip,UnverifiedChip } from '../../components/engineering/EngineeringChips';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field,SearchInput } from '../../components/ui/Form';
import { EmptyState,ErrorNotice,ErrorState,SkeletonRows } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { CoverageMarks,coverageLine } from '../orchestration/Coverage';
import './assembly.css';
import { DefinitionPicker,definitionInput,type DefinitionChoice } from './DefinitionPicker';
import { assemblyListStatus,safeCheckStatus } from './model';

export const assemblyPath = (id: string, tab = 'structure') => `/assemblies/${id}/${tab}`;

/** S5: the policy of a new assembly — the project default unless the person picks one (shown with its source). */
export function AssembliesPanel({ projectId, caps }: { projectId: string; caps: ProjectCapabilities }) {
  const { t, fmt } = useI18n();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const [dialog, setDialog] = useState<'import' | 'new' | null>(null);
  const res = useResource((signal) => listAssemblies(projectId, { q: dq || undefined }, { signal }), [projectId, dq]);

  let body;
  if (res.error && !res.data) body = <ErrorState error={res.error} onRetry={res.reload} />;
  else if (!res.data) body = <SkeletonRows rows={4} cols={5} />;
  else if (!res.data.length)
    body = <EmptyState compact icon={<Layers />} title={t('asm.list.empty.title')} body={caps.design ? t('asm.list.empty.body') : t('asm.list.empty.readOnly')} />;
  else
    body = (
      <ul className="asm-list">
        {res.data.map((a) => {
          const r = a.latest_revision;
          const state = r ? assemblyListStatus(r) : null;
          return (
            <li key={a.id}>
              <button type="button" className="asm-card" onClick={() => navigate(assemblyPath(a.id))}>
                <span className="asm-card-top">
                  <span className="mono">{a.number}</span>
                  <strong className="ellip">{a.name}</strong>
                  <span className="mono faint">{r ? r.label : t('asm.list.noRevision')}</span>
                </span>
                {r && (
                  <span className="asm-card-meta faint small">
                    {t('asm.list.leaves', { n: r.stats.leaf_instances })} · {t('asm.list.defs', { n: r.stats.part_definitions })}
                    {a.used_by_assemblies > 0 ? ` · ${t('asm.list.usedBy', { n: a.used_by_assemblies })}` : ''} · {fmt.relative(a.updated_at)}
                  </span>
                )}
                {state?.coverage && <span className="small faint">{coverageLine(t, state.coverage)}</span>}
                {r && state && (
                  <span className="row wrap">
                    {state.failed && <Chip tone="err" icon={<XCircle aria-hidden />}>{t('rev.failed')}</Chip>}
                    <CheckStatusChip status={safeCheckStatus(r.check_status)} />
                    <PolicyChip policy={r.execution_policy} />
                    <CoverageMarks coverage={state.coverage} unverified={state.incompleteUnverified} />
                    {state.completedUnverified && <UnverifiedChip />}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    );

  return (
    <div className="stack">
      <div className="pp-toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t('asm.list.search')} />
        <span className="grow" />
        {caps.design && (
          <>
            <button type="button" className="btn" onClick={() => setDialog('import')}>
              <Upload aria-hidden /> {t('asm.import')}
            </button>
            <button type="button" className="btn primary" onClick={() => setDialog('new')}>
              <Plus aria-hidden /> {t('asm.new')}
            </button>
          </>
        )}
      </div>
      {body}
      {dialog === 'import' && <ImportDialog projectId={projectId} onClose={() => setDialog(null)} onDone={(id) => navigate(assemblyPath(id))} />}
      {dialog === 'new' && <NewDialog projectId={projectId} onClose={() => setDialog(null)} onDone={(id) => navigate(assemblyPath(id))} />}
    </div>
  );
}

function ImportDialog({ projectId, onClose, onDone }: { projectId: string; onClose: () => void; onDone: (id: string) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [number, setNumber] = useState('');
  const [phase, setPhase] = useState<'idle' | 'upload' | 'import'>('idle');
  const [error, setError] = useState<unknown>(null);
  const busy = phase !== 'idle';
  const go = async () => {
    if (!file) return;
    setError(null);
    try {
      setPhase('upload');
      const f = await uploadFile(projectId, { file, kind: 'model_step' });
      setPhase('import');
      const out = await importAssembly(projectId, { file_version_id: f.versions[0].id, number: number.trim() || undefined });
      const c = out.import.counts;
      toast(t('asm.import.done', { defs: c.part_definitions, leaves: c.leaf_instances, asms: c.assembly_definitions }));
      onDone(out.assembly.id);
    } catch (e) {
      setError(e);
      setPhase('idle');
    }
  };
  return (
    <Dialog
      title={t('asm.import.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void go()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!file || busy}>
            {phase === 'upload' ? t('asm.import.uploading') : phase === 'import' ? t('asm.import.importing') : t('asm.import.go')}
          </button>
        </>
      }
    >
      <p className="small faint">{t('asm.import.body')}</p>
      <Field label={t('asm.import.file')}>
        <input className="input" type="file" accept=".step,.stp,.STEP,.STP" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </Field>
      <Field label={t('asm.import.number')} hint={t('asm.import.numberHint')} optional>
        <input className="input mono" value={number} onChange={(e) => setNumber(e.target.value)} maxLength={80} />
      </Field>
      <></>
      {error ? <ErrorNotice error={error} /> : null}
    </Dialog>
  );
}

function NewDialog({ projectId, onClose, onDone }: { projectId: string; onClose: () => void; onDone: (id: string) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const [number, setNumber] = useState('');
  const [name, setName] = useState('');
  const [choice, setChoice] = useState<DefinitionChoice>({ kind: 'part', itemId: '', revisionId: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ok = number.trim() !== '' && !!choice.itemId;
  const go = async () => {
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      const out = await createAssembly(projectId, {
        number: number.trim(),
        name: name.trim() || number.trim(),
        components: [{ definition: definitionInput(choice), placement: { translate: [0, 0, 0], rotate: [0, 0, 0] }, fixed: true }],

      });
      toast(t('asm.new.created', { number: out.assembly.number }));
      onDone(out.assembly.id);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('asm.new.title')}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void go()}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ok || busy}>
            {busy ? t('common.creating') : t('common.create')}
          </button>
        </>
      }
    >
      <div className="form-grid">
        <Field label={t('asm.new.number')}>
          <input className="input mono" value={number} onChange={(e) => setNumber(e.target.value)} maxLength={80} required />
        </Field>
        <Field label={t('asm.new.name')} optional>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        </Field>
      </div>
      <p className="small faint">{t('asm.new.firstPartHint')}</p>
      <DefinitionPicker projectId={projectId} value={choice} onChange={setChoice} />
      <></>
      {error ? <ErrorNotice error={error} /> : null}
    </Dialog>
  );
}
