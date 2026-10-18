// Model credential administration (W1, server/app/api/ai.py). Create / edit / test of configurations stay in
// endpoints.ts; keys are write-only: these calls never send or receive one.

import { request } from './client';
import type { ModelConfig, RekeyResult, UUID } from './types';

type Opt = { signal?: AbortSignal };

/**
 * POST /model-configs/{id}/credentials/revoke — revoke one credential version, or (version omitted) every one not
 * revoked yet. Operations already running with a revoked key stop at their next model request.
 */
export const revokeModelCredential = (id: UUID, version: number | null, o: Opt = {}) =>
  request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/credentials/revoke`, { method: 'POST', body: { version }, signal: o.signal });

/** POST /model-credentials/rekey — re-encrypt every stored key with the current master key (counts only). */
export const rekeyModelCredentials = (o: Opt = {}) => request<RekeyResult>('/model-credentials/rekey', { method: 'POST', signal: o.signal });
