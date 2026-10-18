import { describe, expect, it } from 'vitest';
import { applyPatch, enumParam, pageInfo, readPaging, stripTab } from './urlState';

describe('asset center url state', () => {
  it('a filter change resets offset; a paging change keeps the filters', () => {
    const s = new URLSearchParams('tab=resources&kind=formal&offset=100&limit=50');
    expect(applyPatch(s, { kind: 'input' }).toString()).toBe('tab=resources&kind=input&limit=50');
    expect(applyPatch(s, { offset: 150 }).toString()).toBe('tab=resources&kind=formal&offset=150&limit=50');
    // Re-setting the same value is not a change.
    expect(applyPatch(s, { kind: 'formal' }).get('offset')).toBe('100');
    // Empty / false values delete the key.
    expect(applyPatch(s, { kind: '', archived: false }).has('kind')).toBe(false);
    expect(applyPatch(s, { archived: true }).get('archived')).toBe('1');
  });

  it('paging falls back on unusable values and reports them', () => {
    expect(readPaging(new URLSearchParams('limit=20&offset=40'))).toEqual({ limit: 20, offset: 40, invalid: [] });
    expect(readPaging(new URLSearchParams('limit=0&offset=-1'))).toEqual({ limit: 50, offset: 0, invalid: ['limit', 'offset'] });
    expect(readPaging(new URLSearchParams('limit=500'), 50, 100)).toEqual({ limit: 50, offset: 0, invalid: ['limit'] });
    expect(readPaging(new URLSearchParams('offset=abc'))).toMatchObject({ offset: 0, invalid: ['offset'] });
  });

  it('enum params ignore unknown values', () => {
    const kinds = ['input', 'formal'] as const;
    expect(enumParam(new URLSearchParams('kind=formal'), 'kind', kinds)).toEqual({ value: 'formal', invalid: false });
    expect(enumParam(new URLSearchParams('kind=bogus'), 'kind', kinds)).toEqual({ value: '', invalid: true });
    expect(enumParam(new URLSearchParams(''), 'kind', kinds)).toEqual({ value: '', invalid: false });
  });

  it('strips the tab key and computes page numbers', () => {
    expect(stripTab(new URLSearchParams('tab=knowledge&q=x'))).toBe('q=x');
    expect(pageInfo(0, 50, 0)).toEqual({ page: 1, pages: 1 });
    expect(pageInfo(101, 50, 100)).toEqual({ page: 3, pages: 3 });
    expect(pageInfo(10, 50, 500)).toEqual({ page: 1, pages: 1 });
  });
});
