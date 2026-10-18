// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Plus } from 'lucide-react';
import { describe, expect, it, vi } from 'vitest';
import { Button, IconButton } from './Button';

describe('Button', () => {
  it('renders a type="button" with variant and size classes', () => {
    render(
      <Button variant="primary" size="sm" icon={<Plus data-testid="ico" />}>
        Create
      </Button>,
    );
    const btn = screen.getByRole('button', { name: 'Create' });
    expect(btn.getAttribute('type')).toBe('button');
    expect(btn.className).toContain('pr-btn--primary');
    expect(btn.className).toContain('pr-btn--sm');
    expect(screen.getByTestId('ico')).toBeTruthy();
  });

  it('defaults to secondary / md and passes type="submit" through', () => {
    render(<Button type="submit">Save</Button>);
    const btn = screen.getByRole('button', { name: 'Save' });
    expect(btn.className).toContain('pr-btn--secondary');
    expect(btn.className).toContain('pr-btn--md');
    expect(btn.getAttribute('type')).toBe('submit');
  });

  it('activates from click, Enter and Space', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Run</Button>);
    const btn = screen.getByRole('button', { name: 'Run' });
    await user.click(btn);
    btn.focus();
    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onClick).toHaveBeenCalledTimes(3);
  });

  it('disabled: natively disabled and inert', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Run
      </Button>,
    );
    const btn = screen.getByRole('button', { name: 'Run' });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    await user.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('loading: keeps focus and name, marks busy and blocks repeat activation', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(<Button onClick={onClick}>Save</Button>);
    const btn = screen.getByRole('button', { name: 'Save' });
    btn.focus();
    rerender(
      <Button onClick={onClick} loading>
        Save
      </Button>,
    );
    // Same element, still focused and still named "Save".
    expect(screen.getByRole('button', { name: 'Save' })).toBe(btn);
    expect(document.activeElement).toBe(btn);
    expect(btn.getAttribute('aria-busy')).toBe('true');
    expect(btn.getAttribute('aria-disabled')).toBe('true');
    expect(btn.hasAttribute('data-loading')).toBe(true);
    await user.keyboard('{Enter}');
    await user.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe('IconButton', () => {
  it('uses the label as the accessible name and renders no visible text', () => {
    render(<IconButton label="Add part" icon={<Plus />} />);
    const btn = screen.getByRole('button', { name: 'Add part' });
    expect(btn.className).toContain('pr-btn--icon-only');
    expect(btn.className).toContain('pr-btn--ghost');
    expect(btn.textContent).toBe('');
  });

  it('requires aria-label for icon-only buttons at the type level', () => {
    // @ts-expect-error iconOnly without aria-label must not compile
    const missingName = <Button iconOnly icon={<Plus />} />;
    // @ts-expect-error IconButton without label must not compile
    const missingLabel = <IconButton icon={<Plus />} />;
    expect(missingName).toBeTruthy();
    expect(missingLabel).toBeTruthy();
  });
});
