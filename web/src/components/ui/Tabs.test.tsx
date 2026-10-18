// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { SegmentedControl, Tabs, type SegmentItem, type TabItem } from './Tabs';

type Section = 'overview' | 'routing' | 'history' | 'files';

const tabItems: TabItem<Section>[] = [
  { value: 'overview', label: 'Overview', panel: <p>Overview body</p> },
  { value: 'routing', label: 'Routing', meta: 3, panel: <p>Routing body</p> },
  { value: 'history', label: 'History', disabled: true, panel: <p>History body</p> },
  { value: 'files', label: 'Files', panel: <p>Files body</p> },
];

function ControlledTabs({ onChange = vi.fn(), items = tabItems }: { onChange?: (v: Section) => void; items?: TabItem<Section>[] }) {
  const [value, setValue] = useState<Section>('overview');
  return (
    <Tabs
      aria-label="Part sections"
      items={items}
      value={value}
      onChange={(v) => {
        onChange(v);
        setValue(v);
      }}
    />
  );
}

describe('Tabs', () => {
  it('renders a labelled tablist with aria-selected and a single tab stop', () => {
    render(<ControlledTabs />);
    expect(screen.getByRole('tablist', { name: 'Part sections' })).toBeTruthy();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false', 'false']);
    // Roving tabindex: only the selected tab is in the Tab order.
    expect(tabs.filter((t) => t.tabIndex === 0)).toEqual([tabs[0]]);
    expect(screen.getByRole('tabpanel').textContent).toBe('Overview body');
    expect(screen.getByRole('tab', { name: /Routing/ }).querySelector('.pr-tabs__meta')?.textContent).toBe('3');
  });

  it('moves focus with arrow keys and wraps; disabled tabs are reachable but inert; Enter activates', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledTabs onChange={onChange} />);
    const [overview, routing, history, files] = screen.getAllByRole('tab');
    overview.focus();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(document.activeElement).toBe(routing));
    // Focus alone does not change the selection (URL-synced callers).
    expect(onChange).not.toHaveBeenCalled();
    // Like Menu: disabled tabs stay focusable (aria-disabled) so they are discoverable.
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(document.activeElement).toBe(history));
    expect(history.getAttribute('aria-disabled')).toBe('true');
    await user.keyboard('{Enter}');
    expect(onChange).not.toHaveBeenCalled();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(document.activeElement).toBe(files));
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(document.activeElement).toBe(overview));
    await user.keyboard('{End}');
    await waitFor(() => expect(document.activeElement).toBe(files));
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('files');
    await waitFor(() => expect(files.getAttribute('aria-selected')).toBe('true'));
    expect(screen.getByRole('tabpanel').textContent).toBe('Files body');
  });

  it('selects on click and ignores disabled tabs', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledTabs onChange={onChange} />);
    await user.click(screen.getByRole('tab', { name: 'History' }));
    expect(onChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole('tab', { name: /Routing/ }));
    expect(onChange).toHaveBeenCalledWith('routing');
    expect(screen.getByRole('tabpanel').textContent).toBe('Routing body');
  });

  it('links each tab to its panel', () => {
    render(<ControlledTabs />);
    const panel = screen.getByRole('tabpanel');
    const tab = screen.getByRole('tab', { name: 'Overview' });
    expect(tab.getAttribute('aria-controls')).toBe(panel.id);
    expect(panel.getAttribute('aria-labelledby')).toBe(tab.id);
  });

  it('renders no panels when items carry none (caller-owned content)', () => {
    render(<ControlledTabs items={tabItems.map(({ panel: _panel, ...rest }) => rest)} />);
    expect(screen.queryByRole('tabpanel')).toBeNull();
    expect(screen.getAllByRole('tab')).toHaveLength(4);
  });
});

type View = 'grid' | 'list' | 'board';

const segItems: SegmentItem<View>[] = [
  { value: 'grid', label: 'Grid' },
  { value: 'list', label: 'List' },
  { value: 'board', label: 'Board', disabled: true },
];

function ControlledSeg({ onChange }: { onChange: (v: View) => void }) {
  const [value, setValue] = useState<View>('grid');
  return (
    <SegmentedControl
      aria-label="View"
      items={segItems}
      value={value}
      onChange={(v) => {
        onChange(v);
        setValue(v);
      }}
    />
  );
}

describe('SegmentedControl', () => {
  it('is a labelled group of pressed-state buttons', () => {
    render(<ControlledSeg onChange={vi.fn()} />);
    expect(screen.getByRole('group', { name: 'View' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Grid' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'List' }).getAttribute('aria-pressed')).toBe('false');
  });

  it('switches on click and keeps the active segment pressed when clicked again', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledSeg onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: 'List' }));
    expect(onChange).toHaveBeenCalledWith('list');
    await user.click(screen.getByRole('button', { name: 'List' }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'List' }).getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'Board' }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('moves focus with arrow keys and activates with Space', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledSeg onChange={onChange} />);
    const grid = screen.getByRole('button', { name: 'Grid' });
    const list = screen.getByRole('button', { name: 'List' });
    grid.focus();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(document.activeElement).toBe(list));
    await user.keyboard(' ');
    expect(onChange).toHaveBeenCalledWith('list');
  });
});
