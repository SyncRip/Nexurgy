// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { Drawer, type DrawerProps } from './Drawer';
import { Menu } from './Menu';

/** Fakes `(min-width: 1024px)` so the drawer picks side panel vs bottom sheet. */
function mockViewport(wide: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: query.includes('min-width: 1024px') ? wide : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

const originalMatchMedia = window.matchMedia;
afterEach(() => {
  window.matchMedia = originalMatchMedia;
});

function Page({ onClose = vi.fn(), ...rest }: Partial<DrawerProps>) {
  const [open, setOpen] = useState(false);
  return (
    <I18nProvider>
      <button type="button" onClick={() => setOpen(true)}>
        Open details
      </button>
      {open && (
        <Drawer
          title="Bracket A-12"
          subtitle="Rev C"
          onClose={() => {
            onClose();
            setOpen(false);
          }}
          footer={<button type="button">Save</button>}
          {...rest}
        >
          <input aria-label="Name" />
          {rest.children}
        </Drawer>
      )}
    </I18nProvider>
  );
}

async function openDrawer(props: Partial<DrawerProps> = {}) {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<Page onClose={onClose} {...props} />);
  const opener = screen.getByRole('button', { name: 'Open details' });
  await user.click(opener);
  const dialog = await screen.findByRole('dialog', { name: 'Bracket A-12' });
  return { user, onClose, opener, dialog };
}

describe('Drawer', () => {
  it('is a modal dialog named by its title and described by its subtitle', async () => {
    mockViewport(true);
    const { dialog } = await openDrawer();
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-describedby')).toBe(screen.getByText('Rev C').id);
  });

  it('slides from the right at ≥ md, honouring size', async () => {
    mockViewport(true);
    const { dialog } = await openDrawer({ size: 'lg' });
    expect(dialog.getAttribute('data-placement')).toBe('side');
    expect(dialog.className).toContain('pr-drawer--lg');
    expect(dialog.getAttribute('data-swipe-direction')).toBe('right');
  });

  it('becomes a bottom sheet below md', async () => {
    mockViewport(false);
    const { dialog } = await openDrawer();
    expect(dialog.getAttribute('data-placement')).toBe('sheet');
    expect(dialog.getAttribute('data-swipe-direction')).toBe('down');
    expect(dialog.querySelector('.pr-drawer__handle')).not.toBeNull();
  });

  it('focuses the panel on open, traps Tab and returns focus to the opener on close', async () => {
    mockViewport(true);
    const { user, onClose, opener, dialog } = await openDrawer();
    await waitFor(() => expect(document.activeElement).toBe(dialog));
    for (let i = 0; i < 5; i += 1) {
      await user.tab();
      // Wrapping passes through a focus guard that refocuses on the next frame.
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    }
    await user.click(screen.getByRole('button', { name: 'Close panel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it('starts on a [data-autofocus] element when present', async () => {
    mockViewport(true);
    await openDrawer({ children: <button type="button" data-autofocus>Primary</button> });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Primary' })));
  });

  it('closes on Escape', async () => {
    mockViewport(true);
    const { user, onClose } = await openDrawer();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('stays open while busy', async () => {
    mockViewport(true);
    const { user, onClose } = await openDrawer({ busy: true });
    await user.keyboard('{Escape}');
    expect((screen.getByRole('button', { name: 'Close panel' }) as HTMLButtonElement).disabled).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('Escape inside a nested menu closes only the menu', async () => {
    mockViewport(true);
    const { user, onClose } = await openDrawer({
      children: <Menu label="Row actions" trigger="⋯" items={[{ id: 'a', label: 'Archive', onSelect: vi.fn() }]} />,
    });
    await user.click(screen.getByRole('button', { name: 'Row actions' }));
    await screen.findByRole('menu');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
