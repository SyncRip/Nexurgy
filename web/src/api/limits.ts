// S2: input limits come from the server (GET /system/capabilities → limits; server/app/ai/limits.py) and are never
// copied here. Until they are known an input has no client-side maxLength; the server validates every request anyway.
// Limits share the authenticated capabilities snapshot with feature gates and service status.

import { loadCapabilities, setCapabilitiesIdentity, useCapabilities } from './capabilities';
import type { InputLimitKey, SystemLimits } from './types';

/** Limits and feature flags always use the same authenticated capability response. */
export async function loadLimits(): Promise<SystemLimits | null> {
  return (await loadCapabilities())?.limits ?? null;
}

export function useLimits(): SystemLimits | null {
  return useCapabilities().data?.limits ?? null;
}

/** maxLength of an input: the server's limit for that entry, undefined while it is not known. */
export function inputLimit(limits: SystemLimits | null, key: InputLimitKey): number | undefined {
  const v = limits?.inputs?.[key];
  return typeof v === 'number' && v > 0 ? v : undefined;
}

/** Tests only: forget the loaded limits. */
export function resetLimitsForTest(): void {
  setCapabilitiesIdentity(null);
}
