// English messages of the resources & knowledge redesign (resExperience): the experience view (all-projects list,
// scope) and the target project / usage of "reference or adopt". Keys must match resExperience.zh.ts.
export const resExperienceEn = {
  'rx.all.noProjects': 'You have no accessible project yet.',
  'rx.all.failedAll': 'Experience could not be loaded for any project',
  'rx.all.empty.title': 'No archived experience yet',
  'rx.all.empty.body': 'When a workflow completes and all its nodes are accepted, it is archived in its own project and listed here with that project.',

  'rx.load.progress': 'Loading experience of each project: {done}/{total}',
  'rx.load.partial': 'Experience loaded for {loaded} of {total} projects; the others failed.',
  'rx.load.failed_one': '{count} project failed to load',
  'rx.load.failed_other': '{count} projects failed to load',
  'rx.load.retryProject': 'Retry {project}',
  'rx.load.truncated': '{project}: only the latest {shown} are shown ({total} in all). Search to narrow the list.',

  'rx.detail.project': 'Source project',

  'rx.adopt.title': 'Reference or adopt this experience',
  'rx.adopt.source': 'Source project',
  'rx.adopt.targetProject': 'Target project',
  'rx.adopt.targetProjectHint': 'Projects where you have the workflow role; the source project by default.',
  'rx.adopt.targetProjectPlaceholder': 'Choose a target project…',
  'rx.adopt.sourceMark': 'source project',
  'rx.adopt.errProject': 'Choose the target project.',
  'rx.adopt.noTasks': 'This project has no open task.',
  'rx.adopt.noTargets': 'You have the workflow role in none of your projects, so you cannot reference or adopt experience.',
  'rx.adopt.node': 'Workflow node',
  'rx.adopt.usage': 'How it is used',
  'rx.adopt.usageReference': 'Reference only',
  'rx.adopt.usageReferenceHint': 'Records the reference; the target task’s inputs do not change.',
  'rx.adopt.usageBaseline': 'Adopt as task input',
  'rx.adopt.usageBaselineHint': 'Puts this exact version into an input slot of the target task once confirmed under the task’s adoption mode.',
  'rx.adopt.noInherit': 'The target task is accepted through its own process; the original task’s acceptance and reviews are not inherited.',
  'rx.adopt.noInheritCross': 'From “{source}” into “{target}”: the target task is accepted through its own process; the source project’s acceptance and reviews are not inherited.',
  'rx.adopt.submitReference': 'Record reference',
  'rx.adopt.into': 'Target project: {project}',
  'rx.adopt.referencedToast': 'Reference to {label} recorded in “{project}”.',
  'rx.adopt.proposedToast': 'Adoption of {label} proposed in “{project}”; waiting for {who} to confirm.',
  'rx.adopt.adoptedToast': '{label} adopted in “{project}”; the target task still needs its own acceptance.',
} as const;

export type ResExperienceKey = keyof typeof resExperienceEn;
