// Compact numeric inputs for the parameter panel. Values commit on every valid
// keystroke (live preview is debounced elsewhere); ArrowUp / ArrowDown step
// (Shift ×10, Alt ×0.1). Text that is not a number yet ("-", "") is kept
// locally and not committed.

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '';
  return String(Math.round(v * 1e6) / 1e6);
}

function parseNum(s: string): number | null {
  const t = s.trim().replace(',', '.');
  if (!t || t === '-' || t === '.' || t === '-.') return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

export interface NumInputProps {
  value: number | null | undefined;
  onChange: (v: number) => void;
  /** Called when the field is cleared (only for optional values). */
  onClear?: () => void;
  step?: number;
  integer?: boolean;
  disabled?: boolean;
  invalid?: boolean;
  placeholder?: string;
  id?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
  className?: string;
}

export function NumInput({ value, onChange, onClear, step = 1, integer, disabled, invalid, placeholder, className = '', ...aria }: NumInputProps) {
  const [text, setText] = useState(fmtNum(value));
  const last = useRef(value);
  useEffect(() => {
    if (value !== last.current) {
      last.current = value;
      if (parseNum(text) !== value) setText(fmtNum(value));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const commit = (n: number) => {
    const v = integer ? Math.round(n) : n;
    last.current = v;
    onChange(v);
  };

  return (
    <input
      className={`input sm ws-num ${className}`}
      type="text"
      inputMode={integer ? 'numeric' : 'decimal'}
      autoComplete="off"
      spellCheck={false}
      value={text}
      disabled={disabled}
      placeholder={placeholder}
      aria-invalid={invalid || undefined}
      {...aria}
      onChange={(e) => {
        const s = e.target.value;
        setText(s);
        const n = parseNum(s);
        if (n !== null) commit(n);
        else if (!s.trim() && onClear) {
          last.current = null;
          onClear();
        }
      }}
      onBlur={() => setText(fmtNum(last.current))}
      onKeyDown={(e) => {
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
        e.preventDefault();
        const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
        const cur = parseNum(text) ?? value ?? 0;
        const next = Math.round((cur + (e.key === 'ArrowUp' ? 1 : -1) * step * mult) * 1e6) / 1e6;
        setText(fmtNum(next));
        commit(next);
      }}
    />
  );
}

/** Label + one numeric input + unit + error, in one compact row. */
export function NumField({
  label,
  unit,
  error,
  hint,
  ...input
}: NumInputProps & { label: ReactNode; unit?: string; error?: ReactNode; hint?: ReactNode }) {
  const id = useId();
  return (
    <div className={`ws-f ${error ? 'has-error' : ''}`}>
      <label htmlFor={id}>{label}</label>
      <div className="ws-f-ctl">
        <NumInput id={id} invalid={!!error} aria-describedby={error ? `${id}-e` : undefined} {...input} />
        {unit && <span className="ws-unit">{unit}</span>}
      </div>
      {error ? (
        <span className="ws-f-err" id={`${id}-e`}>
          {error}
        </span>
      ) : hint ? (
        <span className="ws-f-hint">{hint}</span>
      ) : null}
    </div>
  );
}

/** Label + N numeric inputs (vectors). */
export function VecField({
  label,
  value,
  onChange,
  axes = ['X', 'Y', 'Z'],
  unit,
  step = 1,
  error,
  hint,
  disabled,
}: {
  label: ReactNode;
  value: readonly number[];
  onChange: (v: number[]) => void;
  axes?: string[];
  unit?: string;
  step?: number;
  error?: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className={`ws-f ${error ? 'has-error' : ''}`} role="group" aria-labelledby={id}>
      <span className="ws-f-label" id={id}>
        {label}
        {unit && <span className="ws-unit"> {unit}</span>}
      </span>
      <div className={`ws-vec n${axes.length}`}>
        {axes.map((ax, i) => (
          <label key={ax} className="ws-vec-i">
            <span className={`ws-ax ax-${ax.toLowerCase()}`}>{ax}</span>
            <NumInput
              value={value[i]}
              step={step}
              disabled={disabled}
              invalid={!!error}
              aria-label={`${typeof label === 'string' ? label : ''} ${ax}`}
              onChange={(n) => {
                const next = [...value];
                next[i] = n;
                onChange(next);
              }}
            />
          </label>
        ))}
      </div>
      {error ? <span className="ws-f-err">{error}</span> : hint ? <span className="ws-f-hint">{hint}</span> : null}
    </div>
  );
}

export function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
  error,
  hint,
}: {
  label: ReactNode;
  value: T;
  options: { value: T; label: string; disabled?: boolean }[];
  onChange: (v: T) => void;
  disabled?: boolean;
  error?: ReactNode;
  hint?: ReactNode;
}) {
  const id = useId();
  return (
    <div className={`ws-f ${error ? 'has-error' : ''}`}>
      <label htmlFor={id}>{label}</label>
      <select id={id} className="select sm" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value as T)}>
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
      {error ? <span className="ws-f-err">{error}</span> : hint ? <span className="ws-f-hint">{hint}</span> : null}
    </div>
  );
}

export function CheckField({ label, checked, onChange, disabled }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="check ws-check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}
