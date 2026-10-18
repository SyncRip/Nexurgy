// Accessible tab strip: role=tablist, roving tabindex, arrow / Home / End keys.
// The panel is rendered by the caller with `tabPanelProps`.

import { useRef, type KeyboardEvent, type ReactNode } from 'react';

export interface TabDef<T extends string> {
  id: T;
  label: ReactNode;
  icon?: ReactNode;
  /** Server-reported count shown after the label (undefined = not loaded yet). */
  count?: number;
}

export function TabList<T extends string>({
  tabs,
  value,
  onChange,
  label,
  idPrefix,
  className = '',
}: {
  tabs: TabDef<T>[];
  value: T;
  onChange: (id: T) => void;
  /** Accessible name of the strip. */
  label: string;
  idPrefix: string;
  className?: string;
}) {
  const refs = useRef<Partial<Record<T, HTMLButtonElement | null>>>({});
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    let j = -1;
    if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = tabs.length - 1;
    if (j < 0) return;
    e.preventDefault();
    const next = tabs[j].id;
    onChange(next);
    refs.current[next]?.focus();
  };
  return (
    <div className={`tabs as-tabs ${className}`} role="tablist" aria-label={label}>
      {tabs.map((tab, i) => (
        <button
          key={tab.id}
          ref={(el) => {
            refs.current[tab.id] = el;
          }}
          type="button"
          role="tab"
          id={`${idPrefix}-tab-${tab.id}`}
          aria-selected={value === tab.id}
          aria-controls={`${idPrefix}-panel-${tab.id}`}
          tabIndex={value === tab.id ? 0 : -1}
          className={`tab ${value === tab.id ? 'active' : ''}`}
          onClick={() => onChange(tab.id)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {tab.icon}
          <span>{tab.label}</span>
          {tab.count !== undefined && <span className="as-tab-count mono">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function tabPanelProps(idPrefix: string, id: string) {
  return { role: 'tabpanel' as const, id: `${idPrefix}-panel-${id}`, 'aria-labelledby': `${idPrefix}-tab-${id}` };
}
