import type { ModelConfig } from '../../api/types';

export function probePending(config: ModelConfig, now = Date.now()): boolean {
  const p = config.probe;
  return !!p && (p.status === 'queued' || p.status === 'running') && (!p.expires_at || Date.parse(p.expires_at) > now);
}

/** A poll started for an earlier attempt must not overwrite a new attempt or an edited connection. */
export function acceptProbeUpdate(current: ModelConfig, next: ModelConfig, attempt: string | null): ModelConfig {
  if (next.id !== current.id || (next.config_version ?? 0) < (current.config_version ?? 0)) return current;
  if ((current.probe?.id ?? null) !== attempt) return current;
  if (next.config_version === current.config_version && next.probe?.id === current.probe?.id && current.probe && next.probe && ['passed', 'failed', 'interrupted'].includes(current.probe.status) && ['queued', 'running'].includes(next.probe.status)) return current;
  return next;
}
