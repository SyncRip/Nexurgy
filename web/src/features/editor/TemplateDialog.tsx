// "Standard parts" dialog: pick a part template from the server catalog, edit its typed parameters (unit, range,
// default; checked here and again by the server), see the spec label / representation / ports, then create a
// revision (a new design of the part, or appended to the current revision). Nothing is hidden: the result is an
// ordinary op list the editor shows and the kernel / feature checks replay.

import { useMemo, useState } from 'react';
import { isApiError } from '../../api/client';
import { createTemplateRevision, instantiateTemplate, listTemplates, type TemplateCatalogEntry, type TemplateMeta, type TemplateParams } from '../../api/geometry';
import type { ModelRevision, UUID } from '../../api/types';
import { Dialog } from '../../components/ui/Dialog';
import { ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import type { MessageKey } from '../../i18n/en';
import { NumField, SelectField } from '../part/model/fields';
import { templateParamIssue } from './geometry';
import './geometry.css';

export interface TemplateDialogProps {
  partId: UUID;
  /** The revision an append goes onto (null: no revision yet → replace only). */
  parentId: UUID | null;
  /** Unsaved edits block the insert (the new revision would drop them). */
  dirty: boolean;
  onClose: () => void;
  onCreated: (rev: ModelRevision) => void;
}

function defaults(tpl: TemplateCatalogEntry): TemplateParams {
  return Object.fromEntries(tpl.params.map((p) => [p.name, p.default]));
}

export function TemplateDialog({ partId, parentId, dirty, onClose, onCreated }: TemplateDialogProps) {
  const { t, lang } = useI18n();
  const cat = useResource((signal) => listTemplates({ signal }), []);
  const templates = cat.data?.templates ?? [];
  const [tplId, setTplId] = useState<string | null>(null);
  const tpl = templates.find((x) => x.id === tplId) ?? templates[0] ?? null;
  const [params, setParams] = useState<Record<string, TemplateParams>>({});
  const values = tpl ? { ...defaults(tpl), ...(params[tpl.id] ?? {}) } : {};
  const [mode, setMode] = useState<'replace' | 'append'>('replace');
  const [checked, setChecked] = useState<{ key: string; meta: TemplateMeta; ops: number } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<'check' | 'create' | null>(null);

  const issues = useMemo(() => {
    const out: Record<string, { key: MessageKey; vars?: Record<string, string | number> }> = {};
    for (const p of tpl?.params ?? []) {
      const issue = templateParamIssue(p, values[p.name]);
      if (issue) out[p.name] = issue;
    }
    return out;
  }, [tpl, values]);
  const serverProblems: Record<string, string> = {};
  if (isApiError(error)) {
    for (const pr of ((error.details as { problems?: { param: string; message: string }[] })?.problems ?? [])) serverProblems[pr.param] = pr.message;
  }
  const key = tpl ? JSON.stringify([tpl.id, values]) : '';
  const valid = !!tpl && Object.keys(issues).length === 0;

  const setValue = (name: string, v: number | string) => {
    if (!tpl) return;
    setParams((prev) => ({ ...prev, [tpl.id]: { ...(prev[tpl.id] ?? {}), [name]: v } }));
    setError(null);
  };

  const check = async () => {
    if (!tpl) return;
    setBusy('check');
    setError(null);
    try {
      const res = await instantiateTemplate(tpl.id, values);
      setChecked({ key, meta: res.meta, ops: res.ops.length });
    } catch (e) {
      setError(e);
      setChecked(null);
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    if (!tpl) return;
    setBusy('create');
    setError(null);
    try {
      const rev = await createTemplateRevision(partId, { template_id: tpl.id, params: values, parent_id: parentId, mode: parentId ? mode : 'replace' });
      onCreated(rev);
    } catch (e) {
      setError(e);
      setBusy(null);
    }
  };

  const groups = useMemo(() => {
    const out = new Map<string, TemplateCatalogEntry[]>();
    for (const x of templates) out.set(x.category, [...(out.get(x.category) ?? []), x]);
    return [...out.entries()];
  }, [templates]);

  return (
    <Dialog
      title={t('geo.tpl.title')}
      subtitle={t('geo.tpl.sub')}
      size="lg"
      onClose={onClose}
      busy={!!busy}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={!!busy}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn" onClick={() => void check()} disabled={!valid || !!busy}>
            {t('geo.tpl.preview')}
          </button>
          <button type="button" className="btn primary" onClick={() => void create()} disabled={!valid || !!busy || dirty}>
            {busy === 'create' ? t('geo.tpl.inserting') : t('geo.tpl.insert')}
          </button>
        </>
      }
    >
      {cat.initial && cat.loading ? (
        <SkeletonRows rows={4} />
      ) : cat.error ? (
        <ErrorNotice error={cat.error} onRetry={cat.reload} />
      ) : (
        <div className="ws-tpl">
          <nav className="ws-tpl-list" aria-label={t('geo.tpl.title')}>
            {groups.map(([category, items]) => (
              <div key={category}>
                <div className="faint small">{t(`geo.tpl.cat.${category}` as MessageKey)}</div>
                {items.map((x) => (
                  <button key={x.id} type="button" className={`btn sm ghost ws-tpl-item ${tpl?.id === x.id ? 'active' : ''}`} aria-pressed={tpl?.id === x.id} onClick={() => {
                    setTplId(x.id);
                    setChecked(null);
                    setError(null);
                  }}>
                    {x.title[lang] ?? x.title.en}
                  </button>
                ))}
              </div>
            ))}
          </nav>
          {tpl && (
            <div className="ws-tpl-form stack">
              <div className="row wrap">
                <strong>{tpl.title[lang] ?? tpl.title.en}</strong>
                <span className={`chip ${tpl.representation === 'exact' ? 'ok' : 'warn'}`}>{t(`geo.repr.${tpl.representation}`)}</span>
                <span className="chip">{t(`geo.proc.${tpl.procurement}`)}</span>
                <span className="faint small mono">v{tpl.version}</span>
              </div>
              {tpl.standard && (
                <p className="faint small">
                  {t('geo.tpl.standard')}: {tpl.standard}
                </p>
              )}
              <span className="ws-f-label">{t('geo.tpl.params')}</span>
              {tpl.params.map((p) => {
                const label = p.label[lang] || p.label.en || p.name;
                const issue = issues[p.name];
                const errText = issue ? t(issue.key, issue.vars) : serverProblems[p.name];
                if (p.type === 'choice')
                  return (
                    <SelectField<string> key={p.name} label={label} value={String(values[p.name])} options={(p.choices ?? []).map((c) => ({ value: c, label: c }))} onChange={(v) => setValue(p.name, v)} error={errText} />
                  );
                if (p.type === 'text')
                  return (
                    <label key={p.name} className="ws-f">
                      <span className="ws-f-label">{label}</span>
                      <input className="input sm" value={String(values[p.name] ?? '')} maxLength={120} onChange={(e) => setValue(p.name, e.target.value)} />
                    </label>
                  );
                return (
                  <NumField
                    key={p.name}
                    label={label}
                    unit={p.unit === 'deg' ? '°' : (p.unit ?? undefined)}
                    integer={p.type === 'int'}
                    value={typeof values[p.name] === 'number' ? (values[p.name] as number) : null}
                    onChange={(v) => setValue(p.name, v)}
                    error={errText}
                    hint={p.min !== undefined || p.max !== undefined ? `${p.min ?? '−∞'} … ${p.max ?? '∞'}` : undefined}
                  />
                );
              })}
              {parentId && (
                <SelectField<'replace' | 'append'>
                  label={t('geo.tpl.mode')}
                  value={mode}
                  options={(['replace', 'append'] as const).map((m) => ({ value: m, label: t(`geo.tpl.mode.${m}`) }))}
                  onChange={setMode}
                />
              )}
              {checked && checked.key === key && (
                <div className="callout ok slim">
                  <div className="grow">
                    <div>{t('geo.tpl.valid', { label: checked.meta.spec_label, n: checked.ops })}</div>
                    {checked.meta.notes.length > 0 && (
                      <div className="faint small">
                        {t('geo.tpl.notes')}: {checked.meta.notes.join('; ')}
                      </div>
                    )}
                    <div className="faint small mono">
                      {t('geo.tpl.ports')}: {checked.meta.ports.map((x) => x.name).join(', ')}
                    </div>
                  </div>
                </div>
              )}
              {dirty && <div className="callout warn slim">{t('geo.tpl.dirty')}</div>}
              {error && !Object.keys(serverProblems).length ? <ErrorNotice error={error} /> : null}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
