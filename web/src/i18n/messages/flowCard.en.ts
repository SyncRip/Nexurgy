// English messages for the conversation's flow cards (.trellis/tasks/archive/2026-10/10-12-ai-work-panel): run and workflow cards,
// the workflow_create plan card, blocking items handled in the chat and results flowing back. Merged into en.ts.
// `fc.*`; keys must match flowCard.zh.ts.
export const flowCardEn = {
  'fc.open': 'Open work panel',
  'fc.loading': 'Loading…',
  'fc.view': 'View',

  // run card
  'fc.run.kind': 'Run',
  'fc.run.workflow': 'Workflow {name} · v{v}',
  'fc.run.workflowVersion': 'Workflow v{v}',
  'fc.run.started': 'Run started: {title}',
  'fc.run.askSummary': 'Ask AI to summarise',
  'fc.run.askSummaryHint': 'Adds this run as a reference and fills in a question; nothing is asked until you send it.',
  'fc.run.askSummaryText': 'Summarise the run "{title}": what it produced, the check results and what is still open.',
  'fc.digest.succeeded': '{name} done',
  'fc.digest.waiting_human': '{name} waiting',
  'fc.digest.running': '{name} running',
  'fc.digest.ready': '{name} queued',
  'fc.digest.failed': '{name} failed',
  'fc.digest.pending': '{name} not started',
  'fc.digest.cancelled': '{name} cancelled',
  'fc.digest.skipped': '{name} skipped',
  'fc.digest.sep': '; ',
  'fc.digest.more': '{n} more…',
  'fc.digest.allDone': 'All {n} steps done',
  'fc.digest.empty': 'No steps yet',
  'fc.outcome.succeeded': 'Run finished: {done} of {total} steps done, {results} results',
  'fc.outcome.failed': 'Run failed: {failed} step(s) failed, {done} of {total} done',
  'fc.outcome.cancelled': 'Run cancelled: {done} of {total} steps done',
  'fc.outcome.took': ' · took {d}',
  'fc.outcome.note': 'A finished run is not an acceptance: reviews and the task rules decide.',

  // blocking items
  'fc.pending.title': 'Needs you ({n})',
  'fc.clar.title': '{name} needs more information',
  'fc.clar.submit': 'Submit and continue',
  'fc.approval.title': '{name} is waiting for review',
  'fc.approval.basis': 'View basis',
  'fc.approval.approve': 'Approve',
  'fc.approval.sendBack': 'Send back',
  'fc.approval.comment': 'Comment',
  'fc.approval.commentHint': 'Optional to approve, required to send back',
  'fc.approval.needComment': 'Add a comment to send it back',
  'fc.manual.title': '{name} needs manual entry',
  'fc.manual.body': 'A person enters the drawing data; the form is in the work panel.',
  'fc.manual.handle': 'Handle in panel',
  'fc.waiting.title': '{name} is waiting for a person',
  'fc.failed.title': '{name} failed',
  'fc.failed.attempts': 'attempt {a}/{m}',
  'fc.failed.retry': 'Retry',
  'fc.failed.noPermission': 'Retrying needs the workflow permission of this project.',

  // results
  'fc.results.title': 'Results ({n})',
  'fc.results.missing': 'This version no longer exists',

  // workflow card
  'fc.wf.kind': 'Workflow',
  'fc.wf.nodes': '{n} nodes',
  'fc.wf.ready': 'Ready to run',
  'fc.wf.problems': '{n} problem(s) to fix before it can run',
  'fc.wf.problemsMore': '{n} more…',
  'fc.wf.archived': 'Archived',
  'fc.wf.openPanel': 'Open in panel',
  'fc.wf.run': 'Run',
  'fc.wf.starting': 'Starting…',
  'fc.wf.runBlocked': 'Fix the problems in the panel first',
  'fc.wf.note': 'A run uses this version; later changes only affect later runs.',

  // workflow_create plan
  'fc.create.pending': 'New workflow · {n} steps',
  'fc.create.created': 'Created {name} v{v}',
  'fc.create.part': 'Part {part}',
  'fc.create.create': 'Create workflow',
  'fc.create.creating': 'Creating…',
  'fc.create.preview': 'Preview in panel',
  'fc.create.open': 'Open',
  'fc.create.added': 'new',
  'fc.create.note': 'Nothing is created until you click Create; it becomes the part’s own workflow.',
  'fc.create.appliedNote': 'Created {when}.',

  // workflow_edit plan
  'fc.edit.viewInPanel': 'View changes in panel',
  'fc.edit.focus': 'For node: {name}',
  'fc.edit.fromRun': 'Changes the workflow the next run uses (v{v}); this run stays unchanged.',
  'fc.warn.run_unchanged': 'This run stays unchanged; only the next run uses the change.',
  'fc.warn.movedOnRun': 'This run used v{v}; the plan is based on the current v{base}.',
  'fc.warn.focus_node_missing': 'The selected node is no longer in the current workflow; the plan covers the whole workflow.',

  // composer
  'fc.composer.unsaved': 'The work panel has unsaved changes to this workflow; sending saves them first.',
  'fc.composer.saveAndSend': 'Save and send',
};

export type FlowCardKey = keyof typeof flowCardEn;
