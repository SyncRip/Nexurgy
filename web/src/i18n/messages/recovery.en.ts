// English messages for S3 failure recovery ("continue from the checkpoint"), merged into en.ts. Keys must match recovery.zh.ts.
// A failure carries {stage, code, message, retryable, recovery}; `recovery` is one of the codes below and decides the
// action the page offers (agent execution layer design §3.1).
export const recoveryEn = {
  'recovery.continue': 'Continue',
  'recovery.continuing': 'Continuing…',
  'recovery.label': 'What to do',
  'recovery.code.continue_extract': 'Continue the extraction: the chunks already read are kept; only the unread chunks (and, if needed, the small call for open items) are sent.',
  'recovery.code.retry_design': 'Continue the design using the completed extraction; unchanged sources are not extracted again.',
  'recovery.code.re_extract': 'Fix the cause (the sources or the model configuration), then re-extract.',
  'recovery.code.split_sources': 'The material is over the chunk budget of one spec: split it into several specs or remove sources (nothing was sent to the model).',
  'recovery.code.retry_definition': 'Continue: keep the parts already built and retry only the failed or unstarted modeling and placement work.',
  'recovery.code.answer_questions': 'Answer the open questions on the spec page (a new spec version), then run again with that version.',
  'recovery.code.configure_model': 'Configure a usable modeling model in Settings › Models, then start the request again.',
  'recovery.code.fix_spec': 'The parts that could be built are kept; fix what the step reports in the spec and run again.',
  'recovery.code.retry_sheet': 'Continue: the complete sheets are kept; only the failed documents / sheets are drawn again.',
  'recovery.code.run_standard': 'A step asked a question inside a Turbo run, which Turbo never puts to a person: state the missing value in the spec (a new spec version) or run the step again in standard.',
  'recovery.code.replace_source': 'Some referenced material was not read: replace it with a readable file (text / Markdown in UTF-8) or fix it, then run again; the rest of the material is never used alone.',
  'recovery.code.fix_input': 'The material has no part to model: describe the parts (name, sizes, quantity, where they go) or reference material that defines them, then run again.',
  'recovery.disabled': 'Cannot continue: {reason}',
  'recovery.disabled.notRetryable': 'continuing would fail the same way',
  'recovery.disabled.already_continued': 'it was already continued',
  'recovery.disabled.overlay': 'a layout version cannot be continued',
  'recovery.disabled.other_generator': 'the drawing generator changed; generate a new package',
  'recovery.continuedAs': 'Continued as {label}',
  'recovery.reused': 'kept',
  'recovery.reusedHint': 'Built by an earlier attempt and still valid: kept without a new model call',
  'recovery.progress': '{built} of {total} built · {reused} kept',
  'recovery.continued': 'Continuing from the failed step',
  'error.not_continuable': 'This failure cannot be continued; follow what to do instead.',
  'error.already_continued': 'This was already continued; open the continuation.',
  'error.spec_not_failed': 'Only a failed extraction can be continued.',
  'error.package_not_continuable': 'This drawing package cannot be continued.',
};

export type RecoveryKey = keyof typeof recoveryEn;
