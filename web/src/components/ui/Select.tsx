// Single-value select on Base UI: a button trigger with a listbox popup
// (arrow keys, typeahead, Home / End, Esc returns focus). Works inside <Field>:
// the id / aria-describedby / aria-invalid that Field injects land on the
// trigger, and a <label for> pointing at the trigger names it. Without a Field,
// pass `aria-label`. `name` renders a hidden input so native forms still submit.
// The popup opens below the trigger like Menu does (not macOS-style over it).
// Skin: Select.css (`pr-select`).

import { Select as BaseSelect } from '@base-ui/react/select';
import { Check, ChevronDown } from 'lucide-react';
import type { ReactNode } from 'react';
import { useT } from '../../i18n/I18nProvider';
import './Select.css';

export interface SelectOption<V extends string = string> {
  value: V;
  label: ReactNode;
  disabled?: boolean;
}

export interface SelectProps<V extends string = string> {
  options: readonly SelectOption<V>[];
  /** `null` shows the placeholder. */
  value: V | null;
  onChange: (value: V) => void;
  placeholder?: ReactNode;
  size?: 'sm' | 'md';
  disabled?: boolean;
  name?: string;
  id?: string;
  className?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
}

export function Select<V extends string = string>({ options, value, onChange, placeholder, size = 'md', disabled, name, id, className, ...aria }: SelectProps<V>) {
  const t = useT();
  return (
    <BaseSelect.Root
      items={options}
      value={value}
      onValueChange={(next) => {
        if (next !== null) onChange(next as V);
      }}
      disabled={disabled}
      name={name}
      id={id}
    >
      <BaseSelect.Trigger className={['pr-select', `pr-select--${size}`, className].filter(Boolean).join(' ')} {...aria}>
        <BaseSelect.Value className="pr-select__value" placeholder={placeholder ?? t('select.placeholder')} />
        <BaseSelect.Icon className="pr-select__icon">
          <ChevronDown />
        </BaseSelect.Icon>
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner className="pr-select-positioner" alignItemWithTrigger={false} sideOffset={4} collisionPadding={8}>
          <BaseSelect.Popup className="pr-select__popup nokey">
            <BaseSelect.List className="pr-select__list">
              {options.map((opt) => (
                <BaseSelect.Item key={opt.value} className="pr-select__item" value={opt.value} label={typeof opt.label === 'string' ? opt.label : undefined} disabled={opt.disabled}>
                  <BaseSelect.ItemText className="pr-select__item-text">{opt.label}</BaseSelect.ItemText>
                  <BaseSelect.ItemIndicator className="pr-select__check">
                    <Check />
                  </BaseSelect.ItemIndicator>
                </BaseSelect.Item>
              ))}
            </BaseSelect.List>
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}
