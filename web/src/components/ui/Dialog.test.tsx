// @vitest-environment jsdom
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ConfirmDialog, Dialog, type DialogProps } from './Dialog';

type Overrides = Partial<Omit<DialogProps, 'onClose'>> & { onClose?: () => void };

/** A page with an opener button; the dialog mounts while open (the callers' contract). */
function Harness(props: Overrides) {
  const [open, setOpen] = useState(false);
  return (
    <I18nProvider>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      {open && (
        <Dialog
          title="Edit part"
          {...props}
          onClose={() => {
            props.onClose?.();
            setOpen(false);
          }}
        >
          {props.children ?? <input aria-label="name" />}
        </Dialog>
      )}
    </I18nProvider>
  );
}

async function openDialog(props: Overrides = {}) {
  const user = userEvent.setup();
  render(<Harness {...props} />);
  await user.click(screen.getByRole('button', { name: 'open' }));
  const dialog = await screen.findByRole(props.role ?? 'dialog');
  return { user, dialog };
}

describe('Dialog', () => {
  it('labels the dialog with its title and marks it modal', async () => {
    const { dialog } = await openDialog({ subtitle: 'Bracket A' });
    expect(dialog).toHaveProperty('ariaModal', 'true');
    expect(dialog.getAttribute('aria-labelledby')).toBe(screen.getByRole('heading', { name: 'Edit part' }).id);
    expect(dialog.getAttribute('aria-describedby')).toBe(screen.getByText('Bracket A').id);
  });

  it('focuses the first body field on open', async () => {
    await openDialog();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'name' })));
  });

  it('prefers [data-autofocus] over the first field', async () => {
    await openDialog({
      footer: (
        <button type="button" data-autofocus>
          save
        </button>
      ),
    });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'save' })));
  });

  it('falls back to the primary footer button when the body has no field', async () => {
    await openDialog({
      children: <p>Nothing to edit</p>,
      footer: (
        <>
          <button type="button" className="btn">
            cancel
          </button>
          <button type="button" className="btn primary">
            apply
          </button>
        </>
      ),
    });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'apply' })));
  });

  it('focuses the sheet itself in side placement', async () => {
    const { dialog } = await openDialog({ placement: 'side' });
    expect(dialog.className).toContain('dlg-side');
    await waitFor(() => expect(document.activeElement).toBe(dialog));
  });

  it('closes on Escape and returns focus to the opener', async () => {
    const onClose = vi.fn();
    const { user } = await openDialog({ onClose });
    await waitFor(() => expect(document.activeElement?.tagName).toBe('INPUT'));
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'open' })));
  });

  it('keeps open on Escape when an inner control consumed it', async () => {
    const onClose = vi.fn();
    const { user } = await openDialog({
      onClose,
      children: <input aria-label="search" onKeyDown={(e) => e.key === 'Escape' && e.preventDefault()} />,
    });
    await waitFor(() => expect(document.activeElement?.tagName).toBe('INPUT'));
    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes from the header close button', async () => {
    const onClose = vi.fn();
    const { user } = await openDialog({ onClose });
    await user.click(screen.getByRole('button', { name: 'Close dialog' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on a backdrop press but not on a press inside', async () => {
    const onClose = vi.fn();
    const { user, dialog } = await openDialog({ onClose });
    await user.click(dialog);
    expect(onClose).not.toHaveBeenCalled();
    await user.click(document.querySelector('.scrim')!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('ignores Escape, backdrop and close button while busy', async () => {
    const onClose = vi.fn();
    const { user } = await openDialog({ onClose, busy: true });
    await user.keyboard('{Escape}');
    await user.click(document.querySelector('.scrim')!);
    expect(screen.getByRole('button', { name: 'Close dialog' })).toHaveProperty('disabled', true);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('forwards key events to onKeyDown', async () => {
    const onKeyDown = vi.fn();
    const { user } = await openDialog({ onKeyDown });
    await waitFor(() => expect(document.activeElement?.tagName).toBe('INPUT'));
    await user.keyboard('{ArrowDown}');
    expect(onKeyDown).toHaveBeenCalledWith(expect.objectContaining({ key: 'ArrowDown' }));
  });

  it('submits the wrapped form unless busy', async () => {
    const onSubmit = vi.fn();
    const { user } = await openDialog({ onSubmit, footer: <button type="submit">go</button> });
    await user.click(screen.getByRole('button', { name: 'go' }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('keeps Tab inside the dialog', async () => {
    const { user, dialog } = await openDialog({ footer: <button type="button">save</button> });
    await waitFor(() => expect(document.activeElement?.tagName).toBe('INPUT'));
    for (let i = 0; i < 4; i += 1) {
      await user.tab();
      // Wrapping passes through a focus guard that refocuses on the next frame.
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    }
  });
});

describe('ConfirmDialog', () => {
  function Confirm({ onConfirm, onClose, busy }: { onConfirm: () => void; onClose: () => void; busy?: boolean }) {
    return (
      <I18nProvider>
        <ConfirmDialog title="Delete part?" body="This cannot be undone." confirmLabel="Delete" danger busy={busy} onConfirm={onConfirm} onClose={onClose} />
      </I18nProvider>
    );
  }

  it('renders an alertdialog that focuses the confirm button', async () => {
    render(<Confirm onConfirm={() => {}} onClose={() => {}} />);
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog.getAttribute('aria-labelledby')).toBe(screen.getByRole('heading', { name: 'Delete part?' }).id);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' })));
  });

  it('confirms and cancels through its buttons', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(<Confirm onConfirm={onConfirm} onClose={onClose} />);
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('cannot be dismissed while busy', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Confirm onConfirm={() => {}} onClose={onClose} busy />);
    await screen.findByRole('alertdialog');
    await act(async () => {
      await user.keyboard('{Escape}');
    });
    expect(onClose).not.toHaveBeenCalled();
  });
});
