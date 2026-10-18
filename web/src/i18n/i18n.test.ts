import { describe, expect, it } from 'vitest';
import { en } from './en';
import { zh } from './zh';

const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe('i18n dictionaries', () => {
  it('zh has exactly the same keys as en', () => {
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
  });

  it('every message keeps the same {placeholders} in both languages', () => {
    const mismatched = Object.keys(en).filter((k) => {
      const key = k as keyof typeof en;
      return placeholders(en[key]).join(',') !== placeholders(zh[key]).join(',');
    });
    expect(mismatched).toEqual([]);
  });

  it('no message is empty', () => {
    expect(Object.entries(en).filter(([, v]) => !v.trim())).toEqual([]);
    expect(Object.entries(zh).filter(([, v]) => !v.trim())).toEqual([]);
  });
});
