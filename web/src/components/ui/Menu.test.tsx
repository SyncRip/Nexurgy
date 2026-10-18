// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { Dialog } from './Dialog';
import { Menu, type MenuItem } from './Menu';

function items(onSelect = vi.fn()): MenuItem[] {
  return [
    { id: 'rename', label: 'Rename', onSelect: () => onSelect('rename') },
    { id: 'copy', label: 'Duplicate', onSelect: () => onSelect('copy'), disabled: true },
    { id: 'archive', label: 'Archive', onSelect: () => onSelect('archive') },
    { id: 'delete', label: 'Delete', onSelect: () => onSelect('delete'), danger: true, separator: true },
  ];
}

function setup(extra: Partial<Parameters<typeof Menu>[0]> = {}) {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  const onRowClick = vi.fn();
  const onRowKey = vi.fn();
  render(
    // A clickable row hosting the menu, as in tables.
    <div role="row" onClick={onRowClick} onKeyDown={onRowKey}>
      <Menu label="Part actions" trigger="⋯" items={items(onSelect)} {...extra} />
    </div>,
  );
  const trigger = screen.getByRole('button', { name: 'Part actions' });
  return { user, onSelect, onRowClick, onRowKey, trigger };
}

describe('Menu', () => {
  it('exposes the trigger as a menu button and opens a labelled menu', async () => {
    const { user, trigger } = setup();
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    await user.click(trigger);
    const menu = await screen.findByRole('menu', { name: 'Part actions' });
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByRole('menuitem').map((el) => el.textContent)).toEqual(['Rename', 'Duplicate', 'Archive', 'Delete']);
    expect(menu.querySelector('[role="separator"].menu-sep')).not.toBeNull();
    expect(screen.getByRole('menuitem', { name: 'Delete' }).className).toContain('danger');
  });

  it('renders the optional header inside the menu', async () => {
    const { user, trigger } = setup({ header: <b>Signed in as Ada</b> });
    await user.click(trigger);
    expect((await screen.findByRole('menu')).querySelector('.menu-header')?.textContent).toBe('Signed in as Ada');
  });

  it('runs onSelect, closes and returns focus to the trigger', async () => {
    const { user, onSelect, trigger } = setup();
    await user.click(trigger);
    await user.click(await screen.findByRole('menuitem', { name: 'Archive' }));
    expect(onSelect).toHaveBeenCalledWith('archive');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('does not select disabled items', async () => {
    const { user, onSelect, trigger } = setup();
    await user.click(trigger);
    const disabled = await screen.findByRole('menuitem', { name: 'Duplicate' });
    expect(disabled.getAttribute('aria-disabled')).toBe('true');
    await user.click(disabled);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole('menu')).toBeTruthy();
  });

  it('opens from the keyboard and moves with the arrow keys; disabled items are reachable but inert', async () => {
    const { user, onSelect, trigger } = setup();
    trigger.focus();
    await user.keyboard('{ArrowDown}');
    await screen.findByRole('menu');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Rename' })));
    // WAI-ARIA APG: disabled menu items stay focusable so their presence is discoverable.
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Duplicate' })));
    await user.keyboard('{Enter}');
    expect(onSelect).not.toHaveBeenCalled();
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Archive' })));
    await user.keyboard('{End}');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Delete' })));
    await user.keyboard('{Home}');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Rename' })));
    await user.keyboard('{Enter}');
    expect(onSelect).toHaveBeenCalledWith('rename');
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    const onWindowKey = vi.fn();
    window.addEventListener('keydown', onWindowKey);
    const { user, trigger } = setup();
    await user.click(trigger);
    await screen.findByRole('menu');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    // Global Esc handlers (e.g. closing the assistant dock) don't see it.
    expect(onWindowKey).not.toHaveBeenCalled();
    window.removeEventListener('keydown', onWindowKey);
  });

  it('keeps clicks and Enter / Space on the trigger and items away from the host row', async () => {
    const { user, onRowClick, onRowKey, trigger } = setup();
    await user.click(trigger);
    await user.click(await screen.findByRole('menuitem', { name: 'Rename' }));
    trigger.focus();
    await user.keyboard('{Enter}');
    await user.keyboard('{Escape}');
    await user.keyboard(' ');
    expect(onRowClick).not.toHaveBeenCalled();
    expect(onRowKey.mock.calls.map(([e]) => e.key)).not.toContain('Enter');
    expect(onRowKey.mock.calls.map(([e]) => e.key)).not.toContain(' ');
  });

  it('is non-modal: the page outside stays interactive and an outside press closes it', async () => {
    const user = userEvent.setup();
    const onOutside = vi.fn();
    render(
      <>
        <Menu label="Part actions" trigger="⋯" items={items()} />
        <button type="button" onClick={onOutside}>
          outside
        </button>
      </>,
    );
    await user.click(screen.getByRole('button', { name: 'Part actions' }));
    await screen.findByRole('menu');
    await user.click(screen.getByRole('button', { name: 'outside' }));
    expect(onOutside).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
  });

  it('inside a dialog, Escape closes only the menu', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    function Page() {
      const [open, setOpen] = useState(true);
      return (
        <I18nProvider>
          {open && (
            <Dialog
              title="Edit"
              onClose={() => {
                onClose();
                setOpen(false);
              }}
            >
              <Menu label="Row actions" trigger="⋯" items={items()} />
            </Dialog>
          )}
        </I18nProvider>
      );
    }
    render(<Page />);
    await user.click(await screen.findByRole('button', { name: 'Row actions' }));
    await screen.findByRole('menu');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
