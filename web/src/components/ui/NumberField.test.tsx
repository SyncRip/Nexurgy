// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { Field } from './Form';
import { NumberField, type NumberFieldProps } from './NumberField';

function Controlled({ onChange = vi.fn(), initial = 10, ...rest }: Partial<NumberFieldProps> & { initial?: number | null }) {
  const [value, setValue] = useState<number | null>(initial);
  return (
    <I18nProvider>
      <Field label="Quantity" error={rest['aria-invalid'] ? 'Too many' : undefined}>
        <NumberField
          value={value}
          onChange={(v) => {
            onChange(v);
            setValue(v);
          }}
          {...rest}
        />
      </Field>
    </I18nProvider>
  );
}

const input = () => screen.getByRole('textbox', { name: 'Quantity' }) as HTMLInputElement;

describe('NumberField', () => {
  it('is named by the Field label and shows the value', () => {
    render(<Controlled unit="pcs" />);
    expect(input().value).toBe('10');
    expect(screen.getByText('pcs')).toBeTruthy();
  });

  it('steps with ArrowUp / ArrowDown and Shift for ×10', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    input().focus();
    await user.keyboard('{ArrowUp}');
    expect(onChange).toHaveBeenLastCalledWith(11);
    await user.keyboard('{ArrowDown}{ArrowDown}');
    expect(onChange).toHaveBeenLastCalledWith(9);
    await user.keyboard('{Shift>}{ArrowUp}{/Shift}');
    expect(onChange).toHaveBeenLastCalledWith(19);
  });

  it('clamps to min / max and jumps there with Home / End', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} min={1} max={12} />);
    input().focus();
    await user.keyboard('{End}');
    expect(onChange).toHaveBeenLastCalledWith(12);
    await user.keyboard('{ArrowUp}');
    expect(input().value).toBe('12');
    await user.keyboard('{Home}');
    expect(onChange).toHaveBeenLastCalledWith(1);
  });

  it('stepper buttons have localized names and stay out of the Tab order', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const inc = screen.getByRole('button', { name: 'Increase' });
    const dec = screen.getByRole('button', { name: 'Decrease' });
    expect(inc.tabIndex).toBe(-1);
    expect(dec.tabIndex).toBe(-1);
    // Base UI steps on pointerdown (press-and-hold repeats), not on click.
    fireEvent.pointerDown(inc, { pointerType: 'mouse', button: 0 });
    fireEvent.pointerUp(inc, { pointerType: 'mouse', button: 0 });
    expect(onChange).toHaveBeenLastCalledWith(11);
    // Tab from the page start lands on the input, skipping the − button.
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(document.activeElement).toBe(input());
    await user.tab();
    expect(document.activeElement).toBe(document.body);
  });

  it('accepts typed values and reports null when cleared', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    await user.clear(input());
    expect(onChange).toHaveBeenLastCalledWith(null);
    await user.type(input(), '42');
    await user.tab();
    expect(onChange).toHaveBeenLastCalledWith(42);
    expect(input().value).toBe('42');
  });

  it('forwards aria-invalid / aria-describedby from Field to the input', () => {
    render(<Controlled aria-invalid />);
    expect(input().getAttribute('aria-invalid')).toBe('true');
    expect(input().getAttribute('aria-describedby')).toBe(screen.getByText('Too many').id);
  });

  it('hides the steppers on request and respects disabled', () => {
    render(<Controlled hideSteppers disabled />);
    expect(screen.queryByRole('button')).toBeNull();
    expect(input().disabled).toBe(true);
  });
});
