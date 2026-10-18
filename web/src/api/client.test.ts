import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, onUnauthorized, request, setTokenProvider } from './client';

function mockFetch(impl: () => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setTokenProvider(() => null);
});

describe('api client error parsing', () => {
  it('turns the server error body into a typed ApiError', async () => {
    mockFetch(async () => new Response(JSON.stringify({ error: { code: 'project_code_taken', message: 'project code X is already used', details: { code: 'X' } } }), { status: 409 }));
    const err = await request('/projects', { method: 'POST', body: { code: 'X' } }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: 'project_code_taken', message: 'project code X is already used', details: { code: 'X' } });
  });

  it('maps a failed fetch to code "network" and a 401 to the global sign-out event', async () => {
    mockFetch(async () => {
      throw new TypeError('Failed to fetch');
    });
    await expect(request('/auth/me')).rejects.toMatchObject({ status: 0, code: 'network' });

    setTokenProvider(() => 'tok');
    const seen: string[] = [];
    const off = onUnauthorized((e) => seen.push(e.code));
    const fetchFn = mockFetch(async () => new Response('', { status: 401 }));
    await expect(request('/auth/me')).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    off();
    expect(seen).toEqual(['unauthorized']);
    expect((fetchFn.mock.calls[0] as unknown as [string, RequestInit])[1].headers).toMatchObject({ Authorization: 'Bearer tok' });
  });
});
