// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Popover } from './Popover';

function setup() {
  const user = userEvent.setup();
  render(
    <>
      <Popover label="Filters" trigger="More" align="start">
        {(close) => (
          <div>
            <label>
              Status <input />
            </label>
            <button type="button" onClick={close}>
              apply
            </button>
          </div>
        )}
      </Popover>
      <button type="button">outside</button>
    </>,
  );
  const trigger = screen.getByRole('button', { name: 'Filters' });
  return { user, trigger };
}

describe('Popover', () => {
  it('is a disclosure: aria-expanded toggles, no dialog popup semantics', async () => {
    const { user, trigger } = setup();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-haspopup')).toBe(false);
    await user.click(trigger);
    const panel = await screen.findByRole('region', { name: 'Filters' });
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(panel.className).toContain('popover');
    await user.click(trigger);
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Filters' })).toBeNull());
  });

  it('renders the panel inside its wrapper, right after the trigger', async () => {
    const { user, trigger } = setup();
    await user.click(trigger);
    const panel = await screen.findByRole('region', { name: 'Filters' });
    const wrap = trigger.closest('.pop-wrap');
    expect(wrap?.contains(panel)).toBe(true);
    // Tab from the trigger reaches the panel content first.
    await user.tab();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox')));
  });

  it('does not move focus on open', async () => {
    const { user, trigger } = setup();
    await user.click(trigger);
    await screen.findByRole('region', { name: 'Filters' });
    expect(document.activeElement).toBe(trigger);
  });

  it('passes close to function children', async () => {
    const { user, trigger } = setup();
    await user.click(trigger);
    await user.click(await screen.findByRole('button', { name: 'apply' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Filters' })).toBeNull());
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    const { user, trigger } = setup();
    await user.click(trigger);
    await user.click(await screen.findByRole('textbox'));
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Filters' })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('closes on an outside press', async () => {
    const { user, trigger } = setup();
    await user.click(trigger);
    await screen.findByRole('region', { name: 'Filters' });
    await user.click(screen.getByRole('button', { name: 'outside' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Filters' })).toBeNull());
  });
});
