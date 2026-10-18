// Tabs and SegmentedControl on Base UI.
// - Tabs: a tablist with roving tabindex, arrow / Home / End keys and
//   aria-selected. Selection follows activation (Enter / Space / click), not
//   focus, so URL-synced callers don't navigate on every arrow press. Items may
//   carry their own `panel`; callers that render content themselves (e.g. a
//   routed outlet) just omit it.
// - SegmentedControl: a single-select toggle group (aria-pressed buttons) for
//   view switches that don't own a panel. Pressing the active segment again
//   keeps it selected.
// Skin: Tabs.css (`pr-tabs`, `pr-seg`).

import { Tabs as BaseTabs } from '@base-ui/react/tabs';
import { Toggle } from '@base-ui/react/toggle';
import { ToggleGroup } from '@base-ui/react/toggle-group';
import type { ReactNode } from 'react';
import './Tabs.css';

export interface TabItem<V extends string = string> {
  value: V;
  label: ReactNode;
  icon?: ReactNode;
  disabled?: boolean;
  /** Trailing count or badge. */
  meta?: ReactNode;
  /** Panel content; omit when the caller renders the content itself. */
  panel?: ReactNode;
}

export interface TabsProps<V extends string = string> {
  items: readonly TabItem<V>[];
  value: V;
  onChange: (value: V) => void;
  /** line: underlined page/section tabs; pill: compact boxed tabs. */
  variant?: 'line' | 'pill';
  /** Accessible name of the tablist. */
  'aria-label'?: string;
  className?: string;
}

export function Tabs<V extends string = string>({ items, value, onChange, variant = 'line', 'aria-label': ariaLabel, className }: TabsProps<V>) {
  const hasPanels = items.some((it) => it.panel !== undefined);
  return (
    <BaseTabs.Root className={['pr-tabs', `pr-tabs--${variant}`, className].filter(Boolean).join(' ')} value={value} onValueChange={(next) => onChange(next as V)}>
      <BaseTabs.List className="pr-tabs__list" aria-label={ariaLabel}>
        {items.map((it) => (
          <BaseTabs.Tab key={it.value} className="pr-tabs__tab" value={it.value} disabled={it.disabled}>
            {it.icon}
            <span>{it.label}</span>
            {it.meta !== undefined && <span className="pr-tabs__meta">{it.meta}</span>}
          </BaseTabs.Tab>
        ))}
        <BaseTabs.Indicator className="pr-tabs__indicator" />
      </BaseTabs.List>
      {hasPanels &&
        items.map(
          (it) =>
            it.panel !== undefined && (
              <BaseTabs.Panel key={it.value} className="pr-tabs__panel" value={it.value}>
                {it.panel}
              </BaseTabs.Panel>
            ),
        )}
    </BaseTabs.Root>
  );
}

export interface SegmentItem<V extends string = string> {
  value: V;
  label: ReactNode;
  icon?: ReactNode;
  disabled?: boolean;
  /** Required when the segment shows only an icon. */
  'aria-label'?: string;
}

export interface SegmentedControlProps<V extends string = string> {
  items: readonly SegmentItem<V>[];
  value: V;
  onChange: (value: V) => void;
  /** Accessible name of the group. */
  'aria-label': string;
  size?: 'sm' | 'md';
  className?: string;
}

export function SegmentedControl<V extends string = string>({ items, value, onChange, 'aria-label': ariaLabel, size = 'sm', className }: SegmentedControlProps<V>) {
  return (
    <ToggleGroup
      className={['pr-seg', `pr-seg--${size}`, className].filter(Boolean).join(' ')}
      aria-label={ariaLabel}
      value={[value]}
      onValueChange={(next) => {
        // Single select: un-pressing the active segment yields []; ignore it.
        const picked = next[0] as V | undefined;
        if (picked !== undefined && picked !== value) onChange(picked);
      }}
    >
      {items.map((it) => (
        <Toggle key={it.value} className="pr-seg__item" value={it.value} disabled={it.disabled} aria-label={it['aria-label']}>
          {it.icon}
          {it.label !== null && it.label !== undefined && <span>{it.label}</span>}
        </Toggle>
      ))}
    </ToggleGroup>
  );
}
