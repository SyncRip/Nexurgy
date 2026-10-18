// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { EmptyState, ErrorState } from './States';

function renderEn(ui: ReactNode) {
  localStorage.setItem('partroute.lang', 'en');
  return render(<I18nProvider>{ui}</I18nProvider>);
}

describe('EmptyState', () => {
  it('renders the legacy markup when no kind is given', () => {
    const { container } = renderEn(<EmptyState title="No parts yet" body="Create one to start." action={<button type="button">New part</button>} />);
    const root = container.querySelector('.empty-state')!;
    expect(root.getAttribute('data-kind')).toBe('empty');
    expect(root.hasAttribute('role')).toBe(false);
    expect(root.querySelector('.bp-mark')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'No parts yet' })).toBeTruthy();
    expect(screen.getByText('Create one to start.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'New part' })).toBeTruthy();
  });

  it.each([
    ['no-result', 'No results', 'Try other keywords or clear the filters.'],
    ['no-permission', 'You don’t have access', 'Ask a project admin to grant you access.'],
    ['disabled', 'This feature is turned off', 'An admin can turn it on in Settings.'],
  ] as const)('gives %s its own mark and default copy', (kind, title, body) => {
    const { container } = renderEn(<EmptyState kind={kind} />);
    expect(container.querySelector('.pr-empty-mark svg')).toBeTruthy();
    expect(container.querySelector('.bp-mark')).toBeNull();
    expect(screen.getByRole('heading', { name: title })).toBeTruthy();
    expect(screen.getByText(body)).toBeTruthy();
  });

  it('lets callers override or drop the default copy', () => {
    renderEn(<EmptyState kind="no-result" title="No parts match “M8”" body={null} />);
    expect(screen.getByRole('heading', { name: 'No parts match “M8”' })).toBeTruthy();
    expect(screen.queryByText('Try other keywords or clear the filters.')).toBeNull();
  });

  it('offers clear-filters as the next step', async () => {
    const user = userEvent.setup();
    const onClearFilters = vi.fn();
    renderEn(<EmptyState kind="no-result" onClearFilters={onClearFilters} />);
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(onClearFilters).toHaveBeenCalledTimes(1);
  });

  it('prefers an explicit action over built-in buttons', () => {
    renderEn(<EmptyState kind="no-result" onClearFilters={() => {}} action={<a href="#/parts">All parts</a>} />);
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull();
    expect(screen.getByRole('link', { name: 'All parts' })).toBeTruthy();
  });

  it('announces the error kind as an alert', () => {
    renderEn(<EmptyState kind="error" />);
    const alert = screen.getByRole('alert');
    expect(alert.className).toContain('error');
    expect(alert.querySelector('.err-mark')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Couldn’t load this.' })).toBeTruthy();
  });
});

describe('ErrorState', () => {
  it('shows the error text and retries', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderEn(<ErrorState error={new Error('boom')} title="Runs failed to load" onRetry={onRetry} />);
    const alert = screen.getByRole('alert');
    expect(alert.querySelector('.error-text')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Runs failed to load' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
