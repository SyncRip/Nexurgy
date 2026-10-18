import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FEATURE_NAMES, loadCapabilities, refreshCapabilities, setCapabilitiesIdentity } from '../../api/capabilities';
import { getCapabilities } from '../../api/endpoints';
import type { Capabilities, FeatureFlags } from '../../api/types';
import { I18nProvider } from '../../i18n/I18nProvider';
import { FeatureGate, FeatureNotice } from './FeatureGate';

vi.mock('../../api/endpoints', () => ({ getCapabilities: vi.fn() }));
const flags = (enabled: boolean) => Object.fromEntries(FEATURE_NAMES.map((name) => [name, enabled])) as FeatureFlags;
const response = (enabled: boolean) => ({ features: flags(enabled) }) as Capabilities;
const renderGate = () => renderToStaticMarkup(createElement(I18nProvider, null,
  createElement(FeatureGate, { feature: 'tasks', children: createElement('button', null, 'Create task') })));

beforeEach(() => { vi.mocked(getCapabilities).mockReset(); setCapabilitiesIdentity('gate-user'); });
afterEach(() => setCapabilitiesIdentity(null));

describe('feature route rendering', () => {
  it('does not render a write surface until the shared capability request succeeds', async () => {
    expect(renderGate()).not.toContain('Create task');
    vi.mocked(getCapabilities).mockResolvedValue(response(true));
    await loadCapabilities();
    expect(renderGate()).toContain('Create task');
    setCapabilitiesIdentity('next-user');
    expect(renderGate()).not.toContain('Create task');
  });

  it('shows history guidance when disabled and reopens the original surface after rollback', async () => {
    vi.mocked(getCapabilities).mockResolvedValue(response(false));
    await loadCapabilities();
    expect(renderGate()).toContain('Tasks is disabled');
    expect(renderGate()).not.toContain('Create task');
    expect(renderToStaticMarkup(createElement(I18nProvider, null, createElement(FeatureNotice, { feature: 'tasks' }))))
      .toContain('Historical record');
    vi.mocked(getCapabilities).mockResolvedValue(response(true));
    refreshCapabilities();
    expect(renderGate()).not.toContain('Create task');
    await loadCapabilities();
    expect(renderGate()).toContain('Create task');
  });

  it('offers retry on capability failure without claiming the feature was disabled', async () => {
    vi.mocked(getCapabilities).mockRejectedValue(new Error('offline'));
    await loadCapabilities();
    const rendered = renderGate();
    expect(rendered).toContain('Retry');
    expect(rendered).not.toContain('Tasks is disabled');
    expect(rendered).not.toContain('Create task');
  });
});
