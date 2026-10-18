// Stage list chosen when a project is created (design §4.1): the default
// template (requirements → design review → process assessment → quote
// delivery, named in the current language by the server), a custom ordered
// list, or none yet (a project needs at least one stage before it can start).

import '../tasks/tasks.css';
import './stages.css';
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { useI18n } from '../../i18n/I18nProvider';

export type StageMode = 'default' | 'custom' | 'none';
export interface StageListValue {
  mode: StageMode;
  names: string[];
}

export const DEFAULT_STAGE_VALUE: StageListValue = { mode: 'default', names: [''] };

/** Body fields for POST /projects. */
export function stageBody(v: StageListValue): { milestones?: string[]; stage_template?: 'default' } {
  if (v.mode === 'default') return { stage_template: 'default' };
  if (v.mode === 'custom') return { milestones: v.names.map((n) => n.trim()).filter(Boolean) };
  return {};
}

/** Validation message for the custom list, or null. */
export function stageListError(v: StageListValue, t: ReturnType<typeof useI18n>['t']): string | null {
  if (v.mode !== 'custom') return null;
  const names = v.names.map((n) => n.trim());
  if (!names.some(Boolean)) return t('stg.create.customEmpty');
  if (names.some((n) => n.length > 200)) return t('validation.maxLength', { field: t('tk.ms.name'), max: 200 });
  const filled = names.filter(Boolean);
  if (new Set(filled.map((n) => n.toLowerCase())).size !== filled.length) return t('stg.create.duplicate');
  return null;
}

export function StageListField({ value, onChange, error }: { value: StageListValue; onChange: (v: StageListValue) => void; error?: string | null }) {
  const { t } = useI18n();
  const [focus, setFocus] = useState<number | null>(null);
  const setNames = (names: string[]) => onChange({ ...value, names });
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= value.names.length) return;
    const next = [...value.names];
    [next[i], next[j]] = [next[j], next[i]];
    setNames(next);
    setFocus(j);
  };
  return (
    <fieldset className="tk-fieldset stack-sm span-2">
      <legend className="tk-label">{t('stg.create.title')}</legend>
      <p className="faint small">{t('stg.create.hint')}</p>
      <div className="stg-mode" role="radiogroup" aria-label={t('stg.create.title')}>
        <label className="check">
          <input type="radio" name="stg-mode" checked={value.mode === 'default'} onChange={() => onChange({ ...value, mode: 'default' })} />
          {t('stg.create.default')}
        </label>
        {value.mode === 'default' && (
          <ol className="stg-default-list">
            <li>{t('stg.default.1')}</li>
            <li>{t('stg.default.2')}</li>
            <li>{t('stg.default.3')}</li>
            <li>{t('stg.default.4')}</li>
          </ol>
        )}
        <label className="check">
          <input type="radio" name="stg-mode" checked={value.mode === 'custom'} onChange={() => onChange({ ...value, mode: 'custom', names: value.names.length ? value.names : [''] })} />
          {t('stg.create.custom')}
        </label>
        {value.mode === 'custom' && (
          <div className="stack-sm">
            <ol className="stg-rows">
              {value.names.map((n, i) => (
                <li key={i}>
                  <span className="mono faint">{i + 1}.</span>
                  <input
                    className="input"
                    value={n}
                    maxLength={200}
                    placeholder={t('stg.create.namePlaceholder')}
                    aria-label={t('stg.create.nameN', { n: i + 1 })}
                    aria-invalid={error && !n.trim() ? true : undefined}
                    ref={(el) => {
                      if (el && focus === i) {
                        el.focus();
                        setFocus(null);
                      }
                    }}
                    onChange={(e) => setNames(value.names.map((x, k) => (k === i ? e.target.value : x)))}
                  />
                  <button type="button" className="btn ghost icon sm" onClick={() => move(i, -1)} disabled={i === 0} aria-label={t('stg.create.up', { n: i + 1 })}>
                    <ArrowUp />
                  </button>
                  <button type="button" className="btn ghost icon sm" onClick={() => move(i, 1)} disabled={i === value.names.length - 1} aria-label={t('stg.create.down', { n: i + 1 })}>
                    <ArrowDown />
                  </button>
                  <button
                    type="button"
                    className="btn ghost icon sm"
                    onClick={() => setNames(value.names.filter((_, k) => k !== i))}
                    disabled={value.names.length === 1}
                    aria-label={t('stg.create.remove', { n: i + 1 })}
                  >
                    <Trash2 />
                  </button>
                </li>
              ))}
            </ol>
            <div>
              <button
                type="button"
                className="btn sm"
                onClick={() => {
                  setNames([...value.names, '']);
                  setFocus(value.names.length);
                }}
                disabled={value.names.length >= 20}
              >
                <Plus /> {t('stg.create.add')}
              </button>
            </div>
          </div>
        )}
        <label className="check">
          <input type="radio" name="stg-mode" checked={value.mode === 'none'} onChange={() => onChange({ ...value, mode: 'none' })} />
          {t('stg.create.none')}
        </label>
      </div>
      {error && <span className="field-error">{error}</span>}
    </fieldset>
  );
}
