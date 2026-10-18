// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { Dialog } from './Dialog';
import { Field } from './Form';
import { Select, type SelectOption } from './Select';

type Material = 'al6061' | 'ss304' | 'ti64' | 'pom';

const options: SelectOption<Material>[] = [
  { value: 'al6061', label: 'Aluminium 6061' },
  { value: 'ss304', label: 'Stainless 304' },
  { value: 'ti64', label: 'Titanium Ti-6Al-4V', disabled: true },
  { value: 'pom', label: 'POM' },
];

function Controlled({ onChange = vi.fn(), initial = null as Material | null }) {
  const [value, setValue] = useState<Material | null>(initial);
  return (
    <I18nProvider>
      <Field label="Material" hint="Stock material">
        <Select
          options={options}
          value={value}
          onChange={(v) => {
            onChange(v);
            setValue(v);
          }}
          name="material"
        />
      </Field>
    </I18nProvider>
  );
}

describe('Select', () => {
  it('is named by the Field label, described by its hint and shows the placeholder', () => {
    render(<Controlled />);
    const trigger = screen.getByRole('combobox', { name: 'Material' });
    const hint = screen.getByText('Stock material');
    expect(trigger.getAttribute('aria-describedby')).toBe(hint.id);
    expect(trigger.hasAttribute('data-placeholder')).toBe(true);
    expect(trigger.textContent).toContain('Select…');
  });

  it('opens a listbox on click, selects an option and returns focus to the trigger', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Material' });
    await user.click(trigger);
    expect(await screen.findByRole('listbox')).toBeTruthy();
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Aluminium 6061', 'Stainless 304', 'Titanium Ti-6Al-4V', 'POM']);
    await user.click(screen.getByRole('option', { name: 'Stainless 304' }));
    expect(onChange).toHaveBeenCalledWith('ss304');
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
    expect(trigger.textContent).toContain('Stainless 304');
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('marks the current value as selected', async () => {
    const user = userEvent.setup();
    render(<Controlled initial="pom" />);
    await user.click(screen.getByRole('combobox', { name: 'Material' }));
    await screen.findByRole('listbox');
    expect(screen.getByRole('option', { name: 'POM' }).getAttribute('aria-selected')).toBe('true');
  });

  it('works from the keyboard and never selects a disabled option', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Material' });
    trigger.focus();
    await user.keyboard('{ArrowDown}');
    await screen.findByRole('listbox');
    const disabled = screen.getByRole('option', { name: 'Titanium Ti-6Al-4V' });
    expect(disabled.getAttribute('aria-disabled')).toBe('true');
    await user.click(disabled);
    expect(onChange).not.toHaveBeenCalled();
    await user.keyboard('{End}');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('option', { name: 'POM' })));
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('pom');
  });

  it('closes on Escape without changing the value', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Material' });
    await user.click(trigger);
    await screen.findByRole('listbox');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
    expect(onChange).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('submits through a hidden input named `name`', () => {
    const { container } = render(<Controlled initial="al6061" />);
    expect(container.querySelector<HTMLInputElement>('input[name="material"]')?.value).toBe('al6061');
  });

  it('inside a dialog, Escape closes only the list', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <I18nProvider>
        <Dialog title="Edit" onClose={onClose}>
          <Select aria-label="Material" options={options} value={null} onChange={vi.fn()} />
        </Dialog>
      </I18nProvider>,
    );
    await user.click(await screen.findByRole('combobox', { name: 'Material' }));
    await screen.findByRole('listbox');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
