// Locale-aware formatting via Intl. Pure functions; the provider binds the locale.

export interface Formatters {
  date: (iso: string | null | undefined) => string;
  dateTime: (iso: string | null | undefined) => string;
  time: (iso: string | Date | null | undefined) => string;
  relative: (iso: string | null | undefined, now?: number) => string;
  number: (n: number | null | undefined, opts?: Intl.NumberFormatOptions) => string;
  bytes: (n: number | null | undefined) => string;
}

const DASH = '—';

function toDate(v: string | Date | null | undefined): Date | null {
  if (!v) return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

const RELATIVE_STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 7],
  ['week', 4.34524],
  ['month', 12],
  ['year', Number.POSITIVE_INFINITY],
];

export function makeFormatters(locale: string): Formatters {
  const dateFmt = new Intl.DateTimeFormat(locale, { dateStyle: 'medium' });
  const dateTimeFmt = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' });
  const timeFmt = new Intl.DateTimeFormat(locale, { timeStyle: 'short' });
  const rel = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  return {
    date: (iso) => {
      const d = toDate(iso);
      return d ? dateFmt.format(d) : DASH;
    },
    dateTime: (iso) => {
      const d = toDate(iso);
      return d ? dateTimeFmt.format(d) : DASH;
    },
    time: (iso) => {
      const d = toDate(iso);
      return d ? timeFmt.format(d) : DASH;
    },
    relative: (iso, now = Date.now()) => {
      const d = toDate(iso);
      if (!d) return DASH;
      let delta = (d.getTime() - now) / 1000;
      // Anything older than ~a month reads better as a date.
      if (Math.abs(delta) > 60 * 60 * 24 * 30) return dateFmt.format(d);
      for (const [unit, size] of RELATIVE_STEPS) {
        if (Math.abs(delta) < size) return rel.format(Math.round(delta), unit);
        delta /= size;
      }
      return dateFmt.format(d);
    },
    number: (n, opts) => (n === null || n === undefined || Number.isNaN(n) ? DASH : new Intl.NumberFormat(locale, opts).format(n)),
    bytes: (n) => {
      if (n === null || n === undefined || Number.isNaN(n)) return DASH;
      const units = ['byte', 'kilobyte', 'megabyte', 'gigabyte'] as const;
      let v = n;
      let i = 0;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
      }
      return new Intl.NumberFormat(locale, { style: 'unit', unit: units[i], unitDisplay: 'short', maximumFractionDigits: i === 0 ? 0 : 1 }).format(v);
    },
  };
}
