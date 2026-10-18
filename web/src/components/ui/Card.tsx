// Card and Panel surfaces.
//
// Card is presentational: `interactive` adds the hover shadow and pointer,
// `selected` the accent outline, `status` a coloured edge. It never lifts on
// hover. Semantics stay with the caller: render a link or button through
// `render` (Base UI useRender) and say "selected" with the ARIA state that
// fits the container (aria-current, aria-selected, aria-pressed). The status
// edge only supplements a StatusChip or text inside the card.
//
// Panel is a titled section with header actions; `collapsible` puts the title
// inside a disclosure button (heading > button, as in the APG accordion) and
// keeps the body mounted while closed, so collapsing never drops form state.
// Skin: Card.css (`pr-card`, `pr-panel`).

import { Collapsible } from '@base-ui/react/collapsible';
import { useRender } from '@base-ui/react/use-render';
import { ChevronDown } from 'lucide-react';
import { useId, type ReactNode } from 'react';
import type { StatusKind } from './StatusChip';
import './Card.css';

type CardState = { interactive: boolean; selected: boolean; status: StatusKind | undefined };

export interface CardProps extends useRender.ComponentProps<'div', CardState> {
  interactive?: boolean;
  selected?: boolean;
  status?: StatusKind;
  padding?: 'none' | 'sm' | 'md';
}

export function Card({ interactive = false, selected = false, status, padding = 'md', render, ref, className, ...rest }: CardProps) {
  // State keys become data-interactive / data-selected / data-status.
  return useRender({
    render,
    ref,
    defaultTagName: 'div',
    state: { interactive, selected, status },
    props: { ...rest, className: ['pr-card', `pr-card--pad-${padding}`, className].filter(Boolean).join(' ') },
  });
}

interface PanelBaseProps {
  title: ReactNode;
  subtitle?: ReactNode;
  /** Controls at the right of the header; never inside the collapse button. */
  actions?: ReactNode;
  footer?: ReactNode;
  children?: ReactNode;
  /** Heading level of the title, to fit the page outline. */
  level?: 2 | 3 | 4;
  /** Body without padding (tables, lists that draw their own rows). */
  flush?: boolean;
  className?: string;
}

interface StaticPanelProps extends PanelBaseProps {
  collapsible?: false;
}

interface CollapsiblePanelProps extends PanelBaseProps {
  collapsible: true;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export type PanelProps = StaticPanelProps | CollapsiblePanelProps;

export function Panel(props: PanelProps) {
  const { title, subtitle, actions, footer, children, level = 2, flush = false, className } = props;
  const headingId = useId();
  const Heading = `h${level}` as const;
  const classes = ['pr-panel', className].filter(Boolean).join(' ');
  const body = <div className={flush ? 'pr-panel__body pr-panel__body--flush' : 'pr-panel__body'}>{children}</div>;
  const header = (titleNode: ReactNode) => (
    <div className="pr-panel__head">
      <div className="pr-panel__titles">
        <Heading id={headingId} className="pr-panel__title">
          {titleNode}
        </Heading>
        {subtitle && <p className="pr-panel__subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="pr-panel__actions">{actions}</div>}
    </div>
  );
  const foot = footer && <div className="pr-panel__foot">{footer}</div>;

  if (!props.collapsible) {
    return (
      <section className={classes} aria-labelledby={headingId}>
        {header(title)}
        {body}
        {foot}
      </section>
    );
  }

  return (
    <Collapsible.Root
      className={classes}
      render={<section aria-labelledby={headingId} />}
      defaultOpen={props.defaultOpen ?? true}
      open={props.open}
      onOpenChange={props.onOpenChange ? (next) => props.onOpenChange?.(next) : undefined}
    >
      {header(
        <Collapsible.Trigger className="pr-panel__toggle">
          <ChevronDown aria-hidden className="pr-panel__chevron" />
          <span>{title}</span>
        </Collapsible.Trigger>,
      )}
      <Collapsible.Panel className="pr-panel__collapse" keepMounted>
        {body}
        {foot}
      </Collapsible.Panel>
    </Collapsible.Root>
  );
}
