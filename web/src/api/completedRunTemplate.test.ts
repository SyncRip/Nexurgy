import { afterEach, expect, it, vi } from 'vitest';
import { createTemplateFromRun } from './endpoints';

afterEach(() => vi.unstubAllGlobals());

it.each([201, 200])('keeps completed-run conversion data out of the request (%s)', async (status) => {
  const draft = { id: 'new-template', part_id: null, name: 'Reusable workflow', graph: { nodes: [], edges: [], groups: [] } };
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(new Response(JSON.stringify(draft), { status }));
  vi.stubGlobal('fetch', fetch);
  // An expanded caller object must not turn this endpoint into another raw graph-copy path.
  const body = {
    name: 'Reusable workflow',
    client_request_id: '35fedba0-42b5-4715-a11f-d32088769c5b',
    description: 'PRIVATE_SOURCE_DESCRIPTION',
    part_id: 'PRIVATE_SOURCE_PART',
    graph: { nodes: [{ params: { text: 'PRIVATE_SOURCE_PROMPT' } }] },
  };

  await expect(createTemplateFromRun('selected-run', body)).resolves.toEqual(draft);

  const [url, options] = fetch.mock.calls[0];
  expect(new URL(String(url)).pathname).toBe('/api/v1/runs/selected-run/template');
  expect(options?.method).toBe('POST');
  expect(JSON.parse(String(options?.body))).toEqual({ name: body.name, client_request_id: body.client_request_id });
  expect(String(options?.body)).not.toContain('PRIVATE_SOURCE');
});
