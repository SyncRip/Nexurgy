/** Nexurgy's geometric N, shared by the product and its public homepage. */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden focusable="false" className="logo-mark">
      <rect width="32" height="32" rx="7" fill="var(--surface-3)" />
      <path d="M7 24V8h4l10 12V8h4v16h-4L11 12v12Z" fill="var(--accent)" />
    </svg>
  );
}

export function Wordmark({ size = 26 }: { size?: number }) {
  return (
    <span className="wordmark">
      <Logo size={size} />
      <span className="wordmark-name">Nexurgy</span>
    </span>
  );
}
