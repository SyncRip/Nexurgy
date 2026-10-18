// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { Card, Panel } from './Card';

describe('Card', () => {
  it('maps its states to data attributes and keeps caller ARIA', () => {
    render(
      <Card interactive selected status="err" aria-current="true" data-testid="card">
        Bracket A
      </Card>,
    );
    const card = screen.getByTestId('card');
    expect(card.tagName).toBe('DIV');
    expect(card.className).toContain('pr-card');
    expect(card.hasAttribute('data-interactive')).toBe(true);
    expect(card.hasAttribute('data-selected')).toBe(true);
    expect(card.getAttribute('data-status')).toBe('err');
    expect(card.getAttribute('aria-current')).toBe('true');
  });

  it('omits state attributes when not set', () => {
    render(<Card data-testid="card">Plain</Card>);
    const card = screen.getByTestId('card');
    expect(card.hasAttribute('data-interactive')).toBe(false);
    expect(card.hasAttribute('data-selected')).toBe(false);
    expect(card.hasAttribute('data-status')).toBe(false);
  });

  it('renders as the caller element and stays keyboard-operable', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Card interactive render={<button type="button" />} onClick={onClick} className="extra">
        Open part
      </Card>,
    );
    const button = screen.getByRole('button', { name: 'Open part' });
    expect(button.className).toBe('pr-card pr-card--pad-md extra');
    await user.tab();
    expect(document.activeElement).toBe(button);
    await user.keyboard('{Enter}');
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe('Panel', () => {
  it('is a region named by its title, with actions in the header', () => {
    render(
      <Panel title="Revisions" subtitle="Latest first" actions={<button type="button">Add</button>} level={3}>
        body
      </Panel>,
    );
    const region = screen.getByRole('region', { name: 'Revisions' });
    expect(screen.getByRole('heading', { level: 3, name: 'Revisions' })).toBeTruthy();
    expect(region.querySelector('.pr-panel__actions')?.textContent).toBe('Add');
    expect(screen.getByText('body')).toBeTruthy();
  });

  it('toggles from the title button and keeps the body mounted', async () => {
    const user = userEvent.setup();
    render(
      <Panel collapsible title="Inputs" actions={<button type="button">Reset</button>}>
        <input aria-label="width" defaultValue="40" />
      </Panel>,
    );
    const toggle = screen.getByRole('button', { name: 'Inputs' });
    // The title sits inside the heading; the action is a sibling, not nested.
    expect(toggle.closest('h2')).toBeTruthy();
    expect(toggle.contains(screen.getByRole('button', { name: 'Reset' }))).toBe(false);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    const field = screen.getByRole('textbox', { name: 'width' });
    await user.clear(field);
    await user.type(field, '55');
    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('textbox', { name: 'width' })).toBeNull();

    await user.keyboard('{Enter}');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'width' }).value).toBe('55');
  });

  it('starts closed with defaultOpen={false} and links the toggle to the body once open', async () => {
    const user = userEvent.setup();
    render(
      <Panel collapsible defaultOpen={false} title="Advanced">
        hidden body
      </Panel>,
    );
    const toggle = screen.getByRole('button', { name: 'Advanced' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    // Closed: mounted but hidden (keepMounted); the library adds aria-controls only while open.
    expect(screen.getByText('hidden body').closest('[hidden]')).toBeTruthy();
    await user.click(toggle);
    const controlled = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    expect(controlled?.textContent).toBe('hidden body');
  });

  it('supports controlled open state', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    function Controlled() {
      const [open, setOpen] = useState(false);
      return (
        <Panel
          collapsible
          title="Notes"
          open={open}
          onOpenChange={(next) => {
            onOpenChange(next);
            setOpen(next);
          }}
        >
          notes
        </Panel>
      );
    }
    render(<Controlled />);
    await user.click(screen.getByRole('button', { name: 'Notes' }));
    expect(onOpenChange).toHaveBeenCalledWith(true);
    expect(screen.getByRole('button', { name: 'Notes' }).getAttribute('aria-expanded')).toBe('true');
  });
});
