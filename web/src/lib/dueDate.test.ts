import { describe, expect, it } from 'vitest';
import { dueDay, dueDayForDisplay, shortDay } from './dueDate';

describe('due date display', () => {
  it('keeps the calendar day whatever the stored time', () => {
    expect(dueDayForDisplay('2026-10-07T00:00:00Z')).toBe('2026-10-07T12:00:00');
    expect(dueDayForDisplay('2026-10-07T12:00:00+00:00')).toBe('2026-10-07T12:00:00');
    expect(new Date(dueDayForDisplay('2026-10-07')).getDate()).toBe(7);
    expect(new Date(dueDayForDisplay('2026-10-07T00:00:00Z')).getDate()).toBe(7);
  });

  it('shortens the day on cards without shifting it', () => {
    const now = new Date(2026, 9, 9, 23, 30);
    expect(shortDay('2026-10-18T00:00:00Z', now)).toBe('10-18');
    expect(shortDay('2027-01-02T12:00:00Z', now)).toBe('2027-01-02');
  });

  it('leaves values without a day alone', () => {
    expect(dueDayForDisplay('garbage')).toBe('garbage');
    expect(dueDay(null)).toBeNull();
    expect(dueDay('2026-10-07T00:00:00Z')).toBe('2026-10-07');
  });
});
