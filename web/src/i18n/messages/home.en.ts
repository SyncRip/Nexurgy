// English messages for the workbench (/home) and the parts list (/parts),
// aligned with the R1.2 prototype (#home, #parts). Merged into en.ts; keys must match home.zh.ts.
export const homeEn = {
  // greeting summary
  'home.hello.pending_one': '{n} item waits for you',
  'home.hello.pending_other': '{n} items wait for you',
  'home.hello.none': 'Nothing waits for you',
  'home.hello.tasks_one': '{n} task in your hands',
  'home.hello.tasks_other': '{n} tasks in your hands',
  'home.hello.project': 'Project {code}',

  // one-line AI command
  'home.cmd.label': 'Describe what AI should do',
  'home.cmd.placeholder': 'Describe a part and AI models it, reads its drawing or drafts the task. For example: a 6061 bearing bracket, base 140×60×10, two Ø32 bearing holes 39 apart…',
  'home.cmd.quick': 'Ways to start',
  'home.cmd.model': 'AI modeling',
  'home.cmd.drawing': 'Recognise a drawing',
  'home.cmd.template': 'New workflow from a template…',
  'home.cmd.continue': 'Continue conversation',
  'home.cmd.hint': 'Enter to send · Shift+Enter for a new line',
  'home.cmd.start': 'Start',

  // upload drawing / STEP
  'home.upload': 'Upload drawing / STEP',
  'home.upload.short': 'Import',
  'home.upload.pick': 'Choose a drawing or STEP file',
  'home.upload.busy': 'Uploading {name}…',
  'home.upload.toAi': '{name} uploaded and added to the AI as its target',
  'home.upload.stored': '{name} is in the project files. Import it into a part from the part’s model tab.',
  'home.upload.openFiles': 'Open files',
  'home.upload.failed': 'Upload failed: {reason}',

  // part cards
  'home.card.noModel': 'No model',
  'home.card.noPreview': 'No preview available yet',
  'home.card.previewAlt': 'Drawing of {number} {rev} (iso view)',
  'home.card.modelPreviewAlt': '3D model of {number} {rev} (iso view)',
  'home.card.loadingPreview': 'Loading the part preview',
  'home.card.noRun': 'No workflow run yet',
  'home.card.unknownRun': 'Older runs not loaded',
  'home.card.progress': '{done} of {total} steps done',
  'home.state.none': 'Not run',
  'home.state.unknown': 'Unknown',
  'home.state.queued': 'Queued',
  'home.state.running': 'Running',
  'home.state.waiting': 'Waiting for people',
  'home.state.failed': 'Failed',
  'home.state.cancelled': 'Cancelled',
  'home.state.done': 'Done',

  // parts list
  'home.parts.sub': 'Manage parts, models and manufacturing progress.',
  'home.parts.results': '{n} parts shown',
  'home.parts.all': 'All parts',
  'home.parts.emptyBody': 'Create a part or upload a drawing to start its engineering record.',
  'home.parts.noMatch': 'No parts match',
  'home.parts.noMatchBody': 'Try another filter, or create a part.',
  'home.parts.windowFull': 'Only the latest {n} runs were read; parts without a run among them show “Unknown”.',
  'home.filter.label': 'Filter by workflow state',
  'home.filter.all': 'All',
  'home.filter.attention': 'Needs attention',
  'home.filter.running': 'Running',
  'home.filter.done': 'Done',
  'home.view.label': 'View',
  'home.view.cards': 'Cards',
  'home.view.table': 'Table',
  'home.col.flow': 'Workflow',

  // waiting for me
  'home.todo.all': 'All {n}',
  'home.todo.open': 'Open',
  'home.todo.type.acceptance': 'Acceptance',
  'home.todo.type.approval': 'Approval',
  'home.todo.by': 'from {name}',

  // my tasks
  'home.tasks.board': 'Task board',
  'home.tasks.noRun': 'No workflow run linked yet',
  'home.tasks.reviewer': 'Acceptance: {name}',
  'home.tasks.due': 'Due {date}',

  // full AI workspace
  'home.back.workbench': 'Back to workbench',
  'home.back.page': 'Back',
} as const;

export type HomeKey = keyof typeof homeEn;
