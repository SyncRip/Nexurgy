// Numeric input on Base UI's NumberField: locale-aware parsing / formatting,
// ArrowUp / ArrowDown step (Shift ×10, Alt ×0.1), Home / End jump to min / max,
// and clamping on blur. The stepper buttons are mouse affordances
// (tabIndex -1, keyboard users use the arrow keys); they get localized names.
// Works inside <Field>: the injected id / aria-describedby / aria-invalid land
// on the <input>. `unit` is a visual suffix (mm, pcs…) outside the value.
// Skin: NumberField.css (`pr-number`).

import { NumberField as BaseNumberField } from '@base-ui/react/number-field';
import { Minus, Plus } from 'lucide-react';
import type { ReactNode } from 'react';
import { useI18n } from '../../i18n/I18nProvider';
import './NumberField.css';

export interface NumberFieldProps {
  /** `null` = empty. */
  value: number | null;
  onChange: (value: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  /** Intl number format, e.g. `{ maximumFractionDigits: 2 }`. */
  format?: Intl.NumberFormatOptions;
  unit?: ReactNode;
  size?: 'sm' | 'md';
  /** Hide the − / + buttons (dense tables). */
  hideSteppers?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  required?: boolean;
  name?: string;
  id?: string;
  placeholder?: string;
  className?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
}

export function NumberField({ value, onChange, min, max, step = 1, format, unit, size = 'md', hideSteppers = false, disabled, readOnly, required, name, id, placeholder, className, ...aria }: NumberFieldProps) {
  const { t, locale } = useI18n();
  return (
    <BaseNumberField.Root
      className={['pr-number', `pr-number--${size}`, className].filter(Boolean).join(' ')}
      value={value}
      onValueChange={(next) => onChange(next)}
      min={min}
      max={max}
      step={step}
      format={format}
      locale={locale}
      disabled={disabled}
      readOnly={readOnly}
      required={required}
      name={name}
      id={id}
    >
      <BaseNumberField.Group className="pr-number__group">
        {!hideSteppers && (
          <BaseNumberField.Decrement className="pr-number__step" aria-label={t('numberField.decrement')}>
            <Minus />
          </BaseNumberField.Decrement>
        )}
        <BaseNumberField.Input className="pr-number__input" placeholder={placeholder} {...aria} />
        {unit && <span className="pr-number__unit">{unit}</span>}
        {!hideSteppers && (
          <BaseNumberField.Increment className="pr-number__step" aria-label={t('numberField.increment')}>
            <Plus />
          </BaseNumberField.Increment>
        )}
      </BaseNumberField.Group>
    </BaseNumberField.Root>
  );
}
