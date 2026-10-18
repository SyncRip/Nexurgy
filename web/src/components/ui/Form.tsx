// Form building blocks: labelled field, search box with the `/` shortcut, tag input.

import { Search, X } from 'lucide-react';
import { cloneElement, isValidElement, useId, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { useSlashFocus } from '../../hooks/keyboard';
import { useT } from '../../i18n/I18nProvider';

/**
 * Label + control + hint / error. The single child control receives id,
 * aria-describedby and aria-invalid automatically.
 */
export function Field({ label, hint, error, optional, children, className = '' }: { label: ReactNode; hint?: ReactNode; error?: ReactNode; optional?: boolean; children: ReactElement; className?: string }) {
  const t = useT();
  const id = useId();
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const describedBy = [hint ? hintId : null, error ? errId : null].filter(Boolean).join(' ') || undefined;
  const control = isValidElement(children)
    ? cloneElement(children as ReactElement<Record<string, unknown>>, { id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })
    : children;
  return (
    <div className={`field ${error ? 'has-error' : ''} ${className}`}>
      <label htmlFor={id}>
        {label}
        {optional && <span className="opt"> · {t('common.optional')}</span>}
      </label>
      {control}
      {error ? (
        <span className="field-error" id={errId}>
          {error}
        </span>
      ) : hint ? (
        <span className="field-hint" id={hintId}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

export function SearchInput({ value, onChange, placeholder, label }: { value: string; onChange: (v: string) => void; placeholder: string; label?: string }) {
  const t = useT();
  const ref = useRef<HTMLInputElement>(null);
  useSlashFocus(ref);
  return (
    <div className="search">
      <Search aria-hidden />
      <input
        ref={ref}
        className="input"
        type="search"
        value={value}
        placeholder={placeholder}
        aria-label={label ?? placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && value) {
            e.preventDefault();
            onChange('');
          }
        }}
      />
      {value ? (
        <button type="button" className="btn ghost icon sm" onClick={() => onChange('')} aria-label={t('common.clearFilters')}>
          <X />
        </button>
      ) : (
        <kbd title={t('common.searchShortcut')}>/</kbd>
      )}
    </div>
  );
}

export function TagInput({ value, onChange, placeholder, max = 30, id, ...aria }: { value: string[]; onChange: (v: string[]) => void; placeholder?: string; max?: number; id?: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }) {
  const t = useT();
  const [draft, setDraft] = useState('');
  const commit = (raw: string) => {
    const parts = raw
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!parts.length) return;
    const next = [...value];
    for (const p of parts) if (!next.includes(p) && next.length < max) next.push(p.slice(0, 40));
    onChange(next);
    setDraft('');
  };
  return (
    <div className="tag-input" onClick={(e) => (e.currentTarget.querySelector('input') as HTMLInputElement | null)?.focus()}>
      {value.map((tg) => (
        <span key={tg} className="tag removable">
          {tg}
          <button type="button" onClick={() => onChange(value.filter((x) => x !== tg))} aria-label={t('parts.tagRemove', { tag: tg })}>
            <X />
          </button>
        </span>
      ))}
      <input
        id={id}
        {...aria}
        value={draft}
        placeholder={value.length ? '' : placeholder}
        onChange={(e) => {
          const v = e.target.value;
          if (/[,，]/.test(v)) commit(v);
          else setDraft(v);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            if (draft.trim()) {
              e.preventDefault();
              commit(draft);
            }
          } else if (e.key === 'Backspace' && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={() => draft.trim() && commit(draft)}
      />
    </div>
  );
}
