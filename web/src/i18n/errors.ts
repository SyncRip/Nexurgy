// API error → friendly message key. The raw code is always kept so the UI can
// show it small next to the message (useful when reporting problems).

import { isApiError } from '../api/client';
import type { MessageKey } from './en';
import { hasKey } from './core';

export interface DescribedError {
  key: MessageKey;
  /** Server error code, e.g. "project_code_taken"; null for non-API errors. */
  code: string | null;
  /** Server / runtime message in English, for details and logs. */
  raw: string;
  status: number | null;
}

export type ErrorOverrides = Partial<Record<string, MessageKey>>;

export function describeError(err: unknown, overrides: ErrorOverrides = {}): DescribedError {
  if (isApiError(err)) {
    const override = overrides[err.code];
    const key: MessageKey = override ?? (hasKey(`error.${err.code}`) ? (`error.${err.code}` as MessageKey) : err.status >= 500 ? 'error.server_error' : 'error.generic');
    return { key, code: err.code, raw: err.message, status: err.status };
  }
  const raw = err instanceof Error ? err.message : String(err ?? '');
  return { key: 'error.generic', code: null, raw, status: null };
}

/** Field name of a pydantic validation error (loc like ["body", "code"]), if any. */
export function validationFields(err: unknown): string[] {
  if (!isApiError(err) || err.code !== 'validation_error') return [];
  const list = (err.details as { errors?: { loc?: unknown[] }[] }).errors ?? [];
  return list.map((e) => (Array.isArray(e.loc) ? String(e.loc[e.loc.length - 1] ?? '') : '')).filter(Boolean);
}
