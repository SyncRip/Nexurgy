import { describe, expect, it } from 'vitest';
import { AI_DIAGNOSTIC_CODES } from '../../api/types';
import { credentialsEn } from '../../i18n/messages/credentials.en';
import { credentialsZh } from '../../i18n/messages/credentials.zh';
import { asDiagnostic } from './AiFailureDetails';

describe('AI failure diagnostics (W1)', () => {
  it('reads a diagnostic from an untyped record and ignores anything else', () => {
    const d = asDiagnostic({
      code: 'auth_failed',
      stage: 'model_call',
      purpose: 'planning',
      config_id: 'c0ffee00-0000-0000-0000-000000000001',
      config_version: 3,
      call_ids: ['a', 7, 'b'],
      retryable: false,
      recovery: ['replace_key', 'test_config'],
      provider_code: 'auth_failed',
    });
    expect(d?.call_ids).toEqual(['a', 'b']);
    expect(d?.config_version).toBe(3);
    expect(asDiagnostic(null)).toBeNull();
    expect(asDiagnostic({ stage: 'resolve' })).toBeNull();
    expect(asDiagnostic('auth_failed')).toBeNull();
  });

  it('has a label for every stable code in both languages', () => {
    for (const code of AI_DIAGNOSTIC_CODES) {
      expect(credentialsEn).toHaveProperty(`diag.code.${code}`);
      expect(credentialsZh).toHaveProperty(`diag.code.${code}`);
    }
  });
});
