import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { AssemblyRevision, Scene, SceneNode } from '../../api/assemblies';
import { I18nProvider } from '../../i18n/I18nProvider';
import { StructureTab } from './StructureTab';
import { placementMatrix } from './model';

vi.mock('./AssemblyViewport', () => ({ AssemblyViewport: () => h('div', { className: 'viewport' }) }));
const matrix = placementMatrix([0, 0, 0], [0, 0, 0]);
function node(path: string[], kind: 'assembly' | 'part', children: SceneNode[] = []): SceneNode {
  return { path, instance_id: path[path.length - 1], name: path.join(' / '), children, transform: matrix, world: matrix, suppressed: false, fixed: false,
    definition: { kind, id: kind === 'assembly' ? 'child-a2' : 'part-r1', assembly_id: kind === 'assembly' ? 'child' : undefined, number: 'P', name: 'Saved definition', label: kind === 'assembly' ? 'A2' : 'R1' } } as SceneNode;
}
function render(tree: SceneNode[]) {
  const rev = { id: 'root-a3', assembly_id: 'root', assembly: { id: 'root', number: 'ASM', name: 'Saved root' }, label: 'A3', components: tree, solve: {} } as unknown as AssemblyRevision;
  const scene: Scene = { revision_id: rev.id, tree, definitions: {}, occurrences: [] };
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(StructureTab, { rev, scene, projectId: 'project', canEdit: false,
    edit: { busy: false, dirty: false, beginDraft: () => () => {}, submit: vi.fn(), dialog: null } }))));
}

describe('saved assembly hierarchy presentation', () => {
  it('displays the saved root and the real nested occurrence paths, including reused child ids', () => {
    const html = render([
      node(['front'], 'assembly', [node(['front', 'wheel'], 'part')]),
      node(['rear'], 'assembly', [node(['rear', 'wheel'], 'part')]),
    ]);
    expect(html).toContain('Saved root');
    expect(html).toContain('Total assembly');
    expect(html.match(/role="treeitem"/g)).toHaveLength(5);
    expect(html.match(/aria-level="1"/g)).toHaveLength(1);
    expect(html.match(/aria-level="2"/g)).toHaveLength(2);
    expect(html.match(/aria-level="3"/g)).toHaveLength(2);
    expect(html).toContain('data-instance-path="front/wheel"');
    expect(html).toContain('data-instance-path="rear/wheel"');
    expect(html.match(/>Subassembly<\/span>/g)).toHaveLength(2);
    expect(html.match(/>Part<\/span>/g)).toHaveLength(2);
  });

  it('keeps historical flat structures flat rather than grouping parts by name', () => {
    const html = render([node(['wheel-left'], 'part'), node(['wheel-right'], 'part')]);
    expect(html.match(/role="treeitem"/g)).toHaveLength(3);
    expect(html.match(/aria-level="2"/g)).toHaveLength(2);
    expect(html).not.toContain('aria-level="3"');
    expect(html).not.toContain('>Subassembly</span>');
    expect(html).toContain('aria-posinset="2" aria-setsize="2"');
  });
});
