// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Plus } from 'lucide-react';
import { describe, expect, it } from 'vitest';
import { IconButton } from './Button';
import { Tooltip, TooltipProvider } from './Tooltip';

function setup() {
  const user = userEvent.setup();
  render(
    <TooltipProvider delay={0}>
      <Tooltip content="Add a part (N)">
        <IconButton label="Add part" icon={<Plus />} />
      </Tooltip>
      <button type="button">elsewhere</button>
    </TooltipProvider>,
  );
  const trigger = screen.getByRole('button', { name: 'Add part' });
  return { user, trigger };
}

describe('Tooltip', () => {
  it('renders the child as the trigger without changing its accessible name', () => {
    const { trigger } = setup();
    expect(trigger.className).toContain('pr-btn');
    expect(screen.queryByText('Add a part (N)')).toBeNull();
  });

  it('opens on keyboard focus and closes on blur', async () => {
    const { user } = setup();
    await user.tab();
    expect(await screen.findByText('Add a part (N)')).toBeTruthy();
    await user.tab();
    await waitFor(() => expect(screen.queryByText('Add a part (N)')).toBeNull());
  });

  it('opens on hover and closes on pointer leave', async () => {
    const { user, trigger } = setup();
    await user.hover(trigger);
    expect(await screen.findByText('Add a part (N)')).toBeTruthy();
    await user.unhover(trigger);
    await waitFor(() => expect(screen.queryByText('Add a part (N)')).toBeNull());
  });

  it('closes on Escape while keeping focus on the trigger', async () => {
    const { user, trigger } = setup();
    await user.tab();
    await screen.findByText('Add a part (N)');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText('Add a part (N)')).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it('does not open when disabled', async () => {
    const user = userEvent.setup();
    render(
      <TooltipProvider delay={0}>
        <Tooltip content="Hint" disabled>
          <button type="button">Target</button>
        </Tooltip>
      </TooltipProvider>,
    );
    await user.tab();
    await user.hover(screen.getByRole('button', { name: 'Target' }));
    expect(screen.queryByText('Hint')).toBeNull();
  });
});
