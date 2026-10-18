// Shared Vitest setup. Runs in both the node and jsdom environments, so every
// DOM-only step is guarded.
import { afterEach } from 'vitest';

if (typeof window !== 'undefined') {
  // Base UI waits for CSS animations before unmounting popups; jsdom has no
  // animations, so tell the library to finish transitions synchronously.
  (globalThis as { BASE_UI_ANIMATIONS_DISABLED?: boolean }).BASE_UI_ANIMATIONS_DISABLED = true;

  // jsdom lacks ResizeObserver (used by Tabs indicator and Drawer).
  if (!('ResizeObserver' in window)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (window as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver = ResizeObserverStub;
  }

  // Unmount rendered trees between tests (Testing Library only auto-cleans
  // when test globals are enabled).
  const { cleanup } = await import('@testing-library/react');
  afterEach(() => cleanup());
}
