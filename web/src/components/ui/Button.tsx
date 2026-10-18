// Button primitive on Base UI's Button. While `loading` the button stays
// focusable (aria-disabled instead of native disabled), so a submit in flight
// never drops keyboard focus to <body>; the label is faded rather than removed,
// which keeps both the width and the accessible name. Defaults to
// type="button" so it never submits a surrounding form by accident.
// Skin: Button.css (`pr-btn`).

import { Button as BaseButton } from '@base-ui/react/button';
import type { ComponentPropsWithRef, ReactNode } from 'react';
import './Button.css';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface CommonButtonProps extends Omit<ComponentPropsWithRef<'button'>, 'children' | 'aria-label'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, keeps the width and blocks repeat activation. */
  loading?: boolean;
  /** Leading icon. */
  icon?: ReactNode;
}

interface LabelledButtonProps extends CommonButtonProps {
  iconOnly?: false;
  children: ReactNode;
  'aria-label'?: string;
}

interface IconOnlyButtonProps extends CommonButtonProps {
  /** No visible text, so the accessible name is required. */
  iconOnly: true;
  icon: ReactNode;
  'aria-label': string;
  children?: never;
}

export type ButtonProps = LabelledButtonProps | IconOnlyButtonProps;

export function Button({ variant = 'secondary', size = 'md', loading = false, icon, iconOnly = false, className, disabled, type = 'button', children, ...rest }: ButtonProps) {
  const classes = ['pr-btn', `pr-btn--${variant}`, `pr-btn--${size}`, iconOnly && 'pr-btn--icon-only', className].filter(Boolean).join(' ');
  return (
    <BaseButton
      {...rest}
      type={type}
      className={classes}
      disabled={disabled || loading}
      focusableWhenDisabled={loading}
      aria-busy={loading || undefined}
      data-loading={loading ? '' : undefined}
    >
      <span className="pr-btn__content">
        {icon}
        {!iconOnly && children}
      </span>
      {loading && <span className="pr-btn__spinner" aria-hidden="true" />}
    </BaseButton>
  );
}

export type IconButtonProps = Omit<IconOnlyButtonProps, 'iconOnly' | 'aria-label'> & {
  /** Accessible name; also the natural tooltip text. */
  label: string;
};

/** Square icon-only button (ghost by default). */
export function IconButton({ label, variant = 'ghost', ...rest }: IconButtonProps) {
  return <Button {...rest} variant={variant} iconOnly aria-label={label} />;
}
