// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ThemeProvider } from '../../prefs/ThemeProvider';
import UiPlayground from './UiPlayground';

function setup() {
  localStorage.setItem('partroute.lang', 'en');
  render(
    <ThemeProvider>
      <I18nProvider>
        <ToastProvider>
          <UiPlayground />
        </ToastProvider>
      </I18nProvider>
    </ThemeProvider>,
  );
  return userEvent.setup();
}

describe('UiPlayground (/dev/ui)', () => {
  it('renders every primitive section', () => {
    setup();
    expect(screen.getByRole('heading', { level: 1, name: 'UI primitives' })).toBeTruthy();
    for (const title of ['Button / IconButton / Tooltip', 'Tabs / SegmentedControl', 'Field / Select / NumberField', 'Card / Panel', 'EmptyState kinds']) {
      expect(screen.getByRole('heading', { name: title })).toBeTruthy();
    }
  });

  it('switches theme and reduced motion on the document root', async () => {
    const user = setup();
    await user.click(screen.getByRole('button', { name: 'light' }));
    expect(document.documentElement.dataset.theme).toBe('light');
    await user.click(screen.getByRole('button', { name: 'dark' }));
    expect(document.documentElement.dataset.theme).toBe('dark');
    await user.click(screen.getByRole('button', { name: 'reduced' }));
    expect(document.documentElement.dataset.motion).toBe('reduced');
    await user.click(screen.getByRole('button', { name: 'motion' }));
    expect(document.documentElement.dataset.motion).toBeUndefined();
  });

  it('opens the dialog demo and closes it with Escape', async () => {
    const user = setup();
    await user.click(screen.getByRole('button', { name: 'Dialog' }));
    expect(await screen.findByRole('dialog', { name: 'Rename part' })).toBeTruthy();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
