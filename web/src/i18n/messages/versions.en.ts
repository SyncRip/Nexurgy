// English messages for S6 (status and versions, agent execution layer), merged into en.ts. Keys must match
// versions.zh.ts. `ver.*` = the historical-version banner and failure counts; the other prefixes are new values of
// shared vocabularies (a partial run reads failed, a Turbo node whose check failed).
export const versionsEn = {
  'ver.history.title': 'This is a historical version',
  'ver.history.body': 'The link opened the exact version it refers to; it stays as it was. The current version is {current}.',
  'ver.history.bodyNoCurrent': 'The link opened the exact version it refers to; it stays as it was.',
  'ver.history.open': 'Open the current version',
  'ver.missing': 'The version this link refers to is unavailable.',
  'ver.stat.failed': 'Failed',
  'ver.node.checkFailed': 'Check failed: the outputs exist but did not pass their check. See the check results of the outputs; change them and run again.',
  'ver.delivery.failedSources': 'Check failed: {list}',
  'as.runStatus.partial': 'Failed (partial output)',
  'collab.phase.check_failed': 'Check failed',
} as const;

export type VersionsKey = keyof typeof versionsEn;
