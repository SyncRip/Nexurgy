// @vitest-environment jsdom
// Overlays opened over a React Flow canvas must keep Delete away from it:
// XYFlow listens on document and only skips targets inside `.nokey`.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ReactFlow, ReactFlowProvider, type Node } from '@xyflow/react';
import { useState, type ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n/I18nProvider';
import { ConfirmDialog } from './Dialog';
import { Drawer } from './Drawer';
import { Menu } from './Menu';
import { Select } from './Select';

function Canvas({ overlay }: { overlay: (close: () => void) => ReactNode }) {
  // Opened from a button after mount, like the editor's "Archive" menu item.
  const [open, setOpen] = useState(false);
  // Pre-selected: clicking a node in jsdom trips d3-drag (event.view is null).
  const nodes: Node[] = [{ id: 'a', position: { x: 0, y: 0 }, data: { label: 'A' }, width: 120, height: 40, selected: true }];
  return (
    <div style={{ width: 800, height: 600 }}>
      <ReactFlowProvider>
        <ReactFlow defaultNodes={nodes} onNodesDelete={onNodesDelete} deleteKeyCode={['Backspace', 'Delete']} />
      </ReactFlowProvider>
      <button type="button" onClick={() => setOpen(true)}>
        Open overlay
      </button>
      {open && overlay(() => setOpen(false))}
    </div>
  );
}

const onNodesDelete = vi.fn();

async function setup(overlay: (close: () => void) => ReactNode) {
  onNodesDelete.mockClear();
  const user = userEvent.setup();
  render(
    <I18nProvider>
      <Canvas overlay={overlay} />
    </I18nProvider>,
  );
  await user.click(screen.getByRole('button', { name: 'Open overlay' }));
  return user;
}

describe('overlays over a React Flow canvas', () => {
  it('ConfirmDialog swallows Delete; after closing, Delete reaches the canvas', async () => {
    const user = await setup((close) => <ConfirmDialog title="Archive?" body="Sure?" confirmLabel="Archive" onConfirm={close} onClose={close} />);
    const dialog = await screen.findByRole('alertdialog');
    // jsdom alone does not reproduce the browser deleting the node here, so pin the marker too.
    expect(dialog.classList.contains('nokey')).toBe(true);
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Delete}');
    expect(onNodesDelete).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
    await user.keyboard('{Delete}');
    await waitFor(() => expect(onNodesDelete).toHaveBeenCalledTimes(1));
  });

  it('Drawer swallows Delete', async () => {
    const user = await setup((close) => (
      <Drawer title="Inspector" onClose={close}>
        <p>Body</p>
      </Drawer>
    ));
    const drawer = await screen.findByRole('dialog');
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Delete}');
    expect(onNodesDelete).not.toHaveBeenCalled();
  });

  it('an open Menu swallows Delete', async () => {
    const user = await setup(() => <Menu label="More" trigger="…" items={[{ id: 'x', label: 'Archive', onSelect: () => {} }]} />);
    await user.click(screen.getByRole('button', { name: 'More' }));
    const menu = await screen.findByRole('menu');
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Delete}');
    expect(onNodesDelete).not.toHaveBeenCalled();
  });

  it('an open Select swallows Delete', async () => {
    const user = await setup(() => <Select aria-label="Unit" options={[{ value: 'mm', label: 'mm' }, { value: 'in', label: 'in' }]} value="mm" onChange={() => {}} />);
    await user.click(screen.getByRole('combobox', { name: 'Unit' }));
    const list = await screen.findByRole('listbox');
    await waitFor(() => expect(list.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Delete}');
    expect(onNodesDelete).not.toHaveBeenCalled();
  });
});
