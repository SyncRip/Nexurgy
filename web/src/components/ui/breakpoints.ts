/** Token 2.0 breakpoints (px), mobile-first `min-width`. CSS custom properties
 *  cannot be used inside `@media`, so this is the single source for JS checks;
 *  keep in sync with the comment in styles/tokens.css. */
export const breakpoints = {
  sm: 640,
  md: 1024,
  lg: 1280,
  xl: 1600,
} as const;

export type Breakpoint = keyof typeof breakpoints;

/** `(min-width: Npx)` query string for `matchMedia`. */
export function minWidthQuery(bp: Breakpoint): string {
  return `(min-width: ${breakpoints[bp]}px)`;
}
