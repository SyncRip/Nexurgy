// Contrast guard. Every body-text / background pairing in both themes must
// clear WCAG AA for normal text (4.5:1). Parsed straight from tokens.css, so a
// future palette tweak that dims text or a surface trips this test instead of
// shipping an unreadable combination. Pure arithmetic — no DOM needed.
import { describe, expect, it } from 'vitest';
// This test runs only under Vitest (Node), so it reads tokens.css from disk.
// @types/node is intentionally absent (app code stays browser-only), hence the
// ts-expect-error on these Node-builtin imports — they resolve fine at runtime.
// @ts-expect-error no @types/node in this browser-only project
import { readFileSync } from 'node:fs';
// @ts-expect-error no @types/node in this browser-only project
import { fileURLToPath } from 'node:url';

const css: string = readFileSync(fileURLToPath(new URL('./tokens.css', import.meta.url)), 'utf8');

/** Hex custom properties declared in the first rule whose selector contains `sel`. */
function themeVars(sel: string): Record<string, string> {
  const open = css.indexOf('{', css.indexOf(sel));
  const body = css.slice(open + 1, css.indexOf('}', open));
  const vars: Record<string, string> = {};
  for (const line of body.split('\n')) {
    const m = line.match(/^\s*(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;/);
    if (m) vars[m[1]] = m[2];
  }
  return vars;
}

function channels(hex: string): [number, number, number] {
  let h = hex.slice(1);
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number];
}

/** Relative luminance per WCAG 2.x. */
function luminance(hex: string): number {
  const toLinear = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = channels(hex);
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

function contrast(fg: string, bg: string): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

const THEMES = [
  { name: 'dark', selector: ":root[data-theme='dark']" },
  { name: 'light', selector: ":root[data-theme='light']" },
];
const BACKGROUNDS = ['--bg', '--bg-2', '--surface', '--surface-2', '--surface-3'];
const BODY_TEXT = ['--text', '--text-2', '--text-3'];
const AA_NORMAL = 4.5;

describe('tokens.css text/background contrast (WCAG AA)', () => {
  for (const { name, selector } of THEMES) {
    const vars = themeVars(selector);
    for (const text of BODY_TEXT) {
      it(`${name}: ${text} clears ${AA_NORMAL}:1 on every surface`, () => {
        for (const bg of BACKGROUNDS) {
          expect(contrast(vars[text], vars[bg]), `${text} on ${bg} (${name})`).toBeGreaterThanOrEqual(AA_NORMAL);
        }
      });
    }
    it(`${name}: primary-button ink clears ${AA_NORMAL}:1 on the accent fill`, () => {
      expect(contrast(vars['--accent-ink'], vars['--accent'])).toBeGreaterThanOrEqual(AA_NORMAL);
    });
  }
});
