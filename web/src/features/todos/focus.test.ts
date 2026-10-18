import { describe, expect, it } from 'vitest';
import { todoHref, type TodoItem } from '../../api/todos';
import { focusIds } from './focus';

const todo = (href: string, question_ids?: string[]) => ({ location: { href, question_ids } } as TodoItem);

describe('todo location and exact focus', () => {
  it('keeps exact versions and safely encodes question IDs in app links', () => {
    expect(todoHref(todo('/specs/spec-1?version=v1#question', ['q1', 'q2']))).toBe('/specs/spec-1?version=v1&questions=q1%2Cq2#question');
    expect(todoHref(todo('/assistant/c1?operation=op1&step=3'))).toBe('/assistant/c1?operation=op1&step=3');
  });
  it('rejects external, protocol-relative and browser-normalized escape URLs', () => {
    for (const href of ['https://example.com', '//example.com/path', '/\\example.com', '/\n/example.com', 'javascript:alert(1)', '/a b']) expect(todoHref(todo(href))).toBeNull();
  });
  it('bounds focus IDs and treats them as values rather than selector fragments', () => {
    expect(focusIds('q1,q2,q1', true)).toEqual(['q1', 'q2']);
    expect(focusIds('a"], [data-secret="b')).toEqual(['a"], [data-secret="b']);
    expect(focusIds('q1\nq2', true)).toEqual([]);
    expect(focusIds('x'.repeat(4001))).toEqual([]);
    expect(focusIds(Array.from({ length: 80 }, (_, i) => `q${i}`).join(','), true)).toHaveLength(50);
  });
});
