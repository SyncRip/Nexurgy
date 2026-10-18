import { useEffect, useSyncExternalStore } from 'react';
import { getCapabilities } from './endpoints';
import type { Capabilities, FeatureFlags, FeatureName } from './types';

export const FEATURE_NAMES = ['collaboration', 'tasks', 'stages', 'process_modules', 'template_library', 'turbo_choice', 'model_choice'] as const satisfies readonly FeatureName[];

export interface CapabilityState {
  data: Capabilities | null;
  features: FeatureFlags | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: unknown;
  loading: boolean;
  checkedAt: Date | null;
  /** A successful response from a server predating feature switches. */
  legacy: boolean;
  refresh: () => void;
}

/** Missing features is the explicit old-server contract; a partial/malformed map is never treated as enabled. */
export function capabilityFeatures(value: unknown): FeatureFlags {
  if (value !== undefined && (!value || typeof value !== 'object' || FEATURE_NAMES.some((key) => typeof (value as Record<string, unknown>)[key] !== 'boolean'))) {
    throw new Error('Invalid system feature capabilities');
  }
  const result = Object.fromEntries(FEATURE_NAMES.map((key) => [key, value === undefined ? true : (value as FeatureFlags)[key]])) as FeatureFlags;
  result.collaboration = result.collaboration && result.tasks;
  return result;
}

const listeners = new Set<() => void>();
let identity: string | null = null;
let generation = 0;
let pending: { controller: AbortController; promise: Promise<Capabilities | null> } | null = null;
const emptyState = (): CapabilityState => ({ data: null, features: null, status: 'idle', loading: true, error: null, checkedAt: null, legacy: false, refresh: refreshCapabilities });
let current = emptyState();

function publish(next: CapabilityState): void {
  current = next;
  listeners.forEach((listener) => listener());
}

/** Called synchronously at session transitions, before rendering the next authenticated identity. */
export function setCapabilitiesIdentity(next: string | null): void {
  if (next === identity) return;
  identity = next;
  generation += 1;
  pending?.controller.abort();
  pending = null;
  publish(emptyState());
}

export function capabilitiesSnapshot(): CapabilityState {
  return current;
}

export function featureEnabled(feature: FeatureName, state: CapabilityState = current): boolean {
  return state.status === 'ready' && state.features?.[feature] === true;
}

/** Only for a fresh operation's top-level choices. Never use on retries, saved snapshots, or template node policy. */
export function operationChoices(body: object): Record<string, unknown> {
  const result: Record<string, unknown> = { ...body };
  if (!featureEnabled('turbo_choice')) delete result.execution_policy;
  if (!featureEnabled('model_choice')) delete result.model_config_id;
  return result;
}

/** A fresh run may override node parameters; frozen model choices must also be removed at this boundary. */
export function runChoices(body: object): Record<string, unknown> {
  const result = operationChoices(body);
  if (!featureEnabled('model_choice') && result.params && typeof result.params === 'object' && !Array.isArray(result.params)) {
    result.params = Object.fromEntries(Object.entries(result.params).map(([node, value]) => [node,
      value && typeof value === 'object' && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'model_config_id'))
        : value,
    ]));
  }
  return result;
}

/** One response owns service status, limits, and features. Stale identity responses cannot update it. */
export function loadCapabilities(): Promise<Capabilities | null> {
  if (pending) return pending.promise;
  if (current.status === 'ready') return Promise.resolve(current.data);
  if (!identity) return Promise.resolve(null);
  const requestGeneration = generation;
  const controller = new AbortController();
  publish({ ...current, status: 'loading', loading: true, error: null, features: null });
  const promise = getCapabilities({ signal: controller.signal })
    .then((data) => {
      if (controller.signal.aborted || requestGeneration !== generation) return null;
      const features = capabilityFeatures(data.features);
      publish({ data, features, status: 'ready', loading: false, error: null, checkedAt: new Date(), legacy: data.features === undefined, refresh: refreshCapabilities });
      return data;
    })
    .catch((error: unknown) => {
      if (!controller.signal.aborted && requestGeneration === generation) publish({ ...emptyState(), status: 'error', loading: false, error, checkedAt: new Date() });
      return null;
    })
    .finally(() => {
      if (requestGeneration === generation) pending = null;
    });
  pending = { controller, promise };
  return promise;
}

export function refreshCapabilities(): void {
  if (pending) return;
  publish({ ...current, status: 'idle', features: null });
  void loadCapabilities();
}

function onVisible(): void {
  if (!document.hidden) refreshCapabilities();
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0 && typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisible);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisible);
  };
}

export function useCapabilities(): CapabilityState {
  const state = useSyncExternalStore(subscribe, capabilitiesSnapshot, capabilitiesSnapshot);
  useEffect(() => {
    if (state.status === 'idle') void loadCapabilities();
  }, [state]);
  return state;
}

/** Unknown, refreshing and failed capabilities all hide feature-specific write actions. */
export function useFeature(feature: FeatureName): boolean {
  return featureEnabled(feature, useCapabilities());
}
