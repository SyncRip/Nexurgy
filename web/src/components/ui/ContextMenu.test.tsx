// @vitest-environment jsdom
// Spike: ContextMenuArea around a real React Flow canvas.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ReactFlow, ReactFlowProvider, type Node } from '@xyflow/react';
import { describe, expect, it, vi } from 'vitest';
import { ContextMenuArea } from './ContextMenu';
import type { MenuItem } from './Menu';

type Target = { kind: 'node'; id: string } | { kind: 'pane' };

function resolveTarget(el: Element): Target | null {
  const node = el.closest<HTMLElement>('.react-flow__node');
  if (node?.dataset.id) return { kind: 'node', id: node.dataset.id };
  return el.closest('.react-flow__pane') ? { kind: 'pane' } : null;
}

function setup() {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  const onNodesDelete = vi.fn();
  const items = (t: Target): MenuItem[] =>
    t.kind === 'node'
      ? [
          { id: 'dup', label: `Duplicate ${t.id}`, onSelect: () => onSelect('dup', t.id) },
          { id: 'del', label: 'Delete', danger: true, separator: true, onSelect: () => onSelect('del', t.id) },
        ]
      : [{ id: 'add', label: 'Add node', onSelect: () => onSelect('add') }];
  const nodes: Node[] = [
    // Pre-selected: clicking a node in jsdom trips d3-drag (event.view is null).
    { id: 'a', position: { x: 0, y: 0 }, data: { label: 'A' }, width: 120, height: 40, selected: true },
    { id: 'b', position: { x: 200, y: 0 }, data: { label: <input aria-label="Node name" /> }, width: 120, height: 40 },
  ];
  render(
    <div style={{ width: 800, height: 600 }}>
      <ReactFlowProvider>
        <ContextMenuArea label="Canvas actions" resolveTarget={resolveTarget} items={items}>
          <ReactFlow defaultNodes={nodes} onNodesDelete={onNodesDelete} deleteKeyCode={['Backspace', 'Delete']} />
        </ContextMenuArea>
      </ReactFlowProvider>
    </div>,
  );
  const nodeEl = (id: string) => document.querySelector<HTMLElement>(`.react-flow__node[data-id="${id}"]`)!;
  return { user, onSelect, onNodesDelete, nodeEl, pane: () => document.querySelector<HTMLElement>('.react-flow__pane')! };
}

describe('ContextMenuArea on a React Flow canvas', () => {
  it('right-click on a node opens that node’s menu and an item acts on it', async () => {
    const { user, onSelect, nodeEl } = setup();
    fireEvent.contextMenu(nodeEl('a'), { clientX: 40, clientY: 20 });
    const menu = await screen.findByRole('menu', { name: 'Canvas actions' });
    expect(menu.className).toContain('nokey');
    await user.click(screen.getByRole('menuitem', { name: 'Duplicate a' }));
    expect(onSelect).toHaveBeenCalledWith('dup', 'a');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
  });

  it('right-click on the pane opens the pane menu', async () => {
    const { pane } = setup();
    fireEvent.contextMenu(pane(), { clientX: 500, clientY: 400 });
    await screen.findByRole('menuitem', { name: 'Add node' });
    expect(screen.queryByRole('menuitem', { name: /Duplicate/ })).toBeNull();
  });

  it('leaves the native menu alone inside text fields', () => {
    setup();
    const input = screen.getByRole('textbox', { name: 'Node name' });
    const notCancelled = fireEvent.contextMenu(input, { clientX: 230, clientY: 20 });
    expect(notCancelled).toBe(true);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('keyboard open (Shift+F10 on a focused node) and Esc return focus to the node', async () => {
    const { user, nodeEl } = setup();
    const node = nodeEl('a');
    node.focus();
    // Keyboard-generated contextmenu has no pointer position.
    fireEvent.contextMenu(node, { clientX: 0, clientY: 0 });
    const menu = await screen.findByRole('menu');
    // Focus moves into the popup; arrows then reach the items.
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Duplicate a'));
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(node));
  });

  it('Delete pressed inside the menu does not delete canvas nodes', async () => {
    const { user, onNodesDelete, nodeEl } = setup();
    fireEvent.contextMenu(nodeEl('a'), { clientX: 40, clientY: 20 });
    const menu = await screen.findByRole('menu');
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Delete}');
    expect(onNodesDelete).not.toHaveBeenCalled();
    expect(nodeEl('a')).not.toBeNull();
    // Control: once the menu is closed the same key does delete.
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await user.keyboard('{Delete}');
    await waitFor(() => expect(onNodesDelete).toHaveBeenCalledTimes(1));
  });

  it('touch long-press opens the menu; moving the finger cancels it', async () => {
    const { nodeEl } = setup();
    fireEvent.pointerDown(nodeEl('a'), { pointerType: 'touch', isPrimary: true, clientX: 40, clientY: 20 });
    fireEvent.pointerMove(nodeEl('a'), { pointerType: 'touch', clientX: 80, clientY: 20 });
    await new Promise((r) => setTimeout(r, 600));
    expect(screen.queryByRole('menu')).toBeNull();

    fireEvent.pointerDown(nodeEl('a'), { pointerType: 'touch', isPrimary: true, clientX: 40, clientY: 20 });
    await screen.findByRole('menuitem', { name: 'Duplicate a' }, { timeout: 1500 });
  });
});
