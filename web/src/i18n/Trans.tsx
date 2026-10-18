import { Fragment, type ReactNode } from 'react';
import type { MessageKey } from './en';
import { useI18n } from './I18nProvider';

/** Translate a message whose {placeholders} are React nodes (links, <code>, emphasis). */
export function Trans({ k, values }: { k: MessageKey; values: Record<string, ReactNode> }) {
  const { t } = useI18n();
  const parts = t(k).split(/(\{\w+\})/g);
  return (
    <>
      {parts.map((p, i) => {
        const m = /^\{(\w+)\}$/.exec(p);
        return <Fragment key={i}>{m && m[1] in values ? values[m[1]] : p}</Fragment>;
      })}
    </>
  );
}
