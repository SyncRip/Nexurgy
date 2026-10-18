// Keyboard key / shortcut hint. `Mod` resolves to ⌘ on Apple platforms and
// Ctrl elsewhere. A combination renders nested <kbd> elements (HTML spec).
// Glyph keys carry a visually hidden name so screen readers don't read "⌘"
// as a symbol. Skin: Kbd.css (`pr-kbd`).

import './Kbd.css';

export function isMacPlatform(): boolean {
  return typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
}

/** Apple glyphs and their spoken names; other platforms use the plain text. */
const MAC_KEYS: Record<string, { glyph: string; name: string }> = {
  Mod: { glyph: '⌘', name: 'Command' },
  Shift: { glyph: '⇧', name: 'Shift' },
  Alt: { glyph: '⌥', name: 'Option' },
  Ctrl: { glyph: '⌃', name: 'Control' },
  Enter: { glyph: '↵', name: 'Return' },
};

const PC_KEYS: Record<string, string> = { Mod: 'Ctrl' };

function toKeyList(keys: string | readonly string[]): readonly string[] {
  return typeof keys === 'string' ? [keys] : keys;
}

/** Plain-text form for title/tooltip strings: "⌘K" on Apple, "Ctrl+K" elsewhere. */
export function shortcutText(keys: string | readonly string[], mac = isMacPlatform()): string {
  const list = toKeyList(keys);
  return mac ? list.map((k) => MAC_KEYS[k]?.glyph ?? k).join('') : list.map((k) => PC_KEYS[k] ?? k).join('+');
}

function KeyCap({ name, mac, className }: { name: string; mac: boolean; className?: string }) {
  const classes = ['pr-kbd', className].filter(Boolean).join(' ');
  const glyph = mac ? MAC_KEYS[name] : undefined;
  if (!glyph) return <kbd className={classes}>{mac ? name : (PC_KEYS[name] ?? name)}</kbd>;
  return (
    <kbd className={classes}>
      <span aria-hidden="true">{glyph.glyph}</span>
      <span className="sr-only">{glyph.name}</span>
    </kbd>
  );
}

export interface KbdProps {
  /** One key ("/", "Esc") or a combination (["Mod", "K"]). */
  keys: string | readonly string[];
  className?: string;
  /** Overrides platform detection (tests, docs). */
  mac?: boolean;
}

export function Kbd({ keys, className, mac = isMacPlatform() }: KbdProps) {
  const list = toKeyList(keys);
  if (list.length === 1) return <KeyCap name={list[0]} mac={mac} className={className} />;
  return (
    <kbd className={['pr-kbd-combo', className].filter(Boolean).join(' ')}>
      {list.map((k) => (
        <KeyCap key={k} name={k} mac={mac} />
      ))}
    </kbd>
  );
}
