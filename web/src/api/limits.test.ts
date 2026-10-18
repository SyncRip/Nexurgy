import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LimitedTextarea } from '../components/ui/LimitedText';
import { setCapabilitiesIdentity } from './capabilities';
import { inputLimit, loadLimits, resetLimitsForTest } from './limits';
import type { Capabilities, SystemLimits } from './types';

const limits: SystemLimits = {
  inputs: { chat: 8100, material: 1_900_000, description: 3210, long_note: 8200, objective: 9900, role_prompt: 20100, intake_hints: 3900, drawing_text: 19900, drafting_text: 1999, drafting_mtext: 7999,
    team_display_name: 75, team_profile: 5900, team_tone: 290, team_personality: 1400, team_skill_name: 90, team_skill_description: 490, team_skill_instructions: 3900, team_total: 47000 },
  context: {},
};

vi.mock('./endpoints', () => ({
  getCapabilities: vi.fn(async () => ({ limits }) as unknown as Capabilities),
}));

beforeEach(() => setCapabilitiesIdentity('limits-test'));
afterEach(() => resetLimitsForTest());

describe('S2 input limits', () => {
  it('maxLength comes from /system/capabilities, never from a copy in the frontend', async () => {
    // Unknown yet: no client-side limit (the server still validates).
    expect(renderToStaticMarkup(createElement(LimitedTextarea, { limit: 'description' })).toLowerCase()).not.toContain('maxlength');
    expect(inputLimit(await loadLimits(), 'chat')).toBe(8100);
    expect(renderToStaticMarkup(createElement(LimitedTextarea, { limit: 'description' }))).toContain('maxLength="3210"');
  });
});
