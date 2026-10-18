// English messages shared by the general mechanical engineering packages (status chips), merged into en.ts.
// Package-specific strings live in their own files (credentials, turbo, spec, geometry, assembly, asmDrawing,
// drafting, orchestration). Keys must match engineering.zh.ts.
export const engineeringEn = {
  'me.policy.standard': 'Standard',
  'me.policy.standardHint': 'Engineering checks, reviews, approvals and acceptance run as usual.',
  'me.policy.turbo': 'Turbo',
  'me.policy.turboHint': 'Runs straight to delivery: missing or conflicting values are assumed and recorded; engineering checks, reviews, approvals and acceptance are skipped.',
  'me.unverified': 'Done · not verified',
  'me.unverified.hint': 'The result has been generated but is not fully verified or reviewed. Inspect the check results for this exact version and review any unconfirmed design values.',
  'me.check.not_requested': 'not checked',
  'me.check.skipped_turbo': 'skipped (Turbo)',
  'me.check.pending': 'checking',
  'me.check.passed': 'passed',
  'me.check.failed': 'check failed',
  'me.check.unknown': 'unknown',
  'me.accept.not_requested': 'No acceptance',
  'me.accept.pending': 'Awaiting acceptance',
  'me.accept.accepted': 'Accepted',
  'me.accept.rejected': 'Rejected',
  'me.stale': 'Out of date',
  'me.stale.hint': 'Built from an older version; regenerate it from the current one. The old version stays readable.',
  'me.rep.nominal': 'Nominal',
  'me.rep.nominal.hint': 'Nominal shape: correct envelope and interfaces, manufacturing detail not modelled.',
  'me.rep.simplified': 'Simplified',
  'me.rep.simplified.hint': 'Simplified representation (e.g. no real thread); see the specification for the real part.',
  'me.soon.title': 'Being built',
  'me.soon.body': 'This workspace is part of the general mechanical engineering round and is not connected yet.',
};

export type EngineeringKey = keyof typeof engineeringEn;
