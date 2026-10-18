import { request } from './client';
import type { ModelConfig,UUID } from './types';

type Opt = { signal?: AbortSignal };
export interface AccountPreferences { ai_autonomy: 'auto' | 'step'; version: number }
export const getPreferences = (o: Opt = {}) => request<AccountPreferences>('/auth/preferences', { signal: o.signal });
export const updatePreferences = (body: { ai_autonomy: AccountPreferences['ai_autonomy']; expected_version: number }, o: Opt = {}) =>
  request<AccountPreferences>('/auth/preferences', { method: 'PATCH', body, signal: o.signal });
export const getModelConfig = (id: UUID, o: Opt = {}) => request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}`, { signal: o.signal });
export const probeModelConfig = (id: UUID, version: number, o: Opt = {}) =>
  request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/probe`, { method: 'POST', body: { expected_config_version: version }, signal: o.signal });
