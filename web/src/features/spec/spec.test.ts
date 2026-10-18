import { describe, expect, it } from 'vitest';
import { isUsable, specPath } from '../../api/spec';
import { factStatusLook, fmtValue, fmtXyz } from './common';

describe('engineering spec helpers', () => {
  it('formats values without inventing a unit or a number', () => {
    expect(fmtValue(60, 'mm')).toBe('60 mm');
    expect(fmtValue('Q235A', null)).toBe('Q235A');
    expect(fmtValue(null, 'mm')).toBe('?');
    expect(fmtValue(undefined)).toBe('?');
    expect(fmtValue({ a: 1 })).toBe('{"a":1}');
    expect(fmtXyz([-40, 0, 3.12345])).toBe('-40, 0, 3.123');
    expect(fmtXyz(null)).toBe('');
  });

  it('only asserted / derived facts get the check mark; unrecognised statuses stay neutral', () => {
    expect(factStatusLook('asserted')).toEqual({ tone: 'ok', icon: 'check' });
    expect(factStatusLook('derived').icon).toBe('check');
    expect(factStatusLook('unknown')).toEqual({ tone: 'stale', icon: 'help' });
    expect(factStatusLook('conflicting').icon).toBe('alert');
    expect(factStatusLook('assumed').icon).toBe('slash');
    expect(factStatusLook('verified_by_ai')).toEqual({ tone: 'pending', icon: 'help' });
    expect(factStatusLook('').icon).toBe('help');
  });

  it('only finished versions are usable; paths pin the exact version', () => {
    expect(isUsable('ready')).toBe(true);
    expect(isUsable('needs_answers')).toBe(true);
    expect(isUsable('extracting')).toBe(false);
    expect(isUsable('failed')).toBe(false);
    expect(specPath('s1')).toBe('/specs/s1');
    expect(specPath('s1', 'v2')).toBe('/specs/s1?version=v2');
  });
});
