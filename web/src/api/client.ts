// Fetch wrapper for the PartRoute API (design.md §6).
//
// - Base URL: VITE_API_BASE (default http://localhost:8000) + /api/v1.
// - Bearer token from a provider set by AuthProvider (the client never touches
//   localStorage itself, which keeps it testable in node).
// - Every failure becomes an ApiError {status, code, message, details}. The
//   backend's error body is {"error": {"code", "message", "details"}}; a fetch
//   that never reaches the server becomes code "network" with status 0.
// - A 401 on an authenticated request emits a global "unauthorized" event so
//   the AuthProvider can sign the user out.

export const DEFAULT_API_ORIGIN = 'http://localhost:8000';

function readEnvBase(): string | undefined {
  try {
    return (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env?.VITE_API_BASE;
  } catch {
    return undefined;
  }
}

/** Origin of the API server without a trailing slash, e.g. http://localhost:8000. */
export const API_ORIGIN = (readEnvBase() || DEFAULT_API_ORIGIN).replace(/\/+$/, '');
/** Prefix for every versioned endpoint. */
export const API_BASE = `${API_ORIGIN}/api/v1`;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** True when the endpoint (or the resource) does not exist on this server. */
  get isNotFound(): boolean {
    return this.status === 404 || this.status === 405;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export function isAbortError(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: string }).name === 'AbortError';
}

/** Code used when the backend gives no structured error body. */
export function fallbackCode(status: number): string {
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 405) return 'method_not_allowed';
  if (status === 409) return 'conflict';
  if (status === 413) return 'file_too_large';
  if (status === 422) return 'validation_error';
  if (status >= 500) return 'server_error';
  return 'http_error';
}

/** Build an ApiError from a status and an already-read body (text). Pure, unit-tested. */
export function parseErrorBody(status: number, bodyText: string, statusText = ''): ApiError {
  let parsed: unknown = undefined;
  if (bodyText) {
    try {
      parsed = JSON.parse(bodyText);
    } catch {
      parsed = undefined;
    }
  }
  const err = (parsed as { error?: unknown } | undefined)?.error;
  if (err && typeof err === 'object') {
    const e = err as { code?: unknown; message?: unknown; details?: unknown };
    const code = typeof e.code === 'string' && e.code ? e.code : fallbackCode(status);
    const message = typeof e.message === 'string' ? e.message : statusText || code;
    const details = e.details && typeof e.details === 'object' ? (e.details as Record<string, unknown>) : {};
    return new ApiError(status, code, message, details);
  }
  // FastAPI's default {"detail": "..."} shape, just in case a route bypasses the handlers.
  const detail = (parsed as { detail?: unknown } | undefined)?.detail;
  const message = typeof detail === 'string' ? detail : statusText || (bodyText && bodyText.length < 300 ? bodyText : '') || `HTTP ${status}`;
  return new ApiError(status, fallbackCode(status), message, detail && typeof detail !== 'string' ? { detail } : {});
}

// ─── auth plumbing ─────────────────────────────────────────────────────────

type TokenProvider = () => string | null;
let tokenProvider: TokenProvider = () => null;

export function setTokenProvider(fn: TokenProvider): void {
  tokenProvider = fn;
}

type Listener = (err: ApiError) => void;
const unauthorizedListeners = new Set<Listener>();

/** Subscribe to 401 responses on authenticated requests. Returns an unsubscribe function. */
export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

// ─── request ───────────────────────────────────────────────────────────────

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue>;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  query?: Query;
  /** JSON body. */
  body?: unknown;
  /** Multipart body (uploads). Takes precedence over `body`. */
  form?: FormData;
  signal?: AbortSignal;
  /** Path is relative to the API origin instead of /api/v1 (e.g. /healthz). */
  root?: boolean;
  /** Do not send the bearer token. */
  anonymous?: boolean;
  /** Do not emit the global unauthorized event on 401 (login form). */
  quiet401?: boolean;
  /** Response handling. Default: JSON (undefined for 204). */
  as?: 'json' | 'blob' | 'response';
}

export function buildUrl(path: string, query?: Query, root = false): string {
  const base = root ? API_ORIGIN : API_BASE;
  const p = path.startsWith('/') ? path : `/${path}`;
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      qs.append(k, String(v));
    }
  }
  const s = qs.toString();
  return `${base}${p}${s ? `?${s}` : ''}`;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? (opts.body !== undefined || opts.form ? 'POST' : 'GET');
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = opts.anonymous ? null : tokenProvider();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.form) {
    body = opts.form; // the browser sets the multipart boundary
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query, opts.root), { method, headers, body, signal: opts.signal });
  } catch (e) {
    if (isAbortError(e)) throw e;
    throw new ApiError(0, 'network', e instanceof Error ? e.message : 'network request failed');
  }

  if (!res.ok) {
    let text = '';
    try {
      text = await res.text();
    } catch {
      text = '';
    }
    const err = parseErrorBody(res.status, text, res.statusText);
    if (res.status === 401 && token && !opts.quiet401) unauthorizedListeners.forEach((fn) => fn(err));
    throw err;
  }

  if (opts.as === 'response') return res as unknown as T;
  if (opts.as === 'blob') return (await res.blob()) as unknown as T;
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(res.status, 'bad_response', 'the server returned a response that is not JSON');
  }
}

/** Multipart helper: skips undefined / null / empty-string fields. */
export function toFormData(fields: Record<string, string | Blob | null | undefined>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null || v === '') continue;
    if (typeof v !== 'string' && typeof File !== 'undefined' && v instanceof File) fd.append(k, v, v.name);
    else fd.append(k, v);
  }
  return fd;
}

/** Current bearer token (for streaming helpers that call fetch themselves). */
export function currentToken(): string | null {
  return tokenProvider();
}
