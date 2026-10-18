// @vitest-environment jsdom
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ToastProvider, useToast } from './Toast';

type Show = ReturnType<typeof useToast>;

/** Renders the provider and hands the `show` function back to the test. */
function setup() {
  let show!: Show;
  function Grab() {
    show = useToast();
    return null;
  }
  render(
    <I18nProvider>
      <ToastProvider>
        <Grab />
      </ToastProvider>
    </I18nProvider>,
  );
  return (message: ReactNode, opts?: Parameters<Show>[1]) => act(() => show(message, opts));
}

const visibleToasts = () => Array.from(document.querySelectorAll('.toast:not([data-limited])'));

afterEach(() => {
  vi.useRealTimers();
});

describe('Toast', () => {
  it('throws when used outside the provider', () => {
    function Lonely() {
      useToast();
      return null;
    }
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Lonely />)).toThrow('useToast must be used inside <ToastProvider>');
    spy.mockRestore();
  });

  it('shows messages in a labelled region with the tone as a class', async () => {
    const show = setup();
    show('Saved');
    show('Upload failed', { tone: 'err' });
    const region = screen.getByRole('region', { name: 'Notifications' });
    expect(region.className).toContain('toasts');
    expect(region.textContent).toContain('Saved');
    expect(document.querySelector('.toast.ok .toast-msg')?.textContent).toBe('Saved');
    expect(document.querySelector('.toast.err .toast-msg')?.textContent).toBe('Upload failed');
  });

  it('announces errors assertively', () => {
    const show = setup();
    show('Upload failed', { tone: 'err' });
    expect(screen.getAllByRole('alert', { hidden: true }).some((el) => el.textContent?.includes('Upload failed'))).toBe(true);
  });

  it('dismisses after the default duration per tone, or a custom one', () => {
    vi.useFakeTimers();
    const show = setup();
    show('ok');
    show('bad', { tone: 'err' });
    show('quick', { tone: 'info', durationMs: 1000 });
    act(() => vi.advanceTimersByTime(1100));
    expect(visibleToasts().map((el) => el.textContent)).toEqual(['ok', 'bad']);
    act(() => vi.advanceTimersByTime(4200 - 1100));
    expect(visibleToasts().map((el) => el.textContent)).toEqual(['bad']);
    act(() => vi.advanceTimersByTime(7000 - 4200));
    expect(visibleToasts()).toHaveLength(0);
  });

  it('shows at most four toasts, oldest first, hiding the overflow', () => {
    const show = setup();
    for (const n of [1, 2, 3, 4, 5]) show(`m${n}`);
    expect(visibleToasts().map((el) => el.textContent)).toEqual(['m2', 'm3', 'm4', 'm5']);
  });

  it('runs the action and dismisses the toast', async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    const show = setup();
    show('Deleted', { action: { label: 'Undo', onClick: onUndo } });
    await user.click(screen.getByRole('button', { name: 'Undo' }));
    expect(onUndo).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(visibleToasts()).toHaveLength(0));
  });

  it('dismisses from the close button', async () => {
    const user = userEvent.setup();
    const show = setup();
    show('Saved');
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(visibleToasts()).toHaveLength(0));
  });
});
