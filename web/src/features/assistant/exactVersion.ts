/** An explicit immutable version never silently falls back to the current head. */
export function exactVersion<T extends { id: string }>(versions: readonly T[] | undefined, requested: string | null): T | null {
  return requested ? versions?.find((version) => version.id === requested) ?? null : versions?.[0] ?? null;
}

export function shouldFollowResult(shownId: string | null, headId: string | null, dirty: boolean): boolean {
  return !dirty && (!shownId || shownId === headId);
}
