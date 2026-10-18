export function focusIds(value: string | null, multiple = false): string[] {
  if (!value || value.length > 4000 || /[\u0000-\u001f\u007f]/.test(value)) return [];
  const ids = multiple ? value.split(',') : [value];
  return [...new Set(ids.filter((id) => id.length > 0 && id.length <= 200))].slice(0, 50);
}
