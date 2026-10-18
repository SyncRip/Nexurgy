// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Kbd, shortcutText } from './Kbd';
import { StatusChip, type StatusKind } from './StatusChip';

describe('StatusChip', () => {
  it('always renders an icon next to the visible label', () => {
    const statuses: StatusKind[] = ['ok', 'warn', 'err', 'running', 'pending', 'stale', 'info'];
    render(
      <>
        {statuses.map((s) => (
          <StatusChip key={s} status={s} label={`label-${s}`} />
        ))}
      </>,
    );
    for (const s of statuses) {
      const chip = screen.getByText(`label-${s}`).parentElement!;
      expect(chip.getAttribute('data-status')).toBe(s);
      expect(chip.className).toContain(`pr-status--${s}`);
      const icon = chip.querySelector('svg');
      expect(icon?.getAttribute('aria-hidden')).toBe('true');
    }
  });

  it('exposes only the label as text and keeps the title hint', () => {
    render(<StatusChip status="stale" label="Out of date" title="Model changed after this run" />);
    const chip = screen.getByTitle('Model changed after this run');
    expect(chip.textContent).toBe('Out of date');
  });
});

describe('Kbd', () => {
  it('renders a single key cap', () => {
    const { container } = render(<Kbd keys="/" />);
    expect(container.innerHTML).toBe('<kbd class="pr-kbd">/</kbd>');
  });

  it('nests key caps for a combination and names Apple glyphs', () => {
    const { container } = render(<Kbd keys={['Mod', 'K']} mac />);
    const outer = container.firstElementChild!;
    expect(outer.className).toBe('pr-kbd-combo');
    expect(outer.querySelectorAll('kbd.pr-kbd')).toHaveLength(2);
    expect(outer.textContent).toBe('⌘CommandK');
    expect(screen.getByText('⌘').getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByText('Command').className).toBe('sr-only');
  });

  it('uses plain names off Apple platforms', () => {
    const { container } = render(<Kbd keys={['Mod', 'Shift', 'P']} mac={false} />);
    expect(container.textContent).toBe('CtrlShiftP');
  });

  it('formats shortcut text for titles', () => {
    expect(shortcutText(['Mod', 'K'], true)).toBe('⌘K');
    expect(shortcutText(['Mod', 'K'], false)).toBe('Ctrl+K');
    expect(shortcutText('Esc', false)).toBe('Esc');
  });
});
